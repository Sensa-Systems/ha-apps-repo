"""Binary sensor platform for Sensa Nomad."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    EVENT_SENSA_ALARMS_CHANGED,
    MOVEMENT_BINARY_SENSOR_ENTITY_ID,
    RUNTIME_BACKUP_STATUS,
    RUNTIME_HOST_UPDATE_STATUS,
    RUNTIME_NETWORK_STATUS,
    RUNTIME_OTA_STATUS,
    RUNTIME_SYSTEM_HARDWARE,
    SIGNAL_BACKUP_STATUS_CHANGED,
    SIGNAL_CURRENT_MODE_CHANGED,
    SIGNAL_MOVEMENT_CHANGED,
    SIGNAL_NETWORK_STATUS_CHANGED,
    SIGNAL_SYSTEM_HARDWARE_CHANGED,
    velit_binary_sensor_entity_id,
)
from .modes_catalog import MODES, ModeDef, mode_active_entity_id

_LOGGER = logging.getLogger(__name__)


@callback
def _async_remove_legacy_followme_recording_sensor(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Retire the pre-rename FollowMe recording indicator from the registry.

    `binary_sensor.sensa_nomad_followme_recording` (unique_id ..._followme_recording)
    became `..._tracking_recording` when FollowMe was renamed to Tracking; the old
    unique_id would otherwise linger as an unavailable entity."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_followme_recording"
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
    if entity_id:
        registry.async_remove(entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad binary sensors from a config entry."""
    runtime_data = hass.data[DOMAIN][entry.entry_id]

    # Store callback for dynamic vendor entity creation
    runtime_data["vendor_add_binary_sensors"] = async_add_entities

    # Flush any vendor binary sensors a coordinator queued before this
    # platform registered its callback (the silent-drop fix — see
    # `_add_or_queue_vendor_entities` in __init__.py). Mirrors the sensor
    # platform's `vendor_pending_sensors` drain.
    pending = runtime_data.pop("vendor_pending_binary_sensors", None)
    if pending:
        async_add_entities(pending)

    # Per-mode "is active" sensors — the Habitation Overview mode tiles
    # bind to these with `state_color: true` so HA auto-tints the icon
    # via `--state-binary_sensor-active-color` when the matching mode
    # is current. Centralising the on/off computation here means tile
    # YAML stays a plain HA `button` card, no card-mod / templates.
    async_add_entities(
        SensaModeActiveBinarySensor(entry, mode) for mode in MODES
    )

    # Stale-backup sensor — the Settings → Backup card's red banner reads
    # this. One per config entry; refreshed off the same poller that feeds
    # `sensor.sensa_nomad_backup_status`.
    async_add_entities([SensaBackupsStaleBinarySensor(entry, runtime_data)])

    # Internet reachability — the Cab → Network card reads this for its
    # "online/offline" state. Fed by the same network-status poller as the
    # ping / speed-test sensors.
    async_add_entities([SensaInternetUpBinarySensor(entry, runtime_data)])

    # Tracking recording indicator. Deliberately NOT EntityCategory.CONFIG, so it
    # can sit on a dashboard where people can actually see it — see the class
    # docstring, this is a passenger-transparency measure rather than a
    # diagnostic.
    _async_remove_legacy_followme_recording_sensor(hass, entry)
    async_add_entities([SensaTrackingRecordingBinarySensor(hass, entry)])

    # "Are we moving?" -- the single signal the Drive/Parked instincts trigger
    # on. Added unconditionally: it folds OBD2 road speed AND the resolved GPS,
    # so which sources a van has is a question the detector answers per sample,
    # not one entity creation can settle at startup. With neither, the state is
    # `unknown` (could not ask) rather than `off`.
    async_add_entities([SensaVehicleMovingBinarySensor(hass, entry)])

    # Any managed alarm ACTIVE → drives the cross-panel alarm banner. Push-based
    # from the alarm store.
    async_add_entities([SensaAlarmActiveBinarySensor(entry)])

    # Sensa support access open → drives the cross-panel support banner. Added
    # on every flavour: the state is simply always off where the account does
    # not exist, and gating entity creation on that would mean a box that gained
    # the account later had no sensor until the next restart.
    async_add_entities([SensaSupportActiveBinarySensor(entry)])

    # A service company's engineer looking at one of the owner's devices ->
    # drives the cross-panel "is looking at your heater" banner.
    async_add_entities([SensaServiceViewingBinarySensor(entry)])

    # Raspberry Pi supply + throttling problems, added LATE and only where the
    # App says this box reports hardware (appliance on real Pi hardware). Same
    # deferral, and the same reasoning, as the board sensors in sensor.py.
    _async_setup_hardware_binary_sensors(hass, entry, runtime_data, async_add_entities)

    # NOTE: Velit Rooftop AC power binary sensor is added/queued by
    # `_async_start_velit_rooftop_ac` via `_add_or_queue_vendor_entities`; the
    # `vendor_pending_binary_sensors` drain above flushes it if the coordinator
    # started before this platform registered. No per-device loop needed here.


@callback
def _async_setup_hardware_binary_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: dict[str, Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Pi supply/throttle sensors once the App confirms hardware support.

    Mirrors `sensor._async_setup_hardware_sensors`; see there for why these are
    added on a signal rather than at setup.
    """
    if runtime_data.get("hardware_binary_sensors_added"):
        return

    @callback
    def _on_hardware(hardware: dict[str, Any]) -> None:
        if not hardware.get("supported"):
            return
        if runtime_data.get("hardware_binary_sensors_added"):
            return
        runtime_data["hardware_binary_sensors_added"] = True
        async_add_entities(
            [
                SensaUndervoltageBinarySensor(entry, runtime_data),
                SensaThrottledBinarySensor(entry, runtime_data),
            ]
        )
        unsub()

    unsub = async_dispatcher_connect(hass, SIGNAL_SYSTEM_HARDWARE_CHANGED, _on_hardware)
    entry.async_on_unload(unsub)

    cached = runtime_data.get(RUNTIME_SYSTEM_HARDWARE)
    if cached:
        _on_hardware(cached)


class _SensaHardwareProblemBinarySensor(BinarySensorEntity):
    """Shared base for the Pi board's two problem indicators.

    Both read ``runtime[RUNTIME_SYSTEM_HARDWARE]`` and refresh on
    `SIGNAL_SYSTEM_HARDWARE_CHANGED`; they differ only in which key they read
    and how they turn it into a boolean.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime_data = runtime_data

    @property
    def _hardware(self) -> dict[str, Any]:
        return self._runtime_data.get(RUNTIME_SYSTEM_HARDWARE) or {}

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _on_change(_hardware: dict[str, Any]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_SYSTEM_HARDWARE_CHANGED, _on_change,
            )
        )


class SensaUndervoltageBinarySensor(_SensaHardwareProblemBinarySensor):
    """On when the kernel is reporting the 5V supply below critical.

    This is the single most useful reading the box has. A Pi on a marginal
    supply corrupts its card and then presents as a software fault, so a van
    that "keeps needing a reboot" is very often a cable or a socket rather than
    anything we shipped.

    Unavailable, NEVER off, when the flag cannot be read. "We could not ask" and
    "the supply is fine" must not look the same, because only one of them should
    stop somebody investigating.
    """

    _attr_name = "Undervoltage"
    _attr_icon = "mdi:flash-alert"

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        super().__init__(entry, runtime_data)
        self._attr_unique_id = f"{entry.entry_id}_undervoltage"
        self.entity_id = "binary_sensor.sensa_nomad_undervoltage"

    @property
    def available(self) -> bool:
        return isinstance(self._hardware.get("undervoltage"), bool)

    @property
    def is_on(self) -> bool | None:
        value = self._hardware.get("undervoltage")
        return value if isinstance(value, bool) else None


class SensaThrottledBinarySensor(_SensaHardwareProblemBinarySensor):
    """On when the firmware has capped or throttled the CPU, now or this boot.

    The "since boot" half is the point. Throttling on a vehicle happens at 3am
    in a sealed locker with nobody watching, and by morning the live flags have
    cleared; the sticky bits are the only evidence it ever happened. Each
    decoded flag is carried as an attribute so a support call can tell heat
    apart from a failing supply.
    """

    _attr_name = "Throttled"
    _attr_icon = "mdi:speedometer-slow"

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        super().__init__(entry, runtime_data)
        self._attr_unique_id = f"{entry.entry_id}_throttled"
        self.entity_id = "binary_sensor.sensa_nomad_throttled"

    @property
    def _throttled(self) -> dict[str, Any] | None:
        value = self._hardware.get("throttled")
        return value if isinstance(value, dict) else None

    @property
    def available(self) -> bool:
        # Absent whenever the host helper is not publishing: the bitmask has no
        # sysfs equivalent on a Pi 5, so there is no fallback reading to fall
        # back to and pretending otherwise would report a clean box.
        return self._throttled is not None

    @property
    def is_on(self) -> bool | None:
        throttled = self._throttled
        if throttled is None:
            return None
        return bool(throttled.get("any"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        throttled = self._throttled
        if throttled is None:
            return {}
        # `any` is the state itself, so it would only be noise here.
        return {k: v for k, v in throttled.items() if k != "any"}


class SensaModeActiveBinarySensor(BinarySensorEntity, RestoreEntity):
    """Boolean reflection of `sensor.sensa_nomad_current_mode`'s slug.

    State is `on` iff the integration's current-mode tracker matches
    this entity's mode slug, otherwise `off`. Updated via the
    `SIGNAL_CURRENT_MODE_CHANGED` dispatcher signal so all 8 sensors
    + the parent `SensaCurrentModeSensor` flip in one round-trip.

    Persisted across restarts via RestoreEntity so the Habitation tile
    keeps its highlight after a reboot until the next mode change.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, mode: ModeDef) -> None:
        self._entry = entry
        self._mode = mode
        self._attr_unique_id = (
            f"{entry.entry_id}_mode_{mode.slug}_active"
        )
        self._attr_name = f"{mode.name} Mode Active"
        self._attr_icon = mode.icon
        self.entity_id = mode_active_entity_id(mode.slug)
        self._is_on = False

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == "on"

        @callback
        def _on_change(slug: str) -> None:
            new_state = (slug == self._mode.slug)
            if new_state != self._is_on:
                self._is_on = new_state
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CURRENT_MODE_CHANGED, _on_change,
            )
        )


class SensaBackupsStaleBinarySensor(BinarySensorEntity):
    """`on` when the most recent HA backup is older than the schedule allows.

    Reads from ``runtime[RUNTIME_BACKUP_STATUS]`` (populated by the
    `_async_refresh_backup_status` poller in `__init__.py`). `on` also when
    no backups exist yet — the customer hasn't taken a first snapshot, which
    is just as bad as a stale one. Refreshes on every state-change signal
    plus a default 1-min poll so the card never sticks if a Supervisor blip
    drops a refresh cycle.
    """

    _attr_has_entity_name = True
    _attr_name = "Backups Stale"
    _attr_icon = "mdi:backup-restore"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._attr_unique_id = f"{entry.entry_id}_backups_stale"
        self.entity_id = "binary_sensor.sensa_nomad_backups_stale"

    @property
    def is_on(self) -> bool:
        status = self._runtime_data.get(RUNTIME_BACKUP_STATUS) or {}
        return bool(status.get("stale", True))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._runtime_data.get(RUNTIME_BACKUP_STATUS) or {}
        last = status.get("last") or {}
        addon = status.get("addon") or {}
        ota = self._runtime_data.get(RUNTIME_OTA_STATUS) or {}
        hu = self._runtime_data.get(RUNTIME_HOST_UPDATE_STATUS) or {}
        return {
            "last_backup_at": last.get("date"),
            "last_backup_name": last.get("name"),
            "last_backup_slug": last.get("slug"),
            "count": status.get("count", 0),
            "total_size_mb": status.get("total_size_mb"),
            # Per-backup list (newest first) the Settings → Backup *list*
            # card renders: each {slug, name, date, type, size_mb, pinned}.
            "backups": status.get("backups", []),
            "stale_after_hours": status.get("stale_after_hours"),
            "encryption_key_present": status.get("encryption_key_present", False),
            # The REAL schedule, read from HA's own backup engine rather than
            # the card's config defaults (which promised a nightly run on a box
            # where nothing was scheduled at all). Absent when the engine can't
            # be read; the card falls back rather than inventing a time.
            "schedule_recurrence": status.get("schedule_recurrence"),
            "schedule_time": status.get("schedule_time"),
            "schedule_next": status.get("schedule_next"),
            "retention_copies": status.get("retention_copies"),
            # How long a backup is kept on the vehicle. What the card renders;
            # `retention_copies` is HA's own backstop and stays for the older
            # bundles that read it.
            "retention_days": status.get("retention_days"),
            "encrypted": status.get("encrypted"),
            # True while a backup is being written. The card disables delete on
            # the row being created: the App lists tars off disk, so a half
            # written one looks finished, and deleting it corrupts the backup
            # the customer just asked for.
            "backup_in_progress": status.get("backup_in_progress"),
            # Rough progress for a running backup: {percent, size_mb,
            # reference_mb}. An ESTIMATE, from the size so far against the last
            # finished backup, so the card must present it as one. Absent on a
            # box with no earlier backup to compare against.
            "backup_progress": status.get("backup_progress"),
            # WHICH destination is being written, and how far. This dict is
            # built explicitly, so a field added to the status is dropped HERE
            # unless it is named: the upload phase reached the status and never
            # the card, which sat on a flat "Backing up..." for an entire ~900 MB
            # cloud upload (test box, 2026-08-10). Same trap as the destinations
            # `warning` field a few hours earlier.
            "backup_upload": status.get("backup_upload"),
            # One entry per backup destination for the card's pills:
            # {kind, label, icon, enabled, present, state, cadence, holds_at}.
            "backup_destinations": status.get("backup_destinations") or [],
            # Nothing is both switched on AND actually there -- deliberately not
            # "every toggle is off", because the case that loses a customer's
            # backups is Local off, USB on, stick in their pocket.
            "backup_destinations_none": status.get("backup_destinations_none"),
            "cloud_backup_available": status.get("cloud_backup_available"),
            # True from the moment a restore is handed to Home Assistant until
            # the box restarts, so the card's "Restoring" state survives a page
            # reload in that window.
            "restore_in_progress": status.get("restore_in_progress"),
            # Why a restore failed AFTER it was committed to. The pre-flight
            # reports its refusals through the service call; past that point the
            # service has already returned, so without this the card would sit
            # on "Restoring..." for ever and the customer would be told nothing.
            "restore_error": status.get("restore_error"),
            # Software-update info the Settings → Admin card shows: the running
            # version, the available version (Supervisor only — a container
            # updates by channel, so it has no precomputed "latest"), and the
            # channel for a container appliance's floating-tag update.
            # Why the LAST release-channel change failed, or None. The updater
            # applies the change out-of-band, so without this a change the App
            # accepted but the box never applied is invisible on every surface.
            "update_channel_error": addon.get("channel_error"),
            "installed_version": addon.get("version"),
            # Pin-aware: a box in a rollout group shows its pin here, never a
            # channel build it is not going to install (see ota_available_version).
            "available_version": self._available_version(addon),
            "update_channel": addon.get("channel"),
            # What the CHANNEL tag currently points at, unclamped, so it can
            # read OLDER than installed, which is exactly the case the staff-only
            # Sensa tab needs after a box is moved to a lower channel. The
            # customer-facing rows and the Update-now gate keep using the clamped
            # forward-only figures above.
            "channel_latest": addon.get("channel_latest"),
            # The microSD this unit was FLASHED FROM, and when. Deliberately kept
            # out of the three attributes above and rendered away from them on the
            # card: those say what the box is running now, this says where it
            # started, and after the first update it describes nothing still
            # installed. Absent on a BYO box or a card built before the stamp
            # existed, in which case the card shows no line at all.
            "image_version": (addon.get("image") or {}).get("version"),
            "image_built": (addon.get("image") or {}).get("built"),
            # Whether the LAST update check actually reached the registry
            # (container only). False → the card shows "couldn't check" instead
            # of trusting a stale "available_version" that reads as up-to-date;
            # None/absent → unknown (Supervisor, or an older stack image).
            "update_check_ok": addon.get("latest_check_ok"),
            "update_check_error": addon.get("latest_check_error"),
            # Download size (bytes) + release-notes URL for the available update
            # (container only) — the Admin card shows the size next to Latest and
            # a Release Notes button that opens the URL.
            "available_size": addon.get("download_bytes"),
            "release_notes_url": addon.get("release_notes_url"),
            # Whether an update is actually pending — the SAME gate as the
            # apply button entity's availability (container forward-check +
            # Supervisor rollout-pin aware), so the card's Update-now button
            # can disable in step with it.
            "update_pending": self._update_pending(),
            # Live OTA progress (container updates only): the updater sidecar's
            # /ota/status mirrored by the fast poller in __init__.py. Drives the
            # Admin card's real progress bar (percentage while pulling, phase
            # text, definite done/failed) instead of a blind indeterminate spin.
            "ota_state": ota.get("state"),
            "ota_progress": ota.get("progress"),
            "ota_error": ota.get("error"),
            # Free-text underlying cause of a failure (e.g. the failing image +
            # "not found"/"denied") the updater/reconcile now captures, so the
            # card can show the real reason instead of only the generic token.
            "ota_detail": ota.get("detail"),
            "ota_version": ota.get("version"),
            "ota_unchanged": ota.get("unchanged"),
            # When the updater last wrote this status (epoch seconds). A card
            # that REFUSES something while an update runs (the Diagnostics tab's
            # File Integrity) must be able to tell a live update from a wedged
            # one: the mirror stops refreshing when the fast watch hits its
            # ceiling, so a stuck "pulling" would otherwise disable the control
            # forever. With the timestamp the card applies the same staleness
            # rule the writer does and releases itself.
            "ota_ts": ota.get("ts"),
            # Host-OS (Layer 1) update status the Admin card's "Host OS updates"
            # section shows: last/next run, result, whether a reboot is pending,
            # and the effective auto/policy. "unknown" on a box with no host agent.
            "host_update_last_run": hu.get("last_run"),
            "host_update_last_result": hu.get("last_result"),
            "host_update_next_run": hu.get("next_run"),
            "host_update_reboot_pending": hu.get("reboot_pending"),
            "host_update_auto": hu.get("auto"),
            "host_update_policy": hu.get("policy"),
            # The effective maintenance window the agent applied (confirms the
            # customer's pick landed). The customer-facing control is the
            # `select.sensa_nomad_host_update_window` picker.
            "host_update_window": hu.get("window"),
        }

    @staticmethod
    def _available_version(addon: dict) -> Any:
        from . import (
            ota_available_version,  # inline: platform loads before __init__ finishes
        )

        try:
            return ota_available_version(addon)
        except Exception:  # noqa: BLE001 - an attribute must never break state writes
            return addon.get("version_latest")

    def _update_pending(self) -> bool:
        """The apply-gate verdict, shared with the button entity."""
        if self.hass is None:
            return False
        from . import (
            ota_update_pending,  # inline: platform loads before __init__ finishes
        )

        try:
            return ota_update_pending(self.hass, self._entry.entry_id)
        except Exception:  # noqa: BLE001 - an attribute must never break state writes
            return False

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _on_change(_status: dict[str, Any]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_BACKUP_STATUS_CHANGED, _on_change,
            )
        )


class SensaSupportActiveBinarySensor(BinarySensorEntity):
    """`on` while Sensa support access is open.

    Drives the cross-panel support banner. It exists because the persistent
    notification the feature has always raised has nowhere to appear on the
    Container appliance: the `admin` login is not an HA admin there, so
    `sensa-panel` hides HA's sidebar and with it the notification bell. A
    customer's only signal that Sensa can reach their van was the position of a
    toggle on a settings tab they had no reason to open, which is not good
    enough for this particular fact.

    Reads the store DIRECTLY rather than being pushed to, and refreshes on the
    support bus event. A push-only bind silently no-ops when the store is
    created after the entity is added, which is exactly how the alarm banner
    once went missing while its own tab showed the alarm.
    """

    _attr_has_entity_name = True
    _attr_name = "Support Access Open"
    _attr_icon = "mdi:face-agent"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_support_active"
        self.entity_id = "binary_sensor.sensa_nomad_support_active"

    @property
    def is_on(self) -> bool:
        from .remote_support import get_state

        state = get_state(self.hass) if self.hass is not None else None
        return bool(state and state.enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Enough for the banner to say who opened it and until when, without a
        second round trip to the switch entity."""
        from .remote_support import get_state

        state = get_state(self.hass) if self.hass is not None else None
        if state is None or not state.enabled:
            return {"enabled_until": None, "opened_by": None}
        return {
            "enabled_until": (
                state.enabled_until.isoformat() if state.enabled_until else None
            ),
            "opened_by": state.opened_by,
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from .const import EVENT_SENSA_REMOTE_SUPPORT_CHANGED

        @callback
        def _on_change(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SENSA_REMOTE_SUPPORT_CHANGED, _on_change)
        )


class SensaServiceViewingBinarySensor(BinarySensorEntity):
    """`on` while a service company's engineer has one of the owner's devices
    open. Drives the panel banner that says who is looking at what.

    Reads `service_readings` directly and refreshes on its signal, for the same
    reason as the support sensor above: a push-only bind silently no-ops when the
    state arrives before the entity is added.
    """

    _attr_has_entity_name = True
    _attr_name = "Service Company Looking"
    _attr_icon = "mdi:wrench-clock"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_service_viewing"
        self.entity_id = "binary_sensor.sensa_nomad_service_viewing"

    def _viewers(self) -> list[dict[str, str]]:
        from .service_readings import watching

        return watching(self.hass, self._entry) if self.hass is not None else []

    @property
    def is_on(self) -> bool:
        return bool(self._viewers())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Who is looking at which device, for the banner's words."""
        return {"viewers": self._viewers()}

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from .service_readings import SIGNAL_SERVICE_WATCHING

        @callback
        def _on_change() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SERVICE_WATCHING, _on_change)
        )


class SensaAlarmActiveBinarySensor(BinarySensorEntity):
    """`on` while any managed alarm is ACTIVE (un-snoozed).

    Drives the cross-panel alarm banner (every Sensa panel reads this). Push-
    based: the alarm store calls `set_active` on every raise/snooze/cancel/tick.
    """

    _attr_has_entity_name = True
    _attr_name = "Alarm Active"
    _attr_icon = "mdi:alarm-light"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_alarm_active"
        self.entity_id = "binary_sensor.sensa_nomad_alarm_active"
        self._active = False

    @property
    def is_on(self) -> bool:
        # Read the store directly so the state is correct regardless of whether
        # the store's push-binding landed (bind no-ops if the store wasn't
        # created yet when this entity was added). Falls back to the last pushed
        # value if the store isn't reachable.
        from ._alarms import get_alarm_store
        store = get_alarm_store(self.hass) if self.hass is not None else None
        if store is not None:
            return store.active_count() > 0
        return self._active

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    def set_active(self, active: bool) -> None:
        """Called by the alarm store on every change (push path)."""
        self._active = bool(active)
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from ._alarms import get_alarm_store
        store = get_alarm_store(self.hass)
        if store is not None:
            store.bind_binary(self)

        # Belt-and-braces: also refresh on the alarm bus event, so the banner's
        # binary state is right even if the push-binding wasn't established (the
        # cross-panel banner reads this entity from every panel).
        @callback
        def _on_alarms_changed(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_SENSA_ALARMS_CHANGED, _on_alarms_changed)
        )
        # Reflect any alarm already live at add time.
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        from ._alarms import get_alarm_store
        store = get_alarm_store(self.hass)
        if store is not None:
            store.unbind_binary(self)
        await super().async_will_remove_from_hass()


class SensaInternetUpBinarySensor(BinarySensorEntity):
    """`on` when the last reachability probe reached the internet.

    Reads ``runtime[RUNTIME_NETWORK_STATUS]['ping']`` (populated by the
    `_async_refresh_network_status` poller, which times a TCP connect to a
    well-known host every ~60s). `connectivity` device class so HA shows it as
    Connected / Disconnected. Surfaces the measured round-trip as an attribute
    too, though the dedicated `sensor.sensa_nomad_internet_ping` is the value
    the card's quality bar binds to.
    """

    _attr_has_entity_name = True
    _attr_name = "Internet"
    _attr_icon = "mdi:wifi"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, runtime_data: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime_data = runtime_data
        self._attr_unique_id = f"{entry.entry_id}_internet_up"
        self.entity_id = "binary_sensor.sensa_nomad_internet_up"

    def _ping(self) -> dict[str, Any]:
        return (self._runtime_data.get(RUNTIME_NETWORK_STATUS) or {}).get("ping") or {}

    @property
    def available(self) -> bool:
        """Unavailable until a probe has actually answered.

        "Could not ask" is not "told no". `is_on` defaulted a missing snapshot to
        False, so between startup and the first ~60s poll -- and on any van where
        the App is unreachable -- a `connectivity` sensor with no reading at all
        rendered as a confident **Disconnected**. Reporting unavailable makes HA
        show it as unknown instead, which is the honest answer and is the same
        rule the Radar tier already follows (`is_pro=None` means "could not
        ask"). Keyed on the presence of the `reachable` key rather than its
        truthiness, so a genuine `False` still reads as Disconnected.
        """
        return "reachable" in self._ping()

    @property
    def is_on(self) -> bool:
        return bool(self._ping().get("reachable", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ping = self._ping()
        status = self._runtime_data.get(RUNTIME_NETWORK_STATUS) or {}
        return {
            "ping_ms": ping.get("ping_ms"),
            "host": ping.get("host"),
            # When this reading was taken, so a stale snapshot is detectable
            # rather than being presented as live.
            "checked_at": status.get("checked_at"),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _on_change(_status: dict[str, Any]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_NETWORK_STATUS_CHANGED, _on_change,
            )
        )


class SensaTrackingRecordingBinarySensor(BinarySensorEntity):
    """`on` while Tracking is recording this vehicle's position history.

    This entity exists for the people in the van who did NOT consent to anything.

    Consent is one person tapping the "Tracking" switch, but everybody travelling in
    the vehicle is recorded. We have no way to ask them, and no technical mitigation
    can fix that. The mitigation is transparency: a visible, persistent indicator that
    recording is happening, so a passenger can see it and ask, exactly as they would
    notice a dashcam.

    That is why this is a plain binary sensor rather than an
    `EntityCategory.CONFIG` diagnostic like the switch that drives it: config
    entities are hidden away in the device page, and an indicator nobody can find
    is not an indicator. Do not recategorise it, and do not remove it because it
    "duplicates the switch" -- the switch is a control, this is a disclosure.

    Reads the store rather than caching, for the same reason the switch does: a
    pushed attribute drifts the moment anything else changes the value.
    """

    _attr_has_entity_name = True
    _attr_name = "Tracking recording"
    _attr_icon = "mdi:record-rec"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tracking_recording"
        self.entity_id = "binary_sensor.sensa_nomad_tracking_recording"

    @property
    def is_on(self) -> bool:
        from ._tracking import get_tracking_store

        store = get_tracking_store(self.hass)
        return bool(store and store.enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from ._tracking import get_tracking_store

        store = get_tracking_store(self.hass)
        return {
            "buffered_positions": store.buffered_count() if store else 0,
            "explanation": (
                "This vehicle is recording where it goes (Tracking). Everyone "
                "travelling in it is recorded, not just the account holder."
            ),
        }

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
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }


class SensaVehicleMovingBinarySensor(BinarySensorEntity, RestoreEntity):
    """`on` while the van is under way. THE answer to "are we moving?".

    Everything that needs to know reads this one entity: the Drive instinct
    switches modes off its ON edge and the Parked instinct off its OFF edge.
    The rule itself, and the folding of the two possible sources (OBD2 road
    speed, resolved GPS), lives in `movement.py` -- see that module for why the
    thresholds are what they are, and for the driveway problem they exist to
    solve.

    `unknown` rather than `off` before the first answer: a box with no OBD2
    adapter and no resolvable GPS has not established that the van is parked,
    it has failed to ask. The Parked instinct is conditional on still being in
    Drive precisely so that a source going dark cannot make it act.

    Restored across restarts so a van that rebooted mid-journey comes back
    moving. Only the boolean is restored -- the anchor and the sustain windows
    re-derive from live fixes, since a fix from before the restart is of
    unknown age.
    """

    _attr_has_entity_name = True
    _attr_name = "Vehicle moving"
    _attr_device_class = BinarySensorDeviceClass.MOVING
    _attr_icon = "mdi:van-utility"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_vehicle_moving"
        self.entity_id = MOVEMENT_BINARY_SENSOR_ENTITY_ID

    def _detector(self) -> Any:
        from .movement import get_movement_detector

        return get_movement_detector(self.hass)

    @property
    def is_on(self) -> bool | None:
        detector = self._detector()
        return detector.is_moving if detector is not None else None

    @property
    def available(self) -> bool:
        """Available even with nothing to read.

        An entity that disappears whenever the WiCAN sleeps would take the
        instincts' trigger with it, and "unavailable" is not the same statement
        as "not moving" -- the state carries that distinction itself (`unknown`
        until something answers).
        """
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """WHY this answer, so a surprising one can be explained from the
        entity rather than by reading the detector."""
        detector = self._detector()
        if detector is None:
            return {"source": ""}
        return {
            "source": detector.source,
            "speed_mph": (
                round(detector.speed_mph, 1)
                if detector.speed_mph is not None
                else None
            ),
            "distance_from_parked_m": (
                round(detector.distance_from_anchor_m)
                if detector.distance_from_anchor_m is not None
                else None
            ),
            "changed_at": (
                detector.changed_at.isoformat()
                if detector.changed_at is not None
                else None
            ),
            # The thresholds actually applied on the last sample, so "why has
            # it not switched?" can be answered off the entity rather than by
            # cross-referencing the instinct's card.
            **detector.tuning,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        detector = self._detector()
        if (
            detector is not None
            and last_state is not None
            and last_state.state in ("on", "off")
        ):
            detector.restore(last_state.state == "on")

        @callback
        def _on_changed() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_MOVEMENT_CHANGED, _on_changed
            )
        )

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }


class VelitRooftopACPowerBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Standalone power on/off state for a Velit Rooftop AC.

    The Velit is one `climate.*` control that carries `power_on` internally;
    this surfaces it as a plain binary sensor so an automation can trigger on
    the AC switching on/off without parsing the climate state. Shares the
    climate entity's HA device (same `velit_rooftop_ac_{device_id}` identifier +
    name, matched EXACTLY) so it groups on the AC device.
    """

    _attr_has_entity_name = True
    _attr_name = "Power"
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_icon = "mdi:power"

    def __init__(
        self,
        coordinator: Any,
        entry_id: str,
        device_id: str,
        display_name: str,
        mac: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{entry_id}_velit_rooftop_ac_{device_id}_power"
        self.entity_id = velit_binary_sensor_entity_id(device_id, "power")
        last4 = mac.replace(":", "").upper()[-4:] if mac else ""
        name = display_name or "Velit Rooftop AC"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"velit_rooftop_ac_{device_id}")},
            "name": f"{name} (#{last4})" if last4 else name,
            "manufacturer": "Velit",
            "model": "Rooftop AC",
        }

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        return data.get("power_on")


def build_velit_binary_sensors(
    coordinator: Any,
    entry_id: str,
    device_id: str,
    display_name: str,
    mac: str,
) -> list[BinarySensorEntity]:
    """Power on/off binary sensor for one Velit Rooftop AC. Called by
    `_async_start_velit_rooftop_ac` (live first-add) and the binary-sensor
    platform setup (reload-after-coordinator-started case)."""
    return [
        VelitRooftopACPowerBinarySensor(
            coordinator, entry_id, device_id, display_name, mac,
        )
    ]
