"""The Habitation dashboard: the living surface, and its trial-header sibling.

`_build_test_yaml_data` is named for history, not for testing: it IS the
production Habitation builder, and `_build_habitation_yaml_data` wraps it.

Split out of the old monolithic `dashboard.py`. The card helpers it calls stay in
the package root and are imported per call (see the note in each builder).
"""
from __future__ import annotations

from typing import Any


def _build_habitation_yaml_data() -> dict[str, Any]:
    """Return the managed Nomad Habitation dashboard YAML structure.

    May 2026: Habitation moved onto the panel + dashboard-shell
    architecture validated by the Test2 sandbox. Single HA view
    carrying a `custom:sensa-dashboard-shell` card. Home is always
    present (modes strip + trial banner suppressed because the header
    right slot already shows the countdown); every other tab is
    conditional â€” Electrical appears only when the power-flow card
    resolves an electrical item, Network when a router / Starlink dish
    is registered, BMS with a leisure battery, etc. (no placeholder
    tabs). The trial countdown lives in the header's right slot via
    `header_show_trial=True`.

    The legacy multi-view (Overview / Electrical sections / BMS /
    Network) implementation lived here until that move. The BMS
    view is dropped at the same time â€” leisure-battery telemetry
    surfaces on the Electrical tab (battery slot in the power-flow
    diagram) and per-device detail remains reachable via Settings â†’
    Devices & Services. Re-introducing a dedicated BMS view, if the
    customer asks for one, can be done as an extra tab without
    changing this contract. The Test2 sandbox dashboard
    (`nomad-test-trial.yaml`) is intentionally kept as a side-by-
    side safety net during the rollout.
    """
    return _build_test_yaml_data(
        title="Nomad Habitation",
        header_show_trial=True,
    )

def _build_habitation_yaml_data_legacy() -> dict[str, Any]:
    """Deprecated multi-view Habitation builder, kept around for the
    existing test suites until they're migrated to the shell-based
    layout. New code should use `_build_habitation_yaml_data` (which
    routes to `_build_test_yaml_data`). This function is NOT wired to
    any dashboard filename in DASHBOARD_BUILDERS â€” it's only callable
    from tests. Delete once the test suites move over."""
    # Late-bound on purpose. These live in the package root and are resolved
    # per CALL, exactly as they were when this was all one module: the tests
    # patch them on the package (`patch.object(dashboard, ...)`), and
    # `_dashboard_devices` is REBOUND by `set_dashboard_devices`. A module-level
    # import here would freeze the originals and silently ignore both.
    from . import (
        _LOGGER,
        _bms_family,
        _build_bms_card,
        _build_habitation_router_card,
        _build_habitation_starlink_cards,
        _build_habitation_switch_card,
        _build_header_card,
        _build_modes_strip,
        _build_network_view_card,
        _build_power_flow_card,
        _build_roamer_gen1_card,
        _dashboard_devices,
        _device_service_class,
        _habitation_chips,
        _is_leisure_battery,
        _teltonika_is_switch,
    )

    bms_cards = [
        _build_roamer_gen1_card(d) if _bms_family(d) == "vip"
        else _build_bms_card(d)
        for d in _dashboard_devices if _is_leisure_battery(d)
    ]

    # Router cards collected separately from Starlink cards so the
    # final order is deterministic â€” routers always first, dishes
    # second, switches last, regardless of the order the backend returns
    # devices in. Device selection is by service class, except that a
    # Teltonika SWITCH is caught first by `_teltonika_is_switch` (it
    # classifies as `router` because that is the only catalog entry with a
    # discovery rule) and gets the port-faceplate card instead.
    router_cards: list[dict[str, Any]] = []
    starlink_cards: list[dict[str, Any]] = []
    switch_cards: list[dict[str, Any]] = []
    for d in _dashboard_devices:
        service_class = _device_service_class(d)
        # Only render the card once the vendor coordinator has actually
        # registered entities for this device. A freshly-added Teltonika
        # has no `network_entities` until the customer enters credentials
        # and presses Save â€” surfacing a blank card before that just
        # advertises a broken-looking view. The configure_device handler
        # fires SIGNAL_DASHBOARD_REGEN_REQUESTED after the coordinator
        # starts, which lands here with the role-map populated.
        role_map = d.get("network_entities") or {}
        if not role_map:
            continue
        if _teltonika_is_switch(d):
            switch_cards.append(_build_habitation_switch_card(d))
        elif service_class == "router":
            router_cards.append(_build_habitation_router_card(d))
        elif service_class == "satellite_internet":
            starlink_cards.extend(_build_habitation_starlink_cards(d))
    network_cards: list[dict[str, Any]] = router_cards + starlink_cards + switch_cards

    overview_cards: list[dict[str, Any]] = [
        # Trial / licence state is surfaced in the shell header's right slot
        # (header_show_trial); the standalone sensa-trial-card banner that used
        # to lead this view was retired.
        _build_modes_strip(),
    ]

    # Sensa header strip â€” logo on the left, status chips in the
    # centre, live date/time on the right. Built fresh per view so
    # each view can pass its own `extra_chips` for view-specific
    # signals (e.g. SOC % on BMS, WAN signal on Network) while the
    # base chips (current mode + WAN source) stay identical across
    # views.
    views: list[dict[str, Any]] = [
        {
            "title": "Overview",
            "path": "overview",
            "icon": "mdi:sofa",
            "cards": [_build_header_card(), *overview_cards],
        }
    ]

    # Electrical view â€” sits directly after Overview. Appears when any
    # electrical-category device is registered (leisure battery, Cerbo +
    # its power sub-devices, Orion XS, â€¦). Hero is the power-flow card;
    # below it, one card per device, brand-agnostic, ordered by service
    # class for a deterministic layout. Leisure batteries feed the
    # power-flow card's battery role but get NO card here â€” they have
    # their own dedicated BMS view.
    # The Electrical view is a panel â€” power-flow hero on the left,
    # inverter controls in a column on the right. Per-device cards
    # (MPPT / SmartShunt / Cerbo sub-devices) used to tile underneath
    # but the customer wanted the diagram to dominate, so they're not
    # rendered here; the data is still reachable from each device's
    # own page under Settings > Devices & Services.
    try:
        power_flow_card = _build_power_flow_card()
    except Exception:  # noqa: BLE001 - a card must never fail the dashboard build
        _LOGGER.warning(
            "power-flow card build failed â€” Electrical view will be "
            "omitted this regen; other Habitation views unaffected",
            exc_info=True,
        )
        power_flow_card = None
    if power_flow_card is not None:
        # Sections view with HA's native badge row at the top instead
        # of the Sensa header card. The badges carry the same
        # status chips the header used to (current mode + WAN source
        # when a router is registered), so no information loss â€” just
        # a smaller, more HA-native chrome budget that leaves more
        # vertical room for the power-flow diagram. The power-flow
        # card's own `:host { max-width: 1100px; margin: 0 auto }`
        # keeps the diagram from sprawling on wide monitors regardless
        # of the section width.
        electrical_view: dict[str, Any] = {
            "title": "Electric",
            "path": "electrical",
            "icon": "mdi:flash",
            "type": "sections",
            "sections": [
                {
                    "type": "grid",
                    "cards": [
                        {**power_flow_card, "grid_options": {"columns": "full"}},
                    ],
                },
            ],
        }
        # Promote the header chip list to view-level badges. Each chip
        # is `{entity, label}` â†’ badge `{entity, name}`. HA renders
        # the badge row above the first section.
        chip_badges = [
            {"entity": chip["entity"], "name": chip.get("label", "")}
            for chip in _habitation_chips()
            if chip.get("entity")
        ]
        if chip_badges:
            electrical_view["badges"] = chip_badges
        views.append(electrical_view)

    if bms_cards:
        # Masonry view (default â€” no `type`). Header card sits at the
        # top; one sensa-bms-card per battery flows into the masonry
        # columns below.
        views.append({
            "title": "BMS",
            "path": "bms",
            "icon": "mdi:car-battery",
            "cards": [_build_header_card(), *bms_cards],
        })

    if network_cards:
        # Panel + custom shell. The shell owns the header strip and the
        # responsive CSS grid that lays out the router / Starlink cards
        # â€” HA's masonry is no longer involved, so variable card counts
        # (router only / router + Starlink / Starlink only) always look
        # intentional on phone / tablet / desktop. This is the pilot
        # for the panel + shell architecture described in
        # docs/system-design/dashboards.md; once validated the same
        # pattern rolls out to the other Habitation views.
        views.append({
            "title": "Network",
            "path": "network",
            "icon": "mdi:router-wireless",
            "panel": True,
            "cards": [
                _build_network_view_card(
                    router_cards, starlink_cards, switch_cards,
                )
            ],
        })

    return {"title": "Nomad Habitation", "views": views}

def _build_test_yaml_data(
    *,
    title: str = "Test1",
    header_show_trial: bool = False,
) -> dict[str, Any]:
    """Sandbox dashboard for the panel + dashboard-shell pattern.

    A single HA view (so HA's tab strip auto-hides) wrapping one
    `custom:sensa-dashboard-shell` card that carries the header strip
    plus its own tab strip directly beneath it. Two tabs load the same
    content the production Habitation dashboard renders today so the
    sandbox can be A/B'd visually against the real views. Drop the
    dashboard once the pattern ships to Habitation for real.
    """
    # Late-bound on purpose. These live in the package root and are resolved
    # per CALL, exactly as they were when this was all one module: the tests
    # patch them on the package (`patch.object(dashboard, ...)`), and
    # `_dashboard_devices` is REBOUND by `set_dashboard_devices`. A module-level
    # import here would freeze the originals and silently ignore both.
    from . import (
        _LOGGER,
        _SENSA_DEFAULT_LOGO,
        _SWITCHED_TABS,
        _arrangeable_hab_home,
        _bms_family,
        _build_battery_protect_card,
        _dc_meter_cards,
        _build_bms_card,
        _build_habitation_router_card,
        _build_habitation_starlink_cards,
        _build_habitation_switch_card,
        _build_habitation_esim_card,
        _build_habitation_vlan_cards,
        _build_habitation_wan_card,
        _build_home_standard,
        _build_network_view_card,
        _build_power_flow_card,
        _build_roamer_gen1_card,
        _cerbo_hub_info,
        _climate_tab_cards,
        _current_deployment_mode,
        _dashboard_devices,
        _dealer_logo_url,
        _device_service_class,
        _geofence_card,
        _habitation_chips,
        _has_relay_device,
        _history_cards,
        _home_layout_builder,
        _is_leisure_battery,
        _lighting_tab_cards,
        _media_cards,
        _note_card,
        _relay_controller_tile_cards,
        _resolve_active_home_layout,
        _routed_switch_tiles_by_category,
        _security_sensor_cards,
        _switch_ports_cards,
        _tab_error_card,
        _teltonika_is_switch,
        build_cryptogenerator_cards,
        build_dji_power_cards,
    )


    _placeholder = _note_card
    # Surfacing the traceback inside the tab itself means a builder bug is
    # visible at the exact tab that's broken, instead of taking down the whole
    # dashboard with a generic HA error.
    _error_placeholder = _tab_error_card

    def _safe_build(slug: str, label: str, icon: str, builder) -> dict[str, Any]:
        try:
            card = builder()
        except Exception as exc:  # noqa: BLE001 â€” isolating tab failures
            _LOGGER.exception(
                "test dashboard: tab %r builder raised; surfacing as in-tab error card",
                slug,
            )
            card = _error_placeholder(label, exc)
        # Final guard: shell's setConfig validates `!tab.card`, so a None
        # here serialises as `card: null` and trips "Configuration Error".
        # Substituting a placeholder is always safer than letting None
        # through; if a builder ever returns None it's a contract bug we
        # want visible, not the whole dashboard down.
        if card is None:
            _LOGGER.warning(
                "test dashboard: tab %r builder returned None; "
                "substituting placeholder. Fix the builder to return a dict.",
                slug,
            )
            card = _placeholder(
                f"Tab '{label}' builder returned no card. This is a Sensa "
                "integration bug â€” please report.",
            )
        return {"slug": slug, "label": label, "icon": icon, "card": card}

    def _build_home_card() -> dict[str, Any]:
        # Dispatch through the home-layout seam rather than hardcoding the
        # tab body, so build-time dealer layouts / customer presets slot in
        # later without touching this function (see _HOME_LAYOUTS). The trial
        # countdown lives solely in the header right-slot now (header_show_trial)
        # â€” the standalone sensa-trial-card banner it superseded was retired â€”
        # so the home layout is the whole tab body.
        layout_id = _resolve_active_home_layout()
        if not layout_id:
            # Blank sentinel â€” an exclusive dealer with no layouts (the resolver
            # has already logged it). Show a friendly empty-state note rather
            # than a public page, so "exclusive" stays honoured.
            return {
                "type": "custom:sensa-note",
                "icon": "mdi:home-off-outline",
                "content": (
                    "**No home screen configured.** Your dealer hasn't "
                    "published a home layout yet."
                ),
            }
        # `_home_layout_builder`, not a plain registry lookup: a dealer layout
        # published from Hub has no entry in `_HOME_LAYOUTS` on a build that
        # predates it, and a registry-only lookup would quietly serve Power
        # Central instead of the layout the customer picked. Still falls back to
        # Power Central when nothing can render the id.
        builder = _home_layout_builder(layout_id)
        return _arrangeable_hab_home((builder or _build_home_standard)())

    # Electrical tab â€” conditional on electrical telemetry the power-flow card
    # can show, the same treatment as BMS / Network below (Richard, 2026-05-30:
    # the Electrical tab should be conditional; "if there are no electrical
    # items used by the power flow then it shouldn't be shown"). No placeholder
    # tab on an install with no electrical device. `_build_power_flow_card` can
    # fail two ways on a fresh install: raise (rare) OR return None (its
    # documented "no electrical telemetry resolved yet" contract) â€” both mean
    # "no Electrical tab this regen".
    try:
        electrical_card = _build_power_flow_card()
    except Exception:  # noqa: BLE001 - a card must never fail the dashboard build
        _LOGGER.debug(
            "test dashboard: power-flow card raised, "
            "Electrical tab omitted this regen",
            exc_info=True,
        )
        electrical_card = None

    # Network cards â€” built up front so the tab is only added when at
    # least one router / Starlink dish has registered entities. A
    # device with no `network_entities` role-map yet (freshly added,
    # pre-credential-save) doesn't count â€” its card would be blank.
    network_router_cards: list[dict[str, Any]] = []
    network_starlink_cards: list[dict[str, Any]] = []
    network_switch_cards: list[dict[str, Any]] = []
    # Cards that lead the tab, ahead of every device card and in router
    # order: the full-width Networks diagram first, then Internet Priority.
    network_vlan_cards: list[dict[str, Any]] = []
    network_wan_cards: list[dict[str, Any]] = []
    # The router's eSIM, a card of its own, only for a router with one.
    network_esim_cards: list[dict[str, Any]] = []
    for _net_d in _dashboard_devices:
        if not (_net_d.get("network_entities") or {}):
            continue
        _net_class = _device_service_class(_net_d)
        # Switch BEFORE router: every Teltonika is discovered against the one
        # catalog entry carrying a DHCP rule, so a TSW switch also classifies
        # as `router` and would otherwise be handed the router card and its
        # unfillable WAN / signal rows. See `_teltonika_is_switch`.
        if _teltonika_is_switch(_net_d):
            network_switch_cards.append(_build_habitation_switch_card(_net_d))
        elif _net_class == "router":
            network_vlan_cards.extend(_build_habitation_vlan_cards(_net_d))
            _esim_card = _build_habitation_esim_card(_net_d)
            if _esim_card:
                network_esim_cards.append(_esim_card)
            _wan_card = _build_habitation_wan_card(_net_d)
            if _wan_card:
                network_wan_cards.append(_wan_card)
            network_router_cards.append(_build_habitation_router_card(_net_d))
        elif _net_class == "satellite_internet":
            network_starlink_cards.extend(_build_habitation_starlink_cards(_net_d))
    # With the Internet card, it is the whole tab outside Nerd Mode and every
    # other card (the Networks diagram included) is the detail behind it.
    # Without one (a Starlink-only van) nothing is tagged, or the tab would be
    # empty.
    if network_vlan_cards:
        # In Nerd Mode a tablet shows the Networks diagram alone: it carries
        # every WAN, the router and the dish. A phone cannot show the diagram
        # (`tablet_only`), so there these device cards are Nerd Mode instead.
        for _card in (
            network_wan_cards + network_router_cards
            + network_starlink_cards + network_switch_cards
        ):
            _card["nerd"] = True
            _card["phone_only"] = True
    if network_vlan_cards:
        # Beside the everyday Network card. Buying data is an everyday job;
        # Nerd Mode on a tablet is the Networks diagram alone, as before.
        for _card in network_esim_cards:
            _card["everyday"] = True
    network_router_cards[:0] = network_vlan_cards + network_esim_cards + network_wan_cards
    network_cards_present = bool(
        network_router_cards or network_starlink_cards or network_switch_cards
    )

    def _build_network_card() -> dict[str, Any]:
        network_card = _build_network_view_card(
            network_router_cards, network_starlink_cards, network_switch_cards,
        )
        # The dashboard shell already renders the header strip;
        # suppress the one the network shell would render itself so
        # the chrome doesn't double up.
        network_card["hide_header"] = True
        return network_card

    # BMS cards â€” one per registered leisure battery. Both families render
    # via the native custom:sensa-bms-card: VIP packs (Roamer Gen 1) get
    # the cells + temps Gen1 slot set, everything else the JK/JBD family
    # slot set (SoH + metrics + status). Built up front so the tab is only
    # added when at least one leisure battery is registered.
    bms_cards = [
        _build_roamer_gen1_card(d) if _bms_family(d) == "vip"
        else _build_bms_card(d)
        for d in _dashboard_devices if _is_leisure_battery(d)
    ]

    # BatteryProtect cards â€” read-only status for each adopted Victron Smart
    # BatteryProtect. They sit BELOW the power-flow diagram on the Electrical
    # tab (a load disconnect has no power-flow slot of its own). Built up front;
    # `_build_battery_protect_card` returns None until victron_ble has mapped
    # the device's entities, so those are filtered out (no blank cards).
    # CryptoGenerator - the Bitcoin miners. Its own tab rather than a row on
    # Electrical because a miner in a van is three things at once: a
    # discretionary load you shed when the battery is low, a heater that pays
    # you back, and a thing that makes money. Empty until a miner has
    # registered entities, which is what makes the tab conditional.
    # One card is the whole tab (both the read side and every control), laid
    # out to the Claude Design source; see `build_cryptogenerator_cards`.
    crypto_cards = build_cryptogenerator_cards(_dashboard_devices)
    # DJI Power stations: a portable battery + inverter the customer carries
    # in and out, so its own tab rather than a row on BMS (which is the fitted
    # leisure bank). Empty until the unit's entities exist.
    dji_cards = build_dji_power_cards(_dashboard_devices)

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

    def _build_bms_card_tab() -> dict[str, Any]:
        # One card per pack, laid out in the same responsive grid as the
        # Network tab (auto-fit: two-up on wide screens, a single pack
        # capped + centred rather than stretched to the full panel width).
        # `hide_header` because the panel already renders the Sensa header
        # strip above the tabs.
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            "cards": bms_cards,
        }

    tabs: list[dict[str, Any]] = [
        _safe_build("home", "Home", "mdi:home", _build_home_card),
    ]
    # Alarms moved to the dedicated Intelligence dashboard (Alarms / Automations
    # / Log) â€” the cross-panel banner routes there. Habitation no longer carries
    # a conditional Alarms tab; the banner is still shown on every panel.
    # Electrical tab only when the power-flow card resolved (see above) â€” same
    # conditional treatment as BMS and Network. Sits directly after Home.
    # When a Cerbo with a reachable LAN host is registered, the diagram's
    # central hub becomes the Cerbo (its photo) and opens the Venus OS Remote
    # Console on tap (`console_url`/`hub_image` slots) â€” the Cerbo IS the system
    # hub. This replaced the old standalone console-button card beneath the
    # diagram. With no Cerbo console the tab is the bare diagram exactly as
    # before.
    # Electrical tab appears when the power-flow card resolved OR a BatteryProtect
    # is registered (a lone load-disconnect still earns the tab, just without the
    # diagram). The diagram leads; BatteryProtect cards sit beneath it.
    if electrical_card is not None or battery_protect_cards or dc_meter_cards:
        def _build_electrical_tab(
            card=electrical_card, bp_cards=battery_protect_cards,
            dc_cards=dc_meter_cards,
        ) -> dict[str, Any]:
            stacked: list[dict[str, Any]] = []
            if card is not None:
                try:
                    info = _cerbo_hub_info()
                except Exception:  # noqa: BLE001 - a card must never fail the dashboard build
                    _LOGGER.debug(
                        "test dashboard: cerbo hub lookup raised; omitting",
                        exc_info=True,
                    )
                    info = None
                if info:
                    # Cerbo photo shows whenever a Cerbo is registered; the
                    # tap-to-open console slots only when a LAN host resolved.
                    card = {**card, "hub_image": info["image"]}
                    if info.get("url"):
                        card["console_url"] = info["url"]
                        card["console_name"] = info["name"]
                stacked.append(card)
            stacked.extend(dc_cards)
            if bp_cards:
                stacked.append({
                    "type": "custom:sensa-card-grid",
                    "hide_header": True,
                    "cards": bp_cards,
                })
            # One element â†’ return it bare (preserves the diagram-only tab
            # exactly as before); otherwise stack the diagram + BP grid in a
            # NATIVE grid. A built-in `vertical-stack` needs HA's Lovelace
            # engine, which a `panel_custom` page never loads â€” that is what
            # left the Mobile Electrical tab on "Loading... Taking longer than
            # expected" for weeks (2026-07-09). Same trap, same fix.
            if len(stacked) == 1:
                return stacked[0]
            return {
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                "cards": [{**c, "full_width": True} for c in stacked],
            }

        tabs.append(
            _safe_build(
                "electrical", "Electric", "mdi:flash", _build_electrical_tab,
            ),
        )
    # History tab: VRM-style energy, battery health and graphs from long-term
    # statistics. Conditional like the rest: no roles resolve, no tab.
    try:
        history_cards = _history_cards()
    except Exception:  # noqa: BLE001 - a card must never fail the dashboard build
        _LOGGER.debug(
            "test dashboard: history cards raised; History tab omitted this regen",
            exc_info=True,
        )
        history_cards = []
    if history_cards:
        def _build_history_tab(cards=history_cards) -> dict[str, Any]:
            # Native grid, never a built-in stack: a panel page has no
            # Lovelace engine.
            return {
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                "cards": cards,
            }

        # Nerd Mode only: the panel hides a `nerd` tab until the header's
        # Nerd Mode toggle is on (webcards/src/shared/nerd-mode.ts).
        tabs.append({
            **_safe_build("history", "History", "mdi:chart-bar", _build_history_tab),
            "nerd": True,
        })
    # CryptoGenerator tab - only when a miner is registered AND its entities
    # exist. Sits after Electrical, which is where its power story starts.
    if crypto_cards:
        def _build_crypto_tab(cards=crypto_cards) -> dict[str, Any]:
            # `sensa-card-grid`, never a built-in `vertical-stack`: a
            # `panel_custom` page has no Lovelace engine, and a built-in card
            # as a child is what left the Mobile Electrical tab on "Loading..."
            # for weeks (2026-07-09).
            return {
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                # The one card IS the tab and must fill the panel height with
                # no scrolling; the grid stretches its row to the panel.
                "full_height": True,
                "columns": "minmax(0, 1fr)",
                "cards": cards,
            }

        tabs.append(
            _safe_build(
                "crypto", "Crypto", "mdi:pickaxe",
                _build_crypto_tab,
            ),
        )
    # BMS tab only when at least one leisure battery is registered â€”
    # mirrors the legacy conditional BMS view. Sits between Electrical
    # and Network, the same order the legacy multi-view layout used.
    if bms_cards:
        tabs.append(
            _safe_build("bms", "BMS", "mdi:car-battery", _build_bms_card_tab),
        )
    # DJI tab only when a DJI Power station has registered entities. Sits
    # after BMS: same conditional treatment, same card grid.
    if dji_cards:
        def _build_dji_tab(cards=dji_cards) -> dict[str, Any]:
            return {
                "type": "custom:sensa-card-grid",
                "hide_header": True,
                "cards": cards,
            }

        tabs.append(
            _safe_build("dji", "DJI", "mdi:battery-charging-high", _build_dji_tab),
        )
    # Network tab only when a router / Starlink dish with registered
    # entities is present â€” same conditional treatment as BMS.
    if network_cards_present:
        tabs.append(
            _safe_build("network", "Network", "mdi:router-wireless", _build_network_card),
        )

    # Switched-Device routing: a conditional Lighting / Water / Climate tab for
    # each category a customer has routed a relay channel to. Each tab is a
    # responsive grid of on/off tiles (one per routed channel), only present
    # when its category has a tile â€” same conditional treatment as BMS and
    # Network. (Water is its own tab, no longer folded into Climate.)
    switched_tiles = _routed_switch_tiles_by_category()

    def _make_switched_tab(cards: list[dict[str, Any]], **grid_opts: Any):
        # Factory so each tab's builder closes over its own card list (and any
        # extra grid options, e.g. the Lighting tab's narrower cell minimum).
        return lambda: {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            **grid_opts,
            "cards": cards,
        }

    for _cats, _slug, _label, _icon in _SWITCHED_TABS:
        # Wrap this tab's routed channels in ONE `sensa-relay-tiles` panel â€”
        # the same small, packed, image-forward buttons the Switching tab uses
        # â€” rather than one big `sensa-card-grid` cell per channel (which made
        # the tiles huge and the pictures tiny on Lighting/Climate).
        _tiles = [
            tile for _cat in _cats for tile in (switched_tiles.get(_cat) or [])
        ]
        _cards = (
            [{
                "type": "custom:sensa-relay-tiles",
                "full_width": True,
                "tiles": _tiles,
            }]
            if _tiles
            else []
        )
        _grid_opts: dict[str, Any] = {}
        if _slug == "lighting":
            # The WHOLE Lights card list comes from `_lighting_tab_cards()`,
            # which Mobile uses too: light groups first, then the routed relay
            # switch panel (the "normal" lighting channels), then the Zigbee
            # bulb / LED controller cards with full brightness/colour
            # (Richard, 2026-06-13), then the Light Groups manage card.
            #
            # This branch used to rebuild that list itself, and the two
            # spellings drifted the moment light groups were added: they
            # reached Mobile and not Habitation. So the `_cards` built above
            # is discarded here in favour of the shared builder, which builds
            # the same relay-tiles panel itself.
            _cards = _lighting_tab_cards()
            # The light cards draw at HALF the default size (Richard,
            # 2026-09-08): a column minimum well under the grid's 360px, so
            # twice as many share a row. The relay tile panel is full_width
            # and unaffected.
            #
            # Back to 200px now the cards are no longer squared: 240px only
            # existed because a square cell's WIDTH was also its height, so a
            # narrow cell cut the LED strip card's content off the bottom. A
            # natural-height card has no such coupling, and the narrower
            # minimum fits more lights per row.
            _grid_opts["min_card_width"] = "200px"
            # Every light card the same height, whatever its type (Richard,
            # 2026-09-17). The addressable controller carries an effect row the
            # bulbs do not, so it is the tallest and it sets the height; the
            # rest stretch to match. Done in the GRID rather than by stamping a
            # pixel height on each card: a number here would clip the day a
            # card grows a row.
            _grid_opts["equal_rows"] = True
            # Drag a card to move it. The grid sends the resulting key order
            # to this service, which stores it and rebuilds; the order itself
            # belongs to the integration, because the dashboard is generated
            # from it and a browser-local order would last until the next
            # regen and no longer.
            _grid_opts["reorder_service"] = "sensa_nomad.set_lighting_card_order"
        if _slug == "climate":
            # The WHOLE Climate card list comes from `_climate_tab_cards()`,
            # which Mobile uses too: heater card(s), AC card(s), the
            # temperature dial row, then the routed climate switch panel.
            _cards = _climate_tab_cards()
        if _cards:
            tabs.append(
                _safe_build(
                    _slug, _label, _icon,
                    _make_switched_tab(_cards, **_grid_opts),
                ),
            )

    # SensaClimate: the diagram of how Sensa Climate Control works, each box
    # holding its own settings and rules (Richard, 2026-10-06; it began as a
    # Settings tab). Second by default, right after Home (Richard, 2026-10-11);
    # a customer's own Design Mode order still wins (`tab_order`). Pro and above,
    # and only with a heater or an air conditioner. Its controls are
    # settings, so they stay behind the Settings PIN even here.
    from . import _climate_control_settings_card

    sensa_climate = _climate_control_settings_card()
    if sensa_climate:
        sensa_climate["pin"] = True
        tabs.insert(
            next(
                (i + 1 for i, t in enumerate(tabs) if t.get("slug") == "home"), 0,
            ),
            _safe_build(
                "sensa_climate", "SensaClimate", "mdi:thermostat",
                lambda card=sensa_climate: {
                    "type": "custom:sensa-card-grid",
                    "hide_header": True,
                    # The diagram fills the tab, width and height (Richard). As
                    # the Crypto tab: `full_height` stretches the row to the
                    # panel, and an explicit single column keeps the grid from
                    # being an auto-fit, arrangeable one that sits content-tall.
                    "full_height": True,
                    "columns": "minmax(0, 1fr)",
                    "cards": [card],
                },
            ),
        )

    # Media tab â€” one card per registered speaker (e.g. a Sonos adopted via HA's
    # native `sonos` integration, whose `media_player` entity Sensa surfaces but
    # doesn't own). Conditional like BMS / Network: present only when a speaker
    # resolves a media_player entity. Sits after the climate/switched tabs.
    media_cards = _media_cards()
    if media_cards:
        tabs.append(
            _safe_build(
                "media", "Media", "mdi:speaker",
                lambda cards=media_cards: {
                    "type": "custom:sensa-card-grid",
                    "hide_header": True,
                    "cards": cards,
                },
            ),
        )

    # Security tab â€” read-only state tiles for the Zigbee2MQTT motion / contact
    # / leak sensors. Conditional like Media: present only when at least one
    # such sensor resolves its binary_sensor entity. Sits after Media.
    # The geofence map leads it, and a van with a GPS but no security sensor
    # still gets the tab for it.
    geofence_card = _geofence_card()
    security_cards = ([geofence_card] if geofence_card else []) + (
        _security_sensor_cards()
    )
    if security_cards:
        tabs.append(
            _safe_build(
                "security", "Security", "mdi:shield-home",
                lambda cards=security_cards: {
                    "type": "custom:sensa-card-grid",
                    "hide_header": True,
                    "cards": cards,
                },
            ),
        )

    # Switching tab â€” sits at the END of the Habitation tab order so it's
    # the catch-all after the per-category routed tabs (Lighting / Climate).
    # One card per relay controller, each a compact grid of mini on/off tiles
    # (one per enabled + wired channel) â€” a physical control panel per relay
    # box (Richard, 2026-05-30). Gated on at least one relay being registered;
    # falls back to a hint when a relay exists but has no enabled/wired channel.
    # A network switch's RJ45 port tiles ride the same tab: tapping one cuts or
    # restores PoE on that port, which is a CONTROL, and this is the control
    # surface (Settings is pure config since scenes and log moved to
    # Intelligence). They sit after the relay controllers, which are the van's
    # own switching; the network switch is infrastructure.
    switch_port_cards = _switch_ports_cards()
    if _has_relay_device() or switch_port_cards:
        relay_cards = _relay_controller_tile_cards()
        # The empty-state hint is about RELAY channels, so it belongs only to
        # the no-relay-cards case. A van with a network switch and no relay
        # has real tiles to show and must not be told to go and enable a relay
        # channel.
        tab_cards = relay_cards + switch_port_cards
        tabs.append(
            _safe_build(
                "switching", "Switches", "mdi:electric-switch",
                lambda cards=tab_cards: {
                    "type": "custom:sensa-card-grid",
                    "hide_header": True,
                    "cards": cards or [{
                        "type": "custom:sensa-note",
                        "icon": "mdi:electric-switch",
                        "content": (
                            "**No channels enabled.** Enable a relay channel "
                            "from **Settings â†’ Switching** to surface it here."
                        ),
                    }],
                },
            ),
        )

    # Sidebar policy follows install flavour. BYO leaves HA's drawer
    # reachable so the customer can hop between Sensa dashboards and
    # their own HA bits; appliance hides it so the kiosk owns the
    # whole viewport and the in-page switcher inside `<sensa-header>`
    # is the only nav. `?nokiosk` on the URL bypasses both flags for
    # support / developer access regardless of flavour.
    from ..const import DEPLOYMENT_MODE_APPLIANCE
    appliance = _current_deployment_mode() == DEPLOYMENT_MODE_APPLIANCE
    shell_card: dict[str, Any] = {
        "type": "custom:sensa-dashboard-shell",
        "logo": _dealer_logo_url() or _SENSA_DEFAULT_LOGO,
        "chips": _habitation_chips(),
        "kiosk_hide_header": True,
        "kiosk_hide_sidebar": appliance,
        "tabs": tabs,
    }
    if header_show_trial:
        # Header right-slot variant: replaces date/clock with trial
        # countdown while the licence sensor is on a trial state.
        # Flagged on per-dashboard via DASHBOARD_BUILDERS so we can
        # A/B both treatments side by side.
        shell_card["header_show_trial"] = True

    return {
        "title": title,
        "views": [
            {
                "title": "Sensa",
                "path": "sensa",
                "icon": "mdi:test-tube",
                "panel": True,
                "cards": [shell_card],
            }
        ],
    }

def _build_test_trial_yaml_data() -> dict[str, Any]:
    """Sibling sandbox dashboard with the trial-countdown header
    variant on. Lets the test dashboards be A/B'd side by side via the
    sidebar â€” the only diff between this and `_build_test_yaml_data`
    is the `header_show_trial` flag the shell forwards to
    `<sensa-header>`. See [[panel-dashboard-shell-architecture]] for
    the shell pattern itself.
    """
    return _build_test_yaml_data(
        title="Test2",
        header_show_trial=True,
    )
