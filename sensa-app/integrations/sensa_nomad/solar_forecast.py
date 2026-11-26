"""Solar forecast: expected charge from the van's panels, from Sensa Hub.

Hub's open weather API serves hourly solar irradiance for Europe (DWD
ICON-EU, `GET /api/v1/weather/solar`). This turns it into what an owner can
use: the energy the van's panels should make in each hour, today and tomorrow,
and feeds Home Assistant's Energy dashboard forecast (`energy.py`).

Design notes:
  * The owner turns it on (`switch.sensa_nomad_solar_forecast`, off by default)
    and sets the panels' rating (Wp, `number.sensa_nomad_solar_array_rating`).
    Until both are done nothing is fetched at all, so no position leaves the van
    for a feature the owner is not using.
  * Position, privacy (rounded to 0.1 degrees; the forecast grid is about
    14 km, so accuracy is unchanged), Hub's address and the data meter are
    shared with the weather card's forecast: hub_weather.py.
  * Panels on a van roof lie flat, so the horizontal irradiance Hub serves is the
    right input. Energy = irradiance / 1000 x rating x PERFORMANCE_RATIO, the
    usual allowance for heat, wiring and charge-controller losses.
  * Told vs could-not-ask: a failed poll keeps the last forecast (it stays valid
    for hours); "not configured" and "outside Europe" are answers and say so.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN, SOLAR_ARRAY_RATING_ENTITY_ID, SOLAR_SWITCH_ENTITY_ID
from .hub_weather import HubWeatherCoordinator, rounded

_LOGGER = logging.getLogger(__name__)

SOLAR_SCAN_INTERVAL = timedelta(minutes=30)
# On the van simulator's clock (solar_clock.py) "now" can race ahead at x60 or
# more, so the forecast is asked for more often to keep up with it.
SIM_SCAN_INTERVAL = timedelta(minutes=2)
SOLAR_PATH = "/api/v1/weather/solar"
# Typical for small flat-mounted PV through an MPPT controller into a battery.
PERFORMANCE_RATIO = 0.8


def hourly_watts(forecast: dict[str, Any], rating_wp: float) -> list[tuple[datetime, float]]:
    """(start of hour, expected average watts from the panels) per forecast hour."""
    out = []
    for h in forecast.get("hourly") or []:
        try:
            start = datetime.fromisoformat(str(h["time"]).replace("Z", "+00:00"))
            ghi = float(h["ghi_wm2"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((start, ghi / 1000 * rating_wp * PERFORMANCE_RATIO))
    return out


def energy_for_day(
    hours: list[tuple[datetime, float]], day: date, tz
) -> float | None:
    """kWh expected over a local calendar day, from the hours forecast for it.

    None when the forecast does not reach that day. Hours already past are not
    in the forecast, so "today" is what is still to come.
    """
    in_day = [w for start, w in hours if start.astimezone(tz).date() == day]
    if not in_day:
        return None
    return round(sum(in_day) / 1000, 2)


class SolarForecastCoordinator(HubWeatherCoordinator):
    """Polls Hub for the solar forecast at the van's rounded position."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_solar_forecast",
            update_interval=SOLAR_SCAN_INTERVAL,
        )
        self._failures = 0

    def rating_wp(self) -> float:
        """The panels' rating, or 0 while the forecast is switched off. The one
        test every consumer uses, so "off" and "no rating" cannot disagree."""
        switch = self.hass.states.get(SOLAR_SWITCH_ENTITY_ID)
        if switch is None or switch.state != "on":
            return 0.0
        state = self.hass.states.get(SOLAR_ARRAY_RATING_ENTITY_ID)
        try:
            return max(0.0, float(state.state)) if state is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    async def _async_update_data(self) -> dict[str, Any]:
        previous = self.data or {}
        if self.rating_wp() <= 0:
            # Not configured: an answer, and nothing is fetched or sent.
            return {"status": "not_configured"}
        pos = self._position()
        if pos is None:
            return previous or {"status": "no_position"}
        hub = await self._hub_url()
        if not hub:
            return previous or {"status": "no_hub"}
        lat, lon = rounded(*pos)
        from . import solar_clock

        at, sim = solar_clock.now(self.hass)
        self.update_interval = SIM_SCAN_INTERVAL if sim else SOLAR_SCAN_INTERVAL
        try:
            status, body = await self._fetch(hub, lat, lon, at if sim else None)
        except Exception as exc:  # noqa: BLE001 - patchy mobile data is normal
            self._failures += 1
            log = _LOGGER.warning if self._failures in (3, 30) else _LOGGER.debug
            log("Solar forecast: Hub unreachable (%s), keeping last forecast", exc)
            return previous or {"status": "unavailable"}
        self._failures = 0
        if status == 404:
            return {"status": "outside_coverage"}
        if status != 200 or not isinstance(body, dict):
            return previous or {"status": "unavailable"}
        return {
            "status": "ok",
            "forecast": body,
            "position": (lat, lon),
            "fetched_at": datetime.now(UTC),
            # The forecast was asked for at the simulator's time and place.
            "sim": sim,
        }

    async def _fetch(
        self, hub: str, lat: float, lon: float, at: datetime | None = None
    ) -> tuple[int, Any]:
        return await self._get(
            f"{hub}{SOLAR_PATH}",
            {"lat": lat, "lon": lon}
            | ({"at": at.isoformat()} if at is not None else {}),
        )

    def watts_by_hour(self) -> list[tuple[datetime, float]]:
        data = self.data or {}
        if data.get("status") != "ok":
            return []
        return hourly_watts(data["forecast"], self.rating_wp())
