"""Registry summary for status/health endpoints."""

from __future__ import annotations

from sensa_core.registry.devices import list_devices
from sensa_core.registry.discoveries import list_discoveries


def get_summary() -> dict:
    """Return aggregate counts for discoveries and devices."""
    discoveries = list_discoveries()
    devices = list_devices()

    last_seen_values = [
        item.get("last_seen_utc")
        for item in discoveries
        if item.get("last_seen_utc")
    ]

    return {
        "discoveries_total": len(discoveries),
        "discoveries_new": sum(
            1 for item in discoveries if item.get("registry_state") == "new"
        ),
        "discoveries_known": sum(
            1 for item in discoveries if item.get("registry_state") == "known"
        ),
        "devices_total": len(devices),
        "devices_enabled": sum(
            1 for item in devices if item.get("enabled") is True
        ),
        "devices_ignored": sum(
            1 for item in devices if item.get("ignored") is True
        ),
        "last_seen_utc": max(last_seen_values) if last_seen_values else None,
    }
