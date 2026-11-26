"""Teltonika RutOS network provisioning.

Applies the van network layout in `network_profile.json` (VLANs, firewall
zones, SSIDs, DHCP, users) to a RutOS 7 router over its REST API.

Two callers, one implementation:

  - `nomad/tools/provision_router.py`, a standalone CLI that runs on a laptop
    with no Home Assistant present. A router usually needs configuring before
    there is a box to run anything on.
  - The Teltonika device card in the Devices tab, which drives the same
    functions from an executor thread.

**Standard library only, deliberately.** No `requests`, no `aiohttp`, no
`homeassistant` import anywhere in this module. The CLI has to run on a
support laptop with a bare Python install and no network access to PyPI, and
adding an install step to a customer visit is how a customer visit fails.
That constraint is why this talks `urllib.request` rather than reusing the
session handling in `teltonika_api.py`.

Transport notes, all of which mirror `teltonika_api.py` for the same reasons:

  - HTTPS only. RutOS answers `/api` over HTTP with a 307 to HTTPS, so plain
    HTTP is not a fallback.
  - The certificate is self-signed, so verification is off. This is a LAN
    conversation with a device whose address the operator typed.
  - Redirects are not followed, so an unexpected 307 surfaces instead of
    being silently chased.
  - The bearer token has a short TTL (5 minutes, observed), so any call can
    get a 401 and re-login once.

Nothing in this module writes unless it is handed a plan and told to apply
it. `probe()` is read-only by construction: it issues GET only.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# The API is HTTPS-only and the token is short-lived. Both are observed
# behaviour on RutOS 7, recorded in docs/devices/teltonika-router.md.
DEFAULT_TIMEOUT = 15
LOGIN_PATH = "/api/login"

# Endpoints Sensa already polls in production, so these are known to exist.
# `teltonika_api.py` reads all of them on every coordinator tick.
KNOWN_READ_ENDPOINTS = (
    "/api/system/device/status",
    "/api/firmware/device/status",
    "/api/ports_settings/status",
    "/api/modems/status",
    "/api/interfaces/status",
    "/api/wireless/interfaces/status",
    "/api/gps/position/status",
)

# Endpoints provisioning WOULD need. These are candidates, not facts: the
# RutOS API surface is versioned per model and per firmware, and no public
# document was found that pins them for an arbitrary RUT. `probe()` exists
# precisely to replace this list with observed reality, so do not treat a
# name here as confirmed until a probe report says so.
CANDIDATE_WRITE_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "vlan": (
        "/api/network/vlan/config",
        "/api/network/vlans/config",
        "/api/switch/vlan/config",
    ),
    "interfaces": (
        "/api/network/interfaces/config",
        "/api/interfaces/config",
    ),
    "dhcp": (
        "/api/dhcp/servers/config",
        "/api/network/dhcp/config",
    ),
    "firewall_zones": (
        "/api/firewall/zones/config",
        "/api/network/firewall/zones/config",
    ),
    "firewall_forwardings": (
        "/api/firewall/forwardings/config",
        "/api/firewall/zone_forwardings/config",
    ),
    "firewall_rules": (
        "/api/firewall/rules/config",
        "/api/network/firewall/rules/config",
    ),
    "attack_prevention": (
        "/api/firewall/attack_prevention/config",
        "/api/network/firewall/attack_prevention/config",
    ),
    "wireless_interfaces": (
        "/api/wireless/interfaces/config",
    ),
    "wireless_devices": (
        "/api/wireless/devices/status",
        "/api/wireless/devices/config",
    ),
    "users": (
        "/api/users/config",
        "/api/system/users/config",
    ),
    "routes": (
        "/api/network/routes/config",
        "/api/routes/config",
        "/api/network/static_routes/config",
    ),
    "access_control": (
        "/api/system/access_control/config",
        "/api/access_control/config",
    ),
    "mdns": (
        "/api/services/mdns/config",
        "/api/mdns/config",
        "/api/services/avahi/config",
    ),
}

# Enumeration endpoints. `/api/endpoints` answered 401 rather than 404 on an
# unauthenticated probe, which means it exists; with a token it may hand back
# the whole surface and make every guess above unnecessary.
DISCOVERY_ENDPOINTS = (
    "/api/endpoints",
    "/api/openapi.json",
    "/api/swagger.json",
    "/api/docs",
)


class ProvisionError(Exception):
    """A provisioning step could not be completed."""


class AuthError(ProvisionError):
    """Login was refused, or the token could not be refreshed."""


@dataclass
class Response:
    """One HTTP answer, kept whole so a 4xx body can be read.

    A 4xx is not an exception here. RutOS answers a refusal with a structured
    body naming the reason (`{"success": false, "errors": [...]}`), and that
    body is the most useful thing about the failure. Raising on it would throw
    the diagnosis away.
    """

    status: int
    body: Any
    raw: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error_text(self) -> str:
        """Best available description of a failure, for an operator to read."""
        if isinstance(self.body, dict):
            errors = self.body.get("errors")
            if isinstance(errors, list) and errors:
                parts = []
                for err in errors:
                    if isinstance(err, dict):
                        source = err.get("source", "")
                        code = err.get("code", "")
                        text = err.get("error", "")
                        parts.append(f"{source} {code}: {text}".strip())
                    else:
                        parts.append(str(err))
                return "; ".join(parts)
        return self.raw[:400] if self.raw else f"HTTP {self.status}"


@dataclass
class RouterClient:
    """Minimal RutOS REST client over the standard library.

    Deliberately not a subclass or a wrapper of `TeltonikaApiClient`: that one
    takes a `hass` and a `requests` session, neither of which exists on a
    support laptop.
    """

    host: str
    username: str
    password: str
    timeout: int = DEFAULT_TIMEOUT
    verify_tls: bool = False

    _token: str | None = field(default=None, init=False, repr=False)

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    def _ssl_context(self) -> ssl.SSLContext:
        if self.verify_tls:
            return ssl.create_default_context()
        # The router presents a self-signed certificate. This is a LAN
        # conversation with a device whose address the operator typed in, so
        # the trust decision has already been made by hand.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _raw_request(
        self,
        method: str,
        path: str,
        body: Any = None,
        token: str | None = None,
    ) -> Response:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        ctx = self._ssl_context()

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return Response(resp.status, _try_json(raw), raw)
        except urllib.error.HTTPError as exc:
            # A 4xx carries the reason in its body. Read it rather than
            # letting urllib turn it into a bare exception.
            raw = exc.read().decode("utf-8", errors="replace")
            return Response(exc.code, _try_json(raw), raw)
        except urllib.error.URLError as exc:
            raise ProvisionError(
                f"Cannot reach {url}: {exc.reason}. "
                "A timeout on 443 while other devices reach the WebUI fine is "
                "the signature of the router's Attack Prevention having banned "
                "this machine's IP after failed logins."
            ) from exc

    def login(self) -> None:
        resp = self._raw_request(
            "POST", LOGIN_PATH, {"username": self.username, "password": self.password}
        )
        if not resp.ok:
            raise AuthError(f"Login refused: {resp.error_text()}")

        token = None
        if isinstance(resp.body, dict):
            data = resp.body.get("data")
            if isinstance(data, dict):
                token = data.get("token")
            token = token or resp.body.get("token")
        if not token:
            raise AuthError(
                f"Login returned {resp.status} but no token could be found in "
                f"the response: {resp.raw[:200]}"
            )
        self._token = token

    def request(self, method: str, path: str, body: Any = None) -> Response:
        """Issue a request, logging in first and re-logging once on a 401."""
        if self._token is None:
            self.login()
        resp = self._raw_request(method, path, body, token=self._token)
        if resp.status == 401:
            # Token TTL is short; one silent refresh, then surface the result.
            self.login()
            resp = self._raw_request(method, path, body, token=self._token)
        return resp

    def get(self, path: str) -> Response:
        return self.request("GET", path)


def _try_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


@dataclass
class ProbeResult:
    """What one candidate endpoint actually did.

    `exists` alone is not enough to act on. A 403 means the endpoint is there
    and this account may not use it, which is fixed by granting permission; a
    404 or 501 means the firmware does not implement it at all, which is fixed
    by finding another route or a different path. Collapsing those two into
    one boolean sends you off fixing the wrong thing.
    """

    name: str
    path: str
    status: int
    exists: bool
    note: str = ""

    @property
    def verdict(self) -> str:
        """One of: available, forbidden, absent, error."""
        if self.exists:
            return "available"
        if self.status in (401, 403):
            return "forbidden"
        if self.status in (404, 501):
            return "absent"
        return "error"


def probe(client: RouterClient) -> dict[str, Any]:
    """Read-only capability report. Issues GET only, writes nothing.

    Answers the question the provisioning design cannot proceed without: does
    this firmware expose the config endpoints needed to create VLANs, firewall
    zones, DHCP pools, SSIDs and users, or does it only expose the read paths
    Sensa already polls?

    Safe to run against a customer's router.
    """
    report: dict[str, Any] = {
        "host": client.host,
        "device": {},
        "discovery": [],
        "known_reads": [],
        "candidates": {},
    }

    # Model and firmware first: the API surface is versioned per model, so
    # every other result in this report is only meaningful alongside these.
    dev = client.get("/api/system/device/status")
    if dev.ok and isinstance(dev.body, dict):
        report["device"] = dev.body.get("data", dev.body)
    else:
        report["device"] = {"_error": dev.error_text(), "_status": dev.status}

    fw = client.get("/api/firmware/device/status")
    if fw.ok and isinstance(fw.body, dict):
        report["firmware"] = fw.body.get("data", fw.body)

    # If an enumeration endpoint answers, it replaces every guess below.
    for path in DISCOVERY_ENDPOINTS:
        resp = client.get(path)
        report["discovery"].append(
            ProbeResult(
                name="discovery",
                path=path,
                status=resp.status,
                exists=resp.ok,
                note="" if resp.ok else resp.error_text()[:120],
            )
        )
        if resp.ok:
            report.setdefault("enumerated", {})[path] = resp.body

    # Confirm the endpoints we already rely on, so a failure here tells the
    # operator the problem is credentials or Access Control, not coverage.
    for path in KNOWN_READ_ENDPOINTS:
        resp = client.get(path)
        report["known_reads"].append(
            ProbeResult(
                name="known_read",
                path=path,
                status=resp.status,
                exists=resp.ok,
                note="" if resp.ok else resp.error_text()[:120],
            )
        )

    # The actual question.
    for capability, paths in CANDIDATE_WRITE_ENDPOINTS.items():
        results = []
        for path in paths:
            resp = client.get(path)
            results.append(
                ProbeResult(
                    name=capability,
                    path=path,
                    status=resp.status,
                    exists=resp.ok,
                    note="" if resp.ok else resp.error_text()[:120],
                )
            )
        report["candidates"][capability] = results

    return report


_SENSITIVE_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "psk",
    "key",
    "hash",
    "token",
    "salt",
)


def redact_tree(obj: Any) -> Any:
    """Copy a payload with anything secret-looking masked.

    Router config carries Wi-Fi pre-shared keys and password hashes. A probe
    report gets pasted into chat and committed by accident, so it must never
    carry them, and the masking has to happen where the data is read rather
    than being left to the caller to remember.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SENSITIVE_KEY_HINTS) and value:
                out[key] = f"<redacted {len(str(value))} chars>"
            else:
                out[key] = redact_tree(value)
        return out
    if isinstance(obj, list):
        return [redact_tree(v) for v in obj]
    return obj


def device_model(device: dict[str, Any]) -> str:
    """Pull the model name out of /api/system/device/status.

    The payload nests it rather than exposing a top-level `model`: on a
    RUTX50 the useful values are `static.model` and `board.model.name`, while
    `model` at the top level does not exist. A flat `.get("model")` therefore
    reports "unknown" on hardware that plainly told us what it is.

    The model matters beyond cosmetics: the RutOS API surface is versioned per
    model and per firmware, so a report that cannot name the device is not
    much use as evidence for what that device supports.
    """
    if not isinstance(device, dict):
        return "unknown"
    static = device.get("static")
    if isinstance(static, dict):
        for key in ("model", "device_name", "board_name"):
            value = static.get(key)
            if isinstance(value, str) and value:
                return value
    board = device.get("board")
    if isinstance(board, dict):
        board_model = board.get("model")
        if isinstance(board_model, dict):
            for key in ("name", "platform", "id"):
                value = board_model.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(board_model, str) and board_model:
            return board_model
    for key in ("model", "device_model"):
        value = device.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def summarise_probe(report: dict[str, Any]) -> list[str]:
    """Render a probe report as lines an operator can read and paste back."""
    lines: list[str] = []
    dev = report.get("device") or {}
    lines.append(f"Router:   {report.get('host')}")
    lines.append(f"Model:    {device_model(dev)}")
    fw = report.get("firmware") or {}
    if fw:
        version = fw.get("version") or fw.get("firmware_version") or "unknown"
        lines.append(f"Firmware: {version}")
    lines.append("")

    lines.append("Enumeration endpoints")
    for r in report.get("discovery", []):
        mark = "yes" if r.exists else "no "
        lines.append(f"  [{mark}] {r.status:<4} {r.path}  {r.note}".rstrip())
    lines.append("")

    lines.append("Endpoints Sensa already polls (a failure here means")
    lines.append("credentials or Access Control, not missing coverage)")
    for r in report.get("known_reads", []):
        mark = "yes" if r.exists else "no "
        lines.append(f"  [{mark}] {r.status:<4} {r.path}  {r.note}".rstrip())
    lines.append("")

    lines.append("Provisioning capabilities")
    for capability, results in report.get("candidates", {}).items():
        found = [r for r in results if r.exists]
        if found:
            lines.append(f"  [available] {capability:<22} {found[0].path}")
            continue
        # No 200. Say WHICH kind of no, because they need different fixes.
        forbidden = [r for r in results if r.verdict == "forbidden"]
        if forbidden:
            lines.append(
                f"  [forbidden] {capability:<22} {forbidden[0].path} "
                f"({forbidden[0].status}, exists but this account may not use it)"
            )
            continue
        statuses = ", ".join(str(r.status) for r in results)
        lines.append(
            f"  [absent   ] {capability:<22} not implemented "
            f"({len(results)} tried: {statuses})"
        )

    return lines


def classify_capabilities(report: dict[str, Any]) -> dict[str, list[str]]:
    """Split the probe's capabilities into available / forbidden / absent.

    Kept separate from the rendering so the caller can act on it, and so the
    three outcomes never get collapsed back into a boolean by accident.
    """
    out: dict[str, list[str]] = {"available": [], "forbidden": [], "absent": []}
    for capability, results in report.get("candidates", {}).items():
        if any(r.exists for r in results):
            out["available"].append(capability)
        elif any(r.verdict == "forbidden" for r in results):
            out["forbidden"].append(capability)
        else:
            out["absent"].append(capability)
    return out


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

STARLINK_MODES = ("none", "wan_ethernet", "wifi_client")

# Minimum the router will accept is shorter, but a van's Wi-Fi password is
# typed once and lives for years on a network that a passer-by can hear.
MIN_WIFI_PASSWORD = 12
MIN_ADMIN_PASSWORD = 12

SECRET_FIELDS = (
    "current_password",
    "admin_password",
    "nomad_password",
    "wifi_password",
    "iot_wifi_password",
    "guest_wifi_password",
    "starlink_password",
)


@dataclass
class ProvisionParams:
    """Everything that varies per van.

    The layout itself (subnets, zones, firewall matrix) is not here: that is
    `network_profile.json`, which is the same for every van. This is only what
    the installer supplies on the day.
    """

    identity: str = ""

    # How to reach the router, so the params file is the only thing a run
    # needs. A command line flag still wins where one is given.
    host: str = ""
    username: str = "admin"
    current_password: str = ""

    admin_password: str = ""
    nomad_password: str = ""
    wifi_password: str = ""
    iot_wifi_password: str = ""
    guest_wifi_password: str = ""

    starlink_mode: str = "none"
    starlink_ssid: str = ""
    starlink_password: str = ""

    nomad_username: str = "nomad"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProvisionParams:
        known = {f for f in cls.__dataclass_fields__}
        unknown = {k for k in raw if not k.startswith("_") and k not in known}
        if unknown:
            raise ProvisionError(
                "Unknown parameter(s): "
                + ", ".join(sorted(unknown))
                + ". Known parameters are: "
                + ", ".join(sorted(known))
            )
        return cls(**{k: v for k, v in raw.items() if not k.startswith("_")})

    def validate(self) -> list[str]:
        """Return every problem at once, rather than failing on the first.

        An installer standing in a van wants one list of what to fix, not a
        sequence of single-error runs.
        """
        problems: list[str] = []

        if not self.host:
            problems.append(
                "host is required; put the router's address in the params file "
                "or pass --host"
            )
        if not self.current_password:
            problems.append(
                "current_password is required; it is the password the router "
                "has NOW, used to log in. admin_password is the new one it "
                "will be changed to"
            )

        if not self.identity:
            problems.append("identity is required; it prefixes every SSID")
        elif not self.identity.replace("_", "").replace("-", "").isalnum():
            problems.append(
                f"identity {self.identity!r} must be letters, digits, hyphen or "
                "underscore only: it becomes an SSID"
            )
        elif len(self.identity) > 20:
            problems.append(
                f"identity {self.identity!r} is {len(self.identity)} characters; "
                "an SSID is capped at 32 and the suffix needs room"
            )

        for field_name, minimum in (
            ("admin_password", MIN_ADMIN_PASSWORD),
            ("nomad_password", MIN_ADMIN_PASSWORD),
            ("wifi_password", MIN_WIFI_PASSWORD),
            ("iot_wifi_password", MIN_WIFI_PASSWORD),
            ("guest_wifi_password", MIN_WIFI_PASSWORD),
        ):
            value = getattr(self, field_name)
            if not value:
                problems.append(f"{field_name} is required")
            elif len(value) < minimum:
                problems.append(
                    f"{field_name} is {len(value)} characters; minimum {minimum}"
                )

        if self.starlink_mode not in STARLINK_MODES:
            problems.append(
                f"starlink_mode {self.starlink_mode!r} is not one of "
                + ", ".join(STARLINK_MODES)
            )
        if self.starlink_mode == "wifi_client":
            if not self.starlink_ssid:
                problems.append(
                    "starlink_ssid is required when starlink_mode is wifi_client"
                )
            if not self.starlink_password:
                problems.append(
                    "starlink_password is required when starlink_mode is wifi_client"
                )

        # Distinct passwords are the point of having separate segments. A
        # guest password that also opens the data network makes the guest
        # VLAN decorative.
        wifi_passwords = {
            "wifi_password": self.wifi_password,
            "iot_wifi_password": self.iot_wifi_password,
            "guest_wifi_password": self.guest_wifi_password,
        }
        seen: dict[str, str] = {}
        for name, value in wifi_passwords.items():
            if not value:
                continue
            if value in seen:
                problems.append(
                    f"{name} is the same as {seen[value]}; separate segments with "
                    "a shared password are not separate"
                )
            seen[value] = name

        return problems

    def secrets(self) -> tuple[str, ...]:
        """Every value that must be redacted before anything is printed."""
        return tuple(
            value for f in SECRET_FIELDS if (value := getattr(self, f))
        )

    def ssid_for(self, suffix: str, template: str) -> str:
        return template.format(identity=self.identity, suffix=suffix)


PARAMS_TEMPLATE: dict[str, Any] = {
    "_comment": (
        "Per-van provisioning parameters for provision_router.py. The network "
        "layout itself (subnets, VLANs, firewall zones) is not here: it is the "
        "same for every van and lives in network_profile.json. See "
        "docs/system-design/network-design.md."
    ),
    "_secrets_warning": (
        "This file contains passwords. Keep it out of git, and delete it after "
        "the install. Any field left empty is prompted for instead, which is "
        "the safer option."
    ),
    "host": "192.168.1.1",
    "_host_help": (
        "The router's address. Put it here and the params file is the only "
        "argument a run needs; --host on the command line overrides it."
    ),
    "username": "admin",
    "current_password": "",
    "_current_password_help": (
        "The password the router has NOW, used to log in. This is not the same "
        "as admin_password below, which is the new one it will be changed to. "
        "Leave empty to be prompted, which keeps it off disk."
    ),
    "identity": "",
    "_identity_help": (
        "Prefixes every SSID, so a van named hawthorn gets hawthorn_2g, "
        "hawthorn_5g, hawthorn_iot, hawthorn_guest. Letters, digits, hyphen "
        "and underscore only; 20 characters maximum."
    ),
    "admin_password": "",
    "_admin_password_help": "New router administrator password. Minimum 12.",
    "nomad_password": "",
    "_nomad_password_help": (
        "Password for the nomad service user that Sensa polls the router with. "
        "Minimum 12. This is what ends up in the integration's config."
    ),
    "wifi_password": "",
    "_wifi_password_help": (
        "Shared by the two data SSIDs (_2g and _5g), so the customer has one "
        "Wi-Fi password for their own devices. Minimum 12."
    ),
    "iot_wifi_password": "",
    "_iot_wifi_password_help": "For the _iot SSID. Must differ from the others.",
    "guest_wifi_password": "",
    "_guest_wifi_password_help": "For the _guest SSID. Must differ from the others.",
    "starlink_mode": "none",
    "_starlink_mode_help": (
        "none, wan_ethernet (dish feeds the router's WAN port), or wifi_client "
        "(the router joins Starlink's own Wi-Fi as its upstream). Both assume "
        "the customer has already set the dish up in the Starlink app."
    ),
    "starlink_ssid": "",
    "starlink_password": "",
    "_starlink_credentials_help": "Required only when starlink_mode is wifi_client.",
}
