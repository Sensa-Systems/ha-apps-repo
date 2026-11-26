"""One-shot customer notice when the support contract lapses or is renewed.

A Nomad sale includes a period of support. When it ends, the things Sensa
operates on the customer's behalf stop: remote access to the vehicle, Sensa-driven
software updates, and Radar Premium. The LICENCE is perpetual and the van keeps
working exactly as before, so this is a change worth stating once, not a fault.

That distinction decides the whole shape of this module.

**Why a log entry and not a banner.** Letting support lapse is a legitimate
choice: somebody happy on their current version who has no need of remote access
has done nothing wrong, and a van that nags them about it is a van that punishes a
decision they made deliberately. The Sensa log is append-only history the customer
opens when they want it, so a single entry there records the fact without ever
pestering. It is logged at `info`, which is deliberately NOT in
`SENSA_LOG_PUSH_LEVELS` -- no persistent notification, no mobile push. The full
explanation and the renewal path live in the Hub Portal, where renewal happens.

**Why this needs a file.** The obvious implementation -- compare against the
previous status in memory -- re-announces the lapse after every Home Assistant
restart, which turns "once" into a nag by volume. `sensa_log`'s own `dedup_key` is
no help either: its dedup map is in-memory and is never rebuilt from the persisted
log, so it does not survive a restart. Hence a small durable record of what we have
already told the customer.

**Why silence is not `False`.** The cloud's `support_active` is `None` whenever we
have no answer -- an older licensing Function, a box that has never phoned home, an
unreachable cycle. Treating that as "lapsed" would announce the end of support to
every customer the moment they lost signal. Only an explicit transition between two
KNOWN states is ever announced.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ._io import atomic_write_text
from ._log import sensa_log
from .const import (
    DOMAIN,
    SENSA_LOG_SOURCE_SYSTEM,
    SENSA_SUPPORT_NOTICE_PATH,
    SENSA_SUPPORT_NOTICE_STORE_KEY,
)

_LOGGER = logging.getLogger(__name__)

# What the customer reads in the Log tab. Factual, and it leads with the
# reassurance because "your support plan has expired" alone invites the reasonable
# fear that the van is about to stop working.
LAPSED_MESSAGE = (
    "Your Sensa support plan has ended. Your system carries on working as normal "
    "and your licence does not expire. Remote access to your vehicle, Sensa "
    "software updates and Radar Premium have stopped. You can renew in your Sensa "
    "account."
)
RENEWED_MESSAGE = (
    "Your Sensa support plan is active again. Remote access, software updates and "
    "Radar Premium have been restored."
)


class SupportNoticeStore:
    """Remembers the last support state we ANNOUNCED to the customer.

    Deliberately not the last state we *observed*: the only thing worth persisting
    is what the customer has already been told, because that is what decides
    whether saying it again would be a repeat.
    """

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        # None = we have never announced anything. A fresh install that is already
        # out of support therefore says nothing: the customer did not experience a
        # change, and a box announcing a lapse it never saw begin is just noise.
        self.announced: bool | None = None

    # -- persistence -------------------------------------------------------

    def load_sync(self) -> None:
        """Read state from disk. Corruption leaves the default in place.

        Tolerant on purpose: a truncated file after an unclean shutdown must never
        take the integration down, and the worst case here is one duplicate log
        entry."""
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            _LOGGER.debug(
                "Sensa support notice: unreadable store, starting fresh", exc_info=True
            )
            return
        if not isinstance(raw, dict):
            return
        value = raw.get("announced")
        self.announced = bool(value) if isinstance(value, bool) else None

    async def async_persist(self) -> None:
        try:
            body = json.dumps({"announced": self.announced}, separators=(",", ":"))
            await self.hass.async_add_executor_job(atomic_write_text, self.path, body)
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Sensa support notice: persist failed", exc_info=True)

    # -- the decision ------------------------------------------------------

    def evaluate(self, support_active: object) -> str:
        """Decide what to do about `support_active`, and return WHY.

        Returns one of ``unknown`` / ``unchanged`` / ``first_answer`` /
        ``lapsed`` / ``renewed``. The reason is returned rather than kept private
        because "why was there no log entry?" is the question that actually comes
        up when testing this, and a caller that can log the reason answers it
        without a debugger.

        Pure: it mutates `announced` but performs no I/O and writes no log entry,
        so it can be exercised directly in tests.
        """
        if not isinstance(support_active, bool):
            # No answer from the cloud. NOT a lapse.
            return "unknown"
        if self.announced is None:
            # First real answer for this box. Record it silently: there is no
            # transition to report, only a starting point to measure from.
            self.announced = support_active
            return "first_answer"
        if self.announced == support_active:
            return "unchanged"
        self.announced = support_active
        return "renewed" if support_active else "lapsed"


def get_support_notice_store(hass: Any) -> SupportNoticeStore | None:
    return (hass.data.get(DOMAIN) or {}).get(SENSA_SUPPORT_NOTICE_STORE_KEY)


async def async_get_support_notice_store(hass: Any) -> SupportNoticeStore:
    existing = get_support_notice_store(hass)
    if existing is not None:
        return existing
    store = SupportNoticeStore(hass, hass.config.path(SENSA_SUPPORT_NOTICE_PATH))
    await hass.async_add_executor_job(store.load_sync)
    hass.data.setdefault(DOMAIN, {})[SENSA_SUPPORT_NOTICE_STORE_KEY] = store
    return store


async def async_note_support_state(hass: Any, support_active: object) -> str:
    """Log ONE customer entry if the support contract just changed state.

    Safe to call on every licence refresh -- that is the point. Returns the same
    reason string `evaluate` does, for the caller's diagnostic log.
    """
    store = get_support_notice_store(hass)
    if store is None:
        # Called before setup finished. Saying nothing is right: we have no record
        # of what the customer has been told, so we cannot know this is not a
        # repeat.
        return "no_store"

    outcome = store.evaluate(support_active)
    if outcome in ("lapsed", "renewed", "first_answer"):
        # Persist BEFORE logging. If the write fails we would rather risk missing
        # the entry than risk repeating it on every restart forever.
        await store.async_persist()
    if outcome == "lapsed":
        sensa_log(hass, "info", LAPSED_MESSAGE, source=SENSA_LOG_SOURCE_SYSTEM,
                  context={"support_active": False})
    elif outcome == "renewed":
        sensa_log(hass, "info", RENEWED_MESSAGE, source=SENSA_LOG_SOURCE_SYSTEM,
                  context={"support_active": True})
    return outcome
