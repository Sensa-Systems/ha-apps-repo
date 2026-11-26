"""Customer-authored Routines: When / And If / Then Do, generated to HA automations.

Unlike `modes.py` (scenes, merged from vendor/catalog/customer layers) and
`automations.py` (Instincts, a fixed curated catalog), a Routine has exactly
one author: the customer, via the `sensa-routines-card` webcard on
Intelligence -> Routines. There is no device-default merge — a routine is
whatever the customer built, nothing more.

Storage (`RoutineOverrides`, Store-backed, same pattern as `ModeOverrides`):
    {"routines": {routine_id: {
        "name": str,
        "enabled": bool,
        "trigger": {"kind": "time"|"device"|"service", ...},
        "conditions": [{"entity_id": str, "to"|"above"|"below": ...}, ...],
        "actions": [{"kind": "entity"|"device"|"service", ...}, ...],
    }}}

A routine runs its actions in order. Routines saved before there could be
several carry a single `"action"` instead, and still read (`routine_actions`).
A climate target's action also carries `temperature`, `fan_mode`,
`preset_mode` and `swing_mode`, each optional.

"Then Do" mirrors HA's own action-targeting model (a service call's
`target` may carry `entity_id`, `device_id`, or `area_id` — see
home-assistant.io/docs/scripts/perform-actions), not a bespoke Sensa
scheme, per "that is the basis of what we are providing, but we will
make it better": the Editor's Entity/Device/Service picker just chooses
which of the three the customer builds, with a friendlier UI on top
(grouped-by-device browsing instead of typing a device_id; a value
toggle instead of hand-authored `data`). Action shapes, by `kind`:
    entity:  {"kind": "entity", "entity_id": str, "value": Any}
    device:  {"kind": "device", "device_id": str, "domain": str, "value": Any}
             — `domain` is the entity-domain the customer's device-grouped
             picker resolved to (a device can carry several domains, e.g. a
             relay controller's switches beside its status light).
    service: {"kind": "service", "service": "domain.service",
              "entity_id"?: str, "device_id"?: str, "data"?: dict}
             — the power-user escape hatch: any service, optional target,
             optional raw data. `entity_id`/`device_id` are both optional
             and independent (HA's target accepts either, or neither for a
             service with no target, e.g. a notify service).
    scene:   {"kind": "scene", "mode": str}
             (a Sensa scene's slug, activated through activate_mode).
    dim_by_turn: {"kind": "dim_by_turn", "entity_id": str}
             (a light moved by how far the cube just turned; only under a
             Turn trigger, see `_dim_by_turn_steps`).
    temp_by_turn: {"kind": "temp_by_turn", "entity_id": str}
             (a climate unit's or a number's setpoint moved the same way, see
             `_temp_by_turn_steps`).
    wait:    {"kind": "wait", "until"?: str, "timeout"?: int, "seconds"?: int}
             (pause before the next action: first until `until` is
             connected, giving up after `timeout` seconds, then `seconds`
             more. Not an action on its own; see `_wait_steps`).

Trigger/condition shape: the stored dict carries "to" for a state comparison
or "above"/"below" for a numeric comparison — decided once, by the webcard's
entity-control picker, at authoring time (the same domain-to-control-kind
inference `sensa-mode-editor.ts` already does for scene rows). The backend
trusts whichever keys are present rather than re-deriving the entity's domain
a second time — one predicate, not two.

The "kind" (time / device / service) on the trigger is a picker-UI grouping
only (which entity list the customer chose from); it doesn't change the
generated HA trigger shape beyond time-vs-entity, so the builder below only
branches on kind == "time" vs not.

Generated YAML is written to `nomad/routines.yaml` and registered as a THIRD
independent `automation <label>:` split-config key
(`automation routines: !include nomad/routines.yaml`), coexisting with the
customer's own default `automation:` key and Instincts' `automation sensa:`
key — see `_config_yaml.py` / `modes.py` for why HA merges these instead of
colliding.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ._config_yaml import register_split_key, remove_split_key
from ._io import atomic_write_text, record_generated_checksum
from .const import (
    CUBE_TURN_ACTIONS,
    CUBE_TURN_PRESS,
    DOMAIN,
    ROUTINES_FILE,
    ROUTINES_MANAGED_BEGIN,
    ROUTINES_MANAGED_END,
)

# Dim by turn: half a turn of the cube takes a light through its whole range.
DIM_DEGREES_FOR_FULL_RANGE = 180
DIM_ANGLE_SETTLE_MS = 50
# Temperature by turn: one degree for every this many degrees the cube turns.
TEMP_TURN_DEGREES_PER_STEP = 30

_LOGGER = logging.getLogger(__name__)

_OVERRIDES_STORAGE_VERSION = 1
_OVERRIDES_STORAGE_KEY = f"{DOMAIN}.routine_overrides"


# ---------------------------------------------------------------------------
# User-authored routine persistence
# ---------------------------------------------------------------------------


class RoutineOverrides:
    """Persistent store of every customer-authored routine.

    Storage shape: `{"routines": {routine_id: {name, enabled, trigger,
    conditions, action}}}`. Lives outside the config entry (like
    `ModeOverrides`) so an entry reload or reinstall never loses a
    customer's routines.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, Any]](
            hass, _OVERRIDES_STORAGE_VERSION, _OVERRIDES_STORAGE_KEY,
        )
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        if isinstance(loaded, dict) and isinstance(loaded.get("routines"), dict):
            self._data = dict(loaded["routines"])

    async def async_save(self) -> None:
        await self._store.async_save({"routines": self._data})

    def all(self) -> dict[str, dict[str, Any]]:
        return {rid: dict(r) for rid, r in self._data.items()}

    def get(self, routine_id: str) -> dict[str, Any] | None:
        routine = self._data.get(routine_id)
        return dict(routine) if routine is not None else None

    async def async_save_routine(
        self,
        routine_id: str,
        *,
        name: str,
        enabled: bool,
        trigger: dict[str, Any],
        conditions: list[dict[str, Any]],
        action: dict[str, Any] | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        """Create or fully replace one routine (the editor always sends the
        complete definition on Save; there is no partial-field update).
        `actions` is the list the editor sends; a lone `action` is the older
        single-action shape and is stored as a list of one."""
        stored = [dict(a) for a in (actions or ([action] if action else []))]
        self._data[routine_id] = {
            "name": name,
            "enabled": enabled,
            "trigger": dict(trigger),
            "conditions": [dict(c) for c in conditions],
            "actions": stored,
        }
        await self.async_save()

    async def async_set_enabled(self, routine_id: str, enabled: bool) -> None:
        routine = self._data.get(routine_id)
        if routine is None:
            return
        self._data[routine_id] = {**routine, "enabled": enabled}
        await self.async_save()

    async def async_delete_routine(self, routine_id: str) -> None:
        self._data.pop(routine_id, None)
        await self.async_save()

    async def async_set_order(self, ids: list[Any] | None) -> bool:
        """Put the routines in the customer's order (a Design Mode drag on the
        Routines tab). The order IS the store's key order, so a routine saved
        later keeps its place and a new one goes last. Ids not listed keep
        their relative order after the listed ones. Returns whether anything
        changed."""
        wanted = [i for i in dict.fromkeys(ids or []) if isinstance(i, str) and i in self._data]
        order = wanted + [rid for rid in self._data if rid not in wanted]
        if order == list(self._data):
            return False
        self._data = {rid: self._data[rid] for rid in order}
        await self.async_save()
        return True


# ---------------------------------------------------------------------------
# Routine -> HA automation translation
# ---------------------------------------------------------------------------


def _is_event_trigger(trigger: dict[str, Any]) -> bool:
    """True when the trigger entity is an HA `event.*` — a Zigbee scene
    button's presses (Z2M publishes each `action` as an event entity).

    Written once and read by both `_trigger_dict` and `_trigger_guards`,
    because an event entity needs BOTH halves and a second copy of the test
    is how one of them drifts.
    """
    return str(trigger.get("entity_id") or "").startswith("event.")


def _trigger_guards(trigger: dict[str, Any]) -> list[dict[str, Any]]:
    """Conditions the TRIGGER needs, prepended to the customer's "And If".

    Only an event entity has any. Its state is the timestamp of the last
    press, so a state trigger fires on every press; which press it was lives
    in the `event_type` attribute. Matching that attribute in the trigger
    itself (`attribute: event_type, to: ...`) looks tidier and is wrong: an
    attribute state trigger needs the attribute to CHANGE, so pressing the
    same button twice in a row would fire once and then go dead. Fire on the
    state and test the attribute here instead.

    `wanted` is the raw Z2M action. Z2M keeps `single_button_1` whole in
    `event_type` but splits `1_single` into `event_type: single` + `button: 1`,
    so the guard accepts either, or button 1 would answer every button's press.
    """
    if not _is_event_trigger(trigger):
        return []
    wanted = trigger.get("to")
    if not wanted:
        return []
    if wanted == CUBE_TURN_PRESS:
        return [{
            "condition": "template",
            "value_template":
                "{{ trigger.to_state.attributes.event_type in "
                + json.dumps(list(CUBE_TURN_ACTIONS)) + " }}",
        }]
    literal = json.dumps(str(wanted))
    return [{
        "condition": "template",
        "value_template":
            "{% set a = trigger.to_state.attributes %}{{ " + literal
            + ' in [a.event_type, (a.button | default("")) ~ "_" ~ a.event_type] }}',
    }]


def _trigger_dict(trigger: dict[str, Any]) -> dict[str, Any] | None:
    if trigger.get("kind") == "time":
        at = trigger.get("at")
        return {"platform": "time", "at": at} if at else None

    entity_id = trigger.get("entity_id")
    if not entity_id:
        return None
    if _is_event_trigger(trigger):
        # Any press. WHICH press is `_trigger_guards`' job. `not_to` keeps a
        # restart or a flat battery (unknown/unavailable) from reading as one.
        return {
            "platform": "state",
            "entity_id": entity_id,
            "not_to": ["unknown", "unavailable"],
        }
    if "above" in trigger or "below" in trigger:
        d: dict[str, Any] = {"platform": "numeric_state", "entity_id": entity_id}
        if "above" in trigger:
            d["above"] = trigger["above"]
        if "below" in trigger:
            d["below"] = trigger["below"]
        return d
    return {"platform": "state", "entity_id": entity_id, "to": trigger.get("to")}


def _condition_dict(condition: dict[str, Any]) -> dict[str, Any] | None:
    entity_id = condition.get("entity_id")
    if not entity_id:
        return None
    if "above" in condition or "below" in condition:
        d: dict[str, Any] = {"condition": "numeric_state", "entity_id": entity_id}
        if "above" in condition:
            d["above"] = condition["above"]
        if "below" in condition:
            d["below"] = condition["below"]
        return d
    return {"condition": "state", "entity_id": entity_id, "state": condition.get("to")}


# Domains whose "on/off"-shaped action reads as a boolean-ish value
# ("on"/"off", "true"/"false", True/False) rather than a number or string —
# same 3-bucket split as `sensa-mode-editor.ts`'s `_renderControl`
# (toggle / numeric / text), minus the read-only `binary_sensor` case (an
# action target must be something HA can actuate).
_TOGGLE_DOMAINS = ("switch", "light", "input_boolean", "fan")
_NUMERIC_DOMAINS = ("number", "input_number")
_TEXT_DOMAINS = ("input_text", "text")


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("on", "true", "1", "yes")


def _climate_call(
    value: Any, temperature: Any, target: dict[str, Any],
) -> dict[str, Any]:
    """A climate action: the picked hvac mode, and optionally a setpoint.

    The editor offers the unit's own `hvac_modes`, so `value` is a mode
    ("cool", "heat", ...), not on/off. It used to fall through to the generic
    on/off branch below, where any mode but "on" read as falsy, so picking
    Cool switched the air conditioner OFF.

    Mode and setpoint go in ONE `climate.set_temperature` call (HA accepts
    `hvac_mode` there), because a routine runs a single action. "on" / "off"
    are what routines saved before modes were offered; they keep meaning
    turn on / turn off.
    """
    mode = str(value if value is not None else "").strip().lower()
    try:
        setpoint = float(temperature) if temperature not in (None, "") else None
    except (TypeError, ValueError):
        setpoint = None
    if mode == "off":
        return {"action": "climate.turn_off", "target": target}
    if mode in ("", "on", "true"):
        if setpoint is None:
            return {"action": "climate.turn_on", "target": target}
        return {
            "action": "climate.set_temperature", "target": target,
            "data": {"temperature": setpoint},
        }
    if setpoint is None:
        return {
            "action": "climate.set_hvac_mode", "target": target,
            "data": {"hvac_mode": mode},
        }
    return {
        "action": "climate.set_temperature", "target": target,
        "data": {"hvac_mode": mode, "temperature": setpoint},
    }


def _service_call_for_domain(
    domain: str, value: Any, target: dict[str, Any], temperature: Any = None,
) -> dict[str, Any]:
    """The domain-to-service inference shared by the Entity and Device
    action kinds: given a target (`{"entity_id": ...}` or
    `{"device_id": ...}`) and the customer's picked value, decide which
    service call sets it. Same 3-bucket domain split as
    `sensa-mode-editor.ts`'s `_renderControl` / `entity-control.ts`, plus
    climate, which takes a mode and a setpoint."""
    if domain == "climate":
        return _climate_call(value, temperature, target)
    if domain in _TOGGLE_DOMAINS:
        service = "turn_on" if _is_truthy(value) else "turn_off"
        return {"action": f"{domain}.{service}", "target": target}
    if domain in _NUMERIC_DOMAINS or domain in _TEXT_DOMAINS:
        return {"action": f"{domain}.set_value", "target": target, "data": {"value": value}}
    # Any other actionable domain (climate, cover, lock, media_player, ...):
    # HA's generic `homeassistant.turn_on`/`turn_off` covers the common
    # on/off case without a per-domain service table. A customer wanting
    # finer control (a target temperature, a cover position) reaches for
    # the Service kind instead.
    service = "turn_on" if _is_truthy(value) else "turn_off"
    return {"action": f"homeassistant.{service}", "target": target}


def _action_dict(action: dict[str, Any]) -> dict[str, Any] | None:
    kind = action.get("kind", "entity")

    if kind == "service":
        service = action.get("service")
        if not service or "." not in service:
            return None
        result: dict[str, Any] = {"action": service}
        target: dict[str, Any] = {}
        if action.get("entity_id"):
            target["entity_id"] = action["entity_id"]
        if action.get("device_id"):
            target["device_id"] = action["device_id"]
        if target:
            result["target"] = target
        data = action.get("data")
        if isinstance(data, dict) and data:
            result["data"] = data
        return result

    if kind == "scene":
        # A Sensa scene (preset or customer-made), activated exactly as its
        # tile does: through activate_mode, which also latches it as the
        # current scene. A bare scene.turn_on would skip that.
        mode = action.get("mode")
        if not mode:
            return None
        return {"action": "sensa_nomad.activate_mode", "data": {"mode": str(mode)}}

    if kind == "device":
        device_id = action.get("device_id")
        domain = action.get("domain")
        if not device_id or not domain:
            return None
        return _service_call_for_domain(
            domain, action.get("value"), {"device_id": device_id},
            action.get("temperature"),
        )

    # kind == "entity" (default — also covers pre-existing stored routines
    # saved before "kind" existed, which never carried anything else).
    entity_id = action.get("entity_id")
    if not entity_id:
        return None
    domain = entity_id.split(".", 1)[0]
    return _service_call_for_domain(
        domain, action.get("value"), {"entity_id": entity_id},
        action.get("temperature"),
    )


# A climate unit's other settings, each its own HA service: (the stored key,
# the climate service, the service's field). Sent after the mode / setpoint
# call, and only when the action is not turning the unit off.
_CLIMATE_EXTRAS: tuple[tuple[str, str, str], ...] = (
    ("fan_mode", "climate.set_fan_mode", "fan_mode"),
    ("preset_mode", "climate.set_preset_mode", "preset_mode"),
    ("swing_mode", "climate.set_swing_mode", "swing_mode"),
)


def _action_calls(action: dict[str, Any]) -> list[dict[str, Any]]:
    """Every HA service call one "Then Do" action makes, in order: the call
    `_action_dict` builds, then for a climate target each extra setting the
    customer chose. Empty when the action is incomplete."""
    base = _action_dict(action)
    if base is None:
        return []
    calls = [base]
    kind = action.get("kind", "entity")
    if kind == "device":
        domain = action.get("domain")
    elif kind == "entity":
        domain = str(action.get("entity_id") or "").split(".", 1)[0]
    else:
        domain = None
    if domain != "climate" or str(action.get("value") or "").lower() == "off":
        return calls
    for key, service, field_name in _CLIMATE_EXTRAS:
        chosen = action.get(key)
        if chosen not in (None, ""):
            calls.append({
                "action": service, "target": base["target"],
                "data": {field_name: chosen},
            })
    return calls


WAIT_UNTIL_TIMEOUT_SECONDS = 600


def _whole_seconds(raw: Any, default: int) -> int:
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return default


def _wait_steps(
    hass: HomeAssistant | None, action: dict[str, Any], routine_id: str, name: str,
) -> list[dict[str, Any]]:
    """The HA script steps for one Wait: until the picked entity is connected,
    then a fixed delay. Either half may be absent.

    Waiting for an air conditioner means its bridge is talking to it
    (`ac_link_up_template`), not that the climate entity exists: a FreshJet
    whose AC has just been powered on reads a plain "off" for a while before
    the bridge reconnects, and a command sent then goes nowhere.

    Giving up stops the routine and says so in the Log, rather than running
    the rest against a unit that cannot hear it.
    """
    from .ac_link import ac_link_up_template

    steps: list[dict[str, Any]] = []
    until = str(action.get("until") or "")
    if until:
        timeout = _whole_seconds(action.get("timeout"), WAIT_UNTIL_TIMEOUT_SECONDS)
        state = hass.states.get(until) if hass is not None else None
        label = str(((state.attributes or {}).get("friendly_name") if state else "") or until)
        minutes = max(1, round(timeout / 60))
        steps += [
            {
                "wait_template": ac_link_up_template(hass, until),
                "timeout": {"seconds": timeout},
                "continue_on_timeout": True,
            },
            {
                "if": [{"condition": "template", "value_template": "{{ not wait.completed }}"}],
                "then": [
                    {
                        "action": "sensa_nomad.log",
                        "data": {
                            "level": "warning",
                            "message": (
                                f"Routine {name} stopped: {label} did not connect "
                                f"within {minutes} min."
                            ),
                            "context": {"routine": routine_id, "name": name},
                        },
                    },
                    {"stop": f"{until} did not connect"},
                ],
            },
        ]
    seconds = _whole_seconds(action.get("seconds"), 0)
    if seconds:
        steps.append({"delay": {"seconds": seconds}})
    return steps


def _is_turn_trigger(trigger: dict[str, Any]) -> bool:
    return _is_event_trigger(trigger) and trigger.get("to") == CUBE_TURN_PRESS


def _turn_angle_entity(hass: HomeAssistant | None, event_entity_id: str) -> str:
    """The `Action angle` sensor of the device behind this cube's event entity.
    By the registry, as a customer can rename an entity_id; the name Z2M gives
    it otherwise."""
    fallback = f"sensor.{event_entity_id.split('.', 1)[-1]}_angle"
    if hass is None:
        return fallback
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        entry = registry.async_get(event_entity_id)
        if entry is None or not entry.device_id:
            return fallback
        for sibling in er.async_entries_for_device(registry, entry.device_id):
            if sibling.original_name == "Action angle":
                return sibling.entity_id
    except Exception:
        _LOGGER.debug("Dim by turn: registry lookup failed", exc_info=True)
    return fallback


def _dim_by_turn_steps(
    hass: HomeAssistant | None, action: dict[str, Any], trigger: dict[str, Any],
) -> list[dict[str, Any]]:
    """The HA script steps that move a light by the turn that fired this routine.

    Half a turn is the light's whole range: right brightens, left dims. HA's step
    clamps at full, switches off at nothing and brings a dark light up from low;
    a left turn on a dark light does nothing.

    The pause is what makes the angle this turn's. One Z2M message writes the
    event and the angle sensor as two entities with no promised order, and the
    automation can start before the second is written. Both are written in the
    same pass, so by the time the pause ends the sensor holds this message.
    """
    light = action.get("entity_id")
    if not light or not _is_turn_trigger(trigger):
        return []
    angle = _turn_angle_entity(hass, str(trigger.get("entity_id")))
    read = f"states('{angle}') | float(0)"
    step = f"{{% set s = ({read}) * 100 / {DIM_DEGREES_FOR_FULL_RANGE} %}}{{{{ [[s, 100] | min, -100] | max | round(0) | int }}}}"
    return [
        {"delay": {"milliseconds": DIM_ANGLE_SETTLE_MS}},
        {
            "if": [{
                "condition": "template",
                "value_template":
                    f"{{% set a = {read} %}}{{{{ a > 0 or (a < 0 and is_state('{light}', 'on')) }}}}",
            }],
            "then": [{
                "action": "light.turn_on",
                "target": {"entity_id": light},
                "data": {"brightness_step_pct": step},
            }],
        },
    ]


def _temp_by_turn_steps(
    hass: HomeAssistant | None, action: dict[str, Any], trigger: dict[str, Any],
) -> list[dict[str, Any]]:
    """The HA script steps that move a setpoint by the turn that fired this
    routine: right raises, left lowers, kept inside the unit's own range.

    Whole degrees, so a turn too small to reach one does nothing rather than
    nudging the setpoint by a fraction. It moves the setpoint only: a unit that
    is off stays off. Same pause as `_dim_by_turn_steps`, for the same reason.
    """
    target = str(action.get("entity_id") or "")
    domain = target.split(".", 1)[0]
    if domain not in ("climate", "number") or not _is_turn_trigger(trigger):
        return []
    angle = _turn_angle_entity(hass, str(trigger.get("entity_id")))
    delta = f"{{% set d = ((states('{angle}') | float(0)) / {TEMP_TURN_DEGREES_PER_STEP}) | round(0) %}}"
    if domain == "climate":
        current = f"state_attr('{target}', 'temperature')"
        low, high = f"state_attr('{target}', 'min_temp')", f"state_attr('{target}', 'max_temp')"
        service, field_name = "climate.set_temperature", "temperature"
    else:
        current = f"states('{target}')"
        low, high = f"state_attr('{target}', 'min')", f"state_attr('{target}', 'max')"
        service, field_name = "number.set_value", "value"
    return [
        {"delay": {"milliseconds": DIM_ANGLE_SETTLE_MS}},
        {
            "if": [{
                "condition": "template",
                "value_template": (
                    f"{delta}{{% set c = {current} %}}"
                    "{{ d != 0 and c not in [none, 'unknown', 'unavailable'] }}"
                ),
            }],
            "then": [{
                "action": service,
                "target": {"entity_id": target},
                "data": {field_name: (
                    f"{delta}{{% set v = ({current} | float(0)) + d %}}"
                    f"{{{{ [[v, {high} | float(v)] | min, {low} | float(v)] | max }}}}"
                )},
            }],
        },
    ]


_TURN_STEPS = {"dim_by_turn": _dim_by_turn_steps, "temp_by_turn": _temp_by_turn_steps}


def routine_actions(routine: dict[str, Any]) -> list[dict[str, Any]]:
    """A routine's stored actions: the `actions` list, or the single `action`
    a routine saved before there could be several."""
    actions = routine.get("actions")
    if isinstance(actions, list):
        return [a for a in actions if isinstance(a, dict)]
    single = routine.get("action")
    return [single] if isinstance(single, dict) else []


def _log_action(routine_id: str, name: str) -> dict[str, Any]:
    """The Intelligence -> Log line a routine writes after its action, so the
    customer can see it ran; the context lets the Log tab link the line back
    to the routine's tile.

    `name` is stamped in the context as well as baked into the sentence. A
    stored line keeps the name the routine had when it ran, so renaming the
    routine leaves every old entry talking about something the customer can
    no longer find; the Log tab swaps the stale name for the live one, and it
    can only do that safely if it knows exactly which words to replace.
    """
    return {
        "action": "sensa_nomad.log",
        "data": {
            "level": "info",
            "message": f"Routine {name} ran.",
            "context": {"routine": routine_id, "name": name},
        },
    }


def build_automations(
    overrides: RoutineOverrides, hass: HomeAssistant | None = None,
) -> list[dict[str, Any]]:
    """Translate every enabled, well-formed routine into one HA automation
    dict. A routine missing its trigger or action (mid-edit, or corrupt
    storage) is skipped rather than emitted half-built. `hass` lets a Wait
    step find an air conditioner's bridge; without it a Wait only waits for
    the entity to exist."""
    automations: list[dict[str, Any]] = []
    for routine_id, routine in overrides.all().items():
        if not routine.get("enabled", True):
            continue
        trigger = _trigger_dict(routine.get("trigger") or {})
        name = routine.get("name") or routine_id
        # Every complete action, in order; an incomplete one (mid-edit) is
        # dropped rather than failing the routine, but a routine with none is
        # skipped like one with no trigger. Waits alone do nothing, so they
        # do not count.
        calls: list[dict[str, Any]] = []
        acts = False
        on_turn = False
        for a in routine_actions(routine):
            if a.get("kind") == "wait":
                calls += _wait_steps(hass, a, routine_id, name)
                continue
            turn_steps = _TURN_STEPS.get(a.get("kind"))
            if turn_steps is not None:
                steps = turn_steps(hass, a, routine.get("trigger") or {})
                acts = acts or bool(steps)
                on_turn = on_turn or bool(steps)
                calls += steps
                continue
            made = _action_calls(a)
            acts = acts or bool(made)
            calls += made
        if trigger is None or not acts:
            continue
        conditions = [
            *_trigger_guards(routine.get("trigger") or {}),
            *(
                c for c in (
                    _condition_dict(c) for c in routine.get("conditions") or []
                ) if c is not None
            ),
        ]
        automations.append({
            "id": f"nomad_routine_{routine_id}",
            "alias": name,
            "mode": "single",
            "trigger": [trigger],
            "condition": conditions,
            # One turn of the cube can report several times, and a line for each
            # would bury the Log.
            "action": calls if on_turn else [*calls, _log_action(routine_id, name)],
        })
    return automations


# ---------------------------------------------------------------------------
# YAML writer + configuration.yaml registration
# ---------------------------------------------------------------------------


def _write_routines_yaml(config_dir: str, automations: list[dict[str, Any]]) -> None:
    path = Path(config_dir) / ROUTINES_FILE
    content = yaml.safe_dump(automations, sort_keys=False, allow_unicode=True)
    atomic_write_text(path, content)
    record_generated_checksum(config_dir, ROUTINES_FILE, content)
    _LOGGER.debug(
        "Nomad routines: YAML written to %s (%d entries)", path, len(automations),
    )


def _ensure_registration(config_dir: str) -> bool:
    """Make sure configuration.yaml carries our managed routines block.
    Returns True if the file changed (first-time registration needs a
    restart; afterwards `async_reload_routines` picks up edits live)."""
    return register_split_key(
        config_dir,
        begin_marker=ROUTINES_MANAGED_BEGIN,
        end_marker=ROUTINES_MANAGED_END,
        domain="automation",
        label="routines",
        file_path=ROUTINES_FILE,
    )


def _remove_managed_block(config_dir: str) -> bool:
    return remove_split_key(
        config_dir, begin_marker=ROUTINES_MANAGED_BEGIN, end_marker=ROUTINES_MANAGED_END,
    )


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------


async def async_create_or_update_sensa_routines(
    hass: HomeAssistant, overrides: RoutineOverrides,
) -> bool:
    """Rebuild `nomad/routines.yaml` from the customer's routines and ensure
    configuration.yaml is registered. Returns True if configuration.yaml
    changed (first-time registration needs a restart)."""
    config_dir = hass.config.config_dir
    automations = build_automations(overrides, hass)

    def _write_all() -> bool:
        _write_routines_yaml(config_dir, automations)
        return _ensure_registration(config_dir)

    return await hass.async_add_executor_job(_write_all)


async def async_reload_routines(hass: HomeAssistant) -> None:
    """Ask HA to re-read automations without a full restart. Shares
    `automation.reload` with Instincts — reloading the domain always re-reads
    every `automation <label>:` source, ours and the customer's alike."""
    try:
        await hass.services.async_call("automation", "reload", {}, blocking=True)
        _LOGGER.debug("Nomad routines: automation.reload called")
    except Exception:
        _LOGGER.debug(
            "Nomad routines: automation.reload failed — will refresh on the "
            "next HA restart", exc_info=True,
        )


async def async_remove_sensa_routines(hass: HomeAssistant) -> None:
    """Uninstall path: drop the managed block so configuration.yaml doesn't
    include a routines file we no longer generate."""
    config_dir = hass.config.config_dir
    await hass.async_add_executor_job(_remove_managed_block, config_dir)
