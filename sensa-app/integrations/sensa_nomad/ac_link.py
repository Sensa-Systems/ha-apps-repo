"""Is an ESPHome-bridged air conditioner actually talking to its bridge?

The Dometic FreshJet reaches HA through a Sensa ESP32 bridge. The bridge can be
up, on Wi-Fi and still hold the saved pairing while the AC itself is silent (no
power, or a bond the AC dropped at a power cut). The climate entity then reads
a plain "off" and nothing looked wrong: on a customer van Climate Control said
"Cooling" for two days with the AC dead (2026-10-14).

`ac_link_down` is the ONE predicate for that. The alarm and Climate Control
both ask it, so they cannot disagree.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

_DEAD = (None, "", "unknown", "unavailable")

# Firmware 1.2.0+ reports the live BLE link; older firmware only the pairing.
_CONNECTED_SUFFIX = "air_conditioner_connected"
_PAIRING_SUFFIX = "air_conditioner_pairing"

# Down this long before it counts: a bridge reconnecting after a reboot, or an
# AC switched on a minute ago, must not alarm.
AC_LINK_DOWN_GRACE_SECONDS = 600
# `<prefix>_<climate entity>`. Not a routine, so not in ENTITY_ALARM_KEY_PREFIXES
# (that list must be routine ids); this module sweeps its own orphans.
ALARM_KEY_AC_LINK_DOWN = "ac_link_down"
_DOWN_SINCE_KEY = "sensa_nomad_ac_link_down_since"


def _matches(ent: Any, domain: str, suffix: str) -> bool:
    if not ent.entity_id.startswith(domain + "."):
        return False
    name = (getattr(ent, "original_name", None) or getattr(ent, "name", None) or "")
    return ent.entity_id.endswith(suffix) or name.lower().replace(" ", "_") == suffix


def _bridge_entities(hass: Any, climate_eid: str) -> tuple[str | None, str | None]:
    """(connected binary_sensor, pairing sensor) on the climate entity's device."""
    try:
        from homeassistant.helpers.entity_registry import async_get
    except ImportError:
        return None, None
    reg = async_get(hass)
    climate = reg.async_get(climate_eid) if hasattr(reg, "async_get") else None
    device_id = getattr(climate, "device_id", None)
    if not device_id:
        return None, None
    connected = pairing = None
    for ent in reg.entities.values():
        if getattr(ent, "device_id", None) != device_id:
            continue
        if _matches(ent, "binary_sensor", _CONNECTED_SUFFIX):
            connected = ent.entity_id
        elif _matches(ent, "sensor", _PAIRING_SUFFIX):
            pairing = ent.entity_id
    return connected, pairing


def ac_link_down(hass: Any, climate_eid: str | None) -> bool | None:
    """True when a paired FreshJet bridge is up but not talking to its AC.

    False when it is, None when we cannot tell (bridge unavailable, unpaired,
    or not an ESPHome-bridged AC at all). None is "could not ask", never "fine"
    and never "down".
    """
    if not climate_eid:
        return None
    climate = hass.states.get(climate_eid)
    if climate is None or climate.state in _DEAD:
        return None
    connected_eid, pairing_eid = _bridge_entities(hass, climate_eid)
    if pairing_eid is None:
        return None  # not a FreshJet bridge
    pairing = hass.states.get(pairing_eid)
    if pairing is None or ":" not in str(pairing.state):
        return None  # unpaired or pairing: not a link fault
    if connected_eid is not None:
        st = hass.states.get(connected_eid)
        if st is None or st.state not in ("on", "off"):
            return None
        return st.state == "off"
    # Firmware before 1.2.0: a connected FreshJet always reports the cabin
    # temperature, so none at all means the bridge has nothing from the AC.
    return (climate.attributes or {}).get("current_temperature") is None


def ac_link_up_template(hass: Any, entity_id: str) -> str:
    """Jinja that reads true exactly when `ac_link_down` is False.

    A routine's "Wait until connected" step runs inside HA's script engine,
    which cannot call `ac_link_down`, so the same test is written here as a
    template, next to it. Change one, change the other; `test_ac_link` renders
    both over the same states. The bridge's entities are resolved now, when
    the routine is built.

    Any other entity (no pairing sensor beside it), or no `hass` to look one
    up in, is "connected" once HA has a real state for it.
    """
    e = json.dumps(entity_id)
    alive = f"states({e}) not in ['unknown', 'unavailable', '']"
    connected_eid, pairing_eid = (
        _bridge_entities(hass, entity_id) if hass is not None else (None, None)
    )
    if pairing_eid is None:
        return "{{ " + alive + " }}"
    paired = f"':' in states({json.dumps(pairing_eid)})"
    if connected_eid is not None:
        link = f"is_state({json.dumps(connected_eid)}, 'on')"
    else:
        link = f"state_attr({e}, 'current_temperature') is not none"
    return "{{ " + f"{alive} and {paired} and {link}" + " }}"


def _ac_climates(hass: Any) -> list[str]:
    try:
        from homeassistant.helpers.entity_registry import async_get
    except ImportError:
        return []
    reg = async_get(hass)
    return [
        ent.entity_id for ent in reg.entities.values()
        if ent.entity_id.startswith("climate.") and getattr(ent, "platform", None) == "esphome"
    ]


def evaluate_ac_link_health(hass: Any, *, now: float | None = None) -> None:
    """Raise or clear one "air conditioner not connected" alarm per AC.

    Called from the alarm tick, so it re-asserts on a timer. The producer
    clears: nothing else knows the link came back.
    """
    from ._alarms import cancel_alarm, get_alarms, raise_alarm
    from .const import SENSA_LOG_SOURCE_SYSTEM
    from .vendors.zigbee2mqtt import _ha_fully_started

    stamp = time.monotonic() if now is None else now
    since: dict[str, float] = hass.data.setdefault(_DOWN_SINCE_KEY, {})
    started = _ha_fully_started(hass)
    climates = _ac_climates(hass)
    if started:
        # The bridge was removed: nothing below will ever look at its key again.
        head = ALARM_KEY_AC_LINK_DOWN + "_"
        for alarm in get_alarms(hass):
            key = str(alarm.get("key") or "")
            if key.startswith(head) and key[len(head):] not in climates:
                cancel_alarm(hass, key)
    for eid in climates:
        down = ac_link_down(hass, eid) if started else None
        if down is None:
            # Could not ask: keep the previous verdict, do not restart the clock.
            continue
        key = f"{ALARM_KEY_AC_LINK_DOWN}_{eid}"
        if not down:
            # Unconditional: the clock is in memory but the alarm is stored, so
            # after a restart only this cancel can clear one raised before it.
            since.pop(eid, None)
            cancel_alarm(hass, key)
            continue
        first = since.setdefault(eid, stamp)
        if stamp - first < AC_LINK_DOWN_GRACE_SECONDS:
            continue
        st = hass.states.get(eid)
        name = _ac_name(st)
        raise_alarm(
            hass,
            key,
            f"{name} is not talking to its Sensa bridge, so it cannot be switched on or off.",
            title="Air conditioner not connected",
            guidance=(
                "Check the air conditioner has power. After a power cut it may need "
                "pairing again: hold + and - on its panel until it shows BL, then "
                "press Pair in Settings, Devices."
            ),
            severity="warning",
            log_source=SENSA_LOG_SOURCE_SYSTEM,
        )


def _ac_name(st: Any) -> str:
    name = str(((st.attributes or {}).get("friendly_name") if st else "") or "The air conditioner")
    # The bridge's entity is "<bridge> Air Conditioning"; the customer knows the AC.
    for noise in (" Bridge Air Conditioning", " Air Conditioning"):
        name = name.removesuffix(noise)
    return name.replace("Sensa ", "", 1) if name.startswith("Sensa ") else name
