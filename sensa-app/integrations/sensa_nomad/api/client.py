"""API client for the Sensa App backend."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Every call that makes the App talk to the licensing cloud SYNCHRONOUSLY on the
# request thread must outlast the App's own retry chain, or we time out first and
# report a failure the App was about to resolve.
#
# The App's licensing client retries 3x with a 10s per-attempt timeout plus ~3s
# of backoff (`core/sensa_core/licensing/client.py`), so its worst case is ~33s
# before it records an `unreachable` result -- plus telemetry gathering on the
# phone-home path. Trial / activate / redeem each ran on a 20s timeout, which is
# INSIDE that window: on a slow or flaky link the config flow aborted with
# `licence_unreachable` while the App was still retrying, so a customer on a
# marginal connection saw setup fail for a call that would have succeeded.
#
# 60s gives comfortable margin. These are one-shot, customer-initiated calls with
# a progress indicator, so waiting is far cheaper than a spurious failure. The
# App returns 200 with a status even when the cloud was unreachable.
LICENCE_CLOUD_TIMEOUT = 60


class SensaApiClient:
    """Async client for the Sensa App backend."""

    def __init__(self, hass, base_url: str) -> None:
        self.hass = hass
        self.base_url = base_url.rstrip("/")

    async def async_health(self) -> dict[str, Any]:
        """Check backend health."""
        return await self.async_get_json("/health", timeout=10)

    async def async_health_quiet(self, *, timeout: int = 5) -> dict[str, Any] | None:
        """Best-effort /health for non-critical pollers.

        Returns the parsed body on success, None on any error
        (timeout / connection / HTTP). Suppresses the upstream
        ERROR-level logging so a transient add-on outage (e.g. a
        Supervisor-driven restart while the integration is in the
        middle of `async_setup_entry`) doesn't pollute the HA log
        with a noisy traceback for a code path that handles the
        failure itself.
        """
        try:
            return await self.async_get_json("/health", timeout=timeout, quiet=True)
        except (TimeoutError, ConnectionError):
            return None

    async def async_scan(
        self,
        scan_subnet: str,
    ) -> dict[str, Any]:
        """Run a backend device scan (all discovery methods).

        Device credentials are pulled from env vars on the add-on side.

        Timeout: 60 s. The discovery sensor's probe runs in the background
        and the config flow's first scan has nothing cached to fall back
        on, so the timeout only has to outlast the scan. Two /24s take the backend 14 to 31 s (.177, 2026-10-31);
        at 15 s half the probes timed out and the next one started while
        the backend was still running the last, doubling its load.
        """
        return await self.async_post_json(
            "/scan",
            {"scan_subnet": scan_subnet},
            timeout=60,
        )

    async def async_confirm_device(
        self,
        *,
        discovery_id: str,
        device_id: str,
        display_name: str,
        category: str,
        role: str,
        integration_type: str = "unknown",
        enabled: bool = True,
        catalog_id: str | None = None,
    ) -> dict[str, Any]:
        """Confirm a discovered device into the backend registry."""
        payload = {
            "discovery_id": discovery_id,
            "device_id": device_id,
            "display_name": display_name,
            "category": category,
            "role": role,
            "integration_type": integration_type,
            "enabled": enabled,
        }
        # The catalogue entry this row came from. The App's entitlement gate needs
        # it for DEVICE-scoped rules, which exist precisely where the service class
        # cannot separate two products (the Van Level Pro 10 is Vision-only; the
        # Pro 6 is not, and both are `levelling`).
        if catalog_id:
            payload["catalog_id"] = catalog_id
        _LOGGER.debug("Sensa API confirm payload: %s", payload)
        return await self.async_post_json("/devices/confirm", payload, timeout=30)

    async def async_register_device(
        self,
        *,
        device_id: str,
        display_name: str,
        category: str,
        role: str,
        integration_type: str = "manual",
        hostname: str | None = None,
        mac_address: str | None = None,
        ip_address: str | None = None,
        model: str | None = None,
        vendor: str | None = None,
        catalog_id: str | None = None,
    ) -> dict[str, Any]:
        """Register a manual/catalog device into the backend registry."""
        payload: dict[str, Any] = {
            "device_id": device_id,
            "display_name": display_name,
            "category": category,
            "role": role,
            "integration_type": integration_type,
        }
        # See async_confirm_device: needed for DEVICE-scoped entitlement rules.
        if catalog_id:
            payload["catalog_id"] = catalog_id
        if hostname:
            payload["hostname"] = hostname
        if mac_address:
            payload["mac_address"] = mac_address
        if ip_address:
            payload["ip_address"] = ip_address
        if model:
            payload["model"] = model
        if vendor:
            payload["vendor"] = vendor
        _LOGGER.debug("Sensa API register payload: %s", payload)
        # Defence-in-depth: registration is idempotent on the backend
        # (register_device upserts by device_id), so retry a couple of times
        # on a transient failure — e.g. the add-on momentarily busy with a
        # concurrent subnet scan. Safe to repeat; a duplicate just re-updates
        # the same record.
        return await self.async_post_json(
            "/devices/register", payload, timeout=30, retries=2,
        )

    async def async_update_device(
        self,
        *,
        device_id: str,
        enabled: bool | None = None,
        display_name: str | None = None,
        category: str | None = None,
        role: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an existing backend device record."""
        payload: dict[str, Any] = {"device_id": device_id}
        if enabled is not None:
            payload["enabled"] = enabled
        if display_name is not None:
            payload["display_name"] = display_name
        if category is not None:
            payload["category"] = category
        if role is not None:
            payload["role"] = role
        if config is not None:
            payload["config"] = config

        _LOGGER.debug("Sensa API update payload: %s", payload)
        return await self.async_post_json("/devices/update", payload, timeout=30)

    async def async_get_device(self, device_id: str) -> dict[str, Any]:
        """Get a single device from the backend registry."""
        return await self.async_get_json(f"/devices/{device_id}")

    async def async_delete_device(self, *, device_id: str) -> dict[str, Any]:
        """Delete a device from the backend registry."""
        return await self.async_post_json(
            "/devices/delete", {"device_id": device_id}, timeout=30,
        )

    async def async_clear_devices(self) -> dict[str, Any]:
        """Clear all devices from the backend registry."""
        return await self.async_post_json("/devices/clear", {}, timeout=30)

    async def async_clear_discoveries(self) -> dict[str, Any]:
        """Clear all discoveries from the backend registry."""
        return await self.async_post_json("/discoveries/clear", {}, timeout=30)

    async def async_get_summary(self) -> dict[str, Any]:
        """Get summary data from the backend."""
        return await self.async_get_json("/summary")

    async def async_get_discoveries(self) -> dict[str, Any]:
        """Get discoveries from the backend."""
        return await self.async_get_json("/discoveries", timeout=10)

    async def async_get_devices(self) -> dict[str, Any]:
        """Get confirmed devices from the backend."""
        return await self.async_get_json("/devices")

    async def async_get_crypto_market(self, currency: str | None = None) -> dict[str, Any]:
        """Bitcoin price, difficulty and block subsidy, via the App's cache.

        The integration is LAN-only by design: the van needs a network, not
        the internet, so anything that must reach the outside world goes
        through the add-on, which owns internet access and caches the answer.
        `quiet=True` because the App being briefly unreachable is normal and
        the caller already keeps the previous sample.
        """
        path = "/crypto/market"
        if currency:
            path += f"?currency={currency.strip().upper()}"
        return await self.async_get_json(path, timeout=10, quiet=True)

    # -- Licensing (wraps /api/license/* on the Sensa App) -----------------

    async def async_get_license_status(self) -> dict[str, Any]:
        """Return the cached licence status — no cloud call.

        `quiet=True`: this is polled on a schedule and the App add-on being
        briefly unreachable (restarting, or intentionally stopped in dev) is a
        normal, best-effort condition — `_async_refresh_licence_status` already
        catches the raise and keeps the previous state. Logging it at ERROR with
        a full traceback every poll was just noise, so downgrade to DEBUG.
        """
        return await self.async_get_json("/api/license/status", quiet=True)

    async def async_get_license_fingerprint(self) -> dict[str, Any]:
        """Return the current hardware fingerprint (diagnostics only)."""
        return await self.async_get_json("/api/license/fingerprint")

    async def async_post_license_trial(
        self,
        *,
        edition: str = "nomad",
        ha_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Auto-provision a 14-day trial on the App."""
        payload: dict[str, Any] = {"edition": edition}
        if ha_uuid:
            payload["ha_uuid"] = ha_uuid
        return await self.async_post_json(
            "/api/license/trial", payload, timeout=LICENCE_CLOUD_TIMEOUT,
        )

    async def async_post_license_activate(
        self,
        *,
        licence_key: str,
        slot: str = "production",
        ha_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Activate a paid licence — binds the hardware fingerprint."""
        payload: dict[str, Any] = {"licence_key": licence_key, "slot": slot}
        if ha_uuid:
            payload["ha_uuid"] = ha_uuid
        return await self.async_post_json(
            "/api/license/activate", payload, timeout=LICENCE_CLOUD_TIMEOUT,
        )

    async def async_post_license_redeem(
        self,
        *,
        code: str,
        slot: str = "production",
        ha_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Redeem a short registration code — the App binds this device's
        fingerprint and stores the returned hardware-bound JWT."""
        payload: dict[str, Any] = {"code": code, "slot": slot}
        if ha_uuid:
            payload["ha_uuid"] = ha_uuid
        return await self.async_post_json(
            "/api/license/redeem", payload, timeout=LICENCE_CLOUD_TIMEOUT,
        )

    async def async_post_license_phone_home(self) -> dict[str, Any]:
        """Force an immediate licence phone-home (/validate) and return the
        fresh status. Unlike ``async_get_license_status`` (a cached read) this
        makes the App do the real cloud round-trip synchronously on the request
        thread. The timeout MUST exceed the App's worst case: the licensing
        client retries 3x with a 10s per-attempt timeout + ~3s backoff (~33s)
        before recording an ``unreachable`` result, plus telemetry gathering —
        so a 30s timeout would fire FIRST on a slow/flaky cloud and the caller
        would see a spurious 'couldn't reach' instead of the App's coherent
        'recorded as unreachable, here's your status'. 60s gives comfortable
        margin. The App still returns 200 with status even when unreachable."""
        return await self.async_post_json(
            "/api/license/phone-home", {}, timeout=LICENCE_CLOUD_TIMEOUT
        )

    async def async_post_register(
        self,
        *,
        customer_name: str,
        contact_email: str,
        licence_key: str | None = None,
        editions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Submit customer registration (name + email)."""
        payload: dict[str, Any] = {
            "customer_name": customer_name,
            "contact_email": contact_email,
        }
        if licence_key:
            payload["licence_key"] = licence_key
        if editions:
            payload["editions"] = editions
        return await self.async_post_json("/api/register", payload, timeout=15)

    async def async_get_registration_status(self) -> dict[str, Any]:
        """Return whether customer registration has been submitted."""
        return await self.async_get_json("/api/register/status")

    async def async_post_diagnostics_state(
        self,
        *,
        enabled: bool,
        snapshot: dict[str, Any] | None = None,
        consent_changed: bool = False,
    ) -> dict[str, Any]:
        """Report the diagnostic-reporting consent state (+ snapshot) to the add-on.

        OFF by default. The add-on persists it and carries the snapshot up as
        ``telemetry.diagnostics`` on the next (and an immediate) phone-home. The
        snapshot rides only while ``enabled`` — an OFF report clears the cached
        copy so Sensa stops receiving diagnostics.

        ``consent_changed`` marks a genuine customer change (a category toggle) so
        the add-on fires an immediate phone-home even when the master ``enabled``
        flag is unchanged (a category added/removed while others stay on); a
        heartbeat/reload refresh leaves it False so it doesn't /validate ~4x/hour.
        """
        payload: dict[str, Any] = {"enabled": enabled}
        if enabled and snapshot:
            payload["snapshot"] = snapshot
        if consent_changed:
            payload["consent_changed"] = True
        return await self.async_post_json(
            "/api/v1/diagnostics/state", payload, timeout=20,
        )

    async def async_get_firmware(
        self, *, codes: list[str], force: bool = False,
    ) -> dict[str, Any]:
        """What firmware the box's channel publishes for these device codes.

        `{"base_url": ..., "firmware": {"<code>": {"version", "url", "md5"}}}`. A
        code with no published firmware is ABSENT rather than empty, so "no version
        known" can never be read as "up to date". The add-on holds the cache and
        the blob read, so asking often is cheap. `force` reads the blob past that
        cache (Check now).
        """
        query = ",".join(sorted({c for c in codes if c}))
        suffix = "&force=1" if force else ""
        return await self.async_get_json(f"/api/firmware?codes={query}{suffix}")

    async def async_post_firmware_report(
        self,
        *,
        rows: list[dict[str, Any]],
        collected_at: str,
    ) -> dict[str, Any]:
        """Report this van's equipment firmware to the add-on.

        The rows are our OWN `update.*` entities (Cerbo, Teltonika, Sensa ESP),
        built HA-side because the add-on cannot read HA states on a Container
        appliance (no Supervisor). It caches them and carries them up as
        ``telemetry.firmware`` on the next scheduled phone-home; no immediate
        check-in, because a version that moved is not urgent.
        """
        return await self.async_post_json(
            "/api/v1/firmware/report",
            {"rows": rows, "collected_at": collected_at},
            timeout=20,
        )

    async def async_post_system_name(self, *, name: str) -> dict[str, Any]:
        """Report the customer's name for this system / vehicle to the add-on.

        The add-on persists it and carries it up as ``telemetry.system_name`` on
        the next phone-home (and fires an immediate one when the name changed), so
        the Hub fleet view can label the deployment. An empty name clears it.
        """
        return await self.async_post_json(
            "/api/v1/system/name", {"name": name}, timeout=20,
        )

    async def async_post_remote_support_state(
        self,
        *,
        enabled: bool,
        username: str,
        password: str | None,
        enabled_until: str | None,
        zigbee_token: str | None = None,
        blocked_reason: str | None = None,
    ) -> dict[str, Any]:
        """Report the remote-support consent state to the add-on.

        The add-on persists it and carries it up to Sensa on the next (and an
        immediate) phone-home. The password rides only while ``enabled`` — the
        add-on drops it on an OFF report.
        """
        payload: dict[str, Any] = {"enabled": enabled, "username": username}
        # Why the session is NOT open, when we know. Sent on the OFF report
        # because that is the only report a refusal ever produces: without it a
        # box that cannot honour a grant is indistinguishable from one that was
        # never asked, and the Hub console shows "closed" either way.
        if blocked_reason:
            payload["blocked_reason"] = blocked_reason
        if enabled:
            payload["password"] = password
            payload["enabled_until"] = enabled_until
            # Only when we actually read one. Sending null would tell the add-on
            # to forget a token it already holds, and a support session opened
            # while Z2M happened to be mid-restart would wipe it.
            if zigbee_token:
                payload["zigbee_token"] = zigbee_token
        return await self.async_post_json(
            "/api/v1/remote-support/state", payload, timeout=20,
        )

    async def async_post_remote_access_state(self, *, enabled: bool) -> dict[str, Any]:
        """Set the customer's remote-access switch on the add-on.

        The add-on persists it and applies it at once (starting or stopping
        cloudflared), so this is not a report of something already done, it is
        the request itself. Nothing goes to the cloud: the tunnel stays minted
        and the hostname stays reserved, the connector simply stops running.
        """
        return await self.async_post_json(
            "/api/v1/remote-access/state", {"enabled": enabled}, timeout=20,
        )

    # -- Backup (wraps /api/v1/backup/* on the Sensa App) -------------------

    async def async_get_backup_status(self) -> dict[str, Any]:
        """Return Supervisor backup summary + watermark + stale flag."""
        return await self.async_get_json("/api/v1/backup/status", timeout=20, quiet=True)

    async def async_post_backup_run(self, *, name: str | None = None) -> dict[str, Any]:
        """Trigger a Supervisor full-backup create. Long timeout — backups can
        take a minute or two on a Pi with a populated config."""
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        return await self.async_post_json("/api/v1/backup/run", payload, timeout=600)

    async def async_post_backup_pre_ota(self, *, version: str) -> dict[str, Any]:
        """Trigger a tagged pre-OTA snapshot before an add-on update."""
        return await self.async_post_json(
            "/api/v1/backup/pre-ota", {"version": version}, timeout=600,
        )

    async def async_post_ota_apply(
        self, *, version: str | None = None, channel: str | None = None
    ) -> dict[str, Any]:
        """Apply an add-on update (appliance OTA).

        Two modes: a specific ``version`` (Supervisor/HAOS + rollout pins), or a
        ``channel`` (``edge``/``beta``/``stable``) for a container appliance,
        which has no Supervisor to resolve ``version_latest`` — the App hands the
        channel's floating image tag to the updater sidecar and the newest build
        is pulled. The apply tears the App container down mid-request, so a
        dropped connection / timeout after the call is the EXPECTED success path,
        not an error — the caller confirms on the next backup poll. Long timeout
        so a slow pull doesn't look like a failure prematurely."""
        body: dict[str, Any] = {"channel": channel} if channel else {"version": version}
        return await self.async_post_json("/api/v1/ota/apply", body, timeout=600)

    async def async_post_ota_channel(self, channel: str) -> dict[str, Any]:
        """Set the release channel this box follows (container appliance only).

        The channel is the tag on SENSA_STACK_IMAGE in the compose ``.env``, which
        only the updater sidecar can write, so the App just drops the bare channel
        name on the shared volume for it. Short timeout: this writes a one-line
        signal file and returns, it does not wait for the updater's ~5s tick.

        A 4xx comes back as a parsed BODY, not an exception (only 5xx raises), so
        a caller that needs to know it was refused must check the payload rather
        than trusting the absence of a throw.
        """
        return await self.async_post_json(
            "/api/v1/ota/channel", {"channel": channel}, timeout=15,
        )

    async def async_get_ota_status(self) -> dict[str, Any]:
        """Live updater-sidecar OTA progress (container appliance).

        ``{"state": "idle"}`` when no update is in flight; else the sidecar's
        last status line (state/progress/version/error/ts)."""
        return await self.async_get_json("/api/v1/ota/status", timeout=10, quiet=True)

    async def async_post_ota_check(self) -> dict[str, Any]:
        """Ask the updater sidecar to re-resolve the channel-latest now (the
        Admin card's "Check now"); a harmless no-op under Supervisor."""
        return await self.async_post_json("/api/v1/ota/check", {}, timeout=10)

    # -- Host-OS updates (Layer 1, Container appliance) ---------------------

    async def async_get_host_update_status(self) -> dict[str, Any]:
        """The on-host apt-update agent's last-run summary (last_run/last_result/
        next_run/reboot_pending/auto/policy). ``{"state": "unknown"}`` on a box
        with no host agent (HAOS/BYO)."""
        return await self.async_get_json("/api/v1/host-update/status", timeout=10, quiet=True)

    # -- A/B bank updates (Layer 0, Container appliance on an A/B card) -----

    async def async_get_bank_status(self) -> dict[str, Any]:
        """The host bank agent's state, or ``{"supported": False}``.

        A single-bank box answers `supported: False` rather than 404-ing, so a
        box built before the A/B layout is a normal answer and not an error."""
        return await self.async_get_json("/api/v1/bank/status", timeout=10, quiet=True)

    async def async_post_bank_apply(self, image: str, version: str = "") -> dict[str, Any]:
        """Ask the host to install a bank image and boot it on trial.

        Returns as soon as the request is written: the host pulls gigabytes and
        then reboots, so there is no completion to wait for on this call. The
        box coming back is the result, and `sensa-bank-commit.service` decides
        whether the new bank survives."""
        return await self.async_post_json(
            "/api/v1/bank/apply", {"image": image, "version": version}, timeout=30,
        )

    async def async_post_z2m_restart(self) -> dict[str, Any]:
        """Ask the App to bounce the compose Zigbee2MQTT service (Container) after
        the integration has seeded its configuration.yaml on coordinator adoption.
        A no-op under Supervisor (Z2M is an add-on there)."""
        return await self.async_post_json("/api/v1/z2m/restart", {}, timeout=15)

    async def async_post_host_update_config(
        self, *, auto: bool | None = None, window: str | None = None,
    ) -> dict[str, Any]:
        """Set the customer host-OS-update prefs (on/off toggle and/or the
        maintenance window "HH:MM-HH:MM"). The App MERGES them into the on-host
        override file the systemd agent honours — sending one doesn't clear the
        other."""
        payload: dict[str, Any] = {}
        if auto is not None:
            payload["auto"] = auto
        if window is not None:
            payload["window"] = window
        return await self.async_post_json(
            "/api/v1/host-update/config", payload, timeout=10,
        )

    async def async_post_ota_config(self, *, auto: bool) -> dict[str, Any]:
        """Report the Sensa software OTA auto-update toggle to the add-on.

        The App persists it and carries it up as ``telemetry.updates.sensa_auto``
        on the next phone-home (firing an immediate one when it changed), so the
        Hub fleet view can show whether the box updates itself. The OS-side toggle
        is reported via ``async_post_host_update_config`` (host_update override),
        which the App reads as ``telemetry.updates.os_auto`` — the two together
        are the fleet's update-settings picture."""
        return await self.async_post_json(
            "/api/v1/ota/config", {"auto": auto}, timeout=10,
        )

    async def async_get_backup_emergency_kit(self) -> dict[str, Any]:
        """Return the emergency-kit bundle (encryption key + licence id)."""
        return await self.async_get_json("/api/v1/backup/emergency-kit", timeout=10)

    async def async_post_backup_pin(self, *, slug: str) -> dict[str, Any]:
        return await self.async_post_json(
            f"/api/v1/backup/pin/{slug}", {}, timeout=10,
        )

    async def async_delete_backup_pin(self, *, slug: str) -> dict[str, Any]:
        return await self.async_delete_json(f"/api/v1/backup/pin/{slug}", timeout=10)

    async def async_delete_backup(self, *, slug: str) -> dict[str, Any]:
        """Permanently delete a stored backup (the per-row delete button)."""
        return await self.async_delete_json(f"/api/v1/backup/{slug}", timeout=30)

    # -- Network signals (wraps /api/v1/network/* on the Sensa App) ---------

    async def async_get_network_status(self) -> dict[str, Any]:
        """Return `{ping: {...}, speedtest: {...}}` — a fresh reachability/RTT
        ping plus the LAST speed-test result. Cheap; polled on a timer."""
        return await self.async_get_json("/api/v1/network/status", timeout=10, quiet=True)

    async def async_post_speedtest_run(self) -> dict[str, Any]:
        """Kick off an on-demand speed test. The add-on runs it on a background
        thread and returns immediately (`{status: "running"}`), so a normal
        timeout is fine — the result lands via the next status poll."""
        return await self.async_post_json("/api/v1/network/speedtest/run", {}, timeout=30)

    # -- Data usage (wraps /api/v1/egress/* on the Sensa App) --------------

    async def async_get_egress_usage(self) -> dict[str, Any]:
        """Return `{since, overhead_bytes_per_request, rows: [{category,
        sent_24h, received_24h, sent_30d, received_30d}]}` for Settings ->
        Data usage."""
        return await self.async_get_json("/api/v1/egress/usage", timeout=10, quiet=True)

    # -- System resources (wraps /api/v1/system/* on the Sensa App) ---------

    async def async_get_system_resources(self) -> dict[str, Any]:
        """Return `{cpu_percent, memory_percent, disk_percent, ...}` — a coarse
        host-health snapshot for the Settings → Support card. Cheap; polled on a
        timer."""
        return await self.async_get_json("/api/v1/system/resources", timeout=10, quiet=True)

    async def async_get_system_hardware(self) -> dict[str, Any]:
        """Return `{supported, cpu_temperature_c, fan_rpm, board_power_w, ...}` —
        the Raspberry Pi board readings for the Sensa Core device's sensors.

        `supported` is False on anything that is not a sold appliance running on
        real Pi hardware, and the App decides that (it holds the UNFOLDED
        deployment mode; by the time the integration sees one, enthusiast has
        already been folded into appliance). Treat a False as "no entities", not
        as an error. Cheap; polled on a timer."""
        return await self.async_get_json("/api/v1/system/hardware", timeout=10, quiet=True)

    async def async_post_factory_reset(self) -> dict[str, Any]:
        """Scrub the App's per-box state as part of a factory reset.

        Clears the `/data` state files, the image library and the cached dealer
        assets, keeping `licensing.json` (the box must come back activated),
        `ota.json` and the image label. The Home Assistant half of the reset is
        the integration's own job; this is only the App's share.

        Generous timeout: it walks the uploads directory."""
        return await self.async_post_json(
            "/api/v1/system/factory-reset", {}, timeout=60,
        )

    async def async_request_shutdown(
        self, *, reason: str, action: str = "poweroff", source: str = "integration",
    ) -> dict[str, Any]:
        """Ask the App to have the HOST power the appliance down cleanly.

        Home Assistant cannot do this itself on Container (no Supervisor, so no
        `hassio.host_shutdown`; the container is unprivileged with no host PID
        namespace and no Docker socket). The App writes a timestamped request to
        a bind mount and a systemd path unit on the host acts on it.

        `action="test"` runs the whole path and stops short of the poweroff.
        403 off an appliance. Short timeout: the box may be going down."""
        return await self.async_post_json(
            "/api/v1/system/power/shutdown",
            {"action": action, "reason": reason, "source": source},
            timeout=10,
        )

    async def async_get_power_status(self) -> dict[str, Any]:
        """The host power agent's last recorded outcome, or `{"status": None}`.

        Read once at boot by `boot_log.py` to explain a poweroff that did not go
        through `sensa_nomad.request_shutdown` (and so left no intent behind).
        `quiet=True`: the App is often still starting when this is asked, and an
        unanswered question here is not a fault, just an absent reason."""
        return await self.async_get_json(
            "/api/v1/system/power/status", timeout=10, quiet=True,
        )

    async def async_get_system_processes(self) -> dict[str, Any]:
        """Return `{apps: [{name, cpu_percent, memory_mb, memory_percent}], ...}`
        — the per-app (per-container) CPU/memory breakdown for the Support card,
        from Supervisor container stats. N Supervisor calls server-side, so a
        slightly longer timeout than the single-snapshot resources call."""
        return await self.async_get_json("/api/v1/system/processes", timeout=20, quiet=True)

    async def async_get_system_containers(self) -> dict[str, Any]:
        """Return `{containers: [{service, name, state, version, actions}], source}`
        — the compose stack's containers for the Settings → Supervisor Containers
        panel (Container-appliance only; empty under Supervisor). Cheap; polled on
        a timer."""
        return await self.async_get_json("/api/v1/system/containers", timeout=20, quiet=True)

    async def async_container_action(self, *, service: str, action: str) -> dict[str, Any]:
        """Start/stop/restart a compose service. The App validates against its
        per-service policy then signals the updater sidecar (which holds the
        Docker socket). Returns `{status: "ok", ...}`."""
        return await self.async_post_json(
            "/api/v1/system/containers/action",
            {"service": service, "action": action},
            timeout=30,
        )

    async def async_get_container_inspect(self, *, service: str) -> dict[str, Any]:
        """Read-only diagnostics for one compose service, for Sensa support:
        `{service, id, image, created, state, restart_count, started_at,
        mounts: [...], log, log_lines}`. The App waits for the updater sidecar
        (the Docker-socket holder) to answer, so the timeout covers a 5 s tick
        plus the inspect. A 4xx (`error`: `no_container`,
        `container_inspect_timeout`, ...) comes back as a body, not an
        exception. `service` is validated to a slug by the caller."""
        return await self.async_get_json(
            f"/api/v1/system/containers/inspect?service={service}",
            timeout=30,
            quiet=True,
        )

    # -- Image library (wraps /api/v1/uploads/* on the Sensa App) -----------

    async def async_get_uploads_list(self) -> dict[str, Any]:
        """Return `{images: [{url, name, uploaded_at}]}` for the customer's
        uploaded image library."""
        return await self.async_get_json("/api/v1/uploads/list", timeout=15, quiet=True)

    async def async_post_upload_image(
        self,
        *,
        filename: str,
        content_type: str,
        data_b64: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Upload an image (base64-in-JSON) to the library. Returns
        `{url, name}`. Larger timeout — a few-MB base64 body."""
        payload: dict[str, Any] = {
            "filename": filename,
            "content_type": content_type,
            "data_b64": data_b64,
        }
        if name:
            payload["name"] = name
        return await self.async_post_json("/api/v1/uploads/image", payload, timeout=60)

    async def async_post_delete_image(self, *, url: str) -> dict[str, Any]:
        return await self.async_post_json("/api/v1/uploads/delete", {"url": url}, timeout=15)

    async def async_get_json(
        self,
        path: str,
        *,
        timeout: int = 30,
        quiet: bool = False,
    ) -> dict[str, Any]:
        """GET JSON from the Sensa backend.

        `quiet=True` downgrades the error logging from ERROR (with
        traceback) to DEBUG — for callers that handle the failure
        themselves and don't want a noisy log entry every time the
        add-on is briefly unreachable (e.g. deployment_mode refresh
        during setup, where a Supervisor restart of the add-on can
        legitimately collide with the integration's first poll).
        The raised exception types are unchanged.
        """
        url = f"{self.base_url}{path}"
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        log_err = _LOGGER.debug if quiet else _LOGGER.exception

        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=client_timeout,
            ) as session, session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug("Sensa API GET %s response: %s", url, data)
                return data

        except asyncio.TimeoutError as err:
            _LOGGER.debug("Sensa API GET timed out at %s", url)
            raise TimeoutError(f"Sensa backend GET timed out at {url}") from err
        except aiohttp.ClientResponseError as err:
            _LOGGER.debug("Sensa API GET HTTP %s at %s", err.status, url)
            raise ConnectionError(
                f"Sensa backend GET HTTP error {err.status} at {url}"
            ) from err
        except aiohttp.ClientError as err:
            # Connection-level failure — the add-on is unreachable (stopped,
            # restarting, or not yet up). Expected and transient; callers and
            # HA's DataUpdateCoordinator own the user-facing logging, so this
            # stays a quiet one-liner and NEVER a traceback. (This was the noisy
            # ERROR with a full aiohttp stack on every poll while the add-on was
            # down — see _async_refresh_licence_status / the coordinator, which
            # both already handle the raise.)
            _LOGGER.debug(
                "Sensa backend unreachable at %s (%s)", url, type(err).__name__
            )
            raise ConnectionError(f"Failed to GET Sensa backend at {url}") from err
        except Exception as err:
            # Genuinely unexpected — keep the traceback so real bugs stay visible.
            log_err("Sensa API GET unexpected error for %s", url, exc_info=not quiet)
            raise ConnectionError(f"Unexpected GET error at {url}") from err

    async def async_post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: int = 30,
        retries: int = 0,
        retry_delay: float = 0.5,
    ) -> dict[str, Any]:
        """POST JSON to the Sensa backend and return parsed JSON.

        `retries` gives extra attempts on a *transient* failure
        (TimeoutError / ConnectionError — the add-on unreachable, a
        dropped connection, or a 5xx). 0 = a single attempt (default,
        unchanged behaviour). A 4xx is a definitive structured response
        and is returned, never retried; a genuinely unexpected error is
        re-raised without retry. Only enable retries on IDEMPOTENT
        endpoints — e.g. /devices/register upserts by device_id, so a
        retry after a lost response just updates the same record.
        """
        url = f"{self.base_url}{path}"
        attempts = max(1, retries + 1)

        for attempt in range(attempts):
            try:
                return await self._post_json_once(url, payload, timeout)
            except (TimeoutError, ConnectionError) as err:
                if attempt + 1 >= attempts:
                    raise
                _LOGGER.debug(
                    "Sensa API POST %s failed (%s); retry %d/%d in %.1fs",
                    url, type(err).__name__, attempt + 1, retries, retry_delay,
                )
                await asyncio.sleep(retry_delay)

        # Unreachable — the loop either returns or raises on the final attempt.
        raise ConnectionError(f"Failed to POST to Sensa backend at {url}")

    async def _post_json_once(
        self,
        url: str,
        payload: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        """Single POST attempt. Returns parsed JSON, or raises
        TimeoutError / ConnectionError on a transient failure (which
        ``async_post_json`` may retry)."""
        connector = aiohttp.TCPConnector(family=socket.AF_INET)

        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=client_timeout,
            ) as session, session.post(url, json=payload) as response:
                body = await response.text()

                _LOGGER.debug(
                    "Sensa API POST %s status=%s body=%s",
                    url,
                    response.status,
                    body,
                )

                # 5xx → server problem, treat as transient connection
                # error so callers retry / surface a network message.
                # 4xx → the server reached us and gave a structured
                # response (e.g. `{"status": "trial_exhausted"}`).
                # Returning the parsed body lets the caller read the
                # `status` field and show the right error rather than
                # a misleading "check your internet" message. Earlier
                # behaviour collapsed all >=400 into ConnectionError
                # which caused the config flow to display
                # `licence_unreachable` for genuine API decisions.
                if response.status >= 500:
                    raise ConnectionError(
                        f"Sensa backend POST HTTP error {response.status} at {url}: {body}"
                    )

                try:
                    parsed = json.loads(body) if body else {}
                except (ValueError, TypeError):
                    parsed = {"raw_body": body}
                if not isinstance(parsed, dict):
                    parsed = {"raw_body": body}
                if response.status >= 400:
                    # Stash the HTTP status so callers can disambiguate
                    # if needed — but the structured `status` field
                    # remains the primary signal.
                    parsed.setdefault("http_status", response.status)
                return parsed

        except asyncio.TimeoutError as err:
            _LOGGER.debug("Sensa API POST timed out at %s", url)
            raise TimeoutError(f"Sensa backend POST timed out at {url}") from err
        except aiohttp.ClientError as err:
            # Add-on unreachable — expected/transient, callers surface it (the
            # config flow shows the error to the user via the flow result, not
            # the log). Concise, no traceback.
            _LOGGER.debug(
                "Sensa backend unreachable at %s (%s)", url, type(err).__name__
            )
            raise ConnectionError(f"Failed to POST to Sensa backend at {url}") from err
        except Exception:
            _LOGGER.exception("Sensa API POST unexpected error for %s", url)
            raise

    async def async_delete_json(
        self,
        path: str,
        *,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """DELETE on the Sensa backend and return parsed JSON.

        Same 4xx-preserves-body / 5xx-raises-ConnectionError contract as
        ``async_post_json`` — see that method's comment for the rationale.
        """
        url = f"{self.base_url}{path}"
        connector = aiohttp.TCPConnector(family=socket.AF_INET)

        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=client_timeout,
            ) as session, session.delete(url) as response:
                body = await response.text()
                if response.status >= 500:
                    raise ConnectionError(
                        f"Sensa backend DELETE HTTP error {response.status} at {url}: {body}"
                    )
                try:
                    parsed = json.loads(body) if body else {}
                except (ValueError, TypeError):
                    parsed = {"raw_body": body}
                if not isinstance(parsed, dict):
                    parsed = {"raw_body": body}
                if response.status >= 400:
                    parsed.setdefault("http_status", response.status)
                return parsed
        except asyncio.TimeoutError as err:
            _LOGGER.debug("Sensa API DELETE timed out at %s", url)
            raise TimeoutError(f"Sensa backend DELETE timed out at {url}") from err
        except aiohttp.ClientError as err:
            # Add-on unreachable — expected/transient. Concise, no traceback.
            _LOGGER.debug(
                "Sensa backend unreachable at %s (%s)", url, type(err).__name__
            )
            raise ConnectionError(f"Failed to DELETE Sensa backend at {url}") from err
        except Exception:
            _LOGGER.exception("Sensa API DELETE unexpected error for %s", url)
            raise
