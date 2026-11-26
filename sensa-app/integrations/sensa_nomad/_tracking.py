"""Tracking state: the on/off flag and the offline point buffer.

Modelled on `_schengen.py` (JSON file + `atomic_write_text`), which is the house
pattern for state that has to survive a reboot in a field with no warning.

Two things live here and they have very different characters:

- **`enabled`** is a small durable flag. The switch, a service and any card all read
  it from here rather than from a pushed attribute, because a pushed attribute drifts
  the moment something else changes the value.
- **`buffer`** is up to ~72 hours of unsent positions. It is the reason Tracking works
  at all in a vehicle: the van spends real time out of signal, and a coordinator that
  only kept the latest fix would lose the entire drive that got it there.

The buffer is bounded and drops OLDEST first on overflow. That direction is
deliberate: if we must lose something, losing the start of a long-ago journey is
better than losing where the van is now, and the drop is logged rather than silent so
a van that is permanently over-running its buffer is discoverable.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any

from ._io import atomic_write_text
from .const import (
    DOMAIN,
    EVENT_SENSA_TRACKING_CHANGED,
    SENSA_TRACKING_BUFFER_MAX,
    SENSA_TRACKING_PATH,
    SENSA_TRACKING_STORE_KEY,
)

_LOGGER = logging.getLogger(__name__)


class SensaTrackingStore:
    """Durable Tracking state for this vehicle."""

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        # Off by default. Tracking records nothing until somebody deliberately
        # turns it on, having already consented in the Radar web app.
        self.enabled: bool = False
        # Off by default: Tracking records only a GPS that is part of the van.
        # On, a van with no GPS of its own records a phone instead (the driver's,
        # else the one that reported last), which is a PERSON's position, so it
        # is a separate, deliberate choice rather than a side effect of Auto.
        self.phone_fallback: bool = False
        self._buffer: deque[dict[str, Any]] = deque(maxlen=SENSA_TRACKING_BUFFER_MAX)
        # Where and when we last KEPT a fix, so the movement threshold and the
        # stationary heartbeat both survive a restart. Without persisting these, a
        # reboot would record a fresh point immediately regardless of movement.
        self.last_lat: float | None = None
        self.last_lon: float | None = None
        self.last_sample_ts: float | None = None
        self._dropped_total: int = 0

    # -- persistence -------------------------------------------------------

    def load_sync(self) -> None:
        """Read state from disk. Any corruption leaves the defaults in place.

        Deliberately tolerant: a truncated file after an unclean shutdown must
        leave Tracking off with an empty buffer rather than raise and take the
        whole integration down with it.
        """
        p = self.path
        try:
            if not p.exists():
                return
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            _LOGGER.debug("Sensa tracking: unreadable store, starting fresh", exc_info=True)
            return
        if not isinstance(raw, dict):
            return

        self.enabled = bool(raw.get("enabled"))
        self.phone_fallback = bool(raw.get("phone_fallback"))
        for key in ("last_lat", "last_lon", "last_sample_ts"):
            value = raw.get(key)
            try:
                setattr(self, key, float(value) if value is not None else None)
            except (TypeError, ValueError):
                setattr(self, key, None)

        points = raw.get("buffer")
        if isinstance(points, list):
            for item in points:
                point = _coerce_point(item)
                if point is not None:
                    self._buffer.append(point)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "phone_fallback": self.phone_fallback,
            "last_lat": self.last_lat,
            "last_lon": self.last_lon,
            "last_sample_ts": self.last_sample_ts,
            "buffer": list(self._buffer),
        }

    def _persist(self) -> None:
        snapshot = self._snapshot()
        try:
            self.hass.async_create_task(self._async_persist(snapshot))
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Sensa tracking: persist scheduling failed", exc_info=True)

    async def _async_persist(self, snapshot: dict[str, Any]) -> None:
        try:
            body = json.dumps(snapshot, separators=(",", ":"))
            await self.hass.async_add_executor_job(atomic_write_text, self.path, body)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa tracking: persist failed", exc_info=True)

    def _commit(self) -> None:
        self._persist()
        try:
            self.hass.bus.async_fire(EVENT_SENSA_TRACKING_CHANGED)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa tracking: change event failed", exc_info=True)

    # -- the switch --------------------------------------------------------

    def set_phone_fallback(self, value: bool) -> None:
        value = bool(value)
        if value == self.phone_fallback:
            return
        self.phone_fallback = value
        self._commit()

    def set_enabled(self, value: bool) -> None:
        value = bool(value)
        if value == self.enabled:
            return
        self.enabled = value
        if not value:
            # Turning the switch off is a pause, NOT a withdrawal of consent, so the
            # buffer is kept: the customer may simply be saving data for a week. The
            # only thing that clears the buffer is Radar telling us consent is gone,
            # which the coordinator handles.
            _LOGGER.info(
                "Tracking disabled; keeping %d buffered positions", len(self._buffer)
            )
        self._commit()

    # -- the buffer --------------------------------------------------------

    def buffered_count(self) -> int:
        return len(self._buffer)

    def append_point(self, point: dict[str, Any]) -> None:
        """Add a sampled fix, dropping the oldest if the buffer is full."""
        was_full = len(self._buffer) == self._buffer.maxlen
        self._buffer.append(point)
        if was_full:
            self._dropped_total += 1
            # Log the first drop and then every hundredth, so a van permanently out
            # of signal is visible without flooding the log.
            if self._dropped_total == 1 or self._dropped_total % 100 == 0:
                _LOGGER.warning(
                    "Tracking buffer full (%d); dropped %d oldest positions so far",
                    self._buffer.maxlen,
                    self._dropped_total,
                )
        self._commit()

    def peek_batch(self, limit: int) -> list[dict[str, Any]]:
        """The oldest `limit` points, WITHOUT removing them.

        Peek-then-drop rather than pop-then-send: if the upload fails or the box
        loses power mid-request, the points are still here. Radar's ingest is
        idempotent on (van, recorded_at), so re-sending a batch it already stored
        costs nothing and is far better than losing a journey.
        """
        if limit <= 0:
            return []
        out: list[dict[str, Any]] = []
        for point in self._buffer:
            out.append(point)
            if len(out) >= limit:
                break
        return out

    def drop_first(self, count: int) -> None:
        """Remove `count` points from the front, after they are safely accepted."""
        for _ in range(min(count, len(self._buffer))):
            self._buffer.popleft()
        self._commit()

    def clear_buffer(self) -> None:
        """Discard everything buffered.

        Called when Radar says consent has gone or Premium has lapsed. We must not
        sit on a queue of location data the customer has withdrawn consent for, so
        this is a real deletion rather than a pause.
        """
        if not self._buffer:
            return
        _LOGGER.info(
            "Tracking: discarding %d buffered positions (no longer permitted to send)",
            len(self._buffer),
        )
        self._buffer.clear()
        self._commit()

    def note_sample(self, lat: float, lon: float, ts: float) -> None:
        self.last_lat = lat
        self.last_lon = lon
        self.last_sample_ts = ts


def _coerce_point(item: Any) -> dict[str, Any] | None:
    """Validate one persisted point. Anything malformed is dropped, not raised."""
    if not isinstance(item, dict):
        return None
    recorded_at = item.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at:
        return None
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    point: dict[str, Any] = {"recorded_at": recorded_at, "lat": lat, "lon": lon}
    for optional in ("speed_mps", "accuracy_m"):
        value = item.get(optional)
        if value is None:
            continue
        try:
            point[optional] = float(value)
        except (TypeError, ValueError):
            continue
    return point


def get_tracking_store(hass: Any) -> SensaTrackingStore | None:
    data = getattr(hass, "data", None) or {}
    domain = data.get(DOMAIN)
    if not isinstance(domain, dict):
        return None
    store = domain.get(SENSA_TRACKING_STORE_KEY)
    return store if isinstance(store, SensaTrackingStore) else None


async def async_setup_tracking_store(hass: Any) -> SensaTrackingStore:
    """Create + load the singleton Tracking store. Idempotent across entries."""
    existing = get_tracking_store(hass)
    if existing is not None:
        return existing
    path = hass.config.path(SENSA_TRACKING_PATH)
    store = SensaTrackingStore(hass, path)
    await hass.async_add_executor_job(store.load_sync)
    hass.data.setdefault(DOMAIN, {})[SENSA_TRACKING_STORE_KEY] = store
    return store
