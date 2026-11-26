"""The customer's own order of the Instincts tiles.

In Design Mode the Intelligence -> Instincts tiles can be dragged into a new
order. The order is box-wide, kept here, and applied by the automations sensor
(`SensaAutomationsSensor`), so every screen shows the same order and it
survives restarts and upgrades.

Only ids are stored. An instinct the customer never placed (one a later
release adds, or one that was hidden when they reordered) keeps its catalog
place right after the instinct it follows there, so nothing is ever lost or
landed somewhere arbitrary by a stale order.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORE_KEY = "sensa_nomad.instinct_order"
STORE_VERSION = 1

# Module-level so the sensor, which has no store handle, can read it; the
# store below fills it.
_order: list[str] = []


def apply_instinct_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`rows` (each with an `id`) in the customer's order. The starting order
    is alphabetical by name; saved ids come first in their order, and every
    other row goes right after the row it follows alphabetically (or first, if
    it leads)."""
    rows = sorted(rows, key=lambda r: str(r.get("name") or r.get("id") or "").casefold())
    if not _order:
        return rows
    by_id = {r.get("id"): r for r in rows}
    result = [by_id[i] for i in _order if i in by_id]
    placed = {id(r) for r in result}
    for i, row in enumerate(rows):
        if id(row) in placed:
            continue
        if i == 0:
            result.insert(0, row)
        else:
            prev = rows[i - 1]
            at = next((j for j, r in enumerate(result) if r is prev), len(result) - 1)
            result.insert(at + 1, row)
        placed.add(id(row))
    return result


class InstinctOrderStore:
    """Persists `_order` across restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        _order.clear()
        if isinstance(loaded, dict):
            _order.extend(_dedupe(loaded.get("order") or []))

    async def async_set(self, ids: list[Any] | None) -> bool:
        """Save the order. Empty resets it to the catalog order. Returns
        whether anything changed."""
        clean = _dedupe(ids or [])
        if clean == _order:
            return False
        _order[:] = clean
        await self._store.async_save({"order": list(_order)})
        return True


async def async_get_store(hass: HomeAssistant) -> InstinctOrderStore:
    """The one loaded store for this run, loading it on first use."""
    data = hass.data.setdefault("sensa_nomad", {})
    store = data.get("instinct_order_store")
    if store is None:
        store = InstinctOrderStore(hass)
        await store.async_load()
        data["instinct_order_store"] = store
    return store


def _dedupe(ids: list[Any]) -> list[str]:
    out: list[str] = []
    for i in ids:
        if isinstance(i, str) and i and i not in out:
            out.append(i)
    return out
