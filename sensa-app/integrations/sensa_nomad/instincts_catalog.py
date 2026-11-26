"""Curated Instincts catalog for the Intelligence → Instincts tab.

Renamed from `routines_catalog.py` on 2026-07-26 (file name only) to match the
customer-facing label, and so the future "Routines" feature (the placeholder
tab, unrelated to this file) gets its own module instead of growing this one.
The WIRE names deliberately keep the older eras and MUST stay frozen: tab slug
`automations`, `sensor.sensa_nomad_automations`, `sensa_nomad.set_automation`,
stored options under `entry.options["automations"]` — stored customer choices
and entity ids depend on them.

Each entry is a Sensa-suggested automation the customer can toggle on/off (with
light config, e.g. a threshold). When enabled, Sensa GENERATES a real HA
automation (see `automations.py`) whose action calls the `sensa_nomad.log`
service — so the message lands in the customer-facing Sensa log, and (for
notification+ levels) raises an HA notification. Because they're HA automations,
they're trigger-based and therefore **instant**.

Add a curated automation by adding a `CuratedAutomation` here. `build(hass,
config)` returns the HA automation dict (or None when the devices it needs
aren't present yet — the toggle then shows as "needs <x>" and nothing is
generated). Entity references are resolved with the same heuristic resolvers as
the code-alert framework (`alerts.py`), so the catalog stays vendor-neutral.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .alerts import (
    INVERTER_OFF_STATES,
    _attrs,
    _battery_soc_entity,
    _bms_discharge_flag_entities,
    _bms_pack_signals,
    _bms_pack_soc_entities,
    _cell_delta_entities,
    _engine_rpm_entity,
    _find_entities,
    _fresh_water_entity,
    _fuel_level_entity,
    _grey_water_entity,
    _inverter_state_entity,
    _num,
    _shore_power_entity,
    _shore_voltage_entity,
    _starter_voltage_entity,
)
from .const import (
    FREEZER_FLAG,
    FRIDGE_FLAG,
    GEOFENCE_DISTANCE_ENTITY_ID,
    MOVEMENT_BINARY_SENSOR_ENTITY_ID,
    TEMPERATURE_WATCH_CHANGES_ATTR,
    TEMPERATURE_WATCH_FLAG,
    TEMPERATURE_WATCH_SENSOR_ENTITY_ID,
)
from .modes_catalog import mode_active_entity_id
from .movement import DEFAULT_TUNING, movement_source_available

# An HA-start trigger added to every curated automation so a condition that's
# ALREADY true at boot fires once (paired with a matching `condition:` block).
# The normal `numeric_state` trigger handles the runtime downward crossing; the
# `set_automation` handler covers "enable/adjust while already true" by checking
# `is_active` immediately. Between the three, enabling a "warn below" alert is
# never silent.
_HA_START = {"platform": "homeassistant", "event": "start"}

# The two action modes the customer can pick per automation (see _action_block).
ACTION_WARN = "warn"
ACTION_ALARM = "alarm"
ACTIONS = (ACTION_WARN, ACTION_ALARM)

# Per-entity instincts ("Sensor gone quiet", the Zigbee security pair) build one
# alarm key per watched entity, shaped `<prefix>_<entity_id>`, so two dead
# sensors alert separately. The prefixes are constants because they are read
# from BOTH ends: the builders below stamp one onto the key, and
# `alarm_key_entity` reads it back off so the alarm store can tell which entity
# an alarm is about. Written twice, the two ends would drift and the store would
# stop recognising its own keys.
ALARM_KEY_SENSOR_QUIET = "sensor_quiet"
ALARM_KEY_VIBRATION_DETECTED = "vibration_detected"
ALARM_KEY_LEAK_DETECTED = "leak_detected"
ALARM_KEY_SMOKE_DETECTED = "smoke_detected"
ALARM_KEY_DISCHARGE_DISABLED = "discharge_disabled"
ALARM_KEY_FRIDGE_WATCH = "fridge_watch"
ALARM_KEY_FREEZER_WATCH = "freezer_watch"
ALARM_KEY_TEMPERATURE_WATCH = "temperature_watch"
ALARM_KEY_CIRCUIT_FAULT = "circuit_fault"
ALARM_KEY_SENSOR_BATTERY_LOW = "sensor_battery_low"
ALARM_KEY_TYRE_PRESSURE = "tyre_pressure"
ENTITY_ALARM_KEY_PREFIXES = (
    ALARM_KEY_SENSOR_BATTERY_LOW,
    ALARM_KEY_SENSOR_QUIET,
    ALARM_KEY_VIBRATION_DETECTED,
    ALARM_KEY_LEAK_DETECTED,
    ALARM_KEY_SMOKE_DETECTED,
    ALARM_KEY_DISCHARGE_DISABLED,
    ALARM_KEY_FRIDGE_WATCH,
    ALARM_KEY_FREEZER_WATCH,
    ALARM_KEY_TEMPERATURE_WATCH,
    ALARM_KEY_CIRCUIT_FAULT,
    ALARM_KEY_TYRE_PRESSURE,
)


def _entity_label(hass: Any, eid: str) -> str:
    """Customer-facing name for an entity: its friendly_name, else the raw
    entity_id (friendly_name survives on an unavailable state; the id is the
    only thing left on a fully missing entity). Shared so every per-entity
    instinct message names a device the same way."""
    return str(_attrs(hass, eid).get("friendly_name") or eid)


def alarm_key_entity(key: str) -> str | None:
    """The entity a per-entity alarm key is about, or None when the key isn't
    about one entity (`disk_space`, `low_battery`, a per-check key).

    The alarm store's orphan sweep uses this: an alarm naming an entity that no
    longer exists is watching something the customer has deleted, and nothing
    else would ever cancel it. A key with no `.` in its tail is not an
    entity_id, so it reads as None rather than as a missing entity."""
    for prefix in ENTITY_ALARM_KEY_PREFIXES:
        head = f"{prefix}_"
        if key.startswith(head):
            entity_id = key[len(head):]
            return entity_id if "." in entity_id else None
    return None


def routine_alarm_keys(
    entry: CuratedAutomation,
    live_keys: Any,
    *,
    check_id: str | None = None,
) -> list[str]:
    """Which of `live_keys` were raised BY this routine (or by one check of it).

    Disabling an instinct must take its alarms with it: an alarm only ever
    leaves the store when its producer clears it or the customer cancels it, so
    a routine switched off mid-incident would otherwise re-sound for ever with
    nothing left running to clear it.

    Every key a routine can raise starts with the routine's id: the routine's
    own id for a single-condition routine, `<id>_<check id>` for a check, and
    `<id>_<entity_id>` for the per-entity instincts (whose alarm-key prefix IS
    the routine id — see `ENTITY_ALARM_KEY_PREFIXES`). Matched against those
    three shapes rather than on the bare prefix, so a routine whose id happens
    to start with another's can never sweep away its neighbour's alarms.

    `check_id` narrows it to that one check, for disabling a single check of a
    trigger routine while the rest stay armed.
    """
    if check_id is not None:
        wanted = f"{entry.id}_{check_id}"
        return [k for k in live_keys if k == wanted]
    exact = {entry.id, *(f"{entry.id}_{c.id}" for c in entry.checks)}
    head = f"{entry.id}_"
    out: list[str] = []
    for key in live_keys:
        if key in exact or key.startswith(head) and alarm_key_entity(key) is not None:
            out.append(key)
    return out


@dataclass(frozen=True)
class ConfigField:
    """A single customer-tunable setting on a curated automation.

    `field_type` "number" renders as the card's numeric input; "toggle"
    renders as an on/off switch whose wire value stays numeric 0/1 (so the
    stored config, the service schema and `_setField`'s isFinite guard all
    keep working unchanged) — declare a toggle as min 0 / max 1 / step 1 with
    the default 0 or 1. "select" renders a dropdown whose wire value is a
    STRING (fridge watch stores an entity_id): declare it with `options` (a
    `(hass) -> [{"value","label"}]` resolver, resolved into the card payload
    at describe time so the choices track the live install) and a string
    `default` (usually "" = not yet chosen); min/max/step are unused.
    """

    key: str
    name: str
    minimum: float
    maximum: float
    default: float | str
    unit: str = ""
    step: float = 1.0
    field_type: str = "number"
    options: Callable[[Any], list[dict[str, str]]] | None = None


@dataclass(frozen=True)
class Check:
    """One condition CHECK within a multi-check (trigger) routine.

    A trigger routine (e.g. "Engine Start") fires on an EVENT and then runs a
    list of these checks. Each check is the customer's own enable + Warn/Alert
    choice, carries its own device requirement, and contributes one
    `if <condition> then <warn|alarm>` block to the generated HA automation's
    action. (Single-condition routines don't use this — see CuratedAutomation.)
    """

    id: str
    name: str
    message: str
    # condition(hass, config) -> list[HA condition dicts] | None. None when the
    # device(s) it needs aren't present — the check shows greyed + is skipped.
    condition: Callable[[Any, dict[str, Any]], list[dict[str, Any]] | None]
    # is_active(hass, config) -> bool — whether this check's condition CURRENTLY
    # holds, so enabling the routine while it's already true alerts immediately
    # (the fire-on-enable path). None → no immediate fire for this check.
    is_active: Callable[[Any, dict[str, Any]], bool] | None = None
    device_groups: tuple[str, ...] = ()
    requires_label: str = ""
    supports_alarm: bool = True
    default_action: str = ACTION_WARN
    default_enabled: bool = True
    level: str = "warning"
    config_fields: list[ConfigField] = field(default_factory=list)
    alarm_extras: dict[str, Any] | None = None
    # Whether this check reaches the cloud (e.g. a Sensa Radar signal), so the
    # card can show the "needs internet" marker. False for every check today —
    # carried here for parity with CuratedAutomation.requires_internet, since a
    # future check could need it independently of its parent routine.
    requires_internet: bool = False


@dataclass(frozen=True)
class CuratedAutomation:
    """One Sensa-suggested routine.

    Two shapes: a SINGLE-condition routine (`build`/`is_active` set, `checks`
    empty) or a MULTI-CHECK trigger routine (`trigger` + `checks` set; `build`/
    `is_active`/`message`/`config_fields` unused). The card renders a trigger
    routine as one row whose checks nest beneath it.
    """

    id: str
    name: str
    description: str
    category: str
    level: str
    # The customer-facing log message (shared by the generated automation's
    # action AND the "fire if already true" immediate check). "" for a trigger
    # routine (each check carries its own message).
    message: str
    default_enabled: bool
    # Human note shown when the automation can't be built yet (entities absent).
    requires_label: str
    # `build(hass, config, action) -> HA automation dict | None` (single-condition
    # routines). None for a trigger routine.
    build: Callable[[Any, dict[str, Any], str], dict[str, Any] | None] | None = None
    # `is_active(hass, config) -> bool` — whether the condition CURRENTLY holds,
    # used by `set_automation` to fire immediately on enable/adjust. None for a
    # trigger routine (event-triggered — no "already true" to fire on).
    is_active: Callable[[Any, dict[str, Any]], bool] | None = None
    # A MULTI-CHECK trigger routine: a tuple of checks run on `trigger`. When
    # non-empty, this is a trigger routine (see `_build_check_routine` /
    # `_describe_check_routine`); each check carries its own state.
    checks: tuple[Check, ...] = ()
    # trigger(hass) -> list[HA trigger dicts] | None — the routine's trigger
    # event (e.g. engine RPM rising above 0). Required for a trigger routine.
    trigger: Callable[[Any], list[dict[str, Any]] | None] | None = None
    # Customer-facing **device group(s)** this automation needs a device in
    # (e.g. "Water", "Battery", "Engine"). Drives the per-row pill on the
    # Automations card AND the card's device-type category filter. An entry
    # needing more than one group lists each — the card shows a pill per group.
    device_groups: tuple[str, ...] = ()
    # Whether the customer may escalate this automation from a warning (log
    # message) to a managed alarm. Default action when they haven't chosen.
    supports_alarm: bool = True
    default_action: str = ACTION_WARN
    config_fields: list[ConfigField] = field(default_factory=list)
    # Optional richer alarm presentation (title / guidance / context / severity)
    # used BOTH by the generated automation's alert action AND the "fire if
    # already true on enable" path, so the enhanced banner + detail card get the
    # full detail however the alarm was raised. None → a plain message alarm.
    alarm_extras: dict[str, Any] | None = None
    # Optional `live_message(hass, config) -> str | None`: the fire-on-enable
    # message with the current reading folded in, when the generated
    # automation's action carries a template. None (or a None result) falls
    # back to `message`.
    live_message: Callable[[Any, dict[str, Any]], str | None] | None = None
    # Optional Hub dealer slug (same value as the licence `dealer.id` claim and a
    # `home_layouts_hab.json` `dealer` tag). None → public: every customer sees it.
    # Set to a dealer's slug → only that dealer's licences see it, ADDED to the
    # standard set (always additive, never exclusive) — for a dealer's special
    # hardware. Trials never see dealer routines (current_dealer is None for
    # them). See `_allowed_routines`.
    dealer: str | None = None
    # Optional post-merge config normaliser, applied ONCE in `resolve_state` so
    # every consumer (build / is_active / describe_automations) sees the same
    # canonical config. Used by the disk routine to order its two thresholds
    # (warn <= alert), so an inverted config can't sound the alarm below the warn
    # level AND the card displays what actually fires. None → config unchanged.
    normalise_config: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Optional EXTRA automation builders, each with the same signature as
    # `build`. For a routine that needs a companion automation beyond its
    # primary (cold-charging generates a second one that RESTORES the chargers
    # once the pack warms). Each returning None is simply skipped, so a
    # companion can gate on its own device needs independently.
    extra_builds: tuple[
        Callable[[Any, dict[str, Any], str], dict[str, Any] | None], ...
    ] = ()
    # Optional availability override for the card/sensor row:
    # `availability(hass, config) -> bool`. The default (None) is "build
    # returned an automation", which is wrong for a routine whose build ALSO
    # depends on a customer selection — fridge watch is available the moment a
    # temperature sensor exists, even though build returns None until the
    # customer picks the thermometer. Without the override the row would sit
    # in "Unavailable", where the card hides config fields, so the pick could
    # never be made.
    availability: Callable[[Any, dict[str, Any]], bool] | None = None
    # Optional side effect for the fire-on-enable path: the generated YAML
    # action only runs when the HA automation triggers, so a routine whose
    # action DOES something (not just log/alarm) supplies the same effect here
    # and `set_automation`'s immediate check runs it when the routine is
    # enabled while its condition already holds. Async callable (hass, config).
    on_fire: Callable[[Any, dict[str, Any]], Any] | None = None
    # Whether this routine reaches the cloud to work (e.g. `low_fuel_near_station`
    # reads a Sensa Radar nearest-station signal) — drives the card's "needs
    # internet" marker. A static, hand-set flag: Sensa Control is offline-first,
    # so this stays False (the default) for every routine that's a pure local HA
    # entity check, and only a routine that genuinely can't work offline sets it.
    requires_internet: bool = False


def _action_block(
    action: str,
    level: str,
    message: str,
    dedup_key: str,
    *,
    automation: str | None = None,
    title: str | None = None,
    guidance: str | None = None,
    context: list[dict[str, Any]] | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """The automation's action, per the customer's choice on the Automations tab:

    - **warn** → write a one-shot message to the Sensa log (`sensa_nomad.log`).
    - **alert** → raise a MANAGED alarm (`sensa_nomad.raise_alarm`) that re-sounds
      until snoozed/cancelled. The `dedup_key` doubles as the alarm `key` so a
      re-fire refreshes the same alarm rather than stacking duplicates.

    The optional `title`/`guidance`/`context` enrich the ALARM path only (they
    drive the enhanced banner + the right-side detail card); the warn/log path
    ignores them. `automation` is the owning instinct's id when it is not the
    dedup key (checks, the drive-mode pair); the log path stamps it on the
    entry's context so the Log tab can link the line back to its tile.
    """
    if action == ACTION_ALARM:
        data: dict[str, Any] = {"key": dedup_key, "message": message}
        if title:
            data["title"] = title
        if guidance:
            data["guidance"] = guidance
        if context:
            data["context"] = context
        if severity:
            data["severity"] = severity
        return {"service": "sensa_nomad.raise_alarm", "data": data}
    return {
        "service": "sensa_nomad.log",
        "data": {
            "level": level,
            "message": message,
            "dedup_key": dedup_key,
            # Which instinct wrote the line, so the Log tab can label it and
            # link back to the tile. Flat routines key their dedup on their own
            # id; a check routine and the drive-mode pair pass theirs.
            "context": {"automation": automation or dedup_key},
        },
    }


# -- builders ----------------------------------------------------------------

_MSG_LOW_WATER = "Fresh water is running low. Top up when you next get the chance."
_MSG_FULL_GREY_WATER = (
    "Grey-water tank is getting full. Empty it when you next get the chance."
)
_MSG_LOW_BATTERY = "Leisure battery is getting low. Consider charging soon."


def _low_battery_message(soc: str, threshold: Any) -> str:
    """The log line with the live reading and the level that tripped it. `soc`
    is either a rendered number or a Jinja expression (the generated automation
    renders it at fire time); the wording lives here once for both paths."""
    return (
        f"Leisure battery is at {soc}%, below your {threshold}% warning level. "
        "Consider charging soon."
    )


def _live_low_battery_message(hass: Any, config: dict[str, Any]) -> str | None:
    value = _num(hass, _battery_soc_entity)
    if value is None:
        return None
    return _low_battery_message(f"{value:.0f}", config.get("threshold", 20))
# The generic routine-level message (the card row and the fire-on-enable
# path). The generated automation names the actual battery instead, via
# `_discharge_disabled_message`.
_MSG_DISCHARGE_DISABLED = (
    "A leisure battery has discharging switched off, so the van cannot "
    "draw any power from it. Switch discharging back on in the battery's "
    "own app."
)
_MSG_PACK_IMBALANCE = (
    "Your leisure batteries are a long way apart on charge, so one of them is "
    "doing far more of the work than the others. Check every battery's "
    "isolator, fuse and discharge switch."
)
_MSG_CELL_IMBALANCE = (
    "A leisure-battery cell is out of balance with the others. If this "
    "persists after a full charge, have the battery checked."
)
_MSG_COLD_CHARGING = (
    "Charge current is flowing into a near-freezing leisure battery. "
    "Charging below freezing can permanently damage lithium cells."
)
_MSG_DISK_WARN = (
    "Storage is filling up. Consider clearing old backups or data soon."
)
_MSG_DISK_ALERT = (
    "Storage is critically low. Free up space now to avoid problems."
)
_MSG_CONTROLLER_WARM = (
    "The Sensa controller is running warm. Check nothing is blocking its vents "
    "or its fan."
)
_MSG_CONTROLLER_HOT = (
    "The Sensa controller is too hot and may start slowing itself down. Check "
    "its fan and airflow now."
)
_MSG_CONTROLLER_SHUTDOWN = (
    "The Sensa controller is shutting down to protect itself from heat. Let it "
    "cool, check its fan and vents, then power it back on."
)
_MSG_ENGINE_SHORE = (
    "Engine started while still connected to shore power. Unplug the cable "
    "before driving off."
)
# Richer presentation for the engine-on-shore ALARM (banner + detail card).
# Shared by the generated action AND the fire-if-already-true path so both raise
# the same rich alarm. Static "Connected" value (the rule only fires when shore
# power is in), so no live entity is needed.
_ALARM_ENGINE_SHORE: dict[str, Any] = {
    "title": "Engine started",
    "guidance": "Please unplug external power before departure.",
    "context": [
        {"label": "Shore power", "value": "Connected", "icon": "mdi:power-plug"},
    ],
}
# The tank routines default to Alert, but a tank needing attention isn't the
# same class of problem as driving off still plugged in — so their managed
# alarm carries an amber banner instead of `raise_alarm`'s default red.
# Applied in BOTH places an alarm can be raised for a CuratedAutomation: the
# builder's generated action (`severity=` on `_action_block`) and the
# fire-if-already-true path (`alarm_extras`, read by `_fire_now`). Setting only
# one leaves the two paths disagreeing on colour.
_ALARM_TANK: dict[str, Any] = {"severity": "warning"}
# Cell imbalance is a "have the battery checked" issue, not an emergency, so
# its managed alarm is amber too. Same both-paths rule as _ALARM_TANK: applied
# to the generated action AND carried as `alarm_extras` for the fire-on-enable
# path, so the two raises agree on presentation.
_ALARM_CELL_IMBALANCE: dict[str, Any] = {
    "title": "Battery cell imbalance",
    "guidance": (
        "If this continues after a full charge, contact your battery supplier."
    ),
    "severity": "warning",
}
# Cold charging is damage-imminent, so this one keeps raise_alarm's default
# red severity (no `severity` key) — unlike the amber tank/imbalance alarms.
# Neither pack-level battery alarm is an emergency: nothing is being damaged,
# the van is simply carrying capacity it cannot reach. Amber, like the tank
# and cell-imbalance alarms, and applied to BOTH raise paths (the generated
# action's `severity=` and the fire-on-enable `alarm_extras`).
_ALARM_PACK_IMBALANCE: dict[str, Any] = {
    "title": "Batteries out of balance",
    "guidance": (
        "One battery is not pulling its weight. Check that every battery's "
        "isolator and fuse are on, and that discharging is switched on in "
        "the battery's own app."
    ),
    "severity": "warning",
}
_ALARM_DISCHARGE_DISABLED: dict[str, Any] = {
    "title": "Battery output turned off",
    "guidance": (
        "Switch discharging back on in the battery's own app. Until you do, "
        "the van cannot draw any power from this battery, however full it is."
    ),
    "severity": "warning",
}
_ALARM_COLD_CHARGING: dict[str, Any] = {
    "title": "Battery too cold to charge",
    "guidance": (
        "Stop charging until the battery warms up. If Sensa switched any "
        "chargers off, it will restore them once the battery is warm."
    ),
}


def _build_low_fresh_water(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    entity = _fresh_water_entity(hass)
    if not entity:
        return None
    threshold = config.get("threshold", 20)
    return {
        "id": "sensa_nomad_low_fresh_water",
        "alias": "Sensa: Low fresh water",
        "mode": "single",
        # Crossing below (runtime) OR already-below at HA start. The condition
        # gates the start trigger so it only fires when actually low.
        "trigger": [
            {"platform": "numeric_state", "entity_id": entity, "below": threshold},
            _HA_START,
        ],
        "condition": [
            {"condition": "numeric_state", "entity_id": entity, "below": threshold},
        ],
        "action": [
            _action_block(
                action, "warning", _MSG_LOW_WATER, "low_fresh_water", **_ALARM_TANK,
            )
        ],
    }


def _build_full_grey_water(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    entity = _grey_water_entity(hass)
    if not entity:
        return None
    threshold = config.get("threshold", 80)
    return {
        "id": "sensa_nomad_full_grey_water",
        "alias": "Sensa: Full grey water",
        "mode": "single",
        # Crossing above (runtime) OR already-above at HA start — the mirror of
        # the low-water alert: grey water FILLS, so we warn when it rises past
        # the threshold rather than drops below.
        "trigger": [
            {"platform": "numeric_state", "entity_id": entity, "above": threshold},
            _HA_START,
        ],
        "condition": [
            {"condition": "numeric_state", "entity_id": entity, "above": threshold},
        ],
        "action": [
            _action_block(
                action, "warning", _MSG_FULL_GREY_WATER, "full_grey_water",
                **_ALARM_TANK,
            )
        ],
    }


def _build_low_battery(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    entity = _battery_soc_entity(hass)
    if not entity:
        return None
    threshold = config.get("threshold", 20)
    return {
        "id": "sensa_nomad_low_battery",
        "alias": "Sensa: Low leisure battery",
        "mode": "single",
        "trigger": [
            {"platform": "numeric_state", "entity_id": entity, "below": threshold},
            _HA_START,
        ],
        "condition": [
            {"condition": "numeric_state", "entity_id": entity, "below": threshold},
        ],
        "action": [
            _action_block(
                action, "warning",
                _low_battery_message(
                    "{{ states('" + entity + "') | float(0) | round(0) | int }}",
                    threshold,
                ),
                "low_battery",
            )
        ],
    }


# The customer's threshold field is in whole millivolts (a 100 mV spread is far
# easier to reason about than 0.1); the Cell Voltage Delta sensors read volts.
_CELL_IMBALANCE_DEFAULT_MV = 100


def _cell_imbalance_threshold_v(config: dict[str, Any]) -> float:
    return config.get("threshold", _CELL_IMBALANCE_DEFAULT_MV) / 1000


def _build_cell_imbalance(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    entities = _cell_delta_entities(hass)
    if not entities:
        return None
    threshold = _cell_imbalance_threshold_v(config)
    # Cell spread spikes briefly under heavy load and at the top of charge, so
    # the runtime trigger requires the imbalance to HOLD for 10 minutes. The
    # HA-start path has no sustain (matching the other routines'
    # already-true-at-boot semantics). One trigger covers every pack; with
    # several packs the start condition is an OR so ANY imbalanced pack fires.
    triggers = [
        {
            "platform": "numeric_state",
            "entity_id": entity,
            "above": threshold,
            "for": {"minutes": 10},
        }
        for entity in entities
    ]
    conditions = [
        {"condition": "numeric_state", "entity_id": entity, "above": threshold}
        for entity in entities
    ]
    return {
        "id": "sensa_nomad_cell_imbalance",
        "alias": "Sensa: Battery cell imbalance",
        "mode": "single",
        "trigger": [*triggers, _HA_START],
        "condition": (
            conditions
            if len(conditions) == 1
            else [{"condition": "or", "conditions": conditions}]
        ),
        "action": [
            _action_block(
                action, "warning", _MSG_CELL_IMBALANCE, "cell_imbalance",
                **_ALARM_CELL_IMBALANCE,
            )
        ],
    }


def _cell_imbalance_active(hass: Any, config: dict[str, Any]) -> bool:
    """True when ANY pack's cell spread currently exceeds the threshold (the
    fire-on-enable check — instantaneous by design, no 10-minute sustain)."""
    threshold = _cell_imbalance_threshold_v(config)
    for entity in _cell_delta_entities(hass):
        value = _num(hass, entity)
        if value is not None and value > threshold:
            return True
    return False


# --- pack-level battery instincts (a bank of two or more packs) --------------
# `cell_imbalance` above watches cells drift apart WITHIN one pack. The two
# below watch whole PACKS drift apart from each other, which no cell-delta
# sensor can ever see: every cell inside an unused pack stays in perfect
# balance with its neighbours while the pack itself sits at 100% and its
# bank-mates carry the entire load.
#
# Why state of charge and not voltage, which is the more accurate reading:
# packs wired in parallel are bonded at the busbar, so every pack that is
# actually conducting reads the same terminal voltage whatever its charge, and
# LiFePO4 is flat enough that even a 40-point charge gap is worth about 0.15 V,
# inside cable drop and cross-BMS calibration error. Voltage separates the
# packs only under load. State of charge does not need to be ACCURATE here,
# only comparable: the fault this catches is a 45-point gap, which no amount of
# coulomb-count drift invents.

_PACK_IMBALANCE_DEFAULT_PCT = 25
# Two hours. A bank genuinely does converge on its own after a full charge, and
# a newly added pack starts life out of step, so this is deliberately slow: it
# is a "something is wired or switched wrong" alert, never an urgent one.
_PACK_IMBALANCE_SUSTAIN = {"hours": 2}
# States excluded before the spread is taken, so a pack that has dropped off
# Bluetooth is "could not ask", never a reading of its own.
_PACK_SOC_DEAD_STATES = ("unknown", "unavailable")


def _pack_imbalance_threshold(config: dict[str, Any]) -> float:
    return config.get("threshold", _PACK_IMBALANCE_DEFAULT_PCT)


def _pack_imbalance_template(entities: list[str], threshold: float) -> str:
    """The spread test, as one HA template.

    HA has no "compare these entities to each other" trigger, so the whole
    comparison lives in a template trigger. Packs whose state is not a number
    are dropped BEFORE max/min, and the surviving count must still be at least
    two, so a bank where every pack but one has gone quiet can never
    manufacture a spread out of the one reading left. Plain Jinja tests only
    (`reject('in', ...)`, `map('float')`), so nothing here depends on an HA
    template extension that a future core release could rename.
    """
    ids = ", ".join(f"'{eid}'" for eid in entities)
    dead = ", ".join(f"'{state}'" for state in _PACK_SOC_DEAD_STATES)
    return (
        "{% set soc = expand(" + ids + ") | map(attribute='state')"
        " | reject('in', [" + dead + "]) | map('float') | list %}"
        "{{ soc | count > 1 and soc | max - soc | min > " + str(threshold) + " }}"
    )


def _build_pack_imbalance(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """Alert when the packs in a multi-pack bank drift apart on charge.

    Builds nothing for a single-pack van: there is nothing to compare against,
    and a routine that can never fire has no business on the card.

    A template trigger with the sustain catches the drift opening up while HA
    runs; `_HA_START` plus the matching (sustain-free) condition catches a bank
    already apart at boot, which is the ordinary case, since the customer has
    been living with it for weeks before Sensa is fitted.
    """
    entities = _bms_pack_soc_entities(hass)
    if len(entities) < 2:
        return None
    template = _pack_imbalance_template(entities, _pack_imbalance_threshold(config))
    return {
        "id": "sensa_nomad_pack_imbalance",
        "alias": "Sensa: Batteries out of balance",
        "mode": "single",
        "trigger": [
            {
                "platform": "template",
                "value_template": template,
                "for": dict(_PACK_IMBALANCE_SUSTAIN),
            },
            _HA_START,
        ],
        "condition": [{"condition": "template", "value_template": template}],
        "action": [
            _action_block(
                action, "warning", _MSG_PACK_IMBALANCE, "pack_imbalance",
                **_ALARM_PACK_IMBALANCE,
            )
        ],
    }


def _pack_imbalance_active(hass: Any, config: dict[str, Any]) -> bool:
    """True when the live bank is already apart, the fire-on-enable check.

    Instantaneous by design (no two-hour sustain), and the same drop-then-count
    rule as the template: fewer than two readable packs is "could not ask".
    """
    threshold = _pack_imbalance_threshold(config)
    values = [
        value
        for value in (_num(hass, eid) for eid in _bms_pack_soc_entities(hass))
        if value is not None
    ]
    return len(values) > 1 and max(values) - min(values) > threshold


# The discharge MOSFET is reported read-only and is set in the battery vendor's
# own app, so this instinct never actuates: it tells the customer where to go.
# Only DISCHARGING is watched. The charge line is deliberately left alone, since
# every LiFePO4 BMS opens it by itself in the cold, which is correct behaviour
# and already has its own instinct (`cold_charging`); watching it would put a
# banner on the tab every frosty morning for something Sensa already explains.
_DISCHARGE_OFF_STATE = "off"
# Five minutes. The flag is a real reported field rather than an inferred one,
# so this only has to ride out a BLE reconnect repopulating the entity.
_DISCHARGE_DISABLED_SUSTAIN = {"minutes": 5}


def _discharge_disabled_message(label: str) -> str:
    return (
        f"{label} has discharging switched off, so the van cannot draw any "
        "power from it and the other batteries are carrying the whole load. "
        "Switch discharging back on in the battery's own app."
    )


def _discharge_disabled_alarm_extras(label: str) -> dict[str, Any]:
    return {
        **_ALARM_DISCHARGE_DISABLED,
        "context": [
            {"label": "Battery", "value": label, "icon": "mdi:battery-off-outline"}
        ],
    }


def _build_discharge_disabled(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """Alert when a pack's "Discharging Allowed" flag reads off.

    One automation covering every pack, in the per-entity if-block shape
    ("Sensor gone quiet", the security pair) so each battery gets its own named
    message and its own `discharge_disabled_<entity_id>` alarm key: two
    switched-off packs alert separately, and a re-fire refreshes the right one.

    A STATE instinct, so there is deliberately NO from-state guard. At boot the
    flag goes `unavailable` -> `off` as the BMS first reports, and that
    transition is exactly the one that must alarm, because a pack switched off
    weeks ago will never transition again. `state: "off"` cannot match
    `unavailable` or `unknown`, so a pack that has dropped off Bluetooth is
    "could not ask" and holds the previous answer rather than reading as a
    confident no. The hourly backstop covers any reconnect path the state
    trigger does not see.
    """
    candidates = _bms_discharge_flag_entities(hass)
    if not candidates:
        return None
    sustain = dict(_DISCHARGE_DISABLED_SUSTAIN)
    return {
        "id": "sensa_nomad_discharge_disabled",
        "alias": "Sensa: Battery output turned off",
        "mode": "single",
        "trigger": [
            {
                "platform": "state",
                "entity_id": [eid for eid, _label in candidates],
                "to": _DISCHARGE_OFF_STATE,
                "for": sustain,
            },
            {"platform": "time_pattern", "minutes": 0},
        ],
        "action": [
            {
                "if": [
                    {
                        "condition": "state",
                        "entity_id": eid,
                        "state": _DISCHARGE_OFF_STATE,
                        "for": sustain,
                    }
                ],
                "then": [
                    _action_block(
                        action, "warning", _discharge_disabled_message(label),
                        f"{ALARM_KEY_DISCHARGE_DISABLED}_{eid}",
                        **_discharge_disabled_alarm_extras(label),
                    )
                ],
            }
            for eid, label in candidates
        ],
    }


def _discharge_disabled_active(hass: Any, config: dict[str, Any]) -> bool:
    """True when any pack has read off past the sustain, the fire-on-enable
    check. Honours the sustain via `last_changed` (as "Sensor gone quiet" does)
    so enabling the instinct in the post-boot window cannot page on a flag that
    has only just populated. A stub state with no `last_changed` counts."""
    states = getattr(hass, "states", None)
    if states is None:
        return False
    now = datetime.now(timezone.utc)
    minutes = _DISCHARGE_DISABLED_SUSTAIN["minutes"]
    for eid, _label in _bms_discharge_flag_entities(hass):
        st = states.get(eid)
        if st is None or str(getattr(st, "state", "")) != _DISCHARGE_OFF_STATE:
            continue
        last_changed = getattr(st, "last_changed", None)
        if last_changed is None or now - last_changed >= timedelta(minutes=minutes):
            return True
    return False


# Cold-charging protection. Detection is per BMS pack: charge current flowing
# (Pack Current is signed, positive = charging) while ANY of the pack's temp
# probes reads below the threshold — board/MOSFET probes run warmer than the
# cells, so they can never false-cold. Both runtime triggers carry a short
# sustain to debounce one-off readings; restore waits for ALL packs to warm
# a margin past the threshold, then holds for longer so the chargers don't
# flap around the boundary.
_COLD_CHARGE_MIN_CURRENT_A = 0.5
_COLD_CHARGE_DEFAULT_C = 2
_COLD_CHARGE_RESTORE_MARGIN_C = 3
_COLD_CHARGE_SUSTAIN = {"minutes": 2}
_COLD_CHARGE_RESTORE_SUSTAIN = {"minutes": 10}


def _cold_charge_cutoff_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("charge_cutoff", 1))


def _build_cold_charging(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    packs = _bms_pack_signals(hass)
    if not packs:
        return None
    threshold = config.get("threshold", _COLD_CHARGE_DEFAULT_C)
    triggers: list[dict[str, Any]] = []
    pack_conditions: list[dict[str, Any]] = []
    for pack in packs:
        triggers.append({
            "platform": "numeric_state",
            "entity_id": pack["current"],
            "above": _COLD_CHARGE_MIN_CURRENT_A,
            "for": _COLD_CHARGE_SUSTAIN,
        })
        cold_any = [
            {"condition": "numeric_state", "entity_id": temp, "below": threshold}
            for temp in pack["temps"]
        ]
        for temp in pack["temps"]:
            triggers.append({
                "platform": "numeric_state",
                "entity_id": temp,
                "below": threshold,
                "for": _COLD_CHARGE_SUSTAIN,
            })
        pack_conditions.append({
            "condition": "and",
            "conditions": [
                {
                    "condition": "numeric_state",
                    "entity_id": pack["current"],
                    "above": _COLD_CHARGE_MIN_CURRENT_A,
                },
                cold_any[0] if len(cold_any) == 1
                else {"condition": "or", "conditions": cold_any},
            ],
        })
    actions = [
        _action_block(
            action, "warning", _MSG_COLD_CHARGING, "cold_charging",
            **_ALARM_COLD_CHARGING,
        )
    ]
    # The cut-off step is included purely on the customer's toggle: whether
    # any controllable charger actually exists is the protect service's own
    # question (cold_charge.py) — answering it here too would be the same
    # predicate written twice, and the service is a safe no-op with nothing
    # to control.
    if _cold_charge_cutoff_enabled(config):
        actions.append({"service": "sensa_nomad.cold_charge_protect"})
    return {
        "id": "sensa_nomad_cold_charging",
        "alias": "Sensa: Cold-charging protection",
        "mode": "single",
        "trigger": [*triggers, _HA_START],
        "condition": (
            pack_conditions
            if len(pack_conditions) == 1
            else [{"condition": "or", "conditions": pack_conditions}]
        ),
        "action": actions,
    }


def _build_cold_charge_restore(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """Companion automation: put the chargers back once EVERY pack has warmed
    a margin past the threshold and held there. The restore service no-ops
    when protect never changed anything, so this only needs the toggle + the
    packs to exist."""
    if not _cold_charge_cutoff_enabled(config):
        return None
    packs = _bms_pack_signals(hass)
    if not packs:
        return None
    warm_at = config.get("threshold", _COLD_CHARGE_DEFAULT_C) + (
        _COLD_CHARGE_RESTORE_MARGIN_C
    )
    all_temps = [temp for pack in packs for temp in pack["temps"]]
    return {
        "id": "sensa_nomad_cold_charge_restore",
        "alias": "Sensa: Cold-charging restore",
        "mode": "single",
        "trigger": [
            *(
                {
                    "platform": "numeric_state",
                    "entity_id": temp,
                    "above": warm_at,
                    "for": _COLD_CHARGE_RESTORE_SUSTAIN,
                }
                for temp in all_temps
            ),
            _HA_START,
        ],
        # A numeric_state condition with an entity LIST is AND across them:
        # every probe on every pack must be warm before charging returns.
        "condition": [
            {
                "condition": "numeric_state",
                "entity_id": all_temps,
                "above": warm_at,
            },
        ],
        "action": [{"service": "sensa_nomad.cold_charge_restore"}],
    }


def _cold_charging_active(hass: Any, config: dict[str, Any]) -> bool:
    """True when ANY pack is charging while ANY of its probes is cold (the
    fire-on-enable check — instantaneous by design, no sustain)."""
    threshold = config.get("threshold", _COLD_CHARGE_DEFAULT_C)
    for pack in _bms_pack_signals(hass):
        current = _num(hass, pack["current"])
        if current is None or current <= _COLD_CHARGE_MIN_CURRENT_A:
            continue
        for temp in pack["temps"]:
            value = _num(hass, temp)
            if value is not None and value < threshold:
                return True
    return False


async def _cold_charging_on_fire(hass: Any, config: dict[str, Any]) -> None:
    """Fire-on-enable side effect: the generated YAML action only runs when
    the automation triggers, so enabling the routine while already
    cold-charging runs the cut-off here."""
    if not _cold_charge_cutoff_enabled(config):
        return
    from .cold_charge import async_cold_charge_protect

    await async_cold_charge_protect(hass)


# The Sensa App's disk-usage % sensor (System tab Resources) — always present,
# so this routine is available on every install with no device requirement.
_DISK_USE_ENTITY = "sensor.sensa_nomad_disk_use"


def _disk_use_entity(_hass: Any) -> str | None:
    return _DISK_USE_ENTITY


def _normalise_disk_thresholds(config: dict[str, Any]) -> dict[str, Any]:
    """Order the two disk thresholds so warn <= alert. The config fields let a
    customer set Alert below Warn; left as-is the alarm branch (listed first in
    the `choose`) would sound below the warning level, and the card would show
    thresholds the reverse of what fires. Assigning the lower figure to warn and
    the higher to alert keeps every consumer honest and consistent."""
    lo = config.get("warn_threshold", 80)
    hi = config.get("alert_threshold", 90)
    if lo > hi:
        lo, hi = hi, lo
    return {**config, "warn_threshold": lo, "alert_threshold": hi}


def _build_disk_space(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """Two-level storage routine: WARN above `warn_threshold` (default 80%),
    ALERT (managed alarm) above `alert_threshold` (default 90%). Both levels are
    fixed behaviour (not the customer warn/alert chooser — `supports_alarm` is
    False), so a `choose` picks the alarm when critical, else the warning."""
    entity = _DISK_USE_ENTITY
    # Thresholds arrive already ordered (warn <= alert) via the routine's
    # `normalise_config` hook in `resolve_state`, so build / is_active / the card
    # all agree and the alarm can't sit below the warn level. See
    # `_normalise_disk_thresholds`.
    warn = config.get("warn_threshold", 80)
    alert = config.get("alert_threshold", 90)
    return {
        "id": "sensa_nomad_disk_space",
        "alias": "Sensa: Disk space",
        "mode": "single",
        "trigger": [
            {"platform": "numeric_state", "entity_id": entity, "above": warn},
            {"platform": "numeric_state", "entity_id": entity, "above": alert},
            _HA_START,
        ],
        # Above `alert` → alarm; else above `warn` → warning. The higher branch
        # is listed first so `choose` matches it when both conditions hold.
        "action": [
            {
                "choose": [
                    {
                        "conditions": [{
                            "condition": "numeric_state",
                            "entity_id": entity, "above": alert,
                        }],
                        "sequence": [_action_block(
                            ACTION_ALARM, "error", _MSG_DISK_ALERT, "disk_space",
                            title="Storage almost full", severity="error",
                        )],
                    },
                    {
                        "conditions": [{
                            "condition": "numeric_state",
                            "entity_id": entity, "above": warn,
                        }],
                        "sequence": [_action_block(
                            ACTION_WARN, "warning", _MSG_DISK_WARN, "disk_space",
                        )],
                    },
                ],
            }
        ],
    }


# --- "Core temperature" — the Sensa box's own SoC running hot. Two
# levels, exactly like the storage routine: warn, then alert.
#
# MEASURED ON THE APPLIANCE (Pi 5 + active cooler, 2026-08-15), because the
# thresholds are only meaningful against real numbers:
#
#   * Idle sits around 41 C.
#   * Four cores at 100% for three minutes PLATEAUS at 60-61 C, holding the full
#     2400 MHz the whole time, with `get_throttled` flat at 0x0.
#   * The fan's own trip points are 50 / 60 / 67.5 / 75 C (read from
#     `thermal_zone0`), and the 60 C step is visible in the burn as the fan
#     jumping 2847 -> 4713 rpm.
#   * The kernel's critical trip, where it powers the board off itself, is
#     110 C.
#
# So a healthy box working as hard as it can never reaches 65 C. The defaults
# below are set against that: the WARN means "hotter than this box gets under
# any real load, so something is wrong with the cooling", and the ALERT sits up
# where the firmware actually starts capping the clock. Both are customer
# adjustable.
_CONTROLLER_TEMP_ENTITY = "sensor.sensa_nomad_cpu_temperature"
# Their full-load plateau is ~61 C, so this is "above anything normal" without
# being so tight that a hot day in a sealed locker trips it constantly.
_CONTROLLER_WARN_C = 65
# Where a Pi 5 begins soft-limiting its clock. NB: not verified on the rig (the
# box will not get there with a working fan), so treat it as the documented
# figure rather than a measured one.
_CONTROLLER_ALERT_C = 80
# Clean-shutdown level. ABOVE throttling, so we only get here once the chip has
# already tried to save itself and lost, and BELOW the kernel's 110 C critical
# trip. That gap is the entire point: the kernel's trip is an abrupt cut with
# the filesystem mounted, which is how memory cards get corrupted, whereas this
# is an ordered poweroff. Nothing here reaches 95 C with working cooling (the
# measured full-load plateau is 61 C), so in practice this fires only for a
# failed fan or a blocked vent.
_CONTROLLER_SHUTDOWN_C = 95
# How long the warn level must HOLD before it says anything. Board temperature
# moves fast (the burn above put on 11 C in fifteen seconds), and short bursts
# are normal work: a dashboard rebuild, a discovery scan, an OTA unpacking. Five
# minutes above the line is a cooling problem rather than a busy minute.
_CONTROLLER_SUSTAIN = {"minutes": 5}


def _controller_celsius_above(entity: str, threshold: float) -> str:
    """Template: is the controller hotter than `threshold` DEGREES CELSIUS?

    The thresholds are Celsius facts about the silicon, but the sensor's STATE
    is not Celsius: it declares a native degC and a temperature device_class, so
    Home Assistant republishes it in the customer's own unit system. On a US
    customary box a perfectly cool 48 degC publishes as 118.4 degF, which is
    above the 95 shutdown line, and the van powers itself off. That happened on
    192.168.1.226 at 02:16 on 2026-09-16: the recorded reading was 118.4 with
    every neighbouring sample between 44 and 51.

    So the READING is converted here, at evaluation time. Never bake the
    thresholds into the customer's unit instead: the unit system can be changed
    afterwards with nothing to trigger a regen, which is the same trap the
    dashboards hit and the reason `shared/temperature.ts` exists.

    A non-numeric reading (`unavailable` on every restart, `unknown` before the
    first poll) falls to -999, below every threshold. That is load-bearing, not
    defensive: HA's `numeric_state` TRIGGER treats non-numeric as "not matching"
    and ARMS on it, so the first real reading after any restart fires as though
    the temperature had just crossed the line. A template trigger reading false
    while the sensor is away has nothing to arm.
    """
    return (
        f"{{% set s = states('{entity}') | float(-999) %}}"
        f"{{% set u = state_attr('{entity}', 'unit_of_measurement') %}}"
        "{% set c = (s - 32) * 5 / 9 if u in ['°F', 'F'] else s %}"
        f"{{{{ c > {threshold} }}}}"
    )


def _controller_celsius(hass: Any) -> float | None:
    """The controller reading in CELSIUS, or None when it cannot be read.

    The Python-side twin of `_controller_celsius_above`, for the surfaces that
    ask in Python rather than in a template. One conversion rule, written once:
    the reading carries its own unit and the thresholds are always Celsius.
    """
    entity = _controller_temp_entity(hass)
    if not entity:
        return None
    value = _num(hass, entity)
    if value is None:
        return None
    unit = (_attrs(hass, entity) or {}).get("unit_of_measurement")
    if unit in ("°F", "F"):
        return (value - 32) * 5 / 9
    return value


def _controller_temperature_active(hass: Any, config: dict[str, Any]) -> bool:
    """Is the controller above its WARN line right now? Celsius both sides."""
    celsius = _controller_celsius(hass)
    if celsius is None:
        return False
    return celsius > config.get("warn_threshold", _CONTROLLER_WARN_C)


def _controller_temp_entity(hass: Any) -> str | None:
    """The controller temperature sensor, or None where it does not exist.

    Appliance only: the App declines to report board readings on DIY and
    Enthusiast, so the entity is simply absent there and the routine shows as
    "needs ..." rather than sitting broken.
    """
    states = getattr(hass, "states", None)
    if states is None:
        return None
    try:
        if states.get(_CONTROLLER_TEMP_ENTITY) is None:
            return None
    except Exception:  # noqa: BLE001
        return None
    return _CONTROLLER_TEMP_ENTITY


def _normalise_controller_thresholds(config: dict[str, Any]) -> dict[str, Any]:
    """Order the thresholds so warn <= alert <= shutdown.

    Same reasoning as `_normalise_disk_thresholds`: the branches are listed
    hottest-first in the `choose`, so a level set out of order would fire at the
    wrong temperature and the card would show thresholds that are not what
    happens. Three levels here rather than two, so they are SORTED rather than
    pair-swapped, and the stake is higher: a shutdown level that ended up below
    the warn would power the box off at the temperature meant only to mention
    it.
    """
    lo = config.get("warn_threshold", _CONTROLLER_WARN_C)
    mid = config.get("alert_threshold", _CONTROLLER_ALERT_C)
    hi = config.get("shutdown_threshold", _CONTROLLER_SHUTDOWN_C)
    lo, mid, hi = sorted((lo, mid, hi))
    return {
        **config,
        "warn_threshold": lo,
        "alert_threshold": mid,
        "shutdown_threshold": hi,
    }


def _build_controller_temperature(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """Two-level controller-temperature routine, mirroring `_build_disk_space`.

    Thresholds arrive already ordered via the routine's `normalise_config` hook.
    """
    entity = _controller_temp_entity(hass)
    if not entity:
        return None
    warn = config.get("warn_threshold", _CONTROLLER_WARN_C)
    alert = config.get("alert_threshold", _CONTROLLER_ALERT_C)
    shutdown = config.get("shutdown_threshold", _CONTROLLER_SHUTDOWN_C)
    # A sustain on the WARN so a brief spike (a dashboard rebuild, a discovery
    # scan, an OTA unpacking) does not nag. The hotter levels have none: by then
    # the cooling is losing and the delay would only cost reaction time.
    return {
        "id": "sensa_nomad_controller_temperature",
        "alias": "Sensa: Core temperature",
        "mode": "single",
        # TEMPLATE triggers, not `numeric_state`: the thresholds are Celsius and
        # the sensor's state is in the customer's unit system. See
        # `_controller_celsius_above` for what a raw comparison did to a US
        # customary box.
        "trigger": [
            {
                "platform": "template",
                "value_template": _controller_celsius_above(entity, warn),
                "for": _CONTROLLER_SUSTAIN,
            },
            {
                "platform": "template",
                "value_template": _controller_celsius_above(entity, alert),
            },
            {
                "platform": "template",
                "value_template": _controller_celsius_above(entity, shutdown),
            },
            _HA_START,
        ],
        "action": [
            {
                "choose": [
                    # Hottest first, so `choose` takes the most serious branch
                    # that matches. The shutdown branch RAISES THE ALARM TOO,
                    # before asking for the poweroff: the alarm is what survives
                    # into the log and tells somebody why the box went down.
                    {
                        "conditions": [{
                            "condition": "template",
                            "value_template": _controller_celsius_above(
                                entity, shutdown,
                            ),
                        }],
                        "sequence": [
                            _action_block(
                                ACTION_ALARM, "error", _MSG_CONTROLLER_SHUTDOWN,
                                "controller_temperature",
                                title="Controller shutting down",
                                guidance=(
                                    "The controller got too hot to keep running "
                                    "and is powering itself off to avoid damage. "
                                    "Let it cool, check its fan and vents, then "
                                    "power it back on."
                                ),
                                severity="error",
                            ),
                            {
                                "service": "sensa_nomad.request_shutdown",
                                "data": {
                                    "action": "poweroff",
                                    "reason": (
                                        "controller temperature above "
                                        f"{shutdown}C"
                                    ),
                                },
                            },
                        ],
                    },
                    {
                        "conditions": [{
                            "condition": "template",
                            "value_template": _controller_celsius_above(
                                entity, alert,
                            ),
                        }],
                        "sequence": [_action_block(
                            ACTION_ALARM, "error", _MSG_CONTROLLER_HOT,
                            "controller_temperature",
                            title="Controller too hot",
                            guidance=(
                                "Check the controller's fan is spinning and its "
                                "vents are clear. It will slow itself down to "
                                "stay safe, so the system may feel sluggish "
                                "until it cools."
                            ),
                            severity="error",
                        )],
                    },
                    {
                        "conditions": [{
                            "condition": "template",
                            "value_template": _controller_celsius_above(
                                entity, warn,
                            ),
                        }],
                        "sequence": [_action_block(
                            ACTION_WARN, "warning", _MSG_CONTROLLER_WARM,
                            "controller_temperature",
                        )],
                    },
                ],
            }
        ],
    }


# --- Compartment watches: "Fridge watch" and "Freezer watch". A chilled
# compartment drifting above safe temperature (a door left ajar, or a
# failure).
#
# Which sensors each watches is NOT picked on the routine itself; it's a
# per-DEVICE opt-in, the "Fridge" / "Freezer" switch on the device's own
# Settings -> Devices row (`const.FRIDGE_FLAG` / `const.FREEZER_FLAG`, one
# ledger each in entry.options, same pattern as the header-temperature-chip
# opt-in). Each instinct just discovers every device carrying its flag via
# `dashboard._flagged_temperature_entities(flag)`, with no picker on the
# card and no limit on how many devices are watched, so neither instinct
# ever needs changing to cover a second or third compartment. `threshold`
# stays each routine's own config field: ONE shared alert temperature across
# every device carrying that flag. Both routines are ONE builder
# parameterised by a `_Compartment` row, so the fridge and freezer rules
# cannot drift apart.

# The routine-level generic message/alarm, used for the card row's default
# description and the fire-on-enable path, which (like "Sensor gone quiet")
# doesn't know which device tripped. The generated automation's per-device
# action uses `_compartment_message`/`_compartment_alarm_extras` instead,
# naming the sensor.
_MSG_COMPARTMENT_WATCH = (
    "A watched compartment is above a safe temperature. Check the door is "
    "shut and it is running."
)
# A warm compartment needs prompt attention but isn't damage-in-progress, so
# its managed alarm is amber like the tank alarms (raise_alarm defaults to
# red). Same both-paths rule as _ALARM_TANK: applied to the generated action
# AND carried as `alarm_extras` for the fire-on-enable path.
_ALARM_COMPARTMENT_WATCH: dict[str, Any] = {
    "title": "Too warm",
    "guidance": (
        "Check the door is properly closed and it has power. Chilled food "
        "spoils quickly once it warms."
    ),
    "severity": "warning",
}
_FRIDGE_DEFAULT_C = 8
_FREEZER_DEFAULT_C = -12
# Door openings spike the air reading briefly; the runtime trigger requires
# the temperature to HOLD above the threshold so opening the door to unload
# the shopping doesn't alarm. The HA-start path has no sustain (matching the
# other routines' already-true-at-boot semantics).
_COMPARTMENT_SUSTAIN = {"minutes": 10}
# Lower-cased unit strings that mark a sensor as a temperature even without
# `device_class: temperature` (mirrors the dashboard's _TEMPERATURE_UNITS).
_TEMPERATURE_UNITS = frozenset({"°c", "°f", "k"})


@dataclass(frozen=True)
class _Compartment:
    """One chilled-compartment routine: which Devices-row flag feeds it, its
    per-entity alarm-key prefix and its threshold field's °C range."""

    routine_id: str
    name: str
    flag: Any
    alarm_key: str
    default_c: float
    minimum_c: float
    maximum_c: float


_FRIDGE = _Compartment(
    "fridge_watch", "Fridge watch", FRIDGE_FLAG, ALARM_KEY_FRIDGE_WATCH,
    _FRIDGE_DEFAULT_C, 2, 15,
)
_FREEZER = _Compartment(
    "freezer_watch", "Freezer watch", FREEZER_FLAG, ALARM_KEY_FREEZER_WATCH,
    _FREEZER_DEFAULT_C, -25, 0,
)


def _plain_temperature_entities(hass: Any) -> list[str]:
    """Fallback thermometer scan: every `sensor.*` reporting a temperature
    (device_class or unit), with no environment-ownership filter. Used only
    when the dashboard context isn't stamped (tests / before the first
    dashboard build)."""
    out: list[str] = []
    for eid in _find_entities(hass, "sensor"):
        attrs = _attrs(hass, eid)
        unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
        if attrs.get("device_class") == "temperature" or unit in _TEMPERATURE_UNITS:
            out.append(eid)
    return sorted(out)


def _temperature_candidates(hass: Any) -> list[tuple[str, str]]:
    """(entity_id, label) for every temperature sensor that COULD be flagged
    Temperature Watch / Fridge / Freezer; used only to decide whether the
    three routines read available (`_temperature_watch_available`), since
    the pick itself happens on the device row, not here.

    Prefers the dashboard's environment-temperature set (the SAME allowlist
    as the header temperature chips, `dashboard._all_temperature_entities`),
    so cabin/fridge/outside probes count while battery, router and relay
    board temps don't. That helper reads the module context stamped at
    dashboard-gen time, so it's only trusted when it was stamped for THIS
    hass (never a stale one from another test); otherwise fall back to a
    plain any-temperature scan.
    """
    entities: list[str] = []
    try:
        from . import dashboard as _dashboard

        if getattr(_dashboard, "_dashboard_hass", None) is hass:
            entities = list(_dashboard._all_temperature_entities())
    except Exception:  # noqa: BLE001 - the picker must never break describe
        entities = []
    if not entities:
        entities = _plain_temperature_entities(hass)
    out: list[tuple[str, str]] = []
    for eid in entities:
        out.append((eid, _entity_label(hass, eid)))
    return out


def _flagged_entities(hass: Any, flag: Any) -> list[tuple[str, str]]:
    """(entity_id, label) for every device currently carrying `flag` (a
    `const.TemperatureFlag`): a routine's actual watch list. Reaches into
    `dashboard._flagged_temperature_entities(flag)`, trusted only when the
    dashboard context is stamped for THIS hass (mirrors
    `_temperature_candidates`'s identity-trust rule; unlike that fallback
    scan, there's no per-entity heuristic fallback here, since the opt-in is
    a customer choice, not a heuristic, so an unstamped hass genuinely means
    "nothing is watched yet")."""
    try:
        from . import dashboard as _dashboard

        if getattr(_dashboard, "_dashboard_hass", None) is not hass:
            return []
        return list(_dashboard._flagged_temperature_entities(flag))
    except Exception:  # noqa: BLE001 - build must never break on this
        return []


def _to_sensor_unit(hass: Any, celsius: float, entity_id: str) -> float:
    """A °C boundary converted to the SENSOR's native unit: a numeric_state
    trigger compares the raw state, so an °F probe (a Govee in a
    default-imperial phone app) needs the boundary in °F or it would trip
    permanently. Shared by build AND is_active so the two can't drift."""
    unit = str(
        _attrs(hass, entity_id).get("unit_of_measurement") or ""
    ).strip().lower()
    if unit == "°f":
        return round(celsius * 9 / 5 + 32, 2)
    if unit == "k":
        return round(celsius + 273.15, 2)
    return celsius


def _compartment_threshold(
    hass: Any, config: dict[str, Any], entity_id: str, comp: _Compartment,
) -> float:
    try:
        celsius = float(config.get("threshold", comp.default_c))
    except (TypeError, ValueError):
        celsius = float(comp.default_c)
    return _to_sensor_unit(hass, celsius, entity_id)


def _compartment_slots(
    hass: Any, config: dict[str, Any], comp: _Compartment,
) -> list[tuple[str, str, float]]:
    """(entity_id, label, threshold) for every device carrying the flag."""
    return [
        (entity, label, _compartment_threshold(hass, config, entity, comp))
        for entity, label in _flagged_entities(hass, comp.flag)
    ]


def _compartment_message(
    hass: Any, label: str, entity_id: str, threshold: float,
) -> str:
    """The log line with the live reading (rendered at fire time), the limit
    that tripped it and how long it has been over, all in the sensor's own
    unit, since `threshold` is already converted to it. Only the sustained
    trigger has held for the period; the HA-start path fires at once."""
    unit = str(_attrs(hass, entity_id).get("unit_of_measurement") or "").strip()
    reading = "{{ states('" + entity_id + "') | float(0) | round(1) }}"
    minutes = _COMPARTMENT_SUSTAIN["minutes"]
    held = (
        "{{ ' for over " + str(minutes) + " minutes' "
        "if trigger.platform == 'numeric_state' else '' }}"
    )
    return (
        f"{label} is at {reading}{unit}, above your {threshold:g}{unit} "
        f"limit{held}. Check the door is shut and it is running."
    )


def _compartment_alarm_extras(label: str) -> dict[str, Any]:
    return {**_ALARM_COMPARTMENT_WATCH, "title": f"{label} too warm"}


def _build_compartment_watch(
    hass: Any, config: dict[str, Any], action: str, comp: _Compartment,
) -> dict[str, Any] | None:
    """One automation covering every device carrying the flag, in the
    per-entity if-block shape "Sensor gone quiet" uses, so each device gets
    its own named message and its own dedup/alarm key (two fridges both
    running warm alert independently, and a re-fire refreshes the right
    one)."""
    slots = _compartment_slots(hass, config, comp)
    if not slots:
        # Nothing flagged yet, nothing to watch. The routine still reads
        # available (see `_temperature_watch_available`) so the threshold
        # field (and the pointer to the Devices row) shows.
        return None
    return {
        "id": f"sensa_nomad_{comp.routine_id}",
        "alias": f"Sensa: {comp.name}",
        "mode": "single",
        "trigger": [
            *[
                {
                    "platform": "numeric_state",
                    "entity_id": entity,
                    "above": threshold,
                    "for": _COMPARTMENT_SUSTAIN,
                }
                for entity, _label, threshold in slots
            ],
            _HA_START,
        ],
        "action": [
            {
                "if": [
                    {
                        "condition": "numeric_state",
                        "entity_id": entity, "above": threshold,
                    }
                ],
                "then": [
                    _action_block(
                        action, "warning",
                        _compartment_message(hass, label, entity, threshold),
                        f"{comp.alarm_key}_{entity}",
                        **_compartment_alarm_extras(label),
                    )
                ],
            }
            for entity, label, threshold in slots
        ],
    }


def _compartment_active(
    hass: Any, config: dict[str, Any], comp: _Compartment,
) -> bool:
    """True when ANY flagged device currently reads above the threshold (the
    fire-on-enable check: instantaneous by design, no sustain)."""
    for entity, _label, threshold in _compartment_slots(hass, config, comp):
        value = _num(hass, entity)
        if value is not None and value > threshold:
            return True
    return False


def _build_fridge_watch(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    return _build_compartment_watch(hass, config, action, _FRIDGE)


def _fridge_watch_active(hass: Any, config: dict[str, Any]) -> bool:
    return _compartment_active(hass, config, _FRIDGE)


def _build_freezer_watch(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    return _build_compartment_watch(hass, config, action, _FREEZER)


def _freezer_watch_active(hass: Any, config: dict[str, Any]) -> bool:
    return _compartment_active(hass, config, _FREEZER)


def _temperature_watch_available(hass: Any, config: dict[str, Any]) -> bool:
    """Available once ANY candidate thermometer exists; this only gates
    whether a routine's fields can be configured, since the actual watch
    list is picked on each device's own Devices row, not here. Shared by
    Temperature watch, Fridge watch and Freezer watch."""
    return bool(_temperature_candidates(hass))


# --- "Temperature watch": a watched temperature RISING or FALLING by more
# than X degC inside Y minutes (a cabin heating up in the sun with the dog
# inside, a compressor failing, a heater left running). The opt-in is the
# "Temperature Watch" switch on the device's Devices row
# (`const.TEMPERATURE_WATCH_FLAG`), discovered exactly as the compartment
# flags above are.
#
# HA has no "the reading Y minutes ago" for a trigger to compare against, so
# the arithmetic lives in `sensor.SensaTemperatureWatchSensor`
# (`const.TEMPERATURE_WATCH_SENSOR_ENTITY_ID`): it samples every flagged
# thermometer and publishes each one's change over the routine's window, in
# degC, under its `changes` attribute keyed by entity_id. The generated
# automation only compares those numbers, in one template trigger per
# flagged entity. Unprimed (fewer than Y minutes of samples since boot, or
# the probe unavailable) the entry is absent and reads 0 here: "could not
# ask", never "changing". Always degC on both ends, whatever the probe's own
# unit, because the sensor converts on ingest.
_MSG_TEMPERATURE_WATCH = (
    "A watched temperature is changing fast. Check the door, the power and "
    "anything heating or cooling it."
)
_ALARM_TEMPERATURE_WATCH: dict[str, Any] = {
    "title": "Temperature changing fast",
    "guidance": (
        "A fast change usually means a door left open, a power loss, or a "
        "heater or cooler running. Check it before food spoils or the cabin "
        "overheats."
    ),
    "severity": "warning",
}
TEMPERATURE_WATCH_DEFAULT_CHANGE_C = 4.0
TEMPERATURE_WATCH_DEFAULT_MINUTES = 30
# The longest window the routine offers, and so how much history the change
# sensor keeps per entity.
TEMPERATURE_WATCH_MAX_MINUTES = 240


def temperature_watch_window(config: dict[str, Any]) -> tuple[float, int]:
    """(change degC, minutes) from the routine's config, both clamped to
    something the change sensor can serve. Shared by build, is_active and
    the sensor, so the three cannot disagree on what "over Y minutes"
    means."""
    try:
        change = float(config.get("change", TEMPERATURE_WATCH_DEFAULT_CHANGE_C))
    except (TypeError, ValueError):
        change = TEMPERATURE_WATCH_DEFAULT_CHANGE_C
    if not change > 0:
        change = TEMPERATURE_WATCH_DEFAULT_CHANGE_C
    try:
        minutes = int(float(config.get("minutes", TEMPERATURE_WATCH_DEFAULT_MINUTES)))
    except (TypeError, ValueError):
        minutes = TEMPERATURE_WATCH_DEFAULT_MINUTES
    minutes = max(1, min(TEMPERATURE_WATCH_MAX_MINUTES, minutes))
    return change, minutes


def temperature_watch_window_minutes(options: dict[str, Any]) -> int:
    """The window the change sensor must serve, from the stored entry
    options (the routine's merged config, exactly as build/is_active see
    it)."""
    _enabled, config, _action = resolve_state(
        options, CURATED_AUTOMATIONS_BY_ID["temperature_watch"],
    )
    return temperature_watch_window(config)[1]


def _temperature_change_expr(entity_id: str) -> str:
    """Jinja for one entity's change over the window, in degC, read off the
    change sensor's `changes` attribute. Absent (unprimed, or no longer
    flagged) reads 0, which never crosses a threshold."""
    return (
        f"((state_attr('{TEMPERATURE_WATCH_SENSOR_ENTITY_ID}', "
        f"'{TEMPERATURE_WATCH_CHANGES_ATTR}') or {{}})"
        f".get('{entity_id}') | float(0))"
    )


def _template(expr: str) -> str:
    return "{{ " + expr + " }}"


def _temperature_watch_message(
    label: str, rising: bool, change: float, minutes: int,
) -> str:
    verb = "risen" if rising else "fallen"
    return (
        f"{label} has {verb} more than {change:g}°C in {minutes} minutes. "
        "Check the door, the power and anything heating or cooling it."
    )


def _temperature_watch_alarm_extras(label: str, rising: bool) -> dict[str, Any]:
    word = "rising" if rising else "falling"
    return {**_ALARM_TEMPERATURE_WATCH, "title": f"{label} {word} fast"}


def _build_temperature_watch(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """One automation covering every flagged device: a template trigger per
    entity on the magnitude of its change, and two if-blocks per entity (rose
    / fell) so the message says which way it went. Both directions share
    one dedup/alarm key per entity, so a compartment that warms and then
    recovers refreshes one alarm rather than stacking two. No `_HA_START`
    trigger: nothing is primed at boot."""
    watched = _flagged_entities(hass, TEMPERATURE_WATCH_FLAG)
    if not watched:
        return None
    change, minutes = temperature_watch_window(config)
    triggers = [
        {
            "platform": "template",
            "value_template": _template(
                f"{_temperature_change_expr(entity)} | abs >= {change:g}"
            ),
        }
        for entity, _label in watched
    ]
    actions: list[dict[str, Any]] = []
    for entity, label in watched:
        expr = _temperature_change_expr(entity)
        for rising, cond in (
            (True, f"{expr} >= {change:g}"),
            (False, f"{expr} <= -{change:g}"),
        ):
            actions.append({
                "if": [
                    {"condition": "template", "value_template": _template(cond)}
                ],
                "then": [
                    _action_block(
                        action, "warning",
                        _temperature_watch_message(label, rising, change, minutes),
                        f"{ALARM_KEY_TEMPERATURE_WATCH}_{entity}",
                        **_temperature_watch_alarm_extras(label, rising),
                    )
                ],
            })
    return {
        "id": "sensa_nomad_temperature_watch",
        "alias": "Sensa: Temperature watch",
        "mode": "single",
        "trigger": triggers,
        "action": actions,
    }


def _temperature_watch_active(hass: Any, config: dict[str, Any]) -> bool:
    """True when ANY flagged entity's published change already exceeds the
    threshold, either way (the fire-on-enable check)."""
    change, _minutes = temperature_watch_window(config)
    changes = (
        _attrs(hass, TEMPERATURE_WATCH_SENSOR_ENTITY_ID).get(
            TEMPERATURE_WATCH_CHANGES_ATTR
        ) or {}
    )
    for entity, _label in _flagged_entities(hass, TEMPERATURE_WATCH_FLAG):
        try:
            value = float(changes.get(entity))
        except (TypeError, ValueError):
            continue
        if abs(value) >= change:
            return True
    return False


# --- "Sensor gone quiet" — a battery-powered wireless sensor stops reporting
# (a dead coin cell, out of range), and with it whatever relies on that sensor
# silently stops watching. The instinct that guards the other instincts: it
# covers the fridge-watch thermometer, the header temperature chips, and any
# future environment probe, with no per-routine wiring.

_MSG_SENSOR_QUIET = (
    "A wireless sensor has stopped reporting. Its battery may need replacing. "
    "Instincts that rely on it cannot watch while it is quiet."
)
# A quiet sensor needs attention but nothing is being damaged, so its managed
# alarm is amber like the tanks. Same both-paths rule as _ALARM_TANK: the
# generated per-sensor actions AND the routine-level fire-on-enable path
# (`alarm_extras`) must agree on presentation.
_ALARM_SENSOR_QUIET: dict[str, Any] = {
    "title": "Sensor gone quiet",
    "guidance": (
        "Check the sensor's battery and that it is still in range. Instincts "
        "that rely on it cannot watch while it is quiet."
    ),
    "severity": "warning",
}
_QUIET_STATES = ["unavailable", "unknown"]
_QUIET_DEFAULT_MINUTES = 30


def _quiet_minutes(config: dict[str, Any]) -> int:
    try:
        return int(config.get("quiet_minutes", _QUIET_DEFAULT_MINUTES))
    except (TypeError, ValueError):
        return _QUIET_DEFAULT_MINUTES


def _registry_temperature_entities(hass: Any) -> list[str] | None:
    """Temperature `sensor.*` entities per the entity REGISTRY, or None when
    the registry is unreachable (partial test stubs) so the caller can fall
    back to a state scan.

    None, not `[]`: "the registry answered and nothing qualifies" is a real
    answer and must be honoured, while "could not ask" is the only case that
    earns the fallback. Conflating the two shipped as a bug — on a van whose
    only temperature entities were our own diagnostic ones, the registry
    filtered them out, the empty list read as "could not ask", and the
    unfiltered state scan handed them straight back, so "Sensor gone quiet"
    told the customer to replace the battery in a software sensor.

    The registry — not the live state — is load-bearing here: the moment a
    wireless sensor dies, HA strips `device_class`/`unit_of_measurement` from
    its `unavailable` state, so a state-attribute scan silently drops exactly
    the sensor this instinct exists to catch, and the next regen would rebuild
    the watch without it. Registry fields survive the death. Diagnostic/config
    category entities are skipped (a Z2M `device_temperature` MCU probe is not
    a sensor the customer placed anywhere, and neither is our own CPU probe)."""
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        items = ent_reg.entities
        # A real registry's `.entities` is an EntityRegistryItems, a Mapping
        # (proven against HA 2026.8). Anything else is a stub standing in for
        # a registry we could not actually reach — a bare Mock iterates empty
        # and would otherwise pose as a registry that answered "nothing".
        if not isinstance(items, Mapping):
            return None
        entities = list(items.values())
    except Exception:  # noqa: BLE001 - registry lookup is best-effort
        return None
    out: list[str] = []
    for ent in entities:
        eid = str(getattr(ent, "entity_id", "") or "")
        if not eid.startswith("sensor."):
            continue
        if getattr(ent, "disabled_by", None) is not None:
            continue
        if getattr(ent, "entity_category", None) is not None:
            continue
        device_class = str(
            getattr(ent, "original_device_class", None)
            or getattr(ent, "device_class", None)
            or ""
        ).lower()
        unit = str(
            getattr(ent, "unit_of_measurement", None) or ""
        ).strip().lower()
        if "temperature" in device_class or unit in _TEMPERATURE_UNITS:
            out.append(eid)
    return sorted(out)


def _environment_allowlist(hass: Any) -> set[str] | None:
    """Entities of the environment devices in the customer's Devices list, or
    None when the dashboard context isn't stamped for THIS hass.
    Registry/device based, so a dead sensor stays in it.

    Read from `_environment_device_entity_map` (devices with a Sensa row), not
    the Climate-dial allowlist: that one also takes every Govee / Ruuvi HA
    config entry with no Sensa row, so a tag the customer had removed from
    Devices kept alarming "has stopped reporting" every hour."""
    try:
        from . import dashboard as _dashboard

        if getattr(_dashboard, "_dashboard_hass", None) is hass:
            out: set[str] = set()
            for entity_ids in _dashboard._environment_device_entity_map().values():
                out |= entity_ids
            return out
    except Exception:  # noqa: BLE001, S110
        pass
    return None


def _quiet_watch_entities(hass: Any) -> list[tuple[str, str]]:
    """(entity_id, label) pairs the quiet watch covers: every environment
    temperature sensor, resolved registry-first (see
    `_registry_temperature_entities` for why state attributes can't be
    trusted), filtered to environment-owned devices when the dashboard
    allowlist is available so pack/router/board temps don't page anyone."""
    entities = _registry_temperature_entities(hass)
    if entities is not None:
        # The registry answered, so its answer stands even when it is empty:
        # nothing qualifying means there is genuinely nothing to watch, and
        # falling through to the unfiltered state scan would hand back the
        # diagnostic entities the registry pass just excluded.
        allowed = _environment_allowlist(hass)
        if allowed is not None:
            entities = [eid for eid in entities if eid in allowed]
    else:
        # Registry unreachable (partial test stubs): a state scan sees the
        # same world the registry would, minus dead sensors' stripped
        # attributes — acceptable only as the fallback.
        entities = _plain_temperature_entities(hass)
    out: list[tuple[str, str]] = []
    for eid in entities:
        out.append((eid, _entity_label(hass, eid)))
    return out


def _quiet_message(label: str) -> str:
    return (
        f"{label} has stopped reporting. Its battery may need replacing. "
        "Instincts that rely on it cannot watch while it is quiet."
    )


def _quiet_alarm_extras(label: str) -> dict[str, Any]:
    return {
        **_ALARM_SENSOR_QUIET,
        "context": [{"label": "Sensor", "value": label, "icon": "mdi:signal-off"}],
    }


def _build_sensor_quiet(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """One automation covering every watched sensor, in the per-entity
    if-block shape the Engine Start routine uses — so each sensor gets its own
    named message and its own dedup/alarm key (two dead sensors both alert,
    and a re-fire refreshes the right alarm).

    Deliberately NO `_HA_START` trigger: at boot every wireless entity is
    briefly unavailable, and the state-condition's `for:` window starts at
    entity creation, so a start trigger could only ever fire spuriously or
    not at all. The runtime state trigger catches a sensor dying while HA is
    up; the hourly time_pattern backstop catches one that died while the van
    was powered down (no transition to observe); `set_automation`'s
    fire-on-enable check covers enable-while-already-quiet."""
    candidates = _quiet_watch_entities(hass)
    if not candidates:
        return None
    sustain = {"minutes": _quiet_minutes(config)}
    actions = [
        {
            "if": [
                {
                    "condition": "state",
                    "entity_id": eid,
                    "state": _QUIET_STATES,
                    "for": sustain,
                }
            ],
            "then": [
                _action_block(
                    action, "warning", _quiet_message(label),
                    f"{ALARM_KEY_SENSOR_QUIET}_{eid}",
                    **_quiet_alarm_extras(label),
                )
            ],
        }
        for eid, label in candidates
    ]
    return {
        "id": "sensa_nomad_sensor_quiet",
        "alias": "Sensa: Sensor gone quiet",
        "mode": "single",
        "trigger": [
            {
                "platform": "state",
                "entity_id": [eid for eid, _label in candidates],
                "to": _QUIET_STATES,
                "for": sustain,
            },
            {"platform": "time_pattern", "minutes": 0},
        ],
        "action": actions,
    }


def _sensor_quiet_active(hass: Any, config: dict[str, Any]) -> bool:
    """True when any watched sensor has been quiet past the sustain — the
    fire-on-enable check. Unlike the other routines' instantaneous checks,
    this one honours the sustain via `last_changed`: enabling the routine
    during the post-boot window (when every wireless entity is briefly
    unavailable) must not page the customer. A stub state with no
    last_changed counts as quiet (partial test environments)."""
    minutes = _quiet_minutes(config)
    states = getattr(hass, "states", None)
    if states is None:
        return False
    now = datetime.now(timezone.utc)
    for eid, _label in _quiet_watch_entities(hass):
        st = states.get(eid)
        if st is None or str(getattr(st, "state", "")) not in _QUIET_STATES:
            continue
        last_changed = getattr(st, "last_changed", None)
        if last_changed is None or now - last_changed >= timedelta(minutes=minutes):
            return True
    return False


# --- "Sensor battery low": a wireless sensor's own battery (a Zigbee contact
# sensor's coin cell, a BLE thermometer's AAA) reading below the customer's
# level. The early warning before "Sensor gone quiet": replace it while it
# still reports.
#
# Watches every `sensor.*` in the entity registry with device_class `battery`
# and a `%` unit, which is how Z2M, BTHome, Xiaomi BLE and Shelly all report a
# device battery. The same device class also carries the LEISURE battery, so
# the owners of big batteries are skipped by platform (our own BMS / Victron /
# DJI / Starlink entities are `sensa_nomad`; `victron_ble` and `bms_ble` are the
# native ones; `mobile_app` is the customer's phone), and the two leisure
# resolvers are subtracted on top as a belt for a platform not listed here.

_MSG_SENSOR_BATTERY_LOW = (
    "A wireless sensor's battery is running low. Replace it before the sensor "
    "stops reporting."
)
# Nothing is being damaged yet, so amber like the tanks. Same both-paths rule
# as _ALARM_TANK.
_ALARM_SENSOR_BATTERY_LOW: dict[str, Any] = {
    "title": "Sensor battery low",
    "guidance": (
        "Replace the sensor's battery soon. Once it goes flat the sensor stops "
        "reporting and the instincts that rely on it cannot watch."
    ),
    "severity": "warning",
}
_SENSOR_BATTERY_DEFAULT_PCT = 20
# A fridge or freezer thermometer sits in the cold, which drags its cell's
# reported level down long before it is flat, so it alarms only below this.
_SENSOR_BATTERY_CHILLED_PCT = 5
# An Aqara T1 maps 2850-3000 mV to 0-100%, so one report taken while the coin
# cell is under transmit load reads 0% and sticks until the next report about
# 55 minutes later. Low must hold longer than that before anything is raised.
_SENSOR_BATTERY_SUSTAIN = {"hours": 2}
_SENSOR_BATTERY_LOW_TRIGGER = "battery_low_held"
# A cell drains slowly, so a fall of more than this many points between two
# reports (100% to 0%) is a bad reading, not a battery: the 2-hour path
# ignores it. A low of any origin that then sits unchanged for
# `_SENSOR_BATTERY_SETTLED_H` is believed, which is also the HA-start and
# hourly backstop rule.
_SENSOR_BATTERY_MAX_DROP = 50
_SENSOR_BATTERY_SETTLED_H = 12
_SENSOR_BATTERY_EXCLUDED_PLATFORMS = frozenset({
    "sensa_nomad", "victron_ble", "bms_ble", "mobile_app",
})


def _registry_sensor_battery_entities(hass: Any) -> list[str] | None:
    """Device-battery `sensor.*` entities per the entity REGISTRY, or None
    when the registry is unreachable. None, not `[]`, for the same reason as
    `_registry_temperature_entities`: "nothing qualifies" is an answer, and
    only "could not ask" earns the state-scan fallback. Diagnostic category is
    deliberately NOT skipped here: Z2M files every device battery under it."""
    try:
        from homeassistant.helpers import entity_registry as er

        items = er.async_get(hass).entities
        if not isinstance(items, Mapping):
            return None
        entities = list(items.values())
    except Exception:  # noqa: BLE001 - registry lookup is best-effort
        return None
    out: list[str] = []
    for ent in entities:
        eid = str(getattr(ent, "entity_id", "") or "")
        if not eid.startswith("sensor."):
            continue
        if getattr(ent, "disabled_by", None) is not None:
            continue
        if str(getattr(ent, "platform", "") or "") in _SENSOR_BATTERY_EXCLUDED_PLATFORMS:
            continue
        device_class = str(
            getattr(ent, "original_device_class", None)
            or getattr(ent, "device_class", None)
            or ""
        ).lower()
        unit = str(getattr(ent, "unit_of_measurement", None) or "").strip()
        if device_class == "battery" and unit == "%":
            out.append(eid)
    return sorted(out)


def _state_sensor_battery_entities(hass: Any) -> list[str]:
    """The fallback when the registry cannot answer: a state-attribute scan,
    with our own entities skipped by their id prefix (no platform to read)."""
    out: list[str] = []
    for eid in hass.states.async_entity_ids("sensor"):
        if eid.startswith("sensor.sensa_nomad_"):
            continue
        attrs = _attrs(hass, eid)
        if (
            str(attrs.get("device_class") or "").lower() == "battery"
            and str(attrs.get("unit_of_measurement") or "").strip() == "%"
        ):
            out.append(eid)
    return sorted(out)


def _sensor_battery_watch_entities(hass: Any) -> list[tuple[str, str]]:
    """(entity_id, label) pairs the sensor-battery watch covers."""
    entities = _registry_sensor_battery_entities(hass)
    if entities is None:
        entities = _state_sensor_battery_entities(hass)
    leisure = set(_bms_pack_soc_entities(hass))
    soc = _battery_soc_entity(hass)
    if soc:
        leisure.add(soc)
    return [
        (eid, _sensor_battery_label(hass, eid))
        for eid in entities if eid not in leisure
    ]


def _sensor_battery_label(hass: Any, eid: str) -> str:
    """The sensor's name without the entity's own " Battery" suffix, so the
    message reads "Front Door battery", not "Front Door Battery battery"."""
    label = _entity_label(hass, eid)
    if label.lower().endswith(" battery"):
        label = label[: -len(" battery")].strip() or label
    return label


def _sensor_battery_threshold(config: dict[str, Any]) -> float:
    try:
        return float(config.get("threshold", _SENSOR_BATTERY_DEFAULT_PCT))
    except (TypeError, ValueError):
        return float(_SENSOR_BATTERY_DEFAULT_PCT)


def _chilled_battery_entities(hass: Any, entities: list[str]) -> set[str]:
    """The battery entities that share an HA device with a thermometer
    flagged Fridge or Freezer on its Devices row. Empty when the registry
    cannot answer, so every battery keeps the customer's level."""
    chilled = [
        eid
        for comp in (_FRIDGE, _FREEZER)
        for eid, _label in _flagged_entities(hass, comp.flag)
    ]
    if not chilled:
        return set()
    try:
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(hass)

        def _device(eid: str) -> str | None:
            ent = reg.async_get(eid)
            return getattr(ent, "device_id", None) if ent is not None else None

        devices = {d for d in map(_device, chilled) if d}
        return {eid for eid in entities if _device(eid) in devices}
    except Exception:  # noqa: BLE001 - registry lookup is best-effort
        return set()


def _sensor_battery_held(eid: str) -> str:
    """True when `eid`'s low reading is believed: either the sustained
    trigger fired for this sensor and it got low by a plausible fall (no more
    than `_SENSOR_BATTERY_MAX_DROP` points; a non-numeric previous state such
    as `unavailable` counts as implausible), or its state has not changed for
    `_SENSOR_BATTERY_SETTLED_H` hours."""
    return (
        "{% set s = expand('" + eid + "') | first %}"
        "{{ (trigger.id == '" + _SENSOR_BATTERY_LOW_TRIGGER + "'"
        " and trigger.entity_id == '" + eid + "'"
        " and trigger.from_state.state | float(1000)"
        " - trigger.to_state.state | float(0) <= "
        + str(_SENSOR_BATTERY_MAX_DROP) + ")"
        " or (s is defined and now() - s.last_changed >= timedelta(hours="
        + str(_SENSOR_BATTERY_SETTLED_H) + ")) }}"
    )


def _sensor_battery_message(label: str, level: str) -> str:
    return (
        f"{label} battery is at {level}%. Replace it before the sensor stops "
        "reporting."
    )


def _sensor_battery_alarm_extras(label: str) -> dict[str, Any]:
    return {
        **_ALARM_SENSOR_BATTERY_LOW,
        "context": [{"label": "Sensor", "value": label, "icon": "mdi:battery-low"}],
    }


def _build_sensor_battery_low(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """One automation covering every watched battery, in the per-entity
    if-block shape of "Sensor gone quiet", so each sensor gets its own named
    message and its own `sensor_battery_low_<eid>` alarm key.

    Every trigger re-evaluates every sensor, rather than branching on which
    one fired: a raise on a key already live refreshes in place without
    re-delivering, and the warn line is deduped for 24 h, so the repeat is
    free and nothing is missed. The hourly backstop is what makes that
    matter: a numeric_state trigger only fires on a CROSSING, so a sensor
    already low when the instinct is enabled, or replaced while HA was off,
    would otherwise wait for its next crossing that may never come. It also
    stands in for fire-on-enable, which would raise under the bare routine id
    where no producer could ever clear it.

    On the alarm action a battery back above the level CANCELS its alarm (the
    producer clears what it raised). `cancel_alarm` on a key that is not live
    is a no-op, so the clear branch needs no trigger gate either.

    A sensor on a Fridge or Freezer flagged device uses
    `_SENSOR_BATTERY_CHILLED_PCT` instead of the customer's level. A raise
    needs the low reading held for `_SENSOR_BATTERY_SUSTAIN` (see
    `_sensor_battery_held`); a clear is immediate."""
    candidates = _sensor_battery_watch_entities(hass)
    if not candidates:
        return None
    configured = _sensor_battery_threshold(config)
    chilled = _chilled_battery_entities(hass, [eid for eid, _label in candidates])
    thresholds = {
        eid: min(configured, _SENSOR_BATTERY_CHILLED_PCT) if eid in chilled else configured
        for eid, _label in candidates
    }
    groups: dict[float, list[str]] = {}
    for eid, _label in candidates:
        groups.setdefault(thresholds[eid], []).append(eid)
    actions: list[dict[str, Any]] = [
        {
            "if": [
                {"condition": "numeric_state", "entity_id": eid, "below": thresholds[eid]},
                {"condition": "template", "value_template": _sensor_battery_held(eid)},
            ],
            "then": [
                _action_block(
                    action, "warning",
                    _sensor_battery_message(
                        label,
                        "{{ states('" + eid + "') | float(0) | round(0) | int }}",
                    ),
                    f"{ALARM_KEY_SENSOR_BATTERY_LOW}_{eid}",
                    automation=ALARM_KEY_SENSOR_BATTERY_LOW,
                    **_sensor_battery_alarm_extras(label),
                )
            ],
        }
        for eid, label in candidates
    ]
    if action == ACTION_ALARM:
        actions.extend(
            {
                "if": [
                    {"condition": "numeric_state", "entity_id": eid, "above": thresholds[eid]},
                ],
                "then": [
                    {
                        "service": "sensa_nomad.cancel_alarm",
                        "data": {"key": f"{ALARM_KEY_SENSOR_BATTERY_LOW}_{eid}"},
                    }
                ],
            }
            for eid, _label in candidates
        )
    return {
        "id": "sensa_nomad_sensor_battery_low",
        "alias": "Sensa: Sensor battery low",
        "mode": "queued",
        "max": 25,
        "trigger": [
            *(
                trigger
                for level, eids in groups.items()
                for trigger in (
                    {
                        "platform": "numeric_state", "entity_id": eids,
                        "below": level, "for": dict(_SENSOR_BATTERY_SUSTAIN),
                        "id": _SENSOR_BATTERY_LOW_TRIGGER,
                    },
                    {"platform": "numeric_state", "entity_id": eids, "above": level},
                )
            ),
            _HA_START,
            {"platform": "time_pattern", "minutes": 0},
        ],
        "action": actions,
    }


# --- Tyre pressure (BLE TPMS) -------------------------------------------------
# One automation over every tyre sensor, in the per-entity if-block shape of
# "Sensor battery low": each tyre gets its own `tyre_pressure_<eid>` alarm key,
# raised when the tyre is below the customer's minimum or above their maximum
# and CANCELLED when it is back inside the range (the producer clears what it
# raised). The range is set in psi (default 20-50) and converted per sensor to
# the unit its entity reports in (HA shows the kPa native value in bar by
# default, but a customer can switch a sensor to psi or kPa).

_TYRE_DEFAULT_LOW_PSI = 20
_TYRE_DEFAULT_HIGH_PSI = 50
_TYRE_FROM_PSI: dict[str, float] = {"psi": 1.0, "bar": 1 / 14.5038, "kPa": 6.89476}
_ALARM_TYRE_PRESSURE: dict[str, Any] = {
    "title": "Tyre pressure",
    "guidance": (
        "Check the tyre with a gauge before driving on. A soft tyre overheats "
        "and can fail; an over-inflated one grips badly."
    ),
    "severity": "warning",
}


def _tyre_pressure_entities(hass: Any) -> list[tuple[str, str, str]]:
    """(pressure_eid, wheel_select_eid, label) for every tyre sensor, from the
    entity registry by the `_tpms_ble_<device>_` unique-id marker. The select
    may be "" (not registered yet); the label is the sensor's own name."""
    try:
        from homeassistant.helpers import entity_registry as er

        items = er.async_get(hass).entities
        if not isinstance(items, Mapping):
            return []
        entities = list(items.values())
    except Exception:  # noqa: BLE001 - registry lookup is best-effort
        return []
    by_uid = {str(getattr(e, "unique_id", "") or ""): e for e in entities}
    out: list[tuple[str, str, str]] = []
    for uid, ent in by_uid.items():
        if "_tpms_ble_" not in uid or not uid.endswith("_pressure"):
            continue
        if getattr(ent, "disabled_by", None) is not None:
            continue
        eid = str(getattr(ent, "entity_id", "") or "")
        select = by_uid.get(uid[: -len("_pressure")] + "_wheel_position")
        select_eid = str(getattr(select, "entity_id", "") or "") if select else ""
        label = _entity_label(hass, eid)
        if label.lower().endswith(" pressure"):
            label = label[: -len(" pressure")].strip() or label
        out.append((eid, select_eid, label))
    return sorted(out)


def _normalise_tyre_range(config: dict[str, Any]) -> dict[str, Any]:
    """Order the range so low <= high, whatever order the customer set them."""
    lo = config.get("low", _TYRE_DEFAULT_LOW_PSI)
    hi = config.get("high", _TYRE_DEFAULT_HIGH_PSI)
    if lo > hi:
        lo, hi = hi, lo
    return {**config, "low": lo, "high": hi}


def _tyre_unit(hass: Any, eid: str) -> str:
    unit = str(_attrs(hass, eid).get("unit_of_measurement") or "bar")
    return unit if unit in _TYRE_FROM_PSI else "bar"


def _tyre_wheel_label(select_eid: str, fallback: str) -> str:
    """Jinja naming the tyre by its Wheel Position ("Front left") when one is
    set, else the sensor's own name. Rendered at fire time, so moving a sensor
    to another wheel needs no rebuild."""
    if not select_eid:
        return fallback
    return (
        "{% set p = states('" + select_eid + "') %}"
        "{{ (p | replace('_', ' ') | capitalize) if p not in "
        "('not_set', 'unknown', 'unavailable', '') else '" + fallback.replace("'", "") + "' }}"
    )


def _build_tyre_pressure(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    tyres = _tyre_pressure_entities(hass)
    if not tyres:
        return None
    low_psi = float(config.get("low", _TYRE_DEFAULT_LOW_PSI))
    high_psi = float(config.get("high", _TYRE_DEFAULT_HIGH_PSI))
    actions: list[dict[str, Any]] = []
    for eid, select_eid, label in tyres:
        unit = _tyre_unit(hass, eid)
        factor = _TYRE_FROM_PSI[unit]
        low, high = round(low_psi * factor, 2), round(high_psi * factor, 2)
        key = f"{ALARM_KEY_TYRE_PRESSURE}_{eid}"
        wheel = _tyre_wheel_label(select_eid, label)
        reading = "{{ states('" + eid + "') }} " + unit
        extras = {
            **_ALARM_TYRE_PRESSURE,
            "context": [{"label": "Tyre", "value": label, "icon": "mdi:car-tire-alert"}],
        }
        actions.append({
            "if": [{"condition": "numeric_state", "entity_id": eid, "below": low}],
            "then": [_action_block(
                action, "warning",
                f"{wheel} tyre is at {reading}, below your {low_psi:g} psi minimum.",
                key, automation=ALARM_KEY_TYRE_PRESSURE, **extras,
            )],
        })
        actions.append({
            "if": [{"condition": "numeric_state", "entity_id": eid, "above": high}],
            "then": [_action_block(
                action, "warning",
                f"{wheel} tyre is at {reading}, above your {high_psi:g} psi maximum.",
                key, automation=ALARM_KEY_TYRE_PRESSURE, **extras,
            )],
        })
        if action == ACTION_ALARM:
            actions.append({
                "if": [{
                    "condition": "numeric_state", "entity_id": eid,
                    "above": low, "below": high,
                }],
                "then": [{"service": "sensa_nomad.cancel_alarm", "data": {"key": key}}],
            })
    return {
        "id": "sensa_nomad_tyre_pressure",
        "alias": "Sensa: Tyre pressure",
        "mode": "queued",
        "max": 25,
        # Every new reading re-checks every tyre (sensors report rarely, so a
        # plain state trigger is cheap). HA start and the hourly tick cover a
        # tyre already out of range when the instinct is enabled.
        "trigger": [
            {"platform": "state", "entity_id": [eid for eid, _s, _l in tyres]},
            _HA_START,
            {"platform": "time_pattern", "minutes": 0},
        ],
        "action": actions,
    }


# --- Zigbee2MQTT SECURITY binary-sensor instincts (vibration, water leak) —
# shared plumbing. Each covers every registered device of one security service
# class, reading its `binary_sensor.*` (Detected/Clear or Wet/Dry) via the SAME
# entity + resolver the Security tab tile uses
# (`dashboard._zigbee2mqtt_binary_entity` + `_SECURITY_KIND`), so an instinct
# and the tile can never disagree about which entity carries the state. One
# automation per class in the per-entity if-block shape (like "Sensor gone
# quiet") so a van with several sensors of the same kind gets a message naming
# which one fired.
#
# They are NOT the same shape underneath, and conflating them was a bug: a
# knock is momentary, water is a state that persists until it dries. See the
# `momentary` argument on `_build_security_binary_alarm`. Neither carries an
# `is_active` (enabling an instinct is not itself an event).


def _zigbee_security_binary_entities(
    hass: Any, service_class: str, device_class: str, default_label: str,
) -> list[tuple[str, str]]:
    """(entity_id, label) for every registered Zigbee2MQTT device of
    `service_class`, resolved to its `binary_sensor.*` of `device_class`.
    Empty when the dashboard context isn't stamped for THIS hass (tests /
    before the first dashboard build) — mirrors `_temperature_candidates` /
    `_quiet_watch_entities`. Shared by every security binary-sensor instinct
    so this reach-into-dashboard heuristic is written once."""
    try:
        from . import dashboard as _dashboard

        if getattr(_dashboard, "_dashboard_hass", None) is not hass:
            return []
        devices = list(_dashboard._dashboard_devices)
    except Exception:  # noqa: BLE001 - resolution must never break a build
        return []
    out: list[tuple[str, str]] = []
    for device in devices:
        if _dashboard._device_service_class(device) != service_class:
            continue
        # The Security tile's own resolver, so a sensor on the tab is never
        # missed by its instinct (Z2M today; a Shelly resolves here too).
        entity = _dashboard._security_binary_entity(device, frozenset({device_class}))
        if not entity:
            continue
        label = str(device.get("display_name") or device.get("name") or default_label)
        out.append((entity, label))
    return out


# Same reason, and the same two fail-open defaults, as
# `_SHORE_FROM_STATE_GUARD` below: a bare `to: "on"` state trigger also fires
# on `unavailable -> on` and `unknown -> on`, so a Z2M reconnect or an HA
# restart that lands on a sensor whose last published state was "on" reads as a
# fresh detection. Measured on the PROD van: 3 of 56 vibration fires in a week
# arrived that way (1x unavailable, 2x unknown). Guards the DETECT branch only
# so a clear still lands from any prior state.
#
# MOMENTARY instincts ONLY -- see `_build_security_binary_alarm`. On a STATE
# instinct this guard would suppress a real alarm: a van that boots with a leak
# sensor already wet goes `unknown -> on`, and swallowing that is a safety
# alarm failing CLOSED on "could not ask".
_SECURITY_FROM_STATE_GUARD: dict[str, Any] = {
    "condition": "template",
    "value_template": (
        "{{ trigger is not defined or (trigger.from_state.state "
        "| default('')) not in ['unknown', 'unavailable'] }}"
    ),
}


def _build_security_binary_alarm(
    candidates: list[tuple[str, str]],
    automation_id: str,
    alias: str,
    message_fn: Callable[[str], str],
    alarm_extras_fn: Callable[[str], dict[str, Any]],
    dedup_prefix: str,
    action: str,
    *,
    momentary: bool,
    only_in_mode: str | None = None,
) -> dict[str, Any] | None:
    """One automation covering every candidate entity: fires on ANY of them
    going "on", with a per-entity if-block so each carries its own message,
    dedup key and alarm context. Shared builder body for every security
    binary-sensor instinct.

    `only_in_mode` (a mode slug) gates the DETECT branch on that mode's
    active binary sensor reading "on", so the instinct is silent unless the
    van is in that mode. The Security tab tile is untouched by this: it reads
    the sensor directly and still shows Detected/Clear in every mode. The
    CLEAR branch is deliberately not gated, because an alarm raised in the
    mode must still clear if the customer leaves the mode before the sensor
    settles. Vibration passes `sentinel`: a knock while the family is aboard
    in Work or Movie mode is life, not an intrusion. Water leak passes
    nothing: water is doing damage whatever mode the van is in.

    On the ALARM action the sensor going back to "off" CANCELS that entity's
    alarm. Without it a momentary event raised a managed alarm that nothing
    ever cleared, so it sat "active" re-sounding every 5 minutes until the
    customer dismissed it by hand — seen on the PROD van, still nagging 2h15m
    after the sensor went quiet. `cold_charge` is the pattern: the producer
    that raises an alarm owns clearing it. The WARN action writes a one-shot
    log line, which has nothing to clear, so it gets no clear branch.

    `queued` rather than `single`: detect and clear are now two triggers on the
    same entities, and a clear arriving while a raise is still running must not
    be dropped.

    `momentary` splits the two kinds of security sensor, and it is the whole
    difference between them:

    - **Momentary** (vibration). The event is over in seconds and the alarm is
      cleared each time the sensor falls back to "off", so a later
      `unavailable -> on` from a Z2M reconnect raises a genuinely NEW alarm.
      It therefore carries `_SECURITY_FROM_STATE_GUARD`, and no HA-start
      trigger: being "on" at boot is not an event.
    - **State** (water leak). Wet is a CONDITION, not an event. It gets an
      HA-start trigger (sharing the `detected` id, so one condition covers
      both) so a van that boots with water already in it alarms, and it gets NO
      from-state guard, because suppressing `unknown -> on` there would silence
      exactly that case. The reconnect noise the guard exists to stop is
      harmless here anyway: the alarm only clears on a real `-> off`, and
      `raise_alarm` on a key that is already live refreshes it in place without
      re-delivering, so a flapping sensor that never actually dries out cannot
      re-notify."""
    if not candidates:
        return None
    entity_ids = [eid for eid, _label in candidates]

    def _detect_conditions(eid: str) -> list[dict[str, Any]]:
        conditions: list[dict[str, Any]] = [
            {"condition": "trigger", "id": "detected"},
            {"condition": "state", "entity_id": eid, "state": "on"},
        ]
        if momentary:
            conditions.append(dict(_SECURITY_FROM_STATE_GUARD))
        if only_in_mode:
            conditions.append({
                "condition": "state",
                "entity_id": mode_active_entity_id(only_in_mode),
                "state": "on",
            })
        return conditions

    actions: list[dict[str, Any]] = [
        {
            "if": _detect_conditions(eid),
            "then": [
                _action_block(
                    action, "warning", message_fn(label),
                    f"{dedup_prefix}_{eid}", **alarm_extras_fn(label),
                )
            ],
        }
        for eid, label in candidates
    ]
    if action == ACTION_ALARM:
        actions.extend(
            {
                "if": [
                    {"condition": "trigger", "id": "cleared"},
                    {"condition": "state", "entity_id": eid, "state": "off"},
                ],
                "then": [
                    {
                        "service": "sensa_nomad.cancel_alarm",
                        "data": {"key": f"{dedup_prefix}_{eid}"},
                    }
                ],
            }
            for eid, _label in candidates
        )
    triggers: list[dict[str, Any]] = [
        {
            "platform": "state",
            "entity_id": entity_ids,
            "to": "on",
            "id": "detected",
        },
        {
            "platform": "state",
            "entity_id": entity_ids,
            "to": "off",
            "id": "cleared",
        },
    ]
    if not momentary:
        # Shares the `detected` id deliberately: HA allows two triggers to carry
        # the same id, so the single `condition: trigger` on the detect branch
        # covers both without needing a list.
        triggers.append({**_HA_START, "id": "detected"})
    return {
        "id": automation_id,
        "alias": alias,
        "mode": "queued",
        "max": 25,
        "trigger": triggers,
        "action": actions,
    }


# --- "Vibration detected" — a security/tamper watch for the Aqara DJT11LM
# (and any future Zigbee2MQTT vibration sensor).

# Amber, not red. `raise_alarm` DEFAULTS an unset severity to critical, so
# leaving it off rendered every door slam as a red critical banner. Same rule
# as shore power: prompt attention, nothing being damaged.
_ALARM_VIBRATION: dict[str, Any] = {
    "title": "Vibration detected",
    "guidance": "Check the vehicle for signs of tampering or intrusion.",
    "severity": "warning",
}


def _vibration_message(label: str) -> str:
    return f"Vibration detected on {label}. Check the vehicle."


def _vibration_alarm_extras(label: str) -> dict[str, Any]:
    return {
        **_ALARM_VIBRATION,
        "context": [{"label": "Sensor", "value": label, "icon": "mdi:vibrate"}],
    }


def _vibration_binary_entities(hass: Any) -> list[tuple[str, str]]:
    return _zigbee_security_binary_entities(
        hass, "vibration_sensor", "vibration", "Vibration sensor",
    )


def _build_vibration_detected(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    return _build_security_binary_alarm(
        _vibration_binary_entities(hass), "sensa_nomad_vibration_detected",
        "Sensa: Vibration detected", _vibration_message, _vibration_alarm_extras,
        ALARM_KEY_VIBRATION_DETECTED, action,
        # A knock is over in seconds.
        momentary=True,
        # Alarm only while the van is unattended. The Security tab tile still
        # shows Detected/Clear in every mode; only the alarm is gated.
        only_in_mode=_MODE_SENTINEL,
    )


# --- "Geofence": the van has moved further than the customer's circle from
# where it was when Sentry went on (towed or stolen). The distance comes from
# `geofence.SensaGeofenceSensor`, which only reads a GPS bolted to the van.
#
# A STATE instinct, like leak: being outside the circle is a condition, so it
# carries an HA-start trigger, and the numeric_state trigger arming on
# `unknown` is wanted (a van towed while the box was off alarms on its first
# fix). It clears when the van is back inside or Sentry goes off; losing the
# GPS fix clears nothing, because the sensor carries its last reading forward.

_GEOFENCE_DEFAULT_M = 200
_MSG_GEOFENCE = (
    "The van has moved outside its geofence while in Sentry mode. "
    "Check where it is."
)
_ALARM_GEOFENCE: dict[str, Any] = {
    "title": "Van moved",
    "guidance": (
        "The van has left the area it was parked in while in Sentry mode. "
        "Check where it is."
    ),
    "severity": "critical",
}


def _build_geofence(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    from .device_tracker import van_gps_available

    if not van_gps_available(hass):
        return None
    radius = config.get("radius", _GEOFENCE_DEFAULT_M)
    sentry = mode_active_entity_id(_MODE_SENTINEL)
    actions: list[dict[str, Any]] = [
        {
            "if": [
                {"condition": "trigger", "id": "outside"},
                {
                    "condition": "numeric_state",
                    "entity_id": GEOFENCE_DISTANCE_ENTITY_ID,
                    "above": radius,
                },
                {"condition": "state", "entity_id": sentry, "state": "on"},
            ],
            "then": [
                _action_block(
                    action, "warning", _MSG_GEOFENCE, "geofence",
                    **_ALARM_GEOFENCE,
                )
            ],
        },
    ]
    if action == ACTION_ALARM:
        actions.append({
            "if": [{"condition": "trigger", "id": "inside"}],
            "then": [
                {"service": "sensa_nomad.cancel_alarm", "data": {"key": "geofence"}}
            ],
        })
    return {
        "id": "sensa_nomad_geofence",
        "alias": "Sensa: Geofence",
        "mode": "queued",
        "max": 10,
        "trigger": [
            {
                "platform": "numeric_state",
                "entity_id": GEOFENCE_DISTANCE_ENTITY_ID,
                "above": radius,
                "id": "outside",
            },
            {**_HA_START, "id": "outside"},
            {
                "platform": "numeric_state",
                "entity_id": GEOFENCE_DISTANCE_ENTITY_ID,
                "below": radius,
                "id": "inside",
            },
            {"platform": "state", "entity_id": sentry, "to": "off", "id": "inside"},
        ],
        "action": actions,
    }


def _geofence_active(hass: Any, config: dict[str, Any]) -> bool:
    state = getattr(hass.states.get(mode_active_entity_id(_MODE_SENTINEL)), "state", None)
    if state != "on":
        return False
    value = _num(hass, GEOFENCE_DISTANCE_ENTITY_ID)
    return value is not None and value > config.get("radius", _GEOFENCE_DEFAULT_M)


# --- "Water leak detected" — a burst-pipe / ingress watch for the Aqara
# SJCGQ11LM and the SONOFF Zigbee leak sensor (any registered `leak_sensor`).

# Critical STATED, not inherited: water is actively doing damage, so this one
# genuinely wants the red banner. Spelled out because the unset default is
# critical too, and an implicit default reads as an oversight next to
# `_ALARM_VIBRATION`.
_ALARM_LEAK: dict[str, Any] = {
    "title": "Water leak detected",
    "guidance": "Check the sensor's location for water ingress or a burst pipe.",
    "severity": "critical",
}


def _leak_message(label: str) -> str:
    return f"Water leak detected on {label}. Check the vehicle."


def _leak_alarm_extras(label: str) -> dict[str, Any]:
    return {
        **_ALARM_LEAK,
        "context": [{"label": "Sensor", "value": label, "icon": "mdi:water-alert"}],
    }


def _leak_binary_entities(hass: Any) -> list[tuple[str, str]]:
    return _zigbee_security_binary_entities(
        hass, "leak_sensor", "moisture", "Water leak sensor",
    )


def _build_leak_detected(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    return _build_security_binary_alarm(
        _leak_binary_entities(hass), "sensa_nomad_leak_detected",
        "Sensa: Water leak detected", _leak_message, _leak_alarm_extras,
        ALARM_KEY_LEAK_DETECTED, action,
        # Wet is a condition, not an event: it stays true until it dries.
        momentary=False,
    )


# --- "Smoke detected" — a fire watch for the Shelly Plus Smoke (any registered
# `smoke_sensor`). A STATE instinct like leak: smoke persists until it clears,
# a van that boots full of smoke must alarm, and it is never mode-gated, since a
# fire matters most when someone is asleep aboard.
#
# The device sleeps and reports by dialling HA, so the binary_sensor only moves
# when the detector pushes; a detector that cannot reach HA on 8123 from its own
# segment sounds its siren and never reaches this.

_ALARM_SMOKE: dict[str, Any] = {
    "title": "Smoke detected",
    "guidance": "Get everyone out of the vehicle, then check for fire.",
    "severity": "critical",
}


def _smoke_message(label: str) -> str:
    return f"Smoke detected on {label}. Get everyone out and check for fire."


def _smoke_alarm_extras(label: str) -> dict[str, Any]:
    return {
        **_ALARM_SMOKE,
        "context": [{"label": "Sensor", "value": label, "icon": "mdi:smoke-detector"}],
    }


def _smoke_binary_entities(hass: Any) -> list[tuple[str, str]]:
    return _zigbee_security_binary_entities(
        hass, "smoke_sensor", "smoke", "Smoke detector",
    )


def _build_smoke_detected(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    return _build_security_binary_alarm(
        _smoke_binary_entities(hass), "sensa_nomad_smoke_detected",
        "Sensa: Smoke detected", _smoke_message, _smoke_alarm_extras,
        ALARM_KEY_SMOKE_DETECTED, action,
        # Smoke is a condition, not an event: it stays true until it clears.
        momentary=False,
    )


# --- "Circuit fault": a Shelly Pro 4PM circuit reporting overheating,
# overpowering, overvoltage or overcurrent. One alarm per circuit per fault, so
# two faults on two circuits are two alarms, each cleared by its own flag.

_ALARM_CIRCUIT_FAULT: dict[str, Any] = {
    "title": "Circuit fault",
    "guidance": "Check what is connected to this circuit.",
    "severity": "critical",
}


def _circuit_fault_message(label: str) -> str:
    return f"{label}. Check what is connected to that circuit."


def _circuit_fault_alarm_extras(label: str) -> dict[str, Any]:
    return {
        **_ALARM_CIRCUIT_FAULT,
        "context": [{"label": "Circuit", "value": label, "icon": "mdi:flash-alert"}],
    }


def _circuit_fault_entities(hass: Any) -> list[tuple[str, str]]:
    """(entity_id, label) for every protection flag on every registered
    Shelly's circuits, labelled the way the Switches tab names the circuit:
    "Overheating on Fridge (Shelly Pro 4PM)". Empty when the dashboard context
    isn't stamped for THIS hass, like `_zigbee_security_binary_entities`."""
    try:
        from homeassistant.helpers import entity_registry as er

        from . import dashboard as _dashboard
        from .vendors.shelly import CIRCUIT_FAULTS, shelly_channel_fault_entities

        if getattr(_dashboard, "_dashboard_hass", None) is not hass:
            return []
        devices = list(_dashboard._dashboard_devices)
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001 - resolution must never break a build
        return []
    out: list[tuple[str, str]] = []
    for device in devices:
        integration = (
            device.get("integration_type") or device.get("integration") or ""
        ).lower()
        if integration != "shelly":
            continue
        unit = str(device.get("display_name") or device.get("name") or "Shelly")
        labels = device.get("relay_labels") or {}
        for slot, fault, eid in shelly_channel_fault_entities(hass, ent_reg, device):
            circuit = labels.get(slot) or f"Channel {slot.rsplit('_', 1)[1]}"
            out.append((eid, f"{CIRCUIT_FAULTS[fault]} on {circuit} ({unit})"))
    return out


def _build_circuit_fault(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    return _build_security_binary_alarm(
        _circuit_fault_entities(hass), "sensa_nomad_circuit_fault",
        "Sensa: Circuit fault", _circuit_fault_message, _circuit_fault_alarm_extras,
        ALARM_KEY_CIRCUIT_FAULT, action,
        # A fault is a condition the device holds until it clears, not a
        # blip: a van that boots with one latched must still alarm.
        momentary=False,
    )


# --- "Shore power dropped" — hooked up, the campsite bollard/RCD trips, and
# you believe you are charging when you are not (invisible until morning).
# The inverse of the Engine Start shore-power check. A TRANSITION instinct,
# not a state one: being off shore power is the NORMAL state for most vans,
# so there is no _HA_START trigger (a van that boots off-grid must not
# alarm), no backstop, and no is_active fire-on-enable (enabling it while
# off-grid is not an event). A trip while HA itself is down is missed,
# accepted (HA runs 24/7 on the van).
#
# Omitting _HA_START is NOT on its own enough to keep this quiet at boot, and
# believing it was shipped a false "Shore power lost" on every restart (see
# `_SHORE_FROM_STATE_GUARD` below). Two conditions carry that weight instead.

_MSG_SHORE_DROPPED = (
    "Shore power has dropped while hooked up. The site supply may have "
    "tripped, so the batteries are no longer charging."
)
# Loss of supply needs prompt attention but nothing is being damaged, so the
# managed alarm is amber like the tanks (low battery alarms separately if the
# drain gets that far). Same both-paths rule as _ALARM_TANK.
_ALARM_SHORE_DROPPED: dict[str, Any] = {
    "title": "Shore power lost",
    "guidance": (
        "Check the campsite bollard or circuit breaker. Until the supply "
        "returns, the leisure battery is running the van."
    ),
    "context": [
        {"label": "Shore power", "value": "Disconnected",
         "icon": "mdi:power-plug-off"},
    ],
    "severity": "warning",
}
# Cut-offs are fixed, not config fields: the voltage bound sits well under
# any mains standard, and a tunable watt field would be a dead control on
# every Victron rig (voltage mode ignores it). The customer-meaningful knob
# is the sustain.
_SHORE_DROP_VOLTAGE_BELOW = 100
_SHORE_DROP_POWER_BELOW_W = 5
_SHORE_DROP_DEFAULT_MINUTES = 5

# HA's numeric_state TRIGGER treats `unavailable` / `unknown` as "does not
# match" and therefore ARMS on them (`helpers/condition.async_numeric_state`
# returns False for a non-numeric value rather than raising, and the trigger
# does `if not matching: armed_entities.add(...)`). So every restart arms this
# trigger, and the integration's first real reading — 0 V on a van that has
# never seen shore power — looks like the above→below crossing. Without this
# guard the instinct is an HA-start trigger in all but name.
#
# Fails OPEN twice over, same rule as the inverter and engine gates: `trigger
# is not defined` for a condition evaluated outside a trigger context (which
# RAISES on `trigger.…`, and a raising condition blocks a real alarm), and
# `| default('')` for a trigger that carries no `from_state`.
_SHORE_FROM_STATE_GUARD: dict[str, Any] = {
    "condition": "template",
    "value_template": (
        "{{ trigger is not defined or (trigger.from_state.state "
        "| default('')) not in ['unknown', 'unavailable'] }}"
    ),
}


def _shore_drop_signal(hass: Any) -> tuple[str, float] | None:
    """(entity_id, below-cutoff) for detecting a shore-supply drop.

    Prefers the AC input VOLTAGE (load-independent: ~230 V while the bollard
    is live, collapsing when it trips) over the shore POWER sensor, whose
    wattage legitimately idles near zero on a full battery with no AC loads
    (a 3am false alarm at the margin). Power is the fallback for the
    simulator / non-Victron rigs. One resolver shared by every consumer so
    the entity and its cutoff can't drift apart."""
    voltage = _shore_voltage_entity(hass)
    if voltage:
        return voltage, _SHORE_DROP_VOLTAGE_BELOW
    power = _shore_power_entity(hass)
    if power:
        return power, _SHORE_DROP_POWER_BELOW_W
    return None


def shore_power_connected(hass: Any) -> bool | None:
    """Whether the van is on shore power now, judged by the same signal and
    cutoff as the shore-drop instinct. None when there is no shore sensor or
    no reading: could not ask."""
    signal = _shore_drop_signal(hass)
    if not signal:
        return None
    entity, cutoff = signal
    try:
        value = float(hass.states.get(entity).state)
    except (AttributeError, TypeError, ValueError):
        return None
    return value >= cutoff


def _build_shore_power_dropped(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    signal = _shore_drop_signal(hass)
    if not signal:
        return None
    entity, cutoff = signal
    try:
        sustain_minutes = int(config.get("drop_minutes", _SHORE_DROP_DEFAULT_MINUTES))
    except (TypeError, ValueError):
        sustain_minutes = _SHORE_DROP_DEFAULT_MINUTES
    conditions: list[dict[str, Any]] = [
        {"condition": "numeric_state", "entity_id": entity, "below": cutoff},
        dict(_SHORE_FROM_STATE_GUARD),
    ]
    inverter = _inverter_state_entity(hass, entity)
    if inverter:
        # A MultiPlus that is switched OFF stops measuring its AC input and
        # publishes 0 V, which is indistinguishable from a tripped bollard on
        # this signal alone. Seen for real: a charger that ran bulk →
        # absorption → float overnight and then switched itself off raised
        # "Shore power lost" five minutes later.
        #
        # A template on the live state rather than a `state` condition, so
        # an `unavailable` or `unknown` inverter ALLOWS the alarm: "could
        # not ask" must never be handled as "told no", and this gate exists to
        # suppress a proven-off unit, not an unreadable one.
        conditions.append({
            "condition": "template",
            "value_template": (
                "{{ states('" + inverter + "') not in "
                + repr(list(INVERTER_OFF_STATES)) + " }}"
            ),
        })
    engine = _engine_rpm_entity(hass)
    if engine:
        # Driving away is the one LEGITIMATE way shore power vanishes
        # mid-sustain, so skip the alarm when the engine is PROVABLY running.
        # A template, not numeric_state, and the choice is load-bearing: the
        # WiCAN sleeps when parked (RPM `unavailable`), and unavailable must
        # mean "allow the alarm" — `float(0)` folds it to not-running, while
        # a numeric_state condition on a non-numeric state would suppress the
        # alarm at the campsite, the exact moment this instinct exists for.
        conditions.append({
            "condition": "template",
            "value_template": (
                "{{ (states('" + engine + "') | float(0)) <= 0 }}"
            ),
        })
    return {
        "id": "sensa_nomad_shore_power_dropped",
        "alias": "Sensa: Shore power dropped",
        "mode": "single",
        "trigger": [
            {
                "platform": "numeric_state",
                "entity_id": entity,
                "below": cutoff,
                "for": {"minutes": sustain_minutes},
            },
        ],
        "condition": conditions,
        "action": [
            _action_block(
                action, "warning", _MSG_SHORE_DROPPED, "shore_power_dropped",
                **_ALARM_SHORE_DROPPED,
            )
        ],
    }


# --- "Starter battery watch" — the leisure system is monitored to death
# while the battery that actually gets you home is ignored. Warn when the
# starter voltage sags while parked; signal from the SmartShunt's auxiliary
# sense wire (always live) or the OBD2 12V-rail PID (see
# `alerts._starter_voltage_entity`). A STATE instinct like low leisure
# battery: already-sagged at boot/enable should fire. No engine gate needed —
# with the engine running the alternator holds ~14 V, so the condition
# self-clears while driving, and a sustained sag WITH the engine running is
# a failing alternator, which deserves the alarm just as much.

_MSG_STARTER_LOW = (
    "Starter battery voltage is low. The engine may struggle to start; "
    "charge it or run the engine soon."
)
_ALARM_STARTER_LOW: dict[str, Any] = {
    "title": "Starter battery low",
    "guidance": (
        "Charge the starter battery or run the engine. If it keeps sagging "
        "while parked, something may be draining it."
    ),
    "severity": "warning",
}
_STARTER_DEFAULT_V = 11.9
# A cranking dip drops the voltage for seconds, and a big momentary load can
# sag it briefly — the runtime trigger requires the sag to HOLD. The HA-start
# path has no sustain (already-true-at-boot semantics, like the others).
_STARTER_SUSTAIN = {"minutes": 10}


def _build_starter_battery(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    entity = _starter_voltage_entity(hass)
    if not entity:
        return None
    threshold = config.get("threshold", _STARTER_DEFAULT_V)
    return {
        "id": "sensa_nomad_starter_battery_watch",
        "alias": "Sensa: Starter battery watch",
        "mode": "single",
        "trigger": [
            {
                "platform": "numeric_state",
                "entity_id": entity,
                "below": threshold,
                "for": _STARTER_SUSTAIN,
            },
            _HA_START,
        ],
        "condition": [
            {"condition": "numeric_state", "entity_id": entity, "below": threshold},
        ],
        "action": [
            _action_block(
                action, "warning", _MSG_STARTER_LOW, "starter_battery_watch",
                **_ALARM_STARTER_LOW,
            )
        ],
    }


# --- "Low fuel near a station" — the first routine to consume a Sensa Radar
# nearest-amenity sensor (Stage 1). Two conditions ANDed: the vehicle fuel gauge
# is below a level AND the nearest fuel station (from Radar) is within range.
RADAR_NEAREST_FUEL_ENTITY = "sensor.sensa_nomad_radar_nearest_fuel"
_MILES_TO_M = 1609.344
_MSG_LOW_FUEL_NEAR_STATION = (
    "Fuel is low and there's a fuel station within reach, a good moment to fill up."
)


def _radar_nearest_distance_m(hass: Any, entity_id: str) -> float | None:
    """Read the `distance_m` attribute off a Radar nearest-* sensor, or None."""
    try:
        return float(_attrs(hass, entity_id).get("distance_m"))
    except (TypeError, ValueError):
        return None


def _build_low_fuel_near_station(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    fuel_entity = _fuel_level_entity(hass)
    if not fuel_entity:
        return None
    fuel_pct = config.get("fuel_threshold", 99)
    distance_m = float(config.get("distance_miles", 10)) * _MILES_TO_M
    radar = RADAR_NEAREST_FUEL_ENTITY
    return {
        "id": "sensa_nomad_low_fuel_near_station",
        "alias": "Sensa: Low fuel near a station",
        "mode": "single",
        # Fire when EITHER the fuel gauge drops below the level or the nearest
        # station comes within range (or it's already true at HA start); the two
        # conditions below then AND-gate the action. The Radar distance rides in
        # the sensor's `distance_m` attribute (metres), so match on that.
        "trigger": [
            {"platform": "numeric_state", "entity_id": fuel_entity, "below": fuel_pct},
            {
                "platform": "numeric_state",
                "entity_id": radar,
                "attribute": "distance_m",
                "below": distance_m,
            },
            _HA_START,
        ],
        "condition": [
            {"condition": "numeric_state", "entity_id": fuel_entity, "below": fuel_pct},
            {
                "condition": "numeric_state",
                "entity_id": radar,
                "attribute": "distance_m",
                "below": distance_m,
            },
        ],
        "action": [
            _action_block(
                action, "warning", _MSG_LOW_FUEL_NEAR_STATION, "low_fuel_near_station",
                title="Low fuel nearby",
                guidance="Consider topping up at the fuel station ahead.",
            )
        ],
    }


def _low_fuel_near_active(hass: Any, config: dict[str, Any]) -> bool:
    """True when fuel is below the level AND a station is within range NOW — the
    fire-immediately-on-enable path."""
    fuel = _num(hass, _fuel_level_entity)
    dist = _radar_nearest_distance_m(hass, RADAR_NEAREST_FUEL_ENTITY)
    return (
        fuel is not None
        and fuel < config.get("fuel_threshold", 99)
        and dist is not None
        and dist < float(config.get("distance_miles", 10)) * _MILES_TO_M
    )


# --- "Drive mode when moving" — the first routine whose action is to CHANGE
# THE VAN'S MODE rather than to say something.
#
# Both halves trigger on the one movement signal
# (`binary_sensor.sensa_nomad_vehicle_moving`, see `movement.py`), which folds
# OBD2 road speed and the resolved GPS into a single answer. Nothing about
# "what counts as moving" is expressed here: the driveway problem, the
# thresholds and the two-source preference all live in the detector, so this
# routine and anything else that later wants the answer cannot disagree about
# it.
#
# Neither half carries `_HA_START`, and that is deliberate — the exception to
# the catalog's usual "fire once at boot if already true" rule. These act on
# the customer's chosen mode, and the movement sensor restores its pre-restart
# value, so an HA-start branch would yank a customer who had hand-picked Sleep
# into Drive purely because the last thing the van did before a reboot was
# drive. Mode changes belong on real movement TRANSITIONS. The startup
# mode-restore already puts the van back in whatever mode it was in.
_MSG_DRIVE_MODE = "The van is on the move, so Drive mode is now on."
_MSG_PARKED_MODE = "The van has stopped, so Parked mode is now on."

# Frozen mode slugs (see `modes_catalog.ModeDef.slug` — renaming one would
# orphan every generated scene).
_MODE_DRIVING = "driving"
_MODE_PARKED = "parking"
_MODE_SENTINEL = "sentinel"


def _park_on_stop_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("park_on_stop", 1))


def _build_drive_mode(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """Van starts moving -> Drive.

    Unconditional on the current mode: whatever the van was set to, once it is
    under way the cab's concerns win. That asymmetry with the Parked half below
    is the point — moving is an observed fact about the vehicle, stopping is
    only the ABSENCE of one.

    None on a van with neither an OBD2 adapter nor a nameable GPS source: the
    trigger could never fire, and generating a dead automation would be worse
    than the card saying what is missing.
    """
    if not _drive_mode_available(hass, config):
        return None
    return {
        "id": "sensa_nomad_drive_mode",
        "alias": "Sensa: Drive mode when moving",
        "mode": "single",
        "trigger": [
            # No `from:`, so the first ever detection (`unknown` -> `on`, a van
            # that has just gained a speed source mid-journey) counts too.
            {
                "platform": "state",
                "entity_id": MOVEMENT_BINARY_SENSOR_ENTITY_ID,
                "to": "on",
            },
        ],
        "action": [
            {
                "service": "sensa_nomad.activate_mode",
                "data": {"mode": _MODE_DRIVING},
            },
            _action_block(
                action, "info", _MSG_DRIVE_MODE, "drive_mode_moving",
                automation="drive_mode_when_moving",
            ),
        ],
    }


def _build_parked_mode(
    hass: Any, config: dict[str, Any], action: str = ACTION_WARN,
) -> dict[str, Any] | None:
    """Van stops -> Parked, but ONLY if it is still in Drive.

    The condition is the whole safety of this half. If the van is in some other
    mode, a person put it there during the journey and stopping is no reason to
    overrule them; only a van still sitting in the mode THIS routine selected
    is one whose mode nobody has since taken ownership of.

    It also covers the case the movement sensor cannot: a van whose speed
    sources both go dark keeps its last answer, so the OFF edge may simply never
    arrive. Nothing here fires on a guess.
    """
    if not _park_on_stop_enabled(config) or not _drive_mode_available(
        hass, config
    ):
        return None
    return {
        "id": "sensa_nomad_parked_mode",
        "alias": "Sensa: Parked mode when stopped",
        "mode": "single",
        "trigger": [
            {
                "platform": "state",
                "entity_id": MOVEMENT_BINARY_SENSOR_ENTITY_ID,
                "from": "on",
                "to": "off",
            },
        ],
        "condition": [
            {
                "condition": "state",
                "entity_id": mode_active_entity_id(_MODE_DRIVING),
                "state": "on",
            },
        ],
        "action": [
            {
                "service": "sensa_nomad.activate_mode",
                "data": {"mode": _MODE_PARKED},
            },
            _action_block(
                action, "info", _MSG_PARKED_MODE, "parked_mode_stopped",
                automation="drive_mode_when_moving",
            ),
        ],
    }


def _drive_mode_active(hass: Any, config: dict[str, Any]) -> bool:
    """The fire-on-enable check: is the van moving RIGHT NOW? `unknown` (no
    source has answered yet) is not moving — it is not an answer at all."""
    state = getattr(hass.states.get(MOVEMENT_BINARY_SENSOR_ENTITY_ID), "state", None)
    return state == "on"


def _drive_mode_available(hass: Any, config: dict[str, Any]) -> bool:
    """Available only where the van can actually tell whether it is moving.

    Without this the row would read as enabled and simply never do anything,
    which is worse than saying "needs an OBD2 adapter or a GPS source".
    """
    return movement_source_available(hass)


async def _drive_mode_on_fire(hass: Any, config: dict[str, Any]) -> None:
    """Enabled while the van is already moving: the generated YAML only runs on
    a trigger, so select Drive here too."""
    await hass.services.async_call(
        "sensa_nomad", "activate_mode", {"mode": _MODE_DRIVING}, blocking=False,
    )


# --- "Engine Start" — a TRIGGER routine (engine starts → run departure checks).
# The trigger is the engine-start EVENT; each Check below contributes one
# `if <condition> then <warn|alarm>` block. Add a departure check (steps, roof,
# handbrake…) by adding one Check to the routine's `checks` tuple.

def _engine_start_trigger(hass: Any) -> list[dict[str, Any]] | None:
    """The engine-start event: RPM rising above zero, PLUS an HA-start trigger so
    a routine enabled while the engine is already running (or the Pi rebooted
    mid-journey) still fires once. None when there's no engine sensor."""
    engine = _engine_rpm_entity(hass)
    if not engine:
        return None
    return [
        {"platform": "numeric_state", "entity_id": engine, "above": 0},
        _HA_START,
    ]


def _shore_connected_condition(
    hass: Any, config: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Check: the engine is (still) running AND shore power is connected. The
    engine re-check debounces a spurious RPM blip on the trigger — it mirrors the
    old engine-on-shore automation's action condition. None when either sensor
    is absent."""
    engine = _engine_rpm_entity(hass)
    shore = _shore_power_entity(hass)
    if not engine or not shore:
        return None
    return [
        {"condition": "numeric_state", "entity_id": engine, "above": 0},
        {"condition": "numeric_state", "entity_id": shore, "above": 0},
    ]


def _engine_on_shore_active(hass: Any, config: dict[str, Any]) -> bool:
    """True when the engine is running and shore power is connected NOW (the
    fire-on-enable check for the shore-power departure check)."""
    engine = _num(hass, _engine_rpm_entity)
    shore = _num(hass, _shore_power_entity)
    return (
        engine is not None and engine > 0 and shore is not None and shore > 0
    )


_CHECK_SHORE_POWER = Check(
    id="shore_power",
    name="Shore power still connected",
    message=_MSG_ENGINE_SHORE,
    condition=_shore_connected_condition,
    is_active=_engine_on_shore_active,
    device_groups=("Electrical",),
    requires_label="an engine RPM sensor and a shore-power sensor",
    supports_alarm=True,
    default_action=ACTION_WARN,
    # Warn = a QUIET log entry only (matches the old engine automation's "info"
    # level); "warning" would push a persistent notification, narrowing the gap
    # with Alert. Alert escalates to the managed alarm.
    level="info",
    alarm_extras=_ALARM_ENGINE_SHORE,
)


def instinct_for_alarm_key(key: str) -> str | None:
    """The instinct that owns a managed-alarm key, or None for an alarm raised
    by something else (a notice rule, a service call). A flat routine's alarm
    key is its id; a check's is `<id>_<check>`. Used by the alarm log mirror
    so the Log tab can link an alarm line back to its tile."""
    for automation in CURATED_AUTOMATIONS:
        if key == automation.id or key.startswith(f"{automation.id}_"):
            return automation.id
    return None


def _active_below(
    resolver: Callable[[Any], str | None], default: float = 20,
) -> Callable[[Any, dict], bool]:
    """Predicate: the resolved entity's value is below config['threshold']
    (falling back to `default` — the starter routine's default isn't the
    tank/battery 20)."""
    def _p(hass: Any, config: dict[str, Any]) -> bool:
        value = _num(hass, resolver)
        return value is not None and value < config.get("threshold", default)
    return _p


def _active_above(
    resolver: Callable[[Any], str | None],
    default: float = 80,
    *,
    key: str = "threshold",
) -> Callable[[Any, dict], bool]:
    """Predicate: the resolved entity's value is above config[`key`] (default
    `threshold` — the disk routine passes `warn_threshold`)."""
    def _p(hass: Any, config: dict[str, Any]) -> bool:
        value = _num(hass, resolver)
        return value is not None and value > config.get(key, default)
    return _p


CURATED_AUTOMATIONS: list[CuratedAutomation] = [
    # Kept in alphabetical order by `name` (the customer-facing display order —
    # the Automations card renders rows in catalog order with no client-side
    # sort) so the tab is a predictable A-Z scan.
    CuratedAutomation(
        id="pack_imbalance",
        name="Batteries out of balance",
        description=(
            "Alert me if one leisure battery is far apart from the others on "
            "charge, so the van is not using all of them."
        ),
        category="power",
        level="warning",
        message=_MSG_PACK_IMBALANCE,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="two or more batteries reporting their charge",
        build=_build_pack_imbalance,
        is_active=_pack_imbalance_active,
        device_groups=("Battery",),
        alarm_extras=_ALARM_PACK_IMBALANCE,
        config_fields=[
            ConfigField(
                "threshold", "Alert above", 5, 60,
                _PACK_IMBALANCE_DEFAULT_PCT, unit="%", step=5,
            ),
        ],
    ),
    CuratedAutomation(
        id="cell_imbalance",
        name="Battery cell imbalance",
        description="Alert me if the leisure battery's cells drift out of balance.",
        category="power",
        level="warning",
        message=_MSG_CELL_IMBALANCE,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a battery with cell-voltage monitoring",
        build=_build_cell_imbalance,
        is_active=_cell_imbalance_active,
        device_groups=("Battery",),
        alarm_extras=_ALARM_CELL_IMBALANCE,
        config_fields=[
            ConfigField(
                "threshold", "Alert above", 20, 500,
                _CELL_IMBALANCE_DEFAULT_MV, unit="mV", step=10,
            ),
        ],
    ),
    CuratedAutomation(
        id="discharge_disabled",
        name="Battery output turned off",
        description=(
            "Alert me if a leisure battery has discharging switched off, so "
            "the van cannot draw any power from it."
        ),
        category="power",
        level="warning",
        message=_MSG_DISCHARGE_DISABLED,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a battery that reports its discharge switch",
        build=_build_discharge_disabled,
        is_active=_discharge_disabled_active,
        device_groups=("Battery",),
        alarm_extras=_ALARM_DISCHARGE_DISABLED,
    ),
    CuratedAutomation(
        id="cold_charging",
        name="Cold-charging protection",
        description=(
            "Alert me if charge current flows into a near-freezing battery, "
            "and switch off chargers Sensa can control."
        ),
        category="power",
        level="warning",
        message=_MSG_COLD_CHARGING,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a battery with temperature and current monitoring",
        build=_build_cold_charging,
        extra_builds=(_build_cold_charge_restore,),
        is_active=_cold_charging_active,
        on_fire=_cold_charging_on_fire,
        device_groups=("Battery",),
        alarm_extras=_ALARM_COLD_CHARGING,
        config_fields=[
            ConfigField(
                "threshold", "Alert below", -5, 10,
                _COLD_CHARGE_DEFAULT_C, unit="°C",
            ),
            ConfigField(
                "charge_cutoff", "Charge cut-off", 0, 1, 1,
                field_type="toggle",
            ),
        ],
    ),
    CuratedAutomation(
        id="controller_temperature",
        name="Core temperature",
        description=(
            "Warn me when the Sensa controller runs warm, alert me when it gets "
            "hot enough to slow itself down, and shut it down safely if it gets "
            "dangerously hot."
        ),
        category="system",
        level="warning",
        message=_MSG_CONTROLLER_WARM,
        default_enabled=True,
        # Fixed two-level behaviour (warn AND alert), so no warn/alert chooser.
        # Same shape as the storage routine.
        supports_alarm=False,
        requires_label="a Sensa appliance (the controller reports its own temperature)",
        build=_build_controller_temperature,
        # NOT `_active_above`: that compares the raw state, which is in the
        # customer's unit, against a Celsius threshold. Same bug as the
        # trigger, one surface along, and it would paint the Instincts row as
        # tripped on every US customary box.
        is_active=_controller_temperature_active,
        normalise_config=_normalise_controller_thresholds,
        device_groups=("System",),
        config_fields=[
            ConfigField(
                "warn_threshold", "Warn above", 50, 90, _CONTROLLER_WARN_C, unit="°C",
            ),
            ConfigField(
                "alert_threshold", "Alert above", 60, 100, _CONTROLLER_ALERT_C,
                unit="°C",
            ),
            # Floor of 85 deliberately: below that is normal operating
            # temperature for a Pi under load and a shutdown there would take
            # the van's heating, alarms and tracking down for no reason. Ceiling
            # of 105 keeps it under the kernel's own 110 C trip, which is the
            # only reason to do this in software at all.
            ConfigField(
                "shutdown_threshold", "Shut down above", 85, 105,
                _CONTROLLER_SHUTDOWN_C, unit="°C",
            ),
        ],
    ),
    CuratedAutomation(
        id="drive_mode_when_moving",
        name="Drive mode when moving",
        description=(
            "Switch to Drive when the van starts moving, and back to Parked "
            "when it stops."
        ),
        category="vehicle",
        level="info",
        message=_MSG_DRIVE_MODE,
        default_enabled=True,
        requires_label="an OBD2 adapter or a GPS source",
        device_groups=("Engine",),
        build=_build_drive_mode,
        extra_builds=(_build_parked_mode,),
        is_active=_drive_mode_active,
        on_fire=_drive_mode_on_fire,
        availability=_drive_mode_available,
        # Not escalatable: this routine's job is to select a mode, and the log
        # line is a record of having done it. An alarm that re-sounds until
        # snoozed because the van is driving would be noise.
        supports_alarm=False,
        config_fields=[
            # Keys and defaults come from `movement.DEFAULT_TUNING`, which the
            # detector reads the stored values back through. One source, so the
            # number on the card is always the number being applied.
            ConfigField(
                "move_speed", "Counts as moving above", 3, 60,
                DEFAULT_TUNING["move_speed"], unit="mph",
            ),
            ConfigField(
                "move_seconds", "Held at that speed for", 15, 600,
                DEFAULT_TUNING["move_seconds"], unit="s", step=15,
            ),
            ConfigField(
                "move_distance", "Or once this far from where it parked",
                100, 5000, DEFAULT_TUNING["move_distance"], unit="m", step=50,
            ),
            ConfigField(
                "stop_seconds", "Counts as stopped after", 60, 1800,
                DEFAULT_TUNING["stop_seconds"], unit="s", step=30,
            ),
            ConfigField(
                "park_on_stop", "Back to Parked when stopped", 0, 1, 1,
                field_type="toggle",
            ),
        ],
    ),
    # "Engine Start" — a TRIGGER routine: on engine start, run the departure
    # checks. Today one check (shore power still connected); more (steps, roof,
    # handbrake) drop in as extra `Check` entries. Id kept as
    # `engine_on_shore_power` so any stored customer choices stay valid.
    CuratedAutomation(
        id="engine_on_shore_power",
        name="Engine Start",
        description="When the engine starts, run my departure checks.",
        category="power",
        level="info",
        message="",
        default_enabled=True,
        requires_label="an engine RPM sensor",
        device_groups=("Engine",),
        trigger=_engine_start_trigger,
        checks=(_CHECK_SHORE_POWER,),
    ),
    CuratedAutomation(
        id="temperature_watch",
        name="Temperature watch",
        description=(
            "Alert me if a watched temperature rises or falls by more than a "
            "set amount inside a set time (a cabin heating up, a compressor "
            "failing, a heater left on). Flag which thermometers to watch "
            "with Temperature Watch on the device's own row in Settings -> "
            "Devices, no limit on how many."
        ),
        category="climate",
        level="warning",
        message=_MSG_TEMPERATURE_WATCH,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label=(
            "a temperature sensor flagged Temperature Watch on its Devices row"
        ),
        build=_build_temperature_watch,
        is_active=_temperature_watch_active,
        availability=_temperature_watch_available,
        device_groups=("Climate",),
        alarm_extras=_ALARM_TEMPERATURE_WATCH,
        config_fields=[
            ConfigField(
                "change", "Change of", 1, 20, TEMPERATURE_WATCH_DEFAULT_CHANGE_C,
                unit="°C", step=0.5,
            ),
            ConfigField(
                "minutes", "Over", 5, TEMPERATURE_WATCH_MAX_MINUTES,
                TEMPERATURE_WATCH_DEFAULT_MINUTES, unit="min", step=5,
            ),
        ],
    ),
    CuratedAutomation(
        id=_FRIDGE.routine_id,
        name=_FRIDGE.name,
        description=(
            "Alert me if a fridge drifts above a safe temperature (a door "
            "left ajar, or a fault). Flag which thermometers are fridges "
            "with Fridge on the device's own row in Settings -> Devices, no "
            "limit on how many."
        ),
        category="climate",
        level="warning",
        message=_MSG_COMPARTMENT_WATCH,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a temperature sensor flagged Fridge on its Devices row",
        build=_build_fridge_watch,
        is_active=_fridge_watch_active,
        availability=_temperature_watch_available,
        device_groups=("Climate",),
        alarm_extras=_ALARM_COMPARTMENT_WATCH,
        config_fields=[
            ConfigField(
                "threshold", "Alert above", _FRIDGE.minimum_c, _FRIDGE.maximum_c,
                _FRIDGE.default_c, unit="°C",
            ),
        ],
    ),
    CuratedAutomation(
        id=_FREEZER.routine_id,
        name=_FREEZER.name,
        description=(
            "Alert me if a freezer drifts above a safe temperature (a door "
            "left ajar, or a fault). Flag which thermometers are freezers "
            "with Freezer on the device's own row in Settings -> Devices, no "
            "limit on how many."
        ),
        category="climate",
        level="warning",
        message=_MSG_COMPARTMENT_WATCH,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a temperature sensor flagged Freezer on its Devices row",
        build=_build_freezer_watch,
        is_active=_freezer_watch_active,
        availability=_temperature_watch_available,
        device_groups=("Climate",),
        alarm_extras=_ALARM_COMPARTMENT_WATCH,
        config_fields=[
            ConfigField(
                "threshold", "Alert above", _FREEZER.minimum_c,
                _FREEZER.maximum_c, _FREEZER.default_c, unit="°C",
            ),
        ],
    ),
    CuratedAutomation(
        id="full_grey_water",
        name="Grey water full",
        description="Warn me when the grey-water tank rises above a level.",
        category="water",
        level="warning",
        message=_MSG_FULL_GREY_WATER,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a grey-water level sensor",
        build=_build_full_grey_water,
        is_active=_active_above(_grey_water_entity),
        device_groups=("Water",),
        alarm_extras=_ALARM_TANK,
        config_fields=[
            ConfigField("threshold", "Warn above", 50, 100, 80, unit="%"),
        ],
    ),
    CuratedAutomation(
        id="low_fresh_water",
        name="Low fresh water",
        description="Warn me when the fresh-water tank drops below a level.",
        category="water",
        level="warning",
        message=_MSG_LOW_WATER,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a fresh-water level sensor",
        build=_build_low_fresh_water,
        is_active=_active_below(_fresh_water_entity),
        device_groups=("Water",),
        alarm_extras=_ALARM_TANK,
        config_fields=[
            ConfigField("threshold", "Warn below", 5, 50, 20, unit="%"),
        ],
    ),
    CuratedAutomation(
        id="low_fuel_near_station",
        name="Low fuel near a station",
        description=(
            "Alarm when the fuel gauge is low and a fuel station is within "
            "range (uses Sensa Radar)."
        ),
        category="fuel",
        level="warning",
        message=_MSG_LOW_FUEL_NEAR_STATION,
        # Test-tuned defaults (station within 10 mi / fuel below 99%) so it
        # fires readily while validating the Radar -> Routine -> alarm pipeline.
        # Lower the fuel threshold (and likely set default_enabled=False) before
        # this ships to customers, so it isn't alarming on a near-full tank.
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a vehicle fuel-level sensor",
        build=_build_low_fuel_near_station,
        is_active=_low_fuel_near_active,
        device_groups=("Engine",),
        # The nearest-station distance comes from Sensa Radar (the cloud), so
        # this is the one routine that can't work while the van is offline.
        requires_internet=True,
        config_fields=[
            ConfigField("distance_miles", "Station within", 1, 50, 10, unit="mi"),
            ConfigField("fuel_threshold", "Fuel below", 5, 99, 99, unit="%"),
        ],
    ),
    CuratedAutomation(
        id=ALARM_KEY_TYRE_PRESSURE,
        name="Tyre pressure",
        description=(
            "Alert me when a tyre's pressure goes below my minimum or above my "
            "maximum, and clear it once the tyre is back in range."
        ),
        category="vehicle",
        level="warning",
        message="A tyre's pressure is outside your range. Check it before driving on.",
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a tyre pressure sensor",
        build=_build_tyre_pressure,
        # No is_active: a fire-on-enable would raise under the bare routine id,
        # which the per-tyre clear could never cancel. The HA-start and hourly
        # triggers cover a tyre already out of range.
        is_active=None,
        device_groups=("Tyres",),
        alarm_extras=_ALARM_TYRE_PRESSURE,
        normalise_config=_normalise_tyre_range,
        config_fields=[
            ConfigField("low", "Alert below", 5, 150, _TYRE_DEFAULT_LOW_PSI, unit="psi"),
            ConfigField("high", "Alert above", 5, 150, _TYRE_DEFAULT_HIGH_PSI, unit="psi"),
        ],
    ),
    CuratedAutomation(
        id="low_battery",
        name="Low leisure battery",
        description="Warn me when the leisure battery drops below a level.",
        category="power",
        level="warning",
        message=_MSG_LOW_BATTERY,
        default_enabled=True,
        requires_label="a battery state-of-charge sensor",
        build=_build_low_battery,
        is_active=_active_below(_battery_soc_entity),
        live_message=_live_low_battery_message,
        device_groups=("Battery",),
        config_fields=[
            ConfigField("threshold", "Warn below", 5, 50, 20, unit="%"),
        ],
    ),
    CuratedAutomation(
        id=ALARM_KEY_SENSOR_BATTERY_LOW,
        name="Sensor battery low",
        description=(
            "Alert me when a wireless sensor's own battery drops below a "
            "level, so I can replace it before the sensor goes quiet."
        ),
        category="environment",
        level="warning",
        message=_MSG_SENSOR_BATTERY_LOW,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a wireless sensor that reports its battery",
        build=_build_sensor_battery_low,
        # Deliberately no is_active: a fire-on-enable raises under the bare
        # routine id, which the per-sensor clear could never cancel. The
        # hourly backstop in the build covers enable-while-already-low.
        is_active=None,
        device_groups=("Environment",),
        alarm_extras=_ALARM_SENSOR_BATTERY_LOW,
        config_fields=[
            ConfigField(
                "threshold", "Alert below", 5, 50,
                _SENSOR_BATTERY_DEFAULT_PCT, unit="%", step=5,
            ),
        ],
    ),
    CuratedAutomation(
        id="sensor_quiet",
        name="Sensor gone quiet",
        description=(
            "Alert me if a wireless sensor stops reporting (a flat battery, "
            "or out of range), so the instincts that rely on it are not left "
            "blind."
        ),
        category="environment",
        level="warning",
        message=_MSG_SENSOR_QUIET,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a wireless environment sensor",
        build=_build_sensor_quiet,
        is_active=_sensor_quiet_active,
        device_groups=("Environment",),
        alarm_extras=_ALARM_SENSOR_QUIET,
        config_fields=[
            ConfigField(
                "quiet_minutes", "Quiet for", 10, 240,
                _QUIET_DEFAULT_MINUTES, unit="min", step=5,
            ),
        ],
    ),
    CuratedAutomation(
        id="shore_power_dropped",
        name="Shore power dropped",
        description=(
            "Alert me if shore power drops while hooked up (a tripped "
            "bollard or breaker), so the battery isn't quietly draining "
            "when I think it's charging."
        ),
        category="power",
        level="warning",
        message=_MSG_SHORE_DROPPED,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a shore-power sensor",
        build=_build_shore_power_dropped,
        # Deliberately no is_active: this is a TRANSITION instinct, and
        # "already off shore power" at enable time is the normal off-grid
        # state, not an event to page about.
        is_active=None,
        device_groups=("Electrical",),
        alarm_extras=_ALARM_SHORE_DROPPED,
        config_fields=[
            ConfigField(
                "drop_minutes", "For", 1, 60,
                _SHORE_DROP_DEFAULT_MINUTES, unit="min",
            ),
        ],
    ),
    CuratedAutomation(
        id="starter_battery_watch",
        name="Starter battery watch",
        description=(
            "Alert me if the starter battery sags while parked, so the van "
            "always starts."
        ),
        category="power",
        level="warning",
        message=_MSG_STARTER_LOW,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label=(
            "a battery monitor with a starter input, or an OBD2 adapter"
        ),
        build=_build_starter_battery,
        is_active=_active_below(_starter_voltage_entity, default=_STARTER_DEFAULT_V),
        device_groups=("Battery", "Engine"),
        alarm_extras=_ALARM_STARTER_LOW,
        config_fields=[
            # Max 26 so a 24 V truck can set a sensible bound (~23.8).
            ConfigField(
                "threshold", "Warn below", 10.5, 26,
                _STARTER_DEFAULT_V, unit="V", step=0.1,
            ),
        ],
    ),
    CuratedAutomation(
        id="disk_space",
        name="Core storage space",
        description="Warn me when storage is high, alert me when it's critical.",
        category="system",
        level="warning",
        message=_MSG_DISK_WARN,
        default_enabled=True,
        # Fixed two-level behaviour (warn AND alert) — no warn/alert chooser.
        supports_alarm=False,
        requires_label="",
        build=_build_disk_space,
        is_active=_active_above(_disk_use_entity, default=80, key="warn_threshold"),
        normalise_config=_normalise_disk_thresholds,
        device_groups=("System",),
        config_fields=[
            ConfigField("warn_threshold", "Warn above", 50, 95, 80, unit="%"),
            ConfigField("alert_threshold", "Alert above", 60, 99, 90, unit="%"),
        ],
    ),
    CuratedAutomation(
        id="vibration_detected",
        name="Vibration detected",
        description=(
            "Alert me if a vibration sensor detects movement (possible "
            "tampering or towing) while the van is in Sentry mode."
        ),
        category="security",
        level="warning",
        message="",
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a vibration sensor",
        build=_build_vibration_detected,
        # Deliberately no is_active: like shore_power_dropped, this is a
        # TRANSITION instinct — vibration is a momentary event, not a state
        # that could already be "true" at enable time.
        is_active=None,
        device_groups=("Security",),
        alarm_extras=_ALARM_VIBRATION,
    ),
    CuratedAutomation(
        id="geofence",
        name="Geofence",
        description=(
            "Alert me if the van moves outside a circle around where it was "
            "parked while in Sentry mode (towed or stolen)."
        ),
        category="security",
        level="warning",
        message=_MSG_GEOFENCE,
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a GPS fitted in the van, such as a Teltonika router",
        build=_build_geofence,
        is_active=_geofence_active,
        device_groups=("Security",),
        alarm_extras=_ALARM_GEOFENCE,
        # No requires_internet: the watch and the alarm run on the box off the
        # van's own GPS. Only a push to a phone away from the van, and the
        # Security tab map, need a connection.
        config_fields=[
            ConfigField(
                "radius", "Circle size", 50, 5000,
                _GEOFENCE_DEFAULT_M, unit="m", step=50,
            ),
        ],
    ),
    CuratedAutomation(
        id="leak_detected",
        name="Water leak detected",
        description=(
            "Alert me if a water leak sensor detects moisture (a burst pipe "
            "or ingress)."
        ),
        category="security",
        level="warning",
        message="",
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a water leak sensor",
        build=_build_leak_detected,
        # Deliberately no is_active: like vibration_detected, this is a
        # TRANSITION instinct — a leak event is momentary, not a state that
        # could already be "true" at enable time.
        is_active=None,
        device_groups=("Security",),
        alarm_extras=_ALARM_LEAK,
    ),
    CuratedAutomation(
        id="smoke_detected",
        name="Smoke detected",
        description=(
            "Alert me if a smoke detector detects smoke, whatever mode the "
            "van is in."
        ),
        category="security",
        level="warning",
        message="",
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a smoke detector, such as a Shelly Plus Smoke",
        build=_build_smoke_detected,
        # No is_active, like leak_detected: the HA-start trigger in the
        # generated automation already raises smoke present at boot.
        is_active=None,
        device_groups=("Security",),
        alarm_extras=_ALARM_SMOKE,
    ),
    CuratedAutomation(
        id="circuit_fault",
        name="Circuit fault",
        description=(
            "Alert me if a switched circuit reports overheating, "
            "overpowering, overvoltage or overcurrent."
        ),
        category="power",
        level="warning",
        message="",
        default_enabled=True,
        default_action=ACTION_ALARM,
        requires_label="a Shelly Pro 4PM",
        build=_build_circuit_fault,
        # No is_active, like leak_detected: the HA-start trigger in the
        # generated automation already raises a fault present at boot.
        is_active=None,
        device_groups=("Electrical",),
        alarm_extras=_ALARM_CIRCUIT_FAULT,
    ),
]

CURATED_AUTOMATIONS_BY_ID: dict[str, CuratedAutomation] = {
    a.id: a for a in CURATED_AUTOMATIONS
}


# -- state helpers (catalog merged with the customer's entry.options) --------

def _stored(options: dict[str, Any], automation_id: str) -> dict[str, Any]:
    return (options.get("automations") or {}).get(automation_id) or {}


def config_defaults(automation: CuratedAutomation) -> dict[str, Any]:
    return {f.key: f.default for f in automation.config_fields}


def _config_fields_payload(
    fields: list[ConfigField], hass: Any = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in fields:
        payload: dict[str, Any] = {
            "key": f.key, "name": f.name, "min": f.minimum, "max": f.maximum,
            "default": f.default, "unit": f.unit, "step": f.step,
            "type": f.field_type,
        }
        if f.options is not None:
            # Resolved fresh each describe so a select field's choices track
            # the live install (a newly-adopted BLE probe appears without a
            # reload). Never let a resolver hiccup break the whole card.
            try:
                payload["options"] = f.options(hass)
            except Exception:  # noqa: BLE001
                payload["options"] = []
        out.append(payload)
    return out


def _merge_action_state(
    stored: dict[str, Any],
    config_fields: list[ConfigField],
    default_enabled: bool,
    default_action: str,
    supports_alarm: bool,
) -> tuple[bool, dict[str, Any], str]:
    """Shared (enabled, config, action) merge of a stored choice over defaults,
    with action validation. Used for BOTH a routine and one of its checks."""
    enabled = bool(stored.get("enabled", default_enabled))
    config = {**{f.key: f.default for f in config_fields}, **(stored.get("config") or {})}
    action = stored.get("action", default_action)
    if action not in ACTIONS or (action == ACTION_ALARM and not supports_alarm):
        action = default_action
    return enabled, config, action


def resolve_state(
    options: dict[str, Any], automation: CuratedAutomation
) -> tuple[bool, dict[str, Any], str]:
    """Return (enabled, config, action) for a curated automation, merging the
    stored choice over the catalog defaults. `action` is "warn" | "alarm".

    A routine's optional `normalise_config` is applied here — the single choke
    point through which build / is_active / describe_automations all read config
    — so a canonicalisation (e.g. ordering the disk thresholds) reaches every
    consumer identically instead of diverging per call site."""
    enabled, config, action = _merge_action_state(
        _stored(options, automation.id), automation.config_fields,
        automation.default_enabled, automation.default_action,
        automation.supports_alarm,
    )
    if automation.normalise_config is not None:
        config = automation.normalise_config(config)
    return enabled, config, action


def _routine_enabled(options: dict[str, Any], automation: CuratedAutomation) -> bool:
    return bool(_stored(options, automation.id).get("enabled", automation.default_enabled))


def _resolve_check(
    options: dict[str, Any], automation_id: str, check: Check
) -> tuple[bool, dict[str, Any], str]:
    """Per-check (enabled, config, action), stored under
    `options[automations][automation_id]["checks"][check.id]`."""
    stored = (_stored(options, automation_id).get("checks") or {}).get(check.id) or {}
    return _merge_action_state(
        stored, check.config_fields, check.default_enabled,
        check.default_action, check.supports_alarm,
    )


def _build_check_routine(
    hass: Any, automation: CuratedAutomation, options: dict[str, Any]
) -> dict[str, Any] | None:
    """Generate the single HA automation for a trigger routine: its trigger plus
    one `if <check.condition> then <warn|alarm>` action per ENABLED, resolvable
    check. None when the trigger device is absent or no check produces an action."""
    triggers = automation.trigger(hass) if automation.trigger else None
    if not triggers:
        return None
    actions: list[dict[str, Any]] = []
    for check in automation.checks:
        enabled, config, action = _resolve_check(options, automation.id, check)
        if not enabled:
            continue
        cond = check.condition(hass, config)
        if not cond:
            continue
        actions.append({
            "if": cond,
            "then": [
                _action_block(
                    action, check.level, check.message,
                    f"{automation.id}_{check.id}", automation=automation.id,
                    **(check.alarm_extras or {}),
                )
            ],
        })
    if not actions:
        return None
    return {
        "id": f"sensa_nomad_{automation.id}",
        "alias": f"Sensa: {automation.name}",
        "mode": "single",
        "trigger": triggers,
        "action": actions,
    }


def _describe_check_routine(
    hass: Any, automation: CuratedAutomation, options: dict[str, Any]
) -> dict[str, Any]:
    """The card/sensor row for a trigger routine — a `checks` list nested under
    the routine. The routine is `available` when its trigger device resolves;
    each check carries its own `available`."""
    trigger_ok = bool(automation.trigger and automation.trigger(hass))
    checks_out: list[dict[str, Any]] = []
    for check in automation.checks:
        enabled, config, action = _resolve_check(options, automation.id, check)
        checks_out.append({
            "id": check.id,
            "name": check.name,
            "message": check.message,
            "available": check.condition(hass, config) is not None,
            "enabled": enabled,
            "action": action,
            "supports_alarm": check.supports_alarm,
            "device_groups": list(check.device_groups),
            "requires_label": check.requires_label,
            "requires_internet": check.requires_internet,
            "config": config,
            "config_fields": _config_fields_payload(check.config_fields, hass),
        })
    return {
        "id": automation.id,
        "name": automation.name,
        "description": automation.description,
        "category": automation.category,
        "device_groups": list(automation.device_groups),
        "level": automation.level,
        "enabled": _routine_enabled(options, automation),
        "action": None,
        "supports_alarm": automation.supports_alarm,
        # The routine is available (toggle-able) only when its trigger device
        # resolves AND at least one check resolves — otherwise enabling it would
        # generate no automation (build returns None) and silently do nothing.
        "available": trigger_ok and any(c["available"] for c in checks_out),
        "requires_label": automation.requires_label,
        "requires_internet": automation.requires_internet,
        "config": {},
        "config_fields": [],
        "checks": checks_out,
    }


def _allowed_routines(hass: Any) -> list[CuratedAutomation]:
    """The curated routines visible to this licence: every public routine PLUS
    any tagged for the licence's current dealer. Always additive (no exclusive
    mode) — a dealer's special-hardware routines add to the standard set, never
    replace it. Mirrors the dealer gating of home layouts
    (`home_catalog._allowed`), reusing the same `current_dealer` resolution, so
    trials see public-only (it returns None for them) and a licence-load/lookup
    hiccup degrades safely to public-only rather than leaking dealer routines.
    Applied in BOTH `describe_automations` (display) and
    `build_enabled_automations` (generation) so a dealer routine is never shown
    to, nor generated for, a non-entitled customer.
    """
    dealer_id: str | None = None
    if hass is not None:
        try:
            from .home_catalog import current_dealer

            dealer = current_dealer(hass)
            dealer_id = dealer.get("id") if dealer else None
        except Exception:  # noqa: BLE001 (never let a licence hiccup drop routines)
            dealer_id = None
    return [
        automation
        for automation in CURATED_AUTOMATIONS
        if not automation.dealer or automation.dealer == dealer_id
    ]


def routine_allowed(hass: Any, automation_id: str) -> bool:
    """Whether this licence may enable/fire the given routine. The same dealer
    gate `describe`/`build` apply, exposed so the `set_automation` service can
    enforce it too — otherwise a direct service call could enable AND fire a
    dealer-tagged routine the customer isn't entitled to, even though the card
    never shows it and the build never generates it."""
    return any(a.id == automation_id for a in _allowed_routines(hass))


DRIVE_MODE_ROUTINE_ID = "drive_mode_when_moving"


def movement_tuning(hass: Any) -> dict[str, float]:
    """The customer's movement thresholds, merged over the defaults.

    The detector calls this once per sample. It is the ONE place the stored
    choice and the defaults meet: the `ConfigField`s below declare the same
    keys with the same defaults out of `DEFAULT_TUNING`, so the number on the
    card is always the number being applied.

    Falls back to the defaults on anything unexpected (no config entry yet, a
    stored value that will not read as a number). A detector running on the
    defaults is a working van; one that threw would be a dead sensor.
    """
    from .movement import DEFAULT_TUNING

    tuning = dict(DEFAULT_TUNING)
    routine = CURATED_AUTOMATIONS_BY_ID.get(DRIVE_MODE_ROUTINE_ID)
    if routine is None:
        return tuning
    _enabled, config, _action = resolve_state(_entry_options(hass), routine)
    for key in tuning:
        try:
            value = float(config[key])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0:
            tuning[key] = value
    return tuning


def _entry_options(hass: Any) -> dict[str, Any]:
    """This integration's stored options, or `{}` before setup has run."""
    from .const import DOMAIN

    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:  # noqa: BLE001 - a test stub need not have config_entries
        return {}
    return dict(getattr(entries[0], "options", None) or {}) if entries else {}


def build_enabled_automations(
    hass: Any, options: dict[str, Any]
) -> list[dict[str, Any]]:
    """The HA automation dicts to generate — every enabled, resolvable entry."""
    out: list[dict[str, Any]] = []
    for automation in _allowed_routines(hass):
        if automation.checks:
            if not _routine_enabled(options, automation):
                continue
            built = _build_check_routine(hass, automation, options)
        else:
            enabled, config, action = resolve_state(options, automation)
            if not enabled:
                continue
            built = automation.build(hass, config, action) if automation.build else None
            # Companion automations (e.g. cold-charging's restore) share the
            # routine's enable/config/action and gate themselves by returning
            # None.
            for extra in automation.extra_builds:
                extra_built = extra(hass, config, action)
                if extra_built is not None:
                    out.append(extra_built)
        if built is not None:
            out.append(built)
    return out


def describe_automations(
    hass: Any, options: dict[str, Any]
) -> list[dict[str, Any]]:
    """Catalog + per-entry state for the Routines card / sensor."""
    rows: list[dict[str, Any]] = []
    for automation in _allowed_routines(hass):
        if automation.checks:
            rows.append(_describe_check_routine(hass, automation, options))
            continue
        enabled, config, action = resolve_state(options, automation)
        if automation.availability is not None:
            # Routines whose build ALSO depends on a customer selection
            # (the temperature watches) stay available, and so configurable,
            # before the selection is made, even though build returns None.
            available = bool(automation.availability(hass, config))
        else:
            available = bool(
                automation.build
                and automation.build(hass, config, action) is not None
            )
        rows.append({
            "id": automation.id,
            "name": automation.name,
            "description": automation.description,
            "category": automation.category,
            "device_groups": list(automation.device_groups),
            "level": automation.level,
            "enabled": enabled,
            "action": action,
            "supports_alarm": automation.supports_alarm,
            "available": available,
            "requires_label": automation.requires_label,
            "requires_internet": automation.requires_internet,
            "config": config,
            "config_fields": _config_fields_payload(automation.config_fields, hass),
        })
    return rows
