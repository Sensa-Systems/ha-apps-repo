"""Teltonika REST API client.

Communicates with Teltonika RutOS 7 routers via the REST API at
http://<host>/api. Authenticates with username/password to obtain
a Bearer token, then polls endpoints for router data.

**HTTP stack:** `requests` via `hass.async_add_executor_job`, not aiohttp.
On Python 3.14 + aiohttp (HA 2025.x builds) the aiohttp+aiohappyeyeballs
connect path hangs for the full timeout on literal LAN IPv4 addresses,
even when a raw `asyncio.open_connection` to the same host connects
instantly. Requests goes through urllib3's direct socket path, no happy-
eyeballs, no shared middleware, no pool surprises — proven to work in
the same HA container where aiohttp times out. This choice also isolates
Teltonika traffic from HA's shared aiohttp session entirely.

API docs: https://developers.teltonika-networks.com
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Iterator
from typing import Any

import requests
import urllib3
from homeassistant.core import HomeAssistant

from ..const import TELTONIKA_DEFAULT_SSL

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10  # seconds — healthy router
# When the coordinator has flagged the router as unreachable (consecutive
# failures), per-endpoint waits drop to this so a 30 s poll cycle that
# touches ~5 endpoints doesn't spend a full minute hanging on each. 1.5 s
# is well above the TCP connect + first-byte time of a healthy router
# (~20 ms over LAN) while bounding the worst-case poll wall time.
_REQUEST_TIMEOUT_DEGRADED = 1.5
# The offered-version read is the one endpoint on this API that does not answer
# from the router's own state: `rut_fota` asks Teltonika's servers and the
# request is held open until that round trip finishes. Ten seconds is a budget
# for a LAN answer, and it is not one: observed timing out on every attempt, on
# both a RUTX11 and a TSW202 (192.168.1.226's pair, 2026-09-19), which left the
# Admin card's equipment rows permanently blank. Only this read gets the longer
# budget, and only while the router is healthy -- once the coordinator has
# flagged it unreachable, fail-fast wins.
_REQUEST_TIMEOUT_CLOUD = 45
# Verifying a downloaded image unpacks ~30 MB into /tmp before the router
# answers. Ten seconds timed out on a RUTX11 (2026-10-14, 00.07.24.5 to
# 00.07.25.3) while the router went on to finish the job, so every Update now
# reported failure. Same healthy-only rule as the cloud read.
_REQUEST_TIMEOUT_FIRMWARE = 120
# Making or applying a full backup packs or unpacks the whole config. Not yet
# timed on hardware, so the firmware budget.
_BACKUP_TIMEOUT = _REQUEST_TIMEOUT_FIRMWARE

# Router ships a self-signed cert; suppress urllib3's per-request warning
# rather than logging noise on every poll.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -- FOTA (RutOS firmware over the air) ---------------------------------------
#
# Teltonika's public API docs describe `/firmware/device/status` and nothing
# else on this surface, so every path and payload below was read off a live
# RUTX11 on RUTX_R_00.07.24.3 (2026-09-18) and cross-checked against the
# router's own WebUI bundle (`/www/assets/app.firmware.app-*.js.gz`), which is
# the only complete description of the contract that exists. The ubus service
# behind it is `rut_fota`.
FOTA_UPDATES_PATH = "/firmware/device/updates/status"
FOTA_PROGRESS_PATH = "/firmware/device/progress/status"
FOTA_CONFIG_PATH = "/fota/config"
FOTA_DOWNLOAD_ACTION = "/firmware/actions/fota_download"
FOTA_CANCEL_ACTION = "/firmware/actions/fota_cancel"
FOTA_VERIFY_ACTION = "/firmware/actions/verify"
FOTA_UPGRADE_ACTION = "/firmware/actions/upgrade"

# Teltonika publishes two streams and the router reports both on one read.
# `stable` is what the WebUI offers by default; `latest` is promoted to stable
# after 4-6 weeks with no critical findings (the router's own words).
FOTA_STREAM_STABLE = "stable"
FOTA_STREAM_LATEST = "latest"

# What the version fields say when there is nothing to install. "newest" and
# "Fw_newest" both mean "you are already on the newest in this stream", which is
# a CONFIDENT up-to-date; "N/A" and "" mean the router has no answer, which is
# not. Conflating the two is how a box would report itself current while its
# FOTA client was never able to ask.
_FOTA_NEWEST_MARKERS = ("newest", "fw_newest")
_FOTA_NO_ANSWER = ("", "n/a")

# `process` values on the progress endpoint, observed while a download ran.
FOTA_PROCESS_STARTED = "started"
FOTA_PROCESS_SUCCEEDED = "succeeded"
FOTA_PROCESS_FAILED = "failed"


class TeltonikaApiError(Exception):
    """Base exception for Teltonika API errors."""


class TeltonikaAuthError(TeltonikaApiError):
    """Authentication failed."""


class TeltonikaConnectionError(TeltonikaApiError):
    """Connection to the router failed."""


class TeltonikaHttpError(TeltonikaConnectionError):
    """The router answered with an HTTP error; `status` is the code."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class TeltonikaPermissionError(TeltonikaApiError):
    """The router answered 403: signed in, but this user may not do that.

    Its own type because it is "told no", not "could not ask". A RutOS user
    outside the admin group reads status fine and is refused the firmware
    endpoints, and no amount of retrying changes that until someone widens the
    user's rights on the router.
    """



def _metric_of(iface: dict[str, Any]) -> int:
    """An interface's route metric as an int. RutOS sends it as a string in
    config and an int in status; anything unparseable sorts first."""
    try:
        return int(iface.get("metric") or 0)
    except (TypeError, ValueError):
        return 0


def _pick_active_wan(interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    """The actively-routing WAN from `/interfaces/status` rows; the selection
    rules are documented on `TeltonikaApiClient._fetch_wan_status`."""

    def _is_up(iface: dict[str, Any]) -> bool:
        return iface.get("status") == "up" or iface.get("up") is True

    def _is_wan_candidate(iface: dict[str, Any]) -> bool:
        return (
            iface.get("area_type") == "wan"
            or bool(iface.get("is_wan"))
            or bool(iface.get("default_route"))
        )

    def _label(iface: dict[str, Any]) -> str:
        return (
            iface.get("name")
            or iface.get("interface")
            or iface.get("id")
            or iface.get("type")
            or "unknown"
        )

    wan_candidates = [i for i in interfaces if _is_wan_candidate(i)]
    up_candidates = [i for i in wan_candidates if _is_up(i)]

    # Prefer an interface explicitly flagged as default route.
    for iface in up_candidates:
        if iface.get("default_route"):
            return {"wan_up": True, "wan_source": _label(iface)}

    # Otherwise pick the up WAN candidate with the lowest metric;
    # matches the kernel's routing decision when multiple WANs are
    # up. Stable on ties (Python sort is stable, response order
    # determines tiebreak).
    if up_candidates:
        chosen = min(up_candidates, key=_metric_of)
        return {"wan_up": True, "wan_source": _label(chosen)}

    # No WAN candidate is up: surface the first one's name so the
    # customer sees which interface SHOULD be the WAN.
    if wan_candidates:
        return {"wan_up": False, "wan_source": _label(wan_candidates[0])}

    # No WAN candidate exists at all: last-ditch: any up interface.
    for iface in interfaces:
        if _is_up(iface):
            return {"wan_up": True, "wan_source": _label(iface)}

    return {"wan_up": False, "wan_source": "none"}


class TeltonikaApiClient:
    """Client for the Teltonika REST API.

    Uses `requests` on a background thread via `hass.async_add_executor_job`
    rather than aiohttp — see module docstring for the Python 3.14 bug
    context. Keeps a `requests.Session` across calls so the TCP connection
    is pooled within a poll cycle (login + all endpoint fetchers reuse one
    socket to the router).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        username: str,
        password: str,
        *,
        ssl: bool = TELTONIKA_DEFAULT_SSL,
        fetch_gps: bool = False,
    ) -> None:
        self._hass = hass
        self._host = host
        self._username = username
        self._password = password
        self._base_url = f"{'https' if ssl else 'http'}://{host}/api"
        self._token: str | None = None
        self._fetch_gps = fetch_gps
        self._session = requests.Session()
        self._session.verify = False  # router self-signed cert on HTTPS
        # Ignore HTTP_PROXY / HTTPS_PROXY / NO_PROXY env vars — HA containers
        # sometimes have these set (for updates via a corporate proxy, etc.)
        # and routing a LAN-private request through them guarantees timeout.
        # Both aiohttp and requests honour these by default; asyncio's
        # open_connection does NOT, which is why our raw socket probe works
        # while HTTP libs hang.
        self._session.trust_env = False
        # Per-request timeout. Defaults to the healthy-router value;
        # `set_request_timeout` drops it when the coordinator decides
        # the router is unreachable so per-poll cycles fail fast
        # instead of stacking N × 10 s waits.
        self._timeout: float = _REQUEST_TIMEOUT
        # What the coordinator last asked for, and how many `full_timeout`
        # holds are open. While one is, the timeout never drops below healthy.
        self._asked_timeout: float = _REQUEST_TIMEOUT
        self._full_holds = 0
        # Whether the last firmware-info read failed, so a router that refuses
        # it is logged once when it starts and once when it stops, not every
        # poll.
        self._firmware_info_failing = False
        # Whether this router has an eSIM. None until the first read; False
        # once `/esim/status` answers 404, so a router without one is asked
        # once per session rather than on every poll.
        self._has_esim: bool | None = None

    @property
    def username(self) -> str:
        """The router user this client signs in as."""
        return self._username

    # -- Sync HTTP primitives (run on executor, not event loop) ----------------

    def _sync_post(
        self, url: str, json_payload: dict[str, Any],
        headers: dict[str, str] | None = None, timeout: float | None = None,
    ) -> requests.Response:
        # allow_redirects=False — RutOS 7 serves its WebUI with an HTTP→HTTPS
        # 301 for browser UX; if we let `requests` auto-follow that redirect
        # it silently re-issues the POST to port 443, and we hide an HTTPS-
        # reachability problem behind what looks like an HTTP timeout.
        # Surface the redirect explicitly so we know the REST API is actually
        # on HTTPS.
        return self._session.post(
            url, json=json_payload, headers=headers or {},
            timeout=timeout or self._timeout, allow_redirects=False,
        )

    def _sync_get(
        self, url: str, headers: dict[str, str], timeout: float | None = None,
    ) -> requests.Response:
        return self._session.get(
            url, headers=headers, timeout=timeout or self._timeout,
            allow_redirects=False,
        )

    def _sync_put(
        self, url: str, json_payload: dict[str, Any], headers: dict[str, str],
        timeout: float | None = None,
    ) -> requests.Response:
        # Same no-follow reasoning as _sync_post: a silently-followed HTTP->HTTPS
        # redirect would re-issue the write, and a write is not something to
        # replay by accident.
        return self._session.put(
            url, json=json_payload, headers=headers,
            timeout=timeout or self._timeout, allow_redirects=False,
        )

    def _sync_delete(self, url: str, headers: dict[str, str]) -> requests.Response:
        return self._session.delete(
            url, headers=headers, timeout=self._timeout, allow_redirects=False,
        )

    def set_request_timeout(self, seconds: float) -> None:
        """Override the per-request timeout. Coordinator drops it to the
        degraded value while the router is unreachable so each retry tick
        fails fast instead of stacking N × 10 s waits."""
        self._asked_timeout = seconds
        self._timeout = max(seconds, _REQUEST_TIMEOUT) if self._full_holds else seconds

    @contextlib.contextmanager
    def full_timeout(self) -> Iterator[None]:
        """Keep the healthy timeout, whatever the coordinator decides, while held.

        Fix reads and writes through the coordinator's client. One missed poll
        while the router was busy cut every call to 1.5 s, and Fix stopped on
        "GET /interfaces/config read timed out" 15 s later (customer RUT2M,
        2026-11-26). A busy router is not a gone one while Fix is running.
        """
        self._full_holds += 1
        self._timeout = max(self._timeout, _REQUEST_TIMEOUT)
        try:
            yield
        finally:
            self._full_holds -= 1
            if not self._full_holds:
                self._timeout = self._asked_timeout

    def _call_timeout(self, wanted: float | None) -> float:
        """The wait one call may have, given what the coordinator has decided.

        A caller asking for longer gets it only while the router is answering
        normally. Once the coordinator has dropped the timeout to the degraded
        value it has decided the router is unreachable, and a single read must
        not then re-introduce the multi-second hang per poll that the drop
        exists to prevent.
        """
        if wanted is None or self._timeout < _REQUEST_TIMEOUT:
            return self._timeout
        return wanted

    # -- Authentication -------------------------------------------------------

    async def async_login(self) -> str:
        """Authenticate and store the Bearer token."""
        url = f"{self._base_url}/login"
        payload = {"username": self._username, "password": self._password}

        _LOGGER.debug(
            "Teltonika login: POST %s (user=%s)", url, self._username,
        )

        try:
            # Always the healthy budget, never the degraded one: the login is
            # how a backed-off router proves it is back, and a slow router
            # (a RUT241 takes longer than 1.5 s to log in) could otherwise
            # never recover from a single failed poll.
            resp = await self._hass.async_add_executor_job(
                self._sync_post, url, payload, None, _REQUEST_TIMEOUT,
            )
        except requests.exceptions.ConnectTimeout as err:
            raise TeltonikaConnectionError("Login connect timed out") from err
        except requests.exceptions.ReadTimeout as err:
            raise TeltonikaConnectionError("Login read timed out") from err
        except requests.exceptions.RequestException as err:
            raise TeltonikaConnectionError(f"Login failed: {err}") from err

        # Redirect = router wants us on a different URL (typically HTTPS).
        # Surface clearly — this is the most common "misconfigured" state
        # a customer can land in, and a generic "login failed" message
        # would send us on a wild goose chase.
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location") or "(unset)"
            raise TeltonikaConnectionError(
                f"Login redirected ({resp.status_code}) to {location} — "
                f"the REST API expects a different scheme/host. Update the "
                f"device config to match (likely ssl: true)."
            )

        if resp.status_code == 401:
            raise TeltonikaAuthError("Invalid credentials")
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as err:
            raise TeltonikaConnectionError(f"Login HTTP error: {err}") from err
        try:
            data = resp.json()
        except ValueError as err:
            raise TeltonikaAuthError(f"Login response not JSON: {resp.text[:200]}") from err

        token = data.get("data", {}).get("token")
        if not token:
            raise TeltonikaAuthError(f"No token in login response: {data}")

        self._token = token
        _LOGGER.debug("Teltonika login successful")
        return token

    # -- Core request ---------------------------------------------------------

    async def async_get(
        self, path: str, *, _retry: bool = True, timeout: float | None = None,
        recovery: bool = False,
    ) -> dict[str, Any]:
        """GET a REST API endpoint. Auto-retries login on 401.

        `timeout` asks for a longer wait than the usual per-request budget, for
        the endpoints that answer from Teltonika's servers rather than from the
        router. It is a request, not an override: see `_call_timeout`.
        """
        if not self._token:
            await self.async_login()

        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._hass.async_add_executor_job(
                self._sync_get, url, headers,
                _REQUEST_TIMEOUT if recovery else self._call_timeout(timeout),
            )
        except requests.exceptions.ConnectTimeout as err:
            raise TeltonikaConnectionError(f"GET {path} connect timed out") from err
        except requests.exceptions.ReadTimeout as err:
            raise TeltonikaConnectionError(f"GET {path} read timed out") from err
        except requests.exceptions.RequestException as err:
            raise TeltonikaConnectionError(f"GET {path} failed: {err}") from err

        if resp.status_code == 401 and _retry:
            _LOGGER.debug("Token expired, re-authenticating")
            self._token = None
            await self.async_login()
            return await self.async_get(
                path, _retry=False, timeout=timeout, recovery=recovery,
            )
        if resp.status_code == 404:
            _LOGGER.debug("Endpoint not found: %s", path)
            return {}
        if resp.status_code == 403:
            raise TeltonikaPermissionError(
                f"GET {path} HTTP 403: {_api_error_text(resp)}"
            )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as err:
            raise TeltonikaHttpError(f"GET {path} HTTP error: {err}", resp.status_code) from err
        try:
            return resp.json()
        except ValueError:
            _LOGGER.debug("GET %s returned non-JSON body", path)
            return {}

    async def async_put(
        self, path: str, payload: dict[str, Any], *,
        _retry: bool = True, timeout: float | None = None,
    ) -> dict[str, Any]:
        """PUT a REST API endpoint. Auto-retries login on 401.

        Mirrors `async_get`, with two deliberate differences, both because this
        one CHANGES the device:

          * a 404 RAISES instead of returning {}. On a read, a missing endpoint
            is a capability we simply don't have; on a write it means the thing
            we tried to change does not exist, and reporting that as success
            would leave a control that silently does nothing.
          * the error text carries the device's own message. RutOS answers a
            bad write with a structured reason ("Invalid PUT structure",
            "Section: x does not exist") that is far more use than the status
            code alone when a control stops working in the field.

        `timeout` asks for a longer wait, as on `async_get`.
        """
        if not self._token:
            await self.async_login()

        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._hass.async_add_executor_job(
                self._sync_put, url, payload, headers, self._call_timeout(timeout),
            )
        except requests.exceptions.ConnectTimeout as err:
            raise TeltonikaConnectionError(f"PUT {path} connect timed out") from err
        except requests.exceptions.ReadTimeout as err:
            raise TeltonikaConnectionError(f"PUT {path} read timed out") from err
        except requests.exceptions.RequestException as err:
            raise TeltonikaConnectionError(f"PUT {path} failed: {err}") from err

        if resp.status_code == 401 and _retry:
            _LOGGER.debug("Token expired on write, re-authenticating")
            self._token = None
            await self.async_login()
            return await self.async_put(path, payload, _retry=False, timeout=timeout)

        if resp.status_code == 403:
            raise TeltonikaPermissionError(
                f"PUT {path} HTTP 403: {_api_error_text(resp)}"
            )
        if resp.status_code >= 400:
            raise TeltonikaConnectionError(
                f"PUT {path} HTTP {resp.status_code}: {_api_error_text(resp)}"
            )
        try:
            return resp.json()
        except ValueError:
            _LOGGER.debug("PUT %s returned non-JSON body", path)
            return {}

    async def async_delete(self, path: str, *, _retry: bool = True) -> dict[str, Any]:
        """DELETE a REST API endpoint, with `async_put`'s rules (a 404 raises)."""
        if not self._token:
            await self.async_login()
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = await self._hass.async_add_executor_job(
                self._sync_delete, f"{self._base_url}{path}", headers,
            )
        except requests.exceptions.RequestException as err:
            raise TeltonikaConnectionError(f"DELETE {path} failed: {err}") from err
        if resp.status_code == 401 and _retry:
            self._token = None
            await self.async_login()
            return await self.async_delete(path, _retry=False)
        if resp.status_code >= 400:
            raise TeltonikaConnectionError(
                f"DELETE {path} HTTP {resp.status_code}: {_api_error_text(resp)}"
            )
        try:
            return resp.json()
        except ValueError:
            return {}

    async def async_post(
        self, path: str, payload: dict[str, Any] | None = None, *,
        _retry: bool = True, timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST a REST API endpoint. Auto-retries login on 401.

        RutOS puts every firmware ACTION behind POST (`/firmware/actions/...`),
        and those actions have no PUT equivalent. Mirrors `async_put`, for the
        same reasons: a 404 raises rather than returning {} (a missing action is
        a write that did not happen, and reporting it as success would hide a
        firmware update that never started), and the router's own error text is
        carried into the exception. RutOS answers a FOTA start it cannot serve
        with a structured `code: 15` ("updates from server are not available"),
        which is far more use than "HTTP 500".

        `payload` is optional because most firmware actions take no body at all.
        `timeout` asks for a longer wait, as on `async_get`.
        """
        if not self._token:
            await self.async_login()

        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._hass.async_add_executor_job(
                self._sync_post, url, payload or {}, headers,
                self._call_timeout(timeout),
            )
        except requests.exceptions.ConnectTimeout as err:
            raise TeltonikaConnectionError(f"POST {path} connect timed out") from err
        except requests.exceptions.ReadTimeout as err:
            raise TeltonikaConnectionError(f"POST {path} read timed out") from err
        except requests.exceptions.RequestException as err:
            raise TeltonikaConnectionError(f"POST {path} failed: {err}") from err

        if resp.status_code == 401 and _retry:
            _LOGGER.debug("Token expired on action, re-authenticating")
            self._token = None
            await self.async_login()
            return await self.async_post(
                path, payload, _retry=False, timeout=timeout,
            )

        if resp.status_code == 403:
            raise TeltonikaPermissionError(
                f"POST {path} HTTP 403: {_api_error_text(resp)}"
            )
        if resp.status_code >= 400:
            raise TeltonikaConnectionError(
                f"POST {path} HTTP {resp.status_code}: {_api_error_text(resp)}"
            )
        try:
            return resp.json()
        except ValueError:
            _LOGGER.debug("POST %s returned non-JSON body", path)
            return {}

    async def async_set_port_poe(self, port_id: str, enabled: bool) -> None:
        """Turn PoE on or off for one switch port.

        Contract confirmed against a live TSW202 on 2026-08-07:
        `PUT /ports_settings/config/<port_id>` with the change wrapped in
        `data` (a flat body is rejected as "Invalid PUT structure"). The change
        applies IMMEDIATELY, with no separate UCI commit / apply call, and
        `/ports_settings/status` reflects it on the next poll.

        `poe_enable` is the port's PoE CONFIG flag. It is what a customer
        toggles to power-cycle a stuck PoE device; whether power is actually
        flowing is the separate `power` field (see `_fetch_port_status`).
        """
        if not port_id:
            raise ValueError("port_id is required")
        await self.async_put(
            f"/ports_settings/config/{port_id}",
            {"data": {"poe_enable": "1" if enabled else "0"}},
        )

    # -- Data fetchers --------------------------------------------------------

    async def async_get_gps(self) -> dict[str, Any]:
        """The position, if this edition reads GPS; {} otherwise.

        Read on its own timer (the coordinator's `async_refresh_gps`), never in
        the status sweep, so the customer's GPS refresh decides its cadence.
        """
        if not self._fetch_gps:
            return {}
        return await self._fetch_gps_data()

    async def async_get_router_data(self) -> dict[str, Any]:
        """Fetch device data. System info first, then conditional fetchers.

        GPS is not read here: see `async_get_gps`.
        """
        # Pre-flight the login serially so the concurrent fetchers below
        # don't all race to authenticate at once. Without this, the first
        # poll's `asyncio.gather(...)` spawns N concurrent async_get
        # calls, every one sees `self._token is None`, and every one
        # fires `async_login()` in parallel — the router rejects some
        # of the simultaneous login attempts as "Invalid credentials"
        # and the winning token lands after the losers have already
        # built their `Authorization` headers with None. End result:
        # partial data, entities stuck on "unknown", phantom auth
        # warnings in the log.
        if not self._token:
            await self.async_login()

        # System info is mandatory — drives the hw_mobile switch below and
        # is the source of device_model / firmware / capabilities for
        # enrichment. If it fails (usually an auth error), raise so the
        # coordinator marks the update as failed; swallowing it here would
        # leave entities showing "unknown" forever with no clear signal
        # that authentication was the problem.
        data: dict[str, Any] = {}
        data.update(await self._fetch_system_info())

        # Firmware info from the dedicated /firmware/device/status endpoint.
        # System info already carries a fallback `firmware_version` from
        # `static.fw_version`, but this endpoint is the authoritative source
        # and adds the build date. Non-fatal — overrides firmware_version
        # only when it actually returns a value.
        try:
            data.update(await self._fetch_firmware_info())
        except Exception as err:  # noqa: BLE001
            if self._firmware_info_failing:
                _LOGGER.debug("Firmware info fetch failed: %s", err)
            else:
                _LOGGER.warning(
                    "Firmware info fetch failed: %s (further failures are "
                    "logged at debug until it recovers)", err,
                )
            self._firmware_info_failing = True
        else:
            if self._firmware_info_failing:
                _LOGGER.info("Firmware info fetch recovered")
            self._firmware_info_failing = False

        # Port status can fail independently (some RutOS builds 404 on
        # /ports_settings/status) — non-fatal.
        try:
            data.update(await self._fetch_port_status())
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Port status fetch failed: %s", err)

        # Router-only fetchers — skip for switches (no cellular modem)
        if data.get("hw_mobile", True):
            router_fetchers = [
                self._fetch_modem_data(),
                self._fetch_wan_status(),
                self._fetch_connected_clients(),
                self._fetch_lan_clients(),
                self._fetch_esim_data(),
            ]
            router_results = await asyncio.gather(
                *router_fetchers, return_exceptions=True
            )
            for result in router_results:
                if isinstance(result, Exception):
                    _LOGGER.warning("Router data fetch failed: %s", result)
                    continue
                data.update(result)


        return data

    async def _fetch_modem_data(self) -> dict[str, Any]:
        """Fetch cellular/modem status."""
        resp = await self.async_get("/modems/status")
        modems = resp.get("data", [])

        if not modems:
            return {}

        # Use the first online modem (skip offline modem entries)
        modem = None
        for m in (modems if isinstance(modems, list) else [modems]):
            if isinstance(m, dict) and m.get("rssi") is not None:
                modem = m
                break

        if not modem:
            return {}

        # TX/RX bytes → MB
        tx = _parse_float(modem.get("txbytes"))
        rx = _parse_float(modem.get("rxbytes"))

        return {
            "signal_rssi": _parse_int(modem.get("rssi")),
            "signal_rsrp": _parse_int(modem.get("rsrp")),
            "connection_type": modem.get("ntype"),
            "operator": modem.get("operator"),
            "data_tx_mb": round(tx / 1_048_576, 2) if tx else None,
            "data_rx_mb": round(rx / 1_048_576, 2) if rx else None,
        }

    async def _fetch_esim_data(self) -> dict[str, Any]:
        """The eSIM: its profiles, which one is in use and this month's data.

        Returns `{"esim": {...}}` on a router with an eSIM and {} on one
        without. Read on BP's RUTM11 eSIM (RutOS 7.26, 2026-11-01):

          * `/esim/status` gives the modem id and EID;
          * `/esim/config` lists the profiles, `id` being the ICCID and
            `profile_set` the slot the data-usage route is keyed by (the ICCID
            there answers 404 "SIM not found");
          * `/modems/status` says which SIM the modem is on (`active_sim`) and
            the ICCID in use, with a trailing `F` the profile list does not
            have. The bootstrap profile read `enabled: "0"` while in use, so
            the ICCID, not `enabled`, says which profile is active.

        A failed read raises, so the poll drops the key and the entity keeps
        its last answer ("could not ask" is not "no eSIM").
        """
        if self._has_esim is False:
            return {}
        status = await self.async_get("/esim/status")
        if not status:
            # 404: this router has no eSIM. Remembered for the session.
            self._has_esim = False
            return {}
        self._has_esim = True
        rows = status.get("data") if isinstance(status, dict) else None
        chip = next((r for r in (rows or []) if isinstance(r, dict)), {})
        modem_id = str(chip.get("id") or "")

        config = await self.async_get("/esim/config")
        modems = await self.async_get("/modems/status")
        modem = next(
            (
                m for m in (modems.get("data") or [])
                if isinstance(m, dict) and str(m.get("id") or "") == modem_id
            ),
            {},
        )
        in_use = _iccid(modem.get("iccid"))

        profiles: list[dict[str, Any]] = []
        esim_sim = ""
        for row in config.get("data") or []:
            if not isinstance(row, dict):
                continue
            iccid = _iccid(row.get("id"))
            esim_sim = esim_sim or str(row.get("sim") or "")
            profiles.append({
                "iccid": iccid,
                "name": str(row.get("name") or iccid),
                "provider": str(row.get("provider") or ""),
                "bootstrap": str(row.get("bootstrap") or "0") == "1",
                "slot": str(row.get("profile_set") or ""),
                "active": bool(iccid) and iccid == in_use,
                "used_month_bytes": None,
            })

        # Per-profile usage, this calendar month. A failed read leaves that
        # profile's figure None (not 0): the card shows "--", never a zero it
        # did not measure.
        for prof in profiles:
            if not (modem_id and prof["slot"]):
                continue
            try:
                usage = await self.async_get(
                    f"/data_usage/month/modem/{modem_id}/esim/{prof['slot']}/status"
                )
            except TeltonikaApiError as err:
                _LOGGER.debug("eSIM usage read failed for %s: %s", prof["iccid"], err)
                continue
            if usage:
                prof["used_month_bytes"] = _sum_usage_rows(usage.get("data"))

        return {
            "esim": {
                "eid": str(chip.get("eid") or ""),
                "modem": modem_id,
                "sim": esim_sim,
                "on_esim": bool(esim_sim)
                and str(modem.get("active_sim") or "") == esim_sim,
                "active_iccid": in_use,
                "operator": _known(modem.get("operator")),
                "registered": _known(modem.get("operator_state")),
                "profiles": profiles,
                "errors": [
                    str(e.get("error") or e) if isinstance(e, dict) else str(e)
                    for e in (chip.get("errors") or [])
                ],
                "busy": bool(chip.get("pending_jobs")),
            },
        }

    async def async_get_esim_data(self) -> dict[str, Any]:
        """Just the eSIM read, for the card while an install or switch is on
        screen. The same shape the poll merges in."""
        return await self._fetch_esim_data()

    async def async_esim_download(
        self, modem: str, activation_code: str, *, name: str = "", apn: str = "",
    ) -> str:
        """Download a profile from its activation code and switch to it.

        `POST /esim/actions/download?switch_sim=1` (RutOS API spec 7.26.1
        v1.18.1). Only `modem` and `activation_code` are required. The router
        answers with an operation id and downloads in the background; progress
        and failure show in `/esim/status` (`pending_jobs`, `errors`). The
        download needs internet over another link (Starlink, the physical SIM
        or the bootstrap profile). Not yet exercised on hardware.
        """
        if not (modem and activation_code):
            raise ValueError("modem and activation_code are required")
        body: dict[str, Any] = {"modem": modem, "activation_code": activation_code}
        if name:
            body["name"] = name
        if apn:
            body["apn"] = apn
            body["auth"] = "none"
        resp = await self.async_post(
            "/esim/actions/download?switch_sim=1", {"data": body},
            timeout=_REQUEST_TIMEOUT_FIRMWARE,
        )
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
        return str(resp.get("id") or "") if isinstance(resp, dict) else ""

    async def async_esim_enable(self, iccid: str) -> None:
        """Make this profile the one in use.

        `PUT /esim/config/{iccid}` with `enabled: "1"` (API spec). The mobile
        link drops while the modem moves over. Not yet exercised on hardware.
        There is deliberately no delete: activation codes are often single use,
        so a deleted profile may never come back.
        """
        if not iccid:
            raise ValueError("iccid is required")
        await self.async_put(f"/esim/config/{iccid}", {"data": {"enabled": "1"}})

    async def _fetch_wan_status(self) -> dict[str, Any]:
        """Fetch WAN/interface status and report the actively-routing
        interface.

        Real-world RutOS 7 `/interfaces/status` responses observed on
        a RutX11 do NOT include a `default_route` boolean or an
        `is_wan` flag on each interface. Instead they tag WAN
        candidates with `area_type: "wan"` (e.g. `mob1s1a1` for
        cellular, `wan` for ethernet WAN, `wan1` for Wi-Fi-STA WAN).
        The `metric` field gives the route-table priority — lower
        wins, so the active WAN is the up `area_type=wan` interface
        with the smallest metric.

        Selection logic:
          1. WAN candidates = interfaces with `area_type == "wan"` OR
             `is_wan: True` OR `default_route: True` (covers RutOS
             variants that DO emit those flags).
          2. Among up candidates, prefer one with `default_route`,
             else prefer the lowest `metric`.
          3. If no candidates are up, report wan_up=False but still
             surface the first candidate's name so the customer can
             see which interface RutOS thinks of as "the WAN".
          4. Fallback to any up interface only if there are no WAN
             candidates at all (pathological).

        Returns the customer-friendly `name` (e.g. "Starlink_Wifi",
        "Sim1_EE") rather than the technical `interface` id, since
        RutOS lets the user label these and the labels are what they
        recognise.
        """
        resp = await self.async_get("/interfaces/status")
        data = resp.get("data", [])

        if not data:
            return {}

        interfaces = [
            i for i in (data if isinstance(data, list) else [data])
            if isinstance(i, dict)
        ]

        result = _pick_active_wan(interfaces)
        # The priority list is a second, independent read. A failure leaves
        # `wan_interfaces` OUT of the result ("could not ask"), never an empty
        # list, which the card would show as "this router has no WANs".
        try:
            wan_list = await self._fetch_wan_interfaces(interfaces)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("WAN priority fetch failed: %s", err)
        else:
            if wan_list is not None:
                result["wan_interfaces"] = wan_list
        return result

    async def _fetch_wan_interfaces(
        self, status: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """The router's ENABLED WANs in precedence order, joined with live state.

        Measured on a RUTX11 (RutOS 00.07.24.3, 2026-09-28):
        `/interfaces/config` carries every WAN with `area_type:"wan"`,
        `enabled:"1"|"0"` and `metric` as a string. The metric IS the failover
        precedence (lower wins); RutOS has no separate failover or mwan3 REST
        endpoint ("not implemented"). `/interfaces/status` reports `metric: 0`
        for a WAN that is down, so the order must come from config, never
        status. Status supplies `up` and `ipv4-address`, joined on `id`.

        Returns None when the endpoint is missing (older firmware answers 404,
        which `async_get` turns into {}), so the caller omits the key.
        """
        resp = await self.async_get("/interfaces/config")
        rows = resp.get("data")
        if not isinstance(rows, list):
            return None

        live = {
            str(i.get("id") or i.get("interface")): i
            for i in status
            if i.get("id") or i.get("interface")
        }
        wans = []
        for row in rows:
            if not isinstance(row, dict) or row.get("area_type") != "wan":
                continue
            if str(row.get("enabled", "1")) != "1":
                continue
            iface_id = str(row.get("id") or "")
            if not iface_id:
                continue
            state = live.get(iface_id, {})
            addrs = state.get("ipv4-address") or []
            ip = addrs[0].get("address") if addrs and isinstance(addrs[0], dict) else None
            wans.append({
                "id": iface_id,
                "name": row.get("name") or iface_id,
                "proto": row.get("proto") or state.get("proto") or "",
                "type": state.get("type") or "",
                "metric": _metric_of(row),
                "up": state.get("status") == "up" or state.get("up") is True,
                "ip": ip,
            })
        # Stable on ties, so equal metrics keep the router's own list order.
        wans.sort(key=lambda w: w["metric"])
        return wans

    async def async_get_wan_data(self) -> dict[str, Any]:
        """Just the WAN read (`wan_up`, `wan_source`, `wan_interfaces`), for a
        refresh while a customer is looking at the order. Two small GETs, not
        the full poll."""
        return await self._fetch_wan_status()

    async def async_reboot(self) -> None:
        """Restart the router (the network card's Restart button).

        `POST /system/actions/reboot`, the same action shape as the firmware
        actions. On a RUTX11 (00.07.24.3, 2026-09-28) a GET on that path
        answers 501 while a made-up action answers 403, so the route exists;
        the POST itself has not been exercised on hardware. The router drops
        the connection as it goes down, so a lost reply is expected and the
        coordinator's next poll simply fails and recovers.
        """
        try:
            await self.async_post("/system/actions/reboot")
        except TeltonikaConnectionError as err:
            if "timed out" not in str(err) and "failed:" not in str(err):
                raise
            _LOGGER.debug("Teltonika reboot: no reply, router going down (%s)", err)

    async def _async_raw(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """One authenticated request that hands back the response itself.

        For the backup calls, which carry a file rather than JSON. Logs in
        again once on a 401, like the JSON calls; never follows a redirect.
        """
        for attempt in (1, 2):
            if not self._token:
                await self.async_login()
            headers = {"Authorization": f"Bearer {self._token}"}
            if "json" in kwargs:
                headers["Content-Type"] = "application/json"

            def _send(headers: dict[str, str] = headers) -> requests.Response:
                return self._session.request(
                    method, f"{self._base_url}{path}", headers=headers,
                    timeout=_BACKUP_TIMEOUT, allow_redirects=False, **kwargs,
                )

            try:
                resp = await self._hass.async_add_executor_job(_send)
            except requests.exceptions.RequestException as err:
                raise TeltonikaConnectionError(f"{method} {path} failed: {err}") from err
            if resp.status_code == 401 and attempt == 1:
                self._token = None
                continue
            if resp.status_code >= 400:
                raise TeltonikaConnectionError(
                    f"{method} {path} HTTP {resp.status_code}: {_api_error_text(resp)}"
                )
            return resp
        raise TeltonikaAuthError(f"{method} {path}: refused after logging in again")

    async def async_backup_create(self) -> bytes:
        """The router's own full backup ("Device backup"), as a file.

        `generate` makes it on the router and answers its sha256; `download`
        hands it over. The file is checked against that hash, so a cut-off
        download is never kept as a backup. Paths and fields from Teltonika's
        API spec for RUTM11 7.24.3 (v1.16.3); not yet run on hardware.
        """
        resp = await self._async_raw(
            "POST", "/backup/actions/generate",
            json={"data": {"type": "sysupgrade", "encrypt": "0"}},
        )
        want = str(((resp.json() or {}).get("data") or {}).get("sha256") or "")
        resp = await self._async_raw("POST", "/backup/actions/download", json={})
        data = resp.content
        got = hashlib.sha256(data).hexdigest()
        if not data or (want and got != want):
            raise TeltonikaConnectionError(
                f"Router backup did not download whole ({len(data)} bytes, sha256 {got}, router said {want})"
            )
        return data

    async def async_backup_restore(self, data: bytes) -> None:
        """Put a backup from `async_backup_create` back. The router restarts.

        `upload` then `apply`, as the router's own Backup page does. The
        upload's sha256 must match the file sent. The router drops the
        connection as it goes down, so a lost reply to `apply` is expected.
        """
        resp = await self._async_raw(
            "POST", "/backup/actions/upload",
            files={"file": ("backup.tar.gz", data, "application/x-targz")},
            data={"encrypt": "0"},
        )
        body = resp.json() if resp.content else {}
        sent = hashlib.sha256(data).hexdigest()
        got = str(((body or {}).get("data") or {}).get("sha256") or "")
        if not (body or {}).get("success") or (got and got != sent):
            raise TeltonikaConnectionError(f"Router refused the backup: {str(body)[:300]}")
        try:
            await self._async_raw("POST", "/backup/actions/apply", json={"data": {"encrypt": "0"}})
        except TeltonikaConnectionError as err:
            if "failed:" not in str(err):
                raise
            _LOGGER.debug("Teltonika backup apply: no reply, router going down (%s)", err)

    async def async_set_wan_order(self, order: list[str]) -> None:
        """Rewrite the enabled WANs' metrics so `order` is the precedence.

        `order` is the full list of enabled WAN ids, highest priority first.
        Each gets metric 1..n; a disabled WAN is left alone. Only rows whose
        metric actually changes are written.

        TWO lists hold the order, and both are written. The interface metric
        is what `/interfaces/config` reports (and what this integration reads
        back); the failover engine (mwan3) routes by its OWN per-member metric
        in `/failover/members/config`. Measured on BP-RUTX11 (00.07.24.3,
        2026-09-29): the RutOS web UI keeps the two equal, so a write to the
        interface metric alone would show the new order on every screen while
        the router kept failing over in the old one.

        Write contract ASSUMED, not yet confirmed on hardware: `PUT
        /interfaces/config/<id>` and `PUT /failover/members/config/<id>`, each
        with the change wrapped in `data`, the envelope `/ports_settings/config`
        was measured to need.
        """
        resp = await self.async_get("/interfaces/config")
        rows = resp.get("data")
        if not isinstance(rows, list):
            raise TeltonikaConnectionError("interfaces/config unavailable")
        current = {
            str(r.get("id")): _metric_of(r)
            for r in rows
            if isinstance(r, dict)
            and r.get("area_type") == "wan"
            and str(r.get("enabled", "1")) == "1"
        }
        if sorted(order) != sorted(current):
            raise ValueError(
                f"WAN order must list every enabled WAN exactly once: {sorted(current)}"
            )
        for metric, iface_id in enumerate(order, start=1):
            if current[iface_id] != metric:
                await self.async_put(
                    f"/interfaces/config/{iface_id}",
                    {"data": {"metric": str(metric)}},
                )
        names = {
            str(r.get("id")): r.get("name") or str(r.get("id"))
            for r in rows
            if isinstance(r, dict)
        }
        await self._set_failover_order([names[i] for i in order])

    async def _set_failover_order(self, names: list[str]) -> None:
        """Give the failover policy's members the same order, by interface NAME.

        Members are matched through the active policy's `use_member` list
        (`mwan_default`, measured), because its members are the ones mwan3
        routes by: each interface also has a `balance` member for the load-
        balancing policy, whose metrics are all 1 and must not be touched. A
        router with no failover policy (404 -> {}) has only the interface
        metric, so there is nothing more to write.
        """
        policies = (await self.async_get("/failover/policies/config")).get("data")
        if not isinstance(policies, list):
            return
        policy = next(
            (
                p for p in policies
                if isinstance(p, dict)
                and (p.get("id") == "mwan_default" or p.get("name") == "mwan")
            ),
            None,
        )
        if policy is None:
            return
        members = (await self.async_get("/failover/members/config")).get("data")
        if not isinstance(members, list):
            return
        by_id = {str(m.get("id")): m for m in members if isinstance(m, dict)}
        rank = {name: i for i, name in enumerate(names, start=1)}
        for member_id in policy.get("use_member") or []:
            member = by_id.get(str(member_id))
            if not member:
                continue
            want = rank.get(member.get("interface"))
            if want is None or _metric_of(member) == want:
                continue
            await self.async_put(
                f"/failover/members/config/{member_id}",
                {"data": {"metric": str(want)}},
            )

    async def _fetch_lan_clients(self) -> dict[str, Any]:
        """The router's LAN networks and its DHCP leases, for the Networks card.

        Measured on a RUTX11 (00.07.24.3, 2026-09-28): `/interfaces/config`
        carries each LAN with `ipaddr` and `enabled`, and
        `/dhcp/leases/ipv4/status` lists every lease with `ipaddr`,
        `hostname` (absent for some clients), `macaddr` and the `interface`
        it was served on. Which van segment a lease belongs to is decided by
        its address against `network_profile.json`, not by the interface
        name, which differs between routers.

        Wired or wireless comes from `/wireless/interfaces/status`: each AP's
        `clients` carry `ipaddr` and `macaddr`. A lease is wireless when its
        address OR its MAC is associated; the address matters because a
        client behind a Wi-Fi extender shows the extender's MAC to the AP
        while its lease keeps its own (measured: SEN-SURFACE on BP-RUTX11).

        Either lease read missing (404 -> {}) leaves the keys out, so the card
        waits rather than showing every network empty. A missing Wi-Fi read
        only drops the wired / wireless split.
        """
        config = await self.async_get("/interfaces/config")
        leases = await self.async_get("/dhcp/leases/ipv4/status")
        rows = config.get("data")
        lease_rows = leases.get("data")
        if not isinstance(rows, list) or not isinstance(lease_rows, list):
            return {}
        wifi: list[dict[str, Any]] | None
        try:
            aps = (await self.async_get("/wireless/interfaces/status")).get("data")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Wi-Fi client read failed: %s", err)
            aps = None
        if isinstance(aps, list):
            wifi = [
                {"ip": c.get("ipaddr"), "mac": c.get("macaddr"), "hostname": c.get("hostname")}
                for ap in aps
                if isinstance(ap, dict) and ap.get("mode", "ap") == "ap"
                for c in ap.get("clients") or []
                if isinstance(c, dict)
            ]
        else:
            wifi = None
        return {
            "lan_wifi_clients": wifi,
            "lan_networks": [
                {
                    "id": str(r.get("id") or ""),
                    "name": r.get("name") or r.get("id"),
                    "ip": r.get("ipaddr"),
                    "netmask": r.get("netmask"),
                    "enabled": str(r.get("enabled", "1")) != "0",
                }
                for r in rows
                if isinstance(r, dict) and r.get("area_type") != "wan" and r.get("ipaddr")
            ],
            "lan_leases": [
                {
                    "ip": r.get("ipaddr"),
                    "hostname": r.get("hostname") or None,
                    "mac": r.get("macaddr"),
                }
                for r in lease_rows
                if isinstance(r, dict) and r.get("ipaddr")
            ],
        }

    async def _fetch_connected_clients(self) -> dict[str, Any]:
        """Fetch connected client count from wireless interfaces."""
        resp = await self.async_get("/wireless/interfaces/status")
        data = resp.get("data", [])

        if not data or not isinstance(data, list):
            return {}

        # Count clients across all wireless interfaces
        total = 0
        for iface in data:
            if not isinstance(iface, dict):
                continue
            clients = iface.get("clients", [])
            if isinstance(clients, list):
                total += len(clients)

        return {"connected_clients": total}

    async def _fetch_gps_data(self) -> dict[str, Any]:
        """Fetch GPS position data."""
        resp = await self.async_get("/gps/position/status")
        data = resp.get("data", {})

        if not data:
            return {}

        return {
            "gps_latitude": _parse_float(data.get("latitude")),
            "gps_longitude": _parse_float(data.get("longitude")),
            "gps_speed": _parse_float(data.get("speed")),
        }



    async def _fetch_system_info(self) -> dict[str, Any]:
        """Fetch device info: model, serial, firmware, hardware capabilities."""
        # The one mandatory read, so it is what decides whether a backed-off
        # router has recovered: it gets the healthy budget even while
        # degraded, or a router slower than 1.5 s stays "unreachable" for ever.
        resp = await self.async_get("/system/device/status", recovery=True)
        _LOGGER.info("Teltonika /system/device/status response keys: %s", list(resp.keys()) if resp else "empty")
        data = resp.get("data", {})

        if not data or not isinstance(data, dict):
            _LOGGER.info("Teltonika system info: no data in response")
            return {}

        static = data.get("static") or {}
        mnfinfo = data.get("mnfinfo") or {}
        board = data.get("board") or {}
        model_info = board.get("model") or {}
        hwinfo = board.get("hwinfo") or {}

        _LOGGER.info(
            "Teltonika system info: model=%s serial=%s fw=%s hw_mobile=%s "
            "hw_gps=%s hw_poe=%s hwinfo_keys=%s product_code=%s mnfinfo_keys=%s",
            model_info.get("name") or static.get("model", ""),
            mnfinfo.get("serial", ""),
            static.get("fw_version", ""),
            hwinfo.get("mobile"),
            hwinfo.get("gps"),
            hwinfo.get("poe", False),
            sorted(hwinfo.keys()),
            mnfinfo.get("name", ""),
            sorted(mnfinfo.keys()),
        )

        return {
            "device_model": model_info.get("name") or static.get("model", ""),
            "device_model_id": model_info.get("id", ""),
            "device_platform": model_info.get("platform", ""),
            "device_hostname": static.get("hostname", ""),
            "device_serial": mnfinfo.get("serial", ""),
            # The order code (the "Product code" on the router's System page).
            # Variants sharing one model name, such as the RUTM11 and the
            # RUTM11 eSIM, differ here; see `_model_slug`.
            "device_product_code": mnfinfo.get("name", ""),
            "firmware_version": static.get("fw_version", ""),
            # Mobile + GPS gate the cellular / GPS fetchers. Default TRUE when
            # the API doesn't explicitly report the capability: some RutOS
            # builds/models omit `board.hwinfo.mobile`/`.gps`, and the old
            # `False` default silently SKIPPED the modem/WAN/signal/clients
            # (and GPS) fetchers on a real router — every such sensor stuck
            # "unavailable" with NO error. The fetchers are non-fatal, so
            # attempting them on a genuine non-cellular unit just returns empty.
            # Only an explicit `mobile: false` / `gps: false` skips them.
            "hw_mobile": hwinfo.get("mobile", True),
            "hw_gps": hwinfo.get("gps", True),
            "hw_poe": hwinfo.get("poe", False),
            "hw_wifi": hwinfo.get("wifi", False),
            "hw_bluetooth": hwinfo.get("bluetooth", False),
            "hw_dual_sim": hwinfo.get("dual_sim", False),
            "hw_usb": hwinfo.get("usb", False),
        }

    async def _fetch_firmware_info(self) -> dict[str, Any]:
        """Fetch firmware version + build date from /firmware/device/status.

        Authoritative source for `firmware_version` (system info's
        `static.fw_version` is the fallback). Returns only the keys it can
        populate so a partial/empty response never blanks the fallback
        firmware_version already set by _fetch_system_info.
        """
        resp = await self.async_get("/firmware/device/status")
        data = resp.get("data", {})

        if not data or not isinstance(data, dict):
            return {}

        result: dict[str, Any] = {}
        version = data.get("version")
        if version:
            result["firmware_version"] = version
        build_date = data.get("build_date")
        if build_date:
            result["firmware_build_date"] = build_date
        return result

    async def async_get_firmware_version(self) -> str:
        """The version the device is running RIGHT NOW, read fresh. "" if it
        cannot be read.

        Used to tell a finished flash from a device that has simply not come
        back yet, so it asks the authoritative endpoint first and falls back to
        system info's `static.fw_version`. The fallback is what makes this safe
        on a TSW switch, whose `/firmware/device/status` has not been read off
        real hardware and would answer 404 (an empty dict) if it is absent: a
        device that answers at all then still reports a version, rather than the
        wait for its reboot timing out on an install that worked.

        Raises `TeltonikaApiError` while the device is down, which is the
        expected answer for most of a reboot.
        """
        info = await self._fetch_firmware_info()
        version = str(info.get("firmware_version") or "").strip()
        if version:
            return version
        system = await self._fetch_system_info()
        return str(system.get("firmware_version") or "").strip()

    # -- FOTA: what the router's own updater knows --------------------------

    async def async_get_fota_info(self) -> dict[str, Any]:
        """What Teltonika's server is offering this router, and whether we may ask.

        Three reads, each independent, because they answer three different
        questions and one of them failing must not discard the others:

          * `/firmware/device/updates/status` - the two offered versions
          * `/fota/config`                    - is the FOTA client even enabled
          * `/firmware/device/progress/status` - is a download running right now

        Returns ONLY the keys it could populate. The caller merges this over the
        previous answer, so a read that fails carries the last one forward rather
        than blanking it: "could not ask" must never render as "up to date".

        Deliberately not part of `async_get_router_data`. These values change
        when Teltonika publishes a build, not every 30 seconds, and the
        coordinator calls this on its own slow cadence.
        """
        info: dict[str, Any] = {}

        try:
            resp = await self.async_get(
                FOTA_UPDATES_PATH, timeout=_REQUEST_TIMEOUT_CLOUD,
            )
        except TeltonikaApiError as err:
            _LOGGER.debug("Teltonika FOTA updates read failed: %s", err)
        else:
            info.update(_parse_fota_updates(resp.get("data")))

        try:
            resp = await self.async_get(FOTA_CONFIG_PATH)
        except TeltonikaApiError as err:
            _LOGGER.debug("Teltonika FOTA config read failed: %s", err)
        else:
            info.update(_parse_fota_config(resp.get("data")))

        try:
            resp = await self.async_get(FOTA_PROGRESS_PATH)
        except TeltonikaApiError as err:
            _LOGGER.debug("Teltonika FOTA progress read failed: %s", err)
        else:
            info.update(_parse_fota_progress(resp.get("data")))

        # What the three reads between them actually produced. A read that
        # FAILS says so above; this is the line for the harder case, where every
        # read answered and the entity still has no version to report, and
        # without it the only visible symptom is a blank row on the Admin card.
        _LOGGER.debug(
            "Teltonika %s FOTA answer: %s", getattr(self, "_host", "?"), info,
        )
        return info

    async def async_get_fota_progress(self) -> dict[str, Any]:
        """Just the download progress, for polling during an install."""
        resp = await self.async_get(FOTA_PROGRESS_PATH)
        return _parse_fota_progress(resp.get("data"))

    # -- FOTA: the writes ---------------------------------------------------

    async def async_fota_download(self, stream: str) -> None:
        """Start the download of one stream's firmware from Teltonika's server.

        `stream` is `stable` or `latest`, exactly the two values the router's own
        WebUI sends. This only DOWNLOADS: nothing is flashed until
        `async_fota_upgrade`, which is why the sequence is worth keeping in that
        order even though `rut_fota` can be told to do both at once over ubus.

        A TSW switch has ONE stream and refuses any `type` with 422 "Invalid
        option" (TSW202 on TSW2_R_00.01.07.1, 2026-11-07); an empty `data`
        starts its download. So a refusal of the `type` is retried without it,
        keyed off the device's own answer rather than a guess at the model.
        """
        if stream not in (FOTA_STREAM_STABLE, FOTA_STREAM_LATEST):
            raise ValueError(f"unknown FOTA stream: {stream!r}")
        try:
            await self.async_post(
                FOTA_DOWNLOAD_ACTION, {"data": {"type": stream}},
            )
        except TeltonikaConnectionError as err:
            if "HTTP 422" not in str(err) or "Invalid option" not in str(err):
                raise
            _LOGGER.debug(
                "Teltonika %s: download type refused (%s); retrying with no "
                "type, as a single-stream TSW expects",
                getattr(self, "_host", "?"), err,
            )
            await self.async_post(FOTA_DOWNLOAD_ACTION, {"data": {}})

    async def async_fota_cancel(self) -> None:
        """Cancel a running download."""
        await self.async_post(FOTA_CANCEL_ACTION)

    async def async_fota_verify(self) -> dict[str, Any]:
        """Verify the downloaded image, and report what it is.

        Answers `valid` ("1" when the image checks out), `newer` (whether it is
        newer than what is running) and `message_code` on a refusal. The upgrade
        must not be started on anything but `valid == "1"`: the router will
        happily be told to flash a half-downloaded file.
        """
        resp = await self.async_post(
            FOTA_VERIFY_ACTION, timeout=_REQUEST_TIMEOUT_FIRMWARE,
        )
        data = resp.get("data")
        return data if isinstance(data, dict) else {}

    async def async_fota_upgrade(self, keep_settings: bool) -> None:
        """Flash the verified image. The router reboots; this returns first.

        `keep_settings` is the customer's configuration, and losing it means a
        van off the network until someone reprovisions the router by hand.
        """
        await self.async_post(
            FOTA_UPGRADE_ACTION,
            {"data": {"keep_settings": "1" if keep_settings else "0"}},
        )

    async def _fetch_port_status(self) -> dict[str, Any]:
        """Fetch Ethernet port status, normalised across the RUT and TSW
        firmware families.

        `/ports_settings/status` returns TWO DIFFERENT SHAPES, captured from
        real hardware on 2026-08-07:

          RUTX11 router   {"state": "up", "duplex": "true", "speed": 1000,
                           "name": "LAN", "position": 1, "id": "_lan2",
                           "topology": [...]}                 no PoE keys
          TSW2XX switch   {"link": "1", "full_duplex": "1", "speed": "1000",
                           "poe_enable": "1", "power": "1", "budget": "922.4",
                           "id": "port1"}      no state/name/position/topology

        The old parser only understood the router's shape, so on a switch every
        `state` lookup missed and EVERY PORT READ DOWN while traffic was
        flowing through it. Normalising here (the one place that touches the
        wire) keeps the entities and the dashboard card schema-agnostic
        instead of teaching each of them both dialects.
        """
        resp = await self.async_get("/ports_settings/status")
        data = resp.get("data", [])

        if not data or not isinstance(data, list):
            return {}

        ports: list[dict[str, Any]] = []
        for port in data:
            if not isinstance(port, dict):
                continue

            # Link: router says state up/down, switch says link 1/0.
            if "state" in port:
                up = str(port.get("state", "")).strip().lower() == "up"
            else:
                up = _parse_bool(port.get("link")) is True

            # Duplex: router says duplex "true"/"false", switch full_duplex 1/0.
            # A DARK port has no duplex to report, but the switch still sends
            # `full_duplex: "0"` for it, which would surface as a confident
            # "half" on a port with nothing plugged in. Only a linked port has
            # a duplex, so blank it otherwise (same reasoning as speed 0 below:
            # the device is describing an absence, not taking a measurement).
            if "duplex" in port:
                full = _parse_bool(port.get("duplex"))
            else:
                full = _parse_bool(port.get("full_duplex"))
            duplex = "" if (full is None or not up) else ("full" if full else "half")

            # Speed: router int, switch numeric string, and the switch reports
            # 0 on a dark port -- which is an absence, not a measurement.
            speed = _parse_int(port.get("speed"))
            if not speed:
                speed = None

            # PoE is TWO facts and they must not be conflated, because one is a
            # SETTING the customer changes and the other is an OBSERVATION:
            #
            #   poe_enabled  - the port's CONFIG flag (`poe_enable`): may this
            #                  port supply power. This is what
            #                  `async_set_port_poe` writes.
            #   poe_power    - `power`: is it supplying power right now. This
            #                  is what a PoE indicator should show.
            #
            # Measured on a TSW202 (2026-08-07): poe_enable is 1 on all eight
            # copper ports and 0 on both SFP cages, while power is 1 on only
            # the two ports actually feeding a device -- the same two carrying
            # a non-zero `budget`. So poe_enable tracks the PORT, and power
            # tracks the LOAD; reading either as the other gets one of the two
            # consumers wrong.
            #
            # Firmware that reports neither (the RUT routers) leaves both off.
            poe_enabled = _parse_bool(port.get("poe_enable"))
            if "power" in port:
                poe_power = _parse_bool(port.get("power"))
            else:
                # No delivery signal: the enable flag is the best we have, and
                # claiming "not powering" would be an invention.
                poe_power = poe_enabled

            ports.append({
                "name": port.get("name", ""),
                "id": port.get("id", ""),
                "position": port.get("position", port.get("num")),
                "state": "up" if up else "down",
                "speed": speed,
                "duplex": duplex,
                "poe_power": "on" if poe_power else "off",
                "poe_enabled": "on" if poe_enabled else "off",
                # Whether this port has PoE hardware at all. Only a device that
                # reports the key has it; the RUT routers omit it entirely, and
                # a port with no PoE must not be offered a PoE control.
                "poe_capable": "poe_enable" in port,
                "enabled": port.get("enabled", "off"),
                "traffic_load": port.get("traffic_load"),
                "max_traffic_load": port.get("max_traffic_load"),
                "topology": port.get("topology", []),
            })

        return {"ports": ports}


# -- Parsing helpers ----------------------------------------------------------


def _api_error_text(resp: Any) -> str:
    """RutOS's own reason for refusing a write, or the raw body.

    A failed write answers with `{"errors": [{"error": "...", "source": ...}]}`;
    surfacing that beats a bare status code when a control stops working on a
    customer's box. Falls back to a truncated body so a non-JSON error page
    still says something.

    Each error keeps its `source`, the field RutOS refused: "Invalid option"
    alone left a failed Fix on a RUT241 with no way to tell which of the
    Wi-Fi's fields the router objected to (2026-11-12).
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return (getattr(resp, "text", "") or "")[:200]
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors:
        parts = []
        for e in errors:
            if isinstance(e, str):
                parts.append(e)
            elif isinstance(e, dict):
                text = str(e.get("error") or e)
                source = e.get("source")
                parts.append(f"{text} ({source})" if source else text)
        if parts:
            return "; ".join(parts)[:300]
    return str(body)[:200]


def _fota_version_pair(raw: Any) -> tuple[str | None, bool | None]:
    """One FOTA version field as (version, already_newest).

    The field carries three different kinds of answer in one string, and they
    must not be flattened:

      "RUTX_R_00.07.25.2" -> ("RUTX_R_00.07.25.2", False)  something to install
      "newest"/"Fw_newest" -> (None, True)                 confidently current
      "N/A"/""/absent      -> (None, None)                 the router has no answer

    The third case is the one that matters: it is "could not ask", and a caller
    that treats it as the second reports a router as up to date when its FOTA
    client has never successfully reached Teltonika.
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if text.lower() in _FOTA_NEWEST_MARKERS:
        return None, True
    if text.lower() in _FOTA_NO_ANSWER:
        return None, None
    return text, False


def _parse_fota_updates(raw: Any) -> dict[str, Any]:
    """The offered-version payload, normalised.

    Shape observed on a RUTX11: `{"device": {"version", "date", "size",
    "stable_version", "stable_date", "stable_size"}}`. The whole payload can
    also arrive as the bare string "N/A" when the FOTA client has nothing, which
    is why this returns {} rather than a set of Nones: {} carries the previous
    answer forward, a set of Nones would wipe it.
    """
    device = raw.get("device") if isinstance(raw, dict) else None
    if not isinstance(device, dict):
        # Some payloads (and the modem endpoint) put the fields at the top level.
        device = raw if isinstance(raw, dict) else None
    if not isinstance(device, dict):
        return {}

    info: dict[str, Any] = {}
    for key, prefix in (("stable_version", "stable"), ("version", "latest")):
        if key not in device:
            continue
        version, newest = _fota_version_pair(device.get(key))
        info[f"fota_{prefix}_version"] = version
        info[f"fota_{prefix}_newest"] = newest
    date_keys = (("stable_date", "stable"), ("date", "latest"))
    for key, prefix in date_keys:
        if key in device:
            text = str(device.get(key) or "").strip()
            info[f"fota_{prefix}_date"] = (
                text if text and text.lower() not in _FOTA_NO_ANSWER else None
            )
    return info


def _parse_fota_config(raw: Any) -> dict[str, Any]:
    """The FOTA client's own settings: is it enabled, and which stream it tracks.

    `/fota/config` answers a list of UCI sections; `general` is the one. A
    disabled FOTA client is why a router can sit on an old build with nothing to
    report, so this is surfaced rather than inferred.
    """
    sections = raw if isinstance(raw, list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        if section.get("id") not in (None, "general"):
            continue
        return {
            "fota_enabled": _parse_bool(section.get("enabled")),
            "fota_track_latest": _parse_bool(section.get("latest")),
        }
    return {}


def _parse_fota_progress(raw: Any) -> dict[str, Any]:
    """Download progress: `{"percents": "100", "process": "succeeded"}`.

    `process` is one of started / succeeded / failed, and "succeeded" is about
    the DOWNLOAD, not the flash. It also reads "succeeded" long after a download
    that finished days ago, so it is never on its own evidence that an install
    this session worked.
    """
    if not isinstance(raw, dict):
        return {}
    info: dict[str, Any] = {}
    if "percents" in raw:
        info["fota_progress_percent"] = _parse_int(raw.get("percents"))
    if "process" in raw:
        text = str(raw.get("process") or "").strip().lower()
        info["fota_progress_process"] = text or None
    return info


def _parse_bool(value: Any) -> bool | None:
    """Parse RutOS's several spellings of a boolean, or None if it isn't one.

    One endpoint can return a real bool, an int, "1"/"0", "true"/"false" or
    "on"/"off" for the same idea across firmware families (the RUT ports
    endpoint says `duplex: "true"`, the TSW one says `full_duplex: "1"`).
    None means "the device did not tell us", which callers must keep distinct
    from a definite False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in ("1", "true", "yes", "on", "up", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "down", "disabled"):
        return False
    return None


def _iccid(value: Any) -> str:
    """An ICCID as the profile list writes it: the modem pads it with `F`."""
    return str(value or "").strip().upper().rstrip("F")


def _known(value: Any) -> str:
    """The router's value, or "" for its "N/A"."""
    text = str(value or "").strip()
    return "" if text.lower() in ("", "n/a") else text


def _sum_usage_rows(rows: Any) -> int | None:
    """Total bytes over RutOS data-usage rows.

    The spec gives rows as `[timestamp, rx, tx]`. On BP's RUTM11 the eSIM had
    carried no traffic, so only the empty list has been seen; a dict row with
    `rx`/`tx` is read too in case a build answers that way. None when the
    answer is not a list, 0 for a real empty month.
    """
    if not isinstance(rows, list):
        return None
    total = 0
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 3:
            rx, tx = _parse_float(row[1]), _parse_float(row[2])
        elif isinstance(row, dict):
            rx, tx = _parse_float(row.get("rx")), _parse_float(row.get("tx"))
        else:
            continue
        total += int((rx or 0) + (tx or 0))
    return total


def _parse_int(value: Any) -> int | None:
    """Parse a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(str(value).strip().split()[0].replace("dBm", "").replace("dB", ""))
    except (ValueError, TypeError, IndexError):
        return None


def _parse_float(value: Any) -> float | None:
    """Parse a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
