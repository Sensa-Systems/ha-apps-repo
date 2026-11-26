"""Find a van level's orientation from two readings (2026-10-29).

The customer parks roughly level and presses Next, then lifts the front of
the van (front wheels on the levelling ramps) and presses Done. The first
reading says which axis points up, the change between the two says which
points forward, and left follows from the two. The answer is written to the
three Front / Left / Up selects, the same ones the customer can set by hand.

The accelerometer at rest reads +1 g along whichever axis points UP, so the
axis carrying the gravity reading is Up, and the axis whose share of it grows
when the front is lifted is Front.
"""

from __future__ import annotations

import math
from typing import Any

from .const import (
    VAN_LEVEL_LOGICAL_AXES,
    van_level_axis_select_entity_id,
)

_AXES = ("x", "y", "z")
# Up must carry nearly all of gravity: a unit bolted in at an angle has no
# single Up axis to pick, and a guess there reads as a confident wrong answer.
MIN_UP_SHARE = 0.9
# The lift must tilt the van at least this much (sin of ~4 degrees), or the
# change is noise.
MIN_TILT = 0.07

# Per device: the Next reading, waiting for Done.
_LEVEL_READINGS: dict[str, tuple[float, float, float]] = {}


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
    n = math.sqrt(sum(c * c for c in v))
    if n < 1e-6:
        return None
    return (v[0] / n, v[1] / n, v[2] / n)


def _label(vec: tuple[int, int, int]) -> str:
    for i, c in enumerate(vec):
        if c:
            return _AXES[i].upper() + ("-" if c < 0 else "")
    raise ValueError("zero axis")


def orientation_from(
    level: tuple[float, float, float],
    lifted: tuple[float, float, float],
) -> dict[str, str] | str:
    """Front / Left / Up from a level reading and a front-lifted one.

    Returns `{"forward": "X", "left": "Y-", "up": "Z-"}`, or an error code:
    `no_reading` (a zero vector), `not_square` (no axis points up), or
    `not_tilted` (the front did not go up enough to tell).
    """
    a = _unit(level)
    b = _unit(lifted)
    if a is None or b is None:
        return "no_reading"
    up_i = max(range(3), key=lambda i: abs(a[i]))
    if abs(a[up_i]) < MIN_UP_SHARE:
        return "not_square"
    d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    fwd_i = max((i for i in range(3) if i != up_i), key=lambda i: abs(d[i]))
    if abs(d[fwd_i]) < MIN_TILT:
        return "not_tilted"
    up = [0, 0, 0]
    up[up_i] = 1 if a[up_i] > 0 else -1
    fwd = [0, 0, 0]
    fwd[fwd_i] = 1 if d[fwd_i] > 0 else -1
    # Right-handed: forward x left = up, so left = up x forward.
    left = (
        up[1] * fwd[2] - up[2] * fwd[1],
        up[2] * fwd[0] - up[0] * fwd[2],
        up[0] * fwd[1] - up[1] * fwd[0],
    )
    return {
        "forward": _label(tuple(fwd)),
        "left": _label(left),
        "up": _label(tuple(up)),
    }


def _role_map(device_id: str) -> dict[str, str]:
    from .dashboard import _dashboard_devices

    for d in _dashboard_devices:
        if str(d.get("device_id") or d.get("id") or "") == device_id:
            return d.get("levelling_entities") or {}
    return {}


def levelling_setup_for_device(dev: dict[str, Any] | None) -> dict[str, str]:
    """What the device window needs to help set a van level up: its id and
    its Levelling Mode switch (fast readings while the window is open).
    Empty for any other device."""
    if not dev:
        return {}
    device_id = str(dev.get("device_id") or dev.get("id") or "")
    if not device_id:
        return {}
    roles = _role_map(device_id)
    if not all(roles.get(f"accel_{a}") for a in _AXES):
        return {}
    return {"device_id": device_id, "mode_switch": roles.get("mode_switch") or ""}


def _read(hass: Any, device_id: str) -> tuple[float, float, float] | None:
    roles = _role_map(device_id)
    out: list[float] = []
    for a in _AXES:
        st = hass.states.get(roles.get(f"accel_{a}") or "")
        try:
            out.append(float(st.state))
        except (AttributeError, TypeError, ValueError):
            return None
    return (out[0], out[1], out[2])


async def async_orientation_step(
    hass: Any, device_id: str, step: str,
) -> dict[str, Any]:
    """`level` keeps the reading; `front_up` works out and saves the answer."""
    reading = _read(hass, device_id)
    if reading is None:
        return {"status": "no_reading"}
    if step == "level":
        _LEVEL_READINGS[device_id] = reading
        return {"status": "ok"}
    level = _LEVEL_READINGS.get(device_id)
    if level is None:
        return {"status": "no_level"}
    result = orientation_from(level, reading)
    if isinstance(result, str):
        return {"status": result}
    for logical in VAN_LEVEL_LOGICAL_AXES:
        await hass.services.async_call(
            "select", "select_option",
            {
                "entity_id": van_level_axis_select_entity_id(device_id, logical),
                "option": result[logical],
            },
            blocking=True,
        )
    _LEVEL_READINGS.pop(device_id, None)
    return {"status": "ok", "mapping": result}
