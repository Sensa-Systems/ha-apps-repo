"""Is the vehicle moving? One answer, two sources.

`binary_sensor.sensa_nomad_vehicle_moving` is the whole public surface. The
Drive and Parked instincts read it, and anything later that wants to know
whether the van is under way reads the same entity rather than re-deriving it.
That is deliberate: "moving" expressed once in an OBD2 rule and again in a GPS
rule is the predicate-written-twice trap, and the two copies would disagree the
first time a van had one source and not the other.

Two sources, in preference order:

  * the **OBD2 adapter's road speed** (`sensor.*vehicle_speed`, km/h). Direct
    from the vehicle, so it is the truth when the engine is awake.
  * the **resolved GPS**, via `device_tracker.resolve_nav_source` -- the one
    function that answers "which GPS is this van using". Distance between two
    fixes over the time between them is a speed. Slower to react and noisier,
    but it is there when the OBD2 adapter is not: a WiCAN sleeps with the
    engine, which is exactly when the van has stopped.

Both feed the same state machine, so which source answered only ever shows up
as an attribute.

**Driveway problem.** "Speed above zero" would put a van into Drive for being
reversed off the drive. Two ways ON can be satisfied, and shuffling the van
satisfies neither:

  * road speed held above `SENSA_MOVEMENT_ON_KMH` for
    `SENSA_MOVEMENT_ON_SECONDS`, or
  * `SENSA_MOVEMENT_ON_DISTANCE_M` from the anchor -- where the van last came
    to rest. However slowly it got there, half a kilometre from the pitch is a
    journey.

OFF is one rule: under `SENSA_MOVEMENT_OFF_KMH` for
`SENSA_MOVEMENT_OFF_SECONDS`. Five minutes because a fuel stop is not an
arrival, and because the OFF edge is what hands the van back to Parked.

**Told no vs could not ask.** A sample that reads nothing (no OBD2, no
resolvable GPS) is NOT "stationary": it advances neither window and the
previous answer stands. A van whose sources have both gone dark keeps the
answer it had, which is why the Parked instinct is conditional on still being
in Drive rather than on this sensor alone. A GPS that reports the same fix, on
the other hand, HAS answered: that is a genuine zero.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .const import (
    CAB_LOCATION_SELECT_ENTITY_ID,
    CAB_VEHICLE_TRACKER_ENTITY_ID,
    SENSA_MOVEMENT_MAX_GPS_GAP_SECONDS,
    SENSA_MOVEMENT_OFF_MPH,
    SENSA_MOVEMENT_OFF_SECONDS,
    SENSA_MOVEMENT_ON_DISTANCE_M,
    SENSA_MOVEMENT_ON_MPH,
    SENSA_MOVEMENT_ON_SECONDS,
    SENSA_MOVEMENT_SAMPLE_SECONDS,
    TRACKER_SPEED_MPH_ATTR,
)
from .device_tracker import resolve_nav_source

_LOGGER = logging.getLogger(__name__)

# Which source answered the last sample. Surfaced as the sensor's `source`
# attribute so a surprising answer can be explained without reading this file.
SOURCE_OBD2 = "obd2"
SOURCE_GPS = "gps"
SOURCE_NONE = ""

# The detector works in MPH: it is what every speed dial in the product shows
# and what the customer types into the instinct's thresholds, so converting once
# at the source read beats converting at each of three comparisons.
_KMH_TO_MPH = 0.621371
_MS_TO_MPH = 2.2369363

# Two position writes closer together than this are one fix landing in halves
# (the vehicle tracker is written once for latitude and again for longitude),
# not a journey: dividing by the gap between them would invent a huge speed.
_GPS_MIN_FIX_GAP_SECONDS = 5.0
# How long a derived speed is held while the tracker has not reported a new
# fix. The router reports every ~30 s and the detector samples every 15 s, so
# without the hold every other sample read as a stop.
_GPS_SPEED_HOLD_SECONDS = 90.0

# Config keys for the customer-tunable thresholds. Named here because BOTH ends
# read them -- the instinct declares its `ConfigField`s with these keys, and the
# detector looks them up -- and a key spelled differently at the two ends would
# silently leave the customer editing a number nothing consults.
TUNE_ON_MPH = "move_speed"
TUNE_ON_SECONDS = "move_seconds"
TUNE_ON_DISTANCE_M = "move_distance"
TUNE_OFF_SECONDS = "stop_seconds"

# What the thresholds are before the customer touches them. The instinct's
# `ConfigField` defaults come from this same dict, so the card cannot show one
# default while the detector applies another.
DEFAULT_TUNING: dict[str, float] = {
    TUNE_ON_MPH: SENSA_MOVEMENT_ON_MPH,
    TUNE_ON_SECONDS: SENSA_MOVEMENT_ON_SECONDS,
    TUNE_ON_DISTANCE_M: SENSA_MOVEMENT_ON_DISTANCE_M,
    TUNE_OFF_SECONDS: SENSA_MOVEMENT_OFF_SECONDS,
}


def vehicle_speed_entity(hass: Any) -> str | None:
    """The OBD2 road-speed sensor (km/h), or None when there is no adapter.

    Resolved the same two ways as `alerts._engine_rpm_entity`, and for the same
    reason: a real WiCAN Pro derives its entity_id from the firmware PID name,
    so the substring match usually wins, but the AutoPID object_id rides the
    unique_id (`..._vehicle_speed`) when the PID is named something else. The
    Cab Engine tab's speed dial resolves it by that suffix
    (`_wican_obd2_entity_map`), so matching the same way keeps the dial and this
    sensor agreeing about whether the van has road speed at all.
    """
    from .alerts import _find_entity, _registry_entity_by_uid_suffix

    return (
        _find_entity(hass, "sensor", "vehicle", "speed")
        or _registry_entity_by_uid_suffix(hass, "sensor", "_vehicle_speed")
    )


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Same formula as
    `radar_coordinator._haversine_m`, inlined so the detector carries no
    coordinator import just to measure a gap."""
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



def _reported_mph(state: Any) -> float | None:
    """A tracker's own measured ground speed (`TRACKER_SPEED_MPH_ATTR`), or None.

    The one reading of that attribute: the movement sample and the live
    speedometer both come through here.
    """
    attrs = getattr(state, "attributes", None)
    reported = attrs.get(TRACKER_SPEED_MPH_ATTR) if isinstance(attrs, dict) else None
    if not isinstance(reported, (int, float)) or isinstance(reported, bool):
        return None
    return max(0.0, float(reported))

class MovementDetector:
    """The state machine behind `binary_sensor.sensa_nomad_vehicle_moving`.

    Ticked on a timer by `_async_start_movement` in `__init__.py`. Holds no HA
    entity of its own: the binary sensor subscribes to `SIGNAL_MOVEMENT_CHANGED`
    and reads these attributes, so the answer is computed once per sample rather
    than once per reader.
    """

    def __init__(self, hass: Any) -> None:
        self._hass = hass
        # None = never determined. The sensor renders that as `unknown` rather
        # than as `off`, so a box that has never had a speed source does not
        # claim the van is parked.
        self._moving: bool | None = None
        # Where the van last came to rest. The distance arm of the ON rule
        # measures from here, so it means "half a kilometre from the pitch",
        # not "half a kilometre since boot".
        self._anchor: tuple[float, float] | None = None
        self._last_fix: tuple[float, float] | None = None
        # The speed reference: the last fix, timed by when the TRACKER wrote it
        # (not when this detector sampled it), and which tracker it came from.
        self._ref_fix: tuple[float, float] | None = None
        self._ref_at: datetime | None = None
        self._ref_eid: str = ""
        self._derived_mph: float | None = None
        self._above_since: datetime | None = None
        self._below_since: datetime | None = None
        self._speed_mph: float | None = None
        self._gps_speed_mph: float | None = None
        self._tuning_seen: dict[str, float] = dict(DEFAULT_TUNING)
        self._source: str = SOURCE_NONE
        self._changed_at: datetime | None = None

    # -- read side ----------------------------------------------------------

    @property
    def is_moving(self) -> bool | None:
        return self._moving

    @property
    def speed_mph(self) -> float | None:
        return self._speed_mph

    @property
    def gps_speed_mph(self) -> float | None:
        """The GPS-derived speed from the last sample, whichever source the
        movement answer used. Feeds the Cab Home speedometer on a van with no
        OBD2 road speed; None when there was no usable pair of fixes."""
        return self._gps_speed_mph

    @property
    def tuning(self) -> dict[str, float]:
        """The thresholds applied on the last sample, for the sensor's
        attributes: "why did it not switch?" is a question about these."""
        return dict(self._tuning_seen)

    @property
    def source(self) -> str:
        return self._source

    @property
    def distance_from_anchor_m(self) -> float | None:
        if self._anchor is None or self._last_fix is None:
            return None
        return _haversine_m(*self._anchor, *self._last_fix)

    @property
    def changed_at(self) -> datetime | None:
        return self._changed_at

    def restore(self, moving: bool | None) -> None:
        """Adopt the answer this box had before the last restart.

        Called once from the binary sensor's `RestoreEntity` load. A van that
        rebooted mid-journey comes back moving, and one that rebooted on a pitch
        comes back parked, so neither produces a spurious edge on the first
        sample. Deliberately restores ONLY the boolean: the anchor and the
        windows are re-derived from live fixes, because a fix from before the
        restart is of unknown age.
        """
        if self._moving is None and moving is not None:
            self._moving = moving

    # -- sampling -----------------------------------------------------------

    def _tuning(self) -> dict[str, float]:
        """The customer's thresholds for this sample. See
        :func:`movement_tuning_for`."""
        return movement_tuning_for(self._hass)

    def _read_speed(self, now: datetime) -> tuple[float | None, str]:
        """`(mph, source)` for this sample, or `(None, SOURCE_NONE)` when
        neither source answered.

        Position is resolved on EVERY sample, whichever source supplies the
        speed, because the distance arm of the ON rule needs it. `record=False`:
        sampling for movement is not a consumer having used the navigation
        answer, so it must not advance the plausibility gate's reference fix --
        the same reasoning as `sensor.sensa_nomad_gps_actual`.
        """
        from .alerts import _num

        eid = ""
        pos: tuple[float, float] | None = None
        try:
            resolved = resolve_nav_source(self._hass, record=False)
            if resolved:
                eid, pos = resolved
        except Exception:  # noqa: BLE001 - a dark GPS is normal, not a fault
            pos = None

        # The adapter reports km/h. This is the ONE place that conversion
        # happens: everything downstream, the thresholds the customer types and
        # the sensor's own attribute are all mph.
        obd2 = vehicle_speed_entity(self._hass)
        obd2_kmh = _num(self._hass, obd2) if obd2 else None

        gps_mph = self._gps_speed(pos, now, eid)
        self._gps_speed_mph = gps_mph

        if obd2_kmh is not None:
            return (max(0.0, obd2_kmh) * _KMH_TO_MPH, SOURCE_OBD2)
        if gps_mph is not None:
            return (gps_mph, SOURCE_GPS)
        return (None, SOURCE_NONE)

    def _gps_speed(
        self, pos: tuple[float, float] | None, now: datetime, eid: str = ""
    ) -> float | None:
        """Speed in mph from the resolved GPS, or None when it cannot say.

        The receiver's own reported ground speed wins when the tracker carries
        one (`TRACKER_SPEED_MPH_ATTR`): it is measured, not inferred.

        Otherwise distance over time between two fixes, where the time is when
        the TRACKER wrote each fix, never when this detector sampled it. Timing
        by the sample clock is what made the Cab speedometer read 0 then double
        every other sample on a router polled every ~30 s. A sample that finds
        no new fix holds the last derived speed for `_GPS_SPEED_HOLD_SECONDS`.

        A gap longer than `SENSA_MOVEMENT_MAX_GPS_GAP_SECONDS`, or a change of
        tracker, is a hole in the record: averaging over it would invent a
        speed, so the reference is reset and this sample answers nothing.
        """
        if pos is None:
            return None

        self._last_fix = pos
        if self._anchor is None:
            self._anchor = pos

        state = self._hass.states.get(eid) if eid else None
        reported = _reported_mph(state)
        fix_at = getattr(state, "last_updated", None)
        if not isinstance(fix_at, datetime):
            fix_at = now

        derived = self._derive(pos, fix_at, now, eid)
        if reported is not None:
            return reported
        return derived

    def live_gps_speed_mph(self) -> float | None:
        """The resolved GPS's own measured speed right now, else the last sample's.

        For the Cab speedometer, which reads this every second (the router's
        GPS gives a new fix every second at the default GPS refresh). Only the
        MEASURED speed is read live: a speed derived from fixes one second
        apart is position noise (a parked van reads 1 to 2 mph), so a source
        that reports none keeps the sampled, derived answer. The movement
        answer is untouched and still samples every
        `SENSA_MOVEMENT_SAMPLE_SECONDS`.
        """
        try:
            resolved = resolve_nav_source(self._hass, record=False)
        except Exception:  # noqa: BLE001 - a dark GPS is normal, not a fault
            resolved = None
        if resolved:
            reported = _reported_mph(self._hass.states.get(resolved[0]))
            if reported is not None:
                return reported
        return self._gps_speed_mph

    def _derive(
        self, pos: tuple[float, float], fix_at: datetime, now: datetime, eid: str
    ) -> float | None:
        """Advance the speed reference with this fix; see :meth:`_gps_speed`."""
        if self._ref_fix is None or self._ref_at is None or eid != self._ref_eid:
            self._ref_fix, self._ref_at, self._ref_eid = pos, fix_at, eid
            self._derived_mph = None
            return None

        gap_s = (fix_at - self._ref_at).total_seconds()
        if gap_s <= 0:
            # No new fix since the last sample: hold, unless it has gone stale.
            age_s = (now - self._ref_at).total_seconds()
            return self._derived_mph if age_s <= _GPS_SPEED_HOLD_SECONDS else None
        if gap_s < _GPS_MIN_FIX_GAP_SECONDS:
            # The rest of the same fix: take it as the reference, speed unchanged.
            self._ref_fix, self._ref_at = pos, fix_at
            return self._derived_mph
        if gap_s > SENSA_MOVEMENT_MAX_GPS_GAP_SECONDS:
            self._ref_fix, self._ref_at = pos, fix_at
            self._derived_mph = None
            return None

        self._derived_mph = (_haversine_m(*self._ref_fix, *pos) / gap_s) * _MS_TO_MPH
        self._ref_fix, self._ref_at = pos, fix_at
        return self._derived_mph

    def async_tick(self, now: datetime | None = None) -> bool:
        """Take one sample. Returns True when the answer CHANGED.

        The caller dispatches `SIGNAL_MOVEMENT_CHANGED` on a True, so the binary
        sensor is written on transitions rather than on every sample.
        """
        now = now or datetime.now(timezone.utc)
        tuning = self._tuning()
        self._tuning_seen = tuning
        speed, source = self._read_speed(now)
        self._source = source
        self._speed_mph = speed

        if speed is None:
            # Could not ask. Neither window advances and the previous answer
            # stands -- see the module docstring. Clearing the windows here
            # would mean a flaky source could never arm either edge.
            return False

        if speed >= tuning[TUNE_ON_MPH]:
            if self._above_since is None:
                self._above_since = now
        else:
            self._above_since = None

        if speed < SENSA_MOVEMENT_OFF_MPH:
            if self._below_since is None:
                self._below_since = now
        else:
            self._below_since = None

        if self._moving:
            return self._maybe_stop(now, tuning)
        return self._maybe_start(now, tuning)

    def _maybe_start(self, now: datetime, tuning: dict[str, float]) -> bool:
        """ON when EITHER arm is satisfied: sustained road speed, or distance
        from the anchor."""
        sustained = (
            self._above_since is not None
            and (now - self._above_since).total_seconds()
            >= tuning[TUNE_ON_SECONDS]
        )
        travelled = (
            self.distance_from_anchor_m is not None
            and self.distance_from_anchor_m >= tuning[TUNE_ON_DISTANCE_M]
        )
        if not (sustained or travelled):
            return False
        self._moving = True
        self._changed_at = now
        self._below_since = None
        _LOGGER.info(
            "Movement: under way (source=%s, %.1f mph, %s)",
            self._source,
            self._speed_mph if self._speed_mph is not None else -1.0,
            "sustained speed" if sustained else "distance from anchor",
        )
        return True

    def _maybe_stop(self, now: datetime, tuning: dict[str, float]) -> bool:
        """OFF once the van has been under the threshold for the whole window.
        Re-anchors on the way out, so the next journey's distance arm measures
        from where this one ended."""
        if (
            self._below_since is None
            or (now - self._below_since).total_seconds()
            < tuning[TUNE_OFF_SECONDS]
        ):
            return False
        self._moving = False
        self._changed_at = now
        self._above_since = None
        self._anchor = self._last_fix
        _LOGGER.info("Movement: stopped (source=%s)", self._source)
        return True


def movement_source_available(hass: Any) -> bool:
    """Whether this van can answer "am I moving?" at all.

    True when there is an OBD2 road-speed sensor, OR when the navigation
    resolver currently names a device. False means the Drive/Parked instincts
    can never fire, so they say so on the card ("needs an OBD2 adapter or a GPS
    source") rather than sitting enabled and silent -- the one thing worse than
    a feature that does not work is one that looks like it does.

    NB most vans clear this on the GPS arm alone: a phone lit on the Cab people
    bar is a nameable source, so the OBD2 adapter buys accuracy and speed of
    reaction rather than the feature itself.

    Deliberately asks whether a source EXISTS, not whether one has a fix right
    now. `resolve_nav_source` answers None for a van parked overnight with every
    phone asleep, and keying on that would make the instincts flap in and out of
    "unavailable" -- taking their generated automations with them -- every time
    the van sat still. The source list is the stable fact; whether it is
    answering this second is the detector's problem, and it already holds the
    previous answer rather than guessing.
    """
    if vehicle_speed_entity(hass):
        return True
    try:
        from .device_tracker import van_gps_available

        if van_gps_available(hass):
            return True
        # The select always lists Auto and Vehicle GPS, so a non-empty map
        # proves nothing; only a tracker of its own (a phone) is a source.
        select = hass.states.get(CAB_LOCATION_SELECT_ENTITY_ID)
        source_map = (getattr(select, "attributes", None) or {}).get("source_map")
        if not isinstance(source_map, dict):
            return False
        return any(
            eid and eid != CAB_VEHICLE_TRACKER_ENTITY_ID for eid in source_map.values()
        )
    except Exception:  # noqa: BLE001 - availability must never break the card
        return False


def movement_tuning_for(hass: Any) -> dict[str, float]:
    """The customer's movement thresholds, merged over `DEFAULT_TUNING`.

    Read on EVERY sample rather than cached, so raising "counts as moving
    above" takes effect on the next tick instead of at the next restart. The
    merge itself lives in the instinct (`instincts_catalog.movement_tuning`),
    next to the `ConfigField`s that declare the same keys and defaults: this is
    the reader, not a second copy of the rule.

    Falls back to the defaults if that lookup fails at all. A detector running
    on the defaults is a working van; one that threw would be a dead sensor and
    two instincts that never fire again.
    """
    try:
        from .instincts_catalog import movement_tuning

        return movement_tuning(hass)
    except Exception:
        _LOGGER.debug("movement: tuning lookup raised", exc_info=True)
        return dict(DEFAULT_TUNING)


def get_movement_detector(hass: Any) -> MovementDetector | None:
    """The run-level singleton detector, or None before setup has run.

    One van has one answer to "am I moving?", so the detector is a run-level
    singleton alongside the alert engine and the Tracking sampler rather than a
    per-entry object. Returns None rather than creating one: a caller that made
    its own would tick a second state machine nobody dispatches from, and its
    answer would silently diverge.
    """
    from .const import DOMAIN, RUNTIME_MOVEMENT_DETECTOR

    return (hass.data.get(DOMAIN) or {}).get(RUNTIME_MOVEMENT_DETECTOR)


def sample_interval() -> timedelta:
    return timedelta(seconds=SENSA_MOVEMENT_SAMPLE_SECONDS)
