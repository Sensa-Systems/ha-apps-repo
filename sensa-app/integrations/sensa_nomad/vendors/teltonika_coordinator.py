"""Data coordinator for Teltonika router polling."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .teltonika_api import (
    _REQUEST_TIMEOUT,
    _REQUEST_TIMEOUT_DEGRADED,
    FOTA_PROCESS_FAILED,
    FOTA_PROCESS_STARTED,
    FOTA_PROCESS_SUCCEEDED,
    TeltonikaApiClient,
    TeltonikaApiError,
    TeltonikaAuthError,
    TeltonikaConnectionError,
    TeltonikaPermissionError,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)
# The floor between two WAN-only refreshes (`async_refresh_wan`), however many
# screens are asking. The card asks every 3 s while it is on screen.
_WAN_REFRESH_MIN_S = 2.0

# How often the router is asked what Teltonika is offering it. Firmware appears
# when Teltonika publishes a build, not on a 30-second cadence, and the answer
# rides three extra HTTP reads, so it gets its own slow clock. The first poll of
# a session always checks, so a restart shows a current answer immediately.
FOTA_CHECK_INTERVAL = timedelta(hours=6)

# Bounds on the download half of an install. The image is ~30 MB and comes over
# whatever WAN the van has, so this is generous; it exists so a download that
# stalls for ever cannot leave the entity stuck "installing" until HA restarts.
_FOTA_DOWNLOAD_POLL = timedelta(seconds=5)
_FOTA_DOWNLOAD_TIMEOUT = timedelta(minutes=30)
# Bounds on the reboot half. The install is not finished when the flash starts:
# the device writes the image and restarts, and only when it answers again on a
# different version has anything actually happened. A RUTX11 takes three to five
# minutes; a TSW switch has not been timed on real hardware, hence the headroom.
# The poll is slow because every one of these is a request at a device that is
# deliberately not there yet.
_FOTA_REBOOT_POLL = timedelta(seconds=15)
_FOTA_REBOOT_TIMEOUT = timedelta(minutes=15)
# Back-off ladder for an unreachable router: each consecutive failure
# stretches the next poll by this factor, capped at the ceiling. Resets
# to SCAN_INTERVAL the moment the router responds again. Keeps the log
# (and the executor pool, and the user's nerves) quiet while a router
# is genuinely offline, without giving up on recovery.
_BACKOFF_FACTOR = 2.0
_BACKOFF_CEILING = timedelta(minutes=5)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _model_slug(raw: str, product_code: str = "") -> str:
    """Normalise a Teltonika model string into a `teltonika-<slug>` key.

    "RUTX11" → "teltonika-rutx11", "TSW202" → "teltonika-tsw202". Matches
    the key shape the catalog's `model_images` map uses so `_pick_image`
    can look up the model-specific product shot.

    An eSIM build (RUTM11 eSIM) gets a `-esim` suffix when either the model
    name or the product code says eSIM. Which of the two a real eSIM unit
    reports has not been seen yet; the system-info log line prints both.
    """
    slug = _SLUG_RE.sub("-", (raw or "").strip().lower()).strip("-")
    if not slug:
        return ""
    if not slug.startswith("teltonika-"):
        slug = f"teltonika-{slug}"
    if "esim" in (product_code or "").lower() and "esim" not in slug:
        slug = f"{slug}-esim"
    return slug


def _lookup_teltonika_display_name(slug: str, subtype: str) -> str | None:
    """Return the catalog-declared display name for a Teltonika slug, or
    None if the catalog has no entry for it.

    Matches on subtype first (`router`/`switch`) so the router and switch
    catalog entries can each own their own `display_names` map without
    colliding. Callers fall back to the raw model name (`RUTX11`) so an
    unlisted model still gets a sensible title rather than the generic
    "Teltonika".
    """
    from ..device_catalog import PHYSICAL_DEVICE_CATALOG

    wanted_subtype = (subtype or "").strip().lower()
    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("integration") != "sensa_teltonika_api":
            continue
        if (entry.get("vendor_device_type") or "").lower() != wanted_subtype:
            continue
        detection = entry.get("model_detection") or {}
        display_names = detection.get("display_names") or {}
        name = display_names.get(slug)
        if name:
            return name
    return None


def _mac_tag(mac: str | None) -> str:
    """Return the last 4 hex chars of a MAC for fleet disambiguation."""
    cleaned = (mac or "").replace(":", "").replace("-", "").upper()
    return cleaned[-4:] if len(cleaned) >= 4 else cleaned


class TeltonikaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls a Teltonika router via the REST API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: TeltonikaApiClient,
        device_id: str,
        sensa_client: Any = None,
        mac_address: str | None = None,
        hostname: str | None = None,
        name_locked: bool = False,
    ) -> None:
        self.client = client
        self.device_id = device_id
        self._sensa_client = sensa_client
        # True once the customer has renamed this router / switch. Read
        # from the backend record at startup, because `_enriched` lives
        # only as long as this object and every restart would otherwise
        # rewrite the catalog name over the customer's.
        self._name_locked = name_locked
        self._mac_address = mac_address or ""
        # Hostname from DHCP is the user-facing disambiguator for
        # fleet installs (1 router + N switches) — we append it in
        # brackets after the classified model name so the picker shows
        # `Teltonika RUTX11 (BP-RUTX11)` rather than a MAC-tag suffix.
        # Falls back to `#XXXX` when hostname is absent/generic.
        self._hostname = hostname or ""
        self._enriched = False
        # Consecutive-failure counter — drives the back-off ladder in
        # _async_update_data. Reset to 0 on the first successful poll.
        self._fail_streak = 0
        # What Teltonika's FOTA server last told this router, on its own slow
        # clock (FOTA_CHECK_INTERVAL). Held here rather than re-read every poll,
        # and merged into every `data` dict so entities see one shape. Kept
        # across a failed check, because a failed check is "could not ask" and
        # the previous answer is still the best one we have.
        self._fota: dict[str, Any] = {}
        self._fota_checked: datetime | None = None
        # The detached read started from a poll, so a second poll arriving while
        # the first is still waiting on Teltonika does not start another.
        self._fota_task: Any = None
        # Live state of an install THIS session is running: the percentage the
        # router last reported and whether we are still driving it. The router's
        # own progress endpoint reads "succeeded" for a download that finished
        # days ago, so it can never stand in for this.
        self._install_active = False
        self._install_percent: int | None = None
        # True when the router refused the last install this session drove with
        # a 403: the user Sensa signs in as may not update firmware. The entity
        # reads it to tell the customer something they can act on.
        self.install_forbidden = False
        # Explicit baseline interval — DataUpdateCoordinator's __init__
        # also writes `self.update_interval = update_interval`, so this
        # is set twice in production, but the back-off path mutates it
        # in place and tests against a stubbed-out super().__init__ rely
        # on this attribute existing without HA actually constructing.
        self.update_interval = SCAN_INTERVAL
        # monotonic() of the last WAN-only refresh, for its rate floor.
        self._wan_refreshed = 0.0
        # The last GPS answer and when it was read. GPS runs on its own clock
        # (async_refresh_gps), so the status poll carries these keys over
        # rather than dropping them every 30 s.
        self._gps: dict[str, Any] = {}
        self._gps_read = 0.0
        # A read in flight: at a 1 s refresh a slow answer must not start a
        # second read on top of it.
        self._gps_busy = False
        # eSIM: what MySensa says is left on each eSIM Sensa sold (by ICCID),
        # when it last answered, and the order in progress. Read by
        # `TeltonikaEsim`; written by `teltonika_esim`.
        self.esim_balance: dict[str, dict[str, Any]] = {}
        self.esim_balance_at: float | None = None
        self.esim_order: dict[str, Any] = {}
        self._esim_task: Any = None
        self._esim_resumed = False
        super().__init__(
            hass,
            _LOGGER,
            name=f"teltonika_{device_id}",
            update_interval=SCAN_INTERVAL,
        )

    def update_client(self, client: TeltonikaApiClient) -> None:
        """Swap in a freshly-credentialled API client, preserving this
        coordinator object (and therefore every entity bound to it).

        Used when the customer edits username / password / SSL on an
        already-running device. Recreating the coordinator would orphan
        every entity on the dead object — they stay subscribed to the old
        coordinator (old credentials) and go unavailable, while the new
        coordinator polls successfully with nothing listening. Rebinding
        the client in place keeps the entity subscriptions intact. The
        back-off state is reset so a device that was failing on the old
        credentials gets a clean, full-cadence retry with the new ones.
        """
        self.client = client
        self._fail_streak = 0
        self.update_interval = SCAN_INTERVAL
        self.client.set_request_timeout(_REQUEST_TIMEOUT)

    async def async_refresh_wan(self) -> bool:
        """Re-read only the WAN order and state, while a customer is looking.

        The router cannot tell us its failover order changed (RutOS has no
        push), so the Internet card asks every few seconds while it is on
        screen, and a change made in the RutOS web UI shows in seconds rather
        than at the next 30 s poll. Deliberately NOT a coordinator refresh:
        that re-reads every endpoint, and `async_set_updated_data` would
        reschedule the regular poll, starving signal and port data for as
        long as the card is open. This merges the WAN keys into the current
        data and notifies the entities, leaving the poll's clock alone.

        Skipped (False) when a refresh ran under `_WAN_REFRESH_MIN_S` ago,
        before the first poll, while the router is failing (the back-off owns
        that), or when nothing changed.
        """
        now = time.monotonic()
        if now - self._wan_refreshed < _WAN_REFRESH_MIN_S:
            return False
        self._wan_refreshed = now
        if not self.data or self._fail_streak:
            return False
        try:
            wan = await self.client.async_get_wan_data()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Teltonika %s: WAN refresh failed: %s", self.device_id, err)
            return False
        if not wan:
            return False
        merged = {**self.data, **wan}
        if merged == self.data:
            return False
        self.data = merged
        self.async_update_listeners()
        return True

    async def async_refresh_gps(self, interval_s: float) -> bool:
        """Read the position if `interval_s` has passed since the last read.

        Called every few seconds by a timer; the customer's GPS refresh is
        `interval_s`. Like `async_refresh_wan`, it merges its keys and notifies
        the entities without rescheduling the status poll. Skipped before the
        first poll, while the router is failing (the back-off owns that), and
        on a router with no GPS.
        """
        now = time.monotonic()
        # 0.1 s of slack: a 1 s timer does not land exactly 1 s apart.
        if self._gps_busy or now - self._gps_read < interval_s - 0.1:
            return False
        if not self.data or self._fail_streak or not self.data.get("hw_gps"):
            return False
        self._gps_read = now
        self._gps_busy = True
        try:
            gps = await self.client.async_get_gps()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Teltonika %s: GPS read failed: %s", self.device_id, err)
            return False
        finally:
            self._gps_busy = False
        if not gps:
            return False
        self._gps = gps
        merged = {**self.data, **gps}
        if merged == self.data:
            return False
        self.data = merged
        self.async_update_listeners()
        return True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the Teltonika router.

        Failure handling has a back-off ladder. Healthy state polls every
        SCAN_INTERVAL with the normal 10 s per-request timeout. The first
        failure flips the client to the degraded timeout (1.5 s) so the
        retry cycle doesn't stack 10 s waits across N endpoints, and the
        next-poll interval doubles each consecutive failure up to a
        5-minute ceiling. First-failure logs at WARNING; everything from
        the second consecutive failure onwards drops to DEBUG so a router
        that's offline overnight doesn't fill the log. Any successful
        poll resets the streak, restores the healthy timeout, and brings
        the poll cadence back to SCAN_INTERVAL.
        """
        try:
            data = await self.client.async_get_router_data()
        except TeltonikaAuthError as err:
            self._on_failure()
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except TeltonikaConnectionError as err:
            self._on_failure()
            raise UpdateFailed(f"Connection failed: {err}") from err
        except TeltonikaApiError as err:
            self._on_failure()
            raise UpdateFailed(f"API error: {err}") from err

        self._on_success()

        # What Teltonika is offering, on its own slow clock. Both routers and
        # switches: they run different firmware families but the same REST
        # surface, and a switch that answers none of it simply reports no
        # firmware answer (three 404s every six hours, not every poll).
        self._kick_fota_check()
        data.update(self._fota)
        data.update(self._gps)
        if data.get("esim"):
            self._kick_esim_balance()

        # One-shot enrichment — push the concrete model slug + subtype
        # into the backend device's config so `_pick_image` (model_images
        # lookup) and the Discovery picker's filter (device_subtype pill)
        # have real values to work from. Safe to skip if no Sensa client
        # is wired in (e.g. testing scenarios).
        if not self._enriched and self._sensa_client is not None:
            await self._maybe_enrich(data)

        return data

    # -- eSIM: what is left on it, and an order a restart interrupted --------

    @callback
    def _kick_esim_balance(self) -> None:
        """Ask MySensa what is left on the eSIMs, on its own slow clock and
        detached, like the FOTA read: it is a round trip to eSIM Go. The first
        poll also picks up an order a restart interrupted."""
        from .teltonika_esim import BALANCE_INTERVAL_S, async_refresh_balance, orders

        if self._sensa_client is None:
            return
        if not self._esim_resumed:
            self._esim_resumed = True
            self.hass.async_create_task(
                orders(self.hass).async_resume(self.device_id, self, self._sensa_client)
            )
        if self._esim_task is not None and not self._esim_task.done():
            return
        if (
            self.esim_balance_at is not None
            and time.monotonic() - self.esim_balance_at < BALANCE_INTERVAL_S
        ):
            return

        async def _refresh() -> None:
            before = dict(self.esim_balance)
            await async_refresh_balance(self, self._sensa_client)
            # Asked, answered or not: wait the full interval before asking again.
            self.esim_balance_at = time.monotonic()
            if self.esim_balance != before:
                self.async_update_listeners()

        self._esim_task = self.hass.async_create_task(_refresh())

    # -- Firmware: what the router is offered, and installing it -------------

    def _fota_due(self) -> bool:
        """Has the FOTA answer's slow clock come round?"""
        if self._fota_checked is None:
            return True
        return datetime.now(timezone.utc) - self._fota_checked >= FOTA_CHECK_INTERVAL

    @callback
    def _kick_fota_check(self) -> None:
        """Start a due FOTA read WITHOUT holding up the poll that noticed it.

        The offered-version read does not answer from the router: `rut_fota`
        asks Teltonika's servers and holds the request open until that round
        trip finishes, which is tens of seconds. Awaiting it inside the poll
        would push a 30-second cycle well past its own interval every six
        hours, so it runs detached and pushes its answer when it lands. A poll
        that arrives meanwhile carries the previous answer, which is the same
        thing every poll between checks already does.
        """
        if self._fota_task is not None and not self._fota_task.done():
            return
        if not self._fota_due():
            return
        self._fota_task = self.hass.async_create_task(self._async_fota_check_push())

    async def _async_fota_check_push(self) -> None:
        """The detached read, and the push that carries its answer out."""
        before = dict(self._fota)
        await self._maybe_check_fota()
        if self._fota != before and self.data is not None:
            self.async_set_updated_data({**self.data, **self._fota})

    async def _maybe_check_fota(self) -> None:
        """Refresh the FOTA answer when its slow clock says it is due.

        Never raises: a firmware check that fails must not fail the poll that
        carries every sensor on the device. A failure leaves the previous answer
        in place and says so once at DEBUG (the client has already logged which
        of the three reads failed).
        """
        now = datetime.now(timezone.utc)
        if not self._fota_due():
            return
        try:
            info = await self.client.async_get_fota_info()
        except Exception:
            _LOGGER.debug(
                "Teltonika %s: FOTA check failed; keeping the previous answer",
                self.device_id, exc_info=True,
            )
            return
        # Stamp the clock only on a check that came back, so a router that was
        # unreachable is asked again on the next poll rather than in six hours.
        self._fota_checked = now
        if info:
            self._fota.update(info)

    async def async_refresh_fota(self) -> dict[str, Any]:
        """Force a FOTA re-read now and push it to the entities.

        Used after an install finishes, and by the update entity's own refresh,
        so the offered version stops advertising a build that is now installed.
        """
        self._fota_checked = None
        await self._maybe_check_fota()
        if self.data is not None:
            self.async_set_updated_data({**self.data, **self._fota})
        return dict(self._fota)

    @property
    def install_in_progress(self) -> bool:
        """Is THIS session driving a firmware install right now."""
        return self._install_active

    @property
    def install_percent(self) -> int | None:
        """Download progress of the install this session is driving."""
        return self._install_percent if self._install_active else None

    async def async_install_firmware(
        self, stream: str, *, keep_settings: bool = True,
    ) -> bool:
        """Download, verify, flash and wait out the reboot. True once RUNNING.

        Five steps, in this order, because RutOS separates them and collapsing
        them loses the one check that matters:

          1. start the download of `stream` (stable or latest)
          2. poll until the router says the download succeeded
          3. verify the downloaded image, and read back whether it is NEWER
          4. flash it, keeping settings
          5. wait for the device to come back running a different version

        Step 5 is what makes this call mean "installed" rather than "started".
        Everything up to the flash can succeed on a device that then fails to
        boot the new image, and returning at step 4 reported that as a finished
        install: the spinner stopped, the version did not move, and the only
        way to find out was to look again later. It costs the caller the reboot,
        which is the point.

        Step 3 is the guard that makes this safe to automate. `verify` is the
        only place the router states what it actually downloaded, and a
        `newer != "1"` answer aborts: a same-or-older image is a reflash or a
        downgrade, neither of which anything here should ever start, and RutOS
        cannot carry settings across a downgrade anyway.

        Returns False on every refusal and failure, having logged the reason.
        The caller owns the customer-facing log line.
        """
        if self._install_active:
            _LOGGER.warning(
                "Teltonika %s: a firmware install is already running; ignoring",
                self.device_id,
            )
            return False

        self._install_active = True
        self._install_percent = None
        self.install_forbidden = False
        self.async_update_listeners()
        try:
            return await self._async_run_install(stream, keep_settings)
        finally:
            self._install_active = False
            self._install_percent = None
            self.async_update_listeners()

    async def _async_run_install(self, stream: str, keep_settings: bool) -> bool:
        # Read before anything is started: this is what the reboot wait compares
        # against to tell a finished flash from a device that is merely back.
        installed_before = str((self.data or {}).get("firmware_version") or "").strip()
        try:
            await self.client.async_fota_download(stream)
        except TeltonikaApiError as err:
            self.install_forbidden = isinstance(err, TeltonikaPermissionError)
            _LOGGER.warning(
                "Teltonika %s: could not start the %s firmware download: %s",
                self.device_id, stream, err,
            )
            return False

        if not await self._async_await_download():
            return False

        try:
            verdict = await self.client.async_fota_verify()
        except TeltonikaApiError as err:
            self.install_forbidden = isinstance(err, TeltonikaPermissionError)
            _LOGGER.warning(
                "Teltonika %s: downloaded firmware could not be verified: %s",
                self.device_id, err,
            )
            return False

        if str(verdict.get("valid") or "") != "1":
            _LOGGER.warning(
                "Teltonika %s: downloaded firmware failed verification "
                "(valid=%r, message_code=%r); not flashing",
                self.device_id, verdict.get("valid"), verdict.get("message_code"),
            )
            return False
        if str(verdict.get("newer") or "") != "1":
            _LOGGER.warning(
                "Teltonika %s: downloaded firmware is not newer than the "
                "installed build (newer=%r); not flashing",
                self.device_id, verdict.get("newer"),
            )
            return False

        try:
            await self.client.async_fota_upgrade(keep_settings)
        except TeltonikaApiError as err:
            self.install_forbidden = isinstance(err, TeltonikaPermissionError)
            _LOGGER.warning(
                "Teltonika %s: firmware flash could not be started: %s",
                self.device_id, err,
            )
            return False

        _LOGGER.info(
            "Teltonika %s: flashing %s firmware (keep settings: %s); the router "
            "reboots to finish and the poller will back off until it answers",
            self.device_id, stream, "yes" if keep_settings else "no",
        )
        return await self._async_await_reboot(installed_before)

    async def _async_await_reboot(self, previous: str) -> bool:
        """Wait until the device answers again on a build that is not
        `previous`. False on timeout.

        A read that FAILS is the expected answer for most of this: the device is
        mid-flash and off the network, so every error is swallowed and the poll
        simply comes round again. A read that SUCCEEDS and still names the old
        version is the window between the upgrade call and the restart, and is
        waited out the same way.

        `previous` empty means the installed version was never read, so there is
        nothing to compare against and nothing this can confirm. It waits for the
        device to answer at all and takes that, which is weaker than the version
        check but is the whole of what is knowable.
        """
        deadline = datetime.now(timezone.utc) + _FOTA_REBOOT_TIMEOUT
        while datetime.now(timezone.utc) < deadline:
            await asyncio.sleep(_FOTA_REBOOT_POLL.total_seconds())
            try:
                version = await self.client.async_get_firmware_version()
            except TeltonikaApiError:
                # Down, which is what a device in the middle of a flash does.
                continue
            except Exception:
                _LOGGER.debug(
                    "Teltonika %s: version read failed while waiting for the "
                    "reboot", self.device_id, exc_info=True,
                )
                continue
            if not version:
                continue
            if previous and version == previous:
                continue
            _LOGGER.info(
                "Teltonika %s: back up on firmware %s", self.device_id, version,
            )
            return True

        _LOGGER.warning(
            "Teltonika %s: the device did not come back on a new firmware "
            "version within %s; the flash may still be finishing",
            self.device_id, _FOTA_REBOOT_TIMEOUT,
        )
        return False

    async def _async_await_download(self) -> bool:
        """Poll the download to completion. False on failure or timeout."""
        deadline = datetime.now(timezone.utc) + _FOTA_DOWNLOAD_TIMEOUT
        while datetime.now(timezone.utc) < deadline:
            await asyncio.sleep(_FOTA_DOWNLOAD_POLL.total_seconds())
            try:
                progress = await self.client.async_get_fota_progress()
            except TeltonikaApiError as err:
                # A single missed read mid-download is not a failure: the router
                # is busy writing 30 MB to flash. Keep polling until the
                # deadline, which is what bounds this.
                _LOGGER.debug(
                    "Teltonika %s: progress read failed mid-download: %s",
                    self.device_id, err,
                )
                continue

            percent = progress.get("fota_progress_percent")
            process = progress.get("fota_progress_process")
            if isinstance(percent, int) and percent != self._install_percent:
                self._install_percent = percent
                self.async_update_listeners()

            if process == FOTA_PROCESS_FAILED:
                _LOGGER.warning(
                    "Teltonika %s: firmware download failed at %s%%",
                    self.device_id, percent if percent is not None else "?",
                )
                return False
            if process == FOTA_PROCESS_SUCCEEDED and percent == 100:
                return True
            if process not in (FOTA_PROCESS_STARTED, FOTA_PROCESS_SUCCEEDED):
                # Not a failure state and not one we know: keep waiting, but say
                # so, because a new RutOS spelling would otherwise be invisible.
                _LOGGER.debug(
                    "Teltonika %s: unrecognised download process %r at %s%%",
                    self.device_id, process,
                    percent if percent is not None else "?",
                )

        _LOGGER.warning(
            "Teltonika %s: firmware download did not finish within %s; "
            "abandoning (the router keeps whatever it downloaded)",
            self.device_id, _FOTA_DOWNLOAD_TIMEOUT,
        )
        return False

    def _on_failure(self) -> None:
        """Apply the back-off ladder on a failed poll.

        Increments the streak, lengthens the next poll interval (doubling
        capped at `_BACKOFF_CEILING`), and drops the per-request timeout
        to the degraded value so subsequent polls fail fast.
        """
        self._fail_streak += 1
        next_seconds = min(
            SCAN_INTERVAL.total_seconds() * (_BACKOFF_FACTOR ** self._fail_streak),
            _BACKOFF_CEILING.total_seconds(),
        )
        self.update_interval = timedelta(seconds=next_seconds)
        self.client.set_request_timeout(_REQUEST_TIMEOUT_DEGRADED)
        if self._fail_streak == 1:
            _LOGGER.warning(
                "Teltonika %s: router unreachable; backing off polls and "
                "shortening per-request timeout. Will keep retrying.",
                self.device_id,
            )
        else:
            _LOGGER.debug(
                "Teltonika %s: failure streak %d; next poll in %.0fs",
                self.device_id, self._fail_streak, next_seconds,
            )

    def _on_success(self) -> None:
        """Reset the back-off state on the first successful poll after a
        failure streak. Logs the recovery once at INFO so the log shows a
        bookend pair (offline → online) rather than the offline warning
        alone."""
        if self._fail_streak == 0:
            return
        _LOGGER.info(
            "Teltonika %s: router reachable again after %d failed poll(s); "
            "back to normal cadence.",
            self.device_id, self._fail_streak,
        )
        self._fail_streak = 0
        self.update_interval = SCAN_INTERVAL
        self.client.set_request_timeout(_REQUEST_TIMEOUT)

    async def _maybe_enrich(self, data: dict[str, Any]) -> None:
        model_raw = str(data.get("device_model") or "").strip()
        product_code = str(data.get("device_product_code") or "").strip()
        model_slug = _model_slug(model_raw, product_code)
        hw_mobile = bool(data.get("hw_mobile", False))
        device_subtype = "router" if hw_mobile else "switch"

        # Wait until we have at least the model name — if the first poll
        # came back without system-info (e.g. auth race), try again next
        # cycle.
        if not model_slug:
            return

        config_update: dict[str, Any] = {
            "model": model_slug,
            "device_subtype": device_subtype,
            "device_model_raw": model_raw,
        }
        serial = data.get("device_serial")
        if serial:
            config_update["serial_number"] = str(serial)
        firmware = data.get("firmware_version")
        if firmware:
            config_update["fw_version"] = str(firmware)
        platform = data.get("device_platform")
        if platform:
            config_update["platform"] = str(platform)
        if product_code:
            config_update["product_code"] = product_code

        # Hardware capability flags — downstream code uses these to gate
        # entities (GPS only on hw_gps, PoE port controls only on hw_poe,
        # etc.) and to drive tier checks (e.g. GPS is Pro-tier only, so
        # we don't bother creating GPS sensors on hw_gps=False anyway).
        # Written once on first poll; if the router's hardware ever
        # changes (it won't, realistically) re-enrichment would need a
        # force-refresh anyway.
        for src, dst in (
            ("hw_mobile", "hw_mobile"),
            ("hw_gps", "hw_gps"),
            ("hw_poe", "hw_poe"),
            ("hw_wifi", "hw_wifi"),
            ("hw_bluetooth", "hw_bluetooth"),
            ("hw_dual_sim", "hw_dual_sim"),
            ("hw_usb", "hw_usb"),
        ):
            if src in data:
                config_update[dst] = bool(data.get(src))

        # Catalog-driven model name — "Teltonika RUTX11". Falls back to the raw
        # model string so an unlisted model (e.g. a future RUTX12 before the
        # catalog is updated) still gets a sensible title. The brand prefix rule
        # lives in `teltonika_model_label`, shared with the HA device name, so a
        # switch that self-reports its own brand is not prefixed twice.
        model_name = _lookup_teltonika_display_name(model_slug, device_subtype)
        if not model_name:
            from ..const import teltonika_model_label

            model_name = teltonika_model_label(model_raw)

        # Disambiguator — hostname in brackets when informative, MAC tag
        # otherwise. Uses the same hybrid rule as pre-add discovery rows
        # (see config_flow._build_discovery_name) so the name the customer
        # sees before adding survives through enrichment.
        from ..config_flow import _build_discovery_name
        display_name = _build_discovery_name(
            model_name, self._hostname, self._mac_address,
        )

        # A customer rename owns the name from then on; the config half
        # (model slug, capability flags) still runs.
        write_name = not self._name_locked
        try:
            await self._sensa_client.async_update_device(
                device_id=self.device_id,
                config=config_update,
                **({"display_name": display_name} if write_name else {}),
            )
            self._enriched = True
            # Push the friendly name into HA's device registry too.
            # device_info["name"] alone only sets the name when the
            # device is first registered — subsequent device_info
            # changes don't repaint the name on already-existing rows
            # in `/config/devices/dashboard` or
            # `/config/integrations/integration/sensa_nomad`. Customer
            # would still see "Teltonika a3b1c2" until the integration
            # was reloaded. Update the registry directly so the new
            # name appears immediately.
            if write_name:
                self._update_ha_device_name(display_name)
            caps = [k for k in (
                "hw_mobile", "hw_gps", "hw_poe", "hw_wifi",
                "hw_bluetooth", "hw_dual_sim", "hw_usb",
            ) if config_update.get(k)]
            _LOGGER.info(
                "Teltonika %s: enriched model=%s subtype=%s display=%r caps=%s",
                self.device_id, model_slug, device_subtype,
                display_name if write_name else "(kept)",
                ",".join(caps) or "none",
            )
        except Exception:
            _LOGGER.warning(
                "Teltonika %s: backend enrichment failed",
                self.device_id, exc_info=True,
            )
            return

        # Nudge the picker to re-render without waiting for the main
        # coordinator's next 30s poll — customer sees the upgraded name
        # and the router/switch pill appear within a second of the first
        # successful Teltonika poll, not after they hit Scan.
        await self._nudge_picker_refresh()

    def _update_ha_device_name(self, new_name: str) -> None:
        """Push a friendly name into HA's device registry for this router.

        HA tracks two name fields per device: `name` (what the
        integration declared via device_info on first registration)
        and `name_by_user` (the customer's manual override). We only
        touch `name` — `name_by_user` always wins, so a customer who
        renamed the device in HA still sees their override.
        """
        try:
            from homeassistant.helpers.device_registry import (
                async_get as _dr_async_get,
            )
        except ImportError:
            return
        try:
            from ..const import DOMAIN as NOMAD_DOMAIN
        except ImportError:
            return
        try:
            registry = _dr_async_get(self.hass)
        except Exception:
            _LOGGER.debug(
                "Teltonika %s: device registry lookup failed",
                self.device_id, exc_info=True,
            )
            return
        device = registry.async_get_device(
            identifiers={(NOMAD_DOMAIN, f"teltonika_{self.device_id}")},
        )
        if device is None:
            _LOGGER.debug(
                "Teltonika %s: HA device entry not found for rename",
                self.device_id,
            )
            return
        if device.name == new_name:
            return
        try:
            registry.async_update_device(device.id, name=new_name)
            _LOGGER.info(
                "Teltonika %s: HA device renamed to %r",
                self.device_id, new_name,
            )
        except Exception:
            _LOGGER.debug(
                "Teltonika %s: HA device rename failed",
                self.device_id, exc_info=True,
            )

    async def _nudge_picker_refresh(self) -> None:
        """Force an immediate Discovery sensor re-render.

        Iterates the Sensa Nomad integration runtimes (there's only ever
        one) and triggers the same quick-refresh path that add/remove
        uses. Guarded so a missing runtime / sensor doesn't break
        enrichment.
        """
        try:
            from ..const import DOMAIN

            for runtime in self.hass.data.get(DOMAIN, {}).values():
                if not isinstance(runtime, dict):
                    continue
                main_coord = runtime.get("coordinator")
                if main_coord is not None:
                    try:
                        await main_coord.async_request_refresh()
                    except Exception:
                        _LOGGER.debug(
                            "Teltonika %s: main coordinator refresh failed "
                            "post-enrichment",
                            self.device_id, exc_info=True,
                        )
                discovery_sensor = runtime.get("discovery_sensor")
                if discovery_sensor is not None:
                    try:
                        await discovery_sensor.async_quick_refresh()
                    except Exception:
                        _LOGGER.debug(
                            "Teltonika %s: picker quick-refresh failed "
                            "post-enrichment",
                            self.device_id, exc_info=True,
                        )
        except Exception:
            _LOGGER.debug(
                "Teltonika %s: picker refresh nudge raised",
                self.device_id, exc_info=True,
            )
