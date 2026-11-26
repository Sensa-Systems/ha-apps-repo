"""Sensa DC Meter 6: watts per 12V circuit.

An ESP32 with up to six INA226 shunt monitors, one cut into the positive feed
of each circuit after its fuse (Sensa.Control.Firmware, `ss-dcm-6-c008/c124`).
HA's native `esphome` integration owns the sensors; the firmware names them
"Circuit <n> Power" / "Current" / "Voltage", and an INA226 that is not fitted
reports unavailable, so a box with fewer than six still works.

The customer names each circuit on the meter's Settings -> Devices row; the
names live on the entry (`DC_CIRCUIT_LABELS_OPTION`) and the Electric tab reads
them through `circuit_name`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DC_CIRCUIT_LABELS_OPTION, DC_METER_CHANNELS


def is_dc_meter(device: dict[str, Any]) -> bool:
    """True for a Sensa DC Meter row; the one test every caller uses."""
    from .vendors.service_classes import classify

    return classify(device) == "dc_meter"


def circuit_power_entities(hass: HomeAssistant, device: dict[str, Any]) -> dict[int, str]:
    """Circuit -> the ESPHome `sensor.*` reporting its watts, or empty."""
    from homeassistant.helpers import entity_registry as er

    from . import _esphome_candidate_entry_ids

    candidates = set(_esphome_candidate_entry_ids(hass, device))
    if not candidates:
        return {}
    names = {f"circuit {n} power": n for n in DC_METER_CHANNELS}
    found: dict[int, str] = {}
    for ent in er.async_get(hass).entities.values():
        if ent.config_entry_id not in candidates or ent.disabled_by is not None:
            continue
        if ent.entity_id.split(".", 1)[0] != "sensor":
            continue
        n = names.get((ent.original_name or ent.name or "").strip().lower())
        if n is not None:
            found.setdefault(n, ent.entity_id)
    return found


def circuit_name(entry: ConfigEntry | None, device_id: str, n: int) -> str:
    """The customer's name for one circuit, else "Circuit <n>"."""
    stored = ""
    if entry is not None:
        stored = str(
            ((entry.options.get(DC_CIRCUIT_LABELS_OPTION) or {}).get(device_id) or {})
            .get(f"circuit_{n}") or ""
        ).strip()
    return stored or f"Circuit {n}"
