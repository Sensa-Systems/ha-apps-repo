"""Sensor platform for Sensa Nomad."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfDataRate,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BACKEND_URL,
    CONF_PROFILE_NAME,
    CONF_SCAN_SUBNET,
    CONF_SELECTED_DEVICES,
    CURRENT_TIER,
    DEFAULT_SCAN_SUBNET,
    DOMAIN,
    HEADER_POWER_CHIP_UID_SUFFIX,
    HEADER_TEMP_CHIP_UID_SUFFIX,
    INTEGRATION_ZIGBEE2MQTT,
    LICENCE_TERMS_URL,
    OTA_STATES_SETTLED,
    OTA_STATUS_STALE_SECONDS,
    PURCHASE_URL,
    RUNTIME_APP_USAGE,
    RUNTIME_CONTAINERS,
    RUNTIME_DATA_USAGE,
    RUNTIME_LICENCE_OK,
    RUNTIME_NETWORK_STATUS,
    RUNTIME_OTA_STATUS,
    RUNTIME_SYSTEM_HARDWARE,
    RUNTIME_SYSTEM_RESOURCES,
    SIGNAL_APP_USAGE_CHANGED,
    SIGNAL_CONTAINERS_CHANGED,
    SIGNAL_CURRENT_MODE_CHANGED,
    SIGNAL_DATA_USAGE_CHANGED,
    SIGNAL_DASHBOARD_REGEN_REQUESTED,
    SIGNAL_MODE_ELIGIBILITY_CHANGED,
    SIGNAL_MODES_CHANGED,
    SIGNAL_NETWORK_STATUS_CHANGED,
    SIGNAL_ROUTINE_ELIGIBILITY_CHANGED,
    SIGNAL_STARTUP_MODE_CHANGED,
    SIGNAL_SYSTEM_HARDWARE_CHANGED,
    SIGNAL_SYSTEM_RESOURCES_CHANGED,
    SIM_MAC_OUI,
    VAN_LEVEL_AXIS_OPTIONS,
    VAN_LEVEL_DEFAULT_AXIS_MAP,
    VAN_LEVEL_LOGICAL_AXES,
    VAN_LEVEL_WHEELS,
    ZIGBEE_UNRECOGNISED_SUBTYPE,
    custom_ac_name,
    van_level_axis_select_entity_id,
    van_level_calibrated_sensor_entity_id,
    van_level_geometry_entity_id,
    van_level_lowest_wheel_sensor_entity_id,
    van_level_offset_entity_id,
    van_level_orientated_sensor_entity_id,
    van_level_shim_max_sensor_entity_id,
    van_level_shim_sensor_entity_id,
    velit_sensor_entity_id,
)
from .coordinator import SensaDataUpdateCoordinator
from .breakdown import SensaBreakdownCoverSensor
from .geofence import SensaGeofenceSensor
from .van_level_setup import levelling_setup_for_device
from .modes_catalog import (
    GATED_MODES,
    HOLD_MODE_SLUGS,
    LOCKED_MODE_SLUGS,
    MODES,
    get_mode,
)

_LOGGER = logging.getLogger(__name__)

# Set once per Python process load (i.e. once per HA start / integration
# reload). Appended as `?v=<stamp>` to every image URL so browsers can't
# serve a cached 404 from before the addon's install-webcards.sh copied
# new images into `/config/www/sensa/images/`. On HA restart the stamp
# changes, URLs change, browsers re-fetch. Hard bug to spot because the
# file is physically present on disk but the browser serves the stale
# 404 from its HTTP cache — cost Richard a 30-minute debug session.
_IMAGE_CACHE_BUSTER = str(int(time.time()))

# When this HA process loaded the integration. The Devices page reads it as
# `started_ts` to keep stale reachability dots grey until the first scans land.
_STARTED_TS = time.time()


SUMMARY_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="discoveries_total",
        name="Discoveries Total",
        icon="mdi:magnify-scan",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="discoveries_new",
        name="Discoveries New",
        icon="mdi:star-box-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="discoveries_known",
        name="Discoveries Known",
        icon="mdi:check-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="devices_total",
        name="Devices Total",
        icon="mdi:devices",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="devices_enabled",
        name="Devices Enabled",
        icon="mdi:toggle-switch",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="integrations_suppressed",
        name="Integrations Ignored",
        icon="mdi:eye-off-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="last_seen_utc",
        name="Discovered Last Seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def _compact_discovery_attributes(discoveries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a compact discovery list suitable for sensor attributes."""
    items: list[dict[str, Any]] = []

    for item in discoveries:
        items.append(
            {
                "discovery_id": item.get("discovery_id"),
                "hostname": item.get("hostname"),
                "model": item.get("model"),
                "device_type": item.get("device_type") or item.get("type"),
                "ip": item.get("ip") or item.get("ip_address"),
                "mac": item.get("mac") or item.get("mac_address"),
                "source": item.get("source"),
            }
        )

    return items


def _compact_device_attributes(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a compact confirmed-device list suitable for sensor attributes."""
    items: list[dict[str, Any]] = []

    for item in devices:
        items.append(
            {
                "device_id": item.get("device_id") or item.get("id"),
                "display_name": item.get("display_name") or item.get("name"),
                "category": item.get("category"),
                "integration_type": item.get("integration_type"),
                "ip": item.get("ip_address") or item.get("ip"),
                "mac": item.get("linked_mac_address") or item.get("mac"),
                "hostname": item.get("linked_hostname") or item.get("hostname"),
                "enabled": item.get("enabled"),
                "config": item.get("config") or {},
            }
        )

    return items


# Singleton sensors that PIN a fixed entity_id (so dashboard cards can
# hardcode it) but whose unique_id is entry-id-scoped. Deleting + re-adding
# the integration (routine on a dev Pi) mints a new entry_id → a new
# unique_id → a fresh registry entry, while the old entry's registry row is
# left orphaned still owning the clean entity_id (shows "unavailable"). The
# live sensor's pin can't reclaim a taken id, so it drifts to
# `sensor.sensa_nomad_<name>_2` and the hardcoded card reads the dead orphan
# forever — the "File Integrity card shows Never / all —" report. Mapping is
# unique_id-suffix → the clean entity_id each pins.
_PINNED_SINGLETON_ENTITY_IDS = {
    "integrity": "sensor.sensa_nomad_integrity",
    "current_mode": "sensor.sensa_nomad_current_mode",
    "mode_eligibility": "sensor.sensa_nomad_mode_eligibility",
    "routine_eligibility": "sensor.sensa_nomad_routine_eligibility",
    # The Schengen alert rule reads this by literal entity_id, so its pin must
    # survive a remove+re-add (entry_id change) or the warning silently stops.
    "schengen_days_used": "sensor.sensa_nomad_schengen_days_used",
    # The Geofence instinct reads this by literal entity_id, same reason.
    "geofence_distance": "sensor.sensa_nomad_geofence_distance",
}


@callback
def _reclaim_pinned_singleton_entity_ids(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Heal entity_id drift for the pinned singleton sensors.

    Runs before the sensors are added. For each pinned id: drop any orphan
    registry row (a different/stale unique_id) squatting on the clean id, then
    rename this entry's drifted row back to the clean id so the live sensor
    re-binds to it. A fresh install has neither an orphan nor a drifted row, so
    this is a no-op and the pin works naturally at add time.
    """
    from homeassistant.helpers.entity_registry import (
        async_get as async_get_entity_registry,
    )

    registry = async_get_entity_registry(hass)
    for suffix, desired in _PINNED_SINGLETON_ENTITY_IDS.items():
        unique_id = f"{entry.entry_id}_{suffix}"
        current = registry.async_get_entity_id("sensor", DOMAIN, unique_id)

        # 1. Free the clean id if an orphan from a prior install holds it.
        holder = registry.async_get(desired)
        if holder is not None and holder.unique_id != unique_id:
            _LOGGER.info(
                "Sensa Nomad: removing orphaned registry entry %s (unique_id "
                "%s) so the live sensor can reclaim the pinned id",
                desired,
                holder.unique_id,
            )
            registry.async_remove(desired)

        # 2. Rename this entry's drifted row back to the clean id.
        if current and current != desired and registry.async_get(desired) is None:
            _LOGGER.info(
                "Sensa Nomad: reclaiming pinned entity_id %s (was drifted to %s)",
                desired,
                current,
            )
            registry.async_update_entity(current, new_entity_id=desired)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad sensors from a config entry."""
    runtime_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SensaDataUpdateCoordinator = runtime_data["coordinator"]

    # Heal any pinned-singleton entity_id drift from a prior install BEFORE
    # adding entities, so the live sensors re-bind to the clean ids the
    # dashboard cards hardcode (see _reclaim_pinned_singleton_entity_ids).
    _reclaim_pinned_singleton_entity_ids(hass, entry)

    discovery_sensor = SensaDiscoverySensor(entry)
    ble_sensor = SensaBleDevicesSensor(entry)
    integrity_sensor = SensaIntegritySensor(entry)
    log_sensor = SensaLogSensor(entry)
    # Stash instances so __init__.py can trigger fast-path refreshes
    # directly after add/remove/ignore/restore without going through
    # update_entity (which would re-run the slow subnet scan). The integrity
    # sensor is stashed for the same reason: the `run_integrity_check` service
    # and the paired button recheck it by calling the instance directly, so
    # triggering a check never depends on `homeassistant.update_entity`
    # resolving the entity_id or on the caller holding entity CONTROL
    # permission (a kiosk/tablet user) — the two silent-failure modes behind
    # the recurring "Run Check does nothing" report.
    runtime_data["discovery_sensor"] = discovery_sensor
    runtime_data["ble_sensor"] = ble_sensor
    runtime_data["integrity_sensor"] = integrity_sensor

    entities: list[SensorEntity] = [
        SensaProfileSensor(entry, runtime_data),
        ble_sensor,
        discovery_sensor,
        SensaLicenceSensor(entry, runtime_data),
        integrity_sensor,
        SensaCurrentModeSensor(entry),
        SensaModeEligibilitySensor(entry),
        SensaRoutineEligibilitySensor(entry),
        log_sensor,
        SensaAutomationsSensor(entry),
        SensaTemperatureWatchSensor(entry),
        SensaCryptoAutopilotSensor(entry),
        SensaClimateControlSensor(entry),
        SensaRoutinesSensor(entry),
        SensaPeopleSensor(entry),
        SensaGpsActualSensor(entry),
        SensaBreakdownCoverSensor(entry),
        SensaGpsSpeedSensor(entry),
        SensaAlarmsSensor(entry),
        SensaSchengenSensor(entry),
        SensaGeofenceSensor(entry),
        # Internet signal sensors — fed by the network-status poller. Ping is
        # live (every cycle); download/upload only update when an on-demand
        # speed test has run. The Cab → Network card binds its quality bars to
        # these.
        SensaNetworkMetricSensor(entry, runtime_data, "ping"),
        SensaNetworkMetricSensor(entry, runtime_data, "download"),
        SensaNetworkMetricSensor(entry, runtime_data, "upload"),
        # Host resource sensors (CPU / memory / disk %) — fed by the
        # system-resources poller; the Settings → Support card binds its
        # Resources bars to these.
        SensaResourceMetricSensor(entry, runtime_data, "cpu"),
        SensaResourceMetricSensor(entry, runtime_data, "memory"),
        SensaResourceMetricSensor(entry, runtime_data, "disk"),
        # Per-app (per-container) usage — the Support card's "Usage by app" panel
        # reads the `apps` attribute; state is the app count.
        SensaAppUsageSensor(entry, runtime_data),
        # Compose-stack container list — the Settings → Supervisor Containers panel
        # reads the `containers` attribute; state is the container count. Empty on
        # non-container installs, so the panel self-omits.
        SensaContainersSensor(entry, runtime_data),
        # Internet data usage per kind of traffic, for Settings -> Data usage.
        SensaDataUsageSensor(entry, runtime_data),
    ]
    entities.extend(
        SensaSummarySensor(entry, coordinator, description)
        for description in SUMMARY_SENSORS
    )

    # Role energy counters (kWh made by integrating role power) for the
    # Electrical History tab; see energy_counters.py.
    from .energy_counters import build_energy_counters

    entities.extend(build_energy_counters(entry))

    # Per-device orientated + calibrated pitch/roll for every levelling
    # sensor — fed by the raw accel sensors the ESPHome firmware exposes,
    # mapped through the per-device axis selects (see select.py), then
    # offset-corrected via the per-device offset numbers (see number.py).
    devices = (coordinator.data or {}).get("devices", [])
    for device in devices:
        if (
            device.get("service_class") != "levelling"
            and device.get("vendor_device_type") != "van_level"
        ):
            continue
        entities.extend(build_levelling_derived_sensors(entry, device))

    # % Tank Level for every Mopeka (`mopeka.py`).
    from .mopeka import build_level_sensors

    for device in devices:
        entities.extend(build_level_sensors(entry, device))

    # Diesel burned per Autoterm heater, and the van's days of diesel left
    # (`diesel_use.py`). The ledger loads first, so a heater it knows keeps its
    # sensors on a restart the backend has not answered yet.
    from .diesel_use import (
        SensaDieselDaysLeftSensor,
        SensaDieselTotalCostSensor,
        async_setup_tracker,
        build_heater_sensors,
        heater_ids_for,
    )

    await async_setup_tracker(hass, entry)
    # Each heater's lifetime run time, read from its own report
    # (`heater_runtime.py`); its sensor is in build_heater_sensors.
    from .heater_runtime import async_setup_tracker as async_setup_runtime_tracker

    await async_setup_runtime_tracker(hass, entry)
    entities.append(SensaDieselDaysLeftSensor(entry))
    entities.append(SensaDieselTotalCostSensor(entry))
    diesel_ids: set[str] = runtime_data.setdefault("diesel_sensors_added", set())
    for device_id, name in heater_ids_for(hass, entry, devices).items():
        entities.extend(build_heater_sensors(entry, device_id, name))
        diesel_ids.add(device_id)

    # Sensa Radar nearest-amenity sensors (Stage 1 of the Radar integration).
    # Create the proximity coordinator once, seed it without ever blocking sensor
    # setup on Radar being reachable, and add the per-category sensors. The
    # coordinator polls only while it has these listeners, and reaches Radar via
    # the per-environment base URL (dev Control -> dev Radar, etc.).
    radar_coordinator = runtime_data.get("radar_coordinator")
    if radar_coordinator is None:
        from .radar_coordinator import RadarProximityCoordinator

        radar_coordinator = RadarProximityCoordinator(hass)
        runtime_data["radar_coordinator"] = radar_coordinator
        try:
            await radar_coordinator.async_config_entry_first_refresh()
        except Exception:  # noqa: BLE001 - Radar must never block sensor setup
            radar_coordinator.async_set_updated_data({})
    from .radar_sensors import build_radar_nearest_sensors

    entities.extend(build_radar_nearest_sensors(entry, radar_coordinator))

    # Solar forecast (solar_forecast.py): expected charge from the van's panels,
    # from Hub's weather API. Fetches nothing until a panel rating is set, and
    # like Radar must never block sensor setup.
    solar_coordinator = runtime_data.get("solar_coordinator")
    if solar_coordinator is None:
        from .solar_forecast import SolarForecastCoordinator

        solar_coordinator = SolarForecastCoordinator(hass)
        runtime_data["solar_coordinator"] = solar_coordinator
        try:
            await solar_coordinator.async_config_entry_first_refresh()
        except Exception:  # noqa: BLE001 - Hub must never block sensor setup
            solar_coordinator.async_set_updated_data({"status": "unavailable"})
    from .solar_sensors import build_solar_sensors

    entities.extend(build_solar_sensors(entry, solar_coordinator))

    # Weather at the van (weather_forecast.py) for the Weather card: forecast,
    # warnings, UV and air from Hub's weather API. Must never block setup.
    weather_coordinator = runtime_data.get("weather_coordinator")
    if weather_coordinator is None:
        from .weather_forecast import WeatherCoordinator

        weather_coordinator = WeatherCoordinator(hass)
        runtime_data["weather_coordinator"] = weather_coordinator
        try:
            await weather_coordinator.async_config_entry_first_refresh()
        except Exception:  # noqa: BLE001 - Hub must never block sensor setup
            weather_coordinator.async_set_updated_data({"status": "unavailable"})
    from .weather_sensors import build_weather_sensors

    entities.extend(build_weather_sensors(entry, weather_coordinator))

    # Store callback for dynamic vendor entity creation
    runtime_data["vendor_add_sensors"] = async_add_entities

    # Drain any vendor entities that a coordinator built BEFORE this
    # platform registered its `async_add_entities` callback. Without this,
    # a coordinator that started ahead of the sensor platform (e.g. the
    # Cerbo MQTT system-aggregate set, or a sub-device whose on_new_device
    # fired during the setup race) silently dropped its entities and never
    # retried — leaving DC System Power / AC Consumption Output (and hence
    # the power-flow AC Load / DC Load nodes) missing. The coordinator
    # queues into `vendor_pending_sensors` when the callback isn't ready;
    # we flush it here the instant it is.
    pending = runtime_data.pop("vendor_pending_sensors", None)
    if pending:
        _LOGGER.info(
            "Sensa Nomad sensor setup: flushing %d vendor sensor(s) queued "
            "before the platform was ready",
            len(pending),
        )
        async_add_entities(pending)

    # Raspberry Pi board sensors, added LATE and only if this box reports
    # hardware. They cannot go in the list above because `supported` is the
    # App's answer and the first poll has not landed at setup time.
    #
    # Creating them eagerly and letting them sit unavailable was the obvious
    # alternative and is wrong: on DIY and Enthusiast, which must not have this
    # feature at all, it would leave five permanently-unknown entities in the
    # customer's entity list with no way to tell them from a fault.
    _async_setup_hardware_sensors(hass, entry, runtime_data, async_add_entities)

    # A USB GPS puck, added late for the same reason: only once one is seen.
    from .usb_gps import async_setup_usb_gps

    runtime_data["usb_gps"] = async_setup_usb_gps(hass, entry, async_add_entities)

    _LOGGER.debug(
        "Sensa Nomad sensor setup: adding %s sensors",
        len(entities),
    )
    async_add_entities(entities)


@callback
def _async_setup_hardware_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: dict[str, Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Pi board sensors once the App confirms this box reports hardware.

    Subscribes to the hardware poller's signal and adds the entities on the
    first payload with ``supported`` true, then unsubscribes. A box that answers
    false (DIY, Enthusiast, an appliance that is not a Pi) never gets them, and
    keeps paying nothing but one cheap poll.
    """
    if runtime_data.get("hardware_sensors_added"):
        return

    @callback
    def _on_hardware(hardware: dict[str, Any]) -> None:
        if not hardware.get("supported"):
            return
        # Guard against a second signal arriving before the unsubscribe below
        # takes effect; adding the same unique_id twice is a hard HA error.
        if runtime_data.get("hardware_sensors_added"):
            return
        runtime_data["hardware_sensors_added"] = True
        _LOGGER.debug(
            "Sensa Nomad: box reports hardware (%s) — adding board sensors",
            hardware.get("model") or "unknown model",
        )
        async_add_entities(
            SensaHardwareMetricSensor(entry, runtime_data, kind)
            for kind in _HARDWARE_METRICS
        )
        unsub()

    unsub = async_dispatcher_connect(hass, SIGNAL_SYSTEM_HARDWARE_CHANGED, _on_hardware)
    entry.async_on_unload(unsub)

    # The prime poll may have already landed while the platforms were being
    # forwarded, in which case the signal has been and gone. Check the cached
    # payload so a fast box is not left with no board sensors until something
    # changes.
    cached = runtime_data.get(RUNTIME_SYSTEM_HARDWARE)
    if cached:
        _on_hardware(cached)


# One config block per internet-signal metric. `path` walks
# runtime[RUNTIME_NETWORK_STATUS]; `ping` is the live reachability RTT,
# download/upload come from the last on-demand speed test.
# Vendors whose registered host is healed when discovery finds them at a new IP,
# mapped to the label used in the log line.
#
# Only vendors reached over IP whose coordinator can be restarted in place
# belong here. The Zigbee coordinator is deliberately ABSENT: its address is
# written into Zigbee2MQTT's own settings, and getting that wrong takes the whole
# Zigbee network down and needs a restart, so it needs its own piece of work and
# real hardware rather than a shared code path.
_HOST_RECONCILE_VENDORS: dict[str, str] = {
    "sensa_victron_mqtt": "Cerbo",
    "sensa_teltonika_api": "Router",
}


# Cool-down between adopt retries for one registered-but-unadopted Shelly, so a
# device that is awake but refuses the flow (auth enabled) is retried every few
# minutes rather than on every discovery poll.
_SHELLY_ADOPT_RETRY_S = 5 * 60


def _mac_key(value: Any) -> str:
    """MAC reduced to bare lowercase hex, so `AA:BB:..` and `aabb..` match."""
    return "".join(c for c in str(value or "").lower() if c in "0123456789abcdef")


async def _shelly_answers(hass: Any, ip: str, mac: str) -> bool:
    """True when the Shelly with this MAC answers `GET /shelly` at `ip` now.

    The MAC check stops a lease handed to some other device being taken for
    the Shelly. Any failure is "not awake", never an error: a sleeping battery
    Shelly fails this on almost every call.
    """
    import asyncio
    import aiohttp

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    try:
        session = async_get_clientsession(hass)
        async with session.get(
            f"http://{ip}/shelly", timeout=aiohttp.ClientTimeout(total=2),
        ) as resp:
            if resp.status != 200:
                return False
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
        return False
    return isinstance(body, dict) and _mac_key(body.get("mac")) == mac


def _coordinator_is_live(coord: Any, window: float) -> bool:
    """True while the vendor's CURRENT host is still working.

    Two shapes, because the two vendors report liveness differently. A streaming
    MQTT coordinator (Cerbo) carries `last_message_monotonic`, so "had traffic
    recently" is the signal. A polling coordinator (Teltonika) has no message
    clock, so a successful last poll is the equivalent: if it is still talking to
    the router at the old address, that address is fine.

    Absent both, treat it as not live so reconciliation can proceed.
    """
    import time as _time

    last_msg = getattr(coord, "last_message_monotonic", None)
    if last_msg is not None:
        return (_time.monotonic() - last_msg) < window
    return bool(getattr(coord, "last_update_success", False))


_NETWORK_METRICS: dict[str, dict[str, Any]] = {
    "ping": {
        "suffix": "internet_ping",
        "name": "Internet Ping",
        "icon": "mdi:speedometer-slow",
        "unit": UnitOfTime.MILLISECONDS,
        "device_class": SensorDeviceClass.DURATION,
        "path": ("ping", "ping_ms"),
    },
    "download": {
        "suffix": "internet_download",
        "name": "Internet Download",
        "icon": "mdi:download-network",
        "unit": UnitOfDataRate.MEGABITS_PER_SECOND,
        "device_class": SensorDeviceClass.DATA_RATE,
        "path": ("speedtest", "download_mbps"),
    },
    "upload": {
        "suffix": "internet_upload",
        "name": "Internet Upload",
        "icon": "mdi:upload-network",
        "unit": UnitOfDataRate.MEGABITS_PER_SECOND,
        "device_class": SensorDeviceClass.DATA_RATE,
        "path": ("speedtest", "upload_mbps"),
    },
}


class SensaNetworkMetricSensor(SensorEntity):
    """One internet-signal metric (ping / download / upload).

    Reads ``runtime[RUNTIME_NETWORK_STATUS]`` (populated by the
    `_async_refresh_network_status` poller). Ping refreshes every poll;
    download/upload only after an on-demand speed test. Listens on
    `SIGNAL_NETWORK_STATUS_CHANGED` so it updates the instant a poll lands.
    The speed-test `status` (idle/running/ok/error) + `ran_at` ride along as
    attributes so the card can show "Testing…" / "ran X ago".
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, entry: ConfigEntry, runtime_data: dict[str, Any], kind: str,
    ) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._kind = kind
        cfg = _NETWORK_METRICS[kind]
        self._path: tuple[str, str] = cfg["path"]
        self._attr_name = cfg["name"]
        self._attr_icon = cfg["icon"]
        self._attr_native_unit_of_measurement = cfg["unit"]
        self._attr_device_class = cfg["device_class"]
        self._attr_unique_id = f"{entry.entry_id}_{cfg['suffix']}"
        self.entity_id = f"sensor.sensa_nomad_{cfg['suffix']}"

    @property
    def native_value(self) -> float | None:
        status = self._runtime_data.get(RUNTIME_NETWORK_STATUS) or {}
        section = status.get(self._path[0]) or {}
        value = section.get(self._path[1])
        return value if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = (self._runtime_data.get(RUNTIME_NETWORK_STATUS) or {}).get("speedtest") or {}
        return {
            "speedtest_status": st.get("status"),
            "speedtest_ran_at": st.get("ran_at"),
        }

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

        @callback
        def _on_change(_status: dict[str, Any]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_NETWORK_STATUS_CHANGED, _on_change,
            )
        )


# One config block per host-resource metric. `key` reads
# runtime[RUNTIME_SYSTEM_RESOURCES]; every metric is a 0-100 percentage.
_RESOURCE_METRICS: dict[str, dict[str, Any]] = {
    "cpu": {
        "suffix": "cpu_use",
        "name": "CPU Usage",
        "icon": "mdi:cpu-64-bit",
        "key": "cpu_percent",
    },
    "memory": {
        "suffix": "memory_use",
        "name": "Memory Usage",
        "icon": "mdi:memory",
        "key": "memory_percent",
    },
    "disk": {
        "suffix": "disk_use",
        "name": "Disk Usage",
        "icon": "mdi:harddisk",
        "key": "disk_percent",
    },
}


class SensaResourceMetricSensor(SensorEntity):
    """One host-resource metric (CPU / memory / disk usage %).

    Reads ``runtime[RUNTIME_SYSTEM_RESOURCES]`` (populated by the
    `_async_refresh_system_resources` poller, sourced from the Sensa App
    add-on's `/api/v1/system/resources`). Listens on
    `SIGNAL_SYSTEM_RESOURCES_CHANGED` so it updates the instant a poll lands.
    The disk sensor carries the used/total GB + source as attributes.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: ConfigEntry, runtime_data: dict[str, Any], kind: str,
    ) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._kind = kind
        cfg = _RESOURCE_METRICS[kind]
        self._key: str = cfg["key"]
        self._attr_name = cfg["name"]
        self._attr_icon = cfg["icon"]
        self._attr_unique_id = f"{entry.entry_id}_{cfg['suffix']}"
        self.entity_id = f"sensor.sensa_nomad_{cfg['suffix']}"

    @property
    def native_value(self) -> float | None:
        resources = self._runtime_data.get(RUNTIME_SYSTEM_RESOURCES) or {}
        value = resources.get(self._key)
        return value if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        resources = self._runtime_data.get(RUNTIME_SYSTEM_RESOURCES) or {}
        if self._kind == "disk":
            return {
                "used_gb": resources.get("disk_used_gb"),
                "total_gb": resources.get("disk_total_gb"),
                "source": resources.get("disk_source"),
            }
        if self._kind == "memory":
            # total_gb lets the Support card label read "Memory (8 GB)".
            return {
                "used_gb": resources.get("memory_used_gb"),
                "total_gb": resources.get("memory_total_gb"),
            }
        return {}

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

        @callback
        def _on_change(_resources: dict[str, Any]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_SYSTEM_RESOURCES_CHANGED, _on_change,
            )
        )


# One config block per Raspberry Pi board reading. `key` reads
# runtime[RUNTIME_SYSTEM_HARDWARE]; `attrs` names the payload keys carried as
# attributes alongside it.
#
# Units differ per metric (unlike _RESOURCE_METRICS, where everything is a
# percentage), so each block carries its own device class and unit. Temperature
# is declared as Celsius and CONVERTED BY HOME ASSISTANT at render time from the
# user's unit setting; nothing here bakes a scale or an offset.
_HARDWARE_METRICS: dict[str, dict[str, Any]] = {
    # "CPU" rather than "SoC", which is what the reading physically is: in this
    # codebase `soc` means STATE OF CHARGE everywhere else (battery["soc"],
    # eid("state_of_charge"), the BMS SoC/SoH gauges), so an entity called
    # sensa_nomad_soc_temperature reads as "state-of-charge temperature" on a van.
    # The kernel calls the zone `cpu-thermal` and HA's own System Monitor says
    # "processor", so CPU is both unambiguous and consistent with the neighbours.
    "cpu_temperature": {
        "suffix": "cpu_temperature",
        "name": "CPU Temperature",
        "icon": "mdi:thermometer",
        "key": "cpu_temperature_c",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
        "attrs": ("rp1_temperature_c", "thermal_trips_c"),
    },
    "cpu_frequency": {
        "suffix": "cpu_frequency",
        "name": "CPU Frequency",
        "icon": "mdi:speedometer",
        "key": "cpu_frequency_mhz",
        "device_class": SensorDeviceClass.FREQUENCY,
        "unit": UnitOfFrequency.MEGAHERTZ,
        "attrs": ("cpu_frequency_min_mhz", "cpu_frequency_max_mhz", "cpu_governor"),
    },
    "fan_speed": {
        "suffix": "fan_speed",
        "name": "Fan Speed",
        "icon": "mdi:fan",
        "key": "fan_rpm",
        # No device class: Home Assistant has none for fan speed, and inventing
        # one would make it convertible to something it is not.
        "device_class": None,
        "unit": REVOLUTIONS_PER_MINUTE,
        "attrs": ("fan_duty_percent", "fan_step", "fan_step_max"),
    },
    # The sum of the rails the PMIC measures (~1.8 W idle), NOT wall draw: the
    # Pi 5 has no input-current channel, so USB peripherals and anything else
    # downstream of the 5V rail are not counted. Good for "is the box working
    # hard", not for sizing a supply.
    "board_power": {
        "suffix": "board_power",
        "name": "Board Power",
        "icon": "mdi:flash",
        "key": "board_power_w",
        "device_class": SensorDeviceClass.POWER,
        "unit": UnitOfPower.WATT,
        "attrs": ("power_rails",),
    },
    "input_voltage": {
        "suffix": "input_voltage",
        "name": "Input Voltage",
        "icon": "mdi:current-dc",
        "key": "input_voltage_v",
        "device_class": SensorDeviceClass.VOLTAGE,
        "unit": UnitOfElectricPotential.VOLT,
        "attrs": (),
    },
}


class SensaHardwareMetricSensor(SensorEntity):
    """One Raspberry Pi board reading (temperature / fan / power / clock).

    Reads ``runtime[RUNTIME_SYSTEM_HARDWARE]`` (populated by the
    `_async_refresh_system_hardware` poller, sourced from the Sensa App add-on's
    `/api/v1/system/hardware`). Listens on `SIGNAL_SYSTEM_HARDWARE_CHANGED` so it
    updates the instant a poll lands.

    These entities are created ONLY once the App has confirmed this box reports
    hardware, so an unavailable one here means the reading itself went missing
    (the host helper stopped publishing, a driver did not load), not that the
    feature is off.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: ConfigEntry, runtime_data: dict[str, Any], kind: str,
    ) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._kind = kind
        cfg = _HARDWARE_METRICS[kind]
        self._key: str = cfg["key"]
        self._attrs: tuple[str, ...] = cfg["attrs"]
        self._attr_name = cfg["name"]
        self._attr_icon = cfg["icon"]
        self._attr_device_class = cfg["device_class"]
        self._attr_native_unit_of_measurement = cfg["unit"]
        self._attr_unique_id = f"{entry.entry_id}_{cfg['suffix']}"
        self.entity_id = f"sensor.sensa_nomad_{cfg['suffix']}"

    @property
    def _hardware(self) -> dict[str, Any]:
        return self._runtime_data.get(RUNTIME_SYSTEM_HARDWARE) or {}

    @property
    def available(self) -> bool:
        """Unavailable when this particular reading is absent.

        Absent is NOT zero. 0 rpm is a seized fan and 0 W is a box that is off,
        so a missing reading has to read as unknown rather than as a confident
        and alarming number. The App omits what it could not read for the same
        reason.
        """
        return self._hardware.get(self._key) is not None

    @property
    def native_value(self) -> float | None:
        value = self._hardware.get(self._key)
        return value if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hardware = self._hardware
        return {
            name: hardware[name] for name in self._attrs if hardware.get(name) is not None
        }

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

        @callback
        def _on_change(_hardware: dict[str, Any]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_SYSTEM_HARDWARE_CHANGED, _on_change,
            )
        )


class SensaAppUsageSensor(SensorEntity):
    """Per-app (per-container) CPU/memory breakdown for the Support card.

    State is the number of apps; the `apps` attribute carries the list
    (`[{name, cpu_percent, memory_mb, memory_percent}]`) from the App add-on's
    `/api/v1/system/processes` (Supervisor container stats), cached in
    ``runtime[RUNTIME_APP_USAGE]`` by the self-rescheduling poller. Updates on
    `SIGNAL_APP_USAGE_CHANGED`.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "App Usage"
    _attr_icon = "mdi:chart-box-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._attr_unique_id = f"{entry.entry_id}_app_usage"
        self.entity_id = "sensor.sensa_nomad_app_usage"

    @property
    def native_value(self) -> int:
        apps = self._runtime_data.get(RUNTIME_APP_USAGE)
        return len(apps) if isinstance(apps, list) else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        apps = self._runtime_data.get(RUNTIME_APP_USAGE)
        return {"apps": apps if isinstance(apps, list) else []}

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

        @callback
        def _on_change(_apps: Any) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_APP_USAGE_CHANGED, _on_change,
            )
        )


class SensaDataUsageSensor(SensorEntity):
    """Internet data the box used in the last 30 days, for Settings -> Data usage.

    State is the 30-day total in MB; the `rows` attribute carries the App's
    per-category figures from `/api/v1/egress/usage`, cached in
    ``runtime[RUNTIME_DATA_USAGE]``. Unknown until the App has answered, never
    a confident zero. Updates on `SIGNAL_DATA_USAGE_CHANGED`.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Data Usage"
    _attr_icon = "mdi:swap-vertical"
    _attr_native_unit_of_measurement = "MB"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # The per-row breakdown changes every poll; history needs only the total.
    _unrecorded_attributes = frozenset({"rows", "since", "overhead_bytes_per_request"})

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._attr_unique_id = f"{entry.entry_id}_data_usage"
        self.entity_id = "sensor.sensa_nomad_data_usage"

    @property
    def native_value(self) -> float | None:
        usage = self._runtime_data.get(RUNTIME_DATA_USAGE)
        if not isinstance(usage, dict):
            return None
        total = sum(
            int(row.get("sent_30d") or 0) + int(row.get("received_30d") or 0)
            for row in usage.get("rows") or []
            if isinstance(row, dict)
        )
        return round(total / 1_000_000, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        usage = self._runtime_data.get(RUNTIME_DATA_USAGE)
        if not isinstance(usage, dict):
            return {"rows": []}
        return {
            "rows": usage.get("rows") or [],
            "since": usage.get("since"),
            "overhead_bytes_per_request": usage.get("overhead_bytes_per_request"),
        }

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

        @callback
        def _on_change(_usage: Any) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DATA_USAGE_CHANGED, _on_change,
            )
        )


class SensaContainersSensor(SensorEntity):
    """Compose-stack container list for the Settings → Supervisor panel.

    State is the number of containers; the `containers` attribute carries the
    list (`[{service, name, state, version, actions}]`) from the App add-on's
    `/api/v1/system/containers` (the updater sidecar's `docker ps -a`), cached in
    ``runtime[RUNTIME_CONTAINERS]`` by the self-rescheduling poller. Empty on
    non-container installs (Supervisor/BYO), so the dashboard panel self-omits.
    Updates on `SIGNAL_CONTAINERS_CHANGED`.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Containers"
    _attr_icon = "mdi:docker"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._attr_unique_id = f"{entry.entry_id}_containers"
        self.entity_id = "sensor.sensa_nomad_containers"

    @property
    def native_value(self) -> int:
        containers = self._runtime_data.get(RUNTIME_CONTAINERS)
        return len(containers) if isinstance(containers, list) else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        containers = self._runtime_data.get(RUNTIME_CONTAINERS)
        return {"containers": containers if isinstance(containers, list) else []}

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

        @callback
        def _on_change(_containers: Any) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONTAINERS_CHANGED, _on_change,
            )
        )


class SensaProfileSensor(SensorEntity):
    """Diagnostic sensor showing the Sensa Nomad profile summary."""

    _attr_has_entity_name = True
    _attr_name = "Profile"
    _attr_icon = "mdi:coach-lamp"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._runtime_data = runtime_data
        self._attr_unique_id = f"{entry.entry_id}_profile"
        self._attr_native_unit_of_measurement = "devices"

    @property
    def native_value(self) -> int:
        """Return number of selected devices."""
        config = self._runtime_data["config"]
        selected_devices = config.get(CONF_SELECTED_DEVICES, [])
        return len(selected_devices)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes."""
        config = self._runtime_data["config"]
        scan_result = self._runtime_data.get("scan_result", {})

        return {
            "license_tier": CURRENT_TIER,
            "profile_name": config.get(CONF_PROFILE_NAME),
            "backend_url": config.get(CONF_BACKEND_URL),
            "scan_subnet": config.get(CONF_SCAN_SUBNET),
            "selected_devices": config.get(CONF_SELECTED_DEVICES, []),
            "selected_devices_count": len(config.get(CONF_SELECTED_DEVICES, [])),
            "last_scan_status": scan_result.get("status", "unknown"),
            "discovered_device_count": len(scan_result.get("devices", [])),
            "suppressed_integrations": config.get("suppressed_integrations", []),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info for the Sensa Nomad hub."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }

    @property
    def should_poll(self) -> bool:
        """Enable polling so attributes refresh."""
        return True


class SensaLicenceSensor(SensorEntity):
    """Licence state exposed to Lovelace cards.

    The state string is one of ``active``, ``trial``, ``trial_expiring``,
    ``trial_expired``, ``phone_home_failed``, ``invalid``. The shell header's
    right-slot trial countdown (``sensa-header`` ``showTrial``) branches on
    these to choose copy and colour. Backed entirely by the runtime dict that
    __init__.py refreshes every 15 minutes — no direct cloud calls from the
    sensor.
    """

    _attr_has_entity_name = True
    _attr_name = "Licence"
    _attr_icon = "mdi:certificate-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._attr_unique_id = f"{entry.entry_id}_licence"

    @property
    def native_value(self) -> str:
        status = self._runtime_data.get("licence_status") or {}
        if not status.get("valid", False):
            return "invalid"
        if not status.get("phone_home_ok", True):
            return "phone_home_failed"
        if status.get("trial"):
            days = status.get("trial_days_remaining")
            if days is not None and days < 3:
                return "trial_expiring"
            return "trial"
        return "active"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._runtime_data.get("licence_status") or {}
        dealer = status.get("dealer") or {}
        return {
            "trial": bool(status.get("trial", False)),
            # The licence's own id, straight from the signed claims via the App's
            # /api/license/status. Shown on the Settings -> Support licence panel so
            # you can tell EXACTLY which licence a box is running without decoding
            # its JWT: it's the id you look the box up by in Hub (and the id Radar's
            # Premium resolve audits against).
            "licence_id": status.get("licence_id"),
            # Which Hub account this licence resolved to for Radar Premium, and the
            # reason when it resolved to none ("Licence is not assigned to a Hub
            # account."). An unassigned licence is otherwise invisible on the box:
            # Premium just silently does not appear.
            "premium_email": (status.get("premium") or {}).get("email") or None,
            "premium_active": bool((status.get("premium") or {}).get("is_pro")),
            "premium_error": (status.get("premium") or {}).get("error") or None,
            "trial_days_remaining": status.get("trial_days_remaining"),
            "expires_at": status.get("expires_at"),
            # Device allowance from the App's /api/license/status (the licence's
            # max_devices claim, else the edition default). `device_cap` None =
            # unlimited (Vision / explicit no-limit). Surfaced so the Support +
            # Admin licence panels can show "12 of 30".
            "device_count": status.get("device_count"),
            "device_cap": status.get("device_cap"),
            # Device gating, as the CLOUD last handed it down (the licensing
            # Function decides which devices a tier may ADD; feature gating is a
            # different thing and lives in this integration's code). Shape:
            # {"category": [...], "service_class": [...], "device": [...]}.
            # The Devices picker reads this to show a gated device as LOCKED
            # instead of letting the customer press Add and take a 403. It is not
            # the enforcement — the App refuses the registration regardless.
            "blocked_devices": status.get("blocked_devices") or {},
            "last_phone_home": status.get("last_phone_home"),
            "phone_home_ok": status.get("phone_home_ok"),
            "editions": status.get("editions") or {},
            # Flat dealer fields (None for a non-dealer licence) — the Support
            # tab's licence card shows `dealer_id`, and they're handy for triage.
            "dealer_id": dealer.get("id"),
            "dealer_homepage_mode": dealer.get("homepage_mode"),
            "read_only": status.get("read_only", False),
            # The sold deployment type ("byo"/"enthusiast"/"appliance"/None) +
            # whether it mismatched this box's deployment_mode (the anti-arbitrage
            # backstop, which drives read-only via the same control_locked gate
            # below). Surfaced for the Support tab + triage.
            "deployment_type": status.get("deployment_type"),
            "deployment_type_blocked": bool(status.get("deployment_type_blocked", False)),
            # The AUTHORITATIVE control gate, mirroring the backend's
            # `licence_ok()` (`RUNTIME_LICENCE_OK`): True only once a status
            # poll has actually confirmed read-only / invalid. Defaults to NOT
            # locked when the gate is unprimed (cold boot, App add-on still
            # starting, van briefly offline) so the frontend fails OPEN exactly
            # like the backend — the licence sensor's *state* reads "invalid"
            # in that unprimed window too, so cards must key off this, NOT the
            # state, or an entitled customer is briefly bricked. See
            # shared/licence.ts `isLicenceReadOnly`.
            "control_locked": not bool(
                self._runtime_data.get(RUNTIME_LICENCE_OK, True)
            ),
            "purchase_url": PURCHASE_URL,
            # Where the licence agreement is published. An attribute rather
            # than a string baked into the card so there is one answer, and so
            # a card on an older bundle simply renders no link instead of a
            # wrong one.
            "terms_url": LICENCE_TERMS_URL,
            # Remote access, as the App last reported it: {enabled, hostname,
            # blocked_reason?}. The Admin panel shows the address the customer
            # would use and, when the tunnel is held shut, why. Carried on this
            # sensor because the panel is already watching it and the hostname is
            # not known at dashboard-build time. No token is ever in here -- the
            # App redacts it before it leaves /api/license/status.
            "cloudflare": status.get("cloudflare") or {},
            "message": status.get("message"),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }

    @property
    def should_poll(self) -> bool:
        return True


# Bluetooth SIG "Company Identifiers" — reported by the device in the
# manufacturer_data field of its BLE advertisement. Covers the common
# consumer-grade vendors we expect to encounter in a motorhome install.
# Full list: https://www.bluetooth.com/specifications/assigned-numbers/
_BLE_MANUFACTURER_IDS: dict[int, str] = {
    0x0006: "Microsoft",
    0x000F: "Broadcom",
    0x004C: "Apple",
    0x0059: "Nordic Semiconductor",
    0x0075: "Samsung",
    0x00E0: "Google",
    0x0131: "Cypress",
    0x0157: "Huami (Amazfit/Mi Band)",
    0x0171: "Amazon",
    0x0181: "Govee",
    0x02E1: "Victron Energy",
    0x02FD: "Anker",
    0x038F: "Xiaomi",
    0x03DA: "Oral-B",
    0x0499: "Ruuvi",
    0x0B65: "Roamer",
    0xC1A4: "JBD",
    0xEC88: "Govee",
}


def _identify_manufacturer(info: Any) -> tuple[str, str]:
    """Return (manufacturer_name, manufacturer_id_hex) for a BLE advertisement.

    Uses the first entry in manufacturer_data. Unrecognised IDs get a
    hex representation so the sensor still surfaces something useful.
    """
    manufacturer_data = getattr(info, "manufacturer_data", None) or {}
    if not manufacturer_data:
        return ("Unknown", "")
    company_id = next(iter(manufacturer_data.keys()))
    hex_id = f"0x{company_id:04X}"
    return (_BLE_MANUFACTURER_IDS.get(company_id, "Unknown"), hex_id)


# Victron's BLE Instant-Readout adverts pack a 16-bit "model_id" into the
# first two bytes (little-endian) of the manufacturer_data block under
# company id 0x02E1. Same company id covers SmartShunt, SmartSolar, Orion XS,
# Multiplus etc., so we can't use it on its own to tell which product is
# advertising — and Victron BLE devices often advertise nameless until paired
# in VictronConnect, so name_prefix matching isn't reliable either. Parsing
# the model_id is the only durable way to identify the product family.
_VICTRON_COMPANY_ID = 0x02E1

# Known SmartShunt model_id values. Extend as new revisions appear; an
# unrecognised id under 0x02E1 logs at INFO so we can grow this set from
# field reports without guessing.
_VICTRON_SMARTSHUNT_MODEL_IDS: frozenset[int] = frozenset({
    0xA389,  # SmartShunt 500A/50mV
    0xA3A4,  # SmartShunt 500A/50mV (later revision)
    0xA3A5,  # SmartShunt 1000A/50mV
    0xA3A6,  # SmartShunt 2000A/50mV
    0xA3A8,  # SmartShunt IP65
    0xA3A9,  # SmartShunt IP65 variant
    0xA3AA,  # SmartShunt IP65 variant
    0xA3AB,  # SmartShunt IP65 variant
})

# Known SmartSolar MPPT model_id values. Same growth pattern as the
# SmartShunt set — unknown ids log once per mac so we can extend from
# field reports. SmartSolar shares the 0x02E1 company id with SmartShunt
# but the model_id ranges don't overlap, so the two sets stay disjoint.
_VICTRON_SMARTSOLAR_MODEL_IDS: frozenset[int] = frozenset({
    0xA050,  # SmartSolar MPPT 250/100
    0xA051,  # SmartSolar MPPT 150/100 rev2
    0xA052,  # SmartSolar MPPT 150/85 rev2
    0xA053,  # SmartSolar MPPT 75/15
    0xA054,  # SmartSolar MPPT 75/10
    0xA055,  # SmartSolar MPPT 100/15
    0xA056,  # SmartSolar MPPT 100/30
    0xA057,  # SmartSolar MPPT 100/50
    0xA058,  # SmartSolar MPPT 150/35
    0xA059,  # SmartSolar MPPT 150/100 rev3
    0xA05A,  # SmartSolar MPPT 150/85 rev3
    0xA05B,  # SmartSolar MPPT 250/70
    0xA05C,  # SmartSolar MPPT 250/85
    0xA05D,  # SmartSolar MPPT 250/60
    0xA05E,  # SmartSolar MPPT 250/45
    0xA05F,  # SmartSolar MPPT 100/20
})

# Known Cerbo GX model_id values. Cerbo GX is a Victron Venus OS
# gateway (not an Instant-Readout sensor like SmartShunt/SmartSolar),
# so its BLE advert is for VictronConnect setup pairing rather than
# sensor data — discovery uses the advert, but the actual integration
# data path is MQTT (Settings \u2192 Services \u2192 MQTT on LAN on the Cerbo).
# Set starts empty; ids are added from field reports as the discovery
# sensor logs unrecognised Victron model_ids on devices named "Cerbo GX".
_VICTRON_CERBO_MODEL_IDS: frozenset[int] = frozenset()

# Known Orion XS model_id values. Orion XS is Victron's smart DC-DC
# charger family (e.g. 12/12-30, 12/12-50, 24/12-30) — advert carries
# Instant-Readout data in the same 0x02E1 frame as SmartShunt/SmartSolar
# and is decoded by the same native victron_ble integration. Unlike
# SmartShunt/SmartSolar, upstream `victron-ble` routes Orion XS on the
# mode byte (0x0F) rather than model_id — we do both for defence in
# depth. Populated from field reports via the "Unrecognised Victron
# model_id" INFO log.
_VICTRON_ORION_XS_MODEL_IDS: frozenset[int] = frozenset({
    0xA3F0,  # Orion XS 12/12-50 (BP-rebranded, HQ2412NW2RP) — dev server 2026-04-21
    0xA3F1,  # Orion XS 1400 — 2026-06-12
})

# Known Victron Smart Lithium model_id values. Smart Lithium is Victron's
# managed LiFePO4 battery (e.g. 12.8V/160Ah, 12.8V/200Ah, 25.6V/200Ah).
# Like Orion XS, the SmartLithium Instant-Readout frame is routed by the
# mode byte (0x05) rather than a per-model model_id, so we gate on the
# mode byte (see `_VICTRON_MODE_SMARTLITHIUM`) and keep this set empty
# until field reports give us concrete model_ids to map to product shots.
_VICTRON_SMARTLITHIUM_MODEL_IDS: frozenset[int] = frozenset()

# Known Victron Smart BatteryProtect model_id values. The Smart
# BatteryProtect (e.g. the 12/24V-220A, part BPR122022000) is a low-voltage
# load disconnect with a switchable output. Like Orion XS and SmartLithium,
# its Instant-Readout frame is routed by the mode byte (0x09) rather than a
# per-model model_id, so we gate on the mode byte (see
# `_VICTRON_MODE_BATTERYPROTECT`) and keep this set empty until field reports
# give us concrete model_ids to map to product shots.
_VICTRON_BATTERYPROTECT_MODEL_IDS: frozenset[int] = frozenset()

# Human-readable model labels keyed by 16-bit model_id — populated from
# the same field reports that grow the per-family frozensets above.
# Surfaced in the picker's expanded-row detail as "Detected model" so
# the operator can see at a glance which Victron variant Sensa thinks
# it's looking at without grepping logs. Keep the comments in the
# frozensets above in sync with these labels; they're the authoritative
# source for the per-family set membership, and these are the display
# strings the customer sees.
_VICTRON_MODEL_NAMES: dict[int, str] = {
    # SmartShunt family
    0xA389: "Victron SmartShunt 500A/50mV",
    0xA3A4: "Victron SmartShunt 500A/50mV (rev)",
    0xA3A5: "Victron SmartShunt 1000A/50mV",
    0xA3A6: "Victron SmartShunt 2000A/50mV",
    0xA3A8: "Victron SmartShunt IP65",
    0xA3A9: "Victron SmartShunt IP65",
    0xA3AA: "Victron SmartShunt IP65",
    0xA3AB: "Victron SmartShunt IP65",
    # SmartSolar MPPT family
    0xA050: "Victron SmartSolar MPPT 250/100",
    0xA051: "Victron SmartSolar MPPT 150/100",
    0xA052: "Victron SmartSolar MPPT 150/85",
    0xA053: "Victron SmartSolar MPPT 75/15",
    0xA054: "Victron SmartSolar MPPT 75/10",
    0xA055: "Victron SmartSolar MPPT 100/15",
    0xA056: "Victron SmartSolar MPPT 100/30",
    0xA057: "Victron SmartSolar MPPT 100/50",
    0xA058: "Victron SmartSolar MPPT 150/35",
    0xA059: "Victron SmartSolar MPPT 150/100",
    0xA05A: "Victron SmartSolar MPPT 150/85",
    0xA05B: "Victron SmartSolar MPPT 250/70",
    0xA05C: "Victron SmartSolar MPPT 250/85",
    0xA05D: "Victron SmartSolar MPPT 250/60",
    0xA05E: "Victron SmartSolar MPPT 250/45",
    0xA05F: "Victron SmartSolar MPPT 100/20",
    # Orion XS DC-DC charger family
    0xA3F0: "Victron Orion XS 12/12-50",
    0xA3F1: "Victron Orion XS 1400",
}

# Victron "mode" byte values. The mode byte lives at byte 2 of HA's
# `manufacturer_data[0x02E1]` payload (upstream's raw-advert byte 4;
# HA strips the 2-byte product prefix before handing the payload to
# us, which also shifts model_id from upstream bytes 2-3 to our
# bytes 0-1 — see `_victron_model_id`). Upstream's `detect_device_type`
# dispatch uses mode as the canonical discriminator for Instant
# Readout frames.
#
# Full mode map (from upstream `victron-ble/devices/__init__.py`):
#   0x01 SolarCharger     0x02 BatteryMonitor   0x03 Inverter
#   0x04 DcDcConverter    0x05 SmartLithium     0x08 AcCharger
#   0x09 SmartBatteryProtect  0x0A LynxSmartBMS  0x0B MultiRS
#   0x0C VEBus            0x0D DcEnergyMeter    0x0F OrionXS
#
# We only gate on 0x0F today, because Orion XS adverts don't carry
# the per-model model_ids the rest of the fleet does — mode byte is
# the only durable discriminator. SmartShunt/SmartSolar keep their
# model_id gate (more informative for image lookup) and don't need
# a mode-byte fallback.
_VICTRON_MODE_ORION_XS = 0x0F

# SmartLithium record-type byte. Victron's managed LiFePO4 battery
# broadcasts cell voltages + battery voltage + temperature + balancer
# status (no SoC/current — those come from a shunt / GX). HA core's
# `victron_ble` integration decodes this record, so the Smart Lithium is
# adopted (Adopt method) rather than needing a Sensa decoder like Orion
# XS. We gate on the mode byte because the SmartLithium advert doesn't
# carry per-model model_ids (same as Orion XS).
_VICTRON_MODE_SMARTLITHIUM = 0x05

# Smart BatteryProtect record-type byte. Victron's low-voltage load
# disconnect broadcasts output state (on/off/disconnected), the alarm /
# off-reason, input voltage and output voltage. HA core's `victron_ble`
# integration decodes this record, so the BatteryProtect is adopted (Adopt
# method) rather than needing a Sensa decoder. Instant Readout is broadcast
# only, so the adopted device is read-only — switching the output would need
# a GATT-write vendor module (a follow-on). We gate on the mode byte because
# the BatteryProtect advert doesn't carry per-model model_ids (same as
# Orion XS / SmartLithium).
_VICTRON_MODE_BATTERYPROTECT = 0x09


def _victron_model_id(info: Any) -> int | None:
    """Extract Victron's 16-bit model_id from manufacturer_data, or None.

    Victron Instant Readout payload layout (verified against real
    adverts from SmartShunt, SmartSolar, Orion XS on a live dev server):

        bytes 0-1: frame prefix (first byte always 0x10 — product
                    type; second byte is the IR protocol/encryption
                    variant, 0x00 or 0x02 observed in the wild)
        bytes 2-3: 16-bit model_id (little-endian)
        byte  4  : mode / record-type byte (see `_VICTRON_MODE_*`)
        byte  5+ : encrypted Instant Readout data

    Returns None for non-Victron info objects or frames shorter than
    4 bytes (can't fit model_id).
    """
    mfr_data = getattr(info, "manufacturer_data", None) or {}
    payload = mfr_data.get(_VICTRON_COMPANY_ID)
    if not payload or len(payload) < 4:
        return None
    return payload[2] | (payload[3] << 8)


def _victron_mode_byte(info: Any) -> int | None:
    """Extract Victron's "mode" byte from manufacturer_data, or None.

    Byte 4 of the Victron IR frame — see `_victron_model_id` for the
    full layout. The mode byte identifies the device family (upstream
    `victron-ble` routes to the right decoder class off this byte);
    full map lives above `_VICTRON_MODE_ORION_XS`.
    """
    mfr_data = getattr(info, "manufacturer_data", None) or {}
    payload = mfr_data.get(_VICTRON_COMPANY_ID)
    if not payload or len(payload) < 5:
        return None
    return payload[4]


def _victron_device_subtype(model_id: int | None) -> str:
    """Map a Victron model_id to a `device_subtype` tag, or "" if unsupported.

    The picker filter rules key off `device_subtype` so unknown Victron
    products (Multiplus / Phoenix inverter / …) fall through to the
    Others group instead of getting a SmartShunt / SmartSolar / Cerbo /
    Orion XS tag they can't honour.
    """
    if model_id is None:
        return ""
    if model_id in _VICTRON_SMARTSHUNT_MODEL_IDS:
        return "smartshunt"
    if model_id in _VICTRON_SMARTSOLAR_MODEL_IDS:
        return "smartsolar"
    if model_id in _VICTRON_CERBO_MODEL_IDS:
        return "cerbo"
    if model_id in _VICTRON_ORION_XS_MODEL_IDS:
        return "orion_xs"
    if model_id in _VICTRON_SMARTLITHIUM_MODEL_IDS:
        return "smartlithium"
    if model_id in _VICTRON_BATTERYPROTECT_MODEL_IDS:
        return "battery_protect"
    return ""


# Macs we've already logged an unknown Victron model_id for, so the INFO
# message fires once per mac per process rather than every poll.
_LOGGED_UNKNOWN_VICTRON_MODELS: set[tuple[str, int]] = set()

# Macs we've already INFO-logged the raw manufacturer_data dump for.
# Keeps the diagnostic one line per device per process lifetime so a
# busy discovery loop doesn't flood the log.
_LOGGED_VICTRON_RAW_DUMP: set[str] = set()
_LOGGED_RUUVI_RAW_DUMP: set[str] = set()

# Macs we've already WARNING-logged a Ruuvi backstop miss for, so the
# "why didn't detection fire" diagnostic surfaces exactly once per MAC
# per process.
_LOGGED_RUUVI_BACKSTOP_MISS: set[tuple[str, str]] = set()


def _maybe_log_ruuvi_backstop_miss(mac: str, reason: str) -> None:
    """One-shot WARNING log per (MAC, reason) when the Ruuvi backstop
    can't stamp a detected_model. Surfaces the exact failure mode to
    the HA log so we don't have to add per-attempt diagnostics."""
    key = (mac, reason.split(" ")[0])  # dedup on the short failure code
    if key in _LOGGED_RUUVI_BACKSTOP_MISS:
        return
    _LOGGED_RUUVI_BACKSTOP_MISS.add(key)
    _LOGGER.warning("Ruuvi backstop miss mac=%s reason=%s", mac, reason)


def _find_discovered_info(hass: Any, mac_colon: str) -> Any:
    """Return the `BluetoothServiceInfoBleak` for this MAC from the
    full discovered-service-info view, or None. Fallback for cases
    where `async_last_service_info` can't find the tag (non-connectable
    advert cache key mismatch)."""
    try:
        from homeassistant.components.bluetooth import async_discovered_service_info
    except ImportError:
        return None
    target = mac_colon.upper()
    try:
        for info in async_discovered_service_info(hass):
            if str(getattr(info, "address", "") or "").upper() == target:
                return info
    except Exception:  # noqa: BLE001
        return None
    return None

# Ruuvi Environment Sensor — BLE beacon. Manufacturer ID is Ruuvi
# Innovations. Data-format byte lives in manufacturer_data[0x0499]
# byte 0 (per Ruuvi's spec); real byte layout verified from a dev-Pi
# advert dump before model classification is wired up — see
# `_LOGGED_RUUVI_RAW_DUMP` and the `_annotate_ruuvi_advert` diagnostic.
_RUUVI_COMPANY_ID = 0x0499


# Cerbo GX is BLE-discovery-only (data path is MQTT over IP) and we
# don't yet have a byte-level signature for it — keep the name-prefix
# gate as a stopgap until we get a real Cerbo advert dump. SmartShunt /
# SmartSolar / Orion XS all route purely on bytes (model_id + mode byte)
# because VictronConnect lets the customer rename the BLE local_name,
# which makes name-prefix detection fragile the moment anyone personalises
# the device.
_VICTRON_CERBO_NAME_PREFIXES: tuple[str, ...] = (
    "Cerbo GX",
)


def _annotate_victron_subtype(row: dict[str, Any], info: Any) -> None:
    """Tag a BLE row with `victron_model_id` + `device_subtype` if Victron.

    No-op for non-Victron rows. Subtype detection runs in order:

      1. Model_id lookup — 16-bit model_id parsed from the IR frame's
         bytes 2-3 LE. If it sits in one of the product frozensets
         (SmartShunt/SmartSolar/Cerbo/Orion XS) the row is tagged.
      2. Mode-byte fallback — byte 4 of the frame (same map upstream
         `victron-ble` uses). Today only 0x0F → orion_xs is wired up;
         the other modes could be added here to catch unknown-model
         variants but SmartShunt/SmartSolar already have comprehensive
         model_id sets so the fallback isn't needed for them.
      3. Cerbo GX name-prefix (stopgap until we have a Cerbo byte
         signature — Cerbo BLE is discovery-only so we accept the
         fragility).

    Anything else (Multiplus, Phoenix inverter, …) gets the model_id
    stamped onto the row for diagnostics but no device_subtype, so the
    picker filter rules drop it to Others.

    Unrecognised Victron model_ids log once at INFO with the mac + hex
    id, so we can grow the frozensets from field reports without a
    customer round-trip.
    """
    raw_mfr = str(row.get("manufacturer_id") or "").lower()
    if raw_mfr != f"0x{_VICTRON_COMPANY_ID:04x}":
        return
    # Diagnostic: expose the first 8 bytes of manufacturer_data[0x02E1]
    # on every Victron row so the discovery sensor state carries ground
    # truth for future byte-level detection work. Customer-renameable
    # Victron adverts mean name-prefix gating is brittle — the durable
    # discriminators live in these bytes (model_id, mode byte). Keeping
    # the dump in production is cheap (~24 chars) and makes future
    # device adds inspectable without a log-dive.
    mfr_data = getattr(info, "manufacturer_data", None) or {}
    payload = mfr_data.get(_VICTRON_COMPANY_ID)
    if payload:
        row["victron_mfr_hex"] = payload[:8].hex()
    # One-shot per-MAC diagnostic at INFO level — dumps the complete
    # manufacturer_data dict (every company_id, every payload in hex)
    # and the service_uuids / service_data so we can see exactly what
    # HA's bluetooth integration is handing us, independent of Sensa
    # row mutation. Fires once per MAC per process lifetime to avoid
    # flooding the log during the discovery loop.
    mac_for_dump = str(
        row.get("mac_address") or row.get("address") or ""
    ).upper()
    if mac_for_dump and mac_for_dump not in _LOGGED_VICTRON_RAW_DUMP:
        _LOGGED_VICTRON_RAW_DUMP.add(mac_for_dump)
        mfr_dump = {
            f"0x{cid:04X}": (data.hex() if data else "<empty>")
            for cid, data in sorted(mfr_data.items())
        }
        svc_data = getattr(info, "service_data", None) or {}
        svc_dump = {
            str(uuid): (data.hex() if data else "<empty>")
            for uuid, data in svc_data.items()
        }
        svc_uuids = list(getattr(info, "service_uuids", None) or ())
        # INFO level is below HA's default filter for custom integrations,
        # so this is latent unless the operator adds a logger override
        # for `custom_components.sensa_nomad`. That's intentional — byte
        # detection is now working off verified offsets, so the dump is
        # here as a "new device" diagnostic only. When adding support for
        # a new Victron variant, bump `custom_components.sensa_nomad` to
        # INFO in configuration.yaml to capture real advert bytes.
        _LOGGER.info(
            "Victron advert raw dump mac=%s name=%s manufacturer_data=%s "
            "service_data=%s service_uuids=%s",
            mac_for_dump,
            row.get("name") or "?",
            mfr_dump,
            svc_dump,
            svc_uuids,
        )
    model_id = _victron_model_id(info)
    if model_id is not None:
        row["victron_model_id"] = f"0x{model_id:04X}"
        model_name = _VICTRON_MODEL_NAMES.get(model_id)
        if model_name:
            row["detected_model"] = model_name

    # Primary gate: model_id lookup against the per-product frozensets.
    # Covers the vast majority of real-world Victron IR devices; these
    # model_ids are stable across firmware revisions and customer renames.
    subtype_from_id = _victron_device_subtype(model_id)
    if subtype_from_id:
        row["device_subtype"] = subtype_from_id
        return

    # Secondary gate: mode byte (upstream `victron-ble` routes OrionXS
    # on this byte). Catches variants whose model_id we haven't catalogued
    # yet. Only 0x0F → orion_xs is wired today; extend when a customer
    # reports a new variant whose model_id isn't in the set.
    if _victron_mode_byte(info) == _VICTRON_MODE_ORION_XS:
        row["device_subtype"] = "orion_xs"
        return

    # SmartLithium (managed LiFePO4 battery) — routed on the mode byte
    # for the same reason as Orion XS: the advert carries no per-model
    # model_id. HA core's victron_ble decodes the record, so a tagged
    # row is adoptable straight away.
    if _victron_mode_byte(info) == _VICTRON_MODE_SMARTLITHIUM:
        row["device_subtype"] = "smartlithium"
        return

    # Smart BatteryProtect (low-voltage load disconnect) — routed on the
    # mode byte for the same reason as Orion XS / SmartLithium: the advert
    # carries no per-model model_id. HA core's victron_ble decodes the
    # record, so a tagged row is adoptable straight away (read-only).
    if _victron_mode_byte(info) == _VICTRON_MODE_BATTERYPROTECT:
        row["device_subtype"] = "battery_protect"
        return

    # Cerbo GX stopgap — name prefix until we have a byte dump for one.
    # Cerbo BLE advert is only used for discovery (Venus OS data travels
    # over MQTT on IP) so accepting fragility here doesn't affect runtime
    # telemetry, just whether the customer can tick the Cerbo row during
    # setup. Remove once a Cerbo dump lets us add it to the model_id /
    # mode-byte path above.
    name = str(row.get("name") or "")
    if any(name.startswith(p) for p in _VICTRON_CERBO_NAME_PREFIXES):
        row["device_subtype"] = "cerbo"
        return

    if model_id is None:
        return
    mac = str(row.get("mac_address") or row.get("address") or "").upper()
    key = (mac, model_id)
    if key not in _LOGGED_UNKNOWN_VICTRON_MODELS:
        _LOGGED_UNKNOWN_VICTRON_MODELS.add(key)
        _LOGGER.info(
            "Unrecognised Victron model_id 0x%04X on %s (name=%s) — falling "
            "to Others. Add to _VICTRON_SMARTSHUNT_MODEL_IDS, "
            "_VICTRON_SMARTSOLAR_MODEL_IDS, _VICTRON_CERBO_MODEL_IDS, "
            "_VICTRON_ORION_XS_MODEL_IDS, _VICTRON_SMARTLITHIUM_MODEL_IDS, "
            "or _VICTRON_BATTERYPROTECT_MODEL_IDS "
            "in sensor.py if this is a variant we should support.",
            model_id, mac or "?", name or "?",
        )


def _ruuvi_model_name(payload: bytes) -> str:
    """Derive a Ruuvi model label from the advert's sensor-absence flags.

    Byte layout (verified 2026-04-21 against two real Ruuvi adverts on
    the dev Pi — Format 5 / RAWv2):

        byte 0:   data format (0x05 = RAWv2)
        bytes 1-2: temperature, signed i16, 0.005 °C/LSB
        bytes 3-4: humidity,    u16, 0.0025 %/LSB
        bytes 5-6: pressure,    u16, Pa - 50000 (0xFFFF = sensor absent)
        bytes 7-8,9-10,11-12:  accel X/Y/Z, signed i16 mg (0x8000 = absent)
        bytes 13+: power info, movement counter, sequence, MAC

    Ruuvi's Pro variants physically drop sensors but share the advert
    format — missing sensors set the documented "invalid" sentinel:

        2-in-1: pressure 0xFFFF AND all three accel axes 0x8000
                (temperature + humidity only)
        3-in-1: pressure 0xFFFF, accel axes populated
                (temperature + humidity + accelerometer)
        4-in-1: all fields populated (standard RuuviTag form factor
                is indistinguishable from Pro 4-in-1 via advert alone)

    Non-Format-5 adverts (RAWv1 = 0x03, compact formats) return "" —
    extend this when a customer reports one of those firmware revisions.
    """
    if not payload or len(payload) < 7:
        return ""
    if payload[0] != 0x05:
        return ""
    humidity_raw = (payload[3] << 8) | payload[4]
    pressure_raw = (payload[5] << 8) | payload[6]
    humidity_absent = humidity_raw == 0xFFFF
    pressure_absent = pressure_raw == 0xFFFF
    # Output strings are dash-free (`"Pro 2in1"` not `"Pro 2-in-1"`) to
    # match the `model_images` / `display_names` map keys in
    # `device_catalog.json`'s ruuvitag entry. `_match_model_image` does
    # case-insensitive substring matching, so any dash mismatch drops
    # the row back to the generic ruuvitag.webp silently.
    #
    # Ruuvi SKU classification (motion is present on every variant —
    # the per-SKU sensor differences are humidity and pressure):
    #   Pro 2in1 — temperature + motion only (humidity=0xFFFF, pressure=0xFFFF)
    #   Pro 3in1 — temperature + motion + humidity (pressure=0xFFFF)
    #   Pro 4in1 — all four sensors (temperature + motion + humidity + pressure)
    # A standard RuuviTag advertises identically to a Pro 4in1, so the
    # 4in1 branch covers both. The advert sets 0xFFFF as the Ruuvi-spec
    # sentinel for sensor-absent fields; we trust that sentinel here.
    if humidity_absent and pressure_absent:
        return "RuuviTag Pro 2in1"
    if pressure_absent:
        return "RuuviTag Pro 3in1"
    return "RuuviTag 4in1"


_LOGGED_GOVEE_RAW_DUMP: set[str] = set()


def _annotate_govee_model(row: dict[str, Any], info: Any) -> None:
    """Stamp `detected_model` on a Govee BLE row by decoding the
    manufacturer_data bytes directly — NOT by parsing the advertised
    local_name.

    Why: advert names on Govee sensors are either the user-configurable
    alias from the Govee app or fall off entirely when a neighbour
    advert collides. The byte payload carries the model ID reliably —
    the `govee_ble` PyPI package (already installed as a dep of HA's
    `govee_ble` integration) knows how to decode it.

    Mechanism: feed the `BluetoothServiceInfoBleak` straight into
    `GoveeBluetoothDeviceData.update()`. The parser's `title` property
    holds the model string ("H5074", "H5075", "H5101", …) once it has
    seen enough bytes to classify; we take the first non-empty title
    and stamp it as `detected_model`. Silent no-op if:

      * manufacturer_id isn't 0xEC88,
      * the govee_ble library is missing (shouldn't happen — ships
        with HA's Bluetooth dependency bundle, but don't hard-crash
        if it does),
      * the parser rejects the advert (not a supported Govee model).

    One-shot per MAC: on first successful classification we emit an
    INFO log with the raw advert bytes so "why is my H51xx unknown?"
    is a one-line diagnostic instead of a packet capture.
    """
    raw_mfr = str(row.get("manufacturer_id") or "").lower()
    if raw_mfr != "0xec88":
        return
    try:
        from govee_ble import GoveeBluetoothDeviceData
    except ImportError:
        _LOGGER.debug(
            "Govee annotate: govee_ble library not installed; "
            "byte-level model detection disabled",
        )
        return

    parser = GoveeBluetoothDeviceData()
    try:
        parser.update(info)
    except Exception:
        _LOGGER.debug(
            "Govee annotate: govee_ble parser raised on advert; "
            "leaving detected_model unset",
            exc_info=True,
        )
        return

    # Byte-derived product title; `parser.title` is the byte-parsing
    # output from the govee_ble library — the same string HA's
    # native integration writes to `device_info.model`.
    model = (getattr(parser, "title", None) or "").strip()
    # Some govee_ble versions expose it under `name` / `device_type`
    # instead — try the fallbacks before giving up.
    if not model:
        model = (getattr(parser, "name", None) or "").strip()
    if not model:
        dev_type = getattr(parser, "device_type", None)
        model = str(dev_type).strip() if dev_type else ""
    if not model:
        return
    row["detected_model"] = model

    mac_for_dump = str(
        row.get("mac_address") or row.get("address") or ""
    ).upper()
    if mac_for_dump and mac_for_dump not in _LOGGED_GOVEE_RAW_DUMP:
        _LOGGED_GOVEE_RAW_DUMP.add(mac_for_dump)
        mfr_data = getattr(info, "manufacturer_data", None) or {}
        payload = mfr_data.get(0xEC88)
        _LOGGER.info(
            "Govee advert classified: mac=%s model=%r "
            "manufacturer_data[0xEC88]=%s",
            mac_for_dump, model,
            payload.hex() if payload else "<empty>",
        )


def _annotate_dji_power_advert(row: dict[str, Any], info: Any) -> None:
    """Stamp `detected_model` on a DJI Power advert from its local name.

    The whole range is ONE catalog entry matched loosely (DJI's company id
    plus a "Power" name prefix), so the picker would otherwise show every
    unit as the generic "DJI Power Station". The advert name carries the
    model, so classify it and show the real one. An unrecognised unit falls
    through unchanged and keeps the generic name rather than being labelled
    as a model we have not confirmed.
    """
    advert_name = str(row.get("name") or "").strip()
    if not advert_name.lower().startswith("power"):
        return
    try:
        from .vendors.dji_ble import classify_dji_model
    except ImportError:
        return
    _slug, display, image = classify_dji_model(advert_name)
    if display:
        row["detected_model"] = display
    if image:
        row["image"] = image


def _annotate_tpms_advert(row: dict[str, Any], info: Any) -> None:
    """Stamp `device_subtype = "tpms_<format>"` on a tyre-pressure advert.

    TPMS families share no company ID (the DJTPMS "company ID" even changes
    between adverts), so the catalog matches them on this subtype alone. It
    is stamped only when the advert actually DECODES as a reading — never
    on a name guess — so a matched row is always a real TPMS frame.
    """
    if row.get("device_subtype"):
        return
    try:
        from .vendors.tpms_ble import decode_service_info
    except ImportError:
        return
    try:
        reading = decode_service_info(info)
    except Exception:
        _LOGGER.debug("TPMS decode raised for %s", row.get("address"), exc_info=True)
        return
    if reading is not None:
        row["device_subtype"] = f"tpms_{reading.format}"


def _annotate_roamer_bms_advert(row: dict[str, Any], info: Any) -> None:
    """Stamp `detected_model` on Roamer BMS adverts using the BMS
    catalog's `device_name_prefixes` classifier.

    Roamer packs advertise their model directly in the BLE local_name
    field — NOT as a user-renamable label. The chipset firmware hard-
    codes names like `Roamer-200A-2493` (Gen 1 VIP) or `ROAM121051182`
    (Gen 2 JK) where the leading tokens encode voltage + capacity +
    serial. `classify_bms_model` does a longest-prefix match against
    the catalog's `device_name_prefixes` table and returns the
    canonical display string (e.g. "Roamer 12V 200Ah Xtreme"), which
    is what the picker's synth-display pass already prefers over the
    generic profile name once `detected_model` is set.

    The net result: a freshly-discovered pack shows up in the picker
    immediately as "Roamer 12V 200Ah Xtreme" instead of the raw advert
    name and then staying on the generic profile label until after the
    add-then-enrich cycle finishes.

    Gated on advert name shape so this runs only for known Roamer
    families; anything else falls through unchanged.
    """
    advert_name = str(row.get("name") or "").strip()
    if not advert_name:
        return
    # Cheap pre-filter — neither Gen 1 nor Gen 2 Roamers emit any
    # other name shape, so skipping the catalog walk for non-Roamer
    # rows keeps this helper trivially cheap to call unconditionally.
    lower = advert_name.lower()
    if not lower.startswith(("roamer-", "roam")):
        return
    try:
        from .vendors.bms_ble import classify_bms_model
    except ImportError:
        return
    _slug, display, image = classify_bms_model(advert_name)
    if display:
        row["detected_model"] = display
        # Post-add, the BMS coordinator renders the pack as
        # "<classified_display> (#<mac-tag>)" in the backend
        # display_name. Mark the row so the discovery picker uses
        # the same `#MAC` tag format instead of the raw advert name
        # ("ROAM121051182") — otherwise pre-add and post-add show
        # cosmetically different labels for the same device.
        #
        # EXCEPTION: simulator adverts carry a "-SIM" suffix (e.g.
        # "ROAM12105182-SIM"). Operators verifying a sim install
        # need that suffix visible in the picker so they can match
        # against the Sensa Simulator admin's Simulators tab — which
        # shows the raw advert name. Skip the MAC-tag preference for
        # SIM rows so the synth_display falls through to the raw
        # advert name. Real production Roamer adverts don't carry
        # this suffix; their display is unchanged.
        if not advert_name.upper().endswith("-SIM"):
            row["prefer_mac_tag"] = True
    if image:
        row["image"] = image
    # Gen 2 names start with "ROAM" (JK chipset); Gen 1 names start
    # with "Roamer-" (VIP chipset). Stamp integration fields for Gen 2
    # so the Bluetooth picker's `integration: ["sensa_bms_ble"]` filter
    # rule can route the device even when no manufacturer_id is present
    # in the advert (some JK firmware versions omit it).
    if lower.startswith("roam") and not lower.startswith("roamer-"):
        if not row.get("integration"):
            row["integration"] = "sensa_bms_ble"
        if not row.get("vendor_model"):
            row["vendor_model"] = "roamer_gen2"
        if not row.get("bms_family"):
            row["bms_family"] = "jk"


def _annotate_ruuvi_advert(row: dict[str, Any], info: Any) -> None:
    """Tag a BLE row with `ruuvi_mfr_hex` + `detected_model` if Ruuvi.

    Gates on manufacturer_id 0x0499. Stamps the first 8 bytes of the
    manufacturer_data payload as `ruuvi_mfr_hex` for row inspection,
    derives the model from `_ruuvi_model_name`, and emits a one-shot
    INFO dump of the full advert per MAC for new-format/product
    diagnostics. The dump is cheap (one line per device per process)
    and stays useful as Ruuvi adds new data formats — if
    `_ruuvi_model_name` returns "" the dump is the pointer to why.
    """
    raw_mfr = str(row.get("manufacturer_id") or "").lower()
    if raw_mfr != f"0x{_RUUVI_COMPANY_ID:04x}":
        return
    mfr_data = getattr(info, "manufacturer_data", None) or {}
    payload = mfr_data.get(_RUUVI_COMPANY_ID)
    if payload:
        row["ruuvi_mfr_hex"] = payload[:8].hex()
        model_name = _ruuvi_model_name(payload)
        if model_name:
            row["detected_model"] = model_name
    mac_for_dump = str(
        row.get("mac_address") or row.get("address") or ""
    ).upper()
    if mac_for_dump and mac_for_dump not in _LOGGED_RUUVI_RAW_DUMP:
        _LOGGED_RUUVI_RAW_DUMP.add(mac_for_dump)
        mfr_dump = {
            f"0x{cid:04X}": (data.hex() if data else "<empty>")
            for cid, data in sorted(mfr_data.items())
        }
        svc_data = getattr(info, "service_data", None) or {}
        svc_dump = {
            str(uuid): (data.hex() if data else "<empty>")
            for uuid, data in svc_data.items()
        }
        svc_uuids = list(getattr(info, "service_uuids", None) or ())
        _LOGGER.info(
            "Ruuvi advert raw dump mac=%s name=%s manufacturer_data=%s "
            "service_data=%s service_uuids=%s",
            mac_for_dump,
            row.get("name") or "?",
            mfr_dump,
            svc_dump,
            svc_uuids,
        )


def _mark_sim_name(name: str, mac: str | None, *, simulated: bool = False) -> str:
    """Append a visible ` SIM` marker to a simulated device's picker name.

    Why this is needed for BLE at all: a cooperate-flow BLE product (Ruuvi,
    Govee) is adopted by HA's OWN integration, which titles its discovery
    flow from the MAC (`RuuviTag 3942`) and DISCARDS the advert
    `local_name`. The simulator does set a marker in that local_name
    (`SIM-RuuviTag`), but `_discover_native_devices` prefers the native
    title, so the marker never reaches the picker and a fake tag is
    indistinguishable from real hardware sitting next to it. Keying off the
    MAC instead survives that, because the MAC is the one identifier the
    native integration keeps.

    `SIM_MAC_OUI` is the simulator's locally-administered OUI, so this
    cannot false-positive on real kit (see the constant). Idempotent: a name
    that already carries a SIM token is returned unchanged, so this stays
    safe to call on a row whose name came from a source that already marked
    it (an ESPHome MAC-tag suffix, a Roamer `-SIM` advert).

    `simulated` is for a sim with no MAC to key off (a Cerbo found over
    MQTT, keyed on its VRM portal id): the caller has its own tell.
    """
    if not name:
        return name
    mac_hex = str(mac or "").replace(":", "").replace("-", "").upper()
    if not simulated and not mac_hex.startswith(SIM_MAC_OUI):
        return name
    if _has_sim_token(name):
        return name
    return f"{name} SIM"


def _has_sim_token(name: str) -> bool:
    """True when `name` carries a standalone SIM token."""
    # Token match, not a substring — a bare `in` would wrongly consider a
    # product legitimately containing those letters (e.g. "SIMPLE") to be
    # already-marked and silently skip the marker. Split on punctuation as
    # well as whitespace, because the existing markers use both shapes: the
    # ESPHome path appends a bracketed `... #1101 SIM)` while a Roamer advert
    # carries a hyphenated `ROAM12105182-SIM`.
    normalised = name
    for separator in "()[]-_/,":
        normalised = normalised.replace(separator, " ")
    tokens = {part.strip("#").upper() for part in normalised.split()}
    return "SIM" in tokens


def _cerbo_reports_sim(row: dict[str, Any]) -> bool:
    """True for a simulated Cerbo GX.

    A Cerbo over MQTT has no MAC, so the sim OUI never reaches
    `_mark_sim_name`. Its own CustomName (the row's `hostname`, e.g.
    "sensa-home SIM") is the tell instead: a rename in Sensa changes only the
    stored display name, never the Cerbo's CustomName.
    """
    if not str(row.get("discovery_id") or "").startswith("cerbo-"):
        return False
    return _has_sim_token(str(row.get("hostname") or ""))


class SensaBleDevicesSensor(SensorEntity):
    """Sensor exposing BLE devices discovered via the HA bluetooth integration."""

    _attr_has_entity_name = True
    _attr_name = "BLE Devices"
    _attr_icon = "mdi:bluetooth"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "devices"
    _attr_scan_interval = timedelta(seconds=10)

    # The `devices` attribute is the sole payload for the Bluetooth picker card
    # — live data consumed by the frontend, not history. In a busy RF area the
    # advertisement list (every phone/beacon in range, each a ~13-field row)
    # routinely exceeds the recorder's 16 KB per-attribute cap, so the recorder
    # logs a WARNING and drops the attributes every 10 s tick. Tell it to skip
    # these entirely; the frontend still reads them directly off the state.
    # Mirrors the sibling discovery sensor below.
    _unrecorded_attributes = frozenset({"devices"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ble_devices"
        self._attr_native_value = 0
        self._attr_extra_state_attributes = {"devices": []}

    async def async_update(self) -> None:
        """Read current BLE advertisements from the HA bluetooth integration."""
        try:
            from homeassistant.components.bluetooth import (
                async_discovered_service_info,
                async_scanner_count,
            )
        except ImportError:
            self._attr_native_value = 0
            self._attr_extra_state_attributes = {
                "devices": [], "error": "bluetooth integration not available",
            }
            return

        # If no bluetooth source is registered (no USB adapter, no ESPHome
        # proxy, no Sensa Bluetooth Proxy add-on, no Sensa Virtual BLE
        # Scanner attached) then HA cannot reach any real BLE device, so
        # showing simulated / previously-registered BLE rows in the
        # Nomad discovery picker would be misleading — customer would
        # see devices they can't actually add or talk to. The whole BLE
        # list is suppressed in that case: empty native value, empty
        # devices list, plus a flag so the picker can render an explicit
        # "no bluetooth source" empty state instead of just "no devices".
        if async_scanner_count(self.hass) == 0:
            self._attr_native_value = 0
            self._attr_extra_state_attributes = {
                "devices": [], "no_bluetooth_source": True,
            }
            return

        # Cross-reference with the backend device registry (via the main
        # coordinator) so rows we've already registered can be marked as ADDED
        # and show the friendly Sensa display name instead of the raw BLE
        # advertisement name.
        registered_by_mac: dict[str, dict[str, Any]] = {}
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        ignored_macs: set[str] = (runtime or {}).get("ignored_device_ids", set())
        if runtime:
            coordinator = runtime.get("coordinator")
            if coordinator and coordinator.data:
                for device in coordinator.data.get("devices", []) or []:
                    mac = (
                        device.get("linked_mac_address")
                        or device.get("mac_address")
                        or ""
                    )
                    if mac:
                        registered_by_mac[mac.upper()] = device

        infos = async_discovered_service_info(self.hass)
        devices: list[dict[str, Any]] = []
        for info in infos:
            manufacturer, manufacturer_id = _identify_manufacturer(info)
            address = info.address or ""
            ble_name = info.name or "Unknown"
            registered_dev = registered_by_mac.get(address.upper())
            display_name = (
                (registered_dev or {}).get("display_name") or ble_name
            )
            row: dict[str, Any] = {
                "name": ble_name,
                "display_name": display_name,
                "address": address,
                "rssi": info.rssi,
                "source": info.source,
                "manufacturer": manufacturer,
                "manufacturer_id": manufacturer_id,
                # Advertised service UUIDs — REQUIRED for `_match_ble_profile`
                # to match a `service_uuid`-gated profile (the Velit rooftop AC
                # advertises a service UUID + a "KT" name, no manufacturer
                # data). Without this the matcher sees an empty UUID set and the
                # Velit profile never matches, so it never shows in discovery.
                "service_uuids": list(getattr(info, "service_uuids", None) or ()),
                "registered": address.upper() in registered_by_mac,
            }
            _annotate_victron_subtype(row, info)
            _annotate_govee_model(row, info)
            _annotate_roamer_bms_advert(row, info)
            _annotate_dji_power_advert(row, info)
            _annotate_ruuvi_advert(row, info)
            _annotate_tpms_advert(row, info)
            if address.upper() in ignored_macs:
                row["ignored"] = True
            devices.append(row)

        # Stamp profile metadata (integration, bms_family, category, etc.) so
        # the Bluetooth picker's filter rules can match on integration as well as
        # manufacturer_id. Without this, new BMS families (e.g. Fogstar / JBD)
        # that were added after the dashboard was generated fall to Others because
        # their manufacturer_id rule isn't yet in the dashboard YAML — the
        # integration fallback rule covers them once we enrich here.
        from .config_flow import BLE_NATIVE_PROFILES
        for row in devices:
            profile = _match_ble_profile(row, BLE_NATIVE_PROFILES)
            if profile is None:
                continue
            for key in ("integration", "category", "vendor_model", "bms_family", "image", "native_flow_input"):
                val = profile.get(key)
                if val and not row.get(key):
                    row[key] = val
            raw_image = row.get("image", "")
            if raw_image and not str(raw_image).startswith("/"):
                row["image"] = _pick_image(row)

        # Append registered BLE devices that aren't currently advertising so
        # already-added devices appear in the picker immediately without needing
        # a live scan.  These rows are marked registered=True with rssi=None and
        # sort to the bottom via the key below.
        live_macs = {str(d.get("address") or "").upper() for d in devices}
        for mac_upper, reg_dev in registered_by_mac.items():
            if mac_upper in live_macs:
                continue
            address = reg_dev.get("linked_mac_address") or reg_dev.get("mac_address") or ""
            if not address:
                continue
            display_name = reg_dev.get("display_name") or address
            bms_family = (
                reg_dev.get("bms_family")
                or (reg_dev.get("config") or {}).get("bms_family")
                or ""
            )
            image = reg_dev.get("image") or ""
            if image and not str(image).startswith("/"):
                image = f"/local/sensa/images/{image}"
            devices.append({
                "name": display_name,
                "display_name": display_name,
                "address": address,
                "rssi": None,
                "source": None,
                "manufacturer": None,
                "manufacturer_id": None,
                "registered": True,
                "ignored": mac_upper in ignored_macs,
                "integration": reg_dev.get("integration_type") or reg_dev.get("integration") or "",
                "category": reg_dev.get("category") or "",
                "vendor_model": reg_dev.get("vendor_model") or "",
                "bms_family": bms_family,
                "image": image,
            })

        devices.sort(key=lambda d: (d["rssi"] if d["rssi"] is not None else -999), reverse=True)
        self._attr_native_value = len(devices)
        self._attr_extra_state_attributes = {"devices": devices}

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }

    @property
    def should_poll(self) -> bool:
        return True


def _is_van_level_device(device: dict[str, Any] | None) -> bool:
    """Match either a registered device row or an enriched discovery row.

    Enriched DISCOVERY rows carry `vendor_device_type` / `type` (catalog-stamped)
    and `service_class`. REGISTERED rows (from the backend `/devices` list) do
    NOT — registration stores the catalog `vendor_device_type` under `role`, and
    the backend mirrors it to `device_type` (see `register_device` in
    `core/sensa_core/registry/devices.py`). So a registered Van Level appears as
    `device_type == "van_level"` / `role == "van_level"` with none of the
    discovery-row fields — both shapes must be checked or the singleton gate
    can't tell a Van Level is already registered and lets a second one through.
    """
    if not device:
        return False
    for key in ("vendor_device_type", "type", "device_type", "role"):
        if device.get(key) == "van_level":
            return True
    return device.get("service_class") == "levelling"


def _is_ip_camera_device(device: dict[str, Any] | None) -> bool:
    """Match a registered row or an enriched discovery row for an IP camera.

    Same two shapes as `_is_van_level_device`: a discovery row carries the
    catalog `vendor_device_type` / `type` and `service_class`; a registered
    row carries `role` / `device_type` and `integration_type`.
    """
    if not device:
        return False
    for key in ("vendor_device_type", "type", "device_type", "role"):
        if device.get(key) == "ip_camera":
            return True
    if device.get("service_class") == "ip_camera":
        return True
    integration = device.get("integration_type") or device.get("integration") or ""
    return integration == "sensa_rtsp_camera"


# One-per-system devices, with the reason the picker shows on a second one.
# The dashboard and geometry helpers assume a single Van Level; the camera is
# the Pro tier's ONE grey-waste dump camera (Richard, 2026-09-05: "only 1
# camera should be possible, its a bit like van levels"). `add_devices`
# mirrors this table server-side, so a stale page cannot route around it.
_SINGLETON_RULES: tuple[tuple[Any, str], ...] = (
    (_is_van_level_device, "Only one Van Level supported"),
    (_is_ip_camera_device, "Only one camera supported"),
)


def _singleton_gate(
    row: dict[str, Any],
    registered_dev: dict[str, Any] | None,
    existing_devs: list[dict[str, Any]],
) -> dict[str, Any]:
    """The `blocked` / `blocked_reason` fields for an unregistered row whose
    kind is already registered once, else nothing. A registered row is never
    blocked: it stays `registered: True` as normal."""
    if registered_dev:
        return {}
    for matcher, reason in _SINGLETON_RULES:
        if matcher(row) and any(matcher(d) for d in existing_devs):
            return {"blocked": True, "blocked_reason": reason}
    return {}


class SensaDiscoverySensor(SensorEntity):
    """Unified discovery sensor — aggregates every device source into one list.

    Sources merged into `devices` attribute, each row tagged with a `source`:

    - `esphome_http`  — backend ESPHome subnet probe (BT proxies, Sensa ESPs)
    - `dhcp`          — HA DHCP cache (Teltonika routers/switches)
    - `static_probe`  — well-known IPs (Starlink at 192.168.100.1)
    - `native_zeroconf` — HA Zeroconf pending flows (SMLight SLZB)
    - `native_ble`    — HA Bluetooth pending flows (Ruuvi)
    - `ble_advert`    — raw BLE advertisements (BMS packs, unknown BLE)

    Each row carries enough metadata for the unified add service to dispatch
    per source (discovery_id for confirm_device, native_flow_id for
    async_configure, mac_address + bms_family for Sensa-owned BMS registration,
    etc.). The `registered` flag is cross-referenced from the main
    coordinator's backend device list so the picker card can render ADDED
    pills without issuing its own backend calls.
    """

    _attr_has_entity_name = True
    _attr_name = "Discovery"
    _attr_icon = "mdi:magnify"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "devices"

    # The `devices` attribute is the sole payload for the picker card —
    # live data consumed by the frontend, not history. Once a user has more
    # than ~20 devices the JSON routinely exceeds the recorder's 16 KB cap
    # per-attribute limit and triggers a recorder WARNING every tick. Tell
    # the recorder to skip these attributes entirely; the frontend still
    # reads them directly off the state.
    _unrecorded_attributes = frozenset({"devices"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_discovery"
        self._attr_native_value = 0
        self._attr_extra_state_attributes = {"devices": []}
        # Cache of the last-collected source rows (from DHCP, ESPHome
        # scan, static probes, native flows, BLE). Kept so
        # async_quick_refresh() can re-run the cross-reference step
        # against fresh coordinator.data without re-scanning the subnet.
        self._last_source_rows: list[dict[str, Any]] = []
        # In-flight background subnet probe, if any. One at a time: the probe
        # takes ~10 s and the sensor polls every 30 s, so a fresh result always
        # lands before the next poll reads it.
        self._esphome_scan_task: Any = None

    def _kick_esphome_scan(self, client: Any, scan_subnet: str) -> None:
        """Start a backend subnet probe unless one is still running.

        Fire-and-forget: the probe refreshes what the backend holds, and the
        NEXT poll's cheap read picks it up. Nothing here gates the rows this
        poll returns, which is the whole point — a slow probe must not be able
        to empty the picker.
        """
        task = self._esphome_scan_task
        if task is not None and not task.done():
            _LOGGER.debug(
                "Discovery sensor: ESPHome subnet probe still running, "
                "serving cached discoveries this cycle",
            )
            return

        async def _probe() -> None:
            try:
                await client.async_scan(scan_subnet=scan_subnet)
            except Exception:
                _LOGGER.debug(
                    "Discovery sensor: ESPHome subnet probe failed", exc_info=True,
                )

        self._esphome_scan_task = self.hass.async_create_background_task(
            _probe(), name="sensa_nomad_esphome_probe",
        )

    async def async_will_remove_from_hass(self) -> None:
        task = self._esphome_scan_task
        if task is not None and not task.done():
            task.cancel()

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }

    @property
    def should_poll(self) -> bool:
        return True

    async def async_quick_refresh(self) -> None:
        """Re-render using cached source rows + current coordinator.data.

        Skips the slow IO-bound discovery sources (ESPHome subnet scan,
        static probes, etc.) — they haven't changed just because a
        device was registered or removed. The cross-reference and
        registered-device backfill run against fresh coordinator.data,
        which is what determines the `registered: true` flag on each
        row. ~10 ms instead of 3-10 s.

        EXCEPTION: re-read the Zigbee2MQTT source here. It's a synchronous
        HA-registry read (no IO, no subnet scan), and it's the one source
        that genuinely changes on add/remove — a Sensa removal force-removes
        the device from Z2M, a re-add/re-pair re-adopts it. Without this, a
        re-added Z2M device's product image showed as a "?" until the next
        10 s full poll (the reported "image unknown for 10 seconds" lag).
        """
        rows = [dict(r) for r in self._last_source_rows]
        try:
            from .vendors.zigbee2mqtt import discover_zigbee2mqtt_devices

            z2m_rows = discover_zigbee2mqtt_devices(self.hass)
            rows = [r for r in rows if (r.get("source") or "") != "mqtt_zigbee"]
            rows.extend(z2m_rows)
            self._last_source_rows = [dict(r) for r in rows]
        except Exception:  # pragma: no cover - defensive; fall back to cache
            _LOGGER.debug("quick_refresh: Z2M re-read failed", exc_info=True)
        # Same fast HA-registry re-read for the WiCAN OBD2 source — it changes
        # on add/remove like Z2M, so refresh it here too rather than waiting for
        # the next full poll (keeps the product image current).
        try:
            from .vendors.wican_mqtt import discover_wican_mqtt_devices

            wican_rows = discover_wican_mqtt_devices(self.hass)
            rows = [r for r in rows if (r.get("source") or "") != "wican_mqtt"]
            rows.extend(wican_rows)
            self._last_source_rows = [dict(r) for r in rows]
        except Exception:  # pragma: no cover - defensive; fall back to cache
            _LOGGER.debug("quick_refresh: WiCAN re-read failed", exc_info=True)
        self._render_rows(rows)
        self.async_write_ha_state()

    def _retry_shelly_adoption(
        self, runtime: dict[str, Any], all_rows: list[dict[str, Any]]
    ) -> None:
        """Finish adopting a registered Shelly that HA never got an entry for.

        The add path adopts once, straight away. A battery Shelly (the Plus
        Smoke) is usually back asleep by then: the sweep saw it awake, the
        customer pressed Add a little later, and HA's flow failed with
        `cannot_connect`. Sensa's row exists, HA's does not, and the device
        has no entities, no Security tile and no smoke alarm. Seen on a
        customer box 2026-10-04.

        So every poll asks each such Shelly directly, at the address the sweep
        last saw it on or else the one it was registered with, and adopts the
        moment it answers `/shelly` with its own MAC. Not the subnet sweep: on
        a box with two /24s that sweep can run past its budget and return
        nothing, and a detector woken by its button is only awake for a minute
        or two. One GET to one address per poll is cheap. One attempt in flight
        per device, and a cool-down after an adopt so a Shelly that is awake
        but refuses the flow (auth enabled) logs every few minutes, not every
        poll.
        """
        import time as _time

        from .vendors.shelly import shelly_entry_ids

        coordinator = runtime.get("coordinator")
        devices = (
            (coordinator.data or {}).get("devices", []) if coordinator else []
        ) or []
        swept_ip: dict[str, str] = {}
        for row in all_rows:
            if (row.get("integration") or "") != "shelly":
                continue
            mac = _mac_key(row.get("mac_address"))
            ip = str(row.get("ip_address") or "").strip()
            if mac and ip:
                swept_ip[mac] = ip

        in_flight: set[str] = getattr(self, "_shelly_adopting", None) or set()
        self._shelly_adopting = in_flight
        last_try: dict[str, float] = getattr(self, "_shelly_adopt_tried", None) or {}
        self._shelly_adopt_tried = last_try
        now = _time.monotonic()

        for dev in devices:
            integration = (dev.get("integration_type") or dev.get("integration") or "")
            if integration != "shelly":
                continue
            did = str(dev.get("device_id") or "")
            mac = _mac_key(dev.get("linked_mac_address") or dev.get("mac_address"))
            if not did or not mac or did in in_flight:
                continue
            if now - last_try.get(did, float("-inf")) < _SHELLY_ADOPT_RETRY_S:
                continue
            ip = swept_ip.get(mac) or str(dev.get("ip_address") or "").strip()
            if not ip:
                continue
            if shelly_entry_ids(self.hass, dev):
                continue

            in_flight.add(did)

            async def _probe_and_adopt(
                did: str = did, dev: dict[str, Any] = dev, ip: str = ip,
                mac: str = mac,
            ) -> None:
                try:
                    if not await _shelly_answers(self.hass, ip, mac):
                        return  # asleep: ask again next poll
                    from . import _async_adopt_shelly

                    _LOGGER.info(
                        "Shelly adopt: %s is registered but has no HA entry and "
                        "just answered at %s; retrying the adopt", did, ip,
                    )
                    last_try[did] = _time.monotonic()
                    await _async_adopt_shelly(self.hass, {
                        "display_name": dev.get("display_name") or "",
                        "ip_address": ip,
                        "mac_address": (
                            dev.get("linked_mac_address")
                            or dev.get("mac_address") or ""
                        ),
                        "hostname": dev.get("linked_hostname") or "",
                    })
                finally:
                    in_flight.discard(did)

            self.hass.async_create_task(_probe_and_adopt())

    def _reconcile_vendor_hosts(
        self, runtime: dict[str, Any], all_rows: list[dict[str, Any]]
    ) -> None:
        """Heal a registered device whose LAN IP changed.

        Generalised from the Cerbo-only version. A live scan re-finds the same
        physical device at its new address, so when the discovered IP differs
        from the stored one we fire the existing `configure_device` service: it
        persists the new host and restarts the vendor coordinator, with no
        remove-and-re-add by the customer.

        **Anchoring differs per vendor and that is the whole trick.** The Cerbo
        is keyed on its VRM portal id (`cerbo-{portal}`), so its discovery row's
        `discovery_id` IS the registered `device_id`. A Teltonika is discovered
        from HA's DHCP cache, where the row id is `disc-{mac}` and matches no
        registered id, so it anchors on the MAC instead. Anchoring the router on
        its id would silently never match and the whole thing would look like it
        worked.

        Legacy Cerbos still keyed on `static-{ip}` never match (their device_id
        is the old IP), so they're left for a remove/re-add, by design: their
        identity cannot survive an address change anyway.
        """
        coordinator = runtime.get("coordinator")
        devices = (
            (coordinator.data or {}).get("devices", []) if coordinator else []
        ) or []
        by_id = {str(d.get("device_id")): d for d in devices if d.get("device_id")}
        by_mac: dict[str, dict[str, Any]] = {}
        for d in devices:
            for key in ("linked_mac_address", "mac_address"):
                mac = _mac_key(d.get(key))
                if mac:
                    by_mac.setdefault(mac, d)
        if not by_id:
            return

        # Per-instance guard so a fire-and-forget service call that hasn't yet
        # refreshed coordinator.data doesn't re-trigger every 30s poll until the
        # backend echoes the new host back.
        seen: dict[str, str] = getattr(self, "_cerbo_hosts_seen", None) or {}
        self._cerbo_hosts_seen = seen

        # A device reachable at two addresses at once (e.g. a stale DHCP lease
        # for a now-disabled WiFi interface alongside the live wired IP) makes
        # discovery flap between them every scan. Following each flap fires
        # `configure_device`, which TEARS DOWN AND RECREATES the vendor
        # coordinator (~every 30s), stranding every entity of that device on the
        # dead coordinator so the whole dashboard freezes. So: never reconcile a
        # coordinator that is still working at its current host — only chase a
        # new IP when the current connection is genuinely dead.
        vendor_coords = runtime.get("vendor_coordinators") or {}
        healthy_window = 60.0

        for row in all_rows:
            integration = row.get("integration") or ""
            label = _HOST_RECONCILE_VENDORS.get(integration)
            if label is None:
                continue
            new_ip = str(row.get("ip_address") or "").strip()
            if not new_ip:
                continue

            # Cerbo: the row id IS the registered id. Teltonika: it is not, so
            # match the MAC. See the docstring.
            dev = by_id.get(str(row.get("discovery_id") or ""))
            if dev is None:
                dev = by_mac.get(_mac_key(row.get("mac_address")))
            if dev is None:
                continue
            did = str(dev.get("device_id") or "")
            if not did:
                continue

            cfg = dict(dev.get("config") or {})
            current_host = str(cfg.get("host") or "").strip()
            if current_host == new_ip or seen.get(did) == new_ip:
                continue

            # If the coordinator is still working at its current address, the
            # existing connection is fine — leave it alone rather than thrash it
            # for a flapping or stale discovered IP.
            coord = vendor_coords.get(did)
            if coord is not None and _coordinator_is_live(coord, healthy_window):
                _LOGGER.debug(
                    "%s %s discovered at %s but the current connection (%s) "
                    "is live; not reconciling host (avoids thrashing a working "
                    "coordinator on a flapping IP)",
                    label, did, new_ip, current_host or "<unset>",
                )
                continue

            seen[did] = new_ip
            _LOGGER.info(
                "%s %s IP changed %s -> %s — updating host + reconnecting",
                label, did, current_host or "<unset>", new_ip,
            )
            new_cfg = {**cfg, "host": new_ip}
            self.hass.async_create_task(
                self.hass.services.async_call(
                    DOMAIN,
                    "configure_device",
                    {"device_id": did, "config": new_cfg},
                    blocking=False,
                )
            )

    async def async_update(self) -> None:
        """Run every discovery source in parallel, merge, dedup, cross-ref registered.

        All five sources (backend ESPHome scan, DHCP cache, static IP
        probes, Cerbo subnet scan, native pending flows) are mutually
        independent so we launch them concurrently. Each is wrapped in
        its own guard — a single source raising or timing out never
        silences the rest, and the per-source wall-clock lands in the
        debug log so "my scan is slow" can be pinned to the exact
        offender.
        """
        import asyncio as _asyncio
        import time as _time

        from .config_flow import (
            BLE_NATIVE_PROFILES,
            CERBO_SCAN_BUDGET_S,
            _discover_dhcp_devices,
            _discover_esphome_devices,
            _discover_http_probe,
            _discover_native_devices,
            _discover_rtsp_probe,
            _discover_static_devices,
            _discover_tcp_subnet,
            discover_cerbo_keeping_last,
            experimental_devices_enabled,
            native_profiles,
        )

        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        client = runtime.get("client")
        # Re-resolved here rather than read from the setup-time cache, so a van
        # that changes network (hotspot -> Starlink) starts probing the new range
        # on its own instead of scanning the boot-time one until a reload. The
        # helper short-circuits on a customer override and throttles the adapter
        # enumeration; see `async_refresh_scan_subnet`.
        from . import (
            async_refresh_scan_subnet,
        )

        scan_subnet = (
            await async_refresh_scan_subnet(self.hass, self._entry, runtime)
            or DEFAULT_SCAN_SUBNET
        )
        all_rows: list[dict[str, Any]] = []

        async def _timed(
            label: str, coro, *, timeout: float,
        ) -> list[dict[str, Any]]:
            """Run one source coroutine with a hard wall-clock bound
            and a per-source log line. Returns [] on failure so the
            top-level merge never has to care which source blew up."""
            start = _time.monotonic()
            try:
                result = await _asyncio.wait_for(coro, timeout=timeout)
            except _asyncio.TimeoutError:
                # Best-effort source: the discovery sensor returns [] for it and
                # the others still render, so a slow/unavailable source (e.g. the
                # ESPHome scan when the add-on is stopped) is DEBUG, not a
                # WARNING repeated every 30s poll.
                _LOGGER.debug(
                    "Discovery sensor: %s exceeded %ss budget — skipped",
                    label, timeout,
                )
                return []
            except Exception:
                _LOGGER.debug(
                    "Discovery sensor: %s failed after %.2fs",
                    label, _time.monotonic() - start, exc_info=True,
                )
                return []
            _LOGGER.debug(
                "Discovery sensor: %s took %.2fs (%d rows)",
                label, _time.monotonic() - start, len(result or []),
            )
            return list(result or [])

        async def _native_primary() -> list[dict[str, Any]]:
            # `native_profiles` re-reads the Experimental Devices switch; the
            # frozen `PRIMARY_NATIVE_PROFILES` constant cannot see it, so an
            # experimental cooperate-flow device would never appear.
            rows = _discover_native_devices(self.hass, native_profiles(self.hass))
            for row in rows:
                row["source"] = "native_zeroconf"
            return rows

        async def _native_ble() -> list[dict[str, Any]]:
            # Same gate as everywhere else: no bluetooth source means no
            # BLE rows, even those coming from HA's native flow registry
            # (pending victron_ble / ruuvitag_ble / govee_ble flows).
            # Without this, registered native-BLE devices and any
            # pending discovery flows would still surface in the picker
            # after the Sensa Bluetooth Proxy add-on was uninstalled.
            try:
                from homeassistant.components.bluetooth import (
                    async_scanner_count,
                )
                if async_scanner_count(self.hass) == 0:
                    return []
            except ImportError:
                return []
            rows = _discover_native_devices(self.hass, BLE_NATIVE_PROFILES)
            for row in rows:
                row["source"] = "native_ble"
            return rows

        # DHCP is a pure dict read, no IO — don't spin up a task for it.
        try:
            all_rows.extend(_discover_dhcp_devices(self.hass))
        except Exception:
            _LOGGER.warning("Discovery sensor: DHCP source failed", exc_info=True)

        # Zigbee2MQTT devices are already-adopted HA devices (owned by HA's
        # shared `mqtt` integration); enumerating them is a synchronous registry
        # read, like DHCP — no task needed.
        try:
            from .vendors.zigbee2mqtt import discover_zigbee2mqtt_devices
            all_rows.extend(discover_zigbee2mqtt_devices(self.hass))
        except Exception:
            _LOGGER.warning(
                "Discovery sensor: Zigbee2MQTT source failed", exc_info=True,
            )

        # WiCAN Pro OBD2 — same shape as Zigbee2MQTT: an already-adopted HA
        # device owned by HA's shared `mqtt` integration, enumerated by a
        # synchronous registry read.
        try:
            from .vendors.wican_mqtt import discover_wican_mqtt_devices
            all_rows.extend(discover_wican_mqtt_devices(self.hass))
        except Exception:
            _LOGGER.warning(
                "Discovery sensor: WiCAN OBD2 source failed", exc_info=True,
            )

        # Kick every async source off at once. Per-source timeouts cap
        # the whole cycle at the largest budget — no source can drag
        # the overall update past its own bound, and the slow sources
        # don't block the fast ones. Budgets are deliberately tight:
        # the discovery sensor polls every 30 s, so holding the scan
        # open for longer buys nothing but stale data.
        source_tasks = []
        if client is not None:
            # Read what the backend already holds (milliseconds). The subnet
            # probe that refreshes it runs in the background — see
            # `_kick_esphome_scan`. Awaiting the probe here used to blow the
            # 8 s budget on any populated LAN (a /24 walk measures ~10 s), and
            # a timed-out source contributes NO rows, so every un-added ESPHome
            # device blinked out of the picker on most polls while registered
            # ones (sourced from the coordinator) stayed put.
            self._kick_esphome_scan(client, scan_subnet)
            source_tasks.append(_timed(
                "esphome_discoveries_read",
                _discover_esphome_devices(
                    self.hass, client, scan_subnet, rescan=False,
                ),
                timeout=8.0,
            ))
        source_tasks.extend([
            _timed(
                "static_probes",
                _discover_static_devices(experimental_devices_enabled(self.hass)),
                timeout=6.0,
            ),
            _timed(
                "cerbo_subnet_scan",
                discover_cerbo_keeping_last(scan_subnet),
                # The scan keeps its own time, and answers when it runs out.
                timeout=CERBO_SCAN_BUDGET_S + 1,
            ),
            # Port-only devices (the Dongle-M's raw 6638 socket). It walks the
            # same IP list as the Cerbo sweep, and a timed-out source contributes
            # no rows, so it must not be tighter or the coordinator would blink
            # out of the picker on a busy scan.
            _timed(
                "tcp_subnet_scan",
                _discover_tcp_subnet(
                    self.hass,
                    scan_subnet,
                    experimental_devices_enabled(self.hass),
                ),
                timeout=7.0,
            ),
            # Devices identified by what an HTTP endpoint ANSWERS (the Lucky
            # Miner's AxeOS API). Walks the same IP list as the two sweeps
            # above, so it gets the same budget: a timed-out source contributes
            # NO rows, and a tighter budget would blink the miners out of the
            # picker on a busy scan.
            _timed(
                "http_probe_scan",
                _discover_http_probe(
                    self.hass,
                    scan_subnet,
                    experimental_devices_enabled(self.hass),
                ),
                timeout=7.0,
            ),
            # Devices that ANSWER RTSP (IP cameras). Same IP walk and the same
            # budget as the sweeps above, for the same reason.
            _timed(
                "rtsp_probe_scan",
                _discover_rtsp_probe(
                    self.hass,
                    scan_subnet,
                    experimental_devices_enabled(self.hass),
                ),
                timeout=7.0,
            ),
            _timed("native_zeroconf", _native_primary(), timeout=2.0),
            _timed("native_ble", _native_ble(), timeout=2.0),
        ])

        start = _time.monotonic()
        source_results = await _asyncio.gather(
            *source_tasks, return_exceptions=False,
        )
        for rows in source_results:
            all_rows.extend(rows)
        _LOGGER.debug(
            "Discovery sensor: all async sources finished in %.2fs (%d rows total)",
            _time.monotonic() - start, len(all_rows),
        )

        # Self-heal a Cerbo whose LAN IP moved (manual reconfigure / expired
        # DHCP lease). Its portal-id identity means the live scan re-finds it as
        # the same device; here we push the new IP into the backend config.host
        # and reconnect the coordinator so data resumes without a remove/re-add.
        self._reconcile_vendor_hosts(runtime, all_rows)
        self._retry_shelly_adoption(runtime, all_rows)

        # 5. Raw BLE advertisements — reuse what SensaBleDevicesSensor already
        #    collects by reading HA's bluetooth state. Only include rows that
        #    aren't already represented by one of the above sources (to avoid
        #    listing a Sensa BT proxy twice: once as esphome_http and once as
        #    ble_advert). Prefer infrastructure rows over ad-hoc BLE.
        ble_rows = await self._collect_ble_rows()

        # Native rows (e.g. Ruuvi via ruuvitag_ble) don't know the BLE
        # manufacturer_id — that metadata only lives on the raw advertisement.
        # The picker filter rules key on manufacturer_id, so without this
        # merge the native row renders without a category pill and isn't
        # addable. Copy manufacturer_id / manufacturer / rssi from the ble
        # advert onto its native sibling before we decide whether to keep
        # the advert row standalone.
        ble_by_canonical: dict[str, dict[str, Any]] = {}
        for row in ble_rows:
            mac = _canonical_mac(row.get("mac_address") or row.get("address"))
            if mac:
                ble_by_canonical[mac] = row

        for row in all_rows:
            mac = _canonical_mac(row.get("mac_address") or row.get("address"))
            if not mac:
                continue
            ble = ble_by_canonical.get(mac)
            if not ble:
                continue
            if not row.get("manufacturer_id"):
                row["manufacturer_id"] = ble.get("manufacturer_id", "")
            if not row.get("manufacturer"):
                row["manufacturer"] = ble.get("manufacturer", "")
            if row.get("rssi") is None:
                row["rssi"] = ble.get("rssi")
            # Pre-registration product identifiers stamped by
            # `_annotate_victron_subtype` on the raw BLE advert row only.
            # Native_ble / native_zeroconf rows from `_discover_native_devices`
            # never see the manufacturer_data bytes, so they hit `_render_rows`
            # without device_subtype and the picker's
            # `device_subtype: ["smartshunt"]` filter rule misses — SmartShunt
            # ends up greyed out in Others. Copy through here so a SmartShunt
            # discovered via a pending native victron_ble flow gets the same
            # subtype its raw-advert sibling already carries.
            if not row.get("device_subtype") and ble.get("device_subtype"):
                row["device_subtype"] = ble["device_subtype"]
            if not row.get("victron_model_id") and ble.get("victron_model_id"):
                row["victron_model_id"] = ble["victron_model_id"]
            if not row.get("victron_mfr_hex") and ble.get("victron_mfr_hex"):
                row["victron_mfr_hex"] = ble["victron_mfr_hex"]
            if not row.get("ruuvi_mfr_hex") and ble.get("ruuvi_mfr_hex"):
                row["ruuvi_mfr_hex"] = ble["ruuvi_mfr_hex"]
            if not row.get("detected_model") and ble.get("detected_model"):
                row["detected_model"] = ble["detected_model"]

        known_canonical_macs = {
            _canonical_mac(row.get("mac_address") or row.get("address"))
            for row in all_rows
        }
        known_canonical_macs.discard("")
        # A Cerbo GX found via MQTT-on-LAN carries no MAC (it's keyed on its VRM
        # portal id), but that portal id IS the device's MAC and the Cerbo also
        # beacons over BLE. Seed the portal MAC so the Cerbo's OWN BLE advert is
        # recognised as already represented and dropped below — otherwise it
        # shows as a second "Victron Cerbo GX <mac>" row alongside the added one.
        for row in all_rows:
            portal_mac = _cerbo_portal_canonical_mac(row.get("discovery_id"))
            if portal_mac:
                known_canonical_macs.add(portal_mac)
        for row in ble_rows:
            mac = _canonical_mac(row.get("mac_address") or row.get("address"))
            if mac and mac in known_canonical_macs:
                continue
            all_rows.append(row)
            if mac:
                known_canonical_macs.add(mac)

        # Backstop Victron annotation: a SmartShunt discovered through HA's
        # native pending victron_ble flow (`source == "native_ble"`) never
        # passes through `_collect_ble_rows`, so it doesn't carry the
        # device_subtype that `_annotate_victron_subtype` would otherwise
        # stamp from the manufacturer_data bytes — and the row from
        # `_discover_native_devices` doesn't even carry a manufacturer_id.
        # The earlier merge step copies both from a raw-advert sibling if
        # one exists in this tick, but if not (BT advert hasn't been seen
        # recently / the proxy hasn't refreshed it) the row hits the
        # downstream profile-enrichment loop with both fields empty and
        # `_match_ble_profile` returns None — the row never picks up
        # `native_flow_input`, the picker doesn't show the encryption-key
        # prompt, and the customer's SmartShunt registers in our backend
        # but the native victron_ble flow stays stalled. Run the backstop
        # BEFORE the enrichment loop so a backstop-annotated row still
        # collects native_flow_input from the matching profile.
        try:
            from homeassistant.components.bluetooth import (
                async_last_service_info,
            )
        except ImportError:
            async_last_service_info = None
        if async_last_service_info is not None:
            # Uppercase hex, matching `_identify_manufacturer`'s
            # `f"0x{company_id:04X}"` format. The picker's filter rule
            # uses `manufacturer_id: ["0x02E1"]` and matches with a
            # case-sensitive `.includes()` — writing lowercase here
            # silently breaks the rule even though everything else is
            # right. Always emit uppercase.
            victron_mfr_hex = f"0x{_VICTRON_COMPANY_ID:04X}"
            for row in all_rows:
                if row.get("device_subtype"):
                    continue
                # Trigger for any row that smells like Victron — either we
                # already know the mfr_id (raw advert / sibling-merged) or
                # the native side told us via integration / vendor_model.
                raw_mfr = str(row.get("manufacturer_id") or "").upper()
                integration = str(row.get("integration") or "")
                vendor_model = str(row.get("vendor_model") or "")
                looks_victron = (
                    raw_mfr == victron_mfr_hex
                    or integration == "victron_ble"
                    or vendor_model.startswith("victron")
                )
                if not looks_victron:
                    continue
                mac = row.get("mac_address") or row.get("address") or ""
                hex_mac = str(mac).upper().replace("-", "").replace(":", "")
                if len(hex_mac) != 12:
                    continue
                colonised = ":".join(
                    hex_mac[i:i + 2] for i in range(0, 12, 2)
                )
                try:
                    info = async_last_service_info(self.hass, colonised, False)
                except Exception:  # noqa: BLE001
                    info = None
                if info is None:
                    continue
                # Backfill manufacturer_id from the BLE service info if the
                # row didn't carry one — `_annotate_victron_subtype` keys on
                # `row["manufacturer_id"]`, so without this the annotation
                # is a no-op. Also keeps the picker filter rule (which also
                # requires `manufacturer_id == "0x02E1"`) able to match.
                if not raw_mfr:
                    mfr_data = getattr(info, "manufacturer_data", None) or {}
                    if _VICTRON_COMPANY_ID in mfr_data:
                        row["manufacturer_id"] = victron_mfr_hex
                _annotate_victron_subtype(row, info)

        # Backstop Ruuvi annotation — same shape as the Victron backstop
        # above, but for ruuvitag_ble. A Ruuvi tag discovered via HA's
        # pending native ruuvitag_ble flow (or already registered — they
        # stay in the picker as the "Added" pill) never passes through
        # `_collect_ble_rows`, so its row carries no `detected_model` /
        # `ruuvi_mfr_hex`. Without the backstop, the 2in1/3in1/4in1
        # variant can't be shown on the picker's detail row and the
        # image lookup falls back to the generic ruuvitag.webp.
        if async_last_service_info is not None:
            ruuvi_mfr_hex = f"0x{_RUUVI_COMPANY_ID:04X}"
            for row in all_rows:
                if row.get("detected_model"):
                    continue
                raw_mfr = str(row.get("manufacturer_id") or "").upper()
                integration = str(row.get("integration") or "")
                vendor_model = str(row.get("vendor_model") or "")
                looks_ruuvi = (
                    raw_mfr == ruuvi_mfr_hex
                    or integration == "ruuvitag_ble"
                    or vendor_model == "ruuvitag"
                )
                if not looks_ruuvi:
                    continue
                mac = row.get("mac_address") or row.get("address") or ""
                hex_mac = str(mac).upper().replace("-", "").replace(":", "")
                if len(hex_mac) != 12:
                    continue
                colonised = ":".join(
                    hex_mac[i:i + 2] for i in range(0, 12, 2)
                )
                try:
                    info = async_last_service_info(self.hass, colonised, False)
                except Exception:  # noqa: BLE001
                    info = None
                if info is None:
                    _maybe_log_ruuvi_backstop_miss(colonised, "last_service_info=None")
                    # Fallback: scan the full discovered-service-info list
                    # for a matching MAC. `async_last_service_info` returns
                    # None if the tag's last advert was non-connectable
                    # and the internal cache key mismatches; iterating the
                    # discovered-info view covers that gap.
                    info = _find_discovered_info(self.hass, colonised)
                    if info is None:
                        _maybe_log_ruuvi_backstop_miss(colonised, "discovered_service_info=no-match")
                        continue
                mfr_data_dbg = getattr(info, "manufacturer_data", None) or {}
                if _RUUVI_COMPANY_ID not in mfr_data_dbg:
                    _maybe_log_ruuvi_backstop_miss(
                        colonised,
                        f"no-0x0499-in-mfr-data keys={list(mfr_data_dbg.keys())}",
                    )
                    continue
                if not mfr_data_dbg.get(_RUUVI_COMPANY_ID):
                    _maybe_log_ruuvi_backstop_miss(colonised, "empty-0x0499-payload")
                    continue
                if not raw_mfr:
                    row["manufacturer_id"] = ruuvi_mfr_hex
                _annotate_govee_model(row, info)
                _annotate_roamer_bms_advert(row, info)
                _annotate_dji_power_advert(row, info)
                _annotate_ruuvi_advert(row, info)
                _annotate_tpms_advert(row, info)
                if not row.get("detected_model"):
                    _maybe_log_ruuvi_backstop_miss(
                        colonised,
                        f"annotate-produced-no-model payload={mfr_data_dbg[_RUUVI_COMPANY_ID].hex()}",
                    )

        # Enrich BLE rows against LEVEL2 profiles so that ble_advert rows pick
        # up `bms_family` / `integration` / `category` / `vendor_model` /
        # `native_flow_input`, and so that already-enriched native rows don't
        # lose their per-profile tags on the trip through the picker. Without
        # this, Roamer advert rows hit the dispatch without a bms_family and
        # are refused as "unsupported BLE advert"; SmartShunt rows hit add
        # without an `access_token` descriptor and the picker can't prompt
        # for the encryption key.
        for row in all_rows:
            if (row.get("source") or "") not in ("ble_advert", "native_ble"):
                continue
            profile = _match_ble_profile(row, BLE_NATIVE_PROFILES)
            if profile is None:
                continue
            for key in (
                "integration", "category", "vendor_model", "bms_family",
                "image", "native_flow_input",
            ):
                value = profile.get(key)
                if value and not row.get(key):
                    row[key] = value
            device_type = profile.get("device_type")
            if device_type and not row.get("vendor_device_type"):
                row["vendor_device_type"] = device_type
            # Only override a meaningless name. Keep the real BLE advertised
            # name when we have one — it often carries the serial / capacity.
            if profile.get("name") and (
                not row.get("name") or row.get("name") == "Unknown"
            ):
                row["name"] = profile["name"]

        # Source collection finished. Cache before the (fast) render
        # phase so async_quick_refresh() can re-render with fresh
        # coordinator.data after an add/remove without re-running the
        # subnet scan.
        self._last_source_rows = [dict(r) for r in all_rows]
        # Resolve the HA host's LAN IP (async-safe) so the Sensa Core self-row
        # shows a real address rather than the Supervisor container hostname.
        # Cached on self; _render_rows reads it. Best-effort — "" ⇒ host fallback.
        self._core_host_ip = await _resolve_host_ip(self.hass)
        self._render_rows(all_rows)

    def _render_rows(self, source_rows: list[dict[str, Any]]) -> None:
        """Enrich, cross-reference, and backfill — the fast render phase.

        Takes source rows (as collected in async_update) and produces
        the sensor's state + `devices` attribute. Runs against current
        coordinator.data every time, so adding / removing a device and
        calling this method is enough to flip the picker's registered
        flag on / off without the subnet scan.
        """
        from .config_flow import (
            BLE_NATIVE_PROFILES,
            _build_discovery_name,
            _enrich_from_catalog,
            experimental_devices_enabled,
        )

        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        ignored_device_ids: set[str] = runtime.get("ignored_device_ids", set())

        # Cross-reference against the backend-registered devices so the picker
        # can render ADDED pills + pre-check the registered flag. Match in
        # priority order: canonical MAC → discovery_id → IP → integration type.
        # Built BEFORE enrichment: the experimental gate below needs to know
        # whether a row is already added, and these lookups read `runtime` only.
        registered_by_mac = _registered_macs_by_mac(runtime)
        registered_by_discovery_id = _registered_discoveries_by_id(runtime)
        registered_by_ip = _registered_devices_by_ip(runtime)
        registered_by_integration = _registered_devices_by_integration(runtime)
        # The router a managed switch's Config Network tile opens.
        network_router = _network_router(
            ((runtime.get("coordinator") and runtime["coordinator"].data) or {}).get("devices") or []
        )

        def _already_added(row: dict[str, Any]) -> bool:
            """True when this row is a device the customer has already added.

            Matches on the identifiers that exist BEFORE catalog enrichment.
            `integration` is deliberately not used — it is itself stamped by
            enrichment, so keying on it would be circular.

            MAC and discovery_id identify a device outright. An IP does NOT: it
            is a lease. DHCP reassigns it, and a hardware swap leaves the old
            registration holding it — so an unadded experimental row that
            inherits a registered device's address must never be mistaken for
            "added" and enriched, which would surface a customer-unready device
            in the picker (the exact leak this gate exists to prevent).

            So IP is honoured only when it is the ONLY identity either side has
            (a MAC-less row matching a MAC-less registration) AND the device
            TYPE corroborates. A row carrying a MAC never reaches the IP branch
            at all; a MAC-less row landing on another kind of device's old
            address is rejected on type. An added IP-only device still keeps its
            catalog. This holds without relying on "every experimental catalog
            entry has a MAC", which is true today but is not a guarantee.
            """
            canonical = _canonical_mac(row.get("mac_address") or row.get("address"))
            if canonical and canonical in registered_by_mac:
                return True
            disc_id = str(row.get("discovery_id") or "")
            if disc_id and disc_id in registered_by_discovery_id:
                return True
            if canonical:
                return False  # carries an identity; an IP adds nothing but risk
            row_ip = row.get("ip_address") or ""
            dev = registered_by_ip.get(row_ip) if row_ip else None
            if dev is None:
                return False
            # The registered device must itself be IP-only — otherwise its IP is
            # not its identity and a squatter could borrow it.
            if _canonical_mac(dev.get("linked_mac_address") or dev.get("mac_address")):
                return False
            # And the row must plausibly BE that device. Without a MAC or a
            # discovery_id an IP alone cannot tell "this device" from "a
            # different device that now holds its old address", so require the
            # device TYPE to corroborate. A MAC-less experimental row landing on
            # a MAC-less registration of another kind (a static-probe Starlink,
            # say) is therefore still hidden.
            def _kind(d: dict[str, Any]) -> str:
                return str(
                    d.get("vendor_device_type") or d.get("device_type") or ""
                ).strip().lower()

            row_kind = _kind(row)
            return bool(row_kind) and row_kind == _kind(dev)

        # Enrich each row from the device catalog so `integration` / `category`
        # are populated consistently. An `experimental: true` entry only matches
        # when the Experimental Devices switch is on; otherwise the row stays
        # unenriched and lands in the hidden Others group.
        #
        # EXCEPT for a device the customer has ALREADY ADDED. The experimental
        # gate exists to keep a not-yet-ready device out of DISCOVERY — it must
        # never strip the catalog off a device that is already registered and
        # running, which would silently drop its `catalog_id` / `integration` /
        # service class and with them every catalog-driven control on its row
        # (the Autoterm "Show on Home Layout" toggle, the FreshJet "Pair"
        # button). An added device keeps its identity whatever the switch says.
        include_experimental = experimental_devices_enabled(self.hass)
        enriched = [
            _enrich_from_catalog(r, include_experimental or _already_added(r))
            for r in source_rows
        ]

        # Singleton policy — Van Level is currently supported as one-per-system.
        # The dashboard / geometry helpers assume a single levelling device; a
        # second one would silently overwrite shared state. So once one is
        # registered, the picker gates any other van_level row with a tooltip
        # and a disabled checkbox (rendered below); the add_devices service
        # also rejects it server-side for defence in depth.
        coord_for_singleton = runtime.get("coordinator")
        existing_devs = (
            (coord_for_singleton.data or {}).get("devices", [])
            if coord_for_singleton else []
        ) or []

        # Per-device `last_seen_utc` from the backend discovery registry (it
        # stamps it on every scan upsert). The picker uses this as the
        # reachability fallback for pure-scan devices not yet in HA (no live
        # entity to read). Keyed by discovery_id and canonical MAC.
        discoveries = (
            (coord_for_singleton.data or {}).get("discoveries", [])
            if coord_for_singleton else []
        ) or []
        last_seen_by_disc_id: dict[str, str] = {}
        last_seen_by_mac: dict[str, str] = {}
        for _d in discoveries:
            _ls = _d.get("last_seen_utc")
            if not _ls:
                continue
            _did = str(_d.get("discovery_id") or "")
            if _did:
                last_seen_by_disc_id[_did] = _ls
            _nmac = _canonical_mac(
                _d.get("normalized_mac") or _d.get("mac_address"),
            )
            if _nmac:
                last_seen_by_mac[_nmac] = _ls

        # Resolve the HA device->live-entities lookup ONCE for this render; each
        # registered row is then a dict lookup, not a full registry walk.
        liveness_index = _build_liveness_index(self.hass)

        rendered: list[dict[str, Any]] = []
        seen_row_keys: set[str] = set()
        seen_canonical_macs: set[str] = set()
        seen_discovery_ids: set[str] = set()
        # device_ids of registered devices already rendered via a live discovery
        # row. The backfill pass skips these so a device whose IP just changed
        # (live row at the new IP + a registered record still holding the old IP)
        # isn't listed twice — the IP-keyed backfill dedup can't catch that case.
        matched_registered_ids: set[str] = set()
        unclassed_records: list[tuple[dict[str, Any], str]] = []
        for row in enriched:
            row_id = self._row_id(row)
            canonical = _canonical_mac(row.get("mac_address") or row.get("address"))
            disc_id = str(row.get("discovery_id") or "")

            # Skip if we've already rendered a row with the same canonical MAC
            # OR the same discovery_id. This catches the "Roamer appears 20
            # times" pattern where accumulated pending native flows each emit
            # a row for the same physical device.
            if canonical and canonical in seen_canonical_macs:
                continue
            if disc_id and disc_id in seen_discovery_ids:
                continue
            if row_id in seen_row_keys:
                continue
            seen_row_keys.add(row_id)
            if canonical:
                seen_canonical_macs.add(canonical)
            if disc_id:
                seen_discovery_ids.add(disc_id)

            row_ip = row.get("ip_address") or ""
            row_integration = row.get("integration") or ""
            registered_dev = None
            if canonical:
                registered_dev = registered_by_mac.get(canonical)
            if not registered_dev:
                registered_dev = registered_by_discovery_id.get(disc_id)
            if not registered_dev and row_ip:
                registered_dev = registered_by_ip.get(row_ip)
            if not registered_dev and row_integration:
                registered_dev = registered_by_integration.get(row_integration)

            if registered_dev:
                _matched_id = str(registered_dev.get("device_id") or "")
                if _matched_id:
                    if _matched_id in matched_registered_ids:
                        # Another live discovery row this cycle already rendered
                        # this registered device — e.g. a Cerbo found via
                        # MQTT-on-LAN (no MAC, keyed on portal id) AND seen as
                        # its own BLE advert / a native flow carrying the raw
                        # MAC. A registered device must appear ONCE in the
                        # picker, not once per discovery path.
                        continue
                    matched_registered_ids.add(_matched_id)

                # A Zigbee2MQTT record saved WITHOUT a class. Every surface
                # that reads the record (the Lighting tab, the Home Layout
                # dropdowns, the row's own options) then treats the device as
                # nothing at all, while this row, which reads Z2M live, still
                # calls it a light. Found on a customer box 2026-09-08: two
                # GL-SPI-206P, green dot, no card, no options. Hand it to the
                # heal, which writes the live class back once per boot.
                if (
                    row_integration == INTEGRATION_ZIGBEE2MQTT
                    and row.get("service_class")
                    and not _classify_device(registered_dev)
                ):
                    unclassed_records.append(
                        (registered_dev, str(row["service_class"]))
                    )

            mac = canonical  # kept for the rendered row's mac_address field

            # An ADDED device is what the customer saved, not what this cycle's
            # source row guesses. A sleeping Shelly Plus Smoke is seen only by
            # its DHCP lease, which carries no integration and enriches to the
            # relay's category, so its Devices row turned into an Electrical
            # nothing. Seen on a customer box 2026-10-04. The backfill path
            # already reads the record; this keeps the live path in step.
            row_category = row.get("category") or "unknown"
            row_service_class = row.get("service_class") or ""
            if registered_dev:
                row_category = registered_dev.get("category") or row_category
                if not row_integration:
                    row_integration = registered_dev.get("integration_type") or ""
                row_service_class = (
                    registered_dev.get("service_class")
                    or row_service_class
                    or _classify_device(registered_dev)
                    or ""
                )
            registered_catalog = _find_catalog_by_registered_type(registered_dev)

            source = row.get("source") or "unknown"

            # Subtype-aware catalog override. DHCP / static-probe rows match
            # `_enrich_from_catalog` by `vendor_device_type`, but Teltonikas
            # come in as "router" regardless of whether they actually are.
            # Once the vendor module has written `config.device_subtype`, we
            # can find the real catalog entry (e.g. `teltonika_switch`) and
            # pull its image + model_images for this render.
            image_row = row
            if registered_dev:
                subtype = (
                    registered_dev.get("device_subtype")
                    or (registered_dev.get("config") or {}).get("device_subtype", "")
                )
                vendor_model = row.get("vendor_model") or row.get("model") or ""
                if subtype and vendor_model:
                    override = _find_catalog_by_subtype(vendor_model, subtype)
                    if override:
                        image_row = dict(row)
                        if override.get("image"):
                            image_row["image"] = override["image"]
                        override_images = _catalog_model_images(override)
                        if override_images:
                            image_row["model_images"] = override_images
                # The live row enriched as a DIFFERENT product from the one
                # added (a sleeping Shelly Plus Smoke's DHCP lease reads as the
                # relay): draw the added product, not the guess.
                if (
                    image_row is row
                    and registered_catalog
                    and registered_catalog.get("image")
                    and str(row.get("vendor_device_type") or "").lower()
                    != str(registered_catalog.get("vendor_device_type") or "").lower()
                ):
                    image_row = dict(row)
                    image_row["image"] = registered_catalog["image"]
                    image_row["model_images"] = _catalog_model_images(
                        registered_catalog
                    )

            # BLE advert rows default to the raw advert `local_name` —
            # e.g. "BP-Orion XS HQ2412NW2RP" or "ROAM12105182". Native
            # rows built by `_discover_native_devices` already carry a
            # pre-formatted "<product> (<advert>)" name; when HA's
            # vendor integration hasn't opened a pending flow (no native
            # sibling available to merge), synthesise the same format
            # here from whichever BLE profile matched the row. Covers
            # Victron, Roamer, Ruuvi, Govee and anything else added to
            # `BLE_NATIVE_PROFILES` going forward — consistent picker
            # naming across every recognised BLE product.
            #
            # Prefix preference: byte-level `detected_model` (specific —
            # "Victron SmartShunt 500A/50mV", "RuuviTag Pro 2in1",
            # "Victron Orion XS 12/12-50") takes priority over the
            # generic profile name ("Ruuvi Tag", "Victron SmartShunt").
            # Customers with multiple tags of the same family in a van
            # can distinguish them at a glance without opening the row.
            synth_display = ""
            if not registered_dev:
                matched_profile = _match_ble_profile(row, BLE_NATIVE_PROFILES)
                prefix = str(row.get("detected_model") or "")
                # Upgrade a byte-decoded model code/title to the catalog's
                # friendly product name (e.g. govee "H5074" → "Govee H5074 Mini
                # Hygrometer"), mirroring the image path. Falls through to the
                # raw model when the catalog has no matching display name.
                if prefix:
                    friendly = _match_model_display_name(
                        prefix, _catalog_display_names_for_row(row),
                    )
                    if friendly:
                        prefix = friendly
                if not prefix and matched_profile is not None:
                    prefix = str(matched_profile.get("name") or "")
                if prefix:
                    # Two row shapes feed this path:
                    # - `ble_advert` rows carry the raw advert local_name
                    #   in `row["name"]` (e.g. "RuuviTag E884").
                    # - `native_ble` rows have already been through
                    #   `_discover_native_devices`, which sets
                    #   `row["name"]` to the pre-formatted composite
                    #   "Profile (hostname)" and keeps the raw advert
                    #   name in `row["hostname"]`. Use hostname on that
                    #   path so we don't end up double-wrapping the
                    #   profile prefix ("RuuviTag Pro 2in1 (Ruuvi Tag
                    #   (RuuviTag E884))").
                    if row.get("source") == "ble_advert":
                        advert_name = row.get("name") or ""
                    else:
                        advert_name = (
                            row.get("hostname") or row.get("name") or ""
                        )
                    # Some rows (Roamer Gen 1/2 via
                    # `_annotate_roamer_bms_advert`) request the MAC-tag
                    # format (`<model> (#XXXX)`) to match the post-add
                    # display name. Pass an empty hostname so
                    # `_build_discovery_name` falls through to its
                    # MAC-tag branch.
                    if row.get("prefer_mac_tag"):
                        advert_name = ""
                    synth_display = _build_discovery_name(
                        prefix, advert_name, mac, "",
                    )

            rendered.append({
                "id": row_id,
                "name": row.get("name") or row.get("hostname") or "Unknown",
                # Marked at RENDER time, not baked into the stored name, so an
                # ALREADY-REGISTERED sim device gains the marker too. Baking it
                # at registration would leave every previously-added fake
                # unmarked until it was removed and re-added (the stored
                # `display_name` wins over any freshly-probed name).
                "display_name": _mark_sim_name(
                    (registered_dev or {}).get("display_name")
                    or synth_display
                    or row.get("name")
                    or row.get("hostname")
                    or "Unknown",
                    mac,
                    simulated=_cerbo_reports_sim(row),
                ),
                "category": row_category,
                # The canonical service class (source of truth), surfaced so the
                # add-dispatch can persist a per-device `role` for sources whose
                # integration spans several classes (zigbee2mqtt covers lighting
                # / environment / security) — the integration→class fallback
                # can't disambiguate those.
                "service_class": row_service_class,
                "source": source,
                "protocol": _PROTOCOL_BY_SOURCE.get(source, "—"),
                "integration": row_integration,
                "integration_pattern": _integration_pattern(row),
                # Level 1/2 nesting: what this row provides (infrastructure) and
                # what it depends on (a child rides on a proxy/coordinator/bridge).
                # The picker indents children under their provider. Empty ⇒ flat.
                "provides_kind": _provides_kind(row, registered_dev),
                "parent_kind": _parent_kind(row, registered_dev),
                "manufacturer": row.get("manufacturer") or "",
                "manufacturer_id": row.get("manufacturer_id") or "",
                "address": row.get("ip_address") or row.get("address") or "",
                "hostname": row.get("hostname") or "",
                "mac_address": mac,
                "rssi": row.get("rssi"),
                "image": _pick_image(image_row, registered_dev),
                "registered": registered_dev is not None,
                # The registered device this row was matched to, which Remove
                # deletes. Not always the row's own id: a router swapped for a
                # new one at the same address matches the old record by IP.
                "registered_device_id": (
                    str(registered_dev.get("device_id") or "") if registered_dev else ""
                ),
                "native_flow_id": row.get("native_flow_id"),
                "native_domain": row.get("native_domain"),
                "discovery_id": row.get("discovery_id"),
                "bms_family": row.get("bms_family"),
                # Catalog-sourced experimental flag (stamped by
                # `_enrich_from_catalog` only when the Experimental Devices
                # switch has revealed the row). Must be copied onto the rendered
                # row explicitly — this dict is an allowlist projection, so an
                # un-listed key never reaches the picker's `devices` attribute.
                # The picker badges the row EXPERIMENTAL off this value.
                "experimental": bool(row.get("experimental")),
                # Proven-on-real-hardware state (`_enrich_from_catalog`
                # resolves it per model from the catalog's `verified` field).
                # Same allowlist rule as `experimental`. Tri-state ON PURPOSE:
                # True/False only on a row that matched the catalog, None on
                # anything else, so the picker badges a supported-but-untested
                # device without badging every stray box on the network.
                "verified": _row_verified(row, registered_dev),
                # Brand mark (catalog `brand_logo`, stamped by enrichment). Same
                # allowlist rule as `experimental`: copy it or it never ships.
                "brand_logo": _brand_logo_url(row),
                # Post-registration subtype stamped by the vendor module
                # (e.g. Teltonika router vs switch via hw_mobile, Starlink
                # mini vs gen3 via hardware_version). Falls back to the
                # source row's device_subtype, which is set during BLE
                # enrichment for products we can identify pre-registration
                # (e.g. Victron SmartShunt via manufacturer_data model_id
                # in `_annotate_victron_subtype`). Without that fallback
                # the picker filter rule that gates Victron rows on
                # device_subtype="smartshunt" would never match an
                # unregistered SmartShunt and the customer can't add it.
                "device_subtype": (
                    (registered_dev or {}).get("device_subtype")
                    or (registered_dev or {}).get("config", {}).get("device_subtype", "")
                    or row.get("device_subtype", "")
                    or (registered_catalog or {}).get("device_subtype")
                    or ""
                ),
                # Byte-level-detected Victron model_id + readable model
                # label (SmartShunt 500A, Orion XS 12/12-50, RuuviTag Pro
                # 3in1, …). Exposed on the rendered row so the picker's
                # expanded-row detail can show "Detected model" and
                # "Victron model ID" entries even for BLE advert /
                # native_ble rows whose annotation happened during
                # `_collect_ble_rows` or the backstop.
                # A Z2M device whose model matches no catalog entry but whose
                # entities classify cleanly: a new Tuya batch of a product we do
                # support. Rendered so the picker can tag it and the expanded row
                # can show the raw Zigbee manufacturerName, which is the ONLY
                # string that identifies such a batch (its model id is the
                # generic `TS0601` every Tuya device reports).
                "unrecognised_batch": bool(row.get("unrecognised_batch")),
                "zigbee_manufacturer": row.get("zigbee_manufacturer") or "",
                "detected_model": row.get("detected_model") or "",
                "victron_model_id": row.get("victron_model_id") or "",
                "ruuvi_mfr_hex": row.get("ruuvi_mfr_hex") or "",
                "victron_mfr_hex": row.get("victron_mfr_hex") or "",
                # Catalog-sourced suppression hint — carried onto the row so
                # the add-dispatch can create an HA ignore-entry for the
                # native integration (e.g. Teltonika's own integration)
                # without re-scanning the catalog at dispatch time.
                "ignore_native": row.get("ignore_native") or "",
                # Same reasoning for the cooperate pattern — pending-flow
                # acceptance uses native_flow_id/native_domain already.
                "accept_native": row.get("accept_native") or "",
                # Native flow input descriptor — if the native HA
                # integration needs a value (e.g. victron_ble wants an
                # access_token / encryption key from the VictronConnect
                # app) the BLE profile declares it here and the picker
                # renders an inline input per row so the customer can
                # paste the key at add time. Empty dict = no extra input
                # required (the common case).
                "native_flow_input": row.get("native_flow_input") or {},
                # Inline config — editable fields + Save button surfaced
                # on the picker's expanded detail pane. Populated only
                # for registered devices that have customer-facing
                # config (e.g. Teltonika Username / Password).
                "config_entities": _config_entities_for_device(
                    self.hass, self._entry, registered_dev,
                ),
                "save_button_entity": _save_button_entity_for_device(
                    self.hass, self._entry, registered_dev,
                ),
                "save_button_label": _save_button_label_for_device(
                    registered_dev,
                ),
                "router_setup": _router_setup_for_device(registered_dev),
                "network_setup": _network_setup_for_device(registered_dev, network_router),
                "levelling_setup": levelling_setup_for_device(registered_dev),
                "router_added": _router_added(registered_dev),
                # Pairing status + Pair/Forget actions for the device's expand
                # row (today: the Dometic FreshJet's ESPHome bridge). Empty {}
                # for devices with no pairing surface.
                "pairing_controls": _pairing_controls_for_device(
                    self.hass, self._entry, registered_dev,
                ),
                # Reachability dot inputs. `liveness_entities` are the device's
                # live-state HA entities (the picker reads their availability,
                # reusing the same rule as the cards); empty for a pure-scan
                # device not yet in HA, which then falls back to `last_seen_utc`
                # freshness. Both absent → the picker shows a neutral grey dot.
                "liveness_entities": _device_liveness_entities(
                    liveness_index, registered_dev, self.hass,
                ),
                "last_seen_utc": (
                    last_seen_by_disc_id.get(disc_id)
                    or last_seen_by_mac.get(canonical or "")
                ),
                "ignored": row_id.upper() in ignored_device_ids,
                # Singleton gate (`_SINGLETON_RULES`: one Van Level, one
                # camera). `blocked` flips off the addable checkbox in the
                # picker; `blocked_reason` is the tooltip / pill copy. Only set
                # for unregistered rows that violate a one-per-system rule; a
                # registered row stays `registered: True` as normal.
                **_singleton_gate(row, registered_dev, existing_devs),
            })

            # Nest display-only children under the just-appended parent row
            # (Level 1 → Level 2): the FreshJet AC under its bridge, and a
            # Cerbo GX's Venus sub-devices under the Cerbo. Each helper gates on
            # the parent's identity and returns nothing for other rows.
            _parent_row = rendered[-1]
            _fj_bonded = _freshjet_bonded_mac(
                self.hass, self._entry, registered_dev
            )
            _fj_child = _synthesise_freshjet_child(
                _parent_row, registered_dev, bonded_mac=_fj_bonded
            )
            if _fj_child is not None:
                rendered.append(_fj_child)
            rendered.extend(
                _synthesise_cerbo_children(self.hass, _parent_row, registered_dev)
            )
            rendered.extend(
                _synthesise_shelly_light_children(self.hass, _parent_row, registered_dev)
            )

        # Backfill — some sources drop a device from active discovery the
        # moment it's confirmed (the backend ESPHome path clears the
        # discovery record on confirm, for instance). Without this loop
        # the picker would briefly lose the row between confirm and the
        # backend's next re-scan, which looks like the ADDED pill takes
        # several seconds to arrive.
        #
        # Walk the registered device list and synthesise a row for any
        # device not already in `rendered`. Match by canonical MAC, then
        # discovery_id, then IP — same priority as the cross-ref logic.
        #
        # Skip BLE-integration devices entirely when no bluetooth source
        # is registered — the customer rule is "no bluetooth source means
        # no BLE rows, fake or real, live or backfilled". A stale
        # registered Roamer / SmartShunt / Ruuvi etc. from a prior
        # session with bluetooth loaded would otherwise haunt the
        # picker after the proxy was uninstalled.
        try:
            from homeassistant.components.bluetooth import async_scanner_count
            _no_bluetooth_source = async_scanner_count(self.hass) == 0
        except ImportError:
            _no_bluetooth_source = True
        coordinator = runtime.get("coordinator")
        registered_devices = (
            (coordinator.data or {}).get("devices", []) if coordinator else []
        ) or []
        for dev in registered_devices:
            dev_mac = _canonical_mac(
                dev.get("linked_mac_address") or dev.get("mac_address")
            )
            dev_disc = str(
                dev.get("linked_discovery_id") or dev.get("discovery_id") or ""
            )
            dev_ip = dev.get("ip_address") or ""
            # Already rendered via a live discovery row this cycle (matched by
            # device_id even when its IP moved) — don't add a stale duplicate.
            if str(dev.get("device_id") or "") in matched_registered_ids:
                continue
            if dev_mac and dev_mac in seen_canonical_macs:
                continue
            if dev_disc and dev_disc in seen_discovery_ids:
                continue
            # Fall back to IP match to avoid double-listing Starlink (no MAC)
            if dev_ip and any(r.get("address") == dev_ip for r in rendered):
                continue

            integration = dev.get("integration_type") or ""
            # Use the device-level BLE check, not just the integration string:
            # the Velit AC's `integration_type` drifts/blanks, which let it leak
            # past this gate while every other BLE device obeyed it. The catalog
            # discovery block is the reliable signal.
            if _no_bluetooth_source and _device_is_ble_only(dev):
                continue
            source = "registered"
            row_id = dev_disc or dev_mac or dev_ip or dev.get("device_id") or ""
            if not row_id:
                continue

            # Pull the catalog entry so the synthesised row carries `image`
            # + `model_images` — without this, _pick_image returns "" and
            # the card shows no image at all for registered-only devices
            # (e.g. Starlink via static probe once the discovery source has
            # stopped re-reporting it).
            #
            # Three-tier lookup: vendor_model first (specific), then
            # bms_family (a known leisure-battery chemistry — "jbd" maps
            # to Fogstar, "jk" to Roamer Gen 2, "vip" to Roamer Gen 1),
            # then the integration-wide merge as a last resort. The
            # integration merge takes the FIRST catalog entry's `image`,
            # so on `sensa_bms_ble` a registered Fogstar without
            # vendor_model would inherit Roamer Gen 1's image (first
            # entry). The family fallback keeps each row pinned to its
            # own asset even if the record lost its vendor_model.
            # A BLE device drops out of active discovery once its vendor
            # coordinator holds a connection, so this backfill path —
            # not the cross-ref path — is what renders it.
            dev_vendor_model = dev.get("vendor_model") or dev.get("model") or ""
            dev_bms_family = (
                dev.get("bms_family")
                or (dev.get("config") or {}).get("bms_family")
                or ""
            )
            catalog_entry = (
                _find_catalog_by_vendor_model(dev_vendor_model)
                or _find_catalog_by_bms_family(dev_bms_family)
                or _find_catalog_by_registered_type(dev)
                or _find_catalog_by_integration(integration)
                or {}
            )
            # The display_name was set by our own classifier at add time
            # ("Roamer 12V 200Ah Xtreme (#3655)"); reverse-map it through
            # the catalog's display_names to recover the model slug so
            # _pick_image can swap the generic image for the model-
            # specific one. Empty when there's no model_detection (e.g.
            # Fogstar) or no match — _pick_image then falls back through
            # registered_dev.model to the generic catalog image.
            detected_slug = _bms_model_slug_from_display_name(
                dev.get("display_name") or "", catalog_entry,
            )
            synthetic_row = {
                "integration": integration,
                "vendor_model": dev_vendor_model or catalog_entry.get("vendor_model") or "",
                "image": catalog_entry.get("image"),
                "model_images": catalog_entry.get("model_images")
                or _catalog_model_images(catalog_entry),
                "detected_model": detected_slug,
            }
            # Resolve the service class ONCE — `_config_entities_for_device`
            # would otherwise re-run the vendor classifier (a deferred import
            # that pulls the catalog) for its heater gate on every refresh.
            dev_service_class = dev.get("service_class") or _classify_device(dev)

            rendered.append({
                "id": row_id,
                "name": dev.get("display_name") or integration or "Device",
                "display_name": dev.get("display_name") or integration or "Device",
                "category": dev.get("category") or "unknown",
                "source": source,
                "protocol": _PROTOCOL_BY_SOURCE.get(source, "—"),
                "integration": integration,
                "integration_pattern": _integration_pattern(
                    {"integration": integration}
                ),
                # Level 1/2 nesting (see the main emit site) — resolved from the
                # registered device `dev` so a backfilled BLE/Zigbee child nests
                # under its proxy/coordinator too.
                "provides_kind": _provides_kind(_registered_kind_row(dev), dev),
                "parent_kind": _parent_kind({"integration": integration}, dev),
                "manufacturer": dev.get("vendor") or "",
                "manufacturer_id": "",
                "address": dev_ip or (
                    ":".join(dev_mac[i:i+2] for i in range(0, 12, 2)).upper()
                    if dev_mac and len(dev_mac) == 12 and not dev_ip else ""
                ),
                "hostname": dev.get("hostname") or "",
                "mac_address": dev_mac,
                "rssi": None,
                "image": _pick_image(synthetic_row, dev),
                "registered": True,
                "native_flow_id": None,
                "native_domain": None,
                "discovery_id": dev_disc or None,
                "bms_family": (dev.get("config") or {}).get("bms_family"),
                "device_subtype": (
                    dev.get("device_subtype")
                    or (dev.get("config") or {}).get("device_subtype", "")
                    or (_find_catalog_by_registered_type(dev) or {}).get("device_subtype")
                    or ""
                ),
                # Same flag as the live-discovery projection, DERIVED from the
                # persisted subtype rather than from a source row (a backfilled
                # row has none). Both projections must carry it or an added
                # device loses its tag on any cycle discovery misses it.
                "unrecognised_batch": (
                    dev.get("device_subtype")
                    or (dev.get("config") or {}).get("device_subtype", "")
                ) == ZIGBEE_UNRECOGNISED_SUBTYPE,
                "ignored": row_id.upper() in ignored_device_ids,
                # A backfilled row is a device the customer ALREADY ADDED — it
                # just isn't being discovered live this cycle (an ESPHome heater
                # the subnet probe didn't answer for, say). It must therefore
                # carry the same device-scoped controls the main emit site gives
                # a discovered row, or adding a device and then having discovery
                # miss it once silently strips its row of every control: the
                # heater "Show on Home Layout" toggle, the FreshJet Pair button,
                # the relay + Teltonika config fields. (The Teltonika kept its
                # credentials only because DHCP rediscovers it every cycle.)
                "service_class": dev_service_class,
                "config_entities": _config_entities_for_device(
                    self.hass, self._entry, dev, service_class=dev_service_class,
                ),
                "save_button_entity": _save_button_entity_for_device(
                    self.hass, self._entry, dev,
                ),
                "save_button_label": _save_button_label_for_device(dev),
                "router_setup": _router_setup_for_device(dev),
                "network_setup": _network_setup_for_device(dev, network_router),
                "levelling_setup": levelling_setup_for_device(dev),
                "router_added": _router_added(dev),
                "pairing_controls": _pairing_controls_for_device(
                    self.hass, self._entry, dev,
                ),
                # The EXPERIMENTAL pill. Read straight off the catalog entry, not
                # off the switch: a backfilled row is an ADDED device, and its
                # badge must not flicker on and off as discovery finds or misses
                # it between cycles. The picker tests `=== true`, so the key has
                # to exist with a real boolean.
                "experimental": bool(catalog_entry.get("experimental")),
                # The UNVERIFIED pill, read off the catalog for the same
                # reason as the line above: a backfilled row is an ADDED
                # device and its badge must not flicker as discovery finds
                # or misses it. `detected_slug` resolved just above is the
                # model, which is what verification is recorded against.
                "verified": _row_verified(
                    dict(synthetic_row, catalog_id=catalog_entry.get("id")), dev
                ),
                "brand_logo": _brand_logo_url(catalog_entry),
                # Reachability dot — a backfilled row is a registered device, so
                # resolve its live HA entities; fall back to last_seen freshness.
                "liveness_entities": _device_liveness_entities(
                    liveness_index, dev, self.hass,
                ),
                "last_seen_utc": (
                    last_seen_by_disc_id.get(dev_disc)
                    or last_seen_by_mac.get(dev_mac or "")
                ),
            })
            # Nest display-only children under a backfilled parent row — the
            # FreshJet AC under its bridge, a Cerbo's Venus sub-devices under it.
            _parent_row = rendered[-1]
            _fj_bonded = _freshjet_bonded_mac(self.hass, self._entry, dev)
            _fj_child = _synthesise_freshjet_child(
                _parent_row, dev, bonded_mac=_fj_bonded
            )
            if _fj_child is not None:
                rendered.append(_fj_child)
            rendered.extend(
                _synthesise_cerbo_children(self.hass, _parent_row, dev)
            )
            rendered.extend(
                _synthesise_shelly_light_children(self.hass, _parent_row, dev)
            )
            if dev_mac:
                seen_canonical_macs.add(dev_mac)
            if dev_disc:
                seen_discovery_ids.add(dev_disc)

        # `registered` rows still need a protocol label so the table tint
        # kicks in. Heuristic from integration name.
        for row in rendered:
            if row.get("source") == "registered":
                integ = (row.get("integration") or "").lower()
                if "ble" in integ or "bluetooth" in integ:
                    row["protocol"] = "Bluetooth"
                elif "zigbee" in integ:
                    # A zigbee2mqtt device reaches HA over the Zigbee radio,
                    # not IP. Discovery labels it "Zigbee" via
                    # _PROTOCOL_BY_SOURCE["mqtt_zigbee"]; keep the registered
                    # row consistent instead of defaulting it to TCP/IP.
                    row["protocol"] = "Zigbee"
                else:
                    row["protocol"] = "TCP/IP"

        # DIAGNOSTIC (2026-07, TEMPORARY — remove once confirmed on-device):
        # the "Cerbo children vanish on a refresh, reappear next scan" report.
        # Logs (read-only) when a REGISTERED Cerbo ended this render with no child
        # rows even though HA's device registry still holds its via_device
        # children — the exact vanish symptom — so we can see WHY on the box (a
        # per-cycle discovery match miss → parent `registered: False`, vs an HA
        # registry lookup miss). Never mutates `rendered`; the caller swallows.
        try:
            _log_cerbo_children_diagnostic(self.hass, rendered, registered_devices)
        except Exception:
            _LOGGER.debug("cerbo-children diagnostic raised", exc_info=True)

        # The Sensa Core (the App backend/server) is always present as a
        # Level-1 self-row — auto-added at setup, no scan, not removable. Added
        # after the registered-protocol relabel loop so its "Sensa" protocol is
        # preserved, and before the sort so it lands in the Added tier.
        rendered.append(
            _synthesise_core_device(self.hass, getattr(self, "_core_host_ip", ""))
        )

        # Sort tiers, outer-most first:
        #   1. Registered devices (already added) — always on top.
        #   2. Known devices (catalog-matched `integration`) before
        #      unknown ones. Pushes random BLE adverts / unrecognised
        #      DHCP entries to the bottom so the customer sees the
        #      devices Sensa can actually handle first.
        #   3. Display name alphabetical — primary ordering within a
        #      tier. Source priority (esphome > dhcp > ble) used to be
        #      a tier here but it made the picker feel shuffled on
        #      every scan when sources settle at slightly different
        #      speeds; name-alphabetical is predictable and lets the
        #      customer scan for a specific device.
        rendered.sort(
            key=lambda r: (
                0 if r["registered"] else 1,
                0 if r.get("integration") else 1,
                r["display_name"].lower(),
            )
        )
        # Count real devices only — synthesised display-only rows (the FreshJet
        # AC child, `row_readonly`) are a view of their parent, not a device, so
        # they must not inflate the discovery tally read by cards/telemetry.
        self._attr_native_value = sum(
            1 for r in rendered if not r.get("row_readonly")
        )
        # What the picker will actually show. The rows are excluded from the
        # recorder (they are large and change every poll), so without this there
        # is no way to see them outside the browser, and a row that is discovered
        # but never rendered looks identical to one that was never discovered.
        _LOGGER.debug(
            "Discovery sensor: rendered %d row(s): %s",
            len(rendered),
            [
                f"{r.get('display_name')}[int={r.get('integration') or '-'}"
                f" src={r.get('source')} reg={r.get('registered')}]"
                for r in rendered
            ],
        )
        self._attr_extra_state_attributes = {
            "devices": rendered, "started_ts": _STARTED_TS,
        }

        if unclassed_records:
            from . import async_heal_zigbee_record_class

            for device, service_class in unclassed_records:
                self.hass.async_create_task(
                    async_heal_zigbee_record_class(
                        self.hass, self._entry, device, service_class,
                    )
                )

    async def _collect_ble_rows(self) -> list[dict[str, Any]]:
        """Collect raw BLE advertisements, tagged with source `ble_advert`."""
        try:
            from homeassistant.components.bluetooth import (
                async_discovered_service_info,
                async_scanner_count,
            )
        except ImportError:
            return []

        # No bluetooth source registered → no way to reach BLE devices,
        # so don't list any (real or simulated). The customer rule:
        # "if we aren't showing real bluetooth devices, we can't show
        # fake ones". Matches the gate in SensaBleDevicesSensor.
        if async_scanner_count(self.hass) == 0:
            return []

        infos = async_discovered_service_info(self.hass)
        rows: list[dict[str, Any]] = []
        for info in infos:
            manufacturer, manufacturer_id = _identify_manufacturer(info)
            address = info.address or ""
            row: dict[str, Any] = {
                "name": info.name or "Unknown",
                "address": address,
                "mac_address": address,
                "rssi": info.rssi,
                "source": "ble_advert",
                "manufacturer": manufacturer,
                "manufacturer_id": manufacturer_id,
                # Advertised GATT service UUIDs — lets profiles match on a
                # service UUID (e.g. the Velit AC's 0xFFE0) for devices that
                # carry no manufacturer data.
                "service_uuids": [str(u).lower() for u in (info.service_uuids or [])],
            }
            _annotate_victron_subtype(row, info)
            _annotate_govee_model(row, info)
            _annotate_roamer_bms_advert(row, info)
            _annotate_dji_power_advert(row, info)
            _annotate_ruuvi_advert(row, info)
            _annotate_tpms_advert(row, info)
            rows.append(row)
        return rows

    def _row_id(self, row: dict[str, Any]) -> str:
        discovery_id = row.get("discovery_id")
        if discovery_id:
            return str(discovery_id)
        mac = row.get("mac_address") or row.get("address")
        if mac:
            return str(mac).upper()
        return str(row.get("name") or "unknown")


def _canonical_mac(value: str | None) -> str:
    """Normalise a MAC to uppercase hex without separators.

    Our various discovery sources emit MACs in incompatible formats:
    native_ble rows strip colons (`AABBCCDDEEFF`), ble_advert rows keep
    colons (`AA:BB:CC:DD:EE:FF`), DHCP pre-formats with colons. Compare
    against this canonical form so the dedup and registered-match logic
    sees the same device regardless of which source it came in through.
    """
    if not value:
        return ""
    cleaned = str(value).upper().replace(":", "").replace("-", "").replace(" ", "")
    return cleaned


def _cerbo_portal_canonical_mac(discovery_id: Any) -> str:
    """Canonical MAC embedded in a Cerbo's `cerbo-<portal>` discovery id.

    A Victron Cerbo GX discovered via MQTT-on-LAN is keyed on its VRM portal id
    (`cerbo-<portal>`) and carries NO `mac_address` — but that portal id IS the
    GX device's primary-interface MAC, and the Cerbo also beacons over BLE under
    the same MAC. Returns the canonical MAC when the portal id is a bare 12-hex
    string, else "". Lets the BLE-advert dedup recognise the Cerbo's own advert
    as already represented (so it isn't shown as a second "Victron Cerbo GX").
    """
    s = str(discovery_id or "")
    if not s.startswith("cerbo-"):
        return ""
    mac = _canonical_mac(s[len("cerbo-"):])
    if len(mac) == 12 and all(c in "0123456789ABCDEF" for c in mac):
        return mac
    return ""


def _match_ble_profile(
    row: dict[str, Any], profiles: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the first BLE profile matching this row, or None.

    Match logic per profile:

      - `manufacturer_id` MUST match (case-insensitive). Required —
        every profile keys on this.
      - `name_prefix` and `device_subtype` are alternative tie-
        breakers. If the profile declares either, the row must satisfy
        AT LEAST ONE. The two-channel match exists because some BLE
        adverts arrive nameless (Victron BLE devices often advertise
        with no `local_name` until paired in VictronConnect) — for
        those, `_annotate_victron_subtype` stamps a `device_subtype`
        from the manufacturer_data model_id, and that's the only way
        to identify the row.
    """
    raw_mfr = str(row.get("manufacturer_id") or "").lower()
    name = str(row.get("name") or "")
    row_subtype = str(row.get("device_subtype") or "")
    row_uuids = {str(u).lower() for u in (row.get("service_uuids") or [])}
    for profile in profiles:
        # A profile may gate on a service UUID instead of a manufacturer_id
        # (the Velit AC advertises a service UUID + a "KT" name, no
        # manufacturer data). The service UUID 0xFFE0 is a generic UART
        # service shared by many cheap modules, so the `name_prefix` tie-
        # breaker below still has to match — that's what disambiguates.
        profile_service_uuid = profile.get("service_uuid")
        if profile_service_uuid:
            if str(profile_service_uuid).lower() not in row_uuids:
                continue
        elif profile.get("manufacturer_id") is None and profile.get("device_subtype"):
            # Subtype-only profile: the family has no stable company ID or
            # service UUID (TPMS sensors), so byte detection is the whole
            # match. Safe because `_annotate_tpms_advert` stamps the subtype
            # only on a frame that decodes.
            if row_subtype != str(profile["device_subtype"]):
                continue
            return profile
        else:
            expected = profile.get("manufacturer_id")
            if expected is None:
                continue
            if isinstance(expected, int):
                expected_hex = f"0x{expected:04x}"
            else:
                expected_hex = str(expected).lower()
            if raw_mfr != expected_hex:
                continue

        prefix = profile.get("name_prefix")
        prefixes: list[str] = []
        if prefix:
            prefixes = [prefix] if isinstance(prefix, str) else list(prefix)
        profile_subtype = str(profile.get("device_subtype") or "")

        name_match = bool(prefixes) and any(name.startswith(p) for p in prefixes)
        subtype_match = bool(profile_subtype) and row_subtype == profile_subtype

        # If the profile declared neither selector, manufacturer_id is
        # all the discrimination it asked for — accept.
        if not prefixes and not profile_subtype:
            return profile
        # Otherwise the row needs to satisfy AT LEAST ONE of the
        # declared selectors. A nameless SmartShunt with
        # device_subtype="smartshunt" matches via subtype_match; a
        # named one matches via name_match. Either is sufficient.
        if name_match or subtype_match:
            return profile
    return None


# Customer-facing protocol label per discovery source. Hides the implementation
# detail (whether we saw it via DHCP vs static probe) behind the physical
# transport the customer actually plugged into.
_PROTOCOL_BY_SOURCE: dict[str, str] = {
    "esphome_http": "TCP/IP",
    "dhcp": "TCP/IP",
    "static_probe": "TCP/IP",
    "native_zeroconf": "TCP/IP",
    "native_ble": "Bluetooth",
    "ble_advert": "Bluetooth",
    "mqtt_zigbee": "Zigbee",
    "wican_mqtt": "OBD2",
}


# Integration types that reach their devices ONLY over Bluetooth. Used by
# the discovery sensor's backfill loop to skip registered BLE devices
# when HA has no bluetooth source — the customer rule is "no bluetooth
# source → no BLE rows in the picker, fake or real, live or backfilled".
_BLE_INTEGRATION_TYPES: frozenset[str] = frozenset({
    "sensa_bms_ble",
    "sensa_ble_scanner",
    "sensa_virtual_ble_scanner",
    "sensa_velit_rooftop_ac",
    "sensa_victron_orion_xs",
    "sensa_dji_ble",
    "sensa_tpms_ble",
    "victron_ble",
    "ruuvitag_ble",
    "govee_ble",
})


def _is_ble_integration(integration_type: str) -> bool:
    """True if the integration's only transport is bluetooth.

    A registered device whose integration is in this set can't be
    reached when HA has no bluetooth scanner registered, so the
    discovery picker hides it under that condition. Keep this set
    narrow — only integrations that have NO non-bluetooth path. A
    Cerbo can run over MQTT/BLE so it's not here; a SmartShunt has
    no IP, only BLE, so it is.
    """
    return (integration_type or "").strip().lower() in _BLE_INTEGRATION_TYPES


def _device_is_ble_only(dev: dict[str, Any]) -> bool:
    """True if a REGISTERED device's only transport is bluetooth — so the
    Discovery picker must hide it when HA has no bluetooth source.

    Keys off `integration_type` first (`_is_ble_integration`), but that field
    has drifted / blanked on some installs — the Velit Rooftop AC notably (it's
    also why the Climate card once showed blank). A blank `integration_type`
    made `_is_ble_integration` return False, so the Velit leaked past the
    no-bluetooth gate that every correctly-typed BLE device obeys.

    Falls back to the device's CATALOG entry — resolved by the SAME tiers the
    backfill uses for the image (vendor_model → bms_family → integration) — and
    treats it as BLE-only when its `discovery` block's ONLY transport is `ble`.
    The catalog discovery block is the source of truth for transport,
    independent of a mis-stored `integration_type`. Matching is by specific
    identity (vendor_model / integration), never by service_class, so a non-BLE
    device that merely shares a class with a BLE one is not wrongly hidden.
    """
    if _is_ble_integration(dev.get("integration_type") or ""):
        return True
    catalog = (
        _find_catalog_by_vendor_model(dev.get("vendor_model") or dev.get("model") or "")
        or _find_catalog_by_bms_family(
            dev.get("bms_family") or (dev.get("config") or {}).get("bms_family") or ""
        )
        or _find_catalog_by_integration(dev.get("integration_type") or "")
        or {}
    )
    discovery = catalog.get("discovery") or {}
    return bool(discovery) and set(discovery) == {"ble"}


# Entity domains that do NOT reflect a device's liveness — config helpers /
# meta entities that keep a restored or always-on state and would mask an
# offline device. A liveness entity is anything OUTSIDE this set (a denylist, so
# a new externally-backed domain counts as live by default rather than being
# silently dropped until someone extends an allowlist).
_NON_LIVE_DOMAINS = frozenset({
    "text", "number", "select", "button", "update",
    "scene", "automation", "script", "event",
})

# The liveness index built once per render: (lookup key -> HA device id) and
# (HA device id -> sorted live entity_ids). Lookup keys are a device's canonical
# MAC connection(s), its Zigbee2MQTT IEEE, AND its Sensa backend device_id (our
# vendor integrations key their HA device on `(DOMAIN, "<prefix><device_id>")`
# rather than a MAC connection — without this a Roamer BMS / Starlink / Cerbo
# reads grey because no MAC connection matches).
LivenessIndex = tuple[dict[str, str], dict[str, list[str]]]
_EMPTY_LIVENESS_INDEX: LivenessIndex = ({}, {})


def _build_liveness_index(hass: Any) -> LivenessIndex:
    """Build the reachability lookup ONCE per render, so resolving a device's
    live entities is a dict lookup instead of a full registry walk per row.

    A "live" entity is one that goes `unavailable` when the device drops off —
    so we EXCLUDE config-helper domains (`_NON_LIVE_DOMAINS`), the always-live
    timestamp diagnostics (a Z2M `_last_seen` stays populated off-mesh, a false
    'reachable'), and disabled entities (no `hass.states` entry). Other
    diagnostic entities are KEPT — they go unavailable offline and are the only
    liveness signal for infra devices like the SMLight Zigbee coordinator.
    Devices are keyed by every MAC connection,
    their Zigbee2MQTT IEEE identifier, their WiCAN identifier (`_canonical_mac` /
    `canon_ieee` normalise all three to the same bare-hex form), and their Sensa
    backend `device_id` recovered from a `(DOMAIN, "<prefix><device_id>")`
    identifier."""
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
    except ImportError:
        return _EMPTY_LIVENESS_INDEX
    try:
        from .vendors.zigbee2mqtt import zigbee2mqtt_device_ieee
    except ImportError:
        zigbee2mqtt_device_ieee = None  # type: ignore[assignment]
    try:
        from .vendors.wican_mqtt import wican_device_id
    except ImportError:
        wican_device_id = None  # type: ignore[assignment]
    # Our vendor device_info identifier prefixes (e.g. "bms_", "starlink_"),
    # so we can strip them back to the backend device_id. Imported lazily to
    # avoid a circular import at module load.
    try:
        from . import _SENSA_DEVICE_IDENTIFIER_PREFIXES as _prefixes
    except Exception:  # noqa: BLE001
        _prefixes = ()

    try:
        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
        key_to_haid: dict[str, str] = {}
        for ha_dev in dev_reg.devices.values():
            for _ctype, cval in (ha_dev.connections or set()):
                cmac = _canonical_mac(cval)
                if cmac:
                    key_to_haid.setdefault(cmac, ha_dev.id)
            for _dom, value in (ha_dev.identifiers or set()):
                if _dom != DOMAIN or not isinstance(value, str):
                    continue
                for prefix in _prefixes:
                    if value.startswith(prefix):
                        key_to_haid.setdefault(value[len(prefix):], ha_dev.id)
                        break
            if zigbee2mqtt_device_ieee is not None:
                try:
                    ieee = zigbee2mqtt_device_ieee(ha_dev)
                except Exception:  # noqa: BLE001 — a malformed identifier shouldn't break render
                    ieee = None
                if ieee:
                    key_to_haid.setdefault(ieee, ha_dev.id)
            # A WiCAN Pro (OBD2) is owned by HA's shared `mqtt` integration and
            # keyed on `("mqtt", "wican_<id>")` — no MAC connection, no Sensa
            # identifier, so none of the branches above see it and the picker
            # read a permanent grey. Its `<id>` IS what the discovery row stores
            # as `mac_address`; canonicalise both sides so a firmware that
            # stamps a separated MAC still matches the backend record.
            if wican_device_id is not None:
                try:
                    wican_id = _canonical_mac(wican_device_id(ha_dev))
                except Exception:  # noqa: BLE001 — a malformed identifier shouldn't break render
                    wican_id = ""
                if wican_id:
                    key_to_haid.setdefault(wican_id, ha_dev.id)

        live_by_haid: dict[str, list[str]] = {}
        for ent in ent_reg.entities.values():
            if getattr(ent, "disabled_by", None) is not None:
                continue
            if ent.entity_id.split(".", 1)[0] in _NON_LIVE_DOMAINS:
                continue
            # KEEP diagnostic entities — they DO go `unavailable` when the device
            # drops off, and for an infrastructure device like the SMLight Zigbee
            # coordinator (whose smlight-integration sensors — core temp / RSSI /
            # connection mode — are ALL diagnostic) they are the only liveness
            # signal; excluding them left it permanently grey. Skip only the
            # always-live timestamp diagnostics (a Z2M `_last_seen` keeps updating
            # off-mesh, which would read as a false 'reachable').
            dc = (
                getattr(ent, "device_class", None)
                or getattr(ent, "original_device_class", None)
            )
            if dc == "timestamp" or ent.entity_id.endswith("_last_seen"):
                continue
            live_by_haid.setdefault(ent.device_id, []).append(ent.entity_id)
        for ids in live_by_haid.values():
            ids.sort()
        return key_to_haid, live_by_haid
    except Exception:  # noqa: BLE001 — never let a registry hiccup break the picker
        return _EMPTY_LIVENESS_INDEX


def _device_liveness_entities(
    index: LivenessIndex,
    registered_dev: dict[str, Any] | None,
    hass: Any = None,
) -> list[str]:
    """A registered device's live HA entity_ids, looked up from the prebuilt
    `_build_liveness_index` by its MAC or its Sensa `device_id`. Empty when the
    device isn't in HA yet (a pure-scan discovery) — the picker then falls back
    to the backend `last_seen` freshness.

    Two device classes that HA doesn't key by MAC/prefix/IEEE need a `hass`-based
    fallback (so they stop reading a false grey when they're actually up):
      - a **cooperate-flow speaker** (Sonos), whose HA device HA keys on a RINCON
        id not a MAC, so the index misses it; its `media_player` is the signal.
      - a **Zigbee coordinator with no native HA integration** (SONOFF Dongle-M),
        which owns no HA entities, so Z2M's bridge connection-state is the signal.
    """
    if not registered_dev:
        return []
    key_to_haid, live_by_haid = index
    mac = _canonical_mac(
        registered_dev.get("linked_mac_address")
        or registered_dev.get("linked_normalized_mac")
        or registered_dev.get("mac_address"),
    )
    device_id = str(registered_dev.get("device_id") or "")
    ha_id = (key_to_haid.get(mac) if mac else None) or (
        key_to_haid.get(device_id) if device_id else None
    )
    if ha_id is not None:
        # Keyed in the index: trust this device's own live entities only (even
        # when empty), the original behaviour. The fallbacks below are strictly
        # for devices the index can't key at all, so a keyed-but-empty device
        # never borrows another device's signal.
        return list(live_by_haid.get(ha_id, []))

    # Fallbacks for devices HA doesn't key by MAC/prefix/IEEE. Need a hass to
    # read the registries / config entries from. Coordinator first: its check is
    # a plain set lookup, cheaper than the speaker path's classify + registry walk.
    if hass is not None:
        coordinator = _zigbee_coordinator_liveness(hass, registered_dev)
        if coordinator:
            return [coordinator]
        speaker = _cooperate_speaker_liveness(hass, registered_dev)
        if speaker:
            return [speaker]
    return []


def _cooperate_speaker_liveness(hass: Any, dev: dict[str, Any]) -> str | None:
    """The `media_player.*` liveness entity for a cooperate-flow speaker (Sonos),
    or None. Reuses the dashboard's resolver (same integration-domain rule the
    Media card uses); the media_player goes `unavailable` when the speaker drops,
    so its availability is the right reachability signal."""
    try:
        from .dashboard import _device_service_class, _speaker_media_entity
    except Exception:  # noqa: BLE001 (never let a render hiccup break the picker)
        return None
    try:
        if _device_service_class(dev) != "speaker":
            return None
        return _speaker_media_entity(dev, hass)
    except Exception:  # noqa: BLE001
        return None


def _record_is_zigbee_coordinator(dev: dict[str, Any]) -> bool:
    """True when a RAW backend device record is a Zigbee coordinator.

    The reachability helpers run against the backend record, not the rendered
    picker row. A static-probe coordinator with no native HA integration (the
    SONOFF Dongle-M) is stored with `device_type`/`role` = "unknown" and only
    its `integration_type` names the model; the `zigbee_coordinator` service
    class is attached from the catalog at RENDER time and never persisted. So
    inspecting the raw class fields alone misses it — also match the catalog's
    coordinator integration slugs (`zigbee_coordinator_integrations`, which is
    catalog-driven, so a future networked coordinator is covered with no code
    change here). This omission left the Dongle-M's reachability dot grey: the
    coordinator fallback bailed before it could borrow Z2M's bridge signal."""
    if "zigbee_coordinator" in (
        dev.get("service_class"), dev.get("role"), dev.get("device_type"),
        dev.get("vendor_device_type"), dev.get("type"),
    ):
        return True
    integ = str(dev.get("integration_type") or dev.get("integration") or "").lower()
    if not integ:
        return False
    try:
        from .vendors.zigbee2mqtt import zigbee_coordinator_integrations

        return integ in {s.lower() for s in zigbee_coordinator_integrations()}
    except Exception:  # noqa: BLE001 — never let a catalog hiccup break the picker
        return False


def _zigbee_coordinator_liveness(hass: Any, dev: dict[str, Any]) -> str | None:
    """The Z2M bridge connection-state binary_sensor for a Zigbee coordinator
    that has no native HA integration (the SONOFF Dongle-M ember radio owns no
    entities of its own). Z2M is connected to the bridge only while the radio is
    up, so that entity's availability is the coordinator's reachability signal.
    None when this isn't such a coordinator or no bridge entity exists."""
    if not _record_is_zigbee_coordinator(dev):
        return None
    # The Z2M bridge connection-state is shared: Z2M talks through ONE radio at a
    # time, so a single bridge entity can't say which of several coordinators is
    # live. Only attribute it when exactly one coordinator is registered; with
    # two or more, leave reachability to last_seen rather than green a dead
    # secondary radio off the active one's bridge signal.
    if _registered_zigbee_coordinator_count(hass) > 1:
        return None
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return None
    for ent in ent_reg.entities.values():
        if getattr(ent, "disabled_by", None) is not None:
            continue
        eid = ent.entity_id
        if eid.startswith("binary_sensor.") and "bridge_connection_state" in eid:
            return eid
    return None


def _registered_zigbee_coordinator_count(hass: Any) -> int:
    """How many Zigbee coordinators are registered in the Sensa backend, read
    from the cached coordinator data on the runtime (mirrors the scan in
    `vendors.zigbee2mqtt.zigbee_coordinator_present`). Defaults to 1 when it
    can't introspect, so the single-coordinator fallback still applies."""
    try:
        count = 0
        for runtime in (hass.data.get(DOMAIN, {}) or {}).values():
            if not isinstance(runtime, dict):
                continue
            coord = runtime.get("coordinator")
            devices = (getattr(coord, "data", None) or {}).get("devices", [])
            for device in devices or []:
                if _record_is_zigbee_coordinator(device):
                    count += 1
        return count
    except Exception:  # noqa: BLE001
        return 1


def _classify_device(dev: dict[str, Any]) -> str:
    """The device's canonical service class, resolved from its vendor
    vocabulary. Deferred import — `vendors.service_classes` pulls in the
    catalog, and this module is imported at HA start."""
    from .vendors.service_classes import classify

    return classify(dev) or ""


def _config_entities_for_device(
    hass: Any,
    entry: ConfigEntry,
    registered_dev: dict[str, Any] | None,
    service_class: str | None = None,
) -> list[dict[str, Any]]:
    """Return the editable config fields to render on a row's expanded pane.

    Today: the Teltonika admin password. Resolved to entity_ids via the
    HA entity registry, keyed by the unique_id pattern SensaConfigText
    uses. Returns an empty list for devices without inline config.
    """
    if not registered_dev:
        return []
    integration = registered_dev.get("integration_type") or ""
    device_id = registered_dev.get("device_id") or ""
    if not device_id:
        return []

    try:
        from homeassistant.helpers.entity_registry import (
            async_get as async_get_entity_registry,
        )
    except ImportError:
        return []
    ent_reg = async_get_entity_registry(hass)

    # `required` marks the fields without which the device does not work at
    # all; only those light the picker's red "Not Configured" pill. Everything
    # else on a row is optional and an empty value is a working device.
    fields: list[dict[str, Any]] = []
    if integration == "sensa_teltonika_api":
        # Password only: the username is always `admin` (TELTONIKA_USERNAME).
        # GPS refresh is not offered: it stays at its 1 s default.
        tel_fields = [("password", "Admin password", "password", "text")]
        for key, label, mode, domain in tel_fields:
            unique_id = f"{entry.entry_id}_{device_id}_config_{key}"
            entity_id = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": label,
                    "mode": mode,
                    "required": key == "password",
                    "hint": (
                        "If this is a new Teltonika router, your admin password is "
                        "printed on the back of the router."
                    ),
                })
    elif integration == "sensa_rtsp_camera":
        # An IP camera's stream path and login: never discoverable, always the
        # customer's to enter, applied by the row's Apply button. The path is
        # required; the login is not, since some cameras stream without one.
        for key, label, mode, required in (
            ("stream_path", "Stream Path", "text", True),
            ("username", "Username", "text", False),
            ("password", "Password", "password", False),
        ):
            unique_id = f"{entry.entry_id}_{device_id}_config_{key}"
            entity_id = ent_reg.async_get_entity_id("text", DOMAIN, unique_id)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": label,
                    "mode": mode,
                    "required": required,
                })

    # A Sensa DC Meter: the customer's name for each circuit, shown on the
    # Electric tab's tiles. Optional, so never "Not Configured".
    from .dc_meter import is_dc_meter
    if is_dc_meter(registered_dev):
        from .const import DC_METER_CHANNELS
        for n in DC_METER_CHANNELS:
            unique_id = f"{entry.entry_id}_{device_id}_circuit_{n}_name"
            entity_id = ent_reg.async_get_entity_id("text", DOMAIN, unique_id)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": f"Circuit {n} name",
                    "mode": "text",
                    "required": False,
                })

    # "Home Layout" placement selects — one per surface (Habitation + Cab),
    # each a per-device select (entry.options-backed) rendered as a dropdown on
    # the row (mode: "select", committed immediately, no Save button). Offered
    # for every eligible kind in HOME_LAYOUT_KINDS: heaters, lights, ACs.
    from .const import HOME_LAYOUT_SURFACES, home_layout_kind_for
    resolved = (
        service_class
        or registered_dev.get("service_class")
        or _classify_device(registered_dev)
    )
    kind = home_layout_kind_for(resolved)
    if kind is not None:
        for surface in HOME_LAYOUT_SURFACES:
            unique_id = f"{entry.entry_id}_{device_id}{surface['uid_suffix']}"
            entity_id = ent_reg.async_get_entity_id("select", DOMAIN, unique_id)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": f"{surface['label']} Home Layout",
                    "mode": "select",
                })
            # ...and the sort ORDER within that column, right beneath its own
            # placement dropdown so the pair reads as one setting. A number, not
            # a select: the useful range is open-ended-ish and typing "3" beats
            # picking it from a list. Rendered only when the entity exists, the
            # same rule as the dropdown above.
            order_uid = (
                f"{entry.entry_id}_{device_id}{surface['order_uid_suffix']}"
            )
            order_entity = ent_reg.async_get_entity_id(
                "number", DOMAIN, order_uid,
            )
            if order_entity:
                fields.append({
                    "entity": order_entity,
                    "label": f"{surface['label']} Home Order",
                    "mode": "number",
                })

    # "Temperature Sensor Source" — what an Autoterm heater regulates on in
    # Temperature mode: the panel, the heater, or one of the van's sensors
    # (`select.SensaHeaterTempSourceSelect`; a dropdown whose `labels`
    # attribute names each sensor, so no entity id reaches the row).
    # "Heater Model" first: which Air model the controller is fitted to, so
    # the diagnostics frame decodes with the right map (`autoterm_diag`).
    from .const import HEATER_MODEL_UID_SUFFIX
    model_uid = f"{entry.entry_id}_{device_id}{HEATER_MODEL_UID_SUFFIX}"
    model_entity = ent_reg.async_get_entity_id("select", DOMAIN, model_uid)
    if model_entity:
        fields.append({
            "entity": model_entity,
            "label": "Heater Model",
            "mode": "select",
            # Required: the 2D and 4D decode differently, so an unset model
            # lights "Not Configured" on IoT until the customer picks one.
            "required": True,
        })

    # A Mopeka: "Contents" (what the tank holds, written to the native mopeka
    # entry's medium, which the reading depends on), "Tank Shape", and "Tank
    # Height" when that shape is measured against it. Shape and height are
    # what the % Tank Level scales against, so they are required.
    from .mopeka import (
        MOPEKA_BOTTLE_UID_SUFFIX,
        MOPEKA_CONTENTS_UID_SUFFIX,
        MOPEKA_HEIGHT_UID_SUFFIX,
        device_mac,
        is_top_down,
        needs_height,
        shape_for,
    )
    contents_uid = f"{entry.entry_id}_{device_id}{MOPEKA_CONTENTS_UID_SUFFIX}"
    contents_entity = ent_reg.async_get_entity_id("select", DOMAIN, contents_uid)
    if contents_entity:
        fields.append({
            "entity": contents_entity,
            "label": "Contents",
            "mode": "select",
        })
    bottle_uid = f"{entry.entry_id}_{device_id}{MOPEKA_BOTTLE_UID_SUFFIX}"
    bottle_entity = ent_reg.async_get_entity_id("select", DOMAIN, bottle_uid)
    if bottle_entity:
        fields.append({
            "entity": bottle_entity,
            "label": "Tank Shape",
            "mode": "select",
            "required": True,
        })
        shape = shape_for(hass, entry, device_id, device_mac(registered_dev))
        height_uid = f"{entry.entry_id}_{device_id}{MOPEKA_HEIGHT_UID_SUFFIX}"
        height_entity = ent_reg.async_get_entity_id("number", DOMAIN, height_uid)
        if height_entity and needs_height(shape):
            fields.append({
                "entity": height_entity,
                "label": "Tank Height",
                "mode": "number",
                "slider": True,
                "required": True,
                "hint": (
                    "From the sensor down to the bottom of the tank."
                    if is_top_down(hass, device_mac(registered_dev)) else
                    "Inside height of the tank, or its diameter for a round "
                    "tank on its side."
                ),
            })
    # Diesel only: litres for the heaters' Diesel Days Left (`diesel_use.py`).
    from .mopeka import MOPEKA_CAPACITY_UID_SUFFIX, contents
    capacity_uid = f"{entry.entry_id}_{device_id}{MOPEKA_CAPACITY_UID_SUFFIX}"
    capacity_entity = ent_reg.async_get_entity_id("number", DOMAIN, capacity_uid)
    if capacity_entity and contents(
        hass, entry, device_id, device_mac(registered_dev),
    ) == "diesel":
        fields.append({
            "entity": capacity_entity,
            "label": "Tank Capacity",
            "mode": "number",
            "slider": True,
            "hint": "How many litres the tank holds when full, for the heaters' days of diesel left.",
        })

    # "Frost Protection": the Autoterm box's own select (Off or a frost
    # temperature), so it works with Nomad down. Only on firmware that has it.
    from .const import HEATER_SERVICE_CLASSES
    if resolved in HEATER_SERVICE_CLASSES:
        from .heater_temp_source import firmware_entities
        frost_entity = firmware_entities(hass, registered_dev).get("frost_protection")
        if frost_entity:
            fields.append({
                "entity": frost_entity,
                "label": "Frost Protection",
                "mode": "select",
                # A snowflake, crossed through while Off (the picker swaps
                # them live off the select's state).
                "icon": "mdi:snowflake",
                "icon_off": "mdi:snowflake-off",
                "off_value": "Off",
            })
        # An Air's Frost Heat To: the thermostat a frost start sets (Richard,
        # 2026-10-08: "set the therm to low, e.g. 10C", settable here).
        frost_target = firmware_entities(hass, registered_dev).get("frost_target")
        if frost_target:
            fields.append({
                "entity": frost_target,
                "label": "Frost Heat To",
                "mode": "number",
                "slider": True,
                "hint": "The temperature the heater holds when Frost Protection starts it.",
            })

    from .const import HEATER_TEMP_SOURCE_UID_SUFFIX
    source_uid =f"{entry.entry_id}_{device_id}{HEATER_TEMP_SOURCE_UID_SUFFIX}"
    source_entity = ent_reg.async_get_entity_id("select", DOMAIN, source_uid)
    if source_entity:
        fields.append({
            "entity": source_entity,
            "label": "Temperature Sensor Source",
            "mode": "select",
        })

    # Sensa Fan Temperature Control autopilot: per fan, the sensor it follows and its
    # Start / Max / Critical temperatures, then the device's Night Cap
    # (`fan_autopilot.py`). Each field only when its entity exists.
    if resolved == "fan_controller":
        from .const import FAN_CHANNELS, FAN_TEMP_NUMBERS, fan_autopilot_key
        for ch in FAN_CHANNELS:
            # The name first: it is what the fan card and the rows below call it.
            uid = f"{entry.entry_id}_{device_id}_{fan_autopilot_key(ch, 'name')}"
            entity_id = ent_reg.async_get_entity_id("text", DOMAIN, uid)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": f"Fan {ch} Name",
                    "mode": "text",
                })
            uid = f"{entry.entry_id}_{device_id}_{fan_autopilot_key(ch, 'source')}"
            entity_id = ent_reg.async_get_entity_id("select", DOMAIN, uid)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": f"Fan {ch} Temperature Sensor",
                    "mode": "select",
                })
            for key, (label, *_rest) in FAN_TEMP_NUMBERS.items():
                uid = f"{entry.entry_id}_{device_id}_{fan_autopilot_key(ch, key)}"
                entity_id = ent_reg.async_get_entity_id("number", DOMAIN, uid)
                if entity_id:
                    fields.append({
                        "entity": entity_id,
                        "label": f"Fan {ch} {label}",
                        "mode": "number",
                        "decimals": 1,
                    })
        uid = f"{entry.entry_id}_{device_id}_night_cap"
        entity_id = ent_reg.async_get_entity_id("number", DOMAIN, uid)
        if entity_id:
            fields.append({
                "entity": entity_id,
                "label": "Night Cap",
                "mode": "number",
                "slider": True,
                "hint": (
                    "Top fan speed while Sleep mode is on. Ignored at or above "
                    "a fan's Critical Temperature."
                ),
            })

    # "Show Power In Header" — put this device's live power draw in the Sensa
    # header bar. Offered for every class in HEADER_POWER_CHIP_CLASSES (the
    # Starlink dish today), whose draw is the biggest discretionary load in most
    # vans and so wants to be in the customer's eyeline rather than a tab away.
    # A boolean, so it commits immediately with no Save button.
    # "Header Short Name": the label this device's header chip uses instead of
    # its full name, so shorter names fit more chips. Offered wherever either
    # chip is (`header_chip_eligible`), and placed above the toggles it names.
    # Text, so it takes the row's Save like the other text fields.
    from .const import HEADER_SHORT_NAME_UID_SUFFIX, header_chip_eligible
    if header_chip_eligible(resolved):
        unique_id = f"{entry.entry_id}_{device_id}{HEADER_SHORT_NAME_UID_SUFFIX}"
        entity_id = ent_reg.async_get_entity_id("text", DOMAIN, unique_id)
        if entity_id:
            fields.append({
                "entity": entity_id,
                "label": "Header Short Name",
                "mode": "text",
            })

    from .const import header_power_chip_eligible
    if header_power_chip_eligible(resolved):
        unique_id = (
            f"{entry.entry_id}_{device_id}{HEADER_POWER_CHIP_UID_SUFFIX}"
        )
        entity_id = ent_reg.async_get_entity_id("switch", DOMAIN, unique_id)
        if entity_id:
            fields.append({
                "entity": entity_id,
                "label": "Show Power In Header",
                "mode": "switch",
            })

    # "Show Temperature In Header" — put this device's temperature reading in
    # the Sensa header bar. Offered for every environment device (cabin /
    # fridge / outside probes, Govee and Ruuvi tags, a Cerbo room sensor).
    # Off by default: the header used to carry every one of them and overflowed
    # on a narrow tablet, so the customer picks however many fit.
    # A boolean, so it commits immediately with no Save button.
    from .const import header_temp_chip_eligible
    if header_temp_chip_eligible(resolved):
        unique_id = (
            f"{entry.entry_id}_{device_id}{HEADER_TEMP_CHIP_UID_SUFFIX}"
        )
        entity_id = ent_reg.async_get_entity_id("switch", DOMAIN, unique_id)
        if entity_id:
            fields.append({
                "entity": entity_id,
                "label": "Show Temperature In Header",
                "mode": "switch",
            })

    # Temperature Watch / Fridge / Freezer: flag this device for the matching
    # instinct ("Temperature watch" / "Fridge watch" / "Freezer watch") to
    # cover. Same eligible device set as "Show Temperature In Header" above
    # (reuses `header_temp_chip_eligible`), no separate picker: each instinct
    # discovers every device carrying its flag, with no limit on how many.
    # One loop over the flag table, so the row can never offer a flag its
    # instinct does not read.
    from .const import TEMPERATURE_FLAGS
    if header_temp_chip_eligible(resolved):
        for flag in TEMPERATURE_FLAGS:
            entity_id = ent_reg.async_get_entity_id(
                "switch", DOMAIN, flag.unique_id(entry.entry_id, device_id),
            )
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": flag.label,
                    "mode": "switch",
                })

    # "Light Type" — what the controller's output is wired for. It belongs on
    # the DEVICE, not the light card: it describes the controller, and a WZ5's
    # mode is set on the controller itself with the match button.
    #
    # Offered even though we can usually detect it, because detection reads the
    # controller and the one thing nothing on the network can see is the STRIP.
    # The select defaults to "Auto", so the customer only touches it when what
    # they see is wrong.
    if integration == INTEGRATION_ZIGBEE2MQTT and resolved == "lighting":
        from .const import light_type_select_entity_id
        from .vendors.zigbee2mqtt import zigbee2mqtt_ha_device_id

        ha_dev_id = zigbee2mqtt_ha_device_id(hass, registered_dev)
        if ha_dev_id:
            # The select is keyed on the HA device-registry id, not the Sensa
            # backend device_id every other field here uses — resolve by
            # unique_id like the rest so a missing select emits no field rather
            # than a dead entity_id.
            unique_id = f"{entry.entry_id}_light_type_{ha_dev_id}"
            entity_id = ent_reg.async_get_entity_id("select", DOMAIN, unique_id)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": "Light Type",
                    "mode": "select",
                    # A stable key for the picker to find this field by, so the
                    # RUN LED dot on the row's thumbnail can read the select's
                    # `led_colour`. Keying off the LABEL would break the dot the
                    # day the label is reworded.
                    "role": "light_type",
                })
            else:
                _LOGGER.debug(
                    "Light Type select not yet registered for %s (%s)",
                    device_id, light_type_select_entity_id(ha_dev_id),
                )

    # "LED Strip Profile" -- WHICH STRIP is wired to this controller.
    #
    # The addressable counterpart to Light Type, and the two are mutually
    # exclusive: a pixel controller is never asked what its output is wired for
    # (its strip is RGB by definition), it is asked which strip it is driving.
    #
    # A profile names a bead sequence and a chip type together, because the two
    # are not independent: a four-letter sequence is a four-channel pixel, and
    # the chip type is what makes the controller send four bytes per pixel
    # rather than three. Get that pair wrong and part of the strip does not
    # light at all.
    #
    # The controller's OWN two pickers ride alongside, because `Custom` exists
    # for a strip we have no profile for, and because a customer who has set
    # them by hand should be able to see what they are without opening Z2M.
    # They are Z2M's entities, not ours: this row surfaces them rather than
    # keeping a second copy of the same two settings.
    if integration == INTEGRATION_ZIGBEE2MQTT and resolved == "lighting":
        from .vendors.zigbee2mqtt import zigbee2mqtt_ha_device_id

        ha_dev_id = zigbee2mqtt_ha_device_id(hass, registered_dev)
        if ha_dev_id:
            unique_id = f"{entry.entry_id}_led_strip_{ha_dev_id}"
            entity_id = ent_reg.async_get_entity_id("select", DOMAIN, unique_id)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": "LED Strip Profile",
                    "mode": "select",
                    # Stable key, same reason as `light_type`: the picker finds
                    # this field by role, never by its label.
                    "role": "led_strip_profile",
                })
                # The controller's own pickers, resolved off the HA DEVICE by
                # entity_id suffix. Z2M names them from the device's friendly
                # name, which the customer can change, so matching on the name
                # would lose them the moment a controller is renamed.
                for suffix, label in (
                    ("light_bead_sequence", "Light Bead Sequence"),
                    ("chip_type", "Chip Type"),
                ):
                    own = next(
                        (
                            ent.entity_id
                            for ent in ent_reg.entities.values()
                            if ent.device_id == ha_dev_id
                            and ent.entity_id.startswith("select.")
                            and ent.entity_id.endswith(suffix)
                        ),
                        None,
                    )
                    if own:
                        fields.append({
                            "entity": own,
                            "label": label,
                            "mode": "select",
                            "role": f"led_{suffix}",
                        })

                # PIXEL COUNT. Deliberately NOT part of a profile: it depends on
                # how long a run the customer cut, not on which strip it is, so
                # two vans with the same reel have different values. It ships at
                # 60, which means a longer strip has never been fully addressed
                # and the far end has simply never lit.
                pixels = next(
                    (
                        ent.entity_id
                        for ent in ent_reg.entities.values()
                        if ent.device_id == ha_dev_id
                        and ent.entity_id.startswith("number.")
                        and ent.entity_id.endswith("lightpixel_number_set")
                    ),
                    None,
                )
                if pixels:
                    fields.append({
                        "entity": pixels,
                        "label": "Number of Pixels",
                        "mode": "number",
                        # A length, so dragging suits it. Opt-in per field: a
                        # sort position is a rank you type, and a slider on one
                        # of those is noise.
                        "slider": True,
                        "role": "led_pixel_count",
                    })

    # "Power-On Delay" -- how long the controller is given to boot after its
    # supply relay closes, before the card sends the first turn-on. The relay
    # restores power; the controller then has to boot and rejoin Zigbee, and a
    # command that lands before it does is lost. How long that takes is the
    # customer's hardware, not ours to guess, so it is a number on their row.
    #
    # Offered on EVERY lighting controller, not only one with a relay assigned
    # today: a relay assignment arrives later than the controller does, and a
    # setting that appears only after some other page has been filled in is a
    # setting nobody finds. The hint says when it applies.
    if integration == INTEGRATION_ZIGBEE2MQTT and resolved == "lighting":
        from .const import (
            light_leave_relay_on_unique_id,
            light_power_on_delay_unique_id,
        )
        from .vendors.zigbee2mqtt import zigbee2mqtt_ha_device_id

        ha_dev_id = zigbee2mqtt_ha_device_id(hass, registered_dev)
        if ha_dev_id:
            entity_id = ent_reg.async_get_entity_id(
                "number",
                DOMAIN,
                light_power_on_delay_unique_id(entry.entry_id, ha_dev_id),
            )
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": "Power-On Delay",
                    "mode": "number",
                    # Seconds to one decimal: 0.5 and 0.50 are the same delay,
                    # and a box that renders "0.5000000000000001" off a float
                    # step reads as a fault.
                    "decimals": 1,
                    "role": "light_power_on_delay",
                })
            # ...and whether turning the light OFF also cuts that relay. Right
            # beneath the delay, because the pair describes one thing: what the
            # relay does around a power press.
            leave_on = ent_reg.async_get_entity_id(
                "switch",
                DOMAIN,
                light_leave_relay_on_unique_id(entry.entry_id, ha_dev_id),
            )
            if leave_on:
                fields.append({
                    "entity": leave_on,
                    "label": "Leave Relay On",
                    "mode": "switch",
                    "role": "light_leave_relay_on",
                })

    # Vibration SENSITIVITY -- 1 (most) to 21 (least). It describes one
    # physical unit, so it belongs on that unit's row, next to the reachability
    # dot that says whether the sensor is even talking.
    #
    # Points at the SENSA number, not the Zigbee2MQTT one. Z2M cannot read
    # sensitivity back off the device, so its own `number.*_sensitivity` sits at
    # `unknown` for ever and a row bound to it blanks itself the instant you set
    # it -- which is exactly what "the sensitivity change doesn't work" looked
    # like. Ours remembers what it wrote. See `SensaVibrationSensitivityNumber`.
    if resolved == "vibration_sensor":
        from .const import VIBRATION_SENSITIVITY_UID_SUFFIX
        unique_id = (
            f"{entry.entry_id}_{device_id}{VIBRATION_SENSITIVITY_UID_SUFFIX}"
        )
        entity_id = ent_reg.async_get_entity_id("number", DOMAIN, unique_id)
        if entity_id:
            fields.append({
                "entity": entity_id,
                "label": "Sensitivity",
                "mode": "number",
                # The blank state is genuinely ambiguous without this: the
                # sensor has never been asked, so nobody can say what it is.
                "hint": (
                    "1 is most sensitive, 21 is least. The sensor cannot "
                    "report this value back, so Sensa shows what was last "
                    "set here. Blank means it has never been set."
                ),
            })

    # Orientation -- which way THIS sensor is bolted in. Three selects (Front,
    # Left, Up, each X / X- / Y / Y- / Z / Z-), then the pitch and roll that mapping
    # produces. It lived on Settings -> Vehicle until 2026-08-10, and before
    # that on Settings -> Level; both were the wrong home, because it describes
    # one physical unit rather than the van. Two levelling sensors in the same
    # van can be bolted in facing different ways, so the answer is per-DEVICE
    # and belongs on that device's row. (The singleton measurements -- wheel
    # track, wheelbase, shim tolerance -- stay on Vehicle: one van, one
    # wheelbase, however many sensors are fitted.)
    #
    # Same two-way gate the select platform uses to CREATE these entities
    # (`service_class == "levelling" or vendor_device_type == "van_level"`), or
    # a device that has the selects would not be offered them.
    if (
        resolved == "levelling"
        or registered_dev.get("vendor_device_type") == "van_level"
    ):
        from .const import VAN_LEVEL_LOGICAL_LABELS
        for logical in VAN_LEVEL_LOGICAL_AXES:
            unique_id = f"{entry.entry_id}_{device_id}_{logical}_axis"
            entity_id = ent_reg.async_get_entity_id(
                "select", DOMAIN, unique_id,
            )
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": VAN_LEVEL_LOGICAL_LABELS[logical],
                    "mode": "select",
                })
        # ...and the tilt the mapping produces, as a small top-down bubble level
        # with the front at the top. Without it the customer sets three
        # dropdowns blind. A bubble says WHICH WAY the van tilts, which is the
        # question Front / Left / Up answer: lift the front and it should run
        # to Front, not sideways (a swapped X/Y on the Van Level 6,
        # 2026-10-29). It replaced the Pitch / Roll (orientated) number
        # readouts the same day (Richard: not needed beside the bubble).
        orientated: dict[str, str] = {}
        for axis in ("pitch", "roll"):
            unique_id = f"{entry.entry_id}_{device_id}_{axis}_orientated"
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id:
                orientated[axis] = entity_id
        if len(orientated) == 2:
            fields.append({
                "entity": orientated["pitch"],
                "roll_entity": orientated["roll"],
                "label": "Live tilt",
                "mode": "tilt",
            })
        # ...and the van pictures the Cab -> Level cards draw (top-down / side /
        # rear). The last thing on Settings -> Level, moved here 2026-08-10, and
        # the move is what let that tab go entirely.
        #
        # These are SINGLETONS -- one van, three pictures, held on the hub's own
        # `text.sensa_nomad_level_image_*` entities -- so with two levelling
        # sensors fitted both rows show and write the same three. That is
        # deliberate: they belong to the levelling FEATURE, and the sensor's row
        # is the only place that feature now has. Last on the row, after the
        # per-unit orientation fields, so the row reads unit-first.
        from .const import (
            LEVEL_IMAGE_DEFAULTS,
            LEVEL_IMAGE_LABELS,
            LEVEL_IMAGE_SLOTS,
        )
        for slot in LEVEL_IMAGE_SLOTS:
            unique_id = f"{entry.entry_id}_level_image_{slot}"
            entity_id = ent_reg.async_get_entity_id("text", DOMAIN, unique_id)
            if entity_id:
                fields.append({
                    "entity": entity_id,
                    "label": LEVEL_IMAGE_LABELS.get(slot, slot),
                    "mode": "image",
                    # "" is a real stored value meaning "use the Sensa artwork",
                    # so the row needs the default URL to draw a thumbnail for
                    # it. The library itself (stock + uploads) is fetched by the
                    # card over `sensa_nomad/list_images` when the picker opens,
                    # never embedded here: it would put the whole stock list on
                    # every levelling row of a state attribute.
                    "default": LEVEL_IMAGE_DEFAULTS.get(slot, ""),
                })
    return fields


def _save_button_entity_for_device(
    hass: Any,
    entry: ConfigEntry,
    registered_dev: dict[str, Any] | None,
) -> str:
    """Return the Save button entity_id for devices with inline config."""
    if not registered_dev:
        return ""
    integration = registered_dev.get("integration_type") or ""
    device_id = registered_dev.get("device_id") or ""
    if not device_id:
        return ""

    try:
        from homeassistant.helpers.entity_registry import (
            async_get as async_get_entity_registry,
        )
    except ImportError:
        return ""
    ent_reg = async_get_entity_registry(hass)

    if integration in ("sensa_teltonika_api", "sensa_rtsp_camera"):
        unique_id = f"{entry.entry_id}_{device_id}_save_config"
        entity_id = ent_reg.async_get_entity_id("button", DOMAIN, unique_id)
        if entity_id:
            return entity_id
    if integration == "mopeka":
        # "Tank is full now", once the tank has a shape measured by height.
        from .mopeka import (
            MOPEKA_FULL_UID_SUFFIX,
            device_mac,
            is_top_down,
            needs_height,
            shape_for,
        )
        mac = device_mac(registered_dev)
        # A top-down sensor reads the tank directly: nothing to calibrate.
        if needs_height(shape_for(hass, entry, device_id, mac)) and not is_top_down(hass, mac):
            unique_id = f"{entry.entry_id}_{device_id}{MOPEKA_FULL_UID_SUFFIX}"
            entity_id = ent_reg.async_get_entity_id("button", DOMAIN, unique_id)
            if entity_id:
                return entity_id
    return ""


def _router_setup_for_device(registered_dev: dict[str, Any] | None) -> str:
    """The device_id a row's "Set up network" button reads, or "".

    Teltonika routers only; a managed switch gets its own screen. Every
    router has it: the popup is where its Network Setup is chosen.
    """
    dev = registered_dev or {}
    if dev.get("integration_type") != "sensa_teltonika_api":
        return ""
    from .dashboard import _teltonika_is_switch
    if _teltonika_is_switch(dev):
        return ""
    return str(dev.get("device_id") or "")


def _network_router(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The van's Teltonika router with its admin password saved, or None."""
    return next((d for d in devices if _router_added(d)), None)


def _network_setup_for_device(registered_dev: dict[str, Any] | None,
                              router: dict[str, Any] | None) -> str:
    """The device_id of the router a managed switch's Config Network tile
    opens, or "". A switch is set up from its router's popup, which draws the
    switch's ports under the router."""
    dev = registered_dev or {}
    if dev.get("integration_type") != "sensa_teltonika_api" or router is None:
        return ""
    from .dashboard import _teltonika_is_switch
    if not _teltonika_is_switch(dev):
        return ""
    return str(router.get("device_id") or "")


def _router_added(registered_dev: dict[str, Any] | None) -> bool:
    """A router is added once its admin password is saved. Until then its
    settings popup shows only the name and the password."""
    if not _router_setup_for_device(registered_dev):
        return False
    return bool(((registered_dev or {}).get("config") or {}).get("password"))


def _save_button_label_for_device(
    registered_dev: dict[str, Any] | None,
) -> str:
    """Label for the picker button that presses ``save_button_entity``.

    Teltonika routers and switches read Save; the camera button applies the
    stream settings. "Apply" covers any other inline-config device.
    """
    integration = (registered_dev or {}).get("integration_type") or ""
    if integration == "sensa_teltonika_api":
        return "Save"
    if integration == "sensa_rtsp_camera":
        return "Apply to camera"
    if integration == "mopeka":
        return "Tank is full now"
    return "Apply"


def _pairing_controls_for_device(
    hass: Any,
    entry: ConfigEntry,
    registered_dev: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the pairing status + Pair/Forget actions for a device's expand row.

    Today: the Dometic FreshJet FJX/FJZ. Its ESP32 BLE bridge (adopted by HA's
    ``esphome`` integration) exposes ``Pair Air Conditioner`` / ``Forget Air
    Conditioner`` button entities + an ``Air Conditioner Pairing`` text sensor.
    Nomad doesn't otherwise surface a device's raw ESPHome entities, so we
    resolve them here and hand the picker a small descriptor it renders as a
    read-only status line + two action buttons (each pressed via ``button.press``
    — the Control-side "Pair" trigger that presses the bridge's ``do_pair``).

    Gated to ESPHome air-conditioner rows (the Velit is its own BLE-GATT vendor
    integration, excluded by the ``esphome`` gate). Returns ``{}`` for anything
    else, and for a freshly-added bridge whose button/sensor entities aren't
    registered yet — the next discovery refresh fills it in.
    """
    if not registered_dev:
        return {}

    # Same gate as the climate role-map attach, so both agree on which rows are
    # ESPHome ACs (the FreshJet) vs the Velit (its own vendor integration).
    from . import _esphome_candidate_entry_ids, _is_esphome_air_conditioner

    if not _is_esphome_air_conditioner(registered_dev):
        return {}

    try:
        from homeassistant.helpers.entity_registry import (
            async_get as async_get_entity_registry,
        )
    except ImportError:
        return {}

    candidate_ids = set(_esphome_candidate_entry_ids(hass, registered_dev) or [])
    if not candidate_ids:
        return {}

    ent_reg = async_get_entity_registry(hass)
    pair_entity = forget_entity = status_entity = ""
    for ent in ent_reg.entities.values():
        if ent.config_entry_id not in candidate_ids:
            continue
        if ent.disabled_by is not None:
            continue
        domain = ent.entity_id.split(".", 1)[0]
        object_id = ent.entity_id.split(".", 1)[-1]
        name = (ent.original_name or ent.name or "").lower()
        if domain == "button":
            if "forget" in name or "forget" in object_id:
                forget_entity = ent.entity_id
            elif "pair" in name or "pair" in object_id:
                pair_entity = ent.entity_id
        elif domain == "sensor" and ("pairing" in name or "pairing" in object_id):
            status_entity = ent.entity_id

    if not (pair_entity or forget_entity or status_entity):
        return {}

    actions: list[dict[str, Any]] = []
    if pair_entity:
        actions.append({
            "label": "Pair",
            "icon": "mdi:bluetooth-connect",
            "entity": pair_entity,
            "hint": (
                "Put the air conditioner into pairing mode (hold + and − on its "
                "panel until it shows BL), then press Pair."
            ),
        })
    if forget_entity:
        actions.append({
            "label": "Forget",
            "icon": "mdi:bluetooth-off",
            "entity": forget_entity,
        })

    controls: dict[str, Any] = {"actions": actions}
    if status_entity:
        controls["status"] = {"label": "Pairing", "entity": status_entity}
    return controls


def _freshjet_bonded_mac(
    hass: Any, entry: ConfigEntry, registered_dev: dict[str, Any] | None,
) -> str:
    """The AC MAC the FreshJet bridge is bonded to, '' if unpaired/unknown.

    Reads the bridge's ``Air Conditioner Pairing`` sensor (resolved by
    ``_pairing_controls_for_device``). Its state is exactly one of ``Unpaired`` /
    ``Pairing...`` / the bonded unit's MAC (firmware + sim agree). A bonded MAC is
    the only state that contains ``:``, so that is the reliable "paired" signal —
    it is what gates the display-only AC child (see ``_synthesise_freshjet_child``).
    """
    controls = _pairing_controls_for_device(hass, entry, registered_dev)
    ent_id = (controls.get("status") or {}).get("entity")
    if not ent_id:
        return ""
    st = hass.states.get(ent_id)
    val = (st.state if st else "") or ""
    return val if ":" in val else ""


def _integration_pattern(row: dict[str, Any]) -> str:
    """Return the integration pattern slug for a row.

    Two patterns — matches `docs/dev-guide.md` and
    `docs/system-design/supported-devices.md`:

      - **sensa**: our own vendor module owns the device end-to-end
        (sensa_bms_ble, sensa_starlink_grpc, sensa_teltonika_api). If
        a native HA integration exists we don't want active, we
        suppress it with an Ignore entry (mechanism, not a separate
        pattern). Catalog flag: `ignore_native` (when the native
        integration needs suppressing).
      - **adopt**: HA's native integration owns the device; Sensa just
        discovers and hands off. Covers both the zeroconf/BLE-accepted
        flows (SMLight, Ruuvi, Govee — catalog flag `accept_native`)
        and the ESPHome adoption path (Sensa Bluetooth Proxy, Relay
        Controller — integration == "esphome").

    Discovery method (DHCP, static probe, zeroconf, BLE advert,
    ESPHome HTTP probe) is orthogonal to the integration pattern —
    see the `source` field for that.

    Returned as lowercase slugs. Empty string when there's no
    integration (unknown/unrecognised row).
    """
    integration = (row.get("integration") or "").strip().lower()
    if not integration:
        return ""
    if integration.startswith("sensa_"):
        return "sensa"
    return "adopt"


# --- Level 1 / Level 2 parent-child resolution -----------------------------
# The system-design "Device Levels" concept (docs/system-design/supported-
# devices.md): a Level-2 device (a BLE sensor/BMS, a Zigbee endpoint, the
# FreshJet AC, a future IR-controlled load) only exists because of a Level-1
# infrastructure device (a Bluetooth proxy, a Zigbee coordinator, the FreshJet
# bridge, a future IR transceiver). The picker groups + indents children under
# their parent. Rather than a stored via_device edge, we resolve the link at
# render time from two orthogonal string fields stamped on every row:
#
#   provides_kind — what a Level-1 device OFFERS (bluetooth_proxy, …)
#   parent_kind   — what a Level-2 child NEEDS
#
# A child nests under a provider when child.parent_kind == provider.provides_kind.
# Kinds are INFERRED where possible (service_class → its kind; BLE-only →
# bluetooth_proxy; zigbee2mqtt → zigbee_coordinator) and DECLARED in the catalog
# only for links that can't be inferred (top-level `provides_kind`/`parent_kind`,
# e.g. the FreshJet bridge/AC, a future Maxxfan → ir_transceiver). Absent on both
# sides ⇒ no nesting (today's flat list), so the fields are fully opt-in.

# A Level-1 device's service_class maps 1:1 to the kind it provides. Devices
# whose provider role can't be read off the service_class (the FreshJet bridge)
# declare a top-level `provides_kind` in the catalog instead.
_PROVIDES_KIND_BY_SERVICE_CLASS: dict[str, str] = {
    "bluetooth_proxy": "bluetooth_proxy",
    "zigbee_coordinator": "zigbee_coordinator",
    "ir_transceiver": "ir_transceiver",
}


def _row_catalog_entry(
    row: dict[str, Any], registered_dev: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve a row's catalog entry by SPECIFIC identity (vendor_model), not
    integration — `_find_catalog_by_integration("esphome")` merges every ESPHome
    entry (proxy/relay/heater/FreshJet) into one, which would blur per-device
    `provides_kind`/`parent_kind`. vendor_model is unique per product."""
    vendor_model = (
        (registered_dev or {}).get("vendor_model")
        or (registered_dev or {}).get("model")
        or row.get("vendor_model")
        or ""
    )
    return _find_catalog_by_vendor_model(vendor_model) or {}


def _registered_kind_row(dev: dict[str, Any]) -> dict[str, Any]:
    """The row `_provides_kind` reads for a registered device with no live
    discovery row (the backfill, and the Cerbo children diagnostic).

    A record often stores no `vendor_model` (the Cerbo on .116: `model: null`,
    `device_type: unknown`), so `_row_catalog_entry` found nothing and the
    Cerbo backfilled with `provides_kind: ""`, which drops its children. Carry
    the catalog `vendor_model` when the record's type, or its integration
    alone, names exactly ONE entry. Never the merged first-entry fallback of
    `_find_catalog_by_integration`: on `esphome` that could hand any device
    the FreshJet bridge's `provides_kind`."""
    from .device_catalog import PHYSICAL_DEVICE_CATALOG

    integration = str(dev.get("integration_type") or "")
    row: dict[str, Any] = {"integration": integration}
    wanted = integration.strip().lower()
    on_integration = [
        e for e in PHYSICAL_DEVICE_CATALOG
        if wanted and (e.get("integration") or "").strip().lower() == wanted
    ]
    entry = _find_catalog_by_registered_type(dev) or (
        on_integration[0] if len(on_integration) == 1 else None
    )
    if entry and entry.get("vendor_model"):
        row["vendor_model"] = entry["vendor_model"]
    return row


def _provides_kind(
    row: dict[str, Any], registered_dev: dict[str, Any] | None = None,
) -> str:
    """The infrastructure kind this row PROVIDES (Level 1), or "".

    service_class first (bluetooth_proxy / zigbee_coordinator), else an explicit
    catalog top-level `provides_kind` (the FreshJet bridge)."""
    # The class can live in ANY of these. A backend record persists the class in
    # `role` / `device_type`, NOT `service_class` — that field is attached from
    # the catalog at RENDER time and never stored. Reading `service_class` alone
    # meant the Sensa Bluetooth Proxy (`device_type: "bluetooth_proxy"`)
    # announced NO provides_kind, so every BLE device carrying
    # `parent_kind: "bluetooth_proxy"` (the Velit AC, both Fogstar packs) hunted
    # a parent that never identified itself and rendered flat. Same shape as the
    # Dongle-M's `zigbee_coordinator` below; mirrors the field list in
    # `_record_is_zigbee_coordinator`, so the two agree on where a class lives.
    candidates = (
        (registered_dev or {}).get("service_class"),
        row.get("service_class"),
        (registered_dev or {}).get("role"),
        (registered_dev or {}).get("device_type"),
        (registered_dev or {}).get("vendor_device_type"),
        row.get("device_type"),
        row.get("vendor_device_type"),
    )
    for candidate in candidates:
        if candidate in _PROVIDES_KIND_BY_SERVICE_CLASS:
            return _PROVIDES_KIND_BY_SERVICE_CLASS[candidate]
    # A NETWORKED Zigbee coordinator with no native HA integration (the SONOFF
    # Dongle-M) is invisible to both branches around this one: it persists
    # `role`/`device_type` = "unknown" with `model`/`vendor` = None, so the
    # service-class check above sees nothing and the catalog lookup below
    # (keyed on vendor_model/model) can't resolve an entry. Its
    # `zigbee_coordinator` class is attached from the catalog at RENDER time
    # and never stored.
    #
    # `_record_is_zigbee_coordinator` is the authoritative test — it also
    # matches the catalog's coordinator integration slugs, so a future
    # networked coordinator is covered with no change here. REUSE it rather
    # than re-deriving a narrower version: doing the latter is exactly how
    # this drifted from the reachability path, leaving the coordinator
    # announcing no `provides_kind`, the sole-provider nesting gate unable to
    # fire, and every Zigbee child rendering flat instead of indented.
    if _record_is_zigbee_coordinator({**row, **(registered_dev or {})}):
        return "zigbee_coordinator"
    return _row_catalog_entry(row, registered_dev).get("provides_kind", "") or ""


def _parent_kind(
    row: dict[str, Any], registered_dev: dict[str, Any] | None = None,
) -> str:
    """The infrastructure kind this row DEPENDS ON (Level 2), or "".

    Explicit catalog top-level `parent_kind` first (the FreshJet AC, a future
    Maxxfan); else inferred — a BLE-only device rides on the Bluetooth proxy, a
    zigbee2mqtt device on the coordinator. Reuses `_device_is_ble_only` (the
    authoritative BLE-transport test, incl. the catalog `{"ble"}`-only fallback)."""
    explicit = _row_catalog_entry(row, registered_dev).get("parent_kind", "")
    if explicit:
        return explicit
    # `_device_is_ble_only` reads integration_type/vendor_model/bms_family — feed
    # it the registered device when present, else a probe dict from the row.
    ble_probe = registered_dev or {
        "integration_type": row.get("integration") or row.get("integration_type") or "",
        "vendor_model": row.get("vendor_model") or "",
        "bms_family": row.get("bms_family") or "",
    }
    if _device_is_ble_only(ble_probe):
        return "bluetooth_proxy"
    integration = (
        (registered_dev or {}).get("integration_type")
        or row.get("integration")
        or ""
    ).strip().lower()
    if integration == "zigbee2mqtt":
        return "zigbee_coordinator"
    return ""


def _synthesise_freshjet_child(
    bridge_row: dict[str, Any],
    registered_dev: dict[str, Any] | None = None,
    *,
    bonded_mac: str = "",
) -> dict[str, Any] | None:
    """Return a display-only AC child row nested under a FreshJet bridge, or None.

    The FreshJet reaches HA as ONE ESPHome device — the bridge — which bonds over
    BLE to a Dometic FreshJet AC that has no independent HA presence. To surface
    the system-design parent → child relationship (bridge = Level 1, AC = Level
    2) we synthesise a child ROW derived from the bridge, NOT a second registered
    device, so registration/removal stay single-device and the AC card + climate
    role-map on the bridge are untouched.

    Gated to a REGISTERED bridge that is actually BONDED to an AC — `bonded_mac`
    is the AC's MAC read off the bridge's pairing sensor (see
    ``_freshjet_bonded_mac``); empty means Unpaired / Pairing… / unknown, in which
    case there is no AC to show. The AC is added by the pairing routine (Pair on
    the bridge + BL mode on the unit), NOT by merely adding the bridge — so before
    a bond exists the child must not appear at all. The child is display-only —
    `registered: True` (reach LED, no checkbox), `row_readonly: True` (no
    rename/ignore), no pairing controls (Pair/Forget stays on the bridge). Its
    Remove is `remove_press_entity`, the bridge's Forget button: removing the AC
    unpairs it, and getting it back means pairing again.
    Its reachability mirrors the bridge's (the AC is reachable iff the bridge is).

    The child is named with the bonded AC's own id tag (last-4 of its MAC), plus a
    trailing `SIM` for a simulated unit (the `025E00` sim OUI, same rule as
    ``config_flow`` `append_mac_suffix`) so a fake AC is visibly distinguishable —
    e.g. `Dometic FreshJet FJX/FJZ (#21C7 SIM)` (sim) / `Dometic FreshJet FJX/FJZ (#AB12)`
    (real)."""
    if bridge_row.get("provides_kind") != "freshjet_bridge":
        return None
    if not bridge_row.get("registered"):
        return None
    if not bonded_mac:
        return None
    catalog = _find_catalog_by_vendor_model(
        (registered_dev or {}).get("vendor_model")
        or bridge_row.get("vendor_model")
        or "dometic-fjx-c008"
    ) or {}
    base = catalog.get("child_name") or "Air Conditioner"
    mac_hex = str(bonded_mac).replace(":", "").replace("-", "").upper()
    tag = mac_hex[-4:] if len(mac_hex) >= 4 else ""
    if tag:
        suffix = f"#{tag} SIM" if mac_hex.startswith(SIM_MAC_OUI) else f"#{tag}"
        child_name = f"{base} ({suffix})"
    else:
        child_name = base
    child_name = custom_ac_name(registered_dev) or child_name
    # Removing the AC means unpairing it: the child's Remove presses the
    # bridge's Forget button (the same entity the bridge's Pair/Forget row uses).
    forget_entity = next(
        (
            a.get("entity") or ""
            for a in (bridge_row.get("pairing_controls") or {}).get("actions") or []
            if a.get("label") == "Forget"
        ),
        "",
    )
    return {
        "id": f"{bridge_row.get('id')}:ac",
        "name": child_name,
        "display_name": child_name,
        "category": "climate",
        "service_class": "air_conditioner",
        "source": bridge_row.get("source"),
        "protocol": bridge_row.get("protocol"),
        "integration": "esphome",
        "integration_pattern": bridge_row.get("integration_pattern"),
        # Level 2 — nests under THIS bridge. parent_id (the exact bridge row) is
        # the robust link (multi-bridge safe, no sole-provider gate); parent_kind
        # is the fallback.
        "provides_kind": "",
        "parent_kind": "freshjet_bridge",
        "parent_id": str(bridge_row.get("id") or ""),
        "manufacturer": bridge_row.get("manufacturer") or "",
        "manufacturer_id": "",
        "address": bridge_row.get("address") or "",
        "hostname": bridge_row.get("hostname") or "",
        # The child's own identity is the bonded AC's MAC (not the bridge's).
        "mac_address": bonded_mac or bridge_row.get("mac_address") or "",
        "rssi": None,
        "image": bridge_row.get("image"),
        "registered": True,
        # device_subtype air_conditioner → the AC picker pill; row_readonly makes
        # it non-addable/removable/renamable (see the frontend guard).
        "device_subtype": "air_conditioner",
        "row_readonly": True,
        "remove_press_entity": forget_entity,
        # Renamable although read-only: rename_device stores the name on the
        # bridge (see `custom_ac_name`).
        "renamable": True,
        "liveness_entities": bridge_row.get("liveness_entities") or [],
        "last_seen_utc": bridge_row.get("last_seen_utc"),
        "pairing_controls": {},
        "config_entities": [],
        "save_button_entity": "",
        "ignored": False,
    }


def _synthesise_shelly_light_children(
    hass: Any,
    parent_row: dict[str, Any],
    registered_dev: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One child row per output of a Shelly light controller in four-channel
    "light" profile, or [].

    The outputs are separate lights wired to separate things, so each needs its
    own name. They are HA entities of the one registered device, so like the
    FreshJet AC they are display-only rows that can still be renamed:
    `rename_device` writes the name to the output's entity (see
    `vendors.shelly.shelly_light_outputs`). A one-light profile has nothing to
    name separately from the device, so it gets no children.
    """
    if not parent_row.get("registered") or not registered_dev:
        return []
    integration = str(
        registered_dev.get("integration_type")
        or registered_dev.get("integration")
        or parent_row.get("integration")
        or ""
    ).lower()
    if integration != "shelly":
        return []
    try:
        from homeassistant.helpers import entity_registry as er

        from .vendors.shelly import shelly_light_outputs, shelly_light_row_id

        outputs = shelly_light_outputs(
            hass,
            er.async_get(hass),
            {
                **registered_dev,
                "display_name": (
                    registered_dev.get("display_name")
                    or parent_row.get("display_name")
                ),
            },
        )
    except Exception:  # noqa: BLE001 - never let the child rows break discovery
        return []
    if len(outputs) < 2:
        return []
    parent_id = str(parent_row.get("id") or "")
    return [
        {
            "id": shelly_light_row_id(parent_id, channel),
            # The settings popup's "Advertised name": which terminal this is,
            # still readable once the customer has named it.
            "name": f"Output {channel + 1}",
            "display_name": name,
            "category": parent_row.get("category"),
            "service_class": parent_row.get("service_class"),
            "source": parent_row.get("source"),
            "protocol": parent_row.get("protocol"),
            "integration": "shelly",
            "integration_pattern": parent_row.get("integration_pattern"),
            "provides_kind": "",
            "parent_kind": "",
            "parent_id": parent_id,
            "manufacturer": parent_row.get("manufacturer") or "",
            "manufacturer_id": "",
            "address": "",
            "hostname": "",
            "mac_address": "",
            "rssi": None,
            "image": parent_row.get("image"),
            "registered": True,
            "device_subtype": "lighting",
            "row_readonly": True,
            "renamable": True,
            "liveness_entities": [entity],
            "last_seen_utc": parent_row.get("last_seen_utc"),
            "pairing_controls": {},
            "config_entities": [],
            "save_button_entity": "",
            "ignored": False,
        }
        for channel, entity, name in outputs
    ]


# The Cerbo's own HA-device identifier + its children's identifiers both start
# with this prefix, followed by the Cerbo's backend device_id (see
# vendors/victron_entities.py device_info).
_CERBO_HA_ID_PREFIX = "victron_cerbo_"


# A managed Victron lithium bank (SmartLithium / Lynx-managed LiFePO4)
# publishes these fields on the `com.victronenergy.battery` service that a
# plain SmartShunt / BMV monitor never does (see victron_mqtt_services.py —
# the `lazy` battery tails). Their presence as HA entities is the reliable
# way to tell a managed pack from a shunt at picker-synthesis time, since
# Venus uses the same service name + the HA device model is just "Battery".
# Matched against each child entity's unique_id (which ends with the
# description `key`, e.g. `..._battery_256_state_of_health`).
_LITHIUM_BATTERY_MARKERS: tuple[str, ...] = (
    "state_of_health",
    "balancing",
    "min_cell_voltage",
    "max_cell_voltage",
    "cell_1_voltage",
)


def _cerbo_child_image_file(
    service: str, entity_entries: list[Any], model: str = "",
) -> str:
    """Best-effort per-service image filename for a Cerbo sub-device.

    Starts from the service default (`SERVICE_IMAGES`) and refines where we can
    tell variants apart: the VE.Bus family from the device's reported `model`
    (Venus `ProductName` — an EasyPlus / MultiPlus / MultiPlus-II / Quattro all
    share the `vebus` service, so this is the only discriminator), and a managed
    lithium battery (SmartLithium / Lynx) vs a SmartShunt on the shared
    `battery` service, from the child's entities. "" means no art → the picker
    falls back to the row's icon, then its generic placeholder."""
    from .vendors.victron_mqtt_services import SERVICE_IMAGES, vebus_flavour

    file = SERVICE_IMAGES.get(service, "")
    if service == "vebus":
        _label, flavour_image = vebus_flavour(model)
        # Unrecognised / not-yet-reported → keep the generic vebus art.
        return flavour_image or file
    if service == "battery":
        unique_ids = " ".join(
            str(getattr(e, "unique_id", "") or "") for e in entity_entries
        )
        if any(marker in unique_ids for marker in _LITHIUM_BATTERY_MARKERS):
            return "victron-smartlithium.webp"
    return file


def _cerbo_child_icon(service: str) -> str:
    """Per-service mdi icon for a Cerbo sub-device, or "".

    The picker's fallback when the row has no product photo (temperature, tank,
    grid, PV inverter, generator, DC system ship no art) or when the photo
    fails to load. "" leaves the generic "?" placeholder."""
    from .vendors.victron_mqtt_services import SERVICE_ICONS

    return SERVICE_ICONS.get(service, "")


def _synthesise_cerbo_children(
    hass: Any,
    cerbo_row: dict[str, Any],
    registered_dev: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return read-only child rows for a Cerbo GX's Venus sub-devices, or [].

    A Cerbo registers as ONE Sensa backend device; its sub-devices (battery,
    solar chargers, grid meter, DC system, Multiplus, …) exist only as
    HA-registry devices linked to the Cerbo by `via_device`
    (vendors/victron_entities.py — identifier ``victron_cerbo_<cerbo_id>_<service>_<instance>``,
    ``via_device (DOMAIN, victron_cerbo_<cerbo_id>)``). We surface each as a
    display-only child row so it nests under the Cerbo in the picker. `parent_id`
    is the Cerbo row's id (a SPECIFIC link, so children nest under THIS Cerbo even
    with several present); `row_readonly` drops the checkbox / rename / remove
    (you manage the Cerbo — removing it cascades to these via the existing
    via_device purge). Returns [] for a non-Cerbo/unregistered row or any registry
    miss, so it never breaks discovery."""
    if cerbo_row.get("provides_kind") != "cerbo":
        return []
    if not cerbo_row.get("registered"):
        return []
    cerbo_device_id = (
        (registered_dev or {}).get("device_id")
        or cerbo_row.get("discovery_id")
        or cerbo_row.get("id")
        or ""
    )
    if not cerbo_device_id:
        return []
    try:
        from homeassistant.helpers import (
            device_registry as dr,
        )
        from homeassistant.helpers import (
            entity_registry as er,
        )
    except ImportError:
        return []
    try:
        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001 — never let the self-rows break discovery
        return []

    cerbo_ha = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{_CERBO_HA_ID_PREFIX}{cerbo_device_id}")}
    )
    if cerbo_ha is None:
        return []

    from .vendors.victron_mqtt_services import SERVICE_LABELS

    cerbo_row_id = str(cerbo_row.get("id") or "")
    ident_prefix = f"{_CERBO_HA_ID_PREFIX}{cerbo_device_id}_"
    children: list[dict[str, Any]] = []
    for dev in list(dev_reg.devices.values()):
        if getattr(dev, "via_device_id", None) != cerbo_ha.id:
            continue
        # Parse the Venus service from this child's Sensa identifier
        # (victron_cerbo_<cerbo_id>_<service>_<instance>).
        sensa_ident = next(
            (
                v for (d, v) in dev.identifiers
                if d == DOMAIN and v.startswith(ident_prefix)
            ),
            "",
        )
        service = ""
        if sensa_ident:
            remainder = sensa_ident[len(ident_prefix):]
            service = remainder.rpartition("_")[0] or remainder
        # The device's reported model — Venus `ProductName`, stamped on the HA
        # device by `victron_entities.device_info`. For `vebus` it is the only
        # thing that separates an EasyPlus / MultiPlus / MultiPlus-II / Quattro,
        # so it picks the row's artwork below. Blank on a device created before
        # Venus published it; every fallback treats blank as "unknown", never as
        # a family.
        child_model = str(getattr(dev, "model", "") or "").strip()
        name = (
            getattr(dev, "name_by_user", None)
            or dev.name
            or SERVICE_LABELS.get(service, "Device")
        )
        # ALL of the child device's entities drive the reachability dot — the
        # LED is reachable when ANY is live, so passing the whole set avoids a
        # false-red off a single not-yet-published topic (never key off an
        # arbitrary first entity). The same entries also let us detect device
        # variants for the image (e.g. managed lithium vs SmartShunt).
        try:
            entity_entries = list(
                er.async_entries_for_device(
                    ent_reg, dev.id, include_disabled_entities=False,
                )
            )
        except Exception:  # noqa: BLE001
            entity_entries = []
        liveness = [e.entity_id for e in entity_entries]
        # Give each sub-device its OWN picture (battery / solar / Orion),
        # refined by the reported model (the VE.Bus family) and the child's
        # entities (lithium vs shunt) where variants differ, rather than
        # inheriting the Cerbo's photo; an unmapped service falls back to the
        # row's icon, then the generic picker placeholder.
        service_image_file = _cerbo_child_image_file(
            service, entity_entries, child_model,
        )
        child_image = (
            f"/local/sensa/images/{service_image_file}"
            if service_image_file
            else ""
        )
        children.append({
            "id": f"{cerbo_row_id}:sub:{dev.id}",
            "name": name,
            "display_name": name,
            "category": "electrical",
            "service_class": "",
            "source": cerbo_row.get("source"),
            "protocol": "Victron",
            "integration": "sensa_victron_mqtt",
            "integration_pattern": "sensa",
            # Level 2 — nests under the specific Cerbo row (parent_id), with
            # parent_kind as the kind fallback.
            "provides_kind": "",
            "parent_kind": "cerbo",
            "parent_id": cerbo_row_id,
            "manufacturer": "Victron Energy",
            "manufacturer_id": "",
            "address": "",
            "hostname": "",
            "mac_address": "",
            "rssi": None,
            "image": child_image,  # per-service art; "" → icon, then placeholder
            # mdi fallback the picker draws when there is no photo (temperature
            # / tank / grid / PV / genset / DC system ship no art) or the photo
            # 404s. Never replaces a photo that loads.
            "icon": _cerbo_child_icon(service),
            "registered": True,
            # Venus service → the picker pill (dashboard.py rules); read-only.
            "device_subtype": service,
            "row_readonly": True,
            "liveness_entities": liveness,
            "pairing_controls": {},
            "config_entities": [],
            "save_button_entity": "",
            "ignored": False,
        })
    children.sort(key=lambda r: str(r.get("display_name") or "").lower())
    return children


def _log_cerbo_children_diagnostic(
    hass: Any,
    rendered: list[dict[str, Any]],
    registered_devices: list[dict[str, Any]],
) -> None:
    """TEMPORARY diagnostic (2026-07) for the intermittent "Cerbo children
    disappear on a refresh" report. For every REGISTERED Cerbo, compare the child
    rows actually rendered this cycle against the via_device children HA's device
    registry holds; when the registry HAS children but the picker rendered NONE,
    log a WARNING with the parent row's guard state so the trigger is visible
    on-device — a per-cycle discovery match miss shows `registered: False` /
    `provides_kind: ""` on the parent (or no parent row at all), whereas a
    registry lookup miss shows the Cerbo HA device not found. Read-only; the
    caller swallows any error. Remove once the trigger is confirmed."""
    try:
        from homeassistant.helpers import device_registry as dr
    except ImportError:
        return
    try:
        dev_reg = dr.async_get(hass)
        all_ha = list(dev_reg.devices.values())
    except Exception:  # noqa: BLE001
        return

    for dev in registered_devices or []:
        if _provides_kind(_registered_kind_row(dev), dev) != "cerbo":
            continue
        cerbo_device_id = str(dev.get("device_id") or "")
        if not cerbo_device_id:
            continue
        cerbo_ha = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"{_CERBO_HA_ID_PREFIX}{cerbo_device_id}")}
        )
        # -1 = the Cerbo HA device itself wasn't found (registry lookup miss);
        # else the count of its via_device children (the "expected" children).
        expected = (
            sum(1 for d in all_ha if getattr(d, "via_device_id", None) == cerbo_ha.id)
            if cerbo_ha is not None
            else -1
        )
        # Find the rendered parent row for THIS registered Cerbo (by any id key),
        # so we can report its guard state when the children went missing.
        dmac = _canonical_mac(dev.get("linked_mac_address") or dev.get("mac_address"))
        ddisc = str(dev.get("linked_discovery_id") or dev.get("discovery_id") or "")
        dip = dev.get("ip_address") or ""
        parent = None
        for r in rendered:
            rid = str(r.get("id") or "")
            if ":sub:" in rid:
                continue
            if (
                (rid == cerbo_device_id)
                or (ddisc and (str(r.get("discovery_id") or "") == ddisc or rid == ddisc))
                or (dmac and _canonical_mac(r.get("mac_address")) == dmac)
                or (dip and r.get("address") == dip)
            ):
                parent = r
                break
        parent_id = str(parent.get("id")) if parent else ""
        rendered_kids = sum(
            1
            for r in rendered
            if parent_id and str(r.get("id") or "").startswith(f"{parent_id}:sub:")
        )
        # Only the vanish case: no child rows rendered while the registry has
        # them (or the Cerbo HA device wasn't found). A genuine 0-child Cerbo
        # (expected == 0) is silent.
        if rendered_kids == 0 and expected != 0:
            _LOGGER.warning(
                "cerbo-children DIAGNOSTIC: Cerbo device_id=%s rendered %d child "
                "row(s) this refresh but HA registry via_device children=%s; "
                "parent row=%s",
                cerbo_device_id,
                rendered_kids,
                "cerbo-HA-device-NOT-FOUND" if expected == -1 else expected,
                (
                    {
                        "id": parent.get("id"),
                        "registered": parent.get("registered"),
                        "provides_kind": parent.get("provides_kind"),
                        "source": parent.get("source"),
                    }
                    if parent is not None
                    else "NO-PARENT-ROW-RENDERED"
                ),
            )


async def _resolve_host_ip(hass: Any) -> str:
    """The HA host's primary LAN IP — the address a customer recognises as the
    box — resolved async-safely via the network integration. "" on any failure,
    so the Sensa Core self-row falls back to the backend host."""
    try:
        from homeassistant.components.network import async_get_source_ip
        from homeassistant.components.network.const import PUBLIC_TARGET_IP

        ip = await async_get_source_ip(hass, target_ip=PUBLIC_TARGET_IP)
        return str(ip or "")
    except Exception:  # noqa: BLE001 — best-effort; hostname fallback is fine
        return ""


def _synthesise_core_device(hass: Any, address: str = "") -> dict[str, Any]:
    """Return the always-present 'Sensa Core' self-row for the Devices picker.

    The Sensa Core is the Sensa App backend/server — the add-on on the IoT
    network that does licence validation, discovery and registration. It's not a
    discovered peripheral; it's the system itself, so it's surfaced as a Level-1
    self-row that is ALWAYS in the Added group: auto-present at setup, no scan,
    not addable/removable (`row_readonly`). Its address is the resolved backend
    host (`backend_url.resolve_backend_url`), and the image reflects the host
    hardware — `core-pi.webp` on ARM, `core-nuc.webp` on x86, else
    `core-generic.webp` (a missing file degrades to the picker placeholder).
    Reachability mirrors the licence sensor (present ⇒ the backend is up)."""
    import platform as _platform

    from .backend_url import resolve_backend_url

    # Prefer the resolved HA host LAN IP (a real address the customer recognises,
    # passed in by the sensor's async host-IP lookup). Fall back to the backend
    # host from resolve_backend_url — on HAOS that's the Supervisor container
    # HOSTNAME (e.g. b74b5b85-sensa-app), not an IP, so the IP is the better show.
    host = (address or "").strip()
    if not host:
        try:
            url = resolve_backend_url(hass) or ""
        except Exception:  # noqa: BLE001 — never let the self-row break discovery
            url = ""
        host = (
            url.replace("https://", "").replace("http://", "").split(":", 1)[0]
            if url
            else ""
        )

    # The edition says which Sensa Core was sold: Base and Pro ship the black
    # Pi case, Vision and AI different hardware. Until the licence is read,
    # fall back to the host CPU.
    from .layout_catalog import licence_tier_or_none

    tier = licence_tier_or_none(hass)
    machine = (_platform.machine() or "").lower()
    if tier in ("base", "pro"):
        image_file = "core-base-pro.webp"
    elif tier in ("vision", "ai"):
        image_file = "core-nuc.webp"
    elif machine.startswith(("arm", "aarch")):
        image_file = "core-base-pro.webp"
    elif machine in ("x86_64", "amd64", "x86", "i386", "i686"):
        image_file = "core-nuc.webp"
    else:
        image_file = "core-generic.webp"

    return {
        "id": "sensa-core",
        "name": "Sensa Core",
        "display_name": "Sensa Core",
        "category": "network",
        "service_class": "sensa_core",
        "source": "registered",
        "protocol": "Sensa",
        "integration": "sensa_core",
        "integration_pattern": "sensa",
        "provides_kind": "",
        "parent_kind": "",
        "manufacturer": "Sensa",
        "manufacturer_id": "",
        "address": host,
        "hostname": host,
        "mac_address": "",
        "rssi": None,
        "image": f"/local/sensa/images/{image_file}?v={_IMAGE_CACHE_BUSTER}",
        "registered": True,
        # Level-1 self-device: shows the CORE pill, never a checkbox / rename /
        # remove / ignore (row_readonly). Not counted as a discovered device.
        "device_subtype": "core",
        "row_readonly": True,
        # Pin to the TOP of its (Added) group and give it a standout accent
        # background — it's the system itself, always the first thing shown.
        "pin_top": True,
        "row_accent": True,
        # Reachability proxy — the licence sensor is present whenever the
        # integration + backend are up, so the row's dot reflects backend health.
        "liveness_entities": ["sensor.sensa_nomad_licence"],
        "pairing_controls": {},
        "config_entities": [],
        "save_button_entity": "",
        "ignored": False,
    }


def _catalog_model_images(entry: dict[str, Any]) -> dict[str, str]:
    """Return a catalog entry's `model_images` map.

    Lives under `model_detection` now (grouped with the other
    model-slug-keyed maps `device_name_prefixes` / `display_names` /
    enum/prefix lookups). Fall back to legacy top-level location for
    any catalog that hasn't been migrated.
    """
    detection = entry.get("model_detection") or {}
    return detection.get("model_images") or entry.get("model_images") or {}


def _find_catalog_by_registered_type(dev: dict[str, Any] | None) -> dict[str, Any] | None:
    """The ONE catalog entry for a registered device's integration AND saved type.

    A record often carries no `vendor_model`, and `_find_catalog_by_integration`
    then answers with the first entry on that integration. For `shelly` that is
    the relay, so a Shelly Plus Smoke drew the relay's image and, with no
    `device_subtype`, the picker's RELAY tag. The record's `role` /
    `device_type` is the catalog `vendor_device_type` it was added as, which
    tells the two apart. None unless exactly one entry fits.
    """
    from .device_catalog import PHYSICAL_DEVICE_CATALOG

    if not dev:
        return None
    integration = str(
        dev.get("integration_type") or dev.get("integration") or ""
    ).strip().lower()
    kinds = {
        str(dev.get(k) or "").strip().lower() for k in ("role", "device_type")
    } - {""}
    if not integration or not kinds:
        return None
    matches = [
        entry for entry in PHYSICAL_DEVICE_CATALOG
        if (entry.get("integration") or "").strip().lower() == integration
        and str(entry.get("vendor_device_type") or "").strip().lower() in kinds
    ]
    return matches[0] if len(matches) == 1 else None


def _find_catalog_by_integration(integration: str) -> dict[str, Any] | None:
    """Find a catalog entry for a registered device's integration type.

    When multiple catalog entries share an integration (e.g. Roamer
    Gen 1 and Gen 2 both use `sensa_bms_ble`), returns a synthetic
    entry that MERGES every `model_images` map across them so the
    picker's image lookup works for any classified slug regardless of
    which catalog line it came from. `image` and `vendor_model` take
    the first entry's values — there isn't a single "right" fallback
    when entries disagree, but the generic image is usually
    interchangeable and the vendor_model field is only used as a
    fallback when the device record doesn't carry its own.

    Used by the registered-device backfill path — when a device is only
    surfaced via the coordinator's device list (not an active discovery
    source), the row is synthesised from scratch and needs its `image`
    and `model_images` pulled from the catalog directly.
    """
    from .device_catalog import PHYSICAL_DEVICE_CATALOG

    wanted = (integration or "").strip().lower()
    if not wanted:
        return None

    matches = [
        entry for entry in PHYSICAL_DEVICE_CATALOG
        if (entry.get("integration") or "").strip().lower() == wanted
    ]
    if not matches:
        return None

    # Always return a synthetic entry with `model_images` surfaced at
    # the top level — that keeps every caller on the same shape
    # regardless of whether the integration has one catalog line or
    # many. The actual catalog JSON stores `model_images` nested under
    # `model_detection` (co-located with the other model-slug maps);
    # `_catalog_model_images` abstracts the nesting.
    merged = dict(matches[0])
    model_images: dict[str, str] = {}
    for entry in matches:
        model_images.update(_catalog_model_images(entry))
    if model_images:
        merged["model_images"] = model_images
    return merged


def _find_catalog_by_subtype(
    vendor_model: str, subtype: str
) -> dict[str, Any] | None:
    """Find a catalog entry matching vendor_model + vendor_device_type.

    Used for subtype-aware image resolution — at discovery the DHCP row
    only knows `vendor_device_type: "router"`, but once the vendor module
    has written `config.device_subtype = "switch"` we want the switch
    catalog entry's `image` + `model_images` instead of the router's.

    Returned entry has `model_images` surfaced at top level for
    consistency with `_find_catalog_by_integration` — catalog JSON
    nests it under `model_detection`, callers treat it as flat.
    """
    from .device_catalog import PHYSICAL_DEVICE_CATALOG

    wanted_model = (vendor_model or "").strip().lower()
    wanted_type = (subtype or "").strip().lower()
    if not wanted_model or not wanted_type:
        return None
    for entry in PHYSICAL_DEVICE_CATALOG:
        if (entry.get("vendor_model") or "").strip().lower() != wanted_model:
            continue
        if (entry.get("vendor_device_type") or "").strip().lower() != wanted_type:
            continue
        surfaced = dict(entry)
        model_images = _catalog_model_images(entry)
        if model_images:
            surfaced["model_images"] = model_images
        return surfaced
    return None


def _find_catalog_by_bms_family(bms_family: str) -> dict[str, Any] | None:
    """Find the catalog entry whose `bms_family` matches (e.g. "jbd").

    Second-line fallback in the registered-device backfill path: when
    `vendor_model` isn't stored on the device record (some older Fogstar
    adds landed without it before the BMS adopt flow learned to stamp
    one), `_find_catalog_by_integration("sensa_bms_ble")` would happily
    return the merged catalog entry — whose `image` is the FIRST
    sensa_bms_ble entry (Roamer Gen 1). The user-visible symptom is a
    Fogstar row rendering a Roamer photo. Resolving by family
    ("jbd" → Fogstar entry directly, "jk" → Roamer Gen 2, "vip" →
    Roamer Gen 1) keeps each row pinned to its own image even with no
    vendor_model on the record.

    Returned shape mirrors `_find_catalog_by_vendor_model` — the entry
    is surfaced with `model_images` flattened to the top level.
    """
    from .device_catalog import PHYSICAL_DEVICE_CATALOG

    wanted = (bms_family or "").strip().lower()
    if not wanted:
        return None
    for entry in PHYSICAL_DEVICE_CATALOG:
        if (entry.get("bms_family") or "").strip().lower() != wanted:
            continue
        surfaced = dict(entry)
        model_images = _catalog_model_images(entry)
        if model_images:
            surfaced["model_images"] = model_images
        return surfaced
    return None


def _find_catalog_by_vendor_model(vendor_model: str) -> dict[str, Any] | None:
    """Find the single catalog entry whose `vendor_model` matches.

    The registered-device backfill needs the SPECIFIC catalog line for
    a device, not the integration-wide merge `_find_catalog_by_integration`
    returns. That merge arbitrarily takes the FIRST catalog entry's
    `image`, so for a multi-product integration (`sensa_bms_ble` covers
    Fogstar + Roamer Gen 1 / Gen 2) a registered Roamer would inherit
    whichever entry happens to be first in the catalog — Fogstar's
    generic image, in practice.

    Returned entry has `model_images` surfaced at top level, matching
    `_find_catalog_by_integration` / `_find_catalog_by_subtype`.
    """
    from .device_catalog import PHYSICAL_DEVICE_CATALOG

    wanted = (vendor_model or "").strip().lower()
    if not wanted:
        return None
    for entry in PHYSICAL_DEVICE_CATALOG:
        if (entry.get("vendor_model") or "").strip().lower() != wanted:
            continue
        surfaced = dict(entry)
        model_images = _catalog_model_images(entry)
        if model_images:
            surfaced["model_images"] = model_images
        return surfaced
    return None


def _bms_model_slug_from_display_name(
    display_name: str, catalog_entry: dict[str, Any]
) -> str:
    """Reverse-map a registered device's display_name to its model slug
    via the catalog entry's `model_detection.display_names`.

    Registered BMS devices store `vendor_model` (e.g. `roamer_gen1`),
    not the per-variant slug, so the backfill path can't otherwise pick
    the model-specific image. The display_name, though, IS the
    classifier's own output ("Roamer 12V 200Ah Xtreme (#3655)") — its
    leading product string is one of the catalog's `display_names`
    values. Longest-substring match so a more specific label wins when
    several are substrings of the name.

    Returns "" when nothing matches; the caller falls back to the
    catalog's generic image.
    """
    if not display_name or not catalog_entry:
        return ""
    from .device_catalog import model_slug_for

    return model_slug_for(catalog_entry, display_name)


def _match_model_image(model: str, model_images: dict[str, str]) -> str:
    """Pick a model-specific image filename from a `model_images` map.

    Two-pass match so the catalog can use either exact slugs (the
    long-standing pattern, e.g. `"roamer-gen1-x-12-200"`) OR
    case-insensitive substrings (the new pattern for native-integration-
    sourced model strings, e.g. matching `"500A"` inside HA's
    `"SmartShunt 500A/50mV"`):

      1. Exact case-sensitive match — preserves the existing contract
         for catalogs whose key IS the device's model slug.
      2. Case-insensitive substring search, **longest** matching key wins,
         so a more specific variant ("Pro 4in1") beats a substring of it
         ("4in1"). First-match-wins would make the result depend on JSON key
         order: a `"4in1"` key listed above `"Pro 4in1"` silently served the
         plain RuuviTag image for every Pro 4-in-1 (regression 861864b,
         2026-06-22, fixed 2026-07-20). Mirrors `_match_model_display_name`,
         which has always had this rule, the two MUST agree, or a device
         shows one variant's picture above another variant's name.

    Returns "" when nothing matches; caller falls back to the catalog's
    generic `image`.
    """
    if not model or not model_images:
        return ""
    if model in model_images:
        return model_images[model]
    needle_pool = model.lower()
    best = ""
    best_len = 0
    for key, filename in model_images.items():
        if not key or not filename:
            continue
        if key.lower() in needle_pool and len(key) > best_len:
            best = str(filename)
            best_len = len(key)
    return best


def _match_model_display_name(model: str, display_names: dict[str, str]) -> str:
    """Map a byte-decoded model code/title to its catalog friendly product name.

    The SAME two-pass rule the image lookup (`_match_model_image`) uses — exact
    key, then case-insensitive substring — but returning the friendly
    `display_names` label instead of an image. This is what upgrades a Govee
    advert title ("H5074" / "Govee H5074") to "Govee H5074 Mini Hygrometer", or a
    Ruuvi model ("RuuviTag Pro 2in1") to "Ruuvi Tag Pro 2-in-1". Without it the
    cooperate-BLE picker/registration name stays the raw model code, even though
    the catalog carries the friendly name (the image path used it, the name path
    didn't — the asymmetry this fixes). Longest matching key wins so a more
    specific variant ("Pro 4in1") beats a substring of it ("4in1"). Returns ""
    when nothing matches; the caller keeps the raw model string.
    """
    if not model or not display_names:
        return ""
    if model in display_names:
        return str(display_names[model])
    needle = model.lower()
    best = ""
    best_len = 0
    for key, label in display_names.items():
        if key and label and key.lower() in needle and len(key) > best_len:
            best = str(label)
            best_len = len(key)
    return best


def _catalog_display_names_for_row(row: dict[str, Any]) -> dict[str, str]:
    """The `model_detection.display_names` map for a discovery row's product.

    Resolved by vendor_model first (specific) then integration (govee_ble /
    ruuvitag_ble each have a single catalog entry, so the integration lookup is
    unambiguous). Empty when the product has no per-model name map."""
    vendor_model = row.get("vendor_model") or row.get("model") or ""
    entry = (
        _find_catalog_by_vendor_model(vendor_model)
        or _find_catalog_by_integration(row.get("integration") or "")
        or {}
    )
    return (entry.get("model_detection") or {}).get("display_names") or {}


def _row_verified(
    row: dict[str, Any], registered_dev: dict[str, Any] | None = None
) -> bool | None:
    """Has this exact device been proven on real hardware?

    Tri-state. `None` means the row matched no catalog entry — a stray box on
    the customer's LAN is not a device we claim to support, so it carries no
    flag and the picker leaves it unbadged rather than labelling it unverified.

    Resolved HERE rather than at enrichment time because the model often isn't
    known until the device has been added and polled: a Starlink dish reports
    its generation over gRPC, so the same row reads unverified before it is
    added (we genuinely do not know whether it is a Mini) and verified after.
    """
    from .device_catalog import PHYSICAL_DEVICE_CATALOG, catalog_verified

    catalog_id = row.get("catalog_id")
    if not catalog_id:
        return None
    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("id") == catalog_id:
            return catalog_verified(entry, _model_hint(row, registered_dev))
    return None


def _model_hint(
    row: dict[str, Any], registered_dev: dict[str, Any] | None = None
) -> str:
    """Best available statement of WHICH model this row is.

    Two things key off the model — the product image and the verified flag —
    so they read it from here rather than each deciding for themselves what
    counts as the model.

    Prefer the row's byte-level `detected_model`: it's stamped by
    `_annotate_victron_subtype` / `_annotate_ruuvi_advert` / the DJI + BMS
    classifiers and is the only durable source of the variant substring
    ("500A", "100/50", "4in1") for Adopt-method devices. HA's native
    integration owns their entities and never writes a variant-specific model
    back to the Sensa backend, so `registered_dev.model` stays either empty or
    generic; without this preference a SmartShunt / SmartSolar / Orion XS /
    Ruuvi reverts to the generic catalog image the moment it's added.

    Vendor modules that DO self-enrich (Teltonika, Starlink, BMS) write the
    concrete model slug into `config.model` on first poll, so that is the
    fallback, ahead of the `vendor_model` slug (last-ditch; it names the
    catalog entry, not the model within it).

    Returns "" when the model isn't known yet — an unregistered Starlink is a
    dish of unknown generation until it has been added and polled.
    """
    model = str(row.get("detected_model") or "")
    if not model and registered_dev:
        config = registered_dev.get("config") or {}
        model = (
            registered_dev.get("model")
            or config.get("model")
            or registered_dev.get("vendor_model")
            or ""
        )
    return model


def _pick_image(
    row: dict[str, Any], registered_dev: dict[str, Any] | None = None
) -> str:
    """Return a `/local/sensa/images/<file>` URL for the row's image.

    The generic `image` filename comes from `device_catalog.json` (the
    entry's top-level `image`) and is stamped onto the row by enrichment.
    Override priority:

      1. For a registered device, the vendor integration's reported
         model (Starlink Mini vs Gen 3, SmartSolar 100/50 vs 100/30,
         etc.) → swap the generic image for the matching `model_images`
         entry.
      2. For an unregistered pre-add row, use the row's byte-derived
         `detected_model` (stamped by `_annotate_victron_subtype` when
         the manufacturer_data model_id is catalogued). Means the
         picker shows the correct per-variant image before the
         customer has even added the device.

    The card falls back from `.webp` to `.png` at render time so entries
    can ship either format (or mix).
    """
    name = row.get("image")
    model_images = row.get("model_images") or {}
    # Prefer byte-level `detected_model` from the row — it's stamped by
    # `_annotate_victron_subtype` / `_annotate_ruuvi_advert` and is the
    # only durable source of the variant substring ("500A", "100/50",
    # "4in1") for Adopt-method devices. HA's native integration owns
    # their entities and never writes a variant-specific model back to
    # the Sensa backend, so `registered_dev.model` stays either empty
    # or generic. Without this preference a SmartShunt / SmartSolar /
    # Orion XS / Ruuvi reverts to the generic catalog image the moment
    # it's added.
    model = _model_hint(row, registered_dev)
    if model and model_images:
        override = _match_model_image(model, model_images)
        if override:
            name = override
    if not name:
        return ""
    # Cache-buster — see _IMAGE_CACHE_BUSTER comment at top of file.
    return f"/local/sensa/images/{name}?v={_IMAGE_CACHE_BUSTER}"


def _brand_logo_url(source: dict[str, Any]) -> str:
    """`/local/sensa/images/<file>` URL for a catalog `brand_logo`, or "".

    `source` is a discovery row (stamped by `_enrich_from_catalog`) or, for a
    backfilled registered-only row, the catalog entry itself. The picker draws
    it above the metadata in the expanded row. Empty for the many entries that
    carry no brand mark.
    """
    name = str(source.get("brand_logo") or "")
    if not name:
        return ""
    return f"/local/sensa/images/{name}?v={_IMAGE_CACHE_BUSTER}"


def _registered_macs_by_mac(runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index backend-registered devices by CANONICAL MAC (colon-free, upper)."""
    coordinator = runtime.get("coordinator")
    if not coordinator or not getattr(coordinator, "data", None):
        return {}
    devices = coordinator.data.get("devices", []) or []
    result: dict[str, dict[str, Any]] = {}
    for dev in devices:
        mac = _canonical_mac(
            dev.get("linked_mac_address") or dev.get("mac_address")
        )
        if mac:
            result[mac] = dev
        # A Cerbo GX registered via MQTT-on-LAN stores no MAC (it's keyed on its
        # VRM portal id, `cerbo-<portal>`), but that portal id IS the device's
        # MAC and the Cerbo also beacons over BLE / can raise a native flow under
        # it. Index it by that MAC too, so a duplicate discovery row carrying the
        # raw MAC resolves to the already-added Cerbo instead of showing again.
        portal_mac = _cerbo_portal_canonical_mac(
            dev.get("discovery_id") or dev.get("device_id")
        )
        if portal_mac:
            result.setdefault(portal_mac, dev)
    return result


def _registered_discoveries_by_id(runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
    coordinator = runtime.get("coordinator")
    if not coordinator or not getattr(coordinator, "data", None):
        return {}
    devices = coordinator.data.get("devices", []) or []
    result: dict[str, dict[str, Any]] = {}
    for dev in devices:
        # device_id is included because static-probe registrations (Cerbo GX)
        # don't persist a separate discovery_id — their stable identity rides on
        # the device_id (`cerbo-{portal}`), which equals the discovery row's id.
        # setdefault keeps an explicit linked_discovery_id / discovery_id as the
        # higher-priority key when both are present.
        for key in (
            dev.get("linked_discovery_id"),
            dev.get("discovery_id"),
            dev.get("device_id"),
        ):
            if key:
                result.setdefault(str(key), dev)
    return result


def _registered_devices_by_ip(runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index backend-registered devices by IP. Handles static-probe registrations
    (Starlink) that have no MAC and no linked_discovery_id."""
    coordinator = runtime.get("coordinator")
    if not coordinator or not getattr(coordinator, "data", None):
        return {}
    devices = coordinator.data.get("devices", []) or []
    result: dict[str, dict[str, Any]] = {}
    for dev in devices:
        ip = dev.get("ip_address") or ""
        if ip:
            result[ip] = dev
    return result


def _registered_devices_by_integration(
    runtime: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Index backend-registered devices by integration_type, for the singleton
    integrations (Starlink, eventually a single Teltonika) where the mapping
    discovery row ↔ backend device is 1:1 and MAC/IP alone don't pin it down."""
    coordinator = runtime.get("coordinator")
    if not coordinator or not getattr(coordinator, "data", None):
        return {}
    devices = coordinator.data.get("devices", []) or []
    result: dict[str, dict[str, Any]] = {}
    for dev in devices:
        integration = dev.get("integration_type") or ""
        if integration in ("sensa_starlink_grpc",):
            result[integration] = dev
    return result


class SensaSummarySensor(CoordinatorEntity[SensaDataUpdateCoordinator], SensorEntity):
    """Coordinator-backed Sensa summary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: SensaDataUpdateCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_summary_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        # Integrations suppressed comes from config entry, not backend
        if self.entity_description.key == "integrations_suppressed":
            suppressed = self._entry.data.get("suppressed_integrations", [])
            return len(suppressed)

        data = self.coordinator.data or {}
        summary = data.get("summary", {})
        value = summary.get(self.entity_description.key)

        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except (TypeError, ValueError):
                _LOGGER.warning("Sensa Nomad sensor: invalid timestamp value %s", value)
                return None

        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes for dashboard/debug use."""
        data = self.coordinator.data or {}
        summary = data.get("summary", {})
        discoveries = data.get("discoveries", [])
        devices = data.get("devices", [])

        attributes: dict[str, Any] = {
            "summary_keys": sorted(summary.keys()),
            "discoveries_count": len(discoveries),
            "devices_count": len(devices),
        }

        if self.entity_description.key in {
            "discoveries_total",
            "discoveries_new",
            "discoveries_known",
        }:
            attributes["discoveries"] = _compact_discovery_attributes(discoveries)

        if self.entity_description.key in {
            "devices_total",
            "devices_enabled",
        }:
            attributes["devices"] = _compact_device_attributes(devices)

        if self.entity_description.key == "last_seen_utc":
            attributes["summary"] = summary

        return attributes

    @property
    def available(self) -> bool:
        """Return availability."""
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info for the Sensa Nomad hub."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


def _summarise_check_items(items: list[dict[str, Any]]) -> str:
    """Render '12/12 OK' or '10/12 OK (1 modified, 1 missing)' for a check group."""
    if not items:
        return "—"
    total = len(items)
    ok = sum(1 for f in items if f.get("status") == "ok")
    if ok == total:
        return f"{ok}/{total} OK"
    modified = sum(1 for f in items if f.get("status") == "modified")
    missing = sum(1 for f in items if f.get("status") == "missing")
    parts: list[str] = []
    if modified:
        parts.append(f"{modified} modified")
    if missing:
        parts.append(f"{missing} missing")
    return f"{ok}/{total} OK ({', '.join(parts)})"


class SensaLogSensor(SensorEntity):
    """Lightweight change-ping for both Log tabs.

    State is the Sensa-log entry count; it bumps whenever `_log.sensa_log`
    appends. Each `custom:sensa-log-card` watches this state to know when to
    re-fetch its entries via the `sensa_nomad/get_log` WS command (the entries
    themselves are NOT carried as state attributes — a 1000-entry-per-source
    ring buffer would bloat the recorder and the state machine). No polling: the
    log store pushes via `bind_sensor`. `EntityCategory.DIAGNOSTIC` keeps it
    tidy.

    Deliberately ONE COMBINED count, not one per source (Intelligence → Log vs
    Settings → System Log). It is only a ping, and the `sensa_nomad_log_changed`
    bus event that drives the primary refresh is source-agnostic too, so a split
    sensor would buy nothing: a shared total bumps on ANY write, which means
    each card re-reads on any write — a superset of what it needs, costing one
    in-memory read. The per-surface filter is applied by `sensa_nomad/get_log`.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Log"
    _attr_icon = "mdi:message-text-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_log"
        self.entity_id = "sensor.sensa_nomad_log"
        self._count = 0

    @property
    def native_value(self) -> int:
        return self._count

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def set_count(self, count: int) -> None:
        """Called by the log store on every append/clear. Pushes fresh state so
        the card re-fetches; safe before the entity is added (no-op write)."""
        self._count = count
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from ._log import get_log_store
        store = get_log_store(self.hass)
        if store is not None:
            store.bind_sensor(self)

    async def async_will_remove_from_hass(self) -> None:
        from ._log import get_log_store
        store = get_log_store(self.hass)
        if store is not None:
            store.unbind_sensor(self)
        await super().async_will_remove_from_hass()


class SensaAlarmsSensor(SensorEntity):
    """Backs the Habitation → Alarms tab card + the cross-panel banner.

    State = number of live (active + snoozed) alarms; the `alarms` attribute
    carries the full live-alarm list (key/message/state/raised_at/…). Push-based:
    the alarm store calls `set_alarms` on every raise/snooze/cancel/re-sound. The
    list isn't recorded (it churns on re-sound). The card snoozes/cancels via the
    `sensa_nomad.snooze_alarm` / `cancel_alarm` services and reads via the
    `sensa_nomad/get_alarms` WS command.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Alarms"
    _attr_icon = "mdi:alarm-light"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"alarms"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_alarms"
        self.entity_id = "sensor.sensa_nomad_alarms"
        self._alarms: list[dict[str, Any]] = []
        self._count = 0

    def _live(self) -> tuple[list[dict[str, Any]], int]:
        """Live alarms + active count, read from the store directly so the value
        is correct regardless of whether the push-binding landed (bind no-ops if
        the store wasn't created yet at add time). Falls back to last pushed."""
        from ._alarms import get_alarm_store
        store = get_alarm_store(self.hass) if self.hass is not None else None
        if store is not None:
            return store.snapshot(), store.active_count()
        return self._alarms, self._count

    @property
    def native_value(self) -> int:
        return len(self._live()[0])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alarms, active = self._live()
        return {"alarms": alarms, "active": active}

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def set_alarms(self, alarms: list[dict[str, Any]], active: int) -> None:
        """Called by the alarm store on every change (push path)."""
        self._alarms = list(alarms)
        self._count = active
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from ._alarms import get_alarm_store
        store = get_alarm_store(self.hass)
        if store is not None:
            store.bind_sensor(self)

        # Belt-and-braces: refresh on the alarm bus event too, so the count ping
        # the card watches is right even if the push-binding wasn't established.
        from .const import EVENT_SENSA_ALARMS_CHANGED

        @callback
        def _on_alarms_changed(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SENSA_ALARMS_CHANGED, _on_alarms_changed)
        )
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        from ._alarms import get_alarm_store
        store = get_alarm_store(self.hass)
        if store is not None:
            store.unbind_sensor(self)
        await super().async_will_remove_from_hass()


class SensaSchengenSensor(SensorEntity):
    """Backs the Schengen 90/180 tracker — state = days used in the rolling
    180-day window (0..limit). The attributes carry the full computed usage
    (days remaining, must-leave-by, window start, current country, …) for the
    `custom:sensa-schengen-card` headline, the approaching-limit alert rule, and
    any future header chip.

    Reads the singleton store live (store-backed entity — no push-only attribute
    to drift) and re-writes state on the store's change event, which the sampler
    fires on a new stamp AND on the daily window rollover.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Schengen days used"
    _attr_icon = "mdi:passport"
    _attr_native_unit_of_measurement = "days"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_schengen_days_used"
        self.entity_id = "sensor.sensa_nomad_schengen_days_used"

    def _usage(self) -> dict[str, Any]:
        from ._schengen import get_schengen_store
        store = get_schengen_store(self.hass) if self.hass is not None else None
        if store is None:
            from .const import SENSA_SCHENGEN_LIMIT_DAYS
            return {"enabled": False, "days_used": 0,
                    "days_remaining": SENSA_SCHENGEN_LIMIT_DAYS}
        return store.compute_usage()

    @property
    def native_value(self) -> int:
        return int(self._usage().get("days_used", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        u = self._usage()
        return {
            "enabled": u.get("enabled"),
            "days_remaining": u.get("days_remaining"),
            "limit_days": u.get("limit_days"),
            "window_days": u.get("window_days"),
            "window_start": u.get("window_start"),
            "in_schengen": u.get("in_schengen"),
            "current_country": u.get("current_cc"),
            "must_leave_by": u.get("must_leave_by"),
            "next_day_returns": u.get("next_returns"),
        }

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
        from .const import EVENT_SENSA_SCHENGEN_CHANGED

        @callback
        def _on_changed(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SENSA_SCHENGEN_CHANGED, _on_changed)
        )
        self.async_write_ha_state()


# How often the box re-tries filling an empty household cache. Long enough not
# to hammer the cloud for a customer who has nobody on their account, short
# enough that a van which gains signal mid-afternoon picks its household up
# without anyone doing anything.
_HOUSEHOLD_PRIME_INTERVAL = 15 * 60


class SensaGpsSpeedSensor(SensorEntity):
    """Road speed from the resolved GPS, in mph. The Cab Home speedometer on a
    van with no OBD2 road speed.

    No new rule: it is the movement detector's GPS speed, the receiver's own
    measured speed when the source reports one, else distance between fixes.
    Written every second (`live_gps_speed_mph`), because a speedo that lags
    by the detector's 15 s sample is no speedo (Richard, 2026-10-01).

    Deliberately NO `device_class`: a SPEED sensor gets converted to km/h on a
    metric HA install, and every speed dial in the product reads mph.
    `unknown` when there is no usable pair of fixes (could not ask), never 0.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad GPS Speed"
    _attr_icon = "mdi:speedometer"
    _attr_native_unit_of_measurement = "mph"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import GPS_SPEED_SENSOR_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_gps_speed"
        self.entity_id = GPS_SPEED_SENSOR_ENTITY_ID

    @property
    def native_value(self) -> float | None:
        from .movement import get_movement_detector

        detector = get_movement_detector(self.hass)
        mph = detector.live_gps_speed_mph() if detector is not None else None
        return round(mph, 1) if mph is not None else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from datetime import timedelta

        from homeassistant.helpers.event import async_track_time_interval

        from .const import SIGNAL_MOVEMENT_SAMPLED

        @callback
        def _refresh(*_args: Any) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_MOVEMENT_SAMPLED, _refresh)
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, _refresh, timedelta(seconds=1))
        )


class SensaGpsActualSensor(SensorEntity):
    """WHICH GPS the van is actually reading, right now.

    There are two questions and they are easy to confuse, so they are now two
    entities:

      * `select.sensa_nomad_cab_location_source` is the SETTING -- what the
        customer chose on Settings -> Radar. Only ever changed by the customer.
        It may say "Auto", which names no device at all.
      * this sensor is the ANSWER -- the device that setting resolved to on this
        tick. Auto resolves to the vehicle tracker, else the freshest plausible
        phone.

    Everything that needs a position (the Radar nearest-amenity sensors,
    Tracking, Schengen, the maps) must use the ANSWER. They already do: they all
    call the one function `resolve_nav_source`, which this reads. This entity
    adds no new rule, it only makes the existing answer visible, so it can be
    looked at in Developer Tools or put on a card instead of being something you
    have to reason about.

    State is the human LABEL ("Richard's phone"), because that is what a person
    reading a dashboard wants; the entity_id is an attribute.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad GPS Actual"
    _attr_icon = "mdi:crosshairs-gps"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_gps_actual"
        self.entity_id = "sensor.sensa_nomad_gps_actual"

    def _resolved(self) -> tuple[str, tuple[float, float] | None]:
        try:
            from .device_tracker import resolve_nav_source

            # `record=False`: reading this must not advance the plausibility
            # gate's reference position. A dashboard rendering is not a consumer
            # having used the answer.
            return resolve_nav_source(self.hass, record=False) or ("", None)
        except Exception:  # noqa: BLE001 - a diagnostic must never break setup
            return ("", None)

    def _label_for(self, entity_id: str) -> str:
        """The human name for a tracker, via the select's own source_map.

        Read, never rebuilt: the select owns the label for every source, and a
        second copy here is exactly the "one predicate written twice" trap.
        """
        if not entity_id:
            return ""
        from .const import CAB_LOCATION_SELECT_ENTITY_ID

        sel = self.hass.states.get(CAB_LOCATION_SELECT_ENTITY_ID)
        source_map = (getattr(sel, "attributes", None) or {}).get("source_map") or {}
        if isinstance(source_map, dict):
            for label, eid in source_map.items():
                if eid and eid == entity_id:
                    return str(label)
        # Not in the map (a tracker that has since gone): fall back to the
        # entity's own friendly name rather than showing nothing.
        state = self.hass.states.get(entity_id)
        return str((getattr(state, "attributes", None) or {}).get(
            "friendly_name", entity_id
        ))

    @property
    def native_value(self) -> str:
        """The label of the device in use, or "Unknown" when nothing resolves.

        "Unknown" rather than blank or an error: a van with every tracker asleep
        is a normal state, not a fault.
        """
        entity_id, _pos = self._resolved()
        return self._label_for(entity_id) or "Unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import (
            CAB_LOCATION_AUTO,
            CAB_LOCATION_SELECT_ENTITY_ID,
            CAB_VEHICLE_TRACKER_ENTITY_ID,
        )

        entity_id, pos = self._resolved()
        sel = self.hass.states.get(CAB_LOCATION_SELECT_ENTITY_ID)
        selected = str(getattr(sel, "state", "") or CAB_LOCATION_AUTO)
        # WHY this device, so a surprising answer can be explained without
        # reading the resolver.
        if not entity_id:
            reason = "nothing_resolved"
        elif selected == CAB_LOCATION_AUTO:
            reason = (
                "auto_vehicle"
                if entity_id == CAB_VEHICLE_TRACKER_ENTITY_ID
                else "auto_freshest"
            )
        else:
            # An explicit pick that could not be read falls through to Auto, so
            # the setting and the answer can legitimately disagree here.
            reason = "selected" if self._label_for(entity_id) == selected else "fallback"
        return {
            "source_entity": entity_id,
            "gps_selected": selected,
            "reason": reason,
            "latitude": pos[0] if pos else None,
            "longitude": pos[1] if pos else None,
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
        }


class SensaPeopleSensor(SensorEntity):
    """Backs the Settings → People tab card.

    State = how many people the customer has; the attributes carry everything
    the card draws:

      * `people` — HA's own person records, Sensa's built-in logins removed
        (see people.py: the support account is not a family member).
      * `tracker_options` — the phones assignable to a person, from the SAME
        `_selectable_gps_trackers` the Cab location picker uses, so the two
        surfaces can never disagree about what counts as a phone.
      * `mysensa.members` — the customer's MySensa household, minus anyone
        already added, offered as choices when adding a person. Read from the
        App's local cache, which this sensor quietly fills (see
        `_async_prime_household`) so the household is simply there by the time
        the customer opens the tab.

    Polls because persons and trackers appear and vanish outside this entity (a
    phone that has just paired, a person added on another client), and nothing
    fires an event we could listen to for both.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad People"
    _attr_icon = "mdi:account-group"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True
    # The rows carry names and photo paths. Recording them would write the
    # household into the long-term statistics database for no benefit.
    _unrecorded_attributes = frozenset({"people", "tracker_options", "mysensa"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_people"
        self.entity_id = "sensor.sensa_nomad_people"
        self._people: list[dict[str, Any]] = []
        self._trackers: list[dict[str, str]] = []
        self._mysensa: dict[str, Any] = {"members": []}
        # Monotonic stamp of the last household prime, throttling the retry
        # while the cache is empty. Starts far enough back that the first
        # refresh primes immediately.
        self._last_prime: float = -_HOUSEHOLD_PRIME_INTERVAL
        # Set once a prime's phone-home actually REACHED the cloud. An empty
        # cache after that is an answer ("nobody on this account"), not a
        # missing one, and must not be retried for ever.
        self._prime_answered: bool = False

    @property
    def native_value(self) -> int:
        return len(self._people)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .people import current_driver_person_id

        return {
            "people": list(self._people),
            "tracker_options": list(self._trackers),
            "mysensa": dict(self._mysensa),
            # Who is driving. Read by the Cab people bar to light a face: the
            # bar used to derive that from the location select, which stops
            # being the same question now that the GPS and the driver can
            # legitimately differ.
            "driver_person_id": current_driver_person_id(self.hass),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def _refresh(self) -> None:
        from .people import (
            async_list_people,
            async_prune_dead_trackers,
            async_remove_van_gps_people,
            async_seed_from_trackers,
        )
        from .select import _selectable_gps_trackers

        # Checked on EVERY refresh, so the tab is right whenever the customer
        # opens it and a phone paired five minutes ago already has a person.
        # A van that knows about a phone knows about somebody, and the customer
        # should be RENAMING a list rather than typing one from scratch.
        #
        # Safe to run repeatedly: each tracker is seeded once ever
        # (`PEOPLE_SEEDED_TRACKERS_OPTION`), so a person the customer deleted
        # stays deleted and a settled van writes nothing.
        try:
            # Prune FIRST: a device that has genuinely gone should stop being
            # referenced before we decide who still needs a person.
            await async_prune_dead_trackers(self.hass)
            # Then drop any person the OLD seeder made for the van's own GPS.
            await async_remove_van_gps_people(self.hass)
            await async_seed_from_trackers(self.hass, self._entry)
        except Exception:
            _LOGGER.debug("people sensor: tracker seeding failed", exc_info=True)

        try:
            self._people = await async_list_people(self.hass)
        except Exception:
            _LOGGER.debug("people sensor: listing failed", exc_info=True)

        try:
            self._trackers = [
                {"entity_id": eid, "name": label}
                for eid, label in _selectable_gps_trackers(self.hass)
            ]
        except Exception:
            _LOGGER.debug("people sensor: tracker list failed", exc_info=True)

        # The customer's MySensa household, read from the App's local cache, and
        # offered as choices when adding a person. Only those NOT already added,
        # so the card does not have to work that out and nobody can be added
        # twice.
        try:
            from .people import async_fetch_members

            # Keep imported people in step with MySensa. Cheap and idempotent:
            # it only writes when a name or photo actually differs.
            try:
                from .people import async_sync_imported_people

                await async_sync_imported_people(self.hass)
            except Exception:
                _LOGGER.debug("people sensor: member sync failed", exc_info=True)

            cached = await async_fetch_members(self.hass)
            taken = {p["member_id"] for p in self._people if p.get("member_id")}
            self._mysensa = {
                "members": [
                    m for m in cached if str(m.get("id") or "") not in taken
                ],
            }
            if not cached:
                self._async_prime_household()
        except Exception:  # noqa: BLE001 (never break the tab over this)
            self._mysensa = {"members": []}

    def _async_prime_household(self) -> None:
        """Fill an empty household cache in the background.

        The customer never asks for this: by the time they open the People tab
        their household should already be there. An empty cache simply means we
        have not managed to fetch it yet, so check the licence (the same round
        trip that brings the household down) and let the next poll pick it up.

        Throttled while empty, and it stops the moment the cache fills. A
        once-per-run guard would strand a van that booted in a tunnel and gained
        signal later; priming on every 30s poll would hammer the cloud for a
        customer who simply has nobody on their account.

        It also stops once a prime has REACHED the cloud, because an empty cache
        after a successful check-in is an answer and not a missing one. Without
        that, an account with nobody on it (or a cloud that hands down no
        household url at all) primes every fifteen minutes for ever: ninety-six
        /validate calls a day, which on its own overruns the licensing
        Function's hundred-a-day cap and turns every later check-in into a 429.
        The hourly phone-home daemon still picks up anyone added later.

        Silent by design. No internet is a normal state for a van, not an error
        worth putting on screen: the customer just adds people by hand.
        """
        if self._prime_answered:
            return
        now = time.monotonic()
        if now - self._last_prime < _HOUSEHOLD_PRIME_INTERVAL:
            return
        self._last_prime = now

        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        client = runtime.get("client")
        if client is None:
            return

        async def _prime() -> None:
            from .const import LICENSE_PHONE_HOME_OK

            try:
                status = await client.async_post_license_phone_home()
            except Exception:
                _LOGGER.debug("people sensor: household prime failed", exc_info=True)
                return
            # Only a check-in that actually reached the cloud counts as an
            # answer. Offline, a timeout or a 429 all leave the flag clear so
            # the retry survives a van that gains signal later.
            if isinstance(status, dict) and status.get(LICENSE_PHONE_HOME_OK):
                self._prime_answered = True

        self.hass.async_create_task(_prime())

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._refresh()
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if isinstance(runtime, dict):
            runtime["people_sensor"] = self

    async def async_update(self) -> None:
        await self._refresh()

    async def async_refresh_now(self) -> None:
        """Re-read and push state immediately.

        Called by the People services after a write. Without it the card shows
        nothing until the next poll (up to 30 s), so adding a person looked like
        it had silently failed even though it had worked.
        """
        await self._refresh()
        self.async_write_ha_state()


class SensaTemperatureWatchSensor(SensorEntity):
    """The arithmetic behind the "Temperature watch" instinct.

    HA has no "the reading Y minutes ago" for a trigger to compare against,
    so this sensor samples every device flagged Temperature Watch on its
    Devices row (`dashboard._flagged_temperature_entities`, the same resolver
    the Fridge and Freezer flags use) once a minute and on every change,
    keeps a few hours of (time, degC) samples per entity, and publishes each
    one's change over the routine's window under the `changes` attribute,
    keyed by entity_id (`const.TEMPERATURE_WATCH_CHANGES_ATTR`). The
    generated automation's template triggers only compare those numbers.

    Always degC: an °F or K probe is converted on ingest, so ONE threshold
    on the routine covers a mixed van. State is the largest change under
    way, in degC, or unknown while nothing is primed.

    Unprimed is honest, not zero: an entity with no sample old enough to
    span the window has no entry, and the routine reads that as "could not
    ask" (the template's `float(0)` fallback never crosses a threshold).
    Samples live only in memory: after a restart the window refills before
    anything can fire, the same no-alarm-on-boot rule "Sensor gone quiet"
    follows.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Temperature Watch"
    _attr_icon = "mdi:thermometer-lines"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "°C"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"changes", "watched"})

    SAMPLE_INTERVAL = timedelta(seconds=60)
    _MAX_SAMPLES = 4096

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import TEMPERATURE_WATCH_SENSOR_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_temperature_watch"
        self.entity_id = TEMPERATURE_WATCH_SENSOR_ENTITY_ID
        self._watched: list[str] = []
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._changes: dict[str, float] = {}
        self._window_minutes: int = 0
        self._attr_native_value: float | None = None
        self._unsub_states = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> float | None:
        """The largest change under way, degC, either direction; None (HA
        "unknown") while nothing is primed."""
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import TEMPERATURE_WATCH_CHANGES_ATTR

        return {
            "window_minutes": self._window_minutes,
            "watched": list(self._watched),
            TEMPERATURE_WATCH_CHANGES_ATTR: dict(self._changes),
        }

    def _resolve_sources(self) -> None:
        """Re-read which entities are flagged Temperature Watch. Samples for
        an entity that has just been un-flagged are dropped so re-flagging it
        later starts unprimed rather than alarming on stale history."""
        from .const import TEMPERATURE_WATCH_FLAG
        from .dashboard import _flagged_temperature_entities

        try:
            watched = [
                eid for eid, _label
                in _flagged_temperature_entities(TEMPERATURE_WATCH_FLAG)
            ]
        except Exception:
            _LOGGER.debug("temperature-watch source resolve failed", exc_info=True)
            return
        self._watched = watched
        for eid in list(self._samples):
            if eid not in watched:
                self._samples.pop(eid, None)

    def _resubscribe(self) -> None:
        from homeassistant.helpers.event import async_track_state_change_event

        if self._unsub_states is not None:
            self._unsub_states()
            self._unsub_states = None
        if not self._watched:
            return
        self._unsub_states = async_track_state_change_event(
            self.hass, list(self._watched), self._on_source_change,
        )

    @callback
    def _on_source_change(self, _event: Any) -> None:
        self._recompute()

    async def _async_on_tick(self, _now: Any) -> None:
        self._recompute()

    @staticmethod
    def _celsius(state: Any) -> float | None:
        """A temperature state in degC, or None when unavailable / not a
        number. °F and K probes are converted so one threshold serves all.
        The one implementation lives with the miner autopilot, which reads
        the habitation probe the same way."""
        from .crypto_autopilot import temperature_celsius

        return temperature_celsius(state)

    def _sample(self, now: float) -> None:
        from .instincts_catalog import TEMPERATURE_WATCH_MAX_MINUTES

        keep_after = now - (TEMPERATURE_WATCH_MAX_MINUTES + 2) * 60
        for eid in self._watched:
            value = self._celsius(self.hass.states.get(eid))
            if value is None:
                continue
            samples = self._samples.setdefault(
                eid, deque(maxlen=self._MAX_SAMPLES),
            )
            samples.append((now, value))
            while samples and samples[0][0] < keep_after:
                samples.popleft()

    def _recompute(self, now: float | None = None) -> None:
        """Take a sample and republish every watched entity's change over
        the routine's window: the newest reading minus the last reading
        taken at or before the window's start. No sample that old means no
        entry (unprimed), never a zero."""
        from .instincts_catalog import temperature_watch_window_minutes

        if now is None:
            now = time.time()
        self._sample(now)
        minutes = temperature_watch_window_minutes(dict(self._entry.options))
        cutoff = now - minutes * 60
        changes: dict[str, float] = {}
        for eid in self._watched:
            samples = self._samples.get(eid)
            if not samples:
                continue
            baseline: float | None = None
            for ts, value in samples:
                if ts > cutoff:
                    break
                baseline = value
            if baseline is None:
                continue
            changes[eid] = round(samples[-1][1] - baseline, 2)
        self._changes = changes
        self._window_minutes = minutes
        self._attr_native_value = (
            round(max(abs(v) for v in changes.values()), 1) if changes else None
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resolve_sources()
        self._resubscribe()
        self._recompute()

        @callback
        def _on_regen(*_args: Any) -> None:
            self._resolve_sources()
            self._resubscribe()
            self._recompute()

        async def _async_first_add_retry(_now: Any) -> None:
            _on_regen()

        # Same two signals the Van Level sensor listens on: REGEN_REQUESTED
        # (a flag toggled, a vendor regen) and MODE_ELIGIBILITY_CHANGED (the
        # de-facto "rebuild finished", after `set_dashboard_devices` has
        # stamped the context the resolver reads).
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED, _on_regen,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_MODE_ELIGIBILITY_CHANGED, _on_regen,
            )
        )
        from homeassistant.helpers.event import (
            async_call_later,
            async_track_time_interval,
        )

        # First-add safety net: on a fresh install this sensor can be added
        # before the first rebuild has stamped the dashboard context.
        if not self._watched:
            self.async_on_remove(
                async_call_later(self.hass, 3.0, _async_first_add_retry),
            )
        # The once-a-minute sample: a probe that reports rarely still slides
        # through the window, and a widened window is picked up without a
        # restart. A coroutine, not a bare function: HA runs a plain sync
        # callable in the executor, where async_write_ha_state dies silently.
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_on_tick, self.SAMPLE_INTERVAL,
            )
        )

        def _cleanup() -> None:
            if self._unsub_states is not None:
                self._unsub_states()
                self._unsub_states = None

        self.async_on_remove(_cleanup)


class SensaCryptoAutopilotSensor(SensorEntity):
    """What the miner autopilot is doing and why, for the Crypto card.

    State is the engine's state name (`off` / `waiting` / `mining` / `paused`)
    and `reason` is the one line the card shows ("Waiting: battery 62%, needs
    80%"). Pushed by the engine after every evaluation; the engine is a
    per-run singleton that may start after this entity, so the listener is
    attached lazily and the sensor reads the engine directly on each write
    rather than caching a copy.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Miner Autopilot"
    _attr_icon = "mdi:robot"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import CRYPTO_AUTOPILOT_SENSOR_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_crypto_autopilot"
        self.entity_id = CRYPTO_AUTOPILOT_SENSOR_ENTITY_ID
        self._attached = False

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def _engine(self):
        from .crypto_autopilot import get_crypto_autopilot

        engine = get_crypto_autopilot(self.hass)
        if engine is not None and not self._attached:
            engine.listeners.append(self._on_engine)
            self._attached = True
        return engine

    @callback
    def _on_engine(self) -> None:
        self.async_write_ha_state()

    async def async_update(self) -> None:
        self._engine()

    @property
    def native_value(self) -> str:
        engine = self._engine()
        return engine.state if engine is not None else "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        engine = self._engine()
        if engine is None:
            return {"reason": "Autopilot not running"}
        return {"reason": engine.reason, **engine.snapshot}


class SensaClimateControlSensor(SensorEntity):
    """What Sensa Climate Control is doing: off / heating / cooling / idle /
    waiting (no reading, or nothing nominated yet). `sensor_entity` names the
    nominated sensor so the card reads the temperature live, in the
    customer's unit, and `reason` says why it is waiting. Reads the module's
    status on every write and is pushed by its signal."""

    _attr_has_entity_name = False
    _attr_name = "Climate Control"
    _attr_icon = "mdi:thermostat"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import CLIMATE_CONTROL_STATUS_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_climate_control_status"
        self.entity_id = CLIMATE_CONTROL_STATUS_ENTITY_ID

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        from homeassistant.helpers.dispatcher import async_dispatcher_connect

        from .climate_control import SIGNAL_CLIMATE_CONTROL

        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_CLIMATE_CONTROL, self.async_write_ha_state,
        ))

    @property
    def native_value(self) -> str:
        from .climate_control import status

        return str(status(self._entry).get("state"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .climate_control import status

        return {k: v for k, v in status(self._entry).items() if k != "state"}


class SensaAutomationsSensor(SensorEntity):
    """Backs the Settings → Automations tab card.

    State = number of enabled curated automations; the `automations` attribute
    carries the full catalog merged with the customer's enabled/config choices
    (from `entry.options`) plus whether each one's devices currently resolve.
    The card reads this and calls `sensa_nomad.set_automation` to toggle/config.
    Polls so an automation that needed a not-yet-present device flips to
    `available` once it registers; `set_automation` also pushes state directly.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Automations"
    _attr_icon = "mdi:robot-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True
    _unrecorded_attributes = frozenset({"automations"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_automations"
        self.entity_id = "sensor.sensa_nomad_automations"

    def _rows(self) -> list[dict[str, Any]]:
        from .instinct_order import apply_instinct_order
        from .instincts_catalog import describe_automations
        # In the customer's order (Design Mode drag on the Instincts tab).
        return apply_instinct_order(describe_automations(self.hass, dict(self._entry.options)))

    @property
    def native_value(self) -> int:
        return sum(1 for r in self._rows() if r.get("enabled"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"automations": self._rows()}

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
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if isinstance(runtime, dict):
            runtime["automations_sensor"] = self


class SensaRoutinesSensor(SensorEntity):
    """Backs the Intelligence -> Routines tab card (`sensa-routines-card`).

    Unlike Instincts (a fixed catalog the customer only toggles), a Routine's
    full definition — trigger / conditions / action — is customer-authored,
    so the card needs to read it back to render the list AND repopulate the
    editor when the customer reopens a saved routine. The generated
    `automation.nomad_routine_<id>` entity only exposes on/off state, not the
    structured definition, so this sensor reads `RoutineOverrides` directly
    (the same source `routines.py` builds the YAML from) rather than trying
    to reconstruct it from HA automation entities.

    State = number of enabled routines; the `routines` attribute carries
    every routine (id + name + enabled + trigger + conditions + action).
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Routines"
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True
    _unrecorded_attributes = frozenset({"routines"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_routines"
        self.entity_id = "sensor.sensa_nomad_routines"

    def _rows(self) -> list[dict[str, Any]]:
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        overrides = runtime.get("routine_overrides")
        if overrides is None:
            return []
        return [{"id": rid, **routine} for rid, routine in overrides.all().items()]

    @property
    def native_value(self) -> int:
        return sum(1 for r in self._rows() if r.get("enabled"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"routines": self._rows()}

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
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if isinstance(runtime, dict):
            runtime["routines_sensor"] = self


# Shown when a check is refused because an update is mid-flight, on every
# surface that can start one (the card notice, the service error, the button).
INTEGRITY_OTA_BUSY_MESSAGE = (
    "A software update is running. File integrity can be checked once the "
    "update has finished."
)


def _ota_update_in_flight(hass: HomeAssistant, entry_id: str) -> bool:
    """True while an OTA is mid-flight (requested / pulling / applying / ...).

    An update rewrites the very files the integrity check hashes, so a check run
    during one compares half-replaced files against the old (or new) manifest and
    reports "modified"/"missing" — a FALSE failure the customer reads as their
    system being tampered with. Every entry point is blocked until the update
    settles; the next check then runs against a consistent tree.

    Reads the same runtime OTA mirror the backups-stale sensor's `ota_*`
    attributes expose, and inverts against `OTA_STATES_SETTLED` (done/failed/idle)
    — enumerating the settled end means a NEW updater phase can't silently slip
    through as "not an update".

    A STALE in-flight state does NOT count. The mirror is only refreshed while
    the fast OTA watch runs, and that watch stops at its 600 s ceiling — so a
    wedged updater (one that never writes a terminal state) leaves the mirror
    reading "pulling" with nothing left to correct it. Without this bound the
    gate would refuse every check FOREVER, on precisely the box whose update just
    failed and most needs one. Applying the writer's own freshness rule
    (`OTA_STATUS_STALE_SECONDS`, and a missing/malformed `ts` = a torn write) means
    the refusal always releases itself. Fails OPEN, like every other Sensa gate.
    """
    runtime = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    status = runtime.get(RUNTIME_OTA_STATUS) or {}
    state = status.get("state")
    if not state or state in OTA_STATES_SETTLED:
        return False
    ts = status.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    return (time.time() - ts) <= OTA_STATUS_STALE_SECONDS


class SensaIntegritySensor(SensorEntity, RestoreEntity):
    """Reports whether integration and generated files match their recorded checksums.

    State: "unknown" (never run) | "ok" | "modified" | "missing" | "error".
    Only updates on demand — press the paired button to trigger a fresh check.

    `RestoreEntity` keeps the last check's verdict + summary rows (state +
    `last_checked` / `manifest_version` / the summary strings) across an HA
    restart. Without it the sensor reset to "unknown" with `last_checked: None`
    on every restart, so the card read "Not checked yet" even though a check had
    run — the customer-reported bug. The raw `static_files` / `generated_files`
    lists aren't restored (they're `_unrecorded_attributes`), but the card only
    reads the summaries; the next Run Check repopulates everything.
    """

    _VALID_STATES = frozenset({"ok", "modified", "missing", "error", "unknown"})
    # Summary attributes worth restoring (the card's readout rows). The raw file
    # lists are unrecorded, so they aren't available to restore anyway.
    _RESTORED_ATTRS = (
        "last_checked",
        "manifest_version",
        "static_summary",
        "generated_summary",
        "failed_files_summary",
        "error",
    )

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Integrity"
    _attr_icon = "mdi:shield-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"static_files", "generated_files"})

    _MANIFEST_FILENAME = "sensa_manifest.json"
    _CHECKSUMS_FILENAME = ".sensa_checksums.json"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_integrity"
        # Pin the entity_id rather than leaving it to name-slugification, so the
        # dashboard card / button that hardcode `sensor.sensa_nomad_integrity`
        # resolve it reliably (same pattern as SensaCurrentModeSensor).
        self.entity_id = "sensor.sensa_nomad_integrity"
        self._state: str = "unknown"
        # Pre-seed so the dashboard's File Integrity attribute rows render
        # "—" before the first Run Check rather than showing as missing
        # (which renders as a blank row in HA's entities card).
        self._results: dict[str, Any] = {
            "last_checked": None,
            "manifest_version": None,
            "static_summary": "—",
            "generated_summary": "—",
            "failed_files_summary": "—",
            "static_files": [],
            "generated_files": [],
            "error": None,
        }

    @property
    def native_value(self) -> str:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._results)

    async def async_added_to_hass(self) -> None:
        """Restore the last check's verdict + summary rows across HA restarts so
        the card doesn't read 'Not checked yet' after every reboot."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state not in self._VALID_STATES:
            return
        self._state = last.state
        attrs = last.attributes or {}
        for key in self._RESTORED_ATTRS:
            if key in attrs:
                self._results[key] = attrs[key]

    async def async_update(self) -> None:
        config_dir = self.hass.config.config_dir
        results = await self.hass.async_add_executor_job(self._run_check, config_dir)
        self._results = results
        all_files = results.get("static_files", []) + results.get("generated_files", [])
        if results.get("error"):
            self._state = "error"
        elif any(f["status"] == "missing" for f in all_files):
            self._state = "missing"
        elif any(f["status"] == "modified" for f in all_files):
            self._state = "modified"
        elif all_files:
            self._state = "ok"
        else:
            self._state = "unknown"

    async def async_run_check(self) -> None:
        """Run the check now and push fresh state to the frontend.

        Called in-process by the `sensa_nomad.run_integrity_check` service and
        the paired button. Going through the instance (not
        `homeassistant.update_entity`) means the recheck doesn't depend on the
        caller holding entity CONTROL permission or on the entity_id resolving
        in the state machine — the two ways the old trigger silently no-op'd.
        Mirrors the discovery sensor's direct-refresh pattern.

        Refuses while a software update is in flight: the update is rewriting the
        files being hashed, so a check would report false "modified"/"missing"
        failures. This is the one choke point every caller goes through (service,
        button, card), so the gate lives here rather than in each of them.
        """
        from homeassistant.exceptions import HomeAssistantError

        if _ota_update_in_flight(self.hass, self._entry.entry_id):
            raise HomeAssistantError(INTEGRITY_OTA_BUSY_MESSAGE)
        await self.async_update()
        self.async_write_ha_state()

    def _run_check(self, config_dir: str) -> dict[str, Any]:
        integration_dir = Path(__file__).parent
        manifest_path = integration_dir / self._MANIFEST_FILENAME

        results: dict[str, Any] = {
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "manifest_version": None,
            "static_files": [],
            "generated_files": [],
            "error": None,
        }

        if not manifest_path.exists():
            results["error"] = "sensa_manifest.json not found — run the build pipeline to generate it"
            return results

        try:
            manifest = json.loads(manifest_path.read_bytes())
        except Exception as exc:  # noqa: BLE001
            results["error"] = f"Failed to read sensa_manifest.json: {exc}"
            return results

        results["manifest_version"] = manifest.get("version")

        for rel_path, expected_hash in manifest.get("files", {}).items():
            file_path = integration_dir / rel_path
            if not file_path.exists():
                results["static_files"].append({"path": rel_path, "status": "missing"})
                continue
            actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
            results["static_files"].append({
                "path": rel_path,
                "status": "ok" if actual_hash == expected_hash else "modified",
            })

        checksums_path = Path(config_dir) / "nomad" / self._CHECKSUMS_FILENAME
        if checksums_path.exists():
            try:
                stored: dict[str, str] = json.loads(checksums_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                stored = {}
            for rel_path, stored_hash in stored.items():
                file_path = Path(config_dir) / rel_path
                if not file_path.exists():
                    results["generated_files"].append({"path": rel_path, "status": "missing"})
                    continue
                actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
                results["generated_files"].append({
                    "path": rel_path,
                    "status": "ok" if actual_hash == stored_hash else "modified",
                })

        # Pre-render summary strings the dashboard's File Integrity card
        # consumes via plain `attribute` rows. Folding the old "File Check
        # Results" markdown card into the same entities card means the
        # dashboard can't run Jinja over `static_files` / `generated_files`
        # itself, so we hand back the formatted text it needs.
        results["static_summary"] = _summarise_check_items(results["static_files"])
        results["generated_summary"] = _summarise_check_items(results["generated_files"])
        failed = [
            f["path"]
            for f in (*results["static_files"], *results["generated_files"])
            if f.get("status") != "ok"
        ]
        results["failed_files_summary"] = ", ".join(failed) if failed else "—"

        return results


class SensaCurrentModeSensor(SensorEntity, RestoreEntity):
    """Tracks the active Nomad Mode.

    Native value is the mode display name (e.g. "Driving") so the
    Habitation badge row reads naturally; the slug lives in the
    `slug` attribute for code that needs the stable identifier.

    Updated by `_async_handle_activate_mode` via the
    `SIGNAL_CURRENT_MODE_CHANGED` dispatcher signal — same channel
    drives the per-mode `binary_sensor.sensa_nomad_mode_<slug>_active`
    entities so the Overview tile auto-tints in lock-step.

    RestoreEntity persists the value across HA restarts: a customer
    who activated "Sleep" before a reboot still sees the badge / tile
    showing Sleep when HA comes back up, even though no scene
    activation has fired this session.
    """

    _attr_has_entity_name = False
    _attr_name = "Current Mode"
    _attr_icon = "mdi:tune-variant"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_current_mode"
        self.entity_id = "sensor.sensa_nomad_current_mode"
        self._slug: str = ""
        # Mirrored from `ModeOverrides.get_startup_mode()` — the customer's
        # "default mode on HA startup" choice. Read by the Mode Editor
        # webcard via `attributes.startup_mode` so the dropdown seeds
        # without a service round-trip.
        self._startup_slug: str = ""
        # Mirrored from `ModeOverrides.modes_payload()`: every scene (preset
        # + customer-created) in the customer's display order, with
        # enabled / custom / locked flags. Every mode card renders from
        # `attributes.modes`, so a reorder, hide or new scene on the Scenes
        # tab flows to the Home / Cab tiles and Mobile pills live.
        self._modes: list[dict[str, Any]] = [
            {"slug": m.slug, "name": m.name, "icon": m.icon,
             "enabled": True, "custom": False, "locked": m.slug in LOCKED_MODE_SLUGS,
             "hold": m.slug in HOLD_MODE_SLUGS}
            # A gated preset is off until the overrides store says otherwise.
            for m in MODES if m.slug not in GATED_MODES
        ]

    @property
    def native_value(self) -> str | None:
        if not self._slug:
            return None
        for m in self._modes:
            if m.get("slug") == self._slug:
                return str(m.get("name") or self._slug)
        mode = get_mode(self._slug)
        return mode.name if mode else self._slug

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "slug": self._slug,
            "startup_mode": self._startup_slug,
            "mode_order": [m["slug"] for m in self._modes],
            "modes": [dict(m) for m in self._modes],
        }

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

        # Restore last known slug across restarts. Persisted via the
        # `slug` attribute (state itself stores the human-readable
        # name); reading the attribute keeps the round-trip stable
        # even if the catalog's display label is renamed later.
        last_state = await self.async_get_last_state()
        if last_state is not None:
            stored_slug = (last_state.attributes or {}).get("slug")
            if isinstance(stored_slug, str) and stored_slug:
                self._slug = stored_slug

        # Subscribe to mode-change broadcasts. Sensor + 8 binary
        # sensors all listen on the same signal so a single
        # async_dispatcher_send updates everything atomically.
        @callback
        def _on_change(slug: str) -> None:
            self._slug = slug or ""
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CURRENT_MODE_CHANGED, _on_change,
            )
        )

        # Seed the startup-mode attribute from the persisted overrides
        # store so the Mode Editor dropdown reflects the saved choice
        # immediately after HA start (before the customer touches the
        # service). Subsequent edits flow through SIGNAL_STARTUP_MODE_CHANGED.
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        overrides = runtime.get("mode_overrides")
        if overrides is not None:
            self._startup_slug = overrides.get_startup_mode() or ""
            self._modes = overrides.modes_payload()

        @callback
        def _on_startup_change(slug: str) -> None:
            self._startup_slug = slug or ""
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STARTUP_MODE_CHANGED, _on_startup_change,
            )
        )

        @callback
        def _on_modes_change(modes: list[dict[str, Any]]) -> None:
            self._modes = [dict(m) for m in modes]
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_MODES_CHANGED, _on_modes_change,
            )
        )


class SensaModeEligibilitySensor(SensorEntity):
    """Eligible-entity source for the Mode Editor's Add-entity dropdown.

    State holds the count of eligible entities. Attributes carry:
      - `entities`: ordered list of eligible entity_ids — the dropdown
        source the webcard reads.
      - `rules`: per-rule traces (action, match, matched_count,
        matched_entities) for the debug panel gated behind
        `switch.sensa_nomad_debug_mode`. Off in production → no panel.

    Updated via `SIGNAL_MODE_ELIGIBILITY_CHANGED` after every dashboard
    regen. The integration computes eligibility right after each
    `set_dashboard_devices` so the rule predicates see the freshest
    `vendor_device_type` mapping.

    Marked DIAGNOSTIC so the eligibility set itself can never include
    this sensor (the default rules drop diagnostic-categorised
    entities).
    """

    _attr_has_entity_name = False
    _attr_name = "Mode Eligibility"
    _attr_icon = "mdi:filter-variant"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # The editor reads these live; their history is worthless, and on a full
    # van they pass the recorder's 16KB attribute cap and log on every write.
    _unrecorded_attributes = frozenset(
        {"entities", "entity_names", "entity_types", "rules"}
    )

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mode_eligibility"
        self.entity_id = "sensor.sensa_nomad_mode_eligibility"
        self._entities: list[str] = []
        self._entity_names: dict[str, str] = {}
        self._entity_types: dict[str, dict[str, str]] = {}
        self._rule_traces: list[dict[str, Any]] = []

    @property
    def native_value(self) -> int:
        return len(self._entities)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "entities": list(self._entities),
            # entity_id → display name (relay channels carry their Switching-
            # tab label). The Mode Editor uses this for both the Add dropdown
            # and the scene rows so they show the same meaningful name.
            "entity_names": dict(self._entity_names),
            # entity_id → {label, icon, category}: what the entity is, for the
            # Scenes popup's picker (a relay channel's assigned device type, a
            # Sensa device's service class, else the domain).
            "entity_types": {k: dict(v) for k, v in self._entity_types.items()},
            "rules": list(self._rule_traces),
        }

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

        # Pull the latest snapshot from runtime so the sensor isn't
        # stuck at "0 eligible" if eligibility was computed before
        # this entity finished adding (race on first integration load).
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        snapshot = runtime.get("mode_eligibility_snapshot")
        if isinstance(snapshot, tuple) and len(snapshot) in (3, 4):
            entities, names, traces = snapshot[:3]
            types = snapshot[3] if len(snapshot) == 4 else {}
            if isinstance(entities, list):
                self._entities = list(entities)
            if isinstance(names, dict):
                self._entity_names = dict(names)
            if isinstance(traces, list):
                self._rule_traces = list(traces)
            if isinstance(types, dict):
                self._entity_types = dict(types)

        @callback
        def _on_change(entities: Any, names: Any, traces: Any, types: Any = None) -> None:
            self._entities = list(entities) if isinstance(entities, list) else []
            self._entity_names = dict(names) if isinstance(names, dict) else {}
            self._rule_traces = list(traces) if isinstance(traces, list) else []
            self._entity_types = dict(types) if isinstance(types, dict) else {}
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_MODE_ELIGIBILITY_CHANGED, _on_change,
            )
        )


class SensaRoutineEligibilitySensor(SensorEntity):
    """Eligible-entity source for the Routine Editor's When / And If / Then
    Do entity pickers.

    State holds the combined count of device + service eligible entities.
    Attributes carry three separate lists (the picker groups its dropdown by
    these) plus one shared display-name map:
      - `device_entities`: van/vendor entities, for the "Device" trigger
        group and the "And If" condition picker (broader than
        `action_entities` — read-oriented, includes sensor/binary_sensor).
      - `service_entities`: built-in HA entities (sun, weather) for the
        "Service" trigger group.
      - `action_entities`: the narrower actionable-only set for "Then Do" —
        the same set the Mode Editor uses for scene membership.
      - `entity_names`: entity_id -> display name, covering all three lists.
      - `entity_devices`: entity_id -> {device_id, device_name} for every
        `action_entities` member with an owning HA device — backs Then Do's
        Device-target mode (group by device, target `{device_id: ...}`).
      - `presence_entities`: the `person` records, for the "Presence" group.
      - `alarm_entities`: the alarm binary sensor, for the "Alarms" group.
      - `entity_types`: entity_id -> {label, icon, category, category_label,
        device_name, control} — what the entity IS, so every picker in the
        popup groups by category title and shows a name, never an entity_id.

    Updated via `SIGNAL_ROUTINE_ELIGIBILITY_CHANGED`, broadcast from the same
    call site as `SIGNAL_MODE_ELIGIBILITY_CHANGED` (right after every
    `set_dashboard_devices`), since both are computed from the same device
    list. Marked DIAGNOSTIC like its Mode Editor counterpart.
    """

    _attr_has_entity_name = False
    _attr_name = "Routine Eligibility"
    _attr_icon = "mdi:filter-variant"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Same reason as the Mode Eligibility sensor: read live, never from history,
    # and too large for the recorder's 16KB attribute cap.
    _unrecorded_attributes = frozenset(
        {
            "device_entities", "service_entities", "action_entities",
            "entity_names", "entity_devices", "entity_types",
            "presence_entities", "alarm_entities",
        }
    )

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_routine_eligibility"
        self.entity_id = "sensor.sensa_nomad_routine_eligibility"
        self._device_entities: list[str] = []
        self._service_entities: list[str] = []
        self._action_entities: list[str] = []
        self._entity_names: dict[str, str] = {}
        self._entity_devices: dict[str, dict[str, str]] = {}
        self._entity_types: dict[str, dict[str, str]] = {}
        self._presence_entities: list[str] = []
        self._alarm_entities: list[str] = []

    @property
    def native_value(self) -> int:
        return len(self._device_entities) + len(self._service_entities)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_entities": list(self._device_entities),
            "service_entities": list(self._service_entities),
            "action_entities": list(self._action_entities),
            "entity_names": dict(self._entity_names),
            "entity_devices": dict(self._entity_devices),
            "entity_types": dict(self._entity_types),
            "presence_entities": list(self._presence_entities),
            "alarm_entities": list(self._alarm_entities),
        }

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

        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        snapshot = runtime.get("routine_eligibility_snapshot")
        if isinstance(snapshot, tuple) and len(snapshot) >= 5:
            device, service, action, names, entity_devices = snapshot[:5]
            entity_types = snapshot[5] if len(snapshot) > 5 else None
            presence = snapshot[6] if len(snapshot) > 6 else None
            alarms = snapshot[7] if len(snapshot) > 7 else None
            if isinstance(device, list):
                self._device_entities = list(device)
            if isinstance(service, list):
                self._service_entities = list(service)
            if isinstance(action, list):
                self._action_entities = list(action)
            if isinstance(names, dict):
                self._entity_names = dict(names)
            if isinstance(entity_devices, dict):
                self._entity_devices = dict(entity_devices)
            if isinstance(entity_types, dict):
                self._entity_types = dict(entity_types)
            if isinstance(presence, list):
                self._presence_entities = list(presence)
            if isinstance(alarms, list):
                self._alarm_entities = list(alarms)

        @callback
        def _on_change(
            device: Any, service: Any, action: Any, names: Any,
            entity_devices: Any = None, entity_types: Any = None,
            presence: Any = None, alarms: Any = None,
        ) -> None:
            self._device_entities = list(device) if isinstance(device, list) else []
            self._service_entities = list(service) if isinstance(service, list) else []
            self._action_entities = list(action) if isinstance(action, list) else []
            self._entity_names = dict(names) if isinstance(names, dict) else {}
            self._entity_devices = dict(entity_devices) if isinstance(entity_devices, dict) else {}
            self._entity_types = dict(entity_types) if isinstance(entity_types, dict) else {}
            self._presence_entities = list(presence) if isinstance(presence, list) else []
            self._alarm_entities = list(alarms) if isinstance(alarms, list) else []
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_ROUTINE_ELIGIBILITY_CHANGED, _on_change,
            )
        )


# --- Velit Rooftop AC telemetry sensors ---------------------------------
# The Velit is one `climate.*` control whose current/target temperature, fan
# speed, etc. live as ATTRIBUTES. These companion sensors surface a few of
# those readings as standalone, history-recorded numbers so they can be graphed
# or used as automation triggers — sharing the climate entity's HA device. Each
# entry: the coordinator data key it reads + how to present it.
_VELIT_SENSOR_DESCS: tuple[dict[str, Any], ...] = (
    {
        "key": "current_temperature",
        "data": "current_temp_c",
        "name": "Current Temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
        "icon": None,
    },
    {
        "key": "target_temperature",
        "data": "target_temp_c",
        "name": "Target Temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
        "icon": "mdi:thermostat",
    },
    {
        "key": "fan_speed",
        "data": "fan_speed",
        "name": "Fan Speed",
        "device_class": None,
        "unit": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
        "icon": "mdi:fan",
    },
)


class VelitRooftopACSensor(CoordinatorEntity, SensorEntity):
    """One telemetry reading off a Velit Rooftop AC coordinator, exposed as a
    standalone sensor on the same HA device as the climate control entity.

    Reads a single key from the coordinator's data dict (see
    `VelitRooftopACCoordinator` data shape). `device_info` mirrors
    `VelitRooftopACClimate` EXACTLY (same `velit_rooftop_ac_{device_id}`
    identifier + name) so the sensor groups onto the AC's device rather than
    creating a second one.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Any,
        entry_id: str,
        device_id: str,
        display_name: str,
        mac: str,
        desc: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._desc = desc
        self._device_id = device_id
        self._attr_unique_id = f"{entry_id}_velit_rooftop_ac_{device_id}_{desc['key']}"
        self._attr_name = desc["name"]
        # Pin the entity_id so a re-add doesn't orphan a user's history /
        # automations (same per-device pinned-id pattern as the climate entity).
        self.entity_id = velit_sensor_entity_id(device_id, desc["key"])
        if desc.get("device_class") is not None:
            self._attr_device_class = desc["device_class"]
        if desc.get("unit") is not None:
            self._attr_native_unit_of_measurement = desc["unit"]
        if desc.get("state_class") is not None:
            self._attr_state_class = desc["state_class"]
        if desc.get("icon"):
            self._attr_icon = desc["icon"]
        if desc.get("precision") is not None:
            self._attr_suggested_display_precision = desc["precision"]
        last4 = mac.replace(":", "").upper()[-4:] if mac else ""
        name = display_name or "Velit Rooftop AC"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"velit_rooftop_ac_{device_id}")},
            "name": f"{name} (#{last4})" if last4 else name,
            "manufacturer": "Velit",
            "model": "Rooftop AC",
        }

    @property
    def native_value(self) -> float | int | None:
        data = self.coordinator.data or {}
        return data.get(self._desc["data"])


def build_velit_sensors(
    coordinator: Any,
    entry_id: str,
    device_id: str,
    display_name: str,
    mac: str,
) -> list[SensorEntity]:
    """Companion telemetry sensors (current/target temperature, fan speed) for
    one Velit Rooftop AC. Called by `_async_start_velit_rooftop_ac` (live first
    add) and the sensor platform setup (reload after the coordinator started)."""
    return [
        VelitRooftopACSensor(
            coordinator, entry_id, device_id, display_name, mac, desc,
        )
        for desc in _VELIT_SENSOR_DESCS
    ]


def build_levelling_derived_sensors(
    entry: ConfigEntry, device: dict[str, Any]
) -> list[SensorEntity]:
    """Orientated + calibrated pitch/roll sensors plus shim heights for one
    van-level device.

    Exposed so the discovery picker's add-dispatch can create these for a
    levelling sensor registered after initial setup.
    """
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    display = device.get("display_name") or device.get("name") or "Van Level"
    sensors: list[SensorEntity] = [
        SensaLevellingOrientatedSensor(entry, device_id, display, "pitch"),
        SensaLevellingOrientatedSensor(entry, device_id, display, "roll"),
        SensaLevellingCalibratedSensor(entry, device_id, display, "pitch"),
        SensaLevellingCalibratedSensor(entry, device_id, display, "roll"),
    ]
    sensors.extend(
        SensaLevellingShimSensor(entry, device_id, display, wheel)
        for wheel in VAN_LEVEL_WHEELS
    )
    sensors.append(SensaLevellingShimMaxSensor(entry, device_id, display))
    sensors.append(SensaLevellingLowestWheelSensor(entry, device_id, display))
    return sensors


class SensaLevellingOrientatedSensor(SensorEntity):
    """Vehicle pitch or roll, derived from the firmware's raw accel x/y/z by
    applying the customer's axis mapping (front / left / up → lid axis
    X, X-, Y, Y-, Z or Z-).

    Math (lifted directly from the BlackPaw Van Level Pro template package):
      mapped_x = raw[forward_axis] * forward_sign  (vehicle forward)
      mapped_y = raw[left_axis] * left_sign        (vehicle left)
      mapped_z = raw[up_axis] * up_sign            (vehicle up)
      norm = sqrt(x² + y² + z²)
      pitch = atan2(-x/norm, sqrt((y/norm)² + (z/norm)²)) * 180/π   (nose-up +)
      roll  = atan2(y/norm, z/norm) * 180/π                          (left-down +)

    Using the gravity vector directly handles ANY mounting orientation
    correctly — the firmware's own pitch/roll bake in the default
    forward=X / up=Z mounting, so they're wrong if the device is rotated.
    """

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "°"
    _attr_should_poll = False
    _attr_suggested_display_precision = 1

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
            f"{entry.entry_id}_{device_id}_{axis}_orientated"
        )
        self._attr_name = f"{device_name} {axis.title()} Orientated"
        self._attr_icon = (
            "mdi:angle-acute" if axis == "pitch" else "mdi:angle-obtuse"
        )
        self.entity_id = van_level_orientated_sensor_entity_id(device_id, axis)
        self._attr_native_value: float | None = None
        self._accel_entities: dict[str, str] = {}
        self._unsub_states = None

    def _resolve_sources(self) -> None:
        """Look up the raw accel x/y/z entity_ids from the device's role-map."""
        from .dashboard import _dashboard_devices
        for d in _dashboard_devices:
            if str(d.get("device_id") or d.get("id") or "") != self._device_id:
                continue
            role_map = d.get("levelling_entities") or {}
            self._accel_entities = {
                "x": role_map.get("accel_x") or "",
                "y": role_map.get("accel_y") or "",
                "z": role_map.get("accel_z") or "",
            }
            self._accel_entities = {
                k: v for k, v in self._accel_entities.items() if v
            }
            return

    def _resubscribe(self) -> None:
        """(Re)subscribe to state changes on the source entities."""
        from homeassistant.helpers.event import async_track_state_change_event

        if self._unsub_states is not None:
            self._unsub_states()
            self._unsub_states = None
        watched: list[str] = list(self._accel_entities.values())
        for logical in VAN_LEVEL_LOGICAL_AXES:
            watched.append(
                van_level_axis_select_entity_id(self._device_id, logical)
            )
        if not watched:
            return
        self._unsub_states = async_track_state_change_event(
            self.hass, watched, self._on_source_change,
        )

    @callback
    def _on_source_change(self, _event: Any) -> None:
        self._recompute()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resolve_sources()
        self._resubscribe()
        self._recompute()

        @callback
        def _on_regen(*_args: Any) -> None:
            self._resolve_sources()
            self._resubscribe()
            self._recompute()

        # SIGNAL_DASHBOARD_REGEN_REQUESTED fires BEFORE the rebuild (vendor
        # modules request a regen via this signal) — listen so we drop stale
        # subscriptions early.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED, _on_regen,
            )
        )
        # SIGNAL_MODE_ELIGIBILITY_CHANGED is the de-facto "rebuild finished"
        # signal: `_async_compute_and_broadcast_eligibility` fires it
        # immediately after `set_dashboard_devices` writes the updated
        # `_dashboard_devices` (with `levelling_entities` populated). Listen
        # so the sensor re-resolves its raw-accel entity_ids on every
        # rebuild — without this the first resolve runs before the role-map
        # is populated and the readout stays at `--` until the next
        # vendor-driven regen.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_MODE_ELIGIBILITY_CHANGED, _on_regen,
            )
        )

        # First-add safety net: on a freshly-added device the rebuild path
        # races platform setup (this sensor's async_added_to_hass might run
        # AFTER the post-rebuild signal already fired). Schedule one short
        # retry so a fresh install picks up the role-map without waiting
        # for the next device add/remove.
        if not self._accel_entities:
            from homeassistant.helpers.event import async_call_later
            self.async_on_remove(
                async_call_later(self.hass, 3.0, lambda _now: _on_regen()),
            )

        # Final on-remove: release the state-change subscription.
        def _cleanup() -> None:
            if self._unsub_states is not None:
                self._unsub_states()
                self._unsub_states = None
        self.async_on_remove(_cleanup)

    def _read_float(self, entity_id: str) -> float | None:
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(st.state)
        except (ValueError, TypeError):
            return None

    @callback
    def _recompute(self) -> None:
        import math

        # Raw accel components. Bail to unknown if any are missing.
        physical: dict[str, float] = {}
        for axis_letter in ("x", "y", "z"):
            eid = self._accel_entities.get(axis_letter)
            if not eid:
                if self._attr_native_value is not None:
                    self._attr_native_value = None
                    self.async_write_ha_state()
                return
            v = self._read_float(eid)
            if v is None:
                if self._attr_native_value is not None:
                    self._attr_native_value = None
                    self.async_write_ha_state()
                return
            physical[axis_letter] = v

        # Map logical axes (forward / left / up) → physical (x/y/z, signed).
        mapped: dict[str, float] = {}
        for logical in VAN_LEVEL_LOGICAL_AXES:
            axis_st = self.hass.states.get(
                van_level_axis_select_entity_id(self._device_id, logical)
            )
            chosen = (
                axis_st.state
                if axis_st is not None and axis_st.state in VAN_LEVEL_AXIS_OPTIONS
                else VAN_LEVEL_DEFAULT_AXIS_MAP[logical]
            )
            sign = -1.0 if chosen.endswith("-") else 1.0
            mapped[logical] = sign * physical.get(chosen[0].lower(), 0.0)

        forward = mapped["forward"]
        left = mapped["left"]
        up = mapped["up"]
        if self._axis == "pitch":
            value = math.degrees(
                math.atan2(-forward, math.sqrt(left * left + up * up))
            )
        else:  # roll
            value = math.degrees(math.atan2(left, up))
        rounded = round(value, 2)
        if rounded != self._attr_native_value:
            self._attr_native_value = rounded
            self.async_write_ha_state()


class SensaLevellingCalibratedSensor(SensorEntity):
    """Orientated pitch/roll with the per-device offset subtracted. This is
    the customer-facing "how level is the vehicle" reading."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "°"
    _attr_should_poll = False
    _attr_suggested_display_precision = 1

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
            f"{entry.entry_id}_{device_id}_{axis}_calibrated"
        )
        self._attr_name = f"{axis.title()} Calibrated"
        self._attr_icon = (
            "mdi:angle-acute" if axis == "pitch" else "mdi:angle-obtuse"
        )
        self.entity_id = van_level_calibrated_sensor_entity_id(device_id, axis)
        self._attr_native_value: float | None = None

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
        from homeassistant.helpers.event import async_track_state_change_event

        watched = [
            van_level_orientated_sensor_entity_id(self._device_id, self._axis),
            van_level_offset_entity_id(self._device_id, self._axis),
        ]
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, watched, self._on_source_change,
            )
        )
        self._recompute()

    @callback
    def _on_source_change(self, _event: Any) -> None:
        self._recompute()

    def _read_float(self, entity_id: str) -> float | None:
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(st.state)
        except (ValueError, TypeError):
            return None

    @callback
    def _recompute(self) -> None:
        orientated = self._read_float(
            van_level_orientated_sensor_entity_id(self._device_id, self._axis)
        )
        offset = self._read_float(
            van_level_offset_entity_id(self._device_id, self._axis)
        )
        if orientated is None:
            new_value = None
        else:
            new_value = round(orientated - (offset or 0.0), 2)
        if new_value != self._attr_native_value:
            self._attr_native_value = new_value
            self.async_write_ha_state()


def _shim_heights_cm(
    pitch_deg: float, roll_deg: float, wheelbase_cm: float, track_cm: float
) -> dict[str, float]:
    """Compute the per-wheel height (cm) of each corner relative to the centre,
    given vehicle pitch/roll (degrees) and geometry (cm).

    The shim VALUE for each wheel is then `(max(heights) - height_of_wheel)` —
    how much to raise that corner to match the highest corner. Math lifted
    from the BlackPaw Van Level Pro package: `z_fl = L·tan(pitch) + W·tan(roll)`
    with L = wheelbase/2 and W = track/2 (both in metres so the resulting
    z is in metres; multiplied by 100 by callers to get cm).
    """
    import math

    half_l = (wheelbase_cm / 100.0) / 2.0
    half_w = (track_cm / 100.0) / 2.0
    a = math.tan(math.radians(pitch_deg))
    b = math.tan(math.radians(roll_deg))
    return {
        "FL": half_l * a + half_w * b,
        "FR": half_l * a + (-half_w * b),
        "RL": (-half_l * a) + half_w * b,
        "RR": (-half_l * a) + (-half_w * b),
    }


class _SensaLevellingShimBase(SensorEntity):
    """Shared plumbing for shim-derived sensors. Reads pitch_calibrated +
    roll_calibrated + wheelbase + track, recomputes on any change."""

    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._device_name = device_name
        self._unsub_states = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"van_level_{self._device_id}")},
            "name": self._device_name,
            "manufacturer": "Sensa",
            "model": "Van Level",
        }

    def _watched_entities(self) -> list[str]:
        return [
            van_level_calibrated_sensor_entity_id(self._device_id, "pitch"),
            van_level_calibrated_sensor_entity_id(self._device_id, "roll"),
            van_level_geometry_entity_id("wheelbase_cm"),
            van_level_geometry_entity_id("wheel_track_cm"),
        ]

    def _read_float(self, entity_id: str) -> float | None:
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(st.state)
        except (ValueError, TypeError):
            return None

    def _resolve_heights(self) -> dict[str, float] | None:
        """Return the four corner heights in cm, or None if any source is
        missing / invalid (caller should set state to None in that case)."""
        pitch = self._read_float(
            van_level_calibrated_sensor_entity_id(self._device_id, "pitch")
        )
        roll = self._read_float(
            van_level_calibrated_sensor_entity_id(self._device_id, "roll")
        )
        wheelbase = self._read_float(
            van_level_geometry_entity_id("wheelbase_cm")
        )
        track = self._read_float(van_level_geometry_entity_id("wheel_track_cm"))
        if (
            pitch is None or roll is None
            or wheelbase is None or track is None
            or wheelbase <= 0 or track <= 0
        ):
            return None
        return {
            wheel: height_m * 100.0
            for wheel, height_m in _shim_heights_cm(
                pitch, roll, wheelbase, track,
            ).items()
        }

    @callback
    def _on_source_change(self, _event: Any) -> None:
        self._recompute()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from homeassistant.helpers.event import async_track_state_change_event

        self._unsub_states = async_track_state_change_event(
            self.hass, self._watched_entities(), self._on_source_change,
        )

        def _cleanup() -> None:
            if self._unsub_states is not None:
                self._unsub_states()
                self._unsub_states = None
        self.async_on_remove(_cleanup)

        self._recompute()

    @callback
    def _recompute(self) -> None:
        raise NotImplementedError


class SensaLevellingShimSensor(_SensaLevellingShimBase):
    """Per-wheel shim height (cm): how much to raise this corner so it
    matches the highest corner. The customer-facing 'put X cm under this
    wheel' reading."""

    _attr_native_unit_of_measurement = "cm"
    _attr_icon = "mdi:car-info"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        wheel: str,
    ) -> None:
        super().__init__(entry, device_id, device_name)
        self._wheel = wheel.upper()
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}_shim_{self._wheel.lower()}_cm"
        )
        self._attr_name = f"{device_name} Shim {self._wheel}"
        self.entity_id = van_level_shim_sensor_entity_id(device_id, self._wheel)
        self._attr_native_value: float | None = None

    @callback
    def _recompute(self) -> None:
        heights = self._resolve_heights()
        if heights is None:
            new_value = None
        else:
            zmax = max(heights.values())
            new_value = round(zmax - heights[self._wheel], 1)
        if new_value != self._attr_native_value:
            self._attr_native_value = new_value
            self.async_write_ha_state()


class SensaLevellingShimMaxSensor(_SensaLevellingShimBase):
    """The largest shim height across all four wheels — the customer's
    'level / not level' threshold compares this against shim_tolerance."""

    _attr_native_unit_of_measurement = "cm"
    _attr_icon = "mdi:angle-obtuse"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(entry, device_id, device_name)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_shim_max_cm"
        self._attr_name = f"{device_name} Shim Max"
        self.entity_id = van_level_shim_max_sensor_entity_id(device_id)
        self._attr_native_value: float | None = None

    @callback
    def _recompute(self) -> None:
        heights = self._resolve_heights()
        if heights is None:
            new_value = None
        else:
            zmax = max(heights.values())
            shims = [zmax - h for h in heights.values()]
            new_value = round(max(shims), 1)
        if new_value != self._attr_native_value:
            self._attr_native_value = new_value
            self.async_write_ha_state()


class SensaLevellingLowestWheelSensor(_SensaLevellingShimBase):
    """The wheel that needs the largest shim — one of FL/FR/RL/RR (or None
    when sources are missing). Drives the 'Lift FL ~ 2.3 cm' status copy."""

    _attr_icon = "mdi:steering"

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(entry, device_id, device_name)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_lowest_wheel"
        self._attr_name = f"{device_name} Lowest Wheel"
        self.entity_id = van_level_lowest_wheel_sensor_entity_id(device_id)
        self._attr_native_value: str | None = None

    @callback
    def _recompute(self) -> None:
        heights = self._resolve_heights()
        if heights is None:
            new_value: str | None = None
        else:
            # "Lowest" in this context = the wheel that needs the BIGGEST
            # shim (i.e. the corner that sits lowest relative to the
            # highest corner). Iteration order matches VAN_LEVEL_WHEELS so
            # ties are stable.
            zmax = max(heights.values())
            new_value = max(
                VAN_LEVEL_WHEELS, key=lambda w: zmax - heights[w],
            )
        if new_value != self._attr_native_value:
            self._attr_native_value = new_value
            self.async_write_ha_state()
