"""What "now" means for the solar forecast: the van simulator's clock or the real one.

The van simulator runs its own clock (time of day, sped up by its multiplier)
and its solar output follows it, so a forecast on the real clock makes no sense
beside it: at simulated noon the real evening forecast says 0. The simulator's
GPS carries its clock as a `sim_time` attribute.

The simulated time is used ONLY when the van's position comes from the
simulator's GPS (the one resolver, `device_tracker.resolve_nav_source`), so the
forecast's place and time come from the same source. Any other source, which is
every real van, uses the real clock. Callers show "SIM" when it is simulated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant


def sim_now(hass: HomeAssistant) -> datetime | None:
    """The simulator's clock, when the van's position is the simulator's GPS
    and it carries a usable `sim_time`; otherwise None."""
    try:
        from .device_tracker import _simulator_tracker, _source_map, resolve_nav_source

        resolved = resolve_nav_source(hass, record=False)
        if resolved is None:
            return None
        eid, _pos = resolved
        sim_eid = _simulator_tracker(hass, _source_map(hass))
        if not sim_eid or eid != sim_eid:
            return None
        state: Any = hass.states.get(eid)
        raw = state.attributes.get("sim_time") if state is not None else None
        t = datetime.fromisoformat(str(raw)) if raw else None
    except Exception:  # noqa: BLE001 - a broken sim attribute means the real clock
        return None
    if t is None or t.tzinfo is None:
        return None
    return t.astimezone(UTC)


def now(hass: HomeAssistant) -> tuple[datetime, bool]:
    """(now in UTC, whether it is the simulator's clock)."""
    sim = sim_now(hass)
    return (sim, True) if sim is not None else (datetime.now(UTC), False)
