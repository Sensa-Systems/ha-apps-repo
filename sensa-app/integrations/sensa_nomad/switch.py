"""Switch platform for Sensa Nomad — router config toggles."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import diagnostics_report
from .const import (
    SOLAR_ARRAY_RATING_ENTITY_ID,
    SOLAR_SWITCH_ENTITY_ID,
    WEATHER_SWITCH_ENTITY_ID,
    CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID,
    CRYPTO_HEATER_OFF_SWITCH_ENTITY_ID,
    DEPLOYMENT_MODE_APPLIANCE,
    DEPLOYMENT_MODE_KEY,
    DOMAIN,
    EVENT_SENSA_REMOTE_ACCESS_CHANGED,
    HEADER_POWER_CHIP_ICON,
    HEADER_POWER_CHIP_OPTION,
    HEADER_POWER_CHIP_UID_SUFFIX,
    HEADER_TEMP_CHIP_FALLBACK_NAME,
    HEADER_TEMP_CHIP_ICON,
    HEADER_TEMP_CHIP_OPTION,
    HEADER_TEMP_CHIP_UID_SUFFIX,
    REMOTE_ACCESS_SWITCH_ENTITY_ID,
    RUNTIME_OTA_AUTO_UPDATE,
    RUNTIME_TELTONIKA_AUTO_UPDATE,
    RUNTIME_TELTONIKA_SWITCH_AUTO_UPDATE,
    RUNTIME_VICTRON_AUTO_UPDATE,
    SIGNAL_DASHBOARD_REGEN_REQUESTED,
    TELTONIKA_AUTO_UPDATE_ENTITY_ID,
    TELTONIKA_DEFAULT_SSL,
    TELTONIKA_SWITCH_AUTO_UPDATE_ENTITY_ID,
    TEMPERATURE_FLAG_FALLBACK_NAME,
    TEMPERATURE_FLAGS,
    TEMPERATURE_WATCH_FLAG,
    VICTRON_AUTO_UPDATE_ENTITY_ID,
    TemperatureFlag,
    header_power_chip_eligible,
    header_power_chip_enabled,
    header_power_chip_fallback_name,
    header_power_chip_switch_entity_id,
    header_temp_chip_eligible,
    header_temp_chip_enabled,
    header_temp_chip_switch_entity_id,
    nas_power_switch_entity_id,
    relay_channel_enabled_entity_id,
    teltonika_device_name,
    teltonika_devices_from_registry,
)
from .vendors.service_classes import device_kind

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad switch entities.

    Stashes the add callback so Teltonika devices registered post-setup
    (via the Bluetooth / network picker) get their SSL switch created
    dynamically through `_async_add_teltonika_platform_entities` — same
    pattern as the text and button platforms.
    """
    runtime = hass.data[DOMAIN][entry.entry_id]
    runtime["vendor_add_switches"] = async_add_entities

    # Flush any vendor switches a coordinator queued before this platform
    # registered its callback. `sensor` / `binary_sensor` / `select` have had
    # this drain since the Cerbo silent-drop fix; `switch` never did, so
    # `_add_or_queue_vendor_entities(runtime, "switches", ...)` would stash
    # into `vendor_pending_switches` and NOTHING would ever read it -- the
    # entities would vanish until a full HA restart happened to win the race.
    # Nothing used that path before the Teltonika per-port PoE switches, which
    # is why it went unnoticed.
    pending_switches = runtime.pop("vendor_pending_switches", None)
    if pending_switches:
        _LOGGER.info(
            "Sensa Nomad switch setup: flushing %d vendor switch(es) queued "
            "before the platform was ready",
            len(pending_switches),
        )
        async_add_entities(pending_switches)

    devices = runtime.get("coordinator").data or {}
    device_list = devices.get("devices", [])

    _LOGGER.debug(
        "switch.py setup: coordinator.data has %d devices, "
        "entry.options keys=%s",
        len(device_list),
        sorted((entry.options or {}).keys()),
    )

    # Diagnostic reporting consent -- five per-category toggles (Analytics &
    # Diagnostics on Settings -> Admin), all OFF by default, offered on EVERY
    # flavour (HAOS / Container / BYO). Each is a thin consent store; the shared
    # DiagnosticsCoordinator owns the durable push machinery. Drop the pre-split
    # single switch from the registry so it doesn't linger as unavailable.
    diagnostics_coordinator = diagnostics_report.get_coordinator(hass, entry)
    entry.async_on_unload(diagnostics_coordinator.async_shutdown)
    _async_remove_legacy_diagnostics_entity(hass, entry)
    _async_remove_legacy_followme_switch(hass, entry)

    entities = [
        SensaDebugSwitch(entry),
        SensaNerdModeSwitch(entry),
        SensaWeatherForecastSwitch(entry),
        SensaSolarForecastSwitch(entry),
        SensaExperimentalDevicesSwitch(entry),
        SensaSchengenTrackingSwitch(hass, entry),
        SensaTrackingSwitch(hass, entry),
        SensaTrackingPhonesSwitch(hass, entry),
        SensaCryptoAutopilotSwitch(entry),
        SensaCryptoHeaterOffSwitch(entry),
        SensaClimateControlSwitch(entry),
        # Third-party device firmware (Layer 4): the customer's pause switches
        # for the Victron GX, the Teltonika router and the Teltonika managed
        # switch. Unconditional, unlike the
        # OTA/host toggles below, because the integration drives those installs
        # on every deployment mode. The System-panel rows are gated on the
        # matching device being registered, so a van with no Victron never sees
        # a Victron control; the ENTITY always exists so the install gate always
        # has something to read.
        SensaVictronAutoUpdateSwitch(hass, entry),
        SensaTeltonikaAutoUpdateSwitch(hass, entry),
        SensaTeltonikaSwitchAutoUpdateSwitch(hass, entry),
        *(
            SensaDiagnosticCategorySwitch(hass, entry, category)
            for category in diagnostics_report.CATEGORIES
        ),
    ]

    # A fan's Auto switch for every Sensa Fan Temperature Control; a controller
    # already in the autopilot ledger keeps its switches, as the selects do.
    from .const import FAN_AUTOPILOT_OPTION, FAN_CHANNELS

    fan_ids: set[str] = set()
    for device in device_list:
        for fan_switch in build_fan_auto_switches(entry, device):
            fan_ids.add(fan_switch._device_id)
            entities.append(fan_switch)
    for device_id in entry.options.get(FAN_AUTOPILOT_OPTION) or {}:
        if device_id and device_id not in fan_ids:
            entities.extend(
                SensaFanAutoSwitch(entry, device_id, "Fan Temperature Control", ch)
                for ch in FAN_CHANNELS
            )

    # Remote Support consent toggle — only offered where the pre-provisioned
    # `sensa_support` account exists (appliance images), never on a BYO
    # customer's own HA where there's nothing to activate.
    from . import remote_support
    if await remote_support.async_support_user_exists(hass):
        entities.append(SensaRemoteSupportSwitch(hass, entry))

    # OTA controls are CONTAINER-appliance only. On HAOS the `admin` login is a
    # full HA administrator and updates the add-on + HA Core via Supervisor's own
    # OTA (Settings → System → Updates), so a Sensa software-update toggle would
    # be redundant + misleading — and Sensa does NOT auto-apply on HAOS either
    # (see _async_maybe_apply_ota). On Container there's no Supervisor, so Sensa
    # drives the whole-stack OTA. Both the software-update toggle and the
    # Debian-`apt` Host-OS toggle are therefore gated to Container. (BYO owners
    # update the add-on through HA themselves, so neither applies there.)
    if hass.data.get(DOMAIN, {}).get(DEPLOYMENT_MODE_KEY) == DEPLOYMENT_MODE_APPLIANCE:
        from .appliance import is_supervised

        if not is_supervised(hass):
            entities.append(SensaOtaAutoUpdateSwitch(hass, entry))
            entities.append(SensaHostAutoUpdateSwitch(hass, entry))
            # Remote access is a Container-appliance entitlement, and the same
            # gate the cloud uses to decide who gets a tunnel at all
            # (`is_container_appliance`). Offering the switch anywhere else would
            # be a control over something that cannot happen.
            entities.append(SensaRemoteAccessSwitch(hass, entry))
            # Same scope as the Settings PIN itself (settings_pin._pin_scope in
            # __init__.py) — a box with no Container Settings tab has nowhere to
            # flip this back on.
            entities.append(SensaAdminLockSwitch(hass, entry))

    # Build the set of relay device_ids to register entities for.
    # Sources:
    #   1. coordinator.data — current backend snapshot (transient;
    #      empty when backend is unreachable on HA restart).
    #   2. HA entity registry — persisted unique_ids from any prior
    #      session. The customer's saved mode-overrides reference
    #      these entity_ids; if we don't register entities for them
    #      this turn, scene activation logs "missing or not currently
    #      available" and the dashboard toggle is unresponsive.
    # Backend `role`/`device_type`/`vendor_device_type`/`type` are all
    # checked because the field name differs by registration path.
    relay_devices: dict[str, dict[str, Any]] = {}
    teltonika_seen: set[str] = set()
    for device in device_list:
        integration = device.get("integration_type")
        vdt = device_kind(device)
        if integration == "sensa_teltonika_api":
            entities.extend(build_teltonika_switch_entities(entry, device))
            teltonika_seen.add(str(device.get("device_id") or ""))
        elif integration == "sensa_victron_mqtt":
            entities.extend(build_cerbo_switch_entities(entry, device))
        elif integration == "sensa_asustor_ssh":
            entities.extend(build_asustor_switch_entities(hass, entry, device))
        elif vdt == "relay":
            device_id = str(device.get("device_id") or device.get("id") or "")
            if device_id:
                relay_devices[device_id] = device

    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        entry_prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id:
                continue
            uid = ent.unique_id or ""
            if not uid.startswith(entry_prefix) or not uid.endswith("_enabled"):
                continue
            tail = uid[len(entry_prefix):-len("_enabled")]
            if "_relay_" not in tail:
                continue
            device_id = tail.rsplit("_relay_", 1)[0]
            if not device_id or device_id in relay_devices:
                continue
            relay_devices[device_id] = {
                "device_id": device_id,
                "display_name": "Relay",
                "vendor_device_type": "relay",
            }
    except Exception:
        _LOGGER.warning(
            "Registry walk for relay device_ids failed", exc_info=True,
        )

    # Third source: entry.options. `relay_initialized` is the durable
    # ledger of every relay device the customer has ever added through
    # this config entry — it's written on first add by
    # `_apply_relay_first_add_defaults` and never removed for the
    # device's lifetime. `relay_enabled` keys mirror the same set.
    # This survives backend resets, HACS cache wipes, and entity
    # registry purges, which the prior two sources don't.
    persisted_ids: set[str] = set(entry.options.get("relay_initialized") or [])
    persisted_ids.update((entry.options.get("relay_enabled") or {}).keys())
    for device_id in persisted_ids:
        if not device_id or device_id in relay_devices:
            continue
        relay_devices[device_id] = {
            "device_id": device_id,
            "display_name": "Relay",
            "vendor_device_type": "relay",
        }

    _LOGGER.debug(
        "switch.py setup: relay_devices to register = %s",
        sorted(relay_devices.keys()),
    )
    for device_id, device in relay_devices.items():
        entities.extend(build_relay_enabled_switches(hass, entry, device))
        _LOGGER.info(
            "Registering 8 relay enabled switches for %s",
            device_id,
        )

    # NOTE: the "Home Layout" placement control moved from a boolean switch here
    # to a per-device select (Off / Left / Middle / Right) built in select.py.
    # See `_build_home_placement_selects` there.

    # "Show Power In Header" — one switch per eligible device (Starlink today).
    entities.extend(
        _build_header_power_chip_switches(hass, entry, device_list)
    )

    # "Show Temperature In Header" — one switch per environment device.
    entities.extend(
        _build_header_temp_chip_switches(hass, entry, device_list)
    )

    # Temperature Watch / Fridge / Freezer: one switch per flag per
    # environment device, each feeding its own instinct (no slot limit; each
    # instinct discovers every device carrying its flag).
    entities.extend(
        _build_temperature_watch_switches(hass, entry, device_list)
    )

    # Re-create a still-registered router's SSL switch from the registry when
    # the backend snapshot is momentarily empty (add-on starting after an OTA),
    # so it doesn't become an orphan alongside the credential texts. See the
    # relay union above and `teltonika_devices_from_registry`.
    for device_id, device in teltonika_devices_from_registry(hass, entry).items():
        if device_id in teltonika_seen:
            continue
        entities.extend(build_teltonika_switch_entities(entry, device))

    # Zigbee lighting controllers: "Leave Relay On" on their Devices row,
    # paired with the Power-On Delay number from the same candidate set.
    entities.extend(build_light_leave_relay_on_switches(hass, entry))

    async_add_entities(entities)

    # One automatic-updates switch per Sensa ESP device, added and removed with
    # the device, the same tracking the firmware entities use.
    from .esp_update import track_esp_devices

    track_esp_devices(
        hass, entry, runtime, "switch",
        lambda _coordinator, device_id, name: SensaEspAutoUpdateSwitch(
            hass, entry, device_id, name,
        ),
        async_add_entities,
    )
    _remove_shared_esp_auto_update_switch(hass, entry)


def _remove_shared_esp_auto_update_switch(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the one-for-all Sensa device switch that per-device switches replaced.

    A RestoreEntity class that is deleted leaves its registry row behind as an
    unavailable entity, so the row is removed by its unique_id.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "switch", DOMAIN, f"{entry.entry_id}_sensa_nomad_firmware_auto_update",
        )
        if entity_id:
            ent_reg.async_remove(entity_id)
    except Exception:
        _LOGGER.debug("Could not remove the shared Sensa device switch", exc_info=True)


class SensaDebugSwitch(RestoreEntity, SwitchEntity):
    """Integration-level debug toggle. Off by default; gates the 'Others'
    group in the Discovery picker so unrecognised devices are hidden in
    normal use and visible when diagnosing a new device type."""

    _attr_has_entity_name = True
    _attr_name = "Debug Mode"
    _attr_icon = "mdi:bug-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_debug"

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaNerdModeSwitch(RestoreEntity, SwitchEntity):
    """Box-wide Nerd Mode. Off by default; cards show their technical detail
    (voltages, pump rates, sensor internals) only when it is on, so the
    everyday view stays simple. Toggled from the glasses button in the shared
    header; cards read it through `webcards/src/shared/nerd-mode.ts`.

    One switch for the whole box, not one per card: a toggle on every card
    would add the very clutter this exists to remove."""

    _attr_has_entity_name = True
    _attr_name = "Nerd Mode"
    _attr_icon = "mdi:glasses"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_nerd_mode"

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaWeatherForecastSwitch(RestoreEntity, SwitchEntity):
    """The owner's consent to the weather forecast (weather_forecast.py).

    Off by default: while it is off nothing is fetched, so no position leaves
    the van for a card the owner is not using. The Weather card offers it, with
    what is sent, the first time it is shown. Turning it on fetches at once;
    turning it off clears the forecast."""

    _attr_has_entity_name = True
    _attr_name = "Weather Forecast"
    _attr_icon = "mdi:weather-partly-cloudy"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_weather_forecast"
        self.entity_id = WEATHER_SWITCH_ENTITY_ID

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        if self._attr_is_on:
            # The sensor platform's first refresh can run before this switch
            # is restored, and would read "off" until the next 30 min poll.
            self.hass.async_create_task(self._refresh())

    async def _refresh(self) -> None:
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        coordinator = runtime.get("weather_coordinator")
        if coordinator is not None:
            await coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._refresh()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaSolarForecastSwitch(RestoreEntity, SwitchEntity):
    """The owner's on/off for the solar forecast (solar_forecast.py).

    Off by default, like the weather forecast: while it is off nothing is
    fetched, whatever panel rating is set. A box upgraded from before this
    switch existed starts on if a rating was already entered, since a rating
    above 0 was the old way of turning it on."""

    _attr_has_entity_name = True
    _attr_name = "Solar Forecast"
    _attr_icon = "mdi:solar-power-variant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_solar_forecast"
        self.entity_id = SOLAR_SWITCH_ENTITY_ID

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        else:
            self._attr_is_on = self._legacy_rating_set()
        if self._attr_is_on:
            self.hass.async_create_task(self._refresh())

    def _legacy_rating_set(self) -> bool:
        from homeassistant.helpers.restore_state import async_get as restore_data

        try:
            stored = restore_data(self.hass).last_states.get(
                SOLAR_ARRAY_RATING_ENTITY_ID
            )
            return stored is not None and float(stored.state.state) > 0
        except Exception:  # noqa: BLE001 - no history is simply "not set"
            return False

    async def _refresh(self) -> None:
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        coordinator = runtime.get("solar_coordinator")
        if coordinator is not None:
            await coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._refresh()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaClimateControlSwitch(SwitchEntity):
    """Sensa Climate Control on / off: the power toggle on its Climate-tab
    card. Options-as-truth on the entry (`CLIMATE_CONTROL_OPTION`), so not a
    RestoreEntity. Off stops whichever unit it was running (`climate_control`).
    """

    _attr_has_entity_name = False
    _attr_name = "Climate Control"
    _attr_icon = "mdi:thermostat"

    def __init__(self, entry: ConfigEntry) -> None:
        from .const import CLIMATE_CONTROL_SWITCH_ENTITY_ID

        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_climate_control"
        self.entity_id = CLIMATE_CONTROL_SWITCH_ENTITY_ID

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def is_on(self) -> bool:
        from .climate_control import setting

        return bool(setting(self._entry, "enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        from .climate_control import async_set_enabled

        await async_set_enabled(self.hass, self._entry, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        from .climate_control import async_set_enabled

        await async_set_enabled(self.hass, self._entry, False)
        self.async_write_ha_state()


class SensaExperimentalDevicesSwitch(RestoreEntity, SwitchEntity):
    """Enables catalog entries flagged `experimental: true` in discovery.

    OFF by default, so an experimental device (the Autoterm / Alde / Dometic
    heater + AC controllers) is invisible on a customer's box even though its
    catalog entry, card and dashboard wiring all ship in the release. A dev rig
    or the simulator turns this on to discover, add and drive them.

    Read server-side by `config_flow.experimental_devices_enabled(hass)`, which
    fails CLOSED when this entity is missing — so a discovery pass that runs
    before the switch registers can never leak an experimental device.

    Distinct from `SensaDebugSwitch`: that reveals the *Others* group (rows we
    could not classify at all); this admits a *known* device we deliberately
    hide. Nothing at runtime can enable a `discovery_enabled: false` entry —
    that gate stays hard.
    """

    _attr_has_entity_name = True
    _attr_name = "Experimental Devices"
    _attr_icon = "mdi:flask-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_experimental_devices"

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaCryptoAutopilotSwitch(RestoreEntity, SwitchEntity):
    """Master switch for the miner autopilot (`crypto_autopilot.py`), OFF by
    default. A plain store: the engine watches this entity's state and
    evaluates on every flip, and switches it off itself when the customer
    cuts a miner's power by hand. Always present so the Crypto card's
    Controls panel never binds to a dangling id."""

    _attr_has_entity_name = False
    _attr_name = "Miner Autopilot"
    _attr_icon = "mdi:robot"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_crypto_autopilot"
        self.entity_id = CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }


class SensaCryptoHeaterOffSwitch(SensaCryptoAutopilotSwitch):
    """Miners replace the heater: while the autopilot has a miner running,
    the diesel heater is switched off and put back when the last one stops.
    ON by default, since the miners' heat is the point. Only a heater the
    autopilot itself turned off is ever turned back on."""

    _attr_name = "Miners Replace Heater"
    _attr_icon = "mdi:radiator-off"
    _attr_is_on = True

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_crypto_heater_off"
        self.entity_id = CRYPTO_HEATER_OFF_SWITCH_ENTITY_ID


class SensaOtaAutoUpdateSwitch(RestoreEntity, SwitchEntity):
    """Owner toggle for appliance OTA auto-update — ON by default.

    When ON, the integration auto-applies the version this device's rollout group
    is assigned (or the channel repo's latest), taking a pinned pre-OTA snapshot
    first. When OFF, updates are deferred (the owner still sees "update available"
    and can apply on demand with the Update now button). Appliance-only — a
    fresh appliance ships updating itself; the owner can pause for a trip.

    The on/off state is mirrored into ``runtime[RUNTIME_OTA_AUTO_UPDATE]`` so the
    backup poller gates the apply without resolving an entity_id; the poller
    fails OPEN to ON when the flag is missing (a fresh boot before restore)."""

    _attr_has_entity_name = True
    _attr_name = "Automatic updates"
    _attr_icon = "mdi:update"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_ota_auto_update"
        self.entity_id = "switch.sensa_nomad_ota_auto_update"

    def _publish(self) -> None:
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if isinstance(runtime, dict):
            runtime[RUNTIME_OTA_AUTO_UPDATE] = self._attr_is_on

    async def _push(self) -> None:
        """Report the toggle to the App so it rides the next phone-home as
        telemetry.updates.sensa_auto (the Hub fleet view). Mirrors the host
        switch's _push: state is HA-durable, this push is best-effort."""
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        client = runtime.get("client") if isinstance(runtime, dict) else None
        if client is None:
            return
        try:
            await client.async_post_ota_config(auto=self._attr_is_on)
        except Exception:
            _LOGGER.debug("Sensa OTA auto-update push failed", exc_info=True)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        self._publish()
        # Re-assert to the App on setup: its stored value may be stale (older App,
        # wiped /data), so the Hub reflects the switch's real position.
        await self._push()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._publish()
        self.async_write_ha_state()
        await self._push()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._publish()
        self.async_write_ha_state()
        await self._push()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class _SensaDeviceFirmwareAutoUpdateSwitch(RestoreEntity, SwitchEntity):
    """Base for the third-party device-firmware pause toggles — ON by default.

    Layer 4, and a different machine from the box: these govern whether Sensa
    installs firmware on kit the customer owns but Sensa did not write (the
    Victron GX, the Teltonika router). ON means the rollout group's ceiling is
    honoured automatically; OFF means "paused", exactly as the Sensa OTA toggle
    does — the update entity still reports what is available and the Update
    button still works, so a customer who wants it now can have it now.

    NOT deployment-gated, unlike the OTA and host-OS toggles. Those are Container
    -appliance-only because on HAOS or BYO the Sensa stack is updated by someone
    else; this firmware is installed by the integration on every install type, so
    a toggle that only existed on appliances would leave most boxes with an
    automation they cannot pause.

    The position is mirrored into the runtime under `runtime_key` so an update
    entity reads it without an entity lookup, and a missing flag reads as ON.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:update"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = True
    runtime_key: str = ""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry

    def _publish(self) -> None:
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if isinstance(runtime, dict):
            runtime[self.runtime_key] = self._attr_is_on

    async def _push(self) -> None:
        """Re-send the firmware report so the Hub sees the flip now; the App
        phones home when a row's switch changed. Best-effort, like the Sensa
        and OS switches: the 15-minute heartbeat re-asserts it anyway."""
        from .firmware_report import async_push

        await async_push(self.hass, self._entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        self._publish()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._publish()
        self.async_write_ha_state()
        await self._push()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._publish()
        self.async_write_ha_state()
        await self._push()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaVictronAutoUpdateSwitch(_SensaDeviceFirmwareAutoUpdateSwitch):
    """Pause Sensa-driven Venus OS updates on this van's Victron GX."""

    _attr_name = "Automatic Victron updates"
    runtime_key = RUNTIME_VICTRON_AUTO_UPDATE

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_victron_auto_update"
        self.entity_id = VICTRON_AUTO_UPDATE_ENTITY_ID


class SensaTeltonikaAutoUpdateSwitch(_SensaDeviceFirmwareAutoUpdateSwitch):
    """Pause Sensa-driven RutOS updates on this van's Teltonika ROUTER.

    Routers only. The managed switches have their own toggle below, because
    holding the box's internet and holding a switch nobody depends on
    mid-journey are different decisions, and a shared toggle meant a customer
    who wanted one got both. The split is the rollout pins' split
    (PIN_KEY_ROUTER / PIN_KEY_SWITCH), not a second one.
    """

    _attr_name = "Automatic router updates"
    runtime_key = RUNTIME_TELTONIKA_AUTO_UPDATE

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_teltonika_auto_update"
        self.entity_id = TELTONIKA_AUTO_UPDATE_ENTITY_ID


class SensaTeltonikaSwitchAutoUpdateSwitch(_SensaDeviceFirmwareAutoUpdateSwitch):
    """Pause Sensa-driven firmware updates on this van's Teltonika managed switches.

    One toggle for the switches, however many are fitted: the rollout ceiling
    that decides what they may take is one pin for the whole kind, so the pause
    that holds them is one toggle for the whole kind.
    """

    _attr_name = "Automatic network switch updates"
    runtime_key = RUNTIME_TELTONIKA_SWITCH_AUTO_UPDATE

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = (
            f"{entry.entry_id}_sensa_nomad_teltonika_switch_auto_update"
        )
        self.entity_id = TELTONIKA_SWITCH_AUTO_UPDATE_ENTITY_ID


class SensaEspAutoUpdateSwitch(_SensaDeviceFirmwareAutoUpdateSwitch):
    """Pause automatic firmware updates on ONE Sensa ESP device.

    One per device, so a relay controller can be held while a proxy keeps
    current. Created and removed with the device (`esp_update.track_esp_devices`).
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device_id: str, name: str,
    ) -> None:
        from .esp_update import esp_auto_update_entity_id, esp_auto_update_key

        super().__init__(hass, entry)
        self.runtime_key = esp_auto_update_key(device_id)
        self._attr_name = f"Automatic {name} updates"
        self._attr_unique_id = f"{entry.entry_id}_sensa_esp_{device_id}_auto_update"
        self.entity_id = esp_auto_update_entity_id(device_id)


class SensaHostAutoUpdateSwitch(RestoreEntity, SwitchEntity):
    """Customer toggle for automatic host-OS (Debian apt) updates — ON by default.

    Appliance-only, Layer 1. ON/OFF is POSTed to the App
    (POST /api/v1/host-update/config), which persists it to the on-host override
    file the systemd apt-update agent honours — that file is the durable truth, so
    this is a RestoreEntity purely to show the right position after a restart. Not
    licence-gated: host security patching must keep working in read-only. Distinct
    from switch.sensa_nomad_ota_auto_update (Layer 3, the Sensa-stack OTA)."""

    _attr_has_entity_name = True
    _attr_name = "Automatic OS updates"
    _attr_icon = "mdi:harddisk"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_host_auto_update"
        self.entity_id = "switch.sensa_nomad_host_auto_update"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        # Re-assert on setup, exactly as SensaOtaAutoUpdateSwitch does. Without
        # this the override file only exists once the customer has TOUCHED the
        # toggle, so telemetry.updates.os_auto stays None and the Hub fleet view
        # reads "OS ?" on every box that simply left the default alone.
        await self._push()

    async def _push(self) -> None:
        """Persist the toggle to the host override via the App."""
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        client = runtime.get("client") if isinstance(runtime, dict) else None
        if client is None:
            return
        try:
            await client.async_post_host_update_config(auto=self._attr_is_on)
        except Exception:
            _LOGGER.debug("Host auto-update push failed", exc_info=True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._push()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._push()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaAdminLockSwitch(SwitchEntity):
    """Admin Lock — whether Settings asks for the Settings PIN at all.

    OFF by default: a solo traveller or a couple never left alone in the van
    doesn't want to keep entering a PIN, so a fresh box opens Settings to
    every login. Turning this ON is what makes the PIN box on this same
    Passwords panel start being asked for.

    Not a RestoreEntity — `settings_pin` already persists the flag in its own
    PBKDF2 store (the one place the PIN's own on/off-default and elevation
    state live), so `is_on` reads it straight rather than keeping a second
    copy that could drift from it (see CLAUDE.md's "one predicate written
    twice" rule). `async_ensure_pin` loads that store before platforms are
    forwarded, so the value is already live by the time this entity reads it.
    """

    _attr_has_entity_name = True
    _attr_name = "Admin Lock"
    _attr_icon = "mdi:lock-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_admin_lock"
        self.entity_id = "switch.sensa_nomad_admin_lock"

    @property
    def is_on(self) -> bool:
        from . import settings_pin

        return settings_pin.pin_enabled(self.hass)

    async def async_turn_on(self, **kwargs: Any) -> None:
        from . import settings_pin

        await settings_pin.async_set_pin_enabled(self.hass, True)
        # Elevate the customer who just switched this ON. Without this, the
        # PIN gate applies to them the instant this write lands, and every
        # OTHER kiosk login stopped being a Sensa admin the same instant (the
        # "not pin_enabled" bypass in appliance.user_is_sensa_admin), so the
        # very person setting the PIN up would be refused `set_settings_pin`
        # by their own change before they ever get a chance to type it (an
        # "Unauthorized" report, not a working feature). They are already
        # standing in Settings having just pressed this toggle, which is
        # exactly the authority a PIN elevation represents, so this grants
        # the same standard idle window a correct PIN would.
        context = self._context
        settings_pin.elevate(self.hass, context.user_id if context else None)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        from . import settings_pin

        await settings_pin.async_set_pin_enabled(self.hass, False)
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


# Per-category consent switch metadata (customer-facing name + icon), keyed by the
# category from diagnostics_report.CATEGORIES.
_DIAG_CATEGORY_META: dict[str, tuple[str, str]] = {
    "devices": ("Device inventory", "mdi:devices"),
    "unavailable": ("Unavailable devices", "mdi:link-variant-off"),
    "errors": ("Error logs", "mdi:alert-circle-outline"),
    "resources": ("Resource usage", "mdi:chip"),
    "uptime": ("Uptime", "mdi:timer-outline"),
    "solar": ("Solar forecast accuracy", "mdi:solar-power-variant"),
}


@callback
def _async_remove_legacy_diagnostics_entity(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove the pre-2026-07 single diagnostics switch from the registry.

    The consent split replaced switch.sensa_nomad_diagnostics with five
    switch.sensa_nomad_diag_<category> entities; the old unique_id would otherwise
    linger as a restored/unavailable entity. A unit that had the old switch ON
    comes up with all five new toggles OFF (the new unique_ids have no restore
    state) — a deliberate, privacy-safe reset to the OFF default under the new
    finer-grained consent model; the customer re-picks exactly what to share."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_sensa_nomad_diagnostics"
    entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
    if entity_id:
        registry.async_remove(entity_id)


@callback
def _async_remove_legacy_followme_switch(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove the pre-2026-07-20 FollowMe switch from the registry.

    The feature was renamed to Tracking: switch.sensa_nomad_followme (unique_id
    ..._sensa_nomad_followme) became switch.sensa_nomad_tracking. The old unique_id
    would otherwise linger as an unavailable entity. The on/off preference is held
    in the store (keyed by the unchanged SENSA_TRACKING_PATH), so the customer's
    setting survives; only the registry entry is retired here."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_sensa_nomad_followme"
    entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
    if entity_id:
        registry.async_remove(entity_id)


class SensaDiagnosticCategorySwitch(RestoreEntity, SwitchEntity):
    """One diagnostics category, on EVERY flavour. A new or factory-reset system
    (no restored state) starts with every category on
    (`diagnostics_report.DEFAULT_ON`); a restored choice
    wins, so a customer's own setting holds across restarts and upgrades.

    Analytics & Diagnostics (Settings -> Admin) offers five of these, one per
    snapshot section (diagnostics_report.CATEGORIES): device inventory, unavailable
    devices, error logs, resource usage, uptime. Turning any on makes the device
    include that section in the troubleshooting snapshot it carries up on the
    phone-home; all off means nothing is shared. NOT licence-gated -- a read-only
    unit must still be able to open (or close) diagnostics.

    RestoreEntity purely to show the right toggle position after a restart; the
    durable truth is the App's /data/diagnostics.json. Each switch reports its state
    to the shared DiagnosticsCoordinator, which owns the push machinery and re-syncs
    the App on every reload (an on re-pushes a fresh snapshot + re-arms the
    heartbeat; the last off re-asserts OFF with retry until delivered) -- that's how
    a lost consent state self-heals without the App having to guess."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, category: str
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._category = category
        self._attr_is_on = category in diagnostics_report.DEFAULT_ON
        self._attr_name, self._attr_icon = _DIAG_CATEGORY_META[category]
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_diag_{category}"
        self.entity_id = diagnostics_report.category_switch_entity_id(category)

    def _coordinator(self) -> diagnostics_report.DiagnosticsCoordinator:
        return diagnostics_report.get_coordinator(self.hass, self._entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        # Register the restored consent with the coordinator, which COALESCES all
        # five switches' startup reports into a single re-sync (so they don't race
        # into last-write-wins on the App). No per-switch push here.
        self._coordinator().note_restored_category(self._category, self._attr_is_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._coordinator().async_set_category(self._category, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._coordinator().async_set_category(self._category, False)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaSchengenTrackingSwitch(SwitchEntity):
    """Enable/disable the Schengen 90/180 usage tracker.

    Store-backed: `is_on` reads the singleton Schengen store directly (the store
    is the single source of truth — a push-only attribute would drift if the
    store changed via a service/another client), and on/off call `set_enabled`,
    which persists + fires the change event the card listens to. The card's
    Enable button drives this via the `schengen_set_enabled` service.
    """

    _attr_has_entity_name = True
    _attr_name = "Schengen tracking"
    _attr_icon = "mdi:passport"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_schengen_tracking"
        self.entity_id = "switch.sensa_nomad_schengen_tracking"

    @property
    def is_on(self) -> bool:
        from ._schengen import get_schengen_store
        store = get_schengen_store(self.hass)
        return bool(store and store.enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        from ._schengen import get_schengen_store
        store = get_schengen_store(self.hass)
        if store is not None:
            store.set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        from ._schengen import get_schengen_store
        store = get_schengen_store(self.hass)
        if store is not None:
            store.set_enabled(False)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Reflect store changes made elsewhere (service call / another client).
        from .const import EVENT_SENSA_SCHENGEN_CHANGED

        @callback
        def _on_changed(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SENSA_SCHENGEN_CHANGED, _on_changed)
        )

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


# Plain-English versions of Radar's refusal codes, so the customer sees a reason
# rather than a slug in an attribute they cannot act on. Radar no longer refuses on
# consent or Premium (consent is this switch, and the van's connection rides on the
# Nomad licence), so the only code that reaches here is a session fault.
_TRACKING_HALT_EXPLANATIONS: dict[str, str] = {
    "not_a_van_session": "This unit is not signed in to Radar as a vehicle.",
}


class SensaTrackingSwitch(SwitchEntity):
    """Tracking: start/stop recording this vehicle's position history.

    The on-box PRIMITIVE. Radar stores the uploaded record; a separate mySensa app
    (and future consumers) present it back on a map. Customer-facing name is
    "Tracking".

    Store-backed for the same reason as the Schengen switch: the store is the
    single source of truth, and a push-only attribute drifts the moment anything
    else changes the value.

    **This switch IS the consent.** Turning it on is the customer opting in to
    journey recording, and off means genuinely zero egress: the coordinator returns
    before it reads the tracker or opens a socket, so nothing is sampled, buffered
    or sent. That is why Radar no longer asks for a separate consent step: the van
    sends nothing until this is on, so a batch arriving at Radar is itself the
    opt-in. There is no web-app consent flow to satisfy any more.

    Off by default, like every diagnostics switch: nothing is recorded until the
    customer deliberately asks for it.
    """

    _attr_has_entity_name = True
    _attr_name = "Tracking"
    _attr_icon = "mdi:map-marker-path"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_tracking"
        self.entity_id = "switch.sensa_nomad_tracking"

    @property
    def is_on(self) -> bool:
        from ._tracking import get_tracking_store

        store = get_tracking_store(self.hass)
        return bool(store and store.enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        from ._tracking import get_tracking_store

        store = get_tracking_store(self.hass)
        if store is not None:
            store.set_enabled(True)
        # Clear any halt/back-off: the customer turning this on is them telling us
        # something has changed, and making them wait out a 60-minute back-off to
        # find out whether it worked would be poor.
        coordinator = self._coordinator()
        if coordinator is not None:
            coordinator.resume()
            # Report the flip to Radar so the mySensa on/off indicator updates
            # promptly (best-effort, fire-and-forget so it never blocks the toggle).
            self.hass.async_create_task(coordinator.async_report_state(True))
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        from ._tracking import get_tracking_store

        store = get_tracking_store(self.hass)
        if store is not None:
            store.set_enabled(False)
        coordinator = self._coordinator()
        if coordinator is not None:
            # A one-shot "off" ping (no location) so the mySensa indicator can show
            # OFF even though the switch now goes silent. Fire-and-forget.
            self.hass.async_create_task(coordinator.async_report_state(False))
        self.async_write_ha_state()

    def _coordinator(self) -> Any:
        # Run-level, not per-entry: there is one vehicle and one buffer per HA
        # run, so the coordinator is a singleton alongside the Schengen sampler
        # rather than something each config entry owns a copy of.
        return (self.hass.data.get(DOMAIN) or {}).get("tracking_coordinator")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain the state, including the parts this switch does not control."""
        from ._tracking import get_tracking_store

        store = get_tracking_store(self.hass)
        coordinator = self._coordinator()
        halted = getattr(coordinator, "halted_reason", None) if coordinator else None

        attrs: dict[str, Any] = {
            "buffered_positions": store.buffered_count() if store else 0,
            "gps_source_available": (
                self.hass.states.get("device_tracker.sensa_nomad_vehicle") is not None
            ),
        }
        if halted:
            attrs["paused_reason"] = halted
            attrs["paused_explanation"] = _TRACKING_HALT_EXPLANATIONS.get(
                halted, "Radar refused the last upload."
            )
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from .const import EVENT_SENSA_TRACKING_CHANGED

        @callback
        def _on_changed(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SENSA_TRACKING_CHANGED, _on_changed)
        )

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaTrackingPhonesSwitch(SwitchEntity):
    """Tracking may record a phone when the van has no GPS of its own.

    Off by default, so Tracking records only a GPS that is part of the van: the
    customer agreed to share where the VAN is, and a phone is a person. On, a
    van with no GPS records the navigation position instead, which on Auto is
    the driver's phone, else the phone that reported last (see
    `tracking_coordinator._vehicle_position`). Store-backed like Tracking.
    """

    _attr_has_entity_name = True
    _attr_name = "Use a phone when the van has no GPS"
    _attr_icon = "mdi:cellphone-marker"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_tracking_phones"
        self.entity_id = "switch.sensa_nomad_tracking_phones"

    @property
    def is_on(self) -> bool:
        from ._tracking import get_tracking_store

        store = get_tracking_store(self.hass)
        return bool(store and store.phone_fallback)

    async def _set(self, value: bool) -> None:
        from ._tracking import get_tracking_store

        store = get_tracking_store(self.hass)
        if store is not None:
            store.set_phone_fallback(value)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from .const import EVENT_SENSA_TRACKING_CHANGED

        @callback
        def _on_changed(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SENSA_TRACKING_CHANGED, _on_changed)
        )

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaRemoteSupportSwitch(SwitchEntity):
    """Customer consent toggle for Sensa remote support (default off).

    Store-backed (the single source of truth is `remote_support`'s state, so
    a service call / expiry tick that flips it elsewhere stays reflected). ON
    activates the pre-provisioned `sensa_support` account for a bounded window
    (the window comes from `number.sensa_nomad_remote_support_window_hours`);
    OFF deactivates it and kills its sessions. NOT licence-gated — support
    access must stay reachable even when the unit is read-only.
    """

    _attr_has_entity_name = True
    _attr_name = "Remote Support"
    _attr_icon = "mdi:face-agent"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_remote_support"
        self.entity_id = "switch.sensa_nomad_remote_support"

    @property
    def is_on(self) -> bool:
        from . import remote_support
        state = remote_support.get_state(self.hass)
        return bool(state and state.enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """When the session ends, who opened it, and why.

        On the panel rather than in the dashboard config because none of it is
        known when the dashboard is built, and all of it changes without a
        regen. A closed session reports nothing rather than stale values."""
        from . import remote_support

        state = remote_support.get_state(self.hass)
        # Reported whether or not a session is open: it describes a standing
        # choice the customer made, not this session, so blanking it with the
        # session would hide their own setting from them the moment nobody was
        # signed in.
        opted_out = remote_support.break_glass_opted_out(self.hass)
        # Logins still on the password the image was built with. Carried on the
        # switch the customer is looking at, so the panel can grey the toggle out
        # and name them without a second entity to wire up. `None` = the App
        # could not ask, which is not a block. See default_logins.py.
        from . import default_logins

        weak = default_logins.get(self.hass)
        if state is None or not state.enabled:
            return {
                "enabled_until": None,
                "opened_by": None,
                "reason": None,
                "granted_by": None,
                "break_glass_opted_out": opted_out,
                "default_logins": weak,
            }
        return {
            "enabled_until": (
                state.enabled_until.isoformat() if state.enabled_until else None
            ),
            "opened_by": state.opened_by,
            "reason": state.reason,
            "granted_by": state.granted_by,
            "break_glass_opted_out": opted_out,
            "default_logins": weak,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        from . import remote_support
        from .const import REMOTE_SUPPORT_WINDOW_NUMBER_ENTITY_ID

        window: Any = None
        st = self.hass.states.get(REMOTE_SUPPORT_WINDOW_NUMBER_ENTITY_ID)
        if st is not None:
            window = st.state
        await remote_support.async_set_enabled(
            self.hass, self._entry, True, window_hours=window
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        from . import remote_support
        await remote_support.async_set_enabled(self.hass, self._entry, False)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from .const import EVENT_SENSA_REMOTE_SUPPORT_CHANGED

        @callback
        def _on_changed(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_SENSA_REMOTE_SUPPORT_CHANGED, _on_changed
            )
        )
        # Rotating the last default password must ungrey the toggle on the next
        # status poll, not on the next restart.
        from .const import EVENT_SENSA_DEFAULT_LOGINS_CHANGED

        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_SENSA_DEFAULT_LOGINS_CHANGED, _on_changed
            )
        )

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaRemoteAccessSwitch(SwitchEntity):
    """The customer's own remote-access switch (default off).

    Distinct from Remote Support beside it: that one decides whether a Sensa
    engineer has a login, this one decides whether the box answers on its public
    hostname at all. Off is the default because a provisioned tunnel puts Home
    Assistant's login page on the internet, and that should be the customer's
    deliberate act rather than a side effect of buying an appliance.

    The App owns the setting and the cloudflared process; this entity reads its
    cached answer and posts changes. NOT licence-gated -- a read-only box is
    exactly the box somebody needs to reach.
    """

    _attr_has_entity_name = True
    _attr_name = "Remote Access"
    _attr_icon = "mdi:web"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sensa_nomad_remote_access"
        self.entity_id = REMOTE_ACCESS_SWITCH_ENTITY_ID

    @property
    def is_on(self) -> bool:
        from . import remote_access

        return remote_access.get_enabled(self.hass)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Logins still on the password the image was built with.

        The one thing that stops this switch being usable and that the customer
        can fix themselves, so it travels with the switch rather than only in the
        error they get after pressing it. `None` = the App could not ask, which
        blocks nothing. See default_logins.py."""
        from . import default_logins

        return {"default_logins": default_logins.get(self.hass)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        from . import remote_access

        await remote_access.async_set_enabled(self.hass, self._entry, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        from . import remote_access

        await remote_access.async_set_enabled(self.hass, self._entry, False)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _on_changed(_event) -> None:
            self.async_write_ha_state()

        # The licence-status poll is what learns the App's answer, so without
        # this the switch would keep showing the value it had at startup after a
        # change made anywhere else (the App's own default, a restore, a future
        # service call).
        # Same repaint for the default-password gate, which is the other thing
        # that decides whether this toggle can be used.
        from .const import EVENT_SENSA_DEFAULT_LOGINS_CHANGED

        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_SENSA_DEFAULT_LOGINS_CHANGED, _on_changed
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SENSA_REMOTE_ACCESS_CHANGED, _on_changed)
        )

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


def build_teltonika_switch_entities(
    entry: ConfigEntry, device: dict,
) -> list[SensaConfigSwitch]:
    """Build the config-switch entities for one Teltonika device."""
    if device.get("integration_type") != "sensa_teltonika_api":
        return []

    device_id = device.get("device_id", "unknown")
    mac = device.get("linked_mac_address") or device.get("mac_address") or ""
    return [
        SensaConfigSwitch(
            entry,
            device_id=device_id,
            mac=mac,
            key="ssl",
            name="SSL",
            icon="mdi:shield-lock",
        )
    ]


class SensaConfigSwitch(RestoreEntity, SwitchEntity):
    """Switch entity for a Sensa device configuration toggle."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        key: str,
        name: str,
        icon: str,
        mac: str = "",
        device_info: dict | None = None,
        default_on: bool | None = None,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_config_{key}"
        self._attr_name = name
        self._attr_icon = icon
        # Default ON state: explicit override (used by Cerbo, which wants
        # SSL off by default — Venus OS ships MQTT-on-LAN as plaintext),
        # else the legacy per-key defaults (Teltonika SSL-on).
        if default_on is not None:
            self._attr_is_on = default_on
        else:
            # SSL-specific default sourced from the single-source-of-truth constant.
            # Non-ssl switches default to True too (same behaviour as before).
            self._attr_is_on = TELTONIKA_DEFAULT_SSL if key == "ssl" else True
        self._device_info_override = device_info

    async def async_added_to_hass(self) -> None:
        """Restore previous value on startup."""
        # Skip stale "off" state that was explicitly scrubbed on a prior
        # integration removal — treat unknown/unavailable as "no restore"
        # so the configured default in __init__ wins for a fresh install.
        if (
            last_state := await self.async_get_last_state()
        ) is not None and last_state.state in ("on", "off"):
            self._attr_is_on = last_state.state == "on"

    async def async_will_remove_from_hass(self) -> None:
        """Write a neutral final state so a reinstall doesn't resurrect
        stale user config (e.g. an SSL-off choice that no longer matches
        the router's current setup). Lets the constructor default take
        effect on the next setup."""
        self._attr_is_on = TELTONIKA_DEFAULT_SSL if self._attr_name == "SSL" else True
        try:
            self.async_write_ha_state()
        except Exception:
            _LOGGER.debug(
                "Teltonika config switch %s: state write on remove failed",
                self.entity_id, exc_info=True,
            )

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def device_info(self):
        if self._device_info_override is not None:
            return self._device_info_override
        return {
            "identifiers": {(DOMAIN, f"teltonika_{self._device_id}")},
            "name": teltonika_device_name(self._mac),
            "manufacturer": "Teltonika",
            "model": "RutOS Device",
        }


def build_cerbo_switch_entities(
    entry: ConfigEntry, device: dict,
) -> list[SensaConfigSwitch]:
    """Build the config-switch entities for a Cerbo GX.

    Today that's only the SSL toggle. Defaults OFF because Venus OS
    ships MQTT-on-LAN as plaintext (port 1883); SSL-only mode (port
    8883) is opt-in."""
    if device.get("integration_type") != "sensa_victron_mqtt":
        return []

    from .text import _cerbo_device_info
    device_id = device.get("device_id", "unknown")
    mac = device.get("linked_mac_address") or device.get("mac_address") or ""
    return [
        SensaConfigSwitch(
            entry,
            device_id=device_id,
            mac=mac,
            key="ssl",
            name="SSL",
            icon="mdi:shield-lock",
            device_info=_cerbo_device_info(device_id, mac),
            default_on=False,
        )
    ]


def build_relay_enabled_switches(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
) -> list[SensaRelayChannelEnabledSwitch]:
    """Build 8 channel-enabled toggle switches for one relay controller.

    Exposed so the Discovery picker's add-dispatch can create these for a
    newly-registered relay device without reloading the entry.
    """
    from .device_catalog import relay_channel_count

    device_id = str(device.get("device_id") or device.get("id") or "unknown")
    device_name = device.get("display_name") or device.get("name") or "Relay"
    return [
        SensaRelayChannelEnabledSwitch(hass, entry, device_id, device_name, n)
        for n in range(1, relay_channel_count(device) + 1)
    ]


class SensaRelayChannelEnabledSwitch(SwitchEntity):
    """Enabled toggle for one relay channel.

    Persists to entry.options["relay_enabled"] and triggers a dashboard
    rebuild so disabled channels are immediately hidden from the relay card
    and any other generated selectors.

    Absent from options means enabled (True is the default); only False
    values are stored so options stay minimal.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:toggle-switch-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        n: int,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._slot_key = f"relay_{n}"
        self._attr_name = f"{device_name} channel {n} enabled"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_relay_{n}_enabled"
        self.entity_id = relay_channel_enabled_entity_id(device_id, n)

    @property
    def is_on(self) -> bool:
        val = (
            (self._entry.options.get("relay_enabled") or {})
            .get(self._device_id, {})
            .get(self._slot_key)
        )
        return val is not False  # absent or True → enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug(
            "Relay enabled toggle ON: device=%s slot=%s entity_id=%s",
            self._device_id, self._slot_key, self.entity_id,
        )
        all_enabled: dict[str, Any] = dict(self._entry.options.get("relay_enabled") or {})
        dev_enabled: dict[str, Any] = dict(all_enabled.get(self._device_id) or {})
        dev_enabled.pop(self._slot_key, None)  # remove False → reverts to default True
        if dev_enabled:
            all_enabled[self._device_id] = dev_enabled
        elif self._device_id in all_enabled:
            del all_enabled[self._device_id]
        self._save_and_rebuild(all_enabled)

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug(
            "Relay enabled toggle OFF: device=%s slot=%s entity_id=%s",
            self._device_id, self._slot_key, self.entity_id,
        )
        all_enabled: dict[str, Any] = dict(self._entry.options.get("relay_enabled") or {})
        dev_enabled: dict[str, Any] = dict(all_enabled.get(self._device_id) or {})
        dev_enabled[self._slot_key] = False
        all_enabled[self._device_id] = dev_enabled
        self._save_and_rebuild(all_enabled)

    def _save_and_rebuild(self, all_enabled: dict[str, Any]) -> None:
        _LOGGER.debug(
            "Relay enabled save: device=%s new_state=%s",
            self._device_id,
            all_enabled.get(self._device_id, "absent (all enabled)"),
        )
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, "relay_enabled": all_enabled}
        )
        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


def _build_header_power_chip_switches(
    hass: HomeAssistant, entry: ConfigEntry, device_list: list[dict[str, Any]]
) -> list[SensaHeaderPowerChipSwitch]:
    """Every "Show Power In Header" switch this entry should own this turn.

    Same three-source union the relay switches use above, for the same reason:
    the backend snapshot is empty while the add-on restarts after an OTA, and a
    config entity that vanishes on that turn becomes a registry orphan the
    customer sees as an unavailable row (see the config-entity orphan note in
    `select.py`). Sources, in order of trust:

      1. the live snapshot (the only one carrying a display_name),
      2. the entity registry (recovers the id AND the name we registered with),
      3. `entry.options` (survives a registry purge or a HACS cache wipe).
    """
    from .vendors.service_classes import classify

    # device_id -> (name, service_class)
    found: dict[str, tuple[str, str]] = {}
    for device in device_list:
        service_class = str(device.get("service_class") or classify(device) or "")
        if not header_power_chip_eligible(service_class):
            continue
        device_id = str(device.get("device_id") or device.get("id") or "")
        if not device_id:
            continue
        name = str(
            device.get("display_name")
            or device.get("name")
            or header_power_chip_fallback_name(service_class)
        )
        found.setdefault(device_id, (name, service_class))

    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id or ent.domain != "switch":
                continue
            uid = ent.unique_id or ""
            if not uid.startswith(prefix):
                continue
            if not uid.endswith(HEADER_POWER_CHIP_UID_SUFFIX):
                continue
            device_id = uid[len(prefix):-len(HEADER_POWER_CHIP_UID_SUFFIX)]
            if not device_id or device_id in found:
                continue
            # `original_name` is what we registered the switch with, so a
            # rebuilt switch keeps the customer's device name rather than
            # reverting to a generic fallback.
            recovered = str(ent.original_name or "").removesuffix(
                " Show Power In Header"
            )
            found[device_id] = (recovered or "Device", "")
    except Exception:
        _LOGGER.warning(
            "Registry walk for header-power-chip device_ids failed",
            exc_info=True,
        )

    for device_id in entry.options.get(HEADER_POWER_CHIP_OPTION) or []:
        device_id = str(device_id or "")
        if device_id and device_id not in found:
            found[device_id] = ("Device", "")

    return [
        SensaHeaderPowerChipSwitch(hass, entry, device_id, name)
        for device_id, (name, _sc) in found.items()
    ]


def build_header_power_chip_switches(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
) -> list[SensaHeaderPowerChipSwitch]:
    """The "Show Power In Header" switch for ONE device, or an empty list when
    its service class has no such control.

    Returns a list (uniform with the other `build_*` helpers) so the Discovery
    picker's add-dispatch can create it for a newly-registered device without
    reloading the entry.
    """
    from .vendors.service_classes import classify

    service_class = str(device.get("service_class") or classify(device) or "")
    if not header_power_chip_eligible(service_class):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = str(
        device.get("display_name")
        or device.get("name")
        or header_power_chip_fallback_name(service_class)
    )
    return [SensaHeaderPowerChipSwitch(hass, entry, device_id, name)]


class SensaHeaderPowerChipSwitch(SwitchEntity):
    """Per-device "Show Power In Header" toggle.

    On puts the device's live power draw in the Sensa header bar (the same
    strip as the Mode and temperature chips, so it is in the customer's eyeline
    on Habitation, Cab and Settings alike); off leaves the header alone.
    Surfaced on the Settings -> Devices row's expanded pane via
    `sensor._config_entities_for_device`, as a `mode: "switch"` field.

    Options-as-truth, not RestoreEntity: `entry.options["header_power_chips"]`
    holds only the ids that are ON, so an absent id reads off and a removed
    device leaves nothing behind. Toggling rebuilds the dashboards, which is
    what actually adds or removes the chip -- the header chip list is baked
    into the panel config, not resolved client-side.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._attr_icon = HEADER_POWER_CHIP_ICON
        self._attr_name = f"{device_name} Show Power In Header"
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}{HEADER_POWER_CHIP_UID_SUFFIX}"
        )
        self.entity_id = header_power_chip_switch_entity_id(device_id)

    @property
    def is_on(self) -> bool:
        return header_power_chip_enabled(self._entry.options, self._device_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._save_and_rebuild(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._save_and_rebuild(False)

    def _save_and_rebuild(self, on: bool) -> None:
        ledger = [
            str(d)
            for d in (self._entry.options.get(HEADER_POWER_CHIP_OPTION) or [])
            if str(d) != self._device_id
        ]
        if on:
            ledger.append(self._device_id)
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, HEADER_POWER_CHIP_OPTION: ledger},
        )
        from . import _async_rebuild_sensa_dashboards

        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


def _build_header_temp_chip_switches(
    hass: HomeAssistant, entry: ConfigEntry, device_list: list[dict[str, Any]]
) -> list[SensaHeaderTempChipSwitch]:
    """Every "Show Temperature In Header" switch this entry should own.

    Same three-source union as `_build_header_power_chip_switches` above, for
    the same reason: the backend snapshot is empty while the add-on restarts
    after an OTA, and a config entity that vanishes on that turn becomes a
    registry orphan the customer sees as an unavailable row.
    """
    from .vendors.service_classes import classify

    # device_id -> name
    found: dict[str, str] = {}
    for device in device_list:
        service_class = str(device.get("service_class") or classify(device) or "")
        if not header_temp_chip_eligible(service_class):
            continue
        device_id = str(device.get("device_id") or device.get("id") or "")
        if not device_id:
            continue
        name = str(
            device.get("display_name")
            or device.get("name")
            or HEADER_TEMP_CHIP_FALLBACK_NAME
        )
        found.setdefault(device_id, name)

    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id or ent.domain != "switch":
                continue
            uid = ent.unique_id or ""
            if not uid.startswith(prefix):
                continue
            if not uid.endswith(HEADER_TEMP_CHIP_UID_SUFFIX):
                continue
            device_id = uid[len(prefix):-len(HEADER_TEMP_CHIP_UID_SUFFIX)]
            if not device_id or device_id in found:
                continue
            # `original_name` is what we registered the switch with, so a
            # rebuilt switch keeps the customer's device name rather than
            # reverting to a generic fallback.
            recovered = str(ent.original_name or "").removesuffix(
                " Show Temperature In Header"
            )
            found[device_id] = recovered or HEADER_TEMP_CHIP_FALLBACK_NAME
    except Exception:
        _LOGGER.warning(
            "Registry walk for header-temp-chip device_ids failed",
            exc_info=True,
        )

    for device_id in entry.options.get(HEADER_TEMP_CHIP_OPTION) or []:
        device_id = str(device_id or "")
        if device_id and device_id not in found:
            found[device_id] = HEADER_TEMP_CHIP_FALLBACK_NAME

    return [
        SensaHeaderTempChipSwitch(hass, entry, device_id, name)
        for device_id, name in found.items()
    ]


def build_header_temp_chip_switches(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
) -> list[SensaHeaderTempChipSwitch]:
    """The "Show Temperature In Header" switch for ONE device, or an empty list
    when it is not an environment device.

    Returns a list (uniform with the other `build_*` helpers) so the Discovery
    picker's add-dispatch can create it for a newly-registered device without
    reloading the entry.
    """
    from .vendors.service_classes import classify

    service_class = str(device.get("service_class") or classify(device) or "")
    if not header_temp_chip_eligible(service_class):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = str(
        device.get("display_name")
        or device.get("name")
        or HEADER_TEMP_CHIP_FALLBACK_NAME
    )
    return [SensaHeaderTempChipSwitch(hass, entry, device_id, name)]


class SensaHeaderTempChipSwitch(SwitchEntity):
    """Per-device "Show Temperature In Header" toggle.

    On puts the device's temperature reading in the Sensa header bar (one strip
    shared by Habitation, Cab and Settings); off leaves the header alone.
    Surfaced on the Settings -> Devices row's expanded pane via
    `sensor._config_entities_for_device`, as a `mode: "switch"` field.

    Off by default, because the header used to fill itself with every
    environment temperature a van reports and overflowed on a narrow tablet.
    The customer opts in to as many as their screen fits.

    Options-as-truth, not RestoreEntity: `entry.options["header_temperature_chips"]`
    holds only the ids that are ON, so an absent id reads off and a removed
    device leaves nothing behind. Toggling rebuilds the dashboards, which is
    what actually adds or removes the chip -- the header chip list is baked
    into the panel config, not resolved client-side.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._attr_icon = HEADER_TEMP_CHIP_ICON
        self._attr_name = f"{device_name} Show Temperature In Header"
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}{HEADER_TEMP_CHIP_UID_SUFFIX}"
        )
        self.entity_id = header_temp_chip_switch_entity_id(device_id)

    @property
    def is_on(self) -> bool:
        return header_temp_chip_enabled(self._entry.options, self._device_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._save_and_rebuild(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._save_and_rebuild(False)

    def _save_and_rebuild(self, on: bool) -> None:
        ledger = [
            str(d)
            for d in (self._entry.options.get(HEADER_TEMP_CHIP_OPTION) or [])
            if str(d) != self._device_id
        ]
        if on:
            ledger.append(self._device_id)
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, HEADER_TEMP_CHIP_OPTION: ledger},
        )
        from . import _async_rebuild_sensa_dashboards

        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


def _build_temperature_watch_switches(
    hass: HomeAssistant, entry: ConfigEntry, device_list: list[dict[str, Any]]
) -> list[SensaTemperatureWatchSwitch]:
    """Every temperature-flag switch (Temperature Watch, Fridge, Freezer)
    this entry should own: one per flag per environment device.

    Same three-source union as `_build_header_temp_chip_switches` above, for
    the same reason: the backend snapshot is empty while the add-on restarts
    after an OTA, and a config entity that vanishes on that turn becomes a
    registry orphan the customer sees as an unavailable row.
    """
    from .vendors.service_classes import classify

    # device_id -> name
    found: dict[str, str] = {}
    for device in device_list:
        service_class = str(device.get("service_class") or classify(device) or "")
        if not header_temp_chip_eligible(service_class):
            continue
        device_id = str(device.get("device_id") or device.get("id") or "")
        if not device_id:
            continue
        name = str(
            device.get("display_name")
            or device.get("name")
            or TEMPERATURE_FLAG_FALLBACK_NAME
        )
        found.setdefault(device_id, name)

    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id or ent.domain != "switch":
                continue
            uid = ent.unique_id or ""
            if not uid.startswith(prefix):
                continue
            flag = next(
                (f for f in TEMPERATURE_FLAGS if uid.endswith(f.uid_suffix)),
                None,
            )
            if flag is None:
                continue
            device_id = uid[len(prefix):-len(flag.uid_suffix)]
            if not device_id or device_id in found:
                continue
            # `original_name` is what we registered the switch with, so a
            # rebuilt switch keeps the customer's device name rather than
            # reverting to a generic fallback.
            recovered = str(ent.original_name or "").removesuffix(
                f" {flag.label}"
            )
            found[device_id] = recovered or TEMPERATURE_FLAG_FALLBACK_NAME
    except Exception:
        _LOGGER.warning(
            "Registry walk for temperature-flag device_ids failed",
            exc_info=True,
        )

    for flag in TEMPERATURE_FLAGS:
        for device_id in entry.options.get(flag.option) or []:
            device_id = str(device_id or "")
            if device_id and device_id not in found:
                found[device_id] = TEMPERATURE_FLAG_FALLBACK_NAME

    return [
        SensaTemperatureWatchSwitch(hass, entry, device_id, name, flag)
        for device_id, name in found.items()
        for flag in TEMPERATURE_FLAGS
    ]


def build_temperature_watch_switches(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: dict[str, Any],
    flags: tuple[TemperatureFlag, ...] = TEMPERATURE_FLAGS,
) -> list[SensaTemperatureWatchSwitch]:
    """The temperature-flag switches for ONE device (Temperature Watch,
    Fridge, Freezer, or the `flags` subset asked for), or an empty list when
    it is not an environment device.

    Returns a list (uniform with the other `build_*` helpers) so the Discovery
    picker's add-dispatch can create them for a newly-registered device
    without reloading the entry.
    """
    from .vendors.service_classes import classify

    service_class = str(device.get("service_class") or classify(device) or "")
    if not header_temp_chip_eligible(service_class):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = str(
        device.get("display_name")
        or device.get("name")
        or TEMPERATURE_FLAG_FALLBACK_NAME
    )
    return [
        SensaTemperatureWatchSwitch(hass, entry, device_id, name, flag)
        for flag in flags
    ]


class SensaTemperatureWatchSwitch(SwitchEntity):
    """Per-device temperature-flag toggle: "Temperature Watch", "Fridge" or
    "Freezer" (`const.TEMPERATURE_FLAGS`), one instance per flag.

    On flags the device for that flag's instinct (instincts_catalog.py:
    "Temperature watch" / "Fridge watch" / "Freezer watch") to cover; off
    drops it. Surfaced on the Settings -> Devices row's expanded pane via
    `sensor._config_entities_for_device`, as a `mode: "switch"` field: the
    SAME eligible device set as "Show Temperature In Header"
    (`header_temp_chip_eligible`, reused rather than duplicated).

    Off by default: a fridge probe added for the header chip or the Climate
    tab isn't necessarily one the customer wants alarming.

    Options-as-truth, not RestoreEntity: the flag's ledger in entry.options
    holds only the ids that are ON, so an absent id reads off and a removed
    device leaves nothing behind. Toggling dispatches
    `SIGNAL_DASHBOARD_REGEN_REQUESTED` (NOT a direct dashboard rebuild: this
    switch has no dashboard-YAML footprint) so the debounced automations
    regen listener re-resolves the instinct's watched-entity list and
    rewrites automations.yaml.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        flag: TemperatureFlag = TEMPERATURE_WATCH_FLAG,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._flag = flag
        self._attr_icon = flag.icon
        self._attr_name = f"{device_name} {flag.label}"
        self._attr_unique_id = flag.unique_id(entry.entry_id, device_id)
        self.entity_id = flag.switch_entity_id(device_id)

    @property
    def is_on(self) -> bool:
        return self._flag.enabled(self._entry.options, self._device_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._save_and_regenerate(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._save_and_regenerate(False)

    def _save_and_regenerate(self, on: bool) -> None:
        option = self._flag.option
        ledger = [
            str(d)
            for d in (self._entry.options.get(option) or [])
            if str(d) != self._device_id
        ]
        if on:
            ledger.append(self._device_id)
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, option: ledger},
        )
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        async_dispatcher_send(self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED)
        self.async_write_ha_state()


def build_asustor_switch_entities(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
) -> list[SensaNasSwitch]:
    """Build the single power switch for one Asustor NAS.

    Exposed so the Discovery picker's add-dispatch can create it for a NAS
    registered after initial setup without reloading the entry.
    """
    if device.get("integration_type") != "sensa_asustor_ssh" and (
        device.get("integration") != "sensa_asustor_ssh"
    ):
        return []
    return [SensaNasSwitch(hass, entry, device)]


class SensaNasSwitch(SwitchEntity):
    """Power switch for one Asustor NAS.

    State reflects whether the NAS answers on its SSH port (a TCP-connect
    probe, polled by HA). OFF triggers a clean shutdown over SSH; ON sends a
    Wake-on-LAN magic packet. `assumed_state` because both actions are
    fire-and-forget — the box takes time to halt / boot, so the toggle is
    optimistic until the next reachability poll catches up.
    """

    _attr_has_entity_name = True
    _attr_name = "Power"
    _attr_icon = "mdi:nas"
    _attr_assumed_state = True
    _attr_should_poll = True

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = str(
            device.get("device_id") or device.get("id") or "unknown"
        )
        self._mac = (
            device.get("linked_mac_address") or device.get("mac_address") or ""
        )
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_nas_power"
        self.entity_id = nas_power_switch_entity_id(self._device_id)
        self._attr_is_on = False
        # Built from the device's stored config (host + SSH creds + MAC).
        # None when no host is configured yet — the switch shows unavailable
        # until the customer saves config on the Support tab.
        from . import _build_asustor_client
        self._client = _build_asustor_client(hass, device)

    async def async_added_to_hass(self) -> None:
        """Register this live instance in runtime so the configure_device
        save path can rebind its client in place (no coordinator to restart)."""
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if isinstance(runtime, dict):
            runtime[f"nas_switch_{self._device_id}"] = self

    async def async_will_remove_from_hass(self) -> None:
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if isinstance(runtime, dict):
            runtime.pop(f"nas_switch_{self._device_id}", None)

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def device_info(self) -> dict:
        from .text import _asustor_device_info
        return _asustor_device_info(self._device_id, self._mac)

    def rebind(self, client) -> None:
        """Swap in a freshly-built client after the customer saves new config."""
        self._client = client

    async def async_update(self) -> None:
        if self._client is None:
            self._attr_is_on = False
            return
        try:
            self._attr_is_on = await self._client.async_is_reachable()
        except Exception:
            _LOGGER.debug(
                "NAS %s: reachability probe failed", self._device_id,
                exc_info=True,
            )
            self._attr_is_on = False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Wake-on-LAN power-on."""
        from . import require_licence_ok

        require_licence_ok(self.hass, self._entry.entry_id)
        if self._client is None:
            return
        try:
            await self._client.async_wol()
        except Exception:
            _LOGGER.warning(
                "NAS %s: Wake-on-LAN failed", self._device_id, exc_info=True,
            )
            return
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clean shutdown over SSH."""
        from . import require_licence_ok

        require_licence_ok(self.hass, self._entry.entry_id)
        if self._client is None:
            return
        try:
            await self._client.async_shutdown()
        except Exception:
            _LOGGER.warning(
                "NAS %s: clean shutdown failed", self._device_id, exc_info=True,
            )
            return
        self._attr_is_on = False
        self.async_write_ha_state()


def build_light_leave_relay_on_switches(
    hass: HomeAssistant, entry: ConfigEntry,
) -> list[SensaLightLeaveRelayOnSwitch]:
    """One "Leave Relay On" switch per Zigbee2MQTT lighting controller.

    Same candidate set as the Power-On Delay number beside it, so a controller
    can never be offered one of the pair and not the other.
    """
    from .select import zigbee_light_controller_device_ids

    return [
        SensaLightLeaveRelayOnSwitch(entry, dev_id)
        for dev_id in zigbee_light_controller_device_ids(hass)
    ]


class SensaLightLeaveRelayOnSwitch(RestoreEntity, SwitchEntity):
    """Per-controller "Leave Relay On": does turning the light OFF also cut its
    supply relay?

    OFF (the default) is the behaviour since 2026-09-08: the relay follows the
    controller both ways, so a customer never has to switch a relay channel
    separately from the light it feeds, and a light that is off draws nothing.

    ON leaves the relay powered. The controller stays booted, so the next press
    lights the strip immediately with no Power-On Delay and no re-sends, at the
    cost of its standby draw. A van using its lighting all evening wants that;
    one parked up for a week does not, which is why it is the customer's choice
    per controller rather than a product-wide decision.

    Read LIVE by the cards at press time (never baked into a dashboard build),
    so a change takes effect on the very next press. `RestoreEntity` keeps it
    across restarts; `EntityCategory.CONFIG` keeps it out of the Mode Editor.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:electric-switch"

    def __init__(self, entry: ConfigEntry, ha_device_id: str) -> None:
        from .const import (
            light_leave_relay_on_switch_entity_id,
            light_leave_relay_on_unique_id,
        )

        self._entry = entry
        self._ha_device_id = ha_device_id
        self._attr_name = "Leave Relay On"
        self._attr_unique_id = light_leave_relay_on_unique_id(
            entry.entry_id, ha_device_id,
        )
        self.entity_id = light_leave_relay_on_switch_entity_id(ha_device_id)
        self._attr_is_on = False

    @property
    def device_info(self) -> dict:
        # The Z2M controller's HA device is mqtt-owned, so this attaches to the
        # Sensa Nomad hub device, like the other per-controller config entities.
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # Only a stored "on" turns it on. unknown / unavailable / missing all
        # mean "never chosen", and the default is to cut the relay.
        if last is not None and last.state == "on":
            self._attr_is_on = True

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


def build_fan_auto_switches(entry: ConfigEntry, device: dict[str, Any]) -> list[SensaFanAutoSwitch]:
    """One Auto switch per fan on a Sensa Fan Temperature Control, else none."""
    from .const import FAN_CHANNELS
    from .fan_autopilot import is_fan_controller

    if not is_fan_controller(device):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = device.get("display_name") or device.get("name") or "Fan Temperature Control"
    return [SensaFanAutoSwitch(entry, device_id, name, ch) for ch in FAN_CHANNELS]


class SensaFanAutoSwitch(SwitchEntity):
    """Whether the autopilot may drive one fan right now.

    The fan card turns it off when the customer sets a speed by hand, so the
    autopilot does not undo it within a minute, and its Auto button turns it
    back on. On with no Temperature Sensor chosen does nothing: the fan is
    Manual by its sensor setting. Options-as-truth (`FAN_AUTOPILOT_OPTION`).
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:fan-auto"

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str, channel: int,
    ) -> None:
        from .const import fan_auto_switch_entity_id, fan_autopilot_key

        self._entry = entry
        self._device_id = device_id
        self._key = fan_autopilot_key(channel, "auto")
        self._attr_name = f"{device_name} Fan {channel} Auto"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{self._key}"
        self.entity_id = fan_auto_switch_entity_id(device_id, channel)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def is_on(self) -> bool:
        from .fan_autopilot import setting

        return bool(setting(self._entry, self._device_id, self._key))

    async def _async_set(self, on: bool) -> None:
        from .fan_autopilot import async_drive_fans, set_setting

        set_setting(self.hass, self._entry, self._device_id, self._key, on)
        self.async_write_ha_state()
        if on:
            # Back to Auto: bring the fan to its temperature's speed now.
            self.hass.async_create_task(async_drive_fans(self.hass, self._entry))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


def build_starter_charge_switch(
    coordinator: Any,
    entry_id: str,
    cerbo_device_id: str,
    service: str,
    instance: str,
) -> VictronStarterChargeSwitch:
    """Construct the starter-battery charge control for one Orion XS wired
    to a Cerbo. Called by the Victron `on_new_device` hook in __init__.py,
    only when the Orion has published `EmergencyCharge/Control` (firmware
    1.15 or later, not in PSU mode)."""
    return VictronStarterChargeSwitch(
        coordinator, entry_id, cerbo_device_id, service, instance,
    )


class VictronStarterChargeSwitch(CoordinatorEntity, SwitchEntity):
    """Orion XS emergency reverse charge: charge a flat starter battery from
    the leisure bank, over the Cerbo's MQTT broker.

    On while a charge is started (`Control` bit 0), which includes the
    Orion's 5-minute pause between 15-minute cycles. Attributes carry what
    the power-flow card needs: `ready` (the Orion says it can start one now)
    and the two countdowns in seconds.

    The unique_id follows the Victron sub-device convention so it buckets
    into the Orion's `service_entities["starter_charge"]` role-map.
    """

    _attr_has_entity_name = True
    _attr_name = "Starter Battery Charge"
    _attr_icon = "mdi:car-battery"

    def __init__(
        self,
        coordinator: Any,
        entry_id: str,
        cerbo_device_id: str,
        service: str,
        instance: str,
    ) -> None:
        super().__init__(coordinator)
        self._cerbo_device_id = cerbo_device_id
        self._service = service
        self._instance = str(instance)
        self._attr_unique_id = (
            f"{entry_id}_victron_cerbo_{cerbo_device_id}_"
            f"{service}_{self._instance}_starter_charge"
        )

    def _cached(self, tail: str) -> int | None:
        cache = (self.coordinator.data or {}).get(
            f"{self._service}:{self._instance}", {},
        )
        try:
            return int(cache.get(tail))
        except (TypeError, ValueError):
            return None

    def _control(self) -> int | None:
        from .vendors.victron_mqtt_services import EMERGENCY_CHARGE_CONTROL

        return self._cached(EMERGENCY_CHARGE_CONTROL)

    @property
    def available(self) -> bool:
        return super().available and self._control() is not None

    @property
    def is_on(self) -> bool | None:
        raw = self._control()
        return None if raw is None else bool(raw & 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .vendors.victron_mqtt_services import (
            EMERGENCY_CHARGE_BLOCKED_COUNTDOWN,
            EMERGENCY_CHARGE_CHARGING_COUNTDOWN,
        )

        raw = self._control()
        return {
            "ready": bool(raw and raw & 2),
            "charging_seconds": self._cached(EMERGENCY_CHARGE_CHARGING_COUNTDOWN),
            "paused_seconds": self._cached(EMERGENCY_CHARGE_BLOCKED_COUNTDOWN),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        # Identifiers plus a DEFAULT name only: the Orion's card is named by
        # its sensors (VictronDeviceSensor.device_info), and this must not
        # rename it.
        from .vendors.victron_mqtt_services import SERVICE_LABELS

        label = SERVICE_LABELS.get(self._service, self._service.title())
        return {
            "identifiers": {
                (DOMAIN,
                 (f"victron_cerbo_{self._cerbo_device_id}_"
                  f"{self._service}_{self._instance}")),
            },
            "default_name": f"{label} #{self._instance}",
            "manufacturer": "Victron Energy",
            "via_device": (DOMAIN, f"victron_cerbo_{self._cerbo_device_id}"),
        }

    async def _async_set(self, on: bool) -> None:
        ok = await self.coordinator.async_set_emergency_charge(
            self._service, self._instance, on,
        )
        if not ok:
            _LOGGER.warning(
                "Starter battery charge: write to %s %s did not reach the "
                "Cerbo broker (no connection / VRM id yet)",
                self._service, self._instance,
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not self.extra_state_attributes["ready"]:
            # The Orion ignores a start it cannot do (still forward charging,
            # or no starter battery voltage set in VictronConnect).
            _LOGGER.info(
                "Starter battery charge: %s %s is not ready, start not sent",
                self._service, self._instance,
            )
            return
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
