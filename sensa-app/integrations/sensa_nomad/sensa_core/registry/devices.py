"""Confirmed device management."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone

from sensa_core.registry.store import (
    load_registry,
    registry_transaction,
    save_registry,
)


class RegistryFullError(Exception):
    """Raised when adding a device would exceed the registry's capacity.

    Capacity is a plain number passed in by the caller (the App resolves it
    from the licence — see ``routes/license.py``); the registry itself stays
    licence-agnostic.
    """

    def __init__(self, count: int, capacity: int) -> None:
        super().__init__(
            f"Device limit reached: {count} active of {capacity} allowed."
        )
        self.count = count
        self.capacity = capacity


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def count_active_devices(devices: list[dict] | None = None) -> int:
    """Count devices that count toward the licence cap: enabled and not ignored."""
    items = devices if devices is not None else list_devices()
    return sum(
        1
        for d in items
        if d.get("enabled", True) and not d.get("ignored", False)
    )


def _find_by_identity(
    devices: list[dict],
    hostname: str | None,
    normalized_mac: str | None,
) -> dict | None:
    if normalized_mac:
        for item in devices:
            if item.get("linked_normalized_mac") == normalized_mac:
                return item

    if hostname:
        hostname_key = hostname.lower()
        for item in devices:
            if (item.get("linked_hostname") or "").lower() == hostname_key:
                return item
    return None


def list_devices() -> list[dict]:
    data = load_registry()
    return sorted(
        data.get("devices", []),
        key=lambda item: (
            item.get("category", "").lower(),
            item.get("display_name", "").lower(),
        ),
    )


def get_device(device_id: str) -> dict | None:
    for item in list_devices():
        if item.get("device_id") == device_id:
            return item
    return None


def confirm_device(
    *,
    discovery_id: str,
    display_name: str,
    category: str,
    role: str,
    integration_type: str = "unknown",
    notes: str = "",
    capacity: int | None = None,
) -> dict:
    """Promote a discovery to a confirmed device.

    ``capacity`` (when not None) caps the number of active devices; promoting a
    *new* device past it raises ``RegistryFullError``. Re-confirming a device
    that already exists is always allowed (it doesn't grow the count).
    """
    with registry_transaction():
        data = load_registry()
        discoveries = data["discoveries"]
        devices = data["devices"]

        discovery = None
        for item in discoveries:
            if item.get("discovery_id") == discovery_id:
                discovery = item
                break

        if not discovery:
            raise ValueError(f"Unknown discovery_id: {discovery_id}")

        now = _utc_now_iso()

        existing = _find_by_identity(
            devices,
            discovery.get("hostname"),
            discovery.get("normalized_mac"),
        )
        if existing:
            existing["updated_utc"] = now
            save_registry(data)
            return deepcopy(existing)

        if capacity is not None:
            active = count_active_devices(devices)
            if active >= capacity:
                raise RegistryFullError(active, capacity)

        record = {
            "device_id": f"dev-{uuid.uuid4()}",
            "display_name": display_name,
            "category": category,
            "role": role,
            "integration_type": integration_type,
            "linked_discovery_id": discovery["discovery_id"],
            "linked_hostname": discovery.get("hostname"),
            "linked_mac_address": discovery.get("mac_address"),
            "linked_normalized_mac": discovery.get("normalized_mac"),
            "ip_address": discovery.get("ip_address"),
            "device_type": discovery.get("device_type"),
            "model": discovery.get("model"),
            "version": discovery.get("version"),
            "vendor": discovery.get("vendor"),
            "enabled": True,
            "ignored": False,
            "notes": notes,
            "config": {},
            "created_utc": now,
            "updated_utc": now,
        }

        devices.append(record)
        discovery["registry_state"] = "known"
        save_registry(data)
        return deepcopy(record)


def register_device(
    *,
    device_id: str,
    display_name: str,
    category: str,
    role: str,
    integration_type: str = "manual",
    hostname: str | None = None,
    mac_address: str | None = None,
    ip_address: str | None = None,
    model: str | None = None,
    vendor: str | None = None,
    notes: str = "",
    capacity: int | None = None,
) -> dict:
    """Register a manual/catalog device directly (no discovery required).

    ``capacity`` (when not None) caps the number of active devices; registering
    a *new* device past it raises ``RegistryFullError``. Updating a device that
    already exists is always allowed (it doesn't grow the count).
    """
    from sensa_core.network.helpers import normalize_mac

    with registry_transaction():
        data = load_registry()
        devices = data["devices"]

        for item in devices:
            if item.get("device_id") == device_id:
                if hostname:
                    item["linked_hostname"] = hostname
                if mac_address:
                    item["linked_mac_address"] = mac_address
                    item["linked_normalized_mac"] = normalize_mac(mac_address)
                if ip_address:
                    item["ip_address"] = ip_address
                item["updated_utc"] = _utc_now_iso()
                save_registry(data)
                return deepcopy(item)

        if capacity is not None:
            active = count_active_devices(devices)
            if active >= capacity:
                raise RegistryFullError(active, capacity)

        normalized_mac = normalize_mac(mac_address) if mac_address else None
        now = _utc_now_iso()
        record = {
            "device_id": device_id,
            "display_name": display_name,
            "category": category,
            "role": role,
            "integration_type": integration_type,
            "linked_discovery_id": None,
            "linked_hostname": hostname,
            "linked_mac_address": mac_address,
            "linked_normalized_mac": normalized_mac,
            "ip_address": ip_address,
            "device_type": role,
            "model": model,
            "version": None,
            "vendor": vendor,
            "enabled": True,
            "ignored": False,
            "notes": notes,
            "config": {},
            "created_utc": now,
            "updated_utc": now,
        }

        devices.append(record)
        save_registry(data)
        return deepcopy(record)


def delete_device(device_id: str) -> bool:
    """Remove a device from the registry. Returns True if found and deleted."""
    with registry_transaction():
        data = load_registry()
        devices = data["devices"]
        before = len(devices)
        data["devices"] = [d for d in devices if d.get("device_id") != device_id]
        if len(data["devices"]) < before:
            save_registry(data)
            return True
        return False


def clear_devices() -> int:
    """Remove all devices from the registry. Returns count of devices removed."""
    with registry_transaction():
        data = load_registry()
        count = len(data.get("devices", []))
        data["devices"] = []
        save_registry(data)
        return count


def update_device(
    device_id: str,
    *,
    display_name: str | None = None,
    category: str | None = None,
    role: str | None = None,
    enabled: bool | None = None,
    ignored: bool | None = None,
    notes: str | None = None,
    config: dict | None = None,
) -> dict:
    """Update fields on a confirmed device."""
    with registry_transaction():
        data = load_registry()

        for item in data["devices"]:
            if item.get("device_id") != device_id:
                continue

            if display_name is not None:
                item["display_name"] = display_name
            if category is not None:
                item["category"] = category
            if role is not None:
                item["role"] = role
            if enabled is not None:
                item["enabled"] = enabled
            if ignored is not None:
                item["ignored"] = ignored
            if notes is not None:
                item["notes"] = notes
            if config is not None:
                existing_config = item.get("config", {})
                existing_config.update(config)
                item["config"] = existing_config

            item["updated_utc"] = _utc_now_iso()
            save_registry(data)
            return deepcopy(item)

        raise ValueError(f"Unknown device_id: {device_id}")
