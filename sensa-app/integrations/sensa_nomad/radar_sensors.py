"""Sensa Radar nearest-amenity sensor entities.

One sensor per configured category (see ``RADAR_NEAREST_SENSORS``), e.g.
``sensor.sensa_nomad_radar_nearest_water`` and ``..._fuel``. The state is the
distance to the nearest such place; the place's name and coordinates ride along
as attributes, and ``distance_m`` (raw metres) is exposed for exact Routine
thresholds (e.g. "within 2 miles" == distance_m <= 3219).

These are thin CoordinatorEntity views over ``RadarProximityCoordinator``; the
polling and carry-forward logic all live in the coordinator.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .radar_coordinator import (
    RADAR_NEAREST_SENSORS,
    RADAR_TIER_BASE,
    RADAR_TIER_KEY,
    RADAR_TIER_MEMBER_KEY,
    RADAR_TIER_PREMIUM,
    RADAR_TIER_REASON_KEY,
    RADAR_TIER_TOKEN_KEY,
    RadarProximityCoordinator,
)


def _radar_device_info(hass: HomeAssistant | None, entry: ConfigEntry) -> dict:
    """The "Sensa Radar" device, nested under the Sensa Nomad hub device.

    Shared by every Radar entity so they all describe the same device. The parent
    is passed as `via_device_id`, never the deprecated `via_device` tuple: HA
    2026.9 raises on `via_device` whenever it cannot find an integration frame on
    the stack, and that RuntimeError silently dropped `sensor.sensa_nomad_radar_tier`
    on .207, which took the driver's Radar token (and Google Maps) with it. When
    the hub device is not registered yet the Radar device is simply top-level.
    """
    info: dict = {
        "identifiers": {(DOMAIN, f"{entry.entry_id}_radar")},
        "name": "Sensa Radar",
        "manufacturer": "Sensa",
        "model": "Radar",
    }
    if hass is not None:
        hub = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, entry.entry_id), entry.entry_id
        )
        if hub is not None:
            info["via_device_id"] = hub.id
    return info


class SensaRadarNearestSensor(
    CoordinatorEntity[RadarProximityCoordinator], SensorEntity
):
    """Distance to the nearest amenity of one category to the vehicle."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RadarProximityCoordinator,
        key: str,
        label: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_name = label
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_radar_nearest_{key}"
        self.entity_id = f"sensor.sensa_nomad_radar_nearest_{key}"

    @property
    def device_info(self) -> dict:
        # A dedicated "Sensa Radar" device nested under the Sensa Nomad hub, so all
        # Radar entities group together rather than crowding the hub device.
        return _radar_device_info(self.hass, self._entry)

    def _place(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        return data.get(self._key)

    @property
    def native_value(self) -> float | None:
        place = self._place()
        if not place:
            return None
        distance_m = place.get("distance_m")
        if distance_m is None:
            return None
        return round(float(distance_m) / 1000.0, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        place = self._place()
        if not place:
            return {"available_result": False}
        return {
            "available_result": True,
            "name": place.get("name"),
            "latitude": place.get("lat"),
            "longitude": place.get("lon"),
            # Raw metres for exact Routine thresholds ("within 2 mi" = 3219 m).
            "distance_m": place.get("distance_m"),
            "category": place.get("category"),
            "source": place.get("source"),
        }


class SensaRadarTierSensor(
    CoordinatorEntity[RadarProximityCoordinator], RestoreEntity, SensorEntity
):
    """Which Radar tier the PICKED DRIVER is on: "Premium" or "Base".

    A Nomad licence belongs to a CUSTOMER, and Radar Premium is keyed on a PERSON,
    so there is no single answer for the box: the tier depends on who the driver
    picker currently has selected. Hub decides it (personally-bought Radar licence
    first, else a seat on the customer's van licence with the support contract
    still live, else free) and the answer arrives on the van as the `is_pro` beside
    the session token. This sensor is just the view of it; the ladder lives in
    Hub's `licensing/entitlement.py` and must never be re-derived here.

    Distinct from the licence sensor's `premium_active`, which answers the
    BOX-level question the Support tab asks ("is this licence assigned to a Hub
    account?") and deliberately stays on the primary member.

    `RestoreEntity` so a restart shows the last known tier rather than flashing
    "Base" at a paying customer while the first poll runs -- the same cold-boot
    trap the read-only enforcement guards against with `control_locked`.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_name = "Radar Tier"
    _attr_icon = "mdi:crown-outline"
    # `radar_token` is a BEARER. It is a state attribute because the Cab card has
    # no other way to reach it (the exchange needs the licence, which only the box
    # holds), but it must never be written to the recorder database: it would then
    # outlive its own validity in long-term storage and ride along in every history
    # export and backup. Same mechanism the device/alarm lists here use to stay out
    # of the recorder, for a much better reason.
    _unrecorded_attributes = frozenset({"radar_token"})

    def __init__(
        self, entry: ConfigEntry, coordinator: RadarProximityCoordinator
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_radar_tier"
        self.entity_id = "sensor.sensa_nomad_radar_tier"
        self._restored: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in (RADAR_TIER_PREMIUM, RADAR_TIER_BASE):
            self._restored = last.state

    @property
    def device_info(self) -> dict:
        return _radar_device_info(self.hass, self._entry)

    @property
    def native_value(self) -> str | None:
        # None (not "Base") when nothing has answered yet AND nothing was restored:
        # "we do not know" must stay distinguishable from "you are on the free
        # tier", or an unprimed box would tell a Premium customer they are Base.
        # The card renders no badge at all for None.
        return (self.coordinator.data or {}).get(RADAR_TIER_KEY) or self._restored

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            # Which member this verdict is about, so support can tell "the wrong
            # driver is picked" from "this driver genuinely has no seat". Empty
            # means the primary member (no picker, or no pick yet).
            "member_id": data.get(RADAR_TIER_MEMBER_KEY, ""),
            # Hub's own words for a refusal (`no_seat_available`, `seat_displaced`,
            # `support_lapsed`, ...). Not shown on the Cab -- every one of them is
            # fixable only in Hub -- but it is the first thing support asks for.
            "reason": data.get(RADAR_TIER_REASON_KEY, ""),
            # The picked driver's Radar session, handed to the embedded map so it
            # runs AS THEM. Empty means anonymous, which is the correct free-tier
            # embed. See `RADAR_TIER_TOKEN_KEY`.
            "radar_token": data.get(RADAR_TIER_TOKEN_KEY, ""),
        }


def build_radar_nearest_sensors(
    entry: ConfigEntry,
    coordinator: RadarProximityCoordinator,
) -> list[SensorEntity]:
    """Build the Radar sensors: nearest-amenity per category, plus the tier."""
    sensors: list[SensorEntity] = [
        SensaRadarNearestSensor(entry, coordinator, key, label, icon)
        for key, _category, label, icon in RADAR_NEAREST_SENSORS
    ]
    sensors.append(SensaRadarTierSensor(entry, coordinator))
    return sensors
