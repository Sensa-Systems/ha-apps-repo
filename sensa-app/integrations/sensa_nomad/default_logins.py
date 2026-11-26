"""Which logins still have the password the image was built with.

The appliance seeds its customer logins with password = username, so the tablet
in the van is not a typing exercise. That is defensible while the box is only
reachable from the van's own network, and it stops being defensible the moment
something puts Home Assistant's login page on the internet. The App already
refuses to raise the tunnel while any of them stand
(``weak_credentials.weak_logins`` -> ``routes/cloudflare.py``), which is the
enforcement; what was missing is the customer being told BEFORE they flip a
switch, rather than the Hub quietly showing their van as unreachable.

This module is the HA-side cache of that one answer. It is deliberately NOT a
second check: re-deriving "is this password still the default" inside the
integration would be the same predicate written twice, and the copy that drifts
is the one nobody runs. The App reports the answer on every
``/api/license/status`` poll and this holds the last one.

**Three answers, not two**, exactly as the App reports them. ``[]`` = asked and
clean; a non-empty list = asked, and these logins are still on their defaults;
``None`` = COULD NOT ASK (older App, unreadable auth store, no bcrypt), which
carries forward and never blocks. Refusing a paying customer's remote access
because a file could not be read would be the worse failure.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_LOGINS_STATE_KEY,
    DOMAIN,
    EVENT_SENSA_DEFAULT_LOGINS_CHANGED,
)

_LOGGER = logging.getLogger(__name__)


def get(hass: HomeAssistant) -> list[str] | None:
    """The App's last answer: a list of usernames, or None for could-not-ask."""
    value = (hass.data.get(DOMAIN) or {}).get(DEFAULT_LOGINS_STATE_KEY)
    return value if isinstance(value, list) else None


def blocking(hass: HomeAssistant) -> list[str]:
    """The logins that must be fixed before a remote door may open.

    Empty means nothing is blocking, which folds "clean" together with "could not
    ask" on purpose: both proceed. Callers that need to tell those apart use
    ``get``.
    """
    return sorted(get(hass) or [])


def refusal_text(names: list[str]) -> str:
    """The one wording for "not until the passwords are changed".

    Lives here so the switch error, the log line and the panel cannot drift into
    saying three different things about the same rule.
    """
    joined = ", ".join(names)
    return (
        f"{joined} still {'has' if len(names) == 1 else 'have'} the password this "
        "system was set up with. Set a password for every login under "
        "Settings > Admin > Passwords first: until then this system would be "
        "reachable from the internet with a password anyone can guess."
    )


def ingest_status(hass: HomeAssistant, status: Any) -> bool:
    """Refresh the cache from an ``/api/license/status`` payload.

    Returns True when the answer changed, so the caller can fire the event that
    repaints the switches. A payload with no ``default_logins`` key is an older
    App and leaves the cache alone; an explicit ``null`` is the App saying it
    could not ask, and IS stored, because forgetting a "could not ask" would
    leave a stale "these are weak" blocking the toggles for ever.
    """
    if not isinstance(status, dict) or "default_logins" not in status:
        return False
    raw = status.get("default_logins")
    value = (
        sorted(str(n) for n in raw if isinstance(n, str) and n)
        if isinstance(raw, list)
        else None
    )
    data = hass.data.setdefault(DOMAIN, {})
    if DEFAULT_LOGINS_STATE_KEY in data and data[DEFAULT_LOGINS_STATE_KEY] == value:
        return False
    data[DEFAULT_LOGINS_STATE_KEY] = value
    if value:
        _LOGGER.info(
            "Remote access and remote support are held closed: %s still on the "
            "default password",
            ", ".join(value),
        )
    hass.bus.async_fire(EVENT_SENSA_DEFAULT_LOGINS_CHANGED, {"logins": value})
    return True
