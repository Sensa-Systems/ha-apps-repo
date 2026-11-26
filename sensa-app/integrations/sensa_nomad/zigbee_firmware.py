"""Firmware of Zigbee devices, offered only when Sensa has approved it.

Zigbee2MQTT can update a Zigbee device's own firmware, and out of the box it
checks Koenkk's public firmware list by itself and offers whatever is newest. A
customer box showed a SONOFF SNZB-01M "Update available 4101 -> 4352" that
nobody at Sensa had looked at (2026-11-01). This module puts that behind Sensa:

  * `zigbee_firmware.json` is the approved list, bundled here, so approving a
    firmware is a commit and reaches boxes through the normal channel rollout.
  * The seeder writes it into Z2M's data directory as `OTA_INDEX_FILE` and sets
    `ota.disable_automatic_update_check` (Z2M stops checking on its own) and
    `ota.zigbee_ota_override_index_location` (the same file).
  * Every check and every install is sent with `url: OTA_INDEX_FILE`. Z2M then
    uses that file INSTEAD of the public list (zigbee-herdsman `getOtaIndex`:
    `source.url` replaces the main index), so a device with no approved entry is
    told there is nothing, and a stale "available" from before is cleared.
  * The override location covers a plain request with no `url` (HA's own
    Install on Z2M's entity, the Z2M page): herdsman reads the override index
    first and `images.find` takes the first match, so an approved device still
    lands the approved file.

One `update` entity per paired device Z2M says supports OTA AND that has an
approved entry, mirroring Z2M's own update entity for the version and progress.
It is the row in Settings, System, Software updates, installed through the same
one-by-one Update now as every other device. Manual only: a battery device has
to be woken by hand, so there is no automatic install.

Checked against Zigbee2MQTT 2.14.2 / zigbee-herdsman 10.10.0 source.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, SENSA_LOG_SOURCE_SYSTEM, Z2M_BASE_TOPIC

_LOGGER = logging.getLogger(__name__)

# The `kind` the Hub matches this row on.
ZIGBEE_FIRMWARE_KIND = "zigbee"

# The approved list as Z2M reads it, in Z2M's data directory. Relative, so Z2M
# resolves it against its own data dir whatever that is mounted as; it must end
# in `.json`, which is how herdsman tells an index from a firmware file.
OTA_INDEX_FILE = "sensa-ota-index.json"

# Sensa's own key on an approved entry, stripped before Z2M sees the file.
_MODEL_KEY = "sensa_model"

Z2M_OTA_CHECK_TOPIC = f"{Z2M_BASE_TOPIC}/bridge/request/device/ota_update/check"
Z2M_OTA_UPDATE_TOPIC = f"{Z2M_BASE_TOPIC}/bridge/request/device/ota_update/update"
Z2M_OTA_UPDATE_RESPONSE_TOPIC = (
    f"{Z2M_BASE_TOPIC}/bridge/response/device/ota_update/update"
)

# A device is checked at most this often; the Check now button skips it.
_CHECK_EVERY = 24 * 3600
# Z2M answers a check per device in turn and a sleeping device holds it for its
# 60 s notify wait, so the requests are spaced rather than fired at once.
_CHECK_SPACING = 2

# Z2M waits 60 s for a device to answer the start of an update, and a battery
# device only answers while awake. The install keeps asking for this long, so
# the customer has time to press the device.
_WAKE_WINDOW = 5 * 60
# One transfer: ~190 kB in 50-byte blocks with a 250 ms gap is about 16 minutes.
# Inside the Settings card's 45-minute per-device limit.
_INSTALL_TIMEOUT = 40 * 60


@lru_cache(maxsize=1)
def approved_firmware() -> tuple[dict[str, Any], ...]:
    """The bundled approved list. Empty (never a raise) if the file is unreadable."""
    path = Path(__file__).with_name("zigbee_firmware.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable list approves nothing
        _LOGGER.warning("zigbee firmware: %s unreadable, nothing approved", path.name)
        return ()
    entries = data.get("firmware") if isinstance(data, dict) else None
    return tuple(e for e in (entries or []) if isinstance(e, dict))


def approved_for_model(model: str) -> list[dict[str, Any]]:
    """Approved entries offered to devices of this Z2M definition model."""
    model = str(model or "").strip()
    if not model:
        return []
    return [e for e in approved_firmware() if str(e.get(_MODEL_KEY) or "") == model]


def ota_index_text() -> str:
    """The approved list as a Z2M OTA index (Sensa's keys removed)."""
    index = [
        {k: v for k, v in entry.items() if k != _MODEL_KEY}
        for entry in approved_firmware()
    ]
    return json.dumps(index, indent=2) + "\n"


async def async_write_ota_index(hass: Any, data_path: str) -> bool:
    """Write `OTA_INDEX_FILE` into Z2M's data dir. True when it changed.

    Always written, `[]` when nothing is approved: the config names the file as
    the override index, and Z2M fails every check if that file is missing. Z2M
    reads it on every check, so a changed list needs no restart.
    """
    target = Path(data_path) / OTA_INDEX_FILE
    text = ota_index_text()

    def _write() -> bool:
        try:
            if target.read_text(encoding="utf-8") == text:
                return False
        except FileNotFoundError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return True

    try:
        changed = await hass.async_add_executor_job(_write)
    except Exception:
        _LOGGER.warning("zigbee firmware: could not write %s", target, exc_info=True)
        return False
    if changed:
        _LOGGER.info(
            "zigbee firmware: wrote %d approved image(s) to %s",
            len(approved_firmware()), target,
        )
    return changed


def _version(value: Any) -> str | None:
    """A Z2M file version as a string of digits, or None (Z2M sends `None`)."""
    text = str(value if value is not None else "").strip()
    return text if text.isdigit() else None


def is_newer(candidate: str | None, installed: str | None) -> bool:
    """Forward-only, on Z2M's integer file versions."""
    if not candidate or not installed:
        return False
    return int(candidate) > int(installed)


def _ota_devices(hass: Any) -> list[dict[str, Any]]:
    """Every enabled, interviewed bridge device Z2M says supports OTA."""
    from .vendors.zigbee2mqtt import bridge_devices

    out = []
    for dev in bridge_devices(hass).values():
        definition = dev.get("definition")
        if not isinstance(definition, dict) or not definition.get("supports_ota"):
            continue
        if dev.get("disabled") or not str(dev.get("ieee_address") or ""):
            continue
        out.append(dev)
    return out


async def _publish(hass: Any, topic: str, payload: dict[str, Any]) -> None:
    await hass.services.async_call(
        "mqtt", "publish",
        {"topic": topic, "payload": json.dumps(payload)},
        blocking=True,
    )


# Per-device time of the last check, on hass.data so a reload starts clean.
_LAST_CHECK_KEY = f"{DOMAIN}_zigbee_fw_checked"


async def async_check(hass: Any, *, force: bool = False) -> int:
    """Ask Z2M about every OTA device, against the approved list only.

    Every OTA device, not only those with an approved entry: the answer for an
    unapproved one is "nothing", and that is what clears an update Z2M found on
    its own before this shipped. The answer lands on the device's state topic,
    which Z2M's update entity (and so ours) reads. Returns how many were asked.
    """
    if not hass.services.has_service("mqtt", "publish"):
        return 0
    checked: dict[str, float] = hass.data.setdefault(_LAST_CHECK_KEY, {})
    now = time.monotonic()
    asked = 0
    for dev in _ota_devices(hass):
        raw = str(dev["ieee_address"])
        last = checked.get(raw)
        if not force and last is not None and now - last < _CHECK_EVERY:
            continue
        try:
            await _publish(hass, Z2M_OTA_CHECK_TOPIC, {"id": raw, "url": OTA_INDEX_FILE})
        except Exception:
            _LOGGER.debug("zigbee firmware: check request for %s failed", raw, exc_info=True)
            continue
        checked[raw] = now
        asked += 1
        await asyncio.sleep(_CHECK_SPACING)
    if asked:
        _LOGGER.debug("zigbee firmware: asked Z2M to check %d device(s)", asked)
    return asked


def z2m_update_entity_id(hass: Any, raw_ieee: str) -> str | None:
    """Z2M's own `update.*` entity for this device, from its unique_id.

    Z2M mints `<ieee>_<object_id>_<base_topic>` (homeassistant.ts), so the
    firmware entity is `0x..._update_zigbee2mqtt`.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001 - no registry is no entity
        return None
    unique_id = f"{raw_ieee}_update_{Z2M_BASE_TOPIC}"
    return ent_reg.async_get_entity_id("update", "mqtt", unique_id)


class SensaZigbeeFirmware(UpdateEntity):
    """One Zigbee device's firmware, offered only from the approved list."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )
    _attr_should_poll = False

    def __init__(
        self, entry_id: str, raw_ieee: str, name: str, model: str,
        *, battery: bool = False,
    ) -> None:
        self._entry_id = entry_id
        self._battery = battery
        self._raw_ieee = raw_ieee
        self._model = model
        self._display_name = name
        self._attr_unique_id = f"{entry_id}_zigbee_fw_{raw_ieee}_firmware"
        self._attr_name = f"{name} Firmware"
        self._installing = False
        # True from the press until the device answers: the card tells the
        # customer to wake a battery device.
        self._waking = False
        self._z2m_entity: str | None = None
        self._unsub_state: Any = None

    # -- Z2M's entity, mirrored ---------------------------------------------

    async def async_added_to_hass(self) -> None:
        self.bind()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None

    @callback
    def bind(self) -> None:
        """Follow Z2M's update entity once it exists. Retried on every sync."""
        if self._unsub_state is not None or self.hass is None:
            return
        entity_id = z2m_update_entity_id(self.hass, self._raw_ieee)
        if not entity_id:
            return
        from homeassistant.helpers.event import async_track_state_change_event

        self._z2m_entity = entity_id

        @callback
        def _changed(_event: Any) -> None:
            if self._waking and self._z2m_attr("in_progress") is True:
                self._waking = False
            self.async_write_ha_state()

        self._unsub_state = async_track_state_change_event(
            self.hass, [entity_id], _changed,
        )
        self.async_write_ha_state()

    def _z2m_attr(self, name: str) -> Any:
        if self.hass is None or not self._z2m_entity:
            return None
        state = self.hass.states.get(self._z2m_entity)
        return (state.attributes or {}).get(name) if state is not None else None

    @property
    def available(self) -> bool:
        if self.hass is None or not self._z2m_entity:
            return False
        state = self.hass.states.get(self._z2m_entity)
        return state is not None and state.state != "unavailable"

    @property
    def installed_version(self) -> str | None:
        return _version(self._z2m_attr("installed_version"))

    @property
    def latest_version(self) -> str | None:
        """The approved version Z2M says this device can take, else nothing.

        Z2M's own figure, but believed only when it is an approved version for
        this model: a figure from before this shipped came from the public
        list. When Z2M has no update the latest IS the installed version (its
        answer to our check), which HA reads as up to date.
        """
        latest = _version(self._z2m_attr("latest_version"))
        installed = self.installed_version
        if latest is None:
            return None
        approved = {
            _version(e.get("fileVersion")) for e in approved_for_model(self._model)
        }
        if latest in approved:
            return latest
        if latest == installed:
            return installed
        return None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        return is_newer(_version(latest_version), _version(installed_version))

    @property
    def in_progress(self) -> bool:
        return self._installing or self._z2m_attr("in_progress") is True

    @property
    def update_percentage(self) -> int | float | None:
        value = self._z2m_attr("update_percentage")
        return value if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .firmware_report import FIRMWARE_DEVICE_ATTR, FIRMWARE_KIND_ATTR

        return {
            FIRMWARE_KIND_ATTR: ZIGBEE_FIRMWARE_KIND,
            FIRMWARE_DEVICE_ATTR: self._raw_ieee,
            "sensa_zigbee_model": self._model,
            # Read by the Settings card: a battery device has to be pressed.
            "sensa_wake_device": self._waking and self._battery,
        }

    # -- the install ---------------------------------------------------------

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any,
    ) -> None:
        """Ask Z2M to update from the approved list, and wait for the result.

        RAISES on every refusal and failure, so Update now reports it.
        """
        installed = self.installed_version
        target = self.latest_version
        if not self.available or installed is None:
            raise HomeAssistantError(
                "Zigbee2MQTT has not reported this device's firmware yet."
            )
        if not is_newer(target, installed):
            raise HomeAssistantError("There is no approved firmware newer than this.")
        if self.in_progress:
            raise HomeAssistantError("An update is already in progress for this device.")

        from homeassistant.components import mqtt

        loop = asyncio.get_running_loop()
        answer: asyncio.Future[dict[str, Any]] | None = None
        transaction = ""

        @callback
        def _on_response(msg: Any) -> None:
            try:
                payload = json.loads(msg.payload)
            except Exception:  # noqa: BLE001 - not a Z2M reply
                return
            if not isinstance(payload, dict) or answer is None or answer.done():
                return
            if payload.get("transaction") != transaction:
                return
            answer.set_result(payload)

        unsub = await mqtt.async_subscribe(
            self.hass, Z2M_OTA_UPDATE_RESPONSE_TOPIC, _on_response,
        )
        self._installing = True
        self._waking = True
        self.async_write_ha_state()
        self._log("info", f"{self._display_name}: installing firmware {target}.")
        started = time.monotonic()
        try:
            while True:
                answer = loop.create_future()
                transaction = uuid.uuid4().hex
                await _publish(self.hass, Z2M_OTA_UPDATE_TOPIC, {
                    "id": self._raw_ieee,
                    "url": OTA_INDEX_FILE,
                    "transaction": transaction,
                })
                remaining = _INSTALL_TIMEOUT - (time.monotonic() - started)
                try:
                    reply = await asyncio.wait_for(answer, timeout=max(1, remaining))
                except TimeoutError as err:
                    raise HomeAssistantError(
                        f"no answer from Zigbee2MQTT within {_INSTALL_TIMEOUT // 60} minutes"
                    ) from err
                if reply.get("status") == "ok":
                    break
                error = str(reply.get("error") or "Zigbee2MQTT refused the update")
                # A sleeping device never answers the start. Ask again while the
                # customer has time to wake it; once it has moved, an error is final.
                if (
                    self._waking
                    and time.monotonic() - started < _WAKE_WINDOW
                    and "already in progress" not in error
                ):
                    _LOGGER.debug("zigbee firmware %s: %s, asking again", self._raw_ieee, error)
                    continue
                raise HomeAssistantError(error)
            to_version = _version(((reply.get("data") or {}).get("to") or {}).get("file_version"))
            if to_version != target:
                raise HomeAssistantError(
                    f"the device reports {to_version or 'an unknown version'}, not {target}"
                )
        except HomeAssistantError as err:
            self._log(
                "warning", f"{self._display_name}: firmware {target} did not install: {err}",
            )
            raise
        finally:
            unsub()
            self._installing = False
            self._waking = False
            self.async_write_ha_state()
        self._log("info", f"{self._display_name} is updated to firmware {target}.")
        try:
            from .firmware_report import async_push

            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                await async_push(self.hass, entry)
        except Exception:
            _LOGGER.debug("zigbee firmware: post-install report failed", exc_info=True)

    def _log(self, level: str, message: str) -> None:
        """One customer-facing line in Settings, System Log."""
        try:
            from ._log import sensa_log

            sensa_log(
                self.hass, level, message,
                source=SENSA_LOG_SOURCE_SYSTEM,
                context={"zigbee_ieee": self._raw_ieee, "area": "device_firmware"},
            )
        except Exception:
            _LOGGER.debug("zigbee firmware %s: sensa_log failed", self._raw_ieee, exc_info=True)


def zigbee_ieee_from_unique_id(unique_id: str, entry_id: str) -> str | None:
    """The `0x...` IEEE inside `<entry>_zigbee_fw_<ieee>_firmware`."""
    prefix, suffix = f"{entry_id}_zigbee_fw_", "_firmware"
    if not unique_id.startswith(prefix) or not unique_id.endswith(suffix):
        return None
    return unique_id[len(prefix):-len(suffix)] or None


def _device_name(dev: dict[str, Any], sensa_names: dict[str, str]) -> str:
    """The Sensa name for the device if it is registered, else Z2M's."""
    from .vendors.zigbee2mqtt import _bridge_ieee

    return (
        sensa_names.get(_bridge_ieee(dev.get("ieee_address")))
        or str(dev.get("friendly_name") or "")
        or str(dev.get("ieee_address"))
    )


def _sensa_names(runtime: dict[str, Any]) -> dict[str, str]:
    """Registered Sensa devices by Sensa-style IEEE (their `mac_address`)."""
    from .vendors.zigbee2mqtt import _bridge_ieee

    data = getattr(runtime.get("coordinator"), "data", None)
    devices = data.get("devices") if isinstance(data, dict) else None
    out: dict[str, str] = {}
    for dev in devices or []:
        if isinstance(dev, dict) and dev.get("mac_address"):
            name = str(dev.get("display_name") or dev.get("name") or "").strip()
            if name:
                out[_bridge_ieee(dev["mac_address"])] = name
    return out


@callback
def async_register_zigbee_firmware(
    hass: HomeAssistant, entry: Any, runtime: dict[str, Any], async_add_entities: Any,
) -> None:
    """One entity per OTA device with an approved entry, as devices arrive.

    Synced on the backend coordinator's 30-second tick, the same clock the ESP
    rows use; Z2M's device list is the bridge cache that tick reads. A device
    that leaves the network, or loses its approved entry, has its entity
    removed once the bridge cache has answered.
    """
    from homeassistant.helpers import entity_registry as er

    from .vendors.zigbee2mqtt import bridge_devices

    coordinator = runtime.get("coordinator")
    if coordinator is None:
        return
    live: dict[str, SensaZigbeeFirmware] = {}

    @callback
    def _sync() -> None:
        devices = _ota_devices(hass)
        names = _sensa_names(runtime)
        current: set[str] = set()
        new: list[SensaZigbeeFirmware] = []
        for dev in devices:
            model = str((dev.get("definition") or {}).get("model") or "")
            if not approved_for_model(model):
                continue
            raw = str(dev["ieee_address"])
            current.add(raw)
            if raw in live:
                live[raw].bind()
                continue
            entity = SensaZigbeeFirmware(
                entry.entry_id, raw, _device_name(dev, names), model,
                battery=str(dev.get("power_source") or "") == "Battery",
            )
            live[raw] = entity
            new.append(entity)
        if new:
            async_add_entities(new)
        # Removal only once Z2M has answered: an empty cache is "could not ask".
        if not bridge_devices(hass):
            return
        try:
            ent_reg = er.async_get(hass)
        except Exception:  # noqa: BLE001 - no registry, nothing to remove
            return
        for ent in list(ent_reg.entities.values()):
            if ent.domain != "update" or ent.platform != DOMAIN:
                continue
            raw = zigbee_ieee_from_unique_id(ent.unique_id or "", entry.entry_id)
            if raw is None or raw in current:
                continue
            live.pop(raw, None)
            ent_reg.async_remove(ent.entity_id)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
