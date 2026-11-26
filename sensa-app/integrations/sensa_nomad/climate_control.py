"""Sensa Climate Control: hold the van at one target temperature.

The customer sets a target on the Climate tab. Behind it, on Settings ->
Vehicle, they nominate the primary heater, the primary air conditioner and the
temperature sensor to judge by (optional: without one the unit in use reads
the van, `reading_entity`). This module runs whichever unit the reading
calls for, never both at once:

* heating starts a degree below the target (a diesel heater: the customer's
  "Diesel heater restarts" difference, default 2 degrees, because every
  glow-plug start draws a lot of power) and stops half a degree above it;
* cooling starts a degree above the target and stops half a degree below it.

So after the heater stops there is still half a degree before the air
conditioner may start, and the two never pass the van back and forth. A diesel
heater, once lit, runs at least MIN_HEAT_RUN: stopping one cold leaves carbon
behind.

On shore power, or with the leisure batteries at or above the customer's
"Air con heats above" percentage (default 80, 0 = shore only), an air conditioner that can heat
(its `hvac_modes` list `heat`) does the heating and the diesel heater stays
off; otherwise, or with an air conditioner that only cools, the heater heats.
On batteries it also needs the nominated outside temperature sensor to read
at least AC_BATTERY_MIN_OUTSIDE_C (an air conditioner heats poorly in the
cold, so it would only drain the batteries); with no outside sensor chosen
the battery alone decides, and a chosen sensor that has never read means the
heater heats. Battery heating holds until the charge falls
SOC_BAND under the setting, and the outside reading OUTSIDE_BAND_C under, so
it does not flip at the line; the charge is the Home battery dial's reading
(`dashboard.resolve_role("battery_soc")`), and a missing one carries the last
answer forward. A heater already lit
finishes its minimum run before the air conditioner takes over. Shore power is
`instincts_catalog.shore_power_connected`, the shore-drop instinct's own
signal; a missing reading carries the last answer forward, and with no answer
ever the heater heats.

A unit whose setpoint covers the target (the Autoterm Air's target number, any
air conditioner) is handed the target too, so it modulates itself between our
on and off. A liquid heater's setpoint is its coolant, so it is only switched.

While Climate Control is on it owns both units: every minute, and at once on a
sensor change, the running one is held on and the other off. Switching Climate
Control off stops whichever one it was running and touches nothing else.

A sensor with no reading is "could not ask": the units are left as they are and
the drop-out is logged once on the edge.

An air conditioner that can heat may also be picked in a heater slot (Heater,
Secondary, Third). There it heats whenever its slot calls for it, with none of
the shore, battery or outside checks, and starts at START_BAND_C under like any
air conditioner (no glow plug). Picked as the Air conditioner too, that role
wins and the heater slot is ignored, so one unit is never driven two ways.

A secondary heater, then a third, join when one heater is not enough: at once when the
van is far enough under the target (FAR_BELOW_C for the first helper, twice
that for the second), or when the unit before it has heated for BOOST_AFTER
and the van is still START_BAND_C under. It stops within
BOOST_STOP_C of the target (after its own minimum run), leaving the first unit
to finish. On shore power the chain is the air conditioner, then the primary
heater, then the secondary, then the third, each joining by that same rule;
off shore it is the primary heater, then the secondary, then the third.

When the air conditioner may heat again mid warm-up (shore power arrives, or
the batteries and the outside allow it), it takes the heating over at once.
The heaters already running are then kept only as far as the gap justifies by
that same far-under rule (none, one, or both); the rest are let go as soon as
each has done its minimum run.

Switching Climate Control on decides from the temperature, not from what is
already running: a nominated unit already doing the job the reading calls for
is kept (a kept diesel heater's minimum run counts from switch-on, since Sensa
cannot tell when it was lit), one that is not is switched off. Any other
heater that regulates on the same temperature sensor is switched off once, so
two heaters do not chase one reading; after that it is the customer's.
Air conditioners regulate on their own built-in sensor, so a non-nominated one
is never touched.

What it does is written to the Intelligence log (the vehicle log), one line
each time it starts or stops a unit, swaps the heating unit, loses or regains
the sensor, or is switched on or off; never once a minute. A restart writes
nothing for the state it finds. While the miner autopilot holds the
heater off ("Miners replace the heater"), heating is left to the miners.

Pro and above (`entitled`). An unknown tier fails open.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback

from .ac_link import ac_link_down
from .const import (
    CLIMATE_CONTROL_NONE,
    CLIMATE_CONTROL_OPTION,
    CLIMATE_CONTROL_TARGET_DEFAULT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SIGNAL_CLIMATE_CONTROL = f"{DOMAIN}_climate_control_changed"

TICK = timedelta(seconds=60)
START_BAND_C = 1.0
STOP_PAST_C = 0.5
MIN_HEAT_RUN = timedelta(minutes=10)
# A second (or third) heating unit joins at once this far under the target...
FAR_BELOW_C = 3.0
# ...or when the unit before it has heated this long and the van is still
# START_BAND_C under, and leaves again within BOOST_STOP_C of the target.
BOOST_AFTER = timedelta(minutes=30)
BOOST_STOP_C = 0.5
# Battery heating, once started, holds until the charge is this far under the
# customer's setting (the miner autopilot's margin).
SOC_BAND = 5.0
# On batteries, an air conditioner heats only this warm outside, and once it
# does it holds down to OUTSIDE_BAND_C colder.
AC_BATTERY_MIN_OUTSIDE_C = 10.0
OUTSIDE_BAND_C = 3.0  # wide: each hand-back relights a diesel heater

# The Baltica (an Autoterm Flow 5D with radiators / underfloor) makes the hot
# water and heats the room. Out of SensaClimate 2026-10-17 (two thermostats on
# one room, and every way of stopping it was wrong), back 2026-11-19 (Richard)
# on Baltica firmware 1.1.25, whose "SensaClimate Heat" switch is the room
# thermostat while SensaClimate runs the van (its own Heating Temperature
# waits). So SensaClimate heats the room with that switch and stops with it:
# the heating circuit goes off and the water stays hot, never power off,
# unless the customer chose "Switch off" (`baltica_warm`). Switched off,
# SensaClimate hands the room back as `baltica_off` says. A Baltica on older
# firmware has no switch and is listed but never driven (`heater_entities`).
BALTICA_CATALOG_ID = "sensa_autoterm_abal"
# `baltica_warm`: what a Baltica does when SensaClimate stops heating with it
# (the room is warm, the diesel is low, the miners have the heat).
BALTICA_WARM_HOT_WATER = "hot_water"  # heating circuit off, water stays hot
BALTICA_WARM_OFF = "off"  # the Baltica switched off: no hot water either
BALTICA_WARM_LABELS = {BALTICA_WARM_HOT_WATER: "Hot water only", BALTICA_WARM_OFF: "Switch off"}
# `baltica_off`: what a Baltica is left doing when SensaClimate is turned off.
BALTICA_OFF_HOT_WATER = "hot_water"  # the Baltica set to Hot water
BALTICA_OFF_KEEP = "keep_heating"  # Hot water and heating, at SensaClimate's temperature
BALTICA_OFF_LABELS = {
    BALTICA_OFF_HOT_WATER: "Hot water only",
    BALTICA_OFF_KEEP: "Keep heating at this temperature",
}
# The firmware's `Baltica Mode` text for hot water only.
BALTICA_MODE_HOT_WATER = "Hot water"

HEAT = "heat"
COOL = "cool"
IDLE = "idle"

# Status sensor states.
STATE_OFF = "off"
STATE_HEATING = "heating"
STATE_COOLING = "cooling"
STATE_IDLE = "idle"
STATE_WAITING = "waiting"

_DEAD = (None, "", "unknown", "unavailable")

# entry_id -> the phase Climate Control is running. Absent until the first
# tick, which reads it back off the units so a restart does not stop them.
_phase: dict[str, str] = {}
# entry_id -> when the heater was lit, for MIN_HEAT_RUN.
_heat_since: dict[str, datetime] = {}
# entry_id -> which unit is heating: "heater" or "ac".
_heat_by: dict[str, str] = {}
# entry_id -> when that unit took the heating on, for BOOST_AFTER.
_base_since: dict[str, datetime] = {}
# The extra heaters, in the order they join after the primary.
EXTRA_HEATERS = ("secondary", "third")
# entry_id -> the units helping it ("heater" / "secondary" / "third") and when each
# joined, in the order they joined.
_boost: dict[str, dict[str, datetime]] = {}
# entry_id -> helpers the gap no longer justifies, stopped once their minimum
# run is done (set when the air conditioner takes the heating over).
_release: dict[str, set[str]] = {}
# entry_id -> the last definite shore-power answer, carried through a dropout.
_shore: dict[str, bool] = {}
# entry_id -> whether the batteries allow air-con heating, carried through a
# dropout and held across SOC_BAND.
_soc_ok: dict[str, bool] = {}
# entry_id -> whether it is warm enough outside for air-con heating on
# batteries, carried through a dropout and held across OUTSIDE_BAND_C.
_outside_ok: dict[str, bool] = {}
# entry_id -> whether the last tick had a reading, for the edge-only log.
_reading_ok: dict[str, bool] = {}
# entry_id -> what the status sensor shows.
_status: dict[str, dict[str, Any]] = {}
# entry_id -> the nominated air conditioner's lowest setpoint (degC), carried
# while it is unavailable.
_ac_min: dict[str, float] = {}
# entry_id -> ("heat" / "cool", since when, the reading then, the reading when
# it began) while the van is past the point where that should have started,
# for the cannot-reach alarm.
_stuck: dict[str, tuple[str, datetime, float, float]] = {}

# The cannot-reach alarm (Richard, 2026-10-06: "if the sensaclimate is unable
# to achieve the target temp then it should raise an alarm so the customer is
# notified on mobile"). Raised when the van, at or past the point where heating
# (or cooling) starts, has not moved PROGRESS_C towards the target in the
# customer's `alarm_minutes` (progress, not a fixed time, so a cold morning's
# slow warm-up never alarms), or at once when it has drifted `alarm_drift`
# further away than when it began. Cleared within START_BAND_C.
PROGRESS_C = 0.5
TARGET_ALARM_KEY = "climate_control_cannot_reach"

# A heater's diesel tank (Richard, 2026-10-06): below `low_fuel` % the heater is
# switched off and passed over, and a low-fuel alarm raised for it; it is used
# again once the tank is LOW_FUEL_BAND above (refilled, not a slosh).
LOW_FUEL_BAND = 5.0
HEATER_SLOTS = ("heater", *EXTRA_HEATERS)
# (entry_id, slot) -> whether that heater's tank is low, carried through a
# lost reading and held across LOW_FUEL_BAND.
_fuel_low: dict[tuple[str, str], bool] = {}


# (entry_id, alarm key) raised this episode. An alarm is raised once, on the
# edge, so the customer's Ignore holds until the condition clears and returns.
_raised: set[tuple[str, str]] = set()


def fuel_alarm_key(slot: str) -> str:
    return f"climate_control_low_fuel_{slot}"


def alarm_keys() -> list[str]:
    """Every alarm Climate Control raises: cannot reach the target, and a
    heater slot low on diesel. The History popup's Off target tab marks each."""
    return [TARGET_ALARM_KEY, *(fuel_alarm_key(slot) for slot in HEATER_SLOTS)]


def _raise_once(hass: HomeAssistant, entry: ConfigEntry, key: str, message: str, **kw: Any) -> None:
    if (entry.entry_id, key) in _raised:
        return
    from . import _alarms

    _raised.add((entry.entry_id, key))
    _alarms.raise_alarm(hass, key, message, **kw)


def _clear_alarm(hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
    """Cancel one of Climate Control's alarms, if it is raised. Its producer
    clears it, or `tick` would re-sound it for ever."""
    from . import _alarms

    _raised.discard((entry.entry_id, key))
    if any(a.get("key") == key for a in _alarms.get_alarms(hass)):
        _alarms.cancel_alarm(hass, key)


def fuel_level(hass: HomeAssistant, entry: ConfigEntry, slot: str) -> float | None:
    """The % in a heater slot's chosen diesel tank, or None."""
    st = _state(hass, nominated(entry, f"{slot}_tank"))
    try:
        return float(st.state)
    except (AttributeError, TypeError, ValueError):
        return None


def _watch_fuel(hass: HomeAssistant, entry: ConfigEntry, slot: str) -> bool:
    """Whether a heater slot's diesel tank is low, raising or clearing its
    low-fuel alarm. A lost reading carries the last answer; refilling must
    clear LOW_FUEL_BAND above the level, so a slosh does not flip it."""
    key = (entry.entry_id, slot)
    if (
        not nominated(entry, f"{slot}_tank") or not nominated(entry, slot)
        # An air conditioner in a heater slot burns no diesel: a tank left
        # picked from an earlier heater never stops it.
        or is_ac_device(_device(hass, entry, nominated(entry, slot)))
    ):
        _fuel_low.pop(key, None)
        _clear_alarm(hass, entry, fuel_alarm_key(slot))
        return False
    level = fuel_level(hass, entry, slot)
    floor = float(setting(entry, "low_fuel"))
    low = _fuel_low.get(key, False)
    if level is not None:
        if level < floor:
            low = True
        elif level >= floor + LOW_FUEL_BAND:
            low = False
    _fuel_low[key] = low
    if not low:
        _clear_alarm(hass, entry, fuel_alarm_key(slot))
        return False
    name = unit_name(hass, entry, slot) or "The heater"
    _raise_once(
        hass, entry, fuel_alarm_key(slot),
        f"{name}'s diesel tank is {f'at {level:g}%' if level is not None else 'low'}. "
        "Climate Control has switched it off and is heating with the other units.",
        title=f"{name} is low on diesel",
        guidance="Fill the diesel tank. The heater is used again once it is refilled.",
        context=[{"label": "Diesel", "entity": nominated(entry, f"{slot}_tank"), "icon": "mdi:gas-station"}],
        severity="warning",
    )
    return True
# entry_id -> (state, unit, reason) last written to the log; absent until the
# first publish, which is recorded silently unless Climate Control was just
# switched on.
_logged: dict[str, tuple[Any, ...]] = {}
_announce: set[str] = set()
# entry_ids just switched on: the next drive decides from the temperature
# instead of adopting whatever is running.
_fresh: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- settings ---------------------------------------------------------------


def setting(entry: ConfigEntry, key: str) -> Any:
    """One stored setting, or its default when never set."""
    stored = entry.options.get(CLIMATE_CONTROL_OPTION) or {}
    if key in stored:
        return stored[key]
    if key == "enabled":
        return False
    if key == "target":
        return CLIMATE_CONTROL_TARGET_DEFAULT
    if key == "heater_restart":
        from .const import CLIMATE_CONTROL_HEATER_RESTART_DEFAULT

        return CLIMATE_CONTROL_HEATER_RESTART_DEFAULT
    if key == "ac_heat_soc":
        from .const import CLIMATE_CONTROL_AC_HEAT_SOC_DEFAULT

        return CLIMATE_CONTROL_AC_HEAT_SOC_DEFAULT
    if key == "alarm_minutes":
        from .const import CLIMATE_CONTROL_ALARM_MINUTES_DEFAULT

        return CLIMATE_CONTROL_ALARM_MINUTES_DEFAULT
    if key == "alarm_drift":
        from .const import CLIMATE_CONTROL_ALARM_DRIFT_DEFAULT

        return CLIMATE_CONTROL_ALARM_DRIFT_DEFAULT
    if key == "low_fuel":
        from .const import CLIMATE_CONTROL_LOW_FUEL_DEFAULT

        return CLIMATE_CONTROL_LOW_FUEL_DEFAULT
    if key == "baltica_warm":
        return BALTICA_WARM_HOT_WATER
    if key == "baltica_off":
        return BALTICA_OFF_HOT_WATER
    return CLIMATE_CONTROL_NONE


def nominated(entry: ConfigEntry, key: str) -> str | None:
    """The nominated heater / ac device_id or sensor entity_id, or None."""
    value = setting(entry, key)
    return value if value and value != CLIMATE_CONTROL_NONE else None


def set_setting(hass: HomeAssistant, entry: ConfigEntry, key: str, value: Any) -> None:
    """Store one setting on the entry (options-as-truth)."""
    stored = dict(entry.options.get(CLIMATE_CONTROL_OPTION) or {})
    stored[key] = value
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CLIMATE_CONTROL_OPTION: stored},
    )


def entitled(hass: HomeAssistant) -> bool:
    """Pro and above. A tier not known yet is "could not ask" and fails open."""
    from .const import TIER_BASE
    from .layout_catalog import licence_tier_or_none

    return licence_tier_or_none(hass) != TIER_BASE


def running(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """SensaClimate is on: switched on and entitled. The one test, so the
    drive and the heaters' Frost Protection cannot disagree about it."""
    return bool(setting(entry, "enabled")) and entitled(hass)


# Why Climate Control cannot run yet: the waiting line and the refused switch.
NOT_CONFIGURED = "Choose a heater and an air conditioner"


def configured(entry: ConfigEntry) -> bool:
    """A heater AND an air conditioner nominated: SensaClimate needs both
    (Richard, 2026-11-21). The sensor is optional; without one the unit in
    use reads the van (`reading_entity`)."""
    return bool(nominated(entry, "heater") and nominated(entry, "ac"))


def _heater_reading(hass: HomeAssistant, entry: ConfigEntry, slot: str) -> str | None:
    """The room reading a heater slot's unit offers: the reading its own card
    shows as "Now". A liquid heater's own reading is its coolant, so only its
    panel's counts; an air conditioner in a heater slot reads its climate."""
    from .dashboard import _device_service_class
    from .heater_temp_source import choice_for, firmware_entities, regulating_temp_entity

    device_id = nominated(entry, slot)
    device = _device(hass, entry, device_id)
    if device is None:
        return None
    if is_ac_device(device):
        return ac_entity(hass, device)
    roles = firmware_entities(hass, device)
    if _device_service_class(device) == "hydronic_heater":
        return roles.get("panel_temperature")
    return regulating_temp_entity(roles, choice_for(entry, device_id or ""))


def reading_entity(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The entity Climate Control judges the van by: the chosen sensor, else
    the unit in use (the air conditioner while it cools or heats, else the
    first heater with a room reading, else the air conditioner). The one
    place this is decided; a `climate.*` answer reads its current_temperature."""
    sensor = nominated(entry, "sensor")
    if sensor:
        return sensor
    key = entry.entry_id
    ac = ac_entity(hass, _device(hass, entry, nominated(entry, "ac")))
    phase = _phase.get(key)
    if ac and (phase == COOL or (phase == HEAT and _heat_by.get(key) == "ac")):
        return ac
    for slot in HEATER_SLOTS:
        reading = _heater_reading(hass, entry, slot)
        if reading:
            return reading
    return ac


def reading_celsius(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """`reading_entity`'s reading in degC, or None with no usable reading. A
    climate entity's current_temperature is in HA's own unit."""
    from .crypto_autopilot import temperature_celsius

    st = hass.states.get(entity_id) if entity_id else None
    if st is None:
        return None
    if not str(entity_id).startswith("climate."):
        return temperature_celsius(st)
    try:
        value = float(st.attributes.get("current_temperature"))
    except (TypeError, ValueError):
        return None
    units = getattr(getattr(hass, "config", None), "units", None)
    return _from_unit(value, str(getattr(units, "temperature_unit", "") or ""))


# The diagram boxes on Habitation -> SensaClimate, in the order rules_text reads them.
RULE_BOXES = ("control", "sensor", "outside", "heater", "secondary", "third", "ac", "alarm")


def rules_by_box() -> dict[str, list[str]]:
    """Every rule the controller applies, as the paragraphs behind each box's
    (i) on the Habitation -> SensaClimate diagram. Built from the constants it runs
    on, so the words cannot drift from the behaviour. A rule added to the
    controller is added here too."""
    from .const import (
        CLIMATE_CONTROL_AC_HEAT_SOC_DEFAULT,
        CLIMATE_CONTROL_ALARM_DRIFT_DEFAULT,
        CLIMATE_CONTROL_ALARM_MINUTES_DEFAULT,
        CLIMATE_CONTROL_HEATER_RESTART_DEFAULT,
        CLIMATE_CONTROL_LOW_FUEL_DEFAULT,
        CLIMATE_CONTROL_TARGET_RANGE,
    )

    start, stop = f"{START_BAND_C:g}°C", f"{STOP_PAST_C:g}°C"
    minutes = int(MIN_HEAT_RUN.total_seconds() // 60)
    far, far2, far3 = (f"{n * FAR_BELOW_C:g}°C" for n in (1, 2, 3))
    near = f"{BOOST_STOP_C:g}°C"
    after = int(BOOST_AFTER.total_seconds() // 60)
    restart = f"{CLIMATE_CONTROL_HEATER_RESTART_DEFAULT:g}°C"
    alarm_minutes = f"{CLIMATE_CONTROL_ALARM_MINUTES_DEFAULT:g}"
    alarm_drift = f"{CLIMATE_CONTROL_ALARM_DRIFT_DEFAULT:g}°C"
    low_fuel = f"{CLIMATE_CONTROL_LOW_FUEL_DEFAULT:g}%"
    tank = (
        "Pick the diesel tank this heater draws from to see its level here. "
        f"Below the \"Low diesel\" level in the Alarm box ({low_fuel} unless "
        "you change it) Sensa switches this heater off and uses the next "
        "heater or the air conditioner instead, and raises a low-fuel alarm, "
        f"which reaches your phone. It is used again once the tank is "
        f"{LOW_FUEL_BAND:g}% above that level."
    )
    soc = f"{CLIMATE_CONTROL_AC_HEAT_SOC_DEFAULT:g}%"
    outside = f"{AC_BATTERY_MIN_OUTSIDE_C:g}°C"
    helpers = [
        (
            "If one unit is not enough, more join, in order: the secondary "
            "heater after the heater, then the third heater, or, when the air "
            "conditioner is heating, the heater, the secondary heater and then "
            "the third."
        ),
        (
            "Each joins at once when the van is far below your temperature "
            f"({far} for the first to join, {far2} for the second, {far3} for "
            f"the third), or when the unit before it has run {after} minutes "
            f"and the van is still {start} or more below. Each stops again "
            f"within {near} of your temperature, after its {minutes} minutes."
        ),
    ]
    heater = [
        (
            "An air conditioner that can heat can also be picked here (or as "
            "the secondary or third heater). It then heats whenever that "
            "slot calls for it, with no shore power, battery or outside "
            f"check, starting {start} below your temperature. If it is also "
            "picked as the Air conditioner, that wins and this pick is "
            "ignored."
        ),
        (
            "A diesel heater starts when the van is the \"Diesel heater "
            f"restarts\" difference below your temperature ({restart} "
            "unless you change it), because every start draws a lot of "
            f"power for its glow plug. Heating stops {stop} above your "
            "temperature."
        ),
        (
            f"Once lit, a diesel heater runs for at least {minutes} "
            "minutes before Sensa switches it off. It is given your "
            "temperature as its own target where it can take one; a heater "
            "whose target is its coolant is only switched on and off."
        ),
        (
            "A Baltica heats the room with its heating circuit, starting "
            f"{start} below your temperature, and its burner stays lit for "
            "the hot water. When the room is warm Sensa turns the heating "
            "circuit off and keeps the water hot, unless you chose \"Switch "
            "off\". While SensaClimate is on, the Baltica's own Heating "
            "Temperature waits. A Baltica that is off, or set to hot water "
            "only, is passed over for the next heater. A Baltica needs "
            "controller version 1.1.25."
        ),
        (
            "When you turn Climate Control on, any other heater set to the "
            "same temperature sensor is switched off once. A heater on a "
            "different sensor, such as the cab's, is never touched."
        ),
    ]
    return {
        "control": [
            (
                "Set a temperature on the Climate tab and Sensa holds it with "
                "the heaters and air conditioner chosen here, judged by the "
                "temperature sensor."
            ),
            (
                "The lowest temperature you can set is the lowest the chosen "
                "air conditioner allows. With no air conditioner it is "
                f"{CLIMATE_CONTROL_TARGET_RANGE[0]:g}°C."
            ),
            (
                "Too cold: heating starts below your temperature (how far "
                "depends on the unit: see the heater and air conditioner) and "
                f"stops {stop} above it. Too warm: cooling starts {start} "
                f"above and stops {stop} below. Heating and cooling never run "
                "together."
            ),
            (
                "While Climate Control is on it runs the units chosen here, so "
                "a change made on their own cards is undone within a minute. "
                "Turning it off stops every unit it was running."
            ),
            (
                "When you turn it on, a chosen unit that is already running is "
                "kept if the temperature needs it (a kept diesel heater counts "
                f"its {minutes} minutes from then) and switched off if not. "
                "After a restart it carries on with whatever is running."
            ),
            (
                "If the shore power, battery or outside reading stops, the "
                "last answer stands. If crypto miners are set to replace the "
                "heater, they heat the van and Sensa does not light a heater."
            ),
            "Every time it starts or stops a unit, it says so in the Intelligence log.",
        ],
        "sensor": [
            (
                "Climate Control judges the van by this one sensor: every "
                "start and stop is decided from its reading."
            ),
            "If it stops reporting, nothing is changed until it comes back.",
            (
                "With no sensor chosen, the unit in use reads the van: the air "
                "conditioner while it runs, else the heater's own reading."
            ),
        ],
        "outside": [
            (
                "On batteries, an air conditioner that can heat only does so "
                f"while this sensor reads at least {outside}, because an air "
                "conditioner heats poorly in the cold. Once heating it carries "
                f"on until it is {OUTSIDE_BAND_C:g}°C colder."
            ),
            (
                "With no outside sensor chosen, the battery alone decides. A "
                "sensor that has never given a reading means the heater heats "
                "on batteries. If the reading stops, the last answer stands. On "
                "shore power the outside temperature does not matter."
            ),
        ],
        "heater": [*heater, tank],
        "secondary": [*helpers, tank],
        "third": [*helpers, tank],
        "alarm": [
            (
                "Sensa raises an alarm, which reaches your phone, when it "
                "cannot get the van to your temperature: the heaters or air "
                "conditioner cannot keep up, or none of them can run."
            ),
            (
                "It watches only while the van is where heating (or cooling) "
                "should start. The alarm sounds when the van has not got "
                f"{PROGRESS_C:g}°C closer to your temperature in the minutes set "
                f"here ({alarm_minutes} unless you change it), so a slow warm-up "
                "on a cold morning never alarms, or at once when the van is "
                f"further from your temperature than when it began by the "
                f"degrees set here ({alarm_drift} unless you change it)."
            ),
            (
                f"It clears once the van is back within {start} of your "
                "temperature, or when Climate Control is turned off."
            ),
        ],
        "ac": [
            (
                "An air conditioner that can heat does the heating instead of "
                "the heater on shore power, or on batteries when the charge is "
                f"at least the \"Air con heats above\" percentage ({soc} unless "
                "you change it) and, when an outside temperature sensor is "
                f"chosen, it reads at least {outside}, because an air "
                "conditioner heats poorly in the cold."
            ),
            (
                f"On batteries it carries on until the charge falls "
                f"{SOC_BAND:g}% below that percentage or it is "
                f"{OUTSIDE_BAND_C:g}°C colder outside. With the percentage set "
                "to 0 it heats only on shore power. An "
                "air conditioner with no heat mode never heats."
            ),
            (
                f"It starts heating {start} below your temperature and cooling "
                f"{start} above it, and stops {stop} past it."
            ),
            (
                "If shore power arrives, or the batteries and outside allow "
                "it, while heaters are warming the van, the air conditioner "
                "takes over at once. A heater stays on beside it only if the "
                f"van is still {far} below ({far2} for two, {far3} for three); "
                "the others "
                "switch off once they have run their minimum. If the air "
                "conditioner stops being allowed, the heater takes over again."
            ),
            "An air conditioner you have not chosen is never touched.",
        ],
    }


def rules_text() -> str:
    """Every rule as one text, box by box (`rules_by_box`)."""
    rules = rules_by_box()
    seen: list[str] = []
    for box in RULE_BOXES:
        seen += [p for p in rules[box] if p not in seen]  # boxes can share rules
    return "\n\n".join(seen)


def status(entry: ConfigEntry) -> dict[str, Any]:
    """What the status sensor shows: `state` plus attributes."""
    return _status.get(entry.entry_id) or {"state": STATE_OFF}


# -- the decision -----------------------------------------------------------


def next_phase(
    phase: str,
    temp: float,
    target: float,
    *,
    can_heat: bool,
    can_cool: bool,
    heat_locked: bool = False,
    heat_start: float = START_BAND_C,
) -> str:
    """The phase to run next. Pure, so it is tested alone.

    `heat_locked` is a heater inside its minimum run: it keeps heating.
    `heat_start` is how far under the target heating starts: START_BAND_C for
    an air conditioner, the customer's restart difference for a diesel heater."""
    if phase == HEAT:
        if not can_heat:
            return IDLE
        if temp >= target + STOP_PAST_C and not heat_locked:
            return IDLE
        return HEAT
    if phase == COOL:
        if not can_cool:
            return IDLE
        if temp <= target - STOP_PAST_C:
            return IDLE
        return COOL
    if can_heat and temp <= target - heat_start:
        return HEAT
    if can_cool and temp >= target + START_BAND_C:
        return COOL
    return IDLE


# -- the units --------------------------------------------------------------


def _devices(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    return list(((getattr(coordinator, "data", None) or {}).get("devices")) or [])


def _device(hass: HomeAssistant, entry: ConfigEntry, device_id: str | None) -> dict[str, Any] | None:
    if not device_id:
        return None
    for device in _devices(hass, entry):
        if str(device.get("device_id") or device.get("id") or "") == device_id:
            return device
    return None


def heater_devices(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """Every heater on the van: what the Heater selects offer, and the boxes
    told it is running (a Baltica only while picked, `async_push_frost_standby`)."""
    from .const import HEATER_SERVICE_CLASSES
    from .dashboard import _device_service_class

    return [
        d for d in _devices(hass, entry)
        if d.get("device_id") and _device_service_class(d) in HEATER_SERVICE_CLASSES
    ]


def ac_devices(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """Every air conditioner on the van: what the Air Conditioner select offers."""
    from .const import AC_SERVICE_CLASSES
    from .dashboard import _device_service_class

    return [
        d for d in _devices(hass, entry)
        if d.get("device_id") and _device_service_class(d) in AC_SERVICE_CLASSES
    ]


def is_baltica(device: dict[str, Any] | None) -> bool:
    """An Autoterm Baltica controller, by catalog id or by model."""
    if not device:
        return False
    if device.get("catalog_id") == BALTICA_CATALOG_ID:
        return True
    model = str(device.get("vendor_model") or device.get("model") or "")
    if not model:
        return False
    try:
        from .sensor import _find_catalog_by_vendor_model

        return (_find_catalog_by_vendor_model(model) or {}).get("id") == BALTICA_CATALOG_ID
    except Exception:  # noqa: BLE001 - never fatal: not a Baltica
        return False


def is_ac_device(device: dict[str, Any] | None) -> bool:
    """An air conditioner, by service class."""
    if not device:
        return False
    from .const import AC_SERVICE_CLASSES
    from .dashboard import _device_service_class

    return _device_service_class(device) in AC_SERVICE_CLASSES


def _slot_units(hass: HomeAssistant, entry: ConfigEntry, device_id: str | None) -> tuple[str | None, str | None]:
    """(power, target) for a heater slot's unit. An air conditioner that is
    also the Air conditioner pick is ignored here: its own rules drive it."""
    if not device_id or device_id == nominated(entry, "ac"):
        return None, None
    return heater_entities(hass, _device(hass, entry, device_id))


def heater_entities(hass: HomeAssistant, device: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(power switch, target number) for a heater, either None when absent.
    A Baltica's "power" is its SensaClimate Heat switch (firmware 1.1.25) and
    it takes no target: SensaClimate is its room thermostat. Older Baltica
    firmware has no such switch, so neither, and it is never driven."""
    if device is None:
        return None, None
    if is_ac_device(device):
        # An air conditioner in a heater slot: its climate entity is switched
        # to heat and off, and handed the target (`_heater_on`).
        return ac_entity(hass, device), None
    from .heater_temp_source import firmware_entities

    role_map = firmware_entities(hass, device)
    if is_baltica(device):
        heat = role_map.get("sensaclimate_heat")
        if heat:
            _baltica_roles[heat] = role_map
        return heat, None
    return role_map.get("power"), role_map.get("target_temperature")


# A Baltica's SensaClimate Heat switch -> its role map, filled whenever its
# units are resolved (always before they are driven), so switching it on or
# off can reach its power switch and mode buttons (`baltica_warm`).
_baltica_roles: dict[str, dict[str, str]] = {}


def baltica_needs_update(hass: HomeAssistant, device: dict[str, Any] | None) -> bool:
    """A Baltica whose controller has no SensaClimate Heat switch yet: listed
    with that said, never driven."""
    if not is_baltica(device):
        return False
    from .heater_temp_source import firmware_entities

    return not firmware_entities(hass, device).get("sensaclimate_heat")


def baltica_blocked(hass: HomeAssistant, entry: ConfigEntry, heat: str | None) -> str | None:
    """Why a picked Baltica cannot heat the room now, or None. Set to hot
    water only it never heats the room (the firmware refuses too). Switched
    off it is the customer's, unless SensaClimate is the one switching it off
    ("Switch off", `baltica_warm`). A mode not known yet does not block: not
    knowing is not "hot water only"."""
    roles = _baltica_roles.get(heat or "")
    if roles is None:
        return None
    mode = _state(hass, roles.get("baltica_mode"))
    if mode is not None and mode.state == BALTICA_MODE_HOT_WATER:
        return "The Baltica is set to hot water only"
    if setting(entry, "baltica_warm") != BALTICA_WARM_OFF:
        power = _state(hass, roles.get("power"))
        if power is not None and power.state == "off":
            return "The Baltica is off"
    return None


def forget_device(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> None:
    """A unit removed from the van leaves every slot it held. A re-add gets a
    new device_id, so the old choice could only ever read "No longer reporting"
    (.177, 2026-11-04). Removal only: a unit gone quiet keeps its slot."""
    for slot in (*HEATER_SLOTS, "ac"):
        if nominated(entry, slot) == device_id:
            set_setting(hass, entry, slot, CLIMATE_CONTROL_NONE)


def ac_entity(hass: HomeAssistant, device: dict[str, Any] | None) -> str | None:
    """The air conditioner's `climate.*` entity, or None."""
    if device is None:
        return None
    from . import _attach_air_conditioner_entities
    from .dashboard import _ac_climate_entity_id

    rows = _attach_air_conditioner_entities(hass, [device])
    return _ac_climate_entity_id(rows[0] if rows else device)


def _to_unit(celsius: float, unit: str) -> float:
    unit = (unit or "").strip().lower()
    if unit in ("°f", "f"):
        return celsius * 9 / 5 + 32
    if unit == "k":
        return celsius + 273.15
    return celsius


def _from_unit(value: float, unit: str) -> float:
    unit = (unit or "").strip().lower()
    if unit in ("°f", "f"):
        return (value - 32) * 5 / 9
    if unit == "k":
        return value - 273.15
    return value


def _state(hass: HomeAssistant, entity_id: str | None) -> Any:
    return hass.states.get(entity_id) if entity_id else None


async def _call(hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(domain, service, data, blocking=False)


def _is_on(st: Any) -> bool:
    """A heater slot's unit is running: a switch on, or an air conditioner in
    a heater slot heating."""
    if st is None:
        return False
    return st.state in ("on", "heat")


async def _number_to(hass: HomeAssistant, number: str | None, celsius: float | None) -> None:
    """Set a temperature number to `celsius` (None = its minimum), in the
    number's own unit, when it is in range and not already there."""
    num = _state(hass, number)
    if num is None or num.state in _DEAD:
        return
    attrs = num.attributes or {}
    unit = str(attrs.get("unit_of_measurement") or "")
    try:
        lo = _from_unit(float(attrs.get("min")), unit)
        hi = _from_unit(float(attrs.get("max")), unit)
        now = _from_unit(float(num.state), unit)
    except (TypeError, ValueError):
        return
    want = lo if celsius is None else celsius
    if not lo <= want <= hi or abs(now - want) < 0.05:
        return
    value = float(attrs.get("min")) if celsius is None else round(_to_unit(want, unit), 1)
    await _call(hass, "number", "set_value", {"entity_id": number, "value": value})


async def _heater_on(
    hass: HomeAssistant, power: str | None, number: str | None, target: float,
    entry: ConfigEntry | None = None,
) -> None:
    if power and power.startswith("climate."):
        await _ac_run(hass, power, "heat", target)  # an air conditioner in a heater slot
        return
    roles = _baltica_roles.get(power or "")
    if roles is not None and entry is not None and setting(entry, "baltica_warm") == BALTICA_WARM_OFF:
        # "Switch off": SensaClimate switched it off, so it switches it on.
        on = _state(hass, roles.get("power"))
        if on is not None and on.state == "off":
            await _call(hass, "switch", "turn_on", {"entity_id": roles["power"]})
    st = _state(hass, power)
    if st is None or st.state in _DEAD:
        return  # offline: nothing to drive
    if st.state != "on":
        await _call(hass, power.split(".", 1)[0], "turn_on", {"entity_id": power})
    # Hand the heater the target only when its setpoint covers it: a liquid
    # heater's setpoint is its coolant, and 21 there would be nonsense.
    await _number_to(hass, number, target)


async def _heater_off(hass: HomeAssistant, power: str | None, entry: ConfigEntry | None = None) -> None:
    """Stop a heater slot's unit. A Baltica's heating circuit goes off and its
    water stays hot; only "Switch off" (`baltica_warm`) switches it off."""
    if power and power.startswith("climate."):
        await _ac_off(hass, power)
        return
    st = _state(hass, power)
    if st is not None and st.state == "on":
        await _call(hass, power.split(".", 1)[0], "turn_off", {"entity_id": power})
    roles = _baltica_roles.get(power or "")
    if roles is not None and entry is not None and setting(entry, "baltica_warm") == BALTICA_WARM_OFF:
        on = _state(hass, roles.get("power"))
        if on is not None and on.state == "on":
            await _call(hass, "switch", "turn_off", {"entity_id": roles["power"]})


def _ac_heats(hass: HomeAssistant, climate: str | None) -> bool:
    """The air conditioner lists a heat mode."""
    st = _state(hass, climate)
    modes = (st.attributes or {}).get("hvac_modes") if st is not None else None
    return "heat" in [str(m) for m in (modes or [])]


async def _ac_run(hass: HomeAssistant, climate: str | None, mode: str, target: float) -> None:
    st = _state(hass, climate)
    if st is None or st.state in _DEAD:
        return
    if st.state != mode:
        await _call(hass, "climate", "set_hvac_mode", {"entity_id": climate, "hvac_mode": mode})
    # A climate entity speaks the box's unit system.
    want = round(_to_unit(target, hass.config.units.temperature_unit), 1)
    try:
        have = float(st.attributes.get("temperature"))
    except (TypeError, ValueError):
        have = None
    if have is None or abs(have - want) >= 0.05:
        await _call(hass, "climate", "set_temperature", {"entity_id": climate, "temperature": want})


async def _ac_off(hass: HomeAssistant, climate: str | None) -> None:
    st = _state(hass, climate)
    if st is not None and st.state not in _DEAD and st.state != "off":
        await _call(hass, "climate", "set_hvac_mode", {"entity_id": climate, "hvac_mode": "off"})


def _extra_units(hass: HomeAssistant, entry: ConfigEntry, key: str) -> tuple[str | None, str | None]:
    """(power, target number) of an extra heater ("secondary" / "third");
    none when it is not nominated or is a heater already chosen above it."""
    device_id = nominated(entry, key)
    above = [nominated(entry, "heater"), *(nominated(entry, k) for k in EXTRA_HEATERS[:EXTRA_HEATERS.index(key)])]
    if not device_id or device_id in above:
        return None, None
    return _slot_units(hass, entry, device_id)


def _units(hass: HomeAssistant, entry: ConfigEntry) -> tuple[str | None, str | None, str | None]:
    """(heater power, heater target number, ac climate) for the nominees."""
    power, number = _slot_units(hass, entry, nominated(entry, "heater"))
    climate = ac_entity(hass, _device(hass, entry, nominated(entry, "ac")))
    return power, number, climate


def unit_name(hass: HomeAssistant, entry: ConfigEntry, key: str) -> str:
    """The customer's name for the nominated heater ("heater"), an extra
    heater ("secondary" / "third") or air conditioner ("ac"), as the select
    shows it."""
    device = _device(hass, entry, nominated(entry, key))
    if device is None:
        return ""
    if key == "ac" or is_ac_device(device):
        from .dashboard import _ac_display_name

        return _ac_display_name(device)
    return str(device.get("display_name") or device.get("name") or "Heater")


# The thermostat box's History popup (2026-10-08): the temperature SensaClimate
# judges by as an area, coloured underneath by which unit was running. Slot
# order is the colour order, so a unit keeps its colour as others come and go.
HISTORY_SLOTS: tuple[tuple[str, str], ...] = (
    ("heater", "Heater"),
    ("secondary", "Secondary heater"),
    ("third", "Third heater"),
    ("ac", "Air conditioner"),
)


def _plain_reading(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """`reading_entity` when its history is a temperature (not a climate
    entity, whose state is its mode)."""
    eid = reading_entity(hass, entry)
    return None if not eid or eid.startswith("climate.") else eid


def history_spec(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """What the History popup plots, resolved here so the card never guesses
    an entity: the nominated temperature sensor, the target, and per
    nominated unit the entity that says whether it is running (a heater's
    run state, else its power; an air conditioner's climate entity).

    From each unit's OWN history, not SensaClimate's status, so a heater
    switched on by hand shows too, and asked at the moment the popup opens,
    because changing a nomination does not rebuild the dashboard."""
    from .const import CLIMATE_CONTROL_SWITCH_ENTITY_ID, CLIMATE_CONTROL_TARGET_ENTITY_ID
    from .heater_temp_source import firmware_entities

    units: list[dict[str, str]] = []
    ac_id = nominated(entry, "ac")
    for key, fallback in HISTORY_SLOTS:
        # An air conditioner picked in a heater slot AND as the air conditioner
        # is driven as the air conditioner (that role wins), so it is one lane.
        if key != "ac" and ac_id and nominated(entry, key) == ac_id:
            continue
        device = _device(hass, entry, nominated(entry, key))
        if device is None:
            continue
        if key == "ac" or is_ac_device(device):
            entity, kind = ac_entity(hass, device), "climate"
        else:
            roles = firmware_entities(hass, device)
            entity = roles.get("run_state") or roles.get("power")
            kind = "heater"
        if entity:
            units.append({
                "key": key,
                "label": unit_name(hass, entry, key) or fallback,
                "entity": entity,
                "kind": kind,
            })
    return {
        "temperature": nominated(entry, "sensor") or _plain_reading(hass, entry),
        "target": CLIMATE_CONTROL_TARGET_ENTITY_ID,
        # When SensaClimate was switched on: its own lane, first.
        "switch": CLIMATE_CONTROL_SWITCH_ENTITY_ID,
        "units": units,
        # The "Off target" tab marks each of these alarms (the log mirror
        # carries the key, `_alarms._mirror`).
        "alarm_keys": alarm_keys(),
    }


def _read_back_phase(
    hass: HomeAssistant, power: str | None, climate: str | None,
) -> tuple[str, str | None]:
    """(phase, heating unit) the units are in now, so a restart carries on
    rather than stopping a heater mid-run."""
    if _is_on(_state(hass, power)):
        return HEAT, "heater"
    st = _state(hass, climate)
    if st is not None and st.state == "heat":
        return HEAT, "ac"
    if st is not None and st.state == "cool":
        return COOL, None
    return IDLE, None


def battery_soc(hass: HomeAssistant) -> float | None:
    """The leisure battery charge in %, the Home dial's reading; None when
    there is no battery or no reading."""
    try:
        from .dashboard import resolve_role

        eid = resolve_role("battery_soc")
    except Exception:  # noqa: BLE001 - no battery resolved is "could not ask"
        return None
    st = _state(hass, eid if isinstance(eid, str) else None)
    try:
        return float(st.state)
    except (AttributeError, TypeError, ValueError):
        return None


def battery_allows_ac_heat(soc: float | None, threshold: float, was: bool | None) -> bool | None:
    """Whether the batteries allow air-con heating. Pure, so it is tested
    alone. 0 = off. None when the charge is unknown (the caller carries the
    last answer forward). Once allowed it holds down to SOC_BAND under."""
    if threshold <= 0:
        return False
    if soc is None:
        return None
    if was:
        return soc >= threshold - SOC_BAND
    return soc >= threshold


def _battery_ok(hass: HomeAssistant, entry: ConfigEntry, key: str) -> bool:
    threshold = float(setting(entry, "ac_heat_soc"))
    if threshold <= 0:
        _soc_ok.pop(key, None)
        return False
    answer = battery_allows_ac_heat(battery_soc(hass), threshold, _soc_ok.get(key))
    if answer is not None:
        _soc_ok[key] = answer
    return _soc_ok.get(key, False)


def outside_allows_ac_heat(outside: float | None, was: bool | None) -> bool | None:
    """Whether it is warm enough outside for air-con heating on batteries.
    Pure, so it is tested alone. None when there is no reading (the caller
    carries the last answer forward). Once allowed it holds OUTSIDE_BAND_C
    colder."""
    if outside is None:
        return None
    if was:
        return outside >= AC_BATTERY_MIN_OUTSIDE_C - OUTSIDE_BAND_C
    return outside >= AC_BATTERY_MIN_OUTSIDE_C


def _warm_outside(hass: HomeAssistant, entry: ConfigEntry, key: str) -> bool:
    from .crypto_autopilot import temperature_celsius

    sensor = nominated(entry, "outside")
    if not sensor:
        _outside_ok.pop(key, None)
        return True  # no outside sensor chosen: the battery alone decides
    answer = outside_allows_ac_heat(
        temperature_celsius(hass.states.get(sensor)), _outside_ok.get(key),
    )
    if answer is not None:
        _outside_ok[key] = answer
    return _outside_ok.get(key, False)


def _conditions(
    hass: HomeAssistant, entry: ConfigEntry, key: str, *, evaluated: bool = False,
) -> dict[str, Any]:
    """What the SensaClimate diagram shows beside the air conditioner: shore
    power, the battery and the outside reading, each with whether it allows
    air-con heating. Published whatever Climate Control is doing, so the
    diagram is never blank because it is off. `evaluated` means this tick
    has already asked (the drive); otherwise ask now."""
    from .crypto_autopilot import temperature_celsius

    if not evaluated:
        _on_shore(hass, key)
        _battery_ok(hass, entry, key)
        _warm_outside(hass, entry, key)
    outside_sensor = nominated(entry, "outside")
    return {
        "shore": _shore.get(key),
        "battery_soc": battery_soc(hass),
        "outside_temperature": (
            temperature_celsius(hass.states.get(outside_sensor)) if outside_sensor else None
        ),
        "battery_ok": _soc_ok.get(key),
        "outside_ok": _outside_ok.get(key),
        # No sensor chosen: the diagram hides the Outside line.
        "outside_chosen": bool(outside_sensor),
    }


def _on_shore(hass: HomeAssistant, key: str) -> bool:
    from .instincts_catalog import shore_power_connected

    answer = shore_power_connected(hass)
    if answer is not None:
        _shore[key] = answer
    return _shore.get(key, False)


def target_min(hass: HomeAssistant, entry: ConfigEntry) -> float:
    """The lowest target the customer may set, in degC: the nominated air
    conditioner's own lowest setpoint (Richard, 2026-10-06), else the default.
    An unavailable air conditioner keeps its last known lowest."""
    from .const import CLIMATE_CONTROL_TARGET_RANGE

    lo, hi, _step = CLIMATE_CONTROL_TARGET_RANGE
    key = entry.entry_id
    climate = ac_entity(hass, _device(hass, entry, nominated(entry, "ac")))
    if not climate:
        _ac_min.pop(key, None)
        return lo
    st = _state(hass, climate)
    try:
        value = _from_unit(
            float((st.attributes or {}).get("min_temp")), hass.config.units.temperature_unit,
        )
    except (AttributeError, TypeError, ValueError):
        value = None
    if value is not None:
        _ac_min[key] = min(value, hi)
    return _ac_min.get(key, lo)


def _watch_target(
    hass: HomeAssistant, entry: ConfigEntry, temp: float, target: float,
    heat_start: float, now: datetime,
) -> None:
    """Raise the cannot-reach alarm once the van, at or past the point where
    heating (or cooling) starts, has not moved PROGRESS_C towards the target
    in `alarm_minutes` (a van that is getting there restarts the clock) or has
    drifted `alarm_drift` further away than when it began, and clear it
    once the van is back within START_BAND_C of the target. Its producer
    clears it, or `tick` would re-sound it for ever."""
    key = entry.entry_id
    if temp <= target - heat_start:
        way = HEAT
    elif temp >= target + START_BAND_C:
        way = COOL
    else:
        way = None
    if way is None:
        if abs(temp - target) <= START_BAND_C:
            _stuck.pop(key, None)
            _clear_target_alarm(hass, entry)
        return  # between the two lines: neither start nor clear
    was = _stuck.get(key)
    if was is None or was[0] != way:
        _stuck[key] = (way, now, temp, temp)
        return
    began = was[3]
    worse = began - temp if way == HEAT else temp - began
    minutes = float(setting(entry, "alarm_minutes"))
    if worse >= float(setting(entry, "alarm_drift")):
        why = f"and is {_fmt_diff(worse)} further from it than when Climate Control began"
    elif now - was[1] < timedelta(minutes=minutes):
        return
    else:
        moved = temp - was[2] if way == HEAT else was[2] - temp
        if moved >= PROGRESS_C:
            _stuck[key] = (way, now, temp, began)  # getting there: watch the next stretch
            return
        why = f"and has not got closer in {minutes:g} minutes"
    from .const import CLIMATE_CONTROL_TARGET_ENTITY_ID

    cold = way == HEAT
    _raise_once(
        hass, entry, TARGET_ALARM_KEY,
        f"The van is {_fmt(hass, temp)}, {'below' if cold else 'above'} the "
        f"{_fmt(hass, target)} you set, {why}.",
        title=f"Climate Control cannot {'heat' if cold else 'cool'} the van",
        guidance=(
            "Check the heaters have fuel and power, and that the air "
            "conditioner can run." if cold else
            "Check the air conditioner is running and has power."
        ),
        context=[
            {"label": "Inside", "entity": reading_entity(hass, entry), "icon": "mdi:thermometer"},
            {"label": "Target", "entity": CLIMATE_CONTROL_TARGET_ENTITY_ID, "icon": "mdi:thermostat"},
        ],
        severity="warning",
    )


def _fmt_diff(celsius: float) -> str:
    """A temperature DIFFERENCE in degC, which is not converted like a reading."""
    return f"{round(celsius, 1):g}°C"


def _clear_target_alarm(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cancel the cannot-reach alarm, if it is raised."""
    _stuck.pop(entry.entry_id, None)
    _clear_alarm(hass, entry, TARGET_ALARM_KEY)


def _clear_all_alarms(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Climate Control is off or has nothing to drive: none of its alarms
    stands, because nothing is producing them any more."""
    _clear_target_alarm(hass, entry)
    for slot in HEATER_SLOTS:
        _fuel_low.pop((entry.entry_id, slot), None)
        _clear_alarm(hass, entry, fuel_alarm_key(slot))


def _publish(hass: HomeAssistant, entry: ConfigEntry, state: str, **attrs: Any) -> None:
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    # `target_min` rides along so the target number re-reads its lowest when
    # the air conditioner (or its nomination) changes.
    new = {
        "state": state, "sensor_entity": reading_entity(hass, entry),
        "target_min": target_min(hass, entry), **attrs,
    }
    if _status.get(entry.entry_id) != new:
        _status[entry.entry_id] = new
        async_dispatcher_send(hass, SIGNAL_CLIMATE_CONTROL)
    _log_change(hass, entry, state, attrs)


def _fmt(hass: HomeAssistant, celsius: float) -> str:
    """A temperature in the box's unit, for a log line."""
    unit = hass.config.units.temperature_unit
    text = f"{_to_unit(celsius, unit):.1f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def _log_change(hass: HomeAssistant, entry: ConfigEntry, state: str, attrs: dict[str, Any]) -> None:
    """One Intelligence-log line when what Climate Control is doing changes."""
    key = entry.entry_id
    now = (state, attrs.get("unit"), attrs.get("reason"), attrs.get("ac_heat_because"))
    before = _logged.get(key)
    if before == now:
        return
    _logged[key] = now
    if before is None and key not in _announce:
        return  # a restart: the state it finds is not news
    _announce.discard(key)
    prev_state, prev_unit, prev_reason, _because = before or (STATE_OFF, None, None, None)
    target = _fmt(hass, float(setting(entry, "target")))
    temp = attrs.get("current_temperature")
    reading = _fmt(hass, float(temp)) if temp is not None else ""
    sensor = reading_entity(hass, entry)
    st = hass.states.get(sensor) if sensor else None
    sensor_name = str((st.attributes.get("friendly_name") if st else None) or "the temperature sensor")
    level, lines = "info", []
    no_reading = attrs.get("reason") == "No reading from the temperature sensor"
    if prev_reason == "No reading from the temperature sensor" and not no_reading:
        lines.append(f"Reading from {sensor_name} again.")
    if state == STATE_OFF:
        lines.append("Off.")
        if prev_state in (STATE_HEATING, STATE_COOLING) and prev_unit:
            lines.append(f"{prev_unit} switched off.")
    elif state == STATE_WAITING:
        if not no_reading:
            return  # nothing chosen yet: a setup state, not an event
        level = "warning"
        lines.append(
            f"No reading from {sensor_name}. The heater and air conditioner "
            "are left as they are until it comes back.",
        )
    elif state == STATE_HEATING:
        lines.append(f"{reading} is below {target}, heating with {attrs.get('unit') or 'the heater'}.")
        if attrs.get("ac_heat_because") == "battery":
            lines.append("The batteries are full enough, so the air conditioner heats.")
        elif attrs.get("heating_with") == "air_conditioner":
            lines.append("On shore power, so the air conditioner heats.")
    elif state == STATE_COOLING:
        lines.append(f"{reading} is above {target}, cooling with {attrs.get('unit') or 'the air conditioner'}.")
    elif prev_state in (STATE_HEATING, STATE_COOLING):
        lines.append(f"{reading} reached, {prev_unit or 'the unit'} off. Holding {target}.")
    else:
        lines.append(f"On. {reading}, holding {target}.")
    _log_line(hass, level, "Climate Control: " + " ".join(lines))


def _log_line(hass: HomeAssistant, level: str, message: str) -> None:
    """One line in the Intelligence (vehicle) log, never a popup."""
    from ._log import sensa_log
    from .const import SENSA_LOG_SOURCE_VEHICLE

    try:
        sensa_log(hass, level, message, source=SENSA_LOG_SOURCE_VEHICLE, notify=False)
    except Exception:  # the log must never stop the units being driven
        _LOGGER.debug("Climate Control log failed", exc_info=True)


# -- driving ----------------------------------------------------------------


async def async_drive(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring the two units into line with the target."""
    from .crypto_autopilot import heater_held_off

    key = entry.entry_id
    if not running(hass, entry):
        _phase.pop(key, None)
        _clear_all_alarms(hass, entry)  # nothing is holding the target now
        _publish(hass, entry, STATE_OFF, **_conditions(hass, entry, key))
        return
    if not configured(entry):
        # A unit un-picked mid-run: stop what SensaClimate had running
        # (a no-op once its phase is gone).
        await async_stop(hass, entry)
        _clear_all_alarms(hass, entry)
        _publish(
            hass, entry, STATE_WAITING, reason=NOT_CONFIGURED,
            **_conditions(hass, entry, key),
        )
        return

    power, number, climate = _units(hass, entry)
    extras = {k: _extra_units(hass, entry, k) for k in EXTRA_HEATERS}
    sensor = reading_entity(hass, entry)
    temp = reading_celsius(hass, sensor)
    ok = temp is not None
    if ok != _reading_ok.get(key, True):
        if ok:
            _LOGGER.info("Climate Control: reading from %s again", sensor)
        else:
            _LOGGER.warning(
                "Climate Control: no reading from %s; leaving the heater and "
                "air conditioner as they are", sensor,
            )
    _reading_ok[key] = ok
    if temp is None:
        _publish(hass, entry, STATE_WAITING, reason="No reading from the temperature sensor")
        return

    target = max(float(setting(entry, "target")), target_min(hass, entry))
    now = _now()
    fresh = key in _fresh
    _fresh.discard(key)
    phase = _phase.get(key)
    if phase is None:
        phase, by = _read_back_phase(hass, power, climate)
        if by:
            _heat_by[key] = by
            _base_since[key] = now
        if by == "heater":
            _heat_since[key] = now
        for k, (k_power, _n) in extras.items():
            if phase == HEAT and _is_on(_state(hass, k_power)):
                _boost.setdefault(key, {})[k] = now  # keep it on through a restart
    # While the miners are heating the hab, nothing else heats it.
    held = power is not None and heater_held_off(hass, power)
    heater_ok = power is not None and not held
    extra_ok = {
        k: p is not None and not held and not heater_held_off(hass, p)
        for k, (p, _n) in extras.items()
    }
    # A Baltica that is off, or set to hot water only, cannot heat the room
    # in whichever slot it fills: passed over, its room heat let go.
    if power is not None and baltica_blocked(hass, entry, power):
        heater_ok = False
        await _heater_off(hass, power)
    for k, (k_power, _n) in extras.items():
        if k_power is not None and baltica_blocked(hass, entry, k_power):
            extra_ok[k] = False
            await _heater_off(hass, k_power)
    # A heater whose diesel tank is low is switched off and passed over: the
    # next heater, or the air conditioner, takes its place.
    slot_power = {"heater": power, **{k: p for k, (p, _n) in extras.items()}}
    heater_slot = "heater"  # the slot doing the heater's job
    for slot in HEATER_SLOTS:
        if not _watch_fuel(hass, entry, slot):
            continue
        if slot == "heater":
            heater_ok = False
        else:
            extra_ok[slot] = False
        if slot_power[slot] is not None:
            await _heater_off(hass, slot_power[slot], entry)
    if power is not None and not heater_ok and _fuel_low.get((key, "heater")):
        # The heater is out of diesel: the next heater with fuel steps up
        # into its place, rather than waiting to be called as a helper.
        for k in EXTRA_HEATERS:
            if extra_ok[k]:
                heater_slot = k
                power, number = extras[k]
                heater_ok, extra_ok[k] = True, False
                (_boost.get(key) or {}).pop(k, None)
                break
    shore = _on_shore(hass, key)
    on_battery = _battery_ok(hass, entry, key) and _warm_outside(hass, entry, key)
    # An AC whose bridge has lost it obeys nothing: never claim it is running.
    ac_dead = ac_link_down(hass, climate) is True
    ac_heat_ok = not held and not ac_dead and _ac_heats(hass, climate) and (shore or on_battery)
    live = {**_conditions(hass, entry, key, evaluated=True), "ac_heat_allowed": ac_heat_ok}
    if fresh and phase != IDLE and next_phase(
        phase, temp, target, can_heat=heater_ok or ac_heat_ok,
        can_cool=climate is not None,
    ) != phase:
        # Just switched on, and what is running is not the job the reading
        # calls for: decide afresh rather than give it a minimum run.
        phase = IDLE
        _heat_by.pop(key, None)
        _boost.pop(key, None)
    if fresh and temp >= target - BOOST_STOP_C:
        _boost.pop(key, None)  # a helper already on is not needed: no minimum run
    boosts = _boost.setdefault(key, {})
    # A Baltica's room heat is its heating circuit, its burner stays lit for
    # the water: no glow-plug start, no carbon, so no minimum run either.
    baltica = (power or "") in _baltica_roles
    locked = phase == HEAT and (
        (_heat_by.get(key) == "heater" and not baltica and now - _heat_since.get(key, now) < MIN_HEAT_RUN)
        or any(now - since < MIN_HEAT_RUN for since in boosts.values())
    )
    # A diesel heater relights only at the customer's restart difference: each
    # glow-plug start is a big draw. An air conditioner heating has no such cost.
    no_glow_plug = ac_heat_ok or baltica or (power or "").startswith("climate.")
    heat_start = START_BAND_C if no_glow_plug else float(setting(entry, "heater_restart"))
    new = next_phase(
        phase, temp, target, can_heat=heater_ok or ac_heat_ok,
        can_cool=climate is not None, heat_locked=locked, heat_start=heat_start,
    )
    _watch_target(hass, entry, temp, target, heat_start, now)
    release = _release.setdefault(key, set())
    if new == HEAT:
        by = "heater" if heater_ok and not ac_heat_ok else "ac"
        handover = phase == HEAT and _heat_by.get(key) == "heater" and by == "ac"
        if phase != HEAT or _heat_by.get(key) != by:
            _base_since[key] = now
            if by == "heater":
                _heat_since[key] = boosts.get("heater", now)
        _heat_by[key] = by
        helpers = (["heater"] if by == "ac" and heater_ok else []) + [k for k in EXTRA_HEATERS if extra_ok[k]]
        if handover and heater_ok:
            # The heater was doing the heating: it becomes a helper with the
            # run it has already done, so its minimum still counts.
            boosts["heater"] = _heat_since.get(key, now)
            release.update(src for src in boosts if src in helpers)
        _boosts_update(boosts, helpers, _base_since.get(key), temp, target, now, release)
    else:
        _heat_by.pop(key, None)
        _base_since.pop(key, None)
        boosts.clear()
        release.clear()
    _phase[key] = new

    if new == HEAT:
        by = _heat_by[key]
        if by == "ac":
            await _ac_run(hass, climate, "heat", target)
        else:
            await _ac_off(hass, climate)
        if heater_ok:
            if by == "heater" or "heater" in boosts:
                await _heater_on(hass, power, number, target, entry)
            else:
                await _heater_off(hass, power, entry)
        for k, (k_power, k_number) in extras.items():
            if not extra_ok[k]:
                continue
            if k in boosts:
                await _heater_on(hass, k_power, k_number, target, entry)
            else:
                await _heater_off(hass, k_power, entry)
        names = [unit_name(hass, entry, "ac" if by == "ac" else heater_slot)]
        names += [unit_name(hass, entry, src) for src in boosts]
        _publish(
            hass, entry, STATE_HEATING, current_temperature=round(temp, 1),
            heating_with="air_conditioner" if by == "ac" else "heater",
            ac_heat_because=("shore" if shore else "battery") if by == "ac" else None,
            unit=" and ".join(n for n in names if n),
            running=["ac" if by == "ac" else heater_slot, *boosts], **live,
        )
    elif new == COOL:
        if heater_ok:
            await _heater_off(hass, power, entry)
        for k, (k_power, _n) in extras.items():
            if extra_ok[k]:
                await _heater_off(hass, k_power, entry)
        await _ac_run(hass, climate, "cool", target)
        if ac_dead:
            name = unit_name(hass, entry, "ac") or "The air conditioner"
            _publish(hass, entry, STATE_WAITING, current_temperature=round(temp, 1),
                     reason=f"{name} is not responding", running=[], **live)
            return
        _publish(
            hass, entry, STATE_COOLING, current_temperature=round(temp, 1),
            unit=unit_name(hass, entry, "ac"), running=["ac"], **live,
        )
    else:
        if heater_ok:
            await _heater_off(hass, power, entry)
        for k, (k_power, _n) in extras.items():
            if extra_ok[k]:
                await _heater_off(hass, k_power, entry)
        await _ac_off(hass, climate)
        _publish(hass, entry, STATE_IDLE, current_temperature=round(temp, 1), running=[], **live)


def _boosts_update(
    boosts: dict[str, datetime],
    helpers: list[str],
    base_since: datetime | None,
    temp: float,
    target: float,
    now: datetime,
    release: set[str] | None = None,
) -> None:
    """Start and stop the helping heaters, in chain order. The n-th helper
    joins at once when the van is n x FAR_BELOW_C under the target, or when
    the unit before it has heated for BOOST_AFTER and the van is still
    START_BAND_C under; it leaves near the target once its minimum run is
    done. A unit can only join while the one before it runs.

    `release` holds helpers the gap no longer justifies (after the air
    conditioner took the heating over): each stops as soon as its minimum run
    is done, unless the van falls far enough under to want it again."""
    release = release if release is not None else set()
    for src in [s for s in boosts if s not in helpers]:
        boosts.pop(src)  # no longer a helper (shore came or went, a nominee changed)
        release.discard(src)
    before = base_since
    for n, src in enumerate(helpers, start=1):
        since = boosts.get(src)
        far = temp <= target - FAR_BELOW_C * n
        if since is None:
            lagging = (
                before is not None and now - before >= BOOST_AFTER
                and temp <= target - START_BAND_C
            )
            if before is not None and (far or lagging):
                boosts[src] = since = now
        elif src in release:
            if far:
                release.discard(src)  # cold enough to want it after all
            elif now - since >= MIN_HEAT_RUN:
                boosts.pop(src)
                release.discard(src)
                since = None
        elif temp >= target - BOOST_STOP_C and now - since >= MIN_HEAT_RUN:
            boosts.pop(src)
            since = None
        before = since


async def async_stop(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Stop whichever unit Climate Control is running, and nothing else."""
    phase = _phase.pop(entry.entry_id, None)
    by = _heat_by.pop(entry.entry_id, None)
    boosts = _boost.pop(entry.entry_id, None) or {}
    _release.pop(entry.entry_id, None)
    _heat_since.pop(entry.entry_id, None)
    _base_since.pop(entry.entry_id, None)
    power, _number, climate = _units(hass, entry)
    if phase == HEAT and (by == "heater" or "heater" in boosts):
        await _heater_off(hass, power, entry)
    if phase == COOL or (phase == HEAT and by == "ac"):
        await _ac_off(hass, climate)
    for k in EXTRA_HEATERS:
        if k in boosts:
            await _heater_off(hass, _extra_units(hass, entry, k)[0], entry)


async def _hand_back_balticas(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """SensaClimate turned off: each picked Baltica is left as `baltica_off`
    says, so the room never silently loses its heat or keeps it unasked.
    "Hot water only" sets it to Hot water; "Keep heating" sets its Heating
    Temperature to SensaClimate's and Hot water and heating, and its own
    thermostat carries on. Its room heat switch is let go either way."""
    from .heater_temp_source import firmware_entities

    keep = setting(entry, "baltica_off") == BALTICA_OFF_KEEP
    for slot in HEATER_SLOTS:
        device = _device(hass, entry, nominated(entry, slot))
        if not is_baltica(device):
            continue
        roles = firmware_entities(hass, device)
        heat = roles.get("sensaclimate_heat")
        if not heat:
            continue  # older firmware: SensaClimate never drove it
        st = _state(hass, heat)
        if st is not None and st.state == "on":
            await _call(hass, "switch", "turn_off", {"entity_id": heat})
        if keep:
            await _number_to(hass, roles.get("target_temperature"), float(setting(entry, "target")))
        button = roles.get("baltica_set_heating" if keep else "baltica_set_hot_water")
        if button:
            await _call(hass, "button", "press", {"entity_id": button})


async def async_set_enabled(hass: HomeAssistant, entry: ConfigEntry, on: bool) -> None:
    """The card's power toggle. Refuses on until a heater and an air
    conditioner are both chosen (`configured`)."""
    if on and not configured(entry):
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError(NOT_CONFIGURED)
    if on:
        _announce.add(entry.entry_id)
        _fresh.add(entry.entry_id)
        _phase.pop(entry.entry_id, None)
    if not on:
        await async_stop(hass, entry)
        await _hand_back_balticas(hass, entry)
    set_setting(hass, entry, "enabled", on)
    if on and entitled(hass) and configured(entry):
        await _async_stop_rival_heaters(hass, entry)
    await async_drive(hass, entry)
    await async_push_frost_standby(hass, entry)  # at once, not on the next tick


def rival_heaters(hass: HomeAssistant, entry: ConfigEntry) -> list[tuple[str, str]]:
    """(name, power switch) of every heater other than the nominated one that
    regulates on Climate Control's sensor: the ones that would chase the same
    reading. Uses the heater's own Temperature Sensor Source, the one mapping
    the heater cards read (`heater_temp_source.regulating_temp_entity`)."""
    from .heater_temp_source import (
        choice_for,
        firmware_entities,
        regulating_temp_entity,
    )

    sensor = nominated(entry, "sensor")
    mine = {nominated(entry, "heater"), *(nominated(entry, k) for k in EXTRA_HEATERS)}
    out: list[tuple[str, str]] = []
    if not sensor:
        return out
    for device in heater_devices(hass, entry):
        device_id = str(device.get("device_id") or "")
        if device_id in mine or is_baltica(device):
            continue  # a Baltica not picked keeps its own room, and its hot water
        role_map = firmware_entities(hass, device)
        if "temp_source_select" not in role_map or not role_map.get("power"):
            continue  # no source firmware: it regulates on its own sensor
        if regulating_temp_entity(role_map, choice_for(entry, device_id)) != sensor:
            continue
        out.append((str(device.get("display_name") or device.get("name") or "Heater"), role_map["power"]))
    return out


async def _async_stop_rival_heaters(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """At switch-on only: stop any other heater regulating on the same sensor."""
    from .crypto_autopilot import heater_held_off

    for name, power in rival_heaters(hass, entry):
        st = _state(hass, power)
        if st is None or st.state != "on" or heater_held_off(hass, power):
            continue
        await _heater_off(hass, power, entry)
        _log_line(
            hass, "info",
            f"Climate Control: {name} switched off. It heats on the same "
            "temperature sensor, so Climate Control runs the heating now.",
        )


async def async_set_nominee(hass: HomeAssistant, entry: ConfigEntry, key: str, value: str) -> None:
    """A Settings select changed. A unit taken off the job while it runs is
    stopped first, or it would be left running with nobody watching it."""
    phase = _phase.get(entry.entry_id)
    by = _heat_by.get(entry.entry_id)
    # The sensor stops nothing; a unit stops only if it is the one running.
    boosts = _boost.get(entry.entry_id) or {}
    running = phase == HEAT and key in boosts or (
        (key == "heater" and phase == HEAT and by == "heater")
        or (key == "ac" and (phase == COOL or (phase == HEAT and by == "ac")))
    )
    if running and setting(entry, key) != value:
        await async_stop(hass, entry)
    set_setting(hass, entry, key, value)
    await async_drive(hass, entry)


async def async_push_frost_standby(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Tell every Autoterm box whether SensaClimate is on.

    A box with Frost Protection (Air D 1.1.10) stands aside while SensaClimate
    runs the van, or the two would fight over the heater. A Baltica only while
    it is picked: there its room thermostat stands aside too and follows its
    SensaClimate Heat switch; one not picked keeps its own room.
    "On" is re-asserted every minute and the box believes it for five, so a
    Nomad that goes quiet hands the van back to frost protection, never
    leaves it unprotected. "Off" is sent once, on the edge."""
    from .heater_temp_source import firmware_entities

    on = running(hass, entry)
    picked = {nominated(entry, slot) for slot in HEATER_SLOTS} - {None}
    for device in heater_devices(hass, entry):
        if is_baltica(device) and str(device.get("device_id") or "") not in picked:
            continue
        switch = firmware_entities(hass, device).get("sensaclimate_active")
        st = _state(hass, switch)
        if st is None or st.state in _DEAD:
            continue  # older firmware, or the box is offline
        if on:
            await _call(hass, "switch", "turn_on", {"entity_id": switch})
        elif st.state != "off":
            await _call(hass, "switch", "turn_off", {"entity_id": switch})


async def _drive_safely(hass: HomeAssistant, entry: ConfigEntry) -> None:
    try:
        await async_drive(hass, entry)
    except Exception:  # one bad tick must not stop the timer
        _LOGGER.warning("Climate Control tick failed", exc_info=True)
    try:
        await async_push_frost_standby(hass, entry)
    except Exception:
        _LOGGER.warning("SensaClimate: frost standby push failed", exc_info=True)


@callback
def async_setup_climate_control(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drive every minute, and at once when the chosen sensor changes."""
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_interval,
    )

    tracked: dict[str, Any] = {"id": None, "unsub": None}

    async def _on_change(_event: Event) -> None:
        await _drive_safely(hass, entry)

    def _retrack() -> None:
        sensor = reading_entity(hass, entry)
        if sensor == tracked["id"]:
            return
        if tracked["unsub"] is not None:
            tracked["unsub"]()
            tracked["unsub"] = None
        tracked["id"] = sensor
        if sensor:
            tracked["unsub"] = async_track_state_change_event(hass, [sensor], _on_change)

    async def _tick(_now: Any) -> None:
        _retrack()  # a newly chosen sensor is watched within a minute
        await _drive_safely(hass, entry)

    _retrack()
    entry.async_on_unload(async_track_time_interval(hass, _tick, TICK))

    @callback
    def _stop() -> None:
        if tracked["unsub"] is not None:
            tracked["unsub"]()
        for store in (
            _phase, _heat_since, _heat_by, _base_since, _boost, _release, _shore, _soc_ok, _outside_ok,
            _reading_ok, _status, _logged,
        ):
            store.pop(entry.entry_id, None)
        _announce.discard(entry.entry_id)
        _fresh.discard(entry.entry_id)

    entry.async_on_unload(_stop)
