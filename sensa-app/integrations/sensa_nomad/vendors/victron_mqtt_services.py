"""Per-service topic templates for the Victron Cerbo GX MQTT pipeline.

A Cerbo aggregates data from every Victron device wired to its VE.Direct /
VE.Bus / VE.Can / VE.CAN ports (batteries, SmartSolar MPPTs, Multiplus /
Quattro inverters, temperature sensors, tank senders, grid meters, …).
Venus OS publishes per-device topics under the tree:

    N/<portalID>/<service>/<instance>/<tail>

…where `service` identifies the device family and `instance` is the numeric
device id (e.g. `battery/256`, `solarcharger/288`). The system-level
aggregates Sensa already consumed (`N/<portalID>/system/0/…`) are rollups
*across* all the instances — useful for a dashboard summary, but a customer
looking at their picker rightly expects one entity per physical thing.

This module is the single source of truth for "which topic tails do we
care about per service type, and how should each one render as an HA
sensor." Per-service entries are:

    `tails`         — list of tails we subscribe to for this service.
                      Each tail is a path under `<service>/<instance>/`.
    `entity_descs`  — tail → SensorEntityDescription used by the coordinator
                      to spin up one HA entity per (service, instance, tail)
                      combination dynamically as new instances appear on
                      the broker.

The templates are deliberately curated (~10 topics per service, not the
100+ Venus emits) — Venus publishes a lot of internal state that nobody
wants on their dashboard. Adding a topic is a one-line edit here; a
future dynamic-everything mode could be opt-in via a switch.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)


@dataclass(kw_only=True, frozen=True)
class VenusSensorDescription(SensorEntityDescription):
    """SensorEntityDescription with a Venus-specific payload transform.

    Venus publishes values as `{"value": <x>}` — the coordinator parses
    that envelope generically. Some topics need further transformation
    (enum → label, seconds → hours, mV → V), which lives here as a
    lambda over the raw decoded value. `None` stays `None` so a sensor
    that's "sensor unavailable / no current value" from the Cerbo
    surfaces as Unavailable in HA rather than showing 0.

    `lazy` tails are created ONLY when the broker has actually published
    that tail for a given (service, instance) — used for fields that
    exist on some members of a service family but not others (e.g. a
    managed Smart Lithium battery publishes per-cell voltages + balancer
    state, but a SmartShunt on the same `battery` service does not).
    `build_device_entities` skips a lazy tail with no cached value so
    the SmartShunt isn't littered with permanently-unavailable cell
    sensors. Non-lazy tails are always created (the historical default).
    """
    transform: Callable[[Any], Any] | None = None
    lazy: bool = False


# ---------------------------------------------------------------------------
# Venus OS enum decoders — shared across services
# ---------------------------------------------------------------------------


# Charger / inverter state codes. Same enum the upstream victron-mqtt
# library decodes; we duplicate the mapping here to avoid a runtime
# dependency on that package.
_CHARGER_STATE_LABELS: dict[int, str] = {
    0: "off", 1: "low-power", 2: "fault", 3: "bulk", 4: "absorption",
    5: "float", 6: "storage", 7: "equalize-manual", 9: "inverting",
    11: "power-supply", 245: "starting-up", 246: "repeated-absorption",
    247: "recondition", 248: "battery-safe", 249: "active",
    # Orion XS firmware 1.15 emergency reverse charge (starter battery).
    242: "reverse-charging-paused", 243: "reverse-charging",
    252: "external-control", 255: "not-available",
}

# Multi / Quattro / VE.Bus mode codes (Settings / "Charger only" etc.)
_VEBUS_MODE_LABELS: dict[int, str] = {
    1: "charger-only", 2: "inverter-only", 3: "on", 4: "off",
}

# MPPT solar charger Mode register (distinct from the charge-algorithm State):
# the on/off switch VictronConnect exposes. Written by the cold-charging
# protection (`cold_charge.py`) to stop solar charge into a freezing pack.
_SOLAR_MODE_LABELS: dict[int, str] = {
    1: "on", 4: "off",
}

# Tank fluid type codes (Venus OS Settings → Tank sender).
_TANK_FLUID_LABELS: dict[int, str] = {
    0: "fuel", 1: "fresh-water", 2: "waste-water", 3: "live-well",
    4: "oil", 5: "black-water", 6: "gasoline", 7: "diesel",
    8: "lpg", 9: "lng", 10: "hydraulic-oil", 11: "raw-water",
}


# ---------------------------------------------------------------------------
# Firmware versions
# ---------------------------------------------------------------------------


# Venus publishes a connected device's `/FirmwareVersion` over MQTT as the RAW
# number (dbus GetValue). The formatted string a GX screen shows is the dbus
# GetText reply, which the MQTT bridge does not carry, so it has to be rendered
# here. Two conventions, split by family (raw values probed on a real Cerbo GX
# v3.79, 2026-09-18):
#
#   VE.Direct / VE.Can: the HEX digits are the version. Two-byte form
#     0x0419 (SmartShunt 500A) reads v4.19; three-byte form 0x0113FF (Orion XS)
#     reads v1.13, where the low byte is the release marker. 0xFF is an
#     official release and so is 0x00 (Venus's own `system/0/FirmwareVersion`
#     is 0x037900 on a GX running v3.79); only some OTHER value is a candidate
#     build, and that one is shown with a `~NN` suffix. Rendering 0x00 as a
#     candidate was the first thing the live probe caught.
#   VE.Bus: the DECIMAL value is the version, the 3-to-4 digit number
#     Victron's "VE.Bus firmware versions explained" uses. 1366 on a
#     MultiPlus-II reads 1366, NOT v5.56.
#
# Rendering only: an unrecognised shape falls back to the raw number rather
# than inventing a version string.
def format_firmware_version(raw: Any, *, vebus: bool = False) -> Any:
    """Venus `/FirmwareVersion` payload -> the string Victron would show."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        # The GX's own `Firmware/Installed/Version` is already "v3.79".
        return raw.strip() or None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return raw
    if value <= 0:
        return None
    if vebus:
        return str(value)
    if value > 0xFFFF:
        major = (value >> 16) & 0xFF
        minor = (value >> 8) & 0xFF
        release = value & 0xFF
        text = f"v{major:x}.{minor:02x}"
        if release in (0x00, 0xFF):
            return text
        return f"{text}~{release:02x}"
    return f"v{(value >> 8) & 0xFF:x}.{value & 0xFF:02x}"


def _firmware_text(*, vebus: bool = False) -> Callable[[Any], Any]:
    """Build the per-family transform for a `FirmwareVersion` tail."""
    def _t(value: Any) -> Any:
        return format_firmware_version(value, vebus=vebus)
    return _t


def firmware_version_text(service: str, raw: Any) -> Any:
    """Render one device's firmware for display, choosing the convention from
    its Venus service. The single place that decides vebus-vs-VE.Direct, so the
    sensor and the HA device card's `sw_version` cannot disagree."""
    return format_firmware_version(raw, vebus=service == "vebus")


def _label(labels: dict[int, str]) -> Callable[[Any], Any]:
    """Build a transform that maps an int to a string label."""
    def _t(value: Any) -> Any:
        if value is None:
            return None
        try:
            return labels.get(int(value), f"code-{int(value)}")
        except (TypeError, ValueError):
            return str(value)
    return _t


def _cubic_metres_to_litres(value: Any) -> Any:
    """A tank's `/Capacity` or `/Remaining`: Venus publishes m3, the sensor is
    litres. Unconverted, a 35 L tank read as 0.035 L and the heaters' Diesel
    Days Left as 0.0 (box .116, 2026-10-08)."""
    if value is None:
        return None
    try:
        return float(value) * 1000.0
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-service templates
# ---------------------------------------------------------------------------


# Battery (applies to BMS service records — SmartShunt, Lynx Shunt,
# and other Victron-native battery monitors reporting through a Cerbo).
_BATTERY_ENTITIES: dict[str, VenusSensorDescription] = {
    "Dc/0/Voltage": VenusSensorDescription(
        key="voltage", name="Battery Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    "Dc/0/Current": VenusSensorDescription(
        key="current", name="Battery Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
    ),
    "Dc/0/Power": VenusSensorDescription(
        key="power", name="Battery Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    "Dc/0/Temperature": VenusSensorDescription(
        key="temperature", name="Battery Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    # Dc/1 is the shunt's secondary (auxiliary) voltage input — wired to
    # the starter / vehicle battery. Venus publishes it on the battery
    # service the same way it does the main pack on Dc/0.
    "Dc/1/Voltage": VenusSensorDescription(
        key="starter_voltage", name="Starter Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    "Soc": VenusSensorDescription(
        key="soc", name="State of Charge",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    "TimeToGo": VenusSensorDescription(
        key="time_to_go", name="Time Remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
    "ConsumedAmphours": VenusSensorDescription(
        key="consumed_ah", name="Consumed Ah",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="Ah",
        suggested_display_precision=1,
    ),
    # --- Managed-battery (Victron Smart Lithium) fields -----------------
    # Venus models a Smart Lithium / Lynx-managed LiFePO4 bank on the same
    # `com.victronenergy.battery` service as a SmartShunt, but it ALSO
    # publishes State of Health, the active-balancer flag and per-cell
    # voltages. These are `lazy` so a plain SmartShunt (which never
    # publishes them) doesn't gain a row of permanently-unavailable
    # sensors — they appear only on a battery that actually reports cells.
    # The Sensa BMS card (`custom:sensa-bms-card`) consumes them once the
    # synthetic Cerbo battery row is reclassified `battery` (leisure) and
    # given a `bms_entities` role map. See `_attach_service_class_rows`.
    "Soh": VenusSensorDescription(
        key="state_of_health", name="State of Health", lazy=True,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    "Balancing": VenusSensorDescription(
        key="balancing", name="Balancing", lazy=True,
    ),
    "System/MinCellVoltage": VenusSensorDescription(
        key="min_cell_voltage", name="Min Cell Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    "System/MaxCellVoltage": VenusSensorDescription(
        key="max_cell_voltage", name="Max Cell Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    # Per-cell voltages. Venus publishes `/Voltages/Cell{1..N}` in volts;
    # cell count varies by pack (4 for a 12 V LiFePO4, 8 for 24 V, …), so
    # we declare a generous 1..8 and let the lazy filter create only the
    # cells the broker actually reports. The role keys (`cell_N_voltage`)
    # are exactly what `_bms_cell_voltage_slots` expects.
    "Voltages/Cell1": VenusSensorDescription(
        key="cell_1_voltage", name="Cell 1 Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    "Voltages/Cell2": VenusSensorDescription(
        key="cell_2_voltage", name="Cell 2 Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    "Voltages/Cell3": VenusSensorDescription(
        key="cell_3_voltage", name="Cell 3 Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    "Voltages/Cell4": VenusSensorDescription(
        key="cell_4_voltage", name="Cell 4 Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    "Voltages/Cell5": VenusSensorDescription(
        key="cell_5_voltage", name="Cell 5 Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    "Voltages/Cell6": VenusSensorDescription(
        key="cell_6_voltage", name="Cell 6 Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    "Voltages/Cell7": VenusSensorDescription(
        key="cell_7_voltage", name="Cell 7 Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    "Voltages/Cell8": VenusSensorDescription(
        key="cell_8_voltage", name="Cell 8 Voltage", lazy=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}

# SmartSolar / BlueSolar MPPT.
_SOLARCHARGER_ENTITIES: dict[str, VenusSensorDescription] = {
    "Dc/0/Voltage": VenusSensorDescription(
        key="dc_voltage", name="Battery Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    "Dc/0/Current": VenusSensorDescription(
        key="dc_current", name="Charge Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
    ),
    "Pv/V": VenusSensorDescription(
        key="pv_voltage", name="Panel Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    # Venus deprecated `Pv/I` in v2.80; a current SmartSolar never publishes
    # it, so it is lazy (created only when the broker actually sends it).
    "Pv/I": VenusSensorDescription(
        key="pv_current", name="Panel Current", lazy=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
    ),
    # Instantaneous PV power is `Yield/Power` ("Total PV power (Watts)" in
    # the Venus D-Bus reference), on single- and multi-tracker chargers
    # alike. There is no `Pv/P` on real hardware (only the simulator ever
    # published one); mapping it here left every real MPPT's panel-power
    # entity permanently `unknown`, and because the role resolver keeps
    # `unknown` as live, the power-flow cards' solar slot summed four
    # unknowns and showed a dash instead of falling back to the Cerbo
    # aggregate. Key stays `pv_power` so existing entity ids are kept.
    "Yield/Power": VenusSensorDescription(
        key="pv_power", name="Panel Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    # Today's yield in kWh, reset by the charger at midnight. Key stays
    # `yield_power` so the existing "Yield Today" entity carries on; HA's
    # recorder keeps that entity's statistics in Wh and converts the new
    # kWh states into them (same energy converter, no repair issue).
    "History/Daily/0/Yield": VenusSensorDescription(
        key="yield_power", name="Yield Today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    "State": VenusSensorDescription(
        key="state", name="Charger State",
        transform=_label(_CHARGER_STATE_LABELS),
    ),
    "Mode": VenusSensorDescription(
        key="charger_mode", name="Charger Mode",
        transform=_label(_SOLAR_MODE_LABELS),
    ),
    "ErrorCode": VenusSensorDescription(
        key="error_code", name="Error Code",
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}

# Multiplus / Quattro via VE.Bus.
_VEBUS_ENTITIES: dict[str, VenusSensorDescription] = {
    "Ac/ActiveIn/L1/V": VenusSensorDescription(
        key="ac_in_voltage", name="AC Input Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    "Ac/ActiveIn/L1/I": VenusSensorDescription(
        key="ac_in_current", name="AC Input Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    "Ac/ActiveIn/L1/P": VenusSensorDescription(
        key="ac_in_power", name="AC Input Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    "Ac/ActiveIn/L1/F": VenusSensorDescription(
        key="ac_in_frequency", name="AC Input Frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=1,
    ),
    "Ac/ActiveIn/CurrentLimit": VenusSensorDescription(
        # The shore-power hookup rating (campsite supply current limit).
        # A setpoint, not a measurement — no state_class, so HA doesn't
        # treat it as a graphable / long-term-stats sensor.
        key="ac_in_current_limit", name="AC Input Current Limit",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=0,
    ),
    "Ac/Out/L1/V": VenusSensorDescription(
        key="ac_out_voltage", name="AC Output Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    "Ac/Out/L1/I": VenusSensorDescription(
        key="ac_out_current", name="AC Output Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    "Ac/Out/L1/P": VenusSensorDescription(
        key="ac_out_power", name="AC Output Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    "Ac/Out/L1/F": VenusSensorDescription(
        key="ac_out_frequency", name="AC Output Frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=1,
    ),
    "Dc/0/Voltage": VenusSensorDescription(
        key="dc_voltage", name="DC Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    "Dc/0/Current": VenusSensorDescription(
        key="dc_current", name="DC Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
    ),
    "State": VenusSensorDescription(
        key="state", name="Inverter State",
        transform=_label(_CHARGER_STATE_LABELS),
    ),
    "Mode": VenusSensorDescription(
        key="mode", name="Inverter Mode",
        transform=_label(_VEBUS_MODE_LABELS),
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(vebus=True),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}

# Tank senders (fluid level sensors).
_TANK_ENTITIES: dict[str, VenusSensorDescription] = {
    "Level": VenusSensorDescription(
        key="level", name="Level",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    # Venus publishes both in m3 (dbus com.victronenergy.tank); shown in L.
    "Remaining": VenusSensorDescription(
        key="remaining", name="Remaining",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="L",
        suggested_display_precision=1,
        transform=_cubic_metres_to_litres,
    ),
    "Capacity": VenusSensorDescription(
        key="capacity", name="Capacity",
        native_unit_of_measurement="L",
        suggested_display_precision=1,
        transform=_cubic_metres_to_litres,
    ),
    "FluidType": VenusSensorDescription(
        key="fluid_type", name="Fluid Type",
        transform=_label(_TANK_FLUID_LABELS),
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}

# Temperature sensors (Ruuvi, Victron-branded, etc. wired via the Cerbo).
_TEMPERATURE_ENTITIES: dict[str, VenusSensorDescription] = {
    "Temperature": VenusSensorDescription(
        key="temperature", name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    "Humidity": VenusSensorDescription(
        key="humidity", name="Humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    "Pressure": VenusSensorDescription(
        key="pressure", name="Pressure",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="hPa",
        suggested_display_precision=0,
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}

# Grid meter (EM24, ET112, et al.).
_GRID_ENTITIES: dict[str, VenusSensorDescription] = {
    # Position — where the meter is wired in the AC topology. Venus OS:
    # 0=AC input 1 (the shore feed), 1=AC output (typically a meter on
    # the Multiplus's AC-Out-2 switched output, measuring the
    # shore-conditional loads), 2=AC input 2 (genset feed). Sensa's
    # service-class attach reads this to decide whether the meter is a
    # `grid_meter` or an `ac_output_meter` — the only way to tell them
    # apart, because Venus uses the same `com.victronenergy.grid`
    # service name for every position.
    "Position": VenusSensorDescription(
        key="position", name="Position",
        # Diagnostic so it lives under the device card's diagnostic
        # section (not cluttering the primary entity list) but stays
        # enabled — the synthetic-row attach reads its state to pick
        # service_class. Disabling-by-default would leave it out of
        # `hass.states`, breaking that lookup.
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "Ac/L1/Voltage": VenusSensorDescription(
        key="voltage", name="L1 Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    "Ac/L1/Current": VenusSensorDescription(
        key="current", name="L1 Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    "Ac/L1/Power": VenusSensorDescription(
        key="power", name="L1 Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    "Ac/Energy/Forward": VenusSensorDescription(
        key="energy_imported", name="Energy Imported",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    "Ac/Energy/Reverse": VenusSensorDescription(
        key="energy_exported", name="Energy Exported",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
}

# PV inverter (grid-tied Fronius / SolarEdge / etc. seen by Cerbo).
_PVINVERTER_ENTITIES: dict[str, VenusSensorDescription] = {
    "Ac/L1/Power": VenusSensorDescription(
        key="power", name="Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    "Ac/Energy/Forward": VenusSensorDescription(
        key="energy", name="Total Energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}

# Genset (inverter generators hooked via dataports).
_GENSET_ENTITIES: dict[str, VenusSensorDescription] = {
    "StatusCode": VenusSensorDescription(
        key="status", name="Status",
    ),
    "RunningByCondition": VenusSensorDescription(
        key="running_reason", name="Running Reason",
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}

# DC-DC converter (Orion XS / Orion-Tr Smart wired via VE.Direct /
# VE.Can). Venus OS publishes these under the `alternator` service
# branch (observed on a Cerbo GX with an Orion XS wired in —
# subnet-scan confirmed via `mosquitto_sub -t 'N/#'` 2026-04-22). The
# legacy `dcdc` name from older Venus firmware is kept as an alias
# so either one surfaces an HA device card without template churn.
_DCDC_ENTITIES: dict[str, VenusSensorDescription] = {
    "Dc/0/Voltage": VenusSensorDescription(
        key="output_voltage", name="Output Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    "Dc/0/Current": VenusSensorDescription(
        key="output_current", name="Output Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
    ),
    "Dc/0/Power": VenusSensorDescription(
        key="output_power", name="Output Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    "Dc/1/Voltage": VenusSensorDescription(
        key="input_voltage", name="Input Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    "State": VenusSensorDescription(
        key="state", name="Charger State",
        transform=_label(_CHARGER_STATE_LABELS),
    ),
    "ErrorCode": VenusSensorDescription(
        key="error_code", name="Error Code",
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}

# Orion XS emergency reverse charge (firmware 1.15, gui-v2 issue 3284): the
# Orion charges a flat starter battery from the leisure bank for 15 minutes,
# then pauses 5 so the engine can be started. `Control` is 0 = not possible
# now, 2 = possible, 3 = started (it stays 3 through the pause); writing 1
# starts it, 0 stops it. Published only by firmware that has the feature and
# only outside PSU mode, so its presence is the capability. Cached but NOT
# sensors: the starter-charge switch reads them (see switch.py).
EMERGENCY_CHARGE_CONTROL = "EmergencyCharge/Control"
EMERGENCY_CHARGE_CHARGING_COUNTDOWN = "EmergencyCharge/ChargingCountdown"
EMERGENCY_CHARGE_BLOCKED_COUNTDOWN = "EmergencyCharge/BlockedCountdown"
EMERGENCY_CHARGE_SERVICES: frozenset[str] = frozenset({"alternator", "dcdc"})

# Tails cached for a templated service although no sensor is built for them.
EXTRA_CACHED_TAILS: dict[str, frozenset[str]] = {
    service: frozenset({
        EMERGENCY_CHARGE_CONTROL,
        EMERGENCY_CHARGE_CHARGING_COUNTDOWN,
        EMERGENCY_CHARGE_BLOCKED_COUNTDOWN,
    })
    for service in EMERGENCY_CHARGE_SERVICES
}

# DC system (system-wide DC metering aggregate Venus sometimes emits).
_DCSYSTEM_ENTITIES: dict[str, VenusSensorDescription] = {
    "Dc/0/Voltage": VenusSensorDescription(
        key="voltage", name="DC Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    "Dc/0/Power": VenusSensorDescription(
        key="power", name="DC Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    # Diagnostic: the connected device's own firmware. LAZY, because Venus
    # publishes it for real hardware (shunt, MPPT, Multi, Orion XS) but
    # not for every service, and a tank sender must not gain a
    # permanently-unavailable Firmware row. Updating it is a VRM or
    # VictronConnect job, never Nomad's: only the GX itself can be
    # updated from here (see docs/devices/victron-cerbo-gx.md).
    "FirmwareVersion": VenusSensorDescription(
        key="firmware_version", name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        transform=_firmware_text(),
        lazy=True,
    ),
    "CustomName": VenusSensorDescription(
        key="custom_name", name="Custom Name",
    ),
}


# Public: service name → {tail: description} map the coordinator consumes.
VENUS_SERVICE_TEMPLATES: dict[str, dict[str, VenusSensorDescription]] = {
    "battery": _BATTERY_ENTITIES,
    "solarcharger": _SOLARCHARGER_ENTITIES,
    "vebus": _VEBUS_ENTITIES,
    "tank": _TANK_ENTITIES,
    "temperature": _TEMPERATURE_ENTITIES,
    "grid": _GRID_ENTITIES,
    "pvinverter": _PVINVERTER_ENTITIES,
    "genset": _GENSET_ENTITIES,
    "dcdc": _DCDC_ENTITIES,
    "alternator": _DCDC_ENTITIES,
    "dcsystem": _DCSYSTEM_ENTITIES,
}


# Service name → customer-friendly label used when building the HA
# device name on a fresh discovery (e.g. "Battery #256").
SERVICE_LABELS: dict[str, str] = {
    "battery": "Battery",
    "solarcharger": "Solar Charger",
    "vebus": "Multiplus",
    "tank": "Tank",
    "temperature": "Temperature Sensor",
    "grid": "Grid Meter",
    "pvinverter": "PV Inverter",
    "genset": "Generator",
    "dcdc": "DC-DC Charger",
    "alternator": "Alternator",
    "dcsystem": "DC System",
}


# Service name → device-image filename (under webcards `assets/images/`,
# served at `/local/sensa/images/<file>`). Used to give each Cerbo Venus
# sub-device its OWN picture in the Devices picker instead of inheriting the
# Cerbo's photo. Only the services we currently ship art for are mapped; an
# unmapped service (grid / tank / temperature / PV-inverter / genset /
# DC-system — art TBD) falls back to the generic picker placeholder, which
# reads more honestly than the wrong (Cerbo) product photo. `vebus` → the
# generic MultiPlus-II image (the `victron_multiplus_ii` catalog entry's
# `image`; the file must exist in webcards/assets/images/ or it degrades to
# the placeholder).
SERVICE_IMAGES: dict[str, str] = {
    "battery": "victron-smartshunt-500a.webp",
    "solarcharger": "victron-smartsolar-100-50.webp",
    "vebus": "victron-multiplus2-generic.webp",
    "dcdc": "victron-orion-xs-12-12-50.webp",
    "alternator": "victron-orion-xs-12-12-50.webp",
}


# VE.Bus FLAVOURS. An EasyPlus, a MultiPlus, a MultiPlus-II and a Quattro all
# publish the SAME `com.victronenergy.vebus` service with the same paths — an
# EasyPlus is a MultiPlus Compact plus a distribution board, and the switchgear
# publishes nothing — so the service name alone cannot tell them apart and they
# all read as the generic `SERVICE_LABELS["vebus"]` ("Multiplus"). The one
# discriminator is Venus's `ProductName` (e.g. "Quattro 24/5000/120-2x100"),
# captured by `VictronCoordinator._product_names`.
#
# Key = a lower-case substring searched in ProductName; value = (label, image).
# **LONGEST match wins**, not first — "MultiPlus-II 48/5000" contains BOTH
# "multiplus" and "multiplus-ii", and first-match would label every -II as a
# plain MultiPlus. Same rule as `sensor._match_model_image` /
# `_match_model_display_name`; if you add a key that is a substring of another,
# the longest-wins rule is what keeps them apart.
#
# An unmatched ProductName keeps the generic label + art: a family we have not
# met must not be mislabelled as one we have.
VEBUS_FLAVOURS: dict[str, tuple[str, str]] = {
    "easyplus": ("EasyPlus", "victron-easyplus.webp"),
    "multiplus-ii": ("MultiPlus-II", "victron-multiplus2.webp"),
    "multiplus ii": ("MultiPlus-II", "victron-multiplus2.webp"),
    "multiplus": ("MultiPlus", "victron-multiplus.webp"),
    "quattro": ("Quattro", "victron-quattro.webp"),
}


def vebus_flavour(product_name: str) -> tuple[str, str]:
    """`ProductName` → `(label, image_file)`, or `("", "")` when unrecognised.

    Longest-substring match (see `VEBUS_FLAVOURS`). "" for both means "keep the
    generic vebus label + art" — never guess a family.
    """
    text = (product_name or "").strip().lower()
    if not text:
        return ("", "")
    best = ""
    for key in VEBUS_FLAVOURS:
        if key in text and len(key) > len(best):
            best = key
    return VEBUS_FLAVOURS[best] if best else ("", "")


# Service name → mdi icon, the picker's FALLBACK when a row has no product
# photo (or the photo fails to load). A temperature probe on a wire and a tank
# sender aren't boxed products, so there is no art to ship for them and they
# rendered as an anonymous grey "?" — an icon says what the child IS. Mapped
# for EVERY service, not just the art-less ones, so a mid-deploy 404 on a
# photo degrades to a meaningful icon rather than the placeholder. Icons match
# the equivalent service class in `service_classes.py` so the two surfaces
# agree; the exception is `tank`, where the picker uses a level gauge
# (`mdi:gauge`) rather than that file's `mdi:propane-tank`. An unknown service
# maps to "" and keeps the "?" placeholder, which reads honestly as
# "unrecognised" instead of guessing.
SERVICE_ICONS: dict[str, str] = {
    "battery": "mdi:battery",
    "solarcharger": "mdi:solar-power",
    "vebus": "mdi:power-plug",
    "tank": "mdi:gauge",
    "temperature": "mdi:thermometer",
    "grid": "mdi:transmission-tower",
    "pvinverter": "mdi:solar-panel",
    "genset": "mdi:engine",
    "dcdc": "mdi:current-dc",
    "alternator": "mdi:current-dc",
    "dcsystem": "mdi:home-lightning-bolt",
}


# ---------------------------------------------------------------------------
# The GX device itself: `com.victronenergy.platform`
# ---------------------------------------------------------------------------

# Venus publishes the GX's OWN firmware state under the `platform` service,
# always at instance 0 (`platform/0/DeviceInstance` is 0; probed on a Cerbo GX
# running v3.79, 2026-09-18). The GX is not one of its own connected devices,
# so `platform` is deliberately NOT in `VENUS_SERVICE_TEMPLATES` - it must
# never grow a child device card. These tails feed the Cerbo's `update` entity.
PLATFORM_SERVICE = "platform"
PLATFORM_INSTANCE = "0"

# Read tails. `Installed/Version` is already a display string ("v3.79");
# `Online/AvailableVersion` is null until Venus has checked AND found something
# newer, which is the "up to date" signal, not "could not ask".
FIRMWARE_INSTALLED_VERSION = "Firmware/Installed/Version"
FIRMWARE_INSTALLED_BUILD = "Firmware/Installed/Build"
FIRMWARE_ONLINE_AVAILABLE_VERSION = "Firmware/Online/AvailableVersion"
FIRMWARE_ONLINE_AVAILABLE_BUILD = "Firmware/Online/AvailableBuild"
FIRMWARE_STATE = "Firmware/State"
FIRMWARE_PROGRESS = "Firmware/Progress"

# Write tails. venus-platform's `VeQItemCheckUpdate` / `VeQItemDoUpdate` act on
# ANY write, whatever the payload carries, so an accidental publish to Install
# starts a real firmware install on the customer's GX. Nothing may write these
# except the update entity's explicit check / install calls.
FIRMWARE_ONLINE_CHECK = "Firmware/Online/Check"
FIRMWARE_ONLINE_INSTALL = "Firmware/Online/Install"

# The tails the coordinator caches for `platform`. Curated exactly like a
# device template: everything else Venus publishes under `platform` (network
# config, notification counts, CAN statistics, security, tokens) is not ours.
PLATFORM_FIRMWARE_TAILS: frozenset[str] = frozenset({
    FIRMWARE_INSTALLED_VERSION,
    FIRMWARE_INSTALLED_BUILD,
    FIRMWARE_ONLINE_AVAILABLE_VERSION,
    FIRMWARE_ONLINE_AVAILABLE_BUILD,
    FIRMWARE_STATE,
    FIRMWARE_PROGRESS,
})

# `UpdaterState` from veutil's `inc/veutil/qt/firmware_updater_data.hpp`. The
# offset keeps the error states non-negative; 1000 (Idle) is what a settled GX
# reports, confirmed on the probed Cerbo.
FIRMWARE_STATE_UPDATE_FILE_NOT_FOUND = 997
FIRMWARE_STATE_ERROR_UPDATING = 998
FIRMWARE_STATE_ERROR_CHECKING = 999
FIRMWARE_STATE_IDLE = 1000
FIRMWARE_STATE_CHECKING = 1001
FIRMWARE_STATE_DOWNLOADING = 1002
FIRMWARE_STATE_REBOOTING = 1003

# ---------------------------------------------------------------------------
# The GX's OWN auto-update setting: `com.victronenergy.settings`
# ---------------------------------------------------------------------------

# The GX can install Venus OS by itself, on its own schedule, with nothing to do
# with us. That setting is the one thing that can walk a van straight past its
# rollout pin, so when a pin is set we hold it off the self-installing value.
#
# `settings` is a BIG service (a few thousand topics on a real Cerbo), so it is
# curated to exactly one tail, the same way `platform` is.
SETTINGS_SERVICE = "settings"
SETTINGS_INSTANCE = "0"
SETTING_AUTO_UPDATE = "Settings/System/AutoUpdate"

SETTINGS_TAILS: frozenset[str] = frozenset({SETTING_AUTO_UPDATE})

# `Firmware_AutoUpdate` from gui-v2 `src/enums.h`. The ORDER IS NOT THE ORDER
# THE MENU SHOWS: the self-installing choice is 1, not 3. Read the enum, never
# the drop-down, before acting on this value; a Cerbo probed on 2026-09-18 read
# 2, which is "check only" and does not install.
AUTO_UPDATE_DISABLED = 0
AUTO_UPDATE_CHECK_AND_UPDATE = 1
AUTO_UPDATE_CHECK_ONLY = 2
AUTO_UPDATE_CHECK_AND_DOWNLOAD_ONLY = 3

# What we move a pinned GX to: it keeps checking, so the version on offer stays
# visible and a pin can still be satisfied when we move it, but it never
# installs on its own. Deliberately NOT "disabled": that would hide the offer.
AUTO_UPDATE_SAFE_FOR_PIN = AUTO_UPDATE_CHECK_ONLY


# States where the GX is mid-update. `Rebooting` counts: the box is still
# updating and the customer must not be offered the button again.
FIRMWARE_INSTALLING_STATES: frozenset[int] = frozenset({
    FIRMWARE_STATE_DOWNLOADING,
    FIRMWARE_STATE_REBOOTING,
})
