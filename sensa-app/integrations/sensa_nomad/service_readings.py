"""Send the readings of a device the owner has let a service company see.

The owner chooses, per kind of device, a company whose engineers may look at
it (MySensa, held by Hub). The cloud hands that list down on every check-in,
and this sends the readings of those devices, and nothing else, through the
App to the cloud: every minute, and every few seconds while an engineer has the
device open (the cloud answers how long to wait).

Outbound only. Nothing reaches into the van, and the owner's own remote access
switch is not involved. An empty or missing list sends nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, RUNTIME_LICENCE_STATUS, SENSA_LOG_SOURCE_SYSTEM

_LOGGER = logging.getLogger(__name__)

_IDLE_SECONDS = 60
# The cloud answers this interval (or less) only while an engineer has the
# device open, so it is how the van knows somebody is looking.
_WATCHED_SECONDS = 10
# Fired when who is looking changes; the banner's sensor listens.
SIGNAL_SERVICE_WATCHING = "sensa_nomad_service_watching"
_MIN_SECONDS = 5
# Below the cloud's cap (30,000 characters of JSON), with room to spare.
_MAX_CHARS = 28_000
# Readings of buttons and housekeeping a remote engineer cannot use.
_DROP_KEYS = (
    "read_button",
    "report_button",
    "report_entity",
    "unlock_button",
    "pump_run_button",
    "stop_button",
    "fan_run_button",
    "fan_stop_button",
    "fuel_run_button",
    "history",
)
_MAX_ENTITIES = 150


def _providers(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str] | None:
    """device type -> the company the owner lets see it, as the cloud last said.
    None when it has never said."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    status = runtime.get(RUNTIME_LICENCE_STATUS) or {}
    consents = status.get("service_consents")
    if not isinstance(consents, list):
        return None
    return {
        str(c["device_type"]): str(c.get("provider") or "A service company")
        for c in consents
        if isinstance(c, dict) and c.get("device_type")
    }


def consented_types(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """The device types the cloud last said the owner lets a company see."""
    return sorted((_providers(hass, entry) or {}).keys())


def watching(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, str]]:
    """Who is looking at which device right now, for the banner."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    return [
        {"provider": provider, "device": device}
        for provider, device in (runtime.get("service_watching") or {}).values()
    ]


def _device_label(hass: HomeAssistant, entry: ConfigEntry, device_type: str) -> str:
    """The owner's own name for a device of this type, else the catalogue's."""
    from .device_catalog import PHYSICAL_DEVICE_CATALOG, catalog_id_for

    for row in _rows(hass, entry):
        if catalog_id_for(row) == device_type:
            name = row.get("display_name") or row.get("name")
            if name:
                return str(name)
    for e in PHYSICAL_DEVICE_CATALOG:
        if e.get("id") == device_type and e.get("name"):
            return str(e["name"])
    return "a device"


def _log(hass: HomeAssistant, message: str) -> None:
    from ._log import sensa_log

    sensa_log(hass, "info", message, source=SENSA_LOG_SOURCE_SYSTEM)


def _note_sharing(
    hass: HomeAssistant, entry: ConfigEntry, providers: dict[str, str] | None
) -> None:
    """Log each device that starts or stops being shared.

    The first answer after a start is the baseline, not a change: a restart must
    not announce every standing consent again.
    """
    if providers is None:
        return
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return
    before = runtime.get("service_shared")
    runtime["service_shared"] = dict(providers)
    if before is None:
        return
    for device_type, provider in providers.items():
        if before.get(device_type) != provider:
            label = _device_label(hass, entry, device_type)
            _log(hass, f"{provider} can now see the readings of your {label}")
    for device_type, provider in before.items():
        if device_type not in providers:
            label = _device_label(hass, entry, device_type)
            _log(hass, f"{provider} can no longer see your {label}")


def _set_watched(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_type: str,
    provider: str | None,
) -> bool:
    """Record whether somebody is looking at this device type; log and return
    True when that changed."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return False
    current: dict[str, tuple[str, str]] = runtime.setdefault("service_watching", {})
    was = current.get(device_type)
    if provider is not None and was is None:
        label = _device_label(hass, entry, device_type)
        current[device_type] = (provider, label)
        _log(hass, f"{provider} is looking at your {label}")
        return True
    if provider is None and was is not None:
        del current[device_type]
        _log(hass, f"{was[0]} has finished looking at your {was[1]}")
        return True
    return False


def _rows(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    devices = (getattr(coordinator, "data", None) or {}).get("devices") or []
    return [d for d in devices if isinstance(d, dict)]


def _entity_readings(hass: HomeAssistant, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Every entity of this device's Home Assistant device(s), as plain values."""
    from homeassistant.helpers import entity_registry as er

    from .mode_eligibility import _ha_device_ids_for_sensa_device

    reg = er.async_get(hass)
    out: list[dict[str, Any]] = []
    for ha_id in _ha_device_ids_for_sensa_device(hass, row):
        for ent in er.async_entries_for_device(reg, ha_id):
            st = hass.states.get(ent.entity_id)
            if st is None:
                continue
            out.append(
                {
                    "name": st.attributes.get("friendly_name") or ent.entity_id,
                    "state": st.state,
                    "unit": st.attributes.get("unit_of_measurement"),
                }
            )
            if len(out) >= _MAX_ENTITIES:
                return out
    return out


def _heater_readings(
    hass: HomeAssistant, entry: ConfigEntry, device_id: str
) -> dict[str, Any] | None:
    from .heater_diagnostics import diagnostics_for, heater_rows

    for row in heater_rows(hass, entry):
        if str(row.get("device_id") or "") == device_id:
            data = diagnostics_for(hass, entry, row)
            return {k: v for k, v in data.items() if k not in _DROP_KEYS}
    return None


def build_snapshot(
    hass: HomeAssistant, entry: ConfigEntry, device_type: str
) -> dict[str, Any] | None:
    """The readings of every device of this type in the van, or None if none."""
    from .device_catalog import catalog_id_for

    devices = []
    for row in _rows(hass, entry):
        if catalog_id_for(row) != device_type:
            continue
        device_id = str(row.get("device_id") or "")
        item: dict[str, Any] = {
            "name": row.get("display_name") or row.get("name") or device_type,
            "device_id": device_id,
        }
        heater = _heater_readings(hass, entry, device_id)
        if heater is not None:
            item["diagnostics"] = heater
        else:
            item["readings"] = _entity_readings(hass, row)
        devices.append(item)
    if not devices:
        return None
    snapshot = {
        "collected_at": datetime.now(UTC).isoformat(),
        "devices": devices,
    }
    text = json.dumps(snapshot, default=str)
    if len(text) > _MAX_CHARS:
        # Too much to send whole: keep the names, say so, never send a part
        # that reads as complete.
        snapshot = {
            "collected_at": snapshot["collected_at"],
            "devices": [{"name": d["name"], "device_id": d["device_id"]} for d in devices],
            "truncated": True,
        }
    # Round-trip so anything not JSON (a datetime) arrives as text.
    return json.loads(json.dumps(snapshot, default=str))


async def _send_once(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Send every consented device type once; return seconds until the next."""
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    providers = _providers(hass, entry)
    _note_sharing(hass, entry, providers)
    providers = providers or {}
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    changed = False
    # Nobody is looking at a device that is no longer shared.
    for device_type in list((runtime.get("service_watching") or {}).keys()):
        if device_type not in providers:
            changed |= _set_watched(hass, entry, device_type, None)
    if changed:
        async_dispatcher_send(hass, SIGNAL_SERVICE_WATCHING)
    types = sorted(providers)
    if not types:
        return _IDLE_SECONDS
    client = runtime.get("client")
    if client is None:
        return _IDLE_SECONDS
    wait = _IDLE_SECONDS
    changed = False
    for device_type in types:
        snapshot = build_snapshot(hass, entry, device_type)
        if snapshot is None:
            continue
        try:
            resp = await client.async_post_json(
                "/api/v1/service/snapshot",
                {"device_type": device_type, "snapshot": snapshot},
                timeout=20,
            )
        except Exception:  # noqa: BLE001 - never worth breaking the loop
            _LOGGER.debug("service readings for %s not sent", device_type, exc_info=True)
            continue
        interval = resp.get("interval_seconds") if isinstance(resp, dict) else None
        if isinstance(interval, int):
            wait = min(wait, max(_MIN_SECONDS, interval))
            looking = interval <= _WATCHED_SECONDS
            changed |= _set_watched(
                hass, entry, device_type, providers[device_type] if looking else None
            )
    if changed:
        async_dispatcher_send(hass, SIGNAL_SERVICE_WATCHING)
    return wait


async def async_run(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The loop. Cancelled when the entry unloads."""
    while True:
        try:
            wait = await _send_once(hass, entry)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug("service readings tick failed", exc_info=True)
            wait = _IDLE_SECONDS
        await asyncio.sleep(wait)


def async_start(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start the loop once per entry and stop it on unload."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None or runtime.get("service_readings_task") is not None:
        return
    task = hass.async_create_background_task(
        async_run(hass, entry), "sensa_nomad_service_readings"
    )
    runtime["service_readings_task"] = task
    entry.async_on_unload(task.cancel)
