"""What every poll of Sensa Hub's weather API shares: where, where to, and the meter.

The solar forecast (solar_forecast.py) and the weather card's forecast
(weather_forecast.py) both ask Hub's open weather API about the van's
position. Writing that twice is how the two would drift, so it lives here:

  * Position: a GPS fitted to the van (`resolve_van_position`), else Home
    Assistant's configured location. Never a phone: the owner's phone may be
    hundreds of miles from the van.
  * Privacy: rounded to 0.1 degrees (about 10 km) before it leaves the van, and
    the call carries no licence or account. Disclosed in the privacy policy
    (Sensa Nomad, Weather and solar forecast) and in
    docs/system-design/system-design.md.
  * Hub's address comes from the Sensa App's `/health` (`hub_url`), like Radar's.
  * Every call is metered as "Weather" on Settings -> Data usage.
  * A move is noticed at once (`watch_moves`): when the van crosses into another
    0.1 degree square the forecast is fetched again, rather than waiting up to
    30 minutes for the next poll.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import HUB_URL_KEY

_LOGGER = logging.getLogger(__name__)

# 0.1 degrees is about 10 km of latitude.
POSITION_DECIMALS = 1
# A move into a NEW square fetches at once. Asking again for the SAME square
# waits this long: while Hub is unreachable the kept forecast stays "for the old
# square", and every GPS fix would otherwise retry.
MOVE_REFETCH_S = 120
TIMEOUT_S = 20


def rounded(lat: float, lon: float) -> tuple[float, float]:
    """The position as it may leave the van."""
    return round(lat, POSITION_DECIMALS), round(lon, POSITION_DECIMALS)


class HubWeatherCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Base for coordinators that poll Hub's weather API at the van's position."""

    def _position(self) -> tuple[float, float] | None:
        from .device_tracker import resolve_van_position

        pos = resolve_van_position(self.hass)
        if pos is not None:
            return pos
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        if lat is None or lon is None or (lat == 0 and lon == 0):
            return None
        return float(lat), float(lon)

    def moved(self) -> bool:
        """Whether the van is now in another 0.1 degree square than the one the
        last answer was for, or there is no answer yet. Only for a coordinator
        that is fetching: while it is off or not set up, a move is nothing to
        act on. `watch_moves` rate-limits what this lets through."""
        data = self.data or {}
        if data.get("status") in ("off", "not_configured"):
            return False
        pos = self._position()
        if pos is None:
            return False
        last = data.get("position")
        # No answer yet (no position or no Hub at start-up, before the GPS or
        # the App had reported): a position now is reason enough to try again.
        return last is None or tuple(last) != rounded(*pos)

    def watch_moves(self) -> Callable[[], None]:
        """Fetch again as soon as the van moves into another square. The van's
        position comes from a device tracker (its GPS, or the simulator's), so
        a tracker update is the cue; the coordinator's own debounce keeps a
        stream of GPS fixes to one fetch. Returns the unsubscribe."""

        asked: dict[str, Any] = {"square": None, "at": 0.0}

        @callback
        def _changed(event: Event) -> None:
            if not str(event.data.get("entity_id", "")).startswith("device_tracker."):
                return
            if not self.moved():
                return
            pos = self._position()
            square = rounded(*pos) if pos is not None else None
            now = time.monotonic()
            if square == asked["square"] and now - asked["at"] < MOVE_REFETCH_S:
                return  # already asked for this square: a retry, so wait
            asked.update(square=square, at=now)
            self.hass.async_create_task(self.async_request_refresh())

        # "state_changed" is homeassistant.const.EVENT_STATE_CHANGED.
        return self.hass.bus.async_listen("state_changed", _changed)

    async def _hub_url(self) -> str | None:
        from .api.client import SensaApiClient
        from .backend_url import resolve_backend_url

        try:
            health = await SensaApiClient(
                self.hass, resolve_backend_url(self.hass)
            ).async_health_quiet()
        except Exception:  # noqa: BLE001
            return None
        url = (health or {}).get(HUB_URL_KEY)
        return url.strip().rstrip("/") if isinstance(url, str) and url.strip() else None

    async def _get(self, url: str, params: dict[str, Any]) -> tuple[int, Any]:
        """GET one Hub weather URL: (status, parsed JSON body on a 200, else None)."""
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass)
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)
        ) as resp:
            raw = await resp.read()
            # On Settings -> Data usage as "Weather": the owner should see this
            # poll beside everything else the box sends.
            from . import egress

            egress.record("weather", 40, len(raw))
            return resp.status, json.loads(raw) if resp.status == 200 else None
