"""Geofence: how far the van has moved since Sentry went on.

`sensor.sensa_nomad_geofence_distance` is the whole mechanism. When Sentry
mode turns on it records where the van is (the anchor); from then on its state
is the distance in metres from that anchor. When Sentry turns off the anchor is
dropped and the state goes unknown. The customer's circle size lives on the
Geofence instinct, which alarms when this reading goes above it.

Only a GPS bolted to the van counts (`resolve_van_position`): in Sentry the
owner is away with their phone, so a phone fix would read as the van leaving.

Losing the fix is "could not ask", not "still parked": the last distance is
carried forward, so an alarm already raised stays raised and none is invented.
The anchor survives a restart through the restored state, so a van towed while
the box was off still reads as moved when it boots.

Nothing here egresses a position. The anchor and the van's latest fix are
local entity attributes, and the alarm names a distance, not a place. The
Security tab map (`sensa-geofence-card`) does: it embeds Sensa Radar's
`/geofence` page with the van's position, as the Cab's Radar map does.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import Event, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import DOMAIN, GEOFENCE_DISTANCE_ENTITY_ID
from .modes_catalog import mode_active_entity_id

SENTRY_MODE_SLUG = "sentinel"
# How often the van's position is re-read while in Sentry. A thief doing
# 30 mph covers about 400 m in this time, inside the smallest useful circle
# plus one tick.
_SAMPLE_INTERVAL = timedelta(seconds=30)


class SensaGeofenceSensor(RestoreSensor):
    """Metres from the Sentry anchor. See the module docstring."""

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Geofence distance"
    _attr_icon = "mdi:map-marker-radius"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_suggested_display_precision = 0
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_geofence_distance"
        self.entity_id = GEOFENCE_DISTANCE_ENTITY_ID
        self._anchor: tuple[float, float] | None = None
        # The van's latest fix, in or out of Sentry, for the Security tab map.
        self._position: tuple[float, float] | None = None
        self._attr_native_value = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "anchor_latitude": self._anchor[0] if self._anchor else None,
            "anchor_longitude": self._anchor[1] if self._anchor else None,
            "van_latitude": self._position[0] if self._position else None,
            "van_longitude": self._position[1] if self._position else None,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        last_data = await self.async_get_last_sensor_data()
        if last_state is not None:
            try:
                self._anchor = (
                    float(last_state.attributes["anchor_latitude"]),
                    float(last_state.attributes["anchor_longitude"]),
                )
            except (KeyError, TypeError, ValueError):
                self._anchor = None
            try:
                self._position = (
                    float(last_state.attributes["van_latitude"]),
                    float(last_state.attributes["van_longitude"]),
                )
            except (KeyError, TypeError, ValueError):
                self._position = None
        if self._anchor is not None and last_data is not None:
            self._attr_native_value = last_data.native_value

        # Both `@callback`: an undecorated sync callable is run as an executor
        # job, off the loop, where writing state is not allowed.
        @callback
        def _on_mode(_event: Event) -> None:
            self._refresh()

        @callback
        def _on_tick(_now: Any) -> None:
            self._refresh()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [mode_active_entity_id(SENTRY_MODE_SLUG)], _on_mode
            )
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, _on_tick, _SAMPLE_INTERVAL)
        )
        self._refresh()

    @callback
    def _refresh(self) -> None:
        if self.hass is None:
            return
        from .device_tracker import resolve_van_position
        from .radar_coordinator import _haversine_m

        pos = resolve_van_position(self.hass)
        if pos is not None:
            self._position = pos
        state = self.hass.states.get(mode_active_entity_id(SENTRY_MODE_SLUG))
        sentry = getattr(state, "state", None)
        if sentry == "off":
            self._anchor = None
            self._attr_native_value = None
        elif sentry == "on" and pos is not None:
            if self._anchor is None:
                self._anchor = pos
            self._attr_native_value = round(
                _haversine_m(self._anchor[0], self._anchor[1], pos[0], pos[1])
            )
        # Otherwise the mode sensor has not answered (cold boot), or there is
        # no fix: the anchor and distance are carried forward unchanged.
        self.async_write_ha_state()
