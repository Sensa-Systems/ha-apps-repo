"""Home-page catalog for the Cab Home tab.

There is no customer picker: the first Cab layout this licence is allowed (a
dealer's own first, same licence-tier + Hub-dealer gating as Habitation) is the
arrangement a new box starts from, and the customer rearranges it in Design Mode. This module mirrors `home_catalog.py` for the Cab surface — it loads
`home_layouts_cab.json` and delegates the gating to the shared
`layout_catalog.py`, so neither surface duplicates that logic.

A new public Cab home page ships as a new release: add an entry here AND a
matching builder in `dashboard.py`'s `_CAB_LAYOUTS` (keyed by the entry's `id`).
A dealer's Cab page is a spec in `layouts/cab/` published through Hub. The
dealer claim + licence tier are SHARED with the Habitation catalog (one dealer,
one tier), so a dealer's `homepage_mode` (additive / exclusive) governs the
dealer's Cab pages exactly as it does their Habitation pages.

Each entry: see `layout_catalog` for the `{id, name, tiers?, dealer?}` shape.
"""

from __future__ import annotations

from typing import Any

from . import layout_catalog as _lc

# Module-level catalog list (the test patch point for the Cab surface, mirroring
# home_catalog.HOME_LAYOUTS_CATALOG). Functions read it at call time.
CAB_LAYOUTS_CATALOG: list[dict[str, Any]] = _lc.load_catalog("home_layouts_cab.json")


def cab_layout_names() -> list[str]:
    """Staff display names, in catalog order — the Cab layout dropdown's options."""
    return _lc.names(CAB_LAYOUTS_CATALOG)


def cab_id_for_name(name: str) -> str | None:
    """Map a selected display name back to its stable layout id."""
    return _lc.id_for_name(CAB_LAYOUTS_CATALOG, name)


def default_cab_name() -> str:
    """Display name of the first catalog entry — the fresh-install default."""
    return _lc.default_name(CAB_LAYOUTS_CATALOG)


def effective_cab_catalog(hass: Any | None = None) -> list[dict[str, Any]]:
    """The bundled Cab catalog plus any Cab layouts this box fetched from Hub
    (the Cab mirror of `home_catalog.effective_catalog`)."""
    return _lc.merged_catalog(CAB_LAYOUTS_CATALOG, hass, "cab")


def allowed_cab_names(tier: str | None, hass: Any | None = None) -> list[str]:
    """Cab layout names available to this tier and dealer, in catalog order."""
    return _lc.allowed_names(effective_cab_catalog(hass), tier, hass)


def allowed_cab_ids(tier: str | None, hass: Any | None = None) -> list[str]:
    """Cab layout ids available to this tier and dealer, in catalog order.
    Mirror of `allowed_cab_names` (1:1 with it via `cab_id_for_name`)."""
    return _lc.allowed_ids(effective_cab_catalog(hass), tier, hass)


def cab_dealer_is_exclusive_empty(tier: str | None, hass: Any | None) -> bool:
    """True when this licence's dealer is `exclusive` but owns NO Cab layouts at
    this tier — a contradictory config (the resolver leaves the Cab Home blank
    and logs once). A non-dealer / additive / trial licence is never
    exclusive-empty."""
    return _lc.dealer_is_exclusive_empty(effective_cab_catalog(hass), tier, hass)
