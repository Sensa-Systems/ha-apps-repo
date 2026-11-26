"""Sync HTTPS client for the Sensa Licensing cloud API.

Uses ``requests`` so we avoid the Python 3.14 aiohttp hang on HA Core LAN
connects that already bit us on the Teltonika integration. Consumers running
under asyncio wrap calls in ``hass.async_add_executor_job`` / stdlib
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import requests

from sensa_core.licensing.models import (
    CloudUnavailableError,
    FingerprintComponents,
    PhoneHomeResult,
    RegistrationPayload,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://license.sensa.systems/v1"
DEFAULT_TIMEOUT = 10.0  # seconds
_RETRY_COUNT = 3
_RETRY_BASE_DELAY = 1.0  # seconds


class LicensingClient:
    """Thin wrapper around the four device-facing cloud endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
        on_bytes: Callable[[str, dict[str, Any], int, int], None] | None = None,
    ) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()
        # Told (path, body, bytes sent, bytes received) after every attempt that
        # reached the cloud, for the box's data-usage page. Never allowed to
        # break a call.
        self._on_bytes = on_bytes

    # -- Endpoints ----------------------------------------------------------

    def request_trial(
        self,
        fingerprint: str,
        components: FingerprintComponents,
        edition: str,
        software_version: str,
        deployment_mode: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "fingerprint": fingerprint,
            "fingerprint_components": components.to_dict(),
            "edition": edition,
            "software_version": software_version,
        }
        if deployment_mode:
            body["deployment_mode"] = deployment_mode
        return self._post("/trial", body)

    def activate(
        self,
        licence_key: str,
        fingerprint: str,
        components: FingerprintComponents,
        slot: str,
        software_version: str,
        deployment_mode: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "licence_key": licence_key,
            "fingerprint": fingerprint,
            "fingerprint_components": components.to_dict(),
            "slot": slot,
            "software_version": software_version,
        }
        if deployment_mode:
            body["deployment_mode"] = deployment_mode
        return self._post("/activate", body)

    def redeem(
        self,
        code: str,
        fingerprint: str,
        components: FingerprintComponents,
        slot: str,
        software_version: str,
        deployment_mode: str | None = None,
    ) -> dict[str, Any]:
        """Exchange a short redemption code for a hardware-bound JWT.

        Returns the cloud response; on success it carries ``licence_key`` (a
        JWT with ``hardware_id`` baked in for this device).
        """
        body = {
            "code": code,
            "fingerprint": fingerprint,
            "fingerprint_components": components.to_dict(),
            "slot": slot,
            "software_version": software_version,
        }
        if deployment_mode:
            body["deployment_mode"] = deployment_mode
        return self._post("/redeem", body)

    def validate(
        self,
        licence_key: str,
        fingerprint: str,
        slot: str,
        software_version: str,
        uptime_hours: int,
        remote_support: dict[str, Any] | None = None,
        remote_access: bool | None = None,
        remote_access_blocked: str | None = None,
        deployment_mode: str | None = None,
        telemetry: dict[str, Any] | None = None,
    ) -> PhoneHomeResult:
        body = {
            "licence_key": licence_key,
            "fingerprint": fingerprint,
            "slot": slot,
            "software_version": software_version,
            "uptime_hours": uptime_hours,
        }
        # How this box was installed (`deployment_mode`: byo / enthusiast /
        # appliance) — like software_version, it rides every call so the fleet
        # view stays current. Omitted when unknown.
        if deployment_mode:
            body["deployment_mode"] = deployment_mode
        # Remote-support consent state (enabled flag + the per-enable support
        # credential + expiry) rides the existing authenticated phone-home so
        # Sensa's Hub can show which units have support open, the access URL,
        # and the current password. Only sent when the device has the state to
        # report; a None/empty value keeps the legacy payload unchanged.
        if remote_support:
            body["remote_support"] = remote_support
        # The CUSTOMER's own remote-access switch, reported separately and for a
        # different purpose: the cloud shapes the tunnel's ingress from it, so
        # the customer's public address routes ONLY while they say so.
        #
        # The two are deliberately independent. A customer must never have to
        # open their van to the internet in order to let support in, and letting
        # support in must never open it. One tunnel carries both addresses; which
        # NAMES resolve is what these two flags decide, separately.
        #
        # A bool, and only when known: None means an older App that cannot say,
        # and the cloud must not read silence as "switched off" and dark a
        # customer's working address.
        if remote_access is not None:
            body["remote_access"] = bool(remote_access)
        # WHY the device is holding its own tunnel shut, when it is. Distinct
        # from ``remote_access`` above: that is what the customer asked for,
        # this is what the box decided, and they differ exactly when something
        # is wrong. A box with support open and seeded passwords still refuses
        # to connect, and until this rode along nothing said so: the operator
        # console showed a support address that answered with a bare Cloudflare
        # error, because the reason never left the device.
        #
        # Sent verbatim, never re-derived up there. The cloud can work out some
        # of these for itself (it knows the switch, it knows the support dates)
        # and that is precisely the trap: a second copy of the rule would
        # disagree with the device the day one of them changed, and the device
        # is the one that actually decides whether cloudflared runs.
        #
        # Three states, and the empty one matters most. ``None`` = could not
        # ask (a DIY box, no tunnel provisioned, an App too old to answer) and
        # is omitted, so the cloud carries forward whatever it last knew.
        # ``""`` = asked and clear, sent explicitly, and it is what CLEARS a
        # stored reason. Folding the two together would leave a box that fixed
        # its passwords in June still telling engineers to rotate them in
        # December.
        if remote_access_blocked is not None:
            body["remote_access_blocked"] = str(remote_access_blocked)
        # Deployment telemetry (versions / platform / health) for the fleet
        # view — grouped object, omitted when the device couldn't gather any.
        if telemetry:
            body["telemetry"] = telemetry
        resp = self._post("/validate", body)
        now = datetime.now(timezone.utc)
        return PhoneHomeResult(
            ok=resp.get("status") == "valid",
            status=resp.get("status", "unknown"),
            checked_at=now,
            next_phone_home_before=_parse_iso_opt(resp.get("next_phone_home_before")),
            trial_days_remaining=resp.get("trial_days_remaining"),
            message=resp.get("message"),
            grace_expires_at=_parse_iso_opt(resp.get("grace_expires_at")),
            dealer_logo_url=resp.get("dealer_logo_url"),
            dealer_theme_url=resp.get("dealer_theme_url"),
            dealer_layouts_url=resp.get("dealer_layouts_url"),
            people_url=resp.get("people_url"),
            remote_support_grant=(
                resp.get("remote_support_grant")
                if isinstance(resp.get("remote_support_grant"), dict)
                else None
            ),
            cloud_backup=(
                resp.get("cloud_backup")
                if isinstance(resp.get("cloud_backup"), dict)
                else None
            ),
            registry_credential=(
                resp.get("registry_credential")
                if isinstance(resp.get("registry_credential"), dict)
                else None
            ),
            registry_access=(
                resp.get("registry_access")
                if isinstance(resp.get("registry_access"), str)
                else None
            ),
            # A DICT (even one of empty lists) is the cloud's answer on device
            # gating; anything else — key absent, wrong type — is "no answer", so
            # the device keeps whatever it cached rather than falling open.
            blocked_devices=(
                resp["blocked_devices"]
                if isinstance(resp.get("blocked_devices"), dict)
                else None
            ),
            target_version=(
                resp.get("target_version")
                if isinstance(resp.get("target_version"), str)
                else None
            ),
            cerbo_target_version=(
                resp.get("cerbo_target_version")
                if isinstance(resp.get("cerbo_target_version"), str)
                else None
            ),
            # Read before any Function emits it: a tolerant reader ships first,
            # so the key appearing later needs no device-side release.
            teltonika_target_version=(
                resp.get("teltonika_target_version")
                if isinstance(resp.get("teltonika_target_version"), str)
                else None
            ),
            teltonika_switch_target_version=(
                resp.get("teltonika_switch_target_version")
                if isinstance(resp.get("teltonika_switch_target_version"), str)
                else None
            ),
            cloudflare_tunnel=(
                resp.get("cloudflare_tunnel")
                if isinstance(resp.get("cloudflare_tunnel"), dict)
                else None
            ),
            # Strictly a BOOL or nothing. An older Function omits the key
            # entirely, and `None` must mean "no answer" rather than "lapsed" —
            # anything looser (a truthiness test, a string "false") would let a
            # version skew tell the box its support had ended.
            support_active=(
                resp["support_active"]
                if isinstance(resp.get("support_active"), bool)
                else None
            ),
            # A STRING or nothing, for the same reason: an older Function omits
            # it, and None must mean "no answer". Left unparsed here so a
            # malformed date from a future Function cannot raise inside the
            # phone-home; whoever compares it against the clock is responsible
            # for treating an unparseable value as "no answer" too.
            support_expires_at=(
                resp["support_expires_at"]
                if isinstance(resp.get("support_expires_at"), str)
                and resp["support_expires_at"].strip()
                else None
            ),
            # A LIST or nothing: an older Function omits it, and None must mean
            # "no answer" rather than "nobody may look".
            service_consents=(
                [
                    c
                    for c in resp["service_consents"]
                    if isinstance(c, dict) and isinstance(c.get("device_type"), str)
                ]
                if isinstance(resp.get("service_consents"), list)
                else None
            ),
        )

    def service_snapshot(
        self,
        licence_key: str,
        fingerprint: str,
        slot: str,
        device_type: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Send one consented device's latest readings for its service company.

        Returns the cloud's body: ``{"interval_seconds": n}`` on success, or an
        error body (4xx are returned, not raised; check ``interval_seconds``).
        """
        return self._post(
            "/service/snapshot",
            {
                "licence_key": licence_key,
                "fingerprint": fingerprint,
                "slot": slot,
                "device_type": device_type,
                "snapshot": snapshot,
            },
        )

    def register(self, payload: RegistrationPayload) -> dict[str, Any]:
        return self._post("/register", payload.to_dict())

    # -- HTTP transport -----------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self._base_url + path
        last_error: Exception | None = None

        for attempt in range(_RETRY_COUNT):
            try:
                resp = self._session.post(url, json=body, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = exc
                _LOGGER.debug("licensing POST %s failed (attempt %d): %s", path, attempt + 1, exc)
                time.sleep(_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 0.2))
                continue

            self._report_bytes(path, body, resp)
            # 4xx: response body is authoritative, return it without retry.
            if 400 <= resp.status_code < 500:
                return _decode_json(resp)
            # 5xx: retry with backoff.
            if resp.status_code >= 500:
                last_error = CloudUnavailableError(f"{resp.status_code} from {path}")
                time.sleep(_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 0.2))
                continue
            return _decode_json(resp)

        raise CloudUnavailableError(
            f"licensing API unreachable after {_RETRY_COUNT} attempts: {last_error}"
        ) from last_error

    def _report_bytes(self, path: str, body: dict[str, Any], resp: requests.Response) -> None:
        if self._on_bytes is None:
            return
        try:
            sent = len(json.dumps(body).encode("utf-8"))
            content = getattr(resp, "content", b"")
            received = len(content) if isinstance(content, (bytes, bytearray)) else 0
            self._on_bytes(path, body, sent, received)
        except Exception:  # noqa: BLE001 - metering must never break a call
            _LOGGER.debug("licensing on_bytes failed", exc_info=True)


def _decode_json(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        raise CloudUnavailableError(f"non-JSON response: {resp.text[:200]!r}") from None
    if not isinstance(data, dict):
        raise CloudUnavailableError(f"unexpected response type: {type(data).__name__}")
    return data


def _parse_iso_opt(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
