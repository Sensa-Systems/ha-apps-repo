"""Button platform for Sensa Nomad."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ACTIVATE_LICENCE_ENTITY_ID,
    DEPLOYMENT_MODE_APPLIANCE,
    DEPLOYMENT_MODE_KEY,
    DOMAIN,
    LICENCE_CODE_ENTITY_ID,
    TELTONIKA_DEFAULT_SSL,
    TELTONIKA_USERNAME,
    nas_config_text_entity_id,
    nas_save_button_entity_id,
    teltonika_device_name,
    teltonika_devices_from_registry,
    van_level_calibrate_button_entity_id,
    van_level_offset_entity_id,
    van_level_orientated_sensor_entity_id,
    van_level_reset_button_entity_id,
)

_LOGGER = logging.getLogger(__name__)

# Friendly messages for the licence-recovery card's redeem failures (the App /
# cloud return these statuses). Anything unmapped falls back to the response
# message or a generic line.
_REDEEM_ERRORS: dict[str, str] = {
    "invalid_code": "That licence code wasn't recognised. Check it and try again.",
    "rebind_limit": (
        "This licence has been moved between devices too many times recently. "
        "Please contact support."
    ),
    "rate_limited": "Too many attempts. Please wait a moment and try again.",
    "cloud_unavailable": (
        "Couldn't reach the licensing service. Check the internet connection "
        "and try again."
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad buttons from a config entry."""
    runtime_data = hass.data[DOMAIN][entry.entry_id]

    # Expose the add callback so the Save Router Config button can be
    # added for Teltonika devices registered post-setup.
    runtime_data["vendor_add_button"] = async_add_entities

    # Drain anything a vendor coordinator queued before this platform set up
    # (see the same drain in sensor.py / switch.py). The Starlink dish's
    # Restart button is created from the vendor start path, which can run
    # first; without this it would be stashed in `vendor_pending_button` and
    # never read back.
    pending_buttons = runtime_data.pop("vendor_pending_button", None)

    entities: list[ButtonEntity] = [
        SensaDiscoveryScanButton(entry),
        SensaPairZigbeeButton(entry),
        SensaIntegrityCheckButton(entry),
        SensaRunSpeedtestButton(entry),
        # Appliance licence-recovery: redeems the code typed into
        # text.sensa_nomad_licence_code. Created on every install (harmless),
        # surfaced only by the appliance-only Support-tab card.
        SensaActivateLicenceButton(entry),
    ]

    # Appliance OTA "Update now" + "Check now" — CONTAINER-appliance only, like
    # the OTA toggle. On HAOS the `admin` login is a full HA admin and updates
    # the add-on via Supervisor's built-in OTA, so Sensa doesn't drive updates
    # (no panel, no auto-apply); on Container Sensa owns the whole-stack OTA.
    if hass.data.get(DOMAIN, {}).get(DEPLOYMENT_MODE_KEY) == DEPLOYMENT_MODE_APPLIANCE:
        from .appliance import is_supervised

        if not is_supervised(hass):
            entities.append(SensaApplyUpdateButton(entry))
            entities.append(SensaCheckUpdatesButton(entry))
            entities.append(SensaInstallChannelBuildButton(entry))

    # Add save button for each Teltonika / Cerbo device
    devices = runtime_data.get("coordinator").data or {}
    teltonika_seen: set[str] = set()
    for device in devices.get("devices", []):
        integration = device.get("integration_type")
        if integration == "sensa_teltonika_api":
            entities.extend(
                build_teltonika_button_entities(entry, runtime_data, device)
            )
            teltonika_seen.add(str(device.get("device_id") or ""))
        elif integration == "sensa_victron_mqtt":
            entities.extend(
                build_cerbo_button_entities(entry, runtime_data, device)
            )
        elif integration == "sensa_asustor_ssh":
            entities.extend(
                build_asustor_button_entities(entry, runtime_data, device)
            )
        if (
            device.get("service_class") == "levelling"
            or device.get("vendor_device_type") == "van_level"
        ):
            entities.extend(build_levelling_button_entities(entry, device))

    # Re-create a still-registered router's Save button from the registry when
    # the backend snapshot is momentarily empty (add-on starting after an OTA).
    # Without this the button orphans alongside the credential texts, so a
    # press does nothing and the customer's re-entered credentials never save.
    for device_id, device in teltonika_devices_from_registry(hass, entry).items():
        if device_id in teltonika_seen:
            continue
        entities.extend(
            build_teltonika_button_entities(entry, runtime_data, device)
        )

    # "Tank is full now" for every Mopeka tank sensor (`mopeka.py`).
    from .mopeka import build_buttons as build_mopeka_buttons

    for device in devices.get("devices", []):
        entities.extend(build_mopeka_buttons(entry, device))

    if pending_buttons:
        entities.extend(pending_buttons)

    async_add_entities(entities)


def build_levelling_button_entities(
    entry: ConfigEntry, device: dict[str, Any]
) -> list[ButtonEntity]:
    """Reset-offsets + calibrate-now buttons for one van-level device.

    Exposed so the discovery picker's add-dispatch can create these for a
    levelling sensor registered after initial setup.
    """
    device_id = str(device.get("device_id") or device.get("id") or "")
    if not device_id:
        return []
    display = device.get("display_name") or device.get("name") or "Van Level"
    return [
        SensaLevellingResetOffsetsButton(entry, device_id, display),
        SensaLevellingCalibrateNowButton(entry, device_id, display),
    ]


def build_teltonika_button_entities(
    entry: ConfigEntry, runtime_data: dict[str, Any], device: dict[str, Any]
) -> list[ButtonEntity]:
    """Build the Save Router Config button for one Teltonika device.

    Exposed so the Discovery picker's add-dispatch can create this for a
    newly-registered Teltonika without reloading the entry.
    """
    return [SensaSaveRouterConfigButton(entry, runtime_data, device)]


def build_cerbo_button_entities(
    entry: ConfigEntry, runtime_data: dict[str, Any], device: dict[str, Any]
) -> list[ButtonEntity]:
    """Build the Save Cerbo Config button for one Cerbo GX."""
    return [SensaSaveCerboConfigButton(entry, runtime_data, device)]


def build_camera_button_entities(
    entry: ConfigEntry, runtime_data: dict[str, Any], device: dict[str, Any]
) -> list[ButtonEntity]:
    """Build the Apply button for one IP camera."""
    return [SensaApplyCameraButton(entry, runtime_data, device)]


def build_asustor_button_entities(
    entry: ConfigEntry, runtime_data: dict[str, Any], device: dict[str, Any]
) -> list[ButtonEntity]:
    """Build the Save NAS Config button for one Asustor NAS."""
    return [SensaSaveNasConfigButton(entry, runtime_data, device)]


class SensaDiscoveryScanButton(ButtonEntity):
    """Trigger a full-sources Discovery scan.

    Intentionally NOT attached to the Sensa Nomad hub device — like the
    scan-subnet text entity, this is a runtime knob driven from the
    Discovery picker, not status that belongs on the hub device's
    Controls section. Lovelace references it by entity_id
    (`button.sensa_nomad_scan`).
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Scan"
    _attr_icon = "mdi:magnify-scan"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_discovery_scan"

    async def async_press(self) -> None:
        """Force the unified discovery sensor to refresh immediately.

        Blocking on update_entity so the caller (the picker card's Scan
        button) knows exactly when the sensor finished refreshing. Without
        this, the frontend spinner disappears in <100 ms while the actual
        scan is still running, making it look like nothing happened.
        """
        import asyncio

        from homeassistant.helpers.entity_registry import (
            async_get as async_get_entity_registry,
        )

        from . import async_reload_gateway_devices

        ent_reg = async_get_entity_registry(self.hass)
        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{self._entry.entry_id}_discovery"
        )
        if not entity_id:
            _LOGGER.warning("Discovery scan: discovery sensor not found")
            return
        # One Scan does both: the network discovery refresh AND an in-place
        # rescan of every registered gateway (Cerbo GX) for devices plugged in
        # or unplugged behind it. Concurrent, so the spinner covers the longer
        # of the two; the gateway rescan never removes or re-adds the gateway.
        await asyncio.gather(
            self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": entity_id},
                blocking=True,
            ),
            async_reload_gateway_devices(self.hass, self._entry),
        )


class SensaActivateLicenceButton(ButtonEntity):
    """Redeem the code typed into ``text.sensa_nomad_licence_code``.

    The appliance licence-recovery path. On an appliance the ``owner`` is a
    non-HA-admin and HA's Devices & Services (the only reconfigure / licence
    entry) is unreachable — so a read-only lockout would otherwise leave the
    customer no way to enter a valid licence. This button (the config-card's
    save button on the appliance Support tab) redeems the entered code
    server-side and refreshes the licence, so the lockout clears at once on
    success. **NOT licence-gated** — recovery must work while read-only;
    reachability is the gate (the card is on the owner-only Settings panel).
    Errors are raised so the UI shows a real message, never a silent no-op.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Activate Licence"
    _attr_icon = "mdi:key-plus"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_activate_licence"
        self.entity_id = ACTIVATE_LICENCE_ENTITY_ID

    async def async_press(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        state = self.hass.states.get(LICENCE_CODE_ENTITY_ID)
        code = ((state.state if state else "") or "").strip()
        if not code:
            raise HomeAssistantError("Enter a licence code first.")

        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        client = runtime.get("client")
        if client is None:
            raise HomeAssistantError(
                "Sensa Nomad isn't ready yet — try again in a moment."
            )

        try:
            from homeassistant.helpers import instance_id

            ha_uuid = await instance_id.async_get(self.hass)
        except Exception:  # noqa: BLE001 — ha_uuid is optional for redeem
            ha_uuid = None

        try:
            resp = await client.async_post_license_redeem(code=code, ha_uuid=ha_uuid)
        except Exception as exc:
            raise HomeAssistantError(
                f"Couldn't reach the licensing service: {exc}"
            ) from exc

        resp = resp or {}
        status = resp.get("status")
        if status != "activated":
            # Prefer a friendly, mapped message. Only fall back to the server's
            # own `message` when it's a real, non-empty STRING — a stray numeric
            # or odd field must never reach the customer as a bare value (the
            # "activate just showed '3'" report). Otherwise show a clear sentence
            # that carries the status/HTTP code for support triage.
            friendly = _REDEEM_ERRORS.get(status)
            if not friendly:
                raw = resp.get("message")
                if isinstance(raw, str) and raw.strip():
                    friendly = raw.strip()
            if not friendly:
                detail = status or resp.get("http_status")
                friendly = "That licence code couldn't be activated" + (
                    f" ({detail})." if detail else "."
                )
            # Routine rejections (a mistyped code, the rate cap) are expected and
            # already carry a mapped message — log those at DEBUG. Reserve WARNING
            # for an UNEXPECTED status, matching config_flow's altitude so the log
            # isn't flooded with benign retry noise.
            if status in _REDEEM_ERRORS:
                _LOGGER.debug("Licence redeem rejected (%s): %s", status, resp)
            else:
                _LOGGER.warning("Licence redeem rejected: %s", resp)
            raise HomeAssistantError(friendly)

        # Success — refresh the licence status so a read-only lockout clears now
        # (local import avoids the const→__init__→button import cycle), then wipe
        # the entered code from the field.
        from . import _async_refresh_licence_status

        await _async_refresh_licence_status(self.hass, self._entry.entry_id, client)
        await self.hass.services.async_call(
            "text",
            "set_value",
            {"entity_id": LICENCE_CODE_ENTITY_ID, "value": ""},
            blocking=False,
        )
        _LOGGER.info("Licence redeemed via the appliance recovery card")


class SensaApplyUpdateButton(ButtonEntity):
    """Apply a pending Sensa App update on demand — the appliance "Update now".

    A locked-down appliance owner can't reach HA's Add-ons page, so this drives
    the same snapshot-then-apply the auto-update path uses, forcing past the
    pause toggle and the cooldown (the customer asked for it explicitly). Only
    available when a forward update is actually pending — a rollout-group pin, or
    the channel repo has advanced. Appliance-only; created only on appliances."""

    _attr_has_entity_name = True
    _attr_name = "Update now"
    _attr_icon = "mdi:download"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ota_apply_update"
        self.entity_id = "button.sensa_nomad_apply_update"

    @property
    def available(self) -> bool:
        from . import ota_update_pending

        return ota_update_pending(self.hass, self._entry.entry_id)

    async def async_press(self) -> None:
        from . import async_apply_ota_now

        await async_apply_ota_now(self.hass, self._entry.entry_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaInstallChannelBuildButton(ButtonEntity):
    """Put this box onto its release channel's CURRENT build. Sensa staff only.

    Same snapshot-then-apply as "Update now" (it calls the identical function, so
    there is one apply path, not two), but without the forward-only gate. That
    gate is right for the customer and wrong here: change a box's channel and the
    new channel's current build is often OLDER than what is running, so nothing
    is "pending", Update now is correctly greyed out, and the box keeps running a
    build from the channel it just left, which is the whole reason the channel
    was changed. Surfaced only on the Settings → Sensa tab (`can_sensa`), beside
    the channel picker that creates the situation.

    The version installed is whatever the box's own channel pin points at; this
    button carries no version and cannot name one."""

    _attr_has_entity_name = True
    _attr_name = "Install channel build"
    _attr_icon = "mdi:source-branch-sync"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ota_install_channel_build"
        self.entity_id = "button.sensa_nomad_install_channel_build"

    @property
    def available(self) -> bool:
        from . import ota_channel_install_available

        return ota_channel_install_available(self.hass, self._entry.entry_id)

    async def async_press(self) -> None:
        from . import async_apply_ota_now

        await async_apply_ota_now(self.hass, self._entry.entry_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaCheckUpdatesButton(ButtonEntity):
    """Refresh the latest-version figure on demand — the Admin card's "Check
    now". On a container the App signals the updater sidecar to re-resolve the
    channel tag (~5s tick) and the integration re-polls shortly after, so the
    Latest row updates in seconds instead of the 10-min cadence; under
    Supervisor the signal is a no-op and the re-poll alone refreshes the
    Supervisor-maintained figure. Appliance-only; created only on appliances."""

    _attr_has_entity_name = True
    _attr_name = "Check for Updates"
    _attr_icon = "mdi:refresh"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ota_check_updates"
        self.entity_id = "button.sensa_nomad_check_updates"

    async def async_press(self) -> None:
        from . import async_check_updates_now

        await async_check_updates_now(self.hass, self._entry.entry_id)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa Systems",
            "model": "Nomad Controller",
        }


class SensaPairZigbeeButton(ButtonEntity):
    """Open a Zigbee2MQTT pairing window from Nomad.

    Pressing this fires the `sensa_nomad.pair_zigbee` service, which opens Z2M's
    permit-join window (so a Zigbee end-device joins when the customer presses
    its physical pair button — no trip into the Z2M add-on) and then auto-
    registers whatever joins into the Sensa backend, so a freshly-paired device
    lands in the Discovery picker's *Added* group with no manual add step. All
    the logic lives server-side in `_async_pair_zigbee`. Like the Scan button,
    this is a runtime knob driven from the Discovery picker, not status on the
    hub device.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Pair Zigbee"
    _attr_icon = "mdi:zigbee"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_pair_zigbee"
        self.entity_id = "button.sensa_nomad_pair_zigbee"

    async def async_press(self) -> None:
        await self.hass.services.async_call(
            DOMAIN, "pair_zigbee", {}, blocking=True,
        )


class SensaSaveRouterConfigButton(ButtonEntity):
    """Button that reads config entities and saves router credentials."""

    _attr_has_entity_name = True
    _attr_name = "Save Router Config"
    _attr_icon = "mdi:content-save"

    def __init__(
        self, entry: ConfigEntry, runtime_data: dict[str, Any], device: dict[str, Any]
    ) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._device_id = device.get("device_id", "")
        self._device = dict(device)
        self._mac = device.get("linked_mac_address") or device.get("mac_address") or ""
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_save_config"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"teltonika_{self._device_id}")},
            "name": teltonika_device_name(self._mac),
            "manufacturer": "Teltonika",
            "model": "RutOS Device",
        }

    async def async_press(self) -> None:
        """Read config entity states and save to backend.

        Credentials are resolved by unique_id via the entity registry, NOT the
        MAC-derived slug: the slug diverges if HA slugified the entity_id
        differently (device rename, slug collision), and the unique_id lookup
        also finds the entity when it was rebuilt from the registry after an
        OTA. A missing/blank credential RAISES so the picker surfaces a real
        error instead of collapsing the pane as if the save succeeded.
        """
        from homeassistant.exceptions import HomeAssistantError
        from homeassistant.helpers import entity_registry as er

        hass = self.hass
        ent_reg = er.async_get(hass)

        def _config_entity_id(domain: str, key: str) -> str | None:
            uid = f"{self._entry.entry_id}_{self._device_id}_config_{key}"
            return ent_reg.async_get_entity_id(domain, DOMAIN, uid)

        password_eid = _config_entity_id("text", "password")
        ssl_eid = _config_entity_id("switch", "ssl")

        username = TELTONIKA_USERNAME
        password = _get_state(hass, password_eid) if password_eid else None
        ssl_state = hass.states.get(ssl_eid) if ssl_eid else None
        ssl = ssl_state.state == "on" if ssl_state else TELTONIKA_DEFAULT_SSL

        _LOGGER.info(
            "Save config: device_id=%s username=%s password=%s ssl=%s",
            self._device_id,
            "set" if username else "NONE",
            "set" if password else "NONE",
            ssl,
        )

        if not password:
            raise HomeAssistantError(
                "Enter the router's admin password first."
            )

        # Log in before saving, so a wrong password is said here, beside the
        # box, and never stored. One attempt per press: the router bans an
        # address after ten failed logins.
        from . import _build_teltonika_client
        from .vendors.teltonika_api import TeltonikaAuthError, TeltonikaConnectionError

        trial = dict(self._device)
        trial["config"] = {
            **(trial.get("config") or {}),
            "username": username, "password": password, "ssl": ssl,
        }
        client = _build_teltonika_client(hass, trial)
        if client is None:
            raise HomeAssistantError("Sensa does not know the router's address yet.")
        try:
            await client.async_login()
        except TeltonikaAuthError as err:
            # The hint under the box already says where a new router's is.
            raise HomeAssistantError("Wrong admin password.") from err
        except TeltonikaConnectionError as err:
            raise HomeAssistantError(
                f"Could not reach the router at {client._host}."
            ) from err

        await hass.services.async_call(
            DOMAIN,
            "configure_device",
            {
                "device_id": self._device_id,
                "config": {
                    "username": username,
                    "password": password,
                    "ssl": ssl,
                },
            },
            blocking=True,
        )


class SensaApplyCameraButton(ButtonEntity):
    """Apply an IP camera's stream path and login.

    Reads the three config text entities (by unique_id, the Teltonika rule),
    persists them on the device record, rewrites Frigate's config and restarts
    it, and drops the camera entity's running stream so the next play reads
    the new source. Nothing here needs an entry reload.
    """

    _attr_has_entity_name = True
    _attr_name = "Apply Camera"
    _attr_icon = "mdi:content-save"

    def __init__(
        self, entry: ConfigEntry, runtime_data: dict[str, Any], device: dict[str, Any]
    ) -> None:
        from .const import IP_CAMERA_IDENTIFIER_PREFIX, ip_camera_save_button_entity_id

        self._entry = entry
        self._runtime_data = runtime_data
        self._device = dict(device)
        self._device_id = device.get("device_id", "unknown")
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_save_config"
        self.entity_id = ip_camera_save_button_entity_id(self._device_id)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{IP_CAMERA_IDENTIFIER_PREFIX}{self._device_id}")},
            "name": device.get("display_name") or device.get("name") or "IP Camera",
        }

    async def async_press(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from homeassistant.helpers import entity_registry as er

        from . import _async_apply_ip_camera_config

        hass = self.hass
        ent_reg = er.async_get(hass)

        def _text(key: str) -> str | None:
            uid = f"{self._entry.entry_id}_{self._device_id}_config_{key}"
            eid = ent_reg.async_get_entity_id("text", DOMAIN, uid)
            return _get_state(hass, eid) if eid else None

        config = {
            "stream_path": (_text("stream_path") or "").strip().lstrip("/"),
            "username": (_text("username") or "").strip(),
            "password": _text("password") or "",
        }
        if not config["stream_path"]:
            raise HomeAssistantError("Enter the camera's stream path before applying.")
        _LOGGER.info(
            "Apply camera: device_id=%s stream_path=%s username=%s",
            self._device_id, config["stream_path"],
            "set" if config["username"] else "NONE",
        )
        await _async_apply_ip_camera_config(hass, self._entry, self._device_id, config)


class SensaSaveCerboConfigButton(ButtonEntity):
    """Button that reads Cerbo config entities and saves to the backend.

    Mirrors `SensaSaveRouterConfigButton` — the entity lookup prefix is
    derived from the device card's name (Victron Cerbo GX (#xxxx)) so
    the button finds its sibling text/switch entities dynamically. On
    press it fires the `configure_device` service with host / username
    / password / ssl, which:

      1. Persists the config to the Sensa backend.
      2. Stops any existing Cerbo coordinator (cleanly disconnects the
         aiomqtt session).
      3. Starts a new coordinator with the fresh values — next message
         from the broker populates the Cerbo's entities live.
    """

    _attr_has_entity_name = True
    _attr_name = "Save Cerbo Config"
    _attr_icon = "mdi:content-save"

    def __init__(
        self, entry: ConfigEntry, runtime_data: dict[str, Any], device: dict[str, Any],
    ) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._device_id = device.get("device_id", "")
        self._mac = device.get("linked_mac_address") or device.get("mac_address") or ""
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_save_config"
        # Entity-ID prefix mirrors the device card's name — HA slugifies
        # the card name into entity IDs like `text.victron_cerbo_gx_xxxx_host`.
        last4 = self._mac.replace(":", "").replace("-", "").upper()[-4:] if self._mac else ""
        card_name = f"Victron Cerbo GX (#{last4})" if last4 else "Victron Cerbo GX"
        self._entity_prefix = card_name.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("#", "")

    @property
    def device_info(self) -> dict[str, Any]:
        last4 = self._mac.replace(":", "").replace("-", "").upper()[-4:] if self._mac else ""
        name = f"Victron Cerbo GX (#{last4})" if last4 else "Victron Cerbo GX"
        return {
            "identifiers": {(DOMAIN, f"victron_cerbo_{self._device_id}")},
            "name": name,
            "manufacturer": "Victron Energy",
            "model": "Cerbo GX",
        }

    async def async_press(self) -> None:
        hass = self.hass
        host = _get_state(hass, f"text.{self._entity_prefix}_host")
        username = _get_state(hass, f"text.{self._entity_prefix}_username")
        password = _get_state(hass, f"text.{self._entity_prefix}_password")
        ssl_state = hass.states.get(f"switch.{self._entity_prefix}_ssl")
        ssl = ssl_state.state == "on" if ssl_state else False

        _LOGGER.info(
            "Save Cerbo config: host=%s user=%s pass=%s ssl=%s",
            host or "<missing>",
            "set" if username else "blank",
            "set" if password else "blank",
            ssl,
        )

        if not host:
            _LOGGER.warning("Cerbo host is empty — cannot save")
            return

        await hass.services.async_call(
            DOMAIN,
            "configure_device",
            {
                "device_id": self._device_id,
                "config": {
                    "host": host,
                    "username": username or "",
                    "password": password or "",
                    "ssl": ssl,
                },
            },
            blocking=True,
        )


class SensaSaveNasConfigButton(ButtonEntity):
    """Button that reads the Asustor NAS config entities and saves them.

    Reads the host + SSH credentials + WOL MAC text entities by their
    deterministic entity_ids (the pinned-id pattern, so no slugified-name
    derivation), then fires `configure_device`, which persists the config to
    the backend and rebinds the NAS switch's client so the new credentials take
    effect without a reload.
    """

    _attr_has_entity_name = True
    _attr_name = "Save NAS Config"
    _attr_icon = "mdi:content-save"

    def __init__(
        self, entry: ConfigEntry, runtime_data: dict[str, Any], device: dict[str, Any],
    ) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._device_id = str(device.get("device_id") or "")
        self._mac = device.get("linked_mac_address") or device.get("mac_address") or ""
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_save_config"
        self.entity_id = nas_save_button_entity_id(self._device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        from .text import _asustor_device_info
        return _asustor_device_info(self._device_id, self._mac)

    async def async_press(self) -> None:
        hass = self.hass
        did = self._device_id

        def _field(key: str) -> str:
            return _get_state(hass, nas_config_text_entity_id(did, key)) or ""

        host = _field("host")
        ssh_username = _field("ssh_username")
        ssh_password = _field("ssh_password")
        ssh_key = _field("ssh_key")
        wol_mac = _field("wol_mac")

        _LOGGER.info(
            "Save NAS config: host=%s user=%s pass=%s key=%s wol_mac=%s",
            host or "<missing>",
            "set" if ssh_username else "blank",
            "set" if ssh_password else "blank",
            "set" if ssh_key else "blank",
            wol_mac or "blank",
        )

        if not host:
            _LOGGER.warning("NAS host is empty — cannot save")
            return

        await hass.services.async_call(
            DOMAIN,
            "configure_device",
            {
                "device_id": did,
                "config": {
                    "host": host,
                    "ssh_username": ssh_username,
                    "ssh_password": ssh_password,
                    "ssh_key": ssh_key,
                    "wol_mac": wol_mac,
                },
            },
            blocking=True,
        )


class SensaIntegrityCheckButton(ButtonEntity):
    """Trigger SensaIntegritySensor to recheck all file checksums."""

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Integrity Check"
    _attr_icon = "mdi:shield-check"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_integrity_check"

    async def async_press(self) -> None:
        # Recheck by calling the integrity sensor INSTANCE directly (stashed in
        # runtime data), not via `homeassistant.update_entity`. The old service
        # round-trip silently no-op'd when the entity_id didn't resolve in the
        # state machine — the direct call can't hit that failure mode.
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        sensor = runtime.get("integrity_sensor")
        if sensor is None:
            _LOGGER.warning("Integrity check: integrity sensor not available")
            return
        await sensor.async_run_check()


class SensaRunSpeedtestButton(ButtonEntity):
    """Run an on-demand internet speed test.

    The Cab → Network card's "Run speed test" control. Calls the
    `sensa_nomad.run_speedtest` service, which asks the add-on to run a real
    download/upload test on a background thread; the figures land on the next
    network-status poll. On-demand only — a test burns mobile data.
    """

    _attr_has_entity_name = True
    _attr_name = "Run Speed Test"
    _attr_icon = "mdi:speedometer"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_run_speedtest"
        self.entity_id = "button.sensa_nomad_run_speedtest"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_press(self) -> None:
        await self.hass.services.async_call(
            DOMAIN, "run_speedtest", {}, blocking=True,
        )


def _get_state(hass: HomeAssistant, entity_id: str) -> str | None:
    """Get entity state, returning None for unavailable/unknown."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    return state.state


class SensaLevellingResetOffsetsButton(ButtonEntity):
    """Reset both pitch and roll calibration offsets back to 0°."""

    _attr_has_entity_name = True
    _attr_name = "Reset Offsets"
    _attr_icon = "mdi:backup-restore"

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._device_name = device_name
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}_reset_offsets"
        )
        self.entity_id = van_level_reset_button_entity_id(device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"van_level_{self._device_id}")},
            "name": self._device_name,
            "manufacturer": "Sensa",
            "model": "Van Level",
        }

    async def async_press(self) -> None:
        for axis in ("pitch", "roll"):
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": van_level_offset_entity_id(
                        self._device_id, axis,
                    ),
                    "value": 0.0,
                },
                blocking=True,
            )


class SensaLevellingCalibrateNowButton(ButtonEntity):
    """Copy the current orientated pitch/roll values into the offset numbers
    so the calibrated reading drops to ~0° in one tap.

    The customer parks the vehicle on a known-level surface and presses this
    to teach the system what "level" looks like in their installation.
    """

    _attr_has_entity_name = True
    _attr_name = "Calibrate"
    _attr_icon = "mdi:target"

    def __init__(
        self, entry: ConfigEntry, device_id: str, device_name: str
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._device_name = device_name
        self._attr_unique_id = (
            f"{entry.entry_id}_{device_id}_calibrate_now"
        )
        self.entity_id = van_level_calibrate_button_entity_id(device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"van_level_{self._device_id}")},
            "name": self._device_name,
            "manufacturer": "Sensa",
            "model": "Van Level",
        }

    async def async_press(self) -> None:
        for axis in ("pitch", "roll"):
            current = self.hass.states.get(
                van_level_orientated_sensor_entity_id(self._device_id, axis)
            )
            if current is None:
                _LOGGER.warning(
                    "Calibrate: orientated %s sensor missing for %s",
                    axis, self._device_id,
                )
                continue
            try:
                value = float(current.state)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Calibrate: orientated %s is %r — cannot calibrate",
                    axis, current.state,
                )
                continue
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": van_level_offset_entity_id(
                        self._device_id, axis,
                    ),
                    "value": value,
                },
                blocking=True,
            )
