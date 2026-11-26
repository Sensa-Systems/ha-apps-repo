"""Role energy counters: kWh totals made by integrating role power.

Most vendors report power only, and even the Cerbo gives no running total for
AC load, battery in/out or alternator. These sensors integrate the power of a
vendor-neutral role (`dashboard.resolve_role`) into a `total_increasing` kWh
counter. HA's recorder then keeps hourly long-term statistics for them for
ever, which is what the History tab's day/week/month bars read.

The source entities are never worked out here: every sample asks
`resolve_role`, so the counters follow exactly what the power-flow card
shows (one predicate, one place).

"Could not ask" is not zero. An interval is only counted when both ends were
read cleanly; an unavailable or unparseable source, or a gap longer than
`MAX_GAP_S` (a restart, a stalled loop), adds nothing rather than a guess.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Sampling period. 10 s keeps the trapezoid error well under a percent for
# van loads while costing nothing (a few state reads, no recorder writes:
# the state is only written when it changes by a displayed step).
SAMPLE_INTERVAL = timedelta(seconds=10)

# Longest interval still integrated. Anything longer means we were not
# watching (HA restart, event loop stall), so the energy is unknown.
MAX_GAP_S = 300.0

# Write the state only after this much new energy, so the recorder is not
# given a row every 10 s for a counter that barely moved.
WRITE_STEP_KWH = 0.001


@dataclass(frozen=True)
class CounterSpec:
    """One counter: which role it integrates and which sign it keeps."""

    key: str
    name: str
    role: str
    # +1 keeps positive power, -1 keeps negative power (as a positive
    # energy). Battery power is positive when charging, so charged is +1
    # and discharged is -1.
    sign: int
    icon: str


COUNTERS: tuple[CounterSpec, ...] = (
    CounterSpec("solar", "Solar Energy", "solar_power", 1, "mdi:solar-power"),
    CounterSpec("ac_load", "AC Load Energy", "ac_load", 1, "mdi:power-plug"),
    CounterSpec("dc_load", "DC Load Energy", "dc_load", 1, "mdi:current-dc"),
    CounterSpec("shore", "Shore Energy", "grid_power", 1, "mdi:transmission-tower"),
    CounterSpec("alternator", "Alternator Energy", "alternator", 1, "mdi:engine"),
    CounterSpec(
        "battery_charged", "Battery Charged Energy", "battery_power", 1,
        "mdi:battery-charging",
    ),
    CounterSpec(
        "battery_discharged", "Battery Discharged Energy", "battery_power", -1,
        "mdi:battery-arrow-down",
    ),
)


def counter_entity_id(key: str) -> str:
    """The pinned entity_id for counter `key`. The dashboard builds its
    card config from these, so they must never drift."""
    return f"sensor.sensa_nomad_energy_{key}"


def _watts(state: Any) -> float | None:
    """A state object's power in watts, or None if it cannot be read.

    Only W and kW are accepted. Any other unit, or none, is "could not ask",
    never a number taken at face value.
    """
    if state is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = (getattr(state, "attributes", None) or {}).get("unit_of_measurement")
    if unit == "W":
        return value
    if unit == "kW":
        return value * 1000.0
    return None


def read_role_watts(hass: HomeAssistant, role: str) -> float | None:
    """Current total power of `role` in watts, or None when it cannot be read.

    A sum role is None if ANY resolved member is unreadable: summing the
    readable ones would silently under-count.
    """
    from .dashboard import resolve_role

    try:
        resolved = resolve_role(role)
    except Exception:  # noqa: BLE001 - resolver hiccup is "could not ask"
        return None
    if not resolved:
        return None
    entity_ids = [resolved] if isinstance(resolved, str) else list(resolved)
    total = 0.0
    for eid in entity_ids:
        watts = _watts(hass.states.get(eid))
        if watts is None:
            return None
        total += watts
    return total


class EnergyIntegrator:
    """Trapezoidal integration of signed power into a kWh total.

    Pure (no HA), so the maths is testable on its own. `sample(t, watts)`
    takes a monotonic time in seconds and a power reading, or None when the
    source could not be read.
    """

    def __init__(self, sign: int, total_kwh: float = 0.0) -> None:
        self.sign = sign
        self.total_kwh = total_kwh
        self._last: tuple[float, float] | None = None

    def _kept(self, watts: float) -> float:
        return max(0.0, watts * self.sign)

    def sample(self, t: float, watts: float | None) -> float:
        """Add the energy since the previous sample; return the kWh added."""
        if watts is None:
            # Could not ask: forget the previous reading so the next good one
            # starts a fresh interval instead of bridging the hole.
            self._last = None
            return 0.0
        kept = self._kept(watts)
        added = 0.0
        if self._last is not None:
            t0, w0 = self._last
            dt = t - t0
            if 0.0 < dt <= MAX_GAP_S:
                added = (w0 + kept) / 2.0 * dt / 3_600_000.0
                self.total_kwh += added
        self._last = (t, kept)
        return added


class SensaEnergyCounterSensor(SensorEntity, RestoreEntity):
    """kWh total for one role, fed by `EnergyIntegrator` every 10 s.

    Created for every role on every box, because the roles a box can resolve
    change as devices are added. Until its role has resolved at least once the
    state is unknown, and the History tab leaves unresolved roles out, so an
    idle counter is never drawn.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        entry: ConfigEntry,
        spec: CounterSpec,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entry = entry
        self._spec = spec
        self._clock = clock
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_unique_id = f"{entry.entry_id}_energy_{spec.key}"
        self.entity_id = counter_entity_id(spec.key)
        self._integrator = EnergyIntegrator(spec.sign)
        # None until the role has resolved once (or a total was restored).
        self._value: float | None = None
        self._written: float | None = None

    @property
    def native_value(self) -> float | None:
        if self._value is None:
            return None
        return round(self._value, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"role": self._spec.role}

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
        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                restored = float(last_state.state)
            except (TypeError, ValueError):
                restored = None
            if restored is not None and restored >= 0:
                self._integrator.total_kwh = restored
                self._value = restored
                self._written = restored

        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, SAMPLE_INTERVAL)
        )

    async def _async_tick(self, _now: Any = None) -> None:
        # A coroutine, not a plain callback: HA runs a sync timer callback in
        # an executor thread, where writing state is not allowed.
        self.sample_once()

    @callback
    def sample_once(self) -> None:
        watts = read_role_watts(self.hass, self._spec.role)
        self._integrator.sample(self._clock(), watts)
        if watts is not None and self._value is None:
            # First time the role resolved: start at the running total (0 on
            # a fresh box) so the counter becomes known.
            self._value = self._integrator.total_kwh
            self._written = None
        if self._value is None:
            return
        self._value = self._integrator.total_kwh
        if self._written is None or self._value - self._written >= WRITE_STEP_KWH:
            self._written = self._value
            self.async_write_ha_state()


def build_energy_counters(entry: ConfigEntry) -> list[SensaEnergyCounterSensor]:
    """One counter per `COUNTERS` spec."""
    return [SensaEnergyCounterSensor(entry, spec) for spec in COUNTERS]
