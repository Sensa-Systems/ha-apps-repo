"""Generic network helper functions."""

from __future__ import annotations

import ipaddress
import socket


def normalize_mac(mac: str | None) -> str | None:
    """Normalize a MAC address to lowercase hex without separators."""
    if not mac:
        return None
    return "".join(ch for ch in mac.lower() if ch.isalnum())


def is_ip_address(value: str | None) -> bool:
    """Return True if value is a valid IP address."""
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def reverse_lookup_hostname(ip: str) -> str | None:
    """Return reverse-DNS hostname for an IP, or None."""
    try:
        hostname, _, _ = socket.gethostbyaddr(ip)
        hostname = hostname.rstrip(".")
        if is_ip_address(hostname):
            return None
        return hostname
    except Exception:  # noqa: BLE001
        return None


def is_tcp_port_open(ip: str, port: int, timeout: float = 0.25) -> bool:
    """Return True if a TCP port is open on the target host."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False
