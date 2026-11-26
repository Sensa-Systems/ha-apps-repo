"""The Settings dashboard: discovery, diagnostics, admin, switching, backup, gallery.

Split out of the old monolithic `dashboard.py`. The card helpers it calls stay in
the package root and are imported per call (see the note in each builder).
"""
from __future__ import annotations

from typing import Any

from ..vendors.service_classes import is_relay_device

# The Analytics & Diagnostics consent switches, keyed 1:1 to
# diagnostics_report.CATEGORIES. Read by the consent panel and by Data usage.
_DIAGNOSTICS_SWITCHES: dict[str, str] = {
    "devices": "switch.sensa_nomad_diag_devices",
    "unavailable": "switch.sensa_nomad_diag_unavailable",
    "errors": "switch.sensa_nomad_diag_errors",
    "resources": "switch.sensa_nomad_diag_resources",
    "uptime": "switch.sensa_nomad_diag_uptime",
    "solar": "switch.sensa_nomad_diag_solar",
}


def data_usage_card() -> dict[str, Any]:
    """Data usage: what the box sends over the internet, how often, and how
    much it used in the last 24 hours and 30 days. On no tab; offered in the
    card library for a customer on metered data to add where they like. The
    switches are shown read-only, so the customer sees which rows they control
    without a second place to change them."""
    return {
        "type": "custom:sensa-system-data-usage-card",
        "title": "",
        "sensor": "sensor.sensa_nomad_data_usage",
        "switches": {
            "diagnostics": list(_DIAGNOSTICS_SWITCHES.values()),
            "updates": ["switch.sensa_nomad_ota_auto_update"],
            "tracking": ["switch.sensa_nomad_tracking"],
        },
    }


def _device_firmware_rows(
    hass: Any, devices: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """One row per device whose firmware Sensa can install.

    Read off the ENTITY REGISTRY, not the device list: the table shows what an
    `update.*` entity reports, so a row without one would be four dashes. The
    registry is also the only thing that knows the generated entity_id, and it
    persists across reloads, so this survives the build-time race that drops
    entities looked up in `hass.states`.

    Which pause switch a row gets comes from the unique_id, because that is
    where the owning vendor is already encoded (`..._victron_cerbo_...`,
    `..._teltonika_...`). One switch per vendor, except Teltonika, which splits
    router from managed switch: the router carries the van's internet and is a
    separate decision from the switches, and a shared toggle meant pausing one
    paused the other. Router-or-switch is `_teltonika_is_switch`, the same
    predicate the Devices pill and the vendor card read, never a second one. A
    row whose Sensa device cannot be matched falls back to the router toggle,
    which is what a lone Teltonika almost always is.

    The NAME is the one the CUSTOMER sees everywhere else in the app: the Sensa
    registry's `display_name`, matched to the entity through the device_id its
    unique_id carries. HA's own device name is only the fallback. A switch the
    customer calls "TSW202" was showing as "Teltonika Teltonika TSW2XX Switch
    (920b50)" (the HA registry name, complete with a doubled brand) because this
    read the wrong registry.

    An explicit HA-side rename (`name_by_user`) still wins over both: somebody
    who renamed the device in HA meant it.
    """
    if hass is None:
        return []
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        from ..const import (
            DOMAIN,
            TELTONIKA_AUTO_UPDATE_ENTITY_ID,
            TELTONIKA_SWITCH_AUTO_UPDATE_ENTITY_ID,
            VICTRON_AUTO_UPDATE_ENTITY_ID,
        )
    except ImportError:  # pragma: no cover - HA always has these
        return []

    try:
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        entries = list(ent_reg.entities.values())
    except Exception:  # noqa: BLE001 - no registry means no rows, never a raise
        return []

    # (unique_id marker, pause switch, order). The ORDER is the table's, decided
    # here so the card renders the list as given rather than sorting it again:
    # network kit before power kit, and alphabetically-by-accident is not an
    # order anybody chose.
    vendors = (
        ("_teltonika_", TELTONIKA_AUTO_UPDATE_ENTITY_ID, 0),
        ("_victron_cerbo_", VICTRON_AUTO_UPDATE_ENTITY_ID, 1),
        # Sensa's own ESP devices, each under its own switch (resolved below).
        ("_sensa_esp_", "", 2),
        # Zigbee devices with approved firmware. No switch: manual only.
        ("_zigbee_fw_", "", 3),
    )
    from ..esp_update import esp_auto_update_entity_id, esp_device_id_from_unique_id
    from . import _teltonika_is_switch

    rows: list[dict[str, str]] = []
    for entry in entries:
        # Ours, an update entity, and enabled. `platform` is the exact
        # ownership test: another integration's update entity (HACS, HA Core's
        # own) must never turn up in a Sensa table.
        if getattr(entry, "platform", None) != DOMAIN:
            continue
        if entry.domain != "update" or entry.disabled_by is not None:
            continue
        unique_id = entry.unique_id or ""
        vendor = next(
            ((sw, rank) for marker, sw, rank in vendors if marker in unique_id),
            None,
        )
        if vendor is None:
            continue
        auto_switch, rank = vendor
        if "_sensa_esp_" in unique_id:
            esp_device = esp_device_id_from_unique_id(
                unique_id, unique_id.split("_sensa_esp_", 1)[0],
            )
            if not esp_device:
                continue
            auto_switch = esp_auto_update_entity_id(esp_device)
        ha_device = (
            dev_reg.async_get(entry.device_id) if entry.device_id else None
        )
        # The Sensa device whose id appears in this entity's unique_id. Matched
        # by containment rather than by parsing: the two vendors mint different
        # unique_id shapes, and a split() rule would need to know both.
        sensa_name = ""
        sensa_device: dict[str, Any] | None = None
        for dev in devices or []:
            device_id = str(dev.get("device_id") or "")
            if device_id and f"_{device_id}_" in unique_id:
                sensa_name = str(dev.get("display_name") or "").strip()
                sensa_device = dev
                break
        if "_zigbee_fw_" in unique_id:
            # Keyed by IEEE, not a Sensa device id; the entity already carries
            # the Sensa name (or Z2M's), with " Firmware" on the end.
            sensa_name = str(entry.original_name or "").removesuffix(" Firmware")
        if (
            auto_switch == TELTONIKA_AUTO_UPDATE_ENTITY_ID
            and sensa_device is not None
            and _teltonika_is_switch(sensa_device)
        ):
            auto_switch = TELTONIKA_SWITCH_AUTO_UPDATE_ENTITY_ID
        name = (
            (getattr(ha_device, "name_by_user", "") if ha_device else "")
            or sensa_name
            or (getattr(ha_device, "name", "") if ha_device else "")
            or entry.original_name
            or entry.name
            or entry.entity_id
        )
        rows.append({
            "name": name,
            "update_entity": entry.entity_id,
            "auto_switch": auto_switch,
            # Which of the two everyday groups the row folds into: Sensa's own
            # ESP firmware is a Sensa update, a Cerbo or a router is a vendor's.
            "group": "sensa" if "_sensa_esp_" in unique_id else "vendor",
            "_rank": rank,
        })
    # Vendor order first, then name, so a van with two switches lists them
    # predictably inside their vendor block.
    rows.sort(key=lambda r: (r["_rank"], r["name"].lower()))
    return [{k: v for k, v in r.items() if k != "_rank"} for r in rows]


OTA_AUTO_UPDATE_SWITCH = "switch.sensa_nomad_ota_auto_update"
HOST_AUTO_UPDATE_SWITCH = "switch.sensa_nomad_host_auto_update"


def update_switch_groups(
    hass: Any, devices: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """The auto-update switches behind the two everyday groups (Richard,
    2026-10-21): "sensa" is the operating system, Nomad and Sensa's own device
    firmware; "vendor" is other makers' equipment (Cerbo, Teltonika). Read off
    the same rows as the Software updates table, so the groups and the table
    cannot disagree about who is in which."""
    groups: dict[str, list[str]] = {"sensa": [], "vendor": []}
    for eid in (HOST_AUTO_UPDATE_SWITCH, OTA_AUTO_UPDATE_SWITCH):
        if hass is not None and hass.states.get(eid) is not None:
            groups["sensa"].append(eid)
    for row in _device_firmware_rows(hass, devices):
        sw = row.get("auto_switch")
        if sw and sw not in groups[row["group"]]:
            groups[row["group"]].append(sw)
    return groups


def _build_settings_yaml_data() -> dict[str, Any]:
    """Return the managed Nomad Settings dashboard YAML structure.

    May 2026: Settings moved onto the panel + dashboard-shell pattern
    that Habitation uses. Single HA view carrying a
    `custom:sensa-dashboard-shell` with tabs â€” Discovery, Modes,
    Licensing, Diagnostics, then the conditional functional tabs, with
    the Cards gallery last. Header + trial countdown +
    sidebar policy match Habitation so the two dashboards feel like
    parts of the same product, not separate UIs. The Switching tab is
    conditional â€” it appears only when a Sensa Relay Controller is
    registered (labelled "Switching", the relay service class's
    category), mirroring the conditional Network tab. The Cards gallery
    (design reference) is appended after the functional tabs so it
    always sits at the end. The Bluetooth
    view stays dropped; its per-device cards keep living in the
    codebase and can be re-introduced as a tab (or a drawer inside
    Discovery) when the customer asks for it.

    Tab content reuses the existing card builders that the legacy
    multi-view layout shipped with. Each tab's content is built lazily
    inside `_safe_build` so a broken card doesn't kill the whole
    dashboard â€” instead the bad tab renders an error placeholder with
    the traceback.
    """
    # Late-bound on purpose. These live in the package root and are resolved
    # per CALL, exactly as they were when this was all one module: the tests
    # patch them on the package (`patch.object(dashboard, ...)`), and
    # `_dashboard_devices` is REBOUND by `set_dashboard_devices`. A module-level
    # import here would freeze the originals and silently ignore both.
    from . import (
        _LOGGER,
        _SENSA_DEFAULT_LOGO,
        _build_bluetooth_card,
        _build_log_tab,
        _build_nas_card,
        _build_relay_card,
        _current_deployment_mode,
        _dashboard_devices,
        _dashboard_hass,
        _dealer_logo_url,
        _habitation_chips,
        _note_card,
        _switch_ports_config_cards,
        _tab_error_card,
    )

    # Build per-device card lists once up front. Tab builders capture
    # these via closure rather than re-walking `_dashboard_devices`
    # per tab.
    # Asustor NAS config + power-control cards, appended to the Settings â†’
    # Support tab. Empty when no NAS is registered (the tab then emits no NAS
    # cards at all â€” conditional-card rule: nothing rendered when absent).
    nas_cards = [
        _build_nas_card(d)
        for d in _dashboard_devices
        if d.get("integration_type") == "sensa_asustor_ssh"
        or d.get("integration") == "sensa_asustor_ssh"
    ]

    bluetooth_cards = [
        _build_bluetooth_card(d)
        for d in _dashboard_devices
        if d.get("catalog_id") == "sensa_bluetooth_proxy"
        or (
            d.get("integration") == "esphome"
            and d.get("vendor_device_type") == "bluetooth_proxy"
        )
    ]

    relay_cards = [
        _build_relay_card(d)
        for d in _dashboard_devices
        if is_relay_device(d)
    ]

    # Network-switch port CONFIG rows: name + Device Type per port, the same
    # `sensa-relay-channels` card the relay controllers use so the two read as
    # one surface. The tap-to-toggle PoE tiles are the separate control
    # surface on HABITATION -> Switches.
    switch_port_config_cards = _switch_ports_config_cards()

    # Van Level (levelling-service-class) devices. There is no Levelling tab any
    # more (2026-08-10); this list is what gates the Vehicle tab's geometry card,
    # so the numbers only appear when a sensor that uses them is fitted.
    levelling_devices = [
        d for d in _dashboard_devices
        if (
            d.get("service_class") == "levelling"
            or d.get("vendor_device_type") == "van_level"
        )
    ]

    # Bluetooth stays dropped from the panel structure but its card list
    # is still computed so a future tab (or drawer inside Discovery) can
    # pick it up without redoing the filter logic. Suppress the unused-var
    # lint â€” it's load-bearing for the next contributor. (`relay_cards` is
    # now consumed by the conditional Switching tab below.)
    _ = bluetooth_cards

    # Inline tab helpers â€” same pattern as `_build_test_yaml_data`.
    # Isolating tab failures in a placeholder card means a single
    # broken builder doesn't take the whole Settings dashboard down.
    _placeholder = _note_card
    _error_placeholder = _tab_error_card

    def _safe_build(slug: str, label: str, icon: str, builder) -> dict[str, Any]:
        try:
            card = builder()
        except Exception as exc:  # noqa: BLE001 â€” isolate per-tab failures
            _LOGGER.exception(
                "Settings dashboard: tab %r builder raised; surfacing as in-tab error",
                slug,
            )
            card = _error_placeholder(label, exc)
        if card is None:
            _LOGGER.warning(
                "Settings dashboard: tab %r builder returned None; "
                "substituting placeholder. Fix the builder.", slug,
            )
            card = _placeholder(
                f"Tab '{label}' builder returned no card. This is a Sensa "
                "integration bug â€” please report.",
            )
        return {"slug": slug, "label": label, "icon": icon, "card": card}

    def _build_discovery_tab() -> dict[str, Any]:
        # Unified Discovery picker â€” the single full-width card the
        # customer uses to scan, add, rename, remove, ignore and
        # restore every device the integration knows about.
        # Zigbee allowlist: only catalog-known models get the ZIGBEE rule (â†’
        # Discovered/addable); every other Z2M device falls to Others (the
        # discovery sets `device_subtype` to the catalog model for known ones,
        # blank otherwise â€” so this rule keys off it). Grows by catalog data.
        from ..vendors.zigbee2mqtt import (
            known_zigbee_models,
            zigbee_coordinator_integrations,
        )
        _known_zb_models = known_zigbee_models()
        # Sentinel subtype the discovery stamps on an unrecognised batch; its
        # own rule below makes such a device addable instead of hidden.
        from ..const import ZIGBEE_UNRECOGNISED_SUBTYPE as _ZB_UNRECOGNISED
        # Every coordinator's integration slug (smlight + sonoff_dongle_m + any
        # future model) so the COORDINATOR rule below matches all of them â€” a
        # coordinator with no rule falls to the hidden Others group.
        _coordinator_integrations = zigbee_coordinator_integrations()
        config: dict[str, Any] = {
            "type": "custom:sensa-device-picker",
            "source_entity": "sensor.sensa_nomad_discovery",
            "source_attribute": "devices",
            "id_key": "id",
            "filter": [
                # ESPHome heaters (Autoterm diesel / Alde hydronic) carry a
                # catalog `device_subtype`, so this MORE-SPECIFIC rule must
                # precede the generic ESPHome PROXY catch-all below (first
                # match wins) â€” otherwise a heater would be mislabelled PROXY.
                {"integration": ["esphome"], "device_subtype": ["air_heater", "hydronic_heater"], "tag": "HEATER", "tag_color": "warning"},
                # An ESPHome-bridged rooftop AC (Dometic FreshJet) carries the
                # catalog `device_subtype: air_conditioner`; keep it before the
                # generic PROXY catch-all so it gets an AC pill, not PROXY. The
                # Velit's AC rule keys on its `sensa_velit_rooftop_ac` vendor
                # integration and won't match this esphome-integration device.
                {"integration": ["esphome"], "device_subtype": ["air_conditioner"], "tag": "AC", "tag_color": "warning"},
                # The FreshJet BRIDGE (the ESP that HA adopts) is the Level-1
                # infrastructure device; its synthesised AC child (device_subtype
                # air_conditioner) nests under it and takes the AC pill above.
                # Distinct device_subtype so the bridge gets a BRIDGE pill, not AC.
                {"integration": ["esphome"], "device_subtype": ["air_conditioner_bridge"], "tag": "BRIDGE", "tag_color": "network"},
                # Level 1 â€” network infrastructure
                # The Sensa Core (the App backend/server itself) â€” an
                # always-present self-row auto-added at setup (sensor.
                # _synthesise_core_device), never scanned/added by the customer.
                {"integration": ["sensa_core"], "tag": "CORE", "tag_color": "network"},
                {"integration": ["esphome"], "tag": "PROXY", "tag_color": "network"},
                {"integration": ["sensa_teltonika_api"], "device_subtype": ["router"], "tag": "ROUTER", "tag_color": "network"},
                {"integration": ["sensa_teltonika_api"], "device_subtype": ["switch"], "tag": "SWITCH", "tag_color": "network"},
                {"integration": ["sensa_teltonika_api"]},
                {"integration": ["sensa_starlink_grpc"], "tag": "SATELLITE", "tag_color": "network"},
                # Lucky Miner LV08 Pro: surfaced via its own HTTP-probe sweep
                # (source static_probe, no manufacturer_id, no device_subtype),
                # so like the Velit/WiCAN rows below it's gated on the
                # backend-stamped integration. Without this rule a fully
                # recognised miner falls to Others (hidden unless debug mode)
                # instead of the addable Discovered group, which is exactly
                # what happened before this rule existed: discovery found and
                # correctly identified both simulated miners every cycle, and
                # neither ever reached the picker.
                {"integration": ["sensa_luckyminer_api"], "tag": "MINER", "tag_color": "warning"},
                # Asustor Flashstor NAS: also discovered by static_probe (fixed
                # host, no manufacturer_id, no device_subtype), so it needs the
                # same integration-gated rule the miner above needs and for the
                # same reason: without it a fully-recognised NAS falls to the
                # hidden Others group.
                {"integration": ["sensa_asustor_ssh"], "tag": "NAS", "tag_color": "network"},
                # IP camera: discovered by the RTSP sweep (source static_probe,
                # no manufacturer_id, no device_subtype), so it needs the same
                # integration-gated rule as the miner and the NAS, or it falls
                # to the hidden Others group.
                {"integration": ["sensa_rtsp_camera"], "tag": "CAMERA", "tag_color": "network"},
                # Shelly devices: found by the `/shelly` HTTP sweep (source
                # static_probe, no manufacturer_id), so gated on integration
                # like the miner, NAS and camera, or they fall to the hidden
                # Others group (shipped without this rule at first). The smoke
                # detector carries the catalog `device_subtype: smoke_sensor`
                # and must come first, as must the RGBW PM light controller
                # (`device_subtype: lighting`); any other Shelly is a relay.
                {"integration": ["shelly"], "device_subtype": ["smoke_sensor"], "tag": "SMOKE", "tag_color": "warning"},
                {"integration": ["shelly"], "device_subtype": ["lighting"], "tag": "LIGHT", "tag_color": "switching"},
                {"integration": ["shelly"], "tag": "RELAY", "tag_color": "switching"},
                {"integration": _coordinator_integrations, "tag": "COORDINATOR", "tag_color": "network"},
                # Network speaker adopted via HA's native `sonos` integration
                # (cooperate-flow, source native_zeroconf â€” no manufacturer_id).
                {"integration": ["sonos"], "tag": "SPEAKER", "tag_color": "network"},
                # Zigbee devices surfaced via Zigbee2MQTT (source mqtt_zigbee,
                # owned by HA's shared `mqtt` integration â€” no manufacturer_id).
                # One rule by integration; per-class tags can gate on
                # service_class later if wanted.
                {"integration": ["zigbee2mqtt"], "device_subtype": _known_zb_models, "tag": "ZIGBEE", "tag_color": "network"},
                # A Z2M device whose model matches no catalog entry but whose
                # entities classify cleanly — a new Tuya batch of a product we
                # do support. Without this rule it falls to the debug-gated
                # Others group and the customer never sees it at all, which is
                # exactly how a working set of controllers went missing on a
                # customer box. Addable, tagged in warning colour so it reads as
                # "identified by what it can do, not by our catalog".
                {"integration": ["zigbee2mqtt"], "device_subtype": [_ZB_UNRECOGNISED], "tag": "ZIGBEE?", "tag_color": "warning"},
                # Level 2 â€” end-user devices
                {"manufacturer_id": ["0x0499"], "tag": "SENSOR", "tag_color": "environment"},
                {"manufacturer_id": ["0xEC88"], "tag": "SENSOR", "tag_color": "environment"},
                {"manufacturer_id": ["0x0B65"], "tag": "BMS", "tag_color": "electrical"},
                {"manufacturer_id": ["0x0201"], "tag": "BMS", "tag_color": "electrical"},
                {"manufacturer_id": ["0xC1A4"], "tag": "BMS", "tag_color": "electrical"},
                {"manufacturer_id": ["0xE14C"], "tag": "BMS", "tag_color": "electrical"},
                # Registered BMS rows lose manufacturer_id post-enrichment;
                # gate on integration so the BMS pill survives the flip.
                {"integration": ["sensa_bms_ble"], "tag": "BMS", "tag_color": "electrical"},
                # DJI Power station (manufacturer 0x08AA = SZ DJI Technology).
                {"manufacturer_id": ["0x08AA"], "tag": "POWER STATION", "tag_color": "electrical"},
                {"integration": ["sensa_dji_ble"], "tag": "POWER STATION", "tag_color": "electrical"},
                # Velit Rooftop AC is discovered by service UUID + "KT" name, so
                # its advert carries NO manufacturer_id â€” none of the mfr-id
                # rules match it. Gate on the backend-stamped integration (same
                # pattern as the BMS rule above); without this rule a fully
                # recognised Velit falls to Others instead of Discovered.
                {"integration": ["sensa_velit_rooftop_ac"], "tag": "AC", "tag_color": "warning"},
                # WiCAN Pro OBD2 â€” surfaced via HA's shared `mqtt` integration
                # (source wican_mqtt, no manufacturer_id), so it's gated on the
                # backend-stamped integration like the Velit/BMS rows. Without
                # this rule a fully-recognised WiCAN falls to Others (hidden
                # unless debug mode) instead of the addable Discovered group.
                {"integration": ["wican"], "tag": "OBD2", "tag_color": "default"},
                # Mopeka bottle sensor. Its company ID (0x0059) is Nordic's and
                # shared by unrelated devices, so the rule keys on the
                # integration the profile stamps (mfr id + 0xFEE5 service UUID),
                # never on 0x0059 alone. Without it a Mopeka falls to Others.
                {"integration": ["mopeka"], "tag": "TANK", "tag_color": "default"},
                # Tyre-pressure sensors share no company ID (DJTPMS even
                # changes its own), so the pill keys on the decoded subtype
                # (discovered rows) and the integration (registered rows).
                {"device_subtype": ["tpms_djtpms", "tpms_tpmsii", "tpms_sytpms", "tpms_michelin", "tpms_itpms", "tpms_fobo"], "tag": "TYRE", "tag_color": "default"},
                {"integration": ["sensa_tpms_ble"], "tag": "TYRE", "tag_color": "default"},
                # Victron 0x02E1 covers many products; per-subtype rules
                # so unsupported variants (Multiplus, Phoenix) drop to Others.
                {"manufacturer_id": ["0x02E1"], "device_subtype": ["smartshunt"], "tag": "MONITOR", "tag_color": "electrical"},
                {"manufacturer_id": ["0x02E1"], "device_subtype": ["smartsolar"], "tag": "CHARGER", "tag_color": "electrical"},
                {"manufacturer_id": ["0x02E1"], "device_subtype": ["cerbo"], "tag": "NEEDS NETWORK", "tag_color": "warning"},
                # A Cerbo GX's Venus sub-devices are synthesised as read-only
                # child rows nested under the Cerbo (sensor._synthesise_cerbo_children),
                # each carrying the Venus service as `device_subtype`. These
                # per-service rules MUST precede the GATEWAY catch-all (first
                # match wins) so the Cerbo itself (no device_subtype) still gets
                # GATEWAY while its children get a role-specific pill.
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["battery"], "tag": "BATTERY", "tag_color": "electrical"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["solarcharger", "dcdc"], "tag": "CHARGER", "tag_color": "electrical"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["vebus"], "tag": "INVERTER", "tag_color": "electrical"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["grid"], "tag": "METER", "tag_color": "electrical"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["pvinverter"], "tag": "PV", "tag_color": "electrical"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["genset"], "tag": "GENERATOR", "tag_color": "electrical"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["alternator"], "tag": "ALTERNATOR", "tag_color": "electrical"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["dcsystem"], "tag": "DC", "tag_color": "electrical"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["tank"], "tag": "TANK", "tag_color": "default"},
                {"integration": ["sensa_victron_mqtt"], "device_subtype": ["temperature"], "tag": "SENSOR", "tag_color": "environment"},
                {"integration": ["sensa_victron_mqtt"], "tag": "GATEWAY", "tag_color": "electrical"},
                {"manufacturer_id": ["0x02E1"], "device_subtype": ["orion_xs"], "tag": "CHARGER", "tag_color": "electrical"},
                # Victron Smart Lithium (managed LiFePO4 battery) â€” adopted
                # via HA core's victron_ble. Tagged BMS like the other
                # leisure batteries so it lands in Discovered, not Others.
                {"manufacturer_id": ["0x02E1"], "device_subtype": ["smartlithium"], "tag": "BMS", "tag_color": "electrical"},
                # Victron Smart BatteryProtect (low-voltage load disconnect) â€”
                # adopted read-only via HA core's victron_ble. Tagged so it
                # lands in Discovered, not Others.
                {"manufacturer_id": ["0x02E1"], "device_subtype": ["battery_protect"], "tag": "PROTECT", "tag_color": "electrical"},
            ],
            "columns": [
                {"key": "display_name", "label": "Name"},
                {"key": "category", "label": "Category"},
                {"key": "protocol", "label": "Protocol"},
                {"key": "address", "label": "Address"},
            ],
            "scan_button": {
                "entity": "button.sensa_nomad_scan",
                "label": "Scan",
            },
            "config_entity": {
                "entity": "text.sensa_nomad_scan_subnet",
                "label": "Subnet",
                "placeholder": "192.168.1.0/24,192.168.2.0/24",
            },
            "action": {
                "service": "sensa_nomad.add_devices",
                "data_key": "ids",
            },
            "remove_action": {
                "service": "sensa_nomad.remove_devices",
                "data_key": "ids",
            },
            "rename_action": {
                "service": "sensa_nomad.rename_device",
                "id_key": "id",
                "name_key": "display_name",
                "reset_service": "sensa_nomad.reset_device_name",
            },
            "ignore_action": {
                "service": "sensa_nomad.ignore_device",
                "data_key": "id",
            },
            "restore_action": {
                "service": "sensa_nomad.restore_device",
                "data_key": "id",
            },
            "ignored_group_label": "Ignored",
            # Level 1/2 nesting: friendly labels for the "under <X>" sub-header
            # shown when a Level-2 child's Level-1 parent sits in a different
            # state group (e.g. an Added Bluetooth proxy with a still-Discovered
            # sensor). Keyed by the child's `parent_kind`. Data-driven so a new
            # provider kind (future ir_transceiver) is a one-line addition here.
            "nest_labels": {
                "bluetooth_proxy": "Bluetooth",
                "zigbee_coordinator": "Zigbee",
                "freshjet_bridge": "FreshJet FJX/FJZ Bridge",
                "cerbo": "Cerbo",
            },
            "button_label": "Add Selected Devices",
            "empty_message": (
                "Nothing found yet. Press Scan to search for devices."
            ),
            "debug_entity": "switch.sensa_nomad_debug_mode",
            # BLE Capture: with debug on (which also lists the Bluetooth
            # devices we do not support) and an nRF sniffer dongle plugged in,
            # each unadded Bluetooth row gets Start BLE Capture, and the
            # capture panel (markers, Stop, Download) sits above the list.
            "ble_capture": True,
        }
        # Only offer Nomad-driven Zigbee pairing when BOTH the SLZB-06U
        # coordinator is adopted AND a Zigbee2MQTT bridge is running â€” otherwise
        # the button would open a permit-join window with nothing to pair
        # against. The bridge is a software device that lingers in HA's mqtt
        # registry after the coordinator is removed, so we also require the
        # coordinator's `smlight` config entry to be present (removing the
        # coordinator from Discovery deletes it â†’ the button disappears).
        # Pressing it publishes to Z2M's permit-join API so a device joins on
        # its physical pair-button press, with no trip into the Z2M add-on.
        try:
            from ..vendors.zigbee2mqtt import (
                zigbee2mqtt_present,
                zigbee_coordinator_present,
            )

            _have_hass = _dashboard_hass is not None
            _coord = _have_hass and zigbee_coordinator_present(_dashboard_hass)
            _z2m = _have_hass and zigbee2mqtt_present(_dashboard_hass)
            if _coord and _z2m:
                config["pair_button"] = {
                    "entity": "button.sensa_nomad_pair_zigbee",
                    "label": "Pair Zigbee",
                    "icon": "mdi:zigbee",
                }
            else:
                # Only log when the button is HIDDEN (the case worth diagnosing)
                # â€” it pinpoints which gate is False, e.g. a SONOFF Dongle-M that
                # Z2M uses but that isn't a Sensa-registered coordinator. A
                # healthy system that shows the button stays quiet.
                _LOGGER.info(
                    "discovery tab: Pair Zigbee button hidden â€” hass=%s "
                    "coordinator_present=%s zigbee2mqtt_present=%s",
                    _have_hass, _coord, _z2m,
                )
        except Exception:  # pragma: no cover - defensive, never block the tab  # noqa: BLE001
            _LOGGER.debug("discovery tab: Z2M presence check failed", exc_info=True)
        return config

    def _build_diagnostics_tab() -> dict[str, Any]:
        # Laid out by the native `sensa-card-grid` shell rather than a
        # built-in `vertical-stack` so the interactive File Integrity card
        # DIRECT-CREATES with no Lovelace engine. The engine loads
        # unreliably on the Pi panel, which left the old built-in "Run
        # Check" button unpressable (and could hang the whole tab) â€” see
        # docs/known-issues.md. The integrity card is now a native
        # `custom:sensa-integrity-card`.
        # Layout: a SINGLE consolidated card (2026-06-28) â€” the integrity
        # card renders a Licence section above File Integrity (via its `licence:`
        # config), so the licence detail and integrity controls live in one card
        # instead of two. It ALSO hosts (2026-07-06) the licence-recovery input
        # (appliance-only) and the "Analytics & Diagnostics" consent panel â€” both
        # MOVED here off the `admin`-slug tab, so the all-flavours Diagnostics tab
        # owns everything a customer needs regardless of install type (the
        # `admin`-slug tab is Container-appliance only). Any registered NAS
        # config/power cards are appended at the bottom.
        #
        # The Name field and Factory Reset MOVED to the `admin`-slug tab's
        # System panel (2026-09-03) â€” Container-appliance only now; see
        # `_build_admin_tab`.
        from ..const import (
            ACTIVATE_LICENCE_ENTITY_ID,
            DEPLOYMENT_MODE_APPLIANCE,
            LICENCE_CODE_ENTITY_ID,
            LOG_RETENTION_ENTITY_ID,
        )
        appliance = _current_deployment_mode() == DEPLOYMENT_MODE_APPLIANCE
        integrity_card: dict[str, Any] = {
            "type": "custom:sensa-integrity-card",
            "title": "File Integrity",
            "sensor": "sensor.sensa_nomad_integrity",
            "button": "button.sensa_nomad_integrity_check",
            # Span the full panel width rather than the capped+centred
            # single-card slot (480px). The card's own `@container
            # (min-width: 560px)` query needs >=560px to put Licence +
            # File Integrity side-by-side on the top row; the 480px single
            # cap could never reach it, so all three sections stacked
            # (Richard, 2026-06-28). The card caps its OWN width so it
            # doesn't sprawl on a wide desktop.
            "full_width": True,
            # The OTA mirror (the backups-stale sensor carries the live `ota_*`
            # attributes). The card disables Run Check while an update is in
            # flight â€” the update rewrites the files being hashed, so a check
            # run mid-update reports false "modified"/"missing" failures. The
            # server refuses one too; this is the visible half. Harmless on
            # HAOS/BYO, where `ota_state` is absent (never in flight).
            "ota_sensor": "binary_sensor.sensa_nomad_backups_stale",
            "licence": "sensor.sensa_nomad_licence",
            # Host resources (CPU / memory / disk %) â€” the card renders a
            # Resources bar section when these sensors are present (fed by
            # the system-resources poller; absent on an old add-on â†’ the
            # section is simply omitted).
            "resources": {
                "cpu": "sensor.sensa_nomad_cpu_use",
                "memory": "sensor.sensa_nomad_memory_use",
                "disk": "sensor.sensa_nomad_disk_use",
                # CPU temperature (the Pi hardware poller's sensor), shown as a
                # fixed 30-95 degC gradient bar under the CPU usage bar (green
                # to 65, amber to 80, red above). The card omits the row when
                # the sensor never gets a reading (a non-Pi host).
                "temperature": "sensor.sensa_nomad_cpu_temperature",
                # Board power (PMIC rail total, watts), shown as a "Core Power"
                # bar under CPU Temp on a fixed 0-15 W scale, neutral colour
                # (no danger threshold declared). Same self-omit rule.
                "power": "sensor.sensa_nomad_board_power",
            },
            # Per-app (per-container) usage — rendered INSIDE the Resources panel
            # (they answer the same question), not beside it. The
            # card reads the `apps` attribute; the refresh pill writes
            # the interval select. Both omit gracefully when absent (an
            # old add-on / no Supervisor â†’ empty list â†’ panel hidden).
            "app_usage": "sensor.sensa_nomad_app_usage",
            "app_usage_interval": "select.sensa_nomad_app_usage_interval",
            # Analytics & Diagnostics consent panel â€” five per-category toggles
            # (all OFF by default). MOVED off the `admin`-slug tab's card 2026-07-06
            # so BYO + HAOS (which no longer have that tab) keep it. Switches are
            # created on EVERY flavour, so the panel is always offered. Keys map
            # 1:1 to diagnostics_report.CATEGORIES.
            "diagnostics": dict(_DIAGNOSTICS_SWITCHES),
            # How far back the Intelligence → Log tab's history goes. Rendered
            # under a History sub-heading at the bottom of the File Integrity
            # panel (moved off Analytics & Diagnostics 2026-08-10): it answers
            # "what does my system keep, and for how long?", and unlike every row
            # of the consent panel, nothing about it leaves the box. NOT a storage
            # control: the log store has always been a capped ring buffer, so
            # this only ever shortens the window, never bounds a leak.
            "log_retention": LOG_RETENTION_ENTITY_ID,
        }
        # Licence-recovery input (code entry + Activate) â€” APPLIANCE-ONLY. Moved
        # here off the `admin`-slug tab 2026-07-06 (that tab is Container-appliance
        # only, so recovery must live on the all-flavours Diagnostics tab or an HAOS
        # customer in a read-only lockout would be stranded). A BYO customer is a
        # full HA admin and re-registers via Devices & Services, so they don't
        # see it. NOT licence-gated â€” text/button aren't actuation, so a
        # read-only-locked customer can still recover.
        # On the Container appliance both recovery and "Check in now" moved on
        # to the System tab (2026-09-24), since this tab is behind Nerd Mode;
        # HAOS (no System tab) keeps them here. `container_appliance` is the
        # enclosing builder's, so the two tabs cannot disagree on who has which.
        if container_appliance:
            integrity_card["check_in"] = False
        elif appliance:
            integrity_card["licence_recovery"] = {
                "code_entity": LICENCE_CODE_ENTITY_ID,
                "activate_button": ACTIVATE_LICENCE_ENTITY_ID,
            }
        cards: list[dict[str, Any]] = [integrity_card]
        # Append any registered NAS config/power cards. Conditional-card rule:
        # when no NAS is registered, `nas_cards` is empty and nothing extra is
        # emitted â€” no placeholder.
        if nas_cards:
            cards.extend(nas_cards)
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            # A support page, not a layout: Design Mode leaves it alone.
            "arrangeable": False,
            "cards": cards,
        }

    def _build_sensa_tab() -> dict[str, Any]:
        # Settings â†’ Sensa: STAFF ONLY, for use while remotely logged in as
        # `sensa_support`. The caller gates it with `require_sensa_staff`, which
        # the frontend resolves from the panel_config WS `can_sensa` flag; that
        # is the support USERNAME, deliberately not `can_admin` (any HA admin
        # satisfies that, and on BYO the HA admin is the customer).
        #
        # Reuses `sensa-admin-card` with only the `containers` block configured:
        # every other section self-omits when its config is absent, so the card
        # renders as a Containers-only panel without a new card type.
        #
        # `containers` is emitted UNCONDITIONALLY, never gated on
        # `states.get(sensor)`: the sensor may register just after this build on
        # first boot, which would drop the block and leave the panel permanently
        # absent until a manual reload (the on-device 2026-07-07 "panel never
        # shows" trap). The card self-omits while the live `containers` attribute
        # is empty and appears the instant the poller fills it, with no rebuild.
        # Buttons call the gated `sensa_nomad.container_action`, which is
        # policy-enforced server-side regardless of who can see the tab.
        from ..const import DOMAIN as _DOMAIN
        from ..const import UPDATE_CHANNEL_SELECT_ENTITY_ID

        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            "cards": [
                {
                    "type": "custom:sensa-admin-card",
                    # Release channel — STAFF ONLY, which is why it is here and
                    # not on the customer's System tab (the `admin`-slug tab)
                    # beside the update rows. Putting a customer on beta (or
                    # pulling them back to stable) is a support decision, so it
                    # gates on `can_sensa` via this tab's `require_sensa_staff`,
                    # never on `can_admin` — any HA admin satisfies that, and on
                    # BYO the HA admin IS the customer. The customer still SEES
                    # their channel, as a read-only row on the System tab.
                    "channel": {
                        "select": UPDATE_CHANNEL_SELECT_ENTITY_ID,
                        "status_sensor": "binary_sensor.sensa_nomad_backups_stale",
                        # Install the channel's current build. Emitted
                        # UNCONDITIONALLY, like `containers` below: the button
                        # entity may register just after this build on first
                        # boot, and a build-time `states.get()` would drop it
                        # from the stored config until a manual regen. The card
                        # self-omits the row while the box has not said what its
                        # channel resolves to.
                        "install_button": "button.sensa_nomad_install_channel_build",
                    },
                    "containers": {
                        "sensor": "sensor.sensa_nomad_containers",
                        "service": f"{_DOMAIN}.container_action",
                    },
                    "full_width": True,
                },
            ],
        }

    def _build_admin_tab() -> dict[str, Any]:
        # Settings â†’ System (labelled "Admin" 2026-07-06 to 2026-09-03, briefly
        # "Supervisor" before that; slug stays `admin`): the host/stack-management
        # tasks a Container-appliance customer needs, surfaced for the non-HA-admin
        # `admin` login â€” system units/timezone/theme, restart/reboot, the
        # customer-login passwords, and the Sensa-driven Host-OS + Software
        # updates. The card calls `sensa_nomad.*` services that run server-side
        # after a Sensa-admin check (admin.py), so the customer can use them
        # without being an HA admin. CONTAINER-APPLIANCE ONLY (2026-07-06) â€” the
        # caller only appends this tab when deployment_mode is appliance AND the
        # box is NOT supervised: on HAOS the `admin` login is a full HA
        # administrator who uses HA's native settings for units/theme/passwords,
        # so these panels are redundant there; on BYO the customer is a full HA
        # admin too. The Licence detail + recovery and the Analytics & Diagnostics
        # consent panels MOVED to the all-flavours Diagnostics tab (2026-07-06),
        # which is why this tab can be Container-only without stranding BYO/HAOS.
        # The Name field and Factory Reset MOVED here from the Diagnostics tab
        # (2026-09-03) onto the System panel, so BYO/HAOS no longer have either.
        from ..config_flow import EXPERIMENTAL_DEVICES_ENTITY_ID
        from ..const import (
            APPLIANCE_KIOSK_USERNAMES,
            DEPLOYMENT_MODE_APPLIANCE,
            HOST_UPDATE_WINDOW_SELECT_ENTITY_ID,
            REMOTE_ACCESS_SWITCH_ENTITY_ID,
            SYSTEM_NAME_ENTITY_ID,
            SYSTEM_NAME_MAX_LENGTH,
        )
        from ..const import (
            DOMAIN as _DOMAIN,
        )
        appliance = _current_deployment_mode() == DEPLOYMENT_MODE_APPLIANCE
        # Kiosk logins only. This tab is Container-appliance only, and the
        # Container builder stopped creating an `admin` login (2026-09-03) now
        # that Settings is open to every login behind the Settings PIN, so
        # listing it here would offer a password change for an account that
        # does not exist.
        users = (
            [
                # Usernames shown verbatim (lowercase) so the label matches the
                # actual login name the customer types.
                {"username": u, "name": u.lower()}
                for u in APPLIANCE_KIOSK_USERNAMES
            ]
            if appliance
            else []
        )
        # The WHOLE System tab (the `admin`-slug tab) is ONE responsive multi-panel
        # card (Richard, 2026-07-02 â€” like the Diagnostics tab's integrity card):
        # the admin card renders System / Passwords / Host-OS updates / Software updates / Remote
        # support as @container panels that reflow 2-up â†” 1-up by width, so panels
        # of differing heights sit in a tidy grid instead of a stack of separate
        # cards that clash at certain widths. Panels are opt-in via config, so
        # each appears only where its entities exist.
        admin_card: dict[str, Any] = {
            "type": "custom:sensa-admin-card",
            "appliance": appliance,
            "users": users,
        }

        # Editable name for this system / vehicle, shown on the System panel.
        # MOVED off the Diagnostics tab's integrity card (2026-09-03), so this is
        # now Container-appliance only. The integration pushes it to the App,
        # which reports it on phone-home so the Hub fleet view can label the
        # deployment. NOT licence-gated.
        admin_card["system_name"] = {
            "entity": SYSTEM_NAME_ENTITY_ID,
            "max_length": SYSTEM_NAME_MAX_LENGTH,
        }

        # Factory Reset, rendered as its own full-width panel. MOVED off the
        # Diagnostics tab (2026-09-03), so this is now Container-appliance only.
        # The gate is the server-side `_require_sensa_admin` on
        # `sensa_nomad.factory_reset` (an HA admin, the appliance `admin` login,
        # or a live Settings-PIN elevation).
        admin_card["factory_reset"] = {"service": f"{_DOMAIN}.factory_reset"}

        # Licence panel: "Check in now" and licence recovery, MOVED here off the
        # Diagnostics tab (2026-09-24) once that tab went behind Nerd Mode, so a
        # customer locked out by their licence finds recovery without it. This
        # tab is Container-appliance only; HAOS and DIY keep both on Diagnostics
        # (`_build_diagnostics_tab`). Not licence-gated.
        from ..const import ACTIVATE_LICENCE_ENTITY_ID, LICENCE_CODE_ENTITY_ID
        admin_card["licence"] = {
            "sensor": "sensor.sensa_nomad_licence",
            "code_entity": LICENCE_CODE_ENTITY_ID,
            "activate_button": ACTIVATE_LICENCE_ENTITY_ID,
        }

        # Experimental Devices toggle on the System panel. Passed
        # UNCONDITIONALLY: gating on a build-time states.get() would drop the row
        # whenever the dashboard is generated before the switch platform has
        # added the entity, and it would stay missing until the next regen (the
        # Remote Support race). The card drops the row itself if the entity is
        # genuinely absent at render time.
        admin_card["experimental_devices"] = EXPERIMENTAL_DEVICES_ENTITY_ID

        # Curated theme allow-list for the System-panel Theme picker: the public
        # Sensa themes + the licence's dealer theme (per theme_mode), NOT every
        # theme installed on the box. The card filters its <select> to these;
        # the gated set_theme service enforces the same set server-side.
        if _dashboard_hass is not None:
            from ..theme_catalog import allowed_theme_names, locked_theme_name

            admin_card["theme_options"] = allowed_theme_names(_dashboard_hass)
            # A dealer running `theme_mode: locked` gets no Theme row at all.
            # Both keys come off the same predicate so the picker and the
            # server-side gate can never disagree about what is selectable.
            if locked_theme_name(_dashboard_hass) is not None:
                admin_card["theme_locked"] = True

        # Software updates panel â€” appliance-only; gate on the OTA toggle existing
        # (created only on appliances). Versions are read LIVE by the card from
        # the backup sensor's attributes (they prime after the dashboard builds).
        ota_switch = "switch.sensa_nomad_ota_auto_update"
        if _dashboard_hass is not None and _dashboard_hass.states.get(ota_switch):
            admin_card["updates"] = {
                "version_sensor": "binary_sensor.sensa_nomad_backups_stale",
                "auto_switch": ota_switch,
                "apply_button": "button.sensa_nomad_apply_update",
                "check_button": "button.sensa_nomad_check_updates",
                # Per-version release-notes links (installed + latest) â€” the card
                # builds <base>/nomad-<version-slug> for each.
                "release_notes_base": "https://docs.sensa.systems/releases/nomad",
            }

        # Host-OS (Layer 1) updates panel â€” appliance-only, gated on the host
        # auto-update toggle existing (created only on appliances). Status is read
        # LIVE by the card from the backup sensor's host_update_* attributes.
        host_switch = "switch.sensa_nomad_host_auto_update"
        if _dashboard_hass is not None and _dashboard_hass.states.get(host_switch):
            admin_card["host_updates"] = {
                "auto_switch": host_switch,
                "status_sensor": "binary_sensor.sensa_nomad_backups_stale",
                # Customer maintenance-window picker (appliance-only select).
                "window_select": HOST_UPDATE_WINDOW_SELECT_ENTITY_ID,
            }

        # Third-party device firmware (Layer 4): one row per piece of kit in the
        # Software updates table, beside Sensa and the operating system, because
        # they answer the same customer question. Built from the entity registry
        # rather than from the device list, so a row only exists when the update
        # entity it reads actually does.
        device_rows = _device_firmware_rows(_dashboard_hass, _dashboard_devices)
        if device_rows:
            admin_card["device_updates"] = {"rows": device_rows}

        # Remote Support panel. Passed UNCONDITIONALLY (2026-07-20). This tab is
        # already Container-appliance only, so BYO never sees it anyway, and the
        # old `states.get(rs_switch)` guard was a BUILD-TIME state read: the
        # dashboard is generated during setup, so if the switch platform had not
        # finished adding the entity yet the panel was dropped from the built
        # config and stayed missing until the next regen, with nothing to say
        # why. Same class of race as the deployment_mode boot race.
        #
        # The card now renders the section whether or not the entity resolves,
        # showing a disabled toggle and an explanation when it is unavailable, so
        # "not provisioned" and "not ready yet" are both visible states rather
        # than an empty grid cell.
        # A dict, not the bare entity string it used to be: the card now shows
        # when the session ends, who opened it and why, all of which it reads
        # from the switch's attributes. The card still accepts the old string
        # form, so a stored dashboard config from before this change keeps
        # working and simply shows the toggle alone.
        admin_card["remote_support"] = {
            "switch": "switch.sensa_nomad_remote_support",
        }

        # Admin Lock, leading the Passwords panel above the PIN fields it
        # governs. Passed UNCONDITIONALLY for the same build-time-race reason
        # as remote_support/remote_access above: this tab is already
        # Container-appliance only, so a missing entity here would mean the
        # switch platform just hadn't finished adding it yet, not that the
        # feature doesn't apply.
        admin_card["admin_lock"] = "switch.sensa_nomad_admin_lock"

        # Remote Access sits with it, and for the same reason is passed
        # UNCONDITIONALLY: this tab is Container-appliance only, which is exactly
        # the set of boxes the cloud will mint a tunnel for, and a build-time
        # `states.get` here would be the same platform-not-ready race the note
        # above describes. The card renders the section either way and explains
        # an unresolved entity rather than vanishing.
        #
        # The hostname is NOT passed: it is not known when the dashboard is
        # built, and it changes without a regen. The card reads it live from the
        # licence sensor, which carries the App's `cloudflare` block.
        admin_card["remote_access"] = {
            "switch": REMOTE_ACCESS_SWITCH_ENTITY_ID,
            "licence_sensor": "sensor.sensa_nomad_licence",
        }

        # (The Licence detail + recovery and the Analytics & Diagnostics consent
        # panels were MOVED to the Diagnostics tab's integrity card 2026-07-06 â€” see
        # `_build_diagnostics_tab`. They live there because the Diagnostics tab is
        # all-flavours whereas this System tab (the `admin`-slug tab) is
        # Container-appliance only.)

        # (Containers MOVED to the staff-only Sensa tab 2026-07-20 â€” see
        # `_build_sensa_tab`. It is a docker-level view of the stack: useful to
        # Sensa while remotely logged in, meaningless to the owner.)

        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            # One whole-page card: nothing for Design Mode to arrange or size.
            "arrangeable": False,
            # full_width so the lone card spans the grid (else card-grid caps a
            # single card at 480px and the 620px 2-up @container breakpoint could
            # never fire â€” the same trap the Support integrity card hit); the
            # admin card self-caps its own max-width.
            "cards": [{**admin_card, "full_width": True}],
        }

    def _build_switching_tab() -> dict[str, Any]:
        # One control card per Sensa Relay Controller (channel name +
        # enabled toggle + ESPHome relay toggle table), laid out by the
        # same `sensa-card-grid` shell the Network and BMS tabs use via
        # its generic `cards` slot â€” auto-fit grid, a lone card capped +
        # centred rather than stretched full-width. `hide_header` because
        # the dashboard shell already renders the Sensa header strip above
        # the tabs. Labelled "Switching" (the relay service class's
        # category) rather than "Relays". Only reached when `relay_cards`
        # is non-empty (the tab is gated below).
        # `columns: "1fr 1fr"` caps the layout at TWO cards per row (Richard):
        # the default auto-fit grid packs 3-4 relay-controller cards across a
        # wide panel, which reads cramped. Two equal tracks fold to a single
        # column on a narrow container (the `.body.cols` container query), and a
        # lone card still gets the centred single-card treatment (`single` wins
        # over `cols` in the shell), so this only bites at 2+ cards.
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            "columns": "1fr 1fr",
            # Design Mode may put the controller cards in a new order, but
            # not resize, remove or add any (Richard, 2026-09-30).
            "arrangeable": True,
            "move_only": True,
            "cards": relay_cards + switch_port_config_cards,
        }

    # (The Settings -> Level tab is GONE, 2026-08-10. It was emptied a card at a
    # time onto the surfaces where each piece is actually used, and once the last
    # card had a better home the tab was a heading with nothing under it:
    #
    #   - Vehicle measurements (wheel track / wheelbase / shim tolerance)
    #     -> Settings -> Vehicle. Singletons: one van, one wheelbase.
    #   - Orientation -> the sensor's own row in Settings -> Devices. It
    #     describes ONE unit's physical install, not the vehicle.
    #   - Raw Sensor + Calibration -> Cab -> Level, beside the readings they
    #     explain. Calibrate is pressed while parked on ground the customer
    #     can see is flat, which is the moment they are on that tab.
    #   - The van pictures (top-down / side / rear) -> the levelling sensor's
    #     row in Settings -> Devices too, as `mode: "image"` config fields
    #     (`sensor._config_entities_for_device`).
    #
    # Nothing about levelling is configured on a tab of its own any more.)

    def _build_backup_tab() -> dict[str, Any]:
        # Settings â†’ Backup. One native `sensa-backup-card` inside a
        # `sensa-card-grid` shell â€” same pattern as Diagnostics, so the
        # whole tab direct-creates with NO Lovelace engine (which has been
        # unreliable on the Pi panel, see [[project_panel_cold_load_bundle_loader]]).
        # The card reads `binary_sensor.sensa_nomad_backups_stale` (fed by
        # the `_async_refresh_backup_status` poller in `__init__.py`) and
        # calls `sensa_nomad.run_backup` / `pin_backup` / `unpin_backup`
        # for actions. The emergency-kit download goes via the
        # `sensa_nomad/backup_emergency_kit` WS command (the integration
        # proxies to the add-on â€” a direct ingress fetch can't construct
        # the per-session ingress URL client-side).
        # `full_width: true` so the single card spans the full panel
        # width instead of the half-width grid cell.
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            # One whole-page card: nothing for Design Mode to arrange or size.
            "arrangeable": False,
            "cards": [
                {
                    "type": "custom:sensa-backup-card",
                    "title": "Backups",
                    "sensor": "binary_sensor.sensa_nomad_backups_stale",
                    "schedule_time": "03:00",
                    "full_width": True,
                },
                # The per-backup list (name + date + size), newest first, under
                # the status/actions card. Reads the same sensor's `backups`
                # attribute; renders nothing until at least one backup exists.
                {
                    "type": "custom:sensa-backup-list",
                    "title": "Stored backups",
                    "sensor": "binary_sensor.sensa_nomad_backups_stale",
                    "full_width": True,
                },
            ],
        }

    def _build_images_tab() -> dict[str, Any]:
        # Settings â†’ Images. A reusable image library the customer uploads /
        # names / deletes; consumed by the Switching picker today (per-channel
        # switch pictures) and other surfaces later. One native
        # `sensa-image-library` card in a `sensa-card-grid` (no Lovelace
        # engine). The card lists via `sensa_nomad/list_images`, uploads via
        # `sensa_nomad/upload_image`, deletes via `sensa_nomad/delete_image`.
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            # One whole-page card: nothing for Design Mode to arrange or size.
            "arrangeable": False,
            "cards": [
                {
                    "type": "custom:sensa-image-library",
                    # No in-card header â€” the tab strip already reads "Images", so
                    # an "Images" title inside the card just wastes vertical space
                    # (Richard). Empty title â†’ the card renders no ha-card header.
                    "title": "",
                    "full_width": True,
                },
            ],
        }

    def _build_people_tab() -> dict[str, Any]:
        # Settings -> People. The humans who live in the van: a name, a photo and
        # the phone that is theirs. Backed by HA's OWN `person` records (see
        # people.py), so this is a customer-reachable front end for something HA
        # already models rather than a parallel Sensa list.
        #
        # It has to exist because on an appliance the customer is NOT an HA admin
        # and HA's person UI is unreachable, so a locked-down box could otherwise
        # never gain a person at all.
        #
        # No `title` on the card -- the tab strip already says "People", and a
        # header would repeat it.
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            # One whole-page card: nothing for Design Mode to arrange or size.
            "arrangeable": False,
            "cards": [
                {
                    "type": "custom:sensa-people-card",
                    "title": "",
                    "full_width": True,
                    "sensor": "sensor.sensa_nomad_people",
                },
            ],
        }

    def _build_vehicle_tab() -> dict[str, Any]:
        # Settings -> Vehicle. Everything about THIS VAN and how it navigates:
        # where its position comes from, whether journeys are recorded, the
        # distance units, what fuel it takes and how big it is.
        #
        # Was "Radar" until 2026-08-01 and absorbed it whole (Richard). The old
        # name described the supplier rather than the subject: a customer asking
        # "how tall is my van" or "which fuel do I take" is not thinking about
        # Radar, and the same tab now answers both. Nothing was dropped in the
        # move, so the slug is the only thing that changed.
        #
        # It carries the Tracking on/off switch (moved here from the Cab Home tab): the
        # customer's opt-in to journey recording, and off means genuinely zero egress.
        # The switch is a CONFIG entity otherwise only reachable via HA Settings, which
        # a locked-down appliance customer cannot open, so it needs a card somewhere.
        #
        # A small label + on/off toggle ROW (`sensa-config-card`'s `switch`
        # control), not the big `sensa-switch-tile`: the tile is a 4:3 box sized
        # from its grid cell, so one setting took a whole card's worth of height
        # for a single on/off. One line is the right weight for a preference on a
        # settings tab (Richard, 2026-07-27).
        #
        # No `title` -- the ROW carries "Tracking" instead. A title would repeat
        # the row's only label and add a header line to a card whose whole point
        # is being short. Keeps the customer-facing word identical to the tile it
        # replaced (do NOT invent a second string here).
        from ..const import van_level_geometry_entity_id

        cards: list[dict[str, Any]] = [
            navigation_card(),
            # NO location-sharing card here, deliberately (2026-08-09).
            #
            # Creating a share lives in MySensa, on the customer's phone, for
            # two reasons that both point the same way. The recipient is NOT
            # in the van, so the van cannot deliver a link to them at all:
            # delivery means messaging, and messaging means a phone with a
            # contact list and a share sheet. And Nomad runs on a TABLET,
            # where typing is slow, so a "who is it for?" field on this tab
            # was asking the customer to do the one thing this screen is
            # worst at.
            #
            # The van is not left without a control: the Tracking switch
            # above IS the kill switch. Turning it off stops every live share
            # (Radar's `latest_position` returns `tracking_off`), so somebody
            # in the vehicle can always cut sharing dead in one tap, with no
            # keyboard and no trip to their phone.
            #
            # The van itself: what it takes and how big it is. A second card
            # rather than more rows on the first, because these answer a
            # different question -- the card above is "how does this box
            # navigate", this one is "what is this vehicle".
            vehicle_details_card(),
        ]

        # The measurements the levelling maths needs. Moved here off the
        # Levelling tab's Raw Sensor card (2026-08-10), where they sat under a
        # heading reading "Vehicle Details" on a card that is otherwise a live
        # readout of ONE sensor. They are singletons -- one van has one
        # wheelbase however many levelling sensors are fitted -- so repeating
        # them beside every sensor's pitch and roll invited the customer to
        # think each sensor had its own. "What is this vehicle" is the question
        # this tab already answers.
        #
        # A card of their own rather than more rows on the Vehicle card above:
        # that card's note promises these details go to Radar for routing,
        # which is true of height and weight and not of these.
        #
        # Gated on a levelling device HERE, not inherited from the Levelling
        # tab. The three numbers are always created (they are hub-level
        # singletons), so with no sensor fitted this would be a card of live
        # controls that change nothing. Each card that moved off the Levelling
        # tab brought its own copy of the gate rather than leaning on that tab
        # existing, which is why the tab could then be deleted outright.
        if levelling_devices:
            cards.append({
                "type": "custom:sensa-config-card",
                "title": "Levelling",
                "note": (
                    "Used to work out how much to shim each wheel. Measure "
                    "the van, not the levelling sensor."
                ),
                "fields": [
                    {
                        "entity": van_level_geometry_entity_id("wheel_track_cm"),
                        "name": "Wheel track",
                        "control": "number",
                        "suffix": "cm",
                        "heading": "Measurements",
                    },
                    {
                        "entity": van_level_geometry_entity_id("wheelbase_cm"),
                        "name": "Wheelbase",
                        "control": "number",
                        "suffix": "cm",
                    },
                    # Not a measurement of the van but of how fussy the
                    # customer is: how far off level still counts as level.
                    {
                        "entity": van_level_geometry_entity_id(
                            "shim_tolerance_cm",
                        ),
                        "name": "Shim tolerance",
                        "control": "number",
                        "suffix": "cm",
                        "heading": "Tolerance",
                        "description": (
                            "How far off level the van can sit before Sensa "
                            "asks you to shim a wheel."
                        ),
                    },
                    {
                        "entity": van_level_geometry_entity_id("max_roll_deg"),
                        "name": "Maximum roll angle",
                        "control": "number",
                        "suffix": "°",
                        "description": (
                            "The side lean at which the Cab roll screen turns "
                            "red. It turns amber at 80% of this."
                        ),
                    },
                    *_levelling_calibration_fields(levelling_devices),
                ],
            })
            # (Orientation -- the six axis/sign selects + the pitch and roll
            # they produce -- passed through here on 2026-08-10 and moved the
            # same day onto the levelling sensor's OWN row in Settings ->
            # Devices, where `sensor._config_entities_for_device` emits it.
            # Unlike the measurements above it is not a singleton: two sensors
            # in one van can be bolted in facing different ways, so it is a
            # property of the unit, not of the vehicle. Do not add a second
            # copy back here; two live sets of the same six dropdowns on two
            # tabs is exactly the confusion the move was for.)

        cards += [solar_card(), weather_card()]

        # Every card on the tab is behind the Settings PIN, outlined as a whole
        # card rather than per control (Richard, 2026-10-01).
        for card in cards:
            card["pin"] = True

        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            "cards": cards,
        }

    # Automations + Log moved to the dedicated Intelligence dashboard (see
    # `_build_intelligence_yaml_data`). Settings is now pure configuration.

    def _build_cards_gallery_tab() -> dict[str, Any]:
        """Reference catalogue of every custom Sensa Lit card.

        A design aid (Richard, 2026-05-25): one labelled example of each
        card in one place, so building a new card or reusing an existing
        one is a glance away. Laid out by the native `sensa-card-grid`,
        which direct-creates `custom:sensa-*` cards â€” so the whole gallery
        renders on the panel with NO Lovelace engine, the same property
        we're moving every surface towards. Examples use a deliberately
        non-existent `demo` entity where a live value isn't guaranteed, so
        the tab works on any install (cards render their "--" / empty
        state). The interactive device picker lives on the Discovery tab
        and is noted rather than embedded (it needs the live discovery
        sensor + add/remove services).
        """
        from ..modes_catalog import MODES

        demo = "sensor.sensa_nomad_demo"  # non-existent on purpose â†’ "--"
        mode_tiles = [
            {"slug": m.slug, "name": m.name, "icon": m.icon,
             "entity": f"binary_sensor.sensa_nomad_mode_{m.slug}_active"}
            for m in MODES
        ]
        cards: list[dict[str, Any]] = [
            {"type": "custom:sensa-note", "caption": "custom:sensa-note",
             "icon": "mdi:palette-swatch",
             "full_width": True,  # intro banner spans the whole gallery width
             "content": (
                 "**Sensa card gallery.** A live example of each custom Lit "
                 "card, for design reference. Some show placeholder values. "
                 "The interactive device picker lives on the **Discovery** tab."
             )},
            {"type": "custom:sensa-gauge",
             "caption": "custom:sensa-gauge (compact â€” Home + BMS cells)",
             "gauges": [
                 {"name": "Battery", "value": 72, "unit": "%", "min": 0,
                  "max": 100, "color": "var(--accent-color)"},
                 {"name": "Fresh", "value": 64, "unit": "%", "min": 0,
                  "max": 100, "color": "var(--info-color)"},
                 {"name": "Grey", "value": 28, "unit": "%", "min": 0, "max": 100,
                  "invert": True, "color": "var(--secondary-text-color)"},
             ]},
            {"type": "custom:sensa-gauge-detail",
             "caption": "custom:sensa-gauge-detail (detailed â€” BMS SoC/SoH)",
             "gauges": [
                 {"name": "SoC", "value": 78, "unit": "%", "min": 0, "max": 100},
                 {"name": "SoH", "value": 96, "unit": "%", "min": 0, "max": 100},
             ]},
            {"type": "custom:sensa-bar-chart", "caption": "custom:sensa-bar-chart",
             # Filler card (height:100%) â€” give it a tall cell so the gallery
             # shows it at a useful size (~3x the default content height).
             "cell_height": "540px",
             "title": "Example (24 h)", "hours": 24, "entities": [demo]},
            {"type": "custom:sensa-scene-bar", "caption": "custom:sensa-scene-bar",
             "reorder_key": "custom:sensa-mode-bar",
             "full_width": True,  # tile strip reads best spanning the grid
             "show_name": True, "single_row": False, "columns": 4,
             "modes": mode_tiles},
            {"type": "custom:sensa-power-flow-card",
             "caption": "custom:sensa-power-flow-card (basic â€” Home)",
             "cell_height": "540px",
             "solar": demo, "battery": {"soc": demo, "power": demo},
             "ac_load": demo, "dc_load": demo},
            {"type": "custom:sensa-power-flow-detail-card",
             "caption": "custom:sensa-power-flow-detail-card (Electrical hero)",
             "cell_height": "540px",
             "solar": demo, "battery": {"soc": demo, "power": demo},
             "grid": demo, "ac_load": demo, "dc_load": demo},
            {"type": "custom:sensa-bms-card",
             "caption": "custom:sensa-bms-card (nests gauge-detail + gauge)",
             "demo": True,
             "name": "Example Pack", "soc": demo,
             "cells": [demo, demo, demo, demo],
             "temps": [{"entity": demo, "name": "Cell Temp 1"},
                       {"entity": demo, "name": "Cell Temp 2"},
                       {"entity": demo, "name": "BMS Temp 1"},
                       {"entity": demo, "name": "BMS Temp 2"}],
             "graphs": [{"entity": demo, "name": "Pack Current"},
                        {"entity": demo, "name": "Pack Voltage"}]},
            {"type": "custom:sensa-relay-channels",
             "caption": "custom:sensa-relay-channels",
             "channels": [
                 {"n": 1, "relay": demo, "name": "Water Pump", "enabled": demo},
                 {"n": 2, "relay": demo, "name": "Lights", "enabled": demo},
             ]},
            {"type": "custom:sensa-switch-tile",
             "caption": "custom:sensa-switch-tile (NAS power control)",
             "entity": demo, "name": "Galley Lights", "icon": "mdi:lightbulb"},
            {"type": "custom:sensa-autoterm-heater-card",
             "caption": "custom:sensa-autoterm-heater-card (Habitation â†’ Climate)",
             # Tall thermostat-focal card (height:100%) â€” give it a real cell.
             "cell_height": "560px",
             "demo": True, "name": "Cabin Heater",
             "image": "/local/sensa/images/autoterm-air-d.webp",
             "brand": "Autoterm"},
            {"type": "custom:sensa-autoterm-flow-card",
             "caption": "custom:sensa-autoterm-flow-card (Habitation → Climate)",
             "demo": True, "name": "Water Heater",
             "image": "/local/sensa/images/autoterm-baltica.webp",
             "brand": "Autoterm"},
            {"type": "custom:sensa-air-conditioner-card",
             "caption": "custom:sensa-air-conditioner-card (Habitation â†’ Climate)",
             "cell_height": "560px",
             "demo": True, "name": "Velit Rooftop AC"},
            {"type": "custom:sensa-climate-control-card",
             "caption": "custom:sensa-climate-control-card (Habitation → Climate)",
             "demo": True, "name": "Climate Control"},
            {"type": "custom:sensa-climate-settings-card",
             "caption": "custom:sensa-climate-settings-card (Habitation → SensaClimate)",
             "demo": True, "full_width": True},
            {"type": "custom:sensa-fan-card",
             "caption": "custom:sensa-fan-card (Habitation → Climate)",
             "demo": True, "name": "Fan Controller",
             "fans": [{"entity": demo, "default_name": "Roof fan"},
                      {"entity": demo, "default_name": "Fridge vent"}]},
            {"type": "custom:sensa-light-card",
             "caption": "custom:sensa-light-card (Habitation â†’ Lighting)",
             "cell_height": "520px",
             "demo": True, "name": "Galley Light", "icon": "mdi:lightbulb"},
            {"type": "custom:sensa-led-card",
             "caption": "custom:sensa-led-card (Habitation â†’ Lighting)",
             "cell_height": "640px",
             "demo": True, "name": "Awning Strip", "icon": "mdi:led-strip-variant"},
            {"type": "custom:sensa-sensor-tile",
             "caption": "custom:sensa-sensor-tile (Habitation â†’ Security)",
             "demo": True, "name": "Hall Motion", "kind": "motion"},
            {"type": "custom:sensa-media-player-card",
             "caption": "custom:sensa-media-player-card (Habitation â†’ Media)",
             "demo": True, "name": "Sonos Roam 2"},
            {"type": "custom:sensa-climate-summary",
             "caption": "custom:sensa-climate-summary â€” heater (Home right column)",
             "demo": True, "name": "Heater", "icon": "mdi:fire"},
            {"type": "custom:sensa-climate-summary",
             "caption": "custom:sensa-climate-summary â€” air con (Home right column)",
             # `entity` (climate.*) selects the AC adapter; demo fills values.
             "demo": True, "entity": "climate.gallery_demo",
             "name": "Air Conditioner", "icon": "mdi:air-conditioner"},
            # (sensa-level-orientation-card was here until 2026-08-10. The
            # orientation controls are now config fields on the levelling
            # sensor's row in Settings -> Devices, so there is no card to show.)
            {"type": "custom:sensa-level-raw-card",
             "caption": "custom:sensa-level-raw-card (Cab â†’ Levelling)",
             "demo": True},
            {"type": "custom:sensa-level-calibration-card",
             "caption": "custom:sensa-level-calibration-card (unused: its rows are on Settings â†’ Vehicle â†’ Levelling)",
             "demo": True},
            {"type": "custom:sensa-level-summary-card",
             "caption": "custom:sensa-level-summary-card (Cab â†’ Levelling)",
             "demo": True, "name": "Van Level Pro 10"},
            {"type": "custom:sensa-level-gyro-card",
             "caption": "custom:sensa-level-gyro-card (Cab â†’ Levelling)",
             # Square card sized via aspect-ratio; give it a tall cell so
             # the gallery shows it at the size it'll appear on Cab.
             "cell_height": "560px",
             "demo": True},
            {"type": "custom:sensa-level-gyro-home",
             "caption": "custom:sensa-level-gyro-home (bubble + spirit levels)",
             "full_width": True,
             "cell_height": "200px",
             "demo": True},
            {"type": "custom:sensa-gauge-basic",
             "caption": "custom:sensa-gauge-basic (Home left dials, in panel)",
             "gauges": [
                 {"name": "Battery", "value": 72, "unit": "%", "min": 0,
                  "max": 100, "color": "var(--accent-color)"},
                 {"name": "Fresh", "value": 64, "unit": "%", "min": 0,
                  "max": 100, "color": "var(--info-color)"},
                 {"name": "Grey", "value": 28, "unit": "%", "min": 0, "max": 100,
                  "invert": True, "color": "var(--secondary-text-color)"},
             ]},
            {"type": "custom:sensa-scene-bar-single",
             "reorder_key": "custom:sensa-mode-bar-single",
             "caption": "custom:sensa-scene-bar-single (Cab Overview top strip)",
             "full_width": True,
             "show_name": True,
             "modes": mode_tiles},
            {"type": "custom:sensa-level-tilt-card",
             "caption": "custom:sensa-level-tilt-card (Cab â†’ Levelling, both views)",
             # Stacked variant â€” needs a tall cell to show both views.
             "cell_height": "320px",
             "axis": "both", "demo": True},
            {"type": "custom:sensa-level-topdown-card",
             "caption": "custom:sensa-level-topdown-card (Cab â†’ Levelling)",
             # Tall card with absolute-positioned wheel badges around an
             # SVG van; needs a real cell height to show at intended size.
             "cell_height": "420px",
             "demo": True},
            {"type": "custom:sensa-integrity-card",
             "caption": "custom:sensa-integrity-card"},
            {"type": "custom:sensa-backup-card",
             "caption": "custom:sensa-backup-card",
             "demo": True},
            {"type": "custom:sensa-backup-list",
             "caption": "custom:sensa-backup-list",
             "demo": True},
            {"type": "custom:sensa-relay-tiles",
             "caption": "custom:sensa-relay-tiles",
             "demo": True},
            {"type": "custom:sensa-image-library",
             "caption": "custom:sensa-image-library",
             "full_width": True,
             "demo": True},
            {"type": "custom:sensa-log-card",
             "caption": "custom:sensa-log-card",
             "full_width": True,
             "demo": True},
            {"type": "custom:sensa-automations-card",
             "caption": "custom:sensa-automations-card",
             "full_width": True,
             "demo": True},
            {"type": "custom:sensa-alarms-card",
             "caption": "custom:sensa-alarms-card (Habitation â†’ Alarms)",
             "full_width": True,
             "demo": True},
            {"type": "custom:sensa-alarm-detail-card",
             "caption": "custom:sensa-alarm-detail-card (panel right-side overlay)",
             "cell_height": "360px",
             "demo": True},
            {"type": "custom:sensa-segment-select",
             "caption": "custom:sensa-segment-select (Mobile Navigation map toolbar)",
             "full_width": True,
             "demo": True,
             "title": "Map",
             "options": [
                 {"value": "HA Map", "icon": "mdi:map"},
                 {"value": "Google Maps", "icon": "mdi:google-maps"},
                 {"value": "Sensa Radar", "icon": "mdi:radar"},
             ]},
            {"type": "custom:sensa-inverter-options",
             "caption": "custom:sensa-inverter-options (Power Central: under the power flow)",
             "demo": True},
            {"type": "custom:sensa-nav-map",
             "caption": "custom:sensa-nav-map (Mobile Navigation: toolbar + fill map)",
             "full_width": True,
             "cell_height": "320px",
             "demo": True,
             "nav_entity": "select.sensa_nomad_cab_nav_source",
             "options": [
                 {"value": "HA Map", "icon": "mdi:map"},
                 {"value": "Google Maps", "icon": "mdi:google-maps"},
                 {"value": "Sensa Radar", "icon": "mdi:radar"},
             ]},
            {"type": "custom:sensa-image-picker-card",
             "caption": "custom:sensa-image-picker-card (the van-pictures pattern)",
             "full_width": True,
             "demo": True},
            {"type": "custom:sensa-network-signals",
             "caption": "custom:sensa-network-signals",
             "full_width": True,
             "demo": True},
            {"type": "custom:sensa-header-bar", "caption": "custom:sensa-header-bar",
             "full_width": True,  # the header strip reads best spanning the grid
             "chips": [
                 {"entity": "sensor.sensa_nomad_current_mode",
                  "icon": "mdi:tune-vertical-variant"},
             ]},
            {"type": "custom:sensa-mode-editor",
             "caption": "custom:sensa-mode-editor"},
        ]
        return {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            "cards": cards,
        }

    def _build_system_log_tab() -> dict[str, Any]:
        # The SAME builder the Intelligence Log tab uses, asked for the other
        # source. Deliberately not a second card config: the two logs must stay
        # identical in shape, and one of them drifting is exactly the bug this
        # repo keeps re-finding.
        from ..const import SENSA_LOG_SOURCE_SYSTEM

        return _build_log_tab(SENSA_LOG_SOURCE_SYSTEM)

    # Tab order: Devices, the conditional Switches tab, then System, Backup,
    # Vehicle, People, Images, then the Nerd Mode tabs, Sensa, and the Cards
    # design-reference gallery last.
    tabs: list[dict[str, Any]] = [
        _safe_build("discovery", "Devices", "mdi:magnify", _build_discovery_tab),
        # Scenes + Automations moved to the Intelligence dashboard (2026-06-24),
        # and so did the Log. A Log tab returned here on 2026-08-09, but a
        # DIFFERENT one: Intelligence keeps the van's own messages, Settings
        # takes the box's. Settings is still pure config plus this record of
        # what the box did.
    ]
    # Switches tab when a Sensa Relay Controller OR a network switch is
    # registered: each gets a config card of named, assignable rows. A van
    # with a switch and no relay still needs to name its ports, so the gate
    # is either. (The tap-to-toggle PoE tiles are on HABITATION -> Switches.)
    if relay_cards or switch_port_config_cards:
        tabs.append(
            _safe_build(
                "switching", "Switches", "mdi:electric-switch",
                _build_switching_tab,
            ),
        )
    # (No Levelling tab: removed 2026-08-10 once its last card had a better
    # home. `levelling_devices` survives as the gate on the Vehicle tab's
    # geometry card -- see the comment above that card.)
    # (No Sensors tab: removed 2026-09-01. Its only card was the Z2M
    # vibration sensor's sensitivity slider, bound to the Z2M `number.*_
    # sensitivity` entity -- which Z2M can never read back off the device, so
    # the slider blanked itself the moment you set it and read as broken. The
    # setting now lives on that sensor's own Settings -> Devices row, on the
    # Sensa number that REMEMBERS what it wrote
    # (`number.SensaVibrationSensitivityNumber`). One control, one home.)
    # Install flavour gates the Backup tab AND the sidebar policy below
    # (same rule as Habitation). Computed once here.
    from ..const import DEPLOYMENT_MODE_APPLIANCE
    appliance = _current_deployment_mode() == DEPLOYMENT_MODE_APPLIANCE
    # `deployment_mode` is folded to the two-way FAMILY on ingest (see
    # __init__._async_refresh_deployment_mode), so `appliance` alone cannot tell
    # Enthusiast/HAOS from Container, `is_supervised` is the only runtime
    # discriminator. Container has no Supervisor; HAOS does. Both the Supervisor
    # tab and the Backup tab are Container-only, so compute the predicate once.
    # No hass handle => can't probe the platform => treat as NOT Container, so a
    # tab never leaks onto a flavour that shouldn't have it.
    from ..appliance import is_supervised
    container_appliance = (
        appliance
        and _dashboard_hass is not None
        and not is_supervised(_dashboard_hass)
    )

    # `admin`-slug tab (labelled "Admin" until 2026-09-03, briefly "Supervisor"
    # 2026-07-06 to 2026-08-10 before that, now labelled "System" â€” this is the
    # box-operations tab (restart, updates, remote access), which is what "System"
    # actually describes; swapped with the `diagnostics`-slug tab's label the same
    # day, once the 2026-09-01 Settings PIN removed the reason "Admin" needed to
    # read as gate-only. The slug has always been `admin`, so the switcher / panel
    # routing / `require_sensa_admin` are untouched by any of the renames).
    # CONTAINER-APPLIANCE ONLY: it gives the locked-down `admin` login (deliberately
    # NOT an HA admin on Container) HA-admin-style powers â€” restart / reboot /
    # units-timezone-theme / customer-login passwords / Host-OS + Software updates â€”
    # that HA's own UI would block. Hidden on HAOS (the `admin` login IS a full HA
    # administrator there and uses HA's native settings, so these panels are
    # redundant) and on BYO (the sole user is a full HA admin). The Licence detail +
    # recovery and Analytics & Diagnostics panels MOVED to the all-flavours
    # Diagnostics tab (2026-07-06), so BYO/HAOS lose nothing by not having this tab.
    # (Tab-level gate only; the gated `sensa_nomad.*` admin services stay
    # registered + `_require_sensa_admin`-checked server-side regardless.)
    # SENSA-ADMIN-ONLY: reboot / restart / password rotation must not be reachable
    # from a kiosk tablet, so the tab carries `require_sensa_admin` and the frontend
    # hides it unless the panel_config WS `can_admin` is true. The server-side
    # service gate is the real lockdown.
    if container_appliance:
        admin_tab = _safe_build(
            "admin", "System", "mdi:server", _build_admin_tab,
        )
        admin_tab["require_sensa_admin"] = True
        tabs.append(admin_tab)
    # Default order after Devices (Richard, 2026-10-16): System, Backup, Vehicle,
    # People, Images. Backup is CONTAINER-APPLIANCE ONLY.
    # Not on BYO: the customer owns their own Home Assistant and is responsible for
    # their own backup strategy (and our backup actions drive their Supervisor).
    # Not on Enthusiast/HAOS either (Richard, 2026-07-20): under that flavour the
    # customer is likewise responsible for backups, and the `admin` login there is a
    # full HA administrator with Supervisor's own native backup UI. Open to every
    # Container user (2026-07-01, per direction: only the `admin`-slug tab
    # (labelled "System" since 2026-09-03) is owner-restricted).
    if container_appliance:
        tabs.append(
            _safe_build("backup", "Backup", "mdi:backup-restore", _build_backup_tab),
        )
    # The van itself: GPS source, journey recording, units, fuel and size.
    # Always present. This IS the old "Radar" tab, renamed and widened
    # (2026-08-01) rather than joined by a second one -- everything that was on
    # Radar is here, so a Radar tab would have been an empty tab.
    tabs.append(
        _safe_build("vehicle", "Vehicle", "mdi:van-passenger", _build_vehicle_tab),
    )
    # People — who lives in the van, and whose phone is whose. Always present:
    # a van with no people is exactly the box that needs this tab, and the
    # manual path works with no internet.
    tabs.append(
        _safe_build("people", "People", "mdi:account-multiple", _build_people_tab),
    )
    tabs.append(
        _safe_build("images", "Images", "mdi:image-multiple", _build_images_tab),
    )
    # "Diagnostics" (slug unchanged since before any relabel, so no entity-ids
    # or links move). Mostly troubleshooting: licence detail and recovery, file
    # integrity, resources, the consent toggles and log retention, all on the
    # consolidated `sensa-integrity-card`. After System (Richard, 2026-09-24;
    # it led the settings tabs from 2026-07-02), and shown only while Nerd Mode
    # is on (`nerd`, the flag the Habitation History tab uses): the panel hides
    # it otherwise. The Nerd Mode toggle sits in the shared header, which is
    # how a locked-out customer reaches licence recovery here.
    tabs.append({
        **_safe_build(
            "diagnostics", "Diagnostics", "mdi:heart-pulse", _build_diagnostics_tab,
        ),
        "nerd": True,
    })
    # Log (2026-08-09): the box talking about itself (backups, software updates,
    # licence check-ins, restores, devices added or removed), split off the
    # Intelligence -> Log tab by the entry's `source`. Built on every flavour,
    # shown only while Nerd Mode is on (Richard, 2026-09-24), like Diagnostics.
    # Straight after Diagnostics, which owns the retention setting that governs
    # it. It shares its NAME with Intelligence -> Log, so keep
    # `mdi:script-text-outline` (not `mdi:message-text-outline`) or the two
    # tabs are indistinguishable.
    tabs.append({
        **_safe_build(
            "log", "Log", "mdi:script-text-outline", _build_system_log_tab,
        ),
        "nerd": True,
    })
    if container_appliance:
        # "Sensa" tab â€” SENSA STAFF ONLY (2026-07-20). For use while remotely
        # logged in as `sensa_support`; the customer must never see it, not even
        # the elevated `admin`. So it carries `require_sensa_staff`, which the
        # frontend resolves from the panel_config WS `can_sensa` flag
        # (`appliance.user_is_sensa_staff` = the support username), NOT from
        # `can_admin` â€” any HA admin satisfies that, and on BYO the HA admin is
        # the customer.
        #
        # Containers moved here from the System tab (the `admin`-slug tab): it is a docker-level
        # view of the stack, useful to us and meaningless to an owner.
        sensa_tab = _safe_build(
            "sensa", "Sensa", "mdi:shield-account", _build_sensa_tab,
        )
        sensa_tab["require_sensa_staff"] = True
        tabs.append(sensa_tab)
    # Cards gallery â€” a design-reference tab (a labelled example of every custom
    # card), a dev aid for pulling card names into design/AI conversations. NOT
    # part of the shipped appliance product (2026-07-01); BYO / dev only.
    if not appliance:
        tabs.append(
            _safe_build(
                "cards", "Cards", "mdi:palette-swatch", _build_cards_gallery_tab,
            ),
        )

    # Sidebar policy follows install flavour: BYO leaves HA's drawer reachable;
    # appliance hides it so the in-page switcher in `<sensa-header>` is the only
    # nav.
    shell_card: dict[str, Any] = {
        "type": "custom:sensa-dashboard-shell",
        "logo": _dealer_logo_url() or _SENSA_DEFAULT_LOGO,
        "chips": _habitation_chips(),
        "kiosk_hide_header": True,
        "kiosk_hide_sidebar": appliance,
        # Trial countdown in the header right slot â€” matches the
        # Habitation dashboard. The Licensing tab still carries the
        # full trial card so customers always have a way to see the
        # detail.
        "header_show_trial": True,
        "tabs": tabs,
    }

    return {
        "title": "Nomad Settings",
        "views": [{
            "title": "Sensa",
            "path": "sensa",
            "icon": "mdi:cog-outline",
            "panel": True,
            "cards": [shell_card],
        }],
    }


def _levelling_calibration_fields(
    devices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Calibration rows for the Vehicle tab's Levelling card, one group per
    levelling sensor: pitch / roll offset sliders, Reset + Calibrate, and the
    calibrated pitch / roll (Nerd Mode). Moved here off the Cab Level tab's
    `sensa-level-calibration-card` (Richard, 2026-09-30)."""
    from ..const import (
        van_level_calibrate_button_entity_id,
        van_level_calibrated_sensor_entity_id,
        van_level_offset_entity_id,
        van_level_reset_button_entity_id,
    )

    fields: list[dict[str, Any]] = []
    for d in devices:
        device_id = str(d.get("device_id") or d.get("id") or "")
        heading = "Calibration"
        if len(devices) > 1:
            heading += f": {d.get('display_name') or d.get('name') or device_id}"
        fields += [
            {
                "entity": van_level_offset_entity_id(device_id, "pitch"),
                "name": "Pitch offset",
                "control": "slider",
                "heading": heading,
            },
            {
                "entity": van_level_offset_entity_id(device_id, "roll"),
                "name": "Roll offset",
                "control": "slider",
            },
            {
                "entity": van_level_calibrate_button_entity_id(device_id),
                "name": "Calibrate",
                "control": "button",
                "button_label": "Calibrate",
                "description": (
                    "Park on ground you know is flat, then press to use the "
                    "current readings as level."
                ),
            },
            {
                "entity": van_level_reset_button_entity_id(device_id),
                "name": "Reset offsets",
                "control": "button",
                "button_label": "Reset",
                "description": "Set the pitch and roll offsets back to 0.",
            },
            {
                "entity": van_level_calibrated_sensor_entity_id(device_id, "pitch"),
                "name": "Calibrated pitch",
                "control": "readout",
                "nerd": True,
            },
            {
                "entity": van_level_calibrated_sensor_entity_id(device_id, "roll"),
                "name": "Calibrated roll",
                "control": "readout",
                "nerd": True,
            },
        ]
    return fields


def nav_units_card() -> dict[str, Any]:
    """Settings -> Vehicle: the Miles / Kilometres buttons. Also a Setup
    Wizard step, so it is built in one place."""
    return {
        "type": "custom:sensa-segment-select",
        "entity": "select.sensa_nomad_nav_units",
        "title": "Units",
    }


def navigation_card(units: bool = True) -> dict[str, Any]:
    """Settings -> Vehicle: GPS source, Tracking and its phone opt-in, and
    the distance units. The Setup Wizard shows it without the units, which
    have their own step there."""
    from ..const import CAB_LOCATION_AUTO

    return {
        "type": "custom:sensa-config-card",
        # Titled, like the Vehicle card beside it. It was the only
        # untitled card on the tab, so the page opened on a bare
        # control row with no idea what it belonged to while its
        # neighbour announced itself in bold.
        "title": "Navigation",
        "fields": [
            # The GPS source for the WHOLE box. The Cab tab-strip
            # dropdown of the same name is deliberately per-screen
            # (`client_key`, browser localStorage), so it moves that
            # screen's map and nothing else; the Radar nearest-amenity
            # sensors read this select instead (Tracking only with its
            # phone opt-in below). Until now
            # it had no customer-facing control at all -- reachable
            # only through HA's own settings, which a locked-down
            # appliance customer cannot open.
            {
                "entity": "select.sensa_nomad_cab_location_source",
                "name": "GPS source",
                "control": "select",
                # Auto falls back to a phone when the van has no
                # GPS of its own (`resolve_nav_source`), which shows
                # where a PERSON is, so say so while Auto is chosen.
                "notice": {
                    "when": CAB_LOCATION_AUTO,
                    "text": (
                        "Auto uses a GPS fitted to the van. If the "
                        "van has none, it uses the driver's phone, "
                        "or the phone that reported last, so the map "
                        "and nearby places show where that person is."
                    ),
                },
            },
            {
                "entity": "switch.sensa_nomad_tracking",
                "name": "Tracking",
                "control": "switch",
                # The ONLY notice the customer is ever given about
                # journey recording, so it has to be here.
                # Turning this on IS the consent (Radar dropped its
                # separate web consent step precisely because this
                # switch carries it), and until now the control said
                # nothing at all about what it does. The switch is on
                # a tablet anyone in the van can reach, so the person
                # who taps it may not be the person who bought it.
                #
                # States BOTH halves deliberately: what leaves the
                # van when on, and that nothing does when off. The
                # zero-egress promise is real (async_sample and
                # async_upload both return before reading the tracker
                # or opening a socket), so it is safe to print, and
                # it is the reassurance that makes the on state a
                # real choice rather than a one-way door.
                # "anyone you choose to share it with" covers Location
                # Sharing and Convoy without naming Convoy, which
                # ships hidden behind a Hub feature gate.
                "description": (
                    "On, we record where the van goes and send it to "
                    "Sensa, where anyone on your MySensa account, and "
                    "anyone you choose to share it with, can see it. "
                    "Off, nothing is recorded or sent."
                ),
                # PERMANENT, not behind the info button every other
                # hint uses. A hint explains a control to somebody
                # who went looking; this is a NOTICE, and it has to
                # reach somebody who did not. Turning this switch on
                # IS the consent to upload the van's position, and
                # Radar dropped its separate web consent step on
                # that basis, so a customer must not be able to flip
                # it without the sentence having been on screen.
                # Do not "tidy" this to the default.
                "hint": "always",
            },
            {
                "entity": "switch.sensa_nomad_tracking_phones",
                "name": "Use a phone when the van has no GPS",
                "control": "switch",
                # A phone's position is a PERSON's, so this is its own
                # opt-in, off by default, and says so on screen for
                # the same reason as the Tracking notice above.
                "description": (
                    "If the van has no GPS of its own, Tracking uses the "
                    "driver's phone instead."
                ),
                "hint": "always",
                # Amber while on (Richard, 2026-10-21): the phone is a
                # person, and it is still tracked once it leaves the van.
                "notice": {
                    "when": "on",
                    "text": (
                        "Tracking follows the phone, not the van. If the "
                        "driver leaves the van with their phone, Tracking "
                        "keeps recording where they go."
                    ),
                },
            },
        ],
        # Distance units for the Radar nearest-amenity list and map,
        # moved here from the Cab home Nearby card so the units
        # control lives in one place. Carried as a CHILD CARD rather
        # than a `select` field so it keeps its segmented Miles /
        # Kilometres buttons: the ask was to gather the three
        # controls into one panel, not to change any of them.
        "cards": [nav_units_card()] if units else [],
    }


def vehicle_details_card() -> dict[str, Any]:
    """Settings -> Vehicle: what the van takes and how big it is. Also a
    Setup Wizard step, so it is built in one place."""
    return {
        "type": "custom:sensa-config-card",
        "title": "Vehicle",
        # Was a five-line paragraph sitting between the customer and
        # every control on the card. Shortened, NOT stripped: a
        # first pass cut the Radar Premium condition altogether,
        # which would have let a customer type their dimensions in
        # and never learn why nothing routes differently. Wanting a
        # tidier screen is not a licence to delete the fact that
        # answers "I filled this in and nothing happened".
        "note": (
            "Used for routing that fits your van: low bridges, "
            "weight limits and car parks. Needs Radar Premium to "
            "take effect; without it your details are still saved. "
            "Leave anything at 0 to skip it."
        ),
        "fields": [
            # What the van runs on. A fuel station publishes several
            # prices at once, so the map needs to know which one to
            # put on the pin -- the cheapest is almost always
            # standard petrol, which a diesel van would misread at
            # every station.
            {
                "entity": "select.sensa_nomad_nav_fuel_type",
                "name": "Fuel type",
                "control": "select",
            },
            # Typed numbers, not sliders: you know your van is
            # 3.15 m and want to enter it, not hunt for it. 0 means
            # "not set", which the map treats as "no restriction to
            # check" exactly as Radar's own profile does.
            #
            # Grouped under one heading and carrying their unit as a
            # SUFFIX rather than a bracket in the label. Four
            # separate "X (m)" rows divided by identical hairlines
            # read as four unrelated settings; they are one answer to
            # one question, so they are held together by a heading
            # and white space instead.
            {
                "entity": "number.sensa_nomad_vehicle_height_m",
                "name": "Height",
                "control": "number",
                "suffix": "m",
                "heading": "Size",
            },
            {
                "entity": "number.sensa_nomad_vehicle_width_m",
                "name": "Width",
                "control": "number",
                "suffix": "m",
            },
            {
                "entity": "number.sensa_nomad_vehicle_length_m",
                "name": "Length",
                "control": "number",
                "suffix": "m",
            },
            {
                "entity": "number.sensa_nomad_vehicle_weight_t",
                "name": "Weight",
                "control": "number",
                "suffix": "t",
            },
            {
                "entity": "number.sensa_nomad_vehicle_max_grade_pct",
                "name": "Steepest hill",
                "control": "number",
                "suffix": "%",
                "heading": "Limits",
            },
        ],
    }


def solar_card() -> dict[str, Any]:
    """Settings -> Vehicle: the solar forecast's on/off, panels and battery.
    Also a Setup Wizard step, so it is built in one place.

    It shipped (2026-10-19) as a bare CONFIG entity on the HA device page, which
    an owner cannot reach on an appliance install. A card of its own, not rows
    on the Vehicle card: that card's note says its details go to Radar, and
    this one's do not. Switched on and off by its own toggle, off by default
    (Richard, 2026-10-24); a panel rating of 0 used to be the off switch."""
    from ..const import (
        BATTERY_CAPACITY_ENTITY_ID,
        SOLAR_ARRAY_RATING_ENTITY_ID,
        SOLAR_SWITCH_ENTITY_ID,
    )

    return {
        "type": "custom:sensa-config-card",
        "title": "Solar",
        "note": (
            "See how much charge to expect from your solar panels today and "
            "tomorrow. Enter the total watts on your panels' labels, and your "
            "battery size to see it as a percentage. While it is on, the van's "
            "position rounded to about 10 km is sent every 30 minutes; while "
            "it is off, nothing is sent for it."
        ),
        "fields": [
            {
                "entity": SOLAR_SWITCH_ENTITY_ID,
                "name": "Solar forecast",
                "control": "switch",
            },
            {
                "entity": SOLAR_ARRAY_RATING_ENTITY_ID,
                "name": "Solar panels",
                "control": "number",
                "suffix": "W",
            },
            {
                "entity": BATTERY_CAPACITY_ENTITY_ID,
                "name": "Battery size",
                "control": "number",
                "suffix": "Ah",
            },
        ],
    }


def weather_card() -> dict[str, Any]:
    """Settings -> Vehicle: the weather forecast's on/off (weather_forecast.py),
    off by default. Also a Setup Wizard step, so it is built in one place."""
    from ..const import WEATHER_SWITCH_ENTITY_ID

    return {
        "type": "custom:sensa-config-card",
        "title": "Weather",
        "note": (
            "Weather, warnings, UV and air quality where the van is, on the "
            "Weather card. While it is on, the van's position rounded to "
            "about 10 km is sent every 30 minutes; while it is off, nothing "
            "is sent for it."
        ),
        "fields": [
            {
                "entity": WEATHER_SWITCH_ENTITY_ID,
                "name": "Weather forecast",
                "control": "switch",
            },
        ],
    }
