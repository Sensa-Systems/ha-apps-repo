"""Weather at the van, from Sensa Hub: forecast, official warnings, UV and air.

Two calls to Hub's open weather API every 30 minutes, for the Weather card
(`sensa-weather-card`) through `sensor.sensa_nomad_weather`:

  * `GET /api/v1/weather`: current conditions, the next days and the official
    warnings in force (MET Norway forecast, national agencies' warnings).
  * `GET /api/v1/weather/air`: UV index and air quality (Copernicus CAMS,
    Europe). Optional: outside Europe, or while Hub has no run stored, the card
    simply shows no UV or air figures, and a failed call keeps the last ones.

Nothing is fetched while `switch.sensa_nomad_weather_forecast` is off (the
default): the owner turns it on from the Weather card, which says what is sent.
Position, privacy (0.1 degree rounding), Hub's address and the data meter are
shared with the solar forecast (hub_weather.py). Told vs could-not-ask: a failed
poll keeps the last forecast, which stays useful for hours.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN, WEATHER_SWITCH_ENTITY_ID
from .hub_weather import HubWeatherCoordinator, rounded

_LOGGER = logging.getLogger(__name__)

WEATHER_SCAN_INTERVAL = timedelta(minutes=30)
WEATHER_PATH = "/api/v1/weather"
AIR_PATH = "/api/v1/weather/air"


class WeatherCoordinator(HubWeatherCoordinator):
    """Polls Hub for the weather and air at the van's rounded position."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_weather",
            update_interval=WEATHER_SCAN_INTERVAL,
        )
        self._failures = 0

    def enabled(self) -> bool:
        state = self.hass.states.get(WEATHER_SWITCH_ENTITY_ID)
        return state is not None and state.state == "on"

    async def _async_update_data(self) -> dict[str, Any]:
        if not self.enabled():
            # Off: an answer, and nothing is fetched or sent.
            return {"status": "off"}
        previous = self.data or {}
        pos = self._position()
        if pos is None:
            return previous or {"status": "no_position"}
        hub = await self._hub_url()
        if not hub:
            return previous or {"status": "no_hub"}
        lat, lon = rounded(*pos)
        params = {"lat": lat, "lon": lon}
        try:
            status, weather = await self._get(f"{hub}{WEATHER_PATH}", params)
        except Exception as exc:  # noqa: BLE001 - patchy mobile data is normal
            self._failures += 1
            log = _LOGGER.warning if self._failures in (3, 30) else _LOGGER.debug
            log("Weather: Hub unreachable (%s), keeping last forecast", exc)
            return previous or {"status": "unavailable"}
        self._failures = 0
        if status != 200 or not isinstance(weather, dict):
            return previous or {"status": "unavailable"}
        try:
            air_status, air = await self._get(f"{hub}{AIR_PATH}", params)
        except Exception:  # noqa: BLE001 - the forecast stands without it
            air_status, air = None, None
        if air_status == 200 and isinstance(air, dict):
            pass
        elif air_status in (404, 503):
            air = None  # an answer: outside Europe, or nothing stored yet
        else:
            air = previous.get("air")  # could not ask: keep the last
        return {
            "status": "ok",
            "weather": weather,
            "air": air,
            "position": (lat, lon),
            "fetched_at": datetime.now(UTC),
        }
