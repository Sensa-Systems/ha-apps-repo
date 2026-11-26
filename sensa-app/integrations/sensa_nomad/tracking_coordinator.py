"""Tracking: sample the vehicle's position, buffer it, upload it to Radar.

Tracking is the on-box position-history primitive; it uploads the record to Radar
(the store), and a separate mySensa app presents it back. Radar is the upload target
here; future consumers read the same record.


Follows `radar_coordinator.py`, which has already solved most of these problems in
production, with one real departure: **sampling and uploading are decoupled**.
Sampling is local and costs nothing; uploading is a round trip over a connection
this vehicle loses constantly. So we sample every minute and upload every five,
and everything in between lives in a buffer that survives a reboot.

Not a `DataUpdateCoordinator`. That class exists to keep entities fed with the
latest value, and polls only while something is listening. Tracking is the other
shape: it must keep recording whether or not anybody is looking at a screen, and
its output is a queue rather than a current value. So it is a plain object driven
by two timers, like the Schengen sampler.

Three rules here are load-bearing:

1. **Disabled means genuinely zero egress.** `async_sample` returns before it reads
   the tracker, and `async_upload` returns before it opens a socket. Off must mean
   off, not "sends but discards".
2. **Points are peeked, not popped, until Radar has accepted them.** Losing power
   mid-upload must not lose the journey. Radar's ingest is idempotent on
   (van, recorded_at), so a re-send is free.
3. **Consent is the switch, not a Radar round trip.** The customer turns Tracking on
   in the cab, and off means we never sampled, so there is nothing to withdraw at
   Radar's end. Radar authorises purely on the van session, so the only refusal that
   reaches here is a session fault (not a van session), which halts and discards.
   Everything else is a transient failure and is retried with back-off.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

from ._tracking import get_tracking_store
from .const import (
    SENSA_TRACKING_BACKOFF_MAX_SECONDS,
    SENSA_TRACKING_BACKOFF_START_SECONDS,
    SENSA_TRACKING_BATCH_MAX,
    SENSA_TRACKING_HEARTBEAT_SECONDS,
    SENSA_TRACKING_LIVE_HEARTBEAT_SECONDS,
    SENSA_TRACKING_LIVE_MIN_MOVE_M,
    SENSA_TRACKING_MIN_MOVE_M,
)
from .radar_client import RadarClient, RadarRefused
from .radar_url import async_resolve_radar_target, async_resolve_radar_token

_LOGGER = logging.getLogger(__name__)

# Radar codes that mean "this session may not send this data at all". Anything here
# clears the buffer as well as stopping. Consent and Premium no longer appear: consent
# is the local switch (off means we never sampled), and the van's connection rides on
# the Nomad licence, so Radar does not refuse on tier. What is left is a session that is
# not a van session, which is a configuration fault a retry cannot fix.
_NOT_PERMITTED = frozenset({"not_a_van_session"})


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


class TrackingCoordinator:
    """Samples the vehicle position and ships it to Radar."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        # Wall-clock seconds; 0 means "no back-off in effect".
        self._retry_after_ts: float = 0.0
        self._backoff_seconds: int = 0
        # Set when Radar has told us to stop. Cleared when the switch is toggled,
        # since that is the customer telling us something has changed.
        self._halted_reason: str | None = None
        # Somebody has an open Location Sharing link on this van, per Radar's last
        # ingest reply. Tightens the sampling thresholds so "live" means live.
        #
        # Deliberately NOT persisted: it is a fact about right now, and Radar
        # re-asserts it on the very next upload. A stale `True` restored from disk
        # would sample a parked van every 50 m for ever if the share had since
        # ended, which is the exact opposite of what the flag is for.
        self._live_share_active: bool = False

    # -- state ------------------------------------------------------------

    @property
    def halted_reason(self) -> str | None:
        return self._halted_reason

    def resume(self) -> None:
        """Clear a halt and any back-off.

        Called when the switch is turned on: the customer has presumably just
        fixed whatever Radar was complaining about, and making them wait out a
        60-minute back-off to find out would be poor.
        """
        self._halted_reason = None
        self._retry_after_ts = 0.0
        self._backoff_seconds = 0

    async def async_report_state(self, enabled: bool) -> None:
        """Tell Radar the switch flipped, so the mySensa on/off indicator is live.

        Runs INDEPENDENTLY of the egress gate: it must fire even when disabled,
        because reporting "off" is the whole point (an upload already implies "on").
        It carries no location, so it does not breach the zero-egress rule. Skips
        quietly when Radar or the token is not available this cycle; a stale
        indicator is a fine failure mode.
        """
        base, insecure_tls = await async_resolve_radar_target(self.hass)
        if not base:
            return
        token = await async_resolve_radar_token(self.hass)
        if not token:
            return
        client = RadarClient(
            self.hass, base, token=token, insecure_tls=insecure_tls
        )
        try:
            await client.async_report_state(enabled)
        except Exception:
            _LOGGER.debug("Tracking: state report failed", exc_info=True)

    async def async_report_state_heartbeat(self) -> None:
        """Re-assert the switch state to Radar, whatever it is.

        The flip report is an event, and an event can be lost: the customer turns
        Tracking on in a lay-by with no signal, the one POST fails, and mySensa
        shows OFF on a vehicle that is tracking. Uploading re-asserts "on", but
        only while the van is MOVING, so a van parked with the switch on had
        nothing to correct the record.

        This is the correction: the current state, sent again on a slow timer, so
        the indicator converges within the hour however it got out of step. Reads
        the store rather than taking a caller's word for it, since the whole point
        is to report what is actually true right now.

        Same egress rules as the flip report it repeats: no location, and it runs
        while Tracking is OFF because reporting off is exactly what keeps the
        indicator honest.
        """
        store = get_tracking_store(self.hass)
        if store is None:
            return
        await self.async_report_state(bool(store.enabled))

    def _vehicle_position(self) -> tuple[float, float] | None:
        """Where the van is, from a GPS that is part of the van.

        The customer agreed to share the VAN's position, so by default this is
        the vehicle GPS (or the simulator on a rig) and nothing else, whatever
        the GPS-source setting says: on Auto with no vehicle GPS, the navigation
        answer is a person's phone. A van with no GPS records nothing.

        Only with "Use a phone when the van has no GPS" on does it take the
        navigation position instead (`resolve_nav_position`: the customer's
        GPS-source pick, else Auto, which falls back to the driver's phone).
        """
        from .device_tracker import resolve_nav_position, resolve_vehicle_fix

        store = get_tracking_store(self.hass)
        if store is not None and store.phone_fallback:
            return resolve_nav_position(self.hass)
        return resolve_vehicle_fix(self.hass)

    # -- sampling ---------------------------------------------------------

    async def async_sample(self) -> None:
        """Take one fix, if the van has moved far enough or the heartbeat is due.

        Returns before touching the tracker when Tracking is off. That ordering is
        the point: a customer who has switched this off should be able to satisfy
        themselves that nothing is even being read, let alone sent.
        """
        store = get_tracking_store(self.hass)
        if store is None or not store.enabled:
            return
        if self._halted_reason is not None:
            # Radar has told us to stop. Keep not sampling until the customer
            # intervenes, rather than growing a buffer we may never be allowed
            # to send.
            return

        pos = self._vehicle_position()
        if pos is None:
            return
        lat, lon = pos
        now = time.time()

        if not self._should_keep(store, lat, lon, now):
            return

        store.note_sample(lat, lon, now)
        store.append_point(
            {
                "recorded_at": datetime.fromtimestamp(now, UTC).isoformat(),
                "lat": lat,
                "lon": lon,
            }
        )

    def _should_keep(self, store: Any, lat: float, lon: float, now: float) -> bool:
        """Movement threshold, with a stationary heartbeat on top.

        A parked van records nothing, which is the single biggest lever on both
        data volume and privacy. The heartbeat exists so a journey can still be
        shown to have ENDED where it ended: without it, the last recorded fix
        would be wherever the van was 100 m before it stopped.

        Both thresholds TIGHTEN while somebody has an open Location Sharing link,
        because at 100 m the pin still lags the van through a town and "where are
        they now" stops being answerable. It relaxes again on the first upload after
        the share ends.
        """
        min_move = (
            SENSA_TRACKING_LIVE_MIN_MOVE_M
            if self._live_share_active
            else SENSA_TRACKING_MIN_MOVE_M
        )
        heartbeat = (
            SENSA_TRACKING_LIVE_HEARTBEAT_SECONDS
            if self._live_share_active
            else SENSA_TRACKING_HEARTBEAT_SECONDS
        )
        if store.last_lat is None or store.last_lon is None:
            return True
        moved = _haversine_m(lat, lon, store.last_lat, store.last_lon)
        if moved >= min_move:
            return True
        last_ts = store.last_sample_ts
        if last_ts is None:
            return True
        return (now - last_ts) >= heartbeat

    # -- uploading --------------------------------------------------------

    async def async_upload(self) -> None:
        """Ship one batch, if there is anything to ship and we are allowed to."""
        store = get_tracking_store(self.hass)
        if store is None or not store.enabled:
            return
        if self._halted_reason is not None:
            return
        if store.buffered_count() == 0:
            return
        if self._retry_after_ts and time.time() < self._retry_after_ts:
            # Honouring our own back-off, or Radar's next_upload_after_s.
            return

        base, insecure_tls = await async_resolve_radar_target(self.hass)
        if not base:
            # Radar is not configured in this environment. Not an error, and not
            # something back-off should punish: keep buffering.
            return
        token = await async_resolve_radar_token(self.hass)
        if not token:
            # No van session, so the upload would 401. NOT a tier question: Radar's
            # /auth/nomad deliberately issues a session to a member holding no
            # Premium seat, so Tracking works on the free tier. An empty token means
            # no licence yet, a licence Hub does not recognise, or Radar unreachable,
            # all of which are transient or fixable. Buffer and wait.
            _LOGGER.debug("Tracking: no Radar token this cycle; keeping buffer")
            return

        batch = store.peek_batch(SENSA_TRACKING_BATCH_MAX)
        if not batch:
            return

        client = RadarClient(
            self.hass, base, token=token, insecure_tls=insecure_tls
        )
        try:
            result = await client.async_upload_points(batch)
        except RadarRefused as err:
            self._handle_refusal(store, err)
            return
        except (TimeoutError, ConnectionError) as err:
            self._note_transient_failure(err)
            return

        # Accepted. Only NOW do the points leave the buffer.
        store.drop_first(len(batch))
        self._backoff_seconds = 0
        self._retry_after_ts = 0.0

        # Radar is the authority on whether anyone is watching, so this is read
        # rather than inferred. An older Radar omits the field, which reads as
        # False: the correct degradation, since the faster sampling would then be
        # serving a share that build cannot create anyway.
        self._live_share_active = bool(result.get("live_share_active"))

        next_after = result.get("next_upload_after_s")
        if isinstance(next_after, (int, float)) and next_after > 0:
            # Radar decides the cadence, so it can be changed server-side without
            # shipping new firmware to a fleet parked in fields.
            self._retry_after_ts = time.time() + float(next_after)

        _LOGGER.debug(
            "Tracking: uploaded %d positions (accepted=%s duplicates=%s), %d left",
            len(batch),
            result.get("accepted"),
            result.get("duplicates"),
            store.buffered_count(),
        )
        # No recursion to drain a backlog here. `next_upload_after_s` is a required
        # field on Radar's ingest reply, so `_retry_after_ts` is always set by the
        # time we reach this point and any "keep going while nothing is holding us
        # back" guard is dead code. A backlog instead clears at Radar's cadence,
        # which is the whole point of letting Radar set one: a full buffer is 10
        # batches, so a weekend out of signal drains in ten minutes.

    def _handle_refusal(self, store: Any, err: RadarRefused) -> None:
        """Radar said no. WHY decides what happens to the buffer."""
        self._halted_reason = err.code
        if err.code in _NOT_PERMITTED:
            # We are no longer permitted to hold this. Do not sit on a queue of
            # location data the customer has withdrawn consent for.
            _LOGGER.warning(
                "Tracking: Radar refused upload (%s); stopping and discarding buffer",
                err.code,
            )
            store.clear_buffer()
            # Turn ourselves off too, so the switch reflects reality rather than
            # claiming to be recording into a void.
            store.set_enabled(False)
        else:
            # Any other code: a pause, not a revocation.
            _LOGGER.info(
                "Tracking: Radar refused upload (%s); pausing, keeping %d positions",
                err.code,
                store.buffered_count(),
            )

    def _note_transient_failure(self, err: Exception) -> None:
        """Back off 5, 10, 20, 40 minutes, capped at 60."""
        if self._backoff_seconds <= 0:
            self._backoff_seconds = SENSA_TRACKING_BACKOFF_START_SECONDS
        else:
            self._backoff_seconds = min(
                self._backoff_seconds * 2, SENSA_TRACKING_BACKOFF_MAX_SECONDS
            )
        self._retry_after_ts = time.time() + self._backoff_seconds
        _LOGGER.debug(
            "Tracking: upload failed (%s); retrying in %ds",
            err,
            self._backoff_seconds,
        )
