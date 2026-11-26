"""The Schengen 90/180 usage tracker — store + rolling-window maths.

Visa-exempt visitors (UK travellers post-Brexit, and other third-country
nationals) may be present in the Schengen area for at most **90 days in any
rolling 180-day period**. The Nomad knows where the van is (the vehicle GPS
tracker), so we can stamp each calendar day as in/out of the Schengen area and
compute the usage automatically — surfaced on the Intelligence → Schengen tab.

Design (parallel to `_alarms.py` so the maths is unit-testable without the HA
platform machinery):
  • Store = a dict held on `hass.data[DOMAIN][SENSA_SCHENGEN_STORE_KEY]` and
    persisted to `nomad/.sensa_schengen.json` so the calendar survives a
    restart. Shape:
        { "enabled": bool,
          "settings": { "limit_days": 90, "window_days": 180 },
          "days": { "2026-06-01": {"in": true, "cc": "FR", "src": "auto"} } }
  • A calendar day counts as a Schengen day if the van was in the area at ANY
    sampled moment that day — this is correct because the Schengen rule counts
    BOTH the entry and exit day as full days of presence. So once a day is
    stamped `in`, a later same-day `out` sample never un-stamps it (only an
    explicit manual edit can).
  • `compute_usage(ref_date)` does the rolling-window arithmetic: days used in
    the trailing `window_days`, days remaining, the date you must leave by if
    you stay continuously from today, and the next date a used day frees up.
  • All mutators run on the event loop, take an injectable `now`/`today` for
    tests, and fire `EVENT_SENSA_SCHENGEN_CHANGED` so an open card re-fetches.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._io import atomic_write_text
from .const import (
    DOMAIN,
    EVENT_SENSA_SCHENGEN_CHANGED,
    SENSA_SCHENGEN_LIMIT_DAYS,
    SENSA_SCHENGEN_PATH,
    SENSA_SCHENGEN_STORE_KEY,
    SENSA_SCHENGEN_WINDOW_DAYS,
)

_LOGGER = logging.getLogger(__name__)

_ISO_DAY = "%Y-%m-%d"


def local_today() -> date:
    """Today in HA's configured local timezone (the customer's wall clock).

    The card stamps/toggles days in the browser's LOCAL date, so the sampler
    and the rolling-window maths must use the same local day — not UTC, which
    would mis-attribute a day near midnight in a UTC-offset timezone. Falls
    back to UTC only when the HA helper isn't importable (unit tests pass an
    explicit reference date, so they never reach this)."""
    try:
        from homeassistant.util import dt as dt_util

        return dt_util.now().date()
    except Exception:  # pragma: no cover - non-HA contexts only  # noqa: BLE001
        return datetime.now(timezone.utc).date()


def _parse_day(raw: str) -> date | None:
    try:
        return datetime.strptime(raw, _ISO_DAY).replace(tzinfo=timezone.utc).date()
    except (ValueError, TypeError):
        return None


def _day_key(d: date) -> str:
    return d.strftime(_ISO_DAY)


class SensaSchengenStore:
    """The day-by-day presence map + its durable JSON backing.

    One per HA instance, on `hass.data[DOMAIN][SENSA_SCHENGEN_STORE_KEY]`.
    All mutators run on the event loop.
    """

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        self.enabled: bool = False
        # "YYYY-MM-DD" -> {"in": bool, "cc": str|None, "src": "auto"|"manual"}
        self.days: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, int] = {
            "limit_days": SENSA_SCHENGEN_LIMIT_DAYS,
            "window_days": SENSA_SCHENGEN_WINDOW_DAYS,
        }
        # Last resolved country code, so the backstop sampler can cheaply skip a
        # geocode when the van is parked and the country hasn't changed.
        self.last_cc: str | None = None

    # -- loading / persistence ----------------------------------------------
    def load_sync(self) -> None:
        """Populate from the backing JSON file. Runs in an executor."""
        p = self.path
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        self.enabled = bool(data.get("enabled", False))
        last = data.get("last_cc")
        self.last_cc = last if isinstance(last, str) else None
        days = data.get("days")
        if isinstance(days, dict):
            for key, rec in days.items():
                if isinstance(key, str) and isinstance(rec, dict):
                    raw_countries = rec.get("countries")
                    self.days[key] = {
                        "in": bool(rec.get("in", False)),
                        "cc": rec.get("cc"),
                        "countries": (
                            [str(c) for c in raw_countries]
                            if isinstance(raw_countries, list)
                            else None
                        ),
                        "src": rec.get("src", "auto"),
                    }
        settings = data.get("settings")
        if isinstance(settings, dict):
            for k in ("limit_days", "window_days"):
                try:
                    if settings.get(k) is not None:
                        self.settings[k] = int(settings[k])
                except (ValueError, TypeError):
                    pass

    def _persist(self) -> None:
        snapshot = {
            "enabled": self.enabled,
            "last_cc": self.last_cc,
            "settings": dict(self.settings),
            "days": {k: dict(v) for k, v in self.days.items()},
        }
        try:
            self.hass.async_create_task(self._async_persist(snapshot))
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Sensa schengen: persist scheduling failed", exc_info=True)

    async def _async_persist(self, snapshot: dict[str, Any]) -> None:
        try:
            body = json.dumps(snapshot, separators=(",", ":"))
            await self.hass.async_add_executor_job(atomic_write_text, self.path, body)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa schengen: persist failed", exc_info=True)

    # -- write paths ---------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self._commit()

    def mark_day(
        self,
        day: date | str,
        present: bool,
        *,
        cc: str | None = None,
        src: str = "manual",
    ) -> bool:
        """Stamp a single calendar day in/out of the Schengen area.

        Auto samples never DOWNGRADE a day already marked present (entry+exit
        both count, so any in-sample wins for the day); a manual edit always
        wins, present or absent. Returns True iff the store actually changed
        (and committed) so the sampler can avoid double-firing the change event.
        """
        d = day if isinstance(day, date) else _parse_day(day)
        if d is None:
            return False
        key = _day_key(d)
        existing = self.days.get(key)
        if cc:
            self.last_cc = cc
        # An auto sample must NEVER override a manual edit (present OR absent) —
        # the customer's correction is authoritative.
        if src == "auto" and existing is not None and existing.get("src") == "manual":
            return False
        # Accumulate the distinct countries seen this day (in OR out), so a
        # border-crossing day records every country, not just the last sample.
        countries = list(existing.get("countries") or []) if existing else []
        if cc and cc not in countries:
            countries.append(cc)
        if src == "manual":
            # A manual edit DEFINES the day — `present` is authoritative.
            new_in = bool(present)
        else:
            # An AUTO sample only ever ADDS presence: a day counts if the van was
            # in the area at ANY moment (entry and exit days both count), so a
            # same-day "in then out" stays counted. Auto never un-stamps.
            new_in = (bool(existing.get("in")) if existing else False) or bool(present)
        new_rec: dict[str, Any] = {
            "in": new_in,
            "cc": cc if cc else (existing.get("cc") if existing else None),
            "countries": countries or None,
            "src": src,
        }
        if existing == new_rec:
            return False  # no change — avoid persist/event churn on the backstop tick
        self.days[key] = new_rec
        self._commit()
        return True

    def set_range(
        self,
        start: date | str,
        end: date | str,
        present: bool,
        *,
        src: str = "manual",
    ) -> None:
        """Mark every day in [start, end] (inclusive) in/out. For manual trip
        entry on the card. Order-insensitive."""
        s = start if isinstance(start, date) else _parse_day(start)
        e = end if isinstance(end, date) else _parse_day(end)
        if s is None or e is None:
            return
        if e < s:
            s, e = e, s
        # Guard against an absurd range locking the loop.
        if (e - s).days > 366 * 5:
            return
        changed = False
        d = s
        while d <= e:
            key = _day_key(d)
            existing = self.days.get(key)
            # Preserve any auto-resolved country code + accumulated countries on
            # days this trip overlaps (a manual range carries none of its own).
            prev_cc = existing.get("cc") if existing else None
            prev_countries = existing.get("countries") if existing else None
            if present:
                new_rec = {
                    "in": True,
                    "cc": prev_cc,
                    "countries": prev_countries,
                    "src": src,
                }
            else:
                # Explicit absent marker (manual), so a later auto sample can't
                # re-stamp these days — same reasoning as mark_day's manual out.
                new_rec = {
                    "in": False,
                    "cc": prev_cc,
                    "countries": prev_countries,
                    "src": src,
                }
            if existing != new_rec:
                self.days[key] = new_rec
                changed = True
            d += timedelta(days=1)
        if changed:
            self._commit()

    def clear(self) -> None:
        """Wipe all recorded days (keeps the enabled flag + settings)."""
        if not self.days:
            return
        self.days = {}
        self._commit()

    # -- reads ---------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """The full store state, for the card."""
        return {
            "enabled": self.enabled,
            "settings": dict(self.settings),
            "days": {k: dict(v) for k, v in self.days.items()},
        }

    def _present_days(self) -> list[date]:
        out: list[date] = []
        for key, rec in self.days.items():
            if not rec.get("in"):
                continue
            d = _parse_day(key)
            if d is not None:
                out.append(d)
        out.sort()
        return out

    def compute_usage(self, ref_date: date | None = None) -> dict[str, Any]:
        """Rolling 90/180 arithmetic for reference day `ref_date` (default today).

        Returns:
          enabled, limit_days, window_days,
          days_used        — in-Schengen days in the trailing window [ref-W+1 … ref]
          days_remaining   — max(0, limit - days_used)
          window_start     — first day of the trailing window (ISO)
          in_schengen      — whether `ref_date` itself is stamped present
          current_cc       — the most-recent country code we resolved
          must_leave_by    — last date you may remain if present continuously from
                             today (ISO) or None when not currently consuming days
          next_returns     — date a used day next frees up (ISO) or None
          returns_amount   — how many days free up on next_returns
        """
        limit = int(self.settings.get("limit_days", SENSA_SCHENGEN_LIMIT_DAYS))
        window = int(self.settings.get("window_days", SENSA_SCHENGEN_WINDOW_DAYS))
        ref = ref_date or local_today()
        window_start = ref - timedelta(days=window - 1)

        present = self._present_days()
        present_set = set(present)
        in_window = [d for d in present if window_start <= d <= ref]
        days_used = len(in_window)
        days_remaining = max(0, limit - days_used)

        # must_leave_by: walk forward from `ref`, adding each future day as a
        # presence day, sliding the window each step. The last day where the
        # rolling count stays <= limit is the last day you may remain.
        must_leave_by: date | None = None
        if days_remaining > 0 or (ref in present_set and days_used <= limit):
            sim = dict.fromkeys(present_set, True)
            cursor = ref
            # Cap the projection at one full window ahead (can't legally stay
            # longer than that without days falling off anyway).
            horizon = ref + timedelta(days=window + 1)
            while cursor <= horizon:
                sim[cursor] = True
                w_start = cursor - timedelta(days=window - 1)
                count = sum(1 for d in sim if w_start <= d <= cursor)
                if count > limit:
                    break
                must_leave_by = cursor
                cursor += timedelta(days=1)

        # next_returns: the day after the EARLIEST presence day in the current
        # window rolls off — i.e. that day + window. That's when a consumed day
        # is handed back. Only meaningful when some days are in the window.
        next_returns: date | None = None
        returns_amount = 0
        if in_window:
            earliest = min(in_window)
            roll_off = earliest + timedelta(days=window)
            if roll_off > ref:
                next_returns = roll_off
                # How many days share that earliest date's roll-off boundary.
                returns_amount = sum(1 for d in in_window if d == earliest)

        ref_rec = self.days.get(_day_key(ref))
        return {
            "enabled": self.enabled,
            "limit_days": limit,
            "window_days": window,
            "days_used": days_used,
            "days_remaining": days_remaining,
            "window_start": _day_key(window_start),
            "in_schengen": bool(ref_rec and ref_rec.get("in")),
            "current_cc": self.last_cc,
            "must_leave_by": _day_key(must_leave_by) if must_leave_by else None,
            "next_returns": _day_key(next_returns) if next_returns else None,
            "returns_amount": returns_amount,
        }

    # -- side effects --------------------------------------------------------
    def _commit(self) -> None:
        self._persist()
        try:
            self.hass.bus.async_fire(EVENT_SENSA_SCHENGEN_CHANGED)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa schengen: change-event fire failed", exc_info=True)


# -- module-level helpers (the public surface) -------------------------------

def get_schengen_store(hass: Any) -> SensaSchengenStore | None:
    data = getattr(hass, "data", None) or {}
    domain = data.get(DOMAIN)
    if not isinstance(domain, dict):
        return None
    store = domain.get(SENSA_SCHENGEN_STORE_KEY)
    return store if isinstance(store, SensaSchengenStore) else None


async def async_setup_schengen_store(hass: Any) -> SensaSchengenStore:
    """Create + load the singleton Schengen store. Idempotent across entries."""
    existing = get_schengen_store(hass)
    if existing is not None:
        return existing
    path = hass.config.path(SENSA_SCHENGEN_PATH)
    store = SensaSchengenStore(hass, path)
    await hass.async_add_executor_job(store.load_sync)
    hass.data.setdefault(DOMAIN, {})[SENSA_SCHENGEN_STORE_KEY] = store
    return store
