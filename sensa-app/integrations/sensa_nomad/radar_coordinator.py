"""Proximity coordinator for Sensa Radar nearest-amenity sensors.

Polls the Sensa Radar API for the nearest amenity of each configured category to
the vehicle's current GPS position, and exposes the results for the
``sensor.sensa_nomad_radar_nearest_*`` entities. Those sensors, plus the
existing tank / fuel entities, are what the Stage 2 Routines combine into
location-aware alarms (e.g. "low fresh water and a water point is within 2 mi").

Design notes:
  * Best-effort and connectivity-tolerant. Vans are often on patchy mobile data,
    so a failed poll carries the previous value forward rather than blanking the
    sensor. This mirrors the Schengen tracker's carry-forward approach.
  * Cheap while parked. If the vehicle has not moved beyond a small threshold
    since the last successful poll, the upstream calls are skipped and the last
    result is reused, so a stationary van does not hammer Radar (or, on Pro, run
    up Google Places cost).
  * Gated on a real GPS fix. With no vehicle position we return the last known
    data instead of querying with a stale/zero coordinate.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ._seat_notice import async_note_seat_state
from .const import (
    DOMAIN,
    RADAR_SEARCH_RADIUS_ENTITY_ID,
)
from .radar_client import RadarClient
from .radar_url import async_resolve_radar_session, async_resolve_radar_target

_LOGGER = logging.getLogger(__name__)

# The shared vehicle position (see device_tracker.py).
VEHICLE_TRACKER_ENTITY_ID = "device_tracker.sensa_nomad_vehicle"

# How often to re-poll Radar while the vehicle is moving.
RADAR_SCAN_INTERVAL = timedelta(seconds=120)

# Search radius for "nearest" lookups. Wide enough to reliably find something;
# the alarm proximity threshold (e.g. 2 miles) is applied later in the Routine,
# not here. Metres.
RADAR_SEARCH_RADIUS_M = 16093  # ~10 miles

# Don't re-query upstream unless the vehicle has moved at least this far since
# the last successful poll. Metres.
RADAR_MIN_MOVE_M = 200

# The nearest-sensors to expose: (key, radar_category, label, icon). `key` is the
# sensor id suffix (sensor.sensa_nomad_radar_nearest_<key>); `radar_category` is
# the category id the Radar API understands.
RADAR_NEAREST_SENSORS: tuple[tuple[str, str, str, str], ...] = (
    ("water", "water", "Nearest Water", "mdi:water"),
    ("fuel", "fuel_station", "Nearest Fuel", "mdi:fuel"),
)

# The nearest fuel station that prices DIESEL, for the heaters' Diesel cost
# (`diesel_use.py`). Found in the same fuel lookup: Radar is asked for the
# nearest FUEL_PRICE_LOOKAHEAD stations rather than one, because the nearest is
# often an unpriced OpenStreetMap record. A place dict like the others, or None.
RADAR_DIESEL_PRICE_KEY = "diesel_price"
RADAR_FUEL_CATEGORY = "fuel_station"
FUEL_PRICE_LOOKAHEAD = 25
DIESEL_PRICE_FUEL = "diesel"



def nearest_priced(places: list[Any], fuel: str) -> Any | None:
    """The first (nearest) place with a price for ``fuel``, or None. Radar
    returns places nearest first."""
    for place in places:
        price = (getattr(place, "prices", None) or {}).get(fuel)
        if isinstance(price, (int, float)) and price > 0:
            return place
    return None


# Keys the coordinator publishes for the Radar tier badge on the Cab. The tier is
# the PICKED DRIVER's, not the box's: a licence belongs to a customer, and Premium
# is keyed on a person, so which tier to show depends on who is driving. The
# box-level question ("is this licence assigned to a Hub account at all?") is a
# different one and stays on the licence sensor's `premium_active`.
RADAR_TIER_KEY = "radar_tier"
RADAR_TIER_MEMBER_KEY = "radar_tier_member"
RADAR_TIER_REASON_KEY = "radar_tier_reason"
# The picked driver's Radar bearer, published so the Cab's embedded Radar can be
# signed in AS THAT DRIVER. Without it the iframe runs anonymous, so Radar sees no
# user, and every Premium behaviour it gates on `is_pro` -- Google basemap tiles
# most visibly -- is withheld from everybody, Premium seat or not.
#
# Carried as coordinator data (and on to an UNRECORDED sensor attribute) rather
# than fetched by the card: the exchange needs the licence, which only the box
# holds. See `radar_sensors.SensaRadarTierSensor`.
RADAR_TIER_TOKEN_KEY = "radar_token"

# Customer-facing tier names. These are the STATE of sensor.sensa_nomad_radar_tier
# and the text on the badge, so they are deliberately the words the customer knows
# from the Radar product, not the wire's `is_pro` boolean.
RADAR_TIER_PREMIUM = "Premium"
RADAR_TIER_BASE = "Base"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class RadarProximityCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that keeps the nearest amenity per category up to date.

    ``self.data`` is ``{key: place_dict | None}`` for each configured sensor,
    where ``place_dict`` is ``RadarPlace.as_dict()`` (name, lat, lon, distance_m,
    source, category).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        # The Radar base URL is resolved from the app each poll (see
        # async_resolve_radar_target), so it isn't fixed at construction.
        self._last_poll_lat: float | None = None
        self._last_poll_lon: float | None = None
        # Edge-trigger the "no GPS fix" WARNING so it's visible in the log
        # WITHOUT spamming it every 120s poll: warn once on the transition into
        # no-fix, then stay quiet until a fix returns (which logs an INFO).
        self._warned_no_gps = False
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_radar_coordinator",
            update_interval=RADAR_SCAN_INTERVAL,
        )

    def _vehicle_position(self) -> tuple[float, float] | None:
        """The navigation position, via the shared location-source resolver.

        Honours `select.sensa_nomad_cab_location_source`: an explicit device, or
        Auto = vehicle GPS else the freshest phone. So the nearest-amenity sensors
        use the SAME GPS device the customer picked for the map, and a van with no
        GPS router still works off a phone. See `device_tracker.resolve_nav_position`.
        """
        from .device_tracker import resolve_nav_position

        return resolve_nav_position(self.hass)

    def _search_radius_m(self) -> int:
        """The customer's search radius in METRES.

        Reads the `number` entity, whose NATIVE unit is kilometres regardless of
        what HA displays (it converts to miles for an imperial user on the way
        to the UI and back again on the way in), so this conversion is always
        km -> m and never has to consider the unit system.

        Falls back to the built-in default whenever the entity is missing,
        unavailable or unparseable, an older build, or the window before the
        number platform has set up. Failing to a working radius matters more
        than honouring the setting on the first poll after a restart.
        """
        state = self.hass.states.get(RADAR_SEARCH_RADIUS_ENTITY_ID)
        if state is None:
            return RADAR_SEARCH_RADIUS_M
        try:
            km = float(state.state)
        except (TypeError, ValueError):
            return RADAR_SEARCH_RADIUS_M
        if km <= 0:
            return RADAR_SEARCH_RADIUS_M
        return int(km * 1000)

    async def _async_resolve_tier(
        self, prev: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """The picked driver's Radar tier, plus the bearer to search with.

        Returns `(tier_fields, token)`. The fields are merged into whatever this
        poll ends up returning, INCLUDING the early-return paths, because the tier
        is not a fact about where the van is: a van parked with no GPS fix, or one
        that has not moved since the last poll, still has a driver and still has a
        tier, and both of those paths return before the amenity lookups ever run.
        Resolving it here rather than beside them is what stops the badge going
        blank on a stationary van.

        Fail open: `is_pro is None` means the app could not be asked, so the
        previous verdict is carried forward rather than dropping a paying customer
        to Base. Only a definite answer moves it.
        """
        from .select import current_cab_member_id

        member_id = current_cab_member_id(self.hass)
        token, is_pro, reason = await async_resolve_radar_session(self.hass, member_id)
        if is_pro is None:
            _LOGGER.debug(
                "Radar tier: app did not answer for member %r; carrying forward %r",
                member_id,
                prev.get(RADAR_TIER_KEY),
            )
            carried = {
                k: prev[k]
                for k in (
                    RADAR_TIER_KEY,
                    RADAR_TIER_MEMBER_KEY,
                    RADAR_TIER_REASON_KEY,
                    RADAR_TIER_TOKEN_KEY,
                )
                if k in prev
            }
            return carried, token
        # Tell the customer ONCE when the picked driver holds no seat. Only on a
        # definite answer -- carrying a verdict forward above is not news, and a
        # box that could not ask must never announce a refusal it did not receive.
        await async_note_seat_state(self.hass, member_id, reason)
        return {
            RADAR_TIER_KEY: RADAR_TIER_PREMIUM if is_pro else RADAR_TIER_BASE,
            RADAR_TIER_MEMBER_KEY: member_id,
            RADAR_TIER_REASON_KEY: reason,
            # "" (not None) on the free tier: a member with no seat gets no bearer
            # and the embed correctly runs anonymous. Distinct from the carry-forward
            # path above, which keeps the PREVIOUS token when the app could not be
            # asked -- dropping it there would sign the driver out of their own map
            # on a single hiccup.
            RADAR_TIER_TOKEN_KEY: token or "",
        }, token

    async def _async_update_data(self) -> dict[str, Any]:
        prev: dict[str, Any] = self.data or {}
        # Resolved FIRST, and threaded through every return below, for the reason
        # spelled out in `_async_resolve_tier`: the tier is independent of position.
        tier, token = await self._async_resolve_tier(prev)
        prev = {**prev, **tier}
        pos = self._vehicle_position()
        if pos is None:
            # No GPS fix: keep whatever we last had rather than blanking sensors.
            # WARN (once per no-fix spell) so it's visible why the nearest-amenity
            # sensors sit on "Searching nearby": the vehicle tracker
            # (device_tracker.sensa_nomad_vehicle) has no position, so we have no
            # point to search around. It reads its GPS from a vendor source
            # (currently the Teltonika router); with none present it stays
            # unavailable and this fires.
            if not self._warned_no_gps:
                _LOGGER.warning(
                    "Sensa Radar: no navigation GPS fix from any source (no "
                    "vehicle GPS and no phone with a location) — nearest-amenity "
                    "sensors cannot update. Open the HA Companion app on a phone, "
                    "or add a vehicle GPS source, then pick it in the Location "
                    "device dropdown.",
                )
                self._warned_no_gps = True
            return prev
        if self._warned_no_gps:
            _LOGGER.info("Sensa Radar: vehicle GPS fix restored; resuming lookups")
            self._warned_no_gps = False
        lat, lon = pos

        # Where Radar lives for this environment, per the app's /health. None
        # means it isn't configured (or the app is briefly unreachable) — leave
        # the sensors as they are rather than querying with no target.
        base, insecure_tls = await async_resolve_radar_target(self.hass)
        if not base:
            _LOGGER.debug("Radar coordinator: no Radar URL from app; carrying forward")
            return prev
        # `token` came from `_async_resolve_tier` above: it is the driver picker's
        # member session, so the amenity search returns the saved places of
        # whoever the customer picked. Empty (no picker / no pick) is the primary
        # member, i.e. the pre-picker behaviour.
        client = RadarClient(
            self.hass, base, token=token, insecure_tls=insecure_tls
        )

        # Skip upstream work if we haven't moved since the last successful poll.
        if (
            prev
            and self._last_poll_lat is not None
            and self._last_poll_lon is not None
            and _haversine_m(lat, lon, self._last_poll_lat, self._last_poll_lon)
            < RADAR_MIN_MOVE_M
        ):
            return prev

        result: dict[str, Any] = dict(prev)
        any_success = False
        radius_m = self._search_radius_m()
        for key, category, _label, _icon in RADAR_NEAREST_SENSORS:
            fuel = category == RADAR_FUEL_CATEGORY
            try:
                places = await client.async_nearby(
                    lat=lat,
                    lon=lon,
                    category=category,
                    radius_m=radius_m,
                    limit=FUEL_PRICE_LOOKAHEAD if fuel else 1,
                )
            except (TimeoutError, ConnectionError) as err:
                # Transient: keep the previous value for this category.
                _LOGGER.debug(
                    "Radar coordinator: %s lookup failed (%s); carrying forward",
                    key,
                    err,
                )
                continue
            any_success = True
            result[key] = places[0].as_dict() if places else None
            if fuel:
                priced = nearest_priced(places, DIESEL_PRICE_FUEL)
                result[RADAR_DIESEL_PRICE_KEY] = priced.as_dict() if priced else None

        if any_success:
            self._last_poll_lat = lat
            self._last_poll_lon = lon
        return result
