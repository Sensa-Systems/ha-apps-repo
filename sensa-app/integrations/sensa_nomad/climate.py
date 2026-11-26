"""Climate platform for Sensa Nomad.

Currently serves the Velit Rooftop Air Conditioner (BLE GATT). The Velit
exposes a single 8-value "mode" field (cool/heat/fan/eco/sleep/turbo/
dehumidify/vent) which doesn't map onto HA's orthogonal hvac_mode +
preset_mode model directly, so:

  * the four modes with a core HVAC equivalent map to `hvac_mode`
    (cool→COOL, heat→HEAT, fan/vent→FAN_ONLY, dehumidify→DRY);
  * the special modes (eco/sleep/turbo/vent) surface as `preset_mode`,
    with `hvac_mode` reflecting their thermal family.

Setting a core `hvac_mode` writes that plain mode (clearing any preset);
setting a `preset_mode` writes the special mode. The device only ever
holds one mode, so the two controls are not independent — that's an
honest reflection of the hardware.

Platform wiring follows the other vendor coordinators: this module stores
its `async_add_entities` callback on the runtime dict so
`_async_start_velit_rooftop_ac` (in __init__.py) can add the entity once
its coordinator is up.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, velit_climate_entity_id

_LOGGER = logging.getLogger(__name__)

VELIT_INTEGRATION = "sensa_velit_rooftop_ac"

PRESET_NONE = "none"
_SWING_ON = "on"
_SWING_OFF = "off"

# Device mode → (core HVACMode, preset). Plain modes use PRESET_NONE.
_MODE_MAP: dict[str, tuple[HVACMode, str]] = {
    "cool": (HVACMode.COOL, PRESET_NONE),
    "heat": (HVACMode.HEAT, PRESET_NONE),
    "fan": (HVACMode.FAN_ONLY, PRESET_NONE),
    "dehumidify": (HVACMode.DRY, PRESET_NONE),
    "eco": (HVACMode.COOL, "eco"),
    "sleep": (HVACMode.COOL, "sleep"),
    "turbo": (HVACMode.COOL, "turbo"),
    "vent": (HVACMode.FAN_ONLY, "vent"),
}
# Core hvac_mode → the plain device mode it writes.
_HVAC_TO_MODE: dict[HVACMode, str] = {
    HVACMode.COOL: "cool",
    HVACMode.HEAT: "heat",
    HVACMode.FAN_ONLY: "fan",
    HVACMode.DRY: "dehumidify",
}
_PRESET_MODES = [PRESET_NONE, "eco", "sleep", "turbo", "vent"]
_FAN_MODES = ["1", "2", "3", "4", "5"]


@callback
def reclaim_velit_entity_id(
    hass: HomeAssistant, entry_id: str, device_id: str
) -> None:
    """Ensure the Velit climate entity owns its pinned entity_id.

    The entity_id was name-slugified before the pin landed (2026-06-01), so a
    Velit added on an earlier build sits at e.g. `climate.velit_rooftop_ac`
    while the dashboard's Climate-tab card references the pinned
    `velit_climate_entity_id(device_id)`. HA won't honour a `self.entity_id`
    pin for an ALREADY-registered unique_id, so without this the card reads a
    non-existent entity and renders nothing (and the device may mis-classify).
    Rename the existing registry entry to the pinned id (and drop any orphan
    squatting on it) so the card and the live entity agree. Must run BEFORE
    the entity is added. No-op on a fresh install — the pin is honoured at
    first registration. Mirrors sensor.py's _reclaim_pinned_singleton_entity_ids.
    """
    from homeassistant.helpers.entity_registry import async_get

    registry = async_get(hass)
    unique_id = f"{entry_id}_velit_rooftop_ac_{device_id}"
    desired = velit_climate_entity_id(device_id)

    holder = registry.async_get(desired)
    if holder is not None and holder.unique_id != unique_id:
        registry.async_remove(desired)
    current = registry.async_get_entity_id("climate", DOMAIN, unique_id)
    if current and current != desired and registry.async_get(desired) is None:
        _LOGGER.info(
            "Velit Rooftop AC: reclaiming pinned entity_id %s (was %s)",
            desired,
            current,
        )
        registry.async_update_entity(current, new_entity_id=desired)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad climate entities."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    # Stash the callback so the vendor startup path can add entities once
    # the Velit coordinator is created (mirrors vendor_add_sensors).
    runtime["vendor_add_climate"] = async_add_entities

    # Cover the case where a coordinator already exists at platform-setup
    # time (e.g. a reload after vendor startup): build entities for any
    # already-started Velit devices. Dedup by unique_id is handled by HA.
    coordinator = runtime.get("coordinator")
    devices = (coordinator.data or {}).get("devices", []) if coordinator else []
    entities: list[ClimateEntity] = []
    for device in devices:
        if device.get("integration_type") != VELIT_INTEGRATION:
            continue
        device_id = device.get("device_id")
        vendor_coord = runtime["vendor_coordinators"].get(device_id)
        if vendor_coord is None:
            continue
        # Reconcile a pre-pin (slugified) entity_id to the pinned one BEFORE
        # adding, so the entity binds to the renamed registry entry.
        reclaim_velit_entity_id(hass, entry.entry_id, device_id)
        entities.append(
            VelitRooftopACClimate(
                vendor_coord, entry.entry_id, device_id,
                device.get("display_name") or "Velit Rooftop AC",
                device.get("linked_mac_address") or device.get("mac_address") or "",
            )
        )
    if entities:
        async_add_entities(entities)


class VelitRooftopACClimate(CoordinatorEntity, ClimateEntity):
    """Climate entity for one Velit Rooftop AC unit."""

    _attr_has_entity_name = True
    _attr_name = None  # the device carries the name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_min_temp = 17
    _attr_max_temp = 30
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT,
        HVACMode.FAN_ONLY, HVACMode.DRY,
    ]
    _attr_preset_modes = _PRESET_MODES
    _attr_fan_modes = _FAN_MODES
    _attr_swing_modes: ClassVar[list[str]] = [_SWING_ON, _SWING_OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: Any,
        entry_id: str,
        device_id: str,
        display_name: str,
        mac: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._device_id = device_id
        self._mac = mac
        self._attr_unique_id = f"{entry_id}_velit_rooftop_ac_{device_id}"
        # Pin the entity_id deterministically (rather than letting HA slugify it
        # from the device name) so the Climate-tab card can hardcode it via
        # `velit_climate_entity_id(device_id)` — same per-device pinned-id
        # pattern as the relay / van-level entities.
        self.entity_id = velit_climate_entity_id(device_id)
        last4 = mac.replace(":", "").upper()[-4:] if mac else ""
        name = display_name or "Velit Rooftop AC"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"velit_rooftop_ac_{device_id}")},
            "name": f"{name} (#{last4})" if last4 else name,
            "manufacturer": "Velit",
            "model": "Rooftop AC",
        }

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def current_temperature(self) -> float | None:
        return self._data.get("current_temp_c")

    @property
    def target_temperature(self) -> float | None:
        return self._data.get("target_temp_c")

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self._data.get("power_on") is False:
            return HVACMode.OFF
        mode = self._data.get("mode")
        if mode is None:
            return None
        return _MODE_MAP.get(mode, (HVACMode.OFF, PRESET_NONE))[0]

    @property
    def hvac_action(self) -> HVACAction | None:
        if not self._data.get("power_on"):
            return HVACAction.OFF
        mode = self._data.get("mode")
        if mode in ("fan", "vent"):
            return HVACAction.FAN
        cur = self._data.get("current_temp_c")
        target = self._data.get("target_temp_c")
        if cur is None or target is None:
            return HVACAction.IDLE
        if mode == "heat":
            return HVACAction.HEATING if cur < target - 0.1 else HVACAction.IDLE
        # cooling family
        return HVACAction.COOLING if cur > target + 0.1 else HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        mode = self._data.get("mode")
        if mode is None:
            return None
        return _MODE_MAP.get(mode, (HVACMode.OFF, PRESET_NONE))[1]

    @property
    def fan_mode(self) -> str | None:
        speed = self._data.get("fan_speed")
        return str(speed) if speed else None

    @property
    def swing_mode(self) -> str | None:
        swing = self._data.get("swing_on")
        if swing is None:
            return None
        return _SWING_ON if swing else _SWING_OFF

    # --- commands --------------------------------------------------------

    def _guard_licence(self) -> None:
        """Block actuation when the licence is read-only (see require_licence_ok).

        Local import to avoid a module-load circular import with the package
        __init__; resolved at call time, by which point __init__ is loaded.
        """
        from . import require_licence_ok

        require_licence_ok(self.hass, self._entry_id)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._guard_licence()
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_set_power(False)
            return
        mode = _HVAC_TO_MODE.get(hvac_mode)
        if mode is None:
            return
        if not self._data.get("power_on"):
            await self.coordinator.async_set_power(True)
        await self.coordinator.async_set_mode(mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._guard_licence()
        if preset_mode == PRESET_NONE:
            # Revert the special mode to the plain core mode of its family.
            mode = self._data.get("mode")
            family = _MODE_MAP.get(mode or "cool", (HVACMode.COOL, PRESET_NONE))[0]
            await self.coordinator.async_set_mode(_HVAC_TO_MODE.get(family, "cool"))
            return
        if not self._data.get("power_on"):
            await self.coordinator.async_set_power(True)
        await self.coordinator.async_set_mode(preset_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self._guard_licence()
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            await self.coordinator.async_set_temperature(float(temp))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._guard_licence()
        try:
            await self.coordinator.async_set_fan_speed(int(fan_mode))
        except (TypeError, ValueError):
            _LOGGER.debug("Velit: bad fan_mode %r", fan_mode)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        self._guard_licence()
        await self.coordinator.async_set_swing(swing_mode == _SWING_ON)

    async def async_turn_on(self) -> None:
        self._guard_licence()
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        self._guard_licence()
        await self.coordinator.async_set_power(False)
