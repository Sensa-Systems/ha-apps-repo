"""Starlink dish entity definitions for Sensa Nomad.

Defines sensors, binary sensors and the writable controls (sleep schedule,
stow, reboot) for a Starlink terminal.

Consumer API entities are best-effort -- they degrade gracefully if the
undocumented API changes. Hardware-specific families (battery, actuators)
carry a `requires` tuple so a dish that has no such hardware never gets a
permanently-Unknown row; that is the same lesson that removed the GPS
coordinate sensors.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN

# The shortest sleep window offered, and the floor every write is clamped to.
# The dish refuses a zero-length window (`INVALID_ARGUMENT: Invalid duration
# provided`) even when the schedule is being switched OFF.
MIN_SLEEP_MINUTES = 15

# The controls that carried the retired snow-melt / levelling / update
# settings. Their registry rows are reaped on startup by
# `async_remove_unwritable_controls`; the tuple is `(platform, key)`, the key
# being the `{entry}_starlink_{key}` suffix each entity was registered under.
_RETIRED_CONTROLS: tuple[tuple[str, str], ...] = (
    ("select", "snow_melt"),
    ("select", "levelling"),
    ("switch", "defer_updates"),
    ("number", "update_reboot_hour"),
)


def _device_info(device_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build HA device_info dict from live data."""
    hw = data.get("hardware_version") or data.get("diag_hardware_version") or ""
    sw = data.get("software_version") or data.get("diag_software_version") or ""
    info: dict[str, Any] = {
        "identifiers": {(DOMAIN, f"starlink_{device_id}")},
        "name": "Starlink",
        "manufacturer": "SpaceX",
        "model": hw or "Starlink Dish",
    }
    if sw:
        info["sw_version"] = sw
    return info


def dish_has_actuators(data: dict[str, Any]) -> bool:
    """True when the dish reports motorised actuators (HasActuators.YES).

    A Mini is electronically steered and reports NO (2), so stow / unstow and
    the levelling mode are dead controls on it. UNKNOWN (0) is treated as NO:
    offering a control we are not sure exists is worse than omitting one,
    because a dead button reads as a broken product.
    """
    return data.get("has_actuators") == 1


# -- Sensor descriptions ------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class StarlinkSensorDescription(SensorEntityDescription):
    """Extended sensor description with a value extraction function.

    `fast=True` routes the sensor to the 15s history coordinator instead
    of the 30s main coordinator -- used for Power, Packet Drop Rate and the
    outage list, all of which come from DishGetHistory.

    `requires` names the sample keys that prove the underlying HARDWARE is
    present. When set, the entity is only created if the first good sample
    carries at least one of them, so a Mini never grows a battery row and a
    dish with no actuators never grows a tilt row.

    `attrs_fn` supplies extra state attributes for the few readings that are
    a list rather than a scalar.
    """

    value_fn: Callable[[dict[str, Any]], StateType] = lambda data: None
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    min_tier: str = "lite"
    fast: bool = False
    requires: tuple[str, ...] = dataclass_field(default_factory=tuple)


def _format_uptime(data: dict[str, Any]) -> str | None:
    """Format uptime seconds into a human-readable string."""
    seconds = data.get("uptime_s")
    if seconds is None:
        return None
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _bps_to_mbps(data: dict[str, Any], key: str) -> float | None:
    """Convert bits per second to megabits per second."""
    bps = data.get(key)
    if bps is None:
        return None
    return round(bps / 1_000_000, 2)


def _pct(data: dict[str, Any], key: str) -> float | None:
    """Convert a 0.0-1.0 fraction to a percentage."""
    value = data.get(key)
    if value is None:
        return None
    return round(value * 100, 1)


def _rounded(data: dict[str, Any], key: str, digits: int = 1) -> float | None:
    """Round a float reading, preserving None for "the dish didn't say"."""
    value = data.get(key)
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    """Return the first key's value that is not None.

    Plain `or` collapses False to a missing key -- e.g. on the Mini,
    `alert_motors_stuck = False` (no motors, never stuck) followed by
    a missing `diag_motors_stuck` would yield `None` and surface as
    "Unknown" in HA. This helper preserves the first present-but-falsy
    reading.
    """
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


SENSOR_DESCRIPTIONS: tuple[StarlinkSensorDescription, ...] = (
    # -- Link ---------------------------------------------------------------
    StarlinkSensorDescription(
        key="dish_state",
        name="Status",
        icon="mdi:satellite-variant",
        value_fn=lambda data: data.get("dish_state"),
    ),
    StarlinkSensorDescription(
        key="uptime",
        name="Uptime",
        icon="mdi:timer-outline",
        value_fn=_format_uptime,
    ),
    # SNR (field 1001) was removed in firmware ~2024 -- replaced by two
    # booleans surfaced as binary sensors below. Power input replaces
    # SNR's slot in the sensor list because dish power was the user-
    # facing metric originally missing on the Starlink Mini.
    StarlinkSensorDescription(
        key="power_in",
        name="Power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
        fast=True,
        value_fn=lambda data: _rounded(data, "power_in_w"),
    ),
    StarlinkSensorDescription(
        key="latency",
        name="Latency",
        native_unit_of_measurement="ms",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-sand",
        value_fn=lambda data: _rounded(data, "pop_ping_latency_ms"),
    ),
    StarlinkSensorDescription(
        key="download_throughput",
        name="Download",
        native_unit_of_measurement="Mbit/s",
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:download",
        value_fn=lambda data: _bps_to_mbps(data, "downlink_throughput_bps"),
    ),
    StarlinkSensorDescription(
        key="upload_throughput",
        name="Upload",
        native_unit_of_measurement="Mbit/s",
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:upload",
        value_fn=lambda data: _bps_to_mbps(data, "uplink_throughput_bps"),
    ),
    StarlinkSensorDescription(
        key="drop_rate",
        name="Packet Drop Rate",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:package-variant-remove",
        fast=True,
        value_fn=lambda data: _pct(data, "pop_ping_drop_rate"),
    ),
    # Why the link is throttled, each direction. On a metered Roam plan this
    # is the difference between "the internet is slow here" and "you have
    # used your priority data", which no other reading answers.
    StarlinkSensorDescription(
        key="dl_restricted_reason",
        name="Download Limit",
        icon="mdi:speedometer-slow",
        value_fn=lambda data: data.get("dl_restricted_reason"),
    ),
    StarlinkSensorDescription(
        key="ul_restricted_reason",
        name="Upload Limit",
        icon="mdi:speedometer-slow",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("ul_restricted_reason"),
    ),
    StarlinkSensorDescription(
        key="recent_outages",
        name="Recent Outages",
        icon="mdi:connection",
        state_class=SensorStateClass.MEASUREMENT,
        fast=True,
        value_fn=lambda data: data.get("recent_outage_count"),
        # The list itself rides as attributes: cause, when and how long, which
        # is what turns "the internet dropped" into something diagnosable.
        attrs_fn=lambda data: (
            {"outages": data.get("recent_outages")}
            if data.get("recent_outages")
            else None
        ),
    ),
    # -- Obstruction --------------------------------------------------------
    StarlinkSensorDescription(
        key="obstruction_pct",
        name="Obstruction",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:tree",
        value_fn=lambda data: _pct(data, "fraction_obstructed"),
    ),
    StarlinkSensorDescription(
        key="obstruction_time",
        name="Time Obstructed",
        native_unit_of_measurement="s",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _rounded(data, "obstruction_time_s"),
    ),
    StarlinkSensorDescription(
        key="obstruction_avg_duration",
        name="Average Obstruction",
        native_unit_of_measurement="s",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Only emitted once the dish flags its averages valid, so absence
        # means "not enough data yet" rather than "never obstructed".
        requires=("obstruction_avg_duration_s",),
        value_fn=lambda data: _rounded(data, "obstruction_avg_duration_s"),
    ),
    # -- Service ------------------------------------------------------------
    StarlinkSensorDescription(
        key="disablement_code",
        name="Service Status",
        icon="mdi:shield-check",
        # The status copy is the LIVE value; diagnostics is the fallback for a
        # dish whose consumer API is refusing.
        value_fn=lambda data: _first_present(
            data, "disablement_code_status", "disablement_code",
        ),
    ),
    StarlinkSensorDescription(
        key="class_of_service",
        name="Service Plan",
        icon="mdi:card-account-details-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("class_of_service"),
    ),
    StarlinkSensorDescription(
        key="mobility_class",
        name="Mobility",
        icon="mdi:map-marker-path",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("mobility_class"),
    ),
    # -- GPS (count only; coordinates are policy-gated off Priority plans) ---
    StarlinkSensorDescription(
        key="gps_satellites",
        name="GPS Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-uplink",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("gps_satellites",),
        value_fn=lambda data: data.get("gps_satellites"),
    ),
    # -- Battery (firmware 2026.05.07+, battery-equipped terminals only) -----
    StarlinkSensorDescription(
        key="battery_soc",
        name="Battery",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        requires=("battery_soc",),
        value_fn=lambda data: data.get("battery_soc"),
    ),
    StarlinkSensorDescription(
        key="power_source",
        name="Power Source",
        icon="mdi:power-plug",
        requires=("battery_power_source",),
        value_fn=lambda data: data.get("battery_power_source"),
    ),
    # -- Alignment (actuated dishes only) -----------------------------------
    StarlinkSensorDescription(
        key="tilt_angle",
        name="Tilt",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:angle-acute",
        requires=("tilt_angle_deg",),
        value_fn=lambda data: _rounded(data, "tilt_angle_deg"),
    ),
    StarlinkSensorDescription(
        key="actuator_state",
        name="Actuator",
        icon="mdi:cog-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("actuator_state",),
        value_fn=lambda data: data.get("actuator_state"),
    ),
    StarlinkSensorDescription(
        key="boresight_azimuth",
        name="Azimuth",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:compass-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("boresight_azimuth_deg",),
        value_fn=lambda data: _rounded(data, "boresight_azimuth_deg"),
    ),
    StarlinkSensorDescription(
        key="boresight_elevation",
        name="Elevation",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:angle-obtuse",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("boresight_elevation_deg",),
        value_fn=lambda data: _rounded(data, "boresight_elevation_deg"),
    ),
    # -- Ethernet / update / diagnostics ------------------------------------
    StarlinkSensorDescription(
        key="eth_speed",
        name="Ethernet Speed",
        native_unit_of_measurement="Mbit/s",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:ethernet",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("eth_speed_mbps",),
        value_fn=lambda data: data.get("eth_speed_mbps"),
    ),
    StarlinkSensorDescription(
        key="software_update_state",
        name="Update State",
        icon="mdi:package-down",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("software_update_state",),
        value_fn=lambda data: data.get("software_update_state"),
    ),
    StarlinkSensorDescription(
        key="software_update_progress",
        name="Update Progress",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-download",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("software_update_progress",),
        value_fn=lambda data: _pct(data, "software_update_progress"),
    ),
    # The hour the dish reboots to install a pending update, set in the
    # Starlink app. Read-only: `dish_set_config` is refused from the LAN.
    StarlinkSensorDescription(
        key="update_hour",
        name="Update Hour",
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("config_swupdate_reboot_hour",),
        value_fn=lambda data: (
            f"{int(data['config_swupdate_reboot_hour']):02d}:00"
            if data.get("config_swupdate_reboot_hour") is not None
            else None
        ),
    ),
    StarlinkSensorDescription(
        key="reboot_reason",
        name="Last Reboot Reason",
        icon="mdi:restart-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("reboot_reason",),
        value_fn=lambda data: data.get("reboot_reason"),
    ),
    StarlinkSensorDescription(
        key="self_test",
        name="Self Test",
        icon="mdi:clipboard-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("diag_self_test",),
        value_fn=lambda data: data.get("diag_self_test"),
    ),
    StarlinkSensorDescription(
        key="hardware_version",
        name="Hardware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.get("hardware_version") or data.get("diag_hardware_version")
        ),
    ),
    StarlinkSensorDescription(
        key="software_version",
        name="Software Version",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.get("software_version") or data.get("diag_software_version")
        ),
    ),
    # GPS COORDINATE sensors stay removed. SpaceX disabled local-gRPC GPS on
    # the Mini from firmware 2025.06.03 and across all plans but Priority from
    # 20 May 2026, so `get_location` answers PermissionDenied and every
    # coordinate sensor would read Unknown for ever. Teltonika router GPS is
    # the vehicle's positioning source. The satellite COUNT above is a
    # different field (`gps_stats`, on the status response) and is not gated.
)


# Retired identifiers (lite/plus) share a rank with the canonical ones -- every
# licence signed before the rename still carries them and always will.
TIER_ORDER = {"base": 0, "lite": 0, "pro": 1, "plus": 1, "vision": 2, "ai": 3}


def get_sensor_descriptions_for_tier(
    tier: str,
) -> tuple[StarlinkSensorDescription, ...]:
    """Return sensor descriptions available for the given license tier."""
    tier_level = TIER_ORDER.get(tier, 0)
    return tuple(
        desc
        for desc in SENSOR_DESCRIPTIONS
        if TIER_ORDER.get(desc.min_tier, 0) <= tier_level
    )


def descriptions_for_sample(
    descriptions: tuple[StarlinkSensorDescription, ...],
    sample: dict[str, Any],
) -> tuple[StarlinkSensorDescription, ...]:
    """Drop descriptions whose hardware isn't present in this sample.

    A description with no `requires` is always kept, so a transient poll
    failure can never strip the core sensor set. Only the hardware-specific
    families opt in, and they are the ones where absence is permanent for
    this dish rather than momentary.
    """
    return tuple(
        desc
        for desc in descriptions
        if not desc.requires or any(key in sample for key in desc.requires)
    )


# -- Entity classes -----------------------------------------------------------


class StarlinkSensor(CoordinatorEntity, SensorEntity):
    """Sensor entity for a Starlink dish metric."""

    _attr_has_entity_name = True
    entity_description: StarlinkSensorDescription

    def __init__(
        self, coordinator, entry_id: str, device_id: str, description: StarlinkSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_starlink_{description.key}"
        self._device_id = device_id

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs_fn = self.entity_description.attrs_fn
        if attrs_fn is None:
            return None
        return attrs_fn(self.coordinator.data or {})

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {})


# -- Binary sensor descriptions -----------------------------------------------


@dataclass(frozen=True, kw_only=True)
class StarlinkBinarySensorDescription:
    """Description for a Starlink binary sensor."""

    key: str
    name: str
    icon: str
    device_class: BinarySensorDeviceClass | None = None
    entity_category: EntityCategory | None = None
    requires: tuple[str, ...] = dataclass_field(default_factory=tuple)
    value_fn: Callable[[dict[str, Any]], bool | None] = lambda data: None


BINARY_SENSOR_DESCRIPTIONS: tuple[StarlinkBinarySensorDescription, ...] = (
    StarlinkBinarySensorDescription(
        key="connected",
        name="Connected",
        icon="mdi:satellite-variant",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda data: data.get("dish_state") == "Connected",
    ),
    StarlinkBinarySensorDescription(
        key="obstructed",
        name="Obstructed",
        icon="mdi:tree",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: data.get("currently_obstructed"),
    ),
    StarlinkBinarySensorDescription(
        key="stowed",
        name="Stowed",
        icon="mdi:archive-arrow-down",
        value_fn=lambda data: _first_present(
            data, "stow_requested", "diag_stowed",
        ),
    ),
    StarlinkBinarySensorDescription(
        key="sleeping",
        name="Sleeping",
        icon="mdi:power-sleep",
        # The sleep schedule actually taking effect, as opposed to the switch
        # that configures it. Without this a customer who set a schedule has
        # no way to tell whether the dish is off or simply broken.
        value_fn=lambda data: data.get("alert_power_save_idle"),
    ),
    StarlinkBinarySensorDescription(
        key="heating",
        name="Heating",
        icon="mdi:fire",
        value_fn=lambda data: _first_present(
            data, "alert_is_heating", "diag_heating",
        ),
    ),
    StarlinkBinarySensorDescription(
        key="thermal_throttle",
        name="Thermal Throttle",
        icon="mdi:thermometer-alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: _first_present(
            data, "alert_thermal_throttle", "diag_thermal_throttle",
        ),
    ),
    StarlinkBinarySensorDescription(
        key="thermal_shutdown",
        name="Thermal Shutdown",
        icon="mdi:thermometer-off",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: _first_present(
            data, "alert_thermal_shutdown", "diag_thermal_shutdown",
        ),
    ),
    StarlinkBinarySensorDescription(
        key="motors_stuck",
        name="Motors Stuck",
        icon="mdi:cog-off",
        device_class=BinarySensorDeviceClass.PROBLEM,
        requires=("alert_motors_stuck", "diag_motors_stuck"),
        value_fn=lambda data: _first_present(
            data, "alert_motors_stuck", "diag_motors_stuck",
        ),
    ),
    # Water ingress, both halves. New members of DishAlerts (fields 20 / 21)
    # and the sort of thing a motorhome customer wants to know about before
    # the hardware dies rather than after.
    StarlinkBinarySensorDescription(
        key="dish_water_detected",
        name="Water In Dish",
        icon="mdi:water-alert",
        device_class=BinarySensorDeviceClass.MOISTURE,
        requires=("alert_dish_water_detected",),
        value_fn=lambda data: data.get("alert_dish_water_detected"),
    ),
    StarlinkBinarySensorDescription(
        key="router_water_detected",
        name="Water In Router",
        icon="mdi:water-alert",
        device_class=BinarySensorDeviceClass.MOISTURE,
        requires=("alert_router_water_detected",),
        value_fn=lambda data: data.get("alert_router_water_detected"),
    ),
    StarlinkBinarySensorDescription(
        key="no_ethernet_link",
        name="No Ethernet Link",
        icon="mdi:ethernet-cable-off",
        device_class=BinarySensorDeviceClass.PROBLEM,
        requires=("alert_no_ethernet_link",),
        value_fn=lambda data: data.get("alert_no_ethernet_link"),
    ),
    StarlinkBinarySensorDescription(
        key="slow_ethernet",
        name="Slow Ethernet",
        icon="mdi:ethernet",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _first_present(
            data, "alert_slow_ethernet", "diag_slow_ethernet",
        ),
    ),
    StarlinkBinarySensorDescription(
        key="snr_low",
        name="Signal Persistently Low",
        icon="mdi:signal-off",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: data.get("is_snr_persistently_low"),
    ),
    StarlinkBinarySensorDescription(
        key="signal_below_prediction",
        name="Signal Below Prediction",
        icon="mdi:signal-cellular-outline",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("alert_lower_signal_than_predicted",),
        value_fn=lambda data: data.get("alert_lower_signal_than_predicted"),
    ),
    StarlinkBinarySensorDescription(
        key="roaming",
        name="Roaming",
        icon="mdi:earth",
        value_fn=lambda data: data.get("alert_roaming"),
    ),
    StarlinkBinarySensorDescription(
        key="cell_disabled",
        name="Cell Disabled",
        icon="mdi:map-marker-off",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: data.get("is_cell_disabled"),
    ),
    StarlinkBinarySensorDescription(
        key="moving_too_fast",
        name="Moving Too Fast",
        icon="mdi:speedometer",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: data.get("is_moving_fast_persisted"),
    ),
    StarlinkBinarySensorDescription(
        key="unexpected_location",
        name="Unexpected Location",
        icon="mdi:map-marker-alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("alert_unexpected_location"),
    ),
    StarlinkBinarySensorDescription(
        key="mast_not_vertical",
        name="Mast Not Vertical",
        icon="mdi:format-vertical-align-center",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _first_present(
            data, "alert_mast_not_near_vertical", "diag_mast_not_near_vertical",
        ),
    ),
    StarlinkBinarySensorDescription(
        key="data_overage",
        name="Data Overage",
        icon="mdi:database-alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        requires=("diag_overage_rate_limited",),
        value_fn=lambda data: data.get("diag_overage_rate_limited"),
    ),
    StarlinkBinarySensorDescription(
        key="metered",
        name="Metered Connection",
        icon="mdi:counter",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("treat_as_metered"),
    ),
    StarlinkBinarySensorDescription(
        key="battery_charging",
        name="Battery Charging",
        icon="mdi:battery-charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        requires=("battery_charging",),
        value_fn=lambda data: data.get("battery_charging"),
    ),
    StarlinkBinarySensorDescription(
        key="gps_valid",
        name="GPS Fix",
        icon="mdi:crosshairs-gps",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires=("gps_valid",),
        value_fn=lambda data: data.get("gps_valid"),
    ),
    StarlinkBinarySensorDescription(
        key="install_pending",
        name="Install Pending",
        icon="mdi:wrench-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("alert_install_pending"),
    ),
    StarlinkBinarySensorDescription(
        key="update_pending",
        name="Software Update Pending",
        icon="mdi:update",
        device_class=BinarySensorDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("diag_software_update_pending"),
    ),
)


def binary_descriptions_for_sample(
    sample: dict[str, Any],
) -> tuple[StarlinkBinarySensorDescription, ...]:
    """Binary sensors whose source is present in this sample.

    Same rule as :func:`descriptions_for_sample`: no `requires` means always
    created.
    """
    return tuple(
        desc
        for desc in BINARY_SENSOR_DESCRIPTIONS
        if not desc.requires or any(key in sample for key in desc.requires)
    )


class StarlinkBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor entity for a Starlink dish state."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        entry_id: str,
        device_id: str,
        description: StarlinkBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{entry_id}_starlink_{description.key}"
        self._attr_icon = description.icon
        self._attr_device_class = description.device_class
        self._attr_entity_category = description.entity_category
        self._device_id = device_id

    @property
    def is_on(self) -> bool | None:
        return self._description.value_fn(self.coordinator.data or {})

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {})


# -- Controls -----------------------------------------------------------------
#
# Everything below WRITES to the dish, so every one of them calls
# `require_licence_ok` first -- the same backend gate the Velit climate entity
# and the NAS switch use. Viewing a dish stays available in read-only mode;
# changing its sleep schedule does not.
#
# State comes from `DishGetStatusResponse.config` (field 2000), which rides the
# 30s status poll, so no control needs its own call to know where it stands.
# A write is followed by a config READ (the sleep RPC echoes nothing), and that
# result is merged straight into the coordinator data -- otherwise a control
# would snap back to its old value for up to 30s and read as "the setting did
# not take".


class _StarlinkControlBase(CoordinatorEntity):
    """Shared plumbing for the writable Starlink entities."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator,
        entry_id: str,
        device_id: str,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._device_id = device_id
        self._attr_unique_id = f"{entry_id}_starlink_{key}"
        self._attr_name = name
        self._attr_icon = icon

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {})

    @property
    def _client(self):
        """The gRPC client, via the coordinator that owns it."""
        return getattr(self.coordinator, "client", None)

    def _guard(self) -> None:
        from .. import require_licence_ok

        require_licence_ok(self.hass, self._entry_id)

    async def _async_write_power_save(
        self,
        *,
        enable: bool | None = None,
        start_minutes: int | None = None,
        duration_minutes: int | None = None,
    ) -> None:
        """Write the sleep schedule, carrying the values not being changed.

        `dish_power_save` takes all three values in one message, so a control
        that sent only its own would reset the other two. Each of the three
        controls therefore supplies its own value and lets the current
        coordinator sample fill in the rest.

        A dish that has never had a sleep window holds duration 0, which the
        dish itself rejects, so switching sleep ON before setting a length
        would fail. Anything below the shortest window we offer is treated as
        "not set" and becomes that minimum; the customer then sees a real
        number to adjust rather than an error.
        """
        self._guard()
        client = self._client
        if client is None:
            return
        data = self.coordinator.data or {}
        duration = (
            data.get("config_power_save_duration_minutes", 0)
            if duration_minutes is None
            else duration_minutes
        )
        updated = await client.async_set_power_save(
            start_minutes=(
                data.get("config_power_save_start_minutes", 0)
                if start_minutes is None
                else start_minutes
            ),
            duration_minutes=max(MIN_SLEEP_MINUTES, int(duration or 0)),
            enable=(
                bool(data.get("config_power_save_mode", False))
                if enable is None
                else enable
            ),
        )
        if updated:
            merged = dict(data)
            merged.update(updated)
            self.coordinator.async_set_updated_data(merged)
        else:
            # The read-back failed: fall back to asking, rather than leaving
            # the control showing what the customer just changed away from.
            await self.coordinator.async_request_refresh()


class StarlinkSleepSwitch(_StarlinkControlBase, SwitchEntity):
    """The sleep schedule on/off, from `DishConfig.power_save_mode`."""

    def __init__(self, coordinator, entry_id: str, device_id: str) -> None:
        super().__init__(
            coordinator, entry_id, device_id,
            "sleep_schedule", "Sleep Schedule", "mdi:power-sleep",
        )

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("config_power_save_mode")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write_power_save(enable=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write_power_save(enable=False)


class StarlinkStowSwitch(_StarlinkControlBase, SwitchEntity):
    """Stow / unstow an actuated dish.

    A switch rather than two buttons: stowed is a STATE the dish already
    reports, so a pair of buttons would have been two controls for one
    binary fact with nothing showing which way it currently sits.
    """

    def __init__(self, coordinator, entry_id: str, device_id: str) -> None:
        super().__init__(
            coordinator, entry_id, device_id,
            "stow_control", "Stow Dish", "mdi:archive-arrow-down",
        )

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        return _first_present(data, "stow_requested", "diag_stowed")

    async def _async_stow(self, unstow: bool) -> None:
        self._guard()
        client = self._client
        if client is None:
            return
        await client.async_stow(unstow=unstow)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_stow(unstow=False)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_stow(unstow=True)


class StarlinkSleepNumber(_StarlinkControlBase, NumberEntity):
    """One half of the sleep window: its start, or its length.

    `minimum` is 15 on the duration, not 0: the dish rejects a zero-length
    window outright (`INVALID_ARGUMENT: Invalid duration provided`), and a
    window shorter than a quarter of an hour is not worth offering.
    """

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator,
        entry_id: str,
        device_id: str,
        key: str,
        name: str,
        icon: str,
        config_key: str,
        minimum: float,
        maximum: float,
        step: float,
        unit: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry_id, device_id, key, name, icon)
        self._config_key = config_key
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.data or {}).get(f"config_{self._config_key}")
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        if self._config_key == "power_save_start_minutes":
            await self._async_write_power_save(start_minutes=int(value))
        else:
            await self._async_write_power_save(duration_minutes=int(value))


class StarlinkRebootButton(_StarlinkControlBase, ButtonEntity):
    """Reboot the dish.

    The dish drops its connection immediately, so nothing here waits for a
    reply -- the coordinator's next poll fails and then recovers, which is
    the same shape as any other dish outage.
    """

    def __init__(self, coordinator, entry_id: str, device_id: str) -> None:
        super().__init__(
            coordinator, entry_id, device_id,
            "reboot", "Restart Dish", "mdi:restart",
        )

    async def async_press(self) -> None:
        self._guard()
        client = self._client
        if client is None:
            return
        await client.async_reboot()


def build_control_entities(
    coordinator,
    entry_id: str,
    device_id: str,
    sample: dict[str, Any],
) -> dict[str, list[Any]]:
    """Build the writable Starlink entities, keyed by platform.

    Returns ``{"switches": [...], "selects": [...], "numbers": [...],
    "button": [...]}`` so the caller can hand each list to the matching
    platform callback.

    Controls that need hardware this dish does not have are omitted rather
    than created-and-disabled: `stow` only exists on a dish with actuators,
    and offering it on a Mini would be a control that silently does nothing.

    **Snow melt, levelling, the 3-day update deferral and the update reboot
    hour are deliberately not here.** They live only in DishConfig, and the
    firmware refuses `dish_set_config` from the LAN (see
    `starlink_grpc.async_set_power_save`), so every one of them was a control
    that could only ever fail. They are still READ, on the dish's status
    card. Do not add them back without first proving a write lands.
    """
    has_config = any(key.startswith("config_") for key in sample)
    if not has_config:
        # No DishConfig in the sample means this firmware does not publish it
        # on the status response. Creating the controls anyway would give the
        # customer settings with no state and writes we cannot verify.
        return {"switches": [], "selects": [], "numbers": [], "button": []}

    switches: list[Any] = [
        StarlinkSleepSwitch(coordinator, entry_id, device_id),
    ]
    numbers: list[Any] = [
        StarlinkSleepNumber(
            coordinator, entry_id, device_id,
            "sleep_start", "Sleep Start", "mdi:clock-start",
            "power_save_start_minutes",
            minimum=0, maximum=1439, step=15, unit="min",
        ),
        StarlinkSleepNumber(
            coordinator, entry_id, device_id,
            "sleep_duration", "Sleep Duration", "mdi:clock-end",
            "power_save_duration_minutes",
            minimum=MIN_SLEEP_MINUTES, maximum=1440, step=15, unit="min",
        ),
    ]
    buttons: list[Any] = [
        StarlinkRebootButton(coordinator, entry_id, device_id),
    ]

    if dish_has_actuators(sample):
        switches.append(StarlinkStowSwitch(coordinator, entry_id, device_id))

    return {
        "switches": switches,
        # Nothing left to pick: the two selects were snow melt and levelling.
        # The key stays so callers keep their one shape.
        "selects": [],
        "numbers": numbers,
        "button": buttons,
    }


def async_remove_unwritable_controls(hass, entry_id: str) -> None:
    """Reap the registry rows of the controls the firmware will not accept.

    Snow melt, levelling, "Defer Updates 3 Days" and the update reboot hour
    were all `dish_set_config` writes, which the dish refuses. Deleting the
    entity classes alone leaves their rows behind as restored, unavailable
    entities in Settings -> Devices & Services, which reads as a fault; and
    a stale row would still be resolvable by name, so a dashboard could go on
    rendering a control that can only fail.

    Nothing is lost: every one of these settings is still shown, read-only,
    and still changeable in the Starlink app.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for platform, key in _RETIRED_CONTROLS:
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"{entry_id}_starlink_{key}",
        )
        if entity_id:
            registry.async_remove(entity_id)


__all__ = [
    "BINARY_SENSOR_DESCRIPTIONS",
    "SENSOR_DESCRIPTIONS",
    "StarlinkBinarySensor",
    "StarlinkBinarySensorDescription",
    "StarlinkSensor",
    "StarlinkSensorDescription",
    "async_remove_unwritable_controls",
    "binary_descriptions_for_sample",
    "build_control_entities",
    "descriptions_for_sample",
    "dish_has_actuators",
    "get_sensor_descriptions_for_tier",
]
