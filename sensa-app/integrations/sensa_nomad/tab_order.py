"""The customer's own tab order, per dashboard.

The tab strip of every panel can be drag-reordered (press and hold a tab, then
drag, the same gesture as the Scenes cards). The order is box-wide, not
per-screen: it is kept here and applied by `dashboard.build_panel_config`, so
every screen showing that dashboard agrees and it survives restarts and
upgrades.

Only slugs are stored. A tab the customer never placed (a conditional tab such
as Alarms that was absent when they reordered, a Nerd Mode tab, a tab a later
release adds) keeps its place next to the tab it is built after, so it never
lands somewhere arbitrary and nothing is ever hidden by a stale order.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORE_KEY = "sensa_nomad.tab_order"
STORE_VERSION = 1

# dashboard key -> slugs in the customer's order. Module-level because
# `build_panel_config` is a plain function with no hass (the same way the
# dashboard module holds `_dashboard_devices`); the store below fills it.
_orders: dict[str, list[str]] = {}


def apply_tab_order(dashboard_key: str, tabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return `tabs` in the customer's saved order for this dashboard.

    Saved slugs come first in their saved order. Every other tab is anchored
    right after the tab it is built after (or first, if it is built first), so
    a tab added since the save lands where the builder meant it to.
    """
    saved = _orders.get(dashboard_key)
    if not saved:
        return tabs
    by_slug = {t.get("slug"): t for t in tabs}
    result = [by_slug[s] for s in saved if s in by_slug]
    placed = {t.get("slug") for t in result}
    for i, tab in enumerate(tabs):
        if tab.get("slug") in placed:
            continue
        if i == 0:
            result.insert(0, tab)
        else:
            prev = tabs[i - 1]
            at = next(
                (j for j, t in enumerate(result) if t is prev), len(result) - 1,
            )
            result.insert(at + 1, tab)
        placed.add(tab.get("slug"))
    return result


class TabOrderStore:
    """Persists `_orders` across restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        _orders.clear()
        if not isinstance(loaded, dict):
            return
        for key, slugs in (loaded.get("orders") or {}).items():
            if isinstance(key, str) and isinstance(slugs, list):
                clean = _dedupe(slugs)
                if clean:
                    _orders[key] = clean

    async def async_set(self, dashboard_key: str, slugs: list[Any] | None) -> bool:
        """Save one dashboard's order. Empty resets it to the built order.
        Returns whether anything changed."""
        clean = _dedupe(slugs or [])
        if clean == _orders.get(dashboard_key, []):
            return False
        if clean:
            _orders[dashboard_key] = clean
        else:
            _orders.pop(dashboard_key, None)
        await self._store.async_save({"orders": {k: list(v) for k, v in _orders.items()}})
        return True


async def async_get_store(hass: HomeAssistant) -> TabOrderStore:
    """The one loaded store for this run, loading it on first use."""
    data = hass.data.setdefault("sensa_nomad", {})
    store = data.get("tab_order_store")
    if store is None:
        store = TabOrderStore(hass)
        await store.async_load()
        data["tab_order_store"] = store
    return store


def _dedupe(slugs: list[Any]) -> list[str]:
    out: list[str] = []
    for s in slugs:
        if isinstance(s, str) and s and s not in out:
            out.append(s)
    return out
