"""Dashboard management for Sensa Nomad."""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..light_groups import light_group_entity_id
from ..vendors.service_classes import is_relay_device

# --- package split -----------------------------------------------------------
# `dashboard` was one ~8,000-line module. It is now a package, and this file is
# still that module: the card builders, and crucially the mutable dashboard state
# (`_dashboard_devices` / `_dashboard_hass`) with every reader of it, stay HERE so
# that `set_dashboard_devices`'s rebinding, and the tests' `patch.object(dashboard,
# "_dashboard_devices", ...)`, keep working against one module object.
#
# Re-exported below so `from .dashboard import X` keeps resolving for every caller
# (sensor.py, __init__.py, the tests) exactly as it did before the split.
from ._constants import (  # noqa: F401
    _LEGACY_DASHBOARD_FILES,
    _LEGACY_DASHBOARD_KEYS,
    DASHBOARD_DIR,
    DASHBOARDS,
    DEFAULT_PANEL_PATH,
    HA_DEFAULT_HIDDEN_PANELS,
    MANAGED_BLOCK_END,
    MANAGED_BLOCK_START,
    PANELS,
    SETTINGS_PANEL_PATH,
    TABLET_USER_DEFAULT_PANEL,
)
from .config_yaml import (  # noqa: F401
    _PANEL_REPLACED_DASHBOARD_KEYS,
    _async_build_user_default_panels,
    _atomic_write_text,
    _build_managed_block,
    _build_managed_dashboard_entries,
    _build_sidebar_userdata,
    _create_or_update_sensa_settings_dashboard,
    _forget_generated_checksum,
    _insert_under_dashboards,
    _insert_under_lovelace,
    _record_generated_checksum,
    _remove_existing_sensa_dashboard_entry,
    _remove_sensa_settings_dashboard,
    _replace_existing_managed_block,
    _strip_managed_lovelace,
    _update_configuration_yaml,
    _write_dashboard_yaml,
    async_create_or_update_sensa_settings_dashboard,
    async_remove_ha_builtin_dashboards,
    async_remove_sensa_settings_dashboard,
    async_set_sidebar_order,
)

_LOGGER = logging.getLogger(__name__)


# The BMS card's everyday view: the image, the SoC gauge and these readouts.
# Every other slot (SoH, cells, status, temps, the rest of the metrics, the
# history graphs) is Nerd Mode only (Richard, 2026-10-04).
_BMS_EVERYDAY_METRICS = frozenset({"Voltage", "Current", "Power"})


def _bms_metric_slot(entity: str, label: str, icon: str) -> dict[str, Any]:
    slot: dict[str, Any] = {"entity": entity, "name": label, "icon": icon}
    if label in _BMS_EVERYDAY_METRICS:
        slot["everyday"] = True
    return slot


def _build_roamer_gen1_card(device: dict[str, Any]) -> dict[str, Any]:
    """Roamer Gen 1 (VIP) battery card — native `custom:sensa-bms-card`.

    Replaces the former HACS ultra-card build: the Sensa card renders the
    same layout (SoC gauge, four cell-voltage gauges, four temps, current +
    voltage history) natively, so it works on a custom panel page without
    HACS or HA's Lovelace engine. See feedback_prefer_own_cards_over_hacs.

    The VIP parser's entity set (no State of Health, no cell balancing, 4
    cells, 4 temps) maps 1-to-1 onto the card's slots, so every slot is
    populated on a healthy pack. See webcards/src/sensa-bms-card.ts.
    """
    display_name = (
        device.get("display_name") or device.get("name")
        or "Roamer Gen 1"
    )
    role_map: dict[str, str] = device.get("bms_entities") or {}

    def eid(role: str) -> str:
        return role_map.get(role, "")

    return {
        "type": "custom:sensa-bms-card",
        "name": display_name,
        "image": "/local/sensa/images/roamer-gen1-x-12-200.webp",
        "soc": eid("state_of_charge"),
        # The everyday readouts; the graphs below carry the same two, in Nerd Mode.
        "metrics": [
            _bms_metric_slot(eid(role), label, icon)
            for role, label, icon in (
                ("pack_voltage", "Voltage", "mdi:flash"),
                ("pack_current", "Current", "mdi:current-dc"),
                ("pack_power", "Power", "mdi:lightning-bolt"),
            )
            if eid(role)
        ],
        "cells": [
            eid("cell_1_voltage"), eid("cell_2_voltage"),
            eid("cell_3_voltage"), eid("cell_4_voltage"),
        ],
        "temps": [
            {"entity": eid("cell_temp_1"), "name": "Cell Temp 1"},
            {"entity": eid("cell_temp_2"), "name": "Cell Temp 2"},
            {"entity": eid("bms_temp_1"), "name": "BMS Temp 1"},
            {"entity": eid("bms_temp_2"), "name": "BMS Temp 2"},
        ],
        "graphs": [
            {"entity": eid("pack_current"), "name": "Current"},
            {"entity": eid("pack_voltage"), "name": "Voltage"},
        ],
    }


def _bms_cell_voltage_slots(role_map: dict[str, str]) -> list[str]:
    """Per-cell voltage entity_ids, ordered by cell number.

    JK/JBD packs name each cell sensor "Cell N Voltage", which the
    resolver slugifies into the role key ``cell_N_voltage`` (see
    `__init__._attach_bms_entity_prefixes`). The cell count is dynamic —
    derived from the live sample — so we collect whatever cells the pack
    actually reports (4 on a 12V pack, 8 on a 24V) rather than the fixed
    four the VIP/Gen1 builder hardcodes. ``cell_voltage_delta`` /
    ``min_cell_voltage`` / ``max_cell_voltage`` don't match the digit
    group, so they're excluded.
    """
    numbered: list[tuple[int, str]] = []
    for key, eid in role_map.items():
        m = re.fullmatch(r"cell_(\d+)_voltage", key)
        if m and eid:
            numbered.append((int(m.group(1)), eid))
    numbered.sort(key=lambda pair: pair[0])
    return [eid for _, eid in numbered]


def _bms_temp_slots(role_map: dict[str, str]) -> list[dict[str, str]]:
    """Temperature probe slots (MOSFET / battery probes) for the card.

    The probe set is family- and model-specific (JK reports a MOSFET
    channel + up to five battery probes, JBD up to three), named e.g.
    "Battery Temp 1" / "MOSFET Temperature" → role keys ``battery_temp_1``
    / ``mosfet_temperature``. Any role key mentioning "temp" is a genuine
    temperature reading, so we surface them all, ordered by trailing
    number then name. The slug is humanised back into a display name.
    """
    temps: list[tuple[int, str, dict[str, str]]] = []
    for key, eid in role_map.items():
        if not eid or "temp" not in key:
            continue
        # First number anywhere in the slug is the probe index — works for
        # both `battery_temp_1` (JK) and `cell_1_temp` (JBD/Fogstar). An
        # unnumbered overall probe (`battery_temp` / `mosfet_temperature`)
        # sorts first (order 0).
        num_match = re.search(r"(\d+)", key)
        order = int(num_match.group(1)) if num_match else 0
        name = key.replace("_", " ").title()
        # Restore acronym casing that .title() flattens (BMS / MOSFET).
        name = name.replace("Bms", "BMS").replace("Mosfet", "MOSFET")
        temps.append((order, key, {"entity": eid, "name": name}))
    temps.sort(key=lambda t: (t[0], t[1]))
    return [slot for _, _, slot in temps]


def _build_bms_card(device: dict[str, Any]) -> dict[str, Any]:
    """Battery status card for JK and JBD family leisure batteries.

    Covers Roamer Gen 2 (JK) and Fogstar Drift Gen 2 (JBD), rendered by
    the native `custom:sensa-bms-card` (no HACS, no loadCardHelpers — see
    feedback_prefer_own_cards_over_hacs). JBD packs omit the State of
    Health gauge (not reported by the JBD protocol); its entity is absent
    from the role_map, so the card simply doesn't draw that gauge. Only
    the metrics/status entities present in the role_map are emitted.

    Like the VIP/Gen1 builder, JK/JBD packs report per-cell voltages and
    temperature probes, so the card gets a `cells` gauge row + a `temps`
    row beneath it (counts vary by pack, so both are derived dynamically
    from the role map rather than hardcoded). See sensa-bms-card.ts.
    """
    display_name = (
        device.get("display_name") or device.get("name")
        or "Leisure Battery"
    )
    role_map: dict[str, str] = device.get("bms_entities") or {}

    def eid(role: str) -> str:
        return role_map.get(role, "")

    # Resolve the photo through the SAME catalog lookup the Devices tab
    # uses (`_find_catalog_by_bms_family`), so one battery cannot show two
    # different pictures on two surfaces.
    #
    # This was a hardcoded {family: filename} map with a Roamer default,
    # which is the predicate-written-twice bug the catalog exists to
    # prevent: a family covers several brands (one Renogy protocol serves
    # the Pro and Core ranges), and a family the map had never heard of
    # silently inherited the Roamer photo. Observed with the Renogy Pro on
    # 2026-09-05 — the Devices tab was right and the BMS card was wrong for
    # the same pack. It is the same symptom the Devices-tab resolver was
    # written to fix for Fogstar; this surface just never got the fix.
    from ..sensor import _find_catalog_by_bms_family

    catalog_entry = _find_catalog_by_bms_family(_bms_family(device)) or {}
    image_file = (
        str(device.get("image") or "").strip()
        or str(catalog_entry.get("image") or "").strip()
        or "roamer-gen2-generic.webp"
    )
    # A row may carry either a bare filename or an already-rooted URL.
    image_url = (
        image_file if image_file.startswith("/")
        else f"/local/sensa/images/{image_file}"
    )

    metric_specs: list[tuple[str, str, str]] = [
        ("pack_voltage", "Voltage", "mdi:flash"),
        ("pack_current", "Current", "mdi:current-dc"),
        ("pack_power", "Power", "mdi:lightning-bolt"),
        ("remaining_capacity", "Remaining", "mdi:battery-charging-60"),
        ("cell_voltage_delta", "Cell Δ", "mdi:sine-wave"),
        ("cycle_count", "Cycles", "mdi:counter"),
    ]
    status_specs: list[tuple[str, str, str]] = [
        ("charging_allowed", "Charging", "mdi:battery-charging"),
        ("discharging_allowed", "Discharging", "mdi:battery-arrow-down"),
        ("balancing", "Balancing", "mdi:scale-balance"),
        ("fault", "Fault", "mdi:alert-circle"),
    ]

    def slots(
        specs: list[tuple[str, str, str]],
    ) -> list[dict[str, Any]]:
        return [
            _bms_metric_slot(eid(role), label, icon)
            for role, label, icon in specs if role in role_map
        ]

    card: dict[str, Any] = {
        "type": "custom:sensa-bms-card",
        "name": display_name,
        "image": image_url,
        "soc": eid("state_of_charge"),
        "metrics": slots(metric_specs),
        "status": slots(status_specs),
    }
    if "state_of_health" in role_map:
        card["soh"] = eid("state_of_health")

    cells = _bms_cell_voltage_slots(role_map)
    if cells:
        card["cells"] = cells
    temps = _bms_temp_slots(role_map)
    if temps:
        card["temps"] = temps

    current_eid = eid("pack_current")
    voltage_eid = eid("pack_voltage")
    if current_eid and voltage_eid:
        card["graphs"] = [
            {"entity": current_eid, "name": "Current"},
            {"entity": voltage_eid, "name": "Voltage"},
        ]
    return card


def _is_power_station(device: dict[str, Any]) -> bool:
    """A portable power station (the DJI Power 1000 V2): its own service
    class, so it never sums into the leisure-battery role."""
    return _device_service_class(device) == "power_station"


def _build_dji_power_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Everything the DJI Power station reports, on one `sensa-bms-card`.

    The role map is `service_entities`, attached by
    `__init__._attach_service_class_rows` from the `dji_power_<id>_` unique-id
    marker, so the keys are the vendor module's description keys. None until
    the state-of-charge entity exists (the tab is conditional on that).
    """
    role_map: dict[str, str] = device.get("service_entities") or {}
    soc = role_map.get("state_of_charge")
    if not soc:
        return None

    def eid(key: str) -> str:
        return role_map.get(key, "")

    from ..vendors.dji_ble import uses_station_session

    if uses_station_session(device):
        return _build_dji_station_card(device, role_map)

    metric_specs: list[tuple[str, str, str]] = [
        ("voltage_v", "Voltage", "mdi:flash"),
        ("current_a", "Current", "mdi:current-dc"),
        ("power_w", "Power", "mdi:lightning-bolt"),
        ("remaining_capacity_ah", "Remaining", "mdi:battery-charging-60"),
        ("full_capacity_ah", "Full Charge", "mdi:battery-high"),
        ("design_capacity_ah", "Design", "mdi:battery-outline"),
        ("cell_delta_v", "Cell Δ", "mdi:sine-wave"),
        ("cell_count", "Cells", "mdi:battery-sync"),
        ("serial_number", "Serial", "mdi:barcode"),
        ("product_code", "Product", "mdi:tag"),
        ("firmware_version", "Firmware", "mdi:chip"),
    ]
    status_specs: list[tuple[str, str, str]] = [
        ("charging", "Charging", "mdi:battery-charging"),
        ("discharging", "Discharging", "mdi:battery-arrow-down"),
    ]

    def slots(specs: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
        return [
            _bms_metric_slot(eid(key), label, icon)
            for key, label, icon in specs if key in role_map
        ]

    card: dict[str, Any] = {
        "type": "custom:sensa-bms-card",
        "name": device.get("display_name") or device.get("name") or "DJI Power 1000 V2",
        "image": "/local/sensa/images/dji-power-1000-v2.webp",
        "soc": soc,
        "metrics": slots(metric_specs),
        "status": slots(status_specs),
        # A portable unit that is switched off is a normal state, not a fault:
        # the card says so instead of showing a wall of dashes.
        "offline_title": "DJI unit is currently powered off",
        "offline_note": _DJI_OFFLINE_NOTE,
    }
    if "state_of_health" in role_map:
        card["soh"] = eid("state_of_health")
    cells = [
        role_map[f"cell_{i}"]
        for i in range(1, 33) if f"cell_{i}" in role_map
    ]
    if cells:
        card["cells"] = cells
    if "temperature_c" in role_map:
        card["temps"] = [{"entity": eid("temperature_c"), "name": "Pack Temp"}]
    if "current_a" in role_map and "voltage_v" in role_map:
        card["graphs"] = [
            {"entity": eid("current_a"), "name": "Current"},
            {"entity": eid("voltage_v"), "name": "Voltage"},
        ]
    return card


_DJI_OFFLINE_NOTE = (
    "Press the power button on the unit and it reconnects within a minute. "
    "It also switches itself off after an hour with nothing plugged in."
)


def _build_dji_station_card(device: dict[str, Any], role_map: dict[str, str]) -> dict[str, Any]:
    """A unit on the station session (Power 2000 with its pair key) reports
    SoC, watts, time left and temperature, and has an AC switch; it has no
    SoH, cells or pack voltage, so those slots are left out rather than
    shown as unknown."""
    # (role, label, icon, everyday)
    specs: list[tuple[str, str, str, bool]] = [
        ("output_power_w", "Output", "mdi:export", True),
        ("input_power_w", "Input", "mdi:import", True),
        ("ac_output_power_w", "AC Out", "mdi:power-socket", True),
        ("ac_input_power_w", "AC In", "mdi:power-plug", True),
        ("remaining_minutes", "Time Left", "mdi:timer-sand", True),
        ("temperature_c", "Temperature", "mdi:thermometer", False),
        ("firmware_version", "Firmware", "mdi:chip", False),
    ]
    card: dict[str, Any] = {
        "type": "custom:sensa-bms-card",
        "name": device.get("display_name") or device.get("name") or "DJI Power",
        "image": "/local/sensa/images/dji-power-1000-v2.webp",
        "soc": role_map["state_of_charge"],
        "metrics": [
            {"entity": role_map[key], "name": label, "icon": icon, "everyday": everyday}
            for key, label, icon, everyday in specs if key in role_map
        ],
        "offline_title": "DJI unit is currently powered off",
        "offline_note": _DJI_OFFLINE_NOTE,
    }
    if "ac_output" in role_map:
        card["power"] = role_map["ac_output"]
        card["power_label"] = "AC output"
    return card


def build_dji_power_cards(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One card per registered DJI Power station whose entities exist, else
    [], which is what makes the Habitation DJI tab conditional."""
    cards: list[dict[str, Any]] = []
    for device in devices:
        if not _is_power_station(device):
            continue
        card = _build_dji_power_card(device)
        if card is not None:
            cards.append(card)
    return cards


def build_tyre_cards(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One `sensa-tyres-card` listing every registered tyre sensor whose
    pressure entity exists, else [], which is what makes the Cab Tyres tab
    conditional.

    The role map is `service_entities` from the `tpms_ble_<id>_` unique-id
    marker. Positions are NOT resolved here: the card reads each sensor's
    Wheel Position select live, so moving a sensor to another wheel needs no
    dashboard rebuild.
    """
    sensors: list[dict[str, Any]] = []
    for device in devices:
        if _device_service_class(device) != "tyre_pressure":
            continue
        role_map: dict[str, str] = device.get("service_entities") or {}
        if not role_map.get("pressure"):
            continue
        sensor = {
            "name": device.get("display_name") or device.get("name") or "Tyre sensor",
            "pressure": role_map["pressure"],
        }
        for key in ("temperature", "battery", "last_seen", "alarm", "wheel_position"):
            if role_map.get(key):
                sensor[key] = role_map[key]
        sensors.append(sensor)
    if not sensors:
        return []
    return [{"type": "custom:sensa-tyres-card", "sensors": sensors}]


def _is_leisure_battery(device: dict[str, Any]) -> bool:
    """Filter predicate for rows the BMS view should render.

    Service-class based: the `battery` service class is, by definition,
    a leisure battery (every leisure-battery chemistry we support — JK,
    VIP, JBD — classifies there, and nothing else does). `classify()`
    resolves it from the `sensa_bms_ble` integration slug for rows that
    aren't carrying a stamped `service_class` yet.
    """
    return _device_service_class(device) == "battery"


def _is_obd2_device(device: dict[str, Any]) -> bool:
    """True for an OBD2 / engine-data adapter (e.g. MeatPi WiCAN Pro).

    OBD2 support has landed (the MeatPi WiCAN Pro: discovered via
    `vendors/wican_mqtt`, registered with integration_type `wican` and
    `service_class=obd2`). This detection is deliberately broad so the
    Cab Engine tab auto-appears for it however the row is shaped: a
    stamped `service_class` of `obd2`/`engine`, a WiCAN integration
    slug, or an OBD/WiCAN marker in the device metadata. Returns False
    for every non-OBD2 device, so the tab stays hidden otherwise.
    """
    if _device_service_class(device) in ("obd2", "engine"):
        return True
    integration = str(
        device.get("integration") or device.get("integration_type") or ""
    ).lower()
    if integration in ("wican", "obd2", "sensa_obd2"):
        return True
    haystack = " ".join(
        str(device.get(k, "")) for k in (
            "catalog_id", "vendor_device_type", "device_type", "type", "model",
        )
    ).lower()
    return "obd" in haystack or "wican" in haystack


# ===========================================================================
# Service-class abstraction — view routing + role resolution
#
# `_attach_service_class_rows` (in __init__.py) stamps a canonical
# `service_class` on every device row and appends synthetic rows for Cerbo
# sub-devices. The functions below consume that: `_service_rows_for_view`
# picks the rows for a view by category, `resolve_role` answers "which
# entity supplies this reading?" with a vendor-agnostic preference order.
# See docs/system-design/service-classes.md.
# ===========================================================================


@dataclass(frozen=True)
class RoleSpec:
    """How to resolve one logical role (e.g. `battery_soc`) to entities.

    `mode`            — "pick" returns the single highest-priority entity;
                        "sum" returns every matching entity (the consuming
                        card sums them — a van with 2 MPPTs needs both).
    `sources`         — ordered (service_class, role_key) preference list;
                        `role_key` is the key as it appears in that device's
                        role-map (`service_entities` / `bms_entities`).
    `system_fallback` — key in the Cerbo `victron_system_entities` map used
                        when no per-device source is available (the Cerbo
                        system aggregates are already summed across devices).
                        `None` for roles where the Cerbo aggregate is
                        misleading (e.g. `battery_*` — Venus computes
                        Dc/Battery from whatever DC source it's tracking,
                        which on a van with no real shunt is just noise).
    `meter_fallback`  : last resort after the Cerbo: every live circuit of
                        every Sensa DC Meter, as a list the card sums. Only
                        for `dc_load`; a Cerbo's aggregate wins because it
                        sees every load, a meter only the circuits it is in.
    """

    mode: str
    sources: tuple[tuple[str, str], ...]
    system_fallback: str | None = None
    meter_fallback: bool = False


# The role catalogue the Electrical view + power-flow card resolve against.
# Adding a vendor never touches this — it keys off canonical service classes.
ROLE_SOURCES: dict[str, RoleSpec] = {
    # Battery slot deliberately has NO Cerbo system_fallback. The
    # `Dc/Battery/*` aggregates Venus OS computes from whichever DC
    # source it's currently treating as the battery (which can be the
    # PV input, a stub default, or nothing) are misleading on a van
    # without a real shunt. The honest hierarchy is:
    #   1. Any vendor's shunt classifying as `battery_monitor`
    #      (Victron SmartShunt, Lynx Shunt, etc.)
    #   2. Any leisure-battery BMS (Roamer, Fogstar, …)
    #   3. Otherwise no battery chip — better than a misleading number.
    "battery_soc": RoleSpec(
        mode="pick",
        sources=(
            ("battery_monitor", "soc"),       # SmartShunt and equivalents
            ("battery", "state_of_charge"),   # leisure-battery BMS
        ),
        system_fallback=None,
    ),
    "battery_power": RoleSpec(
        mode="pick",
        sources=(
            ("battery_monitor", "power"),
            ("battery", "pack_power"),
        ),
        system_fallback=None,
    ),
    "battery_current": RoleSpec(
        mode="pick",
        sources=(
            ("battery_monitor", "current"),
            ("battery", "pack_current"),
        ),
        system_fallback=None,
    ),
    "battery_voltage": RoleSpec(
        mode="pick",
        sources=(
            ("battery_monitor", "voltage"),
            ("battery", "pack_voltage"),
        ),
        system_fallback=None,
    ),
    # The battery's OWN charge-cycle counter, for the History tab. Only a BMS
    # that reports one resolves; no estimate is made for a pack without it.
    "battery_cycles": RoleSpec(
        mode="pick",
        sources=(("battery", "cycle_count"),),
        system_fallback=None,
    ),
    # Starter / vehicle battery voltage — the shunt's secondary (aux)
    # voltage input (Venus battery service `Dc/1/Voltage`). Read-only on
    # the power-flow detail card; no system fallback (only a real shunt
    # with the aux wire fitted reports it).
    "battery_starter_voltage": RoleSpec(
        mode="pick",
        sources=(("battery_monitor", "starter_voltage"),),
        system_fallback=None,
    ),
    "solar_power": RoleSpec(
        mode="sum",
        sources=(("solar_charger", "pv_power"),),
        system_fallback="pv_power",
    ),
    "grid_power": RoleSpec(
        mode="pick",
        sources=(("grid_meter", "power"),),
        system_fallback="ac_grid_power",
    ),
    "ac_load": RoleSpec(
        mode="pick",
        sources=(),
        system_fallback="ac_consumption_output",
    ),
    # The Multiplus/Quattro inverter-mode CONTROL (Off / Charger only /
    # Inverter only / On) — a `select.*` entity bucketed into the vebus
    # sub-device's role-map as `inverter_mode`. The Electrical power-flow
    # detail card renders it as a segmented control beside the AC Load
    # node. Pick the first inverter_charger (one per Base RV); no system
    # fallback — there's no aggregate control, only a per-Multi register.
    "inverter_mode": RoleSpec(
        mode="pick",
        sources=(("inverter_charger", "inverter_mode"),),
        system_fallback=None,
    ),
    # The Orion XS starter-battery charge CONTROL (emergency reverse charge,
    # Orion firmware 1.15) - a `switch.*` on the Orion's Cerbo sub-device.
    # The detail card renders it as a button under the Starter node.
    "starter_charge": RoleSpec(
        mode="pick",
        sources=(("dc_dc_charger", "starter_charge"),),
        system_fallback=None,
    ),
    # Shore-conditional AC load on a Multiplus AC-Out-2 line. Resolves
    # only when the customer has wired an external energy meter on
    # AC-Out-2 and configured it in Cerbo as Position=1 — the vebus
    # device itself can't split per-output power (Victron dbus wiki:
    # "There is only a measurement for the total output power; ie AC
    # out1 & AC out 2 are not independently measured."). No system
    # fallback: the system aggregate doesn't break the loads down by
    # AC output, so without the dedicated meter there's no honest
    # source for this slot — the card omits it rather than show a
    # misleading number.
    "shore_ac_load": RoleSpec(
        mode="pick",
        sources=(("ac_output_meter", "power"),),
    ),
    "dc_load": RoleSpec(
        mode="pick",
        sources=(),
        system_fallback="dc_system_power",   # Cerbo Dc/System/Power aggregate
        meter_fallback=True,                 # else the Sensa DC Meter circuits
    ),
    "alternator": RoleSpec(
        mode="sum",
        sources=(("dc_dc_charger", "output_power"),),  # Orion XS / alternator
    ),
    "system_state": RoleSpec(
        mode="pick",
        sources=(),
        system_fallback="system_state",
    ),
}


def _device_service_class(device: dict[str, Any]) -> str | None:
    """The device's canonical service class — the stamped value when
    `_attach_service_class_rows` has run, else derived on the spot so
    callers (and tests passing raw rows) still work."""
    from ..vendors.service_classes import classify
    return device.get("service_class") or classify(device)


def _device_role_map(device: dict[str, Any]) -> dict[str, str]:
    """The device's {role_key: entity_id} map, whichever attach helper
    populated it. `service_entities` (service-class rows + Orion XS),
    `bms_entities` (leisure batteries), `network_entities` (routers),
    `levelling_entities` (van level sensors)."""
    return (
        device.get("service_entities")
        or device.get("bms_entities")
        or device.get("network_entities")
        or device.get("levelling_entities")
        or {}
    )


def _cerbo_system_entities() -> dict[str, str]:
    """The first registered Cerbo's system-aggregate role-map, or empty.
    These aggregates are summed across the whole installation by Venus OS,
    so they are the natural fallback for both pick- and sum-mode roles."""
    for d in _dashboard_devices:
        integration = d.get("integration_type") or d.get("integration") or ""
        if integration == "sensa_victron_mqtt":
            system_map = d.get("victron_system_entities") or {}
            if system_map:
                return system_map
    return {}


def _resolve_role_image(role: str) -> str | None:
    """`/local/sensa/images/<file>` URL for the device backing `role`.

    Walks the same source list `resolve_role` does, but stops at the
    first matching device that carries a live entity AND a catalog
    `image` (or falls through to the sub-device default stamped in
    `_attach_service_class_rows`). Returns None when no device backs
    the role, no image is known, or when the role is sum-mode and
    multiple devices of different makes contribute (showing one
    vendor's product photo for a 2-MPPT mixed-brand setup would be
    misleading; the card falls back to its MDI icon for that case).

    Doubles as a debug aid — at a glance you can tell which physical
    device the slot is currently sourcing.
    """
    from ..sensor import _pick_image

    spec = ROLE_SOURCES.get(role)
    if spec is None:
        return None

    matched_rows: list[dict[str, Any]] = []
    for service_class, role_key in spec.sources:
        for d in _dashboard_devices:
            if _device_service_class(d) != service_class:
                continue
            eid = _device_role_map(d).get(role_key)
            if eid and _entity_is_live(eid):
                matched_rows.append(d)
        if matched_rows and spec.mode == "pick":
            break  # pick mode stops at the first source class with hits

    if not matched_rows:
        return None

    if spec.mode == "sum" and len({r.get("image") for r in matched_rows}) > 1:
        # Mixed-brand sum (e.g. Victron MPPT + Renogy MPPT) — no honest
        # single image to show, defer to the icon.
        return None

    first = matched_rows[0]
    if not first.get("image"):
        return None
    return _pick_image(first)


def _entity_is_live(eid: str) -> bool:
    """True if `eid` is a viable candidate for a role slot.

    Returns True when `hass` isn't stashed yet (tests, pre-setup) so the
    resolver keeps its historical "trust the role-map" behaviour. When
    `hass` is present we drop entities the state machine has never heard
    of (`hass.states.get(eid) is None`) and entities currently reporting
    `unavailable` (the coordinator-level "I exist but have no data"
    signal — happens when a Cerbo sub-device's CustomName arrived but
    none of its data topics did). Without this filter such a sub-device
    would short-circuit the system-aggregate fallback and leave the
    slot showing "—" on the power-flow card.

    `unknown` is deliberately kept as live — entities are `unknown`
    immediately after setup before the first MQTT keepalive tick has
    landed, and treating that transient state as dead would cause the
    Electrical view to disappear on every reload until a debounced
    regen ran a few seconds later. The card itself renders a "—" for
    an unknown-state entity, which is the honest UI for "data on its
    way."
    """
    if _dashboard_hass is None:
        return True
    state = _dashboard_hass.states.get(eid)
    if state is None:
        return False
    return state.state != "unavailable"


def resolve_role(role: str) -> str | list[str] | None:
    """Resolve a logical role to entity_id(s) via `ROLE_SOURCES`.

    pick mode → the highest-priority available entity_id, else the Cerbo
    system aggregate, else None.
    sum  mode → every matching entity_id across all devices of the listed
    service classes, else the (pre-summed) Cerbo system aggregate as a
    one-element list, else None.

    Per-device candidates whose entity_id isn't live (missing /
    unavailable / unknown) are skipped so the fallback can take over —
    see `_entity_is_live` for the rationale.
    """
    spec = ROLE_SOURCES.get(role)
    if spec is None:
        return None

    if spec.mode == "sum":
        found: list[str] = []
        for service_class, role_key in spec.sources:
            for d in _dashboard_devices:
                if _device_service_class(d) != service_class:
                    continue
                eid = _device_role_map(d).get(role_key)
                if eid and _entity_is_live(eid):
                    found.append(eid)
        if found:
            return found
        sys_eid = _cerbo_system_entities().get(spec.system_fallback)
        if sys_eid and _entity_is_live(sys_eid):
            return [sys_eid]
        return None

    # pick mode
    for service_class, role_key in spec.sources:
        for d in _dashboard_devices:
            if _device_service_class(d) != service_class:
                continue
            eid = _device_role_map(d).get(role_key)
            if eid and _entity_is_live(eid):
                return eid
    sys_eid = _cerbo_system_entities().get(spec.system_fallback)
    if sys_eid and _entity_is_live(sys_eid):
        return sys_eid
    if spec.meter_fallback:
        circuits = [
            eid for _device, power in _dc_meter_circuits()
            for eid in power.values() if _entity_is_live(eid)
        ]
        if circuits:
            return circuits
    return None


def _dc_meter_circuits() -> list[tuple[dict[str, Any], dict[int, str]]]:
    """Every Sensa DC Meter row with its circuit -> power entity map. A meter
    none of whose circuits resolve yet is left out."""
    from ..dc_meter import circuit_power_entities, is_dc_meter

    if _dashboard_hass is None:
        return []
    found: list[tuple[dict[str, Any], dict[int, str]]] = []
    for device in _dashboard_devices:
        if not is_dc_meter(device):
            continue
        try:
            power = circuit_power_entities(_dashboard_hass, device)
        except Exception:  # noqa: BLE001 - one meter must not break the dashboard
            _LOGGER.debug("DC meter: could not resolve circuits", exc_info=True)
            continue
        if power:
            found.append((device, power))
    return found


def _service_rows_for_view(category: str) -> list[dict[str, Any]]:
    """Device rows whose service class rolls up into `category`. This is
    the whole of a view's device-discovery — no per-vendor branching."""
    from ..vendors.service_classes import category_of
    rows: list[dict[str, Any]] = []
    for d in _dashboard_devices:
        service_class = _device_service_class(d)
        if service_class and category_of(service_class) == category:
            rows.append(d)
    return rows


def _build_power_flow_card() -> dict[str, Any] | None:
    """The Electrical view's hero card — `custom:sensa-power-flow-detail-card`.

    Shared role-resolution for BOTH power-flow cards: this returns the full
    config (every slot), typed as the DETAIL card for the Electrical hero.
    `_home_center_power_flow` reuses it and swaps the type to the basic
    `custom:sensa-power-flow-card` for the Home tab (same slots, fewer nodes).

    Each slot is filled by `resolve_role`, so the card is brand-agnostic:
    solar from whatever MPPTs exist (summed), battery from the best
    available monitor with the Cerbo aggregate as the last resort, etc.
    Returns None when nothing resolves, so the card is simply absent on an
    install with no electrical telemetry yet.

    The card's visual flow design is developed iteratively; this builder
    only owns the config contract (see webcards/src/sensa-power-flow-card.ts).
    """
    solar = resolve_role("solar_power")
    battery_power = resolve_role("battery_power")
    battery_soc = resolve_role("battery_soc")
    battery_current = resolve_role("battery_current")
    battery_starter = resolve_role("battery_starter_voltage")
    grid = resolve_role("grid_power")
    ac_load = resolve_role("ac_load")
    shore_ac_load = resolve_role("shore_ac_load")
    dc_load = resolve_role("dc_load")
    alternator = resolve_role("alternator")
    system_state = resolve_role("system_state")
    # The Multiplus inverter-mode control select (detail card only — the
    # basic Home/Mobile card ignores this key, gated on `compact`).
    inverter_mode = resolve_role("inverter_mode")
    starter_charge = resolve_role("starter_charge")

    # The alternator slot has no Cerbo system fallback (Venus folds DC-DC
    # power into Dc/Battery/Current rather than exposing it as a
    # standalone aggregate), so a registered DC-DC whose entity is
    # currently `unavailable` would otherwise drop off the diagram
    # entirely. Surface the chip as long as ANY dc_dc_charger device
    # (Victron Orion XS, Renogy, or any future vendor) is registered —
    # the card renders "—" when the underlying entity has no live
    # reading, which beats silently hiding the alternator branch of the
    # power flow on a van that genuinely has one.
    has_dc_dc_charger = any(
        _device_service_class(d) == "dc_dc_charger"
        for d in _dashboard_devices
    )

    if not any((
        solar, battery_power, battery_soc, battery_starter, grid,
        ac_load, shore_ac_load, dc_load, alternator, system_state,
        has_dc_dc_charger,
    )):
        return None

    card: dict[str, Any] = {"type": "custom:sensa-power-flow-detail-card"}
    if solar:
        card["solar"] = solar
        # Today's remaining and tomorrow's expected charge, as two lines on
        # the solar node itself (solar_forecast.py). Blank until the owner
        # sets a panel size.
        from ..const import (
            BATTERY_CAPACITY_ENTITY_ID,
            SOLAR_FORECAST_TODAY_ENTITY_ID,
            SOLAR_FORECAST_TOMORROW_ENTITY_ID,
        )

        card["solar_forecast"] = SOLAR_FORECAST_TODAY_ENTITY_ID
        card["solar_forecast_tomorrow"] = SOLAR_FORECAST_TOMORROW_ENTITY_ID
        # The owner's battery size (Settings → Solar): the card adds the
        # forecast as a % of it. 0 = not set, kWh only.
        card["battery_capacity"] = BATTERY_CAPACITY_ENTITY_ID
    battery: dict[str, Any] = {}
    if battery_power:
        battery["power"] = battery_power
    if battery_soc:
        battery["soc"] = battery_soc
    if battery_current:
        battery["current"] = battery_current
    if battery:
        card["battery"] = battery
    # Starter / vehicle battery voltage — a separate read-only node on the
    # detail card. The basic Home/Mobile card ignores this slot, so the
    # node shows only on the Electrical hero.
    if battery_starter:
        card["starter_battery"] = battery_starter
    if grid:
        card["grid"] = grid
    if ac_load:
        card["ac_load"] = ac_load
    if inverter_mode:
        card["inverter_mode_entity"] = inverter_mode
    if starter_charge:
        card["starter_charge_entity"] = starter_charge
    if shore_ac_load:
        card["shore_ac_load"] = shore_ac_load
    if dc_load:
        card["dc_load"] = dc_load
    if alternator:
        card["alternator"] = alternator
    elif has_dc_dc_charger:
        # Empty list keeps the slot present in the YAML so the card
        # renders the chip; `_sum([])` resolves to null on the card
        # side, surfacing as "—" until the underlying entity comes
        # back to life.
        card["alternator"] = []
    if system_state:
        card["system_state"] = system_state

    # Per-slot product images — the card renders the device photo in
    # place of the MDI icon when one is present. Sourced from the same
    # device row each slot resolves through, so the customer (and
    # whoever's debugging) can see exactly which physical thing is
    # supplying each branch of the diagram. Slots without a backing
    # device, with no catalog image, or with a mixed-brand sum-mode
    # resolution stay imageless and show the icon.
    #
    # Wrapped in try/except per-slot: a bug in the image resolver MUST
    # NOT take down the whole Electrical view. The chip just falls back
    # to its MDI icon if image lookup fails, and we log it so the next
    # iteration has data to chase.
    for slot in ("solar", "battery", "grid", "alternator"):
        role = (
            "battery_power" if slot == "battery"
            else "grid_power" if slot == "grid"
            else f"{slot}_power" if slot == "solar"
            else slot
        )
        try:
            img = _resolve_role_image(role)
        except Exception:
            _LOGGER.warning(
                "power-flow card: image lookup for slot %r raised; "
                "falling back to icon",
                slot, exc_info=True,
            )
            img = None
        if img:
            card[f"{slot}_image"] = img

    return card


def _victron_coordinator_host(device_id: str | None) -> str:
    """The LAN host of the running Victron MQTT coordinator for `device_id`,
    or "" (no hass / no coordinator / no host).

    The backend device row frequently doesn't echo `ip_address`/`config.host`
    back, but a coordinator can only be RUNNING — which it is, since the
    Cerbo's sensors are live — if it resolved a host to connect to. So read
    the host straight off the coordinator.

    Resolved HERE, at card-build time, rather than at device-enrichment time:
    `_attach_service_class_rows` runs during `async_setup_entry` BEFORE
    `_async_start_vendor_integrations` creates the coordinator, so an
    enrichment-time lookup always misses it. The panel is re-served on every
    WS request / regen, by which point the coordinator exists.
    """
    if not device_id or _dashboard_hass is None:
        return ""
    from ..const import DOMAIN

    data = getattr(_dashboard_hass, "data", None) or {}
    for runtime in (data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        coord = (runtime.get("vendor_coordinators") or {}).get(device_id)
        host = getattr(coord, "host", None) if coord is not None else None
        if host:
            return str(host).strip()
    return ""


# Bundled Victron Cerbo GX photo, served by the add-on at this path. Used as
# the power-flow hub graphic when a Cerbo's Remote Console is reachable (a GX
# variant without a catalog image falls back to this).
_CERBO_HUB_IMAGE = "/local/sensa/images/victron-cerbo-gx.webp"


def _cerbo_hub_info() -> dict[str, Any] | None:
    """Hub graphic + (optional) Remote Console URL for a registered Cerbo GX,
    or None when no Cerbo is in the device list.

    Folded INTO the power-flow detail card (2026-06-13): the Cerbo IS the
    system hub, so the diagram's central hub shows the Cerbo photo (lightning
    bolt only when there's NO Cerbo) and — when reachable — opens the Venus OS
    Remote Console on tap, replacing the old standalone console button.

    `image` is ALWAYS present when a Cerbo is registered — decoupled from
    console reachability (Richard 2026-06-13: show the Cerbo photo whenever a
    Cerbo is in the registry; the image was previously gated on the console URL
    resolving, so an unreachable/console-off Cerbo wrongly showed the bolt).

    `url`/`name` are present only when a LAN host resolves — a customer-set
    `config.host`, else the discovered `ip_address`, else the live coordinator's
    host (`_victron_coordinator_host` — the backend row often doesn't echo the
    IP back). The console only loads when "Remote Console on LAN" is enabled in
    Venus OS. The demo Cerbo *simulator* has no web GUI, so the tap-to-open is a
    real-Cerbo feature; the photo shows for the sim too.

    `url` is a PATH on this box wherever the App is proxying the console
    (`_cerbo_console_path`), and only falls back to the Cerbo's own
    `http://<ip>/` where it is not. That fallback is what shipped until
    2026-09-02, and it was broken in two ways at once: an https page refuses to
    frame an http origin, and the new-window link the card offered instead was
    unreachable away from the van, because the tunnel publishes Home Assistant
    and nothing else. Do NOT "simplify" this back to the direct address.
    """
    # Resolve the hub photo. CRITICAL: the URL MUST carry the same `?v=`
    # cache-buster every OTHER image path uses (via `_pick_image`). Without
    # it the browser replays a 404 it cached for this exact bare URL from
    # before the asset shipped — so the Cerbo photo never appears even once
    # the file is on disk and the code is correct (the recurring "Cerbo
    # picture still not showing" report; same class of stale-404 trap the
    # `_IMAGE_CACHE_BUSTER` comment in sensor.py warns about).
    try:
        from ..sensor import _IMAGE_CACHE_BUSTER, _pick_image
    except Exception:  # noqa: BLE001 - image is cosmetic, never fatal
        _IMAGE_CACHE_BUSTER, _pick_image = "", None  # type: ignore[assignment]

    def _bust(url: str) -> str:
        if not url or "?" in url or not _IMAGE_CACHE_BUSTER:
            return url
        return f"{url}?v={_IMAGE_CACHE_BUSTER}"

    for d in _dashboard_devices:
        integration = d.get("integration_type") or d.get("integration") or ""
        if integration != "sensa_victron_mqtt":
            continue
        image = _bust(_CERBO_HUB_IMAGE)
        row_image = str(d.get("image") or "")
        if row_image:
            try:
                if row_image.startswith(("/", "http")):
                    # Already a full URL — don't re-prefix (that double-prefix
                    # bug produced /local/sensa/images//local/... → a 404).
                    image = _bust(row_image)
                elif _pick_image is not None:
                    # Bare catalog filename — `_pick_image` prefixes + busts.
                    image = _pick_image(d) or image
            except Exception:  # noqa: BLE001, S110 - image is cosmetic, never fatal
                pass
        info: dict[str, Any] = {"image": image}
        config = d.get("config") or {}
        ip = (
            str(config.get("host") or "").strip()
            or str(d.get("ip_address") or "").strip()
            or _victron_coordinator_host(d.get("device_id"))
        )
        if ip:
            name = d.get("display_name") or d.get("name") or "Cerbo GX"
            # Prefer this box's own origin. The App proxies the console through
            # the nginx that already fronts Home Assistant, which is also the
            # tunnel's origin, so a relative path is the ONE address that works
            # both on the van and away from it. It also sidesteps the mixed
            # content block for free, since there is no second origin left to
            # block. Whether that proxy exists is the App's answer, never
            # re-derived here.
            info["url"] = _cerbo_console_path() or f"http://{ip}/"
            info["name"] = f"{name} Console"
        return info
    return None


def _cerbo_console_path() -> str | None:
    """The proxied console path, or None to use the Cerbo's direct address.

    None covers a box with no proxy (HAOS, or branding not deployed), an App too
    old to report one, and a dashboard built before the first status poll landed.
    All three want the same thing: the behaviour that shipped before this
    existed.
    """
    if _dashboard_hass is None:
        return None
    try:
        from ..cerbo_console import console_path

        return console_path(_dashboard_hass)
    except Exception:  # noqa: BLE001 - never fail a dashboard build over this
        return None


# Display order + label/icon for each BatteryProtect card row. Roles come from
# `service_entities` (populated by `_attach_victron_ble_battery_protect_roles`);
# a role with no registered entity is omitted, so the card only shows what the
# adopted victron_ble device actually exposes.
_BATTERY_PROTECT_ROWS: tuple[tuple[str, str, str], ...] = (
    ("output_state", "Output", "mdi:power-plug"),
    ("input_voltage", "Input", "mdi:flash"),
    ("output_voltage", "Output Voltage", "mdi:flash-outline"),
    ("device_state", "State", "mdi:information-outline"),
    ("alarm_reason", "Alarm", "mdi:alert-circle"),
    ("warning_reason", "Warning", "mdi:alert"),
    ("off_reason", "Off Reason", "mdi:power-off"),
    ("error_code", "Error", "mdi:alert-octagon"),
)


# --- Bitcoin miner (CryptoGenerator) ---------------------------------------
#
# The miner's entities are Sensa-owned and all sit on one HA device card, so
# they are resolved straight from the registry by that device's identifier
# rather than through a role-map. There is no vendor metadata to disambiguate
# here, one device, one known identifier, so an extra attach step would be a
# second copy of a fact the registry already holds.

def _luckyminer_entities(device: dict[str, Any]) -> dict[str, str]:
    """`{unique_id_suffix: entity_id}` for one miner, or {} before setup.

    Keys are the description keys from `luckyminer_entities` (`hashrate`,
    `power`, `session_sats`, ...), recovered from the tail of each entity's
    unique_id. Empty until the platforms have registered, which is why every
    caller omits missing rows rather than rendering a broken reference.
    """
    device_id = device.get("device_id") or ""
    if not device_id or _dashboard_hass is None:
        return {}
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        from ..const import DOMAIN

        dev_reg = dr.async_get(_dashboard_hass)
        ent_reg = er.async_get(_dashboard_hass)
        ha_device = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"luckyminer_{device_id}")}
        )
        if ha_device is None:
            return {}
        out: dict[str, str] = {}
        marker = f"luckyminer_{device_id}_"
        for entry in er.async_entries_for_device(
            ent_reg, ha_device.id, include_disabled_entities=False,
        ):
            uid = entry.unique_id or ""
            if marker in uid:
                out[uid.split(marker, 1)[1]] = entry.entity_id
        return out
    except Exception:  # noqa: BLE001 - never fail a dashboard build over this
        return {}


def _is_bitcoin_miner(device: dict[str, Any]) -> bool:
    return (
        _device_service_class(device) == "bitcoin_miner"
        or (device.get("integration_type") or device.get("integration") or "")
        == "sensa_luckyminer_api"
    )


# Rows on a miner's card, in the order they answer a customer's questions:
# is it working, what is it costing me, what is it making me, how hot is it.
# --- CryptoGenerator: one card, the whole tab ------------------------------
#
# The tab IS one card, `custom:sensa-crypto-generator-card`, laid out to the
# Claude Design source (project bed94f0c, CryptoGenerator.dc.html): header
# with fleet totals, the heat-recovery band, Cash / the two miners / Controls
# in one row, then the power status bar with the master power-cut. Every
# figure on it comes from an entity named here; fleet totals are summed by
# the card so the backend never carries a second copy of "fleet hash rate".

# (unique_id suffix, card config field). Suffixes are the description/spec
# keys in `luckyminer_entities.py`. A suffix the registry hasn't got is
# left out of the slot and the card shows that row as unavailable, never a
# broken reference.
_MINER_CARD_FIELDS: tuple[tuple[str, str], ...] = (
    ("hashrate", "hashrate"),
    ("power", "power"),
    ("temperature", "temperature"),
    ("fan_rpm", "fan_rpm"),
    ("wifi_rssi", "wifi_rssi"),
    ("heat_output", "heat_output"),
    ("uptime", "uptime"),
    ("session_sats", "session_sats"),
    ("session_value", "session_value"),
    ("expected_daily_sats", "expected_daily_sats"),
    ("expected_daily_value", "expected_daily_value"),
    ("expected_monthly_value", "expected_monthly_value"),
    ("btc_price", "btc_price"),
    ("block_odds_years", "block_odds_years"),
    ("frequency", "frequency"),
    ("core_voltage_mv", "core_voltage"),
    ("restart", "restart"),
    ("clear_overheat", "clear_overheat"),
    ("problem", "problem"),
)


def _build_miner_slot(device: dict[str, Any]) -> dict[str, Any] | None:
    """One miner's entry in the card's `miners` list, or None before its
    telemetry entities exist (a slot with no hash rate and no power would
    draw a dead unit for a miner that is merely still setting up)."""
    entities = _luckyminer_entities(device)
    if not entities:
        return None
    slot: dict[str, Any] = {
        "name": device.get("display_name") or "Crypto Miner",
    }
    for suffix, field in _MINER_CARD_FIELDS:
        if suffix in entities:
            slot[field] = entities[suffix]
    if "hashrate" not in slot and "power" not in slot:
        return None
    # A relay channel assigned to THIS miner as an Integrated Device (Settings,
    # Switching, Device Type = the miner). `_attach_power_relays` stamps it; it
    # is the miner's only real off switch and the card's master cut acts on
    # every miner's one.
    if device.get("power_relay"):
        slot["power_relay"] = device["power_relay"]
    return slot


def _leisure_battery_slots(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per registered leisure battery for the CryptoGenerator
    power bar: the same name and product photo the BMS tab shows, plus the
    pack's SoC and voltage entities when the role map has them. The rig
    runs off these packs, so the bar shows which ones and how they stand."""
    slots: list[dict[str, Any]] = []
    for device in devices:
        if not _is_leisure_battery(device):
            continue
        card = (
            _build_roamer_gen1_card(device) if _bms_family(device) == "vip"
            else _build_bms_card(device)
        )
        role_map: dict[str, str] = device.get("bms_entities") or {}
        slot: dict[str, Any] = {"name": card["name"], "image": card["image"]}
        for field, role in (
            ("soc", "state_of_charge"),
            ("voltage", "pack_voltage"),
            ("current", "pack_current"),
        ):
            if role_map.get(role):
                slot[field] = role_map[role]
        slots.append(slot)
    return slots


def build_cryptogenerator_cards(
    devices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The CryptoGenerator tab: a single card, or [] when no miner has
    registered entities yet, which is what makes the tab conditional.

    `master_cut` lists every miner's `power_relay` (the relay channel a
    customer assigned to that miner on Settings, Switching) and is absent
    when no miner has one; the card renders the control disabled in that
    case rather than faking one, since there is no software power-off.
    """
    miners = [d for d in devices if _is_bitcoin_miner(d)]
    if not miners:
        return []
    slots = [s for s in (_build_miner_slot(d) for d in miners) if s]
    if not slots:
        return []
    card: dict[str, Any] = {
        "type": "custom:sensa-crypto-generator-card",
        # A lone card in sensa-card-grid is capped at 480px and centred;
        # this one IS the tab, so it spans the grid.
        "full_width": True,
        "miners": slots,
        "miner_image": "/local/sensa/images/lucky-miner.webp",
        "heater_image": "/local/sensa/images/autoterm-air-d.webp",
    }
    battery_voltage = resolve_role("battery_voltage")
    if battery_voltage:
        card["battery_voltage"] = battery_voltage
    batteries = _leisure_battery_slots(devices)
    if batteries:
        card["batteries"] = batteries
    relays: list[str] = []
    for slot in slots:
        relay = slot.get("power_relay")
        if relay and relay not in relays:
            relays.append(relay)
    if relays:
        card["master_cut"] = relays
    # The autopilot's settings and status, for the card's Controls panel.
    # Always present: the entities are hub singletons that exist whether or
    # not a miner has a relay yet (the engine says so in its status instead).
    card["autopilot"] = _cryptogenerator_autopilot_block()
    return [card]


def _cryptogenerator_autopilot_block() -> dict[str, str]:
    from ..const import (
        CRYPTO_AUTOPILOT_NUMBERS,
        CRYPTO_AUTOPILOT_SENSOR_ENTITY_ID,
        CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID,
        CRYPTO_HEATER_OFF_SWITCH_ENTITY_ID,
        CRYPTO_ROOM_SENSOR_SELECT_ENTITY_ID,
        crypto_autopilot_number_entity_id,
    )

    block = {
        "enabled": CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID,
        "room_sensor": CRYPTO_ROOM_SENSOR_SELECT_ENTITY_ID,
        "heater_off": CRYPTO_HEATER_OFF_SWITCH_ENTITY_ID,
        "status": CRYPTO_AUTOPILOT_SENSOR_ENTITY_ID,
    }
    for key in CRYPTO_AUTOPILOT_NUMBERS:
        block[key] = crypto_autopilot_number_entity_id(key)
    return block


def _build_battery_protect_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only `custom:sensa-battery-protect-card` for a Victron Smart
    BatteryProtect.

    Rows come from the device's `service_entities` role-map (output state,
    input/output voltage, alarm + warning, off-reason, device state); a missing
    role is omitted. Returns None when no entities are mapped yet — the caller
    skips the card rather than render an empty one (same gate the network cards
    use on `network_entities`).
    """
    role_map = device.get("service_entities") or {}
    rows = [
        {"entity": role_map[role], "name": label, "icon": icon}
        for role, label, icon in _BATTERY_PROTECT_ROWS
        if role_map.get(role)
    ]
    if not rows:
        return None
    display_name = (
        device.get("display_name") or device.get("name") or "Battery Protect"
    )
    return {
        "type": "custom:sensa-battery-protect-card",
        "name": display_name,
        "image": "/local/sensa/images/victron-battery-protect.webp",
        "rows": rows,
    }


def _build_service_class_card(device: dict[str, Any]) -> dict[str, Any]:
    """Generic, brand-agnostic per-device card driven by the device's
    `service_entities` role-map and its `ServiceClass` metadata.

    Deliberately simple this pass — an `entities` card titled with the
    device's display name (the customer's VictronConnect CustomName when
    set) and icon'd by service class. One card per device row, so a van
    with two MPPTs gets two solar-charger cards. Layout is refined
    alongside the power-flow card.
    """
    from ..vendors.service_classes import SERVICE_CLASSES

    service_class = _device_service_class(device) or ""
    meta = SERVICE_CLASSES.get(service_class)
    display_name = (
        device.get("display_name")
        or device.get("name")
        or (meta.label if meta else "Device")
    )
    role_map = device.get("service_entities") or {}
    # `custom_name` is metadata that titles the card, not a metric row.
    rows = [
        {"entity": eid}
        for key, eid in sorted(role_map.items())
        if key != "custom_name"
    ]
    card: dict[str, Any] = {
        "type": "entities",
        "title": display_name,
        "show_header_toggle": False,
        "entities": rows,
    }
    if meta and meta.icon:
        card["icon"] = meta.icon
    return card









def build_panel_config(dashboard_key: str) -> dict[str, Any]:
    """Shell config for a `panel_custom` dashboard.

    Maps a panel key to the matching dashboard builder and returns its
    `custom:sensa-dashboard-shell` card dict (logo, chips, conditional
    tabs, header_show_trial, …) — the exact same config the Lovelace
    version produces. The integration passes this to panel_custom; the
    `<sensa-panel>` web component hydrates the tab cards the same way
    the shell card does, and the WS command re-serves it on a
    live-refresh. The `kiosk_hide_*` flags in the dict are inert for a
    panel; sidebar-hide is driven by the sibling `kiosk_hide_sidebar`
    config key the integration sets per install flavour.
    """
    builder = _PANEL_BUILDERS.get(dashboard_key)
    if builder is None:
        raise KeyError(f"unknown panel dashboard key: {dashboard_key!r}")
    shell = builder()["views"][0]["cards"][0]
    # The customer's drag-reordered tab strip (tab_order.py) and each tab's
    # Design Mode card layout (grid_layout.py).
    # A dealer that fixes every page (`homepage_mode: locked`) gets its own
    # tab order and layouts whatever the customer saved, and no Design Mode
    # button (the header reads `design_locked`).
    from ..grid_layout import apply_grid_layouts
    from ..layout_catalog import design_locked
    from ..tab_order import apply_tab_order

    locked = design_locked(_dashboard_hass)
    if locked:
        shell["design_locked"] = True
    if shell.get("tabs"):
        shell["tabs"] = apply_grid_layouts(
            dashboard_key,
            shell["tabs"] if locked else apply_tab_order(dashboard_key, shell["tabs"]),
            lambda: grid_card_catalog({dashboard_key: shell["tabs"]}),
            locked=locked,
        )
    # One line per fetch saying which tabs this panel got and from how many
    # devices, so "the tab is missing" can be answered from the log (turn the
    # integration's logger up to debug) rather than by rebuilding the box's
    # state on a workstation (2026-09-05, the Dump tab).
    _LOGGER.debug(
        "panel_config %s: tabs=%s devices=%d classes=%s",
        dashboard_key,
        [t.get("slug") for t in (shell.get("tabs") or [])],
        len(_dashboard_devices),
        sorted({str(_device_service_class(d)) for d in _dashboard_devices}),
    )
    return shell




def _network_entity(device: dict[str, Any], role: str) -> str:
    """Resolve a role slug to its registered entity_id via the role-map
    attached by `_attach_network_entity_prefixes`. Empty string when
    the entity isn't registered yet (freshly-added device, first regen
    before platform setup) — callers omit the row in that case rather
    than render a broken reference."""
    role_map = device.get("network_entities") or {}
    return role_map.get(role, "")


def _network_device_model(device: dict[str, Any]) -> str:
    """The model slug for a vendor-polled network device, lower-cased.

    `config.model` is where it actually lives: the vendor coordinator writes
    the slug it derived from the unit's own `/system/device/status` into the
    backend row's CONFIG (`teltonika_coordinator._maybe_enrich`,
    `starlink_coordinator`), and nothing lifts it to the top level. The
    top-level `model` / `vendor_model` are checked first only because a test
    fixture (and a hand-written row) may set them there.
    """
    return str(
        device.get("model")
        or device.get("vendor_model")
        or (device.get("config") or {}).get("model")
        or ""
    ).lower()


def _teltonika_image(device: dict[str, Any]) -> str:
    """Return the model-specific Teltonika product image filename, or
    the generic fallback. Matches the assets shipped in
    `webcards/assets/images/`."""
    model = _network_device_model(device)
    if "rutx11" in model:
        return "teltonika-rutx11.webp"
    if "rutx50" in model:
        return "teltonika-rutx50.webp"
    if "tsw101" in model:
        return "teltonika-tsw101.webp"
    if "tsw202" in model:
        return "teltonika-tsw202.webp"
    return "teltonika-generic.webp"


def _teltonika_is_switch(device: dict[str, Any]) -> bool:
    """True when this Teltonika row is a network SWITCH rather than a router.

    Every Teltonika is DISCOVERED against the one catalog entry that carries a
    DHCP OUI rule (`teltonika_router`), so a TSW switch is registered with
    `role: router` and classifies as the `router` service class for the rest of
    its life, which is why a switch was being handed the router card, full of
    WAN / signal / operator rows it can never fill.

    The vendor coordinator knows better: on its first successful poll
    `_maybe_enrich` writes `config.device_subtype` from the unit's own
    `hw_mobile` flag (no cellular modem == a switch). That is the same source
    the Devices picker already trusts for its ROUTER / SWITCH pill and that
    `sensor._find_catalog_by_subtype` already trusts for the product image, so
    reading it here keeps one answer rather than inventing a second.

    A row whose service class is genuinely `network_switch` is honoured too, so
    this keeps working if the catalog ever gains a switch-side discovery rule.
    """
    if _device_service_class(device) == "network_switch":
        return True
    integration = device.get("integration_type") or device.get("integration") or ""
    if integration != "sensa_teltonika_api":
        return False
    subtype = (
        device.get("device_subtype")
        or (device.get("config") or {}).get("device_subtype")
        or ""
    )
    return str(subtype).strip().lower() == "switch"


def _teltonika_port_specs(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The card's `ports` list from the row's `network_ports`, in port order.

    `_attach_network_entity_prefixes` builds `network_ports` from the port
    index baked into each entity's unique_id, so the faceplate is drawn in
    physical order. A port with no link entity yet (registry not populated on
    the first regen) is dropped rather than rendered as a permanently blank
    tile; the next regen picks it up.
    """
    specs: list[dict[str, Any]] = []
    for port in device.get("network_ports") or []:
        link = port.get("link")
        if not link:
            continue
        spec: dict[str, Any] = {
            "entity": link,
            "name": port.get("label") or f"Port {port.get('index', 0) + 1}",
        }
        if port.get("poe_sensor"):
            spec["poe_entity"] = port["poe_sensor"]
        specs.append(spec)
    return specs


# Port ids a network switch uses for its FIBRE cages rather than RJ45 sockets.
# Read off the device's own naming (a TSW202 reports "port1".."port8" plus
# "sfp1"/"sfp2"), which is the only signal the API gives us. Presentational
# only: the PoE switch entities still exist for every PoE-capable port, so
# correcting this list is a one-line change with nothing to re-register.
_NON_RJ45_PORT_PREFIXES = ("sfp",)


def _is_rj45_port(port: dict[str, Any]) -> bool:
    """True when this port is a physical RJ45 socket, so the RJ45-shaped tile
    is an honest picture of it. An SFP cage takes a fibre/DAC module and
    cannot deliver PoE, so drawing it as an RJ45 would be a lie."""
    label = str(port.get("label") or "").strip().lower()
    return not label.startswith(_NON_RJ45_PORT_PREFIXES)


def _build_switch_ports_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """`custom:sensa-port-tiles` for one network switch on Settings -> Switches.

    One RJ45-socket tile per copper port, sitting alongside the relay
    controllers' tile cards (Richard: "another row ... 8 switches just like the
    relays, but instead of rectangle pills we will use the shape of a RJ45
    plug"). Tapping a tile toggles PoE on that port.

    Returns None when the device has no ports mapped yet (a freshly-added
    switch, before its entities register) so the caller omits the card rather
    than rendering an empty one, and the next regen picks it up.
    """
    ports = [p for p in (device.get("network_ports") or []) if p.get("link")]
    ports = [p for p in ports if _is_rj45_port(p)]
    if not ports:
        return None

    labels = _switch_port_labels(str(device.get("device_id") or ""))
    tiles: list[dict[str, Any]] = []
    for port in ports:
        port_id = str(port.get("label") or "")
        tile: dict[str, Any] = {
            # The customer's name from Settings -> Switches wins over the
            # device's own port id: naming port 5 "Starlink" is the whole
            # point of that field, and a tile still reading "port5" would make
            # it look like it had not saved.
            "name": (
                labels.get(port_id)
                or port_id
                or f"Port {port.get('index', 0) + 1}"
            ),
            "entity": port["link"],
        }
        # Absent on a port with no PoE hardware; the card then renders the
        # tile as read-only status rather than a dead button.
        if port.get("poe_switch"):
            tile["poe_entity"] = port["poe_switch"]
        if port.get("poe_sensor"):
            tile["poe_power_entity"] = port["poe_sensor"]
        tiles.append(tile)

    # No `name`, and `full_width` -- both to match the relay tile cards this
    # sits beside on the Habitation Switches tab (`_relay_controller_tile_cards`
    # dropped its header there in 2026-06-13 and spans the grid). A heading
    # would push the sockets out of line with the relay channels above them,
    # which is the one thing this card has to get right.
    return {
        "type": "custom:sensa-port-tiles",
        "full_width": True,
        "ports": tiles,
    }


def _switch_port_labels(device_id: str) -> dict[str, str]:
    """The customer's per-port names for one switch, or empty.

    Stored on the config entry by `SensaSwitchPortNameText`
    (`entry.options["switch_port_labels"][device_id][port_id]`), read here so
    both the Habitation tile and the Settings row show the same name.
    """
    if not device_id or _dashboard_hass is None:
        return {}
    try:
        from ..const import DOMAIN, SWITCH_PORT_LABELS_OPTION

        for entry in _dashboard_hass.config_entries.async_entries(DOMAIN):
            stored = (entry.options.get(SWITCH_PORT_LABELS_OPTION) or {}).get(
                device_id
            )
            if stored:
                return {str(k): str(v) for k, v in stored.items() if v}
    except Exception:
        _LOGGER.debug("Could not read switch port labels", exc_info=True)
    return {}


def _build_switch_ports_config_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Settings -> Switches CONFIG row for one network switch.

    Deliberately the SAME `custom:sensa-relay-channels` card the relay
    controllers use (Richard: "just like we can with regular relay channels"),
    so the two read as one surface rather than two dialects of the same idea.
    Per port: an editable NAME and a Device Type dropdown.

    Two relay columns are omitted rather than faked: there is no per-port
    ENABLED helper (the card renders that cell only when given one), and no
    picture picker, because a port is identified by its socket, not by an
    image. The card already treats both as optional.

    Returns None when the device has no ports mapped yet, so the caller omits
    the card and the next regen picks it up.
    """
    from ..const import (
        switch_port_assignment_entity_id,
        switch_port_name_entity_id,
    )

    device_id = str(device.get("device_id") or "")
    ports = [p for p in (device.get("network_ports") or []) if p.get("label")]
    ports = [p for p in ports if _is_rj45_port(p)]
    if not device_id or not ports:
        return None

    channels: list[dict[str, Any]] = []
    for port in ports:
        # The LABEL is the storage key. Both sides derive it from the one
        # `teltonika_entities._port_label`, so they cannot drift: the entities
        # are built with it (see `_build_switch_port_config_entities`) and the
        # role-map slot carries the same string.
        port_id = str(port.get("label") or "")
        channels.append({
            "n": port.get("index", 0) + 1,
            "name": switch_port_name_entity_id(device_id, port_id),
            "assignment": switch_port_assignment_entity_id(device_id, port_id),
        })

    card: dict[str, Any] = {
        "type": "custom:sensa-relay-channels",
        "title": device.get("display_name") or device.get("name") or "Network Switch",
        "channels": channels,
        "works_with_options": _switch_port_works_with_options(),
    }
    image = _teltonika_image(device)
    if image:
        card["image"] = f"/local/sensa/images/{image}"
    return card


def _switch_port_works_with_options() -> list[dict[str, Any]]:
    """Grouped value -> label map for the port Device Type dropdown.

    Only Integrated Devices: a network port carries DATA to a device, so the
    relay's Switched-Device service classes ("lighting", "water_pump") would
    be meaningless here and would route a tile that switches nothing.

    Shares `_integrated_device_options` with the relay dropdown rather than
    deriving its own list. A first version walked `_dashboard_devices`
    directly and so INCLUDED relay controllers, which `select.py`'s
    `_integrated_device_choices` excludes -- the two would have offered
    different sets, and a value with no label renders as a raw
    `device:<id>` token.
    """
    options = [{"value": "unassigned", "label": "Not assigned"}]
    options.extend(_integrated_device_options())
    return [{"group": "Device", "options": options}]


def _switch_ports_config_cards() -> list[dict[str, Any]]:
    """One config card per registered network switch, for Settings ->
    Switches. Empty when no switch is registered, which is what gates the
    tab alongside the relay controllers."""
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if not _teltonika_is_switch(device):
            continue
        card = _build_switch_ports_config_card(device)
        if card is not None:
            cards.append(card)
    return cards


def _switch_ports_cards() -> list[dict[str, Any]]:
    """One port-tile card per registered network switch, for Settings ->
    Switches. Empty when no switch is registered (or none has mapped ports
    yet), which is what gates the tab."""
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if not _teltonika_is_switch(device):
            continue
        card = _build_switch_ports_card(device)
        if card is not None:
            cards.append(card)
    return cards


def _network_row(
    entity: str, label: str, icon: str, nerd: bool = False
) -> dict[str, Any]:
    """One `sensa-router-card` status row.

    `nerd` is omitted rather than written as False so an everyday row stays the
    same three keys it has always been, and a card built by an older App keeps
    rendering every row on a newer bundle.
    """
    row: dict[str, Any] = {"entity": entity, "name": label, "icon": icon}
    if nerd:
        row["nerd"] = True
    return row


def _build_habitation_switch_card(device: dict[str, Any]) -> dict[str, Any]:
    """Live-status card for a Teltonika network switch on the Habitation
    Network view.

    Deliberately NOT the router card. A managed switch has no modem, so the
    router's WAN / signal / operator / data-usage rows are entities that exist
    but can never report: the API skips those fetchers outright when
    `hw_mobile` is false. What a switch is actually asked is "what is plugged
    in, and did it come up at full speed", so the port faceplate is the
    substance of this card and the identity rows are the footnote.
    """
    display_name = (
        device.get("display_name")
        or device.get("name")
        or "Network Switch"
    )
    # Identity only. Anything cellular is omitted by construction, not filtered
    # out later, so a switch can never show an "unknown" WAN row.
    role_specs: list[tuple[str, str, str]] = [
        ("model", "Model", "mdi:switch"),
        ("firmware_version", "Firmware", "mdi:information-outline"),
    ]
    rows = [
        {"entity": _network_entity(device, role), "name": label, "icon": icon}
        for role, label, icon in role_specs
        if _network_entity(device, role)
    ]
    card: dict[str, Any] = {
        "type": "custom:sensa-router-card",
        "name": display_name,
        "image": f"/local/sensa/images/{_teltonika_image(device)}",
        "rows": rows,
    }
    ports = _teltonika_port_specs(device)
    if ports:
        card["ports"] = ports
    if device.get("power_relay"):
        card["power_relay"] = device["power_relay"]
    # Nerd Mode only. A managed switch is infrastructure: it has no setting a
    # customer changes and nothing on it fails in a way they could act on, so
    # the whole card is hidden until the header's glasses toggle is on
    # (`sensa-card-grid` drops a cell flagged `nerd`).
    card["nerd"] = True
    return card


def _starlink_image(device: dict[str, Any]) -> str:
    """Return the model-specific Starlink dish image filename, or the
    generic fallback."""
    model = _network_device_model(device)
    if "gen3" in model or "gen 3" in model:
        return "starlink-gen3.webp"
    if "mini" in model:
        return "starlink-mini.webp"
    return "starlink-generic.webp"


# Shared image sizing for ALL Sensa product-image headers (relay,
# router, Starlink). HTML `height` attribute is used (not CSS
# `max-height`) because HA's markdown sanitizer strips the `style`
# attribute on `<img>` tags — sizing via inline CSS silently fails.
# The `height` attribute is in HA's sanitizer allowlist; the browser
# auto-scales width to preserve aspect ratio, giving us a hard
# 50px-tall image regardless of the source asset's natural size.
# Single source of truth so a future tweak is one constant change.
_NETWORK_IMG_HEIGHT = 50
_NETWORK_IMG_STYLE = "float: left; margin: 0 12px 8px 0;"


def _build_habitation_router_card(device: dict[str, Any]) -> dict[str, Any]:
    """Live-status card for a Teltonika router on the Habitation Network
    view. Read-only monitoring only. Router configuration (credentials, signal
    threshold) is on the Settings → Devices row (the standalone Settings Network
    tab was removed 2026-07 — its config duplicated the Devices row)."""
    display_name = (
        device.get("display_name")
        or device.get("name")
        or "Router"
    )
    # WAN Source sits high in the row order — when failover happens
    # this is the first thing the customer wants to see (which WAN is
    # actually carrying traffic right now).
    # The fourth element is Nerd Mode: True rows are hidden until the header's
    # glasses toggle is on (the card filters them, see
    # webcards/src/sensa-router-card.ts). Everyday reading is "is the internet
    # up"; which WAN carries it is on the Internet Priority card, who is on
    # which network is on the Networks card (Connected Clients was removed
    # 2026-09-28), and radio internals and byte counters answer a question
    # only an enthusiast asks (WAN Source moved behind it the same day).
    role_specs: list[tuple[str, str, str, bool]] = [
        ("wan_status", "WAN Status", "mdi:web", False),
        ("wan_source", "WAN Source", "mdi:wan", True),
        ("connection_type", "Connection Type", "mdi:signal-variant", True),
        ("operator", "Operator", "mdi:antenna", True),
        ("signal_strength_rssi", "Signal Strength", "mdi:signal-cellular-3", True),
        ("signal_quality_rsrp", "Signal Quality", "mdi:signal-cellular-2", True),
        ("data_usage_tx", "Data TX", "mdi:upload", True),
        ("data_usage_rx", "Data RX", "mdi:download", True),
    ]
    rows = [
        _network_row(_network_entity(device, role), label, icon, nerd)
        for role, label, icon, nerd in role_specs
        if _network_entity(device, role)
    ]
    card: dict[str, Any] = {
        "type": "custom:sensa-router-card",
        "name": display_name,
        "image": f"/local/sensa/images/{_teltonika_image(device)}",
        "rows": rows,
    }
    # Restart, a two-press button at the foot of the card. Everyday, not
    # Nerd Mode: restarting the router is the first thing anyone tries.
    restart = _network_entity(device, "restart_router")
    if restart:
        card["restart"] = restart
        card["restart_label"] = "Restart"
    # Power on/off control — present only when a relay channel has been
    # assigned to this device as an Integrated Device (`power_relay` attached
    # by `_attach_power_relays`).
    if device.get("power_relay"):
        card["power_relay"] = device["power_relay"]
    return card


def _build_habitation_vlan_cards(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The Network tab's lead cards, from ONE config so they read the same data:
    the everyday Network card (`simple`: online or not, over which WAN and
    how good, the backups, how many devices), which fills the tab outside
    Nerd Mode, and the Networks diagram (WANs, router, VLANs) in Nerd Mode."""
    card = _build_habitation_vlan_card(device)
    if card is None:
        return []
    simple = {
        k: v for k, v in card.items() if k not in ("name", "full_width")
    }
    # Sized to its content, not stretched to the tab (Richard, 2026-09-29).
    simple.update({"name": "Network", "simple": True, "everyday": True})
    # The failover list the customer drags, hosted in the simple card, with
    # each WAN's grade and detail popup (the dish's sleep window among them).
    wan_card = _build_habitation_wan_card(device)
    if wan_card is not None:
        simple["wan_entity"] = wan_card["entity"]
        simple["device_id"] = wan_card["device_id"]
        if wan_card.get("details"):
            simple["wan_details"] = wan_card["details"]
    # The boxes down the left: the dish above the router, each with its
    # status, Restart and (on a relay channel) Power.
    dish = next(
        (d for d in _dashboard_devices if _device_service_class(d) == "satellite_internet"),
        None,
    )
    boxes = []
    if dish is not None:
        boxes.append(_network_device_box(
            dish, "Starlink", f"/local/sensa/images/{_starlink_image(dish)}",
            "status", "restart_dish", "Restart",
        ))
    boxes.append(_network_device_box(
        device, "Router", f"/local/sensa/images/{_teltonika_image(device)}",
        "wan_status", "restart_router", "Restart",
    ))
    simple["devices"] = boxes
    detailed = dict(card)
    detailed["nerd"] = True
    # The diagram does not read on a phone; there the device cards carry it.
    detailed["tablet_only"] = True
    # Alone on a tablet in Nerd Mode, so it fills the tab's height.
    detailed["fill_alone"] = True
    return [simple, detailed]


def _build_habitation_esim_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """The router's eSIM card: plan in use, data used and left, buying more.

    Only for a router that has an eSIM: the `eSIM` sensor is created only when
    the router answered the eSIM API (`TeltonikaEsim`), so a router without
    one has no entity, and the Network tab no card (Richard, 2026-11-01: a
    router without eSIM must not spend space on it).
    """
    entity = _network_entity(device, "esim")
    if not entity:
        return None
    return {
        "type": "custom:sensa-esim-card",
        "entity": entity,
        "device_id": device.get("device_id") or "",
    }


def _network_device_box(
    device: dict[str, Any], fallback: str, image: str,
    status_role: str, restart_role: str, restart_label: str,
) -> dict[str, Any]:
    """One device box on the everyday Network card: name, product shot, a
    status reading, Restart and relay Power, each left out when absent."""
    box: dict[str, Any] = {
        "name": device.get("display_name") or device.get("name") or fallback,
        "image": image,
    }
    if _network_entity(device, status_role):
        box["status"] = _network_entity(device, status_role)
    if _network_entity(device, restart_role):
        box["restart"] = _network_entity(device, restart_role)
        box["restart_label"] = restart_label
    if device.get("power_relay"):
        box["power_relay"] = device["power_relay"]
    return box


def _build_habitation_vlan_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """The van's networks (Secure / IoT / CCTV / Guest, with Starlink as the
    upstream) and the clients on each, as one full-width diagram across the
    top of the Network tab (`custom:sensa-vlan-card`). None until the router
    has its Networks sensor; the card reads the rows live."""
    entity = _network_entity(device, "networks")
    if not entity:
        return None
    card: dict[str, Any] = {
        "type": "custom:sensa-vlan-card",
        "name": "Network Summary",
        "entity": entity,
        # The same product shots the router and dish cards carry.
        "router_image": f"/local/sensa/images/{_teltonika_image(device)}",
        "full_width": True,
    }
    dish = next(
        (
            d for d in _dashboard_devices
            if _device_service_class(d) == "satellite_internet"
        ),
        None,
    )
    card["starlink_image"] = f"/local/sensa/images/{_starlink_image(dish or {})}"
    # Relay channels assigned to the router / dish as Integrated Devices: the
    # round Power button top right of each node (`_attach_power_relays`).
    if device.get("power_relay"):
        card["router_relay"] = device["power_relay"]
    if _network_entity(device, "restart_router"):
        card["router_restart"] = _network_entity(device, "restart_router")
    # Lets the diagram's WAN tiles be dragged into failover order.
    if device.get("device_id"):
        card["device_id"] = str(device["device_id"])
    if dish is not None and dish.get("power_relay"):
        card["starlink_relay"] = dish["power_relay"]
    if dish is not None and _network_entity(dish, "restart_dish"):
        card["starlink_restart"] = _network_entity(dish, "restart_dish")
    # The dish's sleep window, for the Sleep popup on its box.
    if dish is not None:
        sleep = {
            key: _network_entity(dish, role)
            for key, role in _STARLINK_SLEEP_ROLES.items()
            if _network_entity(dish, role)
        }
        if sleep:
            card["starlink_sleep"] = sleep
    # The dish's power draw, under its state on the Starlink tile (Nerd Mode).
    if dish is not None and _network_entity(dish, "power"):
        card["starlink_power"] = _network_entity(dish, "power")
    # Firmware versions, a line under each node's address.
    if _network_entity(device, "firmware_version"):
        card["router_firmware"] = _network_entity(device, "firmware_version")
    if dish is not None and _network_entity(dish, "software_version"):
        card["starlink_firmware"] = _network_entity(dish, "software_version")
    # How each WAN is graded Good / Fair / Poor: the dish by its own link
    # figures, a SIM by the router's modem signal.
    quality: dict[str, Any] = {}
    if dish is not None and _starlink_quality_metrics(dish):
        quality["starlink"] = _starlink_quality_metrics(dish)
    if _mobile_quality_metrics(device):
        quality["mobile"] = _mobile_quality_metrics(device)
    if quality:
        card["quality"] = quality
    return card


def _build_habitation_wan_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """The router's enabled WANs in failover order, dragged to reorder
    (`custom:sensa-wan-priority-card`). None until the router has its WAN Priority
    sensor; the card reads the list live, so a WAN enabled on the router
    appears without a dashboard rebuild."""
    entity = _network_entity(device, "wan_priority")
    device_id = str(device.get("device_id") or "")
    if not entity or not device_id:
        return None
    card: dict[str, Any] = {
        "type": "custom:sensa-wan-priority-card",
        "name": "Internet Priority",
        "entity": entity,
        "device_id": device_id,
    }
    details = _wan_details(device)
    if details:
        card["details"] = details
    return card


# Starlink rows a WAN popup shows whatever Nerd Mode says: the readings a
# customer compares when picking which source to put first.
_STARLINK_POPUP_ROWS = frozenset({
    "Latency", "Download", "Upload", "Speed Limit", "Obstruction", "Packet Drop Rate",
})


def _wan_details(router: dict[str, Any]) -> dict[str, Any]:
    """Per WAN kind, what `sensa-wan-priority-card` shows on the row and in its popup.

    Each kind carries `quality` (the Good / Fair / Poor bands), `summary`
    (entities joined into the row's detail line) and `card` (a bare
    `sensa-router-card` config: the popup's readings and controls). Starlink
    adds `sleeping`, so the row reads Asleep rather than Down. A kind with
    nothing registered is left out.
    """
    details: dict[str, Any] = {}
    mobile_quality = _mobile_quality_metrics(router)
    mobile_rows = [
        _network_row(_network_entity(router, role), label, icon, False)
        for role, label, icon in (
            ("operator", "Operator", "mdi:antenna"),
            ("connection_type", "Network", "mdi:signal-variant"),
            ("signal_strength_rssi", "Signal Strength", "mdi:signal-cellular-3"),
            ("signal_quality_rsrp", "Signal Quality", "mdi:signal-cellular-2"),
        )
        if _network_entity(router, role)
    ]
    if mobile_quality:
        mobile_rows.insert(0, {"name": "Quality", "icon": "mdi:signal", "quality": mobile_quality})
    if mobile_rows:
        details["mobile"] = {
            "quality": mobile_quality,
            "summary": [
                e for e in (
                    _network_entity(router, "operator"),
                    _network_entity(router, "connection_type"),
                ) if e
            ],
            "card": {"type": "custom:sensa-router-card", "bare": True, "rows": mobile_rows},
        }
    dish = next(
        (d for d in _dashboard_devices if _device_service_class(d) == "satellite_internet"),
        None,
    )
    if dish is not None:
        status = _build_habitation_starlink_card(dish)
        rows = [
            {k: v for k, v in r.items() if k != "nerd"}
            if r.get("name") in _STARLINK_POPUP_ROWS else r
            for r in status.get("rows", [])
        ]
        card = {
            k: v for k, v in status.items() if k not in ("name", "image", "rows")
        }
        card.update({"bare": True, "rows": rows})
        starlink: dict[str, Any] = {
            "quality": _starlink_quality_metrics(dish),
            "summary": [],
            "card": card,
        }
        if _network_entity(dish, "sleeping"):
            starlink["sleeping"] = _network_entity(dish, "sleeping")
        details["starlink"] = starlink
    return details


def _build_habitation_starlink_card(device: dict[str, Any]) -> dict[str, Any]:
    """Live-status card for a Starlink dish on the Habitation Network
    view. Mirrors the router card layout."""
    display_name = (
        device.get("display_name")
        or device.get("name")
        or "Starlink"
    )
    # The fourth element is Nerd Mode: True rows are hidden until the header's
    # glasses toggle is on. The everyday reading is the power button (always
    # shown, it is a control), Status and Service: "is the dish working, and is
    # it allowed to work". Latency joined the Nerd rows 2026-09-28.
    role_specs: list[tuple[str, str, str, bool]] = [
        ("status", "Status", "mdi:satellite-variant", False),
        ("connected", "Connected", "mdi:check-network", True),
        # Why the dish is not serving, when it is not. "Service Status"
        # carries the disablement code (no active account, roaming
        # restricted, blocked area, moving too fast), which is the only
        # reading that distinguishes a hardware problem from a billing or
        # policy one -- and every one of those codes used to render as
        # "Code 10".
        ("service_status", "Service", "mdi:shield-check", False),
        # The dish asleep on its own schedule, as opposed to broken. Without
        # this row a customer who set a sleep window has no way to tell the
        # two apart.
        ("sleeping", "Sleeping", "mdi:power-sleep", True),
        ("uptime", "Uptime", "mdi:timer-outline", True),
        ("power", "Power", "mdi:flash", True),
        # Battery-equipped terminals only (the entity is not created
        # otherwise), so this row simply does not exist on a mains dish.
        ("battery", "Battery", "mdi:battery", True),
        ("latency", "Latency", "mdi:timer-sand", True),
        ("download", "Download", "mdi:download", True),
        ("upload", "Upload", "mdi:upload", True),
        # On a metered Roam plan this is the difference between "the
        # internet is slow here" and "you have used your priority data".
        ("download_limit", "Speed Limit", "mdi:speedometer-slow", True),
        ("obstruction", "Obstruction", "mdi:tree", True),
        ("packet_drop_rate", "Packet Drop Rate", "mdi:package-variant-remove", True),
        # When the dish reboots nightly to install firmware, so a short drop
        # at that hour reads as expected. Set in the Starlink app only.
        ("update_hour", "Update Hour", "mdi:clock-outline", False),
    ]
    rows = [
        _network_row(_network_entity(device, role), label, icon, nerd)
        for role, label, icon, nerd in role_specs
        if _network_entity(device, role)
    ]
    # Quality: one red / amber / green bar under Status, the worst of the
    # dish's latency, packet drop and obstructed-now against the same bands the
    # Cab signals card uses. Everyday: it is the plain answer to "is it any
    # good", now the raw figures sit behind Nerd Mode.
    quality = _starlink_quality_metrics(device)
    if quality:
        at = next(
            (i + 1 for i, r in enumerate(rows) if r["name"] == "Status"), 0,
        )
        rows.insert(at, {"name": "Quality", "icon": "mdi:signal", "quality": quality})
    card: dict[str, Any] = {
        "type": "custom:sensa-router-card",
        "name": display_name,
        "image": f"/local/sensa/images/{_starlink_image(device)}",
        "rows": rows,
    }
    if device.get("power_relay"):
        card["power_relay"] = device["power_relay"]
    # The sleep window lives on the dish's own card: it is the setting that
    # explains the Status row reading Sleeping, and it is the one a customer
    # changes. Each part is omitted when its entity is not registered.
    sleep = {
        key: _network_entity(device, role)
        for key, role in _STARLINK_SLEEP_ROLES.items()
        if _network_entity(device, role)
    }
    if sleep:
        card["sleep"] = sleep
    # Restart sits at the foot of the dish's own card, the same shared button
    # the router card carries (moved off the settings card 2026-09-28).
    restart = _network_entity(device, "restart_dish")
    if restart:
        card["restart"] = restart
        card["restart_label"] = "Restart"
    return card


# The Starlink sleep window, `sensa-router-card`'s `sleep` block: config key
# -> role slug (`slugify` of the entity NAME, like the control specs below).
_STARLINK_SLEEP_ROLES: dict[str, str] = {
    "switch": "sleep_schedule",
    "start": "sleep_start",
    "duration": "sleep_duration",
}


# Starlink dish CONTROLS, in the order a customer meets them. Each is
# `(role_slug, label, control_kind)`.
#
# The role slug is `slugify(the entity's NAME)`, because that is what
# `_attach_network_entity_prefixes` keys the map on -- NOT the entity's
# unique_id key, which is a different string for some controls (`stow_control`
# is named "Stow Dish" and so slugs to `stow_dish`). Getting that wrong yields
# no error, just a row that silently never renders, so
# `test_starlink_dashboard_cards.py::test_every_control_spec_slug_matches_a_real_entity_name`
# derives these slugs from the entities themselves and fails on any drift.
#
# Rows are omitted when the entity is not registered, which is how the
# hardware differences take care of themselves: a Mini registers no Stow, so
# that row does not exist on it.
#
# Snow melt, levelling, the update deferral and the reboot hour were dropped
# on 2026-08-10: the firmware refuses the `dish_set_config` RPC they were
# written with, so each was a row that could only fail.
#
# The sleep window moved onto the dish's status card on 2026-09-28
# (`_STARLINK_SLEEP_ROLES`), so this card is Stow and Restart.
_STARLINK_CONTROL_SPECS: list[tuple[str, str, str]] = [
    ("stow_dish", "Stow dish", "switch"),
]


def _build_starlink_controls_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Settings card for a Starlink dish, or None when it has no controls.

    These are the writable half of the dish (stow). They sit
    on the **Habitation -> Network** tab beside
    the dish's own status card rather than on Settings, because the
    standalone Settings Network tab was deliberately removed in July 2026
    for duplicating the Devices row -- so this is where a customer already
    goes to look at the dish.
    """
    rows = [
        {"entity": _network_entity(device, role), "name": label, "control": kind}
        for role, label, kind in _STARLINK_CONTROL_SPECS
        if _network_entity(device, role)
    ]
    # Stow is all that is left here; a Mini has none, so it gets no card.
    if not rows:
        return None

    display_name = (
        device.get("display_name") or device.get("name") or "Starlink"
    )
    card: dict[str, Any] = {
        "type": "custom:sensa-config-card",
        "title": f"{display_name} settings",
        "fields": rows,
    }

    return card


def _build_habitation_starlink_cards(device: dict[str, Any]) -> list[dict[str, Any]]:
    """Every card a Starlink dish contributes to the Network tab.

    ONE helper for both call sites (the Network tab and the combined
    overview). They used to each call the status-card builder directly, so
    adding a second card would have meant remembering both -- and forgetting
    one is how a card ends up on some layouts and not others.
    """
    cards = [_build_habitation_starlink_card(device)]
    controls = _build_starlink_controls_card(device)
    if controls is not None:
        cards.append(controls)
    return cards


def _starlink_quality_metrics(device: dict[str, Any]) -> list[dict[str, Any]]:
    """A dish's link-quality readings with their green / amber / red bands.

    ONE definition for both surfaces that grade the dish: the Cab signals
    card draws a bar per metric, and the dish's own card on the Habitation
    Network tab folds them into a single Quality row (the worst one wins).
    A metric whose entity is not registered is left out.

    Graded on what the link is doing NOW: latency, packet drop and whether the
    dish is obstructed at this moment (`poor_when`, a two-state reading). The
    obstruction percentage is not graded: it is the dish's long-run figure
    and read Poor on .207 while the link was clear (19.7 ms, 0% drop), so it
    is shown as information only (`_starlink_obstruction_info`)."""
    specs = (
        ("latency", "Latency", "ms", 300, 50, 150),
        ("packet_drop_rate", "Packet drop", "%", 10, 2, 5),
    )
    metrics: list[dict[str, Any]] = [
        {
            "label": label, "entity": _network_entity(device, role), "unit": unit,
            "min": 0, "max": top, "good": good, "fair": fair,
            "lower_is_better": True,
        }
        for role, label, unit, top, good, fair in specs
        if _network_entity(device, role)
    ]
    obstructed = _network_entity(device, "obstructed")
    if obstructed:
        metrics.append({
            "label": "Obstructed now", "entity": obstructed, "poor_when": "on",
        })
    return metrics


def _starlink_obstruction_info(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The dish's obstruction percentage as a plain reading, never a grade."""
    entity = _network_entity(device, "obstruction")
    return [{"label": "Obstruction", "entity": entity, "unit": "%"}] if entity else []


def _mobile_quality_metrics(device: dict[str, Any]) -> list[dict[str, Any]]:
    """A router's cellular signal readings with their good / fair bands.

    ONE definition for both surfaces that grade the SIM: the Cab signals card
    draws a bar per reading, and the Networks card grades the SIM that is in
    use (a single-modem router reads only the active SIM, so an idle one has
    no reading at all). Unregistered readings are left out."""
    specs = (
        ("signal_strength_rssi", "Signal (RSSI)", -110, -50, -70, -85),
        ("signal_quality_rsrp", "Quality (RSRP)", -120, -80, -90, -105),
    )
    return [
        {
            "label": label, "entity": _network_entity(device, role), "unit": "dBm",
            "min": low, "max": high, "good": good, "fair": fair,
            "lower_is_better": False,
        }
        for role, label, low, high, good, fair in specs
        if _network_entity(device, role)
    ]


def _build_network_signals_card() -> dict[str, Any]:
    """Internet signal-quality card for the **Cab → Network** tab.

    Answers "is the internet any good here, and what's the real capability?" —
    the question you ask while hunting for a parkup. Sections (each shown only
    when its source is present):

      • **Internet** — the universal ping + online/offline state, from the
        add-on's reachability probe (always present).
      • **Mobile (LTE)** — a registered router's RSSI / RSRP signal bars plus
        connection type + operator.
      • **Starlink** — a registered dish's latency / obstruction / packet-drop
        bars (throughput is deliberately NOT shown — it reflects current usage,
        not link capability).
      • **Speed test** — the last on-demand real download/upload figures + a Run
        button (a test burns mobile data, so it's never automatic).

    Per-metric thresholds are passed through so the card's quality bars stay
    brand-agnostic (good/fair/poor green/amber/red). `lower_is_better` flips the
    quality direction (ping/obstruction/drop = low good; RSSI/RSRP = high good).
    """
    sections: list[dict[str, Any]] = []

    # Internet — always present; ping comes from the add-on, not a device.
    sections.append({
        "title": "Internet",
        "status_entity": "binary_sensor.sensa_nomad_internet_up",
        "metrics": [{
            "label": "Ping", "entity": "sensor.sensa_nomad_internet_ping",
            "unit": "ms", "min": 0, "max": 300,
            "good": 50, "fair": 150, "lower_is_better": True,
        }],
    })

    # Mobile (LTE): the first registered router with signal entities. A
    # Teltonika SWITCH classifies as `router` (see `_teltonika_is_switch`) and
    # registers the signal SENSORS even though it has no modem to fill them, so
    # it must be skipped explicitly or a switch-only van gets a dead LTE
    # section reading "--" on every bar.
    for device in _dashboard_devices:
        if _device_service_class(device) != "router":
            continue
        if _teltonika_is_switch(device):
            continue
        metrics = _mobile_quality_metrics(device)
        info: list[dict[str, Any]] = []
        ct = _network_entity(device, "connection_type")
        op = _network_entity(device, "operator")
        if ct:
            info.append({"label": "Type", "entity": ct})
        if op:
            info.append({"label": "Operator", "entity": op})
        if metrics or info:
            sections.append({
                "title": device.get("display_name") or device.get("name") or "Mobile",
                "metrics": metrics,
                "info": info,
            })
        break

    # Starlink — the first registered dish.
    for device in _dashboard_devices:
        if _device_service_class(device) != "satellite_internet":
            continue
        sl_metrics = _starlink_quality_metrics(device)
        if sl_metrics:
            section: dict[str, Any] = {
                "title": device.get("display_name") or device.get("name") or "Starlink",
                "metrics": sl_metrics,
            }
            sl_info = _starlink_obstruction_info(device)
            if sl_info:
                section["info"] = sl_info
            sections.append(section)
        break

    return {
        "type": "custom:sensa-network-signals",
        "full_width": True,
        "sections": sections,
        "speedtest": {
            "download_entity": "sensor.sensa_nomad_internet_download",
            "upload_entity": "sensor.sensa_nomad_internet_upload",
            "ping_entity": "sensor.sensa_nomad_internet_ping",
            "run_service": "sensa_nomad.run_speedtest",
        },
    }


def _habitation_chips() -> list[dict[str, Any]]:
    """Base chip list shown in the centre of the Sensa header bar on
    every Habitation view.

    Always carries the current-mode chip, then one chip per device opted in
    to "Show Temperature In Header" on its Settings -> Devices row (see
    `_temperature_chips`), then the power chips. Each entry is consumed
    by `custom:sensa-header-bar` as `{entity, label, icon}`.

    (The WAN Source chip that used to ride here was removed 2026-06-27;
    the active uplink is still shown on the Network tab's card.)

    Per-view extras are layered on by passing `extra_chips=` to
    `_build_header_card`, so the base list stays a single source of
    truth and each view only declares what's specific to it.
    """
    chips: list[dict[str, Any]] = [
        {
            "entity": "sensor.sensa_nomad_current_mode",
            "icon": "mdi:tune-vertical-variant",
        },
    ]
    chips.extend(_temperature_chips())
    chips.extend(_header_power_chips())
    return chips


def _header_power_chips() -> list[dict[str, Any]]:
    """Header chips for every device opted in to "Show Power In Header".

    Opt-in per device on the Settings -> Devices row (a `switch.*` backed by
    `entry.options["header_power_chips"]`), the same shape as the temperature
    chips above. Off by default: a header that filled itself
    with every power reading a van can produce would be noise, and the Starlink
    dish -- the one device offered this today -- is the load a customer running
    off battery actually wants in their eyeline.

    A chip is emitted only when the device's `power` role resolves to a
    registered entity, so a dish whose vendor coordinator has not come up yet
    leaves the header alone rather than rendering a broken reference (the same
    rule `_build_habitation_starlink_card` applies to its rows).
    """
    from ..const import (
        HEADER_POWER_CHIP_ICON,
        header_power_chip_enabled,
        header_short_name,
    )

    options = _sensa_entry_options()
    if not options:
        return []

    chips: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        device_id = str(device.get("device_id") or device.get("id") or "")
        if not device_id or not header_power_chip_enabled(options, device_id):
            continue
        entity_id = _network_entity(device, "power")
        if not entity_id:
            continue
        chips.append({
            "entity": entity_id,
            # The customer's Header Short Name when set, else the device name.
            "label": header_short_name(options, device_id) or str(
                device.get("display_name") or device.get("name") or "Power"
            ),
            "icon": HEADER_POWER_CHIP_ICON,
        })
    return chips


def _sensa_entry_options() -> dict[str, Any]:
    """This integration's config-entry options, or empty.

    Empty in tests / dry-run YAML builds where no `hass` is stashed, which is
    the safe reading for every opt-in ledger: nothing is opted in.
    """
    if _dashboard_hass is None:
        return {}
    try:
        from ..const import DOMAIN

        for entry in _dashboard_hass.config_entries.async_entries(DOMAIN):
            if entry.options:
                return dict(entry.options)
    except Exception:
        _LOGGER.debug("Could not read Sensa entry options", exc_info=True)
    return {}


def _build_network_view_card(
    router_cards: list[dict[str, Any]],
    starlink_cards: list[dict[str, Any]],
    switch_cards: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Custom shell that owns the whole Network view.

    The shell embeds `<sensa-header>` for the header strip and lays out
    the router + Starlink cards in a CSS grid (auto-fit, container
    queries) — HA's masonry no longer chooses the layout, so variable
    card counts always look intentional across screen sizes. Card
    configs are passed through as-is and hydrated client-side via HA's
    `loadCardHelpers()`, so existing `_build_habitation_router_card`
    and `_build_habitation_starlink_card` output keeps working unchanged.

    Network switches ride the grid's generic `cards` slot, which the shell
    renders after `routers` and `dishes`: the internet comes first, the LAN
    distribution after it.

    See webcards/src/sensa-card-grid.ts.
    """
    card: dict[str, Any] = {
        "type": "custom:sensa-card-grid",
        "logo": _dealer_logo_url() or _SENSA_DEFAULT_LOGO,
        "chips": _habitation_chips(),
        "routers": router_cards,
        "dishes": starlink_cards,
    }
    if switch_cards:
        card["cards"] = switch_cards
    return card


_SENSA_DEFAULT_LOGO = "/local/sensa/images/sensa-transparent.png"


def _dealer_logo_url() -> str | None:
    """The active dealer's cached logo path for the header, or None.

    A dealer-branded licence's logo is downloaded + cached by the App
    while online and surfaced on the licence status as ``dealer_logo`` (a
    ``/local/sensa/...`` path served locally, so it works offline). Gated on an
    active dealer claim via ``current_dealer`` — which already returns None for
    trials — so trials and normal licences keep the Sensa logo. Falls back to
    the Sensa logo whenever nothing is cached yet (e.g. before first online
    contact, or a dealer with no uploaded logo)."""
    if _dashboard_hass is None:
        return None
    from ..home_catalog import current_dealer

    if current_dealer(_dashboard_hass) is None:
        return None
    from ..const import DOMAIN, RUNTIME_LICENCE_STATUS

    data = getattr(_dashboard_hass, "data", None) or {}
    for runtime in (data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        status = runtime.get(RUNTIME_LICENCE_STATUS) or {}
        logo = status.get("dealer_logo")
        if logo:
            return logo
    return None


def _build_header_card(
    extra_chips: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sensa header-bar card config: logo + chips + live clock.

    Logo file ships with the webcards bundle (`webcards/assets/images/
    sensa-logo.png`); the Sensa App add-on mirrors it to
    `/config/www/sensa/images/` so HA serves it at the `/local` URL
    referenced here. Base chips come from `_habitation_chips`; per-view
    extras (e.g. SOC % on the BMS view, WAN signal on Network) append
    after them so the view-shared chips stay leftmost — keeps the
    layout stable across views and lets the eye land on the same
    landmark first."""
    chips = _habitation_chips()
    if extra_chips:
        chips.extend(extra_chips)
    return {
        "type": "custom:sensa-header-bar",
        "logo": _dealer_logo_url() or _SENSA_DEFAULT_LOGO,
        "chips": chips,
    }


def _build_modes_strip(single_row: bool = False) -> dict[str, Any]:
    """Mode tiles at the top of the Habitation Overview, as the native
    `custom:sensa-scene-bar` with names shown.

    Was a grid of HA `button` cards baked from the preset catalog. The
    native card renders from `sensor.sensa_nomad_current_mode`'s `modes`
    attribute at runtime, so the customer's order, hidden presets and their
    own scenes (Intelligence → Scenes) show here without a dashboard
    rebuild; the baked `modes` list below is only the fallback for an
    integration that doesn't publish the attribute.

    `single_row=True` lays every tile on one slim row; the default wraps
    them 4-up.
    """
    card = _home_modes_bar(single_row=single_row)
    card["show_name"] = True
    return card


def _home_center_power_flow() -> dict[str, Any] | None:
    """Compact power-flow card for a Home layout's centre slot.

    Returns the role-resolved basic `custom:sensa-power-flow-card` (the compact
    MDI-icon variant — Solar / Shore / Battery / AC Load / DC Load), or **None
    when no electrical telemetry resolves** — the power flow is conditional on
    at least one electrical item the diagram can show, so on an install with no
    such device the centre slot is simply omitted, not filled with a placeholder
    (Richard, 2026-05-30: "if there are no electrical items used by the power
    flow then it shouldn't be shown"; see [[conditional-cards-no-placeholder]]).
    Callers must drop the slot when this is None — the `sensa-view-home` shell
    treats an absent `center` as an empty cell (`.cell:empty { display:none }`),
    which is fine; only a literal `card: null` would read as a config error.

    Reuses `_build_power_flow_card`'s shared slot resolution and just swaps the
    type to the basic card; the basic card draws shore power (the `grid` slot)
    as its own node, folds the shore-conditional AC load into AC Load, and
    ignores the detail-only slots/images it's handed.
    """
    power_flow = _build_power_flow_card()
    if power_flow is None:
        return None
    power_flow["type"] = "custom:sensa-power-flow-card"
    return power_flow


def _home_solar_graph() -> dict[str, Any] | None:
    """24h solar-power history graph for the Home centre column.

    Shown beneath the power-flow hero (the `center_below` slot of
    `sensa-view-home`) **only when a solar charger is present** — it resolves
    the same vendor-neutral `solar_power` role the power-flow card's solar slot
    uses, so it tracks whatever MPPT(s) are registered: one charger → a single
    bar per hour, multiple MPPTs (a van with two) → stacked, one segment each. Returns None when no
    solar resolves, so the slot is simply omitted (no empty graph).
    """
    solar = resolve_role("solar_power")
    if not solar:
        return None
    entities = solar if isinstance(solar, list) else [solar]
    # A native hourly BAR chart (not built-in `statistics-graph`), so it
    # direct-creates on a cold panel load without HA's Lovelace engine. Bars
    # are static and only redraw on the periodic refresh — no live, wandering
    # line. Multiple MPPTs are stacked, one coloured segment each, by the card.
    return {
        "type": "custom:sensa-bar-chart",
        "title": "Solar — last 24 h",
        "hours": 24,
        "entities": entities,
    }


def _role_list(role: str) -> list[str]:
    """`resolve_role` as a list: [] when unresolved, [eid] for a pick role."""
    found = resolve_role(role)
    if not found:
        return []
    return [found] if isinstance(found, str) else [e for e in found if e]


def _solar_parts() -> list[dict[str, str]]:
    """One {entity, name} per MPPT whose PV power is live, for the History
    tab's stacked solar bars. Empty unless there are at least two: one MPPT
    (or only the Cerbo's pre-summed total) has nothing to split."""
    parts: list[dict[str, str]] = []
    for d in _dashboard_devices:
        if _device_service_class(d) != "solar_charger":
            continue
        eid = _device_role_map(d).get("pv_power")
        if eid and _entity_is_live(eid):
            parts.append({
                "entity": eid,
                "name": d.get("display_name") or d.get("name") or "Solar",
            })
    return parts if len(parts) > 1 else []


# History tab energy series: (key, label, up roles, down roles, up label,
# down label). A role maps to its counter in energy_counters.COUNTERS; a series
# shows only when at least one of its roles resolves.
# Order matters: the card opens on the FIRST series it is given, so the one
# that leads has to be the one every van has something to show for. Solar led
# until 2026-09-23 and a customer read the card as broken five times running,
# because his MPPTs were fitted (so the series resolved and rendered) but had
# produced nothing, while Shore and Consumption had real kWh sitting one tap
# away. Consumption is the safe lead: a van that is switched on consumes.
_ENERGY_SERIES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], str, str], ...] = (
    ("consumption", "Consumption", ("ac_load", "dc_load"), (), "", ""),
    ("battery", "Battery", ("battery_charged",), ("battery_discharged",),
     "Charged", "Discharged"),
    ("solar", "Solar", ("solar",), (), "", ""),
    ("shore", "Shore", ("shore",), (), "", ""),
    ("alternator", "Alternator", ("alternator",), (), "", ""),
)

# History tab graph presets: (key, label, role), in picker order.
_GRAPH_SERIES: tuple[tuple[str, str, str], ...] = (
    ("battery_soc", "Battery %", "battery_soc"),
    ("battery_voltage", "Battery V", "battery_voltage"),
    ("battery_power", "Battery W", "battery_power"),
    ("solar", "Solar", "solar_power"),
    ("ac_load", "AC load", "ac_load"),
    ("dc_load", "DC load", "dc_load"),
    ("shore", "Shore", "grid_power"),
    ("alternator", "Alternator", "alternator"),
)


def _history_cards() -> list[dict[str, Any]]:
    """The History tab: VRM-style energy, graphs and battery health.

    Every card reads HA long-term statistics, so it reaches back a year while
    raw history is purged after 7 days. Everything keys off `resolve_role`, so
    it works for any vendor; a series, or a whole card, is left out when its
    role does not resolve. An empty list means no History tab.
    """
    from ..energy_counters import COUNTERS, counter_entity_id

    counter_role = {spec.key: spec.role for spec in COUNTERS}
    resolves = {role: bool(_role_list(role)) for role in set(counter_role.values())}

    def counters(keys: tuple[str, ...]) -> list[str]:
        return [counter_entity_id(k) for k in keys if resolves.get(counter_role[k])]

    energy_series: list[dict[str, Any]] = []
    for key, label, up_keys, down_keys, up_label, down_label in _ENERGY_SERIES:
        up = counters(up_keys)
        if not up:
            continue
        series: dict[str, Any] = {"key": key, "label": label, "up": up}
        down = counters(down_keys)
        if down:
            series.update(down=down, up_label=up_label, down_label=down_label)
        energy_series.append(series)

    # Two or more MPPTs: both cards stack the solar bars, one segment each.
    solar_parts = _solar_parts()
    if solar_parts:
        for series in energy_series:
            if series["key"] == "solar":
                series["parts"] = solar_parts

    graph_series = [
        {"key": key, "label": label, "entities": entities}
        for key, label, role in _GRAPH_SERIES
        if (entities := _role_list(role))
    ]
    if solar_parts:
        for series in graph_series:
            if series["key"] == "solar":
                series["parts"] = solar_parts

    cards: list[dict[str, Any]] = []
    if energy_series:
        cards.append({
            "type": "custom:sensa-energy-card",
            "title": "Energy",
            "series": energy_series,
        })
    soc = _role_list("battery_soc")
    if soc:
        health: dict[str, Any] = {
            "type": "custom:sensa-battery-health",
            "title": "Battery health",
            "soc": soc[0],
        }
        voltage = _role_list("battery_voltage")
        if voltage:
            health["voltage"] = voltage[0]
        cycles = _role_list("battery_cycles")
        if cycles:
            health["cycles"] = cycles[0]
        cards.append(health)
    if graph_series:
        cards.append({
            "type": "custom:sensa-electrical-graphs",
            # The card's key from before its rename (it was
            # sensa-advanced-graph), so a place saved in Design Mode holds.
            "reorder_key": "custom:sensa-advanced-graph",
            "title": "Graphs",
            "series": graph_series,
            "full_width": True,
        })
    return cards


# Friendly pill labels for the Multiplus/Quattro inverter-mode select. The
# select's own options carry the canonical values; the panel shows the same
# text, wrapping to two lines where needed (e.g. "Charger only").
_INVERTER_MODE_OPTIONS = ("Off", "Charger only", "Inverter only", "On")


def _home_controls_panel() -> dict[str, Any] | None:
    """A compact `custom:sensa-inverter-options` for the Home centre column.

    Sits directly beneath the power-flow hero (the `center_controls` slot of
    `sensa-view-home`) and holds the "important" controls a customer wants one
    tap from Home. Today that's the **inverter mode** — a four-way control (Off /
    Charger only / Inverter only / On) that a plain on/off switch can't express,
    rendered as a segmented pill selector, plus an on/off **trickle charge**
    toggle for any relay channel assigned that Device Type (the same control the
    Cab "park up" panel offers, via `_trickle_charge_controls`). The panel is
    deliberately generic so more controls can join it later (Richard,
    2026-06-27: "a couple of important switches ... just the inverter for now").

    Returns None when no such control resolves, so the slot is simply omitted
    (no empty panel) — same conditional-card rule as the rest of the Home view.
    """
    controls: list[dict[str, Any]] = []
    inverter_mode = resolve_role("inverter_mode")
    if inverter_mode:
        controls.append({
            "type": "segment",
            "entity": inverter_mode,
            "name": "Inverter",
            "icon": "mdi:sine-wave",
            "options": [{"value": v} for v in _INVERTER_MODE_OPTIONS],
        })
    # Same trickle-charge on/off toggle the Cab "park up" panel offers.
    controls.extend(_trickle_charge_controls())
    if not controls:
        return None
    return {
        # No title: the header (icon + "Controls") just wastes vertical space on
        # the Home centre column; the controls are self-evident.
        "type": "custom:sensa-inverter-options",
        # The card's key from before its rename (it was
        # sensa-control-panel), so a place saved in Design Mode holds.
        "reorder_key": "custom:sensa-control-panel",
        "controls": controls,
    }


def _trickle_charge_controls() -> list[dict[str, Any]]:
    """`toggle` control configs for every ENABLED relay channel the customer
    assigned the **Trickle Charge** Device Type, wired to that channel's ESPHome
    relay switch (toggled via `switch.toggle`). Empty list when none.

    `trickle_charge` is service-class category `electrical`, so it gets NO routed
    Habitation Lighting/Water/Climate tab (driven from Settings → Switching only)
    — but a one-tap on/off for the battery-maintenance charger belongs on the Cab
    "park up" Home screen, so it surfaces there via `_cab_controls_panel`. Reads
    the relay rows enriched by `_attach_relay_entities`, the same source the
    routed-switch tiles use; channels that are unassigned, assigned something
    else, disabled, or have no resolved relay entity yet are skipped.
    """
    from ..vendors.service_classes import SERVICE_CLASSES

    sc = SERVICE_CLASSES.get("trickle_charge")
    icon = sc.icon if sc else "mdi:battery-charging"
    controls: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if not is_relay_device(device):
            continue
        assignments = device.get("relay_assignments") or {}
        relay_entities = device.get("relay_entities") or {}
        relay_enabled = device.get("relay_enabled") or {}
        relay_labels = device.get("relay_labels") or {}
        for n in range(1, 9):
            slot = f"relay_{n}"
            if assignments.get(slot) != "trickle_charge":
                continue
            if relay_enabled.get(slot) is False:  # disabled channel
                continue
            relay_eid = relay_entities.get(slot)
            if not relay_eid:  # ESPHome relay not connected yet → can't toggle
                continue
            controls.append({
                "type": "toggle",
                "entity": relay_eid,
                "name": relay_labels.get(slot) or "Trickle Charger",
                "icon": icon,
            })
    return controls


def _cab_controls_panel() -> dict[str, Any] | None:
    """A `custom:sensa-inverter-options` of "park up" controls for the Cab Home
    tab. Today: an on/off toggle for a trickle charger wired to a relay channel
    (Device Type = Trickle Charge). Returns None when no such channel resolves,
    so the card is omitted (no placeholder — see
    [[conditional-cards-no-placeholder]])."""
    controls = _trickle_charge_controls()
    if not controls:
        return None
    return {
        # No title — the header just wastes space (see `_home_controls_panel`).
        "type": "custom:sensa-inverter-options",
        # The card's key from before its rename (it was
        # sensa-control-panel), so a place saved in Design Mode holds.
        "reorder_key": "custom:sensa-control-panel",
        "controls": controls,
    }


# Lower-cased unit strings that mark a sensor as a temperature even when the
# integration didn't stamp `device_class: temperature` (e.g. a hand-rolled
# ESPHome sensor). Matching on either signal means *any* future device that
# reports a temperature is surfaced with no per-device wiring.
_TEMPERATURE_UNITS = frozenset({"°c", "°f", "k"})

def _canon_mac(value: Any) -> str:
    """Canonicalise a MAC for comparison: upper-case, no separators."""
    return str(value or "").upper().replace(":", "").replace("-", "").replace(" ", "")


def _cooperate_env_domains() -> set[str]:
    """HA integration domains for the catalog's *cooperate-flow* environment
    sensors — the ones whose temperature entities are created and owned by
    HA's own native integration (Govee → `govee_ble`, Ruuvi → `ruuvitag_ble`),
    not by Sensa.

    Derived from the catalog (`accept_native` on every environment-category
    entry) so a future cooperate-flow environment sensor is picked up with no
    change here. These devices are frequently present in HA *without* a Sensa
    backend device row — the cooperate model leaves the device to its native
    integration — so the header-chip allowlist resolves them straight from the
    integration's HA devices rather than depending on a `_dashboard_devices`
    row + MAC round-trip (see `_environment_sensor_entities`, case 3).
    """
    from ..device_catalog import PHYSICAL_DEVICE_CATALOG
    from ..vendors.service_classes import category_of

    domains: set[str] = set()
    for entry in PHYSICAL_DEVICE_CATALOG:
        sc = entry.get("service_class")
        if not sc or category_of(sc) != "environment":
            continue
        # `accept_native` is the explicit "HA owns this device" marker; fall
        # back to the BLE/native discovery block's domain for entries that
        # cooperate without setting it.
        domain = entry.get("accept_native")
        if not domain:
            discovery = entry.get("discovery") or {}
            for block in discovery.values():
                if isinstance(block, dict) and block.get("domain"):
                    domain = block["domain"]
                    break
        if domain:
            domains.add(domain)
    return domains


def _environment_sensor_entities() -> set[str]:
    """Entity_ids owned by an **environment** sensor — the allowlist for the
    header temperature chips.

    Header temperature chips are for environment temperatures only (cabin /
    fridge / outside / weather / Cerbo room sensors / …), so we *include* the
    temperatures of devices whose service class is in the `environment`
    category (`temperature_sensor`, `environment_sensor`, …) rather than trying
    to enumerate everything to exclude. A router, relay, or leisure battery is
    never in that category, so its temperature simply isn't in the allowlist.

    Three kinds of environment device feed this allowlist:

      1. **Sensa-owned** devices (e.g. a Cerbo room-temperature MQTT
         sub-device) carry a `service_entities` / `network_entities` role map
         pointing at Sensa-created entities — those entity_ids seed directly.
      2. **Cooperate-flow BLE** sensors *with* a Sensa backend row (Govee /
         Ruuvi added through Sensa's picker) carry no role map but do carry a
         MAC — resolved to their HA device by a Bluetooth-connection MAC match.
      3. **Cooperate-flow native integrations** (`govee_ble` / `ruuvitag_ble`,
         derived from the catalog by `_cooperate_env_domains`) resolved
         *directly* from the integration's HA config entries → devices. This is
         the robust path: these sensors are owned by HA and are routinely
         present with NO Sensa backend row at all (the device list never sees
         them), so cases 1 & 2 silently miss them — which is exactly why their
         temperatures didn't show next to a Cerbo room sensor.

    All three then expand — when the HA registries are reachable — to every
    entity sharing an HA device with a seed entity or a resolved device, so an
    environment temperature the role map doesn't name is still included.
    """
    entities: set[str] = set()
    for entity_ids in _environment_device_entity_map().values():
        entities |= entity_ids
    entities |= _cooperate_env_entities()
    return entities


def _environment_device_entity_map() -> dict[str, set[str]]:
    """Sensa environment **device_id** -> the HA entity_ids that device owns.

    The PRIMITIVE for every environment surface. The flat allowlist
    (`_environment_sensor_entities`, which feeds the Climate-tab dials) is the
    union of this map plus `_cooperate_env_entities`, and the per-device header
    chip opt-in (Settings -> Devices) reads this map directly. One walker, so the
    header and the dials can never disagree about which device owns a reading --
    the "one predicate written twice" trap this repo keeps hitting.

    Covers cases 1 + 2 of the three described above: a Sensa-owned device with a
    role map, and a cooperate-flow BLE / Z2M sensor carrying a Sensa row + a MAC
    but no role map. Case 3 has no Sensa device_id to key on and so cannot be
    opted in per device; it lives in `_cooperate_env_entities`.

    Values are EXPANDED to every entity sharing the device's HA device when the
    registries are reachable, else the bare role-map seed (dry-run / test builds).
    """
    from ..vendors.service_classes import category_of

    seeds: dict[str, set[str]] = {}
    # device_id -> canonical MAC, for the environment devices Sensa creates no
    # entities for (case 2) — resolved to their HA device below.
    macs: dict[str, str] = {}
    anon = 0
    for device in _dashboard_devices:
        sc = _device_service_class(device)
        if sc is None or category_of(sc) != "environment":
            continue
        device_id = str(device.get("device_id") or device.get("id") or "")
        if not device_id:
            # An environment row with no id still owns readings and must reach
            # the flat allowlist (the Climate dials), so it gets a synthetic
            # key. `\x00` cannot occur in a real device_id, so the key is inert
            # for the header-chip ledgers — such a row has no Devices row to be
            # opted in from anyway.
            anon += 1
            device_id = f"\x00anon{anon}"
        seed = seeds.setdefault(device_id, set())
        has_role_map = False
        for entity_map in (
            device.get("service_entities"),
            device.get("network_entities"),
        ):
            if not isinstance(entity_map, dict):
                continue
            for val in entity_map.values():
                if isinstance(val, str):
                    seed.add(val)
                    has_role_map = True
                elif isinstance(val, (list, tuple)):
                    strs = [v for v in val if isinstance(v, str)]
                    seed.update(strs)
                    has_role_map = has_role_map or bool(strs)
        # No Sensa role map → a cooperate-flow BLE / Z2M sensor whose entities
        # are HA-native. Remember its MAC so we can find that HA device.
        if not has_role_map:
            mac = _canon_mac(
                device.get("linked_mac_address")
                or device.get("mac_address")
                or device.get("address")
                or device.get("mac")
            )
            if mac:
                macs[device_id] = mac

    if not seeds:
        return {}

    # Best-effort: the registries are absent in dry-run / test builds, in which
    # case the role-map seed stands unexpanded (a MAC-resolved device simply
    # contributes nothing there), matching the historic behaviour.
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(_dashboard_hass)
        dev_reg = dr.async_get(_dashboard_hass)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return seeds

    # Sensa device_id -> the HA device id(s) it resolves to.
    ha_ids: dict[str, set[str]] = {
        device_id: {
            ent.device_id
            for eid in seed
            if (ent := ent_reg.async_get(eid)) is not None and ent.device_id
        }
        for device_id, seed in seeds.items()
    }

    device_id_by_mac = {mac: device_id for device_id, mac in macs.items()}
    if device_id_by_mac:
        # Bluetooth-connection MAC match (the same match the remove path uses).
        try:
            for ha_dev in dev_reg.devices.values():
                for _conn_type, val in (ha_dev.connections or set()):
                    owner = device_id_by_mac.get(_canon_mac(val))
                    if owner:
                        ha_ids.setdefault(owner, set()).add(ha_dev.id)
        except Exception:
            _LOGGER.debug(
                "_environment_device_entity_map: MAC->device resolution failed",
                exc_info=True,
            )
        # Zigbee2MQTT environment sensors are owned by HA's `mqtt` integration
        # and key their HA device on the IEEE IDENTIFIER, not a Bluetooth
        # connection — so the match above never finds them. Match the
        # `zigbee2mqtt_<ieee>` identifier instead (the IEEE is stored MAC-style
        # on the Sensa row, so it is already a key here). Per-registered-device,
        # NOT a domain sweep, which would pull in every mqtt device.
        try:
            from ..vendors.zigbee2mqtt import zigbee2mqtt_device_ieee
            for ha_dev in dev_reg.devices.values():
                z_ieee = zigbee2mqtt_device_ieee(ha_dev)
                owner = device_id_by_mac.get(z_ieee) if z_ieee else None
                if owner:
                    ha_ids.setdefault(owner, set()).add(ha_dev.id)
        except Exception:
            _LOGGER.debug(
                "_environment_device_entity_map: Z2M identifier resolution "
                "failed",
                exc_info=True,
            )

    entities_by_ha_id: dict[str, set[str]] = {}
    for ent in ent_reg.entities.values():
        if ent.device_id:
            entities_by_ha_id.setdefault(ent.device_id, set()).add(ent.entity_id)

    out: dict[str, set[str]] = {}
    for device_id, seed in seeds.items():
        owned = set(seed)
        for ha_id in ha_ids.get(device_id) or ():
            owned |= entities_by_ha_id.get(ha_id, set())
        out[device_id] = owned
    _LOGGER.debug(
        "_environment_device_entity_map: %d environment device(s), %d entity(ies)",
        len(out), sum(len(v) for v in out.values()),
    )
    return out


def _cooperate_env_entities() -> set[str]:
    """Entity_ids of cooperate-flow native environment integrations (Govee /
    Ruuvi) resolved straight from their HA config entries -> devices — case 3,
    which needs no Sensa backend row.

    Kept separate from `_environment_device_entity_map` precisely BECAUSE these
    devices have no Sensa device_id: they can still feed the Climate dials, but
    there is no Devices row to hang a per-device header-chip opt-in off, so they
    cannot appear in the header until added through the picker (which turns them
    into case 2).
    """
    coop_domains = _cooperate_env_domains()
    if not coop_domains:
        return set()
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(_dashboard_hass)
        dev_reg = dr.async_get(_dashboard_hass)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return set()
    try:
        coop_entry_ids: set[str] = set()
        for domain in coop_domains:
            for ce in _dashboard_hass.config_entries.async_entries(domain):
                coop_entry_ids.add(ce.entry_id)
        if not coop_entry_ids:
            return set()
        device_ids = {
            ha_dev.id
            for ha_dev in dev_reg.devices.values()
            if coop_entry_ids.intersection(ha_dev.config_entries or ())
        }
    except Exception:
        _LOGGER.debug(
            "_cooperate_env_entities: cooperate-domain resolution failed",
            exc_info=True,
        )
        return set()
    if not device_ids:
        return set()
    return {
        ent.entity_id
        for ent in ent_reg.entities.values()
        if ent.device_id in device_ids
    }


def _diagnostic_or_config_entities() -> set[str]:
    """Entity_ids the HA entity registry marks as diagnostic / config category.

    Used to drop SECONDARY temperature entities — most notably a Z2M
    `device_temperature` (the internal MCU temp, entity_category=diagnostic,
    which many Aqara / SONOFF devices expose alongside the real ambient
    `temperature`). The environment temperature surfaces key off `device_class`
    alone, so without this a device that exposes both would show TWO header chips
    / Climate dials for one physical sensor. A primary ambient temperature has no
    entity_category, so this never drops a real reading. Empty when the registry
    isn't reachable (tests / dry-run YAML builds) — behaviour unchanged there.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(_dashboard_hass)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return set()
    return {
        ent.entity_id
        for ent in ent_reg.entities.values()
        if getattr(ent, "entity_category", None) is not None
    }


def _all_temperature_entities() -> list[str]:
    """Every environment `sensor.*` entity reporting a temperature.

    Scans HA's state machine for sensors flagged `device_class: temperature`
    **or** carrying a temperature unit (°C/°F/K), then keeps only those owned
    by an environment sensor (see `_environment_sensor_entities`) — so cabin /
    fridge / outside / weather / Cerbo room temperatures show, while a router
    board temp, relay-controller temp, or leisure-battery pack/shunt temp does
    not. Any environment device added in future is picked up automatically on
    the next dashboard regen, with nothing to add here per device.

    Returned entity_ids are sorted by friendly name. Empty when `hass` isn't
    stashed (tests / dry-run YAML builds), when no environment sensors are
    registered, or when nothing matches.
    """
    states_obj = getattr(_dashboard_hass, "states", None)
    if states_obj is None:
        return []
    # Real HA exposes a StateMachine (`async_all`); the dashboard tests pass a
    # plain dict of entity_id -> state.
    if hasattr(states_obj, "async_all"):
        states = states_obj.async_all("sensor")
    elif isinstance(states_obj, dict):
        states = list(states_obj.values())
    else:
        return []
    allowed = _environment_sensor_entities()
    # A device that exposes both an ambient `temperature` and an internal
    # `device_temperature` (diagnostic) would otherwise surface twice — drop the
    # diagnostic/config-category ones so one physical sensor = one chip.
    secondary = _diagnostic_or_config_entities()
    matches: list[tuple[str, str]] = []
    for st in states:
        eid = getattr(st, "entity_id", "")
        if not eid.startswith("sensor."):
            continue
        if eid not in allowed or eid in secondary:
            continue
        attrs = getattr(st, "attributes", None) or {}
        unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
        if attrs.get("device_class") != "temperature" and unit not in _TEMPERATURE_UNITS:
            continue
        label = str(attrs.get("friendly_name") or eid)
        matches.append((label.lower(), eid))
    matches.sort()
    return [eid for _, eid in matches]


def wired_heater_probes() -> list[str]:
    """Each Autoterm heater's External Probe Temperature, once a probe is wired.

    For the temperature-sensor DROPDOWNS only (`select.SensaCryptoRoomSensorSelect
    ._probes`), not the header chips or dial cards. "Wired" is a numeric
    reading: the controller never publishes the heater's "no probe" value, so
    an unfitted probe stays unknown and is not offered.
    """
    hass = _dashboard_hass
    if hass is None:
        return []
    from ..const import DOMAIN, HEATER_SERVICE_CLASSES
    from ..heater_temp_source import firmware_entities

    out: list[str] = []
    data = getattr(hass, "data", None) or {}
    for runtime in (data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        coordinator = runtime.get("coordinator")
        for device in ((getattr(coordinator, "data", None) or {}).get("devices")) or []:
            if _device_service_class(device) not in HEATER_SERVICE_CLASSES:
                continue
            eid = firmware_entities(hass, device).get("external_temperature")
            st = hass.states.get(eid) if eid else None
            try:
                float(st.state)
            except (AttributeError, TypeError, ValueError):
                continue
            if eid not in out:
                out.append(eid)
    return out


def _temperature_chip_label(entity_id: str, override: str | None = None) -> str:
    """Short header-chip label for a temperature sensor.

    `override` (the owning Sensa device's display_name — the name the customer
    typed in Settings > Discovery) wins verbatim when present, so a renamed
    cooperate-flow Govee / Ruuvi tag shows the new name immediately without
    waiting on HA to propagate `name_by_user` into the foreign integration's
    entity friendly_name. Otherwise falls back to the entity's friendly name
    with a trailing "Temperature"/"Temp" word trimmed ("Fridge Temperature" ->
    "Fridge"), since the chip already shows the value and carries a thermometer
    icon; and finally to the entity_id.
    """
    if override:
        return override
    name = entity_id
    states_obj = getattr(_dashboard_hass, "states", None)
    if states_obj is not None:
        st = states_obj.get(entity_id)
        if st is not None:
            attrs = getattr(st, "attributes", None) or {}
            name = str(attrs.get("friendly_name") or entity_id)
    words = name.split()
    if words and words[-1].lower().rstrip(".") in ("temperature", "temp"):
        words = words[:-1]
    return " ".join(words) or name


def _env_chip_name_overrides(temp_entity_ids: list[str]) -> dict[str, str]:
    """Map a temperature entity_id to the **Sensa device display_name** that
    owns it — the name the customer edits in Settings > Discovery.

    For cooperate-flow sensors (Govee / Ruuvi) the chip would otherwise show the
    HA entity `friendly_name`, which is owned by the foreign `govee_ble` /
    `ruuvitag_ble` integration and only tracks a Discovery rename if HA's
    `name_by_user` propagation AND the friendly_name recompute both land — they
    often don't, so the badge kept the old name. The Sensa backend display_name
    is the value actually edited, refreshed into `_dashboard_devices` by the
    rename's regen, so for any temp entity whose owning HA device matches a Sensa
    environment row by Bluetooth MAC we surface that name directly.

    Only applied when the owning device contributes exactly ONE temperature chip
    — a device with several temperature probes keeps its per-entity
    friendly_names so the chips stay distinguishable.
    """
    from ..vendors.service_classes import category_of

    # MAC -> Sensa display_name, for environment rows that carry a MAC.
    mac_to_name: dict[str, str] = {}
    for device in _dashboard_devices:
        sc = _device_service_class(device)
        if sc is None or category_of(sc) != "environment":
            continue
        name = device.get("display_name") or device.get("name")
        if not name:
            continue
        mac = _canon_mac(
            device.get("linked_mac_address")
            or device.get("mac_address")
            or device.get("address")
            or device.get("mac")
        )
        if mac:
            mac_to_name[mac] = str(name)
    if not mac_to_name:
        return {}

    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(_dashboard_hass)
        dev_reg = dr.async_get(_dashboard_hass)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return {}

    # HA device_id -> Sensa display_name, by Bluetooth-connection MAC match.
    devid_to_name: dict[str, str] = {}
    try:
        for ha_dev in dev_reg.devices.values():
            for _conn_type, val in (ha_dev.connections or set()):
                if _canon_mac(val) in mac_to_name:
                    devid_to_name[ha_dev.id] = mac_to_name[_canon_mac(val)]
                    break
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return {}
    # Zigbee2MQTT env sensors have no MAC connection — match by IEEE identifier
    # (stored MAC-style on the Sensa row, so it's a key in `mac_to_name`), so a
    # Discovery rename shows on the chip instead of Z2M's lagging friendly_name.
    try:
        from ..vendors.zigbee2mqtt import zigbee2mqtt_device_ieee
        for ha_dev in dev_reg.devices.values():
            if ha_dev.id in devid_to_name:
                continue
            z_ieee = zigbee2mqtt_device_ieee(ha_dev)
            if z_ieee and z_ieee in mac_to_name:
                devid_to_name[ha_dev.id] = mac_to_name[z_ieee]
    except Exception:
        _LOGGER.debug(
            "_env_chip_name_overrides: Z2M identifier resolution failed",
            exc_info=True,
        )
    if not devid_to_name:
        return {}

    # Owning device per temp entity + count so we only override single-temp
    # devices (a multi-probe device keeps its disambiguating friendly_names).
    eid_to_devid: dict[str, str] = {}
    per_device_count: dict[str, int] = {}
    for eid in temp_entity_ids:
        ent = ent_reg.async_get(eid)
        devid = getattr(ent, "device_id", None) if ent is not None else None
        if devid and devid in devid_to_name:
            eid_to_devid[eid] = devid
            per_device_count[devid] = per_device_count.get(devid, 0) + 1

    return {
        eid: devid_to_name[devid]
        for eid, devid in eid_to_devid.items()
        if per_device_count.get(devid) == 1
    }


def _temperature_chips() -> list[dict[str, Any]]:
    """Header chips for the temperature sensors the customer opted in.

    Each chip is `{entity, label, icon}` consumed by the Sensa header bar —
    a thermometer icon, a short label, and the live value. Appended after the
    base Mode chip by `_habitation_chips`, so temperatures ride the header on
    every Habitation view and are promoted to view badges wherever the header
    chips are (e.g. the Electrical view).

    Opt-in per device on the Settings -> Devices row (a `switch.*` backed by
    `entry.options["header_temperature_chips"]`), exactly like the power chips
    below. Before 2026-09-03 EVERY environment temperature rode the header
    automatically, which on a van with cabin / fridge / outside / Cerbo probes
    filled the strip and overflowed on a narrow tablet; the customer now picks
    however many fit their screen. Off by default, and existing installs are
    deliberately not seeded.

    `_environment_device_entity_map` is the one walker mapping a Sensa device
    to the entities it owns, so the opt-in and the Climate-tab dials can never
    disagree about which device owns a reading. A case-3 cooperate-flow sensor
    with no Sensa row (see `_environment_sensor_entities`) has no Devices row
    to be opted in from and so contributes no chip.

    Labels prefer the owning Sensa device's display_name (the Discovery rename)
    via `_env_chip_name_overrides`, falling back to the entity friendly_name.
    """
    from ..const import header_short_name, header_temp_chip_enabled

    options = _sensa_entry_options()
    if not options:
        return []
    opted_in: set[str] = set()
    # entity -> the customer's Header Short Name for the device that owns it.
    short_names: dict[str, str] = {}
    for device_id, entity_ids in _environment_device_entity_map().items():
        if header_temp_chip_enabled(options, device_id):
            opted_in |= entity_ids
            short = header_short_name(options, device_id)
            if short:
                short_names.update({eid: short for eid in entity_ids})
    if not opted_in:
        return []
    temp_eids = [e for e in _all_temperature_entities() if e in opted_in]
    if not temp_eids:
        return []
    overrides = _env_chip_name_overrides(temp_eids)
    return [
        {
            "entity": eid,
            # The Header Short Name wins, as typed: the customer chose it to
            # fit, so it is not run through the label shortening below.
            "label": short_names.get(eid)
            or _temperature_chip_label(eid, overrides.get(eid)),
            "icon": "mdi:thermometer",
        }
        for eid in temp_eids
    ]


def _flagged_temperature_entities(flag: Any) -> list[tuple[str, str]]:
    """(entity_id, label) for every environment temperature sensor whose
    device the customer has flagged `flag` (a `const.TemperatureFlag`:
    Temperature Watch / Fridge / Freezer) on its Settings -> Devices row.

    Feeds the instincts (`instincts_catalog._flagged_entities`) and the
    Temperature Watch change sensor, through the SAME opt-in mechanism as
    `_temperature_chips`'s header opt-in: one walker
    (`_environment_device_entity_map`) so a device flagged for the header
    strip and a device flagged for an instinct are resolved identically and
    can never disagree about which entity is "this device's temperature".
    No slot limit: every flagged device is returned.
    """
    options = _sensa_entry_options()
    if not options:
        return []
    opted_in: set[str] = set()
    for device_id, entity_ids in _environment_device_entity_map().items():
        if flag.enabled(options, device_id):
            opted_in |= entity_ids
    if not opted_in:
        return []
    temp_eids = [e for e in _all_temperature_entities() if e in opted_in]
    if not temp_eids:
        return []
    overrides = _env_chip_name_overrides(temp_eids)
    return [
        (eid, _temperature_chip_label(eid, overrides.get(eid)))
        for eid in temp_eids
    ]


def _temperature_watch_entities() -> list[tuple[str, str]]:
    """The Temperature Watch flag's list (see `_flagged_temperature_entities`)."""
    from ..const import TEMPERATURE_WATCH_FLAG

    return _flagged_temperature_entities(TEMPERATURE_WATCH_FLAG)

def _temperature_gauge_range(unit: str) -> tuple[int, int]:
    """Sensible dial min/max for a temperature unit — a habitation-comfort
    band so cabin / fridge / outside readings sit mid-arc. °C 0-40,
    °F 32-104, K 273-313; unknown units fall back to the °C band."""
    u = (unit or "").strip().lower()
    if u in ("°f", "f"):
        return 32, 104
    if u == "k":
        return 273, 313
    return 0, 40


def _home_temp_unit() -> str:
    """HA's configured home temperature unit ("°C" / "°F"), default °C.

    Read from `hass.config.units.temperature_unit` so every Sensa temperature
    surface reads in the unit the customer picked in Settings → System, NOT
    whatever an individual sensor happens to report natively (e.g. a °F BLE
    probe on a metric van)."""
    if _dashboard_hass is None:
        return "°C"
    units = getattr(getattr(_dashboard_hass, "config", None), "units", None)
    u = getattr(units, "temperature_unit", None)
    return str(u) if u else "°C"


def _climate_temperature_cards() -> list[dict[str, Any]]:
    """The temperatures, one dial card PER sensor (`_all_temperature_entities`,
    environment-owned only), for the Climate tabs and the Mobile Home tab.
    Deliberately EVERY one, not just the header opt-ins: the header is the
    shortlist the customer keeps in eyeline, these are where they read them
    all. Each card is keyed by its entity (`reorder_key`, which the Design
    Mode layout keys on), so a customer can arrange, remove and re-add each
    one on its own. Empty when no temperature sensor resolves (conditional
    cards have no placeholder)."""
    temp_eids = _all_temperature_entities()
    return [
        {
            "type": "custom:sensa-gauge-basic",
            "gauges": [gauge],
            "history": True,
            "reorder_key": gauge["entity"],
        }
        for gauge in _temperature_gauges(temp_eids)
    ]


def _temperature_gauges(temp_eids: list[str]) -> list[dict[str, Any]]:
    """One dial per temperature entity, labelled and ranged for the home unit.
    Labels prefer the owning device's Discovery rename
    (`_env_chip_name_overrides`)."""
    if not temp_eids:
        return []
    overrides = _env_chip_name_overrides(temp_eids)
    # Every dial reads in HA's home unit (Settings → System), converting from
    # the sensor's own unit when they differ, so a °F sensor doesn't show a °F
    # dial next to °C ones.
    #
    # The CARD does that conversion at render time (`convert: "temperature"`,
    # `webcards/src/shared/temperature.ts`), reading both units live. We must
    # NOT bake a scale/offset here: a baked factor freezes the units as they
    # were when this ran, so a per-entity `suggested_unit_of_measurement`
    # override (or a change of unit system) afterwards leaves the dial
    # converting the wrong way with nothing to trigger a regen. That shipped:
    # a °F probe read "84.5 °C" on the Climate tab while the header chip beside
    # it, which converts live, correctly read 29.2 °C.
    #
    # `unit` is the unit `min`/`max` are expressed in; the card converts the
    # range along with the reading if the home unit has since changed.
    home_unit = _home_temp_unit()
    gmin, gmax = _temperature_gauge_range(home_unit)
    gauges: list[dict[str, Any]] = []
    for eid in temp_eids:
        gauges.append({
            "entity": eid,
            "name": _temperature_chip_label(eid, overrides.get(eid)),
            "min": gmin,
            "max": gmax,
            "unit": home_unit,
            "convert": "temperature",
            "decimals": 1,
            "color": "var(--accent-color)",
            # A temperature isn't "low is bad" the way a battery / tank is, so
            # disable the safe-margin warn/crit colouring (negative thresholds
            # never trip) — the dial just fills proportionally in the fixed
            # colour rather than turning amber/red near the ends.
            "warn": -1,
            "crit": -1,
        })
    return gauges


# Venus OS tank `FluidType` labels (see `victron_mqtt_services._TANK_FLUID_LABELS`)
# and how the Home dials render each: (dial name, high_is_good, arc colour).
# Ordered as the dials appear after the battery. A tank that FILLS (grey /
# black water) inverts the colour band so full reads red; everything else
# warns as it empties. A fluid type outside this table gets no dial.
_TANK_FLUID_DIALS: dict[str, tuple[str, bool, str]] = {
    "fresh-water": ("Fresh Water", True, "var(--info-color)"),
    "waste-water": ("Grey Water", False, "var(--secondary-text-color)"),
    "black-water": ("Black Water", False, "var(--secondary-text-color)"),
    "diesel": ("Diesel", True, "var(--warning-color)"),
    "fuel": ("Fuel", True, "var(--warning-color)"),
    "gasoline": ("Petrol", True, "var(--warning-color)"),
    "lpg": ("LPG", True, "var(--warning-color)"),
    "lng": ("LNG", True, "var(--warning-color)"),
    "oil": ("Oil", True, "var(--warning-color)"),
    "hydraulic-oil": ("Hydraulic Oil", True, "var(--warning-color)"),
    "live-well": ("Live Well", True, "var(--info-color)"),
    "raw-water": ("Raw Water", True, "var(--info-color)"),
}


# The name a Cerbo tank sender's card carries until the Cerbo sends its
# CustomName (`victron_entities` default_name, `_victron_subdevice_name`).
_VENUS_TANK_FALLBACK = re.compile(r"^Tank #\d+$")


def _tank_fluid_type(role_map: dict[str, str]) -> str | None:
    """Lower-cased fluid-type label for a tank row, read live from its
    `fluid_type` entity state (e.g. "fresh-water", "waste-water"), or None
    when no fluid-type entity is mapped or its state is blank."""
    eid = role_map.get("fluid_type")
    if not eid:
        return None
    states_obj = getattr(_dashboard_hass, "states", None)
    if states_obj is None:
        return None
    st = states_obj.get(eid)
    if st is None:
        return None
    return str(getattr(st, "state", "") or "").strip().lower() or None


def _tank_levels_by_fluid() -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Every live tank level entity with its device, by fluid type (the keys of
    `_TANK_FLUID_DIALS`). The one place a tank's fluid is decided, for the Home
    dials and the diesel tanks a heater can draw from."""
    by_fluid: dict[str, list[tuple[str, dict[str, Any]]]] = {f: [] for f in _TANK_FLUID_DIALS}
    for device in _dashboard_devices:
        if _device_service_class(device) != "tank":
            continue
        role_map = _device_role_map(device)
        level_eid = role_map.get("level")
        if not level_eid or not _entity_is_live(level_eid):
            continue
        # A Venus tank sender reports its fluid as an entity; a Mopeka's comes
        # off its native config entry and rides on the row.
        fluid = device.get("tank_fluid_type") or _tank_fluid_type(role_map)
        if fluid in by_fluid:
            by_fluid[fluid].append((level_eid, device))
    return by_fluid


def diesel_tanks() -> dict[str, str]:
    """level entity -> name, for every tank a diesel heater can draw from: a
    diesel tank, or one only known as "fuel"."""
    out: dict[str, str] = {}
    by_fluid = _tank_levels_by_fluid()
    for fluid in ("diesel", "fuel"):
        for eid, device in by_fluid[fluid]:
            out[eid] = str(device.get("display_name") or device.get("name") or _TANK_FLUID_DIALS[fluid][0])
    return out


def _gauge_spec(
    entity_id: str,
    name: str,
    *,
    high_is_good: bool,
    color: str | None = None,
) -> dict[str, Any]:
    """One dial spec for the native `custom:sensa-gauge` card (0-100 %).

    The arc fills with the fixed `color` (an HA theme var) while the level is
    safe. `high_is_good` picks which end is the danger end: True (battery SOC,
    fresh water — empty is bad) leaves `invert` off so the arc warns amber/red
    as it nears empty; False sets `invert` so it warns as it nears full (grey
    water — a full waste tank needs emptying).
    """
    spec: dict[str, Any] = {
        "entity": entity_id,
        "name": name,
        "min": 0,
        "max": 100,
        "invert": not high_is_good,
    }
    if color is not None:
        spec["color"] = color
    return spec


def _home_left_dials() -> dict[str, Any]:
    """Left-column card for the Home layout: battery SOC + water-tank dials.

    A native `custom:sensa-gauge` card laying its speedometer dials out in a
    single row — battery state of charge (resolved vendor-neutrally via
    `resolve_role`), then every tank level found among the registered devices
    whose Venus fluid type is in `_TANK_FLUID_DIALS` (fresh, grey, black,
    diesel, fuel, LPG and the rest). A source is omitted when
    absent (honest UI, matching the rest of the dashboard); when *nothing*
    resolves, the card itself renders a short hint, so the slot is always a
    valid card (never the `card: null` trap on the `sensa-view-home` shell).

    Native (not the built-in `gauge`) so it direct-creates on a cold panel
    load without HA's Lovelace engine.
    """
    gauges: list[dict[str, Any]] = []

    soc = resolve_role("battery_soc")
    if isinstance(soc, str) and soc:
        gauges.append(
            _gauge_spec(
                soc, "Battery", high_is_good=True, color="var(--accent-color)",
            )
        )

    from ..mopeka import is_mopeka

    by_fluid = _tank_levels_by_fluid()
    for fluid, (label, high_is_good, color) in _TANK_FLUID_DIALS.items():
        tanks = by_fluid[fluid]
        for i, (eid, device) in enumerate(tanks):
            name = label if len(tanks) == 1 else f"{label} {i + 1}"
            # A tank the customer named carries that name: a Mopeka from the
            # Devices list, a Cerbo tank sender from its CustomName on the
            # Cerbo ("Heater fuel" read "Diesel 3" on .116, 2026-10-08). A
            # Cerbo tank still on its "Tank #23" fallback keeps the fluid label.
            own = str(device.get("display_name") or device.get("name") or "").strip()
            if own and (is_mopeka(device) or not _VENUS_TANK_FALLBACK.match(own)):
                name = own
            gauges.append(
                _gauge_spec(eid, name, high_is_good=high_is_good, color=color)
            )

    # Home-tab variant — wrapped in an ha-card so the dials match the
    # panel chrome of sibling Home cards. The canonical `sensa-gauge`
    # stays bare so it can nest inside e.g. `sensa-bms-card`. Conditional
    # cards have NO placeholder — an empty gauges list renders nothing
    # (parent layout collapses); `empty_text` is intentionally absent.
    return {
        "type": "custom:sensa-gauge-basic",
        # The card's key from before its rename (it was sensa-gauge-home),
        # so a place saved in Design Mode holds.
        "reorder_key": "custom:sensa-gauge-home",
        "gauges": gauges,
    }


def _split_dials(card: dict[str, Any]) -> list[dict[str, Any]]:
    """The Home dials card as one card per dial, so each battery and tank can
    be arranged on its own. Keyed by entity like the temperature cards; the
    first keeps the combined card's key, so a place saved for it holds."""
    out: list[dict[str, Any]] = []
    for i, gauge in enumerate(card.get("gauges") or []):
        out.append({
            **card,
            "gauges": [gauge],
            "reorder_key": card.get("reorder_key") if i == 0 else gauge["entity"],
        })
    return out


def _home_modes_bar(single_row: bool = True) -> dict[str, Any]:
    """Mode tile strip for a Home layout, as a native `custom:sensa-scene-bar`.

    Icon-only tiles (the mode name is the tooltip) so the bar stays slim, each
    bound to its per-mode active sensor `binary_sensor.sensa_nomad_mode_<slug>_active`
    for the active tint, tapping `sensa_nomad.activate_mode`. Native so it
    direct-creates on a cold panel load without HA's Lovelace engine — replaces
    the built-in `button`/`horizontal-stack` tiles for the Home tab.

    `single_row=True` lays all tiles on one row (the standard layout's bottom
    bar); False wraps them into a `columns`-wide grid (the split layout's right
    column).
    """
    from ..modes_catalog import MODES

    modes = [
        {
            "slug": m.slug,
            "name": m.name,
            "icon": m.icon,
            "entity": f"binary_sensor.sensa_nomad_mode_{m.slug}_active",
        }
        for m in MODES
    ]
    card: dict[str, Any] = {
        "type": "custom:sensa-scene-bar",
        # Its key from before the rename (sensa-mode-bar), so a place saved
        # in Design Mode holds. Same on the pill and single-row bars.
        "reorder_key": "custom:sensa-mode-bar",
        "modes": modes,
        "show_name": False,
        "single_row": single_row,
        # THIS bar is the Habitation Home page's: it shows only the scenes
        # the customer ticked "Include in Scenes card" for in the scene
        # popup (every scene by default). The Cab strip and the Mobile pills
        # show every shown scene regardless.
        "home_only": True,
    }
    if not single_row:
        card["columns"] = 4
    return card


def _modes_pill_bar() -> dict[str, Any]:
    """The 8 scenes as a wrapping pill toolbar (Mobile Overview, under the
    header). Same per-mode activation + active-tint as the Home mode bar, but
    styled as rounded name+icon pills that flow onto multiple rows on a phone
    width (`pill: True`)."""
    from ..modes_catalog import MODES

    modes = [
        {
            "slug": m.slug,
            "name": m.name,
            "icon": m.icon,
            "entity": f"binary_sensor.sensa_nomad_mode_{m.slug}_active",
        }
        for m in MODES
    ]
    return {
        "type": "custom:sensa-scene-bar",
        "reorder_key": "custom:sensa-mode-bar",
        "modes": modes,
        "pill": True,
    }


def _cab_overview_mode_bar() -> dict[str, Any]:
    """Mode tile strip for the Cab → Overview tab — the `sensa-scene-bar-single`
    sibling, which lays every tile on one row regardless of count.

    Same per-mode shape as `_home_modes_bar` so a future tile-style switch
    (icon-only vs. with-name) flips both surfaces consistently.
    """
    from ..modes_catalog import MODES

    return {
        "type": "custom:sensa-scene-bar-single",
        "reorder_key": "custom:sensa-mode-bar-single",
        # Span the whole row of the Cab Overview's `sensa-card-grid` so
        # the tiles read as a header strip, not a half-width card.
        "full_width": True,
        "modes": [
            {
                "slug": m.slug,
                "name": m.name,
                "icon": m.icon,
                "entity": f"binary_sensor.sensa_nomad_mode_{m.slug}_active",
            }
            for m in MODES
        ],
        "show_name": False,
    }


# Role keys a diesel-heater vendor module stamps on its device row's
# `service_entities` map (the {role: entity_id} contract `sensa-autoterm-heater-card`
# consumes). Defined here so the future Autoterm module just has to populate
# these and the card lights up on Power Central — no further dashboard wiring.
# Each maps a role key to the card config field it feeds.
_HEATER_ROLE_TO_CARD_FIELD: dict[str, str] = {
    "power": "power_entity",
    "current_temperature": "current_temp_entity",
    "target_temperature": "target_temp_entity",
    "power_level": "power_level_entity",
    "mode": "mode_entity",
    "run_state": "status_entity",
    "vent_available": "vent_available_entity",
    "supply_voltage": "voltage_entity",
    # Optional capability rows — drawn only when the heater exposes them
    # (a richer heater lights more rows; a bare one shows none). These feed
    # the card's diagnostics section, matching the "shared card, per-heater
    # feel from the data" decision (Richard, 2026-05-26).
    "glow_plug": "glow_plug_entity",
    "flame_sensor": "flame_sensor_entity",
    "fan_speed_actual": "fan_speed_actual_entity",
    "max_run_time": "max_run_time_entity",
    # Frost Protection (Air D 1.1.10, Baltica 1.1.18): the header snowflake.
    "frost_protection": "frost_entity",
    "baltica_mode": "baltica_mode_entity",
    # Baltica 1.1.21+: the pump icon, lit while the circulation pump runs.
    "heating_circuit": "heating_circuit_entity",
    "baltica_set_hot_water": "baltica_hot_water_button",
    "baltica_set_heating": "baltica_heating_button",
}


def _level_image(slot: str) -> str:
    """Resolved picture URL for a Cab levelling view (top / side / rear): the
    customer's choice from `text.sensa_nomad_level_image_<slot>`, or the Sensa
    default van artwork shipped in webcards when unset. The Settings → Levelling
    picker writes the override; changing it regenerates the dashboard, so this
    re-reads the fresh value on each rebuild."""
    from ..const import LEVEL_IMAGE_DEFAULTS, level_image_entity_id

    default = LEVEL_IMAGE_DEFAULTS.get(slot, "")
    states = getattr(_dashboard_hass, "states", None)
    if states is None:
        return default
    st = states.get(level_image_entity_id(slot))
    val = (getattr(st, "state", "") or "").strip() if st is not None else ""
    if val in ("", "unknown", "unavailable"):
        return default
    return val


def _build_cab_levelling_tilt_card(device: dict[str, Any]) -> dict[str, Any]:
    """Combined side+rear tilt visualiser for the Cab → Levelling tab.

    One card hosting BOTH views (Side / Pitch on top, Rear / Roll below)
    via the tilt card's `axis: both` mode. Each view reads its calibrated
    pitch/roll and tilts the van picture. Pictures default to the Sensa van
    artwork (webcards) and are customer-overridable on Settings → Levelling.
    """
    from ..const import van_level_calibrated_sensor_entity_id

    device_id = str(device.get("device_id") or device.get("id") or "")
    return {
        "type": "custom:sensa-level-tilt-card",
        "axis": "both",
        "pitch_entity": van_level_calibrated_sensor_entity_id(
            device_id, "pitch",
        ),
        "roll_entity": van_level_calibrated_sensor_entity_id(
            device_id, "roll",
        ),
        "image_side": _level_image("side"),
        "image_rear": _level_image("rear"),
    }


def _build_level_roll_card(
    device: dict[str, Any], size: str = "compact",
) -> dict[str, Any]:
    """Side-roll danger card: the rear-view van leaning at its real angle,
    amber from 80% of the customer's Maximum Roll Angle and red at it.

    `compact` is the Cab Home tile; `large` fills the Cab Roll tab. Both read
    the calibrated roll and the singleton `max_roll_deg` limit, so the two can
    never disagree about where danger starts.
    """
    from ..const import (
        van_level_calibrated_sensor_entity_id,
        van_level_geometry_entity_id,
    )

    device_id = str(device.get("device_id") or device.get("id") or "")
    return {
        "type": "custom:sensa-level-roll-card",
        "size": size,
        "roll_entity": van_level_calibrated_sensor_entity_id(device_id, "roll"),
        "limit_entity": van_level_geometry_entity_id("max_roll_deg"),
        "image": _level_image("rear"),
    }


def _cab_roll_cards() -> list[dict[str, Any]]:
    """The compact roll card for the Cab Home, one per levelling device.

    Always shown when a van level is registered, not opt-in like the placed
    cards: it is a driving safety readout, the reason the Cab screen exists.
    """
    from ..const import LEVELLING_SERVICE_CLASSES

    return [
        _build_level_roll_card(d, "compact")
        for d in _dashboard_devices
        if _device_service_class(d) in LEVELLING_SERVICE_CLASSES
    ]


def _build_cab_levelling_topdown_card(device: dict[str, Any]) -> dict[str, Any]:
    """Top-down van outline with per-wheel shim badges for Cab → Levelling.

    Pairs with the gyro: same shim data, different mental model — the gyro
    is abstract quadrants, this is "where on YOUR van".
    """
    from ..const import (
        van_level_geometry_entity_id,
        van_level_shim_sensor_entity_id,
    )

    device_id = str(device.get("device_id") or device.get("id") or "")
    card: dict[str, Any] = {
        "type": "custom:sensa-level-topdown-card",
        "shim_fl_entity": van_level_shim_sensor_entity_id(device_id, "FL"),
        "shim_fr_entity": van_level_shim_sensor_entity_id(device_id, "FR"),
        "shim_rl_entity": van_level_shim_sensor_entity_id(device_id, "RL"),
        "shim_rr_entity": van_level_shim_sensor_entity_id(device_id, "RR"),
        "shim_tolerance_entity": van_level_geometry_entity_id(
            "shim_tolerance_cm",
        ),
        # Customer-overridable top-down van picture (default = Sensa artwork).
        "image": _level_image("top"),
    }
    return {k: v for k, v in card.items() if v is not None}


def _build_cab_levelling_gyro_card(
    device: dict[str, Any],
    disc_max_width_px: int | None = None,
) -> dict[str, Any]:
    """Big circular gyro visual for the Cab → Overview tab.

    Four quadrants tinted green/red per wheel against the singleton
    shim_tolerance, centre disc shows LEVEL / NOT LEVEL, top-right pill
    toggles the firmware's Levelling Mode (fast publish rate while
    parking). Built from the same shim sensors the summary card uses.

    `disc_max_width_px` caps the square disc, which is the card's only height
    driver, so it sizes the whole card. None leaves the card's 560px default.
    """
    from ..const import (
        van_level_geometry_entity_id,
        van_level_shim_sensor_entity_id,
    )

    device_id = str(device.get("device_id") or device.get("id") or "")
    role_map = device.get("levelling_entities") or {}
    card: dict[str, Any] = {
        "type": "custom:sensa-level-gyro-card",
        "shim_fl_entity": van_level_shim_sensor_entity_id(device_id, "FL"),
        "shim_fr_entity": van_level_shim_sensor_entity_id(device_id, "FR"),
        "shim_rl_entity": van_level_shim_sensor_entity_id(device_id, "RL"),
        "shim_rr_entity": van_level_shim_sensor_entity_id(device_id, "RR"),
        "shim_tolerance_entity": van_level_geometry_entity_id(
            "shim_tolerance_cm",
        ),
        # Optional firmware Levelling Mode toggle — surfaces as the
        # top-right pill on the card. Omitted (card hides the pill) when
        # the classifier hasn't matched the switch yet.
        "mode_entity": role_map.get("mode_switch"),
        "disc_max_width_px": disc_max_width_px,
    }
    return {k: v for k, v in card.items() if v is not None}


def _build_cab_levelling_card(device: dict[str, Any]) -> dict[str, Any]:
    """Customer-facing Cab → Levelling summary card for one Van Level device.

    Surfaces the LEVEL / NOT LEVEL status driven by the shim_tolerance
    singleton, the per-wheel shim cm values, the lowest wheel, the
    calibrated pitch/roll, and the Levelling Mode toggle in one composite
    native Lit card. Fancy visuals (circular gyro, top-down view, tilty
    van images) are later slices; this is the basic actionable summary.
    """
    from ..const import (
        van_level_calibrated_sensor_entity_id,
        van_level_geometry_entity_id,
        van_level_lowest_wheel_sensor_entity_id,
        van_level_shim_max_sensor_entity_id,
        van_level_shim_sensor_entity_id,
    )

    device_id = str(device.get("device_id") or device.get("id") or "")
    role_map = device.get("levelling_entities") or {}
    card: dict[str, Any] = {
        "type": "custom:sensa-level-summary-card",
        "name": device.get("display_name") or device.get("name") or "Van Level",
        "mode_entity": role_map.get("mode_switch"),
        "pitch_calibrated_entity": van_level_calibrated_sensor_entity_id(
            device_id, "pitch",
        ),
        "roll_calibrated_entity": van_level_calibrated_sensor_entity_id(
            device_id, "roll",
        ),
        "shim_fl_entity": van_level_shim_sensor_entity_id(device_id, "FL"),
        "shim_fr_entity": van_level_shim_sensor_entity_id(device_id, "FR"),
        "shim_rl_entity": van_level_shim_sensor_entity_id(device_id, "RL"),
        "shim_rr_entity": van_level_shim_sensor_entity_id(device_id, "RR"),
        "shim_max_entity": van_level_shim_max_sensor_entity_id(device_id),
        "lowest_wheel_entity": van_level_lowest_wheel_sensor_entity_id(
            device_id,
        ),
        "shim_tolerance_entity": van_level_geometry_entity_id(
            "shim_tolerance_cm",
        ),
    }
    return {k: v for k, v in card.items() if v is not None}


def _build_levelling_cards(device: dict[str, Any]) -> list[dict[str, Any]]:
    """**Cab → Level** tab cards for one Van Level device: the Raw Sensor card.
    (Calibration moved to Settings -> Vehicle -> Levelling, 2026-09-30.)

    These were on Settings -> Level until 2026-08-10 and moved beside the
    readings they explain: Calibrate is pressed while parked on ground the
    customer can see is flat, and the raw pitch/roll is how you tell a sensor
    that is not reporting from one reporting something you disagree with. Both
    are questions asked while looking at the Cab level screen, so sending the
    customer to Settings meant leaving it.

    The **Orientation** card was a third of these until 2026-08-10. It is not
    a card any more: which way a unit is bolted in is a property of THAT unit,
    so the six axis/sign selects and the pitch/roll they produce are now config
    fields on the sensor's own row in Settings -> Devices, emitted by
    `sensor._config_entities_for_device`. Do not rebuild a card for them.

    Each card takes only the entity_ids it needs. Firmware-side roles
    (mode_switch / pitch / roll) come from the device's `levelling_entities`
    role-map populated by `_attach_levelling_entities`; integration-side
    entities (numbers / selects / buttons / derived sensors) have
    deterministic entity_ids from `const.py`. The cards drop any keys whose
    value resolves to None.
    """
    role_map = device.get("levelling_entities") or {}

    # Firmware roles only. The singleton vehicle-geometry numbers (wheel track
    # / wheelbase / shim tolerance) that used to ride along under a "Vehicle
    # Details" heading moved to Settings -> Vehicle on 2026-08-10: one van has
    # one wheelbase, so repeating them on every sensor's card read as though
    # each sensor had its own.
    raw_card: dict[str, Any] = {
        "type": "custom:sensa-level-raw-card",
        # Raw pitch/roll is a diagnostic: shown only while Nerd Mode is on
        # (Richard, 2026-09-26). `sensa-card-grid` drops the cell otherwise.
        "nerd": True,
        # Left out if not yet classified; card shows "--".
        "mode_entity": role_map.get("mode_switch"),
        "pitch_entity": role_map.get("pitch"),
        "roll_entity": role_map.get("roll"),
    }

    # The Calibration card that followed moved to Settings -> Vehicle ->
    # Levelling as config-card rows (Richard, 2026-09-30):
    # `build_settings._levelling_calibration_fields`.

    # Drop None-valued keys so the cards' optional-entity logic (which only
    # checks `undefined`, not `null`) treats unresolved roles as missing.
    return [
        {k: v for k, v in c.items() if v is not None}
        for c in (raw_card,)
    ]


def _build_heater_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Build a `custom:sensa-autoterm-heater-card` config for one heater device — AIR
    (Autoterm Air D) or HYDRONIC (Alde, Autoterm Baltica) — or None when its
    role map has no usable entity yet.

    Used by `_heater_cards` (the Habitation Climate tab) and the Settings Cards
    gallery. The heater card was removed from the Power Central Home right slot
    (Richard, 2026-05-30), so the Climate tab is now its only customer surface.
    Capability-driven: every field comes from the device's role map (see
    `_HEATER_ROLE_TO_CARD_FIELD`), populated by the heater's vendor module, so
    any future heater in `HEATER_SERVICE_CLASSES` is picked up unchanged.
    """
    role_map = _device_role_map(device)
    card: dict[str, Any] = {
        "type": "custom:sensa-autoterm-heater-card",
        "name": device.get("display_name") or device.get("name") or "Heater",
    }
    # The card's Nerd Mode Diagnostics popup asks for this heater by id
    # (`sensa_nomad/heater_diagnostics`); offered only when the controller
    # publishes the diagnostics frame.
    if "diagnostics_frame" in _device_role_map(device):
        card["device_id"] = str(device.get("device_id") or device.get("id") or "")
    # Optional per-heater product photo (the catalog `image`, stamped on
    # the row by enrichment) — the "tailored feel, one card" lever. A bare
    # filename becomes a `/local/sensa/images/` URL; an absent image lets
    # the card fall back to its mdi:fire icon.
    image = device.get("image")
    if image:
        card["image"] = (
            image if str(image).startswith("/")
            else f"/local/sensa/images/{image}"
        )
    # Brand strip under the mode buttons (Richard, 2026-07-25): the maker's
    # name in text. No logo on the cards (Richard, 2026-10-08); the Devices
    # row keeps its catalog logo.
    brand = _heater_brand(device)
    if brand:
        card["brand"] = brand
    has_entity = False
    for role_key, field in _HEATER_ROLE_TO_CARD_FIELD.items():
        eid = role_map.get(role_key)
        if eid:
            card[field] = eid
            has_entity = True
    current = _heater_current_temp_entity(device, role_map)
    if current:
        card["current_temp_entity"] = current
    # Diesel burned (`diesel_use.py`), every Autoterm heater. Not the van's
    # days left: that is all heaters together, shown on SensaClimate only.
    from ..diesel_use import (
        cost_entity_id,
        is_autoterm,
        per_day_entity_id,
        use_entity_id,
    )

    heater_id = str(device.get("device_id") or device.get("id") or "")
    if heater_id and is_autoterm(device):
        from ..heater_runtime import runtime_entity_id

        card["diesel_use_entity"] = use_entity_id(heater_id)
        card["diesel_per_day_entity"] = per_day_entity_id(heater_id)
        # What that costs a day at the nearest priced diesel (a Nerd row).
        card["diesel_cost_entity"] = cost_entity_id(heater_id)
        # The heater's lifetime run time (`heater_runtime.py`), a Nerd row.
        card["total_runtime_entity"] = runtime_entity_id(heater_id)
        # An Air reads no run time until its Heater Model is chosen; the card
        # says "Set model" in its place. Flow-family cards drop this key.
        from ..const import heater_model_select_entity_id

        card["heater_model_entity"] = heater_model_select_entity_id(heater_id)
        # Measured fan speed: the Air's controller publishes its own; the Flow
        # and Baltica get it decoded from the diagnostics frame (a Nerd row).
        # The Baltica has no fan, so its card labels this row "Pump speed"
        # (Richard, 2026-10-17).
        if "diagnostics_frame" in role_map and "fan_speed_actual_entity" not in card:
            from ..diesel_use import fan_entity_id

            card["fan_speed_actual_entity"] = fan_entity_id(heater_id)
    # Conditional cards: no entity → no card. A heater device row with an
    # empty role map (e.g. a partially-enriched discovery, a sim that
    # hasn't classified yet) emits nothing rather than a placeholder card.
    if not has_entity:
        return None
    if _is_autoterm_flow(device):
        # The Flow 5D uses the Air's dial card (its circle gauge and lock,
        # Richard 2026-09-28; the dial takes the 60 to 80 range from the target
        # entity), fed only the keys a Flow has, and flagged hydronic so Nerd
        # Mode lists the coolant. `sensa-autoterm-flow-card` is no longer
        # emitted: it had neither the dial nor the lock. The Baltica is a Flow
        # 5D and takes the same card.
        card = {k: v for k, v in card.items() if k in _FLOW_CARD_FIELDS}
        # "Now" is always the heater's own coolant sensor: a pressurised
        # liquid heater never regulates on anything else, so a Temperature
        # Sensor Source left over from older firmware is ignored here.
        coolant = role_map.get("coolant_temperature")
        if coolant:
            card["current_temp_entity"] = coolant
        else:
            card.pop("current_temp_entity", None)
        # The panel's room reading, shown beside the coolant. With the coolant
        # in its own role, the Flow's only other temperature is the panel's.
        panel = role_map.get("panel_temperature") or role_map.get("current_temperature")
        if panel:
            card["panel_temp_entity"] = panel
        card["hydronic"] = True
        card.update(_baltica_room_fields(device))
    return card


def _baltica_room_fields(device: dict[str, Any]) -> dict[str, str]:
    """For a Baltica picked as a SensaClimate heater (any of the three, on
    firmware with its SensaClimate Heat switch): SensaClimate's own target and
    reading, which the card's dial uses in Hot water and heating while
    SensaClimate is on, so the two cards never disagree. Off, the dial sets
    the Baltica's own Heating Temperature again. Empty otherwise."""
    from ..climate_control import (
        HEATER_SLOTS,
        configured,
        entitled,
        heater_entities,
        is_baltica,
        nominated,
    )
    from ..const import CLIMATE_CONTROL_SWITCH_ENTITY_ID, CLIMATE_CONTROL_TARGET_ENTITY_ID

    entry = _climate_control_entry()
    if entry is None or _dashboard_hass is None or not is_baltica(device):
        return {}
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id or device_id not in {nominated(entry, slot) for slot in HEATER_SLOTS}:
        return {}
    if not configured(entry) or not entitled(_dashboard_hass) or not heater_entities(_dashboard_hass, device)[0]:
        return {}
    sensor = nominated(entry, "sensor")
    return {
        "room_target_entity": CLIMATE_CONTROL_TARGET_ENTITY_ID,
        # The card uses the room target only while SensaClimate is on.
        "room_control_entity": CLIMATE_CONTROL_SWITCH_ENTITY_ID,
        **({"room_temp_entity": sensor} if sensor else {}),
    }


# The Flow-family catalog entries (the Flow 5D, and the Baltica built round
# one), and the dial card keys they have (the rest of the heater card's are
# Air-only).
_AUTOTERM_FLOW_CATALOG_IDS = frozenset({"sensa_autoterm_flow", "sensa_autoterm_abal"})
_FLOW_CARD_FIELDS = frozenset({
    "type", "name", "image", "brand", "device_id", "power_entity",
    "current_temp_entity", "target_temp_entity", "status_entity",
    "max_run_time_entity", "baltica_mode_entity", "heating_circuit_entity",
    "room_target_entity", "room_temp_entity", "room_control_entity",
    "baltica_hot_water_button", "baltica_heating_button",
    "diesel_use_entity", "diesel_per_day_entity", "diesel_cost_entity", "total_runtime_entity",
    "frost_entity", "fan_speed_actual_entity",
})


def _is_autoterm_flow(device: dict[str, Any]) -> bool:
    """A Flow-family controller (Flow 5D or Baltica): by the row's catalog id,
    else by the catalog entry its model matches. A registered record can carry
    no `catalog_id` (the App stores `model` only), and a Flow drawn with the
    Air card shows a dial and none of the Flow rows."""
    if device.get("catalog_id") in _AUTOTERM_FLOW_CATALOG_IDS:
        return True
    model = str(device.get("vendor_model") or device.get("model") or "")
    if not model:
        return False
    try:
        from ..sensor import _find_catalog_by_vendor_model
        entry = _find_catalog_by_vendor_model(model) or {}
    except Exception:  # noqa: BLE001 - never fatal: fall back to the Air card
        return False
    return entry.get("id") in _AUTOTERM_FLOW_CATALOG_IDS


def _heater_current_temp_entity(
    device: dict[str, Any], role_map: dict[str, str],
) -> str | None:
    """The "Now" reading for a heater card: the sensor the heater is
    regulating on, per the device row's Temperature Sensor Source. A heater
    without that setting (no source firmware) keeps its own reading."""
    if "temp_source_select" not in role_map:
        return role_map.get("current_temperature")
    choice = None
    device_id = str(device.get("device_id") or device.get("id") or "")
    if _dashboard_hass is not None and device_id:
        try:
            from ..const import DOMAIN
            from ..heater_temp_source import choice_for

            for entry in _dashboard_hass.config_entries.async_entries(DOMAIN):
                choice = choice_for(entry, device_id)
                break
        except Exception:
            _LOGGER.debug("Could not read the heater temperature source", exc_info=True)
    if choice is None:
        return role_map.get("current_temperature")
    from ..heater_temp_source import regulating_temp_entity

    return regulating_temp_entity(role_map, choice)


def _heater_brand(device: dict[str, Any]) -> str:
    """The maker's name for a heater card's brand strip, or "".

    Text only: the heater cards carry no logo (Richard, 2026-10-08), though the
    catalog's `brand_logo` still marks the device's Devices row. The row
    carries `brand` when enrichment stamped it, but a registered record can
    predate that field, so fall back to the catalog entry the device's model
    matches (`_find_catalog_by_vendor_model`), then to the row's `vendor` /
    `manufacturer`, so a heater with no catalog brand still names its maker.
    """
    brand = str(device.get("brand") or "")
    if not brand:
        model = str(device.get("vendor_model") or device.get("model") or "")
        if model:
            try:
                from ..sensor import _find_catalog_by_vendor_model
                entry = _find_catalog_by_vendor_model(model) or {}
            except Exception:  # noqa: BLE001 - cosmetic, never fatal
                entry = {}
            brand = str(entry.get("brand") or "")
    return brand or str(device.get("vendor") or device.get("manufacturer") or "")


def _heater_cards() -> list[dict[str, Any]]:
    """A `custom:sensa-autoterm-heater-card` config for every registered heater device
    whose role map has surfaced at least one usable entity.

    Drives the Habitation **Climate** tab — an adopted heater appears there with
    its full thermostat card. This is now the heater's only customer surface (it
    was removed from the Power Central Home right slot, Richard 2026-05-30). One
    card per heater.

    Gated on `HEATER_SERVICE_CLASSES`, not `air_heater` alone: a HYDRONIC heater
    (Alde, Autoterm Baltica) was silently dropped here, so adding a Baltica
    surfaced no Climate card at all. Same trap as `_attach_heater_entities`.
    """
    from ..const import HEATER_SERVICE_CLASSES

    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if _device_service_class(device) not in HEATER_SERVICE_CLASSES:
            continue
        card = _build_heater_card(device)
        if card is not None:
            cards.append(card)
    return cards


def _ac_climate_entity_id(device: dict[str, Any]) -> str | None:
    """The AC's `climate.*` entity id, or None if it can't be resolved yet.

    Role map first — an ESPHome-bridged AC (the Dometic FreshJet) exposes a
    NATIVE `climate.*` entity stamped onto `service_entities` by
    `_attach_air_conditioner_entities`. Fall back to the deterministic Velit pin
    ONLY for a non-ESPHome AC: an ESPHome AC whose native climate entity is not
    in the role map yet (the cold-boot window before `esphome` adopts it, or a
    hostname/IP that hasn't matched) returns None so the caller OMITS the card
    rather than binding it to a `climate.sensa_nomad_velit_*` id that never
    exists for a FreshJet (code-review finding [3]). The AC entity-registration
    listener + rebuild re-run then fill the map and add the card next regen.
    """
    from ..const import velit_climate_entity_id

    device_id = device.get("device_id") or device.get("id")
    if not device_id:
        return None
    climate = _device_role_map(device).get("climate")
    if climate:
        return climate
    integration = device.get("integration_type") or device.get("integration") or ""
    if integration == "esphome":
        return None
    return velit_climate_entity_id(device_id)


def _ac_display_name(device: dict[str, Any]) -> str:
    """Customer-facing name for an `air_conditioner` device.

    The FreshJet's registered device is the BRIDGE (renamed "…Bridge" for the
    Devices picker), but every AC card is the AC itself — so prefer the catalog
    product name (`child_name`) and the customer never sees the internal
    "…Bridge" name on a thermostat. Any bridge-style catalog entry that declares
    a child_name gets this; the Velit (none) keeps its own device name.

    Shared by the Climate-tab card and the Home-layout compact summary so both
    surfaces title the same AC identically.
    """
    from ..const import custom_ac_name

    if custom := custom_ac_name(device):
        return custom
    vendor_model = device.get("vendor_model") or device.get("model") or ""
    if vendor_model:
        from ..device_catalog import PHYSICAL_DEVICE_CATALOG
        for entry in PHYSICAL_DEVICE_CATALOG:
            if entry.get("vendor_model") == vendor_model and entry.get("child_name"):
                return str(entry["child_name"])
    return device.get("display_name") or device.get("name") or "Air Conditioner"


def _build_air_conditioner_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Build a `custom:sensa-air-conditioner-card` config for one
    `air_conditioner` device (Velit or ESPHome FreshJet), or None when its
    `climate.*` entity can't be resolved (see `_ac_climate_entity_id`).
    """
    climate_entity = _ac_climate_entity_id(device)
    if not climate_entity:
        return None
    name = _ac_display_name(device)
    card: dict[str, Any] = {
        "type": "custom:sensa-air-conditioner-card",
        "entity": climate_entity,
        "name": name,
    }
    image = device.get("image")
    if image:
        card["image"] = (
            image if str(image).startswith("/")
            else f"/local/sensa/images/{image}"
        )
    card.update(_ac_light_entities(device))
    return card


def _ac_light_entities(device: dict[str, Any]) -> dict[str, str]:
    """The FreshJet bridge's interior / roof light entities as card config
    (`interior_light` / `exterior_light`), stamped on the role map by
    `_attach_air_conditioner_entities`. Empty for an AC with no lights (the
    Velit). ONE helper for both the Climate-tab card and the Home summary so
    the two surfaces cannot disagree on which lights exist; each card renders
    a light button only while its entity reports a real on/off state.
    """
    role_map = _device_role_map(device)
    return {
        key: role_map[key]
        for key in ("interior_light", "exterior_light")
        if role_map.get(key)
    }


def _air_conditioner_cards() -> list[dict[str, Any]]:
    """A `custom:sensa-air-conditioner-card` config for every registered
    `air_conditioner` device.

    Leads the Habitation **Climate** tab alongside the diesel-heater card(s) —
    a registered Velit Rooftop AC appears there with its full thermostat card,
    and surfaces the conditional Climate tab on its own (the tab is created when
    a Velit is the first/only climate item). One card per AC unit.
    """
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if _device_service_class(device) != "air_conditioner":
            continue
        card = _build_air_conditioner_card(device)
        if card is not None:
            cards.append(card)
    return cards


def _fan_controller_cards() -> list[dict[str, Any]]:
    """A `custom:sensa-fan-card` for every Sensa Fan Temperature Control.

    One row per fan whose `fan.*` entity HA has, with the entities the card
    needs beside it: the customer's name for the fan, its Auto switch and the
    Temperature Sensor select (whose state names the sensor it follows). A
    controller none of whose fans resolve gets no card, no placeholder.
    """
    from ..const import (
        fan_auto_switch_entity_id,
        fan_name_text_entity_id,
        fan_source_select_entity_id,
    )
    from ..fan_autopilot import fan_entities, is_fan_controller

    if _dashboard_hass is None:
        return []
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if not is_fan_controller(device):
            continue
        device_id = str(device.get("device_id") or device.get("id") or "")
        if not device_id:
            continue
        try:
            fans = fan_entities(_dashboard_hass, device)
        except Exception:  # noqa: BLE001 - one controller must not break the tab
            _LOGGER.debug("Fan card: could not resolve fans for %s", device_id, exc_info=True)
            continue
        if not fans:
            continue
        cards.append({
            "type": "custom:sensa-fan-card",
            "name": device.get("display_name") or device.get("name") or "Fans",
            "fans": [
                {
                    "entity": fans[ch],
                    "default_name": f"Fan {ch}",
                    "name_entity": fan_name_text_entity_id(device_id, ch),
                    "auto_entity": fan_auto_switch_entity_id(device_id, ch),
                    "source_entity": fan_source_select_entity_id(device_id, ch),
                }
                for ch in sorted(fans)
            ],
        })
    return cards


def _speaker_media_entity(device: dict[str, Any], hass: Any = None) -> str | None:
    """Resolve the HA-owned `media_player.*` entity for a cooperate-flow
    speaker device (e.g. a Sonos adopted via HA's native `sonos` integration).

    A speaker's entities are created and owned by HA's own integration, not
    Sensa — there's no Sensa role map and no pinned entity_id. So mirror the
    cooperate-flow resolution in `_environment_sensor_entities`: find the HA
    devices owned by the speaker's integration (its config entries), prefer the
    one whose MAC connection matches this Sensa row, then pick the
    `media_player.*` entity on it. When the MAC can't be pinned (Sonos keys its
    HA device on a RINCON id, not a MAC) fall back to the integration's sole
    `media_player` — the common single-speaker case. Returns None when nothing
    resolves (card/tab omitted — conditional cards have no placeholder), and
    never guesses when several speakers are ambiguous.
    """
    domain = (
        device.get("integration_type") or device.get("integration") or ""
    ).lower()
    if not domain:
        return None
    # Default to the render-time hass when no explicit one is passed (the
    # dashboard path); the discovery sensor's reachability resolver passes
    # its own hass so it doesn't depend on a dashboard render having run.
    hass = hass or _dashboard_hass
    if hass is None:
        return None
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return None

    try:
        entry_ids = {
            ce.entry_id
            for ce in hass.config_entries.async_entries(domain)
        }
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return None
    if not entry_ids:
        return None

    integ_device_ids = {
        ha_dev.id
        for ha_dev in dev_reg.devices.values()
        if entry_ids.intersection(ha_dev.config_entries or ())
    }
    if not integ_device_ids:
        return None

    # Prefer the HA device whose MAC connection matches this Sensa row.
    row_mac = _canon_mac(
        device.get("linked_mac_address")
        or device.get("mac_address")
        or device.get("address")
        or device.get("mac")
    )
    matched_device_id: str | None = None
    if row_mac:
        for ha_dev in dev_reg.devices.values():
            if ha_dev.id not in integ_device_ids:
                continue
            if any(
                _canon_mac(val) == row_mac
                for _conn_type, val in (ha_dev.connections or set())
            ):
                matched_device_id = ha_dev.id
                break

    target_device_ids = (
        {matched_device_id} if matched_device_id else integ_device_ids
    )
    media_entities = sorted(
        ent.entity_id
        for ent in ent_reg.entities.values()
        if ent.device_id in target_device_ids
        and ent.entity_id.startswith("media_player.")
    )
    if not media_entities:
        return None
    # No MAC pin + several candidates → can't safely attribute one to this row.
    if matched_device_id is None and len(media_entities) > 1:
        return None
    return media_entities[0]


def _build_media_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Build a `custom:sensa-media-player-card` config for one `speaker` device
    (e.g. a Sonos Roam 2), or None when its HA `media_player` entity can't be
    resolved (see `_speaker_media_entity`)."""
    entity = _speaker_media_entity(device)
    if not entity:
        return None
    card: dict[str, Any] = {
        "type": "custom:sensa-media-player-card",
        "entity": entity,
        "name": device.get("display_name") or device.get("name") or "Speaker",
    }
    image = device.get("image")
    if image:
        card["image"] = (
            image if str(image).startswith("/")
            else f"/local/sensa/images/{image}"
        )
    return card


def _dump_valve_channel() -> dict[str, str] | None:
    """The relay channel the customer routed as the Dump Valve, or None.

    The first ENABLED channel assigned the `dump_valve` Switched-Device type
    whose relay entity has resolved (ESPHome connected). Read off the same
    enriched relay rows `_routed_switch_tiles_by_category` uses, so the Dump
    tab's button and the Water tab's tile always drive the same switch.
    """
    for device in _dashboard_devices:
        if not is_relay_device(device):
            continue
        assignments = device.get("relay_assignments") or {}
        relay_entities = device.get("relay_entities") or {}
        relay_enabled = device.get("relay_enabled") or {}
        relay_labels = device.get("relay_labels") or {}
        for n in range(1, 9):
            slot = f"relay_{n}"
            if assignments.get(slot) != "dump_valve":
                continue
            if relay_enabled.get(slot) is False:
                continue
            relay_eid = relay_entities.get(slot)
            if not relay_eid:
                continue
            # Always "Dump Valve", never the channel's own label ("Relay 1"):
            # the button names the thing at the drain, not the wiring
            # (Richard, 2026-09-05).
            return {"entity": relay_eid, "name": "Dump Valve", "channel": relay_labels.get(slot) or f"Relay {n}"}
    return None


def _camera_cards() -> list[dict[str, Any]]:
    """A `custom:sensa-camera-card` for every registered IP camera.

    Drives the conditional Habitation **Dump** tab: at Pro the one camera is
    the grey-waste dump camera, shown live through Frigate. The card names the
    camera entity by its pinned id (`camera.py` sets it), so nothing here
    depends on the entity having registered yet; the card shows "connecting"
    until it has.
    """
    from ..const import ip_camera_entity_id

    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if _device_service_class(device) != "ip_camera":
            continue
        device_id = str(device.get("device_id") or "")
        if not device_id:
            continue
        cards.append({
            "type": "custom:sensa-camera-card",
            "entity": ip_camera_entity_id(device_id),
            "name": device.get("display_name") or device.get("name") or "Camera",
            # A lone card in `sensa-card-grid` is capped at 480px and centred
            # unless it asks for the full width; the feed wants the whole tab
            # (Richard, 2026-09-05), letterboxed inside by the card itself.
            "full_width": True,
        })
    # The Dump Valve button rides on the picture (bottom right, large): the
    # whole point of watching the drain is to open the valve over it. Only
    # when a relay channel is routed as the dump valve; the card draws no
    # button otherwise.
    valve = _dump_valve_channel()
    if valve is not None:
        for card in cards:
            card["valve_entity"] = valve["entity"]
            card["valve_name"] = valve["name"]
    return cards


def _media_cards() -> list[dict[str, Any]]:
    """A `custom:sensa-media-player-card` config for every registered `speaker`
    device whose HA `media_player` entity resolves.

    Drives the conditional Habitation **Media** tab — an adopted Sonos (or any
    future speaker) appears there with its now-playing + transport card. One
    card per speaker.
    """
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if _device_service_class(device) != "speaker":
            continue
        card = _build_media_card(device)
        if card is not None:
            cards.append(card)
    return cards


def _zigbee2mqtt_device_id(device: dict[str, Any]) -> str | None:
    """Resolve the HA device id for a registered Zigbee2MQTT device, or None.

    The HA device is created and owned by HA's shared `mqtt` integration (Z2M
    published it) — there's no Sensa role map. Resolve it by its Z2M identifier
    (`("mqtt","zigbee2mqtt_<ieee>")`) rather than a MAC connection: Z2M keys its
    HA device on the IEEE identifier and the connection set is typically empty.
    """
    from ..vendors.zigbee2mqtt import zigbee2mqtt_ha_device_id

    return zigbee2mqtt_ha_device_id(_dashboard_hass, device)


def _is_zigbee2mqtt(device: dict[str, Any]) -> bool:
    """True when a registered device is owned by Zigbee2MQTT (HA's shared
    `mqtt` integration) — the discriminator every Z2M card-builder shares."""
    integration = (
        device.get("integration_type") or device.get("integration") or ""
    ).lower()
    return integration == "zigbee2mqtt"


def _zigbee2mqtt_device_entity(
    device_id: str, domain: str, token: str = "",
) -> str | None:
    """The `<domain>.*` entity on a Z2M HA device that best matches `token`.

    `token=""` takes the first entity in the domain, stable-sorted (e.g. the
    `light`). A token like `_scene` / `_work_mode` / `_music_mode` /
    `_music_sensitivity` picks the Z2M expose that published that control:
    an entity_id ENDING with the token wins, and a plain substring match is
    the fallback so nothing that resolved before stops resolving. Several
    matches means we are guessing, which is logged. Returns None when none
    match (the card omits that section, no placeholder).
    """
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(_dashboard_hass)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return None
    prefix = f"{domain}."
    matches = sorted(
        ent.entity_id
        for ent in ent_reg.entities.values()
        if ent.device_id == device_id
        and ent.entity_id.startswith(prefix)
        and (not token or token in ent.entity_id)
    )
    if not matches:
        return None
    if not token:
        return matches[0]
    # Prefer an entity whose id ENDS with the token. The vendor publishes the
    # expose name last, so `select.lounge_scene` IS the scene picker while
    # `select.lounge_scene_recall_delay` merely contains the word. The plain
    # substring list stays as the fallback, so every device that resolved
    # before still resolves now; this only changes WHICH candidate wins when
    # several match.
    exact = [eid for eid in matches if eid.endswith(token)]
    shortlist = exact or matches
    chosen = shortlist[0]
    if len(shortlist) > 1:
        # Silently taking the first of several is how a card ends up driving
        # the wrong channel after a vendor firmware update renames or reorders
        # exposes. Harmless on a single-expose Zigbee light; not harmless on a
        # multi-channel relay, so say so rather than guessing quietly.
        _LOGGER.warning(
            "Sensa Nomad: %d %s entities on HA device %s match %r (%s); "
            "using %s. A vendor firmware update can reorder these, so this "
            "card may be driving the wrong channel.",
            len(shortlist), domain, device_id, token,
            ", ".join(shortlist), chosen,
        )
    return chosen


def _zigbee2mqtt_light_entity(device: dict[str, Any]) -> str | None:
    """Resolve the HA `light.*` entity for a registered Zigbee2MQTT light, or
    None when unresolved (card/tab omitted — no placeholder). Never guesses
    beyond the first stable-sorted `light.*` on the device."""
    device_id = _zigbee2mqtt_device_id(device)
    if not device_id:
        return None
    return _zigbee2mqtt_device_entity(device_id, "light")


def _build_zigbee_light_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Build a light card config for one Zigbee2MQTT light, or None when its HA
    `light` entity can't be resolved yet (the mqtt entity may register after the
    post-add rebuild; the adoption listener re-runs this once it does).

    An **addressable / pixel controller** (one that publishes a `select.*_scene`
    expose, e.g. the Gledopto GL-SPI-206P) gets the richer
    `custom:sensa-led-card`, wired with its scene + work-mode entities so the
    customer can drive the built-in effect presets. A plain white / RGB bulb
    gets `custom:sensa-light-card`. (The controller's music-reactive mode is
    intentionally NOT wired — it needs the Tuya app's phone mic, so it does
    nothing on a Z2M-only install.)
    """
    device_id = _zigbee2mqtt_device_id(device)
    if not device_id:
        return None
    # ONE resolver, shared with the compact Home card (`_build_home_light_summary`)
    # so a change to how the addressable controller's exposes are named can't fix
    # one surface and leave the other silently driving the wrong entity.
    parts = _zigbee2mqtt_light_parts(device)
    if parts is None:
        return None
    entity, scene_entity, work_mode = parts
    name = device.get("display_name") or device.get("name") or "Light"

    # Per-controller Sensa selects, both keyed on this HA device id (created by
    # select.py). `strip_entity` → the addressable colour-order dropdown;
    # `light_type_entity` → what the controller's output is wired for, which
    # decides whether the colour mixer / colour-temp slider are drawn.
    #
    # The ENTITY is passed, never a resolved value: the select resolves the
    # customer's override or Z2M's detection itself and publishes the answer as
    # its `resolved` attribute, which the card reads. Baking the value here would
    # freeze it at dashboard-generation time, so a controller re-interviewed into
    # a different mode would keep rendering the old one with nothing to trigger a
    # regen — the same trap the temperature dials hit (see CLAUDE.md).
    from ..const import (
        led_strip_select_entity_id,
        light_leave_relay_on_switch_entity_id,
        light_power_on_delay_number_entity_id,
        light_type_select_entity_id,
    )

    light_type_entity = light_type_select_entity_id(device_id)

    if scene_entity:
        card: dict[str, Any] = {
            "type": "custom:sensa-led-card",
            "entity": entity,
            "name": name,
            "scene_entity": scene_entity,
            "strip_entity": led_strip_select_entity_id(device_id),
            "light_type_entity": light_type_entity,
        }
        if work_mode:
            card["work_mode_entity"] = work_mode
    else:
        card = {
            "type": "custom:sensa-light-card",
            "entity": entity,
            "name": name,
            "light_type_entity": light_type_entity,
        }

    image = device.get("image")
    if image:
        card["image"] = (
            image if str(image).startswith("/")
            else f"/local/sensa/images/{image}"
        )
    # A relay channel assigned to this controller as an Integrated Device
    # (`power_relay` attached by `_attach_power_relays`) lets the card fold the
    # channel's mains/12V switch into its own power control, so the customer
    # drives one button instead of the relay switch + the light separately.
    if device.get("power_relay"):
        card["power_relay"] = device["power_relay"]
        # ...and the customer's Power-On Delay for it, set on this controller's
        # Settings -> Devices row. The ENTITY is passed, never the value: the
        # card reads it at press time, so raising the delay takes effect on the
        # next press instead of waiting for a dashboard regen.
        card["power_delay_entity"] = light_power_on_delay_number_entity_id(
            device_id,
        )
        # ...and whether a power-off cuts that relay at all.
        card["leave_relay_on_entity"] = light_leave_relay_on_switch_entity_id(
            device_id,
        )
    return card


_light_group_candidates: list[dict[str, str]] | None = None


def light_group_candidate_entities() -> list[dict[str, str]]:
    """The entities a customer may put in a light GROUP, each with the name the
    Lighting tab shows for it.

    Registered Zigbee2MQTT `lighting` devices, plus the relay channels the
    customer declared as lighting -- the same two tests that decide what the
    Lighting tab shows (`_zigbee2mqtt_light_cards` and
    `_routed_switch_tiles_by_category`), reused rather than restated so the
    group picker can never offer something the Lighting tab does not consider
    a light, or refuse something it does.

    Every `light.*` entity in HA is NOT the answer, which is how the Autoterm
    heater's RGB status LED came to be offered as a group member (Richard,
    2026-09-17): it is a light entity on a HEATER, and its service class says
    so.
    """
    # CACHED, because this is read from a light group's
    # `extra_state_attributes` -- a hot path HA reads on every state write --
    # and `_zigbee2mqtt_light_parts` walks the whole entity registry up to
    # three times per device. Uncached, one group writing state cost a dozen
    # full registry sweeps on the event loop (2026-09-17). Invalidated by
    # `set_dashboard_devices`, which is the pipeline that changes the answer.
    global _light_group_candidates
    if _light_group_candidates is not None:
        return [dict(c) for c in _light_group_candidates]

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for device in _dashboard_devices:
        if _device_service_class(device) != "lighting":
            continue
        if _is_shelly(device):
            # Same names the Lighting tab puts on its cards.
            for entity, name in _shelly_light_parts(device):
                if entity not in seen:
                    seen.add(entity)
                    out.append({"entity": entity, "name": name})
            continue
        if not _is_zigbee2mqtt(device):
            continue
        parts = _zigbee2mqtt_light_parts(device)
        if parts is None:
            continue
        entity = parts[0]
        if not entity or entity in seen:
            continue
        seen.add(entity)
        # The NAME the Lights tab puts on this light's card, not the entity's
        # HA friendly_name: a Z2M device the customer never renamed in Z2M is
        # called by its IEEE address there, so the picker offered
        # `0x00158d0053494d1a` while its card said "Lounge Strip" (Richard,
        # 2026-09-18). Same expression as `_build_zigbee_light_card`.
        out.append({
            "entity": entity,
            "name": device.get("display_name") or device.get("name") or "Light",
        })
    # ...plus every relay channel the customer declared as lighting on the
    # Device Type dropdown. A plain 12V strip on a relay is a light to the
    # customer and already sits on the Lighting tab beside the Zigbee bulbs,
    # so it must be groupable with them; a group drives it on/off and it
    # ignores the brightness and colour its dimmable neighbours take.
    #
    # Read from `_routed_switch_tiles_by_category`, which is the one place
    # that decides which channels are lighting (assigned, enabled, wired).
    # Restating those three tests here is exactly the duplicated predicate
    # that keeps shipping as a bug in this repo.
    for tile in (_routed_switch_tiles_by_category().get("lighting") or []):
        entity = tile.get("entity")
        if not entity or entity in seen:
            continue
        seen.add(entity)
        # The channel's label, for the same reason: the ESPHome entity behind
        # it is called "Relay 3" until the label is applied, and a channel that
        # has never been labelled has no better name than the tile's.
        out.append({"entity": entity, "name": tile.get("name") or entity})
    _light_group_candidates = out
    return [dict(c) for c in out]


def _zigbee2mqtt_light_cards() -> list[dict[str, Any]]:
    """A `custom:sensa-light-card` config for every registered Zigbee2MQTT
    `lighting` device whose HA `light` entity resolves.

    Leads the conditional Habitation **Lighting** tab (above any routed relay
    switch tiles) — a Zigbee bulb adopted via Z2M appears there with its
    brightness/colour control, and surfaces the Lighting tab on its own (the
    AC/heater→Climate pattern). One card per light.
    """
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if _device_service_class(device) != "lighting":
            continue
        if not _is_zigbee2mqtt(device):
            continue
        card = _build_zigbee_light_card(device)
        if card is not None:
            cards.append(card)
    return cards


def _is_shelly(device: dict[str, Any]) -> bool:
    integration = (
        device.get("integration_type") or device.get("integration") or ""
    ).lower()
    return integration == "shelly"


def _shelly_light_parts(device: dict[str, Any]) -> list[tuple[str, str]]:
    """`(light entity, card name)` for each output of an adopted Shelly light
    controller (the Plus RGBW PM), in the vendor's channel order. Empty until
    HA's shelly entry and its entities exist.

    ONE resolver for the Lighting tab, the Home card and the group picker, the
    Z2M pattern. A unit in "light" profile has four dimmable outputs and each
    gets its own card, named by the customer on its Devices row or numbered
    until they do; in "rgbw" / "rgb" profile it has one, named after the device.
    """
    if _dashboard_hass is None:
        return []
    try:
        from homeassistant.helpers import entity_registry as er

        from ..vendors.shelly import shelly_light_outputs

        outputs = shelly_light_outputs(
            _dashboard_hass, er.async_get(_dashboard_hass), device,
        )
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return []
    return [(entity, name) for _channel, entity, name in outputs]


def _shelly_light_cards() -> list[dict[str, Any]]:
    """A `custom:sensa-light-card` per output of every registered Shelly
    `lighting` device. No Light Type select: that is keyed on a Z2M model, and
    the card falls back to what the entity's `supported_color_modes` say,
    which on a Shelly follows the profile set on the device."""
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if _device_service_class(device) != "lighting" or not _is_shelly(device):
            continue
        image = device.get("image")
        for entity, name in _shelly_light_parts(device):
            card: dict[str, Any] = {
                "type": "custom:sensa-light-card",
                "entity": entity,
                "name": name,
            }
            if image:
                card["image"] = (
                    image if str(image).startswith("/")
                    else f"/local/sensa/images/{image}"
                )
            cards.append(card)
    return cards


def _light_groups() -> list[dict[str, Any]]:
    """The customer's light groups, in display order. Empty if the store
    hasn't loaded (a cold boot before setup's bootstrap) — the tab simply
    shows the individual lights until the next regen."""
    from ..const import DOMAIN

    if _dashboard_hass is None:
        return []
    try:
        for runtime in (_dashboard_hass.data.get(DOMAIN) or {}).values():
            if not isinstance(runtime, dict):
                continue
            store = runtime.get("light_groups")
            if store is not None:
                return store.groups_payload()
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return []
    return []


def _light_group_cards() -> list[dict[str, Any]]:
    """One `custom:sensa-light-card` per light group.

    The same card the individual lights use, because a group IS a light: it
    takes only `entity` and every value it reads is a generic light attribute.
    The per-controller options (`power_relay`, `light_type_entity` and
    friends) are deliberately omitted — they belong to one physical
    controller and have no group meaning.
    """
    cards: list[dict[str, Any]] = []
    for group in _light_groups():
        slug = group.get("slug")
        if not slug:
            continue
        cards.append({
            "type": "custom:sensa-light-card",
            "entity": light_group_entity_id(slug),
            "name": group.get("name") or slug,
            "icon": group.get("icon") or "mdi:lightbulb-group",
            # Tints the card apart from the individual lights beside it: the
            # group icon alone was not obvious enough at a glance.
            "group": True,
        })
    return cards


def _lighting_tab_cards() -> list[dict[str, Any]]:
    """Cards for a Lighting surface: the routed `lighting` relay-channel tiles
    panel (leads, one `sensa-relay-tiles` of the small image buttons) + the
    Zigbee bulb / LED controller cards beneath. Empty when no lighting is
    installed (no routed `lighting` channel AND no Z2M light), so the caller omits
    the tab. Shared by the Habitation Lighting tab and the Mobile Lighting tab so
    the two stay in sync.

    Light groups lead, because a customer who has grouped their lights wants
    the group first and the individual lights underneath it. Grouping is done
    from the + on an individual light (Richard, 2026-09-17), which replaced a
    manage card at the foot of the tab: the question a customer actually has is
    whether THIS light belongs with some others, not "show me a list of
    groups"."""
    # `groups: True` puts the same + a Zigbee light card carries on each
    # lighting channel's tile, so a plain light on a relay can be filed into a
    # group like any other light -- and a van whose only lights are relay
    # channels can still make one. Stamped HERE and not in
    # `_routed_switch_tiles_by_category`, because the Switching, Water and
    # Climate tabs share those tiles and a water pump is not a light.
    tiles = [
        {**tile, "groups": True}
        for tile in (_routed_switch_tiles_by_category().get("lighting") or [])
    ]
    cards: list[dict[str, Any]] = []
    cards.extend(_light_group_cards())
    if tiles:
        cards.append({
            "type": "custom:sensa-relay-tiles",
            "full_width": True,
            "tiles": tiles,
        })
    # Individual lights carry the group + ; a GROUP card does not, because a
    # group cannot be a member of a group.
    cards.extend({**card, "groups": True} for card in _zigbee2mqtt_light_cards())
    cards.extend({**card, "groups": True} for card in _shelly_light_cards())
    # NOT squared any more (Richard, 2026-09-17). Squaring was right while the
    # colour wheel sat on the card face and was the tallest thing on it; the
    # wheel moved into the picker the same day, so a square cell is now mostly
    # empty space under a header, a brightness slider and a colour row. The
    # grid levels every light card to the tallest instead (`equal_rows`).
    #
    # Then put them in the order the customer DRAGGED them into, and stamp each
    # light card with the key the grid sends back on a drop. The relay strip
    # gets no key: it is a full-width panel, not a card in the tile flow, and a
    # customer cannot drag it anywhere useful.
    from ..const import lighting_card_order, sort_by_lighting_card_order

    order = lighting_card_order(_sensa_entry_options())
    tiles = iter(sort_by_lighting_card_order(
        [c for c in cards if not c.get("full_width")], order,
    ))
    # The panel keeps its SLOT. Only the tiles permute among themselves, so a
    # drag cannot move the relay strip out from between the groups and the
    # individual lights (the same rule the grid's own preview follows).
    out: list[dict[str, Any]] = []
    for card in cards:
        if card.get("full_width"):
            out.append(card)
            continue
        tile = next(tiles)
        out.append(
            {**tile, "reorder_key": tile["entity"]}
            if tile.get("entity") else tile
        )
    return out


def _climate_control_entry() -> Any:
    """The Sensa Nomad config entry, for Climate Control's settings; None in a
    hass-less build."""
    entries = getattr(_dashboard_hass, "config_entries", None)
    if entries is None:
        return None
    from ..const import DOMAIN

    for entry in entries.async_entries(DOMAIN):
        return entry
    return None


def _climate_units() -> tuple[bool, bool]:
    """(has a heater, has an air conditioner) among the van's devices. The
    Climate tab's Climate Control card needs one; the SensaClimate tab shows
    either way (Richard, 2026-11-21)."""
    from ..const import AC_SERVICE_CLASSES, HEATER_SERVICE_CLASSES

    classes = {_device_service_class(d) for d in _dashboard_devices}
    return (
        bool(classes & set(HEATER_SERVICE_CLASSES)),
        bool(classes & set(AC_SERVICE_CLASSES)),
    )


def _climate_control_card() -> dict[str, Any] | None:
    """`custom:sensa-climate-control-card`, leading the Climate tab once the
    customer has nominated a heater or air conditioner and the van still has
    one (a nomination outlives a deleted device). Pro and above; None
    otherwise (no placeholder)."""
    from ..climate_control import configured, entitled
    from ..const import (
        CLIMATE_CONTROL_STATUS_ENTITY_ID,
        CLIMATE_CONTROL_SWITCH_ENTITY_ID,
        CLIMATE_CONTROL_TARGET_ENTITY_ID,
    )

    entry = _climate_control_entry()
    if entry is None or not entitled(_dashboard_hass) or not configured(entry):
        return None
    if not any(_climate_units()):
        return None
    return {
        "type": "custom:sensa-climate-control-card",
        "name": "Climate Control",
        "switch_entity": CLIMATE_CONTROL_SWITCH_ENTITY_ID,
        "target_entity": CLIMATE_CONTROL_TARGET_ENTITY_ID,
        "status_entity": CLIMATE_CONTROL_STATUS_ENTITY_ID,
    }


def _climate_control_settings_card() -> dict[str, Any] | None:
    """The Habitation -> SensaClimate tab's one card: a diagram of how Climate Control
    works, each box holding its own settings (which heater, air conditioner and
    sensors; the restart difference and the battery %), an (i) opening that
    box's rules (`climate_control.rules_by_box`) and lit while its unit runs.
    Pro and above, on every van: with no heater or air conditioner it is the
    teaser that sells them, so both unit boxes are always drawn; starting it
    needs both picked (`climate_control.configured`). Richard, 2026-11-21,
    reversing 2026-10-15."""
    from ..climate_control import entitled, rules_by_box
    from ..const import (
        CLIMATE_CONTROL_AC_HEAT_SOC_ENTITY_ID,
        CLIMATE_CONTROL_ALARM_DRIFT_ENTITY_ID,
        CLIMATE_CONTROL_ALARM_MINUTES_ENTITY_ID,
        CLIMATE_CONTROL_HEATER_RESTART_ENTITY_ID,
        CLIMATE_CONTROL_LOW_FUEL_ENTITY_ID,
        CLIMATE_CONTROL_SELECTS,
        CLIMATE_CONTROL_STATUS_ENTITY_ID,
        CLIMATE_CONTROL_SWITCH_ENTITY_ID,
        CLIMATE_CONTROL_TARGET_ENTITY_ID,
    )

    if _dashboard_hass is None or not entitled(_dashboard_hass):
        return None
    has_heater, _has_ac = _climate_units()
    from ..diesel_use import DAYS_LEFT_ENTITY_ID, TOTAL_COST_ENTITY_ID

    return {
        "type": "custom:sensa-climate-settings-card",
        "status_entity": CLIMATE_CONTROL_STATUS_ENTITY_ID,
        "switch_entity": CLIMATE_CONTROL_SWITCH_ENTITY_ID,
        "target_entity": CLIMATE_CONTROL_TARGET_ENTITY_ID,
        "selects": {key: CLIMATE_CONTROL_SELECTS[key][0] for key in CLIMATE_CONTROL_SELECTS},
        "restart_entity": CLIMATE_CONTROL_HEATER_RESTART_ENTITY_ID,
        "ac_heat_soc_entity": CLIMATE_CONTROL_AC_HEAT_SOC_ENTITY_ID,
        "alarm_minutes_entity": CLIMATE_CONTROL_ALARM_MINUTES_ENTITY_ID,
        "alarm_drift_entity": CLIMATE_CONTROL_ALARM_DRIFT_ENTITY_ID,
        "low_fuel_entity": CLIMATE_CONTROL_LOW_FUEL_ENTITY_ID,
        # Diesel left in the Climate Control box; the card draws it only once
        # the sensor has a number, so an air-con-only van shows nothing.
        **({"diesel_days_left_entity": DAYS_LEFT_ENTITY_ID} if has_heater else {}),
        # ...and what all the heaters' diesel costs a day, beneath it.
        **({"diesel_cost_entity": TOTAL_COST_ENTITY_ID} if has_heater else {}),
        "has_heater": True,
        "has_ac": True,
        "rules": rules_by_box(),
        "images": _climate_control_images(),
        "full_width": True,
    }


def _climate_control_images() -> dict[str, str]:
    """The nominated units' product photos for the SensaClimate diagram, as
    the device cards show them (a bare catalog filename becomes a
    `/local/sensa/images/` URL). The tab rebuilds when a pick changes."""
    entry = _climate_control_entry()
    if entry is None:
        return {}
    from ..climate_control import nominated

    by_id = {str(d.get("device_id") or ""): d for d in _dashboard_devices}
    out: dict[str, str] = {}
    for box in ("heater", "secondary", "third", "ac"):
        device = by_id.get(nominated(entry, box) or "") or {}
        image = device.get("image")
        if not image:
            # A registered record often carries no image (the App stores the
            # model only): the catalog entry its model matches has the photo,
            # the same fallback `_heater_brand` uses for the maker's name.
            model = str(device.get("vendor_model") or device.get("model") or "")
            if model:
                try:
                    from ..sensor import _find_catalog_by_vendor_model

                    image = (_find_catalog_by_vendor_model(model) or {}).get("image")
                except Exception:  # noqa: BLE001 - a photo is cosmetic, never fatal
                    image = None
        if image:
            out[box] = image if str(image).startswith("/") else f"/local/sensa/images/{image}"
    return out


def _climate_tab_cards() -> list[dict[str, Any]]:
    """Cards for a Climate surface: the Climate Control card (once set up),
    then the diesel heater card(s) and air conditioner card(s), then one temperature dial card per sensor, then the routed
    `climate` relay-channel tiles panel. Empty when none of those resolve, so
    the caller omits the tab. Shared by the Habitation Climate tab and the
    Mobile Climate tab so the two stay in sync, as `_lighting_tab_cards` is.

    A temperature card alone is enough to surface the tab, so a van with only
    environment sensors (no heater, no AC, no switched loads) still gets one."""
    control = _climate_control_card()
    cards: list[dict[str, Any]] = [
        *([control] if control else []),
        *_heater_cards(), *_air_conditioner_cards(), *_fan_controller_cards(),
        *_climate_temperature_cards(),
    ]
    tiles = _routed_switch_tiles_by_category().get("climate") or []
    if tiles:
        cards.append({
            "type": "custom:sensa-relay-tiles",
            "full_width": True,
            "tiles": tiles,
        })
    return cards


# Security service class -> (tile `kind`, preferred binary_sensor device_classes,
# icon). The `kind` tells `sensa-sensor-tile` which on/off vocabulary to show
# (Detected/Clear, Open/Closed, Wet/Dry); the device_classes pick the primary
# binary_sensor on a multi-entity Z2M device (a PIR also reports battery, etc.).
_SECURITY_KIND: dict[str, tuple[str, frozenset[str], str]] = {
    "motion_sensor": ("motion", frozenset({"motion", "occupancy", "moving", "presence"}), "mdi:motion-sensor"),
    "contact_sensor": ("contact", frozenset({"door", "window", "opening", "garage_door"}), "mdi:door"),
    "leak_sensor": ("leak", frozenset({"moisture"}), "mdi:water-alert"),
    "vibration_sensor": ("vibration", frozenset({"vibration"}), "mdi:vibrate"),
    "smoke_sensor": ("smoke", frozenset({"smoke"}), "mdi:smoke-detector"),
}


def _zigbee2mqtt_binary_entity(
    device: dict[str, Any], prefer_classes: frozenset[str],
) -> str | None:
    """Resolve the primary `binary_sensor.*` entity for a registered Z2M
    security device. Same identifier resolution as `_zigbee2mqtt_light_entity`
    (the HA device keys on its `("mqtt","zigbee2mqtt_<ieee>")` identifier); of
    that device's binary_sensors, prefer the one whose device_class matches the
    sensor kind (a motion sensor also exposes battery-low etc.), else the first.
    Returns None when unresolved (card/tab omitted — no placeholder)."""
    from ..vendors.zigbee2mqtt import canon_ieee, zigbee2mqtt_device_ieee

    ieee = canon_ieee(
        device.get("linked_mac_address")
        or device.get("mac_address")
        or device.get("address")
        or device.get("mac")
    )
    if not ieee:
        return None
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(_dashboard_hass)
        dev_reg = dr.async_get(_dashboard_hass)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return None

    target_id: str | None = None
    for ha_dev in dev_reg.devices.values():
        if zigbee2mqtt_device_ieee(ha_dev) == ieee:
            target_id = ha_dev.id
            break
    if not target_id:
        return None

    binaries = [
        ent for ent in ent_reg.entities.values()
        if ent.device_id == target_id
        and ent.entity_id.startswith("binary_sensor.")
    ]
    if not binaries:
        return None
    for ent in binaries:
        dc = (
            getattr(ent, "device_class", None)
            or getattr(ent, "original_device_class", None)
            or ""
        )
        if dc in prefer_classes:
            return ent.entity_id
    return min(b.entity_id for b in binaries)


def _security_binary_entity(
    device: dict[str, Any], prefer_classes: frozenset[str],
) -> str | None:
    """The `binary_sensor.*` behind a registered security device, whoever owns
    it: a Zigbee2MQTT sensor, or a Shelly adopted into HA's `shelly`
    integration. The one resolver the Security tile and the security instincts
    both call, so a sensor cannot show on the tab and be missed by its alarm.
    None for any other owner, or while the entity is not registered yet."""
    if _is_zigbee2mqtt(device):
        return _zigbee2mqtt_binary_entity(device, prefer_classes)
    integration = (
        device.get("integration_type") or device.get("integration") or ""
    ).lower()
    if integration != "shelly":
        return None
    try:
        from homeassistant.helpers import entity_registry as er

        from ..vendors.shelly import shelly_binary_entity

        ent_reg = er.async_get(_dashboard_hass)
        return shelly_binary_entity(_dashboard_hass, ent_reg, device, prefer_classes)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return None


def _security_sensor_cards() -> list[dict[str, Any]]:
    """A `custom:sensa-sensor-tile` config for every registered security
    sensor (a Zigbee2MQTT motion / contact / leak / vibration sensor, or a
    Shelly smoke detector) whose HA `binary_sensor` resolves.

    Drives the conditional Habitation **Security** tab: an adopted sensor
    appears there as a read-only state tile, and surfaces the tab on its own
    (the Media-tab pattern). One tile per sensor."""
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        meta = _SECURITY_KIND.get(_device_service_class(device) or "")
        if not meta:
            continue
        kind, prefer_classes, icon = meta
        entity = _security_binary_entity(device, prefer_classes)
        if not entity:
            continue
        cards.append({
            "type": "custom:sensa-sensor-tile",
            "entity": entity,
            "name": device.get("display_name") or device.get("name") or "Sensor",
            "kind": kind,
            "icon": icon,
        })
    return cards


def _geofence_card() -> dict[str, Any] | None:
    """The Security tab's geofence map, or None on a van with no GPS of its
    own. Gated on the SAME predicate as the Geofence instinct
    (`van_gps_available`), so the map and the instinct appear together."""
    from ..const import GEOFENCE_DISTANCE_ENTITY_ID
    from ..device_tracker import van_gps_available

    if _dashboard_hass is None or not van_gps_available(_dashboard_hass):
        return None
    return {
        "type": "custom:sensa-geofence-card",
        "entity": GEOFENCE_DISTANCE_ENTITY_ID,
        **_radar_url_config(),
    }


# ===========================================================================
# OBD2 / engine tab (MeatPi WiCAN Pro)
#
# The WiCAN is owned by HA's shared `mqtt` integration (discovered via
# `vendors/wican_mqtt`), so — like Zigbee2MQTT — its entities aren't in Sensa's
# role map. We resolve the WiCAN's HA device by its `("mqtt","wican_<id>")`
# identifier (the id round-trips as the Sensa row's MAC) and read its entities,
# keying each catalogue PID to an entity by the object_id the firmware/sim
# stamps on the discovery `unique_id` (`..._<object_id>`), with the entity_id
# suffix as a fallback.
#
# Rendering is pure-compose (no new Lit card, per the Engine-tab design): a
# `sensa-card-grid` of full-width `sensa-note` section headers + `sensa-gauge`
# dial rows, with the two binary diagnostics (Check Engine / DPF regen) as
# `sensa-sensor-tile`s. Only PIDs actually present are emitted, so every vehicle
# profile (a 6-entity EV … a 32-entity Euro-6 diesel) lays out cleanly.
# ===========================================================================

# A reading with no good/bad direction (RPM, speed, pressures, …): suppress the
# safe-margin colouring so a legitimately low value (idle RPM, 0 boost) doesn't
# flash the warning/error colour. `sensa-gauge` compares `safe` (frac, or
# 1-frac when `invert`) against these *margin fractions*, so -1 never trips.
_OBD2_NEUTRAL: dict[str, float] = {"warn": -1.0, "crit": -1.0}

# Speed conversion for customer-facing dials (the WiCAN reports km/h; UK road
# speed is mph). `sensa-gauge`'s `scale` multiplies the raw reading into the
# display unit, so the dial's min/max/unit are all expressed in mph.
_KMH_TO_MPH = 0.621371

# The display-unit override merged onto the `vehicle_speed` dial spec so it
# reads mph (a capped 0-100 arc). Shared by every speed dial (the Cab Overview
# hero row + the BlackPaw Cab telemetry hero) so they can't drift apart.
_SPEED_MPH_OVERRIDE: dict[str, Any] = {
    "max": 100,
    "unit": "mph",
    "scale": _KMH_TO_MPH,
}

# object_id -> `sensa-gauge` dial spec (entity is merged in at build time).
# `min`/`max` frame the 180-degree arc; the centred value text is the real
# readout, so even an unbounded counter like the odometer stays legible. Units
# come from the entity itself. `invert: True` marks a HIGH-is-bad reading (so
# the arc warns near the top, using the gauge's default 20%/10% margins);
# omitting both `invert` and the neutral override leaves the default LOW-is-bad
# behaviour (fuel / AdBlue / SoC warn as they approach empty).
_OBD2_DIAL_SPECS: dict[str, dict[str, Any]] = {
    # --- hero row ---------------------------------------------------------
    "engine_rpm": {"name": "RPM", "min": 0, "max": 6000, "decimals": 0, **_OBD2_NEUTRAL},
    "vehicle_speed": {"name": "Speed", "min": 0, "max": 160, "decimals": 0, **_OBD2_NEUTRAL},
    "coolant_temp": {"name": "Coolant", "min": 0, "max": 130, "decimals": 0, "invert": True},
    "fuel_level": {"name": "Fuel", "min": 0, "max": 100, "decimals": 0},
    # --- engine -----------------------------------------------------------
    "engine_load": {"name": "Load", "min": 0, "max": 100, "decimals": 0, **_OBD2_NEUTRAL},
    "throttle_position": {"name": "Throttle", "min": 0, "max": 100, "decimals": 0, **_OBD2_NEUTRAL},
    "intake_air_temp": {"name": "Intake Air", "min": -10, "max": 80, "decimals": 0, **_OBD2_NEUTRAL},
    "engine_oil_temp": {"name": "Oil Temp", "min": 0, "max": 150, "decimals": 0, "invert": True},
    "control_module_voltage": {"name": "12V Rail", "min": 10, "max": 15, "decimals": 1, **_OBD2_NEUTRAL},
    "ambient_temp": {"name": "Ambient", "min": -10, "max": 45, "decimals": 0, **_OBD2_NEUTRAL},
    "run_time": {"name": "Run Time", "min": 0, "max": 7200, "decimals": 0, **_OBD2_NEUTRAL},
    # --- air & combustion -------------------------------------------------
    "maf_rate": {"name": "Mass Air Flow", "min": 0, "max": 50, "decimals": 1, **_OBD2_NEUTRAL},
    "intake_pressure": {"name": "Intake MAP", "min": 0, "max": 250, "decimals": 0, **_OBD2_NEUTRAL},
    "boost_pressure": {"name": "Boost", "min": 0, "max": 150, "decimals": 0, **_OBD2_NEUTRAL},
    "egr_commanded": {"name": "EGR", "min": 0, "max": 100, "decimals": 0, **_OBD2_NEUTRAL},
    "timing_advance": {"name": "Timing", "min": 0, "max": 30, "decimals": 0, **_OBD2_NEUTRAL},
    "lambda": {"name": "Lambda", "min": 0.8, "max": 2.6, "decimals": 2, **_OBD2_NEUTRAL},
    "stft_b1": {"name": "Short Trim", "min": -20, "max": 20, "decimals": 1, **_OBD2_NEUTRAL},
    "ltft_b1": {"name": "Long Trim", "min": -20, "max": 20, "decimals": 1, **_OBD2_NEUTRAL},
    # --- fuel & DPF -------------------------------------------------------
    "fuel_rate": {"name": "Fuel Rate", "min": 0, "max": 15, "decimals": 1, **_OBD2_NEUTRAL},
    "fuel_rail_pressure": {"name": "Rail Pressure", "min": 0, "max": 200, "decimals": 0, **_OBD2_NEUTRAL},
    "dpf_soot": {"name": "DPF Soot", "min": 0, "max": 100, "decimals": 0, "invert": True},
    "def_level": {"name": "AdBlue", "min": 0, "max": 100, "decimals": 0},
    "dpf_inlet_temp": {"name": "DPF Inlet", "min": 0, "max": 700, "decimals": 0, **_OBD2_NEUTRAL},
    "dpf_outlet_temp": {"name": "DPF Outlet", "min": 0, "max": 700, "decimals": 0, **_OBD2_NEUTRAL},
    "dpf_diff_pressure": {"name": "DPF dP", "min": 0, "max": 30, "decimals": 1, "invert": True},
    "distance_since_regen": {"name": "Since Regen", "min": 0, "max": 1000, "decimals": 0, **_OBD2_NEUTRAL},
    # --- diagnostics (numeric) -------------------------------------------
    # max 1 + invert: 0 codes reads base colour, any fault drives the arc red.
    "dtc_count": {"name": "Fault Codes", "min": 0, "max": 1, "decimals": 0, "invert": True},
    "odometer": {"name": "Odometer", "min": 0, "max": 300000, "decimals": 0, **_OBD2_NEUTRAL},
    "distance_since_codes_cleared": {"name": "Since Cleared", "min": 0, "max": 300000, "decimals": 0, **_OBD2_NEUTRAL},
    # --- electric drive (EV profile) -------------------------------------
    "traction_soc": {"name": "Traction Battery", "min": 0, "max": 100, "decimals": 0},
    "traction_battery_temp": {"name": "Battery Temp", "min": -10, "max": 60, "decimals": 0, **_OBD2_NEUTRAL},
    "ev_range": {"name": "Range", "min": 0, "max": 300, "decimals": 0, **_OBD2_NEUTRAL},
}

# The two binary diagnostics → `sensa-sensor-tile` (no `kind` → Active/Idle
# vocabulary, amber when on; the tile name + tint carry the meaning).
_OBD2_BINARY_SPECS: dict[str, dict[str, str]] = {
    "mil_on": {"name": "Check Engine", "icon": "mdi:engine-outline"},
    "dpf_regen_active": {"name": "DPF Regenerating", "icon": "mdi:air-filter"},
}

# Gauge sections, in display order. An empty label is the hero row (no header).
# Diagnostics is built separately (it mixes binary tiles + numeric gauges).
_OBD2_SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("", "", ("engine_rpm", "vehicle_speed", "coolant_temp", "fuel_level")),
    ("Engine", "mdi:engine", (
        "engine_load", "throttle_position", "intake_air_temp", "engine_oil_temp",
        "control_module_voltage", "ambient_temp", "run_time",
    )),
    ("Air & Combustion", "mdi:weather-windy", (
        "maf_rate", "intake_pressure", "boost_pressure", "egr_commanded",
        "timing_advance", "lambda", "stft_b1", "ltft_b1",
    )),
    ("Fuel & DPF", "mdi:fuel", (
        "fuel_rate", "fuel_rail_pressure", "dpf_soot", "def_level",
        "dpf_inlet_temp", "dpf_outlet_temp", "dpf_diff_pressure", "distance_since_regen",
    )),
    ("Electric Drive", "mdi:car-electric", (
        "traction_soc", "traction_battery_temp", "ev_range",
    )),
)

# The Engine tab's HERO row (RPM / Speed / Coolant / Fuel) gets larger dials than
# the default 150px cap, so the four headline readouts fill the wide cab screen
# instead of huddling in the middle. Proportional (`min(px, cqw)`) so they scale
# with the tab width and cap on an ultra-wide kiosk, matching the BlackPaw hero
# pattern; only the four-dial hero row uses it (the denser sections below stay
# compact so many gauges still fit a row).
_OBD2_HERO_DIAL_MAX = "min(240px, 23cqw)"


def _wican_obd2_entity_map(device: dict[str, Any]) -> dict[str, str]:
    """Map each known OBD2 object_id to the WiCAN's HA entity_id.

    Resolves the WiCAN's HA device by its `("mqtt","wican_<id>")` identifier —
    matched against the Sensa row's MAC (the same id the discovery source
    round-tripped) — then keys each of that device's entities by the object_id
    the firmware/sim stamps on the discovery `unique_id` (`..._<object_id>`),
    with the entity_id suffix as a fallback (HA derives the entity_id from the
    PID *name*, so e.g. `coolant_temp` only matches via the unique_id). Returns
    `{}` when unresolved (the tab then shows its native empty state).
    """
    mac = _canon_mac(
        device.get("linked_mac_address")
        or device.get("mac_address")
        or device.get("address")
        or device.get("mac")
    )
    if not mac:
        return {}
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        from ..vendors.wican_mqtt import wican_device_id

        ent_reg = er.async_get(_dashboard_hass)
        dev_reg = dr.async_get(_dashboard_hass)
    except Exception:  # noqa: BLE001 - best-effort lookup, the dashboard builds without it
        return {}

    target_id: str | None = None
    for ha_dev in dev_reg.devices.values():
        wid = wican_device_id(ha_dev)
        if wid and _canon_mac(wid) == mac:
            target_id = ha_dev.id
            break
    if not target_id:
        return {}

    known = set(_OBD2_DIAL_SPECS) | set(_OBD2_BINARY_SPECS)
    result: dict[str, str] = {}
    for ent in ent_reg.entities.values():
        if getattr(ent, "device_id", None) != target_id:
            continue
        uid = str(getattr(ent, "unique_id", "") or "").lower()
        eid = str(getattr(ent, "entity_id", "") or "").lower()
        for oid in known:
            if oid in result:
                continue
            if uid.endswith("_" + oid) or eid.endswith("_" + oid):
                result[oid] = ent.entity_id
                break
    return result


def _obd2_engine_cards(device: dict[str, Any]) -> list[dict[str, Any]]:
    """Card configs for one OBD2 device's Engine-tab readouts (or `[]`).

    Composes existing native cards only (no Lovelace engine): full-width
    `sensa-note` section headers + `sensa-gauge` dial rows, plus the binary
    diagnostics as `sensa-sensor-tile`s. Each section is emitted only when at
    least one of its PIDs resolved, so any vehicle profile lays out cleanly.
    """
    emap = _wican_obd2_entity_map(device)
    if not emap:
        return []
    cards: list[dict[str, Any]] = []
    for label, icon, oids in _OBD2_SECTIONS:
        gauges = [
            {"entity": emap[oid], **_OBD2_DIAL_SPECS.get(oid, {})}
            for oid in oids if oid in emap
        ]
        if not gauges:
            continue
        if label:
            cards.append({
                "type": "custom:sensa-note", "icon": icon,
                "content": f"**{label}**", "full_width": True,
            })
        gauge_card: dict[str, Any] = {
            "type": "custom:sensa-gauge", "gauges": gauges, "full_width": True,
        }
        # The empty-label section is the hero row (RPM / Speed / Coolant / Fuel):
        # bigger dials than the dense sections below it.
        if not label:
            gauge_card["dial_max_width"] = _OBD2_HERO_DIAL_MAX
        cards.append(gauge_card)

    # Diagnostics: the two binary tiles (2-up) then any numeric counters.
    diag_tiles = [
        {
            "type": "custom:sensa-sensor-tile", "entity": emap[oid],
            "name": meta["name"], "icon": meta["icon"],
        }
        for oid, meta in _OBD2_BINARY_SPECS.items() if oid in emap
    ]
    diag_gauges = [
        {"entity": emap[oid], **_OBD2_DIAL_SPECS.get(oid, {})}
        for oid in ("dtc_count", "odometer", "distance_since_codes_cleared")
        if oid in emap
    ]
    if diag_tiles or diag_gauges:
        cards.append({
            "type": "custom:sensa-note", "icon": "mdi:car-wrench",
            "content": "**Diagnostics**", "full_width": True,
        })
        cards.extend(diag_tiles)
        if diag_gauges:
            cards.append({"type": "custom:sensa-gauge", "gauges": diag_gauges, "full_width": True})
    return cards


def _obd2_overview_gauge_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """The hero dial row (RPM / Speed / Coolant / Fuel) for the Cab → Overview
    tab — the same four dials that lead the Engine tab, but with **speed in
    mph** (the WiCAN reports km/h; `sensa-gauge`'s `scale` converts it).

    A native `custom:sensa-gauge` card (no Lovelace engine), full-width inside
    the Overview grid. Returns None when the WiCAN's entities don't resolve, so
    the Overview tab simply omits it (no placeholder)."""
    emap = _wican_obd2_entity_map(device)
    if not emap:
        return None
    gauges: list[dict[str, Any]] = []
    # Fixed order — Speed, RPM, Coolant, Fuel — must hold at EVERY screen width /
    # orientation. The gauge row is `flex-wrap` (preserves DOM order; no `order:`
    # rule), so this list order is the visual order even when the dials wrap.
    for oid in ("vehicle_speed", "engine_rpm", "coolant_temp", "fuel_level"):
        eid = emap.get(oid)
        if not eid:
            continue
        spec = dict(_OBD2_DIAL_SPECS.get(oid, {}))
        if oid == "vehicle_speed":
            # km/h -> mph: convert in the card, so max/unit are display-unit.
            spec.update(_SPEED_MPH_OVERRIDE)
        gauges.append({"entity": eid, **spec})
    if not gauges:
        return None
    return {"type": "custom:sensa-gauge", "gauges": gauges, "full_width": True}


# Fields the BlackPaw hero dials copy from `_OBD2_DIAL_SPECS` onto a
# `sensa-gauge-detail` gauge. `name` is deliberately EXCLUDED (the hero dials
# carry no title — the unit inside the arc identifies them). The card's colour
# band is controlled by `band` (not the half-circle `sensa-gauge`'s
# `invert`/`warn`/`crit`), so those direction keys are dropped; `offset` is kept
# so an affine display-unit conversion (degF -> degC) isn't silently lost.
_GAUGE_DETAIL_FIELDS = ("min", "max", "decimals", "unit", "scale", "offset", "band")


# Per-dial max widths for the BlackPaw telemetry column. PROPORTIONAL sizing
# (`min(<px>, <cqw>)`) so the hero:secondary ratio (200:120 = 1.67) holds as the
# column narrows — a fixed-px cap made only the hero shrink, inverting the
# hierarchy at narrow widths (real-Chrome-measured). The `cqw` percentages share
# the ratio (45:27 = 1.67) AND hit their px caps at the same column width
# (200/0.45 == 120/0.27 == 444px), so the ratio is exact at every width.
_BLACKPAW_HERO_DIAL = "min(200px, 45cqw)"
_BLACKPAW_SMALL_DIAL = "min(120px, 27cqw)"

# Floor for the BlackPaw fill map so it stays usable when a narrow viewport
# folds the two columns into one, or on a very short viewport in the no-OBD
# full-width shape (a fill map has no intrinsic height and would collapse).
_BLACKPAW_NAV_MIN_HEIGHT = "360px"

# Floor for the Cab Navigation tab's fill map. That tab is the map and nothing
# else, so it takes the full tab height; this only guards a very short viewport
# (a fill map has no intrinsic height and would otherwise collapse). Same value
# as BlackPaw's, one usable-map floor, no reason for two.
_CAB_NAV_MIN_HEIGHT = _BLACKPAW_NAV_MIN_HEIGHT

# BlackPaw outer-grid column split: 35% telemetry (left) / 65% map (right).
# `sensa-card-grid` collapses this to a single stacked column below its 640px
# breakpoint (map folds under the gauges on a narrow screen).
_BLACKPAW_COLUMNS = "35fr 65fr"
# When the left column holds ONLY the GPS speedometer (no OBD2, nothing placed),
# 35% is a lot of screen for one dial. A narrow column, with the dial filling
# it (a touch bigger than the OBD2 hero, being alone), hands the rest to the map.
_BLACKPAW_GPS_ONLY_COLUMNS = "24fr 76fr"
_BLACKPAW_GPS_ONLY_DIAL = "min(280px, 100cqw)"

# "Default (Portrait)" outer grid: ONE column, two rows — the OBD2 dials at
# their natural height on top, the navigation map taking everything left over.
# `columns` is required for `rows` to apply (the default body is an auto-fit
# grid whose column count moves with width).
#
# The map row's floor is the map's OWN `min_height`, not a second number: a grid
# item is sized by its track, so `minmax(0, 1fr)` let a short panel squash the
# row to 220px while the map's `min-height` sat uselessly inside it (measured in
# real Chrome — `portrait-stack-probe.mjs`, which is why that probe exists).
# Below the floor the grid grows past the panel and the panel body scrolls,
# which is the right answer: a 220px map strip is not a map.
_PORTRAIT_COLUMNS = "1fr"
_PORTRAIT_ROWS = f"auto minmax({_BLACKPAW_NAV_MIN_HEIGHT}, 1fr)"


def _cab_alternator_dial() -> dict[str, Any] | None:
    """A charging-power dial for the alternator / DC-DC charger (the Victron
    Orion XS `output_power`, in W), for the BlackPaw Cab telemetry.

    Returns None when no alternator resolves (`resolve_role("alternator")`), so
    the dial appears ONLY when an alternator device is added. Charging power has
    no low=bad direction, so the neutral margins suppress the red band (0 W = not
    charging, not an alarm).
    """
    alt = resolve_role("alternator")
    if isinstance(alt, list):
        eid = alt[0] if alt else None
    else:
        eid = alt
    if not eid:
        return None
    return {
        "entity": eid,
        "name": "Alternator",
        "min": 0,
        "max": 700,
        "unit": "W",
        "decimals": 0,
        **_OBD2_NEUTRAL,
    }


# Half-circle dials on the LANDSCAPE telemetry column, in row order.
_CAB_SMALL_DIAL_OIDS = ("coolant_temp", "fuel_level", "dpf_soot")

# PORTRAIT moves the two percentage readings off the dial row and onto bars
# beneath it (Richard, 2026-08-17), so coolant is the only half-circle dial left
# (the alternator is appended after, when one resolves). Fuel and DPF soot are
# both genuinely 0-100% proportions, which is exactly what a bar states well;
# on a portrait screen the height a dial costs is height the map loses.
_PORTRAIT_SMALL_DIAL_OIDS = ("coolant_temp",)
_PORTRAIT_BAR_OIDS = ("fuel_level", "dpf_soot")


def _cab_obd2_emap() -> dict[str, str]:
    """The registered WiCAN's `object_id -> entity_id` map, or `{}`.

    One lookup shared by the dial and bar spec builders, so the two can never
    disagree about which device is the OBD2 adapter.
    """
    device = next(
        (d for d in _dashboard_devices if _is_obd2_device(d)), None
    )
    if device is None:
        return {}
    return _wican_obd2_entity_map(device) or {}


def _cab_obd2_bar_specs(
    oids: tuple[str, ...] = _PORTRAIT_BAR_OIDS,
) -> list[dict[str, Any]]:
    """`custom:sensa-percent-bar` specs for the 0-100% OBD2 readings.

    Reuses `_OBD2_DIAL_SPECS` unchanged: the bar card shares the gauge card's
    field vocabulary (`min`/`max`/`unit`/`decimals`/`invert`/`warn`/`crit`) for
    exactly this reason, so a reading warns at the same number whether it is
    drawn as an arc or a bar. Fuel warns near empty; DPF soot carries
    `invert: True` and warns near full.
    """
    emap = _cab_obd2_emap()
    return [
        {"entity": emap[oid], **_OBD2_DIAL_SPECS.get(oid, {})}
        for oid in oids
        if emap.get(oid)
    ]


def _cab_gps_speed_dial() -> dict[str, Any] | None:
    """The GPS speedometer for the Cab Home hero slot, or None.

    Used only where OBD2 road speed is absent (the adapter's own speedo wins).
    Gated on the van having a GPS source at all (`movement_source_available`),
    so a van with neither gets no dead dial and keeps its full-width map. The
    sensor already reads mph, so the spec is the OBD2 speedo's minus the km/h
    scale.
    """
    from ..const import GPS_SPEED_SENSOR_ENTITY_ID
    from ..movement import movement_source_available

    if _dashboard_hass is None or not movement_source_available(_dashboard_hass):
        return None
    spec = {
        k: v for k, v in _SPEED_MPH_OVERRIDE.items() if k != "scale"
    }
    return {
        "entity": GPS_SPEED_SENSOR_ENTITY_ID,
        "min": 0,
        "decimals": 0,
        "band": False,
        **spec,
    }


def _cab_obd2_dial_sets(
    small_oids: tuple[str, ...] = _CAB_SMALL_DIAL_OIDS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The WiCAN vehicle-vitals dial specs, as `(hero, small, odometer)`.

    Specs only — no card types, no sizing, no layout. The two Cab home layouts
    compose these differently, and this is the single place that decides what
    each instrument reads. Splitting it out beat threading layout arguments
    through one builder: what a dial reads is the part that must not be written
    twice, the arrangement is the part that legitimately differs.

    `hero` = speed + rpm (needle speedometers, no titles). `small` =
    `small_oids` plus the alternator when one resolves — portrait passes a
    shorter tuple because fuel and DPF soot become bars there. `odometer` = the
    single digital readout row, `[]` when the entity is absent.

    With no OBD2 road speed, `hero` carries the GPS speedometer instead
    (`_cab_gps_speed_dial`) when the van has a GPS source, so a van without an
    adapter still gets a speedo. Otherwise all three are `[]` when no OBD2
    device is registered or its entities don't resolve, so a caller simply
    omits the vitals.
    """
    emap = _cab_obd2_emap()
    if not emap:
        gps = _cab_gps_speed_dial()
        return ([gps] if gps else []), [], []

    hero: list[dict[str, Any]] = []
    for oid in ("vehicle_speed", "engine_rpm"):
        eid = emap.get(oid)
        if not eid:
            continue
        base = _OBD2_DIAL_SPECS.get(oid, {})
        # `_GAUGE_DETAIL_FIELDS` excludes `name` on purpose — the hero dials
        # carry no title (the unit inside the arc identifies them).
        spec = {k: base[k] for k in _GAUGE_DETAIL_FIELDS if k in base}
        # Speed + rpm are directionless: turn off the low=red band so a parked
        # van / idle engine doesn't read as an alarm (the neutral-margin story
        # the half-circle OBD dials get from `_OBD2_NEUTRAL`).
        spec["band"] = False
        if oid == "vehicle_speed":
            # km/h -> mph: convert in the card, so max/unit are display-unit.
            spec.update(_SPEED_MPH_OVERRIDE)
        hero.append({"entity": eid, **spec})
    if not emap.get("vehicle_speed"):
        gps = _cab_gps_speed_dial()
        if gps:
            hero.insert(0, gps)

    small: list[dict[str, Any]] = []
    # DPF soot rides with the vitals rather than on a row of its own: it is a
    # 0-100% dial like the others, and one lone dial on its own row read as a
    # stray. `invert` makes a HIGH reading the bad one. (Portrait passes a
    # shorter `small_oids`, having moved the percentage readings to bars.)
    for oid in small_oids:
        eid = emap.get(oid)
        if not eid:
            continue
        small.append({"entity": eid, **_OBD2_DIAL_SPECS.get(oid, {})})
    # Alternator charging power — only when an alternator / DC-DC charger is
    # registered (rides beside coolant / fuel as a secondary dial).
    alt_dial = _cab_alternator_dial()
    if alt_dial is not None:
        small.append(alt_dial)

    # The ODOMETER sits under the dials as a digital readout, not a dial. A gauge
    # implies a range with a good end and a bad end; an odometer just counts up,
    # so a needle at "80,000 of 300,000" says something untrue. Gated on its
    # entity and the card omitted when empty, so nobody gets a box with nothing
    # in it.
    odometer: list[dict[str, Any]] = (
        [{
            "entity": emap["odometer"],
            # No `name` and no `icon` (2026-08-01, Richard): a row of digit reels
            # in a drum housing already says "odometer", so a label and a counter
            # icon beside it only spent width restating that. The card renders
            # each only when given one, so omitting them here is the whole
            # change; other surfaces that want a label still pass one.
            "decimals": 0,
            # OBD2 reports the odometer in km. The customer's Miles/Kilometres
            # choice is `select.sensa_nomad_nav_units`, so follow it -- a van set
            # to Miles showing a km odometer beside a miles nearby-list reads as
            # a data fault. Deliberately NO baked scale: the card resolves the
            # entity's live unit against the live setting at RENDER time, so
            # flipping the setting takes effect without a dashboard regen. See
            # webcards/src/shared/distance.ts.
            "convert": "distance",
        }]
        if "odometer" in emap
        else []
    )
    return hero, small, odometer


def _cab_blackpaw_telemetry_cards() -> list[dict[str, Any]]:
    """The BlackPaw Cab Home telemetry cards: WiCAN vehicle vitals, STACKED.

    A speed + rpm HERO card (larger `custom:sensa-gauge-detail` needle
    speedometers, speed converted km/h -> mph, NO dial titles — the unit reads
    inside the arc) over a smaller coolant + fuel card (compact
    `custom:sensa-gauge` half-circle dials), then the odometer readout. All are
    `full_width: True` so they stack vertically inside the left column's grid
    (`.cell.full` spans every column).

    Which dials exist is `_cab_obd2_dial_sets`; this only arranges them. The
    portrait layout arranges the same specs as one row
    (`_cab_portrait_telemetry_row`).

    Returns `[]` when no OBD2 device is registered or its entities don't
    resolve, so the caller simply omits the vitals.
    """
    hero, small, odometer = _cab_obd2_dial_sets()
    cards: list[dict[str, Any]] = []
    if hero:
        cards.append({
            "type": "custom:sensa-gauge-detail",
            "gauges": hero,
            "dial_max_width": _BLACKPAW_HERO_DIAL,
            "full_width": True,
            # A card of its own on Cab Home, where Design Mode lifts each one
            # into the tab's grid; bare dials there read as loose.
            "card": True,
        })
    if small:
        cards.append({
            "type": "custom:sensa-gauge",
            "gauges": small,
            "dial_max_width": _BLACKPAW_SMALL_DIAL,
            "full_width": True,
            "card": True,
        })
    if odometer:
        cards.append({
            "type": "custom:sensa-readout",
            "rows": odometer,
            # The one units setting for the surface; the rows differ only in
            # whether they follow it (engine hours would not).
            "units_entity": "select.sensa_nomad_nav_units",
            "full_width": True,
        })
    return cards


def _fill_dial_width(count: int, gap_px: int) -> str:
    """`dial_max_width` that makes `count` dials fill their card's full width.

    Both gauge cards lay their dials out as `flex: 1 1 0` capped by
    `--dial-max`, so a dial GROWS to its share of the row and then stops at the
    cap. Sizing the cap to the exact share is therefore what makes a row fill
    edge to edge instead of centring a short group of small dials.

    `100cqw` is the card's own width (each gauge card is a
    `container-type: inline-size` container), less the `count - 1` inter-dial
    gaps, divided by the dials. `gap_px` differs per card type (16 on
    `sensa-gauge-detail`, 12 on `sensa-gauge`), so it is passed rather than
    assumed.

    Deliberately UNCAPPED. A `min(<px>, …)` ceiling would make the dials stop
    growing on a wide screen and each card would then centre its own group
    inside its column, opening a large uneven gap in the middle of the row.
    Portrait is a narrow surface by definition, so filling is the right
    behaviour at every width it actually sees.
    """
    if count <= 1:
        return "100cqw"
    return f"calc((100cqw - {(count - 1) * gap_px}px) / {count})"


# Inter-instrument gaps, matching each card's own `.row { gap }`. Duplicated
# from CSS because the fill maths needs the number; if any of these gaps
# changes, `portrait-stack-probe.mjs` measures unequal instruments and fails.
_GAUGE_DETAIL_ROW_GAP = 16
_GAUGE_ROW_GAP = 12
_PERCENT_BAR_ROW_GAP = 12
# `sensa-card-grid`'s own `.body { gap }`, i.e. the space BETWEEN two cards
# sharing a row. Needed by the same maths, for the same reason.
_CARD_GRID_GAP = 16
# A readout is one instrument with nothing beside it, so it has no inter-
# instrument gap of its own. Named rather than a bare 0 at the call site.
_READOUT_ROW_GAP = 0


def _fill_row(
    entries: list[tuple[dict[str, Any], int, int]],
) -> dict[str, Any] | None:
    """Lay cards out as ONE full-width row where every INSTRUMENT is the same
    width, whichever card holds it.

    `entries` are `(card, instrument_count, that card's inter-instrument gap)`.

    Plain `<count>fr` weights are the obvious approach and they are WRONG by a
    few pixels, because `fr` splits the leftover space in proportion to the
    counts while each card also spends its own internal gaps out of its share.
    A card holding two instruments pays one internal gap; a card holding one
    pays none, so the single instrument comes out wider. Measured: a fuel/DPF
    bar pair beside the mileage went 247 / 247 / 253.

    So the tracks are solved rather than weighted. With `n` instruments total,
    `k` cards and every gap known, the shared instrument width is

        d = (100% - outer gaps - internal gaps) / n

    and a card holding `n_i` of them takes `n_i * d + (n_i - 1) * gap_i`. Each
    card's own `--dial-max` / `--bar-max` fill maths then resolves to the same
    `d`, so the two agree by construction instead of by luck.

    Two more things here are load-bearing rather than tidy:

    * a LONE card still needs `full_width`, because `sensa-card-grid` treats a
      single cell as `.body.single` and caps it at 480px centred — which would
      shrink a full-width row to half the screen;
    * `hold_columns` moves the grid's column fold from 640px to 400px. Without
      it a narrowing screen FOLDS the row into two lines rather than shrinking
      the instruments, which is the exact wrap this row exists to avoid —
      measured at 158px-wide dials, with room to spare. Below 400px it folds
      and wraps, which is the right answer on a phone.

    Returns None for an empty list, so a caller can drop the row entirely.
    """
    if not entries:
        return None
    if len(entries) == 1:
        return {**entries[0][0], "full_width": True}
    total = sum(n for _, n, _ in entries)
    internal = sum((n - 1) * gap for _, n, gap in entries)
    fixed = (len(entries) - 1) * _CARD_GRID_GAP + internal
    # The width one instrument gets, as a CSS length the browser resolves
    # against the real row width.
    unit = f"((100% - {fixed}px) / {total})"
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "full_width": True,
        "hold_columns": True,
        "columns": " ".join(
            f"calc({n} * {unit} + {(n - 1) * gap}px)" for _, n, gap in entries
        ),
        "cards": [card for card, _, _ in entries],
    }


def _cab_portrait_telemetry_cards() -> list[dict[str, Any]]:
    """The Cab "Default (Portrait)" vitals: a dial row over a bar row.

    Arranged for a TALL screen (Richard, 2026-08-17), in two full-width rows:

    1. **Dials** — speed + rpm as needle speedometers, then coolant and the
       alternator as half-circles. Each is sized to fill its share of the
       width, so the row spans edge to edge instead of centring a short group.
    2. **Bars + mileage** — fuel and DPF soot as 0-100%
       `custom:sensa-percent-bar` bars beside the odometer readout. Both
       readings are genuinely proportions of a whole, which is what a bar
       states well, and a bar costs a fraction of a dial's height — height that
       goes to the map on this layout.

    Each row is two or three CARDS side by side rather than one, because the
    instrument styles differ. That joins seamlessly because none of these cards
    draws an `ha-card`: gauge, bar and readout all render a bare flex row on a
    `display: block` host, so no chrome, border or padding gives away the join.

    Column weights are instrument COUNTS (`_fill_row`), computed from what
    actually resolved rather than fixed, because the alternator, DPF soot and
    the odometer are all conditional — the rows are anywhere from 1 to 4 wide.

    Returns `[]` when no OBD2 device is registered or its entities don't
    resolve, so the caller drops the telemetry entirely.
    """
    hero, small, odometer = _cab_obd2_dial_sets(
        small_oids=_PORTRAIT_SMALL_DIAL_OIDS,
    )
    bars = _cab_obd2_bar_specs()

    # Each entry is (card, instrument count, that card's inter-instrument gap),
    # which is everything `_fill_row` needs to solve for one shared width.
    dials: list[tuple[dict[str, Any], int, int]] = []
    if hero:
        dials.append(({
            "type": "custom:sensa-gauge-detail",
            "gauges": hero,
            "dial_max_width": _fill_dial_width(
                len(hero), _GAUGE_DETAIL_ROW_GAP,
            ),
        }, len(hero), _GAUGE_DETAIL_ROW_GAP))
    if small:
        dials.append(({
            "type": "custom:sensa-gauge",
            "gauges": small,
            "dial_max_width": _fill_dial_width(len(small), _GAUGE_ROW_GAP),
        }, len(small), _GAUGE_ROW_GAP))

    strip: list[tuple[dict[str, Any], int, int]] = []
    if bars:
        strip.append(({
            "type": "custom:sensa-percent-bar",
            "bars": bars,
            "bar_max_width": _fill_dial_width(len(bars), _PERCENT_BAR_ROW_GAP),
        }, len(bars), _PERCENT_BAR_ROW_GAP))
    if odometer:
        strip.append(({
            "type": "custom:sensa-readout",
            "rows": odometer,
            "units_entity": "select.sensa_nomad_nav_units",
        }, 1, _READOUT_ROW_GAP))

    rows = [_fill_row(dials), _fill_row(strip)]
    return [r for r in rows if r is not None]


def _home_heater_summary_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Compact `custom:sensa-climate-summary` card for ONE heater — air
    (Autoterm Air D) or hydronic (Alde / Baltica) — or None when its service
    class isn't a heater OR its role map has no usable entity yet.

    Reuses the heater's role map (the same `service_entities` contract the full
    `sensa-autoterm-heater-card` consumes). Title = the device's own name, matching the
    detailed Climate-tab card (`_build_heater_card`); icon keys off the heater
    type. The detailed, device-named control card stays on the Habitation
    Climate tab (`_heater_cards`) — this is the compact Home surface.
    """
    from ..const import HEATER_SERVICE_CLASSES
    service_class = _device_service_class(device)
    if service_class not in HEATER_SERVICE_CLASSES:
        return None
    role_map = _device_role_map(device)
    icon = "mdi:radiator" if service_class == "hydronic_heater" else "mdi:fire"
    name = device.get("display_name") or device.get("name") or "Heater"
    card: dict[str, Any] = {
        "type": "custom:sensa-climate-summary",
        "name": name,
        "icon": icon,
    }
    # Brand strip under the mode buttons, same as the full card
    # (`_heater_brand`): the maker's name, no logo (Richard, 2026-07-25 and
    # 2026-10-08). The card stays otherwise generic: no product image.
    brand = _heater_brand(device)
    if brand:
        card["brand"] = brand
    # Only the fields the compact summary uses (no power-level / voltage /
    # diagnostics — those live on the full Climate-tab card). No image.
    has_entity = False
    for role_key, field in (
        ("power", "power_entity"),
        ("current_temperature", "current_temp_entity"),
        ("target_temperature", "target_temp_entity"),
        ("mode", "mode_entity"),
        ("run_state", "status_entity"),
        ("vent_available", "vent_available_entity"),
    ):
        eid = role_map.get(role_key)
        if eid:
            card[field] = eid
            has_entity = True
    # "Now" follows the Temperature Sensor Source, same as the full card.
    current = _heater_current_temp_entity(device, role_map)
    if current:
        card["current_temp_entity"] = current
    return card if has_entity else None


def _zigbee2mqtt_light_parts(
    device: dict[str, Any],
) -> tuple[str, str, str] | None:
    """Resolve a Z2M lighting device's `(light, scene, work_mode)` entities, or
    None when it has no `light.*` entity (not a Z2M light, or the mqtt entity
    hasn't registered yet).

    `scene` / `work_mode` are `""` for a plain driver (only an addressable
    controller publishes a `select.*_scene`). ONE resolver, shared by the
    Lighting-tab card and the compact Home card, so a change to how the
    addressable controller's exposes are named can't fix one surface and leave
    the other silently driving the wrong entity.
    """
    device_id = _zigbee2mqtt_device_id(device)
    if not device_id:
        return None
    entity = _zigbee2mqtt_device_entity(device_id, "light")
    if not entity:
        return None
    scene = _zigbee2mqtt_device_entity(device_id, "select", "_scene") or ""
    work_mode = (
        _zigbee2mqtt_device_entity(device_id, "select", "_work_mode") or ""
        if scene else ""
    )
    return entity, scene, work_mode


def _build_home_light_summary(device: dict[str, Any]) -> dict[str, Any] | None:
    """Compact `custom:sensa-light-summary` for one lighting device, or None when
    its `light.*` entity can't be resolved yet.

    The Home layout gets a CONDENSED card, not the Lighting tab's full one
    (Richard, 2026-07-09: "on the home page we don't need as many features and we
    need to use less screen space"). Same relationship `sensa-climate-summary`
    has to the full heater / AC cards. Both controllers share one component:

    * a plain colour/white driver (Skydance WZ5) → header + brightness;
    * an addressable / pixel controller (WZ-SPI, which publishes a Z2M
      `select.*_scene` expose) → the same, plus a compact effect picker.

    The full `sensa-light-card` / `sensa-led-card` (dial, colour mixer, effect
    gallery, strip config) stay on the Habitation Lighting tab, untouched.

    A Shelly light controller gets the plain card for its first output; one
    in four-channel "light" profile shows only channel 1 on Home, the other
    three are on the Lighting tab.
    """
    if _is_shelly(device):
        shelly_parts = _shelly_light_parts(device)
        if not shelly_parts:
            return None
        entity, name = shelly_parts[0]
        return {
            "type": "custom:sensa-light-summary",
            "entity": entity,
            "name": name,
            "icon": "mdi:led-strip-variant",
        }
    parts = _zigbee2mqtt_light_parts(device)
    if parts is None:
        return None
    entity, scene_entity, work_mode = parts
    # Deliberately NOT wired with `light_type_entity`: this card renders power,
    # brightness and (for an addressable controller) effects, and every WZ5 mode
    # has a brightness channel. There is no colour mixer or colour-temp slider
    # here for a Light Type to hide, so passing it would be dead config.
    card: dict[str, Any] = {
        "type": "custom:sensa-light-summary",
        "entity": entity,
        "name": device.get("display_name") or device.get("name") or "Light",
        # Addressable strips read as a strip, not a bulb.
        "icon": "mdi:led-strip-variant" if scene_entity else "mdi:lightbulb",
    }
    if scene_entity:
        card["scene_entity"] = scene_entity
        # The controller only ANIMATES a scene while its work mode is `scene`,
        # so the card must set that too (as `sensa-led-card` does) or picking an
        # effect on Home silently does nothing.
        if work_mode:
            card["work_mode_entity"] = work_mode
    # A relay channel assigned to this controller as an Integrated Device lets
    # the compact card fold the channel's mains/12V switch into its own power
    # button, exactly as the full Lighting-tab cards do. WITHOUT this a
    # relay-gated controller sitting in standby has an `unavailable` light
    # entity, so its Home power button would be dead and the customer could not
    # wake it from Home at all (code-review catch, 2026-07-09).
    if device.get("power_relay"):
        card["power_relay"] = device["power_relay"]
        # Same Power-On Delay entity the full card reads, so Home and the
        # Lighting tab wait exactly as long as each other.
        from ..const import (
            light_leave_relay_on_switch_entity_id,
            light_power_on_delay_number_entity_id,
        )

        ha_device_id = _zigbee2mqtt_device_id(device)
        if ha_device_id:
            card["power_delay_entity"] = light_power_on_delay_number_entity_id(
                ha_device_id,
            )
            card["leave_relay_on_entity"] = (
                light_leave_relay_on_switch_entity_id(ha_device_id)
            )
    return card


def _home_ac_summary_card(device: dict[str, Any]) -> dict[str, Any] | None:
    """Compact `custom:sensa-climate-summary` for ONE air conditioner, or None
    when the device isn't an AC OR its `climate.*` entity hasn't resolved yet.

    Reads the AC's single `climate.*` entity (the same one the full
    `sensa-air-conditioner-card` uses), so the compact summary drives it with
    standard `climate.*` services. Resolves the role-map (ESPHome FreshJet)
    entity first, falling back to the Velit pin. Title = the device's own name
    (matching the heater summaries + the detailed Climate-tab card). No brand
    image. The detailed card stays on the Climate tab.
    """
    if _device_service_class(device) != "air_conditioner":
        return None
    entity = _ac_climate_entity_id(device)
    if not entity:
        # ESPHome AC not resolved yet — bind to nothing rather than a
        # non-existent velit entity (code-review finding [3]).
        return None
    return {
        "type": "custom:sensa-climate-summary",
        "entity": entity,
        "name": _ac_display_name(device),
        "icon": "mdi:air-conditioner",
        **_ac_light_entities(device),
    }


def _home_placement_card_for(device: dict[str, Any]) -> dict[str, Any] | None:
    """The compact Home card for an eligible device (heater / AC / light), or
    None when the device has no Home card or its entities aren't resolved yet.
    ONE dispatcher so a device's card is identical whichever surface/column it
    lands in."""
    service_class = _device_service_class(device)
    from ..const import HEATER_SERVICE_CLASSES, LEVELLING_SERVICE_CLASSES
    if service_class in HEATER_SERVICE_CLASSES:
        return _home_heater_summary_card(device)
    if service_class == "air_conditioner":
        return _home_ac_summary_card(device)
    if service_class == "lighting":
        return _build_home_light_summary(device)
    if service_class in LEVELLING_SERVICE_CLASSES:
        # The COMPACT gyro on every surface, keeping this dispatcher
        # surface-agnostic. The Cab PARKING tab still has its own big park-up
        # gyro (`_build_cab_levelling_gyro_card`) -- that is a different screen
        # and is untouched.
        return _home_levelling_gyro_card_for(device)
    return None


def _home_placement_columns(surface_key: str) -> dict[str, list[dict[str, Any]]]:
    """Compact Home cards bucketed by the customer's chosen column for one
    surface: ``{"left": [...], "middle": [...], "right": [...]}``.

    Reads each device's per-surface `home_placement` (stamped in `__init__` from
    `entry.options["home_layout_placement_<surface>"]`, the ledger the per-device
    "<Surface> Home Layout" select writes). A device with no choice for this
    surface (or "Off") contributes nothing. A chosen device whose card can't be
    built yet (Z2M light entity not registered, ESPHome AC unresolved) is
    skipped until the adoption rebuild fills it in.

    The placement select is offered for every eligible device regardless of the
    active layout, so EVERY layout with a column must render these or the select
    is a dead control (code-review lesson from the boolean toggle).

    Within a column the cards are ordered by the customer's per-device sort
    ORDER (the "<Surface> Home Order" number), lowest first. The sort is STABLE
    and keys on the order ALONE, so devices left at the default keep exactly the
    order they had before this feature existed — adding a sort control must not
    silently rearrange the Home screen of a customer who never uses it.
    """
    from ..const import HOME_LAYOUT_ORDER_DEFAULT

    columns: dict[str, list[tuple[int, dict[str, Any]]]] = {
        "left": [], "middle": [], "right": [],
    }
    for device in _dashboard_devices:
        column = (device.get("home_placement") or {}).get(surface_key)
        if column not in columns:
            continue
        card = _home_placement_card_for(device)
        if card is None:
            continue
        device_id = device.get("device_id") or device.get("id")
        if surface_key == "hab" and device_id:
            # A key per device, so the customer's Home arrangement follows the
            # device, not its position among cards of the same kind. Not on
            # Cab, whose layouts were saved against the older keys.
            card = {**card, "reorder_key": f"home:{device_id}"}
        order = (device.get("home_order") or {}).get(surface_key)
        try:
            order = int(order)
        except (TypeError, ValueError):
            order = HOME_LAYOUT_ORDER_DEFAULT
        columns[column].append((order, card))
    return {
        key: [card for _order, card in sorted(rows, key=lambda r: r[0])]
        for key, rows in columns.items()
    }


def _place_home_cards(
    card: dict[str, Any], slot: str, cards: list[dict[str, Any]],
) -> None:
    """Pack `cards` into the shell `slot`.

    0 cards → no slot (the column collapses). 1 card → the card directly in the
    slot (byte-identical to the old single-card behaviour). N>1 → all cards
    wrapped in a frame-less `custom:sensa-card-grid`, each `full_width` so the
    narrow column stacks them one-per-row (a bare card-grid would otherwise lay
    them multi-column).

    One packing rule for every Home column, so `left_below` / `center_extra` /
    `right` can't drift apart.
    """
    if not cards:
        return
    if len(cards) == 1:
        card[slot] = cards[0]
        return
    card[slot] = {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "cards": [{**c, "full_width": True} for c in cards],
    }


def _place_hab_home_columns(
    card: dict[str, Any],
    gyro: dict[str, Any] | None,
    columns: dict[str, list[dict[str, Any]]],
) -> None:
    """Place a Habitation home layout's per-column placement cards into the
    `sensa-view-home` shell: Left → `left_below` (under the dials, after the
    gyro), Middle → `center_extra` (under the hero/solar), Right → `right`.

    One placer so Power Central / Infinity Truck / Noggin can't drift apart.
    """
    left_stack = ([gyro] if gyro is not None else []) + columns["left"]
    _place_home_cards(card, "left_below", left_stack)
    _place_home_cards(card, "center_extra", columns["middle"])
    _place_home_cards(card, "right", columns["right"])


# The Habitation Home columns a layout's slots fill, in the order a narrow
# screen stacks them (centre first, as the old slot shell did), each with its
# share of the 12 grid columns when all three are present.
_HAB_HOME_COLUMNS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("center", ("center", "center_controls", "center_below", "center_extra"), 6),
    ("left", ("left", "left_below"), 3),
    ("right", ("right", "right_below"), 3),
)

# Heights a new install starts with on Habitation Home, in grid rows (8px
# each). The power flow and the solar chart draw to their card's height, so
# they need one; every other card takes its content's height.
_HAB_HOME_DEFAULT_ROWS: dict[str, int] = {
    "custom:sensa-power-flow-card": 56,
    "custom:sensa-bar-chart": 30,
}


def _arrangeable_hab_home(card: dict[str, Any]) -> dict[str, Any]:
    """Let Design Mode arrange the Habitation Home tab.

    A Home layout (a public builder or a dealer spec) still describes its
    arrangement as named slots: dials left, power flow centre, mode bar
    beneath. Here every slot's card becomes its own card on one arrangeable
    grid, and the slots become the grid's `default_layout`, so a new install
    looks like the layout until the customer arranges it. Cab Home does the
    same (`build_cab._arrangeable_cab_home`). Anything that is not the slot
    shell (the no-layout note) is left as it is.
    """
    if card.get("type") != "custom:sensa-view-home":
        return card
    from ..grid_layout import BOTTOM, GRID_COLUMNS, REST_H, SIZED, card_keys

    def slot_cards(name: str) -> list[dict[str, Any]]:
        value = card.get(name)
        if not isinstance(value, dict):
            return []
        # Several placed cards in one slot arrive wrapped in a frame-less grid
        # (`_place_home_cards`); each is its own card here.
        if value.get("type") == "custom:sensa-card-grid":
            return [
                {k: v for k, v in c.items() if k != "full_width"}
                for c in value.get("cards") or [] if isinstance(c, dict)
            ]
        # One card per dial; dials with nothing to show draw nothing.
        if value.get("type") == "custom:sensa-gauge-basic":
            return _split_dials(value)
        return [value]

    # (name, share, cards) per column that has anything in it.
    columns = [
        (name, share, [c for slot in slots for c in slot_cards(slot)])
        for name, slots, share in _HAB_HOME_COLUMNS
    ]
    columns = [col for col in columns if col[2]]
    modes = slot_cards("modes")
    flat = [c for _n, _s, cards in columns for c in cards] + modes
    keys = dict(zip(map(id, flat), card_keys(flat), strict=True))

    # Widths and starts left to right; the config order above is the narrow
    # screen's stacking order instead.
    total = sum(share for _n, share, _c in columns)
    starts: dict[str, int] = {}
    widths: dict[str, int] = {}
    x = 0
    left_to_right = sorted(columns, key=lambda c: ("left", "center", "right").index(c[0]))
    for i, (name, share, _cards) in enumerate(left_to_right):
        last = i == len(left_to_right) - 1
        widths[name] = GRID_COLUMNS - x if last else round(GRID_COLUMNS * share / total)
        starts[name] = x
        x += widths[name]

    def place(cfg: dict[str, Any], col: int, row: int, w: int) -> list[int]:
        rows = _HAB_HOME_DEFAULT_ROWS.get(str(cfg.get("type")))
        return [col, row, w, rows, SIZED] if rows else [col, row, w, 1]

    # The fresh-install welcome note spans the width, so it sits centred.
    layout = {
        keys[id(cfg)]: (
            place(cfg, 0, row, GRID_COLUMNS) if cfg.get("welcome")
            else place(cfg, starts[name], row, widths[name])
        )
        for name, _share, cards in columns
        for row, cfg in enumerate(cards)
    }
    # The page fits the screen with the mode bar on its bottom edge, not under
    # the tallest column (which made the tab scroll). The first centre card
    # that draws to its height (the power flow) takes the space left; the
    # centre cards under it stack up from the mode bar.
    centre = next((cards for name, _s, cards in columns if name == "center"), [])
    grow = next((i for i, c in enumerate(centre) if str(c.get("type")) in _HAB_HOME_DEFAULT_ROWS), None)
    if grow is not None:
        k = keys[id(centre[grow])]
        layout[k] = [*layout[k][:4], REST_H]
        for cfg in centre[grow + 1:]:
            k = keys[id(cfg)]
            p = layout[k]
            layout[k] = [*p[:4], (p[4] if len(p) > 4 else 0) | BOTTOM]
    # The mode bar spans the width under everything else, on the bottom edge.
    bottom = max((len(cards) for _n, _s, cards in columns), default=0)
    for cfg in modes:
        p = place(cfg, 0, bottom, GRID_COLUMNS)
        layout[keys[id(cfg)]] = [*p[:4], (p[4] if len(p) > 4 else 0) | BOTTOM]
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "arrangeable": True,
        "cards": flat,
        "default_layout": layout,
    }


def _home_levelling_gyro_card_for(
    device: dict[str, Any],
) -> dict[str, Any] | None:
    """The compact bubble + spirit-level card for ONE levelling device.

    Placed via the device's "<Surface> Home Layout" select like every other
    Home-layout kind (2026-08-07). It used to be added AUTOMATICALLY to the
    Habitation left column and to the Cab Level layout; both auto-placements
    were removed, so the gyro now appears only where the customer puts it.
    """
    from ..const import (
        van_level_calibrated_sensor_entity_id,
        van_level_geometry_entity_id,
        van_level_shim_max_sensor_entity_id,
    )

    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return None
    return {
        "type": "custom:sensa-level-gyro-home",
        "pitch_entity": van_level_calibrated_sensor_entity_id(
            device_id, "pitch",
        ),
        "roll_entity": van_level_calibrated_sensor_entity_id(
            device_id, "roll",
        ),
        "shim_max_entity": van_level_shim_max_sensor_entity_id(device_id),
        "shim_tolerance_entity": van_level_geometry_entity_id(
            "shim_tolerance_cm",
        ),
    }


def _home_welcome_note() -> dict[str, Any]:
    """A native `sensa-note` welcome card for an otherwise-empty Home view.

    Shown in the centre slot when the Home surface has no content cards at all
    (no power flow, no dials, no gyro) — only the always-present mode bar would
    render — so a fresh install lands on a friendly message rather than a near-
    blank screen (Richard, 2026-05-30). Native (not built-in `markdown`) so it
    direct-creates on a cold panel load without HA's Lovelace engine.
    """
    return {
        "type": "custom:sensa-note",
        "icon": "mdi:hand-wave",
        # Dropped once the customer adds a card (grid_layout.apply_grid_layouts).
        "welcome": True,
        "full_width": True,
        "content": (
            "Welcome to Sensa Nomad.\n"
            "Please use menu option **Settings** to discover, add and "
            "configure your devices."
        ),
    }


def _home_has_content(
    power_flow: dict[str, Any] | None,
    left: dict[str, Any] | None,
    *others: dict[str, Any] | None,
) -> bool:
    """True when the Home view has at least one CONTENT card to show.

    The mode bar is always present and is navigation, not content, so it's
    deliberately excluded (Richard, 2026-05-30: "obviously we will have the
    modes card"). Content = the power-flow hero, the battery/tank dials (a
    `sensa-gauge-basic` with a non-empty `gauges` list), or ANY of the extra
    cards the caller passes (the van-level gyro, and on Noggin the AC + ticked
    light summaries too) — a layout that renders real cards must never also
    render the "nothing here yet" welcome note.
    """
    if power_flow is not None or any(o is not None for o in others):
        return True
    return bool((left or {}).get("gauges"))


def _build_home_standard() -> dict[str, Any]:
    """'Standard' customer Home layout (catalog name e.g. "Power Central").

    A `custom:sensa-view-home` shell: 30/40/30 columns with the compact
    power-flow diagram centred above a slim full-width mode bar. The left
    column carries the battery-SOC + fresh/grey water dials (a single
    horizontal row, ~10% of the panel each), with the van-level gyro
    stacked beneath them when a levelling device is registered. The right
    column carries a COMPACT generic climate summary for the heater (`right`)
    and the air conditioner (`right_below`) when each is registered — small
    at-a-glance cards (current → target, run state, on/off, mode chips), NOT
    the full thermostat cards (those stay on the Habitation Climate tab, which
    is why the full heater card was pulled from here on 2026-05-30 for being
    too tall). Each appears only when its device exists; absent → the slot is
    omitted and the column collapses. When a solar charger is registered, a 24h
    solar history graph rides beneath the hero in the centre column
    (`center_below`).
    """
    left = _home_left_dials()
    # The van-level gyro is NO LONGER auto-placed here (2026-08-07). It is a
    # Home-layout kind like the rest now, so it appears only in the column the
    # customer picks in its "Habitation Home Layout" select -- see
    # `_home_placement_card_for`.
    # Per-column placement cards (heaters / AC / lights / van level) for the Habitation
    # surface, each in the column the customer picked in its "Habitation Home
    # Layout" select. Resolved up-front so the welcome-note guard can count them:
    # a placed card IS content, and "nothing here yet" must never render beside
    # a real card (code-review catch).
    columns = _home_placement_columns("hab")
    all_placed = columns["left"] + columns["middle"] + columns["right"]
    card: dict[str, Any] = {
        "type": "custom:sensa-view-home",
        "hide_header": True,
        "left": left,
        "modes": _home_modes_bar(single_row=True),
    }
    # Power flow is conditional on an electrical item it can show — omit the
    # centre slot entirely when none resolves (no placeholder card).
    power_flow = _home_center_power_flow()
    if power_flow is not None:
        card["center"] = power_flow
    elif not _home_has_content(power_flow, left, *all_placed):
        # No content cards at all (no power flow, no dials, nothing placed) —
        # only the always-present modes bar would show. Lead with a welcome
        # message so the surface isn't blank on a fresh install (Richard,
        # 2026-05-30). A PLACED van-level gyro counts as content via
        # `all_placed`, so the note cannot render beside one.
        card["center"] = _home_welcome_note()
    # A slim controls panel (inverter mode etc.) directly under the power flow.
    controls = _home_controls_panel()
    if controls is not None:
        card["center_controls"] = controls
    solar_graph = _home_solar_graph()
    if solar_graph is not None:
        card["center_below"] = solar_graph
    # Place each column's cards: Left under the dials, Middle under the
    # hero/solar, Right in the right column.
    _place_hab_home_columns(card, None, columns)
    return card


def _spec_home_builder(layout_id: str) -> Callable[[], dict[str, Any]]:
    """A Home-layout builder backed by a declarative spec (`layouts/<id>.json`).

    Dealer layouts are DATA, not code: the spec names which shared card builder
    fills which slot, and `layout_spec.render_home_layout` applies the same
    conditional rules the hand-written builders used to duplicate. See
    `dashboard/layout_spec.py` for why the split sits there.

    Fails open. A spec that is missing or unloadable falls back to Power Central
    rather than blanking the Home tab, which matters most in exactly the case
    this protects against: a dealer layout that has not reached this box.
    """
    def _build() -> dict[str, Any]:
        from .layout_spec import render_home_layout, resolve_spec

        spec = resolve_spec(_dashboard_hass, layout_id, "hab")
        if spec is None:
            _LOGGER.warning(
                "home layout %r has no loadable spec; serving Power Central",
                layout_id,
            )
            return _build_home_standard()
        return render_home_layout(spec)

    return _build


def _home_layout_builder(layout_id: str) -> Callable[[], dict[str, Any]] | None:
    """The builder for a Home-layout id, or None when nothing can render it.

    Checks the registry first (public builders + the dealer layouts that still
    ship bundled), then falls through to a spec-backed builder for an id this
    build has never heard of. That fall-through is what lets a dealer publish a
    NEW layout from Hub and have it render on their boxes without a release.
    """
    builder = _HOME_LAYOUTS.get(layout_id)
    if builder is not None:
        return builder
    from .layout_spec import resolve_spec

    if resolve_spec(_dashboard_hass, layout_id, "hab") is None:
        return None
    return _spec_home_builder(layout_id)


# The dealer Home layouts. Each was a hand-written builder that copied
# `_build_home_standard` almost verbatim; they are now specs in `layouts/*.json`
# and these names are kept as thin aliases so existing callers and tests keep
# working. A dealer may own SEVERAL layouts: add one spec file per layout (the
# filename is the layout id) plus a catalog entry, and the gating already
# returns every entry matching the dealer.
_build_home_infinity_truck = _spec_home_builder("infinity-truck")
_build_home_noggin_mog = _spec_home_builder("noggin-mog")
_build_home_blackpaw = _spec_home_builder("blackpaw")


# Home-layout registry: layout id → builder returning the Home tab body card
# config. Keys MUST cover the `id`s in `home_layouts_hab.json` (the catalog gives
# each id a staff-facing name + dropdown order).
#
# Public layouts are Python builders. DEALER layouts are declarative specs, so
# adding one is a JSON file plus a catalog entry with no new Python, which is
# what keeps this module from growing a builder per dealer.
_HOME_LAYOUTS: dict[str, Callable[[], dict[str, Any]]] = {
    "standard": _build_home_standard,
    "infinity-truck": _build_home_infinity_truck,
    "noggin-mog": _build_home_noggin_mog,
    "blackpaw": _build_home_blackpaw,
}


def _resolve_active_home_layout() -> str:
    """Return the Home-layout id this box starts from (a `_HOME_LAYOUTS` key or
    a dealer spec id), or `""` to leave the Home tab blank.

    There is no customer picker: the customer arranges Home in Design Mode, and
    the layout is only the arrangement a new install starts with. The first
    allowed id wins, and the allowed set (`allowed_home_ids`) already folds in
    the licence tier AND the Hub dealer claim, with a dealer's own layouts
    first, so a dealer box starts from its dealer's layout and every other box
    from the public one. Trials never see dealer pages (`current_dealer`).

    The one case that returns `""` (blank): a dealer set to `exclusive` or
    `locked` who owns NO layouts, a contradictory config. Rather than silently
    serving a public page (which would defeat the setting), the layout is left
    blank with a one-off warning in the Sensa log.
    """
    from ..home_catalog import (
        allowed_home_ids,
        current_licence_tier,
        dealer_is_exclusive_empty,
    )

    tier = current_licence_tier(_dashboard_hass)
    # `_home_layout_builder` (not `in _HOME_LAYOUTS`) so a layout this build has
    # never heard of, published by a dealer from Hub, still resolves.
    for layout_id in allowed_home_ids(tier, _dashboard_hass):
        if _home_layout_builder(layout_id) is not None:
            return layout_id
    # Nothing allowed has a builder. An exclusive dealer with no layouts gets a
    # blank Home + a deduped log entry (logs once per day, not every regen);
    # everyone else falls back to the standard public layout.
    if dealer_is_exclusive_empty(tier, _dashboard_hass):
        from datetime import timedelta

        from .._log import sensa_log
        from ..const import SENSA_LOG_SOURCE_SYSTEM
        sensa_log(
            _dashboard_hass, "warning",
            "Your dealer hasn't published a home layout yet, so the Home "
            "screen is blank. Contact your dealer if you expected one here.",
            source=SENSA_LOG_SOURCE_SYSTEM,
            dedup_key="dealer_no_layout", cooldown=timedelta(hours=24),
        )
        return ""
    return "standard"


# --- Cab Home layouts ------------------------------------------------------
# The Cab dashboard's Home tab is multi-layout exactly like the Habitation Home
# tab: a catalog (`home_layouts_cab.json` via `cab_catalog.py`) + a tier/dealer-
# gated start layout + per-layout builders keyed here by id.
# Adding a Cab home page = a catalog entry + a builder in `_CAB_LAYOUTS`.


def _build_cab_level() -> dict[str, Any]:
    """The classic park-up landing arrangement: the mode bar, the OBD2
    vehicle-vitals dials (when a WiCAN is registered), a one-tap controls panel
    (the trickle charger), then the customer's opt-in Cab-Home summary cards.
    A native `sensa-card-grid` (no Lovelace engine). Dealer-owned Cab layouts
    slot into `_CAB_LAYOUTS` the same way the Habitation home layouts do (see
    home-layouts.md).

    **NOT rendered by any layout today.** It backed 'Default (Landscape)' until
    2026-08-17 (Richard), when that catalog entry was re-pointed at the BlackPaw
    arrangement (`_build_cab_default_landscape`); 'Default (Portrait)' carried
    it for the rest of that day, then became a portrait BlackPaw stack too
    (`_build_cab_default_portrait`). Kept, not deleted, as the classic
    arrangement. The customer's opt-in Cab cards come from
    `_cab_placement_cards`, which every Cab layout calls.
    """
    from ..const import CAB_LOCATION_SELECT_ENTITY_ID

    cards: list[dict[str, Any]] = [_cab_overview_mode_bar()]
    # "Who is driving": photo buttons that set the van's GPS source to that
    # person's device (and their MySensa session with it). Replaces the old
    # Driver dropdown in the tab strip. The card renders NOTHING when no person
    # has a device, so a van with no phones paired simply does not show it.
    cards.append({
        "type": "custom:sensa-people-bar",
        "sensor": "sensor.sensa_nomad_people",
        "source_entity": CAB_LOCATION_SELECT_ENTITY_ID,
    })
    for d in _dashboard_devices:
        if _is_obd2_device(d):
            gauge = _obd2_overview_gauge_card(d)
            if gauge:
                cards.append(gauge)
    controls_panel = _cab_controls_panel()
    if controls_panel is not None:
        cards.append(controls_panel)
    # The van-level gyro is NO LONGER auto-added here (2026-08-07). Van level is
    # a Home-layout kind now, so it appears on this surface only when the
    # customer picks a column in its "Cab Home Layout" select, arriving through
    # `cab_columns` below like a heater or a light. The Cab PARKING tab keeps
    # its own big park-up gyro -- different screen, untouched.
    cards.extend(_cab_placement_cards())
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "cards": cards,
    }


def _cab_placement_cards() -> list[dict[str, Any]]:
    """The compact cards the customer chose for the Cab home through each
    device's "Cab Home Layout" select, as one list.

    Every Cab layout has a single side column (beside the map in landscape,
    above it in portrait), so Left / Middle / Right only ORDER the cards: left
    first, then middle, then right. Every Cab layout calls this, or the select
    is a dead control on that layout: it was on all of them from 2026-08-17 to
    2026-09-25, when the public Cab pages became BlackPaw copies that never read
    it.
    """
    cab_columns = _home_placement_columns("cab")
    return (
        _cab_roll_cards()
        + _cab_breakdown_cards()
        + cab_columns["left"] + cab_columns["middle"] + cab_columns["right"]
    )


def _cab_breakdown_cards() -> list[dict[str, Any]]:
    """The Breakdown tile, once Hub's `breakdown` feature gate is on for this
    customer. Always shown then, like the roll tile: it is safety information.

    Records what it placed, so the cover sensor rebuilds the Cab when the gate
    flips (breakdown.py).
    """
    from ..breakdown import ENTITY_ID, record_built, tile_enabled
    from ..const import vehicle_profile_entity_id

    enabled = tile_enabled(_dashboard_hass)
    record_built(_dashboard_hass, enabled)
    if not enabled:
        return []
    return [
        {
            "type": "custom:sensa-breakdown-card",
            "entity": ENTITY_ID,
            "gps_entity": "sensor.sensa_nomad_gps_actual",
            "tracking_entity": "switch.sensa_nomad_tracking",
            "length_entity": vehicle_profile_entity_id("length_m"),
            "height_entity": vehicle_profile_entity_id("height_m"),
            "weight_entity": vehicle_profile_entity_id("weight_t"),
        }
    ]


def _build_cab_default_portrait() -> dict[str, Any]:
    """'Default (Portrait)' Cab Home layout.

    The same content as 'Default (Landscape)' (`_build_cab_blackpaw`) stacked
    for a TALL screen instead of a wide one (2026-08-17, Richard): the WiCAN
    vehicle telemetry sits ABOVE the navigation map, full width, and the map
    takes every row of height left over. Landscape's 35/65 side-by-side split
    reads badly in portrait, where a 35% column is too narrow for a dial and
    the map is left tall and thin.

    The vitals are two full-width rows (`_cab_portrait_telemetry_cards`): the
    dials (speed, rpm, coolant, alternator) filling one row, then fuel and DPF
    soot as 0-100% bars beside the mileage. Each row is chrome-less cards side
    by side in a count-proportioned split, so the joins are invisible and every
    instrument comes out the same width.

    The outer grid is `full_height` + a single `columns` track + explicit `rows`
    ("auto" for the telemetry, the rest to the map). `full_height` alone would
    NOT do it: it stretches rows via `grid-auto-rows`, which makes them EQUAL,
    handing the dials half the screen. `_PORTRAIT_ROWS` is what keeps the
    telemetry content-tall.

    The customer's opt-in Cab cards (`_cab_placement_cards`) stack under the
    telemetry in the same top band. With NO telemetry and no placed card the
    split is skipped entirely and the fill-mode map is returned alone, exactly
    as the landscape layout does — no placeholder band for an absent device.
    """
    from ..const import CAB_LOCATION_SELECT_ENTITY_ID

    telemetry = _cab_portrait_telemetry_cards()
    nav_map = _nav_map_card_slot(
        "select.sensa_nomad_cab_nav_source",
        "select.sensa_nomad_cab_location_source",
        "device_tracker.sensa_nomad_vehicle",
        aspect_ratio=None,
        min_height=_BLACKPAW_NAV_MIN_HEIGHT,
        source_pills=True,
        theme_pill=True,
    )
    # Same as landscape: "who is driving" rides the map's OWN pill row (faces
    # left, map-source pills right) rather than adding a control row of its own.
    nav_map["people_bar"] = {
        "sensor": "sensor.sensa_nomad_people",
        "source_entity": CAB_LOCATION_SELECT_ENTITY_ID,
    }
    top_cards = telemetry + _cab_placement_cards()
    if not top_cards:
        return nav_map
    # The telemetry rows go in a nested `stack` grid so the OUTER grid has
    # exactly the two cells `_PORTRAIT_ROWS` describes, whether one row or two
    # resolved. NOT `full_height`: that pushes a stack's last cell to the
    # bottom, which would strand the bar row away from the dials.
    top: dict[str, Any] = {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "stack": True,
        "cards": top_cards,
    }
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "full_height": True,
        "columns": _PORTRAIT_COLUMNS,
        "rows": _PORTRAIT_ROWS,
        "cards": [top, nav_map],
    }


def _build_cab_infinity_truck() -> dict[str, Any]:
    """'Infinity Truck' dealer Cab Home layout.

    Dealer-owned (catalog `dealer: "infinity"`): only the Infinity dealer's
    customers see it, per that licence's `homepage_mode` (additive puts it
    ahead of the public defaults, exclusive replaces them). The dealer claim and
    licence tier are SHARED with the Habitation catalog, so this page follows
    the same dealer as the Habitation `infinity-truck` layout spec.

    A copy of the BlackPaw arrangement (2026-08-17, Richard), ready to diverge;
    it previously carried its own portrait stack (the OBD2 vehicle-vitals
    gauges, then a Park/Drive scene-pill + map-source-pill control row, then a
    16:9 navigation map). The `_cab_scene_pill_bar` and `_cab_map_source_pills`
    helpers went with it; recover them from git history if a control row like
    that is wanted again.
    """
    return _build_cab_blackpaw()


def _build_cab_noggin_mog() -> dict[str, Any]:
    """'Noggin The Mog' dealer Cab Home layout.

    Dealer-owned (catalog `dealer: "noggin"`): the Cab twin of the Habitation
    `noggin-mog` layout spec. Same gating story as `_build_cab_infinity_truck`.

    A copy of the BlackPaw arrangement (2026-08-17, Richard), ready to diverge;
    it previously aliased the classic landing arrangement with a half-size gyro
    and no trickle-charge controls panel. Note this also clears the
    `exclusive-empty` trap: an exclusive dealer owning no Cab layout at its tier
    leaves the Cab Home tab BLANK (`cab_dealer_is_exclusive_empty` →
    `_resolve_active_cab_layout` returns `""`), which is what a noggin box saw
    before this page existed.
    """
    return _build_cab_blackpaw()


def _build_cab_blackpaw() -> dict[str, Any]:
    """'BlackPaw' dealer Cab Home layout.

    Dealer-owned (catalog `dealer: "blackpaw"`). Unlike infinity and noggin,
    blackpaw owns a Cab layout and NO Habitation one: a dealer is free to brand
    just one surface. The consequence to know about is that an `exclusive`
    blackpaw licence gets a BLANK Habitation Home tab (`dealer_is_exclusive_empty`
    → `_resolve_active_home_layout` returns `""` plus a one-off log). On an
    `additive` licence the Habitation Home falls back to the public pages, which
    is the intended shape for a Cab-only dealer.

    Diverges from the classic landing arrangement (`_build_cab_level`): a full-height
    two-panel row split 35 / 65 (`_BLACKPAW_COLUMNS`) — the LEFT column carries
    the WiCAN vehicle telemetry (a speed/rpm hero over smaller coolant/fuel
    dials) then the nearby list; the RIGHT column is the navigation map filling the
    panel's full height, its source-switch pills pinned to the bottom. The
    customer's opt-in Cab-Home cards (`_cab_placement_cards`) stack under the
    telemetry in the left column. The trickle-charger controls and the
    equal-size OBD gauge row are dropped on this layout.

    The outer `sensa-card-grid` is `full_height` (fills the panel body, which
    hands the tab card `flex:1 1 auto`) with an explicit `columns` 35/65 split;
    its single row stretches so both columns reach the full height. The LEFT
    column is a `stack` nested grid — a plain vertical flex column, top-aligned.
    It is deliberately NOT `full_height`: that would push its last cell to the
    bottom (`margin-top:auto`), stranding the trailing gauge away from the rest.
    The map fills the right column and carries the "who is driving" faces in its
    own source-pill row (faces left, map-source pills right). The faces are
    Radar-only and hide on Google Maps / HA Map, like the category pills beside
    them: switching driver switches that person's Radar session, which only the
    Radar embed reflects. A dark/light pill (`theme_pill`) leads the map-source
    pills on the same Radar-only terms, forcing the map's appearance for this
    screen over whatever the driver set for their own phone. A `min_height`
    floors the map for the folded / very-short-viewport case.

    With NO OBD2 telemetry (no adapter registered, or its entities unresolved)
    and no placed card there is nothing to put in the left column, so the
    layout skips the split entirely and returns the fill-mode map alone -- the
    navigation takes the full width, the same shape the Cab Navigation tab
    renders (2026-08-17, Richard: no placeholder third of the screen for an
    absent device).
    """
    from ..const import CAB_LOCATION_SELECT_ENTITY_ID

    # The scene pills were REMOVED from this layout (2026-08-01, Richard). The
    # scenes are reachable from the mode bar on the public Cab layouts and from
    # Intelligence; on a driving screen the column is better spent on telemetry.
    # The nearby list (Water + Fuel) went the same day, for the same reason: the
    # map beside it already answers "what is near me", and the pills on it do so
    # against the view you are actually looking at. `stack` keeps the column a
    # plain vertical flex (not the auto-fit grid) and, without `full_height`,
    # nothing is pushed to the bottom, so everything sits top-aligned.
    telemetry = _cab_blackpaw_telemetry_cards()
    nav_map = _nav_map_card_slot(
        "select.sensa_nomad_cab_nav_source",
        "select.sensa_nomad_cab_location_source",
        "device_tracker.sensa_nomad_vehicle",
        aspect_ratio=None,
        min_height=_BLACKPAW_NAV_MIN_HEIGHT,
        source_pills=True,
        theme_pill=True,
    )
    # "Who is driving" rides the map's OWN pill row: faces left, map-source
    # pills right. Both change where the map looks, so they share one strip
    # rather than stacking two control rows under the map.
    nav_map["people_bar"] = {
        "sensor": "sensor.sensa_nomad_people",
        "source_entity": CAB_LOCATION_SELECT_ENTITY_ID,
    }
    left_cards = telemetry + _cab_placement_cards()
    if not left_cards:
        # Nothing for the left column, so no split. The fill-mode map alone
        # takes the tab's full width and height.
        return nav_map
    left: dict[str, Any] = {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "stack": True,
        "cards": left_cards,
    }
    columns = _BLACKPAW_COLUMNS
    if _is_gps_speedo_only(left_cards):
        columns = _BLACKPAW_GPS_ONLY_COLUMNS
        left_cards[0]["dial_max_width"] = _BLACKPAW_GPS_ONLY_DIAL
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "full_height": True,
        "columns": columns,
        "cards": [left, nav_map],
    }


def _is_gps_speedo_only(cards: list[dict[str, Any]]) -> bool:
    """True when a Cab Home column holds nothing but the GPS speedometer."""
    from ..const import GPS_SPEED_SENSOR_ENTITY_ID

    return (
        len(cards) == 1
        and [g.get("entity") for g in cards[0].get("gauges") or []]
        == [GPS_SPEED_SENSOR_ENTITY_ID]
    )


def _build_cab_default_landscape() -> dict[str, Any]:
    """'Default (Landscape)' Cab Home layout, the public default.

    A copy of the BlackPaw arrangement (2026-08-17, Richard), ready to diverge.
    Its portrait twin `_build_cab_default_portrait` stacks the same content for
    a tall screen. The previous arrangement (mode bar, OBD2 dials,
    trickle-charge controls, opt-in Cab-Home summary cards) lives on in
    `_build_cab_level`, which no layout renders.
    """
    return _build_cab_blackpaw()


# Cab Home layout id → builder. Keys MUST cover the `id`s in
# `home_layouts_cab.json`. Mirrors `_HOME_LAYOUTS`.
_CAB_LAYOUTS: dict[str, Callable[[], dict[str, Any]]] = {
    "level": _build_cab_default_landscape,
    "default-portrait": _build_cab_default_portrait,
    "infinity-truck": _build_cab_infinity_truck,
    "noggin-mog": _build_cab_noggin_mog,
    "blackpaw": _build_cab_blackpaw,
}


def _resolve_active_cab_layout() -> str:
    """Return the Cab Home-layout id this box starts from (a `_CAB_LAYOUTS` key),
    or `""` to leave the Cab Home tab blank. The Cab mirror of
    `_resolve_active_home_layout`: there is no customer picker, the customer
    arranges Cab Home in Design Mode, and the first allowed id wins.

    `allowed_cab_ids` folds in the licence tier AND the Hub dealer claim, with a
    dealer's own layouts first. The one `""` case is an `exclusive` dealer who
    owns no Cab layouts: blank + a one-off log, rather than a public fallback.
    """
    from ..cab_catalog import allowed_cab_ids, cab_dealer_is_exclusive_empty
    from ..home_catalog import current_licence_tier

    from .layout_spec import resolve_spec

    tier = current_licence_tier(_dashboard_hass)
    for layout_id in allowed_cab_ids(tier, _dashboard_hass):
        # A builder, or a Cab spec (bundled, or fetched from Hub for a dealer
        # whose Cab page exists only there).
        if layout_id in _CAB_LAYOUTS or resolve_spec(_dashboard_hass, layout_id, "cab"):
            return layout_id
    if cab_dealer_is_exclusive_empty(tier, _dashboard_hass):
        from datetime import timedelta

        from .._log import sensa_log
        from ..const import SENSA_LOG_SOURCE_SYSTEM
        sensa_log(
            _dashboard_hass, "warning",
            "Your dealer hasn't published a Cab layout yet, so the Cab Home "
            "screen is blank. Contact your dealer if you expected one here.",
            source=SENSA_LOG_SOURCE_SYSTEM,
            dedup_key="dealer_no_cab_layout", cooldown=timedelta(hours=24),
        )
        return ""
    return "level"


def _bms_family(device: dict[str, Any]) -> str:
    """Return the bms_family slug ('jk' / 'vip' / '') for a device row."""
    family = (
        device.get("bms_family")
        or (device.get("config") or {}).get("bms_family")
        or ""
    )
    return str(family).lower()


def _note_card(
    message: str,
    icon: str = "mdi:information-outline",
) -> dict[str, Any]:
    """An empty-state / placeholder card for a panel tab.

    Native `custom:sensa-note`, NOT the built-in `markdown` card: a
    `panel_custom` page has no Lovelace engine, so a built-in card leaves the
    tab on "Loading... Taking longer than expected" forever. `**bold**` runs
    render; nothing else of markdown does.
    """
    return {"type": "custom:sensa-note", "icon": icon, "content": message}


def _tab_error_card(label: str, exc: Exception) -> dict[str, Any]:
    """The in-tab card shown when a tab builder raises.

    Native for the same reason as `_note_card` — and doubly so here: a built-in
    error card would hang on "Loading" and hide the very error it exists to
    show. `preformatted` keeps the traceback's newlines.
    """
    return _note_card(
        f"Tab '{label}' failed to build\n\n"
        f"**{type(exc).__name__}**: {exc}\n\n"
        f"{traceback.format_exc()}",
        icon="mdi:alert-circle-outline",
    ) | {"preformatted": True}


# Radar search categories offered as pills in the row under the map. Pressing
# one shows every place of that kind across the visible map, and keeps showing
# them as the van drives -- the JOURNEY question ("where is the fuel around me
# now"), as opposed to Radar's own typed search, which answers the DESTINATION
# question and is done while parked.
#
# **These are a SAFETY control, not a convenience one** (Richard, 2026-08-01):
# the alternative to a pill is opening the filters drawer and picking through a
# 40-entry list, which is not something anyone should be doing at the wheel. One
# press, eyes barely off the road. That is the argument for keeping this list
# SHORT and every entry one-tap: a row long enough to need reading defeats the
# point, so a new pill has to earn its place against the ones already here.
#
# The ids are RADAR'S OWN category ids and are handed to the embed untranslated,
# so they must match its vocabulary (`Sensa.Radar` routers/categories.py); a
# typo here is a pill that silently finds nothing.
_RADAR_CATEGORY_PILLS: list[dict[str, str]] = [
    {"id": "fuel_station", "label": "Fuel", "icon": "mdi:fuel"},
    {"id": "toilets", "label": "Toilets", "icon": "mdi:toilet"},
]


def _nav_map_card_slot(
    nav_select_entity: str,
    location_select_entity: str,
    vehicle_tracker: str,
    aspect_ratio: str | None = "16:9",
    min_height: str | None = None,
    source_pills: bool = False,
    pills_side: bool = False,
    theme_pill: bool = False,
) -> dict[str, Any]:
    """The Cab Navigation tab's map slot: a native `custom:sensa-nav-map` that
    direct-creates the map card matching the nav select (HA Map / Google Maps /
    Sensa Radar) and swaps it when the dropdown changes.

    This was a built-in `vertical-stack` of three built-in `conditional` cards
    until 2026-07-10. Both are Lovelace card types, and a `panel_custom` page
    has no Lovelace engine — so the tab sat on "Loading... Taking longer than
    expected" forever, the same trap that held the Mobile Electrical tab. The
    native card owns the source→map mapping itself, so nothing here needs the
    engine.

    `hide_toolbar` because the Cab panel's tab strip already carries the
    map-style and location dropdowns; the Mobile Navigation tab builds the same
    card with its own toolbar and no `aspect_ratio` (it fills the screen).

    `aspect_ratio=None` OMITS the key, putting the card in FILL mode
    (`:host{height:100%}`) so it fills its container's height — used by the
    BlackPaw Cab home's right panel. A string aspect (the Cab Navigation tab)
    sizes the map to that ratio instead. `min_height` (fill mode only) floors
    the host height so a short sibling column / a folded 2-up grid can't
    collapse the map to an unusable strip. `source_pills` adds a round
    map-source switcher (Radar / Google Maps / HA Map) under the map,
    right-aligned — used by the BlackPaw home so the customer can swap sources
    without the (hidden) top toolbar. `theme_pill` puts a dark/light pill at the
    head of that group, forcing the embedded map's appearance for that screen.
    """
    card: dict[str, Any] = {
        "type": "custom:sensa-nav-map",
        "nav_entity": nav_select_entity,
        "location_entity": location_select_entity,
        "vehicle_entity": vehicle_tracker,
        "hide_toolbar": True,
        # NO `tier_badge` (the badge was removed from every CUSTOMER map surface
        # on 2026-07-27, Richard). `tier_entity` below is a different thing and is
        # now wired: it feeds the Radar pill's Premium TINT, which has to follow
        # the picked driver. The badge stays off because it is gated on its own
        # `tier_badge` flag, not on the entity. (The Settings Cards gallery is the
        # one exception and always was: its nav-map is `demo: True`, which
        # hard-codes a PREMIUM badge without reading a sensor.)
        #
        # `tier_entity` is set UNCONDITIONALLY because it now carries two things
        # for the picked driver: the tier (which tints the Radar pill) and their
        # Radar SESSION, which the card hands to the embedded map so it runs as
        # them. Gating it on `source_pills` would sign the embed out on every
        # surface that shows no pills.
        "tier_entity": "sensor.sensa_nomad_radar_tier",
        # Emitted UNCONDITIONALLY, for the same reason `tier_entity` above is:
        # gating it per-surface is how two maps sitting next to each other end
        # up behaving differently. The card hides the pills by itself on the
        # Google/HA map sources, which cannot draw the pins.
        "category_pills": list(_RADAR_CATEGORY_PILLS),
        **_radar_url_config(),
    }
    # The dealer's logo, drawn by Radar at the vehicle's position in place of the
    # stock van. Dealer only (never the Sensa default logo): no dealer keeps the van.
    vehicle_logo = _dealer_logo_url()
    if vehicle_logo:
        card["vehicle_logo"] = vehicle_logo
    if aspect_ratio is not None:
        card["aspect_ratio"] = aspect_ratio
    elif min_height is not None:
        card["min_height"] = min_height
    if pills_side:
        # Category + source pills in a column to the RIGHT of the map instead of
        # the row beneath it (categories top, sources bottom). Opt-in per
        # surface: the BlackPaw Cab home keeps its source pills under the map,
        # level with its scene pills.
        card["pills_side"] = True
    if theme_pill:
        # A dark/light pill leading the source pills, forcing the embedded map's
        # appearance for THAT SCREEN. Opt-in per surface: a cab screen is shared
        # and mounted, so it needs a control of its own; a phone running Radar
        # keeps the customer's own account preference, which the pill overrides
        # in the embed's memory and never writes back.
        card["theme_pill"] = True
        # Day/night, read ONLY as the starting point before the customer has
        # pressed the pill (then it is their choice). Passed as config rather
        # than assumed by the card so the entity is named in one place.
        card["sun_entity"] = "sun.sun"
    if source_pills:
        card["source_pills"] = True
        # The pill's Premium tint reads `tier_entity` above. It USED to be a
        # separate `licence_entity`, read for its `premium_active` attribute --
        # but that is the BOX-level question ("is this licence assigned to a Hub
        # account?") and is answered for the PRIMARY member whoever the driver
        # picker has selected. On a customer whose primary member holds a seat the
        # pill stayed gold for every driver, including one whose seat had just
        # been taken back: the single control meant to show "you have Premium" was
        # the one thing that never changed when Premium did.
    # Navigation units come from the Nomad nav-units select, so the Radar embed
    # follows the customer's road-distance preference (metric-except-roads, the UK
    # norm HA can't express) and agrees with the Nearby list.
    card["units_entity"] = "select.sensa_nomad_nav_units"
    # Which fuel price the map's fuel pins should show. The select publishes the
    # resolved Radar price key as an attribute, so the card reads the answer
    # rather than repeating the mapping.
    card["fuel_type_entity"] = "select.sensa_nomad_nav_fuel_type"
    # The van's size + gradient limit, for vehicle-aware routing and low-bridge
    # warnings on the map. Sent as Radar's own profile keys, so the entity list
    # is derived from the ONE key list rather than typed out again.
    from ..const import (
        VEHICLE_PROFILE_KEYS,
        vehicle_profile_entity_id,
    )

    card["vehicle_profile_entities"] = {
        key: vehicle_profile_entity_id(key) for key in VEHICLE_PROFILE_KEYS
    }
    return card






def _build_teltonika_card(device: dict[str, Any]) -> dict[str, Any]:
    """Build a stack-in-card for a single Teltonika device."""
    display_name = device.get("display_name") or device.get("name") or device.get("vendor") or "Teltonika Device"
    ip = device.get("ip_address") or device.get("ip") or "-"
    # Mirror the entity-creation path in text.py / switch.py / number.py /
    # button.py: they all key off `linked_mac_address` first, falling back
    # to `mac_address`. The card MUST use the same chain or the prefix
    # diverges (e.g. `teltonika_device` here vs `teltonika_2912` in the
    # registry) and every row reads "entity not found".
    mac = (
        device.get("linked_mac_address")
        or device.get("mac_address")
        or device.get("mac")
        or "-"
    )
    # A TSW switch registers with `role: router`, so the role cannot tell them
    # apart; `_teltonika_is_switch` is the one predicate for that.
    is_router = not _teltonika_is_switch(device)
    kind = "router" if is_router else "switch"

    # Entity IDs are derived from the HA device name which uses MAC suffix
    # e.g. "Teltonika 2912" → entity prefix "teltonika_2912"
    from ..const import teltonika_device_name
    dev_name = teltonika_device_name(mac)
    prefix = dev_name.lower().replace(" ", "_")

    # Native `custom:sensa-config-card` (no Lovelace engine) — replaced the
    # HACS `vertical-stack-in-card` + built-in markdown/entities/button stack,
    # which hung on a cold panel load (see feedback_vsic_cold_load_hang).
    fields: list[dict[str, Any]] = [
        {"entity": f"text.{prefix}_username", "name": "Username", "control": "text"},
        {"entity": f"text.{prefix}_password", "name": "Password", "control": "password"},
        {"entity": f"switch.{prefix}_ssl", "name": "Use SSL", "control": "switch"},
    ]
    # Signal threshold only applies to routers (not switches).
    if is_router:
        fields.append({
            "entity": f"number.{prefix}_signal_threshold",
            "name": "Minimum Signal (dBm)",
            "control": "number",
        })

    return {
        "type": "custom:sensa-config-card",
        "title": display_name,
        "note": (
            f"Enter the admin credentials below and press Connect to {kind}."
        ),
        "info": [
            {"label": "IP Address", "value": ip},
            {"label": "MAC Address", "value": mac},
        ],
        "fields": fields,
        "save_button": f"button.{prefix}_save_router_config",
        "save_label": f"Connect to {kind}",
    }


def _build_nas_card(device: dict[str, Any]) -> dict[str, Any]:
    """Build the config + power-control card for one Asustor NAS.

    A native `custom:sensa-config-card` with an info/instructions header, the
    host + SSH-credential + WOL-MAC config rows (the pinned-id text entities)
    over a Save button, then the native `custom:sensa-switch-tile` power control
    (OFF = clean SSH shutdown, ON = Wake-on-LAN) as a child card. All entity_ids
    come from the const helpers so they match what the platform builders pin."""
    from ..const import (
        nas_config_text_entity_id,
        nas_power_switch_entity_id,
        nas_save_button_entity_id,
    )

    device_id = (
        device.get("device_id")
        or device.get("catalog_id")
        or device.get("discovery_id")
        or "unknown"
    )
    display_name = (
        device.get("display_name")
        or device.get("name")
        or "Asustor Flashstor 6"
    )
    ip = device.get("ip_address") or device.get("ip") or "-"
    mac = (
        device.get("linked_mac_address")
        or device.get("mac_address")
        or device.get("mac")
        or "-"
    )

    # Native `custom:sensa-config-card` (no Lovelace engine) — info + editable
    # SSH-config fields + Save, with the native `sensa-switch-tile` power
    # control as a child card. Replaced the HACS `vertical-stack-in-card` stack
    # that hung on a cold panel load (see feedback_vsic_cold_load_hang).
    return {
        "type": "custom:sensa-config-card",
        "title": display_name,
        "note": (
            "Enable SSH on the NAS (ADM > Services > Terminal), enter the host "
            "and SSH credentials below and press Connect to NAS. The power "
            "switch then cleanly shuts the NAS down (Off) or wakes it via "
            "Wake-on-LAN (On)."
        ),
        "info": [
            {"label": "IP Address", "value": ip},
            {"label": "MAC Address", "value": mac},
        ],
        "fields": [
            {"entity": nas_config_text_entity_id(device_id, "host"),
             "name": "Host", "control": "text"},
            {"entity": nas_config_text_entity_id(device_id, "ssh_username"),
             "name": "SSH Username", "control": "text"},
            {"entity": nas_config_text_entity_id(device_id, "ssh_password"),
             "name": "SSH Password", "control": "password"},
            {"entity": nas_config_text_entity_id(device_id, "ssh_key"),
             "name": "SSH Private Key (optional)", "control": "text"},
            {"entity": nas_config_text_entity_id(device_id, "wol_mac"),
             "name": "Wake-on-LAN MAC", "control": "text"},
        ],
        "save_button": nas_save_button_entity_id(device_id),
        "save_label": "Connect to NAS",
        "cards": [
            {
                "type": "custom:sensa-switch-tile",
                "entity": nas_power_switch_entity_id(device_id),
                "name": "NAS Power",
                "icon": "mdi:nas",
            },
        ],
    }


def _build_bluetooth_card(device: dict[str, Any]) -> dict[str, Any]:
    """Build an info card for a single Sensa Bluetooth proxy device.

    Product shot on the left, info table on the right. The image lives in the
    Sensa.Control.WebCards repo at assets/images/sensa-bluetooth-proxy.png and
    is deployed via the add-on's install-webcards.sh to /config/www/sensa/images/.
    If the image file isn't present yet HA renders a broken-image placeholder —
    the info table still shows fine.
    """
    display_name = (
        device.get("display_name")
        or device.get("name")
        or device.get("vendor")
        or "Sensa Bluetooth Proxy"
    )
    hostname = device.get("hostname") or "-"
    ip = device.get("ip_address") or device.get("ip") or "-"
    mac = device.get("mac_address") or device.get("mac") or "-"
    model = device.get("model") or device.get("vendor_model") or "-"
    version = device.get("version") or "-"

    # Native `custom:sensa-config-card` (read-only — image + info, no fields).
    # Replaced the HACS `vertical-stack-in-card` that hung on a cold panel load
    # (see feedback_vsic_cold_load_hang).
    return {
        "type": "custom:sensa-config-card",
        "title": display_name,
        "image": "/local/sensa/images/sensa-bluetooth-proxy.webp",
        "info": [
            {"label": "Hostname", "value": hostname},
            {"label": "IP Address", "value": ip},
            {"label": "MAC Address", "value": mac},
            {"label": "Model", "value": model},
            {"label": "Firmware", "value": version},
        ],
    }


# Shipped stock images for the image picker (Settings → Images shows them as a
# read-only library; the Switching and Levelling pickers offer them alongside
# the customer's uploads).
#
# This tuple is the CURATED set: it fixes the DISPLAY ORDER, and it is the
# fallback when the shipped directories can't be read at all, so the eleven
# icons below are offered even on a box where the add-on hasn't copied its
# assets yet (a dry-run YAML build, a test, a first boot).
#
# It is NOT the whole list, and it is no longer where a NAME is edited.
# `stock_images()` merges this with whatever is actually on disk in
# `STOCK_IMAGE_DIRS`, so shipping a new default is a matter of committing the
# file alone; the customer-facing name comes from that directory's
# `labels.json` (see `_STOCK_LABELS_FILE`), which overrides these labels too.
# The labels here therefore only surface on the no-disk fallback path, and
# `test_stock_image_labels.py` pins them against `channel-icons/labels.json` so
# the two cannot drift. A line here is needed only to pin display position.
STOCK_CHANNEL_IMAGES: tuple[dict[str, str], ...] = (
    {"label": "Light", "url": "/local/sensa/channel-icons/light.svg"},
    {"label": "Fairy Lights", "url": "/local/sensa/channel-icons/fairy-lights.svg"},
    {"label": "Exterior / Sun", "url": "/local/sensa/channel-icons/sun.svg"},
    {"label": "Water", "url": "/local/sensa/channel-icons/water.svg"},
    {"label": "Water Pump", "url": "/local/sensa/channel-icons/water-pump.svg"},
    {"label": "Shower", "url": "/local/sensa/channel-icons/shower.svg"},
    {"label": "Heater", "url": "/local/sensa/channel-icons/heater.svg"},
    {"label": "Fan", "url": "/local/sensa/channel-icons/fan.svg"},
    {"label": "Fridge", "url": "/local/sensa/channel-icons/fridge.svg"},
    {"label": "Power / Socket", "url": "/local/sensa/channel-icons/power.svg"},
    {"label": "TV", "url": "/local/sensa/channel-icons/tv.svg"},
)

# Asset directories scanned for shipped defaults, relative to /config/www/sensa
# (served at /local/sensa/<dir>/<file>). They ship from `webcards/assets/<dir>/`,
# which `build.mjs` copies verbatim into `dist/` and the add-on's
# `install-webcards.sh` mirrors onto the box.
#
# `images/` is included (Richard, 2026-08-10): it holds the device product shots
# the card headers reference, and a relay channel driving a known device wants
# exactly that picture — the Autoterm shot on the channel that runs the heater.
#
# An SVG is mask-tinted by the switch tiles so it recolours with the on/off
# state; anything else renders as-is (`/\.svg($|\?)/i` in `sensa-switch-tile.ts`
# and `sensa-relay-tiles.ts`). Both are fine to ship, but an icon meant to track
# the tile state has to be an SVG.
STOCK_IMAGE_DIRS: tuple[str, ...] = ("channel-icons", "stock-images", "images")

_STOCK_IMAGE_EXTS: frozenset[str] = frozenset(
    {".svg", ".png", ".jpg", ".jpeg", ".webp"}
)

# Brand assets that live in `images/` but are not pictures OF anything: the PWA
# launcher icons and the bare logo. They ship there because the manifest and the
# dashboard header need them, and offering "Sensa Icon 512" as a switch picture
# is noise. Matched on the filename stem, so the extension can change freely.
_STOCK_IMAGE_EXCLUDED_STEMS: frozenset[str] = frozenset(
    {"sensa-icon-192", "sensa-icon-512", "sensa-transparent"}
)

# Per-directory name map, committed beside the assets it names:
# `webcards/assets/<dir>/labels.json`, `{"<filename>": "<what the customer
# reads>"}`. It exists because a filename is written for a developer and a label
# is read by a customer, and the two rarely want the same string:
# `ESP32-S3-ETH-8DI-8RO.webp` derives "ESP32 S3 ETH 8DI 8RO", and
# `smlight_slzb-06u.webp` derives "Smlight Slzb 06u". Renaming the FILE is not
# the fix — the catalog, the dashboards and the customers' saved picks all
# reference it by name.
#
# One file per directory rather than one central map: the name sits next to the
# thing it names, and adding a directory to `STOCK_IMAGE_DIRS` needs no code
# change here. It ships with no build or installer change either, since
# `build.mjs` and `install-webcards.sh` both copy these directories whole.
#
# Entirely optional and entirely forgiving: a missing file, unreadable JSON, a
# non-object, a key naming a file that does not exist, or a blank value all mean
# "no override", never an error. A picker must not lose its images over a stray
# comma, so every failure falls back to the derived name.
_STOCK_LABELS_FILE = "labels.json"

# Populated off the event loop by `async_refresh_stock_images`. None = never
# scanned, which reads as "fall back to the curated tuple" rather than "no
# images" — an unprimed cache must not empty the picker.
_stock_images_cache: tuple[dict[str, str], ...] | None = None


def _stock_label_from_filename(filename: str) -> str:
    """`fairy-lights.svg` → `Fairy Lights`. The label for a shipped default that
    is named neither in its directory's `labels.json` nor in
    `STOCK_CHANNEL_IMAGES`."""
    stem = filename.rsplit(".", 1)[0]
    words = [w for w in stem.replace("_", "-").split("-") if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or filename


def _load_stock_labels(path: str) -> dict[str, str]:
    """Read one asset directory's `labels.json` into `{filename: label}`.

    Blocking; called only from `_scan_stock_images`, which runs in an executor.

    Never raises and never partially fails: a bad file yields `{}` and the whole
    directory falls back to filename-derived names. Individual entries are
    filtered rather than rejecting the file, so one bad line costs one label.
    """
    import json
    import os

    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A committed file that does not parse IS worth saying out loud: the
        # developer meant those names to appear and they silently will not.
        _LOGGER.warning(
            "Stock image labels at %s could not be read; "
            "falling back to filename-derived names", path, exc_info=True,
        )
        return {}
    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Stock image labels at %s must be an object of "
            "{\"filename\": \"Label\"}; ignoring", path,
        )
        return {}
    labels: dict[str, str] = {}
    for name, label in raw.items():
        if isinstance(name, str) and isinstance(label, str) and label.strip():
            labels[os.path.basename(name)] = label.strip()
    return labels


def _dedupe_identical_images(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse byte-identical pictures to one offer each.

    The catalog ships one file per MODEL, and models routinely share a photo:
    `roamer-12105`, `roamer-gen1-generic` and `roamer-gen2-generic` are the same
    bytes three times, as are the Starlink, Teltonika, SmartSolar and Van Level
    pairs. Offering all of them puts eleven thumbnails in the picker that the
    customer cannot tell apart, so the picker shows the PICTURE once.

    This narrows what is OFFERED, never what exists: every file stays on disk at
    its own URL, so a picture already saved against a relay channel keeps
    rendering even when its file is no longer the one on offer.

    Survivor precedence, deterministic so the picker doesn't reshuffle between
    scans: a curated entry always wins (it is the fallback set, and its position
    is pinned), then one named in `labels.json` (a developer chose that name for
    this picture), then the first URL alphabetically. When the survivor reads
    badly, name it in `labels.json` rather than adding a rule here.

    Identity is the file bytes. Two pictures that merely look alike are two
    pictures, which is the honest answer: we cannot claim more than we compared.
    """
    import hashlib
    import os

    # Hashing every file on each scan would read the whole asset tree; files of
    # different SIZE cannot be identical, so only same-size groups are read.
    by_size: dict[int, list[dict[str, Any]]] = {}
    for cand in candidates:
        try:
            size = os.path.getsize(cand["path"])
        except OSError:
            size = -1  # unreadable: leave it in the list, on its own
        by_size.setdefault(size, []).append(cand)

    def _digest(path: str) -> str | None:
        try:
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return None

    def _rank(cand: dict[str, Any]) -> tuple[int, int, str]:
        return (0 if cand["curated"] else 1, 0 if cand["named"] else 1, cand["url"])

    dropped: set[str] = set()
    for size, group in by_size.items():
        if size < 0 or len(group) < 2:
            continue
        by_digest: dict[str, list[dict[str, Any]]] = {}
        for cand in group:
            digest = _digest(cand["path"])
            if digest is None:
                continue  # unreadable: never dropped on a failed comparison
            by_digest.setdefault(digest, []).append(cand)
        for identical in by_digest.values():
            if len(identical) < 2:
                continue
            keep = min(identical, key=_rank)
            for cand in identical:
                if cand["url"] != keep["url"]:
                    dropped.add(cand["url"])

    if dropped:
        _LOGGER.debug(
            "Stock images: %d duplicate file(s) not offered (identical bytes): %s",
            len(dropped), ", ".join(sorted(dropped)),
        )
    return [c for c in candidates if c["url"] not in dropped]


def _scan_stock_images(www_root: str) -> tuple[dict[str, str], ...]:
    """Blocking scan of the shipped asset dirs. Call via an executor job.

    Curated entries keep their position; anything else found on disk follows,
    sorted by label. A curated entry whose file is missing is kept regardless —
    the curated tuple is the fallback, and dropping an entry here because a scan
    came up short would empty the picker on a box mid-deploy.

    Label precedence: the directory's `labels.json` wins, else the curated
    tuple's own label, else the filename. `labels.json` overrides a CURATED
    label too, so there is one place a developer edits a name whichever list the
    file happens to be in — see `_STOCK_LABELS_FILE`. (The tuple's labels then
    only surface on the no-disk fallback path, where no `labels.json` was read
    either; `test_stock_image_labels.py` pins the two in step so the fallback
    cannot quietly show a name the box never does.)
    """
    import os

    candidates: list[dict[str, Any]] = []
    curated_urls: set[str] = {e["url"] for e in STOCK_CHANNEL_IMAGES}
    seen_urls: set[str] = set()
    # url -> label, collected across every directory's labels.json. Recorded for
    # curated URLs too (which are skipped as ENTRIES below), so a curated icon
    # can be renamed from the same file as everything else.
    overrides: dict[str, str] = {}

    for subdir in STOCK_IMAGE_DIRS:
        path = os.path.join(www_root, subdir)
        try:
            names = sorted(os.listdir(path))
        except OSError:
            continue
        labels = _load_stock_labels(os.path.join(path, _STOCK_LABELS_FILE))
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext.lower() not in _STOCK_IMAGE_EXTS:
                continue
            if stem in _STOCK_IMAGE_EXCLUDED_STEMS:
                continue
            full = os.path.join(path, name)
            if not os.path.isfile(full):
                continue
            url = f"/local/sensa/{subdir}/{name}"
            named = labels.get(name)
            if named:
                overrides[url] = named
            if url in seen_urls:
                continue
            seen_urls.add(url)
            candidates.append({
                "url": url,
                "path": full,
                "label": named or _stock_label_from_filename(name),
                "named": bool(named),
                "curated": url in curated_urls,
            })

    found = [
        c for c in _dedupe_identical_images(candidates) if not c["curated"]
    ]
    found.sort(key=lambda e: e["label"].casefold())
    curated = tuple(
        {**e, "label": overrides.get(e["url"], e["label"])}
        for e in STOCK_CHANNEL_IMAGES
    )
    return curated + tuple(
        {"label": c["label"], "url": c["url"]} for c in found
    )


async def async_refresh_stock_images(hass: Any) -> tuple[dict[str, str], ...]:
    """Rescan the shipped image dirs off the event loop and cache the result.

    Primed at setup (before the first dashboard build) and re-run when the
    Images tab asks for the list, so an image deployed onto a running box shows
    up without an HA restart. A failed scan leaves the previous cache alone.
    """
    global _stock_images_cache

    www_root = hass.config.path("www", "sensa")
    try:
        _stock_images_cache = await hass.async_add_executor_job(
            _scan_stock_images, www_root,
        )
    except Exception:  # a picker must never fail setup
        _LOGGER.warning("Stock image scan failed; keeping previous set", exc_info=True)
    return stock_images()


def stock_images() -> tuple[dict[str, str], ...]:
    """The shipped image set for the pickers.

    Synchronous by design: card configs are built on the loop and cannot await a
    disk scan, so this reads the cache primed by `async_refresh_stock_images` —
    the same shape as `_current_deployment_mode` / `_current_radar_url`.
    """
    if _stock_images_cache is None:
        return tuple(dict(e) for e in STOCK_CHANNEL_IMAGES)
    return tuple(dict(e) for e in _stock_images_cache)


def _integrated_device_options() -> list[dict[str, str]]:
    """`[{value: "device:<id>", label: <display name>}]` for every registered
    NON-relay device: the Integrated Devices a relay channel or a network-switch
    port can be pointed at.

    ONE derivation shared by both dropdowns' label maps. It must stay in step
    with `select.py::_integrated_device_choices`, which owns the raw option
    VALUES; a value the select offers but this does not name renders as a bare
    `device:<id>` token, and a label for a value the select never offers is
    simply dead. Relay controllers are excluded here for the same reason they
    are there (a relay cannot "work with" a relay).
    """
    options: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    from ..vendors.service_classes import is_relay_device

    for d in _dashboard_devices:
        if is_relay_device(d):
            continue
        d_id = str(d.get("device_id") or d.get("id") or "")
        if not d_id or d_id in seen_ids:
            continue
        seen_ids.add(d_id)
        label = d.get("display_name") or d.get("name") or d_id
        options.append({"value": f"device:{d_id}", "label": str(label)})
    return options


def _relay_works_with_options() -> list[dict[str, Any]]:
    """Build the grouped value→label map for the relay "Works with" dropdown.

    Two groups, mirroring the option values the assignment select entity
    offers (`select.py` `_integrated_device_choices` +
    `RELAY_ASSIGNABLE_SERVICE_CLASSES`):

      • Integrated Device — every registered NON-relay device, value
        `device:<id>`, label = display_name.
      • Switched Device — the relay-assignable service classes, value = the
        class key, label = its customer-facing `SERVICE_CLASSES` label,
        sorted A-Z by label so the group reads like the Integrated one.

    The select entity owns the raw option *values*; this only supplies their
    labels + grouping, so the card can render readable, grouped choices.
    """
    from ..vendors.service_classes import (
        RELAY_ASSIGNABLE_SERVICE_CLASSES,
        SERVICE_CLASSES,
    )

    integrated_options = _integrated_device_options()

    groups: list[dict[str, Any]] = []
    if integrated_options:
        groups.append({"group": "Integrated Device", "options": integrated_options})
    switched_options = sorted(
        (
            {"value": key, "label": SERVICE_CLASSES[key].label}
            for key in RELAY_ASSIGNABLE_SERVICE_CLASSES
        ),
        key=lambda o: o["label"].casefold(),
    )
    groups.append({"group": "Switched Device", "options": switched_options})
    return groups


_RELAY_IMAGE_DEFAULT = "ESP32-S3-ETH-8DI-8RO.webp"


def _relay_image_url(device: dict[str, Any]) -> str:
    """Product photo for a relay card, rooted under `/local/sensa/images/`.

    Same precedence the BMS card uses: whatever the row already carries wins,
    then the catalog entry's `image`, then the Sensa controller's own photo.
    The card used to hard-code the Sensa controller's photo, so an adopted
    Shelly Pro 4PM showed a picture of a Waveshare board (customer box,
    2026-09-23).
    """
    from ..device_catalog import relay_catalog_entry

    entry = relay_catalog_entry(device)
    image_file = (
        str(device.get("image") or "").strip()
        or str(entry.get("image") or "").strip()
        or _RELAY_IMAGE_DEFAULT
    )
    return (
        image_file if image_file.startswith("/")
        else f"/local/sensa/images/{image_file}"
    )


def _build_relay_card(device: dict[str, Any]) -> dict[str, Any]:
    """Build a control card for a single Sensa Relay Controller.

    Layout:
      • Header row — product image (left) + editable device-name input
        and info table (right). Mirrors the Bluetooth-proxy card.
      • Channel table — one row per channel with name input + enabled
        toggle + ESPHome relay toggle. How MANY rows comes from the catalog
        via `relay_channel_count`, not from a fixed 8: an adopted Shelly Pro
        4PM has four.
    """
    from ..device_catalog import relay_channel_count

    display_name = (
        device.get("display_name")
        or device.get("name")
        or device.get("vendor")
        or "Sensa Relay Controller"
    )
    hostname = device.get("linked_hostname") or device.get("hostname") or "-"
    ip = device.get("ip_address") or device.get("ip") or "-"
    model = device.get("model") or device.get("vendor_model") or ""
    version = device.get("version") or "-"

    relay_entities = device.get("relay_entities") or {}
    relay_name_entities = device.get("relay_name_entities") or {}
    relay_enabled_entities = device.get("relay_enabled_entities") or {}
    relay_assignment_entities = device.get("relay_assignment_entities") or {}
    relay_image_entities = device.get("relay_image_entities") or {}
    relay_images = device.get("relay_images") or {}
    relay_device_name_entity = device.get("relay_device_name_entity") or ""
    # One channel config per slot for the sensa-relay-channels webcard.
    # Skip channels with neither a name nor enabled entity (shouldn't
    # happen for a registered relay device but stays defensive). The
    # ESPHome relay entity is optional — when ESPHome hasn't connected
    # yet the relay column renders a dim placeholder and the row still
    # shows the editable name + enabled toggle.
    channels: list[dict[str, Any]] = []
    for n in range(1, relay_channel_count(device) + 1):
        slot = f"relay_{n}"
        name_eid = relay_name_entities.get(slot)
        enabled_eid = relay_enabled_entities.get(slot)
        if not name_eid and not enabled_eid:
            continue
        channels.append({
            "n": n,
            "relay": relay_entities.get(slot),
            "name": name_eid,
            "enabled": enabled_eid,
            "assignment": relay_assignment_entities.get(slot),
            # Per-channel picture: the image text entity (the picker writes the
            # chosen URL to it) + the current URL (for the picker thumbnail).
            "image": relay_image_entities.get(slot),
            "image_url": relay_images.get(slot) or "",
        })

    # A single NATIVE `custom:sensa-relay-channels` card — product image + info
    # header folded in. Previously this was a `custom:vertical-stack-in-card`
    # (HACS) wrapping a built-in `markdown` header + the channels card; both
    # needed HA's Lovelace engine, so on the Settings Switching tab it sat on
    # "Loading" after a fresh restart / went blank on a hard refresh until the
    # engine force-loaded (it only rendered after navigating away and back).
    # Going native means the `sensa-card-grid` shell direct-creates it with no
    # engine, like the other migrated Settings tabs.
    channel_card: dict[str, Any] = {
        "type": "custom:sensa-relay-channels",
        "title": display_name,
        "image": _relay_image_url(device),
        "info": [
            {"label": "Hostname", "value": hostname},
            {"label": "IP Address", "value": ip},
            {"label": "Model", "value": model or "-"},
            {"label": "Firmware", "value": version},
        ],
        "channels": channels,
        "works_with_options": _relay_works_with_options(),
        # Stock image presets for the per-channel image picker; the customer's
        # uploaded images are fetched live by the card via `sensa_nomad/list_images`.
        "image_options": [dict(o) for o in stock_images()],
    }
    if relay_device_name_entity:
        channel_card["device_name"] = relay_device_name_entity
    return channel_card


# Module-level store for devices passed at dashboard generation time.
# Set by the config flow before dashboard generation runs.
_dashboard_devices: list[dict[str, Any]] = []
# Optional HA `hass` reference, also stashed at dashboard-gen time so the
# role resolver can probe entity state (skip unavailable per-device
# sources so the system aggregate fallback kicks in). None when called
# from tests or before HA is set up — the resolver then trusts whatever
# entity_id strings are in the role-maps without state-checking, which
# matches the historical behaviour.
_dashboard_hass: Any = None


def set_dashboard_devices(
    devices: list[dict[str, Any]], hass: Any = None,
) -> None:
    """Store discovered devices for dashboard generation.

    `hass` is optional; when passed, `resolve_role` filters out entities
    whose current state is unavailable / unknown so the system-aggregate
    fallback can supply the slot instead. Without it, role-map entries
    are taken at face value (test-friendly).
    """
    global _dashboard_devices, _dashboard_hass, _light_group_candidates
    _dashboard_devices = list(devices)
    # The device list is what decides which lights may join a group, so this is
    # the one place that answer can change.
    _light_group_candidates = None
    if hass is not None:
        _dashboard_hass = hass


def get_dashboard_devices() -> list[dict[str, Any]]:
    """Return the cached enriched device list (relay assignments / role
    maps attached). The freshest device set the dashboard/modes pipeline
    has seen; used by the Mode Editor's Defaults generator so it sees the
    same `relay_assignments` the routed-switch tiles do."""
    return list(_dashboard_devices)


def _current_deployment_mode() -> str:
    """Return the cached install flavour, defaulting to BYO.

    Reads `hass.data[DOMAIN][DEPLOYMENT_MODE_KEY]` populated at integration
    setup by `_async_refresh_deployment_mode`. Falls back to `byo` when
    hass isn't available (tests, dry-run YAML builds) — the safer
    default since BYO leaves chrome alone.
    """
    # Local import to avoid a top-of-module cycle (const → __init__
    # → dashboard). Resolution is cheap.
    from ..const import DEPLOYMENT_MODE_DEFAULT, DEPLOYMENT_MODE_KEY, DOMAIN

    if _dashboard_hass is None:
        return DEPLOYMENT_MODE_DEFAULT
    data = getattr(_dashboard_hass, "data", None) or {}
    return (data.get(DOMAIN) or {}).get(DEPLOYMENT_MODE_KEY, DEPLOYMENT_MODE_DEFAULT)


def _current_radar_url() -> str:
    """Return the cached per-env Sensa Radar base URL, or "" when unset.

    Reads `hass.data[DOMAIN][RADAR_URL_KEY]`, populated at setup by
    `_async_refresh_deployment_mode` off the app's /health (which carries the
    add-on's `radar_url`, set per environment by the pipeline from the
    `sensaRadarUrl` variable groups). Same cache-read shape as
    `_current_deployment_mode` and for the same reason: card configs are built
    synchronously, so the generator cannot await the add-on.

    "" when hass isn't available (tests, dry-run YAML builds) or Radar isn't
    configured for this environment. NO URL IS BAKED IN HERE: the cards must
    degrade to the HA map rather than point at the prod Radar from a dev box.
    """
    from ..const import DOMAIN, RADAR_URL_KEY

    if _dashboard_hass is None:
        return ""
    data = getattr(_dashboard_hass, "data", None) or {}
    return str((data.get(DOMAIN) or {}).get(RADAR_URL_KEY) or "").strip().rstrip("/")


def _radar_url_config() -> dict[str, Any]:
    """`{"radar_url": ...}` for a nav-map card, or `{}` when Radar is unset.

    Spread into every `custom:sensa-nav-map` config so the card builds its Radar
    iframe from the environment's own Radar, not a hardcoded host. Omitted (not
    empty-stringed) when unconfigured, so the card's own "no Radar" fallback
    applies.
    """
    url = _current_radar_url()
    return {"radar_url": url} if url else {}




# Switched-Device relay routing → conditional Habitation tabs. Each ENABLED
# relay channel the customer assigned a Switched-Device "Device Type" becomes a
# control tile on its category's tab. (`device:<id>` Integrated-Device links are
# Phase 3 — a power toggle on the linked device's own card, not a tab here.)
# (service-class categories, tab slug, tab label, tab icon) — also the tab order.
# Each vendor-neutral service-class category gets its own customer-facing tab:
# **Lighting**, **Water** (water pumps / heaters / tanks), and **Climate**
# (heating, ventilation, AC, refrigeration). Water used to be folded into
# Climate but was split out into its own conditional tab (Richard, 2026-05-31:
# "anything category water should go on a new Water tab"). Each tab is
# conditional — present only when its category has content.
_SWITCHED_TABS: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    # Label is "Lights", slug stays "lighting": the slug is in URLs, the
    # per-user default-panel store and every test, and is not customer-facing.
    (("lighting",), "lighting", "Lights", "mdi:lightbulb-group"),
    (("water",), "water", "Water", "mdi:water"),
    (("climate",), "climate", "Climate", "mdi:thermometer"),
)


def _relay_channel_numbers(device: dict[str, Any]) -> list[int]:
    """Every physical channel number on a relay controller, sorted.

    The Switching-tab tile card shows ONE tile per channel regardless of
    whether it's enabled (Richard, 2026-05-30), so we need the full physical
    channel set — the union of every per-slot relay map's keys (`relay_N`).
    `relay_enabled` carries one entry per channel (its enabled toggle), so it's
    the most complete source, but we union all the relay_* maps defensively so
    a channel that only appears in one of them is still counted.
    """
    present: set[int] = set()
    for key in (
        "relay_enabled",
        "relay_labels",
        "relay_entities",
        "relay_name_entities",
        "relay_enabled_entities",
        "relay_assignment_entities",
    ):
        for slot in (device.get(key) or {}):
            tail = str(slot).removeprefix("relay_")
            if tail.isdigit():
                present.add(int(tail))
    return sorted(present)


def _relay_controller_tile_cards() -> list[dict[str, Any]]:
    """One `custom:sensa-relay-tiles` card per registered relay controller,
    each holding an on/off tile for every ENABLED physical channel. A metered
    relay (a Shelly Pro 4PM) gets `custom:sensa-switches-card` instead: the same
    tiles with each circuit's live watts and the unit's total.

    Drives the Habitation **Switching** tab. Built 2026-05-30 with a tile for
    every channel, disabled ones greyed out; since 2026-09-08 (Richard) a
    disabled channel is not shown at all, so the tab carries only what the
    customer can press. A relay with no enabled channel yields no card, and
    the tab falls back to its "No channels enabled" hint. The same
    `sensa-relay-tiles` card also renders the routed Lighting/Water/Climate
    channel buttons, so all relay switches match. A card-per-relay reads as a
    physical control panel.

    Each enabled channel gets a tile (`_relay_channel_numbers`), carrying:
      • `entity` — the ESPHome relay switch (None until the device hydrates —
        the card then renders the tile dim/non-interactive rather than hiding
        it, so a channel is not lost while the controller connects);
      • `name` — the customer's `relay_labels[slot]`, else "Channel N";
      • `enabled` — always True here (a missing slot counts as enabled).

    Reads the relay rows enriched by `_attach_relay_entities`; the card header
    is the relay's Discovery display name. A relay with no channels at all
    (shouldn't happen) yields no card.

    A Switching tile is a dumb relay toggle whatever Device Type the channel
    is assigned (Richard, 2026-09-08: "the relay channel shouldn't care what's
    connected, it's just a channel"). It never carries `light_entity`: linking
    the tile to an LED controller's light made the tile follow the light's
    last reported state, and Zigbee2MQTT keeps that at "on" after the relay
    cuts the controller's power, so the tile read On and every tap went to the
    unpowered controller instead of the relay (customer 5de4d9a78fe7). The
    combined relay-then-light power-on stays on the LED card's own button.
    """
    cards: list[dict[str, Any]] = []
    for device in _dashboard_devices:
        if not is_relay_device(device):
            continue
        relay_entities = device.get("relay_entities") or {}
        relay_enabled = device.get("relay_enabled") or {}
        relay_labels = device.get("relay_labels") or {}
        relay_images = device.get("relay_images") or {}
        # A metered relay (a Shelly Pro 4PM) reports each circuit's power, and
        # gets `sensa-switches-card`: the same tiles plus live watts per circuit
        # and the unit's total. Keyed on the metering, not the brand.
        relay_power = device.get("relay_power_entities") or {}
        # An adopted relay (a Shelly) has exactly the circuits its own entry
        # reports; a Sensa board has eight terminals by construction.
        adopted = str(
            device.get("integration_type") or device.get("integration") or ""
        ).lower() == "shelly"
        tiles: list[dict[str, Any]] = []
        for n in _relay_channel_numbers(device):
            slot = f"relay_{n}"
            if relay_enabled.get(slot) is False:
                continue  # disabled channels are not on the Switches tab
            if adopted and not relay_entities.get(slot):
                # Every relay is given eight channel slots; a 4PM has four
                # circuits, so a slot with no switch behind it is not one. A
                # Sensa board keeps all eight: its terminals exist whether or
                # not the firmware has reported them yet.
                continue
            tile: dict[str, Any] = {
                "entity": relay_entities.get(slot),
                "name": relay_labels.get(slot) or f"Channel {n}",
                "icon": "mdi:power",
                # The customer's chosen picture (stock icon or uploaded); the
                # tile renders it inside the knob, falling back to the icon.
                "image": relay_images.get(slot) or None,
                "enabled": True,
            }
            if relay_power.get(slot):
                tile["power_entity"] = relay_power[slot]
            tiles.append(tile)
        if not tiles:
            continue
        if relay_power:
            cards.append({
                "type": "custom:sensa-switches-card",
                # The card's key from before its rename (it was
                # sensa-circuit-tiles), so a place saved in Design Mode holds.
                "reorder_key": "custom:sensa-circuit-tiles",
                "full_width": True,
                # Named, unlike the relay controllers' cards: the header also
                # carries the unit's total draw, and that needs a subject.
                "name": device.get("display_name") or device.get("name") or "Circuits",
                "tiles": tiles,
                # Every metered circuit, enabled or not, so the total is what
                # the unit is actually drawing.
                "total_entities": [
                    relay_power[k] for k in sorted(
                        relay_power, key=lambda s: int(s.rsplit("_", 1)[1]),
                    )
                ],
            })
            continue
        cards.append({
            "type": "custom:sensa-relay-tiles",
            # Each relay controller spans the full grid width (Richard,
            # 2026-05-30) — the channel tiles flex-wrap inside, so a wide card
            # reads as the physical controller rather than a cramped half-cell.
            "full_width": True,
            # No card header on the Switching tab (Richard, 2026-06-13: the
            # relay name isn't needed here and only makes the card taller). The
            # card still supports `name` for the Cards gallery / demo.
            "tiles": tiles,
        })
    return cards


def _dc_meter_cards() -> list[dict[str, Any]]:
    """The Electric tab's per-circuit DC power: one `sensa-switches-card` per
    Sensa DC Meter, a read-only tile per circuit with its live watts, and the
    meter's total in the header.

    The tile is the relay tile without the switch (`readonly`), so a metered
    DC circuit reads exactly like a metered Shelly circuit on the Switches tab.
    Names are the customer's, set on the meter's Settings -> Devices row.
    """
    from ..const import DOMAIN
    from ..dc_meter import circuit_name

    meters = _dc_meter_circuits()
    if not meters:
        return []
    entry = None
    try:
        entries = _dashboard_hass.config_entries.async_entries(DOMAIN)
        entry = entries[0] if entries else None
    except Exception:  # noqa: BLE001 - no names is "Circuit <n>", not a broken tab
        _LOGGER.debug("DC meter: could not read circuit names", exc_info=True)
    cards: list[dict[str, Any]] = []
    for device, power in meters:
        device_id = str(device.get("device_id") or device.get("id") or "")
        circuits = sorted(power)
        cards.append({
            "type": "custom:sensa-switches-card",
            "full_width": True,
            "name": device.get("display_name") or device.get("name") or "DC Circuits",
            "tiles": [
                {
                    "name": circuit_name(entry, device_id, n),
                    "icon": "mdi:current-dc",
                    "power_entity": power[n],
                    "readonly": True,
                    "enabled": True,
                }
                for n in circuits
            ],
            "total_entities": [power[n] for n in circuits],
        })
    return cards


def _has_relay_device() -> bool:
    """True iff at least one relay controller is registered. Used to gate the
    Habitation Switching tab — the tab appears as soon as a relay exists,
    even if no channels are enabled yet (lets the customer find the empty
    surface and know where to enable channels)."""
    from ..vendors.service_classes import is_relay_device

    return any(is_relay_device(device) for device in _dashboard_devices)


def _has_gps_source() -> bool:
    """True iff a vehicle-GPS source is registered — today that's a Teltonika
    router (the only active GPS provider; Starlink GPS was retired). Gates the
    Intelligence → Schengen tab: the tracker stamps each day's country from the
    `device_tracker.sensa_nomad_vehicle` position, which sources from this."""
    for device in _dashboard_devices:
        if (
            device.get("integration") == "sensa_teltonika_api"
            or device.get("integration_type") == "sensa_teltonika_api"
        ):
            return True
    return False


def _routed_switch_tiles_by_category() -> dict[str, list[dict[str, Any]]]:
    """One `sensa-relay-tiles` TILE config (`entity`/`name`/`icon`/`image`/
    `enabled`) for every ENABLED relay channel the customer assigned a
    Switched-Device type, grouped by the assigned service class's category
    (lighting / water / climate).

    The caller (`_build_test_yaml_data`'s switched-tab loop) wraps each
    category's tiles in a single `custom:sensa-relay-tiles` card — the SAME
    small, packed, image-forward button panel the Settings/Switching tab uses
    — so the Lighting/Water/Climate buttons match the Switching tab instead of
    being one big `sensa-card-grid` cell each.

    Each tile toggles the channel's ESPHome relay entity. Channels that are
    disabled, unassigned, assigned an Integrated Device (`device:<id>` — Phase
    3), or have no resolved relay entity yet (ESPHome not connected) are
    skipped. Reads the relay rows enriched by `_attach_relay_entities`
    (`relay_assignments` / `relay_entities` / `relay_enabled` / `relay_labels`
    / `relay_images`).
    """
    from ..vendors.service_classes import (
        RELAY_ASSIGNABLE_SERVICE_CLASSES,
        SERVICE_CLASSES,
    )

    by_cat: dict[str, list[dict[str, Any]]] = {}
    for device in _dashboard_devices:
        if not is_relay_device(device):
            continue
        assignments = device.get("relay_assignments") or {}
        relay_entities = device.get("relay_entities") or {}
        relay_enabled = device.get("relay_enabled") or {}
        relay_labels = device.get("relay_labels") or {}
        relay_images = device.get("relay_images") or {}
        for n in range(1, 9):
            slot = f"relay_{n}"
            assignment = assignments.get(slot, "unassigned")
            if assignment not in RELAY_ASSIGNABLE_SERVICE_CLASSES:
                continue
            if relay_enabled.get(slot) is False:  # disabled channel
                continue
            relay_eid = relay_entities.get(slot)
            if not relay_eid:  # ESPHome relay not connected yet → can't toggle
                continue
            sc = SERVICE_CLASSES[assignment]
            by_cat.setdefault(sc.category, []).append({
                "entity": relay_eid,
                "name": relay_labels.get(slot) or f"Relay {n}",
                "icon": sc.icon,
                # The customer's per-channel picture (the same one the Switching
                # tab shows), so a lighting/water/climate tile matches its
                # Switching-tab tile. Omitted when unset → the icon shows.
                "image": relay_images.get(slot) or None,
                # Routed channels are already filtered to enabled; the relay-
                # tiles card greys disabled tiles, so flag these enabled.
                "enabled": True,
            })
    return by_cat


def _has_live_alarm() -> bool:
    """True when at least one managed alarm is live (active or snoozed).

    Drives the conditional Habitation **Alarms** tab. The alarm store fires
    `sensa_nomad_panel_config_changed` when this 0↔>0 boundary crosses, so an
    open panel re-fetches its config and the tab appears / disappears live."""
    if _dashboard_hass is None:
        return False
    try:
        from .._alarms import get_alarm_store
        store = get_alarm_store(_dashboard_hass)
        return bool(store and store.snapshot())
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return False


# --- Intelligence dashboard ------------------------------------------------
#
# The "how my van behaves" hub: the notification subsystem (Alarms +
# Automations + Log) plus Scenes — the on-command cousin of Automations (both
# are coordinated, multi-device behaviour). These live on their own top-level
# panel rather than being scattered across Settings (Scenes / Automations /
# Log) and a conditional Habitation Alarms tab. Grouping them here means the
# cross-panel alarm banner has ONE always-present destination — the dashboard
# is never empty (Scenes + Automations + Log are always there) so the banner
# can never point at a missing surface (the "banner shows but no Alarms tab"
# bug). The tab builders are module-level so this builder reuses them directly.

def _build_alarms_tab() -> dict[str, Any]:
    # Intelligence → Alarms (conditional — present only while a live alarm
    # exists). The live managed-alarm list with per-row Snooze / Ignore.
    # Full-width native card; no Lovelace engine. The cross-panel banner
    # surfaces an active alarm from every panel regardless.
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "cards": [
            {
                "type": "custom:sensa-alarms-card",
                "title": "",
                "full_width": True,
            },
        ],
    }


def _build_automations_tab() -> dict[str, Any]:
    # Intelligence → Automations. A Sensa-curated list of suggested automations
    # the customer toggles on/off (with light config like a threshold); Sensa
    # generates the underlying HA automations, whose action writes to the Sensa
    # log or raises a managed alarm. One native `sensa-automations-card` in a
    # `sensa-card-grid` (no Lovelace engine). The card reads
    # `sensor.sensa_nomad_automations` and calls `sensa_nomad.set_automation`.
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        # Span the full panel width (no 1600px centred cap) — the routines list
        # wants every pixel across the screen.
        "full_bleed": True,
        "cards": [
            {
                "type": "custom:sensa-automations-card",
                # No card header - the tab is already labelled "Instincts"; an
                # empty title suppresses the redundant `ha-card` header.
                "title": "",
                "full_width": True,
            },
        ],
    }


def _build_log_tab(source: str) -> dict[str, Any]:
    # The customer-facing message log — NOT the HA system log. ONE builder for
    # BOTH surfaces, so the two tabs can never drift apart in shape:
    #   • Intelligence → Log        (`vehicle`) — low water, proximity alerts,
    #     an instinct firing, a cold-charge cut-off. The van talking about
    #     itself.
    #   • Settings → System Log     (`system`) — backups, updates, licence
    #     check-ins, devices added or removed. The box talking about itself.
    # Both always present. One native `sensa-log-card` in a `sensa-card-grid`
    # (no Lovelace engine). The card reads its own source's entries via the
    # `sensa_nomad/get_log` WS command, watches `sensor.sensa_nomad_log` for new
    # entries, filters by message type client-side, and clears via
    # `sensa_nomad/clear_log` — scoped, so clearing one leaves the other alone.
    #
    # `source` is emitted EXPLICITLY on both tabs, including the vehicle one
    # that matches the card's own default, so a generated dashboard says which
    # log it means rather than relying on a default two layers away.
    from ..const import SENSA_LOG_SOURCE_SYSTEM

    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        # The Settings System Log is one whole-page card: nothing for Design
        # Mode to arrange. (The Intelligence Log stays arrangeable.)
        **({"arrangeable": False} if source == SENSA_LOG_SOURCE_SYSTEM else {}),
        "cards": [
            {
                # No in-card header — the tab is already titled, so it would
                # just waste vertical space.
                "type": "custom:sensa-log-card",
                "source": source,
                "full_width": True,
            },
        ],
    }


def _build_scenes_tab() -> dict[str, Any]:
    # Intelligence → Scenes. The mode/scene editor (authors what each mode —
    # Sleep / Movie / Work… — does per device). The tab slug is "scenes" (the
    # URL hash; was "modes" until 2026-09-17); the card, entity ids and WS
    # commands still say mode.
    # Moved here from Settings 2026-06-24 (the on-command cousin of Automations).
    return {"type": "custom:sensa-mode-editor"}


def _build_schengen_tab() -> dict[str, Any]:
    # Intelligence → Schengen. The 90/180 usage tracker — an Enable button, a
    # "X / 90 days used" headline with a must-leave-by date, and an editable
    # month calendar. Gated on a GPS source existing (see _has_gps_source). The
    # native card reads `sensa_nomad/schengen_data` and edits via the
    # `schengen_*` services; one card in a `sensa-card-grid` (no Lovelace engine).
    return {
        "type": "custom:sensa-card-grid",
        "hide_header": True,
        "cards": [
            {
                "type": "custom:sensa-schengen-card",
                "title": "Schengen",
                "full_width": True,
            },
        ],
    }








# --- dashboard builders ------------------------------------------------------
# One module per dashboard, imported HERE at the bottom rather than at the top:
# each builder pulls the card helpers back out of this package, so everything above
# must already be defined. The registries below name them.
from .build_cab import _build_cab_yaml_data
from .build_habitation import (  # noqa: F401
    _build_habitation_yaml_data,
    _build_habitation_yaml_data_legacy,
    _build_test_trial_yaml_data,
    _build_test_yaml_data,
)
from .build_intelligence import _build_intelligence_yaml_data
from .build_mobile import _build_mobile_yaml_data
from .build_settings import _build_settings_yaml_data

DASHBOARD_BUILDERS = {
    "nomad-habitation.yaml": _build_habitation_yaml_data,
    "nomad-cab.yaml": _build_cab_yaml_data,
    "nomad-mobile.yaml": _build_mobile_yaml_data,
    "nomad-settings.yaml": _build_settings_yaml_data,
    "nomad-test-trial.yaml": _build_test_trial_yaml_data,
}

# Panel key → dashboard builder, consumed by `build_panel_config`. Same
# builders as DASHBOARD_BUILDERS — the panels and the Lovelace fallback
# dashboards render identical content; only the host (panel vs Lovelace
# view) differs.
def grid_card_catalog(
    built: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Every arrangeable grid tab on every panel dashboard, for the Design Mode
    card gallery and for resolving a card added from another tab
    (grid_layout.grid_catalog). `built` carries tabs already built this call,
    so a dashboard is not built twice. A dashboard whose builder fails is left
    out rather than taking the gallery down."""
    from ..grid_layout import card_keys, grid_catalog

    dashboards: dict[str, list[dict[str, Any]]] = dict(built or {})
    for key, builder in _PANEL_BUILDERS.items():
        if key in dashboards:
            continue
        try:
            dashboards[key] = builder()["views"][0]["cards"][0].get("tabs") or []
        except Exception:  # one broken dashboard must not hide the rest
            _LOGGER.debug("card catalog: %s builder failed", key, exc_info=True)
    catalog = grid_catalog(dashboards)
    entries = library_cards()
    keys = card_keys([cfg for cfg, _ in entries])
    catalog[LIBRARY_ID] = {
        "dashboard": "library",
        "slug": "cards",
        "label": "Card library",
        "cards": {k: cfg for k, (cfg, _) in zip(keys, entries, strict=True)},
        "suggested": {k: tabs for k, (_, tabs) in zip(keys, entries, strict=True)},
    }
    return catalog


# The card library: cards no tab builds, offered in the Design Mode gallery
# and added by reference ("library/cards::<key>") like a card from a tab.
LIBRARY_ID = "library/cards"


def library_cards() -> list[tuple[dict[str, Any], list[str]]]:
    """Each library card with the tabs (layout ids) it suits; the gallery
    offers it first under "Suggested for this tab" there."""
    from .build_settings import data_usage_card

    out: list[tuple[dict[str, Any], list[str]]] = [
        (data_usage_card(), ["habitation/network"]),
        # Weather at the van (weather_sensors.py): owners add it in Design Mode.
        ({"type": "custom:sensa-weather-card"}, ["habitation/home", "cab/overview"]),
    ]
    # One temperature gauge per temperature sensor, one tank gauge per tank.
    for gauge in _temperature_gauges(_all_temperature_entities()):
        out.append((
            {
                "type": "custom:sensa-gauge-single-temperature",
                "entity": gauge["entity"],
                "name": gauge["name"],
            },
            ["habitation/climate", "habitation/home"],
        ))
    for fluid, tanks in _tank_levels_by_fluid().items():
        label, high_is_good, color = _TANK_FLUID_DIALS[fluid]
        for eid, device in tanks:
            name = str(device.get("display_name") or device.get("name") or "")
            if not name or _VENUS_TANK_FALLBACK.match(name):
                name = label
            out.append((
                {
                    "type": "custom:sensa-gauge-single-tank",
                    "entity": eid,
                    "name": name,
                    "fills": not high_is_good,
                    "color": color,
                },
                ["habitation/water", "habitation/home"],
            ))
    return out


_PANEL_BUILDERS = {
    "habitation": _build_habitation_yaml_data,
    "cab": _build_cab_yaml_data,
    "mobile": _build_mobile_yaml_data,
    "intelligence": _build_intelligence_yaml_data,
    "settings": _build_settings_yaml_data,
}


# Deferred to module-end (after the builder registry above) to keep the io
# helpers grouped with their sole use in _write_dashboard_yaml.
