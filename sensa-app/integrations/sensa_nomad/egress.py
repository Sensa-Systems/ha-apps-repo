"""Internet bytes the integration sends itself, for Settings -> Data usage.

The App keeps the counts and serves the page's figures; this side only adds up
what its own calls move (Radar, cloud backup, the Schengen lookup) and posts
the totals to the App's `/api/v1/egress/record` every
`EGRESS_FLUSH_SECONDS`. A failed post keeps the counts for the next one, and
an App too old to know the route answers without `accepted`, which is treated
the same way, so nothing is lost and nothing is double counted.

Categories are the App's keys (`egress_meter.CATEGORIES`); the App files any
key it does not know under `other`. Payload bytes only: the App adds its
per-request allowance for headers and TLS, which is why `requests` is sent.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

_LOGGER = logging.getLogger(__name__)

# {category: [sent, received, requests]}
_pending: dict[str, list[int]] = {}


def record(category: str, sent: int = 0, received: int = 0, *, requests: int = 1) -> None:
    """Add one call's payload bytes. Never raises."""
    try:
        row = _pending.setdefault(category, [0, 0, 0])
        row[0] += max(0, int(sent or 0))
        row[1] += max(0, int(received or 0))
        row[2] += max(0, int(requests or 0))
    except (TypeError, ValueError):
        _LOGGER.debug("egress record failed", exc_info=True)


async def metered_chunks(
    category: str, chunks: AsyncIterator[bytes]
) -> AsyncIterator[bytes]:
    """Pass a download through, counting it. One request, recorded as the
    bytes arrive so an abandoned download still counts what it fetched."""
    record(category, 0, 0, requests=1)
    async for chunk in chunks:
        record(category, 0, len(chunk), requests=0)
        yield chunk


async def async_flush(client: Any) -> None:
    """Post the pending counts to the App; keep them if it did not take them."""
    if not _pending:
        return
    items = [
        {"category": cat, "sent": row[0], "received": row[1], "requests": row[2]}
        for cat, row in _pending.items()
    ]
    _pending.clear()
    try:
        result = await client.async_post_json(
            "/api/v1/egress/record", {"items": items}, timeout=10
        )
    except Exception:  # noqa: BLE001 - the App is unreachable; try next time
        result = None
    if not isinstance(result, dict) or "accepted" not in result:
        for item in items:
            record(
                item["category"], item["sent"], item["received"],
                requests=item["requests"],
            )
