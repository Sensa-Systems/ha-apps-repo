"""Sensa Dial: feed each knob its readings, and act on what it is turned to.

The Dial (`Sensa.Control.Firmware/source/devices/dial/ss-dial-k130.yaml`) looks
nothing up itself. Every TICK, and at once when something it shows changes, this
module calls its API actions with what each of its five modes shows:

* Climate: the SensaClimate thermostat, as its card shows it, and only when
  that card exists (`climate_available`). The target is two-way: the Dial's
  Climate Target number follows Climate Control's target, and turning the knob
  sets it. A click turns Climate Control on or off, the card's power button.
  After it comes a page for each heater on the van (`heater_items`); a double
  click moves on. On a heater's page the Dial's Heater Target and Heater On
  are that heater's own target and power, both two-way.
* Lights: what the Lighting tab shows, in its order and under its names
  (`lighting_items`). On/off is two-way, and brightness too for a light that
  dims; a relay channel is on/off only. A double click moves to the next one.
* Tanks: the first tank of each fluid the Home dials know, and Diesel Days Left.
* Electrical: battery, solar, shore, alternator and loads, through
  `dashboard.resolve_role`, the same answer the Home dials give.
* Bedside: the Dial's Night Mode is Sleep. Switching it on enters Sleep,
  switching it off enters Parked, and Sleep entered elsewhere shows on the Dial.

Anything the van has no source for goes as NaN, which the Dial reads as "not
fitted" and skips. A reading that is unavailable also goes as NaN: the Dial
shows "--" for "could not ask", never a confident zero. And the Dial shows
"--" by itself after five minutes without a push, so a box that stops feeding it
cannot leave an old number on screen.

Every write is checked against the value already there first, so a value this
module wrote to one side and that echoes back from the other is a no-op rather
than a loop. A value the customer has just turned on the Dial is not pushed back
over for ECHO_GUARD, so a slow round trip cannot snap the knob back mid-turn.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CLIMATE_CONTROL_STATUS_ENTITY_ID,
    CLIMATE_CONTROL_SWITCH_ENTITY_ID,
    CLIMATE_CONTROL_TARGET_ENTITY_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

TICK = timedelta(seconds=30)
ECHO_GUARD = 5.0  # seconds
SLEEP_MODE_SLUG = "sleep"
WAKE_MODE_SLUG = "parking"

# The Dial's own entities, as (domain, entity name) -> role. Matched by name
# within the Dial's ESPHome config entry, never by a guessed entity_id.
DIAL_ENTITIES: dict[tuple[str, str], str] = {
    ("select", "mode"): "mode",
    ("number", "climate target"): "climate_target",
    ("number", "heater target"): "heater_target",
    ("switch", "heater on"): "heater_on",
    ("number", "light brightness"): "light_brightness",
    ("switch", "light on"): "light_on",
    ("switch", "night mode"): "night_mode",
    ("event", "dial press"): "dial_press",
}

# (entry_id, device_id) -> the Climate page the knob is on: a heater's
# device_id, or "" for the SensaClimate page.
_heater_choice: dict[tuple[str, str], str] = {}
# (entry_id, device_id) -> the light the knob drives.
_light_choice: dict[tuple[str, str], str] = {}
# (entry_id, device_id) -> when the customer last turned or pressed the Dial.
_touched: dict[tuple[str, str], float] = {}
# (entry_id, device_id) -> whether the last push reached it, so a Dial that
# drops off is logged once on the edge.
_push_ok: dict[tuple[str, str], bool] = {}
# (entry_id, device_id, action) the Dial's firmware has refused, logged once.
_refused: set[tuple[str, str, str]] = set()


def is_dial(device: dict[str, Any]) -> bool:
    """True for a Sensa Dial row; the one test every caller uses."""
    from .vendors.service_classes import classify

    return classify(device) == "dial"


def dial_entities(hass: HomeAssistant, device: dict[str, Any]) -> dict[str, str]:
    """Role (`DIAL_ENTITIES` values) -> this Dial's entity_id, or empty."""
    from homeassistant.helpers import entity_registry as er

    from . import _esphome_candidate_entry_ids

    candidates = set(_esphome_candidate_entry_ids(hass, device))
    if not candidates:
        return {}
    found: dict[str, str] = {}
    for ent in er.async_get(hass).entities.values():
        if ent.config_entry_id not in candidates or ent.disabled_by is not None:
            continue
        domain = ent.entity_id.split(".", 1)[0]
        role = DIAL_ENTITIES.get((domain, (ent.original_name or ent.name or "").strip().lower()))
        if role is not None:
            found.setdefault(role, ent.entity_id)
    return found


def _dials(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    devices = ((getattr(coordinator, "data", None) or {}).get("devices")) or []
    return [d for d in devices if isinstance(d, dict) and d.get("device_id") and is_dial(d)]


def _float(hass: HomeAssistant, entity_id: str | None) -> float:
    """An entity's state as a float, NaN when absent or not a number."""
    st = hass.states.get(entity_id) if entity_id else None
    try:
        value = float(st.state) if st is not None else math.nan
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def _sum(hass: HomeAssistant, entity_ids: list[str]) -> float:
    """The live readings added up, NaN when none of them reads."""
    values = [v for v in (_float(hass, e) for e in entity_ids) if not math.isnan(v)]
    return sum(values) if values else math.nan


def _close(a: Any, b: Any, tolerance: float) -> bool:
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False


# -- what the Dial shows ------------------------------------------------------


def climate_available() -> bool:
    """The Dial's Climate screen exists exactly when SensaClimate can be
    switched on: the SensaClimate tab is there (a heater or air conditioner on
    the van, Pro and above) and one is chosen, else the switch refuses
    (`climate_control.async_set_enabled`). That is the Climate tab's thermostat
    card rule, so the Dial and the card cannot disagree, and the screen appears
    by itself once a unit is chosen, with no reflash."""
    from .dashboard import _climate_control_card

    return _climate_control_card() is not None


def climate_reading(hass: HomeAssistant) -> dict[str, Any]:
    """The `set_climate` payload: what the SensaClimate thermostat card shows
    (`sensa-climate-control-card.ts`), worked out the way it works it out.

    `state` is off while Climate Control is switched off, else the status
    sensor's heating / cooling / idle / waiting (anything else reads waiting).
    `current` is the reading of the status sensor's `sensor_entity`, in the
    target's unit. `unit_name` names the unit doing it, only while heating or
    cooling; `reason` says why it is waiting, only while waiting.
    """
    from .climate_control import _from_unit, _to_unit

    target = hass.states.get(CLIMATE_CONTROL_TARGET_ENTITY_ID)
    t_attrs = target.attributes if target is not None else {}
    unit = str(t_attrs.get("unit_of_measurement") or "°C")
    switch = hass.states.get(CLIMATE_CONTROL_SWITCH_ENTITY_ID)
    status = hass.states.get(CLIMATE_CONTROL_STATUS_ENTITY_ID)
    attrs = status.attributes if status is not None else {}
    if switch is None or switch.state != "on":
        state = "off"
    else:
        raw_state = status.state if status is not None else ""
        state = raw_state if raw_state in ("heating", "cooling", "idle", "waiting") else "waiting"

    current = math.nan
    sensor = attrs.get("sensor_entity")
    sensor_st = hass.states.get(sensor) if sensor else None
    if sensor_st is not None:
        if str(sensor).startswith("climate."):
            raw, src = sensor_st.attributes.get("current_temperature"), str(
                getattr(getattr(hass.config, "units", None), "temperature_unit", "") or unit
            )
        else:
            raw, src = sensor_st.state, str(sensor_st.attributes.get("unit_of_measurement") or unit)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = math.nan
        if math.isfinite(value):
            current = _to_unit(_from_unit(value, src), unit)

    def _attr(key: str, default: float) -> float:
        try:
            return float(t_attrs.get(key))
        except (TypeError, ValueError):
            return default

    return {
        "available": climate_available(),
        "state": state,
        "current": current,
        "unit": unit,
        "unit_name": str(attrs.get("unit") or "") if state in ("heating", "cooling") else "",
        "reason": str(attrs.get("reason") or "") if state == "waiting" else "",
        "min": _attr("min", 10.0),
        "max": _attr("max", 30.0),
        "step": _attr("step", 0.5),
    }


def heater_items(hass: HomeAssistant, entry: ConfigEntry) -> list[tuple[str, str, dict[str, str]]]:
    """(device_id, name, role map) for every heater on the van that can be
    switched on (it has a power role), in the device list's order."""
    from .const import HEATER_SERVICE_CLASSES
    from .dashboard import _device_service_class
    from .heater_temp_source import firmware_entities

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    devices = ((getattr(coordinator, "data", None) or {}).get("devices")) or []
    items: list[tuple[str, str, dict[str, str]]] = []
    for device in devices:
        if not isinstance(device, dict) or not device.get("device_id"):
            continue
        if _device_service_class(device) not in HEATER_SERVICE_CLASSES:
            continue
        roles = firmware_entities(hass, device)
        if roles.get("power"):
            name = str(device.get("display_name") or device.get("name") or "Heater")
            items.append((str(device["device_id"]), name, roles))
    return items


def climate_pages(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """The Dial's Climate pages in order: "" for SensaClimate when it can be
    switched on, then each heater's device_id."""
    return ([""] if climate_available() else []) + [did for did, _n, _r in heater_items(hass, entry)]


def current_heater(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> str | None:
    """The Climate page this Dial is on: a heater's device_id, "" for
    SensaClimate, None when there is no Climate page at all."""
    pages = climate_pages(hass, entry)
    if not pages:
        return None
    key = (entry.entry_id, device_id)
    chosen = _heater_choice.get(key)
    if chosen not in pages:
        chosen = pages[0]
        _heater_choice[key] = chosen
    return chosen


def next_heater(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> str | None:
    """Move this Dial on from the page it is on to the next, wrapping round."""
    chosen = current_heater(hass, entry, device_id)
    if chosen is None:
        return None
    pages = climate_pages(hass, entry)
    page = pages[(pages.index(chosen) + 1) % len(pages)]
    _heater_choice[(entry.entry_id, device_id)] = page
    return page


def _heater_roles(hass: HomeAssistant, entry: ConfigEntry, heater_id: str | None) -> dict[str, str]:
    for did, _name, roles in heater_items(hass, entry):
        if did == heater_id:
            return roles
    return {}


def heater_reading(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> dict[str, Any]:
    """The `set_heater` payload for the page this Dial is on. `index` -1 is the
    SensaClimate page. `current` is the reading the heater regulates on (the
    one its card shows as "Now", `heater_temp_source.regulating_temp_entity`),
    in its target's unit."""
    from .climate_control import _from_unit, _to_unit
    from .heater_temp_source import choice_for, regulating_temp_entity

    items = heater_items(hass, entry)
    chosen = current_heater(hass, entry, device_id)
    ids = [did for did, _n, _r in items]
    payload: dict[str, Any] = {
        "index": -1, "count": len(items), "name": "", "state": "", "current": math.nan,
        "unit": "°C", "min": 5.0, "max": 35.0, "step": 1.0,
    }
    if not chosen or chosen not in ids:
        return payload
    _did, name, roles = items[ids.index(chosen)]
    target = hass.states.get(roles.get("target_temperature", ""))
    t_attrs = target.attributes if target is not None else {}
    unit = str(t_attrs.get("unit_of_measurement") or "°C")
    status = hass.states.get(roles.get("run_state", ""))
    reading = regulating_temp_entity(roles, choice_for(entry, chosen))
    reading_st = hass.states.get(reading) if reading else None
    current = math.nan
    if reading_st is not None:
        try:
            value = float(reading_st.state)
        except (TypeError, ValueError):
            value = math.nan
        if math.isfinite(value):
            src = str(reading_st.attributes.get("unit_of_measurement") or unit)
            current = _to_unit(_from_unit(value, src), unit)

    def _attr(key: str, default: float) -> float:
        try:
            return float(t_attrs.get(key))
        except (TypeError, ValueError):
            return default

    state = status.state if status is not None and status.state not in ("unknown", "unavailable") else ""
    payload.update({
        "index": ids.index(chosen),
        "name": name,
        "state": str(state),
        "current": current,
        "unit": unit,
        "min": _attr("min", 5.0),
        "max": _attr("max", 35.0),
        "step": _attr("step", 1.0),
    })
    return payload


def tank_readings(hass: HomeAssistant) -> dict[str, float]:
    """The `set_tanks` payload: the first tank of each fluid, in percent."""
    from .dashboard import _tank_levels_by_fluid
    from .diesel_use import DAYS_LEFT_ENTITY_ID

    by_fluid = _tank_levels_by_fluid()

    def first(*fluids: str) -> float:
        for fluid in fluids:
            for eid, _device in by_fluid.get(fluid) or []:
                return _float(hass, eid)
        return math.nan

    return {
        "fresh": first("fresh-water"),
        "grey": first("waste-water"),
        "black": first("black-water"),
        "diesel": first("diesel", "fuel"),
        "diesel_days": _float(hass, DAYS_LEFT_ENTITY_ID),
        "lpg": first("lpg"),
    }


def electrical_readings(hass: HomeAssistant) -> dict[str, float]:
    """The `set_electrical` payload, through the Home dials' own roles."""
    from .dashboard import _role_list

    def role(*names: str) -> float:
        return _sum(hass, [eid for name in names for eid in _role_list(name)])

    return {
        "soc": role("battery_soc"),
        "battery_v": role("battery_voltage"),
        "battery_w": role("battery_power"),
        "solar_w": role("solar_power"),
        "shore_w": role("grid_power"),
        "alternator_w": role("alternator"),
        "load_w": role("dc_load", "ac_load"),
    }


def lighting_items() -> list[tuple[str, str]]:
    """(entity_id, name) for every control on the Lighting tab, in the tab's
    order and under the tab's names: Light Groups, the relay channels routed to
    lighting, then the Zigbee and Shelly lights.

    Read off `dashboard._lighting_tab_cards`, the function that builds the
    tab, so the Dial offers exactly what the tab shows and nothing else (a
    heater controller's status LED is not on the tab, so it is not on the
    Dial). An entity on the tab twice is offered once."""
    from .dashboard import _lighting_tab_cards

    items: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(entity_id: Any, name: Any) -> None:
        eid = str(entity_id or "")
        if eid and eid not in seen and eid.split(".", 1)[0] in ("light", "switch"):
            seen.add(eid)
            items.append((eid, str(name or "")))

    for card in _lighting_tab_cards():
        for tile in card.get("tiles") or []:
            add(tile.get("entity"), tile.get("name"))
        add(card.get("entity"), card.get("name"))
    return items


def is_dimmable(hass: HomeAssistant, entity_id: str | None) -> bool:
    """A light that dims. A relay channel (a switch) or an on/off-only light
    gets On / Off on the Dial and no brightness."""
    if not entity_id or not entity_id.startswith("light."):
        return False
    st = hass.states.get(entity_id)
    modes = list((st.attributes.get("supported_color_modes") or []) if st is not None else [])
    return bool(modes) and modes != ["onoff"]


def current_light(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> str | None:
    """The light this Dial drives: the last one moved to, else the first."""
    lights = [eid for eid, _name in lighting_items()]
    if not lights:
        return None
    key = (entry.entry_id, device_id)
    chosen = _light_choice.get(key)
    if chosen not in lights:
        chosen = lights[0]
        _light_choice[key] = chosen
    return chosen


def next_light(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> str | None:
    """Move this Dial on to the next light, wrapping round."""
    lights = [eid for eid, _name in lighting_items()]
    if not lights:
        return None
    key = (entry.entry_id, device_id)
    chosen = _light_choice.get(key)
    index = (lights.index(chosen) + 1) % len(lights) if chosen in lights else 0
    _light_choice[key] = lights[index]
    return lights[index]


def _light_name(hass: HomeAssistant, entity_id: str | None) -> str:
    """The name the Lighting tab gives it, else HA's friendly name."""
    if not entity_id:
        return ""
    for eid, name in lighting_items():
        if eid == entity_id and name:
            return name
    st = hass.states.get(entity_id)
    if st is None:
        return ""
    return str(st.attributes.get("friendly_name") or entity_id.split(".", 1)[1])


def _light_pct(st: Any) -> float | None:
    """A light's brightness in percent, None when it does not say."""
    try:
        return round(float(st.attributes.get("brightness")) * 100 / 255)
    except (TypeError, ValueError):
        return None


def _sleeping(hass: HomeAssistant) -> bool | None:
    """Sleep is the current mode: True, False, or None when that is not known."""
    from .modes_catalog import mode_active_entity_id

    st = hass.states.get(mode_active_entity_id(SLEEP_MODE_SLUG))
    if st is None or st.state not in ("on", "off"):
        return None
    return st.state == "on"


# -- pushing to the Dial ------------------------------------------------------


async def _call(hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(domain, service, data, blocking=False)


async def _set_dial_number(hass: HomeAssistant, entity_id: str | None, value: float, tolerance: float) -> None:
    st = hass.states.get(entity_id) if entity_id else None
    if st is None or st.state in ("unavailable", "unknown") or _close(st.state, value, tolerance):
        return
    await _call(hass, "number", "set_value", {"entity_id": entity_id, "value": value})


async def _set_dial_switch(hass: HomeAssistant, entity_id: str | None, on: bool) -> None:
    st = hass.states.get(entity_id) if entity_id else None
    if st is None or st.state not in ("on", "off") or (st.state == "on") == on:
        return
    await _call(hass, "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id})


async def async_feed_dial(hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]) -> None:
    """Push one Dial everything it shows, and bring its two-way entities into
    line with Sensa unless the customer has just turned it."""
    from .esp_update import esphome_entry, esphome_service

    device_id = str(device.get("device_id"))
    key = (entry.entry_id, device_id)
    esp_entry = esphome_entry(hass, device)
    light = current_light(hass, entry, device_id)
    payloads = {
        "set_climate": climate_reading(hass),
        "set_light": {
            "available": light is not None,  # an empty Lighting tab: the Dial skips Lights
            "name": _light_name(hass, light),
            "dimmable": is_dimmable(hass, light),
        },
        "set_heater": heater_reading(hass, entry, device_id),
        "set_tanks": tank_readings(hass),
        "set_electrical": electrical_readings(hass),
    }
    reached = False
    for action, data in payloads.items():
        service = esphome_service(hass, esp_entry, action) if esp_entry else None
        if service is None:
            continue
        refused_key = (*key, action)
        try:
            await _call(hass, "esphome", service, data)
        except (vol.Invalid, HomeAssistantError) as err:
            # Firmware older than this payload (a field it does not know):
            # skip this one screen, keep feeding the rest, say so once.
            if refused_key not in _refused:
                _LOGGER.warning(
                    "Dial %s: its firmware refused %s (%s); update the Dial", device_id, action, err,
                )
                _refused.add(refused_key)
            continue
        _refused.discard(refused_key)
        reached = True
    if reached != _push_ok.get(key, True):
        if reached:
            _LOGGER.info("Dial %s: feeding it again", device_id)
        else:
            _LOGGER.warning("Dial %s: offline or not on Dial firmware; nothing to feed", device_id)
    _push_ok[key] = reached
    if not reached or time.monotonic() - _touched.get(key, -ECHO_GUARD) < ECHO_GUARD:
        return

    ents = dial_entities(hass, device)
    target = _float(hass, CLIMATE_CONTROL_TARGET_ENTITY_ID)
    if not math.isnan(target):
        await _set_dial_number(hass, ents.get("climate_target"), target, 0.25)
    heater_roles = _heater_roles(hass, entry, current_heater(hass, entry, device_id))
    if heater_roles:
        heater_target = _float(hass, heater_roles.get("target_temperature"))
        if not math.isnan(heater_target):
            await _set_dial_number(hass, ents.get("heater_target"), heater_target, 0.25)
        power_st = hass.states.get(heater_roles["power"])
        if power_st is not None and power_st.state in ("on", "off"):
            await _set_dial_switch(hass, ents.get("heater_on"), power_st.state == "on")
    light_st = hass.states.get(light) if light else None
    if light_st is not None and light_st.state in ("on", "off"):
        await _set_dial_switch(hass, ents.get("light_on"), light_st.state == "on")
        pct = _light_pct(light_st) if is_dimmable(hass, light) else None
        if light_st.state == "on" and pct is not None:
            await _set_dial_number(hass, ents.get("light_brightness"), round(pct / 5) * 5, 2.5)
    sleeping = _sleeping(hass)
    if sleeping is not None:
        await _set_dial_switch(hass, ents.get("night_mode"), sleeping)


async def async_feed_dials(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Feed every Dial on the van."""
    for device in _dials(hass, entry):
        try:
            await async_feed_dial(hass, entry, device)
        except Exception:  # one Dial must not stop the rest
            _LOGGER.warning("Dial feed failed for %s", device.get("device_id"), exc_info=True)


# -- acting on the Dial -------------------------------------------------------


async def async_dial_changed(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any], role: str,
    old: Any, new: Any,
) -> None:
    """The customer turned or pressed a Dial: act on it in Sensa."""
    if new is None or new.state in ("unavailable", "unknown"):
        return
    # The Dial coming online, or HA restoring it: not a turn. An event entity
    # reads "unknown" until its first press, so for the press that old state
    # is a real press, not a Dial coming online.
    coming_online = ("unavailable",) if role == "dial_press" else ("unavailable", "unknown")
    if old is None or old.state in coming_online:
        return
    device_id = str(device.get("device_id"))
    _touched[(entry.entry_id, device_id)] = time.monotonic()

    if role == "climate_target":
        if not _close(new.state, _float(hass, CLIMATE_CONTROL_TARGET_ENTITY_ID), 0.25):
            await _call(hass, "number", "set_value", {
                "entity_id": CLIMATE_CONTROL_TARGET_ENTITY_ID, "value": float(new.state),
            })
        return

    if role == "dial_press":
        kind = new.attributes.get("event_type")
        if kind == "climate_mode":
            await _call(hass, "switch", "toggle", {"entity_id": CLIMATE_CONTROL_SWITCH_ENTITY_ID})
        elif kind == "next_heater":
            next_heater(hass, entry, device_id)
            _touched.pop((entry.entry_id, device_id), None)  # show the new heater's target now
            await async_feed_dial(hass, entry, device)
        elif kind == "next_light":
            next_light(hass, entry, device_id)
            _touched.pop((entry.entry_id, device_id), None)  # show the new light's level now
            await async_feed_dial(hass, entry, device)
        return

    if role in ("heater_target", "heater_on"):
        roles = _heater_roles(hass, entry, current_heater(hass, entry, device_id))
        if not roles:
            return
        if role == "heater_target":
            target = roles.get("target_temperature")
            if target and not _close(new.state, _float(hass, target), 0.25):
                await _call(hass, "number", "set_value", {"entity_id": target, "value": float(new.state)})
            return
        power_st = hass.states.get(roles["power"])
        want = new.state == "on"
        if power_st is None or power_st.state != ("on" if want else "off"):
            await _call(hass, "switch", "turn_on" if want else "turn_off", {"entity_id": roles["power"]})
        return

    if role in ("light_brightness", "light_on"):
        light = current_light(hass, entry, device_id)
        light_st = hass.states.get(light) if light else None
        if light_st is None:
            return
        ents = dial_entities(hass, device)
        on_st = hass.states.get(ents.get("light_on", ""))
        want_on = on_st is not None and on_st.state == "on"
        domain = light.split(".", 1)[0]  # a relay channel is a switch
        if not want_on:
            if light_st.state == "on":
                await _call(hass, domain, "turn_off", {"entity_id": light})
            return
        if not is_dimmable(hass, light):
            if light_st.state != "on":
                await _call(hass, domain, "turn_on", {"entity_id": light})
            return
        pct = _float(hass, ents.get("light_brightness"))
        if math.isnan(pct):
            return
        if light_st.state != "on" or not _close(_light_pct(light_st), pct, 2.5):
            await _call(hass, "light", "turn_on", {"entity_id": light, "brightness_pct": pct})
        return

    if role == "night_mode":
        want = new.state == "on"
        if _sleeping(hass) is not want:
            await _call(hass, DOMAIN, "activate_mode", {
                "mode": SLEEP_MODE_SLUG if want else WAKE_MODE_SLUG,
            })


@callback
def async_setup_dial(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Feed every Dial on a timer and when what it shows changes, and act on
    each Dial the moment it is turned."""
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_interval,
    )

    from .modes_catalog import mode_active_entity_id

    tracked: dict[str, Any] = {"ids": frozenset(), "unsub": None, "roles": {}}

    def _watched() -> tuple[frozenset[str], dict[str, tuple[dict[str, Any], str]]]:
        roles: dict[str, tuple[dict[str, Any], str]] = {}
        dials = _dials(hass, entry)
        for device in dials:
            for role, eid in dial_entities(hass, device).items():
                if role != "mode":
                    roles[eid] = (device, role)
        ids = set(roles)
        if dials:
            ids |= {
                CLIMATE_CONTROL_TARGET_ENTITY_ID,
                CLIMATE_CONTROL_STATUS_ENTITY_ID,
                mode_active_entity_id(SLEEP_MODE_SLUG),
            }
            ids |= {
                light for device in dials
                if (light := _light_choice.get((entry.entry_id, str(device.get("device_id")))))
            }
            # The heater each Dial is on: its power, target, status and reading.
            for device in dials:
                chosen = _heater_choice.get((entry.entry_id, str(device.get("device_id"))))
                heater = _heater_roles(hass, entry, chosen) if chosen else {}
                ids |= {
                    eid for r in ("power", "target_temperature", "run_state",
                                  "panel_temperature", "current_temperature")
                    if (eid := heater.get(r))
                }
        return frozenset(ids), roles

    async def _on_change(event: Event) -> None:
        eid = event.data.get("entity_id")
        hit = tracked["roles"].get(eid)
        if hit is not None:
            device, role = hit
            await async_dial_changed(
                hass, entry, device, role, event.data.get("old_state"), event.data.get("new_state"),
            )
        else:
            await async_feed_dials(hass, entry)

    def _retrack() -> None:
        ids, roles = _watched()
        tracked["roles"] = roles
        if ids == tracked["ids"]:
            return
        if tracked["unsub"] is not None:
            tracked["unsub"]()
            tracked["unsub"] = None
        tracked["ids"] = ids
        if ids:
            tracked["unsub"] = async_track_state_change_event(hass, list(ids), _on_change)

    async def _tick(_now: Any) -> None:
        _retrack()  # a newly added Dial is watched within one tick
        await async_feed_dials(hass, entry)

    _retrack()
    entry.async_on_unload(async_track_time_interval(hass, _tick, TICK))

    @callback
    def _stop() -> None:
        if tracked["unsub"] is not None:
            tracked["unsub"]()

    entry.async_on_unload(_stop)
