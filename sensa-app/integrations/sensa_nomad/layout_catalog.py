"""Shared layout-catalog logic for the customer-selectable dashboard surfaces.

Two surfaces let the customer pick between multiple layouts: the Habitation
**Home** tab (`home_catalog.py` → `home_layouts_hab.json`) and the Cab **Home**
tab (`cab_catalog.py` → `home_layouts_cab.json`). Both load an ordered list of
catalog entries and apply the SAME tier + dealer gating — that gating lives here
as pure functions taking the catalog list as their first argument, so neither
surface duplicates it.

Each catalog entry:
  {
    "id":    stable slug — must match a layout builder key in dashboard.py,
    "name":  staff-facing display name — shown in the layout dropdown and stored
             as the select entity's state. Must be unique within a catalog.
    "tiers": optional licence-tier allow-list (e.g. ["lite", "plus", "vision"]);
             omitted → available in every tier.
    "dealer": optional Hub dealer slug — makes the entry private to that dealer
             (never public). See the dealer gating notes below.
  }

The dealer claim (`current_dealer`) and licence tier (`current_licence_tier`)
are read offline from the installed key and are SHARED across both surfaces —
one dealer, one tier, applied to each catalog's own entries. A dealer's
`homepage_mode` (`additive` / `exclusive` / `locked`) governs BOTH surfaces'
dealer pages. `locked` offers what `exclusive` does and also fixes the design of
every page (`design_locked`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Sensa's own internal/testing dealer claim. A licence carrying this dealer id
# is treated as a preview account: the layout picker surfaces EVERY layout
# (public + every dealer's), still tier-gated, so any dealer homepage can be
# checked without swapping licences. A real customer never carries it.
SENSA_DEALER_ID = "sensa"


def load_catalog(filename: str) -> list[dict[str, Any]]:
    """Load and validate a bundled layout catalog JSON living beside this
    module. Warns (loudly, not silently) on duplicate display names — the
    select state IS the name, so it must map back to exactly one id."""
    path = Path(__file__).with_name(filename)
    with path.open(encoding="utf-8") as f:
        catalog: list[dict[str, Any]] = json.load(f)

    names = [e.get("name") for e in catalog]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        _LOGGER.warning(
            "%s: duplicate layout name(s) %s — a layout dropdown maps a name "
            "back to one id, so names must be unique",
            filename, sorted(dupes),
        )
    return catalog


def names(catalog: list[dict[str, Any]]) -> list[str]:
    """Staff display names, in catalog order — the dropdown's options."""
    return [e["name"] for e in catalog if e.get("name")]


def id_for_name(catalog: list[dict[str, Any]], name: str) -> str | None:
    """Map a selected display name back to its stable layout id."""
    for e in catalog:
        if e.get("name") == name:
            return e.get("id")
    return None


def default_name(catalog: list[dict[str, Any]]) -> str:
    """Display name of the first catalog entry — the fresh-install default."""
    n = names(catalog)
    return n[0] if n else ""


# -- Tier + dealer gating ----------------------------------------------------
# A layout is available to a customer when its `tiers` includes the licence tier
# (or it lists no tiers → all tiers). A `dealer`-tagged entry is NEVER public —
# it appears only for a licence whose `dealer` claim id matches, and the
# dealer's `homepage_mode` decides whether the dealer's pages REPLACE the public
# set (`exclusive`, `locked`) or ADD to it (`additive`). Trials never see dealer
# pages.

# The homepage_mode values whose dealer pages replace the public set.
_REPLACING_MODES = frozenset({"exclusive", "locked"})


def design_locked(hass: Any) -> bool:
    """True when the licence's dealer fixes the design of every page
    (`homepage_mode: locked`): no Design Mode, no arranging, no tab reorder.

    A dealer who supplies the hardware knows the screen size and may force its
    layouts; one who only installs leaves its layout as the default the
    customer can rearrange. A trial carries no dealer (`current_dealer`), and
    an older box that has never heard of `locked` treats it as additive, so
    both fail open to "not locked".
    """
    dealer = current_dealer(hass)
    return bool(dealer) and dealer.get("homepage_mode") == "locked"


def _entry_in_tier(entry: dict[str, Any], tier: str | None) -> bool:
    from .const import normalise_tier

    tiers = entry.get("tiers")
    if not tiers:  # unspecified → available everywhere
        return True
    # Normalise BOTH sides: the licence claim may carry a retired identifier
    # (`lite`/`plus` — signed keys are never re-issued), and so may a catalog
    # entry authored before the rename.
    return normalise_tier(tier) in {normalise_tier(t) for t in tiers}


def _is_public(entry: dict[str, Any], tier: str | None) -> bool:
    """Public = no `dealer` tag AND passes tier gating. Dealer-tagged entries
    are excluded here so they can never leak to a non-matching customer — they
    are added back only by the dealer branch."""
    return not entry.get("dealer") and _entry_in_tier(entry, tier)


def current_dealer(hass: Any) -> dict[str, Any] | None:
    """The active Nomad dealer claim, read offline from the runtime licence
    status (`{"id": <slug>, "homepage_mode": "additive"|"exclusive"}`).

    Returns None when there is no dealer claim, the licence hasn't loaded yet,
    OR the licence is a **trial** — trials (issued as full Vision) never show
    dealer-private layouts, only the public set.
    """
    from .const import DOMAIN, RUNTIME_LICENCE_STATUS

    data = getattr(hass, "data", None) or {}
    for runtime in (data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        status = runtime.get(RUNTIME_LICENCE_STATUS) or {}
        if status.get("trial"):
            return None
        dealer = status.get("dealer")
        if isinstance(dealer, dict) and dealer.get("id"):
            return dealer
    return None


def licence_tier_or_none(hass: Any) -> str | None:
    """The active Nomad licence tier, read offline from the runtime licence
    status (`editions["nomad"]`), normalised so a licence signed with a retired
    identifier (`lite`/`plus`) resolves to the tier its holder paid for. None
    when no tier is resolved yet (hass-less build, licence not loaded, or a
    malformed status): "could not ask", which each caller decides about."""
    from .const import DOMAIN, RUNTIME_LICENCE_STATUS, normalise_tier

    data = getattr(hass, "data", None) or {}
    for runtime in (data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        editions = (runtime.get(RUNTIME_LICENCE_STATUS) or {}).get("editions") or {}
        tier = editions.get("nomad")
        if tier:
            return normalise_tier(tier)
    return None


def current_licence_tier(hass: Any) -> str:
    """The active Nomad licence tier, or Base when none is resolved yet: the
    most-restrictive default, right for catalog filtering."""
    from .const import TIER_BASE

    return licence_tier_or_none(hass) or TIER_BASE


def _dealer_entries(
    catalog: list[dict[str, Any]], key: str, tier: str | None, dealer_id: str,
) -> list[str]:
    """Entries owned by `dealer_id` (matched on the `dealer` tag), still
    honouring `tiers` when the entry also lists them."""
    return [
        e[key]
        for e in catalog
        if e.get(key)
        and e.get("dealer") == dealer_id
        and _entry_in_tier(e, tier)
    ]


def fetched_layouts(hass: Any | None, surface: str) -> list[dict[str, Any]]:
    """The dealer layout specs this box fetched from Hub, for one surface.

    Dealer layouts are served per box rather than bundled, so a van carries only
    its own dealer's and a layout change reaches that dealer's boxes on the next
    phone-home. They arrive on the licence status (the App caches the bundle and
    reports it), so this is a read of what has already been downloaded, never a
    fetch.

    Returns an EMPTY list for every "we do not have them" case: no hass, no
    licence status yet, a box that has never checked in, a dealer who publishes
    none, or a Hub outage. Callers must treat empty as "serve the public
    layouts" and never as "show nothing", which is what keeps a fetch failure
    from blanking a customer's Home tab.
    """
    from .const import DOMAIN, RUNTIME_LICENCE_STATUS

    data = getattr(hass, "data", None) or {}
    for runtime in (data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        status = runtime.get(RUNTIME_LICENCE_STATUS) or {}
        specs = status.get("dealer_layouts")
        if not isinstance(specs, list):
            continue
        return [
            spec for spec in specs
            if isinstance(spec, dict) and spec.get("surface", "hab") == surface
        ]
    return []


def merged_catalog(
    catalog: list[dict[str, Any]], hass: Any | None, surface: str,
) -> list[dict[str, Any]]:
    """The bundled catalog plus any fetched dealer entries for this surface.

    A fetched entry WINS over a bundled one with the same id, so a dealer layout
    still shipping inside the integration is superseded by the Hub copy without
    needing a release to remove it. Fetched entries lead, matching the ordering
    the dealer branch already uses (a dealer's own pages first).
    """
    fetched = fetched_layouts(hass, surface)
    if not fetched:
        return catalog
    # A fetched layout is dealer-owned BY DEFINITION: it came from this
    # licence's dealer bundle. The owner is therefore taken from the dealer
    # CLAIM, never from a `dealer` field in the payload. Reading it off the spec
    # would let a bundle that simply omits the field register as a PUBLIC
    # layout, which every customer would then be offered.
    dealer = current_dealer(hass) if hass is not None else None
    if not dealer or not dealer.get("id"):
        return catalog
    entries = [
        {
            "id": spec.get("id"),
            "name": spec.get("name"),
            "dealer": dealer["id"],
            **({"tiers": spec["tiers"]} if isinstance(spec.get("tiers"), list) else {}),
        }
        for spec in fetched
        if spec.get("id") and spec.get("name")
    ]
    superseded = {e["id"] for e in entries}
    return entries + [e for e in catalog if e.get("id") not in superseded]


def _allowed(
    catalog: list[dict[str, Any]], key: str, tier: str | None, hass: Any | None,
) -> list[str]:
    public = [e[key] for e in catalog if e.get(key) and _is_public(e, tier)]
    dealer = current_dealer(hass) if hass is not None else None
    if not dealer:
        return public
    if dealer["id"] == SENSA_DEALER_ID:
        # Internal Sensa preview account — surface EVERY layout (public + all
        # dealers'), tier-gated, in catalog order. Lets us check any dealer
        # homepage without a per-dealer licence.
        return [e[key] for e in catalog if e.get(key) and _entry_in_tier(e, tier)]
    owned = _dealer_entries(catalog, key, tier, dealer["id"])
    if dealer.get("homepage_mode") in _REPLACING_MODES:
        return owned
    # Additive: the dealer's own pages lead the list (it's the one their
    # customers will use most), with the public pages after them. The dropdown
    # order follows this, and the select's default (`current_option` →
    # `allowed[0]`) lands on the dealer page for a fresh dealer install.
    return owned + public


def allowed_names(
    catalog: list[dict[str, Any]], tier: str | None, hass: Any | None = None,
) -> list[str]:
    """Catalog names available to this tier and dealer, in catalog order."""
    return _allowed(catalog, "name", tier, hass)


def allowed_ids(
    catalog: list[dict[str, Any]], tier: str | None, hass: Any | None = None,
) -> list[str]:
    """Catalog ids available to this tier and dealer, in catalog order.
    Mirror of `allowed_names` (1:1 with it via `id_for_name`)."""
    return _allowed(catalog, "id", tier, hass)


def dealer_is_exclusive_empty(
    catalog: list[dict[str, Any]], tier: str | None, hass: Any | None,
) -> bool:
    """True when this licence's dealer is `exclusive` but owns NO layouts at
    this tier in this catalog — a contradictory config. The resolver uses this
    to leave the layout blank and log a one-off warning, rather than silently
    falling back to a public page.

    Now that dealer layouts are FETCHED, `catalog` here is the MERGED one, so a
    dealer who publishes layouts from Hub is not empty once their bundle has
    landed. Two notes on why this still keys on "owns nothing" rather than on
    "we hold a bundle":

    * A transient failure cannot empty it. `dealer_layouts.sync` keeps the
      existing cache on any network error or non-200, so an outage leaves the
      previously fetched layouts in place. Only an explicit 404 (the dealer
      withdrew them) clears the cache, and that IS a definitive answer.
    * An unprimed box never reaches here: with no licence status there is no
      dealer claim, so `current_dealer` returns None and this returns False.

    Known gap: a dealer box whose VERY FIRST bundle fetch fails, before anything
    has ever been cached, is indistinguishable from a dealer who publishes
    nothing, and gets the blank Home plus its log line until the next successful
    phone-home. Narrow, self-healing and explained to the customer, so it is
    deliberately not papered over by falling back to a public page, which would
    quietly break the "exclusive" promise instead.
    """
    dealer = current_dealer(hass) if hass is not None else None
    if not dealer or dealer.get("homepage_mode") not in _REPLACING_MODES:
        return False
    if dealer["id"] == SENSA_DEALER_ID:
        # The Sensa preview account sees every layout, so it's never empty.
        return False
    return not _dealer_entries(catalog, "id", tier, dealer["id"])
