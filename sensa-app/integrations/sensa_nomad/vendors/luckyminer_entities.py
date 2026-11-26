"""Entities for a Sensa-owned Lucky Miner LV08 Pro.

Seven platforms on one device card: sensors (what it is doing, what it earns,
what it heats), numbers, switches, selects and texts (every writable field
`luckyminer_api.WRITABLE_FIELDS` documents), buttons (restart, clear the
overheat latch) and a binary sensor (the fault).

**Every control the miner's own settings page can set is here.** There is no
separate Lucky Miner app, the on-device web page IS the control surface, and
it is driven by the same API this file wires up. So the numbers/switches/
selects/texts below are a straight walk through `WRITABLE_FIELDS`, not a
curated subset: `luckyminer_api.py` is the single source of truth for what is
writable and its validation rules, this file is only concerned with how each
field is PRESENTED (a slider, a toggle, a dropdown, a text box).

Every value comes out of the coordinator's already-converted cache. Nothing
here rescales anything: `current_a`, `core_voltage_v` and `hashrate_th` were
converted once at the edge in `luckyminer_api`, because a scale applied in two
places is this repo's most reliably shipped bug.

**Sensors and controls are built from the sample, not from a fixed list.**
The stock Lucky Miner firmware is a fork and has not been seen yet, if it
omits a field, the right outcome is one fewer entity, not a permanently-
unavailable one that looks like a fault for ever.

**Write-only fields never get a stale echo.** `wifiPass`, `stratumPassword`
and `fallbackStratumPassword` are in `luckyminer_api.WRITE_ONLY_FIELDS`
because the real API never returns them, so their text entities always read
back as unknown, deliberately, not a bug.

The earnings sensors carry the odds and the price staleness as attributes so
no card can render a pounds-per-day figure without the context that makes it
truthful. That is not decoration: solo mining at 14 TH/s pays nothing at all
until it finds a block, and this goes in front of customers.

**Two things worth knowing before touching the network or pool controls**:
the LV08 Pro is Wi-Fi only, so a bad `ssid`/`wifiPass` write is a physical
recovery trip, not a retry; and setting any `stratum*` field switches the
miner to pool mining, which the CryptoGenerator earnings math does not know
about, it keeps computing a solo answer regardless. See `luckyminer_api.py`'s
module docstring for the full reasoning on both.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from .luckyminer_api import WRITE_ONLY_FIELDS

# Identifier prefix, everything on one HA device page. Must stay in step with
# the purge prefix tuple in __init__.py or a removed miner leaves orphans.
IDENTIFIER_PREFIX = "luckyminer_"


@dataclass(frozen=True, kw_only=True)
class MinerSensorDescription(SensorEntityDescription):
    """A sensor plus how to read it out of the coordinator cache."""

    value_fn: Callable[[dict[str, Any]], Any]
    # Which cache key must be present for this sensor to exist at all. Lets a
    # firmware that omits a field simply produce one fewer entity.
    requires: str = ""
    # Extra attributes, for the context a bare number would lose.
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _earnings_attrs(d: dict[str, Any]) -> dict[str, Any]:
    """Context that must travel with every earnings figure.

    A solo miner's expected revenue is a lottery expectation, not an income
    stream. Shipping the odds and the price's age alongside it is what stops a
    card, or a screenshot in a brochure, implying otherwise.
    """
    return {
        "expected_not_actual": True,
        "block_odds_years": d.get("block_odds_years"),
        "btc_price": d.get("btc_price"),
        "currency": d.get("currency"),
        "price_fetched_at": d.get("price_fetched_at"),
        "price_stale": d.get("price_stale"),
        "network_hashrate_eh": d.get("network_hashrate_eh"),
    }


SENSOR_DESCRIPTIONS: tuple[MinerSensorDescription, ...] = (
    # --- mining ---------------------------------------------------------
    MinerSensorDescription(
        key="hashrate",
        name="Hash Rate",
        icon="mdi:pickaxe",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="TH/s",
        suggested_display_precision=2,
        requires="hashRate",
        value_fn=lambda d: d.get("hashrate_th"),
    ),
    MinerSensorDescription(
        key="expected_hashrate",
        name="Expected Hash Rate",
        icon="mdi:pickaxe",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="TH/s",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="expectedHashrate",
        value_fn=lambda d: d.get("expected_hashrate_th"),
    ),
    MinerSensorDescription(
        key="shares_accepted",
        name="Shares Accepted",
        icon="mdi:check-decagram",
        state_class=SensorStateClass.TOTAL_INCREASING,
        requires="sharesAccepted",
        value_fn=lambda d: d.get("sharesAccepted"),
    ),
    MinerSensorDescription(
        key="shares_rejected",
        name="Shares Rejected",
        icon="mdi:alert-decagram",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="sharesRejected",
        value_fn=lambda d: d.get("sharesRejected"),
    ),
    MinerSensorDescription(
        key="best_difficulty",
        name="Best Difficulty",
        icon="mdi:trophy",
        requires="bestDiff",
        value_fn=lambda d: d.get("bestDiff"),
    ),
    MinerSensorDescription(
        key="best_session_difficulty",
        name="Best Session Difficulty",
        icon="mdi:trophy-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="bestSessionDiff",
        value_fn=lambda d: d.get("bestSessionDiff"),
    ),
    # --- electrical ------------------------------------------------------
    MinerSensorDescription(
        key="power",
        name="Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
        requires="power",
        value_fn=lambda d: d.get("power"),
    ),
    MinerSensorDescription(
        key="voltage",
        name="Supply Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        requires="voltage",
        value_fn=lambda d: d.get("voltage"),
    ),
    MinerSensorDescription(
        key="current",
        name="Supply Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        requires="current",
        # Already mA -> A at the edge. The raw milliamp reading rides along as
        # an attribute so a wrong conversion is visible rather than silent.
        value_fn=lambda d: d.get("current_a"),
        attrs_fn=lambda d: {"reported_milliamps": d.get("current")},
    ),
    MinerSensorDescription(
        key="efficiency",
        name="Efficiency",
        icon="mdi:gauge",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="J/TH",
        suggested_display_precision=1,
        requires="power",
        value_fn=lambda d: d.get("efficiency_j_th"),
    ),
    # --- thermal ---------------------------------------------------------
    MinerSensorDescription(
        key="temperature",
        name="ASIC Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        requires="temp",
        value_fn=lambda d: d.get("temp"),
    ),
    MinerSensorDescription(
        key="vr_temperature",
        name="Regulator Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="vrTemp",
        value_fn=lambda d: d.get("vrTemp"),
    ),
    MinerSensorDescription(
        key="fan_speed",
        name="Fan Speed",
        icon="mdi:fan",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        requires="fanspeed",
        value_fn=lambda d: d.get("fanspeed"),
    ),
    MinerSensorDescription(
        key="fan_rpm",
        name="Fan Speed (RPM)",
        icon="mdi:fan",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="rpm",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="fanrpm",
        value_fn=lambda d: d.get("fanrpm"),
    ),
    # Heat is the reason a miner makes sense in a van in February, so it is a
    # first-class reading rather than something a template has to work out.
    MinerSensorDescription(
        key="heat_output",
        name="Heat Output",
        icon="mdi:radiator",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        requires="power",
        value_fn=lambda d: d.get("heat_w"),
        attrs_fn=lambda d: {"btu_per_hour": d.get("heat_btu_h")},
    ),
    # --- earnings (expectations, never balances) --------------------------
    MinerSensorDescription(
        key="expected_daily_sats",
        name="Expected Daily Earnings",
        icon="mdi:bitcoin",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="sats",
        value_fn=lambda d: d.get("expected_daily_sats"),
        attrs_fn=_earnings_attrs,
    ),
    MinerSensorDescription(
        key="expected_daily_value",
        name="Expected Daily Value",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("expected_daily_value"),
        attrs_fn=_earnings_attrs,
    ),
    MinerSensorDescription(
        key="expected_monthly_value",
        name="Expected Monthly Value",
        icon="mdi:cash-multiple",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("expected_monthly_value"),
        attrs_fn=_earnings_attrs,
    ),
    # The one that visibly moves while somebody watches, which is what makes
    # the tab feel alive rather than like a spreadsheet.
    MinerSensorDescription(
        key="session_sats",
        name="Session Earnings",
        icon="mdi:bitcoin",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="sats",
        value_fn=lambda d: d.get("session_sats"),
        attrs_fn=_earnings_attrs,
    ),
    MinerSensorDescription(
        key="session_value",
        name="Session Value",
        icon="mdi:cash-clock",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.get("session_value"),
        attrs_fn=_earnings_attrs,
    ),
    MinerSensorDescription(
        key="block_odds_years",
        name="Expected Time To A Block",
        icon="mdi:dice-multiple",
        native_unit_of_measurement="years",
        suggested_display_precision=0,
        value_fn=lambda d: d.get("block_odds_years"),
        attrs_fn=lambda d: {
            "explanation": (
                "Solo mining pays nothing until it finds a block, and then pays "
                "the whole block reward at once. This is how long that is "
                "expected to take at the current hash rate."
            ),
            "network_hashrate_eh": d.get("network_hashrate_eh"),
        },
    ),
    MinerSensorDescription(
        key="btc_price",
        name="Bitcoin Price",
        icon="mdi:currency-btc",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("btc_price"),
        attrs_fn=lambda d: {
            "currency": d.get("currency"),
            "fetched_at": d.get("price_fetched_at"),
            "stale": d.get("price_stale"),
        },
    ),
    # --- housekeeping -----------------------------------------------------
    MinerSensorDescription(
        key="uptime",
        name="Uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="uptimeSeconds",
        value_fn=lambda d: d.get("uptimeSeconds"),
    ),
    MinerSensorDescription(
        key="frequency",
        name="Core Frequency",
        icon="mdi:sine-wave",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.MEGAHERTZ,
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="frequency",
        value_fn=lambda d: d.get("frequency"),
    ),
    MinerSensorDescription(
        key="core_voltage",
        name="Core Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="coreVoltageActual",
        value_fn=lambda d: d.get("core_voltage_actual_v"),
        attrs_fn=lambda d: {"reported_millivolts": d.get("coreVoltageActual")},
    ),
    # Wi-Fi signal is NOT trivia here: the LV08 Pro has no Ethernet port, so a
    # miner losing hash rate at the back of a van on a marginal 2.4GHz signal
    # is a real and likely failure.
    MinerSensorDescription(
        key="wifi_rssi",
        name="Wi-Fi Signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="dBm",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires="wifiRSSI",
        value_fn=lambda d: d.get("wifiRSSI"),
        attrs_fn=lambda d: {"ssid": d.get("ssid")},
    ),
)


NUMBER_DESCRIPTIONS: tuple[tuple[str, NumberEntityDescription, str], ...] = (
    (
        "frequency",
        NumberEntityDescription(
            key="frequency",
            name="Core Frequency",
            icon="mdi:sine-wave",
            native_unit_of_measurement=UnitOfFrequency.MEGAHERTZ,
            native_min_value=400,
            native_max_value=650,
            native_step=5,
            mode=NumberMode.SLIDER,
            entity_category=EntityCategory.CONFIG,
        ),
        "frequency",
    ),
    (
        "core_voltage_mv",
        NumberEntityDescription(
            key="core_voltage_mv",
            name="Core Voltage",
            icon="mdi:flash",
            native_unit_of_measurement="mV",
            native_min_value=1000,
            native_max_value=1350,
            native_step=10,
            mode=NumberMode.SLIDER,
            entity_category=EntityCategory.CONFIG,
        ),
        "coreVoltage",
    ),
    (
        "fan_speed_pct",
        NumberEntityDescription(
            key="fan_speed_pct",
            name="Fan Speed",
            icon="mdi:fan",
            native_unit_of_measurement=PERCENTAGE,
            native_min_value=20,
            native_max_value=100,
            native_step=5,
            mode=NumberMode.SLIDER,
            entity_category=EntityCategory.CONFIG,
        ),
        "fanspeed",
    ),
    (
        "temp_target",
        NumberEntityDescription(
            key="temp_target",
            name="Target Temperature",
            icon="mdi:thermometer-auto",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            native_min_value=40,
            native_max_value=80,
            native_step=1,
            mode=NumberMode.SLIDER,
            entity_category=EntityCategory.CONFIG,
        ),
        "temptarget",
    ),
    (
        "display_timeout",
        NumberEntityDescription(
            key="display_timeout",
            name="Display Timeout",
            icon="mdi:monitor-off",
            native_unit_of_measurement=UnitOfTime.MINUTES,
            # Documented floor/ceiling exactly: -1 means never turn off, and
            # 71582 is oddly specific enough that it looks like a real
            # firmware limit rather than a made-up one.
            native_min_value=-1,
            native_max_value=71582,
            native_step=1,
            mode=NumberMode.BOX,
            entity_category=EntityCategory.CONFIG,
        ),
        "displayTimeout",
    ),
    (
        "stats_frequency",
        NumberEntityDescription(
            key="stats_frequency",
            name="Statistics Interval",
            icon="mdi:chart-timeline-variant",
            native_unit_of_measurement=UnitOfTime.SECONDS,
            native_min_value=0,
            native_max_value=3600,  # self-imposed ceiling; API documents no max
            native_step=10,
            mode=NumberMode.BOX,
            entity_category=EntityCategory.CONFIG,
        ),
        "statsFrequency",
    ),
    (
        "stratum_port",
        NumberEntityDescription(
            key="stratum_port",
            name="Pool Port",
            icon="mdi:server-network",
            native_min_value=1,
            native_max_value=65535,
            native_step=1,
            mode=NumberMode.BOX,
            entity_category=EntityCategory.CONFIG,
        ),
        "stratumPort",
    ),
    (
        "fallback_stratum_port",
        NumberEntityDescription(
            key="fallback_stratum_port",
            name="Fallback Pool Port",
            icon="mdi:server-network-outline",
            native_min_value=1,
            native_max_value=65535,
            native_step=1,
            mode=NumberMode.BOX,
            entity_category=EntityCategory.CONFIG,
        ),
        "fallbackStratumPort",
    ),
)


@dataclass(frozen=True)
class _SwitchSpec:
    key: str
    name: str
    icon: str
    field: str


SWITCH_DESCRIPTIONS: tuple[_SwitchSpec, ...] = (
    _SwitchSpec(
        key="autofanspeed", name="Automatic Fan Speed", icon="mdi:fan-auto",
        field="autofanspeed",
    ),
    _SwitchSpec(
        key="invertscreen", name="Invert Display", icon="mdi:flip-vertical",
        field="invertscreen",
    ),
    _SwitchSpec(
        key="overclock_enabled", name="Overclock Enabled", icon="mdi:speedometer",
        field="overclockEnabled",
    ),
)


@dataclass(frozen=True)
class _SelectSpec:
    key: str
    name: str
    icon: str
    options: tuple[str, ...]
    field: str


SELECT_DESCRIPTIONS: tuple[_SelectSpec, ...] = (
    # 0/90/180/270 degrees, not the upstream request schema's contradictory
    # `enum: [0, 1]`, see luckyminer_api's module docstring for why.
    _SelectSpec(
        key="rotation", name="Display Rotation", icon="mdi:screen-rotation",
        options=("0", "90", "180", "270"), field="rotation",
    ),
)


@dataclass(frozen=True)
class _TextSpec:
    key: str
    name: str
    icon: str
    field: str
    mode: Any = TextMode.TEXT
    min_length: int | None = None
    max_length: int | None = None


TEXT_DESCRIPTIONS: tuple[_TextSpec, ...] = (
    _TextSpec(
        key="hostname", name="Hostname", icon="mdi:web", field="hostname",
        max_length=63,
    ),
    _TextSpec(
        key="ssid", name="Wi-Fi SSID", icon="mdi:wifi", field="ssid",
        min_length=1, max_length=32,
    ),
    _TextSpec(
        key="wifi_password", name="Wi-Fi Password", icon="mdi:wifi-lock",
        field="wifiPass", mode=TextMode.PASSWORD, min_length=8, max_length=63,
    ),
    _TextSpec(
        key="stratum_url", name="Pool URL", icon="mdi:server-network",
        field="stratumURL", max_length=255,
    ),
    _TextSpec(
        key="stratum_user", name="Pool User", icon="mdi:account-network",
        field="stratumUser", max_length=255,
    ),
    _TextSpec(
        key="stratum_password", name="Pool Password", icon="mdi:key",
        field="stratumPassword", mode=TextMode.PASSWORD, max_length=255,
    ),
    _TextSpec(
        key="fallback_stratum_url", name="Fallback Pool URL",
        icon="mdi:server-network-outline", field="fallbackStratumURL",
        max_length=255,
    ),
    _TextSpec(
        key="fallback_stratum_user", name="Fallback Pool User",
        icon="mdi:account-network-outline", field="fallbackStratumUser",
        max_length=255,
    ),
    _TextSpec(
        key="fallback_stratum_password", name="Fallback Pool Password",
        icon="mdi:key-outline", field="fallbackStratumPassword",
        mode=TextMode.PASSWORD, max_length=255,
    ),
)


class _MinerEntity(CoordinatorEntity):
    """Shared device card + availability for everything on one miner."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 mac: str = "", display_name: str = "") -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._mac = mac
        self._display_name = display_name or "Lucky Miner LV08 Pro"
        self._entry_id = entry_id

    @property
    def available(self) -> bool:
        """Unavailable while the miner cannot be reached.

        The coordinator still carries the last good sample forward, that is
        what stops a chart flat-lining to zero, but the entity says plainly
        that the reading is not current, rather than presenting stale numbers
        as live ones.
        """
        data = self.coordinator.data or {}
        return bool(data.get("_available"))

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{IDENTIFIER_PREFIX}{self._device_id}")},
            "name": self._display_name,
            "manufacturer": "Lucky Miner",
            "model": "LV08 Pro",
        }


class LuckyMinerSensor(_MinerEntity, SensorEntity):
    """One reading on a miner's device card."""

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 description: MinerSensorDescription, mac: str = "",
                 display_name: str = "") -> None:
        super().__init__(coordinator, entry_id, device_id, mac, display_name)
        self.entity_description = description
        self._attr_unique_id = (
            f"{entry_id}_{IDENTIFIER_PREFIX}{device_id}_{description.key}"
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fn = self.entity_description.attrs_fn
        if fn is None:
            return {}
        return {k: v for k, v in fn(self.coordinator.data or {}).items() if v is not None}


class LuckyMinerNumber(_MinerEntity, NumberEntity):
    """A tuning control. Writes go through the client's allow-list."""

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 description: NumberEntityDescription, field: str,
                 mac: str = "", display_name: str = "") -> None:
        super().__init__(coordinator, entry_id, device_id, mac, display_name)
        self.entity_description = description
        self._field = field
        self._attr_unique_id = (
            f"{entry_id}_{IDENTIFIER_PREFIX}{device_id}_{description.key}"
        )

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.data or {}).get(self._field)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(**{self._field: int(value)})


class LuckyMinerSwitch(_MinerEntity, SwitchEntity):
    """A boolean control (autofanspeed / invertscreen / overclockEnabled)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 spec: _SwitchSpec, mac: str = "", display_name: str = "") -> None:
        super().__init__(coordinator, entry_id, device_id, mac, display_name)
        self._spec = spec
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_unique_id = f"{entry_id}_{IDENTIFIER_PREFIX}{device_id}_{spec.key}"

    @property
    def is_on(self) -> bool | None:
        value = (self.coordinator.data or {}).get(self._spec.field)
        if value is None:
            return None
        return bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(**{self._spec.field: 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(**{self._spec.field: 0})


class LuckyMinerSelect(_MinerEntity, SelectEntity):
    """A discrete-choice control (rotation)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 spec: _SelectSpec, mac: str = "", display_name: str = "") -> None:
        super().__init__(coordinator, entry_id, device_id, mac, display_name)
        self._spec = spec
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_options = list(spec.options)
        self._attr_unique_id = f"{entry_id}_{IDENTIFIER_PREFIX}{device_id}_{spec.key}"

    @property
    def current_option(self) -> str | None:
        value = (self.coordinator.data or {}).get(self._spec.field)
        if value is None:
            return None
        text = str(value)
        return text if text in self._spec.options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_write(**{self._spec.field: int(option)})


class LuckyMinerText(_MinerEntity, TextEntity):
    """A string control (hostname, ssid, and the pool/stratum fields).

    A write-only field (`spec.field in WRITE_ONLY_FIELDS`) always reads back
    as None, the real API never returns it either, so an entity that echoed
    the last value it happened to send would be showing something the
    device itself cannot confirm.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 spec: _TextSpec, mac: str = "", display_name: str = "") -> None:
        super().__init__(coordinator, entry_id, device_id, mac, display_name)
        self._spec = spec
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_mode = spec.mode
        if spec.min_length is not None:
            self._attr_native_min = spec.min_length
        if spec.max_length is not None:
            self._attr_native_max = spec.max_length
        self._attr_unique_id = f"{entry_id}_{IDENTIFIER_PREFIX}{device_id}_{spec.key}"

    @property
    def native_value(self) -> str | None:
        if self._spec.field in WRITE_ONLY_FIELDS:
            return None
        value = (self.coordinator.data or {}).get(self._spec.field)
        return None if value is None else str(value)

    async def async_set_value(self, value: str) -> None:
        await self.coordinator.async_write(**{self._spec.field: value})


class LuckyMinerRestartButton(_MinerEntity, ButtonEntity):
    """Reboot the miner. Tuning persists; the session counters reset."""

    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 mac: str = "", display_name: str = "") -> None:
        super().__init__(coordinator, entry_id, device_id, mac, display_name)
        self._attr_name = "Restart"
        self._attr_unique_id = f"{entry_id}_{IDENTIFIER_PREFIX}{device_id}_restart"

    async def async_press(self) -> None:
        await self.coordinator.async_restart()


class LuckyMinerClearOverheatButton(_MinerEntity, ButtonEntity):
    """Clear the overheat latch.

    The firmware latches `overheat_mode` and does not clear it when the miner
    cools, which is right: the latch records that something went wrong. Nothing
    clears it implicitly, a fault that silently un-latches hides the condition
    that tripped it. This button is the deliberate acknowledgement.
    """

    _attr_icon = "mdi:thermometer-alert"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 mac: str = "", display_name: str = "") -> None:
        super().__init__(coordinator, entry_id, device_id, mac, display_name)
        self._attr_name = "Clear Overheat"
        self._attr_unique_id = f"{entry_id}_{IDENTIFIER_PREFIX}{device_id}_clear_overheat"

    async def async_press(self) -> None:
        await self.coordinator.client.async_clear_overheat()
        await self.coordinator.async_request_refresh()


class LuckyMinerProblemSensor(_MinerEntity, BinarySensorEntity):
    """On when the miner has latched an overheat or reported a power fault."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: Any, entry_id: str, device_id: str,
                 mac: str = "", display_name: str = "") -> None:
        super().__init__(coordinator, entry_id, device_id, mac, display_name)
        self._attr_name = "Problem"
        self._attr_unique_id = f"{entry_id}_{IDENTIFIER_PREFIX}{device_id}_problem"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        if not data:
            return None
        return bool(data.get("overheat_mode")) or bool(data.get("power_fault"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "overheat": bool(data.get("overheat_mode")),
            "power_fault": data.get("power_fault"),
        }


def descriptions_for_sample(sample: dict[str, Any]) -> list[MinerSensorDescription]:
    """The sensors THIS firmware can actually feed.

    A description with a `requires` key only survives if the sample carries it.
    The stock Lucky Miner firmware is an unverified fork, so a missing field
    should cost one sensor, not leave a permanently-unavailable entity looking
    like a fault for the life of the install.

    Descriptions with no `requires` are derived values (earnings, odds, price):
    they are kept regardless, because their absence means "no market data yet",
    which is a temporary state that resolves itself.
    """
    kept: list[MinerSensorDescription] = []
    for description in SENSOR_DESCRIPTIONS:
        if description.requires and description.requires not in sample:
            continue
        kept.append(description)
    return kept


def build_entities(
    coordinator: Any,
    entry_id: str,
    device_id: str,
    sample: dict[str, Any],
    *,
    mac: str = "",
    display_name: str = "",
) -> dict[str, list[Any]]:
    """Every entity for one miner, keyed by the platform that adds it."""
    common = {"mac": mac, "display_name": display_name}
    sensors = [
        LuckyMinerSensor(coordinator, entry_id, device_id, d, **common)
        for d in descriptions_for_sample(sample)
    ]
    numbers = [
        LuckyMinerNumber(coordinator, entry_id, device_id, d, field, **common)
        for _key, d, field in NUMBER_DESCRIPTIONS
        if field in sample
    ]
    switches = [
        LuckyMinerSwitch(coordinator, entry_id, device_id, spec, **common)
        for spec in SWITCH_DESCRIPTIONS
        if spec.field in sample
    ]
    selects = [
        LuckyMinerSelect(coordinator, entry_id, device_id, spec, **common)
        for spec in SELECT_DESCRIPTIONS
        if spec.field in sample
    ]
    # A write-only field (wifiPass, the two stratum passwords) can NEVER
    # appear in `sample`, the real API never echoes it back, and neither
    # does ours. Gating those three on "field in sample" the way every other
    # entity here is gated would mean they could never exist. They are
    # created unconditionally instead; everything else still waits for the
    # firmware to actually report the field once, the usual rule.
    texts = [
        LuckyMinerText(coordinator, entry_id, device_id, spec, **common)
        for spec in TEXT_DESCRIPTIONS
        if spec.field in WRITE_ONLY_FIELDS or spec.field in sample
    ]
    buttons: list[Any] = [
        LuckyMinerRestartButton(coordinator, entry_id, device_id, **common),
    ]
    if "overheat_mode" in sample:
        buttons.append(
            LuckyMinerClearOverheatButton(coordinator, entry_id, device_id, **common)
        )
    binary = [LuckyMinerProblemSensor(coordinator, entry_id, device_id, **common)]
    return {
        "sensors": sensors,
        "numbers": numbers,
        "switches": switches,
        "selects": selects,
        "text": texts,
        "button": buttons,
        "binary_sensors": binary,
    }
