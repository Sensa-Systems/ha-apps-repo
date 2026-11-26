"""Sensor entities for a Sensa-owned Victron Orion XS.

Six sensors map one-to-one onto the coordinator's value cache keys.
Each entity pulls its value with a lambda so a missing key (first
boot, between advert and first decode) becomes Unavailable rather
than crashing the platform setup.

Device-card identifier scheme matches the Teltonika / Cerbo pattern
(`(DOMAIN, f"victron_orion_xs_{device_id}")`) so Save-button,
text/switch config entities, and these sensors all land on the
same HA device page.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from .victron_orion_xs import device_state_label


@dataclass(kw_only=True)
class OrionXsSensorDescription(SensorEntityDescription):
    """SensorEntityDescription with a value extractor.

    The value_fn closes over the coordinator's data dict — returns
    the sensor's reading or None. None becomes Unavailable at the
    HA-entity layer without any extra plumbing.
    """
    value_fn: Callable[[dict[str, Any]], Any]


SENSOR_DESCRIPTIONS: tuple[OrionXsSensorDescription, ...] = (
    OrionXsSensorDescription(
        key="output_voltage",
        name="Output Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        value_fn=lambda d: d.get("output_voltage"),
    ),
    OrionXsSensorDescription(
        key="output_current",
        name="Output Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        value_fn=lambda d: d.get("output_current"),
    ),
    OrionXsSensorDescription(
        key="input_voltage",
        name="Input Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        value_fn=lambda d: d.get("input_voltage"),
    ),
    OrionXsSensorDescription(
        key="input_current",
        name="Input Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        value_fn=lambda d: d.get("input_current"),
    ),
    OrionXsSensorDescription(
        key="device_state",
        name="Charger State",
        # Enum-style string sensor — no device_class, no unit. Label
        # comes from the decoder's human-friendly mapping (bulk /
        # absorption / float / external-control / …).
        value_fn=lambda d: device_state_label(d.get("device_state")),
    ),
    OrionXsSensorDescription(
        key="off_reason",
        name="Off Reason",
        # Joined list of active flags. If the charger is running
        # (no flags), we surface "none" rather than an empty string
        # so the customer sees something meaningful in the UI.
        value_fn=lambda d: (
            ", ".join(d.get("off_reason_flags") or []) or "none"
        ),
    ),
    OrionXsSensorDescription(
        key="rssi",
        name="Signal Strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="dBm",
        value_fn=lambda d: d.get("rssi"),
    ),
)


class VictronOrionXsSensor(CoordinatorEntity, SensorEntity):
    """Single sensor on a Sensa-owned Orion XS device card."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Any,    # VictronOrionXsCoordinator
        entry_id: str,
        device_id: str,
        description: OrionXsSensorDescription,
        mac: str = "",
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._value_fn = description.value_fn
        self._device_id = device_id
        self._mac = mac
        self._attr_unique_id = f"{entry_id}_victron_orion_xs_{device_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        return self._value_fn(data)

    @property
    def device_info(self) -> dict[str, Any]:
        last4 = self._mac.replace(":", "").upper()[-4:] if self._mac else ""
        name = f"Victron Orion XS (#{last4})" if last4 else "Victron Orion XS"
        return {
            "identifiers": {(DOMAIN, f"victron_orion_xs_{self._device_id}")},
            "name": name,
            "manufacturer": "Victron Energy",
            "model": "Orion XS",
        }
