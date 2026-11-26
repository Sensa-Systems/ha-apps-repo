"""Country resolution for the Schengen tracker.

These customers generally have internet of some kind (phone hotspot or vehicle
router), so the primary country resolver is an **online reverse-geocode** of the
vehicle's GPS position. It's free + keyless (BigDataCloud's reverse-geocode-client
endpoint), returns an ISO-3166 alpha-2 country code, and is cached by a coarsely
rounded coordinate so a parked/slowly-moving van doesn't hammer it.

**Only the rounded coordinate is ever sent.** The van's exact position is a far
more sensitive fact than the one we need, which is merely "which country is this".
Two decimal places is a ~1.1 km grid: ample to identify a country (the ambiguous
case is a border, and a full-precision fix resolves that no more reliably than a
rounded one), and coarse enough that the egress is not a movement trace. The
request is what leaves the van, so the rounding must happen before the call and
not merely in the cache key.

The OFFLINE safety-net is NOT a bundled polygon (a hand-authored multi-country
outline would risk mis-classifying border/enclave points — and wrongly marking
the UK as in-Schengen, the worst possible error for our main audience). Instead
the sampler (`__init__.py`) carries forward the last resolved country while the
van is stationary, and leaves a day unstamped when we have never resolved a
country and can't reach the network — the customer's manual edit fills any gap.

Membership: the 29 Schengen states (2025 — EU minus Ireland & Cyprus, plus
Iceland, Norway, Switzerland, Liechtenstein; Bulgaria + Romania fully joined
Jan 2025). The open-border microstates (Monaco, San Marino, Vatican, Andorra)
are treated as Schengen too — you can't reach them without being inside the area,
so a GPS fix there must still count toward the 90/180.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import egress

_LOGGER = logging.getLogger(__name__)

# The 29 Schengen member states (ISO-3166-1 alpha-2).
SCHENGEN_COUNTRIES: frozenset[str] = frozenset({
    "AT", "BE", "BG", "HR", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IS", "IT", "LV", "LI", "LT", "LU", "MT", "NL",
    "NO", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "CH",
})

# Open-border microstates surrounded by Schengen — de-facto inside the area for
# the purposes of the 90/180 count.
SCHENGEN_DE_FACTO: frozenset[str] = frozenset({"MC", "SM", "VA", "AD"})

# Human-readable names for the codes we expect to surface, for the card tooltip
# / chip. Not exhaustive — an unknown code just shows the raw code.
COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia",
    "CZ": "Czechia", "DK": "Denmark", "EE": "Estonia", "FI": "Finland",
    "FR": "France", "DE": "Germany", "GR": "Greece", "HU": "Hungary",
    "IS": "Iceland", "IT": "Italy", "LV": "Latvia", "LI": "Liechtenstein",
    "LT": "Lithuania", "LU": "Luxembourg", "MT": "Malta", "NL": "Netherlands",
    "NO": "Norway", "PL": "Poland", "PT": "Portugal", "RO": "Romania",
    "SK": "Slovakia", "SI": "Slovenia", "ES": "Spain", "SE": "Sweden",
    "CH": "Switzerland", "MC": "Monaco", "SM": "San Marino", "VA": "Vatican",
    "AD": "Andorra", "GB": "United Kingdom", "IE": "Ireland", "CY": "Cyprus",
}

# Keyless, client-oriented reverse geocoder. Returns `countryCode` (alpha-2).
_REVERSE_GEOCODE_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
_HTTP_TIMEOUT = 8  # seconds — short; a miss just defers to carry-forward
_CACHE_CAP = 512
_CACHE_PRECISION = 2  # ~1.1 km grid — plenty for country resolution

# Process-lived cache: rounded (lat, lon) -> alpha-2 code. Country boundaries
# don't move, so this never needs invalidation within a run.
_cache: dict[tuple[float, float], str] = {}


def is_schengen(cc: str | None) -> bool:
    """True when an ISO-3166 alpha-2 code is in (or de-facto in) the Schengen area."""
    if not cc:
        return False
    code = cc.strip().upper()
    return code in SCHENGEN_COUNTRIES or code in SCHENGEN_DE_FACTO


def country_name(cc: str | None) -> str | None:
    if not cc:
        return None
    code = cc.strip().upper()
    return COUNTRY_NAMES.get(code, code)


def _cache_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, _CACHE_PRECISION), round(lon, _CACHE_PRECISION))


async def resolve_country(hass: Any, lat: float, lon: float) -> str | None:
    """Reverse-geocode `lat`/`lon` to an ISO-3166 alpha-2 country code.

    Returns the uppercase code, or None when the lookup fails (no network,
    timeout, bad response) — the caller then carries forward the last known
    country. Cached by a coarsely-rounded coordinate.
    """
    try:
        lat = float(lat)
        lon = float(lon)
    except (ValueError, TypeError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    key = _cache_key(lat, lon)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    # Send the ROUNDED coordinate, never `lat`/`lon`. See the module docstring:
    # BigDataCloud is a third party, and it only needs to know the country.
    grid_lat, grid_lon = key

    try:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        async with asyncio.timeout(_HTTP_TIMEOUT):
            resp = await session.get(
                _REVERSE_GEOCODE_URL,
                params={
                    "latitude": grid_lat,
                    "longitude": grid_lon,
                    "localityLanguage": "en",
                },
            )
            if resp.status != 200:
                return None
            payload = await resp.json()
            egress.record("other", 0, len(json.dumps(payload)))
    except Exception:
        _LOGGER.debug("Schengen reverse-geocode failed", exc_info=True)
        return None

    code = (payload or {}).get("countryCode")
    if not isinstance(code, str) or len(code) != 2:
        return None
    code = code.upper()

    # Bounded cache — drop an arbitrary entry when full (order is good enough).
    if len(_cache) >= _CACHE_CAP:
        try:
            _cache.pop(next(iter(_cache)))
        except StopIteration:  # pragma: no cover
            pass
    _cache[key] = code
    return code
