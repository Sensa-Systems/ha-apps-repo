"""Per-mode "good start" default generator for the Mode Editor.

The Mode Editor's **Defaults** button asks "what's a sensible starting
state for this mode, given the devices I currently have?". This module
answers that: a per-mode profile keyed by *service class* (lighting,
water_pump, …) maps onto the customer's currently-added, eligible
controllable entities.

The result is a `{entity_id: {state}}` dict in the same shape the editor's
edit buffer + `sensa_nomad.update_mode` service already use. The button
loads it as a PREVIEW — nothing is persisted until the customer presses
Save — so re-pressing after adding a device just recomputes for the new
full device set, and the customer always sees (and can tweak) the result
before committing.

Scope (Richard, 2026-06-25): the **switchable loads** only — lighting,
water pump / heater, dump valve, ventilation, space heater, refrigeration
(plus `light.*` / `fan.*` domain entities). Dedicated AC / diesel-hydronic
heater devices (climate setpoints) are deliberately out of scope for now.

Only entities that survive the eligibility pipeline
(`mode_eligibility.compute_eligible_entities`) are ever touched, so the
excluded Buzzer / Debug Logging switches and any non-scene entity can
never be defaulted.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .mode_eligibility import compute_eligible_entities
from .vendors.service_classes import is_relay_device

_LOGGER = logging.getLogger(__name__)


# Per-mode recommended state, keyed by service class. Only the switchable
# loads appear here; a service class absent from a mode's row is simply
# left untouched (no entity of that kind is set for that mode).
#
# Conventions baked into the table:
#   - refrigeration is ON in every mode (keep the food cold).
#   - dump_valve + space_heater are OFF in every mode (never a safe
#     default-on; the customer turns them on deliberately).
#   - Parked makes water available; Work adds lights + ventilation.
#
# This is product judgement, tuned freely — the editor previews the result
# before anything is saved, so a wrong call here is visible and editable.
MODE_DEFAULT_PROFILE: dict[str, dict[str, str]] = {
    "service": {
        "lighting": "off", "water_pump": "off", "water_heater": "off",
        "dump_valve": "off", "ventilation": "off", "space_heater": "off",
        "refrigeration": "on",
    },
    "sentinel": {
        "lighting": "off", "water_pump": "off", "water_heater": "off",
        "dump_valve": "off", "ventilation": "off", "space_heater": "off",
        "refrigeration": "on",
    },
    # Drive: everything off for safety while moving; fridge stays on.
    "driving": {
        "lighting": "off", "water_pump": "off", "water_heater": "off",
        "dump_valve": "off", "ventilation": "off", "space_heater": "off",
        "refrigeration": "on",
    },
    "parking": {
        "lighting": "off", "water_pump": "on", "water_heater": "off",
        "dump_valve": "off", "ventilation": "off", "space_heater": "off",
        "refrigeration": "on",
    },
    "standby": {
        "lighting": "off", "water_pump": "off", "water_heater": "off",
        "dump_valve": "off", "ventilation": "off", "space_heater": "off",
        "refrigeration": "on",
    },
    "work": {
        "lighting": "on", "water_pump": "on", "water_heater": "off",
        "dump_valve": "off", "ventilation": "on", "space_heater": "off",
        "refrigeration": "on",
    },
    "movie": {
        "lighting": "off", "water_pump": "off", "water_heater": "off",
        "dump_valve": "off", "ventilation": "off", "space_heater": "off",
        "refrigeration": "on",
    },
    "sleep": {
        "lighting": "off", "water_pump": "off", "water_heater": "off",
        "dump_valve": "off", "ventilation": "off", "space_heater": "off",
        "refrigeration": "on",
    },
}

# Entity-domain → service class for entities that aren't relay-routed
# (a Zigbee bulb owns a `light.*`, a fan a `fan.*`). Relay channels carry
# their service class explicitly via `relay_assignments`.
_DOMAIN_TO_SERVICE_CLASS: dict[str, str] = {
    "light": "lighting",
    "fan": "ventilation",
}


def _relay_entity_service_classes(
    devices: list[dict[str, Any]],
) -> dict[str, str]:
    """Map each enabled, Switched-Device relay output entity_id to its
    assigned service-class key.

    Mirrors `dashboard._routed_switch_tiles_by_category`: walk every relay
    device's `relay_assignments` / `relay_entities` / `relay_enabled`,
    keeping channels whose assignment is a routable Switched-Device class
    (skips unassigned, disabled, and Integrated-Device `device:<id>`
    assignments, which aren't bare loads).
    """
    from .vendors.service_classes import RELAY_ASSIGNABLE_SERVICE_CLASSES

    mapping: dict[str, str] = {}
    for device in devices:
        if not is_relay_device(device):
            continue
        assignments = device.get("relay_assignments") or {}
        relay_entities = device.get("relay_entities") or {}
        relay_enabled = device.get("relay_enabled") or {}
        for n in range(1, 9):
            slot = f"relay_{n}"
            assignment = assignments.get(slot, "unassigned")
            if assignment not in RELAY_ASSIGNABLE_SERVICE_CLASSES:
                continue
            if relay_enabled.get(slot) is False:
                continue
            entity_id = relay_entities.get(slot)
            if not entity_id:
                continue
            mapping[entity_id] = assignment
    return mapping


def compute_mode_default(
    hass: HomeAssistant,
    devices: list[dict[str, Any]],
    mode_slug: str,
) -> dict[str, dict[str, Any]]:
    """Return the recommended `{entity_id: {state}}` for one mode.

    Built ONLY from currently-added, eligible entities (the same set the
    Add dropdown offers), so it never touches an absent device or an
    excluded entity. An empty dict is a valid result (no matching device).
    """
    profile = MODE_DEFAULT_PROFILE.get((mode_slug or "").strip().lower())
    if not profile:
        return {}

    try:
        eligible, _traces = compute_eligible_entities(hass, devices)
    except Exception:  # pragma: no cover - defensive
        _LOGGER.warning(
            "mode_defaults: eligibility compute failed for %s",
            mode_slug, exc_info=True,
        )
        return {}

    relay_classes = _relay_entity_service_classes(devices)

    result: dict[str, dict[str, Any]] = {}
    for entity_id in eligible:
        service_class = relay_classes.get(entity_id)
        if service_class is None:
            domain = entity_id.split(".", 1)[0]
            service_class = _DOMAIN_TO_SERVICE_CLASS.get(domain)
        if service_class is None:
            continue
        state = profile.get(service_class)
        if state is None:
            continue
        result[entity_id] = {"state": state, "attributes": {}}
    return result
