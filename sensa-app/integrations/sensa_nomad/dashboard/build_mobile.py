"""The Mobile dashboard: the phone surface (bottom tab bar, fill-height maps).

Split out of the old monolithic `dashboard.py`. The card helpers it calls stay in
the package root and are imported per call (see the note in each builder).
"""
from __future__ import annotations

from typing import Any

# The Mobile Navigation map floor, matching the Cab Home one. A phone screen is
# tall, but a folded/short viewport must not collapse a fill map to nothing.
_MOBILE_NAV_MIN_HEIGHT = "360px"


def _mobile_nav_map_card() -> dict[str, Any]:
    """The Mobile Navigation screen's map.

    Built by the SHARED `_nav_map_card_slot`, not hand-rolled. This screen used
    to carry its own copy of the config, and the copies drifted exactly as you
    would expect: the phone silently lost the navigation-units entity, so the
    Radar embed there ignored the customer's Miles/Kilometres preference while
    the Cab map honoured it. Nobody would ever have reported that as a bug, they
    would just have seen two maps disagree.

    That matters more than it sounds: **plenty of customers have no cab tablet,
    so the phone IS their cab screen** (Richard, 2026-08-01). It should be the
    same screen, not a reduced copy of one. Everything shared -- GPS source,
    vehicle tracker, units, fuel type, vehicle profile, category pills, the
    Radar session -- now comes from the one builder, and only what is genuinely
    phone-specific is added here.
    """
    from . import _nav_map_card_slot

    card = _nav_map_card_slot(
        "select.sensa_nomad_cab_nav_source",
        "select.sensa_nomad_cab_location_source",
        "device_tracker.sensa_nomad_vehicle",
        # Fill mode: the map takes the screen, with its pill row beneath.
        aspect_ratio=None,
        min_height=_MOBILE_NAV_MIN_HEIGHT,
        source_pills=True,
    )
    # "Who is driving" rides the map's own pill row, faces left -- the same
    # strip the Cab Home map uses, for the same reason: everything in it
    # changes what the map shows, so it is one row rather than stacked strips.
    card["people_bar"] = {
        "sensor": "sensor.sensa_nomad_people",
        "source_entity": "select.sensa_nomad_cab_location_source",
    }
    # Radar + Google only, no HA Map. HA's built-in map card needs the Lovelace
    # engine, which a locked-down panel never loads, so it is already dropped on
    # an appliance (`select._cab_nav_options`); restricting it here makes the
    # phone behave the same on every box rather than offering a source that
    # renders on some and not others.
    card["options"] = [
        {"value": "Sensa Radar", "icon": "mdi:radar"},
        {"value": "Google Maps", "icon": "mdi:google-maps"},
    ]
    return card


def _build_mobile_yaml_data() -> dict[str, Any]:
    """Return the managed Nomad Mobile dashboard YAML structure.

    May 2026: Mobile adopted the dashboard-shell so it gets the same
    Sensa header chrome (logo left, trial countdown / clock right) and
    the same kiosk hide-HA-topbar behaviour as Habitation. Differences
    from Habitation:
      * `chips: []` â€” no centre badges. Phone widths don't have room
        for the Mode / WAN pills, so the header is just logo + right
        slot.
      * `tab_position: "bottom"` â€” Mobile navigates via a thumb-zone
        BOTTOM tab bar (the native mobile idiom, chosen over top tabs /
        drawer / swipe â€” swipe would collide with the cards' drag-dials).

    Tabs:
      * **Overview** â€” the condensed body: a native `custom:sensa-card-grid`
        with two rows of dials â€” **row 1** the battery / fresh-water /
        grey-water gauges (`_home_left_dials`), then one card per temperature
        sensor (`_climate_temperature_cards`, conditional; the same
        environment-owned set the header chips + Climate tab use). The
        power-flow diagram lives on the Electrical tab, not here.
      * **Navigation** â€” conditional (when a vehicle-GPS source exists): a
        native `custom:sensa-ha-map` reached via the bottom tab bar, resolving
        the location per-screen (viewing phone, else the vehicle).
      * **Electrical** â€” conditional (only when the power-flow detail card
        resolves or a BatteryProtect is registered): the full power-flow
        diagram + BatteryProtect cards, mirroring Habitation Electrical.
      * **Lights** and **Climate**: conditional, the same card lists as the
        Habitation tabs of the same name (`_lighting_tab_cards`,
        `_climate_tab_cards`).

    With only Overview (no Navigation/Electrical item) the shell falls back to
    `hide_tabs` â€” the original single condensed view with no tab strip.
    All cards are `custom:sensa-*` direct-created through the grid, so the
    Mobile panel needs no Lovelace engine.
    """
    # Late-bound on purpose. These live in the package root and are resolved
    # per CALL, exactly as they were when this was all one module: the tests
    # patch them on the package (`patch.object(dashboard, ...)`), and
    # `_dashboard_devices` is REBOUND by `set_dashboard_devices`. A module-level
    # import here would freeze the originals and silently ignore both.
    from ..const import DEPLOYMENT_MODE_APPLIANCE
    from . import (
        _SENSA_DEFAULT_LOGO,
        _build_battery_protect_card,
        _dc_meter_cards,
        _build_power_flow_card,
        _climate_tab_cards,
        _climate_temperature_cards,
        _current_deployment_mode,
        _dashboard_devices,
        _dealer_logo_url,
        _device_service_class,
        _has_gps_source,
        _home_controls_panel,
        _home_left_dials,
        _lighting_tab_cards,
        _modes_pill_bar,
        _split_dials,
    )
    appliance = _current_deployment_mode() == DEPLOYMENT_MODE_APPLIANCE

    cards: list[dict[str, Any]] = []
    # Scenes toolbar â€” the 8 scenes as a wrapping pill row directly under the
    # header (Richard, 2026-06-25), full width.
    cards.append({**_modes_pill_bar(), "full_width": True})
    # Row 1 â€” battery / fresh-water / grey-water dials, full width; the
    # half-circle gauges are aspect-ratio based and self-size, so no cell_height.
    # (The power-flow diagram lives on the Electrical tab now, not here.)
    cards.extend(_split_dials(_home_left_dials()))
    # Then one temperature card per sensor (the same environment-owned set the
    # header chips and the Climate tab use), so each can be arranged, removed
    # and re-added in Design Mode. None when no temperature sensor resolves.
    cards.extend(_climate_temperature_cards())

    body_card = {
        "type": "custom:sensa-card-grid",
        # The shell already draws the Sensa header above the body, so the
        # grid suppresses its own internal header strip.
        "hide_header": True,
        "cards": cards,
    }

    tabs: list[dict[str, Any]] = [
        {
            "slug": "overview",
            "label": "Home",
            "icon": "mdi:home",
            "card": body_card,
        },
    ]

    # Navigation â€” a map surface reached via the bottom tab bar (the customer
    # asked for a Navigation BUTTON on the phone; the shell's `tab_position:
    # "bottom"` renders it as a thumb-zone button, not a top tab). Conditional
    # on a vehicle-GPS source.
    #
    # **Rebuilt on the Cab Home pattern (Richard, 2026-08-01):** the map now
    # runs edge to edge with ALL its controls in the pill row UNDERNEATH it --
    # the faces on the left, the map-source pills on the right, the category
    # pills centred. The old top toolbar (a `sensa-segment-select` plus the
    # Location dropdown) is gone entirely: on a phone it ate the top of the
    # screen to answer two questions nobody asks mid-journey, and the answers
    # are one thumb-reach away at the bottom instead of at the top.
    #
    # **HA Map is dropped here**, so the pills are Radar + Google Maps only. It
    # is already dropped on an appliance (`select._cab_nav_options`: HA's
    # built-in map card needs the Lovelace engine, which a locked-down panel
    # never loads), so this makes the phone behave the same everywhere rather
    # than offering a source that works on some boxes and not others.
    #
    # The nav select is still the SHARED one, so "preferred map" remains one
    # choice across surfaces. Dropping the Location dropdown means this screen
    # no longer carries a per-screen GPS override and simply follows the box's
    # setting (Settings -> Vehicle), which is the same thing the Cab Home map
    # does.
    if _has_gps_source():
        tabs.append({
            "slug": "navigation",
            "label": "Navigation",
            "icon": "mdi:map-marker",
            "card": _mobile_nav_map_card(),
        })

    # Electrical â€” the full power-flow diagram (the same `_build_power_flow_card`
    # detail card the Habitation Electrical tab leads with), full width, plus any
    # BatteryProtect status cards beneath it. Conditional: only when an electrical
    # item resolves OR a BatteryProtect is registered, so the tab never shows
    # empty. Mirrors the Habitation Electrical tab assembly (diagram leads; BP
    # cards stacked beneath; Cerbo photo + console folded into the diagram hub).
    electrical_card = _build_power_flow_card()
    battery_protect_cards = [
        card
        for card in (
            _build_battery_protect_card(d)
            for d in _dashboard_devices
            if _device_service_class(d) == "battery_protect"
        )
        if card is not None
    ]
    # Sensa DC Meter: watts per 12V circuit, under the diagram.
    dc_meter_cards = _dc_meter_cards()
    # Inverter mode + trickle-charge toggle â€” the SAME compact control panel the
    # Home centre column and the Cab "park up" screen use (`_home_controls_panel`,
    # each control conditional). One tap from the phone Electrical tab; omitted
    # when neither an inverter mode nor a trickle-charge relay resolves.
    controls_panel = _home_controls_panel()
    if (
        electrical_card is not None or controls_panel is not None
        or battery_protect_cards or dc_meter_cards
    ):
        stacked: list[dict[str, Any]] = []
        if electrical_card is not None:
            # Mobile uses the PORTRAIT power-flow card â€” the landscape detail
            # diagram looks cramped stretched into a tall phone. Same config
            # (incl. inverter_mode_entity for shore-gating); the animation is
            # fixed to dots and the Cerbo hub/console are detail-card-only, so
            # they're not folded in here.
            electrical_card = {
                **electrical_card, "type": "custom:sensa-power-flow-mobile",
            }
            stacked.append(electrical_card)
        if controls_panel is not None:
            stacked.append(controls_panel)
        stacked.extend(dc_meter_cards)
        if battery_protect_cards:
            stacked.append({
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                "cards": battery_protect_cards,
            })
        # NATIVE stack, not the built-in `vertical-stack`. A `panel_custom`
        # page gets no Lovelace engine, so `sensa-panel` direct-creates
        # `custom:sensa-*` cards but must force-load the engine for a built-in
        # child â€” and that load is unreliable here. With `vertical-stack` the
        # Mobile Electrical tab sat on "Loading... Taking longer than expected"
        # indefinitely (`window.loadCardHelpers` undefined; reproduced in real
        # Chrome at 360px). `custom:sensa-card-grid` renders frame-less and
        # direct-creates its children, so the tab needs no engine at all.
        # Each child is `full_width` so the phone column stacks them one per row.
        electrical_tab_card = (
            stacked[0]
            if len(stacked) == 1
            else {
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                "cards": [{**c, "full_width": True} for c in stacked],
            }
        )
        tabs.append({
            "slug": "electrical",
            "label": "Electric",
            "icon": "mdi:flash",
            "card": electrical_tab_card,
        })

    # Lighting â€” the routed lighting relay tiles + Zigbee bulb/LED cards (the SAME
    # `_lighting_tab_cards` the Habitation Lighting tab uses), reached via the
    # bottom tab bar. Conditional: only when some lighting is installed, so the tab
    # is absent on a van with no lights.
    lighting_cards = _lighting_tab_cards()
    if lighting_cards:
        tabs.append({
            "slug": "lighting",
            "label": "Lights",
            "icon": "mdi:lightbulb-group",
            "card": {
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                "cards": lighting_cards,
            },
        })

    # Climate: heater / AC cards, the temperature dials and the routed climate
    # relay tiles (the SAME `_climate_tab_cards` the Habitation Climate tab
    # uses). Conditional: absent on a van with none of those.
    climate_cards = _climate_tab_cards()
    if climate_cards:
        tabs.append({
            "slug": "climate",
            "label": "Climate",
            "icon": "mdi:thermometer",
            "card": {
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                "cards": climate_cards,
            },
        })

    # SensaClimate: the same Climate Control card as the Habitation tab, which
    # under 640px draws its phone layout (thermostat, readings, a row per
    # unit). After Climate (Richard, 2026-10-06). Behind the Settings PIN.
    from . import _climate_control_settings_card

    sensa_climate = _climate_control_settings_card()
    if sensa_climate:
        sensa_climate["pin"] = True
        tabs.append({
            "slug": "sensa_climate",
            "label": "SensaClimate",
            "icon": "mdi:thermostat",
            "card": {
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                "columns": "minmax(0, 1fr)",
                "cards": [sensa_climate],
            },
        })

    # Bottom tab bar (thumb-zone, the native mobile idiom) whenever there's more
    # than one tab; a lone Overview keeps the original single condensed view with
    # no tab strip (`hide_tabs`).
    multi = len(tabs) > 1

    shell_card: dict[str, Any] = {
        "type": "custom:sensa-dashboard-shell",
        "logo": _dealer_logo_url() or _SENSA_DEFAULT_LOGO,
        "chips": [],
        "kiosk_hide_header": True,
        "kiosk_hide_sidebar": appliance,
        "header_show_trial": True,
        "hide_tabs": not multi,
        "tab_position": "bottom",
        "tabs": tabs,
    }

    return {
        "title": "Nomad Mobile",
        "views": [
            {
                "title": "Sensa",
                "path": "sensa",
                "icon": "mdi:cellphone",
                "panel": True,
                "cards": [shell_card],
            }
        ],
    }
