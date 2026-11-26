"""Home Assistant Energy dashboard: Sensa Nomad as a solar forecast provider.

Home Assistant looks for `async_get_solar_forecast` in an integration's
`energy` platform and offers that integration under Energy -> Solar panels ->
"Forecast production". The forecast is the van's expected charge per hour from
`SolarForecastCoordinator` (solar_forecast.py), in watt-hours keyed by the
start of each hour, which is the shape the dashboard expects.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, Any] | None:
    runtime = hass.data.get(DOMAIN, {}).get(config_entry_id) or {}
    coordinator = runtime.get("solar_coordinator")
    if coordinator is None:
        return None
    hours = coordinator.watts_by_hour()
    if not hours:
        return None
    # An hour's average watts is the energy in watt-hours over that hour.
    return {"wh_hours": {start.isoformat(): round(w, 1) for start, w in hours}}
