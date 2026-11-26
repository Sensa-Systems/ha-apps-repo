"""Home-page catalog for the Habitation Home tab.

Loads the Habitation Home tab layouts from `home_layouts_hab.json` in this
directory. There is no customer picker: the first layout this licence is
allowed (a dealer's own first) is the arrangement a new box starts from, and
the customer rearranges it in Design Mode. A new public layout ships as a new
release: add an entry here AND a matching builder in `dashboard`'s
`_HOME_LAYOUTS` (keyed by the entry's `id`); a dealer layout is a spec in
`layouts/` published through Hub.

The tier + dealer gating logic is shared with the Cab Home catalog
(`cab_catalog.py`); both delegate to `layout_catalog.py`, passing their own
catalog list. The module-level `HOME_LAYOUTS_CATALOG` is the documented test
patch point.

Each entry: see `layout_catalog` for the `{id, name, tiers?, dealer?}` shape.
"""

from __future__ import annotations

from typing import Any

from . import layout_catalog as _lc

# Re-exported so existing importers (e.g. instincts_catalog, select.py,
# dashboard.py) keep importing the dealer/tier resolution from here.
from .layout_catalog import current_dealer, current_licence_tier  # noqa: F401

# Module-level catalog list. Loaded once at import; the documented test patch
# point (`patch.object(home_catalog, "HOME_LAYOUTS_CATALOG", ...)`). Every
# function below reads this global at call time, so a patch takes effect.
HOME_LAYOUTS_CATALOG: list[dict[str, Any]] = _lc.load_catalog("home_layouts_hab.json")


def effective_catalog(hass: Any | None = None) -> list[dict[str, Any]]:
    """The bundled catalog plus any dealer layouts fetched from Hub.

    Dealer layouts are served per box rather than shipped to every van, so the
    picker's real contents are "what we ship" plus "what this licence's dealer
    published". A fetched entry supersedes a bundled one with the same id, so a
    dealer layout still inside the integration can be replaced from Hub without
    waiting for a release.
    """
    return _lc.merged_catalog(HOME_LAYOUTS_CATALOG, hass, "hab")


def home_layout_spec(hass: Any | None, layout_id: str) -> dict[str, Any] | None:
    """The FETCHED spec for a Habitation layout id, or None when this box holds
    no fetched spec by that id (the caller then falls back to a bundled one)."""
    for spec in _lc.fetched_layouts(hass, "hab"):
        if spec.get("id") == layout_id:
            return spec
    return None


def allowed_home_names(tier: str | None, hass: Any | None = None) -> list[str]:
    """Catalog names available to this tier and dealer, in catalog order.

    Public (non-dealer) entries gated by `tiers`, plus the licence dealer's
    own entries per its `homepage_mode`. `hass` is optional for backward
    compatibility — omitted means tier-only (no dealer applied)."""
    return _lc.allowed_names(effective_catalog(hass), tier, hass)


def allowed_home_ids(tier: str | None, hass: Any | None = None) -> list[str]:
    """Catalog ids available to this tier and dealer, in catalog order.
    Mirror of `allowed_home_names`."""
    return _lc.allowed_ids(effective_catalog(hass), tier, hass)


def dealer_is_exclusive_empty(tier: str | None, hass: Any | None) -> bool:
    """True when this licence's dealer is `exclusive` but owns NO Habitation
    home layouts at this tier — a contradictory config. A non-dealer / additive
    / trial licence is never exclusive-empty, and neither is a dealer whose
    fetched bundle simply has not arrived yet."""
    return _lc.dealer_is_exclusive_empty(effective_catalog(hass), tier, hass)
