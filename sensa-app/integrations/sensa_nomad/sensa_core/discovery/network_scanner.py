"""Network device scanner — discovers devices by MAC OUI and HTTP probe.

Complements the ESPHome scanner for devices that are not ESPHome-based
(routers, gateways, cameras, etc.).  Devices are identified by their
MAC address OUI prefix and confirmed reachable via an HTTP port probe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sensa_core.discovery.categories import map_device_type_to_category
from sensa_core.network.arp import get_subnet_macs, ping_sweep
from sensa_core.network.helpers import (
    is_tcp_port_open,
    reverse_lookup_hostname,
)
from sensa_core.registry.discoveries import upsert_discovery

_LOGGER = logging.getLogger(__name__)


@dataclass
class NetworkDeviceProfile:
    """Configuration for discovering a non-ESPHome network device."""

    name: str
    mac_oui_prefixes: list[str] = field(default_factory=list)
    http_port: int = 80
    http_timeout: float = 3.0
    device_type: str = "unknown"
    vendor_model: str = ""


def scan_subnet_network(
    subnet: str,
    profiles: list[NetworkDeviceProfile],
) -> list[dict[str, str]]:
    """Scan a subnet for network devices matching any profile.

    1. Ping-sweep the subnet to populate the ARP cache.
    2. Read the ARP table for MAC addresses in the subnet.
    3. Match MACs against OUI prefixes in the profiles.
    4. Probe the HTTP port to confirm the device is reachable.
    5. Persist each match to the discovery registry.

    Returns a list of device dicts in the same format as the ESPHome
    scanner (``scan_subnet``).
    """
    if not profiles:
        return []

    _LOGGER.info(
        "Network scan: sweeping %s for %d profile(s)",
        subnet,
        len(profiles),
    )

    # Build a lookup of normalised OUI prefix → profile
    oui_lookup: dict[str, NetworkDeviceProfile] = {}
    for profile in profiles:
        for oui in profile.mac_oui_prefixes:
            oui_lookup[oui.lower().replace(":", "").replace("-", "")] = profile

    # Ping sweep to ensure ARP cache is populated
    ping_sweep(subnet)

    # Read ARP entries for this subnet
    arp_entries = get_subnet_macs(subnet)
    _LOGGER.debug("Network scan: %d ARP entries in %s", len(arp_entries), subnet)

    devices: list[dict[str, str]] = []

    for entry in arp_entries:
        mac = entry["mac"]
        ip = entry["ip"]

        # Check if MAC matches any OUI prefix
        matched_profile = _match_oui(mac, oui_lookup)
        if not matched_profile:
            continue

        _LOGGER.debug(
            "Network scan: OUI match for %s (%s) → %s",
            ip,
            mac,
            matched_profile.name,
        )

        # Confirm device is reachable via HTTP
        if not is_tcp_port_open(ip, matched_profile.http_port, timeout=0.5):
            _LOGGER.debug(
                "Network scan: %s port %d closed, skipping",
                ip,
                matched_profile.http_port,
            )
            continue

        hostname = reverse_lookup_hostname(ip) or ip
        category = map_device_type_to_category(matched_profile.device_type)

        # Format MAC for display (colon-separated)
        display_mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))

        upsert_discovery(
            hostname=hostname,
            ip_address=ip,
            mac_address=display_mac,
            device_type=matched_profile.device_type,
            model=matched_profile.vendor_model,
            version="",
            vendor=matched_profile.name,
            reachability="reachable",
            auth_state="unknown",
            parse_state="none",
            source=f"{matched_profile.name}_network_probe",
        )

        devices.append(
            {
                "id": display_mac,
                "name": f"{matched_profile.name.title()} {matched_profile.device_type.title()}",
                "type": matched_profile.device_type,
                "category": category,
                "integration": matched_profile.name,
                "source": f"{matched_profile.name}_network",
                "confidence": "medium",
                "ip": ip,
                "hostname": hostname,
                "model": matched_profile.vendor_model,
                "version": "",
                "mac": display_mac,
                "vendor_device_type": matched_profile.device_type,
                "vendor_model": matched_profile.vendor_model,
            }
        )

    _LOGGER.info("Network scan: found %d device(s)", len(devices))
    return devices


def _match_oui(
    mac: str,
    oui_lookup: dict[str, NetworkDeviceProfile],
) -> NetworkDeviceProfile | None:
    """Check if a normalised MAC matches any OUI prefix in the lookup."""
    for oui_prefix, profile in oui_lookup.items():
        if mac.startswith(oui_prefix):
            return profile
    return None
