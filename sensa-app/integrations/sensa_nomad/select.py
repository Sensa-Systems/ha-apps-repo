"""Select platform for Sensa Nomad.

Currently exposes one entity:
  • `select.sensa_nomad_cab_nav_source` — drives the Cab Overview's
    navigation tool dropdown. State value is one of:
      "HA Map" | "Google Maps" | "Sensa Radar"
    The Cab dashboard uses three sibling `conditional` cards, each
    gating on this select's state, so swapping the dropdown swaps the
    rendered map card instantly with no reload.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    APP_USAGE_INTERVAL_ENTITY_ID,
    APP_USAGE_INTERVAL_OPTIONS,
    APP_USAGE_REFRESH_SECONDS,
    CAB_LOCATION_AUTO,
    CAB_LOCATION_SELECT_ENTITY_ID,
    CAB_LOCATION_VEHICLE,
    CAB_VEHICLE_TRACKER_ENTITY_ID,
    CLIMATE_CONTROL_SELECTS,
    CRYPTO_ROOM_NOT_MONITORED,
    CRYPTO_ROOM_SENSOR_SELECT_ENTITY_ID,
    DEPLOYMENT_MODE_APPLIANCE,
    DEPLOYMENT_MODE_KEY,
    DOMAIN,
    HOME_LAYOUT_COLUMN_TO_LABEL,
    HOME_LAYOUT_LABEL_TO_COLUMN,
    HOME_LAYOUT_OFF_LABEL,
    HOME_LAYOUT_PLACEMENT_OPTIONS,
    HOME_LAYOUT_SURFACES,
    HOST_UPDATE_WINDOW_DEFAULT,
    HOST_UPDATE_WINDOW_OPTIONS,
    HOST_UPDATE_WINDOW_SELECT_ENTITY_ID,
    LED_STRIP_CUSTOM,
    LED_STRIP_DEFAULT,
    LED_STRIP_LABELS,
    LED_STRIP_OPTIONS,
    LED_STRIP_PROFILES,
    LIGHT_TYPE_AUTO,
    RUNTIME_BACKUP_STATUS,
    SWITCH_PORT_ASSIGNMENTS_OPTION,
    UPDATE_CHANNEL_DEFAULT,
    UPDATE_CHANNEL_OPTIONS,
    UPDATE_CHANNEL_SELECT_ENTITY_ID,
    VAN_LEVEL_AXIS_OPTIONS,
    VAN_LEVEL_DEFAULT_AXIS_MAP,
    VAN_LEVEL_LOGICAL_AXES,
    VAN_LEVEL_LOGICAL_LABELS,
    home_layout_placement_select_entity_id,
    led_strip_profile_label,
    led_strip_select_entity_id,
    light_type_select_entity_id,
    normalise_led_strip_profile,
    relay_channel_assignment_entity_id,
    switch_port_assignment_entity_id,
    van_level_axis_select_entity_id,
    van_level_sign_select_entity_id,
)
from .vendors.service_classes import (
    RELAY_ASSIGNABLE_SERVICE_CLASSES,
    RELAY_UNASSIGNED,
    is_relay_device,
)

_LOGGER = logging.getLogger(__name__)


CAB_NAV_HA_MAP = "HA Map"
CAB_NAV_OPTIONS: list[str] = [CAB_NAV_HA_MAP, "Google Maps", "Sensa Radar"]
# Sensa Radar is the default nav surface (Stage 0 of the Radar integration).
# Google Maps + HA Map stay selectable on BYO. RestoreEntity means existing
# installs keep whatever the customer last picked; only fresh installs take this
# default.
CAB_NAV_DEFAULT: str = "Sensa Radar"

# Navigation units, a Nomad-owned setting SEPARATE from HA's system unit. HA has
# one length unit (metric/imperial) and can't express the UK convention of metric
# for everything EXCEPT roads, where miles are used. So the Cab nav surfaces (the
# Radar map embed + the Nearby distances) read THIS instead of HA's unit_system,
# giving one consistent answer the customer controls. The option strings are what
# the picker shows; `nav_units_code()` maps them to the "miles"/"km" the map embed
# and the card use.
NAV_UNITS_OPTIONS: list[str] = ["Miles", "Kilometres"]
# Default Miles: navigation is roads, and roads are miles in the UK (the primary
# market). A metric-roads customer flips it once; RestoreEntity keeps the choice.
NAV_UNITS_DEFAULT: str = "Miles"


def nav_units_code(option: str | None) -> str:
    """Map the picker option to the wire code the map + card use: 'miles' / 'km'."""
    return "miles" if str(option or "").strip().lower().startswith("mile") else "km"


# What the van runs on, so the fuel pins on the Radar map can show the price of
# the fuel this customer actually buys. A station publishes several prices at
# once (petrol, super, diesel...), so without knowing the fuel there is no
# honest single number to put on a pin: the cheapest is almost always standard
# petrol, which a diesel van would misread on every station.
#
# Deliberately a Nomad setting rather than the Radar account's vehicle profile:
# the profile is a Premium dimensions record the customer fills in on the web,
# the van does not know it, and writing to it from here would overwrite what
# they entered.
NAV_FUEL_TYPE_OPTIONS: list[str] = ["Diesel", "Petrol", "Super unleaded", "LPG"]
# Diesel leads and is the default: near enough every motorhome and van is diesel,
# so most customers never touch this. RestoreEntity keeps a change.
NAV_FUEL_TYPE_DEFAULT: str = "Diesel"

# Picker option -> RADAR's own price key (the keys inside a place's `prices`
# map, see Sensa.Radar services/fuel_finder.py + fr/es/de_fuel_prices.py). Sent
# to the map embed as-is, so these strings must match Radar's vocabulary; a typo
# is a pin with no price rather than a visible error.
_NAV_FUEL_PRICE_KEYS: dict[str, str] = {
    "Diesel": "diesel",
    "Petrol": "e10",
    "Super unleaded": "e5",
    "LPG": "lpg",
}


def nav_fuel_price_key(option: str | None) -> str:
    """Map the picker option to Radar's price key ('diesel' / 'e10' / ...).

    An unknown option resolves to the default's key rather than "" -- a value we
    cannot read is not a reason to strip the price off every pin.
    """
    return _NAV_FUEL_PRICE_KEYS.get(
        str(option or "").strip(),
        _NAV_FUEL_PRICE_KEYS[NAV_FUEL_TYPE_DEFAULT],
    )


def _cab_nav_options(hass) -> list[str]:
    """The nav sources offered on THIS box.

    HA Map is dropped on an appliance. It renders HA's built-in `map` card,
    which needs the Lovelace card engine (`window.loadCardHelpers`). A
    locked-down appliance has NO native Lovelace dashboard, so HA never
    registers the `ha-panel-lovelace` route that `ensureLovelaceEngine` force-
    loads from, and the map can never render there (on-box 2026-07-20:
    `[sensa-ha-map] Lovelace engine did not expose loadCardHelpers`). Radar and
    Google Maps are pure iframes and always work, so on an appliance HA Map is
    both broken and redundant. BYO keeps it: the customer has a full HA with a
    real Lovelace engine.
    """
    from .const import DEPLOYMENT_MODE_APPLIANCE, DEPLOYMENT_MODE_KEY

    mode = (hass.data.get(DOMAIN, {}) or {}).get(DEPLOYMENT_MODE_KEY)
    if mode == DEPLOYMENT_MODE_APPLIANCE:
        return [o for o in CAB_NAV_OPTIONS if o != CAB_NAV_HA_MAP]
    return list(CAB_NAV_OPTIONS)

# Power-flow wire-animation styles — MUST match the AnimStyle union shared by
# BOTH power-flow cards (webcards/src/sensa-power-flow-detail-card.ts, whose
# header picker writes this entity, and webcards/src/sensa-power-flow-card.ts,
# the basic Home card, which reads it). Stored as the raw style keys; the cards
# render friendly labels. Default "dots" on a fresh install.
POWER_FLOW_ANIM_OPTIONS: list[str] = [
    "comet", "dots", "glow", "snake", "wave", "sparks", "pulse", "off",
]
POWER_FLOW_ANIM_DEFAULT: str = "dots"

# Victron VE.Bus (Multiplus / Quattro) inverter Mode. The register ints are
# Victron's own: 1=charger-only, 2=inverter-only, 3=on (inverter+charger),
# 4=off. We expose customer-friendly labels and map both ways. Kept decoupled
# from the read-only sensor's `_VEBUS_MODE_LABELS` slugs (victron_mqtt_services)
# on purpose: the sensor is a status string, this is a control vocabulary.
INVERTER_MODE_OPTIONS: list[str] = [
    "Off", "Charger only", "Inverter only", "On",
]
_VEBUS_MODE_OPTION_TO_INT: dict[str, int] = {
    "Off": 4, "Charger only": 1, "Inverter only": 2, "On": 3,
}
_VEBUS_INT_TO_MODE_OPTION: dict[int, str] = {
    v: k for k, v in _VEBUS_MODE_OPTION_TO_INT.items()
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad select entities."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    # Expose the add callback so a relay registered post-setup (via the
    # Discovery picker) gets its per-channel "Works with" selects created
    # without a reload — mirrors vendor_add_text / vendor_add_switches.
    runtime["vendor_add_selects"] = async_add_entities

    # Drain any selects a vendor coordinator queued before this platform
    # finished setting up (e.g. a Multiplus inverter-mode select built by
    # the Victron on_new_device hook during the setup race). Mirrors the
    # vendor_pending_sensors flush in sensor.py — without it those selects
    # were silently dropped until a full HA restart.
    pending_selects = runtime.pop("vendor_pending_selects", None)
    if pending_selects:
        async_add_entities(pending_selects)

    entities: list[SelectEntity] = [
        SensaCabNavSourceSelect(entry),
        SensaNavUnitsSelect(entry),
        SensaNavFuelTypeSelect(entry),
        SensaCabLocationSourceSelect(hass, entry),
        SensaCabMemberSelect(entry),
        SensaPowerFlowAnimationSelect(entry),
        SensaAppUsageIntervalSelect(entry),
        SensaCryptoRoomSensorSelect(hass, entry),
        *(
            SensaClimateControlSelect(hass, entry, key)
            for key in CLIMATE_CONTROL_SELECTS
        ),
    ]

    # Retire the Log Level select, replaced by the card's per-type chips.
    _async_remove_legacy_log_level_select(hass, entry)

    # Host-OS update maintenance window — CONTAINER appliance only. The window
    # is enforced by the Debian `apt` host agent, which only exists on the
    # compose (no-Supervisor) appliance; a BYO host is the customer's own OS and
    # HAOS is updated through HA's native UI, so neither has a Sensa host agent.
    from .appliance import is_supervised

    if (
        hass.data.get(DOMAIN, {}).get(DEPLOYMENT_MODE_KEY) == DEPLOYMENT_MODE_APPLIANCE
        and not is_supervised(hass)
    ):
        entities.append(SensaHostUpdateWindowSelect(hass, entry))
        entities.append(SensaUpdateChannelSelect(hass, entry))

    # One "LED Strip" (colour-order) select per addressable Zigbee LED
    # controller, so the customer can correct a GRB strip that reads green-as-red
    # without ever opening Zigbee2MQTT.
    entities.extend(build_led_strip_selects(hass, entry))

    # One "Light Type" select per Zigbee LED controller/bulb, so a colour-capable
    # controller driving a white strip (e.g. the Skydance WZ5) can hide the
    # colour mixer / colour-temperature controls.
    entities.extend(build_light_type_selects(hass, entry))

    # One "Works with" select per channel for every relay controller. Same
    # three-source union as text.py / switch.py so the selects exist even
    # when the backend was unreachable on HA restart.
    for device in _relay_devices_for_entry(hass, entry).values():
        entities.extend(build_relay_assignment_selects(hass, entry, device))

    # Per-device axis-mapping selects for every levelling sensor (van level).
    runtime_data = hass.data[DOMAIN][entry.entry_id]
    devices = (runtime_data.get("coordinator").data or {}).get("devices", [])
    for device in devices:
        if (
            device.get("service_class") == "levelling"
            or device.get("vendor_device_type") == "van_level"
        ):
            entities.extend(build_levelling_axis_selects(entry, device))

    # "Home Layout" placement select — one per eligible device (heater, light,
    # AC). Three-source union (live device list + registry backstop + the
    # placement ledger) so a placed device keeps its select even when the
    # backend is unreachable on an HA restart (empty coordinator snapshot).
    entities.extend(_build_home_placement_selects(hass, entry, devices))

    # "Temperature Sensor Source" for every Autoterm Air heater. A heater
    # already in the ledger keeps its select when the backend snapshot is
    # empty on restart; the ledger proves it was eligible when chosen.
    from .const import HEATER_TEMP_SOURCE_OPTION

    temp_source_ids: set[str] = set()
    for device in devices:
        for select_entity in build_heater_temp_source_selects(hass, entry, device):
            temp_source_ids.add(select_entity._device_id)
            entities.append(select_entity)
    for device_id in entry.options.get(HEATER_TEMP_SOURCE_OPTION) or {}:
        if device_id and device_id not in temp_source_ids:
            entities.append(SensaHeaterTempSourceSelect(hass, entry, device_id, "Heater"))

    # "Heater Model" for every Autoterm Air heater with the diagnostics
    # firmware; a heater already in the ledger keeps its select, as above.
    from .const import HEATER_MODEL_OPTION

    model_ids: set[str] = set()
    for device in devices:
        for select_entity in build_heater_model_selects(hass, entry, device):
            model_ids.add(select_entity._device_id)
            entities.append(select_entity)
    for device_id in entry.options.get(HEATER_MODEL_OPTION) or {}:
        if device_id and device_id not in model_ids:
            entities.append(SensaHeaterModelSelect(entry, device_id, "Heater"))

    # A fan's "Temperature Sensor" for every Sensa Fan Temperature Control; a controller
    # already in the autopilot ledger keeps its selects, as above.
    from .const import FAN_AUTOPILOT_OPTION, FAN_CHANNELS

    fan_ids: set[str] = set()
    for device in devices:
        for select_entity in build_fan_source_selects(entry, device):
            fan_ids.add(select_entity._device_id)
            entities.append(select_entity)
    for device_id in entry.options.get(FAN_AUTOPILOT_OPTION) or {}:
        if device_id and device_id not in fan_ids:
            entities.extend(
                SensaFanTempSourceSelect(entry, device_id, "Fan Temperature Control", ch)
                for ch in FAN_CHANNELS
            )

    # "Contents" and "Bottle Size" for every Mopeka tank sensor (`mopeka.py`).
    from .mopeka import build_selects as build_mopeka_selects

    for device in devices:
        entities.extend(build_mopeka_selects(entry, device))

    async_add_entities(entities)


def _build_home_placement_selects(
    hass: HomeAssistant, entry: ConfigEntry, device_list: list[dict[str, Any]],
) -> list[SelectEntity]:
    """Build the placement selects (one per surface) for every eligible device,
    recovering ids from the entity registry + the placement ledgers when the
    backend snapshot is empty (mirrors the relay switch three-source union)."""
    from .const import HOME_LAYOUT_KINDS
    from .vendors.service_classes import classify

    kind_by_class = {
        cls: kind for kind in HOME_LAYOUT_KINDS for cls in kind["classes"]
    }

    def _kind_for(device: dict[str, Any]) -> dict[str, Any] | None:
        service_class = device.get("service_class") or classify(device) or ""
        return kind_by_class.get(service_class)

    def _backstop(device_id: str) -> dict[str, Any]:
        """A stand-in device for an id known only from the registry / ledger."""
        return {"device_id": device_id, "display_name": "", "service_class": ""}

    # device_id -> device dict (live snapshot first).
    found: dict[str, dict[str, Any]] = {}
    for device in device_list:
        if _kind_for(device) is None:
            continue
        device_id = str(device.get("device_id") or device.get("id") or "")
        if device_id:
            found.setdefault(device_id, device)

    # Registry backstop: recover ids of already-created placement selects (any
    # surface suffix). LONGEST suffix first so a suffix that extends another
    # can't be claimed by the shorter one.
    ordered = sorted(
        HOME_LAYOUT_SURFACES, key=lambda s: -len(s["uid_suffix"]),
    )
    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id or ent.domain != "select":
                continue
            uid = ent.unique_id or ""
            if not uid.startswith(prefix):
                continue
            for surface in ordered:
                suffix = surface["uid_suffix"]
                if uid.endswith(suffix):
                    did = uid[len(prefix):-len(suffix)]
                    if did:
                        found.setdefault(did, _backstop(did))
                    break
    except Exception:
        _LOGGER.warning("Registry walk for Home-layout device_ids failed", exc_info=True)

    # Ledger backstop: any placed device id (on either surface) must keep its
    # selects.
    for surface in HOME_LAYOUT_SURFACES:
        for device_id in (entry.options.get(surface["option"]) or {}):
            if device_id and device_id not in found:
                found[device_id] = _backstop(device_id)

    entities: list[SelectEntity] = []
    for device in found.values():
        entities.extend(build_home_placement_selects(hass, entry, device))
    return entities


def build_home_placement_selects(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
) -> list[SensaHomePlacementSelect]:
    """Build the "Home Layout" placement selects (one per surface — Habitation
    and Cab) for one device, or an empty list when the device's service class
    has no Home Layout control.

    Returns a list (uniform with the other `build_*` helpers) so the Discovery
    picker's add-dispatch can create them for a newly-registered device without
    reloading the entry.
    """
    from .const import home_layout_kind_for
    from .vendors.service_classes import classify

    service_class = device.get("service_class") or classify(device) or ""
    kind = home_layout_kind_for(service_class)
    if kind is None:
        return []
    device_id = str(device.get("device_id") or device.get("id") or "unknown")
    device_name = (
        device.get("display_name") or device.get("name") or kind["fallback_name"]
    )
    return [
        SensaHomePlacementSelect(hass, entry, device_id, device_name, kind, surface)
        for surface in HOME_LAYOUT_SURFACES
    ]


class SensaHomePlacementSelect(SelectEntity):
    """Per-device, per-surface "Home Layout" placement select.

    One implementation for every kind in `const.HOME_LAYOUT_KINDS` (heaters,
    lights, air conditioners) × every surface in `const.HOME_LAYOUT_SURFACES`
    (Habitation, Cab). The customer picks which home-tab column shows the
    device's compact card on that surface: **Off** (hidden, the opt-in default)
    / **Left** / **Middle** / **Right** — independently per surface, so a device
    can appear on the Habitation home, the Cab home, both, or neither.

    Options-as-truth — the surface's ledger (`entry.options[surface.option]`)
    maps a device id to its chosen column (`left`/`middle`/`right`); an absent id
    reads "Off". Selecting a column writes the ledger and rebuilds the
    dashboards, so the device's compact card appears in / moves between /
    disappears from that surface's home layout immediately. Surfaced on the
    Settings → Devices row via `sensor._config_entities_for_device`.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = HOME_LAYOUT_PLACEMENT_OPTIONS

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        kind: dict[str, Any],
        surface: dict[str, str],
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._option = surface["option"]
        self._attr_icon = kind["icon"]
        self._attr_name = f"{device_name} {surface['label']} Home Layout"
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}{surface['uid_suffix']}"
        )
        self.entity_id = home_layout_placement_select_entity_id(
            device_id, surface["key"],
        )

    @property
    def current_option(self) -> str:
        column = (
            self._entry.options.get(self._option) or {}
        ).get(self._device_id)
        return HOME_LAYOUT_COLUMN_TO_LABEL.get(column, HOME_LAYOUT_OFF_LABEL)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            _LOGGER.warning(
                "Home Layout: ignoring unknown option %r (allowed: %s)",
                option, self._attr_options,
            )
            return
        column = HOME_LAYOUT_LABEL_TO_COLUMN.get(option)
        ledger = dict(self._entry.options.get(self._option) or {})
        if column is None:
            ledger.pop(self._device_id, None)
        else:
            ledger[self._device_id] = column
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, self._option: ledger},
        )
        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


def build_levelling_axis_selects(
    entry: ConfigEntry, device: dict[str, Any]
) -> list[SelectEntity]:
    """Three axis-mapping selects (Front / Left / Up) for one van-level
    device.

    Exposed so the Discovery picker's add-dispatch can create these for a
    levelling sensor registered after initial setup without an integration
    reload.
    """
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    display = device.get("display_name") or device.get("name") or "Van Level"
    out: list[SelectEntity] = []
    for logical in VAN_LEVEL_LOGICAL_AXES:
        out.append(SensaLevellingAxisSelect(entry, device_id, display, logical))
    return out


def _relay_devices_for_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, dict[str, Any]]:
    """Return {device_id: device_row} for every relay controller this entry
    owns, taking the union of three sources (mirrors text.py / switch.py):

      1. coordinator.data — current backend snapshot (empty when the backend
         is unreachable on HA restart).
      2. HA entity registry — any `_relay_` unique_id from a prior session.
      3. entry.options durable ledger (`relay_initialized` + `relay_enabled`).

    Backend role-field name varies by registration path (`vendor_device_type`
    / `device_type` / `type` / `role`).
    """
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    data = getattr(coordinator, "data", None) or {}
    device_list = data.get("devices") or []

    relay_devices: dict[str, dict[str, Any]] = {}
    for device in device_list:
        if not is_relay_device(device):
            continue
        device_id = str(device.get("device_id") or device.get("id") or "")
        if device_id:
            relay_devices[device_id] = device

    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        entry_prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id:
                continue
            uid = ent.unique_id or ""
            if not uid.startswith(entry_prefix):
                continue
            tail = uid[len(entry_prefix):]
            if "_relay_" not in tail and not tail.endswith("_relay_device_name"):
                continue
            if tail.endswith("_relay_device_name"):
                device_id = tail[:-len("_relay_device_name")]
            else:
                device_id = tail.rsplit("_relay_", 1)[0]
            if not device_id or device_id in relay_devices:
                continue
            relay_devices[device_id] = {
                "device_id": device_id,
                "display_name": "Relay",
                "vendor_device_type": "relay",
            }
    except Exception:
        _LOGGER.warning(
            "Registry walk for relay device_ids failed", exc_info=True,
        )

    persisted_ids: set[str] = set(entry.options.get("relay_initialized") or [])
    persisted_ids.update((entry.options.get("relay_enabled") or {}).keys())
    for device_id in persisted_ids:
        if not device_id or device_id in relay_devices:
            continue
        relay_devices[device_id] = {
            "device_id": device_id,
            "display_name": "Relay",
            "vendor_device_type": "relay",
        }

    return relay_devices


def _integrated_device_choices(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[tuple[str, str]]:
    """Return [(device_id, display_name)] for every registered NON-relay
    device — the "Integrated Device" options a relay channel may link to.

    These are devices Sensa already drives (router, Starlink, Cerbo, BMS, …);
    assigning a channel to one just adds power on/off to that device's card.
    Relay controllers themselves are excluded (a relay can't "work with" a
    relay).
    """
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    data = getattr(coordinator, "data", None) or {}
    devices = data.get("devices") or []

    choices: list[tuple[str, str]] = []
    seen: set[str] = set()
    for d in devices:
        if is_relay_device(d):
            continue
        d_id = str(d.get("device_id") or d.get("id") or "")
        if not d_id or d_id in seen:
            continue
        seen.add(d_id)
        name = d.get("display_name") or d.get("name") or d_id
        choices.append((d_id, name))
    return choices


class SensaCabNavSourceSelect(RestoreEntity, SelectEntity):
    """Persistent dropdown driving the Cab Overview navigation card.

    `RestoreEntity` brings the customer's last selection back across HA
    restarts so the Cab dashboard always reopens on the same nav tool
    they were last using. EntityCategory.CONFIG keeps it out of the
    Mode Editor's eligibility set (the eligibility pipeline drops
    config-categorised entities).
    """

    _attr_has_entity_name = False
    _attr_name = "Cab Nav Source"
    _attr_icon = "mdi:map-search"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = CAB_NAV_OPTIONS

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cab_nav_source"
        self.entity_id = "select.sensa_nomad_cab_nav_source"
        self._attr_current_option = CAB_NAV_DEFAULT

    @property
    def device_info(self) -> dict:
        # Attach to the Sensa Nomad hub device so the entity surfaces
        # under Settings → Devices & Services → Sensa Nomad rather than
        # floating as an orphan.
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        """Restore the customer's last selection across HA restarts."""
        await super().async_added_to_hass()
        # Narrow the options to what this box can actually render (HA Map is
        # dropped on an appliance). Done here, not at construction, because it
        # needs `hass.data` (the resolved deployment mode).
        self._attr_options = _cab_nav_options(self.hass)
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._attr_current_option = last.state
        # A previously-restored "HA Map" that this box no longer offers (an
        # appliance that used to list it) must not strand the select on an
        # unrenderable source.
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = CAB_NAV_DEFAULT

    async def async_select_option(self, option: str) -> None:
        """Persist the customer's choice. Conditional cards on the Cab
        Overview re-render automatically when this entity's state
        changes — no extra signal/dispatcher needed."""
        if option not in CAB_NAV_OPTIONS:
            _LOGGER.warning(
                "Cab nav source: ignoring unknown option %r (valid: %s)",
                option, CAB_NAV_OPTIONS,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()


class SensaNavUnitsSelect(RestoreEntity, SelectEntity):
    """Navigation units (Miles / Kilometres), a Nomad-owned setting.

    Separate from HA's system unit ON PURPOSE: HA has a single length unit and
    can't express "metric for everything except roads", which is the UK norm. The
    Cab nav surfaces (the Radar map embed + the Nearby distances) read this so
    they agree with each other and with the customer's road-distance preference,
    regardless of whether HA runs metric or imperial. `RestoreEntity` keeps the
    choice; `EntityCategory.CONFIG` keeps it out of the Mode Editor set.
    """

    _attr_has_entity_name = False
    _attr_name = "Navigation Units"
    _attr_icon = "mdi:ruler"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = NAV_UNITS_OPTIONS

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_nav_units"
        self.entity_id = "select.sensa_nomad_nav_units"
        self._attr_current_option = NAV_UNITS_DEFAULT

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in NAV_UNITS_OPTIONS:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        if option not in NAV_UNITS_OPTIONS:
            _LOGGER.warning(
                "Nav units: ignoring unknown option %r (valid: %s)",
                option, NAV_UNITS_OPTIONS,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()


class SensaNavFuelTypeSelect(RestoreEntity, SelectEntity):
    """What the van runs on, so a fuel pin can show the right price.

    Sits beside the navigation-units setting on Settings -> Radar, and is pushed
    into the Radar map embed the same way. See `NAV_FUEL_TYPE_OPTIONS` for why
    this is a Nomad setting rather than the Radar account's vehicle profile.
    """

    _attr_has_entity_name = False
    _attr_name = "Fuel Type"
    _attr_icon = "mdi:fuel"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = NAV_FUEL_TYPE_OPTIONS

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_nav_fuel_type"
        self.entity_id = "select.sensa_nomad_nav_fuel_type"
        self._attr_current_option = NAV_FUEL_TYPE_DEFAULT

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def extra_state_attributes(self) -> dict:
        """Publish the resolved Radar price key so the card reads the answer
        rather than carrying its own copy of the mapping. One predicate written
        twice is this repo's recurring bug."""
        return {"price_key": nav_fuel_price_key(self._attr_current_option)}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in NAV_FUEL_TYPE_OPTIONS:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        if option not in NAV_FUEL_TYPE_OPTIONS:
            _LOGGER.warning(
                "Nav fuel type: ignoring unknown option %r (valid: %s)",
                option, NAV_FUEL_TYPE_OPTIONS,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()


def _has_coordinates(hass: HomeAssistant, entity_id: str) -> bool:
    """True when a tracker is actually reporting a usable position.

    The discriminator between a GPS device and a presence tracker. A
    router- or Bluetooth-based `device_tracker` only knows home/away and
    has no coordinates to navigate by, so offering it as a location
    source would be a dead choice; anything carrying a real lat/lon can
    drive the map, the Radar sensors and Tracking.
    """
    st = hass.states.get(entity_id)
    if st is None:
        return False
    try:
        lat = float(st.attributes.get("latitude"))
        lon = float(st.attributes.get("longitude"))
    except (TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _selectable_gps_trackers(hass: HomeAssistant) -> list[tuple[str, str]]:
    """`[(entity_id, label), …]` for every GPS tracker the customer can pick.

    Two kinds qualify, and the test differs because the evidence differs:

      * **HA Companion phones** (`platform == "mobile_app"`) are admitted on
        the platform alone. The mobile_app integration creates exactly one
        `device_tracker.<phone>` per registered phone, and a phone that has
        not reported since a restart still deserves a place in the list.
      * **Any other `device_tracker` carrying real coordinates.** Traccar,
        OwnTracks, GPSLogger, iCloud, an MQTT telematics box: all of them
        were excluded by the old `mobile_app`-only filter for no good
        reason, leaving a van with a perfectly good GPS unable to select
        it. Presence-only trackers (router, Bluetooth) are still excluded,
        because they have no position to navigate by.

    Our own `device_tracker.sensa_nomad_vehicle` is skipped: it is already
    offered as the fixed "Vehicle GPS" entry, and listing it twice would
    make two dropdown options that resolve to the same device.

    Label prefers the live `friendly_name` (what the customer sees in HA),
    else the registry name. Colliding labels get the entity tail appended so
    the dropdown to entity mapping stays 1:1; sorted for stable ordering.
    """
    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return []
    raw: list[tuple[str, str]] = []
    for ent in ent_reg.entities.values():
        eid = ent.entity_id
        if eid.split(".", 1)[0] != "device_tracker":
            continue
        if eid == CAB_VEHICLE_TRACKER_ENTITY_ID:
            continue
        if ent.platform != "mobile_app" and not _has_coordinates(hass, eid):
            continue
        st = hass.states.get(eid)
        label = (
            (st.attributes.get("friendly_name") if st else None)
            or ent.name
            or ent.original_name
            or eid.split(".", 1)[-1]
        )
        raw.append((eid, str(label)))
    counts: dict[str, int] = {}
    for _eid, label in raw:
        counts[label] = counts.get(label, 0) + 1
    result: list[tuple[str, str]] = []
    for eid, label in raw:
        if counts[label] > 1:
            label = f"{label} ({eid.split('.', 1)[-1]})"
        result.append((eid, label))
    result.sort(key=lambda t: t[1].lower())
    return result


class SensaCabLocationSourceSelect(RestoreEntity, SelectEntity):
    """The GPS device for navigation — THE SYSTEM DEFAULT (2026-07-20).

    One choice for the box, set on Settings → Radar and honoured by the Radar
    nearest-amenity sensors, Tracking and Schengen through
    `device_tracker.resolve_nav_source`. The Cab dropdowns default to
    `(System Default)`, which follows this, and a pick there is a per-screen
    override.

    A van may have no vehicle GPS at all: plenty of customers drive on a phone,
    drivers swap, and a partner's phone may not even be in the van. So this is a
    first-class customer setting, not an internal detail.

      • `Auto`        — vehicle GPS if it has a fix, else the most-recently
        updated tracker that is PLAUSIBLY in the van (see the gate in
        `device_tracker`, which stops a partner's phone at home winning purely
        for having reported a second ago).
      • `Vehicle GPS` — pin to the shared vehicle tracker.
      • one entry per selectable GPS tracker — an HA Companion-app phone, or
        any other `device_tracker` reporting real coordinates (Traccar,
        OwnTracks, an MQTT telematics box). See `_selectable_gps_trackers`.

    `RestoreEntity` persists the choice. `_attr_should_poll` refreshes the option
    list as phones come and go. `EntityCategory.CONFIG` keeps it out of the Mode
    Editor eligibility set. `source_map` (label -> tracker entity_id) and
    `resolved_entity` (the answer itself) are exported for the frontend cards.
    """

    _attr_has_entity_name = False
    _attr_name = "Cab Location Source"
    _attr_icon = "mdi:crosshairs-gps"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cab_location_source"
        self.entity_id = CAB_LOCATION_SELECT_ENTITY_ID
        self._choice = CAB_LOCATION_AUTO

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def _tracker_map(self) -> dict[str, str]:
        """label → entity_id for every selectable GPS tracker."""
        return {label: eid for eid, label in _selectable_gps_trackers(self.hass)}

    @property
    def options(self) -> list[str]:
        return [
            CAB_LOCATION_AUTO,
            CAB_LOCATION_VEHICLE,
            *sorted(self._tracker_map().keys(), key=str.lower),
        ]

    @property
    def current_option(self) -> str | None:
        # The persisted choice, but only while it's still a valid option (a pinned
        # phone that has since left falls back to Auto rather than a dangling
        # selection).
        return self._choice if self._choice in self.options else CAB_LOCATION_AUTO

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # label → entity_id, consumed by `resolve_nav_position` and the map cards
        # to resolve a chosen label. Auto carries no fixed entity (resolved to
        # vehicle-else-phone at read time).
        source_map: dict[str, str] = {
            CAB_LOCATION_AUTO: "",
            CAB_LOCATION_VEHICLE: CAB_VEHICLE_TRACKER_ENTITY_ID,
            **self._tracker_map(),
        }
        # THE resolved answer, published so the Cab tab-strip dropdown's
        # `(System Default)` can follow it instead of re-deriving the
        # precedence. The frontend used to carry its own copy with an extra
        # "viewing phone" step, so the map and the Radar sensors could disagree
        # with nobody having overridden anything -- the exact "one predicate
        # written twice" trap CLAUDE.md warns about. Read it, never rebuild it.
        resolved_entity = ""
        resolved_label = ""
        try:
            from .device_tracker import resolve_nav_source

            # `record=False`: a property getter must not mutate state, and this
            # is read on the select's own poll cadence, so recording here would
            # advance the plausibility gate's reference position on RENDERING
            # rather than when a consumer actually used the answer.
            resolved = resolve_nav_source(self.hass, record=False)
        except Exception:  # noqa: BLE001 - an attribute must never break the entity
            resolved = None
        if resolved:
            resolved_entity = resolved[0]
            resolved_label = next(
                (
                    label
                    for label, eid in source_map.items()
                    if eid and eid == resolved_entity
                ),
                "",
            )
        return {
            "source_map": source_map,
            "resolved_entity": resolved_entity,
            "resolved_label": resolved_label,
            # The customer's choice itself, persisted with the state so a restore
            # never depends on the state. The state reads Auto while a pinned
            # phone's tracker is absent (as it is early in every boot), and
            # restoring from that state lost the choice across upgrades.
            "choice": self._choice,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        choice = last.attributes.get("choice") or last.state
        if choice and choice not in ("unknown", "unavailable"):
            self._choice = choice

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            _LOGGER.warning(
                "Cab location source: ignoring unknown option %r (valid: %s)",
                option, self.options,
            )
            return
        self._choice = option
        self.async_write_ha_state()


class SensaPowerFlowAnimationSelect(RestoreEntity, SelectEntity):
    """Persistent power-flow wire-animation style, shared by every power-flow
    card on the install.

    The Electrical hero's in-card picker writes it; the Home tab's compact
    power-flow card (which has no picker) reads it. Holding it server-side with
    `RestoreEntity` means the customer's choice survives HA restarts AND device
    reboots AND is identical on every client (the wall tablet and a phone on the
    web), which the old per-browser `localStorage` couldn't guarantee — a kiosk
    browser wipes localStorage on reboot, so the style kept resetting to the
    default. Defaults to "dots" on a fresh install. `EntityCategory.CONFIG`
    keeps it out of the Mode Editor eligibility set.

    No panel regen is needed on a change: power-flow cards read this entity's
    state directly from `hass` and re-render on the state push.
    """

    _attr_has_entity_name = False
    _attr_name = "Power Flow Animation"
    _attr_icon = "mdi:transit-connection-variant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = POWER_FLOW_ANIM_OPTIONS

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_power_flow_animation"
        self.entity_id = "select.sensa_nomad_power_flow_animation"
        self._attr_current_option = POWER_FLOW_ANIM_DEFAULT

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        """Restore the customer's last animation choice across HA restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in POWER_FLOW_ANIM_OPTIONS:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        """Persist the choice. Power-flow cards re-render automatically when
        this entity's state changes — no extra signal needed."""
        if option not in POWER_FLOW_ANIM_OPTIONS:
            _LOGGER.warning(
                "Power flow animation: ignoring unknown option %r (valid: %s)",
                option, POWER_FLOW_ANIM_OPTIONS,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()


class SensaCryptoRoomSensorSelect(RestoreEntity, SelectEntity):
    """Which temperature probe the miner autopilot treats as the habitation.

    Sensa has no interior / exterior distinction between temperature sensors
    (a Ruuvi by the bed and a Govee outside land in one flat list), so the
    customer says which one guards the hab here. Options are entity_ids, the
    honest value: the Crypto card shows each one's friendly name. `Not
    monitored` is a deliberate empty pick and skips the temperature guard;
    a chosen probe that has since gone away is a fault and pauses the engine
    (`crypto_autopilot.py`). Polls so a probe added later appears without a
    restart.
    """

    _attr_has_entity_name = False
    _attr_name = "Mining Room Temperature Sensor"
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_crypto_room_sensor"
        self.entity_id = CRYPTO_ROOM_SENSOR_SELECT_ENTITY_ID
        self._choice = CRYPTO_ROOM_NOT_MONITORED

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @staticmethod
    def _probes() -> list[str]:
        """Every temperature sensor a dropdown offers: the van's environment
        sensors, then any heater's wired external probe. Every temperature
        dropdown (SensaClimate, Temperature Sensor Source, fan autopilot, this
        one) lists these, so they cannot offer different sensors."""
        try:
            from .dashboard import _all_temperature_entities, wired_heater_probes

            probes = list(_all_temperature_entities())
            return probes + [e for e in wired_heater_probes() if e not in probes]
        except Exception:  # noqa: BLE001 - a resolver hiccup must not break the entity
            return []

    @property
    def options(self) -> list[str]:
        probes = self._probes()
        # Keep the customer's pick listed even while its probe is quiet, so the
        # select never silently falls back to "Not monitored": that is the
        # difference between a deliberate empty pick and a fault.
        if self._choice != CRYPTO_ROOM_NOT_MONITORED and self._choice not in probes:
            probes.append(self._choice)
        return [CRYPTO_ROOM_NOT_MONITORED, *probes]

    @property
    def current_option(self) -> str | None:
        return self._choice

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state and last.state not in ("unknown", "unavailable"):
            self._choice = last.state

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            _LOGGER.warning(
                "Mining room sensor: ignoring unknown option %r", option,
            )
            return
        self._choice = option
        self.async_write_ha_state()


class SensaClimateControlSelect(SelectEntity):
    """One Sensa Climate Control nomination on Settings -> Vehicle: the
    primary heater, the primary air conditioner, or the temperature sensor
    it judges by (`key` = heater / ac / sensor).

    Options are device ids (heater, ac) or entity ids (sensor) after "none";
    the `labels` attribute carries the names the card shows, so no id reaches
    the customer. Options-as-truth on the entry (`CLIMATE_CONTROL_OPTION`).
    Polls so a device or sensor added later appears without a restart.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
        from .const import CLIMATE_CONTROL_SELECTS

        self.hass = hass
        self._entry = entry
        self._key = key
        self.entity_id, self._attr_name = CLIMATE_CONTROL_SELECTS[key]
        self._attr_unique_id = f"{entry.entry_id}_climate_control_{key}"
        self._ac_choices: list[str] = []
        self._balticas: list[str] = []
        self._attr_icon = {
            "heater": "mdi:radiator", "secondary": "mdi:radiator", "third": "mdi:radiator",
            "ac": "mdi:air-conditioner", "sensor": "mdi:thermometer",
            "outside": "mdi:sun-thermometer",
            "baltica_warm": "mdi:water-boiler", "baltica_off": "mdi:water-boiler",
        }.get(key, "mdi:gas-station")  # a heater's diesel tank

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def _choices(self) -> dict[str, str]:
        """option -> label, "none" first."""
        from .climate_control import ac_devices, heater_devices
        from .const import CLIMATE_CONTROL_NONE

        if self._key in ("baltica_warm", "baltica_off"):
            # Fixed choices, no "None": a Baltica always does one of these.
            from .climate_control import BALTICA_OFF_LABELS, BALTICA_WARM_LABELS

            return dict(BALTICA_WARM_LABELS if self._key == "baltica_warm" else BALTICA_OFF_LABELS)
        choices = {CLIMATE_CONTROL_NONE: "None"}
        self._ac_choices: list[str] = []
        self._balticas: list[str] = []
        try:
            if self._key.endswith("_tank"):
                from .dashboard import diesel_tanks

                choices.update(diesel_tanks())
            elif self._key in ("sensor", "outside"):
                for eid in SensaCryptoRoomSensorSelect._probes():
                    st = self.hass.states.get(eid)
                    choices[eid] = str((st.attributes.get("friendly_name") if st else None) or eid)
            elif self._key in ("heater", "secondary", "third"):
                from .climate_control import baltica_needs_update, is_baltica

                for d in heater_devices(self.hass, self._entry):
                    name = str(d.get("display_name") or d.get("name") or "Heater")
                    if is_baltica(d):
                        self._balticas.append(str(d["device_id"]))
                        if baltica_needs_update(self.hass, d):
                            # Listed, never driven, until its controller can
                            # take SensaClimate's room heat (1.1.25).
                            name += " (needs a controller update)"
                    choices[str(d["device_id"])] = name
                # An air conditioner that can heat may stand in a heater slot.
                from .climate_control import _ac_heats, ac_entity
                from .dashboard import _ac_display_name

                for d in ac_devices(self.hass, self._entry):
                    if _ac_heats(self.hass, ac_entity(self.hass, d)):
                        choices[str(d["device_id"])] = _ac_display_name(d)
                        self._ac_choices.append(str(d["device_id"]))
            else:
                from .dashboard import _ac_display_name

                for d in ac_devices(self.hass, self._entry):
                    choices[str(d["device_id"])] = _ac_display_name(d)
        except Exception:  # noqa: BLE001 - a resolver hiccup must not break the entity
            _LOGGER.debug("Climate Control %s options failed", self._key, exc_info=True)
        # Keep the nominee listed while it is quiet, so the row never silently
        # shows a different choice from the one in force.
        current = self.current_option
        if current not in choices:
            choices[current] = "No longer reporting"
        return choices

    @property
    def current_option(self) -> str:
        from .climate_control import setting

        return str(setting(self._entry, self._key))

    @property
    def options(self) -> list[str]:
        return list(self._choices())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        labels = self._choices()
        # Which heater-slot options are air conditioners (the card offers a
        # diesel tank only for a diesel heater) and Balticas (the card shows
        # the two Baltica choices under one).
        return {
            "labels": labels,
            "air_conditioners": getattr(self, "_ac_choices", []),
            "balticas": getattr(self, "_balticas", []),
        }

    async def async_select_option(self, option: str) -> None:
        from .climate_control import async_set_nominee

        if option not in self.options:
            _LOGGER.warning("Climate Control %s: ignoring unknown option %r", self._key, option)
            return
        await async_set_nominee(self.hass, self._entry, self._key, option)
        self.async_write_ha_state()
        # The Climate-tab card appears once a sensor and a unit are chosen.
        from . import _async_rebuild_sensa_dashboards

        self.hass.async_create_task(_async_rebuild_sensa_dashboards(self.hass, self._entry))


def build_heater_temp_source_selects(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaHeaterTempSourceSelect]:
    """The "Temperature Sensor Source" select for one Autoterm Air heater, or
    an empty list for any other device. A list, uniform with the other
    `build_*` helpers, so the rebuild loop can add it post-setup."""
    from .heater_temp_source import eligible

    if not eligible(hass, device):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = device.get("display_name") or device.get("name") or "Heater"
    return [SensaHeaterTempSourceSelect(hass, entry, device_id, name)]


class SensaHeaterTempSourceSelect(SelectEntity):
    """What an Autoterm Air heater regulates on in Temperature mode.

    Options: the Autoterm Controller (the wall panel's sensor, the default and
    the old behaviour), the Autoterm Heater (its own intake sensor), then every
    van temperature sensor. A sensor option's value is its entity_id; the
    `labels` attribute carries the names the device row shows, so no entity id
    reaches the customer. Options-as-truth on the entry
    (`HEATER_TEMP_SOURCE_OPTION`); `heater_temp_source.py` carries the choice
    to the controller firmware and keeps it there.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer-lines"
    _attr_should_poll = True  # a sensor added later appears without a restart

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device_id: str, device_name: str,
    ) -> None:
        from .const import (
            HEATER_TEMP_SOURCE_UID_SUFFIX,
            heater_temp_source_select_entity_id,
        )

        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._attr_name = f"{device_name} Temperature Sensor Source"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{HEATER_TEMP_SOURCE_UID_SUFFIX}"
        self.entity_id = heater_temp_source_select_entity_id(device_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def current_option(self) -> str:
        from .const import HEATER_TEMP_SOURCE_CONTROLLER, HEATER_TEMP_SOURCE_OPTION

        return (self._entry.options.get(HEATER_TEMP_SOURCE_OPTION) or {}).get(
            self._device_id, HEATER_TEMP_SOURCE_CONTROLLER,
        )

    @property
    def options(self) -> list[str]:
        from .const import HEATER_TEMP_SOURCE_LABELS

        probes = SensaCryptoRoomSensorSelect._probes()
        choice = self.current_option
        # Keep a chosen sensor listed while it is quiet, so the row never
        # silently shows a different source from the one in force.
        if choice not in HEATER_TEMP_SOURCE_LABELS and choice not in probes:
            probes.append(choice)
        return [*HEATER_TEMP_SOURCE_LABELS, *probes]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import HEATER_TEMP_SOURCE_LABELS

        labels = dict(HEATER_TEMP_SOURCE_LABELS)
        for option in self.options:
            if option in labels:
                continue
            st = self.hass.states.get(option)
            name = st.attributes.get("friendly_name") if st else None
            labels[option] = str(name) if name else "Sensor no longer reporting"
        return {"labels": labels}

    async def async_select_option(self, option: str) -> None:
        from .const import HEATER_TEMP_SOURCE_OPTION

        if option not in self.options:
            _LOGGER.warning("Heater temperature source: ignoring unknown option %r", option)
            return
        ledger = dict(self._entry.options.get(HEATER_TEMP_SOURCE_OPTION) or {})
        ledger[self._device_id] = option
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, HEATER_TEMP_SOURCE_OPTION: ledger},
        )
        self.async_write_ha_state()
        # Tell the controller now rather than on the next minute's tick, and
        # rebuild so the card's "Now" follows the sensor in force.
        from . import _async_rebuild_sensa_dashboards
        from .heater_temp_source import async_push_heater_temp_sources

        self.hass.async_create_task(async_push_heater_temp_sources(self.hass, self._entry))
        self.hass.async_create_task(_async_rebuild_sensa_dashboards(self.hass, self._entry))


def build_fan_source_selects(
    entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaFanTempSourceSelect]:
    """One "Temperature Sensor" select per fan on a Sensa Fan Temperature Control, or an
    empty list for any other device."""
    from .const import FAN_CHANNELS
    from .fan_autopilot import is_fan_controller

    if not is_fan_controller(device):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = device.get("display_name") or device.get("name") or "Fan Temperature Control"
    return [SensaFanTempSourceSelect(entry, device_id, name, ch) for ch in FAN_CHANNELS]


class SensaFanTempSourceSelect(SelectEntity):
    """Which van temperature sensor one fan's autopilot follows.

    "Manual" (the default) leaves the fan to its own speed slider; any other
    option is a sensor's entity_id, labelled by name through the `labels`
    attribute like the heater's select. Options-as-truth on the entry
    (`FAN_AUTOPILOT_OPTION`); `fan_autopilot.py` acts on it.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer-lines"
    _attr_should_poll = True  # a sensor added later appears without a restart

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str, channel: int,
    ) -> None:
        from .const import fan_autopilot_key, fan_source_select_entity_id

        self._entry = entry
        self._device_id = device_id
        self._key = fan_autopilot_key(channel, "source")
        self._attr_name = f"{device_name} Fan {channel} Temperature Sensor"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{self._key}"
        self.entity_id = fan_source_select_entity_id(device_id, channel)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def current_option(self) -> str:
        from .fan_autopilot import setting

        return setting(self._entry, self._device_id, self._key)

    @property
    def options(self) -> list[str]:
        from .const import FAN_SOURCE_LABELS

        probes = SensaCryptoRoomSensorSelect._probes()
        choice = self.current_option
        # Keep a chosen sensor listed while it is quiet, so the row never
        # silently shows a different sensor from the one in force.
        if choice not in FAN_SOURCE_LABELS and choice not in probes:
            probes.append(choice)
        return [*FAN_SOURCE_LABELS, *probes]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import FAN_SOURCE_LABELS

        labels = dict(FAN_SOURCE_LABELS)
        for option in self.options:
            if option in labels:
                continue
            st = self.hass.states.get(option)
            name = st.attributes.get("friendly_name") if st else None
            labels[option] = str(name) if name else "Sensor no longer reporting"
        return {"labels": labels}

    async def async_select_option(self, option: str) -> None:
        from .const import FAN_AUTOPILOT_OPTION
        from .fan_autopilot import async_drive_fans

        if option not in self.options:
            _LOGGER.warning("Fan temperature sensor: ignoring unknown option %r", option)
            return
        ledger = dict(self._entry.options.get(FAN_AUTOPILOT_OPTION) or {})
        ledger[self._device_id] = {**(ledger.get(self._device_id) or {}), self._key: option}
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, FAN_AUTOPILOT_OPTION: ledger},
        )
        self.async_write_ha_state()
        self.hass.async_create_task(async_drive_fans(self.hass, self._entry))


def build_heater_model_selects(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaHeaterModelSelect]:
    """The "Heater Model" select for an Autoterm Air heater whose controller
    publishes the diagnostics frame, else an empty list. The Baltica has one
    model, and so does the Air 9D controller (its own firmware), so they get none."""
    from .autoterm_diag import is_air_9d
    from .heater_temp_source import firmware_entities
    from .vendors.service_classes import classify

    if (device.get("service_class") or classify(device)) != "air_heater":
        return []
    if is_air_9d(device):
        return []
    if "diagnostics_frame" not in firmware_entities(hass, device):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = device.get("display_name") or device.get("name") or "Heater"
    return [SensaHeaterModelSelect(entry, device_id, name)]


class SensaHeaterModelSelect(SelectEntity):
    """Which Air model the Air D controller is fitted to: the extended
    diagnostics field map differs between the 2D and the 4D. Unset reads
    "Not set", and the Diagnostics popup then says to choose it rather than
    decode with a guessed map. Options-as-truth on the entry."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:fire-circle"

    def __init__(self, entry: ConfigEntry, device_id: str, device_name: str) -> None:
        from .const import HEATER_MODEL_UID_SUFFIX, heater_model_select_entity_id

        self._entry = entry
        self._device_id = device_id
        self._attr_name = f"{device_name} Heater Model"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{HEATER_MODEL_UID_SUFFIX}"
        self.entity_id = heater_model_select_entity_id(device_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def options(self) -> list[str]:
        from .autoterm_diag import AIR_MODELS
        from .const import HEATER_MODEL_UNSET

        return [HEATER_MODEL_UNSET, *AIR_MODELS]

    @property
    def current_option(self) -> str:
        from .const import HEATER_MODEL_OPTION, HEATER_MODEL_UNSET

        return (self._entry.options.get(HEATER_MODEL_OPTION) or {}).get(
            self._device_id, HEATER_MODEL_UNSET,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .autoterm_diag import MODELS
        from .const import HEATER_MODEL_UNSET

        labels = {HEATER_MODEL_UNSET: "Not set"}
        labels.update({k: v["label"] for k, v in MODELS.items()})
        return {"labels": {o: labels.get(o, o) for o in self.options}}

    async def async_select_option(self, option: str) -> None:
        from .const import HEATER_MODEL_OPTION

        if option not in self.options:
            _LOGGER.warning("Heater model: ignoring unknown option %r", option)
            return
        ledger = dict(self._entry.options.get(HEATER_MODEL_OPTION) or {})
        ledger[self._device_id] = option
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, HEATER_MODEL_OPTION: ledger},
        )
        self.async_write_ha_state()


class SensaRouterSetupSelect(SelectEntity):
    """Who runs a Teltonika router's network: Self managed, or Nomad managed
    as one network or the secure layout. Decides which layout the Networks
    card and Configure Router read (`network_profile.layout_for_device`).
    Options-as-truth on the entry; unset reads "Not set" until chosen."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:router-network"

    def __init__(self, entry: ConfigEntry, device_id: str, device_name: str) -> None:
        from .const import ROUTER_SETUP_UID_SUFFIX, router_setup_select_entity_id

        self._entry = entry
        self._device_id = device_id
        self._attr_name = f"{device_name} Network Setup"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{ROUTER_SETUP_UID_SUFFIX}"
        self.entity_id = router_setup_select_entity_id(device_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def options(self) -> list[str]:
        from .const import ROUTER_SETUP_LABELS

        return list(ROUTER_SETUP_LABELS)

    @property
    def current_option(self) -> str:
        from .network_profile import layout_for_device

        return layout_for_device(self._entry, self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import ROUTER_SETUP_LABELS

        return {"labels": dict(ROUTER_SETUP_LABELS)}

    async def async_select_option(self, option: str) -> None:
        from .const import ROUTER_SETUP_OPTION

        if option not in self.options:
            _LOGGER.warning("Network setup: ignoring unknown option %r", option)
            return
        ledger = dict(self._entry.options.get(ROUTER_SETUP_OPTION) or {})
        ledger[self._device_id] = option
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, ROUTER_SETUP_OPTION: ledger},
        )
        self.async_write_ha_state()


class SensaAppUsageIntervalSelect(RestoreEntity, SelectEntity):
    """How often the Support card's "Usage by app" panel refreshes (seconds).

    A pill on the Support card writes it; BOTH Support-card pollers (Resources +
    Usage by app) in __init__.py re-read this entity's state each cycle and re-arm
    `async_call_later`, so a change takes effect on the next tick. `RestoreEntity`
    keeps the customer's choice across HA restarts; `EntityCategory.CONFIG` keeps
    it out of the Mode Editor eligibility set. Default 10s (keeps host CPU visibly
    moving); raise it to cut the per-app Supervisor call volume.
    """

    _attr_has_entity_name = True
    _attr_name = "App Usage Refresh"
    _attr_icon = "mdi:timer-refresh-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_unit_of_measurement = "s"
    _attr_options: ClassVar[list[str]] = list(APP_USAGE_INTERVAL_OPTIONS)

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_app_usage_interval"
        self.entity_id = APP_USAGE_INTERVAL_ENTITY_ID
        self._attr_current_option = str(APP_USAGE_REFRESH_SECONDS)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            _LOGGER.warning(
                "App usage refresh: ignoring unknown option %r (valid: %s)",
                option, self._attr_options,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()


# How long a written channel is held on screen before we stop claiming it.
#
# MUST stay in step with CHANNEL_CONFIRM_MS in sensa-admin-card.ts. The card warns
# "the system has not confirmed the change" at that mark, and a dropdown still
# showing the requested value next to that warning is two answers to one
# question. Comfortably past the 8s + 20s settle polls, so a healthy box always
# converges first; past it we show the box's truth rather than our own request.
_CHANNEL_PENDING_TTL = timedelta(seconds=60)

# What the App's refusal codes mean to a person. `not_found` is the one that
# matters most: it means this box is running software older than the channel
# control itself, which is not something the operator can tell by looking.
# NB `ota_channel_write_failed` is deliberately absent: the App answers that with
# a 502, and the client raises on 5xx rather than returning a body, so it never
# reaches this map. It is handled on the exception path instead.
_CHANNEL_REFUSAL = {
    "not_found": (
        "This system's software is too old to change the release channel. "
        "Update it first, then change the channel."
    ),
    "channel_not_supported": "This system does not manage its own release channel.",
    "bad_channel": "That is not a release channel this system recognises.",
}
_CHANNEL_REFUSAL_DEFAULT = "The system refused the release-channel change ({code})."


class SensaUpdateChannelSelect(RestoreEntity, SelectEntity):
    """Which release channel this box follows.

    The channel is the tag on SENSA_STACK_IMAGE in the compose ``.env``. Nothing
    on a fielded box could change it before this existed: not a route, not a
    button, not a pipeline run. A unit provisioned onto edge stayed on edge for
    ever unless somebody reached the filesystem, which for a customer box means
    nobody (proven on a customer unit, 2026-08-26 — it sat on edge taking
    prerelease builds with no remote way off).

    The App has no writable copy of .env either, so selecting an option POSTs the
    bare channel name and the updater sidecar rewrites the line. CONTAINER
    APPLIANCE ONLY: HAOS resolves its channel through the add-on store.

    The current option is read back from the licence/backup status poller
    (``update_channel``) when it lands, so the select reflects what the BOX is
    actually pinned to rather than the last thing anyone picked here. Restored
    state is only the cold-boot placeholder until that first poll.
    """

    _attr_has_entity_name = True
    _attr_name = "Update channel"
    _attr_icon = "mdi:source-branch"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(UPDATE_CHANNEL_OPTIONS)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_update_channel"
        self.entity_id = UPDATE_CHANNEL_SELECT_ENTITY_ID
        self._attr_current_option = UPDATE_CHANNEL_DEFAULT
        # A channel written but not yet reported back by the box. See _held_option.
        self._pending: str | None = None
        self._pending_at = None

    def _reported_channel(self) -> str | None:
        """The channel the BOX says it resolves against, or None if it hasn't said.

        The App publishes this from the SENSA_STACK_IMAGE pin the updater actually
        asks the registry about. It is the truth; everything else here is a
        placeholder for the seconds before it lands.
        """
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if not isinstance(runtime, dict):
            return None
        addon = (runtime.get(RUNTIME_BACKUP_STATUS) or {}).get("addon") or {}
        reported = str(addon.get("channel") or "").strip().lower()
        return reported if reported in self._attr_options else None

    def _held_option(self) -> str | None:
        """A just-written channel we are still holding on screen, if any.

        The write goes App -> shared volume -> updater tick -> .env -> resolve,
        and only THEN does the box report the new channel back. Until it does,
        `_reported_channel` still returns the OLD one, so without this hold the
        dropdown snapped straight back to the previous value the instant it was
        changed and read as "it didn't save" — even though the write had
        succeeded and the notice said so.

        Expires rather than holding for ever: if the box never comes back with
        the new channel then the write did not take effect, and a control that
        kept displaying the value we asked for would be lying about the state of
        the box, which is the whole class of bug this change set exists to close.
        """
        if not self._pending or self._pending_at is None:
            return None
        if dt_util.utcnow() - self._pending_at > _CHANNEL_PENDING_TTL:
            return None
        return self._pending

    @property
    def current_option(self) -> str | None:
        """What to show: a pending write while it settles, else what the box says.

        Falls back to the restored value only while the box has not reported at
        all (a cold boot, or a stack image older than the App that publishes it),
        so the row is never blank.
        """
        reported = self._reported_channel()
        held = self._held_option()
        if held and held != reported:
            return held
        return reported or self._attr_current_option

    async def _async_settle_pending(self) -> None:
        """Re-poll the App so a channel change shows up in seconds, not minutes.

        The status poller that carries `channel` runs on a 15-minute cadence, and
        the optimistic hold above would otherwise be the only thing on screen for
        most of that. The updater applies the change on its ~5s tick and resolves
        immediately after, so a short wait then a refresh converges the display
        while the hold is still covering it. Best effort: the hold and the regular
        poller are the guarantees, this is only the shortcut.
        """
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        client = runtime.get("client") if isinstance(runtime, dict) else None
        if client is None:
            return
        from . import _async_refresh_backup_status

        for delay in (8, 20):
            await asyncio.sleep(delay)
            try:
                await _async_refresh_backup_status(self.hass, self._entry.entry_id, client)
            except Exception:
                _LOGGER.debug("Update channel: settle refresh failed", exc_info=True)
                continue
            self.async_write_ha_state()
            if self._reported_channel() == self._pending:
                self._pending = None
                self._pending_at = None
                return

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            _LOGGER.warning(
                "Update channel: ignoring unknown option %r (valid: %s)",
                option, self._attr_options,
            )
            return
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        client = runtime.get("client") if isinstance(runtime, dict) else None
        if client is None:
            raise HomeAssistantError(
                "The Sensa App is not reachable, so the release channel was not changed."
            )
        # Push FIRST and only move the control on success. Unlike the maintenance
        # window beside it, a channel that silently failed to apply would leave
        # the customer reading "stable" off a box still pulling edge builds, and
        # the next update would arrive from the channel they thought they had
        # left. A refusal must look like a refusal.
        #
        # EVERY failure below RAISES. A quiet `return` is a SUCCESSFUL service
        # call as far as Home Assistant is concerned, so the card showed its
        # "Release channel updated" notice over a write the App had refused, and
        # the only record of the reason was a debug log nobody can reach on an
        # appliance (2026-08-26 — the whole afternoon went into this). Raising is
        # the pattern `run_integrity_check` already uses for exactly this reason.
        try:
            reply = await client.async_post_ota_channel(option)
        except HomeAssistantError:
            raise
        except Exception as exc:
            # Covers BOTH a genuinely unreachable App and a 5xx from a reachable
            # one: the client turns 5xx into a ConnectionError while returning
            # 4xx as a body, so the App's own write failure (502
            # `ota_channel_write_failed`) arrives HERE and not in the body check
            # below. Hence wording that does not assert which it was.
            _LOGGER.warning("Update channel: could not set %s", option, exc_info=True)
            raise HomeAssistantError(
                f"The release channel was not changed: {exc}"
            ) from exc
        # A 4xx returns its parsed BODY rather than raising (only 5xx raises), so
        # an older App with no channel route answers 404 {"error": "not_found"}
        # and looks exactly like a success unless the payload is checked.
        if isinstance(reply, dict) and reply.get("error"):
            code = str(reply.get("error"))
            _LOGGER.warning("Update channel: App refused %s (%s)", option, code)
            raise HomeAssistantError(_CHANNEL_REFUSAL.get(code, _CHANNEL_REFUSAL_DEFAULT).format(code=code))
        # Hold the written value on screen until the box reports it back. Without
        # this the dropdown reverts immediately (the box still reports the old
        # channel) and reads as a failed save next to a success notice.
        self._pending = option
        self._pending_at = dt_util.utcnow()
        self._attr_current_option = option
        self.async_write_ha_state()
        self.hass.async_create_task(self._async_settle_pending())


class SensaHostUpdateWindowSelect(RestoreEntity, SelectEntity):
    """Customer maintenance window for host-OS (Layer 1) updates.

    The release manifest ships only a DEFAULT window; this select is the
    customer's override. On change it POSTs ``{window}`` to the App (which merges
    it into the on-host ``override.json``); the on-host agent re-points the update
    timer to the chosen window at once via its override-watch path unit.
    Appliance-only (a BYO host is the customer's own OS). RestoreEntity so the
    pick survives restarts; the on-host agent is the durable truth host-side.
    """

    _attr_has_entity_name = True
    _attr_name = "Maintenance window"
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(HOST_UPDATE_WINDOW_OPTIONS)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_host_update_window"
        self.entity_id = HOST_UPDATE_WINDOW_SELECT_ENTITY_ID
        self._attr_current_option = HOST_UPDATE_WINDOW_DEFAULT

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            _LOGGER.warning(
                "Host-update window: ignoring unknown option %r (valid: %s)",
                option, self._attr_options,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        client = runtime.get("client") if isinstance(runtime, dict) else None
        if client is None:
            return
        try:
            await client.async_post_host_update_config(window=option)
        except Exception:
            _LOGGER.debug("Host-update window push failed", exc_info=True)


def build_vebus_mode_select(
    coordinator: Any,
    entry_id: str,
    cerbo_device_id: str,
    instance: str,
) -> VictronInverterModeSelect:
    """Construct the inverter-mode control for one Multiplus/Quattro
    (vebus service instance) wired to a Cerbo.

    Exposed so the Victron `on_new_device` hook in __init__.py can create
    it the moment the broker first reports a vebus instance, the same way
    `build_device_entities` builds the read-only sensors.
    """
    return VictronInverterModeSelect(
        coordinator, entry_id, cerbo_device_id, instance,
    )


class VictronInverterModeSelect(CoordinatorEntity, SelectEntity):
    """Multiplus / Quattro inverter Mode control (Off / Charger only /
    Inverter only / On) over the Cerbo's MQTT broker.

    Reads the cached VE.Bus `Mode` register (an int) and writes the chosen
    mode back via `VictronCoordinator.async_set_vebus_mode`. Lives on the
    same per-vebus HA device card as the read-only Multiplus sensors
    (matching `VictronDeviceSensor.device_info`).

    The unique_id deliberately follows the
    `{entry_id}_victron_cerbo_{cerbo_id}_{service}_{instance}_{key}`
    convention so `_victron_cerbo_entity_maps` buckets it into the vebus
    sub-device's `service_entities["inverter_mode"]` role-map with no
    special-casing — that's how the Electrical power-flow card resolves
    the entity to put a control beside the AC Load node.

    Not `EntityCategory.CONFIG`: it's a real control, and a config-category
    entity is dropped from the role-resolution/eligibility sets.
    """

    _attr_has_entity_name = True
    _attr_name = "Inverter Mode"
    _attr_icon = "mdi:power-settings"
    _attr_options = INVERTER_MODE_OPTIONS

    def __init__(
        self,
        coordinator: Any,
        entry_id: str,
        cerbo_device_id: str,
        instance: str,
    ) -> None:
        super().__init__(coordinator)
        self._cerbo_device_id = cerbo_device_id
        self._instance = str(instance)
        self._attr_unique_id = (
            f"{entry_id}_victron_cerbo_{cerbo_device_id}_"
            f"vebus_{self._instance}_inverter_mode"
        )

    @property
    def current_option(self) -> str | None:
        """Map the cached raw `Mode` int to a friendly label.

        Returns None (HA shows "Unknown") until Venus has published the
        Mode topic for this vebus instance, or if it reports a value
        outside the known 1..4 register set.
        """
        cache = (self.coordinator.data or {}).get(f"vebus:{self._instance}", {})
        raw = cache.get("Mode")
        if raw is None:
            return None
        try:
            return _VEBUS_INT_TO_MODE_OPTION.get(int(raw))
        except (TypeError, ValueError):
            return None

    @property
    def device_info(self) -> dict[str, Any]:
        # Mirror VictronDeviceSensor.device_info so the control lands on
        # the same per-vebus device card (CustomName-preferred name,
        # nested under the parent Cerbo via `via_device`).
        from .vendors.victron_mqtt_services import SERVICE_LABELS

        label = SERVICE_LABELS.get("vebus", "Vebus")
        service_cache = (self.coordinator.data or {}).get(
            f"vebus:{self._instance}", {},
        )
        custom_name = str(service_cache.get("CustomName") or "").strip()
        name = custom_name or f"{label} #{self._instance}"
        return {
            "identifiers": {
                (DOMAIN,
                 (f"victron_cerbo_{self._cerbo_device_id}_"
                  f"vebus_{self._instance}")),
            },
            "name": name,
            "manufacturer": "Victron Energy",
            "model": label,
            "via_device": (DOMAIN, f"victron_cerbo_{self._cerbo_device_id}"),
        }

    async def async_select_option(self, option: str) -> None:
        """Write the chosen mode back to the Multiplus over MQTT."""
        mode_int = _VEBUS_MODE_OPTION_TO_INT.get(option)
        if mode_int is None:
            _LOGGER.warning(
                "Inverter mode: ignoring unknown option %r (valid: %s)",
                option, INVERTER_MODE_OPTIONS,
            )
            return
        ok = await self.coordinator.async_set_vebus_mode(
            self._instance, mode_int,
        )
        if ok:
            # Confirm the publish went out so a "nothing happened" report can be
            # split into "Sensa didn't send it" vs "the Multiplus ignored it".
            # If this INFO line appears but the inverter doesn't switch, the
            # write reached the broker and the cause is on the Victron side —
            # most commonly the Multiplus front switch / VEConfigure "remote
            # control" not being enabled (the physical switch overrides remote
            # Mode), or MQTT-on-LAN write access. See the inverter-mode notes.
            _LOGGER.info(
                "Inverter mode: published %r (mode %d) to "
                "W/<vrm>/vebus/%s/Mode",
                option, mode_int, self._instance,
            )
        else:
            _LOGGER.warning(
                "Inverter mode: write of %r (mode %d) to vebus %s did not "
                "reach the Cerbo broker (no connection / VRM id yet)",
                option, mode_int, self._instance,
            )


@callback
def _async_remove_legacy_log_level_select(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove the retired Log Level select from the registry.

    `select.sensa_nomad_log_level` was the Log card's minimum-severity dropdown.
    The per-type chips replaced it in 2026-06 and nothing has read it since; the
    card's vestigial `level_entity` config key went with the log split on
    2026-08-09, and the entity itself goes here. Left registered it would linger
    as a restored, unavailable row in Settings -> Devices & Services and read as
    a fault.

    Nothing is lost: the chips are per-browser display state, so there was never
    a customer choice in this entity worth carrying anywhere.

    Also reaps `select.sensa_nomad_log_retention` (2026-08-09), which existed
    only briefly: retention shipped as a fixed list of windows and became a free
    number of days the same day, so the setting now lives on
    `number.sensa_nomad_log_retention`. Deleting a `RestoreEntity` class leaves
    its registry row behind as a restored, unavailable entity, which reads as a
    fault; there is no migration to do, because the number entity carries its
    own default and any box that had the select had it for hours, not weeks.

    Also reaps `select.sensa_nomad_home_layout` (2026-09-30), the Habitation
    Home layout picker. Design Mode arranges Home now, so the layout is only
    the arrangement a box starts from and there is nothing left to pick.
    `select.sensa_nomad_cab_layout`, the Cab Home picker, went the same way.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for unique_suffix in ("log_level", "log_retention", "home_layout", "cab_layout"):
        unique_id = f"{entry.entry_id}_{unique_suffix}"
        entity_id = registry.async_get_entity_id("select", DOMAIN, unique_id)
        if entity_id:
            registry.async_remove(entity_id)


# Where the driver picker publishes the selected member_id for the nearby
# coordinator to read (one source of truth for the key).
CAB_MEMBER_ID_KEY = "cab_member_id"


def current_cab_member_id(hass: HomeAssistant) -> str:
    """The Hub User.id the driver picker has selected, or "" for the primary.

    "" is exactly what the Radar token exchange treats as the default (no member_id
    sent), so a box with no picker, or before the customer picks, behaves as it did
    before the picker existed. Read by `radar_coordinator` so the nearby-amenity
    search follows whoever is driving.
    """
    return str((hass.data.get(DOMAIN) or {}).get(CAB_MEMBER_ID_KEY, "") or "")


class SensaCabMemberSelect(RestoreEntity, SelectEntity):
    """Driver picker: which customer member the van acts as in Radar.

    A Nomad licence belongs to a CUSTOMER, and members may each use Radar as a
    standalone phone app with their OWN saved places. This dropdown lets whoever is
    driving pick their account, so the van's nearby-amenity search returns THEIR
    saved places. Options are the customer's members from Radar
    (`/auth/nomad/members`, masked emails), polled from the app.

    It changes the Radar SESSION only. It does NOT change who owns a recorded
    Tracking journey: that stays keyed on the customer, never the picked driver, so
    a drive is never shredded across members mid-journey.

    Sits on the Cab Home tab strip to the LEFT of the Layout picker. Self-hides (no
    options) until the app has a member list, so a box whose licence is unassigned
    or whose customer has no members simply shows no picker.
    """

    _attr_has_entity_name = False
    _attr_name = "Driver"
    _attr_icon = "mdi:account"
    _attr_entity_category = EntityCategory.CONFIG
    # Poll so a member added in mySensa appears without an HA restart.
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cab_member"
        self.entity_id = "select.sensa_nomad_cab_member"
        self._members: list[dict] = []
        self._picked: str = ""

    @staticmethod
    def _label(member: dict) -> str:
        return str(
            member.get("display_name")
            or member.get("email_masked")
            or member.get("id")
            or ""
        )

    @property
    def options(self) -> list[str]:
        out: list[str] = []
        for member in self._members:
            label = self._label(member)
            if label:
                out.append(label)
        return out

    @property
    def current_option(self) -> str | None:
        opts = self.options
        if self._picked in opts:
            return self._picked
        return opts[0] if opts else None

    def _member_id_for(self, label: str | None) -> str:
        for member in self._members:
            if self._label(member) == label:
                return str(member.get("id") or "")
        return ""

    def _publish(self) -> None:
        """Publish the selected member_id for the nearby coordinator to read."""
        self.hass.data.setdefault(DOMAIN, {})[CAB_MEMBER_ID_KEY] = self._member_id_for(
            self.current_option
        )

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def _refresh_members(self) -> None:
        from .radar_url import async_resolve_radar_members

        self._members = await async_resolve_radar_members(self.hass)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Prime the list so the dropdown is not empty on first paint.
        await self._refresh_members()
        last = await self.async_get_last_state()
        if last is not None and last.state in self.options:
            self._picked = last.state
        self._publish()

    async def async_update(self) -> None:
        await self._refresh_members()
        self._publish()

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            return
        self._picked = option
        self._publish()
        self.async_write_ha_state()


def _addressable_led_device_ids(hass: HomeAssistant) -> list[str]:
    """HA device ids of every addressable LED controller present in the registry.

    A controller qualifies exactly as the dashboard's `_build_zigbee_light_card`
    addressable branch does: the HA device owns both a `light.*` entity AND a
    `select.*_scene` expose (the Z2M scene-preset picker). That combination is
    specific to a Z2M pixel controller (the Gledopto GL-SPI-206P / WZ-SPI) — a
    plain bulb has no scene select, and the Sensa hub device has no `light`. So
    we never need the Sensa↔HA id mapping or the coordinator here.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
    except Exception:  # pragma: no cover - registry always present at runtime  # noqa: BLE001
        return []

    # `getattr`, not attribute access: this walk now runs from THREE platform
    # setups (select, number, switch), so one registry entry that does not look
    # like a full RegistryEntry would take a whole platform down with it.
    by_device: dict[str, list[str]] = {}
    for ent in ent_reg.entities.values():
        dev_id = getattr(ent, "device_id", None)
        eid = getattr(ent, "entity_id", None)
        if dev_id and eid:
            by_device.setdefault(dev_id, []).append(eid)

    out: list[str] = []
    for dev_id, eids in by_device.items():
        has_light = any(e.startswith("light.") for e in eids)
        has_scene = any(
            e.startswith("select.") and "_scene" in e for e in eids
        )
        if has_light and has_scene:
            out.append(dev_id)
    return out


def build_led_strip_selects(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[SensaLedStripSelect]:
    """One LED-strip (colour-order) select per addressable LED controller.

    Exposed so a controller adopted after initial setup can get its select
    created without an integration reload (see
    `_async_ensure_led_strip_selects` in __init__.py).
    """
    return [
        SensaLedStripSelect(entry, dev_id)
        for dev_id in _addressable_led_device_ids(hass)
    ]


class SensaLedStripSelect(RestoreEntity, SelectEntity):
    """Per-controller "LED Strip Profile" dropdown: which strip is wired to it.

    An addressable strip needs two things right before it lights correctly: the
    LIGHT BEAD SEQUENCE (which order it consumes its colour bytes, so a GRB strip
    driven as RGB reads green-as-red) and the CHIP TYPE (its driver IC). Both are
    printed on a reel or buried in a datasheet, so the customer picks their STRIP
    by name and we set both.

    **The two are not independent.** A four-letter sequence is a four-channel
    pixel, and the chip type is what makes the controller send four bytes per
    pixel instead of three. Mismatch them and the byte stream misaligns: the
    strip does not merely show wrong colours, part of it does not light at all.
    Observed on the bench 2026-09-06 as a strip whose first 10 pixels stayed
    dark until it was set to WBGR + SK6812.

    **The controller is TOLD, not compensated for.** This used to remap colours
    in the card, because this docstring said the Z2M GL-SPI-206P/WZ-SPI
    definition "exposes no colour-order control". That is not true: it exposes
    `light_bead_sequence` (Tuya dp 101) and `chip_type` (dp 102) as settable
    enums. Keeping our own copy beside the controller's own setting is one
    predicate written twice, and they drift the moment anyone touches Z2M. So
    selecting a profile WRITES both to the device.

    `Custom` writes nothing and leaves the controller's own two pickers to the
    customer, for a strip we have no profile for.

    Profiles live in `led_strip_profiles.json`: adding a strip is a data change,
    no code. The resolved values are published as ATTRIBUTES so the card reads
    them from here rather than keeping its own table.

    `RestoreEntity` keeps the choice across HA restarts; `EntityCategory.CONFIG`
    keeps it out of the Mode Editor eligibility set. No panel regen on a change:
    the card reads this entity's state directly from `hass` and re-renders on the
    state push (same pattern as the power-flow animation select).
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:led-strip-variant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(LED_STRIP_OPTIONS)

    def __init__(self, entry: ConfigEntry, ha_device_id: str) -> None:
        self._entry = entry
        self._ha_device_id = ha_device_id
        self._attr_name = "LED Strip Profile"
        self._attr_unique_id = f"{entry.entry_id}_led_strip_{ha_device_id}"
        self.entity_id = led_strip_select_entity_id(ha_device_id)
        self._attr_current_option = LED_STRIP_DEFAULT

    @property
    def device_info(self) -> dict:
        # Attach to the Sensa Nomad hub device (the Z2M controller's HA device is
        # mqtt-owned, so we can't merge onto it). Same home as the other Sensa
        # config selects (power-flow animation / log level).
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        """Restore the customer's last strip choice across HA restarts.

        Through `normalise_led_strip_profile`, so a box that stored the old
        LABEL (everything selected before 2026-09-16) or a label that has since
        been shortened comes back on the same strip instead of silently
        dropping to Standard. Never compare a stored value straight against the
        options.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        current = normalise_led_strip_profile(last.state)
        if current is None:
            return
        self._attr_current_option = current
        if current != last.state:
            _LOGGER.info(
                "LED Strip: %s had %r stored, migrated to id %r",
                self.entity_id, last.state, current,
            )

    @property
    def extra_state_attributes(self) -> dict:
        """The resolved settings, so the card reads them from here.

        `colour_order` is the lowercased sequence the card uses for its own
        swatches. Published rather than re-derived there, because the card
        holding its own name-to-order table is how the two came to disagree.
        """
        profile = LED_STRIP_PROFILES.get(self._attr_current_option or "", {})
        sequence = str(profile.get("bead_sequence") or "RGB")
        return {
            # id -> label for every option, so a UI rendering this dropdown
            # shows product names while the STATE stays an id. HA's select has
            # no value/label split, so the lookup has to live somewhere, and
            # here is the only place that owns both.
            "labels": {
                **LED_STRIP_LABELS,
                LED_STRIP_CUSTOM: led_strip_profile_label(LED_STRIP_CUSTOM),
            },
            "label": led_strip_profile_label(self._attr_current_option),
            "profile_id": profile.get("id") or "",
            "manufacturer_code": profile.get("manufacturer_code") or "",
            "bead_sequence": sequence,
            "chip_type": profile.get("chip_type"),
            "chip_type_is_substitute": bool(profile.get("chip_type_is_substitute")),
            "colour_order": sequence.lower(),
            # Custom means "use the controller's own two pickers": the card shows
            # those instead of a profile summary, and nothing is written here.
            "is_custom": self._attr_current_option == LED_STRIP_CUSTOM,
        }

    async def async_select_option(self, option: str) -> None:
        """Persist the choice and configure the controller to match."""
        if option not in LED_STRIP_OPTIONS:
            _LOGGER.warning(
                "LED strip profile: ignoring unknown option %r (valid: %s)",
                option, LED_STRIP_OPTIONS,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()
        if option == LED_STRIP_CUSTOM:
            # Deliberately writes NOTHING. The customer is about to set the two
            # values themselves, and pushing a profile first would fight them.
            return
        await self._async_apply_to_controller(LED_STRIP_PROFILES.get(option, {}))

    async def _async_apply_to_controller(self, profile: dict) -> None:
        """Write the profile's sequence and chip to the controller's own selects.

        Best effort, never raises: the customer's choice is already saved, and a
        controller that is offline or whose Z2M entities have not appeared yet
        must not make the dropdown fail. It re-applies on the next selection.

        A chip of None is NOT written. An unconfirmed IC left alone is a strip
        that keeps working; a guessed one changes the data protocol and stops it
        working entirely.
        """
        targets = (
            ("light_bead_sequence", profile.get("bead_sequence")),
            ("chip_type", profile.get("chip_type")),
        )
        for suffix, value in targets:
            if not value:
                continue
            entity_id = self._controller_select(suffix)
            if entity_id is None:
                _LOGGER.debug(
                    "LED strip profile: %s has no %s entity yet, skipping",
                    self._ha_device_id, suffix,
                )
                continue
            try:
                await self.hass.services.async_call(
                    "select", "select_option",
                    {"entity_id": entity_id, "option": value},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001 - the choice is already saved
                _LOGGER.warning(
                    "LED strip profile: could not set %s=%s on %s: %s",
                    suffix, value, entity_id, err,
                )

    def _controller_select(self, suffix: str) -> str | None:
        """The controller's own `select.*_<suffix>` entity, or None.

        Resolved off the HA DEVICE, not by name: Z2M names its entities from the
        device's friendly name, which the customer can change at any time, and a
        renamed controller must not silently stop being configured.
        """
        try:
            from homeassistant.helpers import entity_registry as er

            ent_reg = er.async_get(self.hass)
        except Exception:  # noqa: BLE001
            return None
        for ent in ent_reg.entities.values():
            if ent.device_id != self._ha_device_id:
                continue
            if ent.entity_id.startswith("select.") and ent.entity_id.endswith(suffix):
                return ent.entity_id
        return None


def _zigbee_light_device_ids(hass: HomeAssistant) -> list[str]:
    """HA device ids of every Zigbee2MQTT device that owns a `light.*` entity.

    ADDRESSABLE controllers are excluded. The select exists to resolve the one
    thing neither we nor the controller can see: the STRIP. A WZ5 left in
    RGB+CCT mode wired to a white-only strip would otherwise render a colour
    mixer nothing can drive. A pixel controller (the Gledopto GL-SPI-206P /
    WZ-SPI) has no such ambiguity — its strip is addressable RGB by definition —
    so the question has no meaningful answer, and offering it can only mislead:
    picking "White" would hide colour controls that genuinely work.

    The addressable test is `_addressable_led_device_ids`, reused rather than
    restated, so the two lists cannot drift into both claiming a device.

    Otherwise covers plain controllers and bulbs: the WZ5 has no scene entity
    but still needs a Light Type select. Z2M ownership is confirmed via
    `zigbee2mqtt_device_ieee` (non-None only for a real Z2M device, not the
    bridge or a non-Z2M mqtt device), so this never picks the Sensa hub or an
    unrelated entity. A non-Sensa Z2M bulb may also match, but its select is a
    harmless hidden CONFIG entity — no card ever references it (the dashboard
    only wires `light_type_entity` onto cards it renders for Sensa devices).
    """
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        from .vendors.zigbee2mqtt import zigbee2mqtt_device_ieee

        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
    except Exception:  # pragma: no cover - registries always present at runtime  # noqa: BLE001
        return []

    with_light: set[str] = {
        getattr(ent, "device_id", None)
        for ent in ent_reg.entities.values()
        if getattr(ent, "device_id", None)
        and str(getattr(ent, "entity_id", "")).startswith("light.")
    }
    addressable = set(_addressable_led_device_ids(hass))
    out: list[str] = []
    for dev_id in with_light:
        if dev_id in addressable:
            continue
        ha_dev = dev_reg.devices.get(dev_id)
        if ha_dev is not None and zigbee2mqtt_device_ieee(ha_dev):
            out.append(dev_id)
    return out


def zigbee_light_controller_device_ids(hass: HomeAssistant) -> list[str]:
    """HA device ids of EVERY Zigbee2MQTT device that drives a light: the
    addressable / pixel controllers plus the plain controllers and bulbs.

    The two sets are disjoint by construction (`_zigbee_light_device_ids`
    excludes anything `_addressable_led_device_ids` claims), so this is their
    union with no dedup needed. Exists so a per-controller entity that applies
    to both kinds (the Power-On Delay number) enumerates them from the same two
    resolvers the selects use, rather than growing a third rule for what counts
    as a lighting controller.
    """
    return [
        *_addressable_led_device_ids(hass),
        *_zigbee_light_device_ids(hass),
    ]


def build_light_type_selects(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[SensaLightTypeSelect]:
    """One "Light Type" select per Zigbee2MQTT lighting controller/bulb.

    Exposed so a controller adopted after initial setup can get its select
    created without an integration reload (see
    `_async_ensure_light_type_selects` in __init__.py).
    """
    return [
        SensaLightTypeSelect(entry, dev_id)
        for dev_id in _zigbee_light_device_ids(hass)
    ]


class SensaLightTypeSelect(RestoreEntity, SelectEntity):
    """Per-controller "Light Type" dropdown, defaulting to what Z2M detected.

    A 5-in-1 controller like the Skydance WZ5 has its output mode chosen on the
    hardware with the match button. That mode reaches us through Zigbee2MQTT as
    the device's MODEL (`WZ5_cct`, `WZ5_rgbw`, …), because the mode changes the
    Tuya `manufacturerName` and Z2M resolves a different definition — so the
    common case needs no customer input at all, which is what `Auto` (the
    default) does.

    The customer can still override, because the one thing neither we nor the
    controller can see is the STRIP: a white-only strip wired to a controller
    left in RGB+CCT mode would otherwise render a colour mixer nothing can
    drive. A WZ5 is offered its own five modes, each named with the RUN LED
    colour the unit shows (the manual: "White: DIM, Yellow: CCT, Red: RGB,
    Green: RGBW, Blue: RGB+CCT"), so the customer reads the mode off the
    hardware and picks the matching line rather than translating it. Any other
    Z2M light gets the three generic render behaviours.

    Deliberately NOT seeded with the detected value: keeping `Auto` as a real
    option means the choice stays live, so changing the mode on the hardware is
    followed automatically with nothing to re-pick.

    `RestoreEntity` keeps the choice across HA restarts; `EntityCategory.CONFIG`
    keeps it out of the Mode Editor eligibility set. No panel regen on a change:
    the card reads this entity's state directly from `hass` and re-renders on the
    state push (same pattern as `SensaLedStripSelect`).
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:palette-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, ha_device_id: str) -> None:
        self._entry = entry
        self._ha_device_id = ha_device_id
        self._attr_name = "Light Type"
        self._attr_unique_id = f"{entry.entry_id}_light_type_{ha_device_id}"
        self.entity_id = light_type_select_entity_id(ha_device_id)
        self._attr_current_option = LIGHT_TYPE_AUTO

    def _model(self) -> str:
        """The model Z2M reports for this controller, or "" if unresolvable.

        Read live rather than cached at construction: a device re-interviewed
        into a different mode reports a different model, and the options list
        should follow it without an integration reload.
        """
        from .vendors.zigbee2mqtt import zigbee2mqtt_reported_model

        return zigbee2mqtt_reported_model(self.hass, self._ha_device_id)

    def _manufacturer_code(self) -> str:
        """The raw Tuya batch code for this controller, or "" if unknown.

        The SECOND identity key, from Z2M's bridge cache. It carries the mode on
        a batch whose definition Z2M cannot resolve, where the model id falls
        back to a generic `TS0601` and names nothing. "" whenever the cache is
        cold, which every consumer treats as "could not ask" — the model-only
        answer, never a guess.
        """
        from .vendors.zigbee2mqtt import zigbee2mqtt_reported_manufacturer

        return zigbee2mqtt_reported_manufacturer(self.hass, self._ha_device_id)

    @property
    def options(self) -> list[str]:
        from .vendors.zigbee2mqtt import light_type_options_for_model

        return light_type_options_for_model(
            self._model(), self._manufacturer_code(),
        )

    @property
    def current_option(self) -> str:
        # A stored pick from a different option set (the row's device was
        # swapped, or a build shipped different labels) is not offerable — fall
        # back to Auto rather than showing a value the dropdown cannot select.
        option = self._attr_current_option or LIGHT_TYPE_AUTO
        return option if option in self.options else LIGHT_TYPE_AUTO

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Publish what the pick RESOLVED to, so the cards never re-derive it.

        `detected` is what Z2M's model implies on its own; `resolved` is what
        the cards actually render (the override when set, else the detection).
        Both are None when we cannot tell, which is the signal that the model is
        unmapped — never coerce either to a default here.
        """
        from .vendors.zigbee2mqtt import (
            led_colour_for_model,
            light_mode_for_model,
            resolve_led_colour,
            resolve_light_type,
            resolve_wz5_mode_label,
        )

        model = self._model()
        code = self._manufacturer_code()
        option = self.current_option
        return {
            "model": model or None,
            # The raw Tuya manufacturerName. Published so a support call can read
            # the batch straight off the entity instead of needing the Z2M UI —
            # it is the one string that names an unmapped batch.
            "manufacturer_code": code or None,
            "detected": light_mode_for_model(model, code),
            "resolved": resolve_light_type(option, model, code),
            # Which of the FIVE hardware modes is in force, as the RUN LED
            # colour the unit itself shows. Separate from `resolved`, which
            # collapses those five onto three render behaviours and so cannot
            # tell RGB from RGBW from RGB+CCT.
            "detected_led_colour": led_colour_for_model(model, code),
            "led_colour": resolve_led_colour(option, model, code),
            # The mode's own label, so a surface showing the LED can name it
            # without reverse-engineering the colour back into a mode.
            "mode_label": resolve_wz5_mode_label(option, model, code),
        }

    @property
    def device_info(self) -> dict:
        # Attach to the Sensa Nomad hub device (the Z2M controller's HA device is
        # mqtt-owned, so we can't merge onto it) — same home as the other Sensa
        # config selects.
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        """Restore the customer's last light-type choice across HA restarts.

        Translated through `migrate_light_type_option` because a box upgraded
        from a build that offered only the three plain behaviours restores a
        value a WZ5's option list no longer contains, and simply dropping it
        would hand every hand-narrowed light its colour controls back.
        """
        from .vendors.zigbee2mqtt import migrate_light_type_option

        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        migrated = migrate_light_type_option(
            last.state, self._model(), self._manufacturer_code(),
        )
        if migrated is not None:
            self._attr_current_option = migrated

    async def async_select_option(self, option: str) -> None:
        """Persist the choice. The card re-renders (showing/hiding the colour
        controls) automatically when this entity's state changes."""
        if option not in self.options:
            _LOGGER.warning(
                "Light type: ignoring unknown option %r (valid: %s)",
                option, self.options,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()


def build_relay_assignment_selects(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
) -> list[SensaRelayChannelAssignmentSelect]:
    """Build 8 "Works with" select entities for one relay controller.

    Exposed so the Discovery picker's add-dispatch can create these for a
    newly-registered relay device without reloading the entry — mirrors
    build_relay_text_entities / build_relay_enabled_switches.
    """
    from .device_catalog import relay_channel_count

    device_id = str(device.get("device_id") or device.get("id") or "unknown")
    device_name = device.get("display_name") or device.get("name") or "Relay"
    return [
        SensaRelayChannelAssignmentSelect(hass, entry, device_id, device_name, n)
        for n in range(1, relay_channel_count(device) + 1)
    ]


class SensaRelayChannelAssignmentSelect(SelectEntity):
    """The relay channel "Works with" dropdown — one per channel.

    Lets the customer declare what a dry-contact channel actually drives so
    the channel can be routed somewhere useful (Phase 2/3):

      • "unassigned"            — default, no routing.
      • a Switched-Device class — e.g. "lighting", "water_pump"; Sensa only
        switches it on/off (no telemetry). Surfaces as a control tile on its
        category tab.
      • "device:<device_id>"    — an Integrated Device Sensa already drives;
        the channel adds power on/off to that device's card.

    NOT a RestoreEntity: the value lives in
    `entry.options["relay_assignments"][device_id][slot]`, mirroring the
    relay_labels / relay_enabled nested-dict pattern, so it survives restarts
    via the config entry rather than restore-state. `EntityCategory.CONFIG`
    keeps it out of the Mode Editor eligibility set. Polls so the option list
    re-evaluates as integrated devices are added/removed (a dynamic
    `options`).
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:link-variant"
    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        n: int,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._n = n
        self._slot_key = f"relay_{n}"
        self._attr_name = f"{device_name} channel {n} works with"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_relay_{n}_assignment"
        self.entity_id = relay_channel_assignment_entity_id(device_id, n)

    @property
    def options(self) -> list[str]:
        choices = _integrated_device_choices(self.hass, self._entry)
        return [
            RELAY_UNASSIGNED,
            *RELAY_ASSIGNABLE_SERVICE_CLASSES,
            *(f"device:{d_id}" for d_id, _ in choices),
        ]

    @property
    def current_option(self) -> str | None:
        stored = (
            (self._entry.options.get("relay_assignments") or {})
            .get(self._device_id, {})
            .get(self._slot_key)
        )
        # Unassigned by default; also fall back to unassigned if the stored
        # value isn't currently selectable (e.g. the linked Integrated Device
        # was removed) so the dropdown never shows a dangling token.
        if not stored or stored not in self.options:
            return RELAY_UNASSIGNED
        return stored

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            _LOGGER.warning(
                "Relay channel assignment: ignoring unknown option %r "
                "(device=%s slot=%s)", option, self._device_id, self._slot_key,
            )
            return

        all_assign: dict[str, Any] = dict(
            self._entry.options.get("relay_assignments") or {}
        )
        dev_assign: dict[str, str] = dict(all_assign.get(self._device_id) or {})
        if option == RELAY_UNASSIGNED:
            dev_assign.pop(self._slot_key, None)  # default → don't store
        else:
            dev_assign[self._slot_key] = option
        if dev_assign:
            all_assign[self._device_id] = dev_assign
        elif self._device_id in all_assign:
            del all_assign[self._device_id]

        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, "relay_assignments": all_assign},
        )

        # Rebuild dashboards so the assignment is reflected in the card config
        # (and, from Phase 2, the routed category tabs / device power toggle).
        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


def build_switch_port_selects(
    hass: HomeAssistant, entry: ConfigEntry, device_id: str, device_name: str,
    ports: list[dict[str, Any]],
) -> list[SelectEntity]:
    """One "Device Type" dropdown per network-switch port.

    Built from the device's REPORTED port list rather than a fixed count (a
    relay controller always has 8 channels; a switch has whatever it has). A
    port with no id is skipped, since the id is the storage key.
    """
    return [
        SensaSwitchPortAssignmentSelect(
            hass, entry, device_id, device_name,
            str(port.get("id") or ""), str(port.get("label") or ""),
        )
        for port in ports
        if port.get("id")
    ]


class SensaSwitchPortAssignmentSelect(SelectEntity):
    """What is plugged into this network-switch port.

    The relay channel's "Device Type" dropdown, for a switch port: it answers
    "what is on the end of this cable". Offers the SAME Integrated Device list
    the relay assignment does (`_integrated_device_choices`), so a port can be
    pointed at a device Sensa already drives.

    Deliberately NOT offering the relay's Switched-Device service classes: a
    relay channel SWITCHES a load, so declaring it drives `lighting` routes a
    control tile onto the Lighting tab. A network port carries data to a
    device; saying a port "is lighting" would route a tile that switches
    nothing. Integrated Device or nothing.

    Storage mirrors `relay_assignments`:
    `entry.options["switch_port_assignments"][device_id][port_id]`.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:ethernet-cable"
    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        port_id: str,
        port_label: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._port_id = port_id
        self._attr_name = (
            f"{device_name} {port_label or port_id} device type"
        )
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}_netsw_{port_id}_assignment"
        )
        self.entity_id = switch_port_assignment_entity_id(device_id, port_id)

    @property
    def options(self) -> list[str]:
        choices = _integrated_device_choices(self.hass, self._entry)
        return [
            RELAY_UNASSIGNED,
            *(f"device:{d_id}" for d_id, _ in choices),
        ]

    @property
    def current_option(self) -> str | None:
        stored = (
            (self._entry.options.get(SWITCH_PORT_ASSIGNMENTS_OPTION) or {})
            .get(self._device_id, {})
            .get(self._port_id)
        )
        # Fall back to unassigned when the stored device has since been
        # removed, so the dropdown never shows a dangling token.
        if not stored or stored not in self.options:
            return RELAY_UNASSIGNED
        return stored

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            _LOGGER.warning(
                "Switch port assignment: ignoring unknown option %r "
                "(device=%s port=%s)", option, self._device_id, self._port_id,
            )
            return

        all_assign: dict[str, Any] = dict(
            self._entry.options.get(SWITCH_PORT_ASSIGNMENTS_OPTION) or {}
        )
        dev_assign: dict[str, str] = dict(all_assign.get(self._device_id) or {})
        if option == RELAY_UNASSIGNED:
            dev_assign.pop(self._port_id, None)  # default -> don't store
        else:
            dev_assign[self._port_id] = option
        if dev_assign:
            all_assign[self._device_id] = dev_assign
        elif self._device_id in all_assign:
            del all_assign[self._device_id]

        self.hass.config_entries.async_update_entry(
            self._entry,
            options={
                **self._entry.options,
                SWITCH_PORT_ASSIGNMENTS_OPTION: all_assign,
            },
        )

        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


class SensaLevellingAxisSelect(RestoreEntity, SelectEntity):
    """Which way the sensor's lid axes point in the vehicle: one dropdown
    each for Front / Left / Up, answered X, X-, Y, Y-, Z or Z- straight off
    the arrows printed on the lid.

    Lets a customer install the device sideways or upside-down and still get a
    correct pitch/roll reading: the orientated-pitch/roll sensors re-derive
    from the raw accel vector using whatever mapping is set here. Defaults to
    the identity mapping (front=X, left=Y, up=Z).
    `EntityCategory.CONFIG` keeps it out of the Mode Editor eligibility set.

    Until 2026-10-29 the sign was a second select per axis. The entity_id and
    unique_id are unchanged, and a restored "-" from the retired sign select is
    folded in once, so an existing install keeps its mapping.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:axis-arrow"
    _attr_options: ClassVar[list[str]] = list(VAN_LEVEL_AXIS_OPTIONS)

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        logical_axis: str,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._device_name = device_name
        self._logical = logical_axis
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}_{logical_axis}_axis"
        )
        self._attr_name = VAN_LEVEL_LOGICAL_LABELS[logical_axis]
        self._attr_current_option = VAN_LEVEL_DEFAULT_AXIS_MAP[logical_axis]
        self.entity_id = van_level_axis_select_entity_id(
            device_id, logical_axis
        )

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"van_level_{self._device_id}")},
            "name": self._device_name,
            "manufacturer": "Sensa",
            "model": "Van Level",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in VAN_LEVEL_AXIS_OPTIONS:
            self._attr_current_option = last.state
        self._fold_retired_sign()

    def _fold_retired_sign(self) -> None:
        """Fold the retired sign select into this one, then drop it from the
        entity registry. Its last state survives in the restore store after
        the entity stops being created."""
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers.restore_state import (
            async_get as async_get_restore_state,
        )

        sign_eid = van_level_sign_select_entity_id(self._device_id, self._logical)
        ent_reg = er.async_get(self.hass)
        if ent_reg.async_get(sign_eid) is None:
            return
        stored = async_get_restore_state(self.hass).last_states.get(sign_eid)
        current = self._attr_current_option or ""
        if (
            stored is not None
            and stored.state.state == "-"
            and not current.endswith("-")
        ):
            self._attr_current_option = f"{current}-"
            _LOGGER.info(
                "Van Level %s: folded retired sign into %s = %s",
                self._device_id, self.entity_id, self._attr_current_option,
            )
        ent_reg.async_remove(sign_eid)

    async def async_select_option(self, option: str) -> None:
        if option not in VAN_LEVEL_AXIS_OPTIONS:
            _LOGGER.warning(
                "Van Level axis: ignoring unknown option %r (valid: %s)",
                option, VAN_LEVEL_AXIS_OPTIONS,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()
