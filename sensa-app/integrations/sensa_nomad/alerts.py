"""Customer alert framework — declarative rules that write to the Sensa log.

A rule pairs a CONDITION (a predicate over `hass` state) with a customer-facing
MESSAGE and a LEVEL. The engine evaluates every enabled rule on a slow timer
and, when a rule's condition becomes true, writes one entry to the Sensa log
(`_log.sensa_log`) — which in turn raises an HA notification for warning/error/
notification levels. Adding a new alert is a declarative `AlertRule` entry in
`ALERT_RULES`, not bespoke code.

Edge-triggered + cooldown: a rule fires once on the false→true transition, then
again only if it stays true past its `cooldown` (a gentle reminder) — so a
condition that sits true for hours doesn't spam the log every tick.

Conditions are composable predicates (`entity_below`, `proximity_within`,
`all_of`, …). Entity references may be a literal `entity_id` or a
`(hass) -> entity_id | None` resolver, so a rule can find its entity by role at
evaluation time; an unresolved entity makes the condition false (the rule
simply never fires), never an error.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from . import breakdown as _breakdown
from ._log import sensa_log
from .const import (
    SENSA_LOG_SOURCE_SYSTEM,
    SENSA_LOG_SOURCE_VEHICLE,
    SENSA_SCHENGEN_WARN_DAYS_REMAINING,
)

_LOGGER = logging.getLogger(__name__)

# The Schengen days-used sensor the approaching-limit rule reads.
SCHENGEN_DAYS_SENSOR = "sensor.sensa_nomad_schengen_days_used"

# How often the engine evaluates rules as a backstop. Most customer alerts are
# coarse-grained (low water, proximity) so a minute is ample and cheap. Rules
# that must react the INSTANT something happens (e.g. the engine starting while
# on shore power) declare `triggers` — entities whose state change re-evaluates
# the rule immediately, without waiting for this tick.
ALERT_INTERVAL = timedelta(seconds=60)

# Threshold for the proximity rule. (Simple single-entity thresholds like low
# water / low battery moved to curated HA automations — see instincts_catalog.)
LOW_WATER_PCT = 20.0
WATER_SOURCE_RADIUS_MILES = 5.0

# Points of interest the `low_water_near_source` rule checks proximity against.
# DELIBERATELY EMPTY for now: the rule + condition are wired and correct, but
# the water-source geodata is a separate task (see the plan's "out of scope").
# With no points the proximity condition is always false, so the rule never
# fires until this list is populated. Each entry is (latitude, longitude).
WATER_SOURCES: list[tuple[float, float]] = []

EntityRef = str | Callable[[Any], "str | None"]
Predicate = Callable[[Any], bool]


# -- low-level state reads ---------------------------------------------------

def _resolve_ref(hass: Any, ref: EntityRef) -> str | None:
    if callable(ref):
        try:
            return ref(hass)
        except Exception:  # pragma: no cover - resolver heuristics never fatal
            _LOGGER.debug("Alert: entity resolver raised", exc_info=True)
            return None
    return ref


def _state(hass: Any, ref: EntityRef) -> str | None:
    entity_id = _resolve_ref(hass, ref)
    if not entity_id:
        return None
    states = getattr(hass, "states", None)
    if states is None:
        return None
    st = states.get(entity_id)
    if st is None:
        return None
    return getattr(st, "state", None)


def _num(hass: Any, ref: EntityRef) -> float | None:
    raw = _state(hass, ref)
    if raw in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _attrs(hass: Any, ref: EntityRef) -> dict[str, Any]:
    entity_id = _resolve_ref(hass, ref)
    states = getattr(hass, "states", None)
    if not entity_id or states is None:
        return {}
    st = states.get(entity_id)
    if st is None:
        return {}
    return getattr(st, "attributes", None) or {}


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles. Self-contained so the framework carries
    no HA-runtime dependency for distance (and is trivially unit-testable)."""
    radius_miles = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * radius_miles * math.asin(math.sqrt(a))


# -- condition primitives (each returns a predicate) -------------------------

def entity_below(ref: EntityRef, value: float) -> Predicate:
    """True when the numeric state of `ref` is below `value`."""
    def _p(hass: Any) -> bool:
        n = _num(hass, ref)
        return n is not None and n < value
    return _p


def entity_above(ref: EntityRef, value: float) -> Predicate:
    """True when the numeric state of `ref` is above `value`."""
    def _p(hass: Any) -> bool:
        n = _num(hass, ref)
        return n is not None and n > value
    return _p


def state_is(ref: EntityRef, target: str) -> Predicate:
    """True when the state of `ref` equals `target`."""
    return lambda hass: _state(hass, ref) == target


def proximity_within(
    tracker: EntityRef, points: list[tuple[float, float]], miles: float
) -> Predicate:
    """True when the tracker's GPS position is within `miles` of any point."""
    def _p(hass: Any) -> bool:
        if not points:
            return False
        attrs = _attrs(hass, tracker)
        lat, lon = attrs.get("latitude"), attrs.get("longitude")
        if lat is None or lon is None:
            return False
        try:
            lat, lon = float(lat), float(lon)
        except (ValueError, TypeError):
            return False
        return any(
            _haversine_miles(lat, lon, plat, plon) <= miles
            for plat, plon in points
        )
    return _p


def schengen_low_days(hass: Any) -> bool:
    """True when Schengen tracking is on, the van is currently IN the area, and
    days-remaining has dropped to/below the warning threshold. Reading the
    sensor's attributes keeps the rule a single declarative predicate."""
    attrs = _attrs(hass, SCHENGEN_DAYS_SENSOR)
    if not attrs.get("enabled") or not attrs.get("in_schengen"):
        return False
    rem = attrs.get("days_remaining")
    try:
        return rem is not None and int(rem) <= SENSA_SCHENGEN_WARN_DAYS_REMAINING
    except (TypeError, ValueError):
        return False


def _schengen_low_message(hass: Any) -> str:
    attrs = _attrs(hass, SCHENGEN_DAYS_SENSOR)
    rem = attrs.get("days_remaining")
    leave = attrs.get("must_leave_by")
    limit = attrs.get("limit_days") or 90
    msg = f"Schengen {limit}/180: only {rem} of your {limit} days remain."
    if leave:
        msg += f" On current usage you'd need to leave the Schengen area by {leave}."
    return msg


def all_of(*predicates: Predicate) -> Predicate:
    return lambda hass: all(p(hass) for p in predicates)


def any_of(*predicates: Predicate) -> Predicate:
    return lambda hass: any(p(hass) for p in predicates)


# -- the rule + engine -------------------------------------------------------

@dataclass
class AlertRule:
    """One declarative customer alert.

    `message`/`context` may be a static value or a `(hass) -> value` callable so
    the entry can quote the current reading.
    """

    id: str
    level: str
    message: str | Callable[[Any], str]
    condition: Predicate
    cooldown: timedelta = timedelta(hours=1)
    enabled: bool = True
    context: dict[str, Any] | Callable[[Any], dict[str, Any]] | None = None
    # Which log the customer reads this in. Defaults to the van talking about
    # itself, which is what nearly every rule here is; a rule that is really
    # about the BOX (a catalogue gap we want reporting, say) sets `system` and
    # its message moves to Settings -> System Log.
    source: str = SENSA_LOG_SOURCE_VEHICLE
    # Entities whose state change re-evaluates this rule IMMEDIATELY (instant
    # alerts), in addition to the 60 s backstop poll. May be literal entity_ids
    # or resolvers; resolved + subscribed by the engine, re-resolved each tick.
    triggers: list[EntityRef] | None = None


@dataclass
class _RuleState:
    was_active: bool = False
    last_fired: datetime | None = None


# -- entity-role resolvers for the shipping rules ----------------------------
# Best-effort heuristics over the live state machine — Sensa's tank / SoC /
# vehicle-tracker entity_ids aren't fixed singletons (they come from the
# service-class role resolver), so these find the conventional entity by
# substring. An unresolved entity → the rule's condition is false → no alert.

def _find_entities(hass: Any, domain: str, *needles: str) -> list[str]:
    states = getattr(hass, "states", None)
    if states is None:
        return []
    try:
        ids = list(states.async_entity_ids(domain))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            ids = [e for e in states.async_entity_ids() if e.startswith(f"{domain}.")]
        except Exception:  # noqa: BLE001
            ids = [
                getattr(s, "entity_id", "")
                for s in (getattr(states, "async_all", list)())
            ]
    return [
        entity_id
        for entity_id in ids
        if all(n in entity_id.lower() for n in needles)
    ]


def _find_entity(hass: Any, domain: str, *needles: str) -> str | None:
    matches = _find_entities(hass, domain, *needles)
    return matches[0] if matches else None


def _fresh_water_entity(hass: Any) -> str | None:
    return (
        _find_entity(hass, "sensor", "fresh", "water")
        or _find_entity(hass, "sensor", "fresh_water")
    )


def _grey_water_entity(hass: Any) -> str | None:
    return (
        _find_entity(hass, "sensor", "grey", "water")
        or _find_entity(hass, "sensor", "gray", "water")
        or _find_entity(hass, "sensor", "waste", "water")
        or _find_entity(hass, "sensor", "grey_water")
    )


def _battery_soc_entity(hass: Any) -> str | None:
    return (
        _find_entity(hass, "sensor", "battery", "soc")
        or _find_entity(hass, "sensor", "state_of_charge")
    )


def _registry_live_entities(hass: Any, domain: str) -> list[tuple[str, str]]:
    """Every ENABLED `domain` entity in the registry as `(entity_id,
    lowercased unique_id)` pairs.

    For a device whose HA entity_id is slugified from a friendly *name* that
    doesn't name the reading, only the `unique_id` carries the stable object_id.
    Reads the entity registry (not just `hass.states`, which is keyed on the
    slugified id) and confirms each entity has a live state so we never report a
    disabled entity as present.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
    except Exception:  # pragma: no cover - registry always present at runtime  # noqa: BLE001
        return []
    pairs: list[tuple[str, str]] = []
    for ent in getattr(ent_reg, "entities", {}).values():
        eid = str(getattr(ent, "entity_id", "") or "")
        if not eid.startswith(f"{domain}."):
            continue
        uid = str(getattr(ent, "unique_id", "") or "").lower()
        if hass.states.get(eid) is not None:
            pairs.append((eid, uid))
    return pairs


def _registry_entities_by_uid_suffix(
    hass: Any, domain: str, suffix: str
) -> list[str]:
    """Every ENABLED `domain` entity whose registry `unique_id` ends with
    `suffix` (possibly empty). See `_registry_live_entities`."""
    return [
        eid
        for eid, uid in _registry_live_entities(hass, domain)
        if uid.endswith(suffix)
    ]


def _registry_entity_by_uid_suffix(
    hass: Any, domain: str, suffix: str
) -> str | None:
    """The first match of `_registry_entities_by_uid_suffix`, or None."""
    matches = _registry_entities_by_uid_suffix(hass, domain, suffix)
    return matches[0] if matches else None


def _cell_delta_entities(hass: Any) -> list[str]:
    """Every leisure-battery cell-voltage-delta sensor (one per BMS pack).

    All the BLE BMS families (Roamer Gen 1 VIP, Roamer Gen 2 JK, Fogstar JBD)
    share one entity factory (`vendors/bms_ble.py`), so every pack carries a
    "Cell Voltage Delta" sensor with the uniform unique_id
    `{entry_id}_bms_{device_id}_delta_voltage` — the registry suffix is the
    stable signal (the entity_id is slugified from the pack's display name).
    Returns a list so a van running two or more packs is fully covered. Victron
    Smart Lithium exposes min/max cell voltage but no delta sensor yet, so it
    won't resolve here (known gap).
    """
    entities = _registry_entities_by_uid_suffix(hass, "sensor", "_delta_voltage")
    if entities:
        return entities
    # Partial test stubs / registry-less environments: fall back to the
    # slugified entity_id (`sensor.<pack>_cell_voltage_delta`).
    return _find_entities(hass, "sensor", "cell", "voltage", "delta")


# Every BLE BMS pack gets a state-of-charge sensor and a "Discharging Allowed"
# flag from the one shared entity factory (`vendors/bms_ble.py`), with the
# uniform unique_ids `{entry_id}_bms_{device_id}_battery_level` and
# `{entry_id}_bms_{device_id}_flag_dischrg_mosfet` -- so every family (Roamer
# Gen 1 VIP, Roamer Gen 2 JK, Fogstar JBD) and any future pack resolves off the
# registry suffix, exactly as `_cell_delta_entities` does. (The flag then needs
# one per-family exclusion on top; see `_DISCHARGE_FLAG_EXCLUDED_MODELS`.)
# Neither has an entity_id fallback: "battery level" is the single most
# collided name in HA (every Zigbee coin cell and phone reports one), so a
# slug scan would silently pull a door sensor's battery into a leisure-pack
# comparison. Registry or nothing.
_BMS_SOC_UID_SUFFIX = "_battery_level"
_BMS_DISCHARGE_FLAG_UID_SUFFIX = "_flag_dischrg_mosfet"
# The entity name `bms_ble.BINARY_SENSOR_FIELDS` gives the flag, trimmed off the
# friendly_name to leave the pack's own display name for the alert message.
_DISCHARGE_FLAG_ENTITY_NAME = "Discharging Allowed"
# The flag entity exists on every family, but it does not MEAN the same thing on
# every family, so the alert cannot simply take them all. On JK / JBD packs
# (Roamer Gen 2, Fogstar Drift) `dischrg_mosfet` is the customer-settable output
# switch, which is what the alert is about. On the VIP chipset (Roamer Gen 1) it
# is DERIVED from the same protection-flags word that feeds `problem_code`
# (`vendors/bms_ble_vip.py`: `not bool(flags & FLAG_DISCHARGE_INHIBITED)`), so
# "off" means the BMS is inhibiting discharge right now (cold, low cell,
# over-current) and clears itself. Alerting on that sends the customer to look
# for a switch the Roamer app does not have.
#
# Matched on the device-registry `model`, which `bms_ble._device_info` builds as
# `f"{family.upper()} BMS"`, and compared lower-cased. The exclusion is POSITIVE
# only: a pack whose model cannot be read stays in, because "could not ask" must
# never silence a real Fogstar alert.
_DISCHARGE_FLAG_EXCLUDED_MODELS = frozenset({"vip bms"})


def _entity_device_model(hass: Any, entity_id: str) -> str:
    """Lowercased device-registry `model` for an entity's device, or `""`.

    Empty means "could not ask" (no registry, no device, partial test stub),
    never "no model" -- callers must not treat it as a match.
    """
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        entry = er.async_get(hass).async_get(entity_id)
        device_id = getattr(entry, "device_id", None)
        if not device_id:
            return ""
        device = dr.async_get(hass).async_get(device_id)
        return str(getattr(device, "model", "") or "").strip().lower()
    except Exception:  # pragma: no cover - registries always present at runtime  # noqa: BLE001
        return ""


def _bms_pack_soc_entities(hass: Any) -> list[str]:
    """Every leisure-battery pack's state-of-charge sensor (one per pack).

    Returns a list so a van running two or more packs can be compared against
    each other; a single-pack van yields one entry and callers that need a
    comparison simply find nothing to compare."""
    return _registry_entities_by_uid_suffix(hass, "sensor", _BMS_SOC_UID_SUFFIX)


def _bms_discharge_flag_entities(hass: Any) -> list[tuple[str, str]]:
    """`(entity_id, pack label)` for every pack's "Discharging Allowed" flag.

    The BMS reports this MOSFET line read-only; it is set in the battery
    vendor's own app. Turned off, the pack is electrically out of the bank on
    the discharge path: it sits at full charge while its neighbours carry the
    whole load. The label is the pack's display name (friendly_name minus the
    entity's own name) so an alert can say WHICH battery.

    Families whose flag is a protection state rather than a switch are skipped
    (see `_DISCHARGE_FLAG_EXCLUDED_MODELS`); their entity still exists, it just
    isn't something to alert a customer about."""
    out: list[tuple[str, str]] = []
    for eid in _registry_entities_by_uid_suffix(
        hass, "binary_sensor", _BMS_DISCHARGE_FLAG_UID_SUFFIX
    ):
        if _entity_device_model(hass, eid) in _DISCHARGE_FLAG_EXCLUDED_MODELS:
            continue
        name = str(_attrs(hass, eid).get("friendly_name") or "").strip()
        if name.endswith(_DISCHARGE_FLAG_ENTITY_NAME):
            name = name[: -len(_DISCHARGE_FLAG_ENTITY_NAME)].strip()
        out.append((eid, name or eid))
    return sorted(out)


def _bms_pack_signals(hass: Any) -> list[dict[str, Any]]:
    """Per-pack pairing of the BMS Pack Current sensor with the pack's
    temperature sensors: `[{"current": eid, "temps": [eids]}]`.

    Every BLE BMS entity's unique_id shares the pack's `_bms_{device_id}_`
    marker (`vendors/bms_ble.py`): the current is `..._bms_{id}_current` (NB
    `..._balance_current` also ends `_current` and must not match) and each
    temperature is `..._bms_{id}_temp_{slug}` where the slug VARIES per family
    (battery_temp / cell_1_temp / bms_temp_1 / ...) — so temps are matched on
    the `_temp_` marker, not a suffix, and grouped with their current by the
    shared uid prefix. Only packs with BOTH signals are returned. No entity_id
    fallback: reliable per-pack pairing needs the registry.
    """
    currents: dict[str, str] = {}
    temps: dict[str, list[str]] = {}
    for eid, uid in _registry_live_entities(hass, "sensor"):
        if "_bms_" not in uid:
            continue
        if uid.endswith("_current") and not uid.endswith("_balance_current"):
            currents[uid[: -len("_current")]] = eid
        elif "_temp_" in uid.split("_bms_", 1)[1]:
            temps.setdefault(uid.split("_temp_", 1)[0], []).append(eid)
    return [
        {"current": currents[prefix], "temps": temps[prefix]}
        for prefix in sorted(currents)
        if prefix in temps
    ]


def _engine_rpm_entity(hass: Any) -> str | None:
    return (
        _find_entity(hass, "sensor", "engine", "rpm")
        or _find_entity(hass, "sensor", "rpm")
        # A real WiCAN Pro derives its entity_id from the firmware PID *name*
        # (often "Engine Speed" — no "rpm"), so the substring search above
        # misses it, but the AutoPID object_id rides the unique_id
        # (`..._engine_rpm`). This is exactly how the Cab Engine tab resolves
        # the RPM dial (`_wican_obd2_entity_map`); match the same way or the
        # Engine Start routine reads "needs an RPM sensor" while the dial and
        # engine tab plainly show one.
        or _registry_entity_by_uid_suffix(hass, "sensor", "_engine_rpm")
    )


# The registry unique_id suffixes a Victron vebus service stamps on its
# AC-input readings. Defined once because THREE resolvers key off them
# (`_shore_power_entity`, `_shore_voltage_entity`, `_inverter_state_entity`),
# and a suffix that drifts in one place silently unpairs the others.
_SHORE_POWER_UID_SUFFIX = "_ac_in_power"
_SHORE_VOLTAGE_UID_SUFFIX = "_ac_in_voltage"
_SHORE_UID_SUFFIXES = (_SHORE_VOLTAGE_UID_SUFFIX, _SHORE_POWER_UID_SUFFIX)


def _shore_power_entity(hass: Any) -> str | None:
    return (
        _find_entity(hass, "sensor", "shore", "power")
        or _find_entity(hass, "sensor", "shore")
        # A real Victron rig names the vebus entity "AC Input Power" (no
        # "shore" substring), but its unique_id ends `_ac_in_power` — the
        # same registry-suffix rescue as `_engine_rpm_entity`'s WiCAN case.
        # Grid-meter keys are bare `power`/`voltage`, so the `_ac_in_*`
        # suffixes are unambiguously the Multiplus input side.
        or _registry_entity_by_uid_suffix(
            hass, "sensor", _SHORE_POWER_UID_SUFFIX
        )
    )


def _starter_voltage_entity(hass: Any) -> str | None:
    """The vehicle/starter battery voltage.

    Sources, in preference order: a battery monitor's auxiliary input (the
    SmartShunt's second sense wire — Venus battery service `Dc/1/Voltage`,
    entity "Starter Voltage", uid suffix `_starter_voltage`), then the OBD2
    adapter's control-module voltage PID (the Engine tab's "12V Rail" dial,
    uid suffix `_control_module_voltage`). NB the WiCAN sleeps when parked,
    so the OBD2 source can be unavailable exactly while parked — the shunt
    input is the always-live signal; OBD2 still covers sag seen at wake."""
    return (
        _find_entity(hass, "sensor", "starter", "voltage")
        or _registry_entity_by_uid_suffix(hass, "sensor", "_starter_voltage")
        or _find_entity(hass, "sensor", "control_module_voltage")
        or _registry_entity_by_uid_suffix(
            hass, "sensor", "_control_module_voltage"
        )
    )


def _shore_voltage_entity(hass: Any) -> str | None:
    """The shore/AC-input VOLTAGE sensor (Victron `Ac/ActiveIn/L1/V`).

    Preferred over the power sensor for detecting a supply drop: wattage
    legitimately idles near zero on a full battery with no AC loads, while
    the input voltage is load-independent (~230 V while the bollard is live,
    collapsing when it trips)."""
    return (
        _find_entity(hass, "sensor", "shore", "voltage")
        or _registry_entity_by_uid_suffix(
            hass, "sensor", _SHORE_VOLTAGE_UID_SUFFIX
        )
    )


# VE.Bus operating states in which the unit is NOT measuring its AC input, so
# a 0 V reading says "switched off", not "the bollard tripped"
# (`victron_mqtt_services._CHARGER_STATE_LABELS`). Deliberately EXCLUDES
# `inverting`: that is what a MultiPlus does the instant shore really is lost,
# and gating on it would suppress the alarm at exactly the moment it is true.
INVERTER_OFF_STATES = ("off",)


def _inverter_state_entity(hass: Any, paired_with: str | None = None) -> str | None:
    """The VE.Bus operating-state sensor of the unit that owns `paired_with`.

    Paired on the shared unique_id PREFIX rather than by name: one MultiPlus
    publishes `..._vebus_276_ac_in_voltage` and `..._vebus_276_state`, and a van
    with two inverters must not gate one unit's alarm on the other's state (the
    same per-device pairing rule as `_bms_pack_signals`). The `_state` suffix is
    far too generic to match on its own, which is why it is only ever resolved
    through a known sibling.

    Falls back to the slugified entity_id so partial test stubs and
    registry-less environments still resolve, and returns None rather than a
    guess when nothing matches: the caller must fail OPEN and allow the alarm.
    """
    live = _registry_live_entities(hass, "sensor")
    if paired_with:
        uid = next((u for eid, u in live if eid == paired_with), "")
        for suffix in _SHORE_UID_SUFFIXES:
            if uid.endswith(suffix):
                target = uid[: -len(suffix)] + "_state"
                return next((eid for eid, u in live if u == target), None)
    return _find_entity(hass, "sensor", "inverter", "state")


def _fuel_level_entity(hass: Any) -> str | None:
    """The vehicle fuel-level sensor (a percentage, 0-100).

    The WiCAN OBD2 adapter derives its entity_id from the PID's display name
    ("Fuel"), so the entity_id often lacks "level"; the reliable signal is the
    entity-registry unique_id, where the object_id (`fuel_level`) is stamped.
    Fall back to a literal entity_id match (covers a template/helper test
    sensor), and never return the Radar nearest-fuel sensor, which also contains
    "fuel" but is a distance, not a gauge.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        for ent in ent_reg.entities.values():
            eid = getattr(ent, "entity_id", "") or ""
            if not eid.startswith("sensor.") or "radar" in eid.lower():
                continue
            uid = str(getattr(ent, "unique_id", "") or "").lower()
            if "fuel_level" in uid:
                return eid
    except Exception:
        _LOGGER.debug("fuel-level registry lookup failed", exc_info=True)
    return _find_entity(hass, "sensor", "fuel", "level")


def _vehicle_tracker(hass: Any) -> str | None:
    return (
        _find_entity(hass, "device_tracker", "vehicle")
        or _find_entity(hass, "device_tracker", "sensa")
    )


# -- unmapped lighting controller (a new Tuya firmware batch) ----------------
# Tuya re-flashes these controllers between batches, and a new batch reports a
# model we have never seen, so we can no longer tell which output mode it is
# in, and the customer may be looking at the wrong controls. Nothing on the van
# can fix it (it needs a Sensa build that maps the new model), which is exactly
# why this is a LOG WARNING and not an alarm: an alarm re-sounds until cancelled
# and there would be nothing to do about it.

def _unmapped_light_models(hass: Any) -> list[tuple[str, str]]:
    try:
        from .vendors.zigbee2mqtt import unmapped_sensa_light_models

        return unmapped_sensa_light_models(hass)
    except Exception:
        _LOGGER.debug("unmapped-light lookup failed", exc_info=True)
        return []


def _has_unmapped_light(hass: Any) -> bool:
    return bool(_unmapped_light_models(hass))


def _unmapped_light_message(hass: Any) -> str:
    found = _unmapped_light_models(hass)
    if not found:  # pragma: no cover - only reached if state changed mid-tick
        return "A lighting controller reported an unrecognised model."
    name, model = found[0]
    extra = f" (and {len(found) - 1} more)" if len(found) > 1 else ""
    return (
        f"{name}{extra} reported an unrecognised model ({model}), so Sensa "
        "can't tell which output mode it's set to. Its controls may not match "
        "the strip. You can set this yourself on the device under "
        "Settings, Devices."
    )


def _unmapped_light_context(hass: Any) -> dict[str, Any]:
    # The model string is the whole point of the message reaching us: it names
    # the batch that needs mapping in the next build.
    return {"models": [model for _name, model in _unmapped_light_models(hass)]}


# -- a Zigbee device Z2M ITSELF cannot identify ------------------------------
# A different failure from the one above, and the difference decides what the
# message can honestly ask for. Above, Z2M knows the device and only SENSA's
# catalogue is behind, so the customer has a usable light and a Light Type
# override. Here Z2M resolved no definition at all: the device exposes nothing,
# has no HA entities, and cannot be made to work by anything we ship. The
# identity table is zigbee-herdsman-converters', not ours.
#
# It is worth a message precisely because the failure is otherwise SILENT. The
# device is absent from HA entirely, so it appears nowhere in the picker, not
# even in Others, and the customer's report is "it won't add" with nothing to
# look at. Naming the two codes turns that into a five-second diagnosis.

def _unidentified_zigbee(hass: Any) -> list[dict[str, Any]]:
    try:
        from .vendors.zigbee2mqtt import unrecognised_bridge_devices

        return unrecognised_bridge_devices(hass)
    except Exception:
        _LOGGER.debug("unidentified-zigbee lookup failed", exc_info=True)
        return []


def _has_unidentified_zigbee(hass: Any) -> bool:
    return bool(_unidentified_zigbee(hass))


def _z2m_is_ours(hass: Any) -> bool:
    """True when SENSA pins the Zigbee2MQTT version, not the customer.

    On a Container appliance Z2M is a service in the Sensa stack
    (`pipelines/config/stack-manifest.yaml`), reconciled by OTA, so a stale
    device table is OUR pin and the customer has nothing to press. On DIY/HAOS
    it is the customer's own Home Assistant add-on and the Add-ons page is the
    fix. Same `appliance and not is_supervised` split as every other
    Container-only behaviour.
    """
    try:
        from .appliance import _is_appliance, is_supervised

        return bool(_is_appliance(hass) and not is_supervised(hass))
    except Exception:  # noqa: BLE001
        return False


def _converters_behind(hass: Any) -> tuple[str, str] | None:
    try:
        from .vendors.zigbee2mqtt import converters_version_behind

        return converters_version_behind(hass)
    except Exception:
        _LOGGER.debug("converters-version lookup failed", exc_info=True)
        return None


def _unidentified_zigbee_message(hass: Any) -> str:
    found = _unidentified_zigbee(hass)
    if not found:  # pragma: no cover - only reached if state changed mid-tick
        return "A paired Zigbee device could not be identified."
    dev = found[0]
    name = str(dev.get("friendly_name") or "A paired Zigbee device")
    model = str(dev.get("model_id") or "unknown")
    code = str(dev.get("manufacturer") or "unknown")
    extra = f" (and {len(found) - 1} more)" if len(found) > 1 else ""
    behind = _converters_behind(hass)
    # Two different asks, and getting them the wrong way round wastes the
    # customer's time: an out-of-date table is fixed by updating Zigbee2MQTT,
    # a current one means the batch is new to everybody and only a report
    # upstream helps. Never claim "out of date" when `bridge/info` never
    # arrived — `_converters_behind` returns None for that, same as for current.
    if not behind:
        tail = (
            "This looks like a device batch Zigbee2MQTT doesn't know yet, so "
            "please report it to Sensa support."
        )
    elif _z2m_is_ours(hass):
        # Sensa pins this version, so there is no button for the customer.
        # Saying "update the add-on" here would send them looking for a page
        # that, on an appliance, they cannot even reach.
        tail = (
            "Sensa's device table is out of date on this system; please "
            "report it to Sensa support."
        )
    else:
        tail = "Updating the Zigbee2MQTT add-on should fix it."
    return (
        f"{name}{extra} paired but Zigbee2MQTT couldn't identify it "
        f"(model {model}, code {code}), so it has no controls and can't be "
        f"added. {tail}"
    )


def _unidentified_zigbee_context(hass: Any) -> dict[str, Any]:
    # The two codes are the whole point of this reaching us.
    devices = [
        {
            "friendly_name": d.get("friendly_name"),
            "model_id": d.get("model_id"),
            "manufacturer": d.get("manufacturer"),
        }
        for d in _unidentified_zigbee(hass)
    ]
    behind = _converters_behind(hass)
    return {
        "devices": devices,
        "converters_running": behind[0] if behind else None,
        "converters_floor": behind[1] if behind else None,
    }


# -- a Zigbee device that joined but never finished pairing ------------------
# Z2M's interview of the device failed (a battery sensor fell asleep half way),
# so Z2M knows nothing about it, publishes nothing, and it appears nowhere in
# Sensa. The bridge watch keeps re-interviewing it; the customer's part is to
# keep it awake long enough for one attempt to land.

def _incomplete_zigbee(hass: Any) -> list[dict[str, Any]]:
    try:
        from .vendors.zigbee2mqtt import failed_interview_bridge_devices

        return failed_interview_bridge_devices(hass)
    except Exception:
        _LOGGER.debug("incomplete-zigbee lookup failed", exc_info=True)
        return []


def _has_incomplete_zigbee(hass: Any) -> bool:
    return bool(_incomplete_zigbee(hass))


def _incomplete_zigbee_message(hass: Any) -> str:
    found = _incomplete_zigbee(hass)
    name = str(
        (found[0].get("friendly_name") if found else "") or "A Zigbee device"
    )
    extra = f" (and {len(found) - 1} more)" if len(found) > 1 else ""
    return (
        f"{name}{extra} joined but didn't finish pairing, so Sensa can't tell "
        "what it is yet. Press the button on the device once every couple of "
        "seconds for about a minute. Sensa keeps retrying and adds it as soon "
        "as it answers."
    )


def _incomplete_zigbee_context(hass: Any) -> dict[str, Any]:
    return {
        "devices": [
            {
                "friendly_name": d.get("friendly_name"),
                "ieee_address": d.get("ieee_address"),
            }
            for d in _incomplete_zigbee(hass)
        ],
    }


# -- Z2M's identity table is behind the one we test against ------------------
# Reported on its own, not only alongside a missing device, because the table
# going stale is what CAUSES the missing device: a box that updates before the
# customer buys the next controller never sees the fault at all.

def _converters_old(hass: Any) -> bool:
    return _converters_behind(hass) is not None


def _converters_old_message(hass: Any) -> str:
    behind = _converters_behind(hass)
    if behind is None:  # pragma: no cover - only if state changed mid-tick
        return "The Zigbee2MQTT add-on is out of date."
    running, floor = behind
    action = (
        "Please report it to Sensa support."
        if _z2m_is_ours(hass)
        else "Update the Zigbee2MQTT add-on from the Add-ons page."
    )
    return (
        f"The Zigbee device table is out of date ({running}, Sensa tests "
        f"against {floor}). Newly bought Zigbee devices may fail to add, or "
        f"add with controls missing. {action}"
    )


def _converters_old_context(hass: Any) -> dict[str, Any]:
    behind = _converters_behind(hass)
    return {
        "converters_running": behind[0] if behind else None,
        "converters_floor": behind[1] if behind else None,
        "version_pinned_by_sensa": _z2m_is_ours(hass),
    }


# The declarative registry of CODE-ONLY alerts — conditions awkward to express
# as an HA automation. Simple single-entity thresholds (low water, low battery)
# and state-trigger alerts (engine on shore power) live as curated HA
# automations instead (see instincts_catalog.py); they're easier for the
# customer to toggle and HA evaluates them instantly. This list is for logic
# that genuinely needs Python — today, GPS proximity to any of N water sources.
ALERT_RULES: list[AlertRule] = [
    AlertRule(
        id="low_water_near_source",
        level="warning",
        message=(
            "You're near a water source and fresh water is low — a good "
            "moment to top up."
        ),
        condition=all_of(
            entity_below(_fresh_water_entity, LOW_WATER_PCT),
            proximity_within(
                _vehicle_tracker, WATER_SOURCES, WATER_SOURCE_RADIUS_MILES
            ),
        ),
        # Instant: re-evaluate the moment the van's position changes, so the
        # alert fires as you arrive near a source rather than up to 60 s later.
        triggers=[_vehicle_tracker],
        cooldown=timedelta(hours=2),
    ),
    AlertRule(
        id="unmapped_lighting_model",
        level="warning",
        message=_unmapped_light_message,
        condition=_has_unmapped_light,
        context=_unmapped_light_context,
        # A gap in OUR catalogue, not something the van is doing, so it belongs
        # in the System Log with the other "about this box" messages.
        source=SENSA_LOG_SOURCE_SYSTEM,
        # Nothing the customer can do about it, and it cannot resolve itself, so
        # this is a "tell us" not a "fix it now": say it once a week at most.
        cooldown=timedelta(days=7),
    ),
    AlertRule(
        id="unidentified_zigbee_device",
        level="warning",
        message=_unidentified_zigbee_message,
        condition=_has_unidentified_zigbee,
        context=_unidentified_zigbee_context,
        # About the BOX and its add-ons, not about the van.
        source=SENSA_LOG_SOURCE_SYSTEM,
        # Cannot resolve itself and the customer may need to act once (update
        # the add-on), so say it weekly at most — same cadence, same reasoning
        # as the unmapped-model warning above.
        cooldown=timedelta(days=7),
    ),
    AlertRule(
        id="zigbee_pairing_incomplete",
        level="warning",
        message=_incomplete_zigbee_message,
        condition=_has_incomplete_zigbee,
        context=_incomplete_zigbee_context,
        source=SENSA_LOG_SOURCE_SYSTEM,
        # The customer has something to do, but repeating it every hour while
        # a forgotten sensor sits in a drawer is a nag, not help.
        cooldown=timedelta(days=1),
    ),
    AlertRule(
        id="zigbee_device_table_old",
        level="warning",
        message=_converters_old_message,
        condition=_converters_old,
        context=_converters_old_context,
        source=SENSA_LOG_SOURCE_SYSTEM,
        cooldown=timedelta(days=7),
    ),
    AlertRule(
        id="schengen_low_days",
        level="warning",
        message=_schengen_low_message,
        condition=schengen_low_days,
        # Re-evaluate the instant the days-used sensor changes (a new stamp or
        # the daily window rollover), not just on the 60 s poll.
        triggers=[SCHENGEN_DAYS_SENSOR],
        # Nag at most once a day while still over the threshold.
        cooldown=timedelta(hours=24),
    ),
    # Breakdown cover renewal (Hub feature gate `breakdown`). Each window fires
    # once as the date approaches: the cooldown outlasts the window.
    *[
        AlertRule(
            id=f"breakdown_cover_{days}d",
            level="warning",
            message=_breakdown.reminder_message,
            condition=lambda hass, d=days: _breakdown.reminder_due(hass, d),
            triggers=[_breakdown.ENTITY_ID],
            cooldown=timedelta(days=60),
        )
        for days in _breakdown.REMIND_DAYS
    ],
    AlertRule(
        id="breakdown_cover_expired",
        level="warning",
        message=_breakdown.reminder_message,
        condition=_breakdown.expired_due,
        triggers=[_breakdown.ENTITY_ID],
        # Once a fortnight until renewed; a new date clears the condition.
        cooldown=timedelta(days=14),
    ),
]


def evaluate_rules(
    hass: Any,
    rules: list[AlertRule],
    rule_states: dict[str, _RuleState],
    now: datetime,
) -> list[str]:
    """Evaluate `rules`, firing each that transitions true (or stays true past
    its cooldown). Returns the ids that fired this pass (handy for tests)."""
    fired: list[str] = []
    for rule in rules:
        if not rule.enabled:
            continue
        st = rule_states.setdefault(rule.id, _RuleState())
        try:
            active = bool(rule.condition(hass))
        except Exception:  # pragma: no cover - a bad rule must not break others
            _LOGGER.debug("Alert rule %s condition raised", rule.id, exc_info=True)
            active = False
        if not active:
            st.was_active = False
            continue
        should_fire = (
            not st.was_active
            or st.last_fired is None
            or (now - st.last_fired) >= rule.cooldown
        )
        st.was_active = True
        if not should_fire:
            continue
        st.last_fired = now
        message = rule.message(hass) if callable(rule.message) else rule.message
        ctx = rule.context(hass) if callable(rule.context) else (rule.context or {})
        # dedup_key=rule.id gives the HA notification a stable id (re-fires
        # update rather than stack); cooldown=0 disables _log's own dedup so
        # the engine's edge+cooldown is the sole gate.
        sensa_log(
            hass, rule.level, message,
            source=rule.source,
            context={"rule": rule.id, **ctx},
            dedup_key=rule.id, cooldown=timedelta(0),
        )
        fired.append(rule.id)
    return fired


def _collect_watch_entities(hass: Any, rules: list[AlertRule]) -> frozenset[str]:
    """The set of entity_ids to subscribe to for instant evaluation — the
    resolved `triggers` of every enabled rule. Unresolved refs are dropped (the
    next tick re-resolves them once the entity exists)."""
    out: set[str] = set()
    for rule in rules:
        if not rule.enabled or not rule.triggers:
            continue
        for ref in rule.triggers:
            eid = _resolve_ref(hass, ref)
            if eid:
                out.add(eid)
    return frozenset(out)


class _AlertEngine:
    """Runs the rules two ways: a 60 s backstop poll, plus instant
    re-evaluation whenever a rule's declared trigger entity changes state. The
    watch set is re-resolved every tick so late-registering entities (ESPHome
    adopting after setup) get an instant subscription once they appear."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.rule_states: dict[str, _RuleState] = {}
        self._interval_cancel: Callable[[], None] | None = None
        self._state_cancel: Callable[[], None] | None = None
        self._watched: frozenset[str] = frozenset()

    def start(self) -> Callable[[], None]:
        self._interval_cancel = async_track_time_interval(
            self.hass, self._on_tick, ALERT_INTERVAL,
        )
        self._refresh_watch()
        return self.stop

    def stop(self) -> None:
        for cancel_attr in ("_interval_cancel", "_state_cancel"):
            cancel = getattr(self, cancel_attr)
            if cancel is not None:
                try:
                    cancel()
                except Exception:  # pragma: no cover  # noqa: BLE001, S110
                    pass
                setattr(self, cancel_attr, None)

    @callback
    def _on_tick(self, now: datetime) -> None:
        self._refresh_watch()
        evaluate_rules(
            self.hass, ALERT_RULES, self.rule_states,
            now or datetime.now(timezone.utc),
        )

    @callback
    def _on_state_change(self, _event: Any) -> None:
        evaluate_rules(
            self.hass, ALERT_RULES, self.rule_states,
            datetime.now(timezone.utc),
        )

    def _refresh_watch(self) -> None:
        desired = _collect_watch_entities(self.hass, ALERT_RULES)
        if desired == self._watched:
            return
        if self._state_cancel is not None:
            try:
                self._state_cancel()
            except Exception:  # pragma: no cover  # noqa: BLE001, S110
                pass
            self._state_cancel = None
        self._watched = desired
        if desired:
            self._state_cancel = async_track_state_change_event(
                self.hass, list(desired), self._on_state_change,
            )


def async_setup_alerts(hass: Any) -> Callable[[], None]:
    """Start the alert engine (poll + instant triggers). Returns a cancel."""
    return _AlertEngine(hass).start()
