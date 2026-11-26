"""Keep Home Assistant's external URL in step with the remote-access tunnel.

The licensing cloud mints a per-device tunnel and hands its hostname down on
phone-home; the App flattens that into ``/data/cloudflare.json`` and republishes
it on ``/api/license/status`` as ``cloudflare: {enabled, hostname}``. Nothing was
then putting it into HA's own config, so a customer wanting remote access had to
read a 12-hex hostname off a screen and type it into their phone. On a product
whose whole premise is that you do not type on it, that is the wrong ending.

So: when the tunnel is up we write ``https://<hostname>`` into HA's external URL,
and when it goes away we take it back out. Settings > System > Network describes
that field as the address "Home Assistant should share with other devices", which
is exactly why it has to be removed as well as added -- a lapsed box that keeps
advertising a dead hostname hands broken URLs to text-to-speech and media
players.

**Never overwrite the customer's own address.** A customer with their own domain
pointed at the van has made a deliberate choice, and stamping over it on every
15-minute refresh is the device-rename bug in another costume. We therefore
remember the value WE wrote and only ever touch an empty field or our own
previous value. Anything else is theirs and we leave it entirely alone, in both
directions -- we will not clear it either.

**Silence is not "off".** The ``cloudflare`` block is absent on a BYO box, an
older App, or a status poll that could not reach the App. That is "could not
ask", and it must not clear a working URL. Only an explicit ``enabled: false``
(the kill-switch, a lapsed support contract, or the default-password gate) means
the tunnel is genuinely gone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ._io import atomic_write_text
from .const import (
    DOMAIN,
    SENSA_EXTERNAL_URL_PATH,
    SENSA_EXTERNAL_URL_STORE_KEY,
)

_LOGGER = logging.getLogger(__name__)


def desired_external_url(cloudflare: object) -> str | None | object:
    """What the external URL should be, from the App's ``cloudflare`` block.

    Returns the URL string when the tunnel is up, ``None`` when it is explicitly
    down (so the caller clears), or ``UNKNOWN`` when we have no answer and must
    leave everything alone.
    """
    if not isinstance(cloudflare, dict):
        return UNKNOWN
    if not cloudflare.get("enabled"):
        # An explicit block, tunnel not enabled: kill-switch, lapsed support, or
        # the default-password gate. A real answer, so the URL comes back out.
        return None
    hostname = str(cloudflare.get("hostname") or "").strip().strip("/")
    if not hostname:
        # Enabled but nameless is malformed rather than an answer. Leave be.
        return UNKNOWN
    # Always https and never a path: HA refuses an external_url carrying a path
    # ("Invalid external_url set. It's not allowed to have a path").
    return f"https://{hostname}"


class _Unknown:
    """Sentinel: no usable answer, distinct from a URL and from None."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNKNOWN"

    def __bool__(self) -> bool:
        return False


UNKNOWN = _Unknown()


class ExternalUrlStore:
    """Remembers the external URL this integration last wrote.

    Deliberately not "the last URL we saw": the only thing worth persisting is
    what we are responsible for, because that is what decides whether the value
    now in HA is ours to change or the customer's to keep.
    """

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        # None = we have never written one, so ANY value present belongs to the
        # customer.
        self.written: str | None = None

    # -- persistence -------------------------------------------------------

    def load_sync(self) -> None:
        """Read state from disk. Corruption leaves the default in place, which
        errs towards treating an existing URL as the customer's and doing
        nothing -- the harmless direction."""
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            _LOGGER.debug(
                "Sensa external URL: unreadable store, starting fresh", exc_info=True
            )
            return
        if not isinstance(raw, dict):
            return
        value = raw.get("written")
        self.written = value if isinstance(value, str) and value else None

    async def async_persist(self) -> None:
        try:
            body = json.dumps({"written": self.written}, separators=(",", ":"))
            await self.hass.async_add_executor_job(atomic_write_text, self.path, body)
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Sensa external URL: persist failed", exc_info=True)

    # -- the decision ------------------------------------------------------

    def evaluate(self, desired: object, current: str | None) -> tuple[str, str | None]:
        """Decide what to do, and return ``(reason, value_to_write)``.

        Reasons: ``unknown`` (no answer), ``customer_owned`` (hands off),
        ``unchanged``, ``set``, ``cleared``. ``value_to_write`` is meaningful
        only for ``set`` and ``cleared``.

        Pure apart from updating ``written``: no I/O, so the decision can be
        tested directly rather than through a fake Home Assistant.
        """
        if isinstance(desired, _Unknown):
            return "unknown", None
        current = current or None
        if current is not None and current != self.written:
            # Either the customer typed one before we ever wrote (written is
            # None), or they replaced ours. Either way it is theirs now.
            return "customer_owned", None
        if current == desired:
            return "unchanged", None
        self.written = desired
        return ("set" if desired else "cleared"), desired


def get_external_url_store(hass: Any) -> ExternalUrlStore | None:
    return (hass.data.get(DOMAIN) or {}).get(SENSA_EXTERNAL_URL_STORE_KEY)


async def async_get_external_url_store(hass: Any) -> ExternalUrlStore:
    existing = get_external_url_store(hass)
    if existing is not None:
        return existing
    store = ExternalUrlStore(hass, hass.config.path(SENSA_EXTERNAL_URL_PATH))
    await hass.async_add_executor_job(store.load_sync)
    hass.data.setdefault(DOMAIN, {})[SENSA_EXTERNAL_URL_STORE_KEY] = store
    return store


async def async_apply_external_url(hass: Any, cloudflare: object) -> str:
    """Set or clear HA's external URL from the App's ``cloudflare`` block.

    Safe to call on every licence refresh -- that is the point. Returns the same
    reason string ``evaluate`` does, so the caller can log why nothing happened,
    which is the question that comes up when testing this.
    """
    store = get_external_url_store(hass)
    if store is None:
        store = await async_get_external_url_store(hass)

    desired = desired_external_url(cloudflare)
    current = getattr(hass.config, "external_url", None)
    reason, value = store.evaluate(desired, current)
    if reason in ("unknown", "customer_owned", "unchanged"):
        return reason

    try:
        await hass.config.async_update(external_url=value)
    except Exception:
        _LOGGER.warning("Sensa external URL: could not %s the address", reason, exc_info=True)
        # Roll the memory back so a failed write is not remembered as ours; the
        # next refresh retries rather than believing it already succeeded.
        store.written = current
        return "failed"

    await store.async_persist()
    _LOGGER.info(
        "Sensa Nomad: %s Home Assistant's external URL (%s)",
        reason,
        value or "removed",
    )
    return reason
