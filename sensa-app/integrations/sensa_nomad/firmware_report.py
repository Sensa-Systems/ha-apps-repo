"""Equipment firmware, built here and pushed to the App for the phone-home.

One row per piece of kit whose firmware Sensa tracks, read off OUR OWN `update.*`
entities (the Cerbo's Venus OS, a Teltonika router or switch, each Sensa ESP
device), pushed to `POST /api/v1/firmware/report`. The App caches it and carries
it up as `telemetry.firmware`, which is what Hub's Deployments page lists.

**Why here and not in the App.** The App reads Home Assistant through the
Supervisor proxy, and a Container appliance has no Supervisor: its
`deployment_telemetry._ha_states()` returns None, so the firmware block was
omitted from every appliance in the fleet - the Cerbo and Teltonika rows added in
September had never once arrived from a van (found 2026-09-27). The integration
runs inside HA on every flavour and owns these entities, so this is the only place
the rows can be built everywhere. Same reasoning as the diagnostics snapshot
(`diagnostics_report.py`), minus the consent gate: the firmware a box runs is
operational fleet state, like the Sensa version beside it, not a diagnostic.

The `sensa_firmware_kind` attribute is both the tag and the ownership test: no
other integration's update entity sets it, and each of ours sets its own value
(`victron_gx` / `teltonika_router` / `teltonika_switch` / `sensa_esp` /
`zigbee`). Adding a
kind is one attribute value there and one entry in Hub's `lib/firmware.ts`;
nothing here or in the App needs to learn it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

FIRMWARE_KIND_ATTR = "sensa_firmware_kind"
FIRMWARE_DEVICE_ATTR = "sensa_device_id"
FIRMWARE_AUTO_KEY_ATTR = "sensa_auto_update_key"
FIRMWARE_CODE_ATTR = "sensa_firmware_code"

# Re-push cadence. The phone-home is hourly and the App drops a report older than
# two hours, so a quarter-hour heartbeat keeps the cached copy comfortably fresh
# without pushing at a rate anybody would notice.
REFRESH_MINUTES = 15

# The first push waits for the update platform to have registered its entities
# (the vendor coordinators start after setup, and an ESP device's version sensor
# arrives with its ESPHome entry). Reporting an empty list a second after boot
# would be a lie the App would then hold for 15 minutes.
FIRST_PUSH_DELAY_SECONDS = 120


@callback
def build_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every Sensa-owned `update` entity, as the phone-home reports it.

    `installed` / `available` are HA's own `installed_version` / `latest_version`,
    sent exactly as the device reported them (`v3.79`, `RUTX_R_00.07.24.5`,
    `1.0.4`); the Hub owns display. `available` None, or an unavailable entity,
    means the device could not be asked, which the Hub must show as unknown and
    never as current. An `unknown` state is NOT that: HA reads an update entity
    as unknown whenever the INSTALLED version is missing, which is every Sensa
    ESP that is switched off, while its `latest_version` comes from the channel
    manifest and is still a real answer.

    The NAME is the one the CUSTOMER sees everywhere else: the Sensa registry's
    `display_name`, matched through the `sensa_device_id` the entity carries. HA's
    friendly name ("Cerbo GX Venus OS") is only the fallback.
    """
    names = _device_names(hass)
    rows: list[dict[str, Any]] = []
    for state in hass.states.async_all("update"):
        attrs = state.attributes or {}
        kind = attrs.get(FIRMWARE_KIND_ATTR)
        if not kind:
            continue
        reachable = state.state not in ("unavailable", None)
        name = names.get(str(attrs.get(FIRMWARE_DEVICE_ATTR) or "")) or str(
            attrs.get("friendly_name") or state.entity_id
        )
        rows.append({
            "kind": str(kind),
            "name": name,
            "installed": attrs.get("installed_version") or None,
            "available": (attrs.get("latest_version") or None) if reachable else None,
            "in_progress": bool(attrs.get("in_progress")),
            "auto": _auto_flag(hass, attrs.get(FIRMWARE_AUTO_KEY_ATTR)),
            # A Sensa device's build code, which its rollout group's pin is keyed
            # by; None for equipment, which is judged by kind.
            "code": attrs.get(FIRMWARE_CODE_ATTR) or None,
        })
    rows.sort(key=lambda r: (r["kind"], str(r["name"]).lower()))
    return rows


@callback
def _auto_flag(hass: HomeAssistant, runtime_key: Any) -> bool | None:
    """The customer's automatic-updates switch for this row's kind.

    None when the entity names no switch (a Sensa ESP device) or the switch has
    not restored yet: the Hub shows "not reported", never a guessed default.
    """
    if not runtime_key:
        return None
    for runtime in (hass.data.get(DOMAIN) or {}).values():
        if isinstance(runtime, dict) and isinstance(runtime.get(runtime_key), bool):
            return runtime[runtime_key]
    return None


@callback
def _device_names(hass: HomeAssistant) -> dict[str, str]:
    """`{device_id: display_name}` from the backend snapshot, best-effort."""
    names: dict[str, str] = {}
    try:
        for runtime in (hass.data.get(DOMAIN) or {}).values():
            if not isinstance(runtime, dict):
                continue
            coordinator = runtime.get("coordinator")
            data = getattr(coordinator, "data", None)
            devices = data.get("devices") if isinstance(data, dict) else None
            for device in devices or []:
                if not isinstance(device, dict):
                    continue
                device_id = str(device.get("device_id") or device.get("id") or "")
                name = str(device.get("display_name") or "").strip()
                if device_id and name:
                    names[device_id] = name
    except Exception:
        # A name is a nicety; the row still goes without one.
        _LOGGER.debug("firmware report: could not read device names", exc_info=True)
    return names


async def async_push(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Send the current rows to the App. True when it accepted them.

    Best-effort: an unreachable App (starting after an OTA) just means the Hub
    sees this van's firmware on the next heartbeat instead.
    """
    runtime = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    client = runtime.get("client")
    if client is None:
        return False
    rows = build_rows(hass)
    try:
        await client.async_post_firmware_report(
            rows=rows,
            collected_at=datetime.now(timezone.utc).isoformat(),
        )
    except (ConnectionError, TimeoutError):
        _LOGGER.debug("firmware report: App unreachable, retrying on the next tick")
        return False
    except Exception:
        # Reporting never breaks anything upstream.
        _LOGGER.debug("firmware report: push failed", exc_info=True)
        return False
    _LOGGER.debug("firmware report: pushed %d row(s)", len(rows))
    return True


@callback
def async_setup_firmware_report(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push the equipment firmware shortly after start, then every 15 minutes.

    A timer rather than a state listener: a firmware version changes a few times a
    year, the App's copy only has to be fresher than the hourly phone-home, and a
    re-assert on a timer is what survives a lost push (the same rule the remote
    indicator follows). No unsub bookkeeping beyond the entry's own unload.
    """

    async def _tick(_now: Any = None) -> None:
        # What the channel publishes FIRST, then the rows: the ESP entities read
        # that answer for their `latest_version`, so refreshing after the push
        # would report every van one heartbeat behind on the Available column.
        from .esp_update import async_refresh_available
        from .zigbee_firmware import async_check

        await async_refresh_available(hass, entry)
        # Zigbee devices answer on their own state topic, so this push may be a
        # tick behind them; daily checks make that immaterial.
        try:
            await async_check(hass)
        except Exception:
            _LOGGER.debug("firmware report: zigbee firmware check failed", exc_info=True)
        await async_push(hass, entry)

    entry.async_on_unload(
        async_call_later(hass, FIRST_PUSH_DELAY_SECONDS, _tick)
    )
    entry.async_on_unload(
        async_track_time_interval(hass, _tick, timedelta(minutes=REFRESH_MINUTES))
    )
