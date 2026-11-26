"""Device-tracker platform for Sensa Nomad.

Exposes `device_tracker.sensa_nomad_vehicle` — the VEHICLE's position from the
first-available vendor GPS source (today: Teltonika router GPS, then a USB GPS
puck; Starlink GPS was removed when SpaceX sunset the local-gRPC API). Surfaces lat/lon as the standard
`latitude`/`longitude` attributes so HA's built-in `map` card can render it.

This is the shared, vehicle-fixed position. The Cab → Navigation "Location
device" picker (per-screen, client-side) is a SEPARATE concern handled in the
frontend (`sensa-map-frame` + the tab-strip dropdown): the HA Map always shows
THIS tracker, while the Google/Radar maps can default to the viewing phone. So
this tracker stays deliberately simple — just the vehicle's vendor GPS.

Why a tracker, not a sensor: HA's `map` card requires `latitude` AND `longitude`
*attributes* on a single entity. Teltonika exposes GPS as TWO scalar sensors
(`*_gps_latitude` / `*_gps_longitude`), so this tracker stitches them together.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.device_tracker import (
    SourceType,
    TrackerEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CAB_LOCATION_AUTO,
    CAB_LOCATION_SELECT_ENTITY_ID,
    CAB_VEHICLE_TRACKER_ENTITY_ID,
    DOMAIN,
    NAV_LAST_FIX_KEY,
    NAV_MAX_PLAUSIBLE_SPEED_MPS,
    NAV_PLAUSIBILITY_FLOOR_M,
    TRACKER_SPEED_MPH_ATTR,
)

# The one great-circle helper, borrowed rather than copied: `alerts.py` and this
# module both used to grow their own, and a third would have been the third.
from .radar_coordinator import _haversine_m

_LOGGER = logging.getLogger(__name__)


def _tracker_latlon(hass: HomeAssistant, entity_id: str) -> tuple[float, float] | None:
    """(lat, lon) from any device_tracker's attributes, or None with no fix."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        lat = float(state.attributes.get("latitude"))
        lon = float(state.attributes.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _plausible_for_van(
    hass: HomeAssistant, pos: tuple[float, float]
) -> bool:
    """Could the van have got from its last known position to `pos`?

    Auto's last resort ranks phones on `last_updated` alone, and in a van with
    no vehicle GPS that rule DECIDES. It picks wrong in the obvious household
    case: a partner's phone at home that just reported beats the driver's phone
    that reported thirty seconds earlier, and the box navigates from the wrong
    county.

    So reject a candidate that would need an impossible speed to reach. Physics
    rather than a fixed radius, because it needs no tuning and self-heals: the
    allowance grows with elapsed time, so a van trailered overnight, or simply
    switched off for a week, is accepted again on its own.

    Fails OPEN with no prior fix (the first ever answer has nothing to be
    implausible against).
    """
    last = (hass.data.get(DOMAIN) or {}).get(NAV_LAST_FIX_KEY)
    if not last:
        return True
    try:
        last_lat, last_lon, last_ts = last
        elapsed = max(0.0, (dt_util.utcnow() - last_ts).total_seconds())
    except (TypeError, ValueError):
        return True
    allowed_m = NAV_PLAUSIBILITY_FLOOR_M + elapsed * NAV_MAX_PLAUSIBLE_SPEED_MPS
    return _haversine_m(pos[0], pos[1], last_lat, last_lon) <= allowed_m


def _remember_fix(hass: HomeAssistant, pos: tuple[float, float]) -> None:
    """Record the accepted position, so the next call has something to judge
    against. Only ever called when a position is actually returned."""
    store = hass.data.get(DOMAIN)
    if store is None:
        return
    store[NAV_LAST_FIX_KEY] = (pos[0], pos[1], dt_util.utcnow())


# The van simulator's GPS, matched on the MQTT unique_id the simulator
# publishes (`sensa_sim_gps_position`, see Sensa.Control.Simulator
# `devices/gps_tracker_mqtt.py` NODE_ID). Matched on the REGISTRY unique_id, not
# on the entity_id or the friendly name: a customer can rename a device, and
# "van_simulator_gps_sim" is only the id HA happened to slugify first.
_SIMULATOR_GPS_UNIQUE_ID = "sensa_sim_gps"


def _simulator_tracker(hass: HomeAssistant, source_map: dict[str, str]) -> str:
    """The van simulator's tracker entity_id, or "" when none is publishing.

    Scoped to the trackers the select already offers, so the simulator is only
    ever picked from devices the customer could have picked by hand.
    """
    try:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
    except Exception:  # pragma: no cover - registry unavailable in some tests  # noqa: BLE001
        return ""
    for eid in source_map.values():
        if not eid:
            continue
        entry = registry.async_get(eid)
        unique_id = getattr(entry, "unique_id", "") or ""
        if _SIMULATOR_GPS_UNIQUE_ID in str(unique_id):
            return eid
    return ""


def _driver_tracker_entities(hass: HomeAssistant) -> set[str]:
    """The device_trackers belonging to the currently picked driver.

    Empty when nobody is picked or the People sensor has not primed, which is
    the right answer rather than a guess: Auto then falls through to the
    freshest phone, exactly as it did before there was a driver at all.
    """
    try:
        from .people import current_driver_person_id
        person_id = current_driver_person_id(hass)
    except Exception:  # pragma: no cover - people module optional in tests  # noqa: BLE001
        return set()
    if not person_id:
        return set()
    state = hass.states.get(person_id)
    attrs = getattr(state, "attributes", None) or {}
    out: set[str] = set()
    source = attrs.get("source")
    if isinstance(source, str) and source.startswith("device_tracker."):
        out.add(source)
    for eid in attrs.get("device_trackers") or []:
        if isinstance(eid, str) and eid.startswith("device_tracker."):
            out.add(eid)
    return out


def resolve_nav_source(
    hass: HomeAssistant,
    *,
    record: bool = True,
) -> tuple[str, tuple[float, float]] | None:
    """`(entity_id, (lat, lon))` for the box's navigation position, or None.

    `record=False` resolves WITHOUT updating the plausibility gate's reference
    position. The select's `extra_state_attributes` needs that: a property
    getter must not mutate state, and it is read on the select's own poll
    cadence, so recording there would let the reference advance on rendering
    rather than on a consumer actually using the answer.

    THE single server-side answer to "which GPS is the van using", honouring
    ``select.sensa_nomad_cab_location_source``. The Radar nearest-amenity
    sensors and Schengen read it through :func:`resolve_nav_position` (Tracking
    only with its phone opt-in; by default it reads :func:`resolve_vehicle_fix`), and the Cab dropdown's `(System Default)`
    reads the entity_id back off the select's `resolved_entity` attribute — so
    the map and the sensors cannot disagree about who is driving the box.

    That mattered: the frontend used to carry its OWN copy of this precedence
    with an extra "viewing phone" step, so the map and the sensors could differ
    with nobody having overridden anything.

      * an explicit choice (Vehicle GPS / a named device) resolves to that
        device; if it has no fix we fall through to Auto rather than return
        nothing.
      * **Auto**, in order (2026-08-01):

          1. the **van simulator's** GPS, when one is publishing. A real van
             never has one, so this costs a customer nothing; a demo rig or a
             dev box always means the sim to BE the van, and having to pick it
             by hand every time was the only thing standing in the way.
          2. the **vehicle's own GPS**, when it has a fix.
          3. the **driver's phone** -- whoever is lit on the Cab people bar.
             With no tracker bolted to the van, the person driving is the van.
          4. else the phone that reported most recently.

        Steps 3 and 4 are both plausibility-gated; steps 1 and 2 are not,
        because those devices ARE the van (if the tracker says home, the van is
        home). The gate is what stops a partner's phone sitting at home from
        winning step 4 just by reporting recently, and it protects step 3 too:
        a driver who left their phone in the house should not drag the van's
        position back there.

        Note this means picking a driver CAN change the resolved position, but
        only in a van with no vehicle GPS of its own. It still never writes the
        GPS setting, which remains the customer's alone -- see
        `test_a_pinned_vehicle_gps_is_not_moved_by_the_driver`.

    Devices come from the select's ``source_map`` attribute (label -> tracker),
    which the select already maintains, so nothing here re-enumerates them.
    """
    sel = hass.states.get(CAB_LOCATION_SELECT_ENTITY_ID)
    attrs = getattr(sel, "attributes", None) or {}
    source_map: dict[str, str] = attrs.get("source_map") or {}
    if not isinstance(source_map, dict):
        source_map = {}
    choice = getattr(sel, "state", None) or CAB_LOCATION_AUTO

    # Explicit device choice: try it first, and NEVER plausibility-gate it. The
    # customer named this device; second-guessing their pick is how you get a
    # map that cannot be explained.
    if choice != CAB_LOCATION_AUTO:
        eid = source_map.get(choice)
        pos = _tracker_latlon(hass, eid) if eid else None
        if pos is not None:
            if record:
                _remember_fix(hass, pos)
            return (eid, pos)
        # Chosen device has no fix right now: fall through to Auto so the box
        # still navigates rather than going blind on a stale selection.

    # Auto 1 + 2: a GPS that is part of the van (the simulator on a rig, else
    # the vehicle GPS). See `_van_fix`.
    sim_eid = _simulator_tracker(hass, source_map)
    van = _van_fix(hass, source_map)
    if van is not None:
        if record:
            _remember_fix(hass, van[1])
        return van

    # Auto 3 + 4: the phones. Every source_map entry that isn't Auto's blank,
    # the vehicle tracker or the simulator (both already tried above).
    phone_eids = [
        eid
        for eid in source_map.values()
        if eid and eid not in (CAB_VEHICLE_TRACKER_ENTITY_ID, sim_eid)
    ]

    # Auto 3: the driver's phone. With nothing bolted to the van, the person
    # driving IS the van's position. Plausibility-gated like any other phone --
    # a driver who left their phone at home must not drag the van back there.
    #
    # A person can own several trackers (HA's `person` carries `source` plus a
    # `device_trackers` list), so this picks the FRESHEST of theirs rather than
    # whichever happened to come first in the source map. Same rule as step 4
    # below, applied to a smaller field: an arbitrary answer where a defined one
    # exists is a bug waiting to be reported as "it keeps switching phones".
    driver_eids = _driver_tracker_entities(hass)
    driver_best: tuple[str, tuple[float, float]] | None = None
    driver_ts = None
    for eid in phone_eids:
        if eid not in driver_eids:
            continue
        pos = _tracker_latlon(hass, eid)
        if pos is None or not _plausible_for_van(hass, pos):
            continue
        state = hass.states.get(eid)
        ts = state.last_updated if state else None
        if driver_best is None or (ts is not None and (driver_ts is None or ts > driver_ts)):
            driver_best = (eid, pos)
            driver_ts = ts
    if driver_best is not None:
        if record:
            _remember_fix(hass, driver_best[1])
        return driver_best

    freshest: tuple[str, tuple[float, float]] | None = None
    freshest_ts = None
    # Tracked separately so an all-implausible field can still answer: refusing
    # to return anything would leave the box blind, and refusing to RECORD would
    # wedge the gate on a stale position for ever.
    fallback: tuple[str, tuple[float, float]] | None = None
    fallback_ts = None
    for eid in phone_eids:
        pos = _tracker_latlon(hass, eid)
        if pos is None:
            continue
        state = hass.states.get(eid)
        ts = state.last_updated if state else None
        if fallback is None or (ts is not None and (fallback_ts is None or ts > fallback_ts)):
            fallback = (eid, pos)
            fallback_ts = ts
        if not _plausible_for_van(hass, pos):
            continue
        if freshest is None or (ts is not None and (freshest_ts is None or ts > freshest_ts)):
            freshest = (eid, pos)
            freshest_ts = ts

    chosen = freshest or fallback
    if chosen is None:
        return None
    if freshest is None and fallback is not None:
        _LOGGER.debug(
            "nav: no plausible tracker, falling back to the freshest (%s)",
            fallback[0],
        )
    if record:
        _remember_fix(hass, chosen[1])
    return chosen


def _van_fix(
    hass: HomeAssistant, source_map: dict[str, str],
) -> tuple[str, tuple[float, float]] | None:
    """Auto's first two steps: a GPS that is part of the van, never a phone.

      1. the van simulator, when one is publishing. On a rig running it, that IS
         the van; a real van has no simulator to find, so it costs nothing.
      2. the vehicle GPS, when it has a fix.

    Neither is plausibility-gated: both are bolted to the van, so if one says
    home, the van IS home. The one copy of this rule, read by Auto and by
    :func:`resolve_vehicle_fix`.
    """
    sim_eid = _simulator_tracker(hass, source_map)
    if sim_eid:
        pos = _tracker_latlon(hass, sim_eid)
        if pos is not None:
            return (sim_eid, pos)
    pos = _tracker_latlon(hass, CAB_VEHICLE_TRACKER_ENTITY_ID)
    if pos is not None:
        return (CAB_VEHICLE_TRACKER_ENTITY_ID, pos)
    return None


def resolve_vehicle_fix(hass: HomeAssistant) -> tuple[float, float] | None:
    """The van's own GPS position, whatever the GPS-source setting says, or None.

    Tracking reads this by default: the customer agreed to share where the VAN
    is, and on Auto with no vehicle GPS the navigation answer is a person's
    phone. A phone is used only when they also turn on "Use a phone when the
    van has no GPS" (`switch.sensa_nomad_tracking_phones`). Unlike
    :func:`resolve_van_position` this ignores an explicit phone pick, so a van
    that has a GPS still tracks when someone has pointed navigation at a phone.
    """
    van = _van_fix(hass, _source_map(hass))
    return van[1] if van else None


def resolve_nav_position(hass: HomeAssistant) -> tuple[float, float] | None:
    """The navigation position for THIS box. See :func:`resolve_nav_source`."""
    resolved = resolve_nav_source(hass)
    return resolved[1] if resolved else None


def _source_map(hass: HomeAssistant) -> dict[str, str]:
    sel = hass.states.get(CAB_LOCATION_SELECT_ENTITY_ID)
    source_map = (getattr(sel, "attributes", None) or {}).get("source_map") or {}
    return source_map if isinstance(source_map, dict) else {}


def resolve_van_position(hass: HomeAssistant) -> tuple[float, float] | None:
    """The resolved position, but ONLY when it came from a device bolted to the
    van (the vehicle GPS, or the simulator standing in for it).

    The geofence needs this: in Sentry the owner is away, and their phone went
    with them, so a phone answer would read as the van driving off. Filters the
    one resolver's answer rather than re-deriving the precedence. None means
    "could not ask", never "the van is where it was".
    """
    resolved = resolve_nav_source(hass, record=False)
    if resolved is None:
        return None
    eid, pos = resolved
    if eid == CAB_VEHICLE_TRACKER_ENTITY_ID:
        return pos
    sim_eid = _simulator_tracker(hass, _source_map(hass))
    return pos if sim_eid and eid == sim_eid else None


def van_gps_available(hass: HomeAssistant) -> bool:
    """Whether the van has a GPS of its own at all, fix or no fix.

    Asks whether the source EXISTS, not whether it is answering this second,
    so the geofence instinct does not flap to "unavailable" (taking its
    automation with it) every time the router loses sky.
    """
    try:
        if _simulator_tracker(hass, _source_map(hass)):
            return True
        entries = hass.config_entries.async_entries(DOMAIN)
        return any(_find_gps_pair(hass, e.entry_id) for e in entries)
    except Exception:  # noqa: BLE001 - availability must never break the card
        return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad device-tracker entities."""
    async_add_entities([SensaVehicleTracker(hass, entry)])


# Vendor GPS sources, in priority order. Each tuple is
# `(vendor, lat_key, lon_key, friendly_label)`.
#
# A vendor entity's unique_id is `{entry_id}_{vendor}_{device_id}_{key}` (minted
# by `teltonika_unique_id`), so the vendor and the key are matched at the two
# ENDS and whatever sits between them is the DEVICE. Matching the device out
# rather than ignoring it is what pairs a latitude with the longitude from the
# same router on a van carrying two Teltonikas.
#
# This used to match one substring, `_teltonika_gps_latitude`, which was the
# whole id under the pre-2026-08-07 scheme. Per-device scoping inserted the
# device id into the middle of every vendor unique_id, no id has contained that
# substring since, and the tracker silently went unavailable on every van with a
# router. Nothing caught it because the test minted its fixture ids by hand in
# the old shape; the test now calls `teltonika_unique_id` so the two cannot
# drift apart again.
#
# Starlink was removed (SpaceX sunset the local GPS API, see
# starlink_entities.py). A USB puck (`usb_gps.py`) sits behind the router, so
# fitting one to a van that already has router GPS changes nothing.
_GPS_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("teltonika", "gps_latitude", "gps_longitude", "Teltonika router GPS"),
    ("usb_gps", "gps_latitude", "gps_longitude", "USB GPS"),
)


class SensaVehicleTracker(TrackerEntity):
    """Aggregated vehicle position from the first-available vendor GPS source.

    Walks the entity registry once on add (and on a source-change re-resolve) to
    find the first matching `_gps_latitude` / `_gps_longitude` sensor pair, in
    priority order, and surfaces lat/lon for HA's `map` card. Unavailable until a
    vendor registers GPS sensors.
    """

    _attr_has_entity_name = False
    _attr_name = "Vehicle"
    _attr_icon = "mdi:map-marker-radius"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_vehicle_tracker"
        self.entity_id = "device_tracker.sensa_nomad_vehicle"
        self._lat_eid: str = ""
        self._lon_eid: str = ""
        self._speed_eid: str = ""
        self._source_label: str = ""

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        return self._read_float(self._lat_eid)

    @property
    def longitude(self) -> float | None:
        return self._read_float(self._lon_eid)

    @property
    def location_accuracy(self) -> int:
        # Vehicle GPS is rarely better than ~10 m; HA treats <50 m as "very
        # accurate". Pin to 50 so the pin doesn't render a giant halo.
        return 50

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"source": self._source_label or "none"}
        mph = self._speed_mph()
        if mph is not None:
            attrs[TRACKER_SPEED_MPH_ATTR] = mph
        return attrs

    def _speed_mph(self) -> float | None:
        """The receiver's own ground speed, in mph, or None.

        The receiver measures this directly, so it is the honest reading. A
        speed derived from two polled positions is only as good as the poll
        clock: the router reports every ~30 s, and dividing its step by any
        other interval reads as zero, then double (seen on .207, 2026-10-01).
        Read in whatever unit HA shows it in, because the sensor carries a
        SPEED device class and an imperial install converts it.
        """
        state = self.hass.states.get(self._speed_eid) if self._speed_eid else None
        if state is None:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = (state.attributes or {}).get("unit_of_measurement") or "km/h"
        factor = _SPEED_TO_MPH.get(unit)
        if factor is None or value < 0:
            return None
        return round(value * factor, 1)

    @property
    def available(self) -> bool:
        return self._lat_eid != "" and self._lon_eid != "" and (
            self._read_float(self._lat_eid) is not None
            and self._read_float(self._lon_eid) is not None
        )

    def _read_float(self, eid: str) -> float | None:
        if not eid:
            return None
        state = self.hass.states.get(eid)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def async_added_to_hass(self) -> None:
        """Resolve the GPS source on add and subscribe to its sensor state
        changes for live updates."""
        from homeassistant.helpers import entity_registry as er

        self._resolve_source()
        self._unsub_source: Any = None

        @callback
        def _on_source_change(_event: Event) -> None:
            self.async_write_ha_state()

        @callback
        def _bind_source() -> None:
            if self._unsub_source is not None:
                self._unsub_source()
                self._unsub_source = None
            eids = [e for e in (self._lat_eid, self._lon_eid, self._speed_eid) if e]
            if eids:
                self._unsub_source = async_track_state_change_event(
                    self.hass, eids, _on_source_change
                )

        # A router registered after HA started mints its GPS sensors after this
        # entity was added, so resolving once on add left the van GPS
        # unavailable until a restart. Re-resolve whenever the registry changes.
        @callback
        def _on_registry_change(_event: Event) -> None:
            before = (self._lat_eid, self._lon_eid, self._speed_eid)
            self._resolve_source()
            if (self._lat_eid, self._lon_eid, self._speed_eid) != before:
                _bind_source()
                self.async_write_ha_state()

        @callback
        def _unbind_source() -> None:
            if self._unsub_source is not None:
                self._unsub_source()

        _bind_source()
        self.async_on_remove(_unbind_source)
        self.async_on_remove(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, _on_registry_change
            )
        )

    def _resolve_source(self) -> None:
        """Find the vendor GPS pair; see `_find_gps_pair`."""
        try:
            pair = _find_gps_pair(self.hass, self._entry.entry_id)
        except Exception:  # noqa: BLE001
            return
        self._lat_eid, self._lon_eid, self._source_label = pair or ("", "", "")
        self._speed_eid = _speed_eid_beside(self.hass, self._lat_eid)


# The receiver's reported ground speed rides the latitude's device: same
# unique_id with this key in place of the latitude's. Only Teltonika today.
_LAT_KEY = "gps_latitude"
_SPEED_KEY = "gps_speed"
_SPEED_TO_MPH = {"km/h": 0.621371, "mph": 1.0, "m/s": 2.2369363, "kn": 1.150779}


def _speed_eid_beside(hass: HomeAssistant, lat_eid: str) -> str:
    """The speed sensor on the same device as `lat_eid`, or "" without one."""
    if not lat_eid:
        return ""
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    ent = ent_reg.async_get(lat_eid)
    uid = getattr(ent, "unique_id", "") or ""
    if not uid.endswith(f"_{_LAT_KEY}"):
        return ""
    speed_uid = uid[: -len(_LAT_KEY)] + _SPEED_KEY
    return ent_reg.async_get_entity_id("sensor", ent.platform, speed_uid) or ""


def _find_gps_pair(
    hass: HomeAssistant, entry_id: str
) -> tuple[str, str, str] | None:
    """`(lat_eid, lon_eid, label)`: walk the entity registry once, in priority
    order, to find the first pair of vendor GPS sensors that both exist ON THE
    SAME DEVICE. Shared by the tracker and `van_gps_available`, so "the van has
    a GPS" and "the tracker has a source" cannot disagree."""
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    for vendor, lat_key, lon_key, label in _GPS_SOURCES:
        head = f"{entry_id}_{vendor}_"
        lat_tail = f"_{lat_key}"
        lon_tail = f"_{lon_key}"
        # device id (the middle of the unique_id) -> entity_id. An id minted
        # under the old flat scheme yields an empty device id, which pairs
        # with itself perfectly well, so a box that has not yet run
        # `migrate_unique_ids` still resolves.
        lats: dict[str, str] = {}
        lons: dict[str, str] = {}
        for ent in ent_reg.entities.values():
            uid = ent.unique_id or ""
            if not uid.startswith(head):
                continue
            if uid.endswith(lat_tail):
                lats[uid[len(head):-len(lat_tail)]] = ent.entity_id
            elif uid.endswith(lon_tail):
                lons[uid[len(head):-len(lon_tail)]] = ent.entity_id

        for device, lat_eid in lats.items():
            lon_eid = lons.get(device)
            if lon_eid:
                return lat_eid, lon_eid, label
    return None
