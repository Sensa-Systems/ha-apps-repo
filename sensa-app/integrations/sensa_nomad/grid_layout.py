"""The customer's own card layout, per tab.

In Design Mode a `sensa-card-grid` tab shows a 12-column grid, and each card
can be dragged to a cell and resized by whole cells. The layout is box-wide,
not per-screen: it is kept here and handed to the grid by
`dashboard.build_panel_config` (as `layout_id` + `layout`), so every screen
showing that tab agrees and it survives restarts and upgrades.

A layout maps a card key to `[col, row, w, h]` (plus optional flags):
zero-based column and row, width in columns, height in rows. Rows are fine and
a card's height comes from its content, measured by the grid. A card with no
entry (one added since the save) is placed by the grid after the saved ones,
so nothing is ever hidden by a stale layout.

Card keys are worked out HERE (`card_keys`) and stamped on each card as
`layout_key`; the grid never derives one, so the key the layout is saved under
and the key it is read back under cannot disagree.

A card from another tab is added by its REFERENCE, "<source layout id>::<key>",
never a copy of its config: `apply_grid_layouts` looks the source up in the
catalog of every grid tab on the box (`grid_catalog`) each time the panel is
built, so the card stays as live as the tab it came from, and one whose source
has gone is simply not shown.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORE_KEY = "sensa_nomad.grid_layout"
STORE_VERSION = 1

# The grid's column count. The one copy: it goes to the grid on the wire as
# `layout_columns`, so the frontend never holds a second number.
GRID_COLUMNS = 12
# Rows are fine (8 px each, webcards/src/shared/grid-layout.ts ROW_PX) and a
# card spans as many as its content needs, so these are generous.
MAX_ROWS = 5000
MAX_ROW_SPAN = 1000

# Place flags (the optional fifth element): a height the customer set, and a
# card the customer took off the tab.
SIZED = 1
REMOVED = 2
# A card that reaches the bottom of the screen (the Cab Home map); its height
# is worked out on the screen, never stored.
FILL = 4
# Fill the spare space to the right / below; worked out on the screen.
REST_W = 8
REST_H = 16
# Scale the card's content to its cell; set with every height Design Mode gives
# from 1.8.2, so a 1.6.x saved height keeps the card at its natural size.
FIT = 32
# Scale it as 1.7.x did: the content fitted to the cell. Put once on every
# saved height of a box that ran 1.7 (`async_migrate_scaling`), so its
# dashboard looks exactly as it did on 1.7.21.
LEGACY_FIT = 64
# Sits on the bottom of the screen (the Habitation Home scene bar, and the
# cards under the power flow, stacked up from it); worked out on the screen.
BOTTOM = 128
# The flags that give a card a height of its own.
HEIGHT_FLAGS = SIZED | FILL | REST_H
# Between a source layout id and a card key in an added card's reference.
REF_SEP = "::"
# Dashboards whose tabs Design Mode never arranges: each Intelligence tab is
# one working card (Scenes, Instincts, Routines, Log, Schengen), not a layout.
# The Instincts tiles still reorder INSIDE their card (instinct_order.py).
_FIXED_DASHBOARDS = frozenset({"intelligence"})
# The card lists a grid draws, in the order it draws them.
_CARD_LISTS = ("routers", "dishes", "cards")

# "<dashboard>/<tab slug>" -> card key -> [col, row, w, h]. Module-level for the
# same reason as tab_order._orders: `build_panel_config` has no hass.
_layouts: dict[str, dict[str, list[int]]] = {}

# The layout spec a tab was last built from this run ("<dashboard>/<slug>" ->
# spec), and, persisted, the spec a tab the customer has arranged keeps. A
# customer's arrangement only places cards; the CARDS come from the spec. So a
# new dealer spec published to Hub must never reach a tab the customer has
# already arranged (Richard, 2026-11-07: "we do not change customers home
# layouts"): the spec is pinned the moment they first save, and only a tab with
# no saved arrangement follows the newest spec.
_built_specs: dict[str, dict[str, Any]] = {}
_pinned_specs: dict[str, dict[str, Any]] = {}


def note_built_spec(lid: str, spec: dict[str, Any] | None) -> None:
    """Record the spec `lid` was just built from (None: a Python builder)."""
    if spec is None:
        _built_specs.pop(lid, None)
    else:
        _built_specs[lid] = spec


def has_saved_layout(lid: str) -> bool:
    """Whether the customer has arranged this tab in Design Mode."""
    return bool(_layouts.get(lid))


def pinned_spec(lid: str) -> dict[str, Any] | None:
    """The spec an arranged tab keeps, or None (arranged before pinning
    existed, or built from a Python builder): build it the way the box
    always has."""
    return _pinned_specs.get(lid)


def layout_id(dashboard_key: str, slug: str) -> str:
    return f"{dashboard_key}/{slug}"


def _is_arrangeable(card: Any) -> bool:
    """A plain auto-fit `sensa-card-grid`, or one whose builder set
    `arrangeable` (Cab Home, Cab Parking). Other grids with an explicit
    `columns` split or a `stack` are fixed designs and are left alone, and so
    is a grid set `arrangeable: False` (the Settings pages that are one
    whole-page card or a support page: Images, People, Backup, System,
    Diagnostics, System Log)."""
    if not isinstance(card, dict) or card.get("type") != "custom:sensa-card-grid":
        return False
    if card.get("arrangeable") is False:
        return False
    if card.get("arrangeable"):
        return True
    return not card.get("columns") and not card.get("stack")


def card_keys(cards: list[dict[str, Any]]) -> list[str]:
    """A stable key per card: `reorder_key` when the builder set one, else its
    type and entity or device. Duplicates get `#2`, `#3`, so two cards of one
    kind with no entity still keep separate places."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for cfg in cards:
        def pick(k: str, cfg: Any = cfg) -> str:
            v = cfg.get(k) if isinstance(cfg, dict) else None
            return v if isinstance(v, str) else ""

        ident = pick("entity") or pick("entity_id") or pick("device_id") or pick("device")
        base = pick("reorder_key") or "|".join(p for p in (pick("type"), ident) if p) or "card"
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append(base if n == 1 else f"{base}#{n}")
    return out


def _all_cards(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for name in _CARD_LISTS for c in (card.get(name) or []) if isinstance(c, dict)]


def grid_catalog(dashboards: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Every arrangeable grid tab on the box: layout id -> dashboard, slug,
    label and its cards by key. `dashboards` maps a dashboard key to the tabs
    its builder produced (before any layout is applied, so an added card is
    never offered as a source)."""
    out: dict[str, dict[str, Any]] = {}
    for dashboard_key, tabs in dashboards.items():
        if dashboard_key in _FIXED_DASHBOARDS:
            continue
        for tab in tabs or []:
            card = tab.get("card")
            slug = tab.get("slug")
            if not _is_arrangeable(card) or not isinstance(slug, str):
                continue
            cards = _all_cards(card)
            out[layout_id(dashboard_key, slug)] = {
                "dashboard": dashboard_key,
                "slug": slug,
                "label": tab.get("label") or slug,
                "cards": dict(zip(card_keys(cards), cards, strict=True)),
            }
    return out


# A card whose key changed and was then changed back. 1.7.x keyed the Home
# dials and the History graphs by their new type names; 1.6.x and 1.8.2 on key
# them by the old names (`reorder_key`). Key as 1.7 read it -> key now.
_RENAMED_KEYS = {
    "custom:sensa-gauge-basic": "custom:sensa-gauge-home",
    "custom:sensa-electrical-graphs": "custom:sensa-advanced-graph",
}
# The Settings card gallery's own gauge card is keyed custom:sensa-gauge-basic.
_RENAME_EXEMPT = "settings/cards"
# Renamed 2026-11-02: a place saved under the old name keeps the card where it is.
_CARDS_RENAMED = (
    ("custom:sensa-network-card", "custom:sensa-router-card"),
    ("custom:sensa-wan-card", "custom:sensa-wan-priority-card"),
)


def _current_key(key: str) -> str:
    """A saved layout key under the card's current name."""
    for old, new in _CARDS_RENAMED:
        key = key.replace(old, new)
    return key


def _as_1_7_read_it(lid: str, layout: dict[str, Any]) -> dict[str, Any]:
    """`layout` keyed as 1.8.2 keys it, holding exactly what 1.7 showed. 1.7
    read the renamed cards under their new names, so a place saved under the
    old name (left from 1.6) was never read: it is dropped, and the 1.7 place,
    or none (the card sat after the saved ones), is what stays."""
    if lid == _RENAME_EXEMPT:
        return layout

    def split(k: str) -> tuple[str, str]:
        src, sep, key = k.rpartition(REF_SEP)
        return (src + sep, key) if sep else ("", k)

    out = {
        k: p for k, p in layout.items()
        if split(k)[1] not in _RENAMED_KEYS.values() or split(k)[0] == _RENAME_EXEMPT + REF_SEP
    }
    for k in list(out):
        prefix, key = split(k)
        if key in _RENAMED_KEYS and prefix != _RENAME_EXEMPT + REF_SEP:
            out[prefix + _RENAMED_KEYS[key]] = out.pop(k)
    return out


def apply_grid_layouts(
    dashboard_key: str,
    tabs: list[dict[str, Any]],
    sources: Callable[[], dict[str, dict[str, Any]]] | None = None,
    locked: bool = False,
) -> list[dict[str, Any]]:
    """Give every arrangeable grid tab its `layout_id` (which turns Design Mode
    arranging on), each card its `layout_key`, its saved layout, and the cards
    added to it from other tabs. `sources` builds the catalog (`grid_catalog`)
    and is only called when a tab has an added card.

    `locked` (the dealer fixes every page, `layout_catalog.design_locked`)
    keeps the ids and keys, so a tab still draws its `default_layout`, but
    ignores what the customer saved. The saved layout stays in the store, so
    it comes back if the dealer unlocks."""
    if dashboard_key in _FIXED_DASHBOARDS:
        return tabs
    catalog: dict[str, dict[str, Any]] | None = None
    out: list[dict[str, Any]] = []
    for tab in tabs:
        card = tab.get("card")
        slug = tab.get("slug")
        if _is_arrangeable(card) and isinstance(slug, str):
            lid = layout_id(dashboard_key, slug)
            card = {**card, "layout_id": lid, "layout_columns": GRID_COLUMNS}
            keys = iter(card_keys(_all_cards(card)))
            for name in _CARD_LISTS:
                if card.get(name):
                    card[name] = [
                        {**c, "layout_key": next(keys)} if isinstance(c, dict) else c
                        for c in card[name]
                    ]
            saved = {} if locked else _layouts.get(lid) or {}
            refs = [
                k for k, p in saved.items()
                if REF_SEP in k and not (len(p) == 5 and p[4] & REMOVED)
            ]
            if refs and sources is not None:
                if catalog is None:
                    catalog = sources()
                added = []
                for ref in refs:
                    src_lid, _, src_key = ref.partition(REF_SEP)
                    cfg = catalog.get(src_lid, {}).get("cards", {}).get(src_key)
                    if isinstance(cfg, dict):
                        added.append({**cfg, "layout_key": ref})
                kept = card.get("cards") or []
                if added:
                    # The welcome note is only for an empty Home: gone as soon
                    # as the customer adds a card.
                    kept = [c for c in kept if not (isinstance(c, dict) and c.get("welcome"))]
                card["cards"] = [*kept, *added]
            if saved:
                card["layout"] = {k: list(v) for k, v in saved.items()}
            if _legacy_box and isinstance(card.get("default_layout"), dict):
                # The built heights (Cab Home's climate summary) scaled on 1.7.
                card["default_layout"] = {
                    k: _as_legacy(p) for k, p in card["default_layout"].items()
                }
            tab = {**tab, "card": card}
        out.append(tab)
    return out


def clean_layout(raw: Any) -> dict[str, list[int]]:
    """Keep only well-formed placements, clamped into the grid."""
    out: dict[str, list[int]] = {}
    if not isinstance(raw, dict):
        return out
    for key, place in raw.items():
        if not isinstance(key, str) or not key or not isinstance(place, (list, tuple)):
            continue
        if len(place) not in (4, 5) or not all(
            isinstance(n, int) and not isinstance(n, bool) for n in place
        ):
            continue
        col, row, w, h = place[:4]
        # A fifth element carries flags (SIZED, REMOVED); anything else is dropped.
        flags = (
            place[4] & (SIZED | REMOVED | FILL | REST_W | REST_H | FIT | LEGACY_FIT | BOTTOM)
            if len(place) == 5 and place[4] > 0 else 0
        )
        w = min(max(w, 1), GRID_COLUMNS)
        col = min(max(col, 0), GRID_COLUMNS - w)
        row = min(max(row, 0), MAX_ROWS - 1)
        h = min(max(h, 1), MAX_ROW_SPAN)
        out[key] = [col, row, w, h, flags] if flags else [col, row, w, h]
    return out


def _as_legacy(place: Any) -> Any:
    """A height with no scaling rule of its own, marked to scale as 1.7 did."""
    if not isinstance(place, (list, tuple)) or len(place) != 5:
        return place
    flags = place[4]
    if not isinstance(flags, int) or not flags & HEIGHT_FLAGS or flags & (FIT | LEGACY_FIT):
        return place
    return [*place[:4], flags | LEGACY_FIT]


# This box ran 1.7, so its built default heights scale as 1.7 did. Set by
# `GridLayoutStore.async_migrate_scaling`.
_legacy_box = False


def ran_1_7(hass: HomeAssistant, entry_id: str) -> bool:
    """Whether this box ran 1.7 before. The data usage sensor is made on
    every box from 1.7.1 and the entity registry keeps it across restarts,
    so before this run's platforms set up it is there only if 1.7 made it."""
    from homeassistant.helpers import entity_registry as er

    return er.async_get(hass).async_get_entity_id(
        "sensor", "sensa_nomad", f"{entry_id}_data_usage",
    ) is not None


class GridLayoutStore:
    """Persists `_layouts` across restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        # Which release's scaling this box's saved heights were made under,
        # decided once on its first 1.8.2 start: "1.6" or "1.7".
        self._era: str | None = None

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        _layouts.clear()
        if not isinstance(loaded, dict):
            return
        era = loaded.get("scaling")
        self._era = era if era in ("1.6", "1.7") else None
        for lid, raw in (loaded.get("layouts") or {}).items():
            clean = clean_layout(raw)
            if isinstance(lid, str) and clean:
                _layouts[lid] = {_current_key(k): p for k, p in clean.items()}
        _pinned_specs.clear()
        for lid, spec in (loaded.get("pinned_specs") or {}).items():
            if isinstance(lid, str) and isinstance(spec, dict) and lid in _layouts:
                _pinned_specs[lid] = spec

    async def async_migrate_scaling(self, ran_1_7_before: bool) -> None:
        """Keep every dashboard as it looked before 1.8.2. Card scaling came
        in 1.7 (fitted to the content) and changed in 1.8 (fitted to the
        cell); a height saved on 1.6 must keep the card at its natural size
        and one saved on 1.7 must keep 1.7's scaling. Decided once and stored,
        so a later restart (when the data usage sensor exists everywhere)
        does not decide again."""
        global _legacy_box
        if self._era is None:
            self._era = "1.7" if ran_1_7_before else "1.6"
            if self._era == "1.7":
                for lid, layout in list(_layouts.items()):
                    _layouts[lid] = {
                        k: _as_legacy(p) for k, p in _as_1_7_read_it(lid, layout).items()
                    }
            await self._save()
        _legacy_box = self._era == "1.7"

    async def _save(self) -> None:
        await self._store.async_save({
            "scaling": self._era,
            "layouts": {k: dict(v) for k, v in _layouts.items()},
            "pinned_specs": dict(_pinned_specs),
        })

    async def async_set(self, lid: str, raw: Any) -> bool:
        """Save one tab's layout. Empty resets it to the built layout.
        Returns whether anything changed.

        The first save pins the spec the tab was built from, so a newer spec
        never changes an arranged tab. A reset unpins it: the tab is the
        dealer's again and follows the newest spec."""
        clean = clean_layout(raw)
        if clean == _layouts.get(lid, {}):
            return False
        if clean:
            if lid not in _layouts and lid in _built_specs:
                _pinned_specs[lid] = _built_specs[lid]
            _layouts[lid] = clean
        else:
            _layouts.pop(lid, None)
            _pinned_specs.pop(lid, None)
        await self._save()
        return True


async def async_get_store(hass: HomeAssistant) -> GridLayoutStore:
    """The one loaded store for this run, loading it on first use."""
    data = hass.data.setdefault("sensa_nomad", {})
    store = data.get("grid_layout_store")
    if store is None:
        store = GridLayoutStore(hass)
        await store.async_load()
        data["grid_layout_store"] = store
    return store
