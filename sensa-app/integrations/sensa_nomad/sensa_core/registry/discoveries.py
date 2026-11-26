"""Discovery record management."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sensa_core.network.helpers import normalize_mac
from sensa_core.registry.store import (
    load_registry,
    registry_transaction,
    save_registry,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_discovery_id(hostname: str, mac_address: str | None) -> str:
    """Generate a stable discovery ID from MAC or hostname."""
    normalized = normalize_mac(mac_address)
    if normalized:
        return f"disc-{normalized}"
    return f"disc-{hostname.lower()}"


def _find_by_identity(
    discoveries: list[dict],
    hostname: str,
    normalized_mac: str | None,
) -> dict | None:
    if normalized_mac:
        for item in discoveries:
            if item.get("normalized_mac") == normalized_mac:
                return item

    hostname_key = hostname.lower()
    for item in discoveries:
        if item.get("hostname", "").lower() == hostname_key:
            return item
    return None


def list_discoveries() -> list[dict]:
    data = load_registry()
    return sorted(
        data.get("discoveries", []),
        key=lambda item: (
            item.get("hostname", "").lower(),
            item.get("last_seen_utc", ""),
        ),
    )


def clear_discoveries() -> int:
    """Remove all discoveries from the registry. Returns count removed."""
    with registry_transaction():
        data = load_registry()
        count = len(data.get("discoveries", []))
        data["discoveries"] = []
        save_registry(data)
        return count


def prune_stale_discoveries(cutoff_iso: str) -> int:
    """Drop STALE ghost discovery records — those last seen before `cutoff_iso`.

    A discovery record is keyed on MAC-then-hostname (`_find_by_identity`), so
    if a device's identity changes — a sim rebuilt with a new LAA MAC, a
    replaced NIC, a hostname change — the old record no longer matches the new
    one and `upsert_discovery` appends a SECOND record. Nothing ever removed the
    first, so the ghost persisted in `list_discoveries()` forever (the picker
    showed the device twice — e.g. the Baltica ethernet sim after its MAC was
    reassigned off the FreshJet's 0x16 clash).

    `cutoff_iso` must be a GRACE window before the scan (e.g. now − 15 min), NOT
    the scan-start instant: every device found this scan re-`upsert`s its
    `last_seen_utc` to ~now, so anything older than the grace window genuinely
    wasn't seen for a while. Keying off the scan-start instant instead would
    delete a device that merely missed ONE scan (a Wi-Fi blip, a device
    rebooting) or, via a sub-second backward NTP step, one that was literally
    just seen. The grace window is the sole guard the caller needs — it is safe
    to call on an empty/failed scan too (recently-seen records survive on their
    timestamp; only genuinely-stale ones age out, which is exactly what should
    happen when the last device has departed).

    A record linked to a confirmed device is kept even when stale — a confirmed
    device that was merely offline must not lose its discovery row — UNLESS a
    FRESHER record links to the SAME confirmed device (the device's identity
    changed and this is the superseded old row, which would otherwise show the
    device twice). Returns the count removed.
    """
    from sensa_core.registry.devices import _find_by_identity as _find_device

    with registry_transaction():
        data = load_registry()
        discoveries = data.get("discoveries", [])
        devices = data.get("devices", [])

        # Resolve each record's confirmed-device link once, and track the
        # freshest last_seen among records linking to the same device so a
        # superseded (older-identity) linked ghost can be pruned.
        links: list[dict | None] = []
        freshest_for_device: dict[object, str] = {}
        for item in discoveries:
            dev = _find_device(
                devices, item.get("hostname", ""), item.get("normalized_mac")
            )
            links.append(dev)
            if dev is not None:
                did = dev.get("device_id") or id(dev)
                seen = item.get("last_seen_utc", "")
                if seen > freshest_for_device.get(did, ""):
                    freshest_for_device[did] = seen

        kept: list[dict] = []
        removed = 0
        for item, dev in zip(discoveries, links):
            if item.get("last_seen_utc", "") >= cutoff_iso:
                kept.append(item)  # seen recently — never stale
                continue
            if dev is not None:
                did = dev.get("device_id") or id(dev)
                # Keep unless a FRESHER record links to the same device (this is
                # the superseded old identity → prune it to kill the duplicate).
                if item.get("last_seen_utc", "") >= freshest_for_device.get(did, ""):
                    kept.append(item)
                    continue
            removed += 1

        if removed:
            data["discoveries"] = kept
            save_registry(data)
        return removed


def get_discovery(discovery_id: str) -> dict | None:
    for item in list_discoveries():
        if item.get("discovery_id") == discovery_id:
            return item
    return None


def upsert_discovery(
    *,
    hostname: str,
    ip_address: str,
    mac_address: str | None = None,
    device_type: str | None = None,
    model: str | None = None,
    version: str | None = None,
    vendor: str = "unknown",
    reachability: str = "unknown",
    auth_state: str = "unknown",
    parse_state: str = "none",
    source: str = "network_probe",
) -> dict:
    """Insert or update a discovery record, matching on MAC then hostname."""
    with registry_transaction():
        data = load_registry()
        discoveries = data["discoveries"]
        devices = data["devices"]

        normalized_mac = normalize_mac(mac_address)
        existing = _find_by_identity(discoveries, hostname, normalized_mac)

        # Check if already linked to a confirmed device
        from sensa_core.registry.devices import _find_by_identity as find_device
        linked_device = find_device(devices, hostname, normalized_mac)

        now = _utc_now_iso()

        if existing:
            existing["hostname"] = hostname
            existing["ip_address"] = ip_address
            existing["mac_address"] = mac_address
            existing["normalized_mac"] = normalized_mac
            existing["device_type"] = device_type
            existing["model"] = model
            existing["version"] = version
            existing["vendor"] = vendor
            existing["reachability"] = reachability
            existing["auth_state"] = auth_state
            existing["parse_state"] = parse_state
            existing["registry_state"] = "known" if linked_device else "new"
            existing["last_seen_utc"] = now
            save_registry(data)
            return deepcopy(existing)

        record = {
            "discovery_id": make_discovery_id(hostname, mac_address),
            "source": source,
            "hostname": hostname,
            "ip_address": ip_address,
            "mac_address": mac_address,
            "normalized_mac": normalized_mac,
            "device_type": device_type,
            "model": model,
            "version": version,
            "vendor": vendor,
            "reachability": reachability,
            "auth_state": auth_state,
            "parse_state": parse_state,
            "registry_state": "known" if linked_device else "new",
            "first_seen_utc": now,
            "last_seen_utc": now,
        }

        discoveries.append(record)
        save_registry(data)
        return deepcopy(record)
