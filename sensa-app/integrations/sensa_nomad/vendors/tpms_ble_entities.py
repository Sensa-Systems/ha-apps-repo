"""Entities for a Sensa-owned BLE tyre-pressure (TPMS) sensor.

One HA device per sensor (each cap sensor has its own MAC). Sensors map
onto the coordinator's value keys; the Wheel Position select is the
customer's answer to "which tyre is this on", because no TPMS format
carries a position.

Readings are `RestoreSensor`s: a parked van's sensors are asleep, so
after an HA restart the last reading comes back (with its `last_seen`)
instead of every tyre reading Unavailable until the van moves.

Device identifier `(DOMAIN, f"tpms_ble_{device_id}")`; unique IDs
`{entry}_tpms_ble_{device_id}_{key}` — the role map and removal purge
key on that prefix.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    DOMAIN,
    TYRE_POSITION_LABELS,
    TYRE_POSITION_OPTION,
    TYRE_POSITION_UNSET,
    TYRE_POSITIONS,
)
from .tpms_ble import (
    FORMAT_DJTPMS,
    FORMAT_FOBO,
    FORMAT_ITPMS,
    FORMAT_MICHELIN,
    FORMAT_SYTPMS,
    FORMAT_TPMSII,
)

# Formats whose frames carry an alarm bit worth an entity.
ALARM_FORMATS = frozenset({FORMAT_TPMSII, FORMAT_ITPMS})

_FORMAT_LABELS: dict[str, tuple[str, str]] = {
    FORMAT_DJTPMS: ("OBEST", "DJTPMS tyre sensor"),
    FORMAT_TPMSII: ("Generic", "TPMSII tyre sensor"),
    FORMAT_SYTPMS: ("Generic", "SYTPMS tyre sensor"),
    FORMAT_MICHELIN: ("Michelin", "TMS tyre sensor"),
    FORMAT_ITPMS: ("WODHMIEY", "ITPMS tyre sensor"),
    FORMAT_FOBO: ("Salutica", "FOBO Tyre"),
}


def tpms_device_info(device_id: str, mac: str, tpms_format: str | None) -> dict[str, Any]:
    last4 = mac.replace(":", "").upper()[-4:] if mac else ""
    manufacturer, model = _FORMAT_LABELS.get(tpms_format or "", ("Generic", "Tyre sensor"))
    return {
        "identifiers": {(DOMAIN, f"tpms_ble_{device_id}")},
        "name": f"Tyre Sensor (#{last4})" if last4 else "Tyre Sensor",
        "manufacturer": manufacturer,
        "model": model,
    }


@dataclass(frozen=True, kw_only=True)
class TpmsSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    # Restore the last value across an HA restart (readings, not RSSI).
    restore: bool = True


SENSOR_DESCRIPTIONS: tuple[TpmsSensorDescription, ...] = (
    TpmsSensorDescription(
        key="pressure",
        name="Pressure",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.KPA,
        suggested_unit_of_measurement=UnitOfPressure.BAR,
        suggested_display_precision=2,
        value_fn=lambda d: d.get("pressure_kpa"),
    ),
    TpmsSensorDescription(
        key="temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        value_fn=lambda d: d.get("temperature_c"),
    ),
    TpmsSensorDescription(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("battery_pct"),
    ),
    TpmsSensorDescription(
        key="battery_voltage",
        name="Battery Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("battery_v"),
    ),
    TpmsSensorDescription(
        key="last_seen",
        name="Last Seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("last_seen"),
    ),
    TpmsSensorDescription(
        key="rssi",
        name="Signal Strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="dBm",
        entity_category=EntityCategory.DIAGNOSTIC,
        restore=False,
        value_fn=lambda d: d.get("rssi"),
    ),
)


class TpmsBleSensor(CoordinatorEntity, RestoreSensor):
    """One reading on a tyre sensor's device card."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Any,    # TpmsBleCoordinator
        entry_id: str,
        device_id: str,
        description: TpmsSensorDescription,
        mac: str = "",
        tpms_format: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._value_fn = description.value_fn
        self._device_id = device_id
        self._mac = mac
        self._format = tpms_format
        self._restored: Any = None
        self._attr_unique_id = f"{entry_id}_tpms_ble_{device_id}_{description.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if not self.entity_description.restore:
            return
        last = await self.async_get_last_sensor_data()
        if last is None or last.native_value is None:
            return
        value = last.native_value
        if (
            self.entity_description.device_class == SensorDeviceClass.TIMESTAMP
            and isinstance(value, str)
        ):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return
        self._restored = value

    @property
    def native_value(self) -> Any:
        value = self._value_fn(self.coordinator.data or {})
        return self._restored if value is None else value

    @property
    def device_info(self) -> dict[str, Any]:
        return tpms_device_info(self._device_id, self._mac, self._format)


class TpmsBleAlarm(CoordinatorEntity, BinarySensorEntity):
    """The sensor's own alarm bit (formats in ALARM_FORMATS only)."""

    _attr_has_entity_name = True
    _attr_name = "Alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: Any, entry_id: str, device_id: str,
        mac: str = "", tpms_format: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._mac = mac
        self._format = tpms_format
        self._attr_unique_id = f"{entry_id}_tpms_ble_{device_id}_alarm"

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("alarm")

    @property
    def device_info(self) -> dict[str, Any]:
        return tpms_device_info(self._device_id, self._mac, self._format)


class TpmsWheelPositionSelect(SelectEntity):
    """Which wheel this sensor is on. Options-as-truth on the entry
    (`entry.options[TYRE_POSITION_OPTION][device_id]`), like Heater Model:
    the options are loaded before any platform, so the choice reads back
    exactly after every restart and OTA."""

    _attr_has_entity_name = True
    _attr_name = "Wheel Position"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:tire"

    def __init__(
        self, entry: Any, device_id: str, mac: str = "", tpms_format: str | None = None,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._mac = mac
        self._format = tpms_format
        self._attr_unique_id = f"{entry.entry_id}_tpms_ble_{device_id}_wheel_position"
        self._attr_options = [TYRE_POSITION_UNSET, *TYRE_POSITIONS]

    @property
    def current_option(self) -> str:
        return (self._entry.options.get(TYRE_POSITION_OPTION) or {}).get(
            self._device_id, TYRE_POSITION_UNSET,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"labels": {o: TYRE_POSITION_LABELS.get(o, o) for o in self._attr_options}}

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        ledger = dict(self._entry.options.get(TYRE_POSITION_OPTION) or {})
        if option == TYRE_POSITION_UNSET:
            ledger.pop(self._device_id, None)
        else:
            # One sensor per wheel: whoever held this position gives it up.
            for other, pos in list(ledger.items()):
                if pos == option and other != self._device_id:
                    ledger.pop(other)
            ledger[self._device_id] = option
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, TYRE_POSITION_OPTION: ledger},
        )
        # Re-publish every wheel select so a sensor that just lost its
        # position stops showing it.
        siblings = getattr(getattr(self, "platform", None), "entities", {}) or {}
        for entity in list(siblings.values()):
            if isinstance(entity, TpmsWheelPositionSelect) and entity is not self:
                entity.async_write_ha_state()
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        return tpms_device_info(self._device_id, self._mac, self._format)
