"""Whether the Cerbo's Remote Console is reachable through this box's origin.

The Electrical tab's centre hub opens the Cerbo GX Remote Console. It used to be
handed the Cerbo's own address and framed it directly, which failed twice over: a
secure page refuses to frame an http origin, and away from the van that address
reaches nothing at all, because the tunnel publishes Home Assistant and nothing
else. Serving the console from this box instead fixes both, since the browser
then only talks to the origin it is already signed in to.

This module is the HA-side cache of the App's answer, and it is deliberately NOT
a second check. Working out here whether a proxy exists would mean re-deriving,
on this side, whether the branding nginx is running and whether a Cerbo has a
resolvable address. That is the same predicate written twice, and the copy that
drifts is the one nobody runs: the two would disagree exactly when it mattered,
and the card would frame a path with nothing behind it. The App decides, because
it is the side that also renders the nginx snippet
(``routes/cerbo_console.py``); this holds the last thing it said.

**Two answers, and the fallback is the old behaviour, not an error.** ``True``
means use the proxied path. ``False`` covers both "no proxy on this box" (HAOS,
or the branding overlay is not deployed) and "we could not ask" (an older App
that does not report the block), and both mean the same thing here: carry on
handing the card the Cerbo's direct address exactly as before. Unlike a licence
gate there is nothing to fail open or shut, because the fallback is what every
box did until this existed.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CERBO_CONSOLE_STATE_KEY, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Where the App proxies the console, used only when it has not told us its own
# path yet. It has to match ``CONSOLE_PATH`` in the App's routes/cerbo_console.py
# and the `location` in the snippet updater.sh renders; the App sends the live
# value with every status, so this is a floor and not a second source of truth.
_DEFAULT_PATH = "/sensa-cerbo/"


def get(hass: HomeAssistant) -> dict[str, Any]:
    """The App's last answer: ``{"proxied": bool, "path": str}``."""
    value = (hass.data.get(DOMAIN) or {}).get(CERBO_CONSOLE_STATE_KEY)
    if not isinstance(value, dict):
        return {"proxied": False, "path": _DEFAULT_PATH}
    return value


def console_path(hass: HomeAssistant) -> str | None:
    """The path to frame, or ``None`` to use the Cerbo's direct address.

    ``None`` is the caller's cue to keep doing what it always did, so a box with
    no proxy is unchanged rather than broken.
    """
    state = get(hass)
    if not state.get("proxied"):
        return None
    path = str(state.get("path") or "").strip()
    return path or _DEFAULT_PATH


def ingest_status(hass: HomeAssistant, status: dict[str, Any]) -> None:
    """Cache the ``cerbo_console`` block from ``/api/license/status``.

    A missing key means an App too old to have an opinion; leave the cache alone
    rather than writing a false, because a status body that simply predates this
    feature must not flip a working proxy off between polls.
    """
    if not isinstance(status, dict) or "cerbo_console" not in status:
        return
    block = status.get("cerbo_console")
    if not isinstance(block, dict):
        return
    value = {
        "proxied": bool(block.get("proxied")),
        "path": str(block.get("path") or "").strip() or _DEFAULT_PATH,
    }
    store = hass.data.setdefault(DOMAIN, {})
    if store.get(CERBO_CONSOLE_STATE_KEY) == value:
        return
    store[CERBO_CONSOLE_STATE_KEY] = value
    _LOGGER.debug("Cerbo console: %s", value)
