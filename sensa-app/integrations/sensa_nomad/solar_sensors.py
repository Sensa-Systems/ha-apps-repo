"""Solar forecast sensors: expected charge from the van's panels.

Three sensors over `SolarForecastCoordinator` (solar_forecast.py):

  * Solar Forecast Today: kWh still to come today (local day), with every
    local day the forecast reaches as `days` (date, kwh) for the Weather card.
  * Solar Forecast Tomorrow: kWh for tomorrow.
  * Solar Forecast Next Hour: average watts expected over the coming hour, with
    the next 24 hours as an attribute for charts and routines.
  * Solar Forecast Accuracy: what the panels actually made as a percentage of
    the forecast over the last 7 days, with each day's forecast and actual
    kWh as an attribute (solar_accuracy.py). Owns the hourly tracker.

Each reads "unknown" with a `status` attribute saying why when there is no
forecast: `not_configured` (no panel rating set), `outside_coverage` (the
forecast covers Europe), `no_position`, `no_hub` or `unavailable`. Credit for
the data (CC BY 4.0, "Contains DWD data") rides on every sensor.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import solar_accuracy, solar_clock
from .const import (
    DOMAIN,
    SOLAR_ACCURACY_STORAGE_KEY,
    SOLAR_ACCURACY_STORAGE_VERSION,
    SOLAR_FORECAST_ACCURACY_ENTITY_ID,
    SOLAR_FORECAST_NEXT_HOUR_ENTITY_ID,
    SOLAR_FORECAST_TODAY_ENTITY_ID,
    SOLAR_FORECAST_TOMORROW_ENTITY_ID,
)
from .solar_forecast import SolarForecastCoordinator, energy_for_day

ATTRIBUTION = "Solar forecast: Contains DWD data (ICON-EU, CC BY 4.0), via Sensa"


class _SolarSensor(CoordinatorEntity[SolarForecastCoordinator], SensorEntity):
    _attr_has_entity_name = False
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: SolarForecastCoordinator,
        key: str,
        name: str,
        entity_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_solar_forecast_{key}"
        self.entity_id = entity_id

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        attrs: dict[str, Any] = {
            "status": data.get("status", "unavailable"),
            # "sim" when the forecast is for the van simulator's time and place
            # (solar_clock.py), so a screen can say so; "real" otherwise.
            "clock": "sim" if data.get("sim") else "real",
        }
        forecast = data.get("forecast") or {}
        if data.get("status") == "ok":
            attrs["model_run"] = forecast.get("model_run")
            attrs["updated_at"] = forecast.get("updated_at")
            attrs["panel_rating_w"] = self.coordinator.rating_wp()
        return attrs


class SolarForecastDaySensor(_SolarSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, entry, coordinator, *, tomorrow: bool) -> None:
        super().__init__(
            entry,
            coordinator,
            "tomorrow" if tomorrow else "today",
            "Solar Forecast Tomorrow" if tomorrow else "Solar Forecast Today",
            SOLAR_FORECAST_TOMORROW_ENTITY_ID
            if tomorrow
            else SOLAR_FORECAST_TODAY_ENTITY_ID,
        )
        self._tomorrow = tomorrow

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._tomorrow:
            return
        # A move into another 10 km square fetches at once (hub_weather.py).
        self.async_on_remove(self.coordinator.watch_moves())
        # The van simulator's clock (solar_clock.py) can jump: its time slider
        # moves hours at once, and at startup the simulator's GPS reports after
        # the first forecast was fetched on the real clock. Fetch again the
        # moment its time changes rather than waiting for the next poll. The
        # coordinator's own debounce keeps a racing clock to one fetch a burst.
        @callback
        def _sim_time_changed(event) -> None:
            new = event.data.get("new_state")
            old = event.data.get("old_state")
            new_t = new.attributes.get("sim_time") if new is not None else None
            old_t = old.attributes.get("sim_time") if old is not None else None
            if new_t and new_t != old_t:
                self.hass.async_create_task(self.coordinator.async_request_refresh())

        self.async_on_remove(
            # "state_changed" is homeassistant.const.EVENT_STATE_CHANGED.
            self.hass.bus.async_listen("state_changed", _sim_time_changed)
        )

    def _hours(self) -> tuple[datetime, list[tuple[datetime, float]]]:
        now, _sim = solar_clock.now(self.hass)
        hour = now.replace(minute=0, second=0, microsecond=0)
        # Hours from the one under way: on the simulator's racing clock the
        # forecast fetched a few minutes ago can already hold passed hours.
        return now, [(t, w) for t, w in self.coordinator.watts_by_hour() if t >= hour]

    @property
    def native_value(self) -> float | None:
        now, hours = self._hours()
        if not hours:
            return None
        tz = dt_util.get_default_time_zone()
        day = now.astimezone(tz).date() + timedelta(days=1 if self._tomorrow else 0)
        return energy_for_day(hours, day, tz)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        if self._tomorrow:
            return attrs
        _now, hours = self._hours()
        tz = dt_util.get_default_time_zone()
        days = sorted({t.astimezone(tz).date() for t, _ in hours})
        attrs["days"] = [
            {"date": d.isoformat(), "kwh": energy_for_day(hours, d, tz)} for d in days
        ]
        return attrs


class SolarForecastNextHourSensor(_SolarSensor):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:solar-power"

    def __init__(self, entry, coordinator) -> None:
        super().__init__(
            entry,
            coordinator,
            "next_hour",
            "Solar Forecast Next Hour",
            SOLAR_FORECAST_NEXT_HOUR_ENTITY_ID,
        )

    def _upcoming(self) -> list[tuple[datetime, float]]:
        now, _sim = solar_clock.now(self.hass)
        now = now.replace(minute=0, second=0, microsecond=0)
        return [(t, w) for t, w in self.coordinator.watts_by_hour() if t >= now]

    @property
    def native_value(self) -> float | None:
        upcoming = self._upcoming()
        return round(upcoming[0][1]) if upcoming else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["forecast"] = [
            {"time": t.isoformat(), "watts": round(w)} for t, w in self._upcoming()[:24]
        ]
        return attrs


class SolarForecastAccuracySensor(_SolarSensor):
    """Actual against forecast, and the hourly tracker that records it.

    State: actual as a percentage of forecast over the last 7 days (100 = spot
    on). Attributes: `days` (the last 7, oldest first, each date / forecast_kwh
    / actual_kwh / hours) and today so far. The tracker's state is also left in
    the entry's runtime data as `solar_accuracy` for diagnostics_report.py.
    """

    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:bullseye-arrow"

    def __init__(self, entry, coordinator) -> None:
        super().__init__(
            entry,
            coordinator,
            "accuracy",
            "Solar Forecast Accuracy",
            SOLAR_FORECAST_ACCURACY_ENTITY_ID,
        )
        self._state: dict[str, Any] = {}
        self._store: Any = None
        self._unsub = None

    def _runtime(self) -> dict[str, Any]:
        return self.hass.data.setdefault(DOMAIN, {}).setdefault(
            self._entry.entry_id, {}
        )

    async def async_added_to_hass(self) -> None:
        from homeassistant.helpers.event import async_track_time_change
        from homeassistant.helpers.storage import Store

        await super().async_added_to_hass()
        self._store = Store(
            self.hass, SOLAR_ACCURACY_STORAGE_VERSION, SOLAR_ACCURACY_STORAGE_KEY
        )
        self._state = (await self._store.async_load()) or {}
        self._runtime()["solar_accuracy"] = self._state
        # A few seconds past the hour: the energy counter integrates every 10 s,
        # so this reads it as near the boundary as it gets.
        self._unsub = async_track_time_change(
            self.hass, self._async_tick, minute=0, second=5
        )
        self.async_on_remove(self._unsub)

    def _counter_kwh(self) -> float | None:
        from .energy_counters import counter_entity_id

        st = self.hass.states.get(counter_entity_id("solar"))
        try:
            return float(st.state) if st is not None else None
        except (TypeError, ValueError):
            return None

    async def _async_tick(self, now: datetime) -> None:
        # Real hours only. On the van simulator's clock the solar energy
        # counter still integrates in real time, so a sped-up hour would read
        # as a fraction of its forecast, and simulated days must never reach
        # the accuracy figures sent to Sensa.
        if solar_clock.sim_now(self.hass) is not None:
            return
        hour_start = dt_util.as_utc(now).replace(minute=0, second=0, microsecond=0)
        rating = self.coordinator.rating_wp()
        data = self.coordinator.data or {}
        forecast = (
            solar_accuracy.forecast_kwh_for(
                self.coordinator.watts_by_hour(), hour_start
            )
            if data.get("status") == "ok" and rating > 0
            else None
        )
        self._state = solar_accuracy.tick(
            self._state,
            hour_start,
            self._counter_kwh(),
            forecast,
            rating,
            dt_util.get_default_time_zone(),
        )
        self._runtime()["solar_accuracy"] = self._state
        if self._store is not None:
            await self._store.async_save(self._state)
        self.async_write_ha_state()

    def _rows(self) -> list[dict[str, Any]]:
        today = datetime.now(dt_util.get_default_time_zone()).date()
        return solar_accuracy.day_rows(self._state, today, solar_accuracy.DISPLAY_DAYS + 1)

    @property
    def native_value(self) -> float | None:
        today = datetime.now(dt_util.get_default_time_zone()).date().isoformat()
        past = [r for r in self._rows() if r["date"] != today]
        return solar_accuracy.accuracy_pct(past[-solar_accuracy.DISPLAY_DAYS :])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        rows = self._rows()
        today = datetime.now(dt_util.get_default_time_zone()).date().isoformat()
        attrs["days"] = rows[-solar_accuracy.DISPLAY_DAYS :]
        now_row = next((r for r in rows if r["date"] == today), None)
        attrs["today_forecast_kwh"] = now_row["forecast_kwh"] if now_row else None
        attrs["today_actual_kwh"] = now_row["actual_kwh"] if now_row else None
        return attrs


def build_solar_sensors(
    entry: ConfigEntry, coordinator: SolarForecastCoordinator
) -> list[SensorEntity]:
    return [
        SolarForecastDaySensor(entry, coordinator, tomorrow=False),
        SolarForecastDaySensor(entry, coordinator, tomorrow=True),
        SolarForecastNextHourSensor(entry, coordinator),
        SolarForecastAccuracySensor(entry, coordinator),
    ]
