"""Mopeka tank sensors, adopted from HA's own `mopeka` integration.

A Mopeka is an ultrasonic BLE sensor stuck to the underside of a bottle. HA
core's `mopeka` integration (Adopt method) owns the device and reports the
liquid HEIGHT in millimetres (`tank_level`), never a percentage: the sensor
cannot know the tank. So Sensa adds, per Mopeka, on its device row:

  * "Tank Shape": what it is fitted to, offered by Contents. LPG offers the
    bottle sizes; diesel and water offer a flat-sided tank or a round tank on
    its side. Options-as-truth in `entry.options[MOPEKA_BOTTLE_OPTION]`;
  * "Tank Height": the inside height of a flat-sided tank, or the diameter of
    a round one, in mm (`entry.options[MOPEKA_HEIGHT_OPTION]`). A slider, since
    Nomad is a tablet. Not used by a bottle size;
  * a "Tank is full now" button: pressed on ONE full tank, it saves the
    reading over that tank's height as a correction for its Contents
    (`entry.options[MOPEKA_CAL_OPTION][medium]`). The mismatch between HA's
    millimetres and the real height comes from the library's conversion, not
    from the tank, so every Mopeka holding that liquid uses it;
  * a "Tank Level" sensor in %, from the native millimetres and those two.
    Unavailable until they are set: a guessed tank is a confident wrong
    number, which is worse than no dial.

The bottle presets and their straight-line conversion are ESPHome's
`mopeka_pro_check` component (`CONF_SUPPORTED_TANKS_MAP`), so a Sensa dial
reads the same as an ESPHome one on the same bottle; the domed ends make the
top and bottom few percent approximate. A flat-sided tank is linear in height;
a round tank on its side uses the area of a circular segment.

The medium (diesel, water, ...) is the native config entry's `medium_type`,
and it matters: the library corrects the reading for the speed of sound in that
liquid. Our add path completes the native flow with `{}`, which takes its
default (`propane`), so a "Contents" select on the device row lets the customer
change it. It reads and writes the native entry itself, never a copy of its own.

A TD40 / TD200 is the exception: it sits on TOP of the tank and measures the
air gap down to the liquid, so HA needs the medium `air` whatever the tank
holds. For one of those the integration sets `air` itself, keeps the liquid on
our own entry (`entry.options[MOPEKA_CONTENTS_OPTION]`, for the Home dial),
offers tank shapes only, and turns the gap into a level against Tank Height
(measured from the sensor down to the bottom of the tank).
"""
from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN, _relay_device_id_safe

_LOGGER = logging.getLogger(__name__)

MOPEKA_DOMAIN = "mopeka"

MOPEKA_BOTTLE_OPTION = "mopeka_bottle"
MOPEKA_BOTTLE_UID_SUFFIX = "_mopeka_bottle"
MOPEKA_LEVEL_UID_SUFFIX = "_mopeka_level"
MOPEKA_CONTENTS_UID_SUFFIX = "_mopeka_contents"
MOPEKA_HEIGHT_OPTION = "mopeka_height"
MOPEKA_CAL_OPTION = "mopeka_calibration"
MOPEKA_FULL_UID_SUFFIX = "_mopeka_full"
SIGNAL_MOPEKA_CALIBRATED = "sensa_nomad_mopeka_calibrated"
# A calibration outside this reading/height ratio is refused: the tank was not
# full, the height is wrong, or the sensor lost its echo.
MOPEKA_CAL_MIN = 0.2
MOPEKA_CAL_MAX = 2.0
MOPEKA_HEIGHT_UID_SUFFIX = "_mopeka_height"
# Slider range for Tank Height, mm.
MOPEKA_HEIGHT_MIN = 100
MOPEKA_HEIGHT_MAX = 1500
MOPEKA_HEIGHT_STEP = 5
# "Tank Capacity" in litres, diesel only: what turns the % into litres for the
# heaters' Diesel Days Left (`diesel_use.py`). Options-as-truth on the entry.
MOPEKA_CAPACITY_OPTION = "mopeka_capacity"
MOPEKA_CAPACITY_UID_SUFFIX = "_mopeka_capacity"
MOPEKA_CAPACITY_MIN = 5
MOPEKA_CAPACITY_MAX = 250
MOPEKA_CAPACITY_STEP = 1

# Tank shapes measured against Tank Height rather than a preset.
SHAPE_FLAT = "flat"
SHAPE_HORIZONTAL = "horizontal"
MOPEKA_SHAPES: dict[str, str] = {
    SHAPE_FLAT: "Flat-sided tank",
    SHAPE_HORIZONTAL: "Round tank, on its side",
}

# The Contents choices a customer is offered: every liquid
# `mopeka_iot_ble.MediumType` has (HA's `mopeka` pins 0.8.0), value -> label.
# Labels match the Home dials (`dashboard._TANK_FLUID_DIALS`). `air` is not a
# liquid: it is the medium a top-down sensor needs, set by the integration.
MOPEKA_CONTENTS: dict[str, str] = {
    "propane": "LPG",
    "diesel": "Diesel",
    "gasoline": "Petrol",
    "fresh_water": "Fresh Water",
    "waste_water": "Grey Water",
    "black_water": "Black Water",
    "raw_water": "Raw Water",
    "live_well": "Live Well",
    "lng": "LNG",
    "oil": "Oil",
    "hydraulic_oil": "Hydraulic Oil",
}
# The medium HA needs for a top-down sensor, whatever the tank holds.
MEDIUM_AIR = "air"
# The `mopeka_iot_ble` device model of the sensors that mount on top of the
# tank (TD40 / TD200, with or without cellular, share it).
MOPEKA_TOP_DOWN_MODELS = frozenset({"TD40/TD200"})
# What a top-down sensor's tank holds, per Sensa device id (its native medium
# is always air, so the liquid has to live here).
MOPEKA_CONTENTS_OPTION = "mopeka_contents"

# id -> (label, empty mm, full mm). Copied from ESPHome's
# `esphome/components/mopeka_pro_check/sensor.py` CONF_SUPPORTED_TANKS_MAP;
# add a bottle only with a measured full height, never an estimate.
MOPEKA_BOTTLES: dict[str, tuple[str, int, int]] = {
    "eu_6kg": ("6 kg bottle (Europe)", 38, 336),
    "eu_11kg": ("11 kg bottle (Europe)", 38, 366),
    "eu_14kg": ("14 kg bottle (Europe)", 38, 467),
    "us_20lb": ("20 lb bottle (US)", 38, 254),
    "us_30lb": ("30 lb bottle (US)", 38, 381),
    "us_40lb": ("40 lb bottle (US)", 38, 508),
}

# `mopeka_iot_ble.MediumType` value -> the Venus OS fluid label the Home dials
# key on (`dashboard._TANK_FLUID_DIALS`). `air` has no dial and is absent.
MEDIUM_TO_FLUID: dict[str, str] = {
    "propane": "lpg",
    "fresh_water": "fresh-water",
    "waste_water": "waste-water",
    "black_water": "black-water",
    "live_well": "live-well",
    "raw_water": "raw-water",
    "gasoline": "gasoline",
    "diesel": "diesel",
    "lng": "lng",
    "oil": "oil",
    "hydraulic_oil": "hydraulic-oil",
}
# The native flow's own default (`mopeka/const.py` DEFAULT_MEDIUM_TYPE), also
# what an entry created before medium types existed reads as.
_DEFAULT_MEDIUM = "propane"


def is_mopeka(device: dict[str, Any]) -> bool:
    """Whether a backend device row is an adopted Mopeka."""
    integration = device.get("integration_type") or device.get("integration") or ""
    return integration == MOPEKA_DOMAIN


def device_mac(device: dict[str, Any]) -> str:
    """A device row's Bluetooth MAC in HA's canonical `AA:BB:...` form."""
    mac = str(device.get("linked_mac_address") or device.get("mac_address") or "")
    mac = mac.replace("-", ":").upper()
    if ":" not in mac and len(mac) == 12:
        mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
    return mac


def level_percent(
    distance_mm: float | None,
    shape: str | None,
    height_mm: float | None = None,
    calibration: float | None = None,
    top_down: bool = False,
) -> float | None:
    """Fill level in % for a liquid height, or None when anything it needs is
    unknown. A bottle size uses ESPHome's clamp-and-scale; a flat-sided tank is
    height over `height_mm`; a round tank on its side is the filled share of a
    circle of diameter `height_mm`. A tank shape's reading is first divided by
    the Contents' `calibration` (reading at full over real height), if any.
    `top_down`: the reading is the air gap from the top, so the liquid is
    `height_mm` minus it (a bottle size never applies)."""
    if distance_mm is None:
        return None
    if calibration and shape in MOPEKA_SHAPES:
        distance_mm = distance_mm / calibration
    if top_down:
        if shape not in MOPEKA_SHAPES or not height_mm or height_mm <= 0:
            return None
        distance_mm = float(height_mm) - float(distance_mm)
    spec = MOPEKA_BOTTLES.get(shape or "")
    if spec is not None:
        _label, empty, full = spec
        if distance_mm >= full:
            return 100.0
        if distance_mm <= empty:
            return 0.0
        return round(100.0 * (distance_mm - empty) / (full - empty), 1)
    if shape not in MOPEKA_SHAPES or not height_mm or height_mm <= 0:
        return None
    h = min(max(float(distance_mm), 0.0), float(height_mm))
    if shape == SHAPE_FLAT:
        return round(100.0 * h / height_mm, 1)
    r = height_mm / 2.0
    segment = r * r * math.acos((r - h) / r) - (r - h) * math.sqrt(max(2 * r * h - h * h, 0.0))
    return round(100.0 * segment / (math.pi * r * r), 1)


def bottle_for(entry: ConfigEntry, device_id: str) -> str | None:
    """The tank shape chosen for a Mopeka (a MOPEKA_SHAPES key or a bottle
    size), or None when none has been chosen."""
    ledger = entry.options.get(MOPEKA_BOTTLE_OPTION) or {}
    value = ledger.get(device_id) if isinstance(ledger, dict) else None
    return value if value in MOPEKA_BOTTLES or value in MOPEKA_SHAPES else None


def height_for(entry: ConfigEntry, device_id: str) -> float | None:
    """The Tank Height set for a Mopeka in mm, or None."""
    ledger = entry.options.get(MOPEKA_HEIGHT_OPTION) or {}
    value = ledger.get(device_id) if isinstance(ledger, dict) else None
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def capacity_for(entry: ConfigEntry, device_id: str) -> float | None:
    """The Tank Capacity set for a Mopeka in litres, or None."""
    ledger = entry.options.get(MOPEKA_CAPACITY_OPTION) or {}
    value = ledger.get(device_id) if isinstance(ledger, dict) else None
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def shape_options(medium: str | None, top_down: bool = False) -> list[str]:
    """The Tank Shape choices for a Contents: bottle sizes for LPG under a
    bottom-up sensor, a tank shape for everything else."""
    if medium == "propane" and not top_down:
        return list(MOPEKA_BOTTLES)
    return list(MOPEKA_SHAPES)


def shape_for(
    hass: HomeAssistant, entry: ConfigEntry, device_id: str, mac: str,
) -> str | None:
    """The chosen Tank Shape if it fits the Contents, else None. A bottle size
    left over from when the tank was LPG must not scale a diesel tank."""
    shape = bottle_for(entry, device_id)
    options = shape_options(
        contents(hass, entry, device_id, mac), is_top_down(hass, mac),
    )
    return shape if shape in options else None


def calibration_for(entry: ConfigEntry, medium: str | None) -> float | None:
    """The saved reading-over-height correction for a Contents, or None."""
    ledger = entry.options.get(MOPEKA_CAL_OPTION) or {}
    value = ledger.get(medium or "") if isinstance(ledger, dict) else None
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def needs_height(shape: str | None) -> bool:
    """Whether a shape is measured against Tank Height."""
    return shape in MOPEKA_SHAPES


def native_entity(hass: HomeAssistant, mac: str, key: str) -> str | None:
    """The native `mopeka` entity for one key (`tank_level`, `temperature`,
    `battery`, ...) on the sensor at `mac`, or None until it registers.

    HA's passive-Bluetooth entities are unique by `{address}-{key}` (plus
    `-{device_id}` on multi-device adverts), so this matches on the address
    and the key and never on the entity_id, which the customer can rename.
    """
    if not mac:
        return None
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return None
    for ent in ent_reg.entities.values():
        if getattr(ent, "platform", None) != MOPEKA_DOMAIN:
            continue
        parts = str(getattr(ent, "unique_id", "") or "").split("-")
        if len(parts) >= 2 and parts[0].upper() == mac and parts[1] == key:
            return ent.entity_id
    return None


def native_entry(hass: HomeAssistant, mac: str) -> ConfigEntry | None:
    """The native `mopeka` config entry for the sensor at `mac` (its unique_id
    is the Bluetooth address), or None."""
    for cfg in hass.config_entries.async_entries(MOPEKA_DOMAIN):
        if str(cfg.unique_id or "").upper() == mac:
            return cfg
    return None


def medium_type(hass: HomeAssistant, mac: str) -> str | None:
    """The native entry's `medium_type`, or None when there is no entry."""
    cfg = native_entry(hass, mac)
    if cfg is None:
        return None
    return str(cfg.data.get("medium_type") or _DEFAULT_MEDIUM)


def native_model(hass: HomeAssistant, mac: str) -> str | None:
    """The `mopeka_iot_ble` model HA recorded on the sensor's device (e.g.
    "M1017", "TD40/TD200"), or None until its first advert is decoded."""
    cfg = native_entry(hass, mac)
    if cfg is None:
        return None
    try:
        from homeassistant.helpers import device_registry as dr

        devices = dr.async_entries_for_config_entry(dr.async_get(hass), cfg.entry_id)
    except Exception:  # noqa: BLE001
        return None
    for device in devices:
        if device.model:
            return str(device.model)
    return None


def is_top_down(hass: HomeAssistant, mac: str) -> bool:
    """Whether the sensor mounts on top of the tank and reads the air gap."""
    return native_model(hass, mac) in MOPEKA_TOP_DOWN_MODELS


def contents(
    hass: HomeAssistant, entry: ConfigEntry | None, device_id: str, mac: str,
) -> str | None:
    """What the tank holds (a MOPEKA_CONTENTS key), or None when unknown. The
    native medium for a bottom-up sensor; our own ledger for a top-down one,
    whose native medium is always air."""
    if not is_top_down(hass, mac):
        return medium_type(hass, mac)
    ledger = (entry.options.get(MOPEKA_CONTENTS_OPTION) or {}) if entry else {}
    value = ledger.get(device_id) if isinstance(ledger, dict) else None
    return value if value in MOPEKA_CONTENTS else None


def ensure_top_down_medium(hass: HomeAssistant, mac: str) -> None:
    """Set the native medium to air on a top-down sensor, the only medium its
    reading converts correctly with. A no-op once set, or for any other model."""
    if not is_top_down(hass, mac):
        return
    cfg = native_entry(hass, mac)
    if cfg is None or cfg.data.get("medium_type") == MEDIUM_AIR:
        return
    _LOGGER.info("Mopeka %s is a top-down sensor: setting its medium to air", mac)
    hass.config_entries.async_update_entry(
        cfg, data={**cfg.data, "medium_type": MEDIUM_AIR},
    )


def fluid_type(
    hass: HomeAssistant, mac: str, entry: ConfigEntry | None = None, device_id: str = "",
) -> str | None:
    """The Home-dial fluid label for a Mopeka, from what its tank holds, or
    None when that is unknown or has no dial."""
    return MEDIUM_TO_FLUID.get(contents(hass, entry, device_id, mac) or "")


def role_map(hass: HomeAssistant, entry_id: str, device: dict[str, Any]) -> dict[str, str]:
    """A Mopeka's `service_entities`: `level` is Sensa's % sensor (what the
    Home dials read), the rest are the native entities."""
    from homeassistant.helpers import entity_registry as er

    mac = device_mac(device)
    device_id = str(device.get("device_id") or "")
    roles: dict[str, str] = {}
    level = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}_{device_id}{MOPEKA_LEVEL_UID_SUFFIX}",
    )
    if level:
        roles["level"] = level
    for role, key in (
        ("distance", "tank_level"),
        ("temperature", "temperature"),
        ("battery", "battery"),
        ("reading_quality", "reading_quality"),
    ):
        eid = native_entity(hass, mac, key)
        if eid:
            roles[role] = eid
    return roles


def bottle_select_entity_id(device_id: str) -> str:
    return f"select.sensa_{_relay_device_id_safe(device_id)}_bottle_size"


def full_button_entity_id(device_id: str) -> str:
    return f"button.sensa_{_relay_device_id_safe(device_id)}_tank_full"


def height_number_entity_id(device_id: str) -> str:
    return f"number.sensa_{_relay_device_id_safe(device_id)}_tank_height"


def capacity_number_entity_id(device_id: str) -> str:
    return f"number.sensa_{_relay_device_id_safe(device_id)}_tank_capacity"


def contents_select_entity_id(device_id: str) -> str:
    return f"select.sensa_{_relay_device_id_safe(device_id)}_contents"


def level_sensor_entity_id(device_id: str) -> str:
    return f"sensor.sensa_{_relay_device_id_safe(device_id)}_tank_level"


async def _async_refresh_device_row(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rebuild the Discovery picker's rows now. Which fields a Mopeka's row
    shows (Tank Height only for a tank shape) is decided when the row is
    built, so without this the slider only appeared on the next full poll."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    discovery_sensor = runtime.get("discovery_sensor")
    if discovery_sensor is None:
        return
    try:
        await discovery_sensor.async_quick_refresh()
    except Exception:  # the setting is already saved
        _LOGGER.debug("Mopeka: device row refresh failed", exc_info=True)


def _hub_device_info(entry: ConfigEntry) -> dict[str, Any]:
    # The Mopeka's HA device belongs to the native integration, so these sit on
    # the Sensa Nomad hub device like the other per-device config selects.
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": "Sensa Nomad",
        "manufacturer": "Sensa",
        "model": "Nomad Integration",
    }


class SensaMopekaBottleSelect(SelectEntity):
    """Tank Shape: a flat-sided tank, a round tank on its side, or an LPG
    bottle size. Options-as-truth on the entry.

    No "Not set" option: an unchosen shape has no current option, so the
    state reads `unknown` and the picker's required-field check lights the
    row's "Not Configured" pill until the customer picks one."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:propane-tank"

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str, mac: str,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._mac = mac
        self._attr_name = f"{device_name} Tank Shape"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{MOPEKA_BOTTLE_UID_SUFFIX}"
        self.entity_id = bottle_select_entity_id(device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        return _hub_device_info(self._entry)

    @property
    def options(self) -> list[str]:
        return shape_options(
            contents(self.hass, self._entry, self._device_id, self._mac),
            is_top_down(self.hass, self._mac),
        )

    @property
    def current_option(self) -> str | None:
        return shape_for(self.hass, self._entry, self._device_id, self._mac)

    @callback
    def _on_contents(self, _event: Event) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        # Contents changes the choices, so follow it.
        self.async_on_remove(async_track_state_change_event(
            self.hass, [contents_select_entity_id(self._device_id)], self._on_contents,
        ))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"labels": {
            **MOPEKA_SHAPES, **{k: v[0] for k, v in MOPEKA_BOTTLES.items()},
        }}

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            _LOGGER.warning("Mopeka tank shape: ignoring unknown option %r", option)
            return
        ledger = dict(self._entry.options.get(MOPEKA_BOTTLE_OPTION) or {})
        ledger[self._device_id] = option
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, MOPEKA_BOTTLE_OPTION: ledger},
        )
        self.async_write_ha_state()
        await _async_refresh_device_row(self.hass, self._entry)


class SensaMopekaContentsSelect(SelectEntity):
    """What the tank holds: the native entry's `medium_type`, read and written
    there. A medium outside MOPEKA_CONTENTS (set in HA directly) reads as no
    current option rather than being mislabelled. On a top-down sensor the
    native medium stays air and the liquid is kept on our own entry."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:storage-tank"

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str, mac: str,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._mac = mac
        self._attr_name = f"{device_name} Contents"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{MOPEKA_CONTENTS_UID_SUFFIX}"
        self.entity_id = contents_select_entity_id(device_id)
        self._attr_options = list(MOPEKA_CONTENTS)

    @property
    def device_info(self) -> dict[str, Any]:
        return _hub_device_info(self._entry)

    @property
    def current_option(self) -> str | None:
        medium = contents(self.hass, self._entry, self._device_id, self._mac)
        return medium if medium in MOPEKA_CONTENTS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"labels": dict(MOPEKA_CONTENTS)}

    async def async_select_option(self, option: str) -> None:
        if option not in MOPEKA_CONTENTS:
            _LOGGER.warning("Mopeka contents: ignoring unknown option %r", option)
            return
        cfg = native_entry(self.hass, self._mac)
        if cfg is None:
            _LOGGER.warning("Mopeka contents: no mopeka entry for %s", self._mac)
            return
        if is_top_down(self.hass, self._mac):
            # The reading converts with air whatever the tank holds.
            ledger = dict(self._entry.options.get(MOPEKA_CONTENTS_OPTION) or {})
            ledger[self._device_id] = option
            self.hass.config_entries.async_update_entry(
                self._entry,
                options={**self._entry.options, MOPEKA_CONTENTS_OPTION: ledger},
            )
            ensure_top_down_medium(self.hass, self._mac)
        else:
            # The native integration's update listener reloads the entry, so
            # the next reading is already decoded for the new medium.
            self.hass.config_entries.async_update_entry(
                cfg, data={**cfg.data, "medium_type": option},
            )
        self.async_write_ha_state()
        # The Home dial's label comes from the medium, read at dashboard build.
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        from .const import SIGNAL_DASHBOARD_REGEN_REQUESTED

        async_dispatcher_send(self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED)
        # A new Contents changes which shapes fit, and so the row's fields.
        await _async_refresh_device_row(self.hass, self._entry)


class SensaMopekaHeightNumber(NumberEntity):
    """Tank Height in mm: the inside height of a flat-sided tank, or the
    diameter of a round one. Options-as-truth on the entry; unset reads
    unknown, which keeps the row "Not Configured" while the shape needs it."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "mm"
    _attr_native_min_value = MOPEKA_HEIGHT_MIN
    _attr_native_max_value = MOPEKA_HEIGHT_MAX
    _attr_native_step = MOPEKA_HEIGHT_STEP

    def __init__(self, entry: ConfigEntry, device_id: str, device_name: str) -> None:
        self._entry = entry
        self._device_id = device_id
        self._attr_name = f"{device_name} Tank Height"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{MOPEKA_HEIGHT_UID_SUFFIX}"
        self.entity_id = height_number_entity_id(device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        return _hub_device_info(self._entry)

    @property
    def native_value(self) -> float | None:
        return height_for(self._entry, self._device_id)

    async def async_set_native_value(self, value: float) -> None:
        value = max(MOPEKA_HEIGHT_MIN, min(MOPEKA_HEIGHT_MAX, float(value)))
        ledger = dict(self._entry.options.get(MOPEKA_HEIGHT_OPTION) or {})
        ledger[self._device_id] = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, MOPEKA_HEIGHT_OPTION: ledger},
        )
        self.async_write_ha_state()


class SensaMopekaCapacityNumber(NumberEntity):
    """Tank Capacity in litres, for a diesel tank: the heaters' Diesel Days
    Left needs litres, and the sensor only knows the height. Options-as-truth
    on the entry; unset reads unknown, and Days Left says to set it."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:barrel"
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "L"
    _attr_native_min_value = MOPEKA_CAPACITY_MIN
    _attr_native_max_value = MOPEKA_CAPACITY_MAX
    _attr_native_step = MOPEKA_CAPACITY_STEP

    def __init__(self, entry: ConfigEntry, device_id: str, device_name: str) -> None:
        self._entry = entry
        self._device_id = device_id
        self._attr_name = f"{device_name} Tank Capacity"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{MOPEKA_CAPACITY_UID_SUFFIX}"
        self.entity_id = capacity_number_entity_id(device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        return _hub_device_info(self._entry)

    @property
    def native_value(self) -> float | None:
        return capacity_for(self._entry, self._device_id)

    async def async_set_native_value(self, value: float) -> None:
        value = max(MOPEKA_CAPACITY_MIN, min(MOPEKA_CAPACITY_MAX, float(value)))
        ledger = dict(self._entry.options.get(MOPEKA_CAPACITY_OPTION) or {})
        ledger[self._device_id] = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, MOPEKA_CAPACITY_OPTION: ledger},
        )
        self.async_write_ha_state()


class SensaMopekaFullButton(ButtonEntity):
    """"Tank is full now": saves this tank's reading over its Tank Height as
    the correction for its Contents, used by every Mopeka holding that liquid.
    Refuses, with the reason, when it cannot be right."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:gauge-full"

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str, mac: str,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._mac = mac
        self._attr_name = f"{device_name} Tank is Full"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{MOPEKA_FULL_UID_SUFFIX}"
        self.entity_id = full_button_entity_id(device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        return _hub_device_info(self._entry)

    async def async_press(self) -> None:
        if is_top_down(self.hass, self._mac):
            raise HomeAssistantError(
                "This sensor reads from the top of the tank and needs no "
                "calibration: set the Tank Height."
            )
        medium = medium_type(self.hass, self._mac)
        shape = shape_for(self.hass, self._entry, self._device_id, self._mac)
        height = height_for(self._entry, self._device_id)
        if medium is None or not needs_height(shape) or not height:
            raise HomeAssistantError("Set the Tank Shape and Tank Height first.")
        eid = native_entity(self.hass, self._mac, "tank_level")
        st = self.hass.states.get(eid) if eid else None
        try:
            reading = float(st.state) if st is not None else None
        except (TypeError, ValueError):
            reading = None
        if not reading:
            raise HomeAssistantError("The sensor has no reading yet. Try again in a minute.")
        ratio = reading / height
        if not MOPEKA_CAL_MIN <= ratio <= MOPEKA_CAL_MAX:
            raise HomeAssistantError(
                f"The sensor reads {reading:.0f} mm against a {height:.0f} mm tank. "
                "Check the tank is full and its height is right."
            )
        ledger = dict(self._entry.options.get(MOPEKA_CAL_OPTION) or {})
        ledger[medium] = round(ratio, 4)
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, MOPEKA_CAL_OPTION: ledger},
        )
        _LOGGER.info(
            "Mopeka %s calibrated full: %s mm over %s mm, %s correction %s",
            self._mac, reading, height, medium, ledger[medium],
        )
        async_dispatcher_send(self.hass, SIGNAL_MOPEKA_CALIBRATED)


class SensaMopekaLevelSensor(SensorEntity):
    """A Mopeka's fill level in %, from the native millimetres and the chosen
    bottle. Unavailable while either is unknown, never a guessed 0 %."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:propane-tank"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str, mac: str,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._mac = mac
        self._attr_name = f"{device_name} Tank Level"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}{MOPEKA_LEVEL_UID_SUFFIX}"
        self.entity_id = level_sensor_entity_id(device_id)
        self._distance_eid: str | None = None
        self._unsub_state: Any = None
        self._was_available: bool | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        return _hub_device_info(self._entry)

    def _distance_mm(self) -> float | None:
        if not self._distance_eid:
            return None
        st = self.hass.states.get(self._distance_eid)
        if st is None:
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None  # unavailable / unknown: not a reading

    @property
    def native_value(self) -> float | None:
        return level_percent(
            self._distance_mm(),
            shape_for(self.hass, self._entry, self._device_id, self._mac),
            height_for(self._entry, self._device_id),
            calibration_for(self._entry, medium_type(self.hass, self._mac)),
            is_top_down(self.hass, self._mac),
        )

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "shape": shape_for(self.hass, self._entry, self._device_id, self._mac),
            "height_mm": height_for(self._entry, self._device_id),
            "calibration": calibration_for(self._entry, medium_type(self.hass, self._mac)),
            "distance_mm": self._distance_mm(),
            "top_down": is_top_down(self.hass, self._mac),
            "fluid_type": fluid_type(self.hass, self._mac, self._entry, self._device_id),
        }

    @callback
    def _subscribe(self) -> None:
        """(Re)track the native distance entity and our shape and height. The
        native entity registers on the sensor's first advert, which can be
        after we are added, so a registry create re-runs this."""
        distance = native_entity(self.hass, self._mac, "tank_level")
        if self._unsub_state is not None and distance == self._distance_eid:
            return
        if self._unsub_state is not None:
            self._unsub_state()
        self._distance_eid = distance
        watched = [
            contents_select_entity_id(self._device_id),
            bottle_select_entity_id(self._device_id),
            height_number_entity_id(self._device_id),
        ]
        if distance:
            watched.append(distance)
        self._unsub_state = async_track_state_change_event(
            self.hass, watched, self._on_change,
        )
        self.async_write_ha_state()
        self._rebuild_on_availability_edge()

    @callback
    def _on_change(self, _event: Event) -> None:
        # The model is only known once the first advert is decoded, which is
        # also when the distance first changes.
        ensure_top_down_medium(self.hass, self._mac)
        self.async_write_ha_state()
        self._rebuild_on_availability_edge()

    @callback
    def _rebuild_on_availability_edge(self) -> None:
        """Ask for a dashboard rebuild when the level turns available or
        unavailable. The Home dial is only built for a live level, and the
        level only turns live after the first reading and once the tank is
        set up, both of which can land after the dashboard was built."""
        available = self.available
        if available == self._was_available:
            return
        first = self._was_available is None
        self._was_available = available
        if first and not available:
            return  # nothing has changed for the dashboard yet
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        from .const import SIGNAL_DASHBOARD_REGEN_REQUESTED

        async_dispatcher_send(self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED)

    @callback
    def _on_calibrated(self) -> None:
        self._on_change(None)

    async def async_added_to_hass(self) -> None:
        self._subscribe()
        # A calibration on ANY tank of this Contents changes this level too.
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_MOPEKA_CALIBRATED, self._on_calibrated,
        ))

        @callback
        def _on_registry(event: Event) -> None:
            if event.data.get("action") == "create" and self._distance_eid is None:
                self._subscribe()

        self.async_on_remove(
            self.hass.bus.async_listen("entity_registry_updated", _on_registry)
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None


def _device_identity(device: dict[str, Any]) -> tuple[str, str] | None:
    if not is_mopeka(device):
        return None
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return None
    return device_id, str(device.get("display_name") or device.get("name") or "Mopeka")


def build_selects(
    entry: ConfigEntry, device: dict[str, Any],
) -> list[SelectEntity]:
    """The Contents and Tank Shape selects for a Mopeka row, else []."""
    ident = _device_identity(device)
    if ident is None:
        return []
    mac = device_mac(device)
    return [
        SensaMopekaContentsSelect(entry, *ident, mac),
        SensaMopekaBottleSelect(entry, *ident, mac),
    ]


def build_numbers(
    entry: ConfigEntry, device: dict[str, Any],
) -> list[NumberEntity]:
    """The Tank Height and Tank Capacity numbers for a Mopeka row, else []."""
    ident = _device_identity(device)
    if ident is None:
        return []
    return [SensaMopekaHeightNumber(entry, *ident), SensaMopekaCapacityNumber(entry, *ident)]


def build_buttons(
    entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaMopekaFullButton]:
    """The "Tank is full now" button for a Mopeka row, else []."""
    ident = _device_identity(device)
    if ident is None:
        return []
    return [SensaMopekaFullButton(entry, *ident, device_mac(device))]


def build_level_sensors(
    entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaMopekaLevelSensor]:
    """The % Tank Level sensor for a Mopeka row, else an empty list."""
    ident = _device_identity(device)
    if ident is None:
        return []
    return [SensaMopekaLevelSensor(entry, *ident, device_mac(device))]
