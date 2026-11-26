"""The Cab dashboard: the driving surface (overview, navigation, engine, levelling).

Split out of the old monolithic `dashboard.py`. The card helpers it calls stay in
the package root and are imported per call (see the note in each builder).
"""
from __future__ import annotations

import re
from typing import Any


def _build_cab_yaml_data() -> dict[str, Any]:
    """Return the managed Nomad Cab dashboard YAML structure.

    Overview view gains a navigation selector + three sibling
    `conditional` cards. The select entity drives which one renders;
    HA swaps them instantly on state change with no reload.
    """
    # Late-bound on purpose. These live in the package root and are resolved
    # per CALL, exactly as they were when this was all one module: the tests
    # patch them on the package (`patch.object(dashboard, ...)`), and
    # `_dashboard_devices` is REBOUND by `set_dashboard_devices`. A module-level
    # import here would freeze the originals and silently ignore both.
    from . import (
        _CAB_NAV_MIN_HEIGHT,
        _LOGGER,
        _SENSA_DEFAULT_LOGO,
        _build_cab_levelling_card,
        _build_cab_levelling_gyro_card,
        _build_cab_levelling_tilt_card,
        _build_cab_levelling_topdown_card,
        _build_level_roll_card,
        _build_levelling_cards,
        _build_network_signals_card,
        _current_deployment_mode,
        _dashboard_devices,
        _dealer_logo_url,
        _habitation_chips,
        _is_obd2_device,
        _nav_map_card_slot,
        _note_card,
        _obd2_engine_cards,
        _resolve_active_cab_layout,
        _tab_error_card,
        build_tyre_cards,
    )

    nav_select_entity = "select.sensa_nomad_cab_nav_source"
    location_select_entity = "select.sensa_nomad_cab_location_source"
    vehicle_tracker = "device_tracker.sensa_nomad_vehicle"

    # The map slot: a native `sensa-nav-map` picking the map card (HA Map /
    # Google Maps / Sensa Radar) from the Navigation dropdown. HA Map resolves
    # the location device PER-SCREEN (defaulting to the viewing phone, else the
    # vehicle); the two iframe variants centre on the vehicle tracker's live
    # coords.
    #
    # FILL, not 16:9 (Richard, 2026-07-20). The map is the only thing on this
    # tab, so an aspect box sized the frame to 16:9 of its width and left the
    # remainder of the tab empty below it, 94px on a 1152px-wide Cab screen,
    # and worse the wider the display. Fill mode makes it take the tab's full
    # height, matching Mobile Navigation and the BlackPaw home. `min_height`
    # floors it so a short viewport can't collapse it to a strip.
    nav_card_slot = _nav_map_card_slot(
        nav_select_entity,
        location_select_entity,
        vehicle_tracker,
        aspect_ratio=None,
        min_height=_CAB_NAV_MIN_HEIGHT,
        # The map IS this tab, so its controls read better in a column beside it
        # than in a strip underneath: category pills (Fuel / Toilets) at the top,
        # then the driver faces, then the Radar / Google / HA Map picker pinned
        # to the bottom. (The card's OWN toolbar is already hidden by
        # `_nav_map_card_slot`; the dropdowns this tab used to show came from
        # the panel's tab strip, removed above.)
        source_pills=True,
        pills_side=True,
        # The dark/light map pill, as on Cab Home (Richard, 2026-10-09).
        theme_pill=True,
    )
    # The same "who is driving" faces the Cab Home layout carries, on this tab
    # too -- one tap records the driver and switches their MySensa session, so
    # the map's nearby-amenity results are theirs.
    nav_card_slot["people_bar"] = {
        "sensor": "sensor.sensa_nomad_people",
        "source_entity": location_select_entity,
    }

    # May 2026: Cab adopted the dashboard-shell for parity with
    # Habitation â€” same Sensa header (logo + Mode/WAN chips + trial
    # countdown in the right slot), same kiosk hide-HA-topbar
    # behaviour, and a real tab strip. The standalone trial banner is
    # dropped â€” the header right slot already shows the countdown.
    # Tabs are built below: Home, Navigation and Parking always, then
    # Engine and Level gated on their devices being registered.
    from ..const import DEPLOYMENT_MODE_APPLIANCE
    appliance = _current_deployment_mode() == DEPLOYMENT_MODE_APPLIANCE

    _placeholder = _note_card

    def _safe_build(slug: str, label: str, icon: str, builder) -> dict[str, Any]:
        try:
            card = builder()
        except Exception as exc:  # noqa: BLE001 â€” isolate per-tab failures
            _LOGGER.exception(
                "Cab dashboard: tab %r builder raised; surfacing as in-tab error",
                slug,
            )
            card = _tab_error_card(label, exc)
        if card is None:
            card = _placeholder(
                f"Tab '{label}' builder returned no card. This is a Sensa "
                "integration bug â€” please report.",
            )
        return {"slug": slug, "label": label, "icon": icon, "card": card}

    def _build_overview_tab() -> dict[str, Any]:
        # The starting layout is the first one this licence allows (tier +
        # dealer gated), dispatched through `_CAB_LAYOUTS`; the customer then
        # arranges it in Design Mode. The default
        # public layout is "Default (Landscape)" (`_build_cab_default_landscape`,
        # a copy of the BlackPaw arrangement since 2026-08-17); dealer-owned
        # Cab layouts slot in the same way the Habitation home layouts do.
        layout_id = _resolve_active_cab_layout()
        if not layout_id:
            # Exclusive dealer with no Cab layouts (resolver already logged it).
            # A friendly empty-state rather than a public page, so "exclusive"
            # stays honoured.
            return {
                "type": "custom:sensa-note",
                "icon": "mdi:home-off-outline",
                "content": (
                    "**No Cab layout configured.** Your dealer hasn't "
                    "published a Cab layout yet."
                ),
            }
        return _arrangeable_cab_home(_cab_home_for(layout_id))

    def _build_navigation_tab() -> dict[str, Any]:
        # The map slot; the map's own source pills choose the source.
        return nav_card_slot

    def _build_engine_tab() -> dict[str, Any]:
        # Engine + vehicle diagnostics from the OBD2 adapter (MeatPi WiCAN
        # Pro). The tab is only added when an OBD2 device is registered, so
        # the customer only reaches this once the adapter is present. Built
        # from native cards (`sensa-card-grid` of `sensa-gauge` dial rows +
        # `sensa-sensor-tile` diagnostics) so it direct-creates with no
        # Lovelace engine. See `_obd2_engine_cards`.
        cards: list[dict[str, Any]] = []
        for d in _dashboard_devices:
            if _is_obd2_device(d):
                cards.extend(_obd2_engine_cards(d))
        if not cards:
            # Adapter registered but its HA `mqtt` entities haven't resolved
            # yet â€” a native empty state (no engine), not the markdown
            # placeholder, so the tab still renders on a cold panel load.
            cards = [{
                "type": "custom:sensa-note", "icon": "mdi:engine",
                "content": (
                    "Engine and vehicle diagnostics from your OBD2 adapter "
                    "will appear here once it reports."
                ),
            }]
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            "cards": cards,
        }

    def _build_cab_levelling_tab() -> dict[str, Any]:
        # Customer-facing parking aid. Per device: the summary card
        # (status pill + actionable copy + wheel cells + readouts) and the
        # gyro hero (big circular four-quadrant visual). `sensa-card-grid`
        # auto-fits them: side-by-side at desktop width, stacked on
        # narrow. Only reached when `levelling_devices_cab` is non-empty.
        cards: list[dict[str, Any]] = []
        for d in levelling_devices_cab:
            # Note: the big gyro lives on Cab â†’ Overview now, so the
            # Levelling tab focuses on the actionable + per-wheel views.
            cards.append(_build_cab_levelling_card(d))
            cards.append(_build_cab_levelling_tilt_card(d))
            cards.append(_build_cab_levelling_topdown_card(d))
            # Raw Sensor + Calibration, moved off Settings -> Level 2026-08-10.
            # Both belong beside the readings they explain: Calibrate is pressed
            # while parked on ground the customer can see is flat, which is the
            # same moment they are looking at this tab, and the raw pitch/roll
            # is how you tell a sensor that is not reporting from one that is
            # reporting something you disagree with. Sending them to Settings
            # for either meant leaving the screen they were parking by.
            cards.extend(_build_levelling_cards(d))
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            "cards": cards,
        }

    def _build_cab_parking_tab() -> dict[str, Any]:
        # A combined park-up screen: the big van-level gyro (level the van) plus
        # the internet signal-quality card (check signal at the parkup).
        #
        # The signals card is ALWAYS drawn. Its Internet section is fed by the
        # add-on's own reachability probe, not by a device
        # (`_build_network_signals_card`), so "is there internet at this parkup?"
        # is answerable on every van. It used to be gated on a registered router
        # or Starlink, which meant the customers most likely to be hunting for
        # signal -- a phone hotspot or a MiFi, neither of which registers as a
        # device -- were the only ones who could not see it. The Mobile (LTE)
        # and Starlink sections stay device-gated inside the card builder.
        cards: list[dict[str, Any]] = [
            _build_cab_levelling_gyro_card(d) for d in levelling_devices_cab
        ]
        signals = _build_network_signals_card()
        if levelling_devices_cab:
            # The signals card ships `full_width: True` for the Internet
            # tab, where it is the only card and should span the grid. Here
            # it has to SHARE a row with the level panel, and full_width
            # spans every column -- so the `columns` split below was
            # silently overruled and the internet panel dropped onto its own
            # row underneath. Strip it for this pairing only.
            signals.pop("full_width", None)
        cards.append(signals)
        grid: dict[str, Any] = {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            # Design Mode may arrange it despite the explicit column split.
            "arrangeable": True,
            "cards": cards,
        }
        # Gyro | internet, half the width each on one row. An explicit track ratio
        # rather than the default auto-fit, which sizes the columns to content and
        # left the two panels unequal. `columns` folds back to a single stacked
        # column under 640px, so a narrow Cab screen still reads top-to-bottom.
        # Only when BOTH are present -- a lone card is `single` (centred + capped)
        # and would ignore it anyway.
        if len(cards) == 2:
            grid["columns"] = "1fr 1fr"
        return grid

    # OBD2 / engine tab â€” conditional on a WiCAN (or other OBD2)
    # adapter being registered. Forward-looking: no device matches
    # today, so the tab stays hidden until OBD2 support lands.
    obd2_present = any(_is_obd2_device(d) for d in _dashboard_devices)

    # Van Level â€” show the customer-facing parking aid when a levelling
    # device is registered. Mirrors the Settings tab's conditional gate.
    levelling_devices_cab = [
        d for d in _dashboard_devices
        if (
            d.get("service_class") == "levelling"
            or d.get("vendor_device_type") == "van_level"
        )
    ]

    # The signal-quality panel is no longer gated on a registered router /
    # Starlink here: the Parking tab is always built and the card decides its
    # own sections. `_build_network_signals_card` keeps the per-section device
    # gating, including the Teltonika-switch exclusion (a switch classifies as
    # `router` but has no modem, so it can never answer "is the signal any good
    # here?").

    tabs: list[dict[str, Any]] = [
        _safe_build("overview", "Home", "mdi:view-dashboard-outline", _build_overview_tab),
        _safe_build("navigation", "Navigation", "mdi:steering", _build_navigation_tab),
    ]
    # Parking - a combined park-up screen (van-level gyro + internet signal).
    # ALWAYS present: the internet signal card needs no registered device (the
    # ping comes from the add-on's own probe), so every van can answer "is there
    # signal at this parkup?". It was gated on a van-level device OR a router,
    # which hid the tab entirely from a van running on a phone hotspot or a
    # MiFi -- exactly the customers who most need to check signal before
    # stopping. The van-level gyro inside stays device-gated.
    tabs.append(
        _safe_build(
            "parking", "Parking", "mdi:parking", _build_cab_parking_tab,
        ),
    )
    # No standalone Internet tab: the signal-quality card lives on Parking,
    # which is where the customer asks "is the signal any good HERE?".
    if obd2_present:
        tabs.append(
            _safe_build("engine", "Engine", "mdi:engine", _build_engine_tab),
        )
    if levelling_devices_cab:
        tabs.append(
            _safe_build(
                "levelling", "Level", "mdi:spirit-level",
                _build_cab_levelling_tab,
            ),
        )
    # Roll: the big side-roll danger screen for off-camber driving, the large
    # version of the tile every Cab Home layout carries. Gated like Level.
    if levelling_devices_cab:
        tabs.append(
            _safe_build(
                "roll", "Roll", "mdi:car-traction-control",
                lambda: {
                    "type": "custom:sensa-card-grid",
                    "hide_header": True,
                    "full_height": True,
                    # full_width, or the grid caps a lone card at 480px and
                    # centres it; the roll view is meant to fill the tab.
                    "cards": [
                        {**_build_level_roll_card(d, "large"), "full_width": True}
                        for d in levelling_devices_cab
                    ],
                },
            ),
        )
    # Tyres: the BLE tyre-pressure sensors laid out by wheel. Gated on at
    # least one sensor with a pressure entity, like Engine and Level.
    tyre_cards = build_tyre_cards(_dashboard_devices)
    if tyre_cards:
        tabs.append(
            _safe_build(
                "tyres", "Tyres", "mdi:car-tire-alert",
                lambda cards=tyre_cards: {
                    "type": "custom:sensa-card-grid",
                    "hide_header": True,
                    "columns": "minmax(0, 1fr)",
                    "cards": cards,
                },
            ),
        )
    # Dump: the live feed of the grey-waste dump camera, the one camera a Pro
    # licence allows, through Frigate's restream. On CAB, not Habitation
    # (Richard, 2026-09-05): dumping is done from the driving seat with the
    # van over the drain. Gated on a camera being registered, the way Engine
    # and Level are; the placeholder Grey Dump tab this replaces was removed
    # on 2026-08-17 for only ever explaining its own emptiness. One
    # full-height grid, the card fills it and letterboxes the picture.
    from . import _camera_cards

    camera_cards = _camera_cards()
    if camera_cards:
        tabs.append(
            _safe_build(
                "dump", "Dump", "mdi:cctv",
                lambda cards=camera_cards: {
                    "type": "custom:sensa-card-grid",
                    "hide_header": True,
                    "full_height": True,
                    "columns": "minmax(0, 1fr)",
                    "cards": cards,
                },
            ),
        )

    # No tab-strip selectors. The Cab Home layout picker went (2026-09-30), as
    # the Habitation one did: Design Mode arranges Cab Home, so the layout is
    # only the arrangement a box starts from (`_resolve_active_cab_layout`).
    # The Navigation tab's "Navigation" (map source) and "Location device"
    # DROPDOWNS were removed 2026-08-07 so it follows the same logic as the Cab
    # HOME layout: the map source is chosen by the round source pills on the map
    # itself, and the location follows the box-wide setting from
    # Settings -> Radar (`resolve_nav_source`) with no per-screen override. Two
    # controls for the same two questions, in two different idioms, on two tabs
    # of the same dashboard was the thing to remove.
    tab_strip_selects: list[dict[str, Any]] = []
    # The "Driver" DROPDOWN was removed (2026-08-01) in favour of the photo
    # buttons on the Home tab (`custom:sensa-people-bar`). It only ever switched
    # the MySensa session and never touched the GPS, so it sat next to a
    # "Location device" picker meaning something different; one tap on a face
    # now does both. `select.sensa_nomad_cab_member` still EXISTS and is still
    # what the nearby coordinator reads -- the buttons write it through
    # `sensa_nomad.set_driver` -- it simply has no dropdown of its own.

    shell_card: dict[str, Any] = {
        "type": "custom:sensa-dashboard-shell",
        "logo": _dealer_logo_url() or _SENSA_DEFAULT_LOGO,
        "chips": _habitation_chips(),
        "kiosk_hide_header": True,
        "kiosk_hide_sidebar": appliance,
        "header_show_trial": True,
        "tab_strip_selects": tab_strip_selects,
        "tabs": tabs,
    }

    return {
        "title": "Nomad Cab",
        "views": [
            {
                "title": "Sensa",
                "path": "sensa",
                "icon": "mdi:steering",
                "panel": True,
                "cards": [shell_card],
            }
        ],
    }


# Cab Home's Design Mode layout id (`grid_layout.layout_id("cab", "overview")`).
_CAB_HOME_LID = "cab/overview"


def _cab_home_for(layout_id: str) -> dict[str, Any]:
    """Cab Home's card for the starting layout `layout_id`.

    A layout spec (fetched from Hub first, else bundled in `layouts/cab/`)
    wins over the Python builder, so a dealer's Cab Home can change without a
    release. Never on a Cab Home the customer has arranged: that keeps the spec
    it was arranged from (`grid_layout.pinned_spec`), or, arranged before specs
    existed, the builder it always had. A spec that cannot render falls back to
    the builder too, so a bad Hub upload never blanks the tab.
    """
    from .. import grid_layout
    from . import _CAB_LAYOUTS, _build_cab_default_landscape, _dashboard_hass
    from .layout_spec import render_cab_layout, resolve_spec

    def builder() -> dict[str, Any]:
        return _CAB_LAYOUTS.get(layout_id, _build_cab_default_landscape)()

    from ..layout_catalog import design_locked

    # A `locked` dealer ignores what the customer saved, so its page is always
    # the dealer's newest design.
    arranged = (grid_layout.has_saved_layout(_CAB_HOME_LID)
                and not design_locked(_dashboard_hass))
    if arranged:
        spec = grid_layout.pinned_spec(_CAB_HOME_LID)
    else:
        spec = resolve_spec(_dashboard_hass, layout_id, "cab")
    card = render_cab_layout(spec) if spec else None
    if not arranged:
        # What a first Design Mode save pins.
        grid_layout.note_built_spec(_CAB_HOME_LID, spec if card is not None else None)
    return card if card is not None else builder()


def _arrangeable_cab_home(card: dict[str, Any]) -> dict[str, Any]:
    """Let Design Mode arrange the Cab Home tab (Add card, drag, resize).

    Every card is its own grid card, so a customer can move, resize, remove
    and add each one. The Cab layouts are built as a stacked column of cards
    (speedometer, side roll, heater...) beside or above the map; that column is
    lifted into the tab's own grid, and a `default_layout` puts everything
    where the built layout had it (`_cab_default_layout`) until the customer
    arranges it (`_CAB_DEFAULT_ROWS` holds the heights it starts with). A
    layout that is one bare card (the map alone) is wrapped in a
    grid where it still fills the tab. The no-layout note is left as it is.
    """
    lifted = _lift_cab_column(card)
    if lifted is not None:
        return lifted
    if card.get("type") == "custom:sensa-card-grid":
        return {**card, "arrangeable": True}
    if card.get("type") == "custom:sensa-note":
        return card
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "arrangeable": True,
        "cards": [{**card, "full_width": True, "fill_alone": True}],
    }


# Heights a new install starts with on Cab Home, in grid rows (8px each), as
# Richard arranged them on the fleet box, 2026-09-29. A card not listed takes
# its content's height.
_CAB_DEFAULT_ROWS: dict[str, int] = {
    "custom:sensa-climate-summary": 33,
    "custom:sensa-nav-map": 82,
}


def _cab_default_place(cfg: dict[str, Any], col: int, row: int, w: int) -> list[int]:
    """A Cab Home card's starting place: its set height (SIZED) if it has one."""
    from ..grid_layout import SIZED

    rows = _CAB_DEFAULT_ROWS.get(str(cfg.get("type")))
    return [col, row, w, rows, SIZED] if rows else [col, row, w, 1]


# Dial cards that draw bare by default and need their frame once they are a
# card of their own on Cab Home.
_FRAMED_WHEN_LIFTED = frozenset({"custom:sensa-gauge-detail", "custom:sensa-gauge"})


def _lift_cab_column(card: dict[str, Any]) -> dict[str, Any] | None:
    """A Cab layout of [stacked column grid, map] as one flat arrangeable grid
    of the column's cards then the map, with the built arrangement as its
    `default_layout`. None for any other shape."""
    from ..grid_layout import GRID_COLUMNS, card_keys

    cards = card.get("cards") or []
    if card.get("type") != "custom:sensa-card-grid" or len(cards) != 2:
        return None
    column, nav_map = cards
    if not (isinstance(column, dict) and column.get("type") == "custom:sensa-card-grid"
            and column.get("stack")):
        return None
    # Each lifted card now stands alone in the grid, so a dial card gets its
    # own frame (`card: true`): without one it has nothing to fill, and the
    # dial fit (webcards shared/dial-fit.ts) never runs, so a dial sized to
    # the card's width (the portrait layout's lone speedometer, 100cqw) runs
    # out of the bottom of a card given a height in Design Mode.
    lifted = [
        {**c, "card": True} if c.get("type") in _FRAMED_WHEN_LIFTED else c
        for c in column.get("cards") or [] if isinstance(c, dict)
    ]
    flat = [*lifted, nav_map]
    keys = card_keys(flat)
    # Side by side ("35fr 65fr"): the column takes its share of the 12 grid
    # columns and the map the rest. One track (portrait): the column over the map.
    fr = [float(n) for n in re.findall(r"([\d.]+)fr", str(card.get("columns") or ""))]
    side = len(fr) == 2 and sum(fr) > 0
    col_w = max(1, min(GRID_COLUMNS - 1, round(GRID_COLUMNS * fr[0] / sum(fr)))) if side else GRID_COLUMNS
    layout: dict[str, list[int]] = {
        key: _cab_default_place(cfg, 0, row, col_w)
        for row, (key, cfg) in enumerate(zip(keys[:-1], lifted, strict=True))
    }
    layout[keys[-1]] = (
        _cab_default_place(nav_map, col_w, 0, GRID_COLUMNS - col_w) if side
        else _cab_default_place(nav_map, 0, len(lifted), GRID_COLUMNS)
    )
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "arrangeable": True,
        "cards": flat,
        "default_layout": layout,
    }
