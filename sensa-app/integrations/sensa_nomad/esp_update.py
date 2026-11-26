"""Firmware version of Sensa's OWN ESP devices, reported to the estate.

The Cerbo and the Teltonika kit already tell the Hub what firmware they are
running, through an `update` entity the App's phone-home reads
(`vendors/victron_update.py`, `vendors/teltonika_update.py`). This is the same
row for the devices we build ourselves: Bluetooth proxies, relay controllers,
Van Level, the Autoterm and FreshJet bridges, the fan controller. One
`sensa_firmware_kind` value (`sensa_esp`) and the existing shape carries them, so
a van's own kit lists beside its third-party kit on Hub's Deployments page.

**Installing, LAN only** (`docs/system-design/firmware-updates.md` section 3).
The App fetches the published binary from the blob once, checks its md5 and serves
it on the LAN (`/api/firmware/<code>/<version>.bin`); the device never touches the
internet. Two ways onto the device, chosen per device at install time:

  * PULL, firmware that has the `flash_firmware` API action: HA calls
    `esphome.<node>_flash_firmware` with the App's LAN URL and the md5, and the
    device downloads and flashes itself (`ota.http_request.flash`).
  * PUSH, firmware from before that action: the integration posts the image to
    the device's own `web_server` OTA (`/update`). This is how a device in the
    field gets its FIRST update; that update carries the action, so every later
    one is a pull. A device with neither has no LAN way in and needs one USB flash.

Either way the install only counts once the device is back and its `Sensa
Version` reads the new version. What the entity knows is what the
box's channel PUBLISHES: `latest_version` comes from the firmware pipeline's own
`<code>/manifest.json`, read by the App (`routes/firmware.py`) and asked for here
by product code. Unknown while that answer is missing - never mirrored from the
installed version, because that is how HA spells "up to date" and a row claiming
to be current on no evidence is the one thing this table must not say.

The version is read from the device's own `Sensa Version` text sensor, which
every Sensa firmware declares as `return { ESPHOME_PROJECT_VERSION };` and which
is therefore the exact string the firmware pipeline published. HA's
device-registry `sw_version` is deliberately NOT used: with a `project:` block
the `esphome` integration writes "1.0.4 (ESPHome 2025.10.1)" there (checked in
the 2026 HA image), and picking a version back out of a display string is a
second rule about version formatting that nothing else needs.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SENSA_LOG_SOURCE_SYSTEM

_LOGGER = logging.getLogger(__name__)

# The `kind` the Hub matches this row on (`admin/src/lib/firmware.ts` there).
# Third-party kinds name their vendor; ours names ours, because one value covers
# every Sensa ESP product: they share one firmware pipeline and one channel.
SENSA_ESP_FIRMWARE_KIND = "sensa_esp"

# How the `Sensa Version` text sensor is recognised in HA's entity registry.
# ESPHome mints the unique_id "<mac>-sensor-<object_id>" (aioesphomeapi's
# `build_unique_id`), and the object_id is the snake_cased entity name. The name
# is checked too, so a future unique_id scheme cannot silently drop every row.
_VERSION_UNIQUE_ID_SUFFIX = "-sensor-sensa_version"
_VERSION_ENTITY_NAME = "sensa version"


def esp_auto_update_key(device_id: str) -> str:
    """Runtime key holding this device's automatic-updates switch position."""
    return f"sensa_esp_auto_update_{device_id}"


def esp_auto_update_entity_id(device_id: str) -> str:
    """This device's automatic-updates switch. Deterministic, so the Settings
    table can name it without a registry lookup."""
    slug = re.sub(r"[^a-z0-9]+", "_", device_id.lower()).strip("_")
    return f"switch.sensa_nomad_firmware_auto_update_{slug}"


def esp_device_id_from_unique_id(unique_id: str, entry_id: str) -> str | None:
    """The Sensa device id inside `<entry>_sensa_esp_<device>_<suffix>`."""
    prefix = f"{entry_id}_sensa_esp_"
    if not unique_id.startswith(prefix):
        return None
    rest = unique_id[len(prefix):]
    for suffix in ("_firmware", "_auto_update"):
        if rest.endswith(suffix):
            return rest[: -len(suffix)] or None
    return None


def is_sensa_esp_device(device: dict[str, Any]) -> bool:
    """True for a registered device running firmware WE build.

    The test is that the catalog gives the row a `firmware_code`, NOT that its
    integration is `esphome`: the catalog also carries third-party ESPHome kit
    (Home Assistant Voice PE), and reporting somebody else's device as Sensa
    firmware would put a row on the Hub that no Sensa build will ever match.
    A device is ours exactly when Sensa builds firmware for it, which is the same
    fact that says which manifest to read.
    """
    from .device_catalog import firmware_code_for

    return bool(firmware_code_for(device))


# Where the App's answer about published firmware is kept for the entities to
# read: one dict per config entry on the runtime, refreshed by
# `async_refresh_available`. On the runtime rather than in a module global so it
# cannot outlive a reload, and so a test can see it.
RUNTIME_AVAILABLE_FIRMWARE = "esp_firmware_available"


def _firmware_code(device: dict[str, Any]) -> str:
    """This device's firmware build code, via the catalog. See `firmware_code_for`."""
    from .device_catalog import firmware_code_for

    return firmware_code_for(device)


def available_firmware(hass: Any, entry_id: str) -> dict[str, dict[str, Any]]:
    """`{code: {version, url, md5}}` as last read from the App, or empty."""
    try:
        runtime = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    except (AttributeError, TypeError):
        # No runtime is "nothing known". Deliberately NOT a blind `except`: one
        # here swallowed a missing import and reported every version as unknown.
        return {}
    value = runtime.get(RUNTIME_AVAILABLE_FIRMWARE)
    return value if isinstance(value, dict) else {}


async def async_refresh_available(hass: Any, entry: Any, *, force: bool = False) -> bool:
    """Ask the App what this van's channel publishes for the codes it has.

    Only the codes of devices actually registered here, because the blob container
    cannot be listed and asking about kit the van does not have would be a request
    per product we sell. The previous answer is KEPT on failure: "could not ask the
    blob" is not "no update", and every gate in this repo follows that rule.

    `force` (the Check now button) has the App read the blob now instead of
    answering from its hour-long cache.
    """
    entry_id = getattr(entry, "entry_id", entry)
    runtime = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    client = runtime.get("client")
    coordinator = runtime.get("coordinator")
    if client is None or coordinator is None:
        return False
    data = getattr(coordinator, "data", None)
    devices = data.get("devices") if isinstance(data, dict) else None
    codes = sorted({
        _firmware_code(d)
        for d in (devices or [])
        if isinstance(d, dict) and _firmware_code(d)
    })
    if not codes:
        return False
    try:
        answer = await client.async_get_firmware(codes=codes, force=force)
    except (ConnectionError, TimeoutError):
        _LOGGER.debug("esp firmware: App unreachable, keeping the last answer")
        return False
    except Exception:
        # Keeping the last answer, never clearing it.
        _LOGGER.debug("esp firmware: availability read failed", exc_info=True)
        return False
    published = (answer or {}).get("firmware")
    if not isinstance(published, dict):
        return False
    runtime[RUNTIME_AVAILABLE_FIRMWARE] = published
    # Write the entities' state NOW rather than waiting for the next coordinator
    # tick. The phone-home rows are read out of `hass.states`, and the report is
    # pushed in the same tick as this refresh, so without this the first push
    # after a restart carries the versions from BEFORE the refresh and the Hub
    # shows "not reported" until the tick after that (seen on a van, 2026-09-28).
    try:
        coordinator.async_update_listeners()
    except Exception:
        # A listener that objects must not lose the answer we just stored.
        _LOGGER.debug("esp firmware: could not refresh entity state", exc_info=True)
    _LOGGER.debug(
        "esp firmware: %d of %d code(s) have a published version",
        len(published), len(codes),
    )
    return True


def version_entity_id(hass: Any, device: dict[str, Any]) -> str | None:
    """The device's `Sensa Version` sensor, or None until it has registered.

    Matched through the ESPHome config entry that serves this device row
    (`_esphome_candidate_entry_ids`, the same hostname/IP match the heater, AC
    and levelling role maps use), never by guessing an entity_id off the
    hostname: a device whose ESPHome node name and Sensa hostname differ would
    silently report nothing.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        from . import _esphome_candidate_entry_ids
    except ImportError:  # pragma: no cover - HA always has these
        return None
    try:
        ent_reg = er.async_get(hass)
        candidates = set(_esphome_candidate_entry_ids(hass, device))
    except Exception:
        # No registry is "cannot ask", never a raise. (No `noqa: BLE001` here:
        # ruff does not flag a broad except whose body logs with exc_info.)
        _LOGGER.debug("esp firmware: registry lookup failed", exc_info=True)
        return None
    if not candidates:
        return None
    for ent in ent_reg.entities.values():
        if ent.config_entry_id not in candidates:
            continue
        if not ent.entity_id.startswith("sensor."):
            continue
        unique_id = ent.unique_id or ""
        name = (ent.original_name or ent.name or "").strip().lower()
        if unique_id.endswith(_VERSION_UNIQUE_ID_SUFFIX) or name == _VERSION_ENTITY_NAME:
            return ent.entity_id
    return None


def installed_firmware_version(hass: Any, device: dict[str, Any]) -> str | None:
    """What the device says it is running, or None when it cannot be asked.

    Deliberately NOT the `version` field on the device row: that is a snapshot
    the discovery probe took when the device was added, so it still reads the old
    version after the device has been flashed. A version that cannot be read is
    reported as nothing, which the Hub shows as not known; a stale one would be
    shown as fact.
    """
    entity_id = version_entity_id(hass, device)
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    text = str(getattr(state, "state", "") or "").strip()
    if not text or text in ("unknown", "unavailable"):
        return None
    return text


# -- Installing -----------------------------------------------------------------

# The user-defined API action every Sensa firmware from 1.1 on declares.
FLASH_ACTION = "flash_firmware"

# The device's own web UI login, the same in every Sensa firmware (the shared
# include's `web_server: auth:`). Only used for the push, on the van LAN.
_WEB_USER = "admin"
_WEB_PASSWORD = "sensaweb"

# How long a device has to come back on its new version after the image is
# handed over: download (pull), write, reboot, Wi-Fi, ESPHome reconnect.
_RETURN_TIMEOUT = 300
_RETURN_POLL = 5
# Uploading the image to `/update` is the write itself, so it is slow.
_PUSH_TIMEOUT = 240

# Automatic tries per published version before the box stops and says so.
_MAX_ATTEMPTS = 3
# Between automatic tries, and after "could not ask" (App could not reach the
# blob): long enough not to hammer, short enough to finish the same evening.
_RETRY_HOLD = 900
# A relay controller's channels all drop for the reboot. Not while somebody is
# using them: no channel switched in this long.
_RELAY_QUIET = timedelta(minutes=10)


class CouldNotAsk(HomeAssistantError):
    """The binary is not on the box and the blob cannot be reached. Not a failure."""


class NoLanPath(HomeAssistantError):
    """The device's firmware has no way to be flashed over the LAN."""


def _parse_version(value: str | None) -> tuple[int, ...] | None:
    # Firmware versions are the pipeline's plain `x.y.z`; anything else is not
    # ordered, so never installed over or onto.
    try:
        return tuple(int(p) for p in str(value or "").split("."))
    except ValueError:
        return None


def is_newer(candidate: str | None, installed: str | None) -> bool:
    """Forward-only: True only when both parse and `candidate` is higher."""
    return wants_install(candidate, installed, pinned=False)


def wants_install(candidate: str | None, installed: str | None, *, pinned: bool) -> bool:
    """Whether `candidate` should go onto a device running `installed`.

    Forward-only, except that a version the rollout group pins installs up OR
    down: a pin is how a device is rolled back to known-good firmware.
    """
    new, old = _parse_version(candidate), _parse_version(installed)
    if not new or not old:
        return False
    return new != old if pinned else new > old


def esphome_entry(hass: Any, device: dict[str, Any]) -> Any | None:
    """The ESPHome config entry that serves this device row, or None."""
    from . import _esphome_candidate_entry_ids

    for entry_id in _esphome_candidate_entry_ids(hass, device):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None:
            return entry
    return None


def flash_service(hass: Any, entry: Any) -> str | None:
    """`<node>_flash_firmware` when HA has registered it for this device.
    Absent means firmware from before the action, which is the push path."""
    return esphome_service(hass, entry, FLASH_ACTION)


def esphome_service(hass: Any, entry: Any, action: str) -> str | None:
    """`<node>_<action>` when HA has registered that API action for this device.

    ESPHome names a device's actions `esphome.<node name, - as _>_<action>`. The
    node name is read off the entry (the device's own report first, then the name
    stored at setup), never derived from a hostname, and the service is only
    returned when HA actually has it.
    """
    names: list[str] = []
    info = getattr(getattr(entry, "runtime_data", None), "device_info", None)
    if getattr(info, "name", None):
        names.append(str(info.name))
    stored = (getattr(entry, "data", None) or {}).get("device_name")
    if stored:
        names.append(str(stored))
    for name in names:
        service = f"{name.replace('-', '_')}_{action}"
        if hass.services.has_service("esphome", service):
            return service
    return None


def device_address(device: dict[str, Any], entry: Any) -> str:
    """Where the box reaches the device: its IP, else the ESPHome entry's host."""
    ip = str(device.get("ip_address") or "").strip()
    if ip:
        return ip
    return str(((getattr(entry, "data", None) or {}).get("host")) or "").strip()


def relay_switch_entities(hass: Any, device: dict[str, Any]) -> list[str]:
    """This device's relay channels (`switch.*_relay_*`), the relay card's rule."""
    try:
        from homeassistant.helpers import entity_registry as er

        from . import _esphome_candidate_entry_ids

        candidates = set(_esphome_candidate_entry_ids(hass, device))
        ent_reg = er.async_get(hass)
    except Exception:
        _LOGGER.debug("esp firmware: relay lookup failed", exc_info=True)
        return []
    return [
        ent.entity_id
        for ent in ent_reg.entities.values()
        if ent.config_entry_id in candidates
        and ent.entity_id.startswith("switch.")
        and "_relay_" in ent.entity_id
    ]


class SensaEspFirmware(CoordinatorEntity, UpdateEntity):
    """The firmware one Sensa ESP device is running, and its install.

    Rides the Sensa backend coordinator, so the reported version follows the
    device: the coordinator ticks every 30 seconds, each tick re-reads the
    device's own version sensor, and a device flashed on the bench shows its new
    version without a reload.
    """

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(
        self,
        coordinator: Any,
        entry_id: str,
        device_id: str,
        display_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._device_id = device_id
        self._attr_unique_id = f"{entry_id}_sensa_esp_{device_id}_firmware"
        self._attr_name = f"{display_name} Firmware"
        self._display_name = display_name
        self._attr_in_progress = False
        # Automatic-install bookkeeping, per published version.
        self._attempts: dict[str, int] = {}
        self._given_up: set[str] = set()
        self._retry_at: float | None = None

    @property
    def _row(self) -> dict[str, Any]:
        """This device's current row from the backend snapshot.

        Re-read rather than held from construction: an IP change or a rename has
        to reach the ESPHome entry match, and a row that has gone (device
        removed, backend unreachable) must read as "cannot ask" rather than as a
        version from minutes ago.
        """
        data = getattr(self.coordinator, "data", None)
        devices = data.get("devices") if isinstance(data, dict) else None
        if not isinstance(devices, list):
            return {}
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            if str(dev.get("device_id") or dev.get("id") or "") == self._device_id:
                return dev
        return {}

    @property
    def installed_version(self) -> str | None:
        hass = getattr(self, "hass", None)
        row = self._row
        if hass is None or not row:
            return None
        return installed_firmware_version(hass, row)

    @property
    def latest_version(self) -> str | None:
        """What the box's channel publishes for this device, or None.

        None means nobody has told us, which HA renders as unknown: a device whose
        firmware has never been published to this channel, or a box that has not
        been able to read the manifest yet. Never the installed version.
        """
        hass = getattr(self, "hass", None)
        if hass is None:
            return None
        code = _firmware_code(self._row)
        if not code:
            return None
        entry = available_firmware(hass, self._entry_id).get(code)
        version = str((entry or {}).get("version") or "").strip()
        return version or None

    @property
    def _pinned(self) -> bool:
        """The rollout group pins this device's version, so it may go down."""
        hass = getattr(self, "hass", None)
        if hass is None:
            return False
        code = _firmware_code(self._row)
        entry = available_firmware(hass, self._entry_id).get(code) if code else None
        return (entry or {}).get("pinned") is True

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """HA's on/off verdict, from the same `wants_install` the installer uses.

        HA's default would call `1.0.4` newer than a simulator's `1.0.4-sim`,
        offering an update the install path then refuses.
        """
        return wants_install(latest_version, installed_version, pinned=self._pinned)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What this is and which Sensa device it belongs to.

        Read by the App's phone-home, which lists every van's equipment firmware
        for Hub's Deployments page. The App reads entity STATES and has no access
        to HA's registries, so the kind has to travel on the state. `sensa_`
        prefixed so it can never shadow one of the update platform's own
        attributes.
        """
        from .firmware_report import FIRMWARE_AUTO_KEY_ATTR, FIRMWARE_CODE_ATTR

        return {
            "sensa_firmware_kind": SENSA_ESP_FIRMWARE_KIND,
            "sensa_device_id": self._device_id,
            FIRMWARE_AUTO_KEY_ATTR: esp_auto_update_key(self._device_id),
            # The build code a rollout group pins by, so Hub's Deployments page
            # can show this device's pin beside it.
            FIRMWARE_CODE_ATTR: _firmware_code(self._row) or None,
        }

    # -- automatic -----------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._maybe_auto_install()

    def _is_relay(self) -> bool:
        hass = getattr(self, "hass", None)
        return bool(hass and relay_switch_entities(hass, self._row))

    def _relay_busy(self) -> bool:
        """A relay channel switched recently: someone is using it, wait."""
        from homeassistant.util import dt as dt_util

        hass = self.hass
        cutoff = dt_util.utcnow() - _RELAY_QUIET
        for entity_id in relay_switch_entities(hass, self._row):
            state = hass.states.get(entity_id)
            if state is not None and state.last_changed > cutoff:
                return True
        return False

    @callback
    def _maybe_auto_install(self) -> None:
        """Start an automatic install when this device has one to take."""
        hass = getattr(self, "hass", None)
        if hass is None or self._attr_in_progress:
            return
        latest = self.latest_version
        if (
            not wants_install(latest, self.installed_version, pinned=self._pinned)
            or latest in self._given_up
        ):
            return
        if self._retry_at is not None and time.monotonic() < self._retry_at:
            return
        from .update import (
            INSTALL_SLOT_SENSA_ESP,
            INSTALL_SLOT_SENSA_ESP_RELAY,
            auto_update_allowed,
            note_install_pending,
            start_auto_install,
        )

        if not auto_update_allowed(
            hass, self._entry_id, esp_auto_update_key(self._device_id)
        ):
            return
        relay = self._is_relay()
        slot = INSTALL_SLOT_SENSA_ESP_RELAY if relay else INSTALL_SLOT_SENSA_ESP
        # Only ever marked, never cleared: every Sensa device shares the slot, so
        # one device clearing it would hide another's work. The mark expires on
        # its own (update.py `_INSTALL_PENDING_TTL`).
        note_install_pending(hass, self._entry_id, slot, True)
        if relay and self._relay_busy():
            return
        started = start_auto_install(
            hass, self._entry_id, slot, self._async_auto_install,
            what=f"{self._device_id} firmware {latest}",
        )
        if started:
            # Held until the install has run; cleared or kept by its result.
            self._retry_at = time.monotonic() + _RETRY_HOLD

    async def _async_auto_install(self) -> None:
        """The automatic install, counting attempts and swallowing the raise."""
        target = self.latest_version
        try:
            await self.async_install(None, False)
        except CouldNotAsk:
            return  # not an attempt; `_retry_at` already holds it off
        except NoLanPath:
            if target:
                self._given_up.add(target)
            return
        except HomeAssistantError as err:
            if not target:
                return
            count = self._attempts.get(target, 0) + 1
            self._attempts[target] = count
            if count >= _MAX_ATTEMPTS:
                self._given_up.add(target)
                self._log(
                    "warning",
                    f"{self._display_name}: firmware {target} failed to install "
                    f"{count} times and will not be tried again automatically. "
                    "Press Install on the device to try again.",
                    notify=True,
                )
            else:
                _LOGGER.warning(
                    "esp firmware %s: automatic install failed (%d of %d): %s",
                    self._device_id, count, _MAX_ATTEMPTS, err,
                )
            return
        self._retry_at = None

    # -- the install ---------------------------------------------------------

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any,
    ) -> None:
        """Flash the channel's published version and wait for the device to return.

        RAISES on every refusal and failure, so the Install button reports it.
        """
        hass = self.hass
        row = self._row
        installed = self.installed_version
        target = self.latest_version
        if not row or installed is None:
            raise HomeAssistantError(
                "The device is not reachable, so it cannot be updated now."
            )
        if not target or not wants_install(target, installed, pinned=self._pinned):
            raise HomeAssistantError("There is no newer firmware for this device.")
        code = _firmware_code(row)
        published = available_firmware(hass, self._entry_id).get(code) or {}
        md5 = str(published.get("md5") or "")
        lan_path = str(
            published.get("lan_path") or f"/api/firmware/{code}/{target}.bin"
        )
        runtime = (hass.data.get(DOMAIN) or {}).get(self._entry_id) or {}
        client = runtime.get("client")
        entry = esphome_entry(hass, row)
        address = device_address(row, entry)
        if client is None or entry is None or not address:
            raise HomeAssistantError(
                "The device's connection details are not known yet."
            )

        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            # Always through the App first: this is what fetches and checks the
            # image, so neither path hands a device something unverified.
            image = await self._fetch_image(hass, client.base_url + lan_path)
            service = flash_service(hass, entry)
            if service:
                box_ip = await self._box_ip_for(hass, address)
                port = urlsplit(client.base_url).port or 8099
                url = f"http://{box_ip}:{port}{lan_path}"
                self._log("info", f"{self._display_name}: installing firmware {target}.")
                await hass.services.async_call(
                    "esphome", service, {"url": url, "checksum": md5}, blocking=True,
                )
            else:
                self._log(
                    "info",
                    f"{self._display_name}: installing firmware {target} (first update "
                    "over the LAN).",
                )
                await self._push_image(hass, address, image)
            await self._await_version(target)
        except CouldNotAsk:
            self._log(
                "info",
                f"{self._display_name}: firmware {target} could not be downloaded; the "
                "box will try again when it is online.",
            )
            raise
        except NoLanPath:
            self._log(
                "warning",
                f"{self._display_name} cannot be updated over the network with the "
                "firmware it is running. It needs one update by USB; after that "
                "it updates itself.",
                notify=True,
            )
            raise
        except HomeAssistantError as err:
            self._log(
                "warning", f"{self._display_name}: firmware {target} did not install: {err}"
            )
            raise
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()
        self._log("info", f"{self._display_name} is updated to firmware {target}.")
        # Tell the App now rather than on the next 15-minute report: a moved
        # version makes it phone home, so the Hub stops showing "installing".
        try:
            from .firmware_report import async_push

            entry = hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                await async_push(hass, entry)
        except Exception:
            _LOGGER.debug("esp firmware: post-install report failed", exc_info=True)

    async def _fetch_image(self, hass: Any, url: str) -> bytes:
        """The checked image from the App. 503 / unreachable is "could not ask"."""
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=180)
            ) as resp:
                if resp.status == 503:
                    raise CouldNotAsk("The firmware could not be downloaded yet.")
                if resp.status != 200:
                    raise HomeAssistantError(
                        f"The box could not provide the firmware (HTTP {resp.status})."
                    )
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise CouldNotAsk(f"The Sensa App did not answer: {err}") from err

    async def _box_ip_for(self, hass: Any, address: str) -> str:
        """The box's address on the route to the device, as the device sees it."""
        from homeassistant.components.network import async_get_source_ip

        try:
            return await async_get_source_ip(hass, target_ip=address)
        except Exception as err:
            raise HomeAssistantError(
                f"Could not work out the box's address on the device's network: {err}"
            ) from err

    async def _push_image(self, hass: Any, address: str, image: bytes) -> None:
        """Post the image to the device's own web OTA (`web_server` `/update`)."""
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        form = aiohttp.FormData()
        form.add_field(
            "update", image, filename="firmware.bin",
            content_type="application/octet-stream",
        )
        try:
            async with session.post(
                f"http://{address}/update",
                data=form,
                auth=aiohttp.BasicAuth(_WEB_USER, _WEB_PASSWORD),
                timeout=aiohttp.ClientTimeout(total=_PUSH_TIMEOUT),
            ) as resp:
                body = (await resp.text())[:200]
                if resp.status in (404, 405):
                    raise NoLanPath("The device has no web update.")
                if resp.status != 200:
                    raise HomeAssistantError(
                        f"The device refused the firmware (HTTP {resp.status}: {body})."
                    )
        except aiohttp.ClientConnectorError as err:
            # Nothing listening on port 80: firmware with no web UI at all.
            raise NoLanPath(str(err)) from err
        except aiohttp.ServerDisconnectedError:
            # The device rebooting as it finishes is a normal end to the upload;
            # the version check that follows is what decides.
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(f"The upload to the device failed: {err}") from err

    async def _await_version(self, target: str) -> None:
        """Wait for the device to come back on `target`, bounded."""
        deadline = time.monotonic() + _RETURN_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(_RETURN_POLL)
            if self.installed_version == target:
                return
        raise HomeAssistantError(
            f"the device did not come back on {target} within "
            f"{_RETURN_TIMEOUT // 60} minutes"
        )

    def _log(self, level: str, message: str, *, notify: bool | None = None) -> None:
        """One customer-facing line in Settings -> System Log."""
        try:
            from ._log import sensa_log

            sensa_log(
                self.hass, level, message,
                source=SENSA_LOG_SOURCE_SYSTEM,
                context={"device_id": self._device_id, "area": "device_firmware"},
                notify=notify,
            )
        except Exception:
            _LOGGER.debug(
                "esp firmware %s: sensa_log write failed", self._device_id,
                exc_info=True,
            )


@callback
def remove_orphaned_esp_entities(
    hass: Any, entry_id: str, domain: str, current: set[str],
) -> None:
    """Delete this entry's Sensa-device `domain` entities whose device has gone.

    They belong to no HA device, so removing the device from the picker never
    takes them with it; without this every simulator or bench device ever added
    keeps a row in Software updates. The caller skips an empty device list,
    which is as likely "the backend did not answer" as "the van has nothing".
    """
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
    except Exception:
        _LOGGER.debug("esp firmware: registry unavailable", exc_info=True)
        return
    for ent in list(ent_reg.entities.values()):
        if ent.domain != domain:
            continue
        device_id = esp_device_id_from_unique_id(ent.unique_id or "", entry_id)
        if device_id is None or device_id in current:
            continue
        _LOGGER.info(
            "Removing %s: device %s is no longer registered", ent.entity_id, device_id,
        )
        ent_reg.async_remove(ent.entity_id)


@callback
def track_esp_devices(
    hass: Any,
    entry: Any,
    runtime: dict[str, Any],
    domain: str,
    build: Any,
    async_add_entities: Any,
) -> None:
    """Create one `domain` entity per Sensa ESP device as devices arrive, and
    remove the ones whose device has gone. Shared by the firmware entities and
    their automatic-updates switches, so the two always cover the same devices.
    """
    coordinator = runtime.get("coordinator")
    if coordinator is None:
        return
    seen: set[str] = set()

    @callback
    def _sync() -> None:
        # Type-checked rather than trusted: this runs on every coordinator tick,
        # including the ones where the backend answered with nothing useful, and
        # one raise in a coordinator listener takes the whole tick down.
        data = getattr(coordinator, "data", None)
        devices = data.get("devices") if isinstance(data, dict) else None
        if not isinstance(devices, list):
            return
        current: set[str] = set()
        new: list[Any] = []
        for device in devices:
            if not isinstance(device, dict) or not is_sensa_esp_device(device):
                continue
            device_id = str(device.get("device_id") or device.get("id") or "")
            if not device_id:
                continue
            current.add(device_id)
            if device_id in seen:
                continue
            seen.add(device_id)
            display_name = str(
                device.get("display_name") or device.get("name") or "Sensa Device"
            )
            new.append(build(coordinator, device_id, display_name))
        if new:
            _LOGGER.info(
                "Sensa ESP devices: adding %d %s entit%s",
                len(new), domain, "y" if len(new) == 1 else "ies",
            )
            async_add_entities(new)
        if devices:
            remove_orphaned_esp_entities(hass, entry.entry_id, domain, current)
            seen.intersection_update(current)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


@callback
def async_register_esp_firmware(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: dict[str, Any],
    async_add_entities: Any,
) -> None:
    """One firmware entity per Sensa ESP device, now and as they arrive.

    Driven off the backend coordinator rather than the add path: it re-reads the
    device list every 30 seconds, so a device added through the picker gets its
    row on the next tick with no reload. A removed device's entity is deleted
    from the registry on the next tick.
    """
    track_esp_devices(
        hass, entry, runtime, "update",
        lambda coordinator, device_id, name: SensaEspFirmware(
            coordinator, entry.entry_id, device_id, name,
        ),
        async_add_entities,
    )
