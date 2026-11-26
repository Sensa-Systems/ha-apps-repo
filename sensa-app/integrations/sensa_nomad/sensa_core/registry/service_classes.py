"""Catalogue device `type` -> canonical `service_class`.

Why this exists twice. The integration owns the device catalogue and its own copy
of this table (`sensa_nomad/vendors/service_classes.py`); the App can't import it
(they are separate deployables — an HA custom component and an add-on container),
and the App is where device entitlement is ENFORCED. So the enforcement side needs
its own copy.

The mismatch this guards is not hypothetical: the Teltonika managed switch has
catalogue type ``switch`` but service class ``network_switch``, and the add path
sends the TYPE as ``role``. A gate keyed on the raw role would compare "switch"
against a policy that blocks "network_switch" and wave the device straight
through — the paid device would be free, silently.

`nomad/tests/test_service_class_alias_drift.py` pins this table against the
integration's, so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

# Only the entries where type != service_class actually matter to the gate, but
# the full table is mirrored so the drift test can compare like for like.
CATALOG_TYPE_TO_SERVICE_CLASS: dict[str, str] = {
    "relay": "relay",
    "bluetooth_proxy": "bluetooth_proxy",
    "router": "router",
    "switch": "network_switch",
    "satellite_internet": "satellite_internet",
    "nas": "nas",
    "zigbee_coordinator": "zigbee_coordinator",
    "environment_sensor": "environment_sensor",
    "battery_monitor": "battery_monitor",
    "solar_charger": "solar_charger",
    "dc_dc_converter": "dc_dc_charger",
    "battery_protect": "battery_protect",
    "gx_gateway": "gx_gateway",
    "leisure_battery": "battery",
    "air_heater": "air_heater",
    "diesel_heater": "air_heater",  # legacy: rows persisted before the 2026-07-08 rename.
    "van_level": "levelling",
}


def service_class_for(role: str | None) -> str:
    """Canonical service class for a catalogue type / registry ``role``.

    Unknown values pass through unchanged: most catalogue types ARE their service
    class (``obd2``, ``speaker``, ``air_conditioner``), so identity is the right
    default and a new device is gateable the day it is added, without an entry
    here.
    """
    key = str(role or "").strip().lower()
    return CATALOG_TYPE_TO_SERVICE_CLASS.get(key, key)
