"""ARP table parsing for MAC address discovery."""

from __future__ import annotations

import ipaddress
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from sensa_core.network.helpers import normalize_mac

_LOGGER = logging.getLogger(__name__)


def get_arp_table() -> list[dict[str, str]]:
    """Parse the system ARP table and return IP-to-MAC mappings.

    Runs ``ip neigh`` (Linux) and parses each line.  Returns a list of
    dicts with keys ``ip`` and ``mac`` (normalised lowercase hex).

    Entries without a valid MAC (INCOMPLETE, FAILED) are skipped.
    """
    try:
        result = subprocess.run(
            ["ip", "neigh"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        _LOGGER.warning("'ip' command not found — ARP discovery unavailable")
        return []
    except subprocess.TimeoutExpired:
        _LOGGER.warning("'ip neigh' timed out")
        return []

    entries: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parsed = _parse_ip_neigh_line(line)
        if parsed:
            entries.append(parsed)

    _LOGGER.debug("ARP table: %d entries", len(entries))
    return entries


def get_mac_for_ip(ip: str) -> str | None:
    """Look up a single IP's MAC address from the ARP cache.

    Returns the normalised MAC or None if not found.
    """
    for entry in get_arp_table():
        if entry["ip"] == ip:
            return entry["mac"]
    return None


def _ping_host(ip: str, timeout: float) -> None:
    """Ping a single host. Failures are silently ignored."""
    try:
        subprocess.run(
            ["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip],
            capture_output=True,
            timeout=timeout + 1,
            check=False,
        )
    except Exception:  # noqa: BLE001, S110
        pass


def ping_sweep(subnet: str, timeout: float = 0.5, max_workers: int = 50) -> None:
    """Send a single ping to every host in the subnet to populate ARP cache.

    Runs up to ``max_workers`` pings concurrently.  Failures are silently
    ignored — the goal is just to trigger ARP entries.
    """
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(host) for host in network.hosts()]

    _LOGGER.debug("Ping sweep: %d hosts with %d workers", len(hosts), max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_ping_host, ip, timeout) for ip in hosts]
        for future in as_completed(futures):
            pass  # just wait for completion


def get_subnet_macs(subnet: str) -> list[dict[str, str]]:
    """Return ARP entries filtered to a specific subnet."""
    network = ipaddress.ip_network(subnet, strict=False)
    return [
        entry
        for entry in get_arp_table()
        if ipaddress.ip_address(entry["ip"]) in network
    ]


def _parse_ip_neigh_line(line: str) -> dict[str, str] | None:
    """Parse a single ``ip neigh`` output line.

    Expected format::

        192.168.1.1 dev eth0 lladdr 00:1e:42:ab:cd:ef REACHABLE

    Returns ``{"ip": "192.168.1.1", "mac": "001e42abcdef"}`` or None.
    """
    parts = line.split()
    if len(parts) < 5:
        return None

    ip_str = parts[0]
    try:
        ipaddress.ip_address(ip_str)
    except ValueError:
        return None

    # Skip entries without a resolved MAC (INCOMPLETE, FAILED, etc.)
    if "lladdr" not in parts:
        return None

    try:
        lladdr_index = parts.index("lladdr")
        raw_mac = parts[lladdr_index + 1]
    except (ValueError, IndexError):
        return None

    mac = normalize_mac(raw_mac)
    if not mac or len(mac) != 12:
        return None

    return {"ip": ip_str, "mac": mac}
