"""Solar forecast accuracy: what the panels made against what was forecast.

Every hour, on the hour, the tracker:

  1. closes the hour that just ended: the solar energy counter's rise over it
     (`sensor.sensa_nomad_energy_solar`, vendor-neutral kWh) is that hour's
     ACTUAL, set beside the FORECAST taken for it an hour earlier;
  2. takes the forecast for the hour now starting from the solar forecast
     coordinator (solar_forecast.py), to be closed next hour.

An hour only counts when both ends are known: the counter read at both ticks,
the ticks exactly an hour apart (a van switched off for a while just skips the
hours it missed) and a forecast taken. So a day's forecast and actual always
cover the same hours, and a partial day compares like with like.

Days are kept for 30 days in HA's own store. The owner sees them on the
Electric tab (`sensor.sensa_nomad_solar_forecast_accuracy`); with the "Solar
forecast accuracy" diagnostics switch on, the last 14 days also go to Sensa so
the forecast can be tuned (diagnostics_report.py).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

KEEP_DAYS = 30
SHARE_DAYS = 14
DISPLAY_DAYS = 7
# A counter that jumps by more than the panels could make in an hour (a reset,
# or a replaced meter re-reading its lifetime total) is not an hour of sun.
_MAX_HOURLY_FACTOR = 1.5


def _iso(t: datetime) -> str:
    return t.isoformat()


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def forecast_kwh_for(hours: list[tuple[datetime, float]], start: datetime) -> float | None:
    """The forecast energy (kWh) for the hour starting at ``start``."""
    for t, watts in hours:
        if t == start:
            return round(watts / 1000, 4)
    return None


def tick(
    state: dict[str, Any],
    hour_start: datetime,
    counter_kwh: float | None,
    forecast_next_kwh: float | None,
    rating_wp: float,
    tz,
) -> dict[str, Any]:
    """Advance the tracker to ``hour_start`` (an aware datetime on the hour).

    Pure: returns the new state. ``forecast_next_kwh`` is the forecast for the
    hour starting now (None when there is no forecast or no rating).
    """
    state = {
        "days": dict(state.get("days") or {}),
        "last_tick": state.get("last_tick"),
        "last_counter": state.get("last_counter"),
        "pending": state.get("pending"),
    }
    prev = hour_start - timedelta(hours=1)
    last_tick = _parse(state["last_tick"])
    pending = state["pending"] or {}
    if (
        counter_kwh is not None
        and state["last_counter"] is not None
        and last_tick == prev
        and _parse(pending.get("hour")) == prev
        and pending.get("forecast_kwh") is not None
    ):
        actual = counter_kwh - float(state["last_counter"])
        cap = rating_wp / 1000 * _MAX_HOURLY_FACTOR if rating_wp > 0 else None
        if actual >= 0 and (cap is None or actual <= cap):
            key = prev.astimezone(tz).date().isoformat()
            day = dict(state["days"].get(key) or {})
            day["forecast_kwh"] = round(
                day.get("forecast_kwh", 0.0) + pending["forecast_kwh"], 4
            )
            day["actual_kwh"] = round(day.get("actual_kwh", 0.0) + actual, 4)
            day["hours"] = day.get("hours", 0) + 1
            state["days"][key] = day

    state["last_tick"] = _iso(hour_start)
    state["last_counter"] = counter_kwh
    state["pending"] = (
        {"hour": _iso(hour_start), "forecast_kwh": forecast_next_kwh}
        if forecast_next_kwh is not None and rating_wp > 0
        else None
    )
    oldest = (hour_start.astimezone(tz).date() - timedelta(days=KEEP_DAYS)).isoformat()
    state["days"] = {k: v for k, v in state["days"].items() if k >= oldest}
    return state


def day_rows(state: dict[str, Any], today: date, count: int) -> list[dict[str, Any]]:
    """The last ``count`` days up to and including ``today``, oldest first,
    each ``{date, forecast_kwh, actual_kwh, hours}``; days with nothing
    recorded are left out."""
    days = state.get("days") or {}
    out = []
    for n in range(count - 1, -1, -1):
        key = (today - timedelta(days=n)).isoformat()
        d = days.get(key)
        if d and d.get("hours"):
            out.append(
                {
                    "date": key,
                    "forecast_kwh": round(d["forecast_kwh"], 2),
                    "actual_kwh": round(d["actual_kwh"], 2),
                    "hours": d["hours"],
                }
            )
    return out


def accuracy_pct(rows: list[dict[str, Any]]) -> float | None:
    """Actual as a percentage of forecast over ``rows``: 100 is spot on, 80 means
    the panels made a fifth less than forecast. None until there is at least
    0.1 kWh of forecast to compare against (a dark week says nothing)."""
    forecast = sum(r["forecast_kwh"] for r in rows)
    if forecast < 0.1:
        return None
    return round(sum(r["actual_kwh"] for r in rows) / forecast * 100)
