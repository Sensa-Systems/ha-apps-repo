"""Declarative home-layout specs.

A home layout used to be a Python builder in `dashboard/__init__.py`, one per
layout. That does not scale: dealer-owned layouts are already two thirds of the
layout code, `__init__.py` is the largest file in the repo by a wide margin, and
every box ships every dealer's layout so it can use exactly one.

Most layouts are not really code. They pick a shell and fill named slots from a
small set of SHARED card builders; the only genuine logic is the conditional and
fallback rules, and those are identical across layouts. So the arrangement moves
to DATA (a JSON spec) and the rules stay HERE, in Python.

That split is deliberate and load-bearing:

  * The spec names a builder; it never carries code. Names are resolved through
    an explicit allow-list (`_slot_builders`), an unknown name is dropped and
    logged rather than raised, and nothing is ever `eval`/`exec`ed. This matters
    because a spec is expected to arrive over the network from Hub later on, and
    a layout must never be able to execute anything on a customer's van.
  * The conditional rules (omit a slot whose builder resolves to nothing, lead
    with the welcome note when a fresh install has no content at all) live in one
    place instead of being copy-pasted into each new layout, which is how
    `_build_home_standard` and its three copies drifted apart in the first place.

Spec shape (see `layouts/*.json`):

    {
      "id": "blackpaw",
      "surface": "hab",
      "shell": "custom:sensa-view-home",
      "slots": {
        "left": "home_left_dials",
        "modes": {"builder": "home_modes_bar", "args": {"single_row": true}},
        "center": "home_center_power_flow",
        "center_controls": "home_controls_panel",
        "center_below": "home_solar_graph"
      },
      "placement": "hab"
    }

The slots describe the arrangement a box starts from: `_arrangeable_hab_home`
turns them into the Home tab's arrangeable grid and its `default_layout`, and
the customer rearranges it in Design Mode (unless the dealer is `locked`).

A slot is either a builder name, or `{"builder": ..., "args": {...}}` when the
builder takes keyword arguments. Omitting a slot omits it from the layout, which
is how Infinity Truck differs from Power Central (no `center_controls`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Bundled spec directory. Public layouts live here permanently; dealer-owned
# specs live here until Hub serves them per-box, at which point only the public
# ones stay bundled.
_LAYOUTS_DIR = Path(__file__).parent.parent / "layouts"

# The shell every Habitation Home layout renders into.
_HOME_SHELL = "custom:sensa-view-home"

# Slots omitted entirely when their builder resolves to nothing. `left` and
# `modes` are always present (their builders always return a card), and `center`
# is handled separately because it carries the welcome-note fallback.
_OPTIONAL_SLOTS = ("center_controls", "center_below")


def _slot_builders() -> dict[str, Callable[..., Any]]:
    """The allow-list mapping a spec's builder NAME to the real callable.

    Imported lazily: these live in `dashboard/__init__.py`, which imports this
    module, so a module-level import would be circular. Anything not in this
    dict cannot be referenced by a spec, which is the whole point.
    """
    from . import (
        _home_center_power_flow,
        _home_controls_panel,
        _home_left_dials,
        _home_modes_bar,
        _home_solar_graph,
    )

    return {
        "home_left_dials": _home_left_dials,
        "home_modes_bar": _home_modes_bar,
        "home_center_power_flow": _home_center_power_flow,
        "home_controls_panel": _home_controls_panel,
        "home_solar_graph": _home_solar_graph,
    }


def _cab_builders() -> dict[str, Callable[..., Any]]:
    """The Cab allow-list. A `column` builder returns a LIST of cards (it may
    return none), the `map` builder returns one card."""
    from . import _cab_blackpaw_telemetry_cards, _cab_placement_cards

    return {
        "cab_vehicle_telemetry": _cab_blackpaw_telemetry_cards,
        "cab_placed_cards": _cab_placement_cards,
        "cab_nav_map": _cab_nav_map,
    }


def _cab_nav_map(min_height: str | None = None, source_pills: bool = False,
                 theme_pill: bool = False, people_bar: bool = False) -> dict[str, Any]:
    """The Cab Home navigation map, filling its column."""
    from ..const import CAB_LOCATION_SELECT_ENTITY_ID
    from . import _nav_map_card_slot

    nav_map = _nav_map_card_slot(
        "select.sensa_nomad_cab_nav_source",
        "select.sensa_nomad_cab_location_source",
        "device_tracker.sensa_nomad_vehicle",
        aspect_ratio=None,
        min_height=min_height,
        source_pills=source_pills,
        theme_pill=theme_pill,
    )
    if people_bar:
        # "Who is driving" rides the map's own pill row.
        nav_map["people_bar"] = {
            "sensor": "sensor.sensa_nomad_people",
            "source_entity": CAB_LOCATION_SELECT_ENTITY_ID,
        }
    return nav_map


def _layout_path(layout_id: str, surface: str) -> Path:
    """Habitation specs sit in `layouts/`, Cab ones in `layouts/cab/`: an id is
    unique per surface, and one dealer may own the same id on both."""
    base = _LAYOUTS_DIR / "cab" if surface == "cab" else _LAYOUTS_DIR
    return base / f"{layout_id}.json"


def load_spec(layout_id: str, surface: str = "hab") -> dict[str, Any] | None:
    """Load a bundled layout spec by id, or None when there is no such file.

    Returns None (never raises) on a missing or malformed file so a bad spec
    degrades to "this layout does not exist" and the resolver falls back to the
    public layouts, rather than taking the Home tab down.
    """
    path = _layout_path(layout_id, surface)
    try:
        with path.open(encoding="utf-8") as f:
            spec: Any = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _LOGGER.warning("layout spec %s is unreadable or malformed; ignoring", path)
        return None
    if not isinstance(spec, dict):
        # Valid JSON of the wrong shape (a list, a string). Checked because a
        # spec is expected to arrive over the network later, where "parses" and
        # "is a spec" are very different things.
        _LOGGER.warning("layout spec %s is not an object; ignoring", path)
        return None
    if spec.get("surface", "hab") != surface:
        return None
    return spec


def resolve_spec(
    hass: Any | None, layout_id: str, surface: str = "hab",
) -> dict[str, Any] | None:
    """The spec for a layout id: the FETCHED one if this box holds it, else the
    bundled one, else None.

    Fetched wins because Hub is the fresher source: a dealer whose layout still
    ships inside the integration can update it from Hub without waiting for a
    release. Falling back to bundled is what keeps a box working offline and
    before its first check-in.
    """
    if hass is not None:
        from ..layout_catalog import fetched_layouts

        for fetched in fetched_layouts(hass, surface):
            if fetched.get("id") == layout_id:
                return fetched
    return load_spec(layout_id, surface)


def _build_slot(spec_slots: dict[str, Any], name: str,
                builders: dict[str, Callable[..., Any]]) -> Any:
    """Resolve one slot to its card, or None when absent/unknown/empty."""
    entry = spec_slots.get(name)
    if entry is None:
        return None
    if isinstance(entry, str):
        builder_name, args = entry, {}
    elif isinstance(entry, dict):
        builder_name, args = entry.get("builder"), entry.get("args") or {}
    else:
        _LOGGER.warning("layout slot %r has an unsupported shape; ignoring", name)
        return None
    fn = builders.get(builder_name or "")
    if fn is None:
        # Dropped, not raised: a spec naming a builder this build does not have
        # (an older box, a typo, a hostile payload) must lose that slot, not the
        # whole layout.
        _LOGGER.warning(
            "layout slot %r names unknown builder %r; ignoring that slot",
            name, builder_name,
        )
        return None
    try:
        return fn(**args)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "layout slot %r builder %r rejected its args; ignoring that slot",
            name, builder_name,
        )
        return None


CAB_SHELL = "cab-split"


def render_cab_layout(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Render a Cab Home spec, or None when it cannot be (wrong shell, no map),
    so the caller builds the Cab Home the way the box always has.

    Shape (`layouts/cab/*.json`):

        {"id": "blackpaw", "surface": "cab", "shell": "cab-split",
         "columns": "35fr 65fr",
         "slots": {"column": ["cab_vehicle_telemetry", "cab_placed_cards"],
                   "map": {"builder": "cab_nav_map", "args": {...}}}}

    The column's cards stack beside the map at `columns`. The rules stay here,
    exactly as `_build_cab_blackpaw` had them: with nothing for the column the
    map takes the whole tab, and a column holding only the GPS speedometer
    narrows to give the map more room.
    """
    from . import (
        _BLACKPAW_GPS_ONLY_COLUMNS,
        _BLACKPAW_GPS_ONLY_DIAL,
        _is_gps_speedo_only,
    )

    if spec.get("shell") != CAB_SHELL:
        _LOGGER.warning("cab layout %r has unknown shell %r; ignoring it",
                        spec.get("id"), spec.get("shell"))
        return None
    builders = _cab_builders()
    slots = spec.get("slots") or {}
    nav_map = _build_slot(slots, "map", builders)
    if not isinstance(nav_map, dict):
        _LOGGER.warning("cab layout %r has no map; ignoring it", spec.get("id"))
        return None
    column = slots.get("column") or []
    if not isinstance(column, list):
        column = [column]
    left_cards: list[dict[str, Any]] = []
    for i, entry in enumerate(column):
        cards = _build_slot({str(i): entry}, str(i), builders)
        if isinstance(cards, list):
            left_cards.extend(c for c in cards if isinstance(c, dict))
        elif isinstance(cards, dict):
            left_cards.append(cards)
    if not left_cards:
        return nav_map
    columns = str(spec.get("columns") or "35fr 65fr")
    if _is_gps_speedo_only(left_cards):
        columns = _BLACKPAW_GPS_ONLY_COLUMNS
        left_cards[0]["dial_max_width"] = _BLACKPAW_GPS_ONLY_DIAL
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "full_height": True,
        "columns": columns,
        "cards": [
            {"type": "custom:sensa-card-grid", "hide_header": True, "stack": True,
             "cards": left_cards},
            nav_map,
        ],
    }


def render_home_layout(spec: dict[str, Any]) -> dict[str, Any]:
    """Render a Habitation Home layout spec into a `sensa-view-home` config.

    Reproduces exactly what the hand-written builders did: the dials and mode bar
    are always present, the power-flow hero is conditional (with the fresh-install
    welcome note as its fallback when there is no content at all), the controls
    panel and solar chart are omitted when they resolve to nothing, and the
    per-device column placement is applied last.
    """
    from . import (
        _home_has_content,
        _home_placement_columns,
        _home_welcome_note,
        _place_hab_home_columns,
    )

    builders = _slot_builders()
    slots = spec.get("slots") or {}

    left = _build_slot(slots, "left", builders)
    # The van-level gyro is no longer auto-placed (2026-08-07) — it is a
    # Home-layout kind now and arrives through `columns` below, so a dealer
    # spec gets the same opt-in behaviour as the shipped layouts with no
    # per-spec change.
    # Resolved up-front so the welcome-note guard can count them: a placed card
    # IS content, and "nothing here yet" must never render beside a real card.
    columns = _home_placement_columns(spec.get("placement", "hab"))
    all_placed = columns["left"] + columns["middle"] + columns["right"]

    card: dict[str, Any] = {
        "type": spec.get("shell", _HOME_SHELL),
        "hide_header": True,
    }
    # `left` and `modes` are present in every shipped spec, but a spec that
    # omits one must leave the slot OUT rather than set it to None: the shell
    # would try to render the null and take the tab down with it.
    for name, value in (("left", left), ("modes", _build_slot(slots, "modes", builders))):
        if value is not None:
            card[name] = value

    power_flow = _build_slot(slots, "center", builders)
    if power_flow is not None:
        card["center"] = power_flow
    elif not _home_has_content(power_flow, left, *all_placed):
        # No content cards at all, so only the always-present modes bar would
        # show. Lead with a welcome message rather than a blank fresh install.
        card["center"] = _home_welcome_note()

    for name in _OPTIONAL_SLOTS:
        value = _build_slot(slots, name, builders)
        if value is not None:
            card[name] = value

    _place_hab_home_columns(card, None, columns)
    return card
