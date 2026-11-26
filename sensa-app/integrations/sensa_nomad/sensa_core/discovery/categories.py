"""Device type to Sensa category mapping."""

from __future__ import annotations

CATEGORY_MAP: dict[str, str] = {
    # Electrical
    "battery_monitor": "electrical",
    "bms": "electrical",
    "power": "electrical",
    "inverter": "electrical",
    "charger": "electrical",
    "solar": "electrical",
    "dc_dc": "electrical",
    # Water
    "tank_sensor": "water",
    "water": "water",
    "water_tank": "water",
    "water_pump": "water",
    # Lighting
    "lighting": "lighting",
    "light": "lighting",
    # Climate
    "climate": "climate",
    "heater": "climate",
    "fridge": "climate",
    "air_conditioner": "climate",
    "fan": "climate",
    "fan_controller": "climate",
    # Switching
    "relay": "switching",
    # Connectivity
    "router": "connectivity",
    "connectivity": "connectivity",
    "wifi": "connectivity",
    "bluetooth_proxy": "connectivity",
    "ir_transceiver": "connectivity",
    # Vehicle
    "van_level": "vehicle",
    "obd2": "vehicle",
    # Tracking
    "gps": "tracking",
    "tracking": "tracking",
    # Security
    "alarm": "security",
    "lock": "security",
    "camera": "security",
}

DEFAULT_CATEGORY = "electrical"


def map_device_type_to_category(device_type: str) -> str:
    """Map a device type string to a Sensa category."""
    return CATEGORY_MAP.get(device_type.strip().lower(), DEFAULT_CATEGORY)
