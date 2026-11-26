"""Vendor-agnostic ESPHome subnet scanner.

Scans a subnet for ESPHome devices, probes them via HTTP, and reads
vendor metadata from the SSE event stream. Results are persisted to
the device registry as discovery records.
"""

from __future__ import annotations

import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from sensa_core.discovery.categories import map_device_type_to_category
from sensa_core.network.esphome import (
    DEFAULT_HTTP_PORT,
    fetch_esphome_events_chunk,
    fetch_esphome_root_page,
    is_esphome_web_shell,
    parse_esphome_event_fields,
)
from sensa_core.network.helpers import (
    is_tcp_port_open,
    reverse_lookup_hostname,
)
from sensa_core.registry.discoveries import upsert_discovery

_LOGGER = logging.getLogger(__name__)


@dataclass
class VendorProfile:
    """Configuration for scanning a specific vendor's ESPHome devices."""

    name: str
    hostname_prefix: str
    event_field_mapping: dict[str, str]
    brand: str = ""
    http_port: int = DEFAULT_HTTP_PORT
    http_timeout: float = 3.0
    model_field: str = "model"
    mac_field: str = "device_mac"
    device_type_field: str = "device_type"
    version_field: str = "version"
    # Only the identifying fields gate discovery. Everything else (version,
    # MAC, IP, channel) is captured if present but never required, so a device
    # isn't dropped when the firmware's metadata set drifts.
    required_fields: tuple[str, ...] = ("device_type", "model")
    # Fields the /events read should WAIT for (best-effort) before it stops,
    # but which do NOT gate discovery. The MAC is the stable identity anchor
    # for a discovery record (`make_discovery_id` prefers it over the
    # hostname): if the read stops the moment device_type+model appear — which
    # is often before the MAC text_sensor streams — the record falls back to a
    # hostname-based id (reverse-DNS vs the model+suffix fallback differ), the
    # id flips between scans, and a picker selection can submit a superseded id
    # ("no discovery row"). Waiting for the MAC keeps the id MAC-stable; a
    # device that never emits it still parses on timeout and shows with a
    # hostname id (the old behaviour).
    wait_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.brand:
            self.brand = self.name.title()

    def events_wait_markers(self) -> list[str]:
        """ESPHome event NAMES the /events read should wait for: the
        identifying `required_fields` plus best-effort `wait_fields` (the MAC,
        for a stable discovery_id). Maps output keys back to their event names
        via `event_field_mapping`."""
        wait_keys = set(self.required_fields) | set(self.wait_fields)
        return [
            name
            for name, key in self.event_field_mapping.items()
            if key in wait_keys
        ]


def _fallback_hostname(
    ip: str,
    parsed: dict[str, str],
    prefix: str,
    model_field: str,
    mac_field: str,
) -> str:
    """Build a fallback hostname when reverse DNS is unavailable."""
    model = (parsed.get(model_field) or "").lower().replace(" ", "-")
    mac = (parsed.get(mac_field) or "").lower().replace(":", "")
    suffix = mac[-6:] if mac else ""
    if model and suffix:
        return f"{prefix}{model}-{suffix}"
    if suffix:
        return f"{prefix}{suffix}"
    return ip


def _probe_host(
    ip: str,
    vendor: VendorProfile,
    username: str,
    password: str,
) -> dict[str, str] | None:
    """Probe a single host for an ESPHome device. Returns device dict or None."""
    hostname = reverse_lookup_hostname(ip)
    if hostname:
        hostname = hostname.split(".", 1)[0]

    if hostname and not hostname.lower().startswith(vendor.hostname_prefix.lower()):
        _LOGGER.debug(
            "probe %s: hostname %r doesn't match prefix %r — skipping",
            ip, hostname, vendor.hostname_prefix,
        )
        return None

    if not is_tcp_port_open(ip, vendor.http_port, timeout=0.5):
        return None

    html = fetch_esphome_root_page(ip, username, password, timeout=vendor.http_timeout)
    if not html:
        _LOGGER.debug("probe %s: no/empty root page (auth or unreachable)", ip)
        return None
    if not is_esphome_web_shell(html):
        _LOGGER.debug("probe %s: port-80 page is not an ESPHome web shell", ip)
        return None

    # Wait for the identifying markers PLUS any `wait_fields` (the MAC, so the
    # discovery_id is MAC-stable — see VendorProfile.wait_fields). wait_fields
    # are best-effort: they extend how long the read lingers but don't gate the
    # parse below, so a device that never emits one still isn't dropped — the
    # read just falls through to its timeout, then parses on required_fields.
    required_markers = vendor.events_wait_markers()
    events_text = fetch_esphome_events_chunk(
        ip,
        username,
        password,
        port=vendor.http_port,
        timeout=vendor.http_timeout,
        required_markers=required_markers,
    )
    if not events_text:
        _LOGGER.debug("probe %s: ESPHome shell but no /events body", ip)
        return None

    parsed = parse_esphome_event_fields(
        events_text,
        vendor.event_field_mapping,
        required=list(vendor.required_fields),
    )
    if not parsed:
        _LOGGER.debug(
            "probe %s: /events missing identifying Sensa fields %s",
            ip, list(vendor.required_fields),
        )
        return None

    effective_hostname = hostname or _fallback_hostname(
        ip, parsed, vendor.hostname_prefix, vendor.model_field, vendor.mac_field,
    )

    device_type = parsed.get(vendor.device_type_field)
    category = map_device_type_to_category(device_type) if device_type else "electrical"
    mac = parsed.get(vendor.mac_field)

    upsert_discovery(
        hostname=effective_hostname,
        ip_address=ip,
        mac_address=mac,
        device_type=device_type,
        model=parsed.get(vendor.model_field),
        version=parsed.get(vendor.version_field),
        vendor=vendor.brand,
        reachability="reachable",
        auth_state="required" if (username or password) else "none",
        parse_state="complete",
        source=f"{vendor.name}_http_probe",
    )

    return {
        "id": mac or effective_hostname,
        "name": f"{vendor.brand} {parsed.get(vendor.model_field, 'Unknown')}",
        "type": device_type or "unknown",
        "category": category,
        "integration": vendor.name,
        "source": f"{vendor.name}_http",
        "confidence": "high",
        "ip": ip,
        "hostname": effective_hostname,
        "model": parsed.get(vendor.model_field, ""),
        "version": parsed.get(vendor.version_field, ""),
        "mac": mac or "",
    }


def expand_scan_subnets(scan_subnet: str) -> list[str]:
    """Expand the integration's ``scan_subnet`` setting into host addresses.

    The setting is a single CIDR or a comma-separated list
    (``192.168.1.0/24,192.168.2.0/24``): the integration's Cerbo and TCP
    sweeps split it the same way, and a customer with an IoT VLAN is told to
    set exactly that. Handing the whole string to ``ipaddress`` raised, which
    killed the ESPHome sweep (and so every Sensa-flashed device) the moment a
    second subnet was added. An invalid part is logged and skipped rather than
    aborting the parts that are fine; duplicate addresses across overlapping
    ranges are probed once.
    """
    hosts: list[str] = []
    seen: set[str] = set()
    for part in (scan_subnet or "").split(","):
        cidr = part.strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as err:
            _LOGGER.warning(
                "ESPHome scan: invalid subnet %r (%s), skipping", cidr, err,
            )
            continue
        for host in network.hosts():
            ip = str(host)
            if ip not in seen:
                seen.add(ip)
                hosts.append(ip)
    return hosts


def scan_subnet(
    subnet: str,
    vendor: VendorProfile,
    username: str = "",
    password: str = "",
    max_workers: int = 20,
) -> list[dict[str, str]]:
    """Scan a subnet for ESPHome devices matching a vendor profile.

    Probes hosts concurrently using up to ``max_workers`` threads.
    Returns a list of candidate dicts for each discovered device.
    Also persists each find to the discovery registry.
    """
    hosts = expand_scan_subnets(subnet)

    _LOGGER.info(
        "ESPHome scan: %d hosts across %r, vendor=%s, workers=%d",
        len(hosts),
        subnet,
        vendor.name,
        max_workers,
    )

    devices: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_probe_host, ip, vendor, username, password): ip
            for ip in hosts
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    devices.append(result)
            except Exception:
                _LOGGER.debug(
                    "ESPHome scan: error probing %s", futures[future], exc_info=True,
                )

    _LOGGER.info("ESPHome scan: found %d device(s)", len(devices))
    return devices
