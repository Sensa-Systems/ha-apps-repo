"""Mode-editor add-entity eligibility rules.

The Mode Editor's "Add entity" dropdown was previously sourced from
every key in `hass.states`. That's hundreds of entities most of which
make no sense in a scene (sensors, diagnostics, internal config like
Username/Password fields, channel-enabled toggles). This module narrows
the set via an ordered include/exclude pipeline.

Pipeline shape:
  Each rule is `{action, match}`. `action` is "include" or "exclude".
  `match` is a predicate dict; ALL listed fields must match an entity
  for the rule to apply (AND across fields). Rules run top-to-bottom,
  starting from an empty set: an `include` adds matching entities, an
  `exclude` removes them. The resulting set is what the dropdown shows.

Predicate fields supported (each is either a single value or a list):
  domain               — entity_id prefix (e.g. "switch", ["light","switch"])
  vendor_device_type   — Sensa backend device type (e.g. "relay")
  sensa_device         — True for an entity whose HA device belongs to a
                         registered Sensa device (any type), False otherwise
  integration          — config-entry domain (e.g. "esphome", "sensa_nomad")
  entity_category      — "config" | "diagnostic" | None (no category)
  entity_id_contains   — substring on entity_id
  entity_id_regex      — regex on entity_id
  name_contains        — substring on entity_registry.original_name

While the rule set is being tuned the active rules and per-rule match
counts are exposed via `sensor.sensa_nomad_mode_eligibility`'s `rules`
attribute. The Mode Editor renders this as a collapsible debug panel
gated behind `switch.sensa_nomad_debug_mode` (off in production).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant

from .vendors.service_classes import device_kind, is_relay_device

_LOGGER = logging.getLogger(__name__)


# Default rule set. Tune freely while iterating; the debug panel shows
# what each rule actually matches in the current install. The user's
# "exclude relay_2 by name" example from design conversation is NOT a
# default — it's illustrative of what the regex/contains predicates can
# express if added later.
# Scene-actionable domains: what a Mode's scene, or a Routine's "Then Do"
# action, can plausibly set state on. Shared between `DEFAULT_RULES` (the
# Mode Editor) and `ROUTINE_TRIGGER_RULES` (the Routine Editor) below so the
# "what counts as a controllable device" predicate is written once.
_LIGHTING_RELAY_HELPER_INCLUDE_RULES: list[dict[str, Any]] = [
    # Lighting — anywhere, Sensa-managed or customer
    {"action": "include", "match": {"domain": "light"}},

    # Relay outputs. ESPHome native switches whose owning HA device is
    # tied to a Sensa backend row with vendor_device_type == "relay".
    # `_build_entity_context` resolves `vendor_device_type` per entity
    # via the per-device entity-id maps attached upstream
    # (_attach_relay_entities / _attach_bms_entity_prefixes / etc.).
    {"action": "include", "match": {"domain": "switch", "vendor_device_type": "relay"}},

    # The controls of any REGISTERED Sensa device that a scene can write:
    # an Autoterm heater's power switch, mode select, target temperature and
    # power level numbers, a water heater's setpoint. Scoped to Sensa devices
    # so a customer's stray ESPHome node or an integration's config switch
    # is not swept in; a Sensa device's own config / diagnostic entities are
    # dropped by the noise rules below. Added 2026-09-17: the heater was on
    # the Home screen and in Devices but never offered to a scene.
    {
        "action": "include",
        "match": {
            "domain": ["switch", "select", "number", "water_heater", "valve", "siren"],
            "sensa_device": True,
        },
    },

    # Typical scene-relevant domains beyond lighting / relays.
    {
        "action": "include",
        "match": {
            "domain": [
                "climate",
                "fan",
                "cover",
                "humidifier",
                "lock",
                "media_player",
            ],
        },
    },

    # Customer-owned HA helpers — the only `input_*` domain a scene can
    # set state on. Common pattern: customer creates `input_boolean.guest_mode`
    # and wants Sentry mode to flip it on.
    {
        "action": "include",
        "match": {
            "domain": [
                "input_boolean",
                "input_number",
                "input_select",
                "input_text",
            ],
        },
    },
]

# Noise entities to drop regardless of which include rules matched them —
# shared tail for the same reason as the include block above.
_NOISE_EXCLUDE_RULES: list[dict[str, Any]] = [
    # Drop CONFIG / DIAGNOSTIC entities. Sensa marks Username, Password,
    # SSL, signal threshold, channel-enabled, channel-name with
    # EntityCategory.CONFIG; firmware/profile/integrity sensors with
    # DIAGNOSTIC. None are meaningful in a scene.
    {"action": "exclude", "match": {"entity_category": "config"}},
    {"action": "exclude", "match": {"entity_category": "diagnostic"}},

    # Drop disabled relay channels. The customer-facing channel-enabled
    # toggle on the relay card is the source of truth: a channel set to
    # disabled shouldn't appear as a mode-controllable entity. The
    # `relay_channel_enabled` predicate only matches relay-output
    # entities (other entities have it as None and are unaffected).
    {"action": "exclude", "match": {"relay_channel_enabled": False}},

    # Drop the relay controller's Buzzer + Debug Logging switches. The
    # relay firmware exposes both on the SAME HA device as the relay
    # outputs, so `vendor_device_type` resolves to "relay" for them (the
    # device-id map in `_build_entity_context`) and the relay include rule
    # above sweeps them into the dropdown. Neither is a controllable load
    # that belongs in a scene. Scoped to `vendor_device_type == "relay"`
    # so a customer's own "buzzer"/"debug" entity elsewhere is untouched.
    # `name_contains` matches the entity registry `original_name`.
    {
        "action": "exclude",
        "match": {"vendor_device_type": "relay", "name_contains": "Buzzer"},
    },
    {
        "action": "exclude",
        "match": {"vendor_device_type": "relay", "name_contains": "Debug Logging"},
    },
    # The relay controller's onboard RGB status LED (an ESPHome `light.*`
    # whose entity_id carries `led_rgb`) is swept in by the lighting include
    # rule. It's a status indicator, not a controllable load, so drop it.
    # Matched on the stable entity_id slug (survives a friendly-name change).
    {
        "action": "exclude",
        "match": {"vendor_device_type": "relay", "entity_id_contains": "led_rgb"},
    },
    # The same LED on a board Sensa has no device record for: the relay type
    # above never resolves for it, so it read as a light (.177, 2026-11-03).
    # Lights should be real lights. Keyed on the firmware's own entity name,
    # which a relay, heater or any other Sensa board publishes for this LED.
    {
        "action": "exclude",
        "match": {"domain": "light", "integration": "esphome", "name_contains": "LED RGB"},
    },
    # An addressable LED controller (Gledopto GL-SPI-206P) publishes its
    # strip wiring (pixel count, chip type, bead sequence), its phone-mic
    # music mode and sensitivity, and a do-not-disturb switch as plain
    # select / number / switch entities with no entity category, so the
    # Sensa-device include rule above sweeps them into a scene's Add items.
    # None is something a scene sets (Richard, 2026-09-18): the wiring is
    # set once in Settings -> Devices, music does nothing on a Z2M-only
    # install, and do-not-disturb is a power-on behaviour. The effect
    # itself (`_scene`) and `_work_mode` stay: the tile folds them into
    # one Effect dropdown. Matched on the Z2M expose slug in the entity_id.
    *(
        {
            "action": "exclude",
            "match": {"sensa_device": True, "entity_id_contains": slug},
        }
        for slug in (
            "lightpixel_number",
            "chip_type",
            "light_bead_sequence",
            "music_mode",
            "music_sensitivity",
            "do_not_disturb",
            # The Aqara H1's fast / multi press setting, the same plain select.
            # Fast would silently stop every double, triple and hold routine.
            "click_mode",
        )
    ),
]

DEFAULT_RULES: list[dict[str, Any]] = [
    *_LIGHTING_RELAY_HELPER_INCLUDE_RULES,
    *_NOISE_EXCLUDE_RULES,
]

# Routine Editor's "Device" trigger/condition picker. Broader than
# DEFAULT_RULES: a trigger/condition is READ-oriented (any sensor reading is
# fair game), unlike DEFAULT_RULES which is scoped to what a scene can WRITE
# to. The Routine Editor's "Then Do" action picker reuses DEFAULT_RULES
# as-is instead — an action target must be something HA can actuate, which
# is exactly what DEFAULT_RULES already encodes.
ROUTINE_TRIGGER_RULES: list[dict[str, Any]] = [
    *_LIGHTING_RELAY_HELPER_INCLUDE_RULES,
    # `event` is a Zigbee scene button's presses (Z2M publishes each `action`
    # as an `event.*` entity). Trigger-only by nature — an event entity has
    # nothing to write to, so it is deliberately absent from DEFAULT_RULES and
    # never reaches a scene or a Then Do target.
    {"action": "include", "match": {"domain": ["sensor", "binary_sensor", "event"]}},
    *_NOISE_EXCLUDE_RULES,
]

# Domains for the Routine Editor's "Service" trigger group: built-in HA
# integrations exposed as entities, not physical van hardware. Deliberately
# NOT run through the entity-registry rule pipeline above — `sun.sun` in
# particular is often not a registered entity (no unique_id), so scanning
# `hass.states` directly is the only reliable way to surface it.
ROUTINE_SERVICE_DOMAINS = ("sun", "weather")

# The Routine Editor's "Presence" group: who is in the van. `person` is HA's
# own record (the People tab writes it), so the answer already folds every
# device tracker bound to that person together; offering the trackers as well
# would be the same fact twice, and the wrong one to pick.
ROUTINE_PRESENCE_DOMAINS = ("person",)

# The Routine Editor's "Alarms" group. Not a domain scan: an alarm is a Sensa
# concept with one entity, and a `binary_sensor` scan would sweep in every
# device sensor in the van.
ROUTINE_ALARM_ENTITIES = ("binary_sensor.sensa_nomad_alarm_active",)


def compute_domain_entities(hass: HomeAssistant, domains: tuple[str, ...]) -> list[str]:
    """Every live entity in these domains. Deliberately a state-machine scan
    rather than the registry-based rule pipeline `compute_eligible_entities`
    uses: `sun.sun` in particular is often not a registered entity, and a
    `person` is a helper record rather than a device's entity."""
    try:
        entity_ids = hass.states.async_entity_ids()
    except Exception:  # noqa: BLE001
        return []
    return sorted(eid for eid in entity_ids if eid.split(".", 1)[0] in domains)


def compute_service_entities(hass: HomeAssistant) -> list[str]:
    """Entity ids for the Routine Editor's "Sun & weather" trigger group."""
    return compute_domain_entities(hass, ROUTINE_SERVICE_DOMAINS)


def compute_presence_entities(hass: HomeAssistant) -> list[str]:
    """Entity ids for the Routine Editor's "Presence" trigger group."""
    return compute_domain_entities(hass, ROUTINE_PRESENCE_DOMAINS)


def compute_alarm_entities(hass: HomeAssistant) -> list[str]:
    """Entity ids for the Routine Editor's "Alarms" trigger group: the ones
    that exist on this box, so a cold boot offers an empty group rather than a
    card leading nowhere."""
    out = []
    for eid in ROUTINE_ALARM_ENTITIES:
        try:
            if hass.states.get(eid) is not None:
                out.append(eid)
        except Exception:  # noqa: BLE001, S112
            continue
    return out


def _build_entity_context(
    hass: HomeAssistant,
    sensa_devices: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return entity_id → context dict for every registered entity.

    Context fields are exactly what the rule predicates can match on.
    `vendor_device_type` is sparse — populated only for entities tied
    to a Sensa-managed device via the per-vendor entity-id maps the
    integration attaches before dashboard regen
    (`bms_entities` / `network_entities` / `relay_entities`). Entities
    not tied to a Sensa device land with `vendor_device_type=""`.
    """
    try:
        from homeassistant.helpers import entity_registry as er
    except ImportError:
        return {}
    try:
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return {}

    # HA device_id → vendor_device_type map. Built by walking each
    # Sensa device's attached entity-id map and resolving back to its
    # owning HA device via the entity registry.
    ha_dev_to_vdt: dict[str, str] = {}
    # entity_id → "enabled" / "disabled" for relay channel switches.
    # Sourced from `relay_enabled` on the device (already populated by
    # `_attach_relay_entities`). Anything not in this map is "n/a".
    relay_channel_state: dict[str, bool] = {}
    for dev in sensa_devices:
        # A row whose only stated kind is "unknown" is still a Sensa device and
        # must keep marking its entities as one here, so it is kept rather than
        # skipped: `device_kind` reports "unknown" as no answer, which is right
        # for "is this a relay" and wrong for "is this ours".
        vdt = device_kind(dev) or (
            "unknown"
            if any(
                dev.get(key)
                for key in ("vendor_device_type", "device_type", "type", "role")
            )
            else ""
        )
        if not vdt:
            continue
        for attr_name in ("bms_entities", "network_entities", "relay_entities"):
            attached = dev.get(attr_name) or {}
            for eid in attached.values():
                if not eid:
                    continue
                ent = ent_reg.async_get(eid)
                if ent and ent.device_id:
                    ha_dev_to_vdt[ent.device_id] = vdt
        # Devices with no attached entity map (a heater, an air conditioner,
        # a Zigbee light) still own an HA device, reachable by MAC or by
        # Zigbee2MQTT IEEE; without this their entities read as nobody's.
        for ha_id in _ha_device_ids_for_sensa_device(hass, dev):
            ha_dev_to_vdt.setdefault(ha_id, vdt)
        if vdt == "relay":
            relay_entities = dev.get("relay_entities") or {}
            relay_enabled = dev.get("relay_enabled") or {}
            for slot, eid in relay_entities.items():
                if not eid:
                    continue
                # Default True (enabled) if the slot isn't recorded.
                relay_channel_state[eid] = bool(relay_enabled.get(slot, True))

    # config_entry_id → integration domain. Cheaper to compute once
    # than to reach into hass.config_entries per entity.
    entry_to_domain: dict[str, str] = {}
    try:
        for entry in hass.config_entries.async_entries():
            entry_to_domain[entry.entry_id] = entry.domain
    except Exception:  # noqa: BLE001, S110
        pass

    context: dict[str, dict[str, Any]] = {}
    for ent in ent_reg.entities.values():
        # A DISABLED entity has no state at all: HA never creates it. Offering
        # one builds a routine that can never fire, and a condition that can
        # never be true. The Companion app registers a few dozen of these on
        # every phone (Android Auto's car odometer, speed, fuel), all disabled
        # by the integration, and they were filling the picker.
        if getattr(ent, "disabled_by", None) is not None:
            continue
        domain = ent.entity_id.split(".", 1)[0]
        ec = ent.entity_category
        # entity_category is a HA enum-like; .value is the string the
        # rules speak ("config" / "diagnostic"). None when uncategorised.
        ec_value = ec.value if ec is not None else None
        # Only set for entries known to be relay channels; everywhere
        # else it stays None so the predicate doesn't match.
        rce = relay_channel_state.get(ent.entity_id)
        vdt = ha_dev_to_vdt.get(ent.device_id or "", "")
        context[ent.entity_id] = {
            "entity_id": ent.entity_id,
            "domain": domain,
            "entity_category": ec_value,
            "original_name": ent.original_name or "",
            "device_id": ent.device_id or "",
            "integration": entry_to_domain.get(ent.config_entry_id or "", ""),
            "vendor_device_type": vdt,
            "sensa_device": bool(vdt),
            "relay_channel_enabled": rce,
        }
    return context


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def _entity_matches(
    ctx: dict[str, Any], match: dict[str, Any]
) -> bool:
    """Return True iff every listed predicate field matches the entity."""
    if "domain" in match and ctx["domain"] not in _as_list(match["domain"]):
        return False
    if "vendor_device_type" in match and (
        ctx["vendor_device_type"] not in _as_list(match["vendor_device_type"])
    ):
        return False
    if "sensa_device" in match and (
        bool(ctx.get("sensa_device")) != bool(match["sensa_device"])
    ):
        return False
    if "integration" in match and (
        ctx["integration"] not in _as_list(match["integration"])
    ):
        return False
    # Compare against literal None too — `None` means "no category"
    # which is the most common case for operational entities.
    if "entity_category" in match and (
        ctx["entity_category"] != match["entity_category"]
    ):
        return False
    if "entity_id_contains" in match and (
        match["entity_id_contains"] not in ctx["entity_id"]
    ):
        return False
    if "entity_id_regex" in match:
        try:
            if not re.search(match["entity_id_regex"], ctx["entity_id"]):
                return False
        except re.error:
            return False
    if "name_contains" in match and (
        match["name_contains"] not in (ctx["original_name"] or "")
    ):
        return False
    # ctx value is True / False / None (None = not a relay channel).
    # The predicate only matches rows that are relay channels with
    # the requested enabled-state.
    return "relay_channel_enabled" not in match or (
        ctx.get("relay_channel_enabled") == match["relay_channel_enabled"]
    )


def compute_eligible_entities(
    hass: HomeAssistant,
    sensa_devices: list[dict[str, Any]],
    rules: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return `(eligible_entity_ids, rule_traces)`.

    `rule_traces` is the same length / order as `rules`; each entry
    carries `{action, match, matched_count, matched_entities}`. The
    sensor exposes the trace verbatim so the debug panel can render
    per-rule effect.

    `matched_entities` is capped to keep the sensor attribute payload
    bounded; the count is reported as the true total separately.
    """
    rules = rules if rules is not None else DEFAULT_RULES
    context = _build_entity_context(hass, sensa_devices)

    eligible: set[str] = set()
    traces: list[dict[str, Any]] = []

    for rule in rules:
        action = rule.get("action")
        match = rule.get("match") or {}
        matched = sorted(
            eid for eid, ctx in context.items()
            if _entity_matches(ctx, match)
        )
        if action == "include":
            eligible.update(matched)
        elif action == "exclude":
            eligible.difference_update(matched)
        else:
            _LOGGER.warning("mode_eligibility: unknown rule action %r", action)
        traces.append({
            "action": action,
            "match": dict(match),
            "matched_count": len(matched),
            # Cap to keep sensor.attributes payload reasonable. Debug
            # panel pages through this; full audit happens in logs if
            # ever needed.
            "matched_entities": matched[:50],
        })

    return sorted(eligible), traces


def _light_output_names(
    hass: HomeAssistant, sensa_devices: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """HA device id -> the name of a Shelly light output, from the one place an
    output gets its name (`shelly_light_outputs`): what the customer called it
    on its Devices row, else numbered. HA's own child device carries the name
    the unit reports for that channel, which is not what the Lighting tab shows."""
    names: dict[str, str] = {}
    try:
        from homeassistant.helpers import entity_registry as er

        from .vendors.shelly import shelly_light_outputs
        ent_reg = er.async_get(hass)
        for dev in sensa_devices or []:
            outputs = shelly_light_outputs(hass, ent_reg, dev)
            if len(outputs) < 2:
                continue
            for _channel, entity_id, name in outputs:
                ent = ent_reg.async_get(entity_id)
                if ent is not None and ent.device_id:
                    names[ent.device_id] = name
    except Exception:  # noqa: BLE001 - names only, the picker works without them
        return names
    return names


def compute_entity_devices(
    hass: HomeAssistant, entity_ids: list[str],
    sensa_devices: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, str]]:
    """entity_id -> {device_id, device_name} for its owning HA device, for
    the Routine Editor's "Then Do" DEVICE target mode.

    HA's own action-targeting model (see
    home-assistant.io/docs/scripts/perform-actions) lets a service call's
    `target` carry `entity_id`, `device_id`, or `area_id` — a device target
    runs the service against every entity on that device supporting it
    (e.g. `light.turn_on` with `target: {device_id: ...}` turns on every
    light on that device). This is that same mechanism, not HA's separate
    per-integration `device_action` platform (which most of this
    integration's ESPHome/MQTT-backed devices don't implement, so it would
    list nothing useful) — the Routine Editor picks a device then infers
    the service from whichever entity domain the customer's toggle implies,
    the same domain inference `routines._action_dict` already does for a
    single-entity target.

    Entities with no owning device (a helper, a template) are omitted; the
    Editor's Entity picker mode already covers those.
    """
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
    except Exception:  # noqa: BLE001
        return {}
    output_names = _light_output_names(hass, sensa_devices)
    result: dict[str, dict[str, str]] = {}
    for eid in entity_ids:
        try:
            ent = ent_reg.async_get(eid)
        except Exception:  # noqa: BLE001
            ent = None
        if ent is None or not ent.device_id:
            continue
        try:
            device = dev_reg.async_get(ent.device_id)
        except Exception:  # noqa: BLE001
            device = None
        name = None
        if device is not None:
            name = getattr(device, "name_by_user", None) or getattr(device, "name", None)
        name = output_names.get(ent.device_id) or name
        result[eid] = {"device_id": ent.device_id, "device_name": name or ent.device_id}
    return result


def compute_entity_names(
    hass: HomeAssistant,
    sensa_devices: list[dict[str, Any]],
    entity_ids: list[str],
) -> dict[str, str]:
    """Best human display name per entity_id, for the Mode Editor.

    The editor's Add dropdown reads `friendly_name`, but once an entity is in
    a scene the row needs a name too — and for a relay CHANNEL the meaningful
    name is the customer's Switching-tab label (the source of truth in
    `entry.options["relay_labels"]`, surfaced on the device row as
    `relay_labels`), not the generic ESPHome "Relay 1". This resolves, per id:

      1. the relay channel label (so a scene row reads "Galley Lights", not
         `switch.x_relay_1`),
      2. else the live `friendly_name`,
      3. else the entity registry name / original_name (so a registry-known
         entity with no live state still gets a name, not a raw slug),
      4. else the entity_id.

    Both the dropdown and the rows read this map, so they always agree.
    """
    relay_label: dict[str, str] = {}
    for dev in sensa_devices:
        if not is_relay_device(dev):
            continue
        relay_entities = dev.get("relay_entities") or {}
        relay_labels = dev.get("relay_labels") or {}
        for slot, eid in relay_entities.items():
            if not eid:
                continue
            label = (relay_labels.get(slot) or "").strip()
            if label:
                relay_label[eid] = label

    ent_reg = None
    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        ent_reg = None

    names: dict[str, str] = {}
    for eid in entity_ids:
        label = relay_label.get(eid)
        if label:
            names[eid] = label
            continue
        try:
            state = hass.states.get(eid)
        except Exception:  # noqa: BLE001
            state = None
        friendly = (state.attributes or {}).get("friendly_name") if state else None
        if isinstance(friendly, str) and friendly.strip():
            names[eid] = friendly
            continue
        if ent_reg is not None:
            ent = ent_reg.async_get(eid)
            registry_name = (ent.name or ent.original_name) if ent else None
            if registry_name:
                names[eid] = registry_name
                continue
        names[eid] = eid
    return names


# What an entity IS, for a customer who never reads an entity id. The
# picker in the Scenes popup shows this beside the name (icon + label), so
# "Galley Light" reads as a light and "Water Pump" as a water pump without
# `light.0x00158d0053494d0c` ever appearing.
#
# Resolution, most specific first:
#   1. a relay CHANNEL: the Switched-Device type the customer assigned it on
#      Settings -> Switching (its service class: label, icon, category), or
#      plain "Switch" when unassigned;
#   2. an entity owned by a registered Sensa device (matched through the HA
#      device registry: a Zigbee2MQTT device by its IEEE identifier, anything
#      else by its MAC connection): that device's service class;
#   3. the entity's DOMAIN, which is what HA itself knows (a light is a
#      light, a climate entity is climate).
# The service-class table owns every label / icon / category it can, so a
# rename there reaches the picker; only the domain fallback is local.
_DOMAIN_TYPES: dict[str, tuple[str, str, str]] = {
    "light": ("Light", "mdi:lightbulb", "lighting"),
    "switch": ("Switch", "mdi:toggle-switch", "switching"),
    "input_boolean": ("Switch", "mdi:toggle-switch", "switching"),
    "fan": ("Fan", "mdi:fan", "climate"),
    "climate": ("Climate", "mdi:thermostat", "climate"),
    "humidifier": ("Humidifier", "mdi:air-humidifier", "climate"),
    "water_heater": ("Water Heater", "mdi:water-boiler", "water"),
    "valve": ("Valve", "mdi:valve", "water"),
    "cover": ("Cover", "mdi:window-shutter", "vehicle"),
    "lock": ("Lock", "mdi:lock", "security"),
    "siren": ("Siren", "mdi:bullhorn", "security"),
    "media_player": ("Media", "mdi:speaker", "media"),
    "select": ("Setting", "mdi:form-select", "settings"),
    "input_select": ("Setting", "mdi:form-select", "settings"),
    "number": ("Setting", "mdi:numeric", "settings"),
    "input_number": ("Setting", "mdi:numeric", "settings"),
    "text": ("Setting", "mdi:form-textbox", "settings"),
    "input_text": ("Setting", "mdi:form-textbox", "settings"),
    # A bare sensor is NOT an environment sensor. Its category comes from its
    # device class below; with none it lands in the picker's "Other" group,
    # which reads honestly, where "Environment" was a confident lie about a
    # battery reading or a router's signal.
    "sensor": ("Sensor", "mdi:eye-outline", ""),
    "binary_sensor": ("Sensor", "mdi:eye-outline", ""),
    # Routine-only domains: never scene-actionable, but a Routine's When can
    # start from any of them, and an untyped one lands in the picker's "Other"
    # group.
    #
    # An `event` entity is NOT necessarily a button. A REGISTERED scene button
    # is typed by its service class long before this table is reached, and
    # Zigbee2MQTT publishes its `action` event entity with no device class at
    # all (read in the container: ENUM_DISCOVERY_LOOKUP.action carries only an
    # icon), so this fallback only ever sees an event nobody owns - HA's own
    # `event.backup_automatic_backup`, which was reading as a Button under
    # Switching. No category, so it files under Other.
    "event": ("Event", "mdi:flash-alert", ""),
    "sun": ("Sun", "mdi:weather-sunny", "environment"),
    "weather": ("Weather", "mdi:weather-partly-cloudy", "environment"),
    "person": ("Person", "mdi:account", "presence"),
}


# What a sensor MEASURES, for an entity no Sensa device owns (a customer's own
# probe, an HA helper). HA's own device classes, folded into our categories;
# anything unlisted keeps the domain's category and, for a sensor, that is
# nothing, so it reads as "Other".
_DEVICE_CLASS_TYPES: dict[str, tuple[str, str, str]] = {
    "temperature": ("Temperature", "mdi:thermometer", "environment"),
    "humidity": ("Humidity", "mdi:water-percent", "environment"),
    "pressure": ("Pressure", "mdi:gauge", "environment"),
    "atmospheric_pressure": ("Pressure", "mdi:gauge", "environment"),
    "carbon_dioxide": ("Air Quality", "mdi:molecule-co2", "environment"),
    "carbon_monoxide": ("Air Quality", "mdi:molecule-co", "environment"),
    "illuminance": ("Light Level", "mdi:brightness-6", "environment"),
    "battery": ("Battery", "mdi:battery", "electrical"),
    "voltage": ("Voltage", "mdi:flash", "electrical"),
    "current": ("Current", "mdi:current-dc", "electrical"),
    "power": ("Power", "mdi:flash", "electrical"),
    "energy": ("Energy", "mdi:lightning-bolt", "electrical"),
    "moisture": ("Water Leak", "mdi:water-alert", "water"),
    "water": ("Water", "mdi:water", "water"),
    "motion": ("Motion", "mdi:motion-sensor", "security"),
    # HA's three EVENT device classes (event/__init__.py: DOORBELL, BUTTON,
    # MOTION). An entity that does declare one is a button or a doorbell for
    # real; one that declares nothing is not assumed to be either.
    "button": ("Button", "mdi:gesture-tap-button", "switching"),
    "doorbell": ("Doorbell", "mdi:doorbell", "security"),
    "occupancy": ("Occupancy", "mdi:motion-sensor", "security"),
    "presence": ("Presence", "mdi:account", "presence"),
    "door": ("Door", "mdi:door", "security"),
    "garage_door": ("Door", "mdi:garage", "security"),
    "window": ("Window", "mdi:window-closed", "security"),
    "opening": ("Opening", "mdi:door-open", "security"),
    "lock": ("Lock", "mdi:lock", "security"),
    "smoke": ("Smoke", "mdi:smoke-detector", "security"),
    "gas": ("Gas", "mdi:gas-cylinder", "security"),
    "tamper": ("Tamper", "mdi:vibrate", "security"),
    "vibration": ("Vibration", "mdi:vibrate", "security"),
    "sound_pressure": ("Sound", "mdi:volume-high", "media"),
    "connectivity": ("Connection", "mdi:lan-connect", "network"),
    "signal_strength": ("Signal", "mdi:signal", "network"),
    "data_rate": ("Data Rate", "mdi:speedometer", "network"),
    "data_size": ("Data", "mdi:database", "network"),
    "speed": ("Speed", "mdi:speedometer", "vehicle"),
    "distance": ("Distance", "mdi:map-marker-distance", "vehicle"),
}


def _type_of_device_class(device_class: str | None) -> dict[str, str] | None:
    row = _DEVICE_CLASS_TYPES.get(str(device_class or "").strip().lower())
    if not row:
        return None
    return {"label": row[0], "icon": row[1], "category": row[2]}


def _type_of_row_category(dev: dict[str, Any]) -> dict[str, str] | None:
    """What a device IS, taken from its own discovery row's `category` when no
    service class claims it. The row's category is what the Devices picker
    shows, so the two surfaces agree by construction."""
    category = str(dev.get("category") or "").strip().lower()
    if not category or category == "unknown":
        return None
    try:
        from .device_catalog import CATEGORY_TITLES
    except Exception:  # noqa: BLE001
        return None
    title = CATEGORY_TITLES.get(category)
    if not title:
        return None
    return {"label": title, "icon": "mdi:devices", "category": category}


def _type_of_service_class(key: str | None) -> dict[str, str] | None:
    if not key:
        return None
    try:
        from .vendors.service_classes import SERVICE_CLASSES
    except Exception:  # noqa: BLE001
        return None
    sc = SERVICE_CLASSES.get(key)
    if sc is None:
        return None
    return {"label": sc.label, "icon": sc.icon, "category": sc.category}


def _ha_device_name(hass: HomeAssistant, ha_device_id: str) -> str:
    """What HA calls one of its own devices, the customer's rename winning."""
    try:
        from homeassistant.helpers import device_registry as dr
        found = dr.async_get(hass).devices.get(ha_device_id)
    except Exception:  # noqa: BLE001
        return ""
    if found is None:
        return ""
    return str(getattr(found, "name_by_user", "") or getattr(found, "name", "") or "")


def _ha_device_ids_for_sensa_device(
    hass: HomeAssistant, dev: dict[str, Any], *, roots_only: bool = False,
) -> list[str]:
    """The HA device registry ids a Sensa device row resolves to: its
    Zigbee2MQTT IEEE identifier, the Sensa identifier its vendor module mints,
    or a MAC connection — plus every HA device nested under those through
    `via_device`. Empty when HA has no device for it (a pure-scan discovery, a
    cloud-only device).

    The identifier path is what makes the vendor devices resolve at all. A
    Cerbo, a Teltonika router and a Starlink are registered by our OWN vendor
    modules as `("sensa_nomad", f"{prefix}{device_id}")` with NO MAC
    connection, so a MAC-only lookup found nothing for any of them and every
    one of their entities fell through to the domain table — which is how a
    battery reading and a router's signal both ended up filed under
    "Environment" in the Routines picker (measured on the Container box
    2026-09-19: 17 HA devices, not one with a MAC connection).

    `via_device` matters for the same reason: a Cerbo's Venus sub-devices
    (Battery, MultiPlus, Orion) are SEPARATE HA devices carrying most of the
    interesting sensors, and they inherit the Cerbo's kind."""
    ids: list[str] = []
    try:
        from .vendors.zigbee2mqtt import zigbee2mqtt_ha_device_id
        z2m = zigbee2mqtt_ha_device_id(hass, dev)
        if z2m:
            ids.append(z2m)
    except Exception:  # noqa: BLE001, S110
        pass

    dev_reg = None
    try:
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(hass)
    except Exception:  # noqa: BLE001
        dev_reg = None

    device_id = str(dev.get("device_id") or "").strip()
    if dev_reg is not None and device_id:
        # The same prefix list device removal resolves against, imported late
        # (the package imports this module) so the two can never drift.
        try:
            from . import _SENSA_DEVICE_IDENTIFIER_PREFIXES as _prefixes
            from .const import DOMAIN
        except Exception:  # noqa: BLE001
            _prefixes = ()
            DOMAIN = ""
        for prefix in _prefixes:
            try:
                found = dev_reg.async_get_device(
                    identifiers={(DOMAIN, f"{prefix}{device_id}")}
                )
            except Exception:  # noqa: BLE001
                found = None
            if found is not None:
                if found.id not in ids:
                    ids.append(found.id)
                break

    mac = str(
        dev.get("linked_mac_address")
        or dev.get("linked_normalized_mac")
        or dev.get("mac_address")
        or ""
    ).strip().lower().replace("-", ":")
    if dev_reg is not None and mac:
        try:
            from homeassistant.helpers import device_registry as dr
            found = dev_reg.async_get_device(
                connections={(dr.CONNECTION_NETWORK_MAC, mac)}
            )
            if found is not None and found.id not in ids:
                ids.append(found.id)
        except Exception:  # noqa: BLE001, S110
            pass

    if roots_only:
        return ids

    if dev_reg is not None and ids:
        try:
            from . import _collect_via_device_descendants
            everything = list(dev_reg.devices.values())
            ids = [
                ha_id for ha_id in _collect_via_device_descendants(everything, set(ids))
            ]
        except Exception:  # noqa: BLE001, S110
            pass
    return ids


def _device_class_of(hass: HomeAssistant, ent: Any, entity_id: str) -> str:
    """An entity's device class: the registry's override, its original, else
    the live state's attribute (a vendor entity often declares it only there)."""
    for attr in ("device_class", "original_device_class"):
        try:
            value = getattr(ent, attr, None)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return str(value)
    try:
        state = hass.states.get(entity_id)
        value = (state.attributes or {}).get("device_class") if state else None
    except Exception:  # noqa: BLE001
        value = None
    return str(value or "")


def compute_entity_types(
    hass: HomeAssistant,
    sensa_devices: list[dict[str, Any]],
    entity_ids: list[str],
) -> dict[str, dict[str, str]]:
    """entity_id -> {label, icon, category}: what the entity is, for the
    Scenes popup's Add-items picker and the Routines popup's grouped entity
    pickers. See the module note above `_DOMAIN_TYPES` for the resolution
    order."""
    from .device_catalog import CATEGORY_TITLES
    from .vendors.service_classes import classify

    # 1. Relay channels, by their assigned Switched-Device type.
    relay_type: dict[str, dict[str, str]] = {}
    # 2. Every other Sensa device, by HA device id -> its service class,
    #    plus the device itself (id + name) so the picker and the tiles can
    #    show a device as ONE item with its controls inside (the Autoterm
    #    heater is a power switch, a mode, a setpoint and a power level;
    #    Richard, 2026-09-18: "1 device just like an air con").
    by_ha_device: dict[str, dict[str, str]] = {}
    output_names = _light_output_names(hass, sensa_devices)
    for dev in sensa_devices:
        if is_relay_device(dev):
            assignments = dev.get("relay_assignments") or {}
            for slot, eid in (dev.get("relay_entities") or {}).items():
                if not eid:
                    continue
                relay_type[eid] = (
                    _type_of_service_class(assignments.get(slot))
                    or {"label": "Switch", "icon": "mdi:toggle-switch", "category": "switching"}
                )
            continue
        try:
            sc_type = _type_of_service_class(classify(dev))
        except Exception:  # noqa: BLE001
            sc_type = None
        if not sc_type:
            # No service class, but the device row still carries the category
            # the Devices picker files it under. Better a device in the right
            # group with a plain label than one missing from the picker.
            sc_type = _type_of_row_category(dev)
        if not sc_type:
            continue
        device = {
            "device_id": str(dev.get("device_id") or ""),
            "device_name": str(dev.get("name") or dev.get("display_name") or sc_type["label"]),
        }
        root_ids = set(_ha_device_ids_for_sensa_device(hass, dev, roots_only=True))
        for ha_id in _ha_device_ids_for_sensa_device(hass, dev):
            entry = {**sc_type, **device}
            if ha_id not in root_ids:
                # A `via_device` child names itself better than its parent
                # does: forty cards all reading "Victron Cerbo GX" say less
                # than "Battery #278" and "Multiplus #276".
                child_name = _ha_device_name(hass, ha_id)
                if child_name:
                    entry = {**entry, "device_name": child_name}
            if ha_id in output_names:
                entry = {**entry, "device_name": output_names[ha_id]}
            by_ha_device.setdefault(ha_id, entry)

    ent_reg = None
    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        ent_reg = None

    types: dict[str, dict[str, str]] = {}
    for eid in entity_ids:
        found = relay_type.get(eid)
        ent = None
        if ent_reg is not None:
            try:
                ent = ent_reg.async_get(eid)
            except Exception:  # noqa: BLE001
                ent = None
        if found is None and by_ha_device and ent is not None and ent.device_id:
            found = by_ha_device.get(ent.device_id)
            if found is not None:
                # The control's own short name ("Power", "Mode"), for the
                # rows inside the device's tile. The registry's original
                # name is the one without the device name in front.
                control = ent.name or ent.original_name or ""
                # A light output's own name is the title, not a control of it.
                if control == found.get("device_name"):
                    control = ""
                found = {**found, "control": str(control)}
        if found is None:
            found = _type_of_device_class(_device_class_of(hass, ent, eid))
        if found is None:
            domain = eid.split(".", 1)[0]
            dom = _DOMAIN_TYPES.get(domain)
            if dom:
                found = {"label": dom[0], "icon": dom[1], "category": dom[2]}
        if found is not None:
            found = dict(found)
            # The customer-facing word for the category, resolved here rather
            # than in each picker: the slug ("electrical") is never shown.
            title = CATEGORY_TITLES.get(str(found.get("category") or ""))  # "" -> Other
            if title:
                found["category_label"] = title
            if eid.startswith("event.") and ent is not None:
                presses = _zigbee_presses(hass, ent.device_id)
                if presses:
                    found["presses"] = presses
            # By the expose's own name: `Action from side` is a different
            # reading whose entity_id also ends in `_side`.
            if eid.startswith("sensor.") and ent is not None and ent.original_name == "Side":
                values = _zigbee_side_values(hass, ent.device_id)
                if values:
                    found["values"] = values
            if _is_temperature_target(eid):
                found["temperature_target"] = True
            types[eid] = found
    return types


def _is_temperature_target(entity_id: str) -> bool:
    """A setpoint Temperature by turn can move: an air conditioner or other
    climate unit, SensaClimate's target, or a heater's own target (the Autoterm
    firmware's `Target Temperature`, the Baltica's `Heating Temperature`)."""
    from .const import CLIMATE_CONTROL_TARGET_ENTITY_ID

    if entity_id.startswith("climate.") or entity_id == CLIMATE_CONTROL_TARGET_ENTITY_ID:
        return True
    return entity_id.startswith("number.") and entity_id.endswith(
        ("_target_temperature", "_heating_temperature"),
    )


def _zigbee_side_values(hass: HomeAssistant, ha_device_id: str | None) -> list[str]:
    """The values a routine can pick for the `Side` of the Z2M device behind
    this HA device. Without them a sensor's Equals has nothing to pick, and
    stays on a value no side ever reads."""
    if not ha_device_id:
        return []
    try:
        from homeassistant.helpers import device_registry as dr

        from .vendors.zigbee2mqtt import bridge_side_values, zigbee2mqtt_device_ieee

        ha_dev = dr.async_get(hass).devices.get(ha_device_id)
        ieee = zigbee2mqtt_device_ieee(ha_dev) if ha_dev is not None else None
        return bridge_side_values(hass, ieee) if ieee else []
    except Exception:  # noqa: BLE001
        return []


def _zigbee_presses(hass: HomeAssistant, ha_device_id: str | None) -> list[str]:
    """The raw actions of the Z2M device behind an `event.*` entity, for the
    Routine Editor's press dropdown. Empty for anything else, and the card
    then falls back to the entity's own `event_types`."""
    if not ha_device_id:
        return []
    try:
        from homeassistant.helpers import device_registry as dr

        from .vendors.zigbee2mqtt import bridge_press_values, zigbee2mqtt_device_ieee

        ha_dev = dr.async_get(hass).devices.get(ha_device_id)
        ieee = zigbee2mqtt_device_ieee(ha_dev) if ha_dev is not None else None
        return bridge_press_values(hass, ieee) if ieee else []
    except Exception:  # noqa: BLE001
        return []
