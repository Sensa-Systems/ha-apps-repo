"""Text platform for Sensa Nomad — router credential + scan-subnet input."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import system_name as system_name_store
from .const import (
    CONF_SCAN_SUBNET,
    DOMAIN,
    HEADER_SHORT_NAME_OPTION,
    HEADER_SHORT_NAME_UID_SUFFIX,
    LEVEL_IMAGE_SLOTS,
    LICENCE_CODE_ENTITY_ID,
    SWITCH_PORT_LABELS_OPTION,
    SYSTEM_NAME_ENTITY_ID,
    SYSTEM_NAME_MAX_LENGTH,
    header_chip_eligible,
    header_short_name,
    header_short_name_entity_id,
    level_image_entity_id,
    nas_config_text_entity_id,
    relay_channel_image_entity_id,
    relay_channel_name_entity_id,
    relay_device_name_entity_id,
    switch_port_name_entity_id,
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
    """Set up Sensa Nomad text entities."""
    runtime = hass.data[DOMAIN][entry.entry_id]

    # Expose the add callback so dynamic post-setup additions (e.g. a
    # Teltonika device added via the Discovery picker after install)
    # can create their per-device text entities without a reload.
    runtime["vendor_add_text"] = async_add_entities

    # Flush any vendor text entities queued before this platform registered
    # its callback. `sensor` / `binary_sensor` / `select` have had this drain
    # since the Cerbo silent-drop fix; `text` never did, so anything using
    # `_add_or_queue_vendor_entities(runtime, "text", ...)` would stash into
    # `vendor_pending_text` with nothing to read it back. The switch-port name
    # fields are the first user of that path.
    pending_text = runtime.pop("vendor_pending_text", None)
    if pending_text:
        _LOGGER.info(
            "Sensa Nomad text setup: flushing %d vendor text entity(ies) "
            "queued before the platform was ready",
            len(pending_text),
        )
        async_add_entities(pending_text)

    entities: list[TextEntity] = [
        SensaScanSubnetText(hass, entry),
        # Appliance licence-recovery code field (paired with the activate
        # button). Created on every install — harmless config entity — but only
        # surfaced by the appliance-only Support-tab card.
        SensaLicenceCodeText(entry),
        # Customer's name for this system / vehicle, shown on the Licence panel
        # and carried up on phone-home for the Hub fleet view. All flavours.
        SensaSystemNameText(hass, entry),
    ]

    # Singleton picture overrides for the Cab → Levelling cards (one van, so
    # one each for the top-down / side / rear views). Always present; the
    # Settings → Levelling picker writes them via `text.set_value`. Empty =
    # use the Sensa default artwork.
    entities.extend(
        SensaLevelImageText(hass, entry, slot) for slot in LEVEL_IMAGE_SLOTS
    )

    devices = runtime.get("coordinator").data or {}
    device_list = devices.get("devices", [])

    # Same union strategy as switch.py: take relay device_ids from the
    # coordinator snapshot AND from the persistent entity registry, so
    # the channel-name + device-name texts always exist even if the
    # backend was unreachable on HA restart. Backend role-field name
    # varies (`role` / `device_type` / `vendor_device_type` / `type`).
    relay_devices: dict[str, dict[str, Any]] = {}
    teltonika_seen: set[str] = set()
    for device in device_list:
        integration = device.get("integration_type")
        vdt = device_kind(device)
        if integration == "sensa_teltonika_api":
            entities.extend(build_teltonika_text_entities(entry, device))
            teltonika_seen.add(str(device.get("device_id") or ""))
        elif integration == "sensa_victron_mqtt":
            entities.extend(build_cerbo_text_entities(entry, device))
        elif integration == "sensa_asustor_ssh":
            entities.extend(build_asustor_text_entities(entry, device))
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
            if not uid.startswith(entry_prefix):
                continue
            tail = uid[len(entry_prefix):]
            if tail.endswith("_relay_device_name"):
                device_id = tail[:-len("_relay_device_name")]
            elif tail.endswith("_name") and "_relay_" in tail:
                core = tail[:-len("_name")]
                device_id = core.rsplit("_relay_", 1)[0]
            else:
                continue
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

    # Third source: entry.options durable ledger. See switch.py for
    # the rationale — survives backend resets / registry purges.
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

    for device_id, device in relay_devices.items():
        entities.extend(build_relay_text_entities(hass, entry, device))
        _LOGGER.info(
            "Registering relay text entities for %s "
            "(8 channel-name + 1 device-name)",
            device_id,
        )

    # Backend snapshot can be momentarily empty right after an OTA (the add-on
    # is still starting), so a router already registered may be absent from
    # `coordinator.data` on this reload. Re-create its Username/Password from
    # the registry so they don't become orphans reading `unavailable` (which
    # makes the Devices picker's Save silently no-op). Mirrors the relay union.
    for device_id, device in teltonika_devices_from_registry(hass, entry).items():
        if device_id in teltonika_seen:
            continue
        entities.extend(build_teltonika_text_entities(entry, device))

    entities.extend(_build_header_short_name_texts(hass, entry, device_list))

    # A name per fan on every Sensa Fan Temperature Control; a controller already
    # in the autopilot ledger keeps its names, as its selects and switches do.
    from .const import FAN_AUTOPILOT_OPTION, FAN_CHANNELS

    fan_ids: set[str] = set()
    for device in device_list:
        for fan_text in build_fan_name_texts(entry, device):
            fan_ids.add(fan_text._device_id)
            entities.append(fan_text)
    for device_id in entry.options.get(FAN_AUTOPILOT_OPTION) or {}:
        if device_id and device_id not in fan_ids:
            entities.extend(
                SensaFanNameText(entry, device_id, "Fan Temperature Control", ch)
                for ch in FAN_CHANNELS
            )

    # A name per circuit on every Sensa DC Meter; a meter already named keeps its
    # names even when the backend snapshot is momentarily empty.
    from .const import DC_CIRCUIT_LABELS_OPTION, DC_METER_CHANNELS

    meter_ids: set[str] = set()
    for device in device_list:
        for circuit_text in build_dc_circuit_name_texts(entry, device):
            meter_ids.add(circuit_text._device_id)
            entities.append(circuit_text)
    for device_id in entry.options.get(DC_CIRCUIT_LABELS_OPTION) or {}:
        if device_id and device_id not in meter_ids:
            entities.extend(
                SensaDcCircuitNameText(entry, device_id, "DC Meter", n)
                for n in DC_METER_CHANNELS
            )

    if entities:
        async_add_entities(entities)


def _build_header_short_name_texts(
    hass: HomeAssistant, entry: ConfigEntry, device_list: list[dict[str, Any]],
) -> list[SensaHeaderShortNameText]:
    """Every "Header Short Name" field this entry should own this turn.

    The same three-source union as the header chip switches in switch.py, for
    the same reason: the backend snapshot is empty while the add-on restarts
    after an OTA, and a config entity missing on that turn becomes an orphan
    the customer sees as an unavailable row. Sources: the live snapshot (the
    only one carrying the display name), the entity registry, then
    `entry.options` (a device with a saved short name).
    """
    from .vendors.service_classes import classify

    found: dict[str, str] = {}
    for device in device_list:
        service_class = str(device.get("service_class") or classify(device) or "")
        if not header_chip_eligible(service_class):
            continue
        device_id = str(device.get("device_id") or device.get("id") or "")
        if device_id:
            found.setdefault(
                device_id,
                str(device.get("display_name") or device.get("name") or "Device"),
            )

    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id or ent.domain != "text":
                continue
            uid = ent.unique_id or ""
            if not (uid.startswith(prefix) and uid.endswith(HEADER_SHORT_NAME_UID_SUFFIX)):
                continue
            device_id = uid[len(prefix):-len(HEADER_SHORT_NAME_UID_SUFFIX)]
            if device_id and device_id not in found:
                recovered = str(ent.original_name or "").removesuffix(
                    " Header Short Name"
                )
                found[device_id] = recovered or "Device"
    except Exception:
        _LOGGER.warning(
            "Registry walk for header short-name device_ids failed", exc_info=True,
        )

    for device_id in (entry.options.get(HEADER_SHORT_NAME_OPTION) or {}):
        device_id = str(device_id or "")
        if device_id and device_id not in found:
            found[device_id] = "Device"

    return [
        SensaHeaderShortNameText(hass, entry, device_id, name)
        for device_id, name in found.items()
    ]


def build_header_short_name_texts(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaHeaderShortNameText]:
    """The "Header Short Name" field for ONE device, or an empty list when it
    cannot put a chip in the header. For the Discovery picker's add path."""
    from .vendors.service_classes import classify

    service_class = str(device.get("service_class") or classify(device) or "")
    if not header_chip_eligible(service_class):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = str(device.get("display_name") or device.get("name") or "Device")
    return [SensaHeaderShortNameText(hass, entry, device_id, name)]


class SensaHeaderShortNameText(TextEntity):
    """Per-device "Header Short Name": the label the device's header chip uses
    instead of its display name, so shorter names fit more chips.

    Persists to `entry.options["header_short_names"][device_id]`, the same
    config-entry storage the header chip ledgers use, so it survives restarts
    and rides HA backups. Blank clears it and the chip falls back to the
    device's display name.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:form-textbox"

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device_id: str, device_name: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._attr_name = f"{device_name} Header Short Name"
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}{HEADER_SHORT_NAME_UID_SUFFIX}"
        )
        self.entity_id = header_short_name_entity_id(device_id)

    @property
    def native_value(self) -> str:
        return header_short_name(self._entry.options, self._device_id)

    async def async_set_value(self, value: str) -> None:
        name = (value or "").strip()
        names: dict[str, str] = dict(
            self._entry.options.get(HEADER_SHORT_NAME_OPTION) or {}
        )
        if name:
            names[self._device_id] = name
        else:
            names.pop(self._device_id, None)
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, HEADER_SHORT_NAME_OPTION: names},
        )
        # The header is built into the dashboard config, so rebuild for the
        # new label to reach every surface's header.
        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


def build_teltonika_text_entities(
    entry: ConfigEntry, device: dict[str, any]
) -> list[TextEntity]:
    """Build the Username + Password text entities for one Teltonika device.

    Exposed so the Discovery picker's add-dispatch can create these for a
    newly-registered Teltonika without reloading the entry.

    `backend_value` wins over RestoreEntity's cached state — the backend
    is the authoritative store for config, and a stale restore-state
    snapshot from before-the-fix was the source of the "cached password
    on re-add" bug. When the backend has nothing (fresh add), we fall
    back to the explicit initial and RestoreEntity no longer has a say.
    """
    device_id = device.get("device_id", "unknown")
    mac = device.get("linked_mac_address") or device.get("mac_address") or ""
    backend_config = device.get("config") or {}
    return [
        SensaConfigText(
            entry,
            device_id=device_id,
            mac=mac,
            key="username",
            name="Username",
            icon="mdi:account",
            initial="",
            mode=TextMode.TEXT,
            backend_value=backend_config.get("username"),
        ),
        SensaConfigText(
            entry,
            device_id=device_id,
            mac=mac,
            key="password",
            name="Password",
            icon="mdi:lock",
            initial="",
            mode=TextMode.PASSWORD,
            backend_value=backend_config.get("password"),
        ),
    ]


class SensaConfigText(RestoreEntity, TextEntity):
    """Text entity for a Sensa device configuration field."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        key: str,
        name: str,
        icon: str,
        initial: str,
        mode: TextMode,
        mac: str = "",
        backend_value: str | None = None,
        device_info: dict | None = None,
        entity_id: str | None = None,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_config_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_mode = mode
        self._attr_native_value = initial
        self._initial = initial
        # Optional deterministic entity_id (pinned-id pattern). When set the
        # Save button + dashboard card can reference the field without
        # resolving the entity registry or guessing HA's slugified name.
        if entity_id:
            self.entity_id = entity_id
        # Backend value wins over RestoreEntity — see docstring on
        # build_teltonika_text_entities. Held as the seed for
        # async_added_to_hass so HA's restore path can't override it.
        self._backend_value = backend_value
        # Callers can inject a device_info dict to attach this text
        # entity to a non-Teltonika device card (e.g. Cerbo GX). If
        # omitted, the old Teltonika default stays for back-compat.
        self._device_info_override = device_info

    async def async_added_to_hass(self) -> None:
        """Initialise from backend only — never from RestoreEntity state.

        The backend is the authoritative store for config. RestoreEntity
        persistence was causing stale passwords to come back on re-add
        after a delete, because `core.restore_state` kept the value on
        disk even when the backend row had been cleared. By explicitly
        ignoring the restore path we get deterministic behaviour:

          - Backend has a value → show it.
          - Backend has no value → show the constructor `_initial` (e.g.
            "admin" for username, "" for password).
          - Nothing else matters.

        Side-effect: if a customer types into a field and HA restarts
        before they hit Save, their typed value is lost. That's fine
        for config fields — losing unsaved text is safer than
        resurrecting stale passwords on a re-add.
        """
        if self._backend_value is not None:
            self._attr_native_value = self._backend_value
        else:
            self._attr_native_value = self._initial or ""

    async def async_set_value(self, value: str) -> None:
        """Set the text value."""
        self._attr_native_value = value
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Scrub the stored value before HA writes final state to disk.

        RestoreEntity persists the last observed state to
        `.storage/core.restore_state`. Without this scrub, a Teltonika
        password entered into a text entity survives both integration
        removal AND a reinstall — when the new entity appears with the
        same unique_id, RestoreEntity replays the old value. Clear the
        native_value and write state so the persisted entry is empty.
        """
        self._attr_native_value = ""
        try:
            self.async_write_ha_state()
        except Exception:
            _LOGGER.debug(
                "Teltonika config text %s: state write on remove failed",
                self.entity_id, exc_info=True,
            )

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


def build_camera_text_entities(
    entry: ConfigEntry, device: dict[str, Any]
) -> list[TextEntity]:
    """Stream path + Username + Password for one IP camera.

    RTSP discovery finds the camera but never its stream path or login; those
    differ per model (`stream1`, `h264Preview_01_main`, `Streaming/Channels/101`)
    and are the customer's to enter. Backend value wins, same as Teltonika.
    """
    from .const import (
        IP_CAMERA_DEFAULT_STREAM_PATH,
        IP_CAMERA_IDENTIFIER_PREFIX,
        ip_camera_config_text_entity_id,
    )

    device_id = device.get("device_id", "unknown")
    mac = device.get("linked_mac_address") or device.get("mac_address") or ""
    backend_config = device.get("config") or {}
    device_info = {
        "identifiers": {(DOMAIN, f"{IP_CAMERA_IDENTIFIER_PREFIX}{device_id}")},
        "name": device.get("display_name") or device.get("name") or "IP Camera",
        "manufacturer": device.get("vendor") or "IP Camera",
        "model": device.get("model") or "RTSP camera",
    }
    fields = (
        ("stream_path", "Stream Path", "mdi:video-input-antenna",
         IP_CAMERA_DEFAULT_STREAM_PATH, TextMode.TEXT),
        ("username", "Username", "mdi:account", "", TextMode.TEXT),
        ("password", "Password", "mdi:key", "", TextMode.PASSWORD),
    )
    return [
        SensaConfigText(
            entry,
            device_id=device_id,
            mac=mac,
            key=key,
            name=name,
            icon=icon,
            initial=initial,
            mode=mode,
            backend_value=backend_config.get(key),
            device_info=device_info,
            entity_id=ip_camera_config_text_entity_id(device_id, key),
        )
        for key, name, icon, initial, mode in fields
    ]


def build_cerbo_text_entities(
    entry: ConfigEntry, device: dict[str, any]
) -> list[TextEntity]:
    """Build the Host + Username + Password text entities for a Cerbo GX.

    Default for Host is the device's discovered IP (DHCP lease / static
    probe result) so a customer on DHCP doesn't need to type anything —
    they hit Save and the coordinator connects straight away. Blank
    credentials are valid (Venus OS "MQTT on LAN" factory-default is
    no-auth plaintext on port 1883).
    """
    device_id = device.get("device_id", "unknown")
    mac = device.get("linked_mac_address") or device.get("mac_address") or ""
    backend_config = device.get("config") or {}
    discovered_ip = str(device.get("ip_address") or "").strip()
    device_info = _cerbo_device_info(device_id, mac)
    return [
        SensaConfigText(
            entry,
            device_id=device_id,
            mac=mac,
            key="host",
            name="Host",
            icon="mdi:ip-network",
            initial=discovered_ip,
            mode=TextMode.TEXT,
            backend_value=backend_config.get("host"),
            device_info=device_info,
        ),
        SensaConfigText(
            entry,
            device_id=device_id,
            mac=mac,
            key="username",
            name="Username",
            icon="mdi:account",
            initial="",
            mode=TextMode.TEXT,
            backend_value=backend_config.get("username"),
            device_info=device_info,
        ),
        SensaConfigText(
            entry,
            device_id=device_id,
            mac=mac,
            key="password",
            name="Password",
            icon="mdi:lock",
            initial="",
            mode=TextMode.PASSWORD,
            backend_value=backend_config.get("password"),
            device_info=device_info,
        ),
    ]


def _cerbo_device_info(device_id: str, mac: str) -> dict:
    """Shared device_info block for all Cerbo GX config entities, so
    Host/Username/Password/SSL/Save all land on one device card in
    HA's Settings → Devices & Services pane."""
    last4 = mac.replace(":", "").replace("-", "").upper()[-4:] if mac else ""
    name = f"Victron Cerbo GX (#{last4})" if last4 else "Victron Cerbo GX"
    return {
        "identifiers": {(DOMAIN, f"victron_cerbo_{device_id}")},
        "name": name,
        "manufacturer": "Victron Energy",
        "model": "Cerbo GX",
    }


def _asustor_device_info(device_id: str, mac: str) -> dict:
    """Shared device_info block for all Asustor NAS entities, so the power
    switch + host/credential config + Save button all land on one device card
    in HA's Settings → Devices & Services pane.

    The `asustor_nas_` identifier prefix is registered in
    `_SENSA_DEVICE_IDENTIFIER_PREFIXES` (__init__.py) so device removal purges
    the HA device cleanly — keep the two in sync."""
    last4 = mac.replace(":", "").replace("-", "").upper()[-4:] if mac else ""
    name = f"Asustor Flashstor 6 (#{last4})" if last4 else "Asustor Flashstor 6"
    return {
        "identifiers": {(DOMAIN, f"asustor_nas_{device_id}")},
        "name": name,
        "manufacturer": "Asustor",
        "model": "Flashstor 6 FS6706T",
    }


def build_asustor_text_entities(
    entry: ConfigEntry, device: dict[str, Any]
) -> list[TextEntity]:
    """Build the Host / SSH username / SSH password / SSH key / WOL MAC text
    entities for one Asustor NAS.

    Default for Host is the device's discovered IP and WOL MAC the discovered
    MAC, so a customer on DHCP only needs to fill in SSH credentials and hit
    Save. `backend_value` (the persisted config) wins over the initial, same
    as the Teltonika / Cerbo flows. Entity IDs are pinned via the const helper
    so the Save button + Support card reference them deterministically."""
    device_id = device.get("device_id", "unknown")
    mac = device.get("linked_mac_address") or device.get("mac_address") or ""
    backend_config = device.get("config") or {}
    discovered_ip = str(device.get("ip_address") or "").strip()
    device_info = _asustor_device_info(device_id, mac)

    def _text(key, name, icon, mode, initial="") -> SensaConfigText:
        return SensaConfigText(
            entry,
            device_id=device_id,
            mac=mac,
            key=key,
            name=name,
            icon=icon,
            initial=initial,
            mode=mode,
            backend_value=backend_config.get(key),
            device_info=device_info,
            entity_id=nas_config_text_entity_id(device_id, key),
        )

    return [
        _text("host", "Host", "mdi:ip-network", TextMode.TEXT, discovered_ip),
        _text("ssh_username", "SSH Username", "mdi:account", TextMode.TEXT),
        _text("ssh_password", "SSH Password", "mdi:lock", TextMode.PASSWORD),
        _text("ssh_key", "SSH Private Key", "mdi:key-variant", TextMode.PASSWORD),
        _text("wol_mac", "Wake-on-LAN MAC", "mdi:lan", TextMode.TEXT, mac),
    ]


class SensaScanSubnetText(TextEntity):
    """Customer-editable scan subnet, shown on the Discovery view.

    Reads the persisted value from `entry.options[CONF_SCAN_SUBNET]` so it
    survives HA restarts. Writes on change both persist to options and
    update the in-memory `runtime["config"]["scan_subnet"]` so the next
    Discovery-sensor scan picks up the new value without needing a reload.

    Intentionally NOT attached to the Sensa Nomad hub device — this is a
    runtime knob surfaced through the Discovery view's picker, not a piece
    of status that belongs on the hub device page. Skipping `device_info`
    keeps it out of Settings → Devices & Services → Sensa Nomad device's
    "Configuration" section, which confused customers who hadn't entered
    it in the config flow.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Scan Subnet"
    _attr_icon = "mdi:ip-network"
    _attr_mode = TextMode.TEXT

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_scan_subnet"

    @property
    def native_value(self) -> str:
        """Current subnet — options override takes priority over the default."""
        override = self._entry.options.get(CONF_SCAN_SUBNET)
        if override:
            return override
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        return (runtime.get("config") or {}).get(CONF_SCAN_SUBNET, "")

    async def async_set_value(self, value: str) -> None:
        """Validate, persist to options, and refresh the runtime config.

        Accepts a single CIDR or a comma-separated list. Cerbos and
        other devices are often on an IoT VLAN distinct from the HA
        host, so listing multiple subnets lets one scan cover the
        whole topology without a per-device subnet field.
        """
        raw = (value or "").strip()
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            _LOGGER.warning("Scan subnet: rejected empty value %r", value)
            return
        canonical_parts: list[str] = []
        for cidr in parts:
            try:
                # strict=False accepts both 192.168.1.0/24 and 192.168.1.5/24.
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                _LOGGER.warning(
                    "Scan subnet: rejected invalid CIDR %r in %r", cidr, value,
                )
                return
            canonical_parts.append(str(network))
        canonical = ",".join(canonical_parts)

        new_options = {**self._entry.options, CONF_SCAN_SUBNET: canonical}
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options
        )

        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if runtime is not None:
            runtime.setdefault("config", {})[CONF_SCAN_SUBNET] = canonical

        _LOGGER.info("Scan subnet updated to %s", canonical)
        self.async_write_ha_state()

    # No device_info — stays unattached so it doesn't appear on the Sensa
    # Nomad hub device's Configuration pane. Lovelace references it by
    # entity_id (text.sensa_nomad_scan_subnet).


class SensaLicenceCodeText(TextEntity):
    """Redemption-code entry field for the appliance licence-recovery card.

    On an appliance the `owner` is a non-HA-admin and HA's Devices & Services
    (the only reconfigure / licence-entry path) is unreachable — so a read-only
    lockout would otherwise leave the customer with no way to enter a valid
    licence. This field holds the code the owner types; the paired
    `button.sensa_nomad_activate_licence` reads this entity's state and redeems
    it. In-memory only (a redemption code isn't worth persisting, and it's
    cleared after a successful activate); NOT a RestoreEntity and NOT
    licence-gated — recovery must work in read-only. Unattached (no device_info),
    referenced by entity_id.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Licence Code"
    _attr_icon = "mdi:key-variant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_licence_code"
        self.entity_id = LICENCE_CODE_ENTITY_ID
        self._attr_native_value = ""

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = (value or "").strip()
        self.async_write_ha_state()


class SensaSystemNameText(TextEntity):
    """The customer's name for this system / vehicle ("Bertha", "Van 3").

    HA is the source of truth: the value lives in `entry.options["system_name"]`
    so it survives restarts and travels in HA backups. On change we push it to
    the App, which persists its own copy and folds it into the phone-home
    telemetry — the App owns phone-home (its daemon runs even while HA Core is
    down), so it can't read this entity when it needs the name.

    Set at the top of the Settings → Diagnostics tab's Resources panel, on every install
    flavour. NOT licence-gated (naming your van is config, not actuation).
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad System Name"
    _attr_icon = "mdi:rv-truck"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = SYSTEM_NAME_MAX_LENGTH

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_system_name"
        self.entity_id = SYSTEM_NAME_ENTITY_ID

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> str:
        return system_name_store.get_name(self._entry)

    async def async_added_to_hass(self) -> None:
        """Re-assert the stored name to the App on setup/reload.

        Self-heal: the App keeps its own copy for phone-home, so a reinstalled
        App (or a wiped /data) would otherwise report a name HA no longer holds.
        We push UNCONDITIONALLY, including an empty name — HA is the source of
        truth, and an empty push is exactly how a cleared name reaches an App that
        was down when the customer cleared it. Skipping the empty case left the
        App reporting the deleted name to Sensa forever.
        """
        await super().async_added_to_hass()
        self.hass.async_create_task(
            system_name_store.async_push(self.hass, self._entry)
        )

    async def async_set_value(self, value: str) -> None:
        await system_name_store.async_set_name(self.hass, self._entry, value)
        self.async_write_ha_state()


def build_relay_text_entities(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]
) -> list[TextEntity]:
    """Build 1 device-name + 8 channel-name text entities for one relay
    controller.

    Exposed so the Discovery picker's add-dispatch can create these for a
    newly-registered relay device without reloading the entry.
    """
    from .device_catalog import relay_channel_count

    device_id = str(device.get("device_id") or device.get("id") or "unknown")
    device_name = device.get("display_name") or device.get("name") or "Relay"
    channels = relay_channel_count(device)
    return [
        SensaRelayDeviceNameText(hass, entry, device_id, device_name),
        *(
            SensaRelayChannelNameText(hass, entry, device_id, device_name, n)
            for n in range(1, channels + 1)
        ),
        *(
            SensaRelayChannelImageText(hass, entry, device_id, device_name, n)
            for n in range(1, channels + 1)
        ),
    ]


def build_switch_port_text_entities(
    hass: HomeAssistant, entry: ConfigEntry, device_id: str, device_name: str,
    ports: list[dict[str, Any]],
) -> list[TextEntity]:
    """One name field per network-switch port.

    Unlike a relay controller's fixed 8 channels these are built from the
    device's REPORTED port list, so the caller passes the ports it found (see
    `_async_start_teltonika`). A port with no id is skipped: the id is the
    storage key, so without one there is nothing stable to save against.
    """
    return [
        SensaSwitchPortNameText(
            hass, entry, device_id, device_name,
            str(port.get("id") or ""), str(port.get("label") or ""),
        )
        for port in ports
        if port.get("id")
    ]


class SensaSwitchPortNameText(TextEntity):
    """Editable label for one network-switch port.

    The relay-channel equivalent, so the customer can call port 5 "Starlink"
    rather than counting sockets. Persists to
    `entry.options["switch_port_labels"][device_id][port_id]` -- the same
    nested-dict-on-the-config-entry shape as `relay_labels`, so it survives
    restarts and rides HA backups without a restore-state dance.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:label-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        port_id: str,
        port_label: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._port_id = port_id
        self._attr_name = f"{device_name} {port_label or port_id} name"
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}_netsw_{port_id}_name"
        )
        self.entity_id = switch_port_name_entity_id(device_id, port_id)

    @property
    def native_value(self) -> str:
        return (
            (self._entry.options.get(SWITCH_PORT_LABELS_OPTION) or {})
            .get(self._device_id, {})
            .get(self._port_id, "")
        )

    async def async_set_value(self, value: str) -> None:
        label = (value or "").strip()

        all_labels: dict[str, Any] = dict(
            self._entry.options.get(SWITCH_PORT_LABELS_OPTION) or {}
        )
        dev_labels: dict[str, str] = dict(all_labels.get(self._device_id) or {})
        if label:
            dev_labels[self._port_id] = label
        elif self._port_id in dev_labels:
            del dev_labels[self._port_id]
        if dev_labels:
            all_labels[self._device_id] = dev_labels
        elif self._device_id in all_labels:
            del all_labels[self._device_id]

        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, SWITCH_PORT_LABELS_OPTION: all_labels},
        )

        # Rebuild so the new name reaches BOTH surfaces: the Habitation tile
        # and this Settings row.
        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )

        self.async_write_ha_state()


class SensaRelayChannelNameText(TextEntity):
    """Editable label for one relay channel.

    Persists to entry.options["relay_labels"] and triggers a dashboard
    rebuild that applies the label to the corresponding ESPHome switch
    entity via the HA entity registry.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:label-outline"

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
        self._n = n
        self._attr_name = f"{device_name} channel {n}"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_relay_{n}_name"
        self.entity_id = relay_channel_name_entity_id(device_id, n)

    @property
    def native_value(self) -> str:
        return (
            (self._entry.options.get("relay_labels") or {})
            .get(self._device_id, {})
            .get(self._slot_key, "")
        )

    async def async_set_value(self, value: str) -> None:
        label = (value or "").strip()

        all_labels: dict[str, Any] = dict(self._entry.options.get("relay_labels") or {})
        dev_labels: dict[str, str] = dict(all_labels.get(self._device_id) or {})
        if label:
            dev_labels[self._slot_key] = label
        elif self._slot_key in dev_labels:
            del dev_labels[self._slot_key]
        if dev_labels:
            all_labels[self._device_id] = dev_labels
        elif self._device_id in all_labels:
            del all_labels[self._device_id]

        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, "relay_labels": all_labels}
        )

        # Rebuild dashboards — _attach_relay_entities applies the stored label
        # to the HA entity registry as part of the rebuild.
        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )

        self.async_write_ha_state()


class SensaRelayChannelImageText(TextEntity):
    """Holds the chosen picture URL for one relay channel.

    Stores a `/local/sensa/...` URL (stock icon or an uploaded library image)
    or "" for none. Persists to entry.options["relay_images"] and triggers a
    dashboard rebuild so the Habitation Switching tile picks up the picture.
    The Settings Switching picker writes it via `text.set_value` (same commit
    path as the channel name); it's not meant to be typed by hand, hence the
    CONFIG category + hidden-friendly default.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:image-outline"
    # URLs can be long (uploaded filenames + prefix); the default 100 is plenty
    # but bump for safety.
    _attr_native_max = 255

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
        self._n = n
        self._attr_name = f"{device_name} channel {n} image"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_relay_{n}_image"
        self.entity_id = relay_channel_image_entity_id(device_id, n)

    @property
    def native_value(self) -> str:
        return (
            (self._entry.options.get("relay_images") or {})
            .get(self._device_id, {})
            .get(self._slot_key, "")
        )

    async def async_set_value(self, value: str) -> None:
        url = (value or "").strip()

        all_images: dict[str, Any] = dict(self._entry.options.get("relay_images") or {})
        dev_images: dict[str, str] = dict(all_images.get(self._device_id) or {})
        if url:
            dev_images[self._slot_key] = url
        elif self._slot_key in dev_images:
            del dev_images[self._slot_key]
        if dev_images:
            all_images[self._device_id] = dev_images
        elif self._device_id in all_images:
            del all_images[self._device_id]

        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, "relay_images": all_images}
        )

        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )

        self.async_write_ha_state()


class SensaLevelImageText(TextEntity):
    """Holds the chosen picture URL for one Cab → Levelling view (singleton).

    Stores a `/local/sensa/...` URL or "" (use the Sensa default artwork) in
    `entry.options["level_images"][slot]`, where slot is top/side/rear. The
    picture field on the levelling sensor's row (Settings → Devices) writes it
    via `text.set_value`; on change it
    triggers a dashboard rebuild so the Cab top-down / tilt cards pick up the
    new picture (same commit path + regen as the relay channel pictures).
    `EntityCategory.CONFIG` keeps it out of the Mode Editor eligibility set.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:image-outline"
    _attr_native_max = 255

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, slot: str) -> None:
        self.hass = hass
        self._entry = entry
        self._slot = slot
        self._attr_name = f"Levelling {slot} image"
        self._attr_unique_id = f"{entry.entry_id}_level_image_{slot}"
        self.entity_id = level_image_entity_id(slot)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> str:
        return (self._entry.options.get("level_images") or {}).get(self._slot, "")

    async def async_set_value(self, value: str) -> None:
        url = (value or "").strip()
        all_images: dict[str, Any] = dict(self._entry.options.get("level_images") or {})
        if url:
            all_images[self._slot] = url
        elif self._slot in all_images:
            del all_images[self._slot]

        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, "level_images": all_images},
        )

        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()


class SensaRelayDeviceNameText(TextEntity):
    """Editable display name for the relay device itself.

    Mirrors `SensaRelayChannelNameText` for the device level. On commit
    we call `sensa_nomad.rename_device`, which updates the Sensa backend
    and the HA device-registry name; the next dashboard regen re-reads
    the new display_name and the card header refreshes. The text entity
    holds the latest value locally so the input field reflects edits
    immediately, before the rename round-trip completes.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:rename"

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
        self._attr_name = f"{device_name} device name"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_relay_device_name"
        self.entity_id = relay_device_name_entity_id(device_id)
        self._attr_native_value = device_name

    @property
    def native_value(self) -> str:
        return self._attr_native_value or ""

    async def async_set_value(self, value: str) -> None:
        new_name = (value or "").strip()
        if not new_name:
            # Empty submit → reset to catalog default via the existing
            # service. Mirrors the rename pencil's reset action in the
            # Discovery picker — blank always means "no override, use
            # default". We then re-read the backend's new display_name
            # below so the input field shows what the device was reset
            # to (e.g. "Sensa Relay Controller (Wi-Fi) (#22D8)") rather
            # than going blank — earlier behaviour left the box empty
            # and the placeholder "Relay" read as the new device name,
            # confusing customers who expected to see the catalog value.
            try:
                await self.hass.services.async_call(
                    DOMAIN,
                    "reset_device_name",
                    {"id": self._device_id},
                    blocking=True,
                )
            except Exception:
                _LOGGER.warning(
                    "Relay device-name reset failed for %s",
                    self._device_id, exc_info=True,
                )
                return
            new_name = await self._async_read_backend_display_name() or ""
        else:
            try:
                await self.hass.services.async_call(
                    DOMAIN,
                    "rename_device",
                    {"id": self._device_id, "display_name": new_name},
                    blocking=True,
                )
            except Exception:
                _LOGGER.warning(
                    "Relay device-name rename failed for %s",
                    self._device_id, exc_info=True,
                )
                return
        self._attr_native_value = new_name
        self.async_write_ha_state()

    async def _async_read_backend_display_name(self) -> str | None:
        """Fetch this device's current display_name from the Sensa backend.

        Used after a reset so the input field can populate with the
        catalog-default value the rename pipeline just wrote — without
        duplicating the catalog-lookup logic on the integration side.
        Returns None if anything along the chain fails; caller treats
        that as "leave the entity blank, the card header still shows
        the right value via the dashboard regen path".
        """
        from .api.client import SensaApiClient
        from .const import CONF_BACKEND_URL

        backend_url = self._entry.data.get(CONF_BACKEND_URL)
        if not backend_url:
            return None
        try:
            client = SensaApiClient(self.hass, backend_url)
            resp = await client.async_get_devices()
        except Exception:
            _LOGGER.debug(
                "Relay device-name reset: couldn't read back fresh "
                "display_name for %s", self._device_id, exc_info=True,
            )
            return None
        for d in resp.get("devices") or []:
            if d.get("device_id") == self._device_id:
                return d.get("display_name") or None
        return None


def build_fan_name_texts(entry: ConfigEntry, device: dict[str, Any]) -> list[SensaFanNameText]:
    """One name per fan on a Sensa Fan Temperature Control, else none."""
    from .const import FAN_CHANNELS
    from .fan_autopilot import is_fan_controller

    if not is_fan_controller(device):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = device.get("display_name") or device.get("name") or "Fan Temperature Control"
    return [SensaFanNameText(entry, device_id, name, ch) for ch in FAN_CHANNELS]


class SensaFanNameText(TextEntity):
    """The customer's name for one fan ("Roof fan"). Empty means "Fan <n>".

    Read by the fan card; options-as-truth (`FAN_AUTOPILOT_OPTION`). The card
    reads the state live, so a rename needs no dashboard rebuild.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:label-outline"
    _attr_native_max = 40

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str, channel: int,
    ) -> None:
        from .const import fan_autopilot_key, fan_name_text_entity_id

        self._entry = entry
        self._device_id = device_id
        self._key = fan_autopilot_key(channel, "name")
        self._attr_name = f"{device_name} Fan {channel} Name"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{self._key}"
        self.entity_id = fan_name_text_entity_id(device_id, channel)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> str:
        from .fan_autopilot import setting

        return str(setting(self._entry, self._device_id, self._key) or "")

    async def async_set_value(self, value: str) -> None:
        from .fan_autopilot import set_setting

        set_setting(self.hass, self._entry, self._device_id, self._key, (value or "").strip())
        self.async_write_ha_state()


def build_dc_circuit_name_texts(
    entry: ConfigEntry, device: dict[str, Any],
) -> list[SensaDcCircuitNameText]:
    """One name per circuit on a Sensa DC Meter, else none."""
    from .const import DC_METER_CHANNELS
    from .dc_meter import is_dc_meter

    if not is_dc_meter(device):
        return []
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    name = device.get("display_name") or device.get("name") or "DC Meter"
    return [SensaDcCircuitNameText(entry, device_id, name, n) for n in DC_METER_CHANNELS]


class SensaDcCircuitNameText(TextEntity):
    """The customer's name for one DC meter circuit ("Fridge"). Empty means
    "Circuit <n>".

    Options-as-truth (`DC_CIRCUIT_LABELS_OPTION`), edited on the meter's
    Settings -> Devices row. The Electric tab bakes the name into its tiles,
    so a rename rebuilds the dashboards, as a relay channel rename does.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:label-outline"
    _attr_native_max = 40

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str, n: int,
    ) -> None:
        from .const import dc_circuit_name_entity_id

        self._entry = entry
        self._device_id = device_id
        self._slot_key = f"circuit_{n}"
        self._attr_name = f"{device_name} Circuit {n} Name"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_circuit_{n}_name"
        self.entity_id = dc_circuit_name_entity_id(device_id, n)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def native_value(self) -> str:
        from .const import DC_CIRCUIT_LABELS_OPTION

        return str(
            ((self._entry.options.get(DC_CIRCUIT_LABELS_OPTION) or {}).get(self._device_id) or {})
            .get(self._slot_key, "")
        )

    async def async_set_value(self, value: str) -> None:
        from .const import DC_CIRCUIT_LABELS_OPTION

        label = (value or "").strip()
        all_labels: dict[str, Any] = dict(
            self._entry.options.get(DC_CIRCUIT_LABELS_OPTION) or {}
        )
        dev_labels: dict[str, str] = dict(all_labels.get(self._device_id) or {})
        if label:
            dev_labels[self._slot_key] = label
        else:
            dev_labels.pop(self._slot_key, None)
        if dev_labels:
            all_labels[self._device_id] = dev_labels
        else:
            all_labels.pop(self._device_id, None)
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, DC_CIRCUIT_LABELS_OPTION: all_labels},
        )

        from . import _async_rebuild_sensa_dashboards
        self.hass.async_create_task(
            _async_rebuild_sensa_dashboards(self.hass, self._entry)
        )
        self.async_write_ha_state()
