"""One-shot customer notice when the picked driver holds no Radar Premium seat.

Radar Premium is sold as a fixed number of SEATS (a Nomad Base licence covers one
person, Pro and Vision two), and a seat belongs to a named member of the customer
account. So a driver can pick themselves on the Cab, see the badge read **Base**,
and have no idea why -- the licence is perfectly valid and somebody else in the van
gets Premium.

This is the only Radar refusal the CUSTOMER can fix themselves, in mySensa, by
moving a seat. Every other one (`no_customer`, `licence_revoked`, ...) is a support
call, which is why the badge stays silent about those and says something about this.

Shape follows `_support_notice.py`, and for the same reasons:

* **A log entry, not a banner.** Driving without Premium is not a fault, and a van
  that nags about it punishes a choice the account owner made. `info` is
  deliberately outside `SENSA_LOG_PUSH_LEVELS`, so there is no notification and no
  mobile push -- just a line in the Log tab if they go looking.
* **It needs a file.** Comparing against the previous poll in memory re-announces
  after every restart, and `sensa_log`'s own dedup map is in-memory too, so neither
  survives a reboot.

**Keyed per member, and that is load-bearing.** The tier follows the driver picker,
so "Jo has no seat" and "Sam has no seat" are different pieces of news; one map
entry per member is what stops the second going unsaid. A member who is granted a
seat later has their entry cleared, so if it is taken away again the customer is
told again -- a seat can move at any time, which is exactly why silence forever
would be wrong.
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
    SENSA_SEAT_NOTICE_PATH,
    SENSA_SEAT_NOTICE_STORE_KEY,
)

_LOGGER = logging.getLogger(__name__)

# Hub's wire codes for the two seat refusals, and what each one means for the
# customer. `no_seat_available` is "the licence is full"; `seat_displaced` is "you
# had one and it was moved" -- different fixes, so different sentences.
SEAT_MESSAGES = {
    "no_seat_available": (
        "The driver selected on this screen does not have a Radar Premium seat. "
        "Every seat on this licence is in use. The account owner can move a seat "
        "in your Sensa account, or add another. Everything else works as normal."
    ),
    "seat_displaced": (
        "The driver selected on this screen no longer has a Radar Premium seat -- "
        "it was reassigned to somebody else on the account. The account owner can "
        "move it back in your Sensa account. Everything else works as normal."
    ),
}


class SeatNoticeStore:
    """Remembers, per member, which seat refusal we have already announced."""

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        # member_id -> the reason last announced for them. A member with no entry
        # has never been the subject of a notice.
        self.announced: dict[str, str] = {}

    # -- persistence -------------------------------------------------------

    def load_sync(self) -> None:
        """Read state from disk. Corruption leaves the default in place.

        Tolerant on purpose: a truncated file after an unclean shutdown must never
        take the integration down, and the worst case is one duplicate log entry.
        """
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            _LOGGER.debug(
                "Sensa seat notice: unreadable store, starting fresh", exc_info=True
            )
            return
        if not isinstance(raw, dict):
            return
        self.announced = {
            str(k): str(v)
            for k, v in (raw.get("announced") or {}).items()
            if isinstance(v, str)
        }

    async def async_persist(self) -> None:
        try:
            body = json.dumps({"announced": self.announced}, separators=(",", ":"))
            await self.hass.async_add_executor_job(atomic_write_text, self.path, body)
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Sensa seat notice: persist failed", exc_info=True)

    # -- the decision ------------------------------------------------------

    def evaluate(self, member_id: str, reason: object) -> str:
        """Decide what to say about this member's seat, and return WHY.

        Returns ``announce`` / ``repeat`` / ``cleared`` / ``nothing``. The reason is
        returned rather than kept private because "why was there no log entry?" is
        the question that comes up when testing this.

        Pure: it mutates `announced` but performs no I/O and writes no log entry.
        """
        code = str(reason or "")
        if code not in SEAT_MESSAGES:
            # Not a seat refusal. That includes the happy path (this member holds a
            # seat) and every other refusal, which are support's problem, not the
            # customer's. Forget any previous notice so a seat taken away LATER is
            # announced again -- seats move, and a one-shot that never resets would
            # tell the customer once in the lifetime of the box.
            if self.announced.pop(member_id, None) is not None:
                return "cleared"
            return "nothing"
        if self.announced.get(member_id) == code:
            return "repeat"
        self.announced[member_id] = code
        return "announce"


def get_seat_notice_store(hass: Any) -> SeatNoticeStore | None:
    return (hass.data.get(DOMAIN) or {}).get(SENSA_SEAT_NOTICE_STORE_KEY)


async def async_get_seat_notice_store(hass: Any) -> SeatNoticeStore:
    existing = get_seat_notice_store(hass)
    if existing is not None:
        return existing
    store = SeatNoticeStore(hass, hass.config.path(SENSA_SEAT_NOTICE_PATH))
    await hass.async_add_executor_job(store.load_sync)
    hass.data.setdefault(DOMAIN, {})[SENSA_SEAT_NOTICE_STORE_KEY] = store
    return store


async def async_note_seat_state(hass: Any, member_id: str, reason: object) -> str:
    """Log ONE customer entry if this member has just been refused a seat.

    Safe to call on every poll -- that is the point. Returns the same outcome
    string `evaluate` does, for the caller's diagnostic log.
    """
    store = get_seat_notice_store(hass)
    if store is None:
        # Called before setup finished. Saying nothing is right: with no record of
        # what the customer has been told, we cannot know this is not a repeat.
        return "no_store"

    outcome = store.evaluate(member_id, reason)
    if outcome in ("announce", "cleared"):
        # Persist BEFORE logging: if the write fails we would rather miss the entry
        # than repeat it on every restart forever.
        await store.async_persist()
    if outcome == "announce":
        sensa_log(
            hass,
            "info",
            SEAT_MESSAGES[str(reason)],
            source=SENSA_LOG_SOURCE_SYSTEM,
            context={"member_id": member_id, "reason": str(reason)},
        )
    return outcome
