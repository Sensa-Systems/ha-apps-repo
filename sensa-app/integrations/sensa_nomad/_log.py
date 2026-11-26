"""The Sensa log — a customer-facing, non-technical message store.

This is NOT a replacement for Home Assistant's system log, and NOT a place for
developer/debug/crash output. It holds useful, human-readable messages the van
owner cares about, and every entry belongs to one of TWO logs (its `source`):

  • `vehicle` — the van talking about itself ("Low fresh water near a water
    source", an instinct firing, a cold-charge cut-off). Surfaced on
    Intelligence → **Log**.
  • `system` — the box talking about itself ("Tonight's backup failed",
    "Software update installed", a licence check-in, a device added). Surfaced
    on Settings → **System Log**.

Both surfaces are the same `custom:sensa-log-card`, filtered by source; higher
-severity entries also raise an HA notification, regardless of source, so a
driving customer sees them.

Design (see CLAUDE.md / the plan):
  • Runtime store = one in-memory ring buffer PER SOURCE (`collections.deque`,
    each capped at `SENSA_LOG_MAX_LINES`) held on
    `hass.data[DOMAIN][SENSA_LOG_STORE_KEY]`. Reads come from memory (fast); a
    single JSONL file backs them for restart survival. Per source, not shared,
    so a chatty update-and-backup week cannot evict the vehicle history the
    customer went looking for.
  • Capture-all: every level is stored. The Log tab's type chips are a DISPLAY
    filter (client-side), not a capture filter — nothing is dropped.
  • `sensa_log(hass, level, message, *, source, …)` is the one writer. `source`
    is a REQUIRED keyword: a producer states which log it belongs to rather
    than inheriting an answer, so a new one cannot land in the wrong tab
    silently. (The read side has a default — see `SENSA_LOG_SOURCE_DEFAULT` —
    because entries written before the split carry no source at all.)
    It's synchronous and expects to run on the event loop (every caller —
    dashboard build, the alert engine, lifecycle handlers — does). The deque
    append happens inline; file persistence, the count-sensor bump, the change
    event and any HA notification are scheduled as loop work.

Kept as its own small module (parallel to `_io.py`) so the ring-buffer / dedup
/ serialisation logic can be unit-tested without the dashboard or alert
machinery.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._io import atomic_write_text
from .const import (
    DOMAIN,
    EVENT_SENSA_LOG_CHANGED,
    SENSA_LOG_LEVEL_DEFAULT,
    SENSA_LOG_LEVELS,
    SENSA_LOG_MAX_LINES,
    SENSA_LOG_PATH,
    SENSA_LOG_PUSH_LEVELS,
    SENSA_LOG_RETENTION_DAYS_KEY,
    SENSA_LOG_SOURCE_DEFAULT,
    SENSA_LOG_SOURCES,
    SENSA_LOG_STORE_KEY,
)

_LOGGER = logging.getLogger(__name__)

# Default suppression window for a deduped entry when the caller passes a
# `dedup_key` but no explicit `cooldown` — a repeating condition (e.g. the
# dealer-no-layout warning emitted on every dashboard regen) logs at most once
# per window instead of on every fire.
_DEFAULT_DEDUP_COOLDOWN = timedelta(hours=24)

_LEVEL_RANK: dict[str, int] = {level: i for i, level in enumerate(SENSA_LOG_LEVELS)}
_PUSH_LEVELS: frozenset[str] = frozenset(SENSA_LOG_PUSH_LEVELS)
_SOURCES: frozenset[str] = frozenset(SENSA_LOG_SOURCES)


def _level_rank(level: str) -> int:
    """Severity rank for a level string; unknown levels clamp to `info`."""
    return _LEVEL_RANK.get(level, _LEVEL_RANK[SENSA_LOG_LEVEL_DEFAULT])


def _should_push(level: str) -> bool:
    """Whether an entry of this type also raises a one-shot HA notification.

    Explicit membership (warning/error) rather than a rank floor, so `alarm`
    — which ranks highest — is NOT pushed here: the alarm subsystem owns its
    own sticky, re-asserting notification and would otherwise double-notify.
    """
    return level in _PUSH_LEVELS


def _read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Read the backing JSONL file into a list (oldest → newest). Missing file
    or a corrupt line is tolerated — a bad line is skipped, never fatal."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _entry_time(entry: dict[str, Any]) -> datetime | None:
    """The entry's timestamp as an aware UTC datetime, or None if unreadable.

    Entries written before a timezone was carried (or hand-edited) can be naive;
    those are read as UTC, which is what `record()` always writes.
    """
    raw = entry.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _entry_source(entry: dict[str, Any]) -> str:
    """Which log an entry belongs to.

    An entry carrying no `source` — every entry written before the split, and
    anything hand-edited — reads as `vehicle`, because that is where all of
    them are displayed TODAY. An upgrade therefore moves nothing and hides
    nothing. This read-time rule is the ONLY default: `sensa_log()` requires
    the keyword on the write side.
    """
    raw = entry.get("source")
    return raw if raw in _SOURCES else SENSA_LOG_SOURCE_DEFAULT


def _entry_sort_key(entry: dict[str, Any]) -> datetime:
    """Sort key for merging the per-source buffers into one time-ordered view.
    An unparseable timestamp sorts oldest rather than raising, so a corrupt
    line costs its position and never the merge."""
    return _entry_time(entry) or datetime.min.replace(tzinfo=timezone.utc)


def _normalise_source(source: str) -> str:
    """Clamp an unrecognised source, exactly as an unknown level is clamped —
    a bad value must never cost the customer the entry itself."""
    if source in _SOURCES:
        return source
    _LOGGER.debug(
        "Sensa log: clamping unknown source %r to %s", source, SENSA_LOG_SOURCE_DEFAULT,
    )
    return SENSA_LOG_SOURCE_DEFAULT


def _write_jsonl(path: Path | str, entries: list[dict[str, Any]]) -> None:
    """Atomically rewrite the backing JSONL file from `entries`."""
    body = "\n".join(json.dumps(e, separators=(",", ":")) for e in entries)
    atomic_write_text(path, body + ("\n" if body else ""))


class SensaLogStore:
    """The in-memory ring buffers + their durable JSONL backing.

    One per HA instance, stored on `hass.data[DOMAIN][SENSA_LOG_STORE_KEY]`.
    `record()` is the hot path; it runs on the event loop.

    One deque PER SOURCE, each capped at `SENSA_LOG_MAX_LINES`. A single shared
    buffer would let the system log (the far chattier of the two — backups,
    updates, licence check-ins) evict the vehicle history the customer went
    looking for, and the split would HIDE that: their Log tab would look empty
    while the System Log was full. Filtering on read cannot fix eviction; only
    separate buffers can.
    """

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        self._by_source: dict[str, deque[dict[str, Any]]] = {
            source: deque(maxlen=SENSA_LOG_MAX_LINES)
            for source in SENSA_LOG_SOURCES
        }
        # Dedup is keyed on (source, key) so a scoped clear resets only its own
        # suppressions, and so two surfaces can never collide on a shared key.
        self._dedup: dict[tuple[str, str], datetime] = {}
        self._sensor: Any | None = None
        # notification id -> the source that raised it, so a scoped clear
        # dismisses only its own popups (clearing the System Log must not
        # dismiss a live low-water warning).
        self._notif_ids: dict[str, str] = {}
        self._notif_seq = 0
        self._flush_inflight = False
        self._flush_again = False
        # Retention window in days; None = keep everything (the ring-buffer cap
        # alone decides).
        #
        # Seeded to None, NOT to the fresh-install default, for the same reason
        # `load_sync` doesn't prune: nothing here knows the customer's choice
        # until `number.sensa_nomad_log_retention` restores and calls
        # `set_retention_days`, and entries CAN be written before that (a
        # `sensa_log()` during entry setup, e.g. from the dashboard build).
        # Seeding to the 90-day default meant those early writes pruned at 90
        # days on a box whose customer had chosen "Keep everything", destroying
        # the history the load path had just been careful to preserve. Permissive
        # until told otherwise is the only safe pre-restore state.
        self.retention_days: int | None = None

    # -- buffers -------------------------------------------------------------
    def buffer(self, source: str) -> deque[dict[str, Any]]:
        """The ring buffer for one source (an unknown name clamps)."""
        return self._by_source[_normalise_source(source)]

    @property
    def entries(self) -> list[dict[str, Any]]:
        """Every entry across both sources, oldest first.

        A merged VIEW, not a buffer — anything that appends goes through
        `record()`, and the reads that need the whole store (a flush, an
        unfiltered snapshot) are rare. Sorted by timestamp so the backing file
        stays in time order; the sort is stable, so entries sharing a timestamp
        keep the order they were written in, and one whose `ts` won't parse
        sorts to the front rather than being dropped.
        """
        merged: list[dict[str, Any]] = []
        for buf in self._by_source.values():
            merged.extend(buf)
        merged.sort(key=_entry_sort_key)
        return merged

    # -- retention -----------------------------------------------------------
    def set_retention_days(self, days: int | None) -> None:
        """Set the age window and apply it at once.

        Called by `number.sensa_nomad_log_retention` on restore and on every
        change. A shorter window takes effect immediately (the customer expects
        the tab to reflect their choice, not to wait for the next entry).
        """
        self.retention_days = days
        if self.prune():
            self._push_count()
            # Separate try blocks, mirroring `record()`: a failed flush must not
            # cost the change event too, or an open Log card would keep showing
            # entries that have just been pruned until something else logs.
            try:
                self._schedule_flush()
            except Exception:  # pragma: no cover - best-effort durability
                _LOGGER.debug("Sensa log: retention flush failed", exc_info=True)
            try:
                self.hass.bus.async_fire(EVENT_SENSA_LOG_CHANGED)
            except Exception:  # pragma: no cover - best-effort
                _LOGGER.debug("Sensa log: retention event fire failed", exc_info=True)

    def prune(self, now: datetime | None = None) -> int:
        """Drop entries older than the retention window. Returns how many went.

        Applies to EVERY source: the window is one customer-facing setting
        ("keep my history for 90 days"), not a per-tab one, so the Log and the
        System Log must age out together or the setting would be lying about
        one of them.

        Entries are appended in time order within a source, so this only ever
        has to walk the OLD end of each deque and can stop at the first entry
        inside the window. An entry whose `ts` won't parse is treated as
        in-window and kept: a corrupt timestamp is not evidence the entry is
        old, and dropping on a parse failure would silently eat history.
        """
        if self.retention_days is None:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(
            days=self.retention_days
        )
        dropped = 0
        for entries in self._by_source.values():
            while entries:
                stamp = _entry_time(entries[0])
                if stamp is None or stamp >= cutoff:
                    break
                entries.popleft()
                dropped += 1
        return dropped

    # -- sensor wiring -------------------------------------------------------
    def bind_sensor(self, sensor: Any) -> None:
        """Attach the count sensor so appends push a fresh state (the card's
        change-ping). Called from the sensor platform's `async_added_to_hass`."""
        self._sensor = sensor
        self._push_count()

    def unbind_sensor(self, sensor: Any) -> None:
        if self._sensor is sensor:
            self._sensor = None

    @property
    def total_entries(self) -> int:
        """Entry count across both sources.

        Counted from the buffers directly rather than via `len(self.entries)`:
        that property MERGES AND SORTS, and this runs on every single write.
        """
        return sum(len(buf) for buf in self._by_source.values())

    def _push_count(self) -> None:
        sensor = self._sensor
        if sensor is None:
            return
        try:
            sensor.set_count(self.total_entries)
        except Exception:  # pragma: no cover - sensor not fully set up yet
            _LOGGER.debug("Sensa log: count-sensor push failed", exc_info=True)

    # -- loading / persistence ----------------------------------------------
    def load_sync(self) -> None:
        """Populate the deques from the backing file. Runs in an executor.

        One file, both sources: each line is routed by its own `source`, and a
        line that carries none is read as `vehicle` (see `_entry_source`), so
        every entry written before the split lands exactly where the customer
        last saw it.

        Deliberately does NOT prune. The retention select restores AFTER this,
        so pruning here would apply the DEFAULT window to a customer who chose
        "Keep everything" and delete their history on every restart, with no way
        back. `set_retention_days` prunes the moment the real choice is known;
        until then the ring-buffer cap is the only bound, which is where we
        started.
        """
        for entry in _read_jsonl(self.path):
            self._by_source[_entry_source(entry)].append(entry)

    def _schedule_flush(self) -> None:
        """Persist the deque to disk, coalescing bursts. Timer-free: while a
        flush is in flight, later appends just re-arm one final write."""
        if self._flush_inflight:
            self._flush_again = True
            return
        self._flush_inflight = True
        self.hass.async_create_task(self._async_flush())

    async def _async_flush(self) -> None:
        try:
            while True:
                self._flush_again = False
                snapshot = list(self.entries)
                await self.hass.async_add_executor_job(
                    _write_jsonl, self.path, snapshot
                )
                if not self._flush_again:
                    break
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Sensa log: flush failed", exc_info=True)
        finally:
            self._flush_inflight = False

    # -- the write path ------------------------------------------------------
    def record(
        self,
        level: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        dedup_key: str | None = None,
        cooldown: timedelta | None = None,
        notify: bool | None = None,
        source: str = SENSA_LOG_SOURCE_DEFAULT,
        now: datetime | None = None,
    ) -> bool:
        """Append a customer-facing log entry. Returns True if recorded, False
        if suppressed by dedup/cooldown. Runs on the event loop.

        `notify` overrides whether a high-severity entry also raises a
        persistent_notification: None = the default for the level (warning/error
        push), False = log only (no popup), True = force a push. Use False for a
        recurring, self-healing condition (e.g. a licence check-in blip) that
        belongs in the support log but must not nag the customer.

        `source` picks which log the entry lands in (`vehicle` = Intelligence →
        Log, `system` = Settings → System Log). It defaults here so the store's
        own tests stay terse; the module-level `sensa_log()` that every producer
        calls REQUIRES it, which is where the "state which log you mean"
        guarantee lives.

        Whether an entry also raises a notification is decided by its LEVEL
        alone and deliberately does not vary by source: a failed backup is
        exactly the sort of thing a customer needs told about, and making the
        new tab quieter would turn it into a place things go to die unseen. A
        specific noisy producer passes `notify=False` instead.

        `now` is injectable for tests; production passes None → wall clock.
        """
        if level not in _LEVEL_RANK:
            _LOGGER.debug("Sensa log: clamping unknown level %r to %s",
                          level, SENSA_LOG_LEVEL_DEFAULT)
            level = SENSA_LOG_LEVEL_DEFAULT
        source = _normalise_source(source)

        stamp = now or datetime.now(timezone.utc)
        if dedup_key is not None:
            window = cooldown if cooldown is not None else _DEFAULT_DEDUP_COOLDOWN
            last = self._dedup.get((source, dedup_key))
            if last is not None and (stamp - last) < window:
                return False
            self._dedup[(source, dedup_key)] = stamp

        entry = {
            "ts": stamp.isoformat(),
            "level": level,
            "source": source,
            "message": str(message),
            "context": dict(context) if context else {},
        }
        self._by_source[source].append(entry)
        # Age out on the write path so a busy install stays trimmed without a
        # timer. A quiet install is covered by `snapshot()` pruning on read.
        self.prune(stamp)

        # Side effects — all loop-safe (we're on the loop). Guarded so a half
        # set-up hass during early start can't make logging itself raise.
        try:
            self._schedule_flush()
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa log: scheduling flush failed", exc_info=True)
        self._push_count()
        try:
            self.hass.bus.async_fire(EVENT_SENSA_LOG_CHANGED)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa log: change-event fire failed", exc_info=True)
        push = _should_push(level) if notify is None else notify
        if push:
            self._raise_notification(level, message, dedup_key, source)
        return True

    def _raise_notification(
        self, level: str, message: str, dedup_key: str | None, source: str,
    ) -> None:
        """Surface a high-severity entry where the customer will see it: an HA
        persistent_notification (always) + best-effort mobile push.

        The id keeps its historical `sensa_log_<key>` shape (a running notice
        must keep replacing itself across restarts), so the source is recorded
        alongside it rather than folded into the id.
        """
        if dedup_key:
            notif_id = f"sensa_log_{dedup_key}"
        else:
            self._notif_seq += 1
            notif_id = f"sensa_log_{self._notif_seq}"
        self._notif_ids[notif_id] = source
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification", "create",
                    {
                        "notification_id": notif_id,
                        "title": "Sensa",
                        "message": message,
                    },
                    blocking=False,
                )
            )
            # Best-effort mobile push — only when a notify service exists (the
            # HA companion app registers `notify.notify`). Never a hard dep.
            if self.hass.services.has_service("notify", "notify"):
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "notify", "notify",
                        {"title": "Sensa", "message": message},
                        blocking=False,
                    )
                )
        except Exception:  # pragma: no cover - notifications are best-effort
            _LOGGER.debug("Sensa log: notification dispatch failed", exc_info=True)

    def clear(self, source: str | None = None) -> None:
        """Empty one log (or both) and dismiss the notifications it raised.

        `source=None` clears everything — the historical behaviour, and what a
        support wipe wants. A scoped clear touches ONLY that source: clearing
        the System Log must not throw away the customer's vehicle history, nor
        dismiss a live low-water warning that has nothing to do with it.

        Dedup is reset alongside the entries (per source), so a still-true
        condition re-logs at once instead of staying suppressed for up to 24 h
        against entries the customer has just deleted.
        """
        if source is None:
            targets = list(self._by_source)
        else:
            targets = [_normalise_source(source)]
        for name in targets:
            self._by_source[name].clear()
        self._dedup = {
            key: stamp for key, stamp in self._dedup.items()
            if key[0] not in targets
        }
        self._push_count()
        try:
            self._schedule_flush()
            self.hass.bus.async_fire(EVENT_SENSA_LOG_CHANGED)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa log: clear side-effects failed", exc_info=True)
        dismissing = [
            notif_id for notif_id, owner in self._notif_ids.items()
            if owner in targets
        ]
        for notif_id in dismissing:
            try:
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "persistent_notification", "dismiss",
                        {"notification_id": notif_id}, blocking=False,
                    )
                )
            except Exception:  # pragma: no cover  # noqa: BLE001, S110
                pass
            self._notif_ids.pop(notif_id, None)

    def snapshot(
        self, limit: int | None = None, source: str | None = None,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent entries, newest first, capped to `limit`.

        `source=None` returns both logs merged (a raw store read); a named
        source returns just that one. **The filter is applied BEFORE `limit`**,
        which is the whole point of filtering here rather than in the card: it
        makes `limit` mean "N entries of THIS log", so a quiet vehicle log
        still shows its entries next to a busy system one.

        Prunes first so a van that has logged nothing for weeks still shows the
        window the customer chose rather than whatever was last written. Reads
        are rare (opening the Log tab), so this costs nothing in practice; it
        does mean the read path can shrink the store, which is why the count
        sensor is re-pushed when it does.
        """
        if self.prune():
            self._push_count()
            try:
                self._schedule_flush()
            except Exception:  # pragma: no cover - best-effort durability
                _LOGGER.debug("Sensa log: prune-on-read flush failed", exc_info=True)
        if source is None:
            items = list(reversed(self.entries))
        else:
            items = list(reversed(self._by_source[_normalise_source(source)]))
        # One device's lines (the heater diagnostics popup's fault history),
        # matched on the producer's `context.device_id`; before `limit` too.
        if device_id:
            items = [
                e for e in items
                if ((e.get("context") or {}).get("device_id")) == device_id
            ]
        if limit is not None:
            items = items[:limit]
        return items


# -- module-level helpers (the public surface) -------------------------------

def get_log_store(hass: Any) -> SensaLogStore | None:
    """The singleton store, or None if logging isn't set up yet."""
    data = getattr(hass, "data", None) or {}
    domain = data.get(DOMAIN)
    if not isinstance(domain, dict):
        return None
    store = domain.get(SENSA_LOG_STORE_KEY)
    return store if isinstance(store, SensaLogStore) else None


async def async_setup_log_store(hass: Any) -> SensaLogStore:
    """Create + load the singleton log store. Idempotent across entries."""
    existing = get_log_store(hass)
    if existing is not None:
        return existing
    path = hass.config.path(SENSA_LOG_PATH)
    store = SensaLogStore(hass, path)
    await hass.async_add_executor_job(store.load_sync)
    hass.data.setdefault(DOMAIN, {})[SENSA_LOG_STORE_KEY] = store
    # Adopt the retention window the select already published. Platforms are
    # forwarded before this runs, so on a normal boot the select was added while
    # this store did not exist and its own push found nothing; without this the
    # customer's choice was silently dropped every boot and nothing ever pruned.
    # Absent key = the select hasn't been added (a partial setup), and the
    # permissive None seed correctly stands until it is.
    domain_data = hass.data.get(DOMAIN) or {}
    if SENSA_LOG_RETENTION_DAYS_KEY in domain_data:
        store.set_retention_days(domain_data[SENSA_LOG_RETENTION_DAYS_KEY])
    return store


def sensa_log(
    hass: Any,
    level: str,
    message: str,
    *,
    source: str,
    context: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    cooldown: timedelta | None = None,
    notify: bool | None = None,
) -> bool:
    """Write a customer-facing log entry. No-op (returns False) if the store
    isn't up yet. See `SensaLogStore.record`. Must be called on the loop.

    `source` is REQUIRED and has no default: it decides which of the two logs
    the customer reads this in (`SENSA_LOG_SOURCE_VEHICLE` = Intelligence →
    Log, the van talking about itself; `SENSA_LOG_SOURCE_SYSTEM` = Settings →
    System Log, the box talking about itself). Making it required is the whole
    guarantee — a producer states which log it means, so a new one cannot land
    in the wrong tab by inheriting somebody else's default, and a missed call
    site is a TypeError at the first test rather than a message quietly filed
    under the wrong heading.

    `notify=False` logs a high-severity entry WITHOUT raising a popup."""
    store = get_log_store(hass)
    if store is None:
        return False
    return store.record(
        level, message, context=context, dedup_key=dedup_key, cooldown=cooldown,
        notify=notify, source=source,
    )


def get_sensa_log(
    hass: Any, limit: int | None = None, source: str | None = None,
    device_id: str | None = None,
) -> list[dict[str, Any]]:
    """Recent entries, newest first. Empty when the store isn't up.

    `source=None` means every entry, not a default surface — this is the raw
    store read, and the legacy "absent means vehicle" rule belongs to the
    ENTRY (`_entry_source`) and to the card's config, not here."""
    store = get_log_store(hass)
    if store is None:
        return []
    return store.snapshot(limit, source, device_id)


def clear_sensa_log(hass: Any, source: str | None = None) -> None:
    """Empty one log (or both, when `source` is None). No-op when the store
    isn't up."""
    store = get_log_store(hass)
    if store is not None:
        store.clear(source)
