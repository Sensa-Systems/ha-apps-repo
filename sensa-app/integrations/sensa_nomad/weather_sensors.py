"""The Weather sensor: weather at the van for the Weather card.

`sensor.sensa_nomad_weather` over `WeatherCoordinator` (weather_forecast.py).
State: the temperature now (degrees C; Home Assistant shows it in the home
unit). Attributes, for `sensa-weather-card`:

  * `condition`: MET Norway's symbol for now, e.g. "partlycloudy_night".
  * `now`: wind, gusts, humidity, rain this hour, and the UV index and air
    quality (European Air Quality Index and the pollutant that set it) for the
    hour under way when Hub has them.
  * `days`: the next few days (UTC dates, as Hub gives them), each with low and
    high, rain, condition, the UV peak and the worst air quality.
  * `warnings`: the official warnings in force, most severe first, their text
    unchanged (several agencies' licences require it).
  * `credits`: the sources actually shown, each with its licence and link, as
    their licences require (a card lists them behind a "Sources" link).

Reads "unknown" with a `status` attribute saying why when there is no forecast:
`off` (the owner has not turned on `switch.sensa_nomad_weather_forecast`),
`no_position`, `no_hub` or `unavailable`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, WEATHER_ENTITY_ID
from .weather_forecast import WeatherCoordinator

DAYS = 5
_SEVERITY_ORDER = {"severe": 0, "warning": 1, "alert": 2}


def _parse(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def air_now(air: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """UV and air quality for the hour under way, if Hub has it."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    for h in (air or {}).get("hourly") or []:
        t = _parse(h.get("time"))
        if t is not None and t >= hour:
            return {
                k: h.get(k)
                for k in (
                    "uv_index",
                    "eaqi",
                    "eaqi_label",
                    "eaqi_pollutant",
                    "pm2_5",
                    "pm10",
                    "o3",
                    "no2",
                )
            }
    return {}


def merge_days(weather: dict[str, Any], air: dict[str, Any] | None) -> list[dict]:
    """The forecast days with each day's UV peak and worst air quality."""
    air_days = {d.get("date"): d for d in (air or {}).get("daily") or []}
    out = []
    for d in (weather.get("daily") or [])[:DAYS]:
        a = air_days.get(d.get("date"), {})
        out.append(
            {
                "date": d.get("date"),
                "temperature_min_c": d.get("temperature_min_c"),
                "temperature_max_c": d.get("temperature_max_c"),
                "precipitation_mm": d.get("precipitation_mm"),
                "condition": d.get("condition"),
                "uv_index_max": a.get("uv_index_max"),
                "eaqi_max": a.get("eaqi_max"),
                "eaqi_label": a.get("eaqi_max_label"),
            }
        )
    return out


def warnings_of(weather: dict[str, Any]) -> list[dict]:
    keep = (
        "event",
        "severity",
        "headline",
        "description",
        "onset",
        "expires",
        "updated_at",
        "sender",
    )
    found = [{k: w.get(k) for k in keep} for w in weather.get("warnings") or []]
    return sorted(found, key=lambda w: _SEVERITY_ORDER.get(w.get("severity"), 3))


def credits_of(weather: dict[str, Any], air: dict[str, Any] | None) -> list[dict]:
    """The sources actually shown, each as its licence asks to be named.

    Hub lists every agency whose area the van is in or near; a card must name
    only what it shows: the forecast's source (MET Norway) always, a warning
    agency only while one of its warnings is in force (a warning's sender is
    the start of its agency's credit: "Met Office" -> "Met Office Weather
    Warnings"), and Copernicus only with UV or air quality figures."""
    senders = [w.get("sender") for w in weather.get("warnings") or [] if w.get("sender")]

    def shown(name: str) -> bool:
        return name == "MET Norway" or any(name.startswith(s) for s in senders)

    out = [
        {"name": a.get("name"), "licence": a.get("licence"), "url": a.get("url")}
        for a in (weather.get("attribution") or [])
        if a.get("name") and shown(a["name"])
    ]
    out += [
        {"name": a.get("name"), "licence": a.get("licence"), "url": a.get("url")}
        for a in ((air or {}).get("attribution") or [])
        if a.get("name")
    ]
    return out


class WeatherSensor(CoordinatorEntity[WeatherCoordinator], SensorEntity):
    _attr_has_entity_name = False
    _attr_name = "Weather"
    _attr_icon = "mdi:weather-partly-cloudy"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_suggested_display_precision = 0
    # The forecast lists change every poll and would bloat the history database.
    _unrecorded_attributes = frozenset({"now", "days", "warnings", "credits"})

    def __init__(self, entry: ConfigEntry, coordinator: WeatherCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_weather"
        self.entity_id = WEATHER_ENTITY_ID

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # A move into another 10 km square fetches at once (hub_weather.py).
        self.async_on_remove(self.coordinator.watch_moves())

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def _weather(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        if data.get("status") != "ok":
            return {}
        return data.get("weather") or {}

    @property
    def native_value(self) -> float | None:
        current = self._weather().get("current") or {}
        value = current.get("temperature_c")
        return round(float(value), 1) if isinstance(value, int | float) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        attrs: dict[str, Any] = {"status": data.get("status", "unavailable")}
        weather = self._weather()
        if not weather:
            return attrs
        air = data.get("air")
        current = weather.get("current") or {}
        attrs["condition"] = current.get("condition")
        attrs["now"] = {
            "wind_speed_ms": current.get("wind_speed_ms"),
            "wind_gust_ms": current.get("wind_gust_ms"),
            "wind_direction_deg": current.get("wind_direction_deg"),
            "humidity_pct": current.get("humidity_pct"),
            "precipitation_mm": current.get("precipitation_mm"),
            **air_now(air, datetime.now(UTC)),
        }
        attrs["days"] = merge_days(weather, air)
        attrs["warnings"] = warnings_of(weather)
        attrs["credits"] = credits_of(weather, air)
        fetched = data.get("fetched_at")
        attrs["updated_at"] = fetched.isoformat() if fetched else None
        return attrs


def build_weather_sensors(entry: ConfigEntry, coordinator: WeatherCoordinator):
    return [WeatherSensor(entry, coordinator)]
