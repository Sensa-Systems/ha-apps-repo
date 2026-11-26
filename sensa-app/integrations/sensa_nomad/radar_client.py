"""Async client for the Sensa Radar REST API.

Stage 1 of the Radar integration. Wraps the endpoints Nomad needs to answer
"what is the nearest X to the vehicle" questions, so a proximity coordinator can
turn them into sensors and, later, Routine alarms.

Style mirrors ``api/client.py``: a fresh aiohttp session per request pinned to
IPv4 (avoids the IPv6-only-DNS connect failures seen on some networks), a total
timeout, and connection failures surfaced as ``ConnectionError`` / ``TimeoutError``
so callers and HA's DataUpdateCoordinator own the user-facing logging.

Auth is anonymous for now: ``/api/v1/search/nearby`` works without a token
(results are UK-filtered for anonymous callers, which is fine for the UK-based
fleet). A per-licence token arrives in a later stage and would attach here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

import aiohttp

from . import egress
from .const import RADAR_API_PREFIX

_LOGGER = logging.getLogger(__name__)


class RadarRefused(Exception):
    """Radar answered a Tracking upload with a 4xx that RETRYING CANNOT FIX.

    Deliberately distinct from ``ConnectionError``, which the caller treats as
    "try again later". It carries Radar's own error code. Since consent moved to
    the local "Tracking" toggle and the van's connection rides on the Nomad
    licence, Radar no longer refuses on Premium or consent; the only code that
    reaches here in practice is:

    - ``not_a_van_session`` -- the bearer is not a van session (a configuration
      fault, not something the customer did). The coordinator halts on it.

    The coordinator decides what to do with the buffer; the client only reports
    the code.
    """

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"Radar refused: HTTP {status} {code}")
        self.status = status
        self.code = code


class RadarPlace:
    """A single nearest-place result, normalised from a GeoJSON feature."""

    __slots__ = ("category", "distance_m", "lat", "lon", "name", "price_unit", "prices", "source")

    def __init__(
        self,
        *,
        category: str,
        name: str,
        lat: float,
        lon: float,
        distance_m: float,
        source: str | None,
        prices: dict[str, float] | None = None,
        price_unit: str | None = None,
    ) -> None:
        self.category = category
        self.name = name
        self.lat = lat
        self.lon = lon
        self.distance_m = distance_m
        self.source = source
        # A fuel station's prices, per litre in the currency's MINOR unit
        # (171.9 = 171.9p), and the currency: "p" (pence; also when absent)
        # or "c" (euro cents). Radar's rule: the unit always travels with
        # the prices, or euro cents read as pence.
        self.prices = prices
        self.price_unit = price_unit

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "distance_m": self.distance_m,
            "source": self.source,
            "prices": self.prices,
            "price_unit": self.price_unit,
        }


class RadarClient:
    """Async client for the Sensa Radar API."""

    def __init__(
        self,
        hass,
        base_url: str,
        token: str | None = None,
        *,
        insecure_tls: bool = False,
    ) -> None:
        self.hass = hass
        self.base_url = base_url.rstrip("/")
        # Premium bearer (Stage 4). None => anonymous / free tier.
        self._token = token
        # Dev only, and always the App's answer rather than ours (see
        # `radar_url.async_resolve_radar_target`). Defaults False so any caller
        # that has not been told keeps verifying.
        self._insecure_tls = insecure_tls

    def _connector(self) -> aiohttp.TCPConnector:
        """The connector every request uses. ONE place, so the two settings that
        have to be on every Radar connection cannot drift apart per method.

        IPv4-only: some networks return AAAA records the host cannot route to,
        which shows up as "All connection attempts failed". Matches api/client.

        `ssl=False` skips certificate verification, for the dev Radar's
        self-signed certificate. It has no effect on an http origin.
        """
        if self._insecure_tls:
            return aiohttp.TCPConnector(family=socket.AF_INET, ssl=False)
        return aiohttp.TCPConnector(family=socket.AF_INET)

    async def async_nearby(
        self,
        *,
        lat: float,
        lon: float,
        category: str,
        radius_m: int,
        limit: int,
        timeout: int = 12,
    ) -> list[RadarPlace]:
        """Up to ``limit`` places of ``category``, nearest first. Raises like
        `async_nearest`. More than one when a later one matters: the nearest
        fuel station often has no prices (an OpenStreetMap record with no
        priced twin), so the nearest PRICED one can be further down."""
        params = {
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "radius_m": str(int(radius_m)),
            "categories": category,
            "per_category_limit": str(int(limit)),
            "limit": str(int(limit)),
        }
        data = await self._async_get_json(
            f"{RADAR_API_PREFIX}/search/nearby", params=params, timeout=timeout
        )
        places = [
            _feature_to_place(f, fallback_category=category)
            for f in (data or {}).get("features") or []
        ]
        return [p for p in places if p is not None]

    async def async_nearest(
        self,
        *,
        lat: float,
        lon: float,
        category: str,
        radius_m: int,
        timeout: int = 12,
    ) -> RadarPlace | None:
        """Return the single nearest place of ``category`` to ``lat``/``lon``.

        Queries ``/api/v1/search/nearby`` for just this category and takes the
        first feature (the API returns results sorted nearest-first). Returns
        ``None`` when nothing is in range. Raises ``TimeoutError`` /
        ``ConnectionError`` on transport failure so the coordinator can decide
        to carry forward the previous value.
        """
        params = {
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "radius_m": str(int(radius_m)),
            "categories": category,
            # One result is enough for "nearest"; keep the response tiny.
            "per_category_limit": "1",
            "limit": "1",
        }
        data = await self._async_get_json(
            f"{RADAR_API_PREFIX}/search/nearby", params=params, timeout=timeout
        )
        features = (data or {}).get("features") or []
        if not features:
            return None
        return _feature_to_place(features[0], fallback_category=category)

    async def _async_get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        timeout: int = 12,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        connector = self._connector()
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=client_timeout,
            ) as session:
                headers = (
                    {"Authorization": f"Bearer {self._token}"}
                    if self._token
                    else None
                )
                async with session.get(
                    url, params=params, headers=headers
                ) as response:
                    raw = await response.read()
                    # Only the nearby search comes through here.
                    egress.record("nearby", 0, len(raw))
                    response.raise_for_status()
        except asyncio.TimeoutError as err:
            _LOGGER.debug("Radar GET timed out at %s", url)
            raise TimeoutError(f"Radar GET timed out at {url}") from err
        except aiohttp.ClientResponseError as err:
            _LOGGER.debug("Radar GET HTTP %s at %s", err.status, url)
            raise ConnectionError(
                f"Radar GET HTTP error {err.status} at {url}"
            ) from err
        except aiohttp.ClientError as err:
            _LOGGER.debug("Radar GET connection error at %s: %s", url, err)
            raise ConnectionError(f"Radar GET failed at {url}: {err}") from err
        try:
            return json.loads(raw) if raw else {}
        except ValueError as err:
            raise ConnectionError(f"Radar GET returned non-JSON at {url}") from err

    async def async_upload_points(
        self,
        points: list[dict[str, Any]],
        *,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Upload a batch of Tracking positions to Radar.

        Returns Radar's response (``accepted``, ``duplicates``, ``rejected``,
        ``trip_id``, ``next_upload_after_s``).

        Unlike the GET path this does NOT collapse every HTTP error into
        ``ConnectionError``. A 4xx here is Radar telling us something a retry
        cannot fix -- the bearer is not a van session -- so it surfaces as
        ``RadarRefused`` carrying the code. Consent and Premium are no longer
        checked by Radar: consent is the local "Tracking" toggle, and the van's
        connection rides on the Nomad licence. 5xx and transport failures stay
        ``ConnectionError``, which means "try again later".

        A longer default timeout than the GETs: this can be up to 500 points over
        a phone hotspot in a lay-by, and giving up early would leave the buffer
        growing for no reason.
        """
        url = f"{self.base_url}{RADAR_API_PREFIX}/tracking/points"
        connector = self._connector()
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=client_timeout,
            ) as session:
                headers = (
                    {"Authorization": f"Bearer {self._token}"}
                    if self._token
                    else None
                )
                async with session.post(
                    url, json={"points": points}, headers=headers
                ) as response:
                    body = await response.text()
                    egress.record(
                        "tracking",
                        len(json.dumps({"points": points})),
                        len(body.encode("utf-8")),
                    )
                    if response.status >= 500:
                        raise ConnectionError(
                            f"Radar upload HTTP {response.status} at {url}"
                        )
                    parsed: dict[str, Any]
                    try:
                        parsed = json.loads(body) if body else {}
                    except (ValueError, TypeError):
                        parsed = {}
                    if response.status >= 400:
                        detail = parsed.get("detail")
                        code = detail if isinstance(detail, str) else "unknown"
                        raise RadarRefused(response.status, code)
                    return parsed
        except asyncio.TimeoutError as err:
            _LOGGER.debug("Radar upload timed out at %s", url)
            raise TimeoutError(f"Radar upload timed out at {url}") from err
        except aiohttp.ClientError as err:
            _LOGGER.debug("Radar upload connection error at %s: %s", url, err)
            raise ConnectionError(f"Radar upload failed at {url}: {err}") from err

    async def async_report_state(
        self, switch_on: bool, *, timeout: int = 10
    ) -> None:
        """Tell Radar whether the Tracking switch is on or off.

        A control-plane ping carrying NO location, so it does not breach the
        zero-egress-when-off rule: it is exactly what lets the owner see a read-only
        on/off indicator in the mySensa app while the switch is OFF (and therefore
        not uploading). Best-effort and non-raising: a failed report just leaves the
        indicator stale, it must never disrupt the switch or nag the customer.
        """
        url = f"{self.base_url}{RADAR_API_PREFIX}/tracking/state"
        connector = self._connector()
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(
                connector=connector, timeout=client_timeout
            ) as session:
                headers = (
                    {"Authorization": f"Bearer {self._token}"}
                    if self._token
                    else None
                )
                async with session.post(
                    url, json={"switch_on": bool(switch_on)}, headers=headers
                ) as response:
                    raw = await response.read()
                    egress.record("tracking", 20, len(raw))
                    if response.status >= 400:
                        _LOGGER.debug(
                            "Radar state report HTTP %s at %s", response.status, url
                        )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Radar state report failed at %s: %s", url, err)


def _feature_to_place(
    feature: dict[str, Any], *, fallback_category: str
) -> RadarPlace | None:
    """Normalise a Radar GeoJSON place feature into a RadarPlace.

    Radar features are ``{geometry: {coordinates: [lon, lat]}, properties:
    {name, category, distance_m, source, ...}}``. Returns ``None`` if the shape
    is unexpected rather than raising, so one odd row can't blank the sensor.
    """
    try:
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        lon = float(coords[0])
        lat = float(coords[1])
        props = feature.get("properties") or {}
        distance_m = float(props.get("distance_m"))
    except (TypeError, ValueError, IndexError):
        return None
    props = feature.get("properties") or {}
    raw_prices = props.get("prices")
    prices: dict[str, float] | None = None
    if isinstance(raw_prices, dict):
        prices = {}
        for fuel, value in raw_prices.items():
            try:
                prices[str(fuel)] = float(value)
            except (TypeError, ValueError):
                continue
        prices = prices or None
    return RadarPlace(
        category=str(props.get("category") or fallback_category),
        name=str(props.get("name") or "Unnamed"),
        lat=lat,
        lon=lon,
        distance_m=distance_m,
        source=(str(props["source"]) if props.get("source") else None),
        prices=prices,
        price_unit=(str(props["price_unit"]) if props.get("price_unit") else None),
    )
