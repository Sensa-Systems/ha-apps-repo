"""The customer's remote-access switch, integration side.

The App owns this setting: it holds the file, and it is the only thing that can
start or stop cloudflared. This module is the thin HA end of it, so the switch
entity has somewhere to send a change and somewhere to read the current answer
from.

Deliberately NOT a store. Persisting the flag here as well would be the same
predicate written twice, and the two copies would disagree the first time a box
was restored from a backup or the App applied its own default. What is kept here
is a CACHE of the App's answer, refreshed on every licence-status poll, so the
toggle can render before the next poll rather than flickering.

Not licence-gated. Turning remote access off must work on a read-only box, and
turning it on is how a customer lets support reach a box that has locked itself
down.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    EVENT_SENSA_REMOTE_ACCESS_CHANGED,
    REMOTE_ACCESS_STATE_KEY,
    RUNTIME_LICENCE_OK,
)

_LOGGER = logging.getLogger(__name__)


def get_enabled(hass: HomeAssistant) -> bool:
    """The App's last known answer, defaulting to OFF.

    Off is the right default for the gap before the first status poll, because
    it matches what the App does with an unreadable file and because a switch
    that reads ON while the tunnel is down would be the more misleading of the
    two wrong answers.
    """
    return bool((hass.data.get(DOMAIN) or {}).get(REMOTE_ACCESS_STATE_KEY))


def ingest_status(hass: HomeAssistant, status: Any) -> bool:
    """Refresh the cache from an ``/api/license/status`` payload.

    Returns True when the value changed, so the caller can fire the event that
    repaints the switch. An absent or malformed ``remote_access`` block is "could
    not ask" and leaves the cache alone: an older App, or a poll that failed,
    must not silently flip the customer's switch on screen.
    """
    block = status.get("remote_access") if isinstance(status, dict) else None
    if not isinstance(block, dict) or not isinstance(block.get("enabled"), bool):
        return False
    return _set_cached(hass, block["enabled"])


def _set_cached(hass: HomeAssistant, enabled: bool) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    if data.get(REMOTE_ACCESS_STATE_KEY) == enabled:
        return False
    data[REMOTE_ACCESS_STATE_KEY] = enabled
    hass.bus.async_fire(EVENT_SENSA_REMOTE_ACCESS_CHANGED, {"enabled": enabled})
    return True


async def async_set_enabled(hass: HomeAssistant, entry: Any, enabled: bool) -> None:
    """Send the customer's choice to the App and cache the result.

    The cache is only updated once the App has accepted the change, so a failed
    write leaves the switch showing what is actually true rather than what was
    asked for. Failures RAISE: HA turns that into a visible error, and a silent
    no-op on a control this consequential is worse than a red toast. This is the
    opposite of the remote-support push, which retries quietly on a tick, and
    deliberately so -- there the customer has already consented and the box is
    catching up, whereas here somebody is standing at the screen waiting to find
    out whether their van is now reachable from the internet.
    """
    entry_id = entry if isinstance(entry, str) else getattr(entry, "entry_id", None)
    runtime = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    # Read-only means the customer's own remote access stops, because it is
    # something they pay for. It does NOT stop Sensa support: a system in
    # read-only is exactly the system somebody needs help with, and cutting off
    # the help makes renewing harder for both sides. See vendor-access.md 3.6.
    #
    # Turning it OFF is always allowed. Refusing to let somebody close a door is
    # not a licensing decision, and a lapse must never leave a box more exposed
    # than the customer wants it.
    if enabled and not runtime.get(RUNTIME_LICENCE_OK, True):
        raise HomeAssistantError(
            "Remote access is part of an active Sensa licence. Renew to switch "
            "it back on. Sensa support can still reach this system if you ask "
            "for help."
        )
    # Seeded passwords. The App refuses to raise the tunnel while any login is
    # still on its default, so accepting the change here would only give the
    # customer a switch that reads ON while their van sits unreachable and the
    # Hub reports it as blocked -- which is exactly what it used to do. Refuse
    # it where somebody is standing at the screen to read the reason. Turning
    # OFF is never gated.
    if enabled:
        from . import default_logins

        weak = default_logins.blocking(hass)
        if weak:
            raise HomeAssistantError(default_logins.refusal_text(weak))
    client = runtime.get("client")
    if client is None:
        raise HomeAssistantError(
            "The Sensa App is not available, so remote access cannot be changed."
        )
    await client.async_post_remote_access_state(enabled=enabled)
    _set_cached(hass, enabled)
    _LOGGER.info("Remote access switched %s", "on" if enabled else "off")
