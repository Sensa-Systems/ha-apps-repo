"""Sensor entities for the Victron Cerbo GX MQTT vendor module.

Each description names a Venus OS topic tail (the bit after `system/0/`)
and pulls the latest cached value through the coordinator's data dict.
Entity set is intentionally narrow for the demo — system-level totals
only. Per-device fanout (per-battery, per-MPPT) is a follow-on.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .victron_mqtt_services import VenusSensorDescription

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN


def _device_info(device_id: str, _data: dict[str, Any]) -> dict[str, Any]:
    """Build the HA device_info dict for the Cerbo HA device card."""
    return {
        "identifiers": {(DOMAIN, f"victron_cerbo_{device_id}")},
        "name": "Victron Cerbo GX",
        "manufacturer": "Victron Energy",
        "model": "Cerbo GX",
    }


# Venus OS publishes most numeric values as JSON `{"value": <number>}`.
# A trailing-path key returns the parsed scalar; `None` means "topic
# exists but no current reading" (entity should show Unknown rather
# than unavailable).
#
# Cache shape is `{ "system:0": {tail: value, ...}, "battery:256": {...}, ... }`
# — the coordinator keys per (service, instance) so entities for one
# device don't churn when another device's value changes. `_value`
# unwraps the system-branch dict for aggregate readings; per-device
# entities use `_service_value` directly.
def _value(data: dict[str, Any], path: str) -> StateType:
    system_cache = (data or {}).get("system:0") or {}
    raw = system_cache.get(path)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw


def _service_value(
    data: dict[str, Any], service: str, instance: str, tail: str,
) -> Any:
    """Extract a raw value for a specific (service, instance, tail)
    triple. Returns None when the topic hasn't been published yet or
    when Venus explicitly sent null."""
    key = f"{service}:{instance}"
    service_cache = (data or {}).get(key) or {}
    return service_cache.get(tail)


# Venus OS `SystemState/State` is a small enum — these are the values
# Sensa surfaces in the demo. Anything outside the table falls through
# as the raw integer so a future Cerbo firmware adding new states still
# yields *some* readable output.
_SYSTEM_STATE_LABELS: dict[int, str] = {
    0: "Off",
    1: "Low Power",
    2: "Fault",
    3: "Bulk",
    4: "Absorption",
    5: "Float",
    6: "Storage",
    7: "Equalize",
    8: "Passthru",
    9: "Inverting",
    10: "Power Assist",
    11: "Power Supply",
    252: "External Control",
}


def _system_state(data: dict[str, Any]) -> StateType:
    raw = _value(data, "SystemState/State")
    if raw is None:
        return None
    try:
        return _SYSTEM_STATE_LABELS.get(int(raw), str(raw))
    except (TypeError, ValueError):
        return raw


@dataclass(frozen=True, kw_only=True)
class VictronSensorDescription(SensorEntityDescription):
    """Sensor description with a Venus topic tail and value extractor."""

    value_fn: Callable[[dict[str, Any]], StateType] = lambda data: None


SENSOR_DESCRIPTIONS: tuple[VictronSensorDescription, ...] = (
    VictronSensorDescription(
        key="battery_voltage",
        name="Battery Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        value_fn=lambda data: _value(data, "Dc/Battery/Voltage"),
    ),
    VictronSensorDescription(
        key="battery_current",
        name="Battery Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:current-dc",
        value_fn=lambda data: _value(data, "Dc/Battery/Current"),
    ),
    VictronSensorDescription(
        key="battery_power",
        name="Battery Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-charging",
        value_fn=lambda data: _value(data, "Dc/Battery/Power"),
    ),
    VictronSensorDescription(
        key="battery_soc",
        name="Battery State of Charge",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-50",
        value_fn=lambda data: _value(data, "Dc/Battery/Soc"),
    ),
    VictronSensorDescription(
        key="pv_power",
        name="PV Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-power",
        value_fn=lambda data: _value(data, "Dc/Pv/Power"),
    ),
    VictronSensorDescription(
        key="pv_current",
        name="PV Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-panel",
        value_fn=lambda data: _value(data, "Dc/Pv/Current"),
    ),
    VictronSensorDescription(
        key="ac_consumption_input",
        name="AC Consumption (Input)",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: _value(data, "Ac/ConsumptionOnInput/L1/Power"),
    ),
    VictronSensorDescription(
        key="ac_consumption_output",
        name="AC Consumption (Output)",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:power-socket-eu",
        value_fn=lambda data: _value(data, "Ac/ConsumptionOnOutput/L1/Power"),
    ),
    VictronSensorDescription(
        key="ac_grid_power",
        name="AC Grid Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:transmission-tower",
        value_fn=lambda data: _value(data, "Ac/Grid/L1/Power"),
    ),
    VictronSensorDescription(
        key="dc_system_power",
        name="DC System Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: _value(data, "Dc/System/Power"),
    ),
    VictronSensorDescription(
        key="system_state",
        name="System State",
        icon="mdi:state-machine",
        value_fn=_system_state,
    ),
)


class VictronSensor(CoordinatorEntity, SensorEntity):
    """Single Cerbo system metric."""

    _attr_has_entity_name = True
    entity_description: VictronSensorDescription

    def __init__(
        self,
        coordinator,
        entry_id: str,
        device_id: str,
        description: VictronSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_victron_cerbo_{device_id}_{description.key}"
        self._device_id = device_id

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._device_id, self.coordinator.data or {})


class VictronDeviceSensor(CoordinatorEntity, SensorEntity):
    """One sensor on a per-service Venus device (battery / MPPT / Multi / …).

    Each instance corresponds to a specific (service, instance, tail)
    triple — e.g. "battery #256 Dc/0/Voltage". The HA device card it
    attaches to is `(DOMAIN, f"victron_cerbo_{cerbo_id}_{service}_{instance}")`,
    so every templated entity from the same Venus instance lands on
    one card and every one of those cards sits under the parent Cerbo
    in HA's device tree via the `via_device` link.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Any,
        entry_id: str,
        cerbo_device_id: str,
        service: str,
        instance: str,
        tail: str,
        description: VenusSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._cerbo_device_id = cerbo_device_id
        self._service = service
        self._instance = instance
        self._tail = tail
        self.entity_description = description
        self._transform = getattr(description, "transform", None)
        self._attr_unique_id = (
            f"{entry_id}_victron_cerbo_{cerbo_device_id}_"
            f"{service}_{instance}_{description.key}"
        )

    @property
    def native_value(self) -> Any:
        raw = _service_value(
            self.coordinator.data or {},
            self._service,
            self._instance,
            self._tail,
        )
        if self._transform is not None:
            return self._transform(raw)
        return raw

    @property
    def device_info(self) -> dict[str, Any]:
        from .victron_mqtt_services import SERVICE_LABELS, vebus_flavour
        label = SERVICE_LABELS.get(self._service, self._service.title())
        # VE.Bus only: an EasyPlus / MultiPlus / MultiPlus-II / Quattro publish
        # the IDENTICAL `vebus` service with identical paths, so Venus's
        # `ProductName` is the only thing that separates them — without it every
        # one of them reads as the generic "Multiplus". Deliberately scoped to
        # `vebus`: every other service keeps exactly the label + model it had.
        # Timing: `ProductName` is captured on arrival and `on_new_device` only
        # fires after NEW_DEVICE_SETTLE_SECS, so the keepalive burst has landed
        # by the time this runs on a fresh device.
        model = label
        if self._service == "vebus":
            try:
                product_name = self.coordinator.product_name(
                    self._service, self._instance,
                )
            except AttributeError:  # older coordinator / test double
                product_name = ""
            flavour_label, _flavour_image = vebus_flavour(product_name)
            if flavour_label:
                label = flavour_label
            # The FULL reported string ("Quattro 24/5000/120"), not just the
            # family — it carries the rating for support, and the Devices picker
            # reads it back through `vebus_flavour()` to pick the child's art.
            # Unrecognised but present is still worth showing.
            if product_name:
                model = product_name
        # Prefer the customer's VictronConnect-set friendly name
        # (`CustomName` topic — e.g. "House Battery", "Roof MPPT"),
        # fall back to the service#instance default. `device_info.name`
        # seeds the HA device card's display name at creation; later
        # CustomName changes re-render on the next integration reload.
        service_cache = (self.coordinator.data or {}).get(
            f"{self._service}:{self._instance}", {},
        )
        custom_name = str(service_cache.get("CustomName") or "").strip()
        # The `#instance` fallback goes in as `default_name`, which HA applies
        # only to a device with no name yet. As `name` it overwrote the
        # CustomName already on the card whenever a restart built the entity
        # before the name topic arrived (the coordinator's
        # `_apply_custom_name` renames the card when it does).
        name_key = {"name": custom_name} if custom_name else {
            "default_name": f"{label} #{self._instance}",
        }
        # Per-Venus-device HA card. `via_device` nests this under the
        # parent Cerbo card in HA's device tree so the relationship is
        # visible without duplicating the Cerbo data on every child.
        return {
            "identifiers": {
                (DOMAIN,
                 (f"victron_cerbo_{self._cerbo_device_id}_"
                  f"{self._service}_{self._instance}")),
            },
            **name_key,
            "manufacturer": "Victron Energy",
            "model": model,
            "via_device": (DOMAIN, f"victron_cerbo_{self._cerbo_device_id}"),
        }


def build_device_entities(
    coordinator: Any,
    entry_id: str,
    cerbo_device_id: str,
    service: str,
    instance: str,
) -> list[VictronDeviceSensor]:
    """Build the full entity set for one (service, instance) from the
    service template. Returns an empty list for unknown services so the
    caller can blindly pass along whatever the coordinator signalled
    without pre-filtering."""
    from .victron_mqtt_services import VENUS_SERVICE_TEMPLATES

    template = VENUS_SERVICE_TEMPLATES.get(service)
    if not template:
        return []
    # `lazy` tails (e.g. a managed Smart Lithium's per-cell voltages /
    # balancer / SoH on the shared `battery` service) are created only
    # when the broker has actually published that tail for this
    # (service, instance) — so a SmartShunt on the same service isn't
    # littered with permanently-unavailable cell sensors. Venus replays
    # all retained topics on subscribe and `on_new_device` fires after a
    # settle window, so a real managed battery's cell tails are already
    # cached by the time this runs.
    seen_tails = (coordinator.data or {}).get(f"{service}:{instance}", {})
    return [
        VictronDeviceSensor(
            coordinator, entry_id, cerbo_device_id,
            service, instance, tail, description,
        )
        for tail, description in template.items()
        if not getattr(description, "lazy", False) or tail in seen_tails
    ]
