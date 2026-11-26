"""Number platform for Sensa Nomad — configurable thresholds."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CLIMATE_CONTROL_ALARM_DRIFT_ENTITY_ID,
    CLIMATE_CONTROL_ALARM_MINUTES_ENTITY_ID,
    CLIMATE_CONTROL_LOW_FUEL_ENTITY_ID,
    CRYPTO_AUTOPILOT_NUMBERS,
    DOMAIN,
    LIGHT_POWER_ON_DELAY_DEFAULT_S,
    LIGHT_POWER_ON_DELAY_MAX_S,
    LIGHT_POWER_ON_DELAY_MIN_S,
    LIGHT_POWER_ON_DELAY_STEP_S,
    RADAR_SEARCH_RADIUS_DEFAULT_KM,
    RADAR_SEARCH_RADIUS_ENTITY_ID,
    RADAR_SEARCH_RADIUS_MAX_KM,
    RADAR_SEARCH_RADIUS_MIN_KM,
    crypto_autopilot_number_entity_id,
    light_power_on_delay_number_entity_id,
    light_power_on_delay_unique_id,
    teltonika_device_name,
    van_level_geometry_entity_id,
    van_level_offset_entity_id,
    vehicle_profile_entity_id,
)

_LOGGER = logging.getLogger(__name__)


# Singleton vehicle-geometry numbers. One vehicle = one set of values
# regardless of how many levelling sensors are installed, so these live
# under the Sensa Nomad hub rather than per-device.
_VEHICLE_GEOMETRY_NUMBERS: tuple[dict[str, Any], ...] = (
    {
        "key": "wheel_track_cm",
        "name": "Wheel Track",
        "icon": "mdi:axis-arrow",
        "min_value": 100.0, "max_value": 300.0, "step": 1.0, "initial": 170.0,
        "unit": "cm",
    },
    {
        "key": "wheelbase_cm",
        "name": "Wheelbase",
        "icon": "mdi:car-side",
        "min_value": 200.0, "max_value": 600.0, "step": 1.0, "initial": 360.0,
        "unit": "cm",
    },
    {
        "key": "shim_tolerance_cm",
        "name": "Shim Tolerance",
        "icon": "mdi:tape-measure",
        "min_value": 0.0, "max_value": 10.0, "step": 0.5, "initial": 1.0,
        "unit": "cm",
    },
    # Side-roll angle at which the Cab roll card turns red. A driving limit,
    # not a parking one: overlanders on off-camber tracks read it as "this
    # close to going over". Caution (amber) starts at 80% of it, in the card.
    {
        "key": "max_roll_deg",
        "name": "Maximum Roll Angle",
        "icon": "mdi:angle-acute",
        "min_value": 5.0, "max_value": 45.0, "step": 1.0, "initial": 25.0,
        "unit": "°",
    },
)


# The van's own size and what it will climb, for the Radar map: vehicle-aware
# routing, low-bridge / weight-limit warnings and whether a car park will take
# it. Mirrors Radar's own vehicle profile (`web/src/lib/vehicleProfile.ts`
# LIMITS) so the two express the same thing; the keys ARE Radar's profile keys
# and go over the embed untranslated.
#
# **0 MEANS NOT SET**, and that is why every minimum is 0 rather than Radar's
# own floor. Radar treats a missing dimension as permissive (it cannot warn
# about a bridge if it does not know the height), and an HA number always holds
# a value, so 0 is how "I have not told you" is expressed. `sanitise` on the
# Radar side already drops anything <= 0, so the two agree with no translation.
_VEHICLE_PROFILE_NUMBERS: tuple[dict[str, Any], ...] = (
    {
        "key": "height_m",
        "name": "Vehicle Height",
        "icon": "mdi:arrow-expand-vertical",
        "min_value": 0.0, "max_value": 5.0, "step": 0.05, "initial": 0.0,
        "unit": "m",
    },
    {
        "key": "width_m",
        "name": "Vehicle Width",
        "icon": "mdi:arrow-expand-horizontal",
        "min_value": 0.0, "max_value": 3.0, "step": 0.05, "initial": 0.0,
        "unit": "m",
    },
    {
        "key": "length_m",
        "name": "Vehicle Length",
        "icon": "mdi:arrow-left-right",
        "min_value": 0.0, "max_value": 20.0, "step": 0.1, "initial": 0.0,
        "unit": "m",
    },
    {
        "key": "weight_t",
        "name": "Vehicle Weight",
        "icon": "mdi:weight",
        "min_value": 0.0, "max_value": 50.0, "step": 0.1, "initial": 0.0,
        "unit": "t",
    },
    {
        "key": "max_grade_pct",
        "name": "Max Gradient",
        "icon": "mdi:angle-acute",
        "min_value": 0.0, "max_value": 45.0, "step": 1.0, "initial": 0.0,
        "unit": "%",
    },
)


def _is_levelling_device(device: dict[str, Any]) -> bool:
    """A `levelling`-service-class device per the device catalog."""
    return (
        device.get("service_class") == "levelling"
        or device.get("vendor_device_type") == "van_level"
    )


def build_levelling_number_entities(
    entry: ConfigEntry, device: dict[str, Any]
) -> list[NumberEntity]:
    """Per-device pitch/roll offset numbers for one van-level device.

    Exposed so the discovery picker's add-dispatch can create these for a
    levelling sensor registered after initial setup (mirrors the relay
    builder pattern).
    """
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    display = device.get("display_name") or device.get("name") or "Van Level"
    return [
        SensaLevellingOffsetNumber(entry, device_id, display, axis="pitch"),
        SensaLevellingOffsetNumber(entry, device_id, display, axis="roll"),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad number entities."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    # Late-add callback so a levelling device registered via the Discovery
    # picker gets its offset numbers without an integration reload.
    runtime["vendor_add_numbers"] = async_add_entities

    # Drain anything a vendor coordinator queued before this platform set up.
    # Without it `_add_or_queue_vendor_entities(runtime, "numbers", ...)`
    # stashes into `vendor_pending_numbers` and NOTHING ever reads it back --
    # the silent-drop the Cerbo aggregates hit, and the reason the Starlink
    # dish config numbers can be created from the vendor start path at all.
    pending_numbers = runtime.pop("vendor_pending_numbers", None)
    if pending_numbers:
        async_add_entities(pending_numbers)

    devices = runtime.get("coordinator").data or {}
    device_list = devices.get("devices", [])

    entities: list[NumberEntity] = []

    # Hub-level singleton vehicle-geometry numbers — always present.
    for spec in _VEHICLE_GEOMETRY_NUMBERS:
        entities.append(SensaVehicleGeometryNumber(entry, **spec))

    # The van's size + max gradient for the Radar map. Always present, and
    # deliberately NOT gated on entitlement: a customer must be able to record
    # their vehicle whether or not the routing that uses it is included today,
    # so that renewing lights it up rather than sending them back to a form.
    for spec in _VEHICLE_PROFILE_NUMBERS:
        entities.append(SensaVehicleProfileNumber(entry, **spec))

    # Radar nearest-amenity search radius, always present. Harmless with Radar
    # unconfigured (the coordinator simply never asks), and being unconditional
    # keeps the entity_id stable so the Nearby card's slider never dangles.
    entities.append(SensaRadarRadiusNumber(entry))
    entities.append(SensaSolarArrayRatingNumber(entry))
    entities.append(SensaBatteryCapacityNumber(entry))

    # Sensa Climate Control's target, always present so the card's dial never
    # dangles; whether the card shows is decided by the dashboard.
    entities.append(SensaClimateControlTargetNumber(entry))
    entities.append(SensaClimateControlAcHeatSocNumber(entry))
    entities.append(SensaClimateControlHeaterRestartNumber(entry))
    entities.append(SensaClimateControlAlarmMinutesNumber(entry))
    entities.append(SensaClimateControlAlarmDriftNumber(entry))
    entities.append(SensaClimateControlLowFuelNumber(entry))

    # Miner autopilot thresholds, always present so the Crypto card's
    # Controls panel never binds to a dangling id (a miner can be added later).
    for key in CRYPTO_AUTOPILOT_NUMBERS:
        entities.append(SensaCryptoAutopilotNumber(entry, key))

    # How far back the Log / System Log keep history, in days (0 = keep
    # everything). Always present: it governs a store every flavour has.
    entities.append(SensaLogRetentionNumber(hass, entry))

    # Remote-support access window — paired with the consent switch, so only
    # where the `sensa_support` account is provisioned (appliance images).
    from . import remote_support
    if await remote_support.async_support_user_exists(hass):
        entities.append(SensaRemoteSupportWindowNumber(entry))

    # Z2M vibration sensors: the sensitivity control on their Devices row.
    entities.extend(
        _collect_vibration_sensitivity_numbers(hass, entry, device_list)
    )

    for device in device_list:
        if device.get("integration_type") == "sensa_teltonika_api":
            device_id = device.get("device_id", "unknown")
            mac = (
                device.get("linked_mac_address")
                or device.get("mac_address")
                or ""
            )
            entities.append(
                SensaConfigNumber(
                    entry,
                    device_id=device_id,
                    mac=mac,
                    key="signal_threshold",
                    name="Signal Threshold",
                    icon="mdi:signal-cellular-2",
                    min_value=-120.0,
                    max_value=0.0,
                    step=1.0,
                    initial=-90.0,
                    unit="dBm",
                )
            )
            from .const import (
                TELTONIKA_GPS_REFRESH_DEFAULT,
                TELTONIKA_GPS_REFRESH_MAX,
                TELTONIKA_GPS_REFRESH_MIN,
            )
            entities.append(
                SensaConfigNumber(
                    entry,
                    device_id=device_id,
                    mac=mac,
                    key="gps_refresh",
                    name="GPS Refresh",
                    icon="mdi:crosshairs-gps",
                    min_value=float(TELTONIKA_GPS_REFRESH_MIN),
                    max_value=float(TELTONIKA_GPS_REFRESH_MAX),
                    step=1.0,
                    initial=float(TELTONIKA_GPS_REFRESH_DEFAULT),
                    unit="s",
                )
            )
        if _is_levelling_device(device):
            entities.extend(build_levelling_number_entities(entry, device))

    # Z2M lighting controllers: the Power-On Delay on their Devices row, so a
    # customer whose relay-fed controller is slow to boot can give it longer.
    entities.extend(build_light_power_on_delay_numbers(hass, entry))

    # Home-layout sort-order numbers, one per eligible device per surface.
    # Collected the same way select.py collects the placement selects (live
    # devices + a registry backstop + a ledger backstop), so a device that has
    # an order set but is momentarily missing from the coordinator keeps its
    # entity instead of going unavailable and losing the value.
    entities.extend(_collect_home_order_numbers(hass, entry, device_list))

    # Sensa Fan Temperature Control autopilot numbers. A controller already in the
    # autopilot ledger keeps them when the backend snapshot is empty on restart.
    from .const import FAN_AUTOPILOT_OPTION

    fan_ids: set[str] = set()
    for device in device_list:
        built = build_fan_autopilot_numbers(entry, device)
        if built:
            fan_ids.add(built[0]._device_id)
            entities.extend(built)
    for device_id in entry.options.get(FAN_AUTOPILOT_OPTION) or {}:
        if device_id and device_id not in fan_ids:
            entities.extend(fan_autopilot_numbers_for(entry, device_id, "Fan Temperature Control"))

    # "Tank Height" for every Mopeka tank sensor (`mopeka.py`).
    from .mopeka import build_numbers as build_mopeka_numbers

    for device in device_list:
        entities.extend(build_mopeka_numbers(entry, device))

    if entities:
        async_add_entities(entities)


def build_fan_autopilot_numbers(
    entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaFanAutopilotNumber]:
    """A Sensa Fan Temperature Control's autopilot numbers (Start, Max and Critical per
    fan, then the device's Night Cap), or an empty list for any other device."""
    from .fan_autopilot import is_fan_controller

    if not is_fan_controller(device):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    return fan_autopilot_numbers_for(
        entry, device_id, device.get("display_name") or device.get("name") or "Fan Temperature Control",
    )


def fan_autopilot_numbers_for(
    entry: ConfigEntry, device_id: str, device_name: str,
) -> list[SensaFanAutopilotNumber]:
    from .const import FAN_CHANNELS, FAN_TEMP_NUMBERS, fan_autopilot_key

    numbers = [
        SensaFanAutopilotNumber(
            entry, device_id, device_name, fan_autopilot_key(ch, key), f"Fan {ch} {label}",
        )
        for ch in FAN_CHANNELS
        for key, (label, *_rest) in FAN_TEMP_NUMBERS.items()
    ]
    numbers.append(
        SensaFanAutopilotNumber(entry, device_id, device_name, "night_cap", "Night Cap"),
    )
    return numbers


class SensaFanAutopilotNumber(NumberEntity):
    """One Sensa Fan Temperature Control autopilot setting: a fan's Start, Max or
    Critical temperature (degC, shown in the customer's unit), or the device's
    Night Cap (%). Options-as-truth on the entry (`FAN_AUTOPILOT_OPTION`), so
    not a RestoreEntity; an unset value reads its default, never unknown, so
    the row does not show "Not Configured".
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        ledger_key: str,
        label: str,
    ) -> None:
        from homeassistant.const import PERCENTAGE, UnitOfTemperature

        from .const import FAN_TEMP_NUMBERS, fan_autopilot_number_entity_id

        self._entry = entry
        self._device_id = device_id
        self._key = ledger_key
        self._attr_name = f"{device_name} {label}"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{ledger_key}"
        self.entity_id = fan_autopilot_number_entity_id(device_id, ledger_key)
        if ledger_key == "night_cap":
            self._attr_icon = "mdi:weather-night"
            self._attr_mode = NumberMode.SLIDER
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_native_min_value = 0
            self._attr_native_max_value = 100
            self._attr_native_step = 5
        else:
            _label, lo, hi, step, _default = FAN_TEMP_NUMBERS[ledger_key.split("_", 2)[-1]]
            self._attr_icon = "mdi:thermometer"
            self._attr_mode = NumberMode.BOX
            self._attr_device_class = NumberDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_native_min_value = lo
            self._attr_native_max_value = hi
            self._attr_native_step = step

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> float:
        from .fan_autopilot import setting

        return float(setting(self._entry, self._device_id, self._key))

    async def async_set_native_value(self, value: float) -> None:
        from .const import FAN_AUTOPILOT_OPTION
        from .fan_autopilot import async_drive_fans

        value = max(self.native_min_value, min(self.native_max_value, float(value)))
        ledger = dict(self._entry.options.get(FAN_AUTOPILOT_OPTION) or {})
        ledger[self._device_id] = {**(ledger.get(self._device_id) or {}), self._key: value}
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, FAN_AUTOPILOT_OPTION: ledger},
        )
        self.async_write_ha_state()
        self.hass.async_create_task(async_drive_fans(self.hass, self._entry))


class SensaClimateControlTargetNumber(NumberEntity):
    """Sensa Climate Control's target temperature: the dial on its Climate-tab
    card. degC on the entry (`CLIMATE_CONTROL_OPTION`), shown in the
    customer's unit by the temperature device class."""

    _attr_has_entity_name = False
    _attr_name = "Climate Control Target"
    _attr_icon = "mdi:thermometer"
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.TEMPERATURE

    def __init__(self, entry: ConfigEntry) -> None:
        from homeassistant.const import UnitOfTemperature

        from .const import (
            CLIMATE_CONTROL_TARGET_ENTITY_ID,
            CLIMATE_CONTROL_TARGET_RANGE,
        )

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_climate_control_target"
        self.entity_id = CLIMATE_CONTROL_TARGET_ENTITY_ID
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        lo, hi, step = CLIMATE_CONTROL_TARGET_RANGE
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step

    async def async_added_to_hass(self) -> None:
        from homeassistant.helpers.dispatcher import async_dispatcher_connect

        from .climate_control import SIGNAL_CLIMATE_CONTROL

        # Re-read the lowest when the nominated air conditioner changes.
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_CLIMATE_CONTROL, self.async_write_ha_state,
        ))

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_min_value(self) -> float:
        """The nominated air conditioner's lowest setpoint, else the default."""
        if self.hass is None:
            return self._attr_native_min_value
        from .climate_control import target_min

        return target_min(self.hass, self._entry)

    @property
    def native_value(self) -> float:
        from .climate_control import setting

        # A target saved below a newly chosen air conditioner's lowest reads
        # as that lowest, which is also what the controller holds.
        return max(float(setting(self._entry, "target")), self.native_min_value)

    async def async_set_native_value(self, value: float) -> None:
        from .climate_control import async_drive, set_setting

        value = max(self.native_min_value, min(self.native_max_value, float(value)))
        set_setting(self.hass, self._entry, "target", value)
        self.async_write_ha_state()
        await async_drive(self.hass, self._entry)


class SensaClimateControlHeaterRestartNumber(NumberEntity):
    """How far under Climate Control's target a diesel heater relights, in
    degC. A difference, so deliberately no temperature device class: HA would
    convert it as if it were a reading (2 degC is not 35.6 degF). On the entry
    (`CLIMATE_CONTROL_OPTION`, key `heater_restart`)."""

    _attr_has_entity_name = False
    _attr_name = "Climate Control Diesel Heater Restarts"
    _attr_icon = "mdi:fire"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0.5
    _attr_native_max_value = 5
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "°C"

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import CLIMATE_CONTROL_HEATER_RESTART_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_climate_control_heater_restart"
        self.entity_id = CLIMATE_CONTROL_HEATER_RESTART_ENTITY_ID

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> float:
        from .climate_control import setting

        return float(setting(self._entry, "heater_restart"))

    async def async_set_native_value(self, value: float) -> None:
        from .climate_control import async_drive, set_setting

        value = max(self.native_min_value, min(self.native_max_value, float(value)))
        set_setting(self.hass, self._entry, "heater_restart", value)
        self.async_write_ha_state()
        await async_drive(self.hass, self._entry)


class _ClimateControlAlarmNumber(NumberEntity):
    """One of the cannot-reach alarm's two settings, on the entry
    (`CLIMATE_CONTROL_OPTION`, key `_key`)."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _key = ""
    _entity_id = ""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_climate_control_{self._key}"
        self.entity_id = self._entity_id

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> float:
        from .climate_control import setting

        return float(setting(self._entry, self._key))

    async def async_set_native_value(self, value: float) -> None:
        from .climate_control import async_drive, set_setting

        value = max(self.native_min_value, min(self.native_max_value, float(value)))
        set_setting(self.hass, self._entry, self._key, value)
        self.async_write_ha_state()
        await async_drive(self.hass, self._entry)


class SensaClimateControlAlarmMinutesNumber(_ClimateControlAlarmNumber):
    """Minutes without getting closer to the target before the alarm."""

    _attr_name = "Climate Control Alarm Minutes"
    _attr_icon = "mdi:timer-alert-outline"
    _attr_native_min_value = 5
    _attr_native_max_value = 240
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"
    _key = "alarm_minutes"
    _entity_id = CLIMATE_CONTROL_ALARM_MINUTES_ENTITY_ID


class SensaClimateControlAlarmDriftNumber(_ClimateControlAlarmNumber):
    """How far (degC) further from the target than when it began before the
    alarm sounds at once. A difference, so no temperature device class."""

    _attr_name = "Climate Control Alarm Drift"
    _attr_icon = "mdi:thermometer-alert"
    _attr_native_min_value = 0.5
    _attr_native_max_value = 10
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "°C"
    _key = "alarm_drift"
    _entity_id = CLIMATE_CONTROL_ALARM_DRIFT_ENTITY_ID


class SensaClimateControlLowFuelNumber(_ClimateControlAlarmNumber):
    """The diesel tank % below which a heater is not used and the low-fuel
    alarm is raised."""

    _attr_name = "Climate Control Low Diesel"
    _attr_icon = "mdi:gas-station-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 50
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _key = "low_fuel"
    _entity_id = CLIMATE_CONTROL_LOW_FUEL_ENTITY_ID


class SensaClimateControlAcHeatSocNumber(NumberEntity):
    """The battery % at or above which Climate Control heats with an air
    conditioner off shore power; 80 by default, 0 = only on shore power. On the entry
    (`CLIMATE_CONTROL_OPTION`, key `ac_heat_soc`)."""

    _attr_has_entity_name = False
    _attr_name = "Climate Control Air Con Heats Above"
    _attr_icon = "mdi:battery-charging-high"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5

    def __init__(self, entry: ConfigEntry) -> None:
        from homeassistant.const import PERCENTAGE

        from .const import CLIMATE_CONTROL_AC_HEAT_SOC_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_climate_control_ac_heat_soc"
        self.entity_id = CLIMATE_CONTROL_AC_HEAT_SOC_ENTITY_ID
        self._attr_native_unit_of_measurement = PERCENTAGE

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> float:
        from .climate_control import setting

        return float(setting(self._entry, "ac_heat_soc"))

    async def async_set_native_value(self, value: float) -> None:
        from .climate_control import async_drive, set_setting

        value = max(0.0, min(100.0, float(value)))
        set_setting(self.hass, self._entry, "ac_heat_soc", value)
        self.async_write_ha_state()
        await async_drive(self.hass, self._entry)


def _collect_home_order_numbers(
    hass: HomeAssistant, entry: ConfigEntry, device_list: list[dict[str, Any]],
) -> list[NumberEntity]:
    """Every device that should own Home-layout sort-order numbers.

    Mirrors `select._collect_home_placement_devices`: live coordinator devices
    first, then a REGISTRY backstop (an already-created order number names its
    device id in the unique_id) and a LEDGER backstop (a device with a stored
    order must keep its control), so the entity does not vanish on a cycle where
    the backend has not answered yet.
    """
    from .const import HOME_LAYOUT_SURFACES

    found: dict[str, dict[str, Any]] = {}
    for device in device_list:
        device_id = str(device.get("device_id") or device.get("id") or "")
        if device_id:
            found.setdefault(device_id, device)

    def _backstop(device_id: str) -> dict[str, Any]:
        return {"device_id": device_id}

    # LONGEST suffix first so a suffix extending another cannot be misclaimed.
    ordered = sorted(
        HOME_LAYOUT_SURFACES, key=lambda s: -len(s["order_uid_suffix"]),
    )
    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id or ent.domain != "number":
                continue
            uid = ent.unique_id or ""
            if not uid.startswith(prefix):
                continue
            for surface in ordered:
                suffix = surface["order_uid_suffix"]
                if uid.endswith(suffix):
                    did = uid[len(prefix):-len(suffix)]
                    if did:
                        found.setdefault(did, _backstop(did))
                    break
    except Exception:
        _LOGGER.warning(
            "Registry walk for Home-order device_ids failed", exc_info=True,
        )

    for surface in HOME_LAYOUT_SURFACES:
        for device_id in (entry.options.get(surface["order_option"]) or {}):
            if device_id and device_id not in found:
                found[device_id] = _backstop(device_id)

    entities: list[NumberEntity] = []
    for device in found.values():
        entities.extend(build_home_order_numbers(hass, entry, device))
    return entities


def build_home_order_numbers(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaHomeOrderNumber]:
    """The sort-order numbers (one per surface) for one device, or [] when the
    device's service class has no Home Layout control at all.

    Returns a list like the other `build_*` helpers so the Discovery picker's
    add-dispatch can create them for a freshly-registered device with no reload.
    """
    from .const import HOME_LAYOUT_SURFACES, home_layout_kind_for
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
        SensaHomeOrderNumber(hass, entry, device_id, device_name, kind, surface)
        for surface in HOME_LAYOUT_SURFACES
    ]


class SensaHomeOrderNumber(NumberEntity):
    """Per-device, per-surface Home-layout SORT ORDER.

    The placement select says WHICH column a device's compact card goes in; this
    says where it sits among the others already in that column. Lower first.

    Options-as-truth, exactly like the placement select: the surface's order
    ledger (`entry.options[surface["order_option"]]`) maps device id -> int, and
    an absent id reads `HOME_LAYOUT_ORDER_DEFAULT`. Writing rebuilds the
    dashboards so the card moves immediately. Surfaced on the Settings ->
    Devices row beside the placement dropdown.

    Deliberately NOT a RestoreEntity: the ledger IS the value, so restoring a
    stale state could disagree with what the dashboard builder reads.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        kind: dict[str, Any],
        surface: dict[str, str],
    ) -> None:
        from .const import (
            HOME_LAYOUT_ORDER_MAX,
            HOME_LAYOUT_ORDER_MIN,
            home_layout_order_number_entity_id,
        )

        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._surface = surface
        self._option = surface["order_option"]
        self._attr_icon = "mdi:sort-numeric-variant"
        self._attr_name = f"{device_name} {surface['label']} Home Order"
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}{surface['order_uid_suffix']}"
        )
        self._attr_native_min_value = HOME_LAYOUT_ORDER_MIN
        self._attr_native_max_value = HOME_LAYOUT_ORDER_MAX
        self.entity_id = home_layout_order_number_entity_id(
            device_id, surface["key"],
        )

    @property
    def native_value(self) -> float:
        from .const import home_layout_order_for

        return home_layout_order_for(
            self._entry.options, self._surface, self._device_id,
        )

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_set_native_value(self, value: float) -> None:
        from .const import HOME_LAYOUT_ORDER_MAX, HOME_LAYOUT_ORDER_MIN

        order = round(value)
        order = max(HOME_LAYOUT_ORDER_MIN, min(HOME_LAYOUT_ORDER_MAX, order))
        ledger = dict(self._entry.options.get(self._option) or {})
        ledger[self._device_id] = order
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, self._option: ledger},
        )
        # An options write alone changes nothing on screen -- there is no entry
        # update listener, so the dashboards keep whatever order the LAST
        # rebuild baked in. The placement select rebuilds explicitly for the
        # same reason; the order number must too, or setting an order silently
        # does nothing until something else happens to trigger a rebuild.
        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


def _collect_vibration_sensitivity_numbers(
    hass: HomeAssistant, entry: ConfigEntry, device_list: list[dict[str, Any]],
) -> list[NumberEntity]:
    """Every registered Zigbee2MQTT vibration sensor's sensitivity number.

    Same shape as `_collect_home_order_numbers`: live coordinator devices, then
    a REGISTRY backstop and a LEDGER backstop, so the control does not vanish on
    a cycle where the backend has not answered yet.
    """
    from .const import (
        VIBRATION_SENSITIVITY_OPTION,
        VIBRATION_SENSITIVITY_UID_SUFFIX,
    )

    found: dict[str, dict[str, Any]] = {}
    for device in device_list:
        device_id = str(device.get("device_id") or device.get("id") or "")
        if device_id:
            found.setdefault(device_id, device)

    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id or ent.domain != "number":
                continue
            uid = ent.unique_id or ""
            if uid.startswith(prefix) and uid.endswith(
                VIBRATION_SENSITIVITY_UID_SUFFIX
            ):
                did = uid[len(prefix):-len(VIBRATION_SENSITIVITY_UID_SUFFIX)]
                if did:
                    found.setdefault(did, {"device_id": did})
    except Exception:
        _LOGGER.warning(
            "Registry walk for vibration-sensitivity device_ids failed",
            exc_info=True,
        )

    for device_id in (entry.options.get(VIBRATION_SENSITIVITY_OPTION) or {}):
        if device_id and device_id not in found:
            found[device_id] = {"device_id": device_id}

    entities: list[NumberEntity] = []
    for device in found.values():
        entities.extend(build_vibration_sensitivity_numbers(hass, entry, device))
    return entities


def build_vibration_sensitivity_numbers(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaVibrationSensitivityNumber]:
    """The sensitivity number for one device, or [] when it is not a vibration
    sensor. A `build_*` helper like the rest so the Discovery picker's
    add-dispatch can create it for a freshly-registered sensor with no reload.
    """
    from .vendors.service_classes import classify

    service_class = device.get("service_class") or classify(device) or ""
    if service_class != "vibration_sensor":
        return []
    device_id = str(device.get("device_id") or device.get("id") or "unknown")
    device_name = (
        device.get("display_name") or device.get("name") or "Vibration Sensor"
    )
    return [SensaVibrationSensitivityNumber(hass, entry, device_id, device_name)]


class SensaVibrationSensitivityNumber(NumberEntity):
    """A Zigbee2MQTT vibration sensor's trigger sensitivity, 1 (most) to 21
    (least), on the Settings -> Devices row for that sensor.

    Writes THROUGH to the Z2M `number.*_sensitivity` entity and REMEMBERS the
    value in `entry.options`, because Z2M cannot read sensitivity back off the
    device (Zigbee2MQTT documents it as not gettable). Its own MQTT number is
    therefore permanently `unknown`, so binding the row straight to that entity
    would reproduce the bug this exists to fix: a slider that blanks itself the
    moment you let go of it.

    Options-as-truth like `SensaHomeOrderNumber`, and deliberately NOT a
    RestoreEntity for the same reason: the ledger IS the value, and a restored
    state could disagree with it.

    `native_value` is None until somebody sets it. Never-set and set-to-21 are
    different facts, and only one of them is something we were told.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 1
    _attr_icon = "mdi:tune"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
    ) -> None:
        from .const import (
            VIBRATION_SENSITIVITY_MAX,
            VIBRATION_SENSITIVITY_MIN,
            VIBRATION_SENSITIVITY_UID_SUFFIX,
            vibration_sensitivity_number_entity_id,
        )

        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._attr_name = f"{device_name} Sensitivity"
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}{VIBRATION_SENSITIVITY_UID_SUFFIX}"
        )
        self._attr_native_min_value = VIBRATION_SENSITIVITY_MIN
        self._attr_native_max_value = VIBRATION_SENSITIVITY_MAX
        self.entity_id = vibration_sensitivity_number_entity_id(device_id)

    @property
    def native_value(self) -> float | None:
        from .const import vibration_sensitivity_for

        value = vibration_sensitivity_for(self._entry.options, self._device_id)
        return None if value is None else float(value)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def _z2m_sensitivity_entity(self) -> str | None:
        """The Z2M-published `number.*_sensitivity` for this device, or None.

        Resolved live off the entity registry rather than cached: the MQTT
        entity can register after us on a cold boot.
        """
        from .vendors.zigbee2mqtt import zigbee2mqtt_ha_device_id

        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        coordinator = runtime.get("coordinator")
        devices = (
            (coordinator.data or {}).get("devices", []) if coordinator else []
        )
        device = next(
            (
                d for d in devices
                if str(d.get("device_id") or d.get("id") or "") == self._device_id
            ),
            None,
        )
        if device is None:
            return None
        ha_dev_id = zigbee2mqtt_ha_device_id(self.hass, device)
        if not ha_dev_id:
            return None
        try:
            from homeassistant.helpers import entity_registry as er
            ent_reg = er.async_get(self.hass)
        except ImportError:
            return None
        for ent in ent_reg.entities.values():
            if (
                ent.device_id == ha_dev_id
                and ent.entity_id.startswith("number.")
                and ent.entity_id.endswith("_sensitivity")
            ):
                return ent.entity_id
        return None

    async def async_set_native_value(self, value: float) -> None:
        from .const import (
            VIBRATION_SENSITIVITY_MAX,
            VIBRATION_SENSITIVITY_MIN,
            VIBRATION_SENSITIVITY_OPTION,
        )

        level = round(value)
        level = max(
            VIBRATION_SENSITIVITY_MIN, min(VIBRATION_SENSITIVITY_MAX, level),
        )
        # Write THROUGH first. If the Z2M entity cannot be resolved the write is
        # the part that matters, so log it and do NOT record a value we did not
        # manage to send: a remembered number the sensor never got is worse than
        # a blank one, because it reads as confirmation.
        target = self._z2m_sensitivity_entity()
        if not target:
            _LOGGER.warning(
                "No Zigbee2MQTT sensitivity entity for vibration sensor %s; "
                "sensitivity not written",
                self._device_id,
            )
            return
        await self.hass.services.async_call(
            "number", "set_value",
            {"entity_id": target, "value": level},
            blocking=True,
        )
        ledger = dict(
            self._entry.options.get(VIBRATION_SENSITIVITY_OPTION) or {}
        )
        ledger[self._device_id] = level
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={
                **self._entry.options,
                VIBRATION_SENSITIVITY_OPTION: ledger,
            },
        )
        self.async_write_ha_state()


class SensaConfigNumber(RestoreEntity, NumberEntity):
    """Number entity for a Sensa device configuration threshold."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        key: str,
        name: str,
        icon: str,
        min_value: float,
        max_value: float,
        step: float,
        initial: float,
        unit: str,
        mac: str = "",
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_config_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_native_value = initial
        self._initial = initial

    async def async_added_to_hass(self) -> None:
        """Restore previous value on startup."""
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = self._initial

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"teltonika_{self._device_id}")},
            "name": teltonika_device_name(self._mac),
            "manufacturer": "Teltonika",
            "model": "RutOS Device",
        }


class SensaVehicleGeometryNumber(RestoreEntity, NumberEntity):
    """Singleton vehicle-geometry number — one per vehicle, not per device.

    Lives under the Sensa Nomad hub device so the wheel track / wheelbase /
    shim tolerance stay valid no matter how many levelling sensors are
    installed (or which one is registered first). `RestoreEntity` carries the
    customer's last value across HA restarts. `EntityCategory.CONFIG` keeps
    them out of the Mode Editor eligibility set.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        entry: ConfigEntry,
        *,
        key: str,
        name: str,
        icon: str,
        min_value: float,
        max_value: float,
        step: float,
        initial: float,
        unit: str,
    ) -> None:
        self._entry = entry
        self._key = key
        self._initial = initial
        self._attr_unique_id = f"{entry.entry_id}_vehicle_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_native_value = initial
        self.entity_id = van_level_geometry_entity_id(key)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = self._initial

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class SensaCryptoAutopilotNumber(SensaVehicleGeometryNumber):
    """One miner-autopilot threshold (start / stop battery level, solar per
    miner, maximum room temperature). A singleton like the geometry numbers;
    the values are dumb, every rule between them lives in
    `crypto_autopilot.py`. Bound by the Crypto card's Controls panel."""

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        name, icon, lo, hi, step, default, unit = CRYPTO_AUTOPILOT_NUMBERS[key]
        super().__init__(
            entry, key=key, name=name, icon=icon, min_value=lo, max_value=hi,
            step=step, initial=default, unit=unit,
        )
        self._attr_unique_id = f"{entry.entry_id}_crypto_autopilot_{key}"
        self.entity_id = crypto_autopilot_number_entity_id(key)


class SensaVehicleProfileNumber(RestoreEntity, NumberEntity):
    """One field of the van's Radar vehicle profile (height / width / length /
    weight / max gradient).

    A typed BOX rather than a slider: you know your van is 3.15 m and you want
    to enter it, not hunt for it with a drag. 0 means "not set" -- see
    `_VEHICLE_PROFILE_NUMBERS`.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        entry: ConfigEntry,
        *,
        key: str,
        name: str,
        icon: str,
        min_value: float,
        max_value: float,
        step: float,
        initial: float,
        unit: str,
    ) -> None:
        self._entry = entry
        self._key = key
        self._initial = initial
        self._attr_unique_id = f"{entry.entry_id}_vehicle_profile_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_native_value = initial
        self.entity_id = vehicle_profile_entity_id(key)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = self._initial

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class SensaRadarRadiusNumber(RestoreEntity, NumberEntity):
    """How far out the Radar nearest-amenity sensors look.

    Native unit is KILOMETRES with `device_class: distance`, which is the whole
    point: HA then converts the displayed value (and the value the slider hands
    back) to miles on an imperial system by itself. Nothing downstream has to
    know about unit systems, the coordinator always reads native km and turns
    it into metres for the API.

    `RestoreEntity` carries the customer's setting across restarts;
    `EntityCategory.CONFIG` keeps it out of the Mode Editor eligibility set,
    like the other configuration numbers here.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_name = "Radar Search Radius"
    _attr_icon = "mdi:map-marker-radius"
    _attr_device_class = NumberDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_radar_search_radius"
        self._attr_native_min_value = RADAR_SEARCH_RADIUS_MIN_KM
        self._attr_native_max_value = RADAR_SEARCH_RADIUS_MAX_KM
        self._attr_native_step = 1.0
        self._attr_native_value = RADAR_SEARCH_RADIUS_DEFAULT_KM
        self.entity_id = RADAR_SEARCH_RADIUS_ENTITY_ID

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = RADAR_SEARCH_RADIUS_DEFAULT_KM

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class SensaRemoteSupportWindowNumber(RestoreEntity, NumberEntity):
    """How long a Remote Support window stays open (hours) before auto-closing.

    Read by the consent switch when support is turned on. `RestoreEntity` so the
    chosen window survives restarts; bounded by the same hard safety cap the
    backend clamps to.
    """

    _attr_has_entity_name = True
    _attr_name = "Remote support window"
    _attr_icon = "mdi:timer-lock-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "h"

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import (
            REMOTE_SUPPORT_DEFAULT_WINDOW_HOURS,
            REMOTE_SUPPORT_MAX_WINDOW_HOURS,
            REMOTE_SUPPORT_MIN_WINDOW_HOURS,
            REMOTE_SUPPORT_WINDOW_NUMBER_ENTITY_ID,
        )

        self._entry = entry
        self._initial = float(REMOTE_SUPPORT_DEFAULT_WINDOW_HOURS)
        self._attr_unique_id = f"{entry.entry_id}_remote_support_window_hours"
        self._attr_native_min_value = float(REMOTE_SUPPORT_MIN_WINDOW_HOURS)
        self._attr_native_max_value = float(REMOTE_SUPPORT_MAX_WINDOW_HOURS)
        self._attr_native_step = 1.0
        self._attr_native_value = self._initial
        self.entity_id = REMOTE_SUPPORT_WINDOW_NUMBER_ENTITY_ID

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = self._initial

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class SensaLogRetentionNumber(RestoreEntity, NumberEntity):
    """How far back the log history goes, in days — BOTH logs.

    A free number rather than a preset list, so the customer picks the window
    that suits them. **Zero means keep everything** (see
    `SENSA_LOG_RETENTION_MIN_DAYS`); the ring-buffer cap still applies either
    way, so this only ever shortens the window and never bounds a leak.

    Publishes through `hass.data` as well as pushing to the store, because
    platforms are forwarded before `async_setup_log_store` runs: on a normal
    boot this entity is added while the store does not yet exist, and a direct
    push alone was silently dropped every boot. `RestoreEntity` brings the
    choice back, and that restore is what performs the first prune (the store
    deliberately doesn't prune on load).
    """

    _attr_has_entity_name = True
    _attr_name = "Log retention"
    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "d"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        from .const import (
            LOG_RETENTION_ENTITY_ID,
            SENSA_LOG_RETENTION_DEFAULT_DAYS,
            SENSA_LOG_RETENTION_MAX_DAYS,
            SENSA_LOG_RETENTION_MIN_DAYS,
        )

        self.hass = hass
        self._entry = entry
        self._initial = float(SENSA_LOG_RETENTION_DEFAULT_DAYS)
        self._attr_unique_id = f"{entry.entry_id}_log_retention_days"
        self._attr_native_min_value = float(SENSA_LOG_RETENTION_MIN_DAYS)
        self._attr_native_max_value = float(SENSA_LOG_RETENTION_MAX_DAYS)
        self._attr_native_step = 1.0
        self._attr_native_value = self._initial
        self.entity_id = LOG_RETENTION_ENTITY_ID

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def _apply(self) -> None:
        """Publish the window, and push it to the log store when it's up."""
        from ._log import get_log_store
        from .const import SENSA_LOG_RETENTION_DAYS_KEY

        raw = self._attr_native_value
        # 0 is the "keep everything" sentinel, which the store spells as None.
        days = None if not raw else int(raw)
        self.hass.data.setdefault(DOMAIN, {})[SENSA_LOG_RETENTION_DAYS_KEY] = days
        store = get_log_store(self.hass)
        if store is not None:
            store.set_retention_days(days)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(round(float(last_state.state)))
            except (ValueError, TypeError):
                # Includes a box upgrading from the short-lived retention
                # SELECT, whose state was a label like "90 days".
                self._attr_native_value = self._initial
        self._apply()

    async def async_set_native_value(self, value: float) -> None:
        # Whole days only — a retention window of 14.5 days is meaningless, and
        # a fractional value renders as "14.5" everywhere the state is shown.
        self._attr_native_value = float(round(value))
        self._apply()
        self.async_write_ha_state()


class SensaLevellingOffsetNumber(RestoreEntity, NumberEntity):
    """Per-device pitch/roll calibration offset (degrees).

    Subtracted from the orientated pitch/roll sensor to produce the
    customer-facing "calibrated" reading. The Calibrate Now button writes the
    current orientated value into this entity, zeroing the calibrated reading
    in one tap. `RestoreEntity` so calibration survives restarts.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = -45.0
    _attr_native_max_value = 45.0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "°"

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        axis: str,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._device_name = device_name
        self._axis = axis
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}_{axis}_offset"
        )
        self._attr_name = f"{axis.title()} Offset"
        self._attr_icon = (
            "mdi:angle-acute" if axis == "pitch" else "mdi:angle-obtuse"
        )
        self._attr_native_value = 0.0
        self.entity_id = van_level_offset_entity_id(device_id, axis)

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
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = 0.0

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


def build_light_power_on_delay_numbers(
    hass: HomeAssistant, entry: ConfigEntry,
) -> list[SensaLightPowerOnDelayNumber]:
    """One Power-On Delay number per Zigbee2MQTT lighting controller.

    Exposed like `select.build_led_strip_selects` so a controller adopted after
    platform setup gets its number without an integration reload (see
    `_async_ensure_light_power_on_delay_numbers` in __init__.py).
    """
    from .select import zigbee_light_controller_device_ids

    return [
        SensaLightPowerOnDelayNumber(entry, dev_id)
        for dev_id in zigbee_light_controller_device_ids(hass)
    ]


class SensaLightPowerOnDelayNumber(RestoreEntity, NumberEntity):
    """Per-controller "Power-On Delay": how long the controller is given to boot
    after its supply relay closes, before the card sends the first turn-on.

    A relay-fed controller is a computer on the end of a switched supply. The
    relay restores its power; the controller then has to boot and rejoin Zigbee,
    and any command that lands before it does is simply lost. How long that takes
    is a property of the customer's hardware, so it is theirs to set on the
    device's row rather than a constant in three cards.

    Seconds, one decimal. `NumberMode.BOX` because the useful values are a small
    typed number, not a magnitude anybody drags for.

    The cards read this entity LIVE and never a value baked at dashboard-build
    time, so changing it takes effect on the very next press with no regen (the
    same rule `light_type_entity` follows). `RestoreEntity` keeps the setting
    across restarts; `EntityCategory.CONFIG` keeps it out of the Mode Editor
    eligibility set.

    Created for every Z2M lighting controller, not only the relay-fed ones: the
    entity is a cheap hidden CONFIG entity, while a relay assignment can arrive
    at any time. The Devices row is where the relay gate lives — the field is
    only OFFERED once a relay actually feeds the controller, because until then
    there is no power-up sequence for it to time.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-play-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = LIGHT_POWER_ON_DELAY_MIN_S
    _attr_native_max_value = LIGHT_POWER_ON_DELAY_MAX_S
    _attr_native_step = LIGHT_POWER_ON_DELAY_STEP_S
    _attr_native_unit_of_measurement = "s"

    def __init__(self, entry: ConfigEntry, ha_device_id: str) -> None:
        self._entry = entry
        self._ha_device_id = ha_device_id
        self._attr_name = "Power-On Delay"
        self._attr_unique_id = light_power_on_delay_unique_id(
            entry.entry_id, ha_device_id,
        )
        self.entity_id = light_power_on_delay_number_entity_id(ha_device_id)
        self._attr_native_value = LIGHT_POWER_ON_DELAY_DEFAULT_S

    @property
    def device_info(self) -> dict[str, Any]:
        # The Z2M controller's HA device is mqtt-owned, so this attaches to the
        # Sensa Nomad hub device, exactly as the LED Strip / Light Type selects
        # keyed on the same HA device id do.
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        try:
            self._attr_native_value = float(last.state)
        except (ValueError, TypeError):
            # Never set, or restored as a sentinel. The default is the answer:
            # a blank delay would make the card fall back anyway, and a stored
            # 0 must stay 0.
            self._attr_native_value = LIGHT_POWER_ON_DELAY_DEFAULT_S

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class SensaSolarArrayRatingNumber(RestoreEntity, NumberEntity):
    """The van's solar array rating in watts-peak (the total on the panels'
    labels), which turns Hub's sunshine forecast into expected charge.

    0 means "not set", and the solar forecast then fetches nothing at all, so no
    position leaves the van for a feature the owner is not using. Setting it
    refreshes the forecast at once rather than at the next half-hourly poll.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_name = "Solar Array Rating"
    _attr_icon = "mdi:solar-panel"
    _attr_native_min_value = 0
    _attr_native_max_value = 5000
    _attr_native_step = 10
    _attr_native_unit_of_measurement = "W"

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import SOLAR_ARRAY_RATING_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_solar_array_rating"
        self._attr_native_value = 0
        self.entity_id = SOLAR_ARRAY_RATING_ENTITY_ID

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = max(
            self.native_min_value, min(self.native_max_value, float(value))
        )
        self.async_write_ha_state()
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        coordinator = runtime.get("solar_coordinator")
        if coordinator is not None:
            await coordinator.async_request_refresh()


class SensaBatteryCapacityNumber(RestoreEntity, NumberEntity):
    """The house battery's size in Ah, set by the owner beside the solar panel
    watts. The Electrics card turns the solar forecast's kWh into a battery %
    with it. Van-level, not on a device row: a Victron shunt under a Cerbo has
    no editable properties. 0 means "not set", and the card shows kWh only.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_name = "Battery Capacity"
    _attr_icon = "mdi:car-battery"
    _attr_native_min_value = 0
    _attr_native_max_value = 2000
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "Ah"

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import BATTERY_CAPACITY_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_battery_capacity"
        self._attr_native_value = 0
        self.entity_id = BATTERY_CAPACITY_ENTITY_ID

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = max(
            self.native_min_value, min(self.native_max_value, float(value))
        )
        self.async_write_ha_state()
