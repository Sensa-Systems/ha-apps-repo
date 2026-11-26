"""MeatPi WiCAN Pro (OBD2) device enumeration.

The WiCAN Pro plugs into a vehicle's OBD2 port and — in its AutoPID + Home
Assistant Discovery mode — publishes engine/vehicle telemetry as HA
MQTT-discovery configs. HA's *shared* ``mqtt`` integration auto-creates the HA
device + one sensor entity per PID. This module reads HA's device registry to
surface that WiCAN device in Sensa's Discovery picker — the same "behind a
shared integration" shape as Zigbee2MQTT (`vendors/zigbee2mqtt`), just keyed on
the WiCAN identifier prefix instead of the Z2M one.

We key on the device identifier ``("mqtt", "wican_<id>")`` (the prefix the WiCAN
firmware stamps on its discovery ``device.identifiers``) so other mqtt-discovered
devices are ignored. The ``<id>`` is the WiCAN's stable per-device id (its WiFi
MAC on real hardware; the ``02:5E:00`` sim-OUI MAC on the simulator) — stored
MAC-style (uppercase, no separators) so it round-trips through the discovery
sensor + the backend's MAC-keyed registered-device matching unchanged.

Sensa never *owns* the device: HA's mqtt integration creates and manages the HA
device + entities. So a Sensa-side removal must NOT delete the HA device
(`_native_domain_for_integration("wican")` returns None → the native-entry
removal guard no-ops). This module only reads the registry and registers a
backend row. The WiCAN is always a single, catalog-known model, so — unlike the
Zigbee path with its per-model allowlist + coordinator gate — every WiCAN device
present is addable (`service_class="obd2"`).
"""
from __future__ import annotations

import logging
from typing import Any

from ..const import INTEGRATION_WICAN

_LOGGER = logging.getLogger(__name__)

# Identifier prefix the WiCAN firmware stamps on its HA-discovery device block.
# HA stores it as ``("mqtt", "wican_<id>")``.
_WICAN_IDENTIFIER_PREFIX = "wican_"

# The OBD2 service class every WiCAN classifies to (see vendors/service_classes).
_OBD2_SERVICE_CLASS = "obd2"


def _mqtt_entry_ids(hass: Any) -> set[str]:
    try:
        return {ce.entry_id for ce in hass.config_entries.async_entries("mqtt")}
    except Exception:  # noqa: BLE001
        return set()


def _canon_id(identifier: str) -> str:
    """Normalise a WiCAN device identifier to a bare uppercase token.

    Strips the ``wican_`` prefix; uppercases so a MAC-shaped id matches the form
    the discovery sensor's `_canonical_mac` produces and compares MAC-style.
    """
    value = str(identifier or "")
    value = value.removeprefix(_WICAN_IDENTIFIER_PREFIX)
    return value.upper()


def wican_device_id(ha_dev: Any) -> str | None:
    """The canonical WiCAN id for an mqtt-owned HA device, or None.

    Returns None for any device without a ``("mqtt", "wican_…")`` identifier.
    """
    for domain, value in (getattr(ha_dev, "identifiers", None) or set()):
        if domain != "mqtt":
            continue
        text = str(value)
        if text.startswith(_WICAN_IDENTIFIER_PREFIX):
            return _canon_id(text)
    return None


def _wican_catalog_entry() -> dict[str, Any] | None:
    """The catalog entry for the WiCAN (integration ``wican``), or None."""
    try:
        from ..device_catalog import PHYSICAL_DEVICE_CATALOG
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return None
    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("integration") == INTEGRATION_WICAN:
            return entry
    return None


def discover_wican_mqtt_devices(hass: Any) -> list[dict[str, Any]]:
    """Discovery-source rows for every WiCAN OBD2 device HA has adopted.

    Reads the HA device registry (no MQTT subscription, no IO): a WiCAN already
    exists as an HA device once its MQTT discovery is on. Returns ``[]``
    gracefully when the ``mqtt`` integration / a WiCAN isn't present. Rows carry
    ``source="wican_mqtt"``, the id as ``mac_address`` + ``discovery_id``, a
    stashed ``service_class="obd2"`` (so `classify()` resolves it directly) and
    ``device_type="obd2"`` (so the backend `role` round-trips post-add).
    """
    mqtt_entries = _mqtt_entry_ids(hass)
    if not mqtt_entries:
        return []
    try:
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(hass)
    except Exception:  # noqa: BLE001
        return []

    cat_entry = _wican_catalog_entry() or {}
    catalog_image = str(cat_entry.get("image") or "").strip()
    vendor_model = str(cat_entry.get("vendor_model") or "").strip()

    rows: list[dict[str, Any]] = []
    for ha_dev in dev_reg.devices.values():
        if not mqtt_entries.intersection(ha_dev.config_entries or ()):
            continue
        wican_id = wican_device_id(ha_dev)
        if not wican_id:
            continue
        manufacturer = ha_dev.manufacturer or "MeatPi"
        model = ha_dev.model or "WiCAN Pro"
        name = (
            ha_dev.name_by_user
            or ha_dev.name
            or f"WiCAN {wican_id[-4:]}"
        )
        rows.append({
            "source": "wican_mqtt",
            "discovery_id": f"wican-{wican_id}",
            "name": name,
            "mac_address": wican_id,
            "integration": INTEGRATION_WICAN,
            # OBD2 is a single, catalog-known model — always addable when
            # present (no per-model allowlist / coordinator gate like Zigbee).
            "service_class": _OBD2_SERVICE_CLASS,
            "classified_service_class": _OBD2_SERVICE_CLASS,
            "category": "vehicle",
            "device_type": _OBD2_SERVICE_CLASS,
            # Carried for the picker's display/meta line. The WiCAN lands in the
            # *Discovered* (addable) group via the integration-keyed picker rule
            # ({"integration": ["wican"]} in dashboard.py `_build_discovery_tab`),
            # NOT a device_subtype known-model rule (that one is Zigbee-only).
            "device_subtype": vendor_model,
            "image": catalog_image,
            "vendor": manufacturer,
            "manufacturer": manufacturer,
            "model": model,
            "vendor_model": vendor_model or model,
        })
    _LOGGER.debug(
        "wican discovery: %d mqtt entry(ies), %d WiCAN device row(s)",
        len(mqtt_entries), len(rows),
    )
    return rows
