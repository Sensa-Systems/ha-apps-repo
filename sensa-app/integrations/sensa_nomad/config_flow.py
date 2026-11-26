"""Config flow for the Sensa Nomad integration."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.data_entry_flow import FlowResult

from .api.client import SensaApiClient
from .backend_url import resolve_backend_url
from .const import (
    CONF_BACKEND_URL,
    CONF_CONTACT_EMAIL,
    CONF_CUSTOMER_NAME,
    CONF_PROFILE_NAME,
    CONF_REDEMPTION_CODE,
    DEFAULT_PROFILE_NAME,
    DOMAIN,
    PURCHASE_URL,
    SIM_MAC_OUI,
)
from .device_catalog import CATEGORY_ORDER, PHYSICAL_DEVICE_CATALOG
from .vendors.service_classes import category_of, classify

_LOGGER = logging.getLogger(__name__)
UNIQUE_ID = "sensa_nomad_singleton"

# ---------------------------------------------------------------------------
# Two independent catalog gates decide whether an entry takes part in discovery
# at all. Both hide the entry across EVERY discovery method (BLE / native / dhcp
# / static_probe / mqtt_subnet / esphome_http) AND catalog enrichment, so a
# matching physical device falls to the hidden Others group.
#
#   `discovery_enabled: false`  — HARD off. Ship an entry (and all its code /
#       dashboard wiring) before the device is ready to surface at all. Nothing
#       at runtime can turn it on. Absent/true = enabled (opt-out).
#
#   `experimental: true`        — SOFT off. The device works, but isn't ready
#       for customers. Hidden by default; the `Experimental Devices` switch
#       (`switch.sensa_nomad_experimental_devices`, off by default) turns the
#       whole experimental set on for a dev rig / simulator. Absent = normal
#       (opt-in).
#
# A dev box enables the switch to see the Autoterm / Alde / Dometic controllers;
# a customer never does, so those devices stay invisible in a shipped release
# without anyone remembering to flip a catalog field before the pipeline runs.
# ---------------------------------------------------------------------------

EXPERIMENTAL_DEVICES_ENTITY_ID = "switch.sensa_nomad_experimental_devices"


def experimental_devices_enabled(hass: Any) -> bool:
    """True when the customer/dev has switched experimental devices on.

    Fails CLOSED (experimental stays hidden) when the switch is missing, the
    state machine isn't up yet, or `hass` is None — a discovery pass that runs
    before the switch entity registers must not leak an experimental device
    into the picker.
    """
    if hass is None:
        return False
    try:
        state = hass.states.get(EXPERIMENTAL_DEVICES_ENTITY_ID)
    except Exception:  # pragma: no cover - defensive, states may be unavailable  # noqa: BLE001
        return False
    return bool(state) and state.state == "on"


def _entry_hidden(entry: dict[str, Any], include_experimental: bool) -> bool:
    """True when this catalog entry must not take part in discovery."""
    if entry.get("discovery_enabled") is False:
        return True
    return bool(entry.get("experimental")) and not include_experimental


# Build suppression map from device catalog ignore_native field. Hidden entries
# are skipped so an in-development device's native flow is neither suppressed
# nor auto-accepted while it's hidden (finding [6]).
#
# NOTE: these two maps are frozen at import with `include_experimental=False`,
# so the Experimental Devices switch does NOT reach them. That is fine only
# because no `experimental: true` entry carries `ignore_native` / `accept_native`
# today — pinned by `test_catalog_integrity.py::
# test_no_experimental_entry_uses_native_maps`. If you need to flag a
# cooperate-flow device (one with either key) experimental, these must become
# functions of the switch first, or its native flow will stay unsuppressed /
# unaccepted even with experimental turned on.
SUPPRESS_NATIVE_INTEGRATIONS: dict[str, str] = {
    item["integration"]: item["ignore_native"]
    for item in PHYSICAL_DEVICE_CATALOG
    if item.get("ignore_native") and not _entry_hidden(item, False)
}

# Build acceptance map from device catalog accept_native field.
ACCEPT_NATIVE_INTEGRATIONS: dict[str, str] = {
    item["integration"]: item["accept_native"]
    for item in PHYSICAL_DEVICE_CATALOG
    if item.get("accept_native") and not _entry_hidden(item, False)
}

# ---------------------------------------------------------------------------
# Discovery profiles — DERIVED from device_catalog.json (the single source of
# truth). Each catalog entry declares how it's found via a nested `discovery`
# block keyed by method (`dhcp` / `native` / `ble` / `static_probe` /
# `mqtt_subnet` / `esphome_http`); a device may declare several (the Cerbo is
# found by DHCP, BLE and the MQTT subnet scan). The builders below flatten the
# relevant entries into the exact profile-dict shapes the discovery functions,
# the BLE matchers (`_match_ble_profile`, `_ble_profile_matches`) and the
# add-flow already expect, so those consumers are unchanged — only the source
# of the data moved out of hardcoded lists / ble_profiles.json and into the
# catalog. `tests/test_catalog_unification.py` pins the derived lists against a
# snapshot of the old hardcoded values.
# ---------------------------------------------------------------------------


def _entries_with_discovery(
    method: str, include_experimental: bool = False
) -> list[tuple[dict[str, Any], Any]]:
    """Catalog entries declaring discovery `method`, paired with its block.

    Applies both catalog gates (see `_entry_hidden`), so a hidden device is
    hidden across EVERY discovery method (BLE / native / dhcp / static_probe /
    mqtt_subnet / esphome_http), not just catalog enrichment (code-review
    finding [6]). `include_experimental` defaults False — the caller must opt in
    by reading the Experimental Devices switch."""
    out: list[tuple[dict[str, Any], Any]] = []
    for entry in PHYSICAL_DEVICE_CATALOG:
        if _entry_hidden(entry, include_experimental):
            continue
        block = (entry.get("discovery") or {}).get(method)
        if block:
            out.append((entry, block))
    return out


def _base_profile(entry: dict[str, Any]) -> dict[str, Any]:
    """Fields every discovery profile shares, mapped from a catalog entry.

    `device_type` mirrors the catalog `vendor_device_type` (the value the
    discovery row carries and `_enrich_from_catalog` matches on). `category`
    is the entry's view-routing bucket (derived from `service_class`)."""
    return {
        "name": entry["name"],
        "device_type": entry["vendor_device_type"],
        "vendor_model": entry["vendor_model"],
        "integration": entry["integration"],
        "category": category_of(entry["service_class"]),
        # The class itself, not only the category derived from it. Registration
        # sends it as the device's `role`, and the Zigbee coordinator resolver
        # looks for `zigbee_coordinator` there: keeping only the category meant
        # a coordinator adopted through a native flow registered as `unknown`,
        # so Z2M was never repointed and stayed bound to the previous radio.
        "service_class": entry["service_class"],
    }


def _build_dhcp_profiles(include_experimental: bool = False) -> list[dict[str, Any]]:
    """Non-ESPHome network devices found via HA's DHCP cache by MAC OUI."""
    profiles: list[dict[str, Any]] = []
    for entry, block in _entries_with_discovery("dhcp", include_experimental):
        profile = _base_profile(entry)
        profile["mac_oui_prefixes"] = list(block.get("mac_oui_prefixes") or [])
        profiles.append(profile)
    return profiles


def _build_native_profiles(include_experimental: bool = False) -> list[dict[str, Any]]:
    """Level-1 infrastructure found via HA's own Zeroconf/mDNS watcher (we
    adopt matching pending config flows — the cooperate pattern). Level 1 is
    infrastructure that enables discovering Level 2 devices (e.g. a Zigbee
    coordinator must be online before any Zigbee endpoint appears)."""
    profiles: list[dict[str, Any]] = []
    for entry, block in _entries_with_discovery("native", include_experimental):
        profile = _base_profile(entry)
        profile["domain"] = block.get("domain")
        profile["level"] = block.get("level", 1)
        # A shared domain (esphome carries Sensa's own boards AND the Voice PE)
        # needs the product-family filter `_discover_native_devices` applies.
        if block.get("name_prefix") is not None:
            profile["name_prefix"] = block["name_prefix"]
        profiles.append(profile)
    return profiles


def _build_ble_profiles(include_experimental: bool = False) -> list[dict[str, Any]]:
    """Secondary BLE devices added from the Bluetooth view. `manufacturer_id`
    is the Bluetooth SIG Company Identifier used to filter the scan;
    `device_subtype` / `name_prefix` disambiguate vendors that share an id
    (Victron's 0x02E1 covers shunt / MPPT / Cerbo / Orion XS)."""
    profiles: list[dict[str, Any]] = []
    for entry, block in _entries_with_discovery("ble", include_experimental):
        profile = _base_profile(entry)
        profile["domain"] = block.get("domain")
        profile["manufacturer_id"] = block.get("manufacturer_id")
        profile["level"] = block.get("level", 2)
        profile["image"] = entry.get("image")
        # Match/adopt extras live on the discovery block; device-level extras
        # (set at registration) live on the entry. Both are optional and
        # consumers read them with .get(), so only copy what's present.
        for key in ("device_subtype", "name_prefix", "native_flow_input", "service_uuid"):
            if block.get(key) is not None:
                profile[key] = block[key]
        for key in ("append_mac_suffix", "accept_native", "bms_family", "default_config"):
            if entry.get(key) is not None:
                profile[key] = entry[key]
        profiles.append(profile)
    return profiles


def _build_static_probe_profiles(include_experimental: bool = False) -> list[dict[str, Any]]:
    """Devices at well-known IPs not on the HA subnet (won't appear in DHCP);
    found by a TCP connect to each host:port. Hosts are tried in order, first
    responder wins (real Starlink dish at 192.168.100.1 before the demo sim)."""
    profiles: list[dict[str, Any]] = []
    for entry, block in _entries_with_discovery("static_probe", include_experimental):
        profile = _base_profile(entry)
        profile["hosts"] = list(block.get("hosts") or [])
        profile["port"] = block.get("port")
        profile["timeout"] = block.get("timeout")
        profiles.append(profile)
    return profiles


def _build_tcp_subnet_profiles(include_experimental: bool = False) -> list[dict[str, Any]]:
    """Devices identified ONLY by an open TCP port, swept across the scanned
    subnet(s) rather than a fixed host list.

    For the `static_probe` method the catalog names the exact addresses to try,
    which only works when the device is at a known fixed IP. A SONOFF Dongle-M
    is a bare serial-over-TCP socket on a DHCP address: nothing announces it
    (no mDNS, no HTTP, no HA integration), so the only way to find it is to
    look for the open port wherever it happens to be. Same shape as the Cerbo's
    `mqtt_subnet` sweep — see `_discover_tcp_subnet` for the false-positive
    caveat, which is the reason this is a separate method and not a
    `hosts: ["*"]` special case on `static_probe`.
    """
    profiles: list[dict[str, Any]] = []
    for entry, block in _entries_with_discovery("tcp_subnet", include_experimental):
        profile = _base_profile(entry)
        profile["port"] = block.get("port")
        profile["timeout"] = block.get("timeout") or 0.5
        profile["mac_oui_prefixes"] = [
            str(o).lower().replace(":", "")
            for o in (block.get("mac_oui_prefixes") or [])
        ]
        # Addresses the SAME entry's fixed-host probe already owns. The sweep
        # must not also claim them: `static_probe` emits `static-<ip>` with NO
        # mac_address, this emits `<model>-<mac>`, and the render-time dedup
        # matches on canonical MAC or discovery_id — with one row lacking a MAC
        # and the ids differing, neither test fires and the SAME physical
        # device would be listed TWICE. Only reachable when the fixed host sits
        # inside the scanned subnet (not the default), but that is exactly the
        # customer with an IoT VLAN in `scan_subnet`, i.e. real hardware that
        # worked before this method existed.
        static_block = (entry.get("discovery") or {}).get("static_probe") or {}
        profile["skip_hosts"] = {str(h) for h in (static_block.get("hosts") or [])}
        profiles.append(profile)
    return profiles


def _build_http_probe_profiles(include_experimental: bool = False) -> list[dict[str, Any]]:
    """Devices identified by what an HTTP endpoint ANSWERS, swept across the
    scanned subnet(s).

    The Lucky Miner is the case this exists for, and it defeats every method
    above it. It is an ESP32 on a DHCP address behind an Espressif OUI it
    shares with our own relay controllers and Bluetooth proxies, so `dhcp`
    cannot tell it apart. It has no fixed address, so `static_probe` finds it
    on one network and nowhere else. And it answers on port 80, which half the
    LAN does, so `tcp_subnet` would claim anything with a web server.

    What identifies it is the response body: its firmware is an AxeOS fork and
    `GET /api/system/info` returns a JSON document with a known key set. So the
    profile carries `require_keys` (every one must be present) and `match`
    (field -> allowed values), and a responder is claimed only when both agree.
    That is a protocol handshake, which is the same standard `_discover_tcp_
    subnet` was forced to adopt after an unrelated host with the right port
    open surfaced as a phantom SONOFF Dongle-M.
    """
    profiles: list[dict[str, Any]] = []
    for entry, block in _entries_with_discovery("http_probe", include_experimental):
        profile = _base_profile(entry)
        profile["port"] = block.get("port") or 80
        profile["path"] = block.get("path") or "/"
        profile["timeout"] = block.get("timeout") or 2.0
        # The object to read, where the answer is wrapped: the SLZB's `/ha_info`
        # is `{"Info": {...}}`. Every field below is then read inside it.
        profile["body_key"] = block.get("body_key") or ""
        profile["require_keys"] = list(block.get("require_keys") or [])
        # {field: [allowed, ...]}. A bare string is accepted and wrapped, so a
        # single-value match reads naturally in the catalog.
        match: dict[str, list[str]] = {}
        for field, allowed in (block.get("match") or {}).items():
            match[field] = (
                [str(allowed)] if isinstance(allowed, (str, int, float))
                else [str(v) for v in allowed]
            )
        profile["match"] = match
        profile["mac_field"] = block.get("mac_field") or ""
        profile["hostname_field"] = block.get("hostname_field") or ""
        profile["model_field"] = block.get("model_field") or ""
        # Addresses the SAME entry's fixed-host probe already owns, the
        # double-listing trap `_build_tcp_subnet_profiles` guards. That path
        # emits `static-<ip>` with no MAC while this emits `<model>-<mac>`, so
        # the render-time dedup matches on neither and one physical device
        # would appear twice.
        static_block = (entry.get("discovery") or {}).get("static_probe") or {}
        profile["skip_hosts"] = {str(h) for h in (static_block.get("hosts") or [])}
        profiles.append(profile)
    return profiles


def _build_rtsp_probe_profiles(include_experimental: bool = False) -> list[dict[str, Any]]:
    """Devices identified by ANSWERING RTSP, swept across the scanned subnet(s).

    An IP camera is the case this exists for: there is no vendor allowlist yet,
    so no OUI to match, and an open port 554 is not identification (the
    phantom-Dongle-M rule). What identifies a camera is that it speaks RTSP: an
    `OPTIONS` request is answered with an `RTSP/1.0` status line. That is a
    protocol handshake, the same standard `_discover_http_probe` holds the
    Lucky Miner to, and it is what lets the sweep skip a web server, a printer
    or anything else that happens to sit on the port.
    """
    profiles: list[dict[str, Any]] = []
    for entry, block in _entries_with_discovery("rtsp_probe", include_experimental):
        profile = _base_profile(entry)
        profile["port"] = block.get("port") or 554
        profile["timeout"] = block.get("timeout") or 2.0
        static_block = (entry.get("discovery") or {}).get("static_probe") or {}
        profile["skip_hosts"] = {str(h) for h in (static_block.get("hosts") or [])}
        profiles.append(profile)
    return profiles


DHCP_DEVICE_PROFILES: list[dict[str, Any]] = _build_dhcp_profiles()

HTTP_PROBE_PROFILES: list[dict[str, Any]] = _build_http_probe_profiles()

RTSP_PROBE_PROFILES: list[dict[str, Any]] = _build_rtsp_probe_profiles()

TCP_SUBNET_PROFILES: list[dict[str, Any]] = _build_tcp_subnet_profiles()

# NOTE: these are consumed directly as module constants across `sensor.py` and
# `__init__.py`, so they are FROZEN at `include_experimental=False`. For the
# NATIVE list that no longer matters: the discovery sensor calls
# `native_profiles(hass)` below, which rebuilds against the live switch. The
# BLE list has no such wrapper yet, so an `experimental: true` entry discovered
# by `ble` would still be hidden permanently, which reads as a broken toggle;
# give it the same treatment before flagging one. Pinned by
# `test_catalog_integrity.py::
# test_experimental_entries_only_use_switch_aware_discovery_methods`.
LEVEL1_NATIVE_PROFILES: list[dict[str, Any]] = _build_native_profiles()
# Alias kept for any external reference; prefer LEVEL1_NATIVE_PROFILES in new code.
PRIMARY_NATIVE_PROFILES = LEVEL1_NATIVE_PROFILES


def native_profiles(hass: Any) -> list[dict[str, Any]]:
    """`LEVEL1_NATIVE_PROFILES`, rebuilt against the live Experimental Devices
    switch.

    The module constant above is frozen at `include_experimental=False`, so an
    `experimental: true` entry discovered by `native` is invisible through it no
    matter how the switch is set. Scanners call THIS, so the switch reaches the
    native path the same way it already reaches `dhcp` and `static_probe`. The
    rebuild is a comprehension over the catalog, cheap enough to run per scan,
    and returns the frozen list unchanged when the switch is off.
    """
    if not experimental_devices_enabled(hass):
        return LEVEL1_NATIVE_PROFILES
    return _build_native_profiles(include_experimental=True)

LEVEL2_BLE_PROFILES: list[dict[str, Any]] = _build_ble_profiles()
# Alias kept for any external reference; prefer LEVEL2_BLE_PROFILES in new code.
BLE_NATIVE_PROFILES = LEVEL2_BLE_PROFILES

STATIC_PROBE_PROFILES: list[dict[str, Any]] = _build_static_probe_profiles()


def _looks_like_mac(value: str | None) -> bool:
    """True if the string is effectively a 12-hex-digit MAC address."""
    if not value:
        return False
    cleaned = value.replace(":", "").replace("-", "").replace(" ", "").strip()
    return len(cleaned) == 12 and all(
        c in "0123456789abcdefABCDEF" for c in cleaned
    )


def _ble_cache_subtype(hass: Any, mac: str) -> str:
    """Byte-derived Victron device_subtype for this MAC, or ''.

    Looks up HA's BLE cache for the given MAC, runs the same byte-level
    Victron annotation the discovery sensor runs (model_id at bytes 2-3,
    mode byte at 4), and returns the `device_subtype` string it stamps
    (`smartshunt` / `smartsolar` / `orion_xs` / `cerbo` / '').

    Why this matters: HA's `victron_ble` integration opens one pending
    config flow per Victron device regardless of product, all under the
    same `victron_ble` domain. Our picker has three Sensa profiles
    (SmartShunt / SmartSolar / Orion XS) and needs to know which flow
    belongs to which profile. Gating on the flow's advert local_name is
    fragile because VictronConnect lets the customer rename the BLE
    device to anything — a customer rename would drop their Victron
    gear into the picker's Others column. The 16-bit model_id + mode
    byte in the manufacturer_data frame don't change on rename, so
    this byte-level lookup is the durable gate.
    """
    if not mac:
        return ""
    try:
        from homeassistant.components.bluetooth import async_last_service_info
    except ImportError:
        return ""
    cleaned = mac.replace(":", "").replace("-", "")
    if len(cleaned) != 12:
        return ""
    mac_colon = ":".join(cleaned[i : i + 2].upper() for i in range(0, 12, 2))
    try:
        info = async_last_service_info(hass, mac_colon, connectable=False)
    except Exception:  # noqa: BLE001
        return ""
    if info is None:
        return ""
    # Delegate to the sensor's annotator so this stays in lockstep with
    # the discovery sensor's byte-gate rules (any new model_id or mode
    # added there automatically flows through here).
    from .sensor import _VICTRON_COMPANY_ID, _annotate_victron_subtype
    mfr_data = getattr(info, "manufacturer_data", None) or {}
    if _VICTRON_COMPANY_ID not in mfr_data:
        return ""
    shim: dict[str, Any] = {"manufacturer_id": f"0x{_VICTRON_COMPANY_ID:04X}"}
    _annotate_victron_subtype(shim, info)
    return str(shim.get("device_subtype") or "")


def _ble_cache_name(hass: Any, mac: str) -> str:
    """Latest advertised name from HA's BLE cache for this MAC, or ''.

    Some integrations (e.g. victron_ble) seed their flow's title_placeholders
    from the first service_info they see, which can be a packet without
    local_name — the flow title then reads as the MAC. Later packets from the
    same device usually carry the real name; this helper lets us pick it up
    for our own picker row without mutating someone else's flow state.
    """
    if not mac:
        return ""
    try:
        from homeassistant.components.bluetooth import async_last_service_info
    except ImportError:
        return ""
    mac_colon = ":".join(
        mac.replace(":", "").replace("-", "")[i : i + 2].upper()
        for i in range(0, 12, 2)
    ) if len(mac.replace(":", "").replace("-", "")) == 12 else mac
    try:
        info = async_last_service_info(hass, mac_colon, connectable=False)
    except Exception:  # noqa: BLE001
        return ""
    if info is None:
        return ""
    return info.name or ""


def _build_discovery_name(
    profile_name: str, hostname: str | None, mac: str | None, ip: str | None = None,
) -> str:
    """Hybrid picker display name for pre-add rows.

    Rule: use the hostname in brackets when it's genuinely informative
    (non-empty, not the IP, not just the generic profile name), otherwise
    fall back to a `#{last-4-MAC}` suffix for fleet disambiguation. If
    neither is available, return the plain profile name.

    Why: a fleet of Teltonikas (1 router + 3 switches) with factory-default
    hostnames shows as "Teltonika (RUTX11)" / "Teltonika (TSW101)" /
    "Teltonika (TSW101)" / "Teltonika (TSW202)" — the two TSW101s still
    look identical but customer can see model at a glance, and MACs on
    the other columns disambiguate the pair. Customer who renamed
    everything to "my-router" / "my-switch" still gets sensible names.
    A customer whose DHCP didn't supply hostnames at all falls through
    to "Teltonika #2912" / "Teltonika #ABCD" via the MAC tag.
    """
    cleaned_host = (hostname or "").strip()
    generic = {
        "",
        (ip or "").strip(),
        profile_name.strip().lower(),
        "unknown",
        "openwrt",
        "router",
    }
    if cleaned_host and cleaned_host.lower() not in generic:
        return f"{profile_name} ({cleaned_host})"

    mac_hex = (mac or "").replace(":", "").replace("-", "").upper()
    if len(mac_hex) >= 4:
        return f"{profile_name} (#{mac_hex[-4:]})"

    return profile_name


def _discover_dhcp_devices(hass: Any) -> list[dict[str, Any]]:
    """Query HA's DHCP cache for devices matching known OUI prefixes."""
    _LOGGER.info("DHCP discovery: starting lookup")
    try:
        from homeassistant.components.dhcp import DATA_DHCP
    except ImportError:
        _LOGGER.warning("DHCP discovery: component not available (import failed)")
        return []

    dhcp_data = hass.data.get(DATA_DHCP)
    if not dhcp_data:
        _LOGGER.warning("DHCP discovery: no DHCP data in hass.data")
        return []

    _LOGGER.info(
        "DHCP discovery: %d entries in address_data",
        len(dhcp_data.address_data),
    )

    devices: list[dict[str, Any]] = []

    # The prebuilt constant IS the non-experimental list, so the common path
    # costs nothing and stays monkeypatchable in tests. Only rebuild when the
    # Experimental Devices switch is on, so flipping it takes effect on the next
    # discovery pass without an integration reload. Built once per scan, not per
    # MAC.
    dhcp_profiles = (
        _build_dhcp_profiles(True)
        if experimental_devices_enabled(hass)
        else DHCP_DEVICE_PROFILES
    )

    for mac_upper, addr_data in dhcp_data.address_data.items():
        mac_lower = mac_upper.lower()

        for profile in dhcp_profiles:
            matched = any(
                mac_lower.startswith(oui) for oui in profile["mac_oui_prefixes"]
            )
            if not matched:
                continue

            # Format MAC for display
            mac_display = ":".join(mac_lower[i : i + 2] for i in range(0, 12, 2))
            ip = addr_data.get("ip", "")
            hostname = addr_data.get("hostname", ip)

            # Hybrid name: hostname in brackets if informative, MAC tag
            # suffix otherwise. See `_build_discovery_name` docstring.
            # `_enrich_from_catalog` only fills `name` when missing, so
            # this value survives through to the picker.
            name = _build_discovery_name(
                profile["name"], hostname, mac_display, ip,
            )

            devices.append({
                "name": name,
                "discovery_id": f"disc-{mac_lower}",
                "hostname": hostname,
                "ip_address": ip,
                "mac_address": mac_display,
                "device_type": profile["device_type"],
                "model": profile["vendor_model"],
                "vendor": profile["name"],
                "vendor_device_type": profile["device_type"],
                "vendor_model": profile["vendor_model"],
                "category": profile["category"],
                "source": "dhcp",
                "reachability": "reachable",
            })

            _LOGGER.info(
                "DHCP discovery: %s at %s (%s)",
                profile["name"],
                ip,
                mac_display,
            )
            break  # one profile match per MAC

    return devices


async def _discover_esphome_devices(
    hass: Any,
    client: SensaApiClient,
    scan_subnet: str,
    *,
    rescan: bool = True,
) -> list[dict[str, Any]]:
    """Return the backend's ESPHome HTTP discoveries, optionally re-probing first.

    Extracted from the config-flow scan path so the post-setup Discovery
    sensor can re-run the scan without pulling in the whole flow machinery.
    Rows are tagged `source="esphome_http"` so the unified add service can
    route them to `async_confirm_device`.

    `rescan=False` skips the subnet probe and just reads what the backend
    already holds. The probe walks a whole /24 and takes ~10 s on a populated
    LAN; the read is a few milliseconds. The Discovery sensor uses the cheap
    read on its poll and drives the probe separately in the background, so a
    slow probe can never time the source out and blank every un-added ESPHome
    device from the picker. The config flow keeps `rescan=True` — its first
    scan has nothing cached to fall back on.
    """
    if rescan:
        await client.async_scan(scan_subnet=scan_subnet)
    result = await client.async_get_discoveries()
    devices = list(result.get("discoveries", []))
    # Backend tags its HTTP probes with `sensa_http_probe`; the picker's
    # add-dispatch is keyed on `esphome_http`. Force-overwrite so any
    # backend-emitted source never reaches the dispatch unmatched.
    for device in devices:
        device["source"] = "esphome_http"
    return devices


async def _discover_cerbo_subnet(scan_subnet: str) -> list[dict[str, Any]]:
    """Scan one or more LAN subnets for Victron Cerbo GX via MQTT-on-LAN.

    Name-agnostic, OUI-agnostic, BLE-agnostic: probes every IP in the
    scan subnet(s) on port 1883, and for each responder runs a short
    MQTT connect + `N/+/system/0/#` subscription to confirm the device
    is actually Venus OS (vs a generic Mosquitto broker running for HA
    itself, Zigbee2MQTT, or any other neighbour). Only devices that
    publish at least one Venus system topic within ~2 s of the probe
    connect are returned.

    `scan_subnet` accepts a single CIDR or a comma-separated list
    (e.g. `"192.168.1.0/24,192.168.2.0/24"`) — Cerbos are often on an
    IoT VLAN separate from the HA host. Each CIDR is parsed with
    `ipaddress.ip_network(..., strict=False)` so both host and
    network forms are accepted. Invalid entries are logged and skipped
    so one typo doesn't take the whole scan down.

    Shape of returned rows mirrors the static-probe source (same
    `source=static_probe` tag so the add dispatch in
    `_async_register_single_device` can route them to
    `sensa_victron_mqtt` without a new branch).

    Cost on a /24 with zero Cerbos: ~500 ms (254 parallel TCP probes,
    500 ms per-host timeout, all failing fast with ConnectionRefused).
    Cost per Cerbo hit: +~2 s for the MQTT confirmation. Multi-subnet
    scales linearly.
    """
    import ipaddress

    candidate_ips: list[str] = []
    seen_subnets: list[str] = []
    for part in (scan_subnet or "").split(","):
        cidr = part.strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as err:
            _LOGGER.warning(
                "Cerbo subnet scan: invalid subnet %r (%s) — skipping",
                cidr, err,
            )
            continue
        seen_subnets.append(str(network))
        candidate_ips.extend(str(host) for host in network.hosts())

    if not candidate_ips:
        return []

    _LOGGER.info(
        "Cerbo subnet scan: probing %d IP(s) across %s for MQTT on LAN",
        len(candidate_ips), seen_subnets,
    )

    # Step 1 — TCP-probe the two Venus "MQTT on LAN" ports in parallel:
    # 1883 (plaintext) and 8883 (SSL). Probing both means a Cerbo left in
    # Venus OS's more-secure SSL-only mode is still found — the runtime
    # client speaks TLS, so there's no reason discovery should require
    # plaintext. Plaintext wins when both are open (no TLS handshake to
    # pay); otherwise fall back to 8883. Brief timeout so a big subnet of
    # dark IPs doesn't stretch the scan cycle.
    subnet_key = ",".join(seen_subnets)
    plain_alive, tls_alive = await asyncio.gather(
        _sweep_port(
            candidate_ips, 1883, timeout=0.5, cache_key=f"cerbo:{subnet_key}",
        ),
        _sweep_port(
            candidate_ips, 8883, timeout=0.5,
            cache_key=f"cerbo-tls:{subnet_key}",
        ),
    )
    plain_set, tls_set = set(plain_alive), set(tls_alive)
    # (ip, port, use_tls) for each responder, preferring plaintext.
    targets: list[tuple[str, int, bool]] = []
    for ip in candidate_ips:
        if ip in plain_set:
            targets.append((ip, 1883, False))
        elif ip in tls_set:
            targets.append((ip, 8883, True))
    if not targets:
        _LOGGER.info("Cerbo subnet scan: no port-1883/8883 responders")
        return []

    _LOGGER.info(
        "Cerbo subnet scan: %d MQTT responder(s); confirming Venus on each",
        len(targets),
    )

    # Step 2 — MQTT probe each responder in parallel with a small
    # concurrency cap. Sequentially walking N brokers at ~2.5 s each
    # used to pay linear time; capping at 4 simultaneous connects
    # bounds file-descriptor pressure on small hardware (Pi 4) while
    # turning a 10 s scan on a VLAN with 4 brokers into a 2.5 s scan.
    sem = asyncio.Semaphore(4)

    async def _probe(ip: str, port: int, use_tls: bool) -> dict[str, Any] | None:
        async with sem:
            info = await _mqtt_probe_venus_identity(
                ip, port=port, use_tls=use_tls,
            )
        if not info.get("is_venus"):
            return None
        hostname = info.get("system_name") or await _reverse_dns(ip)
        name = _build_discovery_name(
            "Victron Cerbo GX", hostname, None, ip,
        )
        # Identity must be STABLE across IP changes (manual reconfigure or an
        # expired DHCP lease). The IP is not — a Cerbo keyed on `static-{ip}`
        # re-appears as a brand-new device the moment its address moves, and the
        # old registration lingers unreachable. The Cerbo's VRM portal id is the
        # durable anchor: it's already captured during the Venus probe (from the
        # `N/<portalId>/system/0/#` topic path), so key the discovery_id — and
        # therefore the backend device_id — on it. Re-discovery at a new IP then
        # matches the existing device (no duplicate); the runtime IP-reconcile in
        # the discovery sensor updates the stored host and reconnects. Fall back
        # to the IP-based id only on the cold edge where the portal id never
        # arrived (shouldn't happen — `is_venus` itself fires off that topic).
        portal_id = str(info.get("portal_id") or "").strip()
        discovery_id = f"cerbo-{portal_id}" if portal_id else f"static-{ip}"
        row = {
            "name": name,
            "discovery_id": discovery_id,
            "hostname": hostname or ip,
            "ip_address": ip,
            "mac_address": "",
            "device_type": "gx_gateway",
            "model": "victron_cerbo_gx",
            "vendor": "Victron Energy",
            "vendor_device_type": "gx_gateway",
            "vendor_model": "victron_cerbo_gx",
            "category": "electrical",
            "source": "static_probe",
            "reachability": "reachable",
            "integration": "sensa_victron_mqtt",
        }
        # First confirmation per IP per process lifetime lands at
        # INFO; re-confirmations on every 30 s scan tick drop to
        # DEBUG so the log isn't dominated by the same "Cerbo still
        # at .13" line twice a minute.
        if ip not in _CONFIRMED_CERBO_IPS:
            _CONFIRMED_CERBO_IPS.add(ip)
            _LOGGER.info("Cerbo subnet scan: Venus OS confirmed at %s", ip)
        else:
            _LOGGER.debug("Cerbo subnet scan: Venus OS still at %s", ip)
        return row

    probed = await asyncio.gather(
        *(_probe(ip, port, use_tls) for ip, port, use_tls in targets),
        return_exceptions=False,
    )
    return [row for row in probed if row is not None]


# How long one Cerbo scan may take. Finding the live hosts costs about 4.5 s on
# a cold cycle and each batch of four MQTT brokers about 2.5 s, so five brokers
# measured 8.2 s on the BlackPaw van (2026-11-02) against the old 7 s.
CERBO_SCAN_BUDGET_S = 12.0
# scan subnet -> the Cerbos the last finished scan found.
_LAST_CERBO_ROWS: dict[str, list[dict[str, Any]]] = {}
# Subnets whose last scan ran out of time, so the log says so once.
_CERBO_SCAN_STALLED: set[str] = set()


async def discover_cerbo_keeping_last(
    scan_subnet: str, timeout: float = CERBO_SCAN_BUDGET_S,
) -> list[dict[str, Any]]:
    """`_discover_cerbo_subnet`, with a timeout read as "could not ask".

    A scan that runs out of time says nothing about the Cerbo, so the last
    finished scan's answer stands and one slow cycle cannot take it off the
    picker. A scan that finishes and finds none is "told no", and clears it.
    """
    try:
        rows = await asyncio.wait_for(_discover_cerbo_subnet(scan_subnet), timeout)
    except TimeoutError:
        if scan_subnet not in _CERBO_SCAN_STALLED:
            _CERBO_SCAN_STALLED.add(scan_subnet)
            _LOGGER.info(
                "Cerbo subnet scan: over %ss on %s, keeping the last answer",
                timeout, scan_subnet,
            )
        return [dict(row) for row in _LAST_CERBO_ROWS.get(scan_subnet, [])]
    _CERBO_SCAN_STALLED.discard(scan_subnet)
    _LAST_CERBO_ROWS[scan_subnet] = rows
    return rows


def _arp_ip_to_mac() -> dict[str, str]:
    """`{ip: mac_lower_nocolons}` from the kernel ARP table, or {}.

    The DHCP cache only knows devices that took a lease HA witnessed, which
    misses a static address and misses a container on a docker macvlan (it gets
    its address from IPAM, not DHCP) — i.e. exactly the simulator. The ARP
    table knows anything we have just exchanged packets with on the same L2,
    and the sweep's own TCP connect is what puts the responder there, so read
    this AFTER probing.

    Linux-only (`/proc/net/arp`); returns {} anywhere else, including the
    Windows dev box, so callers must treat an unknown MAC as unknown rather
    than as a verdict.
    """
    out: dict[str, str] = {}
    try:
        with open("/proc/net/arp", encoding="utf-8") as fh:
            next(fh, None)  # header row
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip, mac = parts[0], parts[3].lower().replace(":", "")
                # 00:00:00:00:00:00 is an incomplete entry, not a device.
                if len(mac) == 12 and mac != "0" * 12:
                    out[ip] = mac
    except (OSError, StopIteration):
        return {}
    return out


def _dhcp_ip_to_mac(hass: Any) -> dict[str, tuple[str, str]]:
    """`{ip: (mac_lower_nocolons, hostname)}` from HA's DHCP cache, or {}.

    The cache is keyed by MAC; the TCP sweep only knows an IP, so invert it.
    Complements `_arp_ip_to_mac`, which has no hostname to offer.
    """
    try:
        from homeassistant.components.dhcp import DATA_DHCP
    except ImportError:
        return {}
    dhcp_data = hass.data.get(DATA_DHCP) if hass is not None else None
    if not dhcp_data:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for mac_upper, addr_data in (dhcp_data.address_data or {}).items():
        ip = str((addr_data or {}).get("ip") or "")
        if ip:
            out[ip] = (str(mac_upper).lower(), str((addr_data or {}).get("hostname") or ""))
    return out


async def _discover_tcp_subnet(
    hass: Any, scan_subnet: str, include_experimental: bool = False,
) -> list[dict[str, Any]]:
    """Sweep the scanned subnet(s) for devices identified only by an open port.

    The SONOFF Dongle-M is the case this exists for: a Zigbee radio exposed as
    a raw serial-over-TCP socket on 6638. It announces nothing — no mDNS, no
    HTTP, no HA integration — so an open 6638 is the only signal it gives. Its
    only route used to be `static_probe.hosts: ["192.168.2.20"]` (still present
    as a fallback for a unit pinned there), which finds it on exactly one
    network and nowhere else.

    **An open port alone is NOT identification.** Unlike the Cerbo sweep,
    which confirms with an MQTT handshake, nothing here speaks a protocol —
    any other service on the same port looks identical. So when the catalog
    entry lists `mac_oui_prefixes`, a responder is only claimed if its MAC
    matches: the sweep resolves the MAC from the ARP table (which its own
    connect has just populated), falling back to HA's DHCP cache. An
    unidentifiable responder is skipped, NOT listed hopefully.

    This rule was written after the first version listed one: the dev LAN has
    an unrelated host with 6638 open, and it surfaced as a phantom
    "SONOFF Dongle-M" in the picker (it broke four unrelated tests, which is
    how it was caught). A discovery row is a claim about what a device IS;
    making that claim off a port number alone is worse than not finding it.
    An entry that declares no OUIs opts out and lists every responder.

    **Identity** is the MAC, so the device survives an address change like
    every other source. Only where no OUIs are declared (and so no MAC is
    required) does it fall back to `static-<ip>`, matching the id
    `static_probe` would have produced so a device found by both paths dedups
    instead of doubling up.

    Rows carry `source: "static_probe"` so the add dispatch in
    `_async_register_single_device` routes them exactly as the fixed-host
    probe already does — same reasoning as the Cerbo subnet scan.
    """
    import ipaddress

    profiles = (
        _build_tcp_subnet_profiles(True)
        if include_experimental
        else TCP_SUBNET_PROFILES
    )
    profiles = [p for p in profiles if p.get("port")]
    if not profiles:
        return []

    candidate_ips: list[str] = []
    for part in (scan_subnet or "").split(","):
        cidr = part.strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as err:
            _LOGGER.warning(
                "TCP subnet scan: invalid subnet %r (%s) — skipping", cidr, err,
            )
            continue
        candidate_ips.extend(str(host) for host in network.hosts())
    if not candidate_ips:
        return []

    dhcp_map = _dhcp_ip_to_mac(hass)
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for profile in profiles:
        port = int(profile["port"])
        timeout = float(profile.get("timeout") or 0.5)
        alive = await _sweep_port(
            candidate_ips, port, timeout=timeout,
            cache_key=f"tcp:{profile['name']}:{port}",
        )
        skip_hosts = profile.get("skip_hosts") or set()
        responders = [ip for ip in alive if ip not in skip_hosts]
        if not responders:
            _LOGGER.debug(
                "TCP subnet scan: no port-%d responders for %s",
                port, profile["name"],
            )
            continue
        _LOGGER.info(
            "TCP subnet scan: %d responder(s) on port %d for %s",
            len(responders), port, profile["name"],
        )
        ouis = profile.get("mac_oui_prefixes") or []
        # Read AFTER probing: our own connect is what puts the responder in the
        # ARP table. DHCP only supplements it (it carries a hostname).
        arp_map = _arp_ip_to_mac() if ouis else {}
        for ip in responders:
            dhcp_mac, hostname = dhcp_map.get(ip, ("", ""))
            mac_raw = arp_map.get(ip) or dhcp_mac
            if ouis and not any(mac_raw.startswith(o) for o in ouis):
                # Either unidentifiable or identifiably something else. Both
                # mean "do not claim this is the product" — see the docstring.
                _LOGGER.debug(
                    "TCP subnet scan: %s open on %d but MAC %r is not a %s",
                    ip, port, mac_raw or "<unknown>", profile["name"],
                )
                continue
            mac_display = (
                ":".join(mac_raw[i:i + 2] for i in range(0, 12, 2))
                if len(mac_raw) == 12
                else ""
            )
            discovery_id = (
                f"{profile['vendor_model']}-{mac_raw}" if mac_raw
                else f"static-{ip}"
            )
            if discovery_id in seen_ids:
                continue
            seen_ids.add(discovery_id)
            rows.append({
                "name": _build_discovery_name(
                    profile["name"], hostname, mac_display, ip,
                ),
                "discovery_id": discovery_id,
                "hostname": hostname or ip,
                "ip_address": ip,
                "mac_address": mac_display,
                "device_type": profile["device_type"],
                "model": profile["vendor_model"],
                "vendor": profile["name"],
                "vendor_device_type": profile["device_type"],
                "vendor_model": profile["vendor_model"],
                "category": profile["category"],
                "source": "static_probe",
                "reachability": "reachable",
                "integration": profile["integration"],
            })
    return rows


# IPs we've already INFO-logged a Cerbo confirmation for. Resets on
# HA restart (module reload). No expiry — if a Cerbo at an IP goes
# away and a different device appears at the same IP, the next scan
# cycle would DEBUG-only; acceptable since the discovery sensor's
# row set is still the ground truth, this is log-volume hygiene only.
_CONFIRMED_CERBO_IPS: set[str] = set()


async def _reverse_dns(ip: str, timeout: float = 1.0) -> str:
    """Best-effort reverse-DNS PTR lookup. Returns "" on any failure.

    Why: the Cerbo subnet probe path has no MAC/DHCP handle (we connect
    by IP), so the discovery row has no natural hostname to drop into
    the picker's "Profile (hostname)" format that DHCP- and
    Zeroconf-discovered devices use. A PTR lookup recovers the network
    hostname (typically `venus` on stock Venus OS, or whatever the
    owner set in Settings → General → System hostname), so a Cerbo
    at 192.168.2.13 shows as "Victron Cerbo GX (venus)" in the
    picker rather than a bare "Victron Cerbo GX".

    Trailing DNS suffix (`.local`, `.lan`, owner's domain) is stripped
    so the bracketed label stays short. Runs in the default executor
    because `socket.gethostbyaddr` is blocking.
    """
    import socket
    loop = asyncio.get_running_loop()
    try:
        host, _aliases, _addrs = await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyaddr, ip),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return ""
    if not host or host == ip:
        return ""
    short = host.split(".", 1)[0]
    # Guard against PTR records that resolve back to the IP itself
    # (some routers mirror the address as the "hostname"): a string
    # like "192.168.1.13" would otherwise slice to "192", surfacing
    # as "Victron Cerbo GX (192)" in the picker. An all-digit short
    # label is always wrong — real hostnames contain at least one
    # non-digit character.
    if not short or short.isdigit():
        return ""
    return short


async def _discover_http_probe(
    hass: Any, scan_subnet: str, include_experimental: bool = False,
) -> list[dict[str, Any]]:
    """Sweep the scanned subnet(s) for devices identified by an HTTP response.

    Two passes, because a /24 is 254 hosts and an HTTP GET is not free: first a
    cheap TCP connect to narrow to the handful with the port open, then the
    actual request only on those. Same IP walk and the same wall-clock budget
    as the Cerbo and TCP sweeps, which run beside it.

    **Identity is the MAC the device reports about itself** (`mac_field`),
    falling back to ARP and then HA's DHCP cache. That ordering matters here in
    a way it does not for the other sweeps: the payload's own MAC is
    authoritative, survives a DHCP lease change, and is what lets two miners on
    one subnet be two devices rather than one that keeps moving. Only where no
    MAC can be had at all does it fall back to `static-<ip>`, matching the id
    `static_probe` would have produced so a device found by both dedups instead
    of doubling up.

    Rows carry `source: "static_probe"` so `_async_register_single_device`
    routes them exactly as the fixed-host probe already does, same reasoning
    as the Cerbo and TCP subnet scans.
    """
    import ipaddress

    profiles = (
        _build_http_probe_profiles(True)
        if include_experimental
        else HTTP_PROBE_PROFILES
    )
    profiles = [p for p in profiles if p.get("port")]
    if not profiles:
        return []

    candidate_ips: list[str] = []
    for part in (scan_subnet or "").split(","):
        cidr = part.strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as err:
            _LOGGER.warning(
                "HTTP probe scan: invalid subnet %r (%s), skipping", cidr, err,
            )
            continue
        candidate_ips.extend(str(host) for host in network.hosts())
    if not candidate_ips:
        return []

    dhcp_map = _dhcp_ip_to_mac(hass)
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # ONE connect sweep per port, shared by every profile on it, then every
    # profile's GETs at once. Profiles used to run one after another, each with
    # its own full sweep, so the cost grew with the catalog: the miner plus the
    # two Shelly entries is three sweeps of every address, which can run past
    # the caller's 7 s budget, and a source that times out returns NO rows at
    # all. Now it costs one sweep plus one round of GETs however many profiles
    # share port 80.
    open_by_port: dict[int, list[str]] = {}
    for port in sorted({int(p["port"]) for p in profiles}):
        open_by_port[port] = await _sweep_port(
            candidate_ips, port, timeout=0.5, cache_key=f"http:{port}",
        )

    async def _identify_for(
        profile: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        port = int(profile["port"])
        skip_hosts = profile.get("skip_hosts") or set()
        responders = [ip for ip in open_by_port[port] if ip not in skip_hosts]
        if not responders:
            _LOGGER.debug(
                "HTTP probe scan: no port-%d responders for %s",
                port, profile["name"],
            )
            return []
        _LOGGER.debug(
            "HTTP probe scan: %d responder(s) on port %d, asking each whether "
            "it is a %s", len(responders), port, profile["name"],
        )
        bodies = await asyncio.gather(
            *(_http_identify(hass, ip, profile) for ip in responders),
        )
        return [(ip, b) for ip, b in zip(responders, bodies) if b]

    per_profile = await asyncio.gather(*(_identify_for(p) for p in profiles))

    for profile, identified in zip(profiles, per_profile):
        if not identified:
            continue
        _LOGGER.info(
            "HTTP probe scan: identified %d %s(s)", len(identified), profile["name"],
        )

        arp_map = _arp_ip_to_mac()
        for ip, body in identified:
            dhcp_mac, hostname = dhcp_map.get(ip, ("", ""))
            reported = ""
            if profile["mac_field"]:
                reported = str(body.get(profile["mac_field"], "") or "")
            mac_raw = (
                reported.replace(":", "").replace("-", "").lower()
                or arp_map.get(ip)
                or dhcp_mac
            )
            if len(mac_raw) != 12:
                mac_raw = ""
            mac_display = (
                ":".join(mac_raw[i:i + 2] for i in range(0, 12, 2))
                if mac_raw else ""
            )
            if profile["hostname_field"]:
                hostname = str(body.get(profile["hostname_field"], "") or "") or hostname
            model = profile["vendor_model"]
            if profile["model_field"] and body.get(profile["model_field"]):
                model = str(body[profile["model_field"]])

            discovery_id = (
                f"{profile['vendor_model']}-{mac_raw}" if mac_raw else f"static-{ip}"
            )
            if discovery_id in seen_ids:
                continue
            seen_ids.add(discovery_id)
            rows.append({
                "name": _build_discovery_name(
                    profile["name"], hostname, mac_display, ip,
                ),
                "discovery_id": discovery_id,
                "hostname": hostname or ip,
                "ip_address": ip,
                "mac_address": mac_display,
                "device_type": profile["device_type"],
                "model": model,
                "vendor": profile["name"],
                "vendor_device_type": profile["device_type"],
                "vendor_model": profile["vendor_model"],
                "category": profile["category"],
                "source": "static_probe",
                "reachability": "reachable",
                "integration": profile["integration"],
            })
    return rows


async def _http_identify(
    hass: Any, ip: str, profile: dict[str, Any],
) -> dict[str, Any] | None:
    """GET the profile's path on `ip` and return the body iff it identifies.

    Shares `_SWEEP_SOCKETS` with the connect sweeps: a GET holds a socket for
    up to its timeout, so an unbounded round of them can exhaust the box's
    descriptors exactly as the sweeps could.

    Returns None for every "not this device" outcome, and logs WHICH one at
    debug with the value actually observed. That log line is the point: when
    two miners are plugged into a real van for the first time and nothing
    appears in the picker, the difference between "discovery is broken" and
    "their firmware reports BM1370, change one string" is whether this said so.

    A 401 is called out separately because the AxeOS API authorises by network
    range. On a van with the miners on an IoT VLAN and HA elsewhere that is the
    single likeliest failure, and reading it as "offline" sends you hunting the
    wrong problem entirely.
    """
    url = f"http://{ip}:{profile['port']}{profile['path']}"
    timeout = float(profile.get("timeout") or 2.0)
    try:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        async with _SWEEP_SOCKETS, session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 401:
                _LOGGER.debug(
                    "HTTP probe: %s answered 401 for %s, the device is there but "
                    "refused us; AxeOS authorises by network range, so check the "
                    "miner and HA are on the same subnet",
                    url, profile["name"],
                )
                return None
            if resp.status != 200:
                _LOGGER.debug(
                    "HTTP probe: %s answered HTTP %d, not a %s",
                    url, resp.status, profile["name"],
                )
                return None
            # AxeOS serves application/json; be lenient about the header since
            # a fork that sets text/plain is still perfectly identifiable.
            body = await resp.json(content_type=None)
    except Exception as err:  # noqa: BLE001 - every failure means "not this"
        _LOGGER.debug("HTTP probe: %s did not answer (%s)", url, err)
        return None

    if not isinstance(body, dict):
        _LOGGER.debug("HTTP probe: %s returned %s, not an object",
                      url, type(body).__name__)
        return None

    if profile.get("body_key"):
        body = body.get(profile["body_key"])
        if not isinstance(body, dict):
            _LOGGER.debug("HTTP probe: %s has no %r object, not a %s",
                          url, profile["body_key"], profile["name"])
            return None

    missing = [k for k in profile["require_keys"] if k not in body]
    if missing:
        _LOGGER.debug(
            "HTTP probe: %s answered JSON but is missing %s, NOT claiming it "
            "as a %s (an unidentifiable responder is skipped, not listed hopefully)",
            url, missing, profile["name"],
        )
        return None

    for field, allowed in (profile["match"] or {}).items():
        observed = str(body.get(field, ""))
        if observed not in allowed:
            _LOGGER.debug(
                "HTTP probe: %s reports %s=%r, expected one of %s, not a %s",
                url, field, observed, allowed, profile["name"],
            )
            return None
    return body


async def _discover_rtsp_probe(
    hass: Any, scan_subnet: str, include_experimental: bool = False,
) -> list[dict[str, Any]]:
    """Sweep the scanned subnet(s) for devices that answer RTSP (IP cameras).

    Same two-pass shape as `_discover_http_probe`: a cheap TCP connect narrows
    a /24 to the hosts with the port open, then an RTSP `OPTIONS` is sent only
    to those. A responder is claimed only when it answers with an RTSP status
    line; an open port on its own is never enough.

    Identity is the MAC from ARP (the sweep's own connect is what puts the
    responder there) with HA's DHCP cache as the fallback for the hostname,
    exactly as the TCP sweep does: RTSP carries no MAC of its own. Only where
    no MAC can be had does it fall back to `static-<ip>`. Rows carry
    `source: "static_probe"` so `_async_register_single_device` routes them as
    the fixed-host probe already does.
    """
    import ipaddress

    profiles = (
        _build_rtsp_probe_profiles(True)
        if include_experimental
        else RTSP_PROBE_PROFILES
    )
    profiles = [p for p in profiles if p.get("port")]
    if not profiles:
        return []

    candidate_ips: list[str] = []
    for part in (scan_subnet or "").split(","):
        cidr = part.strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as err:
            _LOGGER.warning(
                "RTSP probe scan: invalid subnet %r (%s), skipping", cidr, err,
            )
            continue
        candidate_ips.extend(str(host) for host in network.hosts())
    if not candidate_ips:
        return []

    dhcp_map = _dhcp_ip_to_mac(hass)
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for profile in profiles:
        port = int(profile["port"])
        skip_hosts = profile.get("skip_hosts") or set()
        alive = await _sweep_port(
            candidate_ips, port, timeout=0.5,
            cache_key=f"rtsp:{profile['name']}:{port}",
        )
        responders = [ip for ip in alive if ip not in skip_hosts]
        if not responders:
            _LOGGER.debug(
                "RTSP probe scan: no port-%d responders for %s",
                port, profile["name"],
            )
            continue
        _LOGGER.debug(
            "RTSP probe scan: %d responder(s) on port %d, asking each whether "
            "it speaks RTSP", len(responders), port,
        )

        answers = await asyncio.gather(
            *(_rtsp_identify(ip, profile) for ip in responders),
        )
        identified = [(ip, a) for ip, a in zip(responders, answers) if a]
        if not identified:
            continue
        _LOGGER.info(
            "RTSP probe scan: identified %d %s(s)", len(identified), profile["name"],
        )

        arp_map = _arp_ip_to_mac()
        for ip, answer in identified:
            dhcp_mac, hostname = dhcp_map.get(ip, ("", ""))
            mac_raw = arp_map.get(ip) or dhcp_mac or ""
            if len(mac_raw) != 12:
                mac_raw = ""
            mac_display = (
                ":".join(mac_raw[i:i + 2] for i in range(0, 12, 2))
                if mac_raw else ""
            )
            # The Server header is the closest thing RTSP has to a model
            # ("go2rtc/1.9.14", "Hipcam RealServer/V1.0"); shown, not matched.
            model = answer.get("server") or profile["vendor_model"]

            discovery_id = (
                f"{profile['vendor_model']}-{mac_raw}" if mac_raw else f"static-{ip}"
            )
            if discovery_id in seen_ids:
                continue
            seen_ids.add(discovery_id)
            rows.append({
                "name": _build_discovery_name(
                    profile["name"], hostname, mac_display, ip,
                ),
                "discovery_id": discovery_id,
                "hostname": hostname or ip,
                "ip_address": ip,
                "mac_address": mac_display,
                "device_type": profile["device_type"],
                "model": model,
                "vendor": profile["name"],
                "vendor_device_type": profile["device_type"],
                "vendor_model": profile["vendor_model"],
                "category": profile["category"],
                "source": "static_probe",
                "reachability": "reachable",
                "integration": profile["integration"],
            })
    return rows


_RTSP_STATUS_LINE = re.compile(r"^RTSP/(\d)\.(\d) (\d{3})")


def _parse_rtsp_response(raw: bytes) -> dict[str, Any] | None:
    """The status code and headers of an RTSP response, or None if it is not one.

    Pure, so the handshake predicate is testable without a socket. Only the
    status line decides whether the responder speaks RTSP; headers are extra.
    """
    text = raw.decode("latin-1", "replace")
    lines = text.split("\r\n")
    match = _RTSP_STATUS_LINE.match(lines[0].strip())
    if not match:
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return {
        "status": int(match.group(3)),
        "server": headers.get("server", ""),
        "public": headers.get("public", ""),
    }


async def _rtsp_identify(ip: str, profile: dict[str, Any]) -> dict[str, Any] | None:
    """Send an RTSP `OPTIONS` to `ip` and return the answer iff it is RTSP.

    Returns None for every "not this device" outcome and logs which one, so a
    camera that is plugged in and never appears can be told apart from one
    that answered something unexpected. A 401 is still a camera: many require
    credentials for `DESCRIBE`, and some for `OPTIONS` too, and the credential
    is the customer's to add later. Any other status is logged and refused.
    """
    port = int(profile["port"])
    timeout = float(profile.get("timeout") or 2.0)
    request = (
        f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "User-Agent: Sensa Nomad\r\n\r\n"
    ).encode("ascii")
    async with _SWEEP_SOCKETS:
        return await _rtsp_identify_unbounded(ip, port, timeout, request, profile)


async def _rtsp_identify_unbounded(
    ip: str, port: int, timeout: float, request: bytes, profile: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout,
        )
        try:
            writer.write(request)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        finally:
            writer.close()
    except Exception as err:  # noqa: BLE001 - every failure means "not this"
        _LOGGER.debug("RTSP probe: %s:%d did not answer (%s)", ip, port, err)
        return None

    answer = _parse_rtsp_response(raw)
    if answer is None:
        _LOGGER.debug(
            "RTSP probe: %s:%d answered but not with RTSP (%r), NOT claiming it "
            "as a %s (an unidentifiable responder is skipped, not listed hopefully)",
            ip, port, raw[:60], profile["name"],
        )
        return None
    if answer["status"] not in (200, 401):
        _LOGGER.debug(
            "RTSP probe: %s:%d answered RTSP %d, not a %s",
            ip, port, answer["status"], profile["name"],
        )
        return None
    return answer


# ONE cap on how many sockets every subnet sweep may hold open between them.
#
# Each sweep used to fan out to every address at once, and several sweeps run
# together (TCP subnet, Cerbo, HTTP probe, RTSP). One /24 is 254 addresses; a
# van whose `scan_subnet` names two segments is 508, and each catalog profile
# sweeps again. On a customer's box that reached the process limit and Home
# Assistant fell over with "[Errno 24] No file descriptors available", taking
# the router poller and the dashboards with it (2026-09-23).
#
# 64 keeps a two-subnet sweep well inside a default 1024-descriptor budget and
# still walks 508 addresses in a few seconds at a 0.5 s connect timeout.
_SWEEP_SOCKETS = asyncio.Semaphore(64)


async def _tcp_probe(ip: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if `ip:port` accepts a TCP connection within timeout.

    Closes the socket immediately; we only care about reachability,
    not what the service says. Holds a slot from `_SWEEP_SOCKETS`, the one
    thing standing between a multi-subnet scan and the box's file-descriptor
    limit.
    """
    async with _SWEEP_SOCKETS:
        return await _tcp_probe_unbounded(ip, port, timeout)


async def _tcp_probe_unbounded(ip: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:  # noqa: BLE001, S110
        pass
    return True


# --- subnet port sweeps -----------------------------------------------------
#
# Every probe's timeout is measured on the event loop. Hand the loop a few
# hundred simultaneous connects and one stall longer than the timeout expires
# ALL of them together, so the sweep reports an empty subnet rather than a slow
# one. Observed on the Container box 2026-09-05: `cerbo_subnet_scan` and
# `tcp_subnet_scan` both returned 0 responders on the same cycle while every
# other source on that cycle ran 2-5x slower than usual, and both were back to
# normal on the next. The user-visible symptom was the real Cerbo appearing in
# the Discovery picker on roughly every other scan.
#
# The mitigation: treat a WHOLESALE-empty result as "could not ask", not "told
# no" — retry the pass, and failing that carry the previous cycle's responders
# forward exactly ONCE, so a single stalled cycle cannot erase every device the
# sweep is the only source for.
#
# The carry-forward is safe because it only re-offers CANDIDATE addresses: the
# Cerbo sweep still has to confirm Venus over MQTT, and the TCP sweep still has
# to match the responder's MAC against the catalog OUIs. A device that has
# genuinely gone fails those and contributes no row.
#
# Capping the number of in-flight connects was tried here first and REVERTED
# the same day: a 96-connect gate serialises 508 dead-IP timeouts into batches
# and took the Cerbo sweep from 3.1s to 6.6s, which then blew its own 7.0s
# source budget on some cycles — the identical symptom, arrived at from the
# other direction. Measure the sweep's wall time on the box before reaching for
# a cap again.

# What a sweep costs is the DEAD addresses: each one waits out the full connect
# timeout, on every port, and under the one `_SWEEP_SOCKETS` cap the sweeps
# queue behind each other. Measured on a customer's box 2026-09-23 with two
# /24s: one port took 4.0 s, and the five ports the catalog sweeps (80, 554,
# 6638, 1883, 8883) finished at 4 / 8 / 12 / 16 s, so every sweep past the
# first blew its 7 s source budget and the Shelly never reached the picker.
#
# So the scan asks "which hosts exist?" ONCE per cycle, shared by every sweep,
# and each sweep then knocks only on those. A host exists if the liveness port
# connects OR refuses (a refusal is an RST, i.e. a live machine with the port
# shut), or if the kernel already has it in ARP. Every device the catalog
# sweeps for serves a web page on port 80, so a device that drops port 80
# silently is not one we can identify anyway.

_LIVENESS_PORT = 80
# Long enough for every source in one discovery cycle to share the pass,
# short enough that the next cycle asks again.
_LIVENESS_TTL_S = 20.0

# ips key -> (monotonic time, live hosts)
_LIVE_HOSTS: dict[int, tuple[float, list[str]]] = {}
# ips key -> the pass in flight, so concurrent sweeps await one pass
_LIVE_HOSTS_INFLIGHT: dict[int, asyncio.Future[list[str]]] = {}


async def _tcp_knock(ip: str, port: int, timeout: float) -> bool:
    """True if `ip` is a live host: `port` accepted OR refused the connect."""
    async with _SWEEP_SOCKETS:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout,
            )
        except ConnectionRefusedError:
            return True
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001, S110
            pass
        return True


async def _live_hosts(ips: list[str], timeout: float = 0.5) -> list[str]:
    """The addresses in `ips` that are a live host, from one shared pass.

    A wholesale-empty pass is "could not ask" (see the loop-stall note above),
    so it falls back to the previous pass, and failing that to every address:
    slow, but never a subnet reported dark on the strength of a stall.
    """
    import time

    key = hash(tuple(ips))
    now = time.monotonic()
    cached = _LIVE_HOSTS.get(key)
    if cached and now - cached[0] < _LIVENESS_TTL_S:
        return list(cached[1])
    inflight = _LIVE_HOSTS_INFLIGHT.get(key)
    if inflight is not None:
        return list(await asyncio.shield(inflight))

    future: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
    _LIVE_HOSTS_INFLIGHT[key] = future
    try:
        answered = await asyncio.gather(
            *(_tcp_knock(ip, _LIVENESS_PORT, timeout) for ip in ips),
        )
        in_arp = _arp_ip_to_mac()
        live = [
            ip for ip, ok in zip(ips, answered) if ok or ip in in_arp
        ]
        if live:
            _LIVE_HOSTS[key] = (time.monotonic(), live)
        elif cached:
            live = list(cached[1])
        else:
            live = list(ips)
        future.set_result(live)
        return list(live)
    except BaseException as err:
        future.set_exception(err)
        # Nobody else may be awaiting it; mark the exception retrieved.
        future.exception()
        raise
    finally:
        _LIVE_HOSTS_INFLIGHT.pop(key, None)


# cache_key -> (responders, carried_forward_already)
_LAST_SWEEP_RESPONDERS: dict[str, tuple[list[str], bool]] = {}


async def _sweep_port(
    ips: list[str], port: int, *, timeout: float, cache_key: str,
) -> list[str]:
    """Return the IPs in `ips` accepting a connection on `port`.

    Never reports a wholesale-empty subnet on the strength of one pass — see
    the module comment above. An empty result is retried once, and if that is
    empty too the previous cycle's responders are offered one more time before
    the sweep finally agrees the subnet is dark.

    Knocks only on the hosts `_live_hosts` found, never the whole list: see
    the cost note above `_LIVENESS_PORT`.
    """
    async def _pass() -> list[str]:
        hosts = await _live_hosts(ips)
        alive = await asyncio.gather(
            *(_tcp_probe(ip, port, timeout=timeout) for ip in hosts),
        )
        return [ip for ip, ok in zip(hosts, alive) if ok]

    responders = await _pass()
    if not responders:
        # One retry: a loop stall is usually over by the time we get here.
        responders = await _pass()

    previous, carried = _LAST_SWEEP_RESPONDERS.get(cache_key, ([], False))
    if responders:
        _LAST_SWEEP_RESPONDERS[cache_key] = (responders, False)
        return responders
    if previous and not carried:
        # "Could not ask", not "told no". Offer the last known responders once;
        # the caller still has to identify each one before claiming a device.
        _LOGGER.info(
            "Subnet sweep %s: port %d found nothing this cycle — reusing the "
            "%d responder(s) from the previous cycle (a loop stall expires "
            "every probe at once, and an empty sweep would drop the device "
            "from this scan entirely)",
            cache_key, port, len(previous),
        )
        _LAST_SWEEP_RESPONDERS[cache_key] = (previous, True)
        return list(previous)
    _LAST_SWEEP_RESPONDERS[cache_key] = ([], False)
    return []


# Probes still finishing after the caller gave up; held so they are not
# garbage collected mid-connect.
_VENUS_PROBES: set[asyncio.Task[dict[str, Any]]] = set()


async def _mqtt_probe_venus_identity(
    ip: str, timeout: float = 2.5, *, port: int = 1883, use_tls: bool = False,
) -> dict[str, Any]:
    """`_venus_identity`, shielded from the caller's cancellation.

    The Discovery sensor cancels the Cerbo sweep at its 7 s budget. aiomqtt
    cancelled inside `Client.__aenter__` leaves its paho socket open with a
    cancelled `_connected` future, and the broker's later disconnect then
    logs "Exception in callback" (CancelledError) or "Future exception was
    never retrieved" (MqttCodeError 128) on every cycle (.177, 2026-10-31).
    Shielded, the probe finishes and closes its socket on aiomqtt's own
    timeouts, a few seconds at most.
    """
    task = asyncio.ensure_future(_venus_identity(ip, timeout, port=port, use_tls=use_tls))
    _VENUS_PROBES.add(task)
    task.add_done_callback(_VENUS_PROBES.discard)
    return await asyncio.shield(task)


async def _venus_identity(
    ip: str, timeout: float = 2.5, *, port: int = 1883, use_tls: bool = False,
) -> dict[str, Any]:
    """Probe `ip:<port>` for a Venus OS broker and, while we're connected,
    grab the system's identity.

    `port`/`use_tls` let the caller confirm a Cerbo on either Venus OS
    "MQTT on LAN" mode: plaintext (1883) or SSL (8883). TLS uses
    `CERT_NONE` — Venus's self-signed cert isn't verifiable without the
    customer exporting the CA, the same stance the runtime client takes.

    Returns a dict shaped as
        {"is_venus": bool, "system_name": str, "portal_id": str}

    `is_venus` flips True on the first message from any `N/<portal>
    /system/0/#` topic — that's the Venus OS fingerprint; a generic
    mosquitto running HA, Zigbee2MQTT, etc. wouldn't publish anything
    on that tree. Once confirmed we keep the socket open until either
    the friendly name arrives (typically retained, so within a few
    hundred ms) or the full `timeout` elapses — whichever comes first.

    `system_name` is the Cerbo's owner-visible friendly name. Venus OS
    publishes it retained on `system/0/CustomName` — the same topic the
    device-card builder and the rest of this integration read (and what
    the simulator emits). Some firmware also exposes `SystemName`; we
    accept it as a fallback. Much friendlier than a reverse-DNS hostname
    (often just `venus.local`, or — when the Cerbo is the simulator on a
    shared host — the host machine's name) so the picker prefers it when
    present.

    `portal_id` is the VRM id from the topic path — handy for
    diagnostics but not currently surfaced.

    Returns `is_venus=False` on connect/auth/subscribe failure — the
    probe deliberately never raises so the orchestrator can walk
    through a large list of responders quickly.
    """
    import json

    info: dict[str, Any] = {
        "is_venus": False, "system_name": "", "portal_id": "",
    }

    try:
        import aiomqtt
    except ImportError:
        _LOGGER.warning(
            "Cerbo MQTT probe %s: aiomqtt not installed; cannot confirm",
            ip,
        )
        return info

    client_kwargs: dict[str, Any] = {
        "hostname": ip,
        "port": port,
        "identifier": f"sensa-nomad-probe-{ip}",
        "timeout": timeout,
    }
    if use_tls:
        import ssl as ssl_module
        client_kwargs["tls_params"] = aiomqtt.TLSParameters(
            cert_reqs=ssl_module.CERT_NONE,
        )
        client_kwargs["tls_insecure"] = True

    try:
        async with aiomqtt.Client(**client_kwargs) as client:
            await client.subscribe("N/+/system/0/#", qos=0)
            # Discovery deliberately relies on the retained-replay
            # mechanism: Venus OS publishes Serial + CustomName as
            # retained topics under `N/<vrm>/system/0/`, so any fresh
            # subscriber to this filter receives them within
            # milliseconds — that's the `is_venus` signal.
            #
            # We used to also publish `R/+/keepalive` here as a
            # belt-and-braces "wake the Cerbo" nudge, but MQTT 3.1.1
            # disallows wildcard chars in publish topic names and
            # mosquitto 2.x rejects the packet outright (verified
            # against this repo's broker: "Invalid publish topic
            # 'R/+/keepalive', does it contain '+' or '#'?"). The
            # publish was a silent no-op on modern brokers, so dropping
            # it changes nothing about what works AND removes a
            # confusing failure mode where the wildcard error masked
            # the real "no retained Serial" cold-edge case.
            try:
                async with asyncio.timeout(timeout):
                    async for message in client.messages:
                        info["is_venus"] = True
                        topic = str(message.topic)
                        # `N/<portalId>/system/0/<path>`
                        parts = topic.split("/", 4)
                        if len(parts) >= 2 and not info["portal_id"]:
                            info["portal_id"] = parts[1]
                        # The Cerbo's friendly name rides on the retained
                        # `system/0/CustomName` topic — that's what real
                        # Venus OS publishes (and what the device-card
                        # builder + the simulator both emit). Older
                        # firmware also exposes `SystemName`; accept it as
                        # a fallback. Reading only `SystemName` (as we used
                        # to) meant the name was never captured for stock
                        # Venus or the simulator, so discovery fell back to
                        # a reverse-DNS lookup that surfaced the *host's*
                        # machine name instead.
                        leaf = topic.rsplit("/", 1)[-1]
                        if leaf in ("CustomName", "SystemName") and not info["system_name"]:
                            payload = message.payload
                            if isinstance(payload, (bytes, bytearray)):
                                try:
                                    payload = payload.decode("utf-8", errors="replace")
                                except Exception:  # noqa: BLE001
                                    payload = ""
                            try:
                                parsed = json.loads(payload)
                            except (ValueError, TypeError):
                                parsed = None
                            value = parsed.get("value") if isinstance(parsed, dict) else None
                            if isinstance(value, str) and value.strip():
                                info["system_name"] = value.strip()
                        # Got everything we wanted — don't hold the
                        # socket open for the rest of the timeout.
                        if info["system_name"]:
                            break
            except asyncio.TimeoutError:
                pass
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Cerbo MQTT probe %s: connect/subscribe failed (%s)",
            ip, err,
        )
    return info


async def _mqtt_probe_is_venus(ip: str, timeout: float = 2.5) -> bool:
    """Thin back-compat wrapper around `_mqtt_probe_venus_identity`
    for callers that only care about the binary confirmation."""
    info = await _mqtt_probe_venus_identity(ip, timeout=timeout)
    return bool(info.get("is_venus"))


async def _discover_static_devices(
    include_experimental: bool = False,
) -> list[dict[str, Any]]:
    """Probe well-known IP/port pairs for devices on other subnets.

    All profiles are probed concurrently — they're independent hosts on
    independent subnets (Starlink dish, etc.), so serialising them just
    pays N × timeout when devices are absent. `asyncio.gather` with
    `return_exceptions=True` means one unreachable host never blocks
    another from surfacing.
    """
    _LOGGER.info("Static probe discovery: starting")

    async def _probe(profile: dict[str, Any]) -> dict[str, Any] | None:
        port = profile["port"]
        timeout = profile.get("timeout", 3)
        # Profiles can list a single `host` (legacy) or multiple `hosts`
        # candidates. Multiple candidates exist so the same probe profile
        # can match different deployments without per-network config —
        # e.g. real Starlink dish on the dev LAN AND the sim NUC on the
        # demo LAN both surface as Starlink. First responder wins.
        hosts = profile.get("hosts")
        if not hosts:
            single = profile.get("host")
            hosts = [single] if single else []

        async def _try(host: str) -> str | None:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=timeout,
                )
            except (OSError, asyncio.TimeoutError):
                return None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001, S110
                pass
            return host

        results = await asyncio.gather(*(_try(h) for h in hosts))
        matched = next((h for h in results if h), None)
        if matched is None:
            _LOGGER.info(
                "Static probe discovery: %s not found on any of %s:%s",
                profile["name"], list(hosts), port,
            )
            return None

        _LOGGER.info(
            "Static probe discovery: %s at %s:%s",
            profile["name"], matched, port,
        )
        return {
            "discovery_id": f"static-{matched}",
            "hostname": matched,
            "ip_address": matched,
            "mac_address": "",
            "device_type": profile["device_type"],
            "model": profile["vendor_model"],
            "vendor": profile["name"],
            "vendor_device_type": profile["device_type"],
            "vendor_model": profile["vendor_model"],
            "category": profile["category"],
            "source": "static_probe",
            "reachability": "reachable",
        }

    results = await asyncio.gather(
        # As in `_discover_dhcp_devices`: the constant is the non-experimental
        # list (and the seam tests monkeypatch); only rebuild when enabled.
        *(
            _probe(p)
            for p in (
                _build_static_probe_profiles(True)
                if include_experimental
                else STATIC_PROBE_PROFILES
            )
        ),
        return_exceptions=False,
    )
    return [row for row in results if row is not None]


def _discover_native_devices(
    hass: Any, profiles: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Check HA's pending discovery flows for devices we want to cooperate with."""
    _LOGGER.info("Native discovery: checking pending flows for %d profile(s)", len(profiles))
    devices: list[dict[str, Any]] = []

    for profile in profiles:
        domain = profile["domain"]
        # Sensa-owned BLE BMS profiles (e.g. Roamer) have no native HA handler;
        # they carry only vendor metadata used when enriching ble_advert rows.
        # Passing domain=None through to async_entries / async_progress_by_handler
        # matches every entry / flow in HA and fabricates bogus rows — one per
        # unrelated pending flow. Skip them here.
        if not domain:
            continue
        try:
            # Check for already-configured entries. Dedup by unique_id —
            # some BLE integrations (e.g. ruuvitag_ble) can end up with
            # multiple entries per physical device if the add-flow re-ran.
            # We only want one row per unique_id in the picker.
            existing_entries = hass.config_entries.async_entries(domain)
            configured_uids: set[str] = set()
            seen_entry_keys: set[str] = set()
            for entry in existing_entries:
                if entry.source == config_entries.SOURCE_IGNORE:
                    continue
                host = entry.data.get("host", "")
                uid = entry.unique_id or ""
                configured_uids.add(uid)
                entry_key = uid or entry.entry_id
                if entry_key in seen_entry_keys:
                    continue
                seen_entry_keys.add(entry_key)
                # Same product-family filter as below — skip entries
                # whose display title doesn't match this profile's
                # name_prefix list. Prevents an existing SmartSolar
                # entry from being rebranded as a SmartShunt in our
                # picker.
                profile_name_prefixes = profile.get("name_prefix")
                if isinstance(profile_name_prefixes, str):
                    profile_name_prefixes = [profile_name_prefixes]
                elif profile_name_prefixes is None:
                    profile_name_prefixes = []
                entry_title = entry.title or ""
                if profile_name_prefixes and not any(
                    entry_title.startswith(p) for p in profile_name_prefixes
                ):
                    _LOGGER.debug(
                        "Native discovery: skipping existing %s entry %r — "
                        "title doesn't match profile prefix %s",
                        domain, entry_title, profile_name_prefixes,
                    )
                    continue

                _LOGGER.info(
                    "Native discovery: %s already configured at %s (uid=%s)",
                    profile["name"],
                    host,
                    uid,
                )
                # Hybrid name — hostname if informative, else MAC tag.
                # See `_build_discovery_name` docstring.
                native_hostname = entry.title or host
                native_name = _build_discovery_name(
                    profile["name"], native_hostname, uid, host,
                )

                devices.append({
                    "name": native_name,
                    "discovery_id": f"native-{domain}-{uid or entry.entry_id}",
                    "hostname": native_hostname,
                    "ip_address": host,
                    # `address` is the picker's Address-column field; show
                    # the colon-separated MAC for BLE entries so the column
                    # isn't blank for Ruuvi / Govee / SmartShunt native rows.
                    "address": uid if _looks_like_mac(uid) else "",
                    "mac_address": uid.replace(":", "") if uid else "",
                    "device_type": profile["device_type"],
                    "model": profile["vendor_model"],
                    "vendor": profile["name"],
                    "vendor_device_type": profile["device_type"],
                    "vendor_model": profile["vendor_model"],
                    "category": profile["category"],
                    # Registration sends this as the device's `role`. Without it a
                    # coordinator adopted through a native flow registered as
                    # `unknown`, the Zigbee resolver could not recognise it, and Z2M
                    # stayed bound to the previous radio while looking healthy.
                    "service_class": profile.get("service_class") or "",
                    "source": "native",
                    "reachability": "reachable",
                    "native_entry_id": entry.entry_id,
                })

            # Check for pending discovery flows (new devices not yet configured).
            # `include_uninitialized=True` is REQUIRED: an SSDP/zeroconf discovery
            # flow that HA is holding at its confirm step (the "Discovered" card in
            # Settings → Devices & Services — e.g. a Sonos found over SSDP) has
            # `cur_step is None` until the user opens it, and the default
            # `async_progress_by_handler` drops those. Without this we never saw
            # the pending Sonos flow even though it was sitting in HA's discovered
            # list, so the speaker never reached our Discovery picker.
            active_flows = hass.config_entries.flow.async_progress_by_handler(
                domain, include_uninitialized=True
            )
            if not active_flows and not existing_entries:
                _LOGGER.info("Native discovery: no %s flows or entries found", domain)

            # Some HA integrations handle multiple distinct product
            # families under one domain (victron_ble covers SmartShunt,
            # SmartSolar, Orion XS, Multiplus, etc.). Our profiles are
            # product-specific, so each flow needs to be routed to the
            # matching profile. Preferred gate: byte-derived `device_subtype`
            # from the cached advert — durable across customer renames
            # in VictronConnect. Fallback: `name_prefix` startswith match
            # (kept solely for products that don't yet have byte-level
            # subtype detection in `_annotate_victron_subtype` — today
            # that's only Cerbo GX, which is BLE-discovery-only).
            profile_subtype = str(profile.get("device_subtype") or "")
            profile_name_prefixes = profile.get("name_prefix") or []
            if isinstance(profile_name_prefixes, str):
                profile_name_prefixes = [profile_name_prefixes]

            seen_flow_keys: set[str] = set()
            for flow in active_flows:
                context = flow.get("context", {})
                unique_id = context.get("unique_id", "")
                # Skip if this device is already in the configured list
                if unique_id and unique_id in configured_uids:
                    continue
                # Dedup within pending flows too — BLE integrations can spawn
                # a fresh flow on every advertisement burst, so the same
                # physical device can appear twice with identical unique_ids
                # (and three or more times with blank unique_ids).
                flow_key = unique_id or flow.get("flow_id", "")
                if flow_key in seen_flow_keys:
                    continue
                seen_flow_keys.add(flow_key)
                flow_step = flow.get("step_id", "")
                title_placeholders = context.get("title_placeholders", {})
                flow_name = (
                    title_placeholders.get("name")
                    or title_placeholders.get("host")
                    or flow.get("handler", "")
                )
                # BLE integrations sometimes set title_placeholders["name"]
                # to the MAC when the first advert they saw had no
                # local_name. Later adverts usually carry the real name —
                # pull it from HA's bluetooth cache so our picker row
                # reads "Victron SmartShunt (HQ2137JCHXA)" instead of
                # "Victron SmartShunt (AA:BB:CC:DD:EE:FF)". Only bypass
                # the MAC when the cache actually gives us a richer name.
                if unique_id and _looks_like_mac(flow_name):
                    advert_name = _ble_cache_name(hass, unique_id)
                    if advert_name and not _looks_like_mac(advert_name):
                        flow_name = advert_name

                # Product-family gate. Accept if EITHER the byte-derived
                # subtype matches the profile's declared subtype, OR the
                # advert name starts with one of the profile's declared
                # prefixes. Bytes are the durable channel and are used
                # first; name is the fallback for products without
                # byte-level subtype detection (Cerbo today). Profiles
                # declaring neither are accepted unconditionally.
                if profile_subtype or profile_name_prefixes:
                    byte_match = bool(profile_subtype) and (
                        _ble_cache_subtype(hass, unique_id) == profile_subtype
                    )
                    name_match = bool(profile_name_prefixes) and any(
                        flow_name.startswith(p) for p in profile_name_prefixes
                    )
                    if not (byte_match or name_match):
                        _LOGGER.debug(
                            "Native discovery: skipping %s flow for %s — "
                            "neither byte-subtype %r nor name prefixes %s "
                            "matched the %s profile",
                            domain, unique_id, profile_subtype,
                            profile_name_prefixes, profile["name"],
                        )
                        continue
                _LOGGER.info(
                    "Native discovery: found %s flow (step=%s uid=%s name=%s context=%s)",
                    profile["name"],
                    flow_step,
                    unique_id,
                    flow_name,
                    list(context.keys()),
                )

                # Try to get IP from flow context. NB: `configuration_url` can be
                # present-but-None on an SSDP flow (Sonos), so `.get(key, "")`
                # returns None, not "" — guard with `or ""` before .replace(),
                # else this raises AttributeError, the domain-level except below
                # swallows it at DEBUG, and the Sonos produces NO rows silently.
                # (This branch never ran for SMLight: its zeroconf flow carries
                # `host` in title_placeholders and short-circuits above.)
                config_url = context.get("configuration_url") or ""
                flow_host = (
                    title_placeholders.get("host")
                    or config_url.replace("http://", "").replace("https://", "").split(":")[0]
                    or ""
                )

                pending_hostname = flow_name or ""
                pending_name = _build_discovery_name(
                    profile["name"], pending_hostname, unique_id, flow_host,
                )

                devices.append({
                    "name": pending_name,
                    "discovery_id": f"native-{domain}-{unique_id or 'unknown'}",
                    "hostname": pending_hostname or profile["name"],
                    "ip_address": flow_host,
                    # Populate `address` with the colon-separated MAC so the
                    # picker's Address column isn't blank for BLE-native rows
                    # (Ruuvi / Govee / SmartShunt pending flows).
                    "address": unique_id if _looks_like_mac(unique_id) else "",
                    "mac_address": unique_id.replace(":", "") if unique_id else "",
                    "device_type": profile["device_type"],
                    "model": profile["vendor_model"],
                    "vendor": profile["name"],
                    "vendor_device_type": profile["device_type"],
                    "vendor_model": profile["vendor_model"],
                    "category": profile["category"],
                    # Registration sends this as the device's `role`. Without it a
                    # coordinator adopted through a native flow registered as
                    # `unknown`, the Zigbee resolver could not recognise it, and Z2M
                    # stayed bound to the previous radio while looking healthy.
                    "service_class": profile.get("service_class") or "",
                    "source": "native",
                    "reachability": "reachable",
                    # The picker's filter rules key on `integration`, and a row
                    # matching NO rule falls into the debug-gated Others group,
                    # where it is invisible unless Debug mode is on. That is
                    # silent: discovery works, the row exists, and the customer
                    # sees nothing.
                    #
                    # This row carried `native_domain` alone, so a device whose
                    # ONLY route in is a pending native flow was always hidden.
                    # A device that ALSO answers a probe was fine, because the
                    # probe row carries an integration, which is why this went
                    # unnoticed: every affected device had a second route until
                    # the SLZB simulator, whose sim OUI the probe sweep rejects
                    # by design, arrived with only this one.
                    "integration": domain,
                    "native_flow_id": flow.get("flow_id"),
                    "native_domain": domain,
                    "native_unique_id": unique_id,
                })

        except Exception as err:
            # A crash here drops EVERY device for this domain with no row — keep
            # it visible (one concise WARNING line, the full traceback at DEBUG)
            # rather than swallowing it at DEBUG, which is what hid the Sonos
            # `configuration_url` AttributeError above.
            _LOGGER.warning(
                "Native discovery: error checking %s — %s: %s",
                domain, type(err).__name__, err,
            )
            _LOGGER.debug("Native discovery: %s traceback", domain, exc_info=True)

    return devices


def _normalize_for_match(value: str | None) -> str:
    """Normalize strings for matching."""
    if not value:
        return ""
    return value.strip().lower()


def _assign_category(device: dict[str, Any]) -> str:
    """Derive a device's view-routing category from its service class.

    `category` is no longer stored independently — it's always the rollup of
    the canonical `service_class` via `category_of()`, so there's a single
    source of truth. We take any `service_class` already on the row, else
    `classify()` it from its vendor vocabulary, then map class -> category.
    A pre-existing stored `category`, then a device_type heuristic, remain
    as last-resort fallbacks for rows `classify()` can't yet place — so an
    unrecognised device still lands somewhere sensible rather than vanishing.
    """
    service_class = device.get("service_class") or classify(device)
    if service_class:
        derived = category_of(service_class)
        if derived:
            return derived

    category = device.get("category")
    if category in CATEGORY_ORDER:
        return category

    device_type = _normalize_for_match(
        device.get("vendor_device_type")
        or device.get("type")
        or device.get("device_type")
        or ""
    )

    if device_type in {"battery_monitor", "electrical_shunt", "power"}:
        return "electrical"
    if device_type in {"relay"}:
        return "switching"
    if device_type in {"bluetooth", "bluetooth_proxy", "connectivity", "network", "router", "satellite_internet"}:
        return "network"

    return "electrical"


def _enrich_from_catalog(
    device: dict[str, Any], include_experimental: bool = False
) -> dict[str, Any]:
    """Enrich a discovered device with catalog metadata (integration, ignore_native)."""
    enriched = dict(device)
    # Stamp the canonical service class (the single source of truth) so it
    # rides on the discovery row, then derive category from it. classify()
    # resolves it from the row's vendor vocabulary (device_type / Venus
    # service / integration); it's pure and safe at discovery time.
    if not enriched.get("service_class"):
        service_class = classify(enriched)
        if service_class:
            enriched["service_class"] = service_class
    enriched["category"] = _assign_category(enriched)

    discovered_type = _normalize_for_match(
        device.get("vendor_device_type")
        or device.get("type")
        or device.get("device_type")
    )
    discovered_model = _normalize_for_match(
        device.get("vendor_model")
        or device.get("model")
    )

    for catalog_item in PHYSICAL_DEVICE_CATALOG:
        # A device can ship its catalog entry (and all the code / dashboard
        # wiring that references it) BEFORE it is ready to surface to customers,
        # via either gate — the hard `discovery_enabled: false` or the soft
        # `experimental: true` (see `_entry_hidden`). A hidden entry is skipped
        # here, so a matching physical device is never enriched, named or
        # tagged — it falls to the hidden "Others" group instead of becoming an
        # addable, recognised device.
        if _entry_hidden(catalog_item, include_experimental):
            continue

        catalog_type = _normalize_for_match(catalog_item.get("vendor_device_type"))
        catalog_model = _normalize_for_match(catalog_item.get("vendor_model"))

        if not catalog_type:
            continue

        if discovered_type != catalog_type:
            continue

        if catalog_model and discovered_model != catalog_model:
            continue

        # Match found — enrich with catalog metadata
        enriched["catalog_id"] = catalog_item["id"]
        enriched["integration"] = catalog_item.get("integration", "esphome")
        # Surface the experimental flag so the picker can badge the row. A
        # match only happens here when `include_experimental` is on (an
        # experimental entry is skipped by `_entry_hidden` above otherwise), so
        # the flag is only ever set on rows the customer/dev deliberately chose
        # to reveal.
        if catalog_item.get("experimental"):
            enriched["experimental"] = True
        # Only fill in a catalog name when the row doesn't carry its own.
        # BLE advertisements often include identifying info in the name
        # (e.g. "ROAM121051182" encodes voltage/capacity/serial); clobbering
        # that with the generic catalog name both hides the info from the
        # customer and breaks name_prefix filter rules like "ROAM".
        #
        # For catalog entries with `append_mac_suffix: true`, tag the
        # catalog name with the device's last-4 MAC so a fleet of identical
        # units is distinguishable:
        #   * Sensa BT proxies — hostnames are auto-generated junk like
        #     `ss-bt-proxy-c124-0ed520`; the name has no trailing bracket,
        #     so we append a fresh one: "Sensa Bluetooth Proxy (#0ED5)".
        #   * Sensa relays — the catalog name already ends in a transport
        #     bracket ("Sensa Relay Controller (Wi-Fi)"), so we merge the
        #     tag INTO it: "Sensa Relay Controller (Wi-Fi #1001)" rather
        #     than the doubled "... (Wi-Fi) (#1001)".
        # MAC is read from any of the keys discovery uses (`mac_address` /
        # `address` for native/BLE rows, `mac` for the ESPHome HTTP probe).
        # For a simulator the 02:5E:00 OUI makes the tag read as a fake
        # (same MAC-is-the-tell rule as RuuviTag/Govee).
        if catalog_item.get("name") and not enriched.get("name"):
            base_name = catalog_item["name"]
            if catalog_item.get("append_mac_suffix"):
                mac = (
                    enriched.get("mac_address")
                    or enriched.get("address")
                    or enriched.get("mac")
                    or ""
                )
                mac_hex = str(mac).replace(":", "").replace("-", "").upper()
                if len(mac_hex) >= 4:
                    tag = mac_hex[-4:]
                    # Simulated Sensa-flashed devices carry the 02:5E:00
                    # locally-administered OUI (real ESP32s use a global
                    # Espressif OUI, so this never false-positives on real
                    # hardware). Append a visible " SIM" so an operator can
                    # tell a fake relay/proxy from the real thing in the
                    # picker — same intent as the "-SIM" advert suffix the
                    # BLE sims already surface.
                    suffix = f"#{tag} SIM" if mac_hex.startswith(SIM_MAC_OUI) else f"#{tag}"
                    if base_name.endswith(")"):
                        enriched["name"] = f"{base_name[:-1]} {suffix})"
                    else:
                        enriched["name"] = f"{base_name} ({suffix})"
                else:
                    enriched["name"] = base_name
            else:
                enriched["name"] = base_name
        if catalog_item.get("ignore_native"):
            enriched["ignore_native"] = catalog_item["ignore_native"]
        if catalog_item.get("accept_native"):
            enriched["accept_native"] = catalog_item["accept_native"]
        # A catalog `device_subtype` feeds the picker's filter rules (the
        # same field Victron BLE rows carry at runtime), so a catalog match
        # can drive a per-subtype tag where `integration` alone is too coarse
        # — e.g. ESPHome heaters tagged HEATER instead of the generic ESPHome
        # PROXY catch-all. Only fill when the row doesn't already carry one.
        if catalog_item.get("device_subtype") and not enriched.get("device_subtype"):
            enriched["device_subtype"] = catalog_item["device_subtype"]
        if catalog_item.get("image") and not enriched.get("image"):
            enriched["image"] = catalog_item["image"]
        # Brand mark the picker draws above the metadata in the expanded row
        # (Autoterm today; written permission held, 2026-07-25). Same fill
        # rule as `image`: the catalog supplies it, never overrides one.
        if catalog_item.get("brand_logo") and not enriched.get("brand_logo"):
            enriched["brand_logo"] = catalog_item["brand_logo"]
        # The maker's name, shown on the heater card when there is no logo.
        if catalog_item.get("brand") and not enriched.get("brand"):
            enriched["brand"] = catalog_item["brand"]
        # model_images lives under model_detection now — keep reading any
        # legacy top-level value during the migration window, but prefer
        # the nested location when both are present.
        catalog_detection = catalog_item.get("model_detection") or {}
        catalog_model_images = (
            catalog_detection.get("model_images")
            or catalog_item.get("model_images")
        )
        if catalog_model_images and not enriched.get("model_images"):
            enriched["model_images"] = catalog_model_images
        break

    return enriched


async def _async_arm_setup_wizard(hass: Any) -> None:
    """A new entry is a new system, or the re-install after a factory reset:
    either way the owner sees the Setup Wizard next."""
    from .setup_wizard import async_arm

    await async_arm(hass)

class SensaNomadConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sensa Nomad."""

    VERSION = 2

    def __init__(self) -> None:
        # Carried between connection form → register form on fresh installs.
        self._pending_entry_data: dict[str, Any] = {}
        self._licence_announcement: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle initial setup."""
        await self.async_set_unique_id(UNIQUE_ID)
        self._abort_if_unique_id_configured()

        return await self._async_step_connection_form(
            step_id="user",
            user_input=user_input,
            existing_data=None,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reconfiguration — just re-enter the license key."""
        entry = self._get_reconfigure_entry()
        await self.async_set_unique_id(UNIQUE_ID)
        self._abort_if_unique_id_mismatch()

        return await self._async_step_connection_form(
            step_id="reconfigure",
            user_input=user_input,
            existing_data=entry.data,
        )

    async def async_step_import(
        self, import_data: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create the entry on an appliance without a UI run or licence gate.

        On an appliance the add-on seeds ``sensa_nomad:`` into
        configuration.yaml and ``async_setup`` fires this import flow, because
        the customer logins are all non-admin and can't add the integration via
        Settings > Devices & Services. Unlike ``async_step_user`` this:

        - does NOT contact cloud licensing — the entry is created immediately so
          the panels come up even offline; the 14-day trial is provisioned at
          runtime by the licence poller once the unit has internet (see
          ``__init__._async_maybe_provision_appliance_trial``);
        - does NOT restart HA — we're already mid-startup, and creating the
          entry drives ``async_setup_entry`` in this same boot.

        Singleton unique id makes it idempotent: a second boot (entry already
        present) aborts cleanly.
        """
        await self.async_set_unique_id(UNIQUE_ID)
        self._abort_if_unique_id_configured()

        entry_data = {
            CONF_REDEMPTION_CODE: "",
            CONF_PROFILE_NAME: DEFAULT_PROFILE_NAME,
            CONF_BACKEND_URL: resolve_backend_url(self.hass),
        }
        _LOGGER.info("Sensa Nomad: self-installing config entry (appliance import)")
        await _async_arm_setup_wizard(self.hass)
        return self.async_create_entry(title="Sensa Nomad", data=entry_data)

    async def _async_restart_ha(self) -> None:
        """Wait briefly, then restart HA so the new Sensa dashboards load.

        The dashboards are written to disk and registered in
        configuration.yaml by async_setup_entry, but HA's storage-mode
        Lovelace only picks up new dashboard files at startup. Running
        restart as a background task means the config-flow result can
        render its success screen before HA goes down.
        """
        _LOGGER.info("Sensa Nomad: restarting Home Assistant to load dashboards")
        await asyncio.sleep(3)
        try:
            await self.hass.services.async_call(
                "homeassistant", "restart", {}, blocking=False
            )
        except Exception:
            _LOGGER.warning(
                "Sensa Nomad: restart call failed — customer will need to restart manually",
                exc_info=True,
            )

    async def _async_step_connection_form(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        existing_data: dict[str, Any] | None,
    ) -> FlowResult:
        """License-key entry + backend health check.

        Stage B: this is the whole config flow. Scanning, device selection,
        and dashboard generation moved to the Discovery view + async_setup_entry
        respectively. A successful submit creates (or updates) the config entry
        and returns — async_setup_entry takes it from there.
        """
        errors: dict[str, str] = {}

        defaults = existing_data or {}
        default_code = defaults.get(CONF_REDEMPTION_CODE, "")

        if user_input is not None:
            cleaned_input = dict(user_input)
            # Hardcode profile name; resolve backend URL from Supervisor
            # so we don't drift across edge/beta/stable distribution
            # repos (each picks up a different addon hostname prefix).
            backend_url = resolve_backend_url(self.hass)
            cleaned_input[CONF_PROFILE_NAME] = DEFAULT_PROFILE_NAME
            cleaned_input[CONF_BACKEND_URL] = backend_url

            client = SensaApiClient(self.hass, backend_url)

            try:
                health = await client.async_health()

                if health.get("status") != "ok":
                    errors["base"] = "cannot_connect"
                else:
                    entry_data = {
                        CONF_REDEMPTION_CODE: cleaned_input.get(CONF_REDEMPTION_CODE, ""),
                        CONF_PROFILE_NAME: DEFAULT_PROFILE_NAME,
                        CONF_BACKEND_URL: backend_url,
                    }

                    # -- Licensing: redeem a code or auto-provision a trial ----
                    redemption_code = entry_data[CONF_REDEMPTION_CODE].strip()
                    try:
                        from homeassistant.helpers import instance_id
                        ha_uuid = await instance_id.async_get(self.hass)
                    except Exception:  # noqa: BLE001
                        ha_uuid = None
                    licence_error = await self._async_handle_licensing(
                        client, redemption_code, ha_uuid
                    )
                    if licence_error:
                        errors["base"] = licence_error
                    else:
                        if self.source == SOURCE_RECONFIGURE:
                            entry = self._get_reconfigure_entry()
                            merged = {**entry.data, **entry_data}
                            return self.async_update_reload_and_abort(
                                entry,
                                data=merged,
                            )

                        # Fresh install — clear any stale backend state left from
                        # previous testing so Discoveries / Devices counts start
                        # at zero. Failures here are non-fatal; worst case the
                        # customer sees stale entries until they hit Scan.
                        try:
                            _LOGGER.info("Sensa Nomad: clearing backend registries for fresh install")
                            await client.async_clear_devices()
                            await client.async_clear_discoveries()
                        except Exception:
                            _LOGGER.warning(
                                "Sensa Nomad: backend clear on fresh install failed",
                                exc_info=True,
                            )

                        # Registration form is only for *paid* activations —
                        # trial installs go straight through. Reasoning:
                        # the trial is the friction-free "click install,
                        # use it for 14 days" path; demanding a name and
                        # email up-front is exactly the wall we're trying
                        # not to put in their way. If they later enter a
                        # paid licence key (reconfigure path), they hit
                        # the form then. A returning install whose
                        # registration was already captured (uninstall +
                        # reinstall on the same hardware) also skips.
                        if not redemption_code:
                            self.hass.async_create_task(self._async_restart_ha())
                            await _async_arm_setup_wizard(self.hass)
                            return self.async_create_entry(title="Sensa Nomad", data=entry_data)

                        reg = await self._async_get_registration_status(client)
                        if reg == "complete":
                            self.hass.async_create_task(self._async_restart_ha())
                            await _async_arm_setup_wizard(self.hass)
                            return self.async_create_entry(title="Sensa Nomad", data=entry_data)

                        self._pending_entry_data = entry_data
                        return await self.async_step_register()

            except (ConnectionError, TimeoutError):
                # TimeoutError is an OSError sibling of ConnectionError —
                # both mean "couldn't reach the add-on backend", so they
                # share the cannot_connect message. Without listing
                # TimeoutError explicitly it falls through to the
                # generic "unknown" branch, which hides what the
                # customer actually needs to do (check the add-on is
                # running).
                _LOGGER.exception("Sensa Nomad: connection error while validating backend")
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Sensa Nomad: unexpected error while validating backend")
                errors["base"] = "unknown"

        data_schema = vol.Schema(
            {
                vol.Optional(CONF_REDEMPTION_CODE, default=default_code): str,
            }
        )

        description_placeholders: dict[str, str] = {}
        if self._licence_announcement:
            description_placeholders["licence_announcement"] = self._licence_announcement

        return self.async_show_form(
            step_id=step_id,
            data_schema=data_schema,
            errors=errors,
            description_placeholders=description_placeholders or None,
        )

    async def _async_handle_licensing(
        self,
        client: SensaApiClient,
        redemption_code: str,
        ha_uuid: str | None,
    ) -> str | None:
        """Redeem a code or auto-trial. Return error key on failure, else ``None``."""
        try:
            if redemption_code:
                response = await client.async_post_license_redeem(
                    code=redemption_code,
                    slot="production",
                    ha_uuid=ha_uuid,
                )
                status = response.get("status")
                if status == "activated":
                    self._licence_announcement = "Licence activated."
                    return None
                if status == "rebind_limit":
                    self._licence_announcement = None
                    return "licence_rebind_limit"
                if status == "invalid_code":
                    self._licence_announcement = None
                    return "licence_invalid"
                _LOGGER.warning("Sensa Nomad: redeem returned %s", response)
                return "licence_invalid"

            response = await client.async_post_license_trial(
                edition="nomad",
                ha_uuid=ha_uuid,
            )
            status = response.get("status")
            if status == "trial_issued":
                days = response.get("trial_expires_at", "")
                self._licence_announcement = f"14-day trial started (ends {days[:10]})."
                return None
            # The backend may return any of these when a trial is no
            # longer available — `_exhausted` (already used the slot),
            # `_expired` (slot used, time elapsed), or `_used` (older
            # naming). Treat them all as the same customer-facing
            # error since the action is identical: enter a paid licence.
            if status in ("trial_exhausted", "trial_expired", "trial_used"):
                self._licence_announcement = (
                    f"This hardware has already used its free trial. "
                    f"Purchase a licence at {PURCHASE_URL} and re-enter it here."
                )
                return "trial_exhausted"
            _LOGGER.warning("Sensa Nomad: trial returned %s", response)
            return "licence_unavailable"

        except (ConnectionError, TimeoutError):
            _LOGGER.exception("Sensa Nomad: cloud licensing API unreachable")
            return "licence_unreachable"
        except Exception:
            _LOGGER.exception("Sensa Nomad: unexpected licensing error")
            return "unknown"

    async def _async_get_registration_status(self, client: SensaApiClient) -> str:
        try:
            reg = await client.async_get_registration_status()
            return (reg.get("registration") or {}).get("status", "not_submitted")
        except Exception:
            _LOGGER.debug("Sensa Nomad: registration status check failed", exc_info=True)
            return "not_submitted"

    async def async_step_register(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect customer name + contact email after a paid activation.

        Only reachable when a paid licence key was entered AND backend
        registration isn't already on file for this install (a
        reinstall on the same hardware skips this step). Trial installs
        never see this form — see the comment in
        `_async_step_connection_form` for the reasoning.

        Name + email are both mandatory at the form level. Backend
        submission failure is non-fatal: the App queues the
        registration for a background retry, so a transient cloud
        outage doesn't strand a paying customer mid-setup.
        """
        errors: dict[str, str] = {}
        entry_data = self._pending_entry_data

        if user_input is not None:
            name = (user_input.get(CONF_CUSTOMER_NAME) or "").strip()
            email = (user_input.get(CONF_CONTACT_EMAIL) or "").strip()
            if not name or "@" not in email:
                errors["base"] = "registration_required"
            else:
                client = SensaApiClient(self.hass, resolve_backend_url(self.hass))
                try:
                    await client.async_post_register(
                        customer_name=name,
                        contact_email=email,
                        licence_key=entry_data.get(CONF_REDEMPTION_CODE) or None,
                    )
                except Exception:
                    _LOGGER.info(
                        "Sensa Nomad: registration submit failed, App will retry in background",
                        exc_info=True,
                    )

                self.hass.async_create_task(self._async_restart_ha())
                await _async_arm_setup_wizard(self.hass)
                return self.async_create_entry(title="Sensa Nomad", data=entry_data)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_CUSTOMER_NAME): str,
                vol.Required(CONF_CONTACT_EMAIL): str,
            }
        )
        return self.async_show_form(
            step_id="register",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "licence_announcement": self._licence_announcement or "",
            },
        )

