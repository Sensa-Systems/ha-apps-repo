"""Why the box went down, told on the way back up.

Before this, a shutdown only explained itself when the thermal Instinct raised
an alarm on its way out. Everything else (the System tab's Restart, a host
reboot, a factory reset, a restore, an OTA) left nothing on the customer's Log,
and a power cut left nothing anywhere. The Log jumped from "yesterday" to "this
morning" with no seam, so nobody could tell a deliberate restart from a crash.

Two halves:

* **on the way down**, anything Sensa initiates records an INTENT (a reason
  string) through :func:`async_record_shutdown_intent`; and
* **on the way up**, :func:`async_log_boot_reason` writes exactly one entry to
  the System Log saying why, consuming the intent as it goes.

What it deliberately does NOT do is guess. A restart with no recorded intent
could be a power cut, a crash, `docker restart`, or HA's own Restart button, and
this cannot tell them apart without host uptime, which nothing reports today. So
it says what it knows ("no reason was recorded") and lists the possibilities
rather than announcing a power cut that may not have happened. The same rule
applies to the App: unreachable is "could not ask", and an unasked question is
never answered "no".
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ._log import sensa_log
from .const import (
    SENSA_LOG_SOURCE_SYSTEM,
    SHUTDOWN_INTENT_STORAGE_KEY,
    SHUTDOWN_INTENT_STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Once per HA PROCESS, not once per config-entry setup. A module-level flag is
# exactly the right lifetime: a config-entry reload re-runs setup but leaves the
# module loaded (so the boot entry is not written again), and only a real HA
# restart reimports it (which is the event we are reporting).
_BOOT_LOGGED = False


def _store(hass: HomeAssistant) -> Store:
    return Store(hass, SHUTDOWN_INTENT_STORAGE_VERSION, SHUTDOWN_INTENT_STORAGE_KEY)


async def async_record_shutdown_intent(
    hass: HomeAssistant, reason: str, *, action: str = "restart"
) -> None:
    """Record WHY the box is about to go down, for the next boot to report.

    Called immediately before asking for the restart / reboot / poweroff, and
    awaited: `Store.async_save` writes the file there and then, unlike
    `async_delay_save`, which would lose the race against the restart it is
    describing.

    Best-effort. A box that cannot write this still has to be allowed to
    restart, so a failure is logged and swallowed: the cost is one boot entry
    reading "no reason was recorded", never a restart that does not happen.
    """
    try:
        await _store(hass).async_save(
            {"reason": reason, "action": action, "at": int(time.time())}
        )
    except Exception:
        _LOGGER.warning(
            "Could not record the shutdown reason %r", reason, exc_info=True
        )


async def _async_take_intent(hass: HomeAssistant) -> dict[str, Any] | None:
    """Read and CLEAR the recorded intent.

    Cleared on read, which is what keeps it honest: an intent that was written
    but never acted on (the restart failed, the service was called by hand)
    explains the next boot and then stops explaining every boot after it.

    Not time-limited. A clean poweroff may be reported days later, when someone
    finally powers the box back on, and that answer is still the right one.
    """
    try:
        store = _store(hass)
        record = await store.async_load()
        if not isinstance(record, dict) or not record.get("reason"):
            return None
        await store.async_remove()
        return record
    except Exception:
        _LOGGER.debug("Could not read the recorded shutdown reason", exc_info=True)
        return None


async def _async_host_power_reason(hass: HomeAssistant) -> str | None:
    """The host power agent's own record of the last poweroff, or None.

    Backstop for a poweroff that did not come through
    `sensa_nomad.request_shutdown` (so left no intent). None covers BOTH "the
    host recorded nothing" and "the App could not be asked" -- the caller treats
    them the same, as an absent reason rather than as a confident negative, and
    says so in the wording.
    """
    from . import _find_app_client

    client = _find_app_client(hass)
    if client is None:
        return None
    try:
        payload = await client.async_get_power_status()
    except Exception:
        _LOGGER.debug("Could not read the host power status at boot", exc_info=True)
        return None
    # A 4xx comes back as a parsed BODY, not an exception (see api/client.py),
    # so an older App that has no such route looks like success until checked.
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    status = payload.get("status")
    if not isinstance(status, dict):
        return None
    reason = status.get("reason")
    return str(reason) if reason else None


async def async_log_boot_reason(hass: HomeAssistant) -> bool:
    """Write the one System Log entry explaining this boot. Returns whether it
    wrote (False = already written for this process).

    Ordering matters. The intent is checked FIRST because it is the richer
    answer and the only one that covers an HA-only restart; the host's power
    record is a backstop for a poweroff that bypassed our service.
    """
    global _BOOT_LOGGED
    if _BOOT_LOGGED:
        return False
    _BOOT_LOGGED = True

    intent = await _async_take_intent(hass)
    reason = (intent or {}).get("reason") or await _async_host_power_reason(hass)
    if reason:
        sensa_log(
            hass, "info", f"System restarted: {reason}.",
            source=SENSA_LOG_SOURCE_SYSTEM,
        )
        return True

    # No reason anywhere. Say that, and say what it could mean, rather than
    # announcing a power cut we have not established: an HA restart from HA's
    # own UI, a `docker restart`, a crash and a power cut are indistinguishable
    # from here. `notify=False` because this is a normal sight on a dev box and
    # a popup on every restart would train everyone to ignore it.
    sensa_log(
        hass, "warning",
        "System restarted, and no shutdown reason was recorded. Sensa did not "
        "start this one: it was a power cut, a crash, or a restart from outside "
        "Sensa.",
        source=SENSA_LOG_SOURCE_SYSTEM, notify=False,
    )
    return True
