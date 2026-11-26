"""Venus OS firmware update entity for a registered Cerbo GX.

The ONLY Victron firmware Nomad can update is the GX device's own Venus OS.
Everything wired to it (MultiPlus over VE.Bus, SmartSolar / SmartShunt over
VE.Direct, Orion XS, the Bluetooth-only kit) is updated from VRM or
VictronConnect, because Victron exposes no local trigger for those: the GX does
the flashing but only the VRM portal can start it. See
`docs/devices/victron-cerbo-gx.md` for what was checked and where.

Venus exposes the GX's own updater on `com.victronenergy.platform`, which the
GX's MQTT bridge publishes as `platform/0/Firmware/...`, so the whole feature
rides the MQTT link the Cerbo coordinator already holds. No cloud account, no
second transport, and it works on a van with a LAN and no internet (the check
itself needs the GX to reach Victron, which is the GX's own business).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    DOMAIN,
    RUNTIME_BACKUP_STATUS,
    RUNTIME_VICTRON_AUTO_UPDATE,
    SENSA_LOG_SOURCE_SYSTEM,
)
from .victron_mqtt_services import (
    AUTO_UPDATE_CHECK_AND_UPDATE,
    AUTO_UPDATE_SAFE_FOR_PIN,
    FIRMWARE_INSTALLED_VERSION,
    FIRMWARE_INSTALLING_STATES,
    FIRMWARE_ONLINE_AVAILABLE_VERSION,
    FIRMWARE_PROGRESS,
    FIRMWARE_STATE,
)

_LOGGER = logging.getLogger(__name__)


def version_key(value: str | None) -> tuple[int, ...] | None:
    """A Venus version string as a comparable tuple, or None if unreadable.

    Two things this has to get right, both of which bite the wrong way round:

    * Venus publishes "v3.79"; nobody is going to type the "v" into an admin
      field, and they should not have to. Stripped here, along with whitespace
      and case, so "3.79" and "v3.79" are the same version.

    * **The minor is a two-digit field, not a decimal.** Victron ship v3.00,
      v3.10, v3.20 ... v3.79, so "3.2" means 3.20 and 3.12 comes BEFORE it, not
      after. Comparing the minor as a plain integer would read 3.12 as newer
      than 3.2 and refuse an upgrade that should have gone ahead. The minor is
      therefore right-padded to two digits before comparison.

    * **A pre-release sorts BEFORE its release.** Venus publishes a beta as
      "v3.90~1" and the GX shows it as "v3.90-beta1"; both are 3.90 beta 1,
      and final 3.90 is newer than either. Dropping the suffix would read a
      beta as the final release and never offer the real one to that GX.

    The key is always (major, minor, patch, is_final, pre_number), so "3.8",
    "3.80" and "3.80.0" compare equal.

    None means "I could not read this". Callers must treat that as "do not
    install", never as a comparison that happened to come out favourable.
    """
    text = str(value or "").strip().lower().removeprefix("v")
    match = _VERSION_RE.fullmatch(text)
    if not match:
        return None
    release, suffix, pre_number = match.groups()
    if suffix is not None and not suffix.strip("~-") and not pre_number:
        # A bare "3.90-" or "3.90~" names no build.
        return None
    fields = release.split(".")
    if len(fields) > 3:
        return None
    if len(fields) > 1:
        # "2" -> 20, "12" -> 12, "79" -> 79. See the two-digit note above.
        fields[1] = fields[1].ljust(2, "0")
    parts = [int(f) for f in fields] + [0] * (3 - len(fields))
    if suffix is None:
        return (*parts, 1, 0)
    return (*parts, 0, int(pre_number or 0))


# "3.90", "3.90~1", "3.90-beta1", "3.90-beta.1", "3.90-rc2". The label is not
# ranked: Venus numbers its candidates with one counter.
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)([~-][a-z]*\.?)?(\d*)")


def same_version(a: str | None, b: str | None) -> bool:
    """True when two version strings name the same release."""
    left, right = version_key(a), version_key(b)
    return left is not None and left == right


def at_or_below(offered: str | None, ceiling: str | None) -> bool:
    """True when `offered` is no newer than `ceiling`.

    The whole rollout rule. An unreadable version on either side is False: a
    version we cannot parse is not a version we are willing to install onto a
    customer's inverter gateway.
    """
    left, right = version_key(offered), version_key(ceiling)
    if left is None or right is None:
        return False
    return left <= right


def cerbo_target_version(hass: Any, entry_id: str) -> str | None:
    """The Venus OS version this box's rollout group may move its GX to.

    Set per rollout group in Hub Admin (Control -> Rollout Groups, the "Victron
    Cerbo" column), handed down on the licence check-in and passed through by
    the App beside the Sensa pin. None means the group names no ceiling, which
    hands the decision to the customer's "Automatic Victron updates" toggle: the
    customer cannot see the Admin column, so a switch they have left ON must
    mean what it says rather than waiting on a box nobody filled in.

    Read fresh on each use rather than cached, so clearing the column in Admin
    stops the next install without waiting for anything to be rebuilt.
    """
    try:
        runtime = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
        addon = (runtime.get(RUNTIME_BACKUP_STATUS) or {}).get("addon") or {}
    except Exception:  # noqa: BLE001 - a missing runtime is "no target"
        return None
    text = str(addon.get("cerbo_target_version") or "").strip()
    return text or None


def _version_text(raw: Any) -> str | None:
    """A Venus firmware version payload as a display string.

    `Firmware/Installed/Version` already arrives formatted ("v3.79"), so this
    only normalises: null and empty both mean "the GX has not told us", which
    is not the same as "there is nothing newer" and must stay None.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


class VictronCerboUpdate(CoordinatorEntity, UpdateEntity):
    """Venus OS update for one Cerbo GX.

    `latest_version` follows HA's contract: equal to `installed_version` means
    up to date. Venus reports a genuinely-newer build on
    `Firmware/Online/AvailableVersion` and leaves it null otherwise, so a null
    is reported as "same as installed" ONLY once the GX has told us what is
    installed. Before that this entity is unavailable, because an unprimed
    read is "could not ask", never a confident "you are current".
    """

    _attr_has_entity_name = True
    _attr_name = "Venus OS"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, coordinator: Any, entry_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_victron_cerbo_{device_id}_venus_firmware"
        # The version we have already fired an install for. Venus keeps
        # republishing `Online/AvailableVersion` while it installs and for the
        # minutes it spends rebooting, and every one of those republishes runs
        # this check again; without this the same update would be started over
        # and over on a GX that is already busy installing it.
        self._auto_installed: str | None = None
        # The version we asked Venus to install and have not yet seen come up,
        # so the System Log gets an "installed" line when the GX reports it.
        self._awaiting_version: str | None = None

    # -- what the GX has told us ------------------------------------------

    @property
    def installed_version(self) -> str | None:
        return _version_text(
            self.coordinator.firmware_value(FIRMWARE_INSTALLED_VERSION),
        )

    @property
    def latest_version(self) -> str | None:
        offer = self._offer()
        if offer:
            return offer
        # Venus publishes null here when its last check found nothing newer, and
        # an offer past the rollout ceiling is a van HELD at its pin with nothing
        # to do: both read "up to date" by mirroring the installed version, as
        # the Teltonika entity does. The unprimed case never reaches this line
        # as a version: `installed_version` is None there too, and `available`
        # returns False, so HA renders unknown.
        return self.installed_version

    def _offer(self) -> str | None:
        """The Venus version this GX may install now, or None.

        What Venus offers, unless the rollout group's ceiling refuses it. The
        ONE predicate for the shown version, the automatic install and the
        customer's Update button: when only the automatic path checked the
        ceiling, a GX pinned at 3.80 showed "3.81 available" on Software updates
        and the button would have installed it.
        """
        offered = _version_text(
            self.coordinator.firmware_value(FIRMWARE_ONLINE_AVAILABLE_VERSION),
        )
        if not offered:
            return None
        hass = getattr(self, "hass", None)
        target = cerbo_target_version(hass, self._entry_id) if hass is not None else None
        if target and not at_or_below(offered, target):
            return None
        return offered

    @property
    def available(self) -> bool:
        # Unprimed reads as unavailable, not as a confident "up to date".
        return (
            super().available
            and self.coordinator.firmware_value(FIRMWARE_INSTALLED_VERSION)
            is not None
        )

    @property
    def in_progress(self) -> bool:
        state = self.coordinator.firmware_value(FIRMWARE_STATE)
        try:
            return int(state) in FIRMWARE_INSTALLING_STATES
        except (TypeError, ValueError):
            return False

    @property
    def update_percentage(self) -> int | None:
        if not self.in_progress:
            return None
        raw = self.coordinator.firmware_value(FIRMWARE_PROGRESS)
        if raw is None:
            return None
        try:
            return max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What this is and which Sensa device it belongs to.

        Read by the App's phone-home, which lists every van's equipment firmware
        for Hub's Deployments page and matches the kind to the rollout group's
        ceiling column. The App reads entity STATES and has no access to HA's
        registries, so the kind has to travel on the state. `sensa_` prefixed so
        it can never shadow one of the update platform's own attributes.
        """
        return {
            "sensa_firmware_kind": "victron_gx",
            "sensa_device_id": self._device_id,
            # The runtime key of the pause switch this entity obeys, so the
            # firmware report reads the same flag the auto-install does.
            "sensa_auto_update_key": RUNTIME_VICTRON_AUTO_UPDATE,
        }

    @property
    def device_info(self) -> dict[str, Any]:
        from .victron_entities import _device_info

        return _device_info(self._device_id, self.coordinator.data or {})

    # -- the rollout group's target -----------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        parent = getattr(super(), "_handle_coordinator_update", None)
        if parent is not None:
            parent()
        self._log_install_landed()
        self._enforce_no_self_install()
        self._maybe_auto_install()

    @callback
    def _log_install_landed(self) -> None:
        """One System Log line when the GX comes back on the version we asked for."""
        if not self._awaiting_version:
            return
        if not same_version(self.installed_version, self._awaiting_version):
            return
        hass = getattr(self, "hass", None)
        if hass is not None:
            self._log(
                hass, "info",
                f"Victron Cerbo firmware (Venus OS) {self._awaiting_version} "
                "is installed and the Cerbo is back on the network.",
            )
        self._awaiting_version = None

    @callback
    def _enforce_no_self_install(self) -> None:
        """Stop a PINNED GX installing Venus OS on its own.

        The GX has its own auto-update setting and will happily install a new
        Venus OS on its own schedule. That is the one thing that can walk a van
        straight past its rollout pin, and it would do it silently: the pin
        would look correct in Admin while the van moved anyway.

        So whenever this box has a pin, a GX set to "check and update" is moved
        to "check only". It keeps checking, so the offer stays visible and we
        can still move the ring forward deliberately; it just never decides to
        install by itself.

        Only ever writes on an explicit reading of the self-installing value. A
        GX that has not published the setting is "could not ask", and guessing
        it is unsafe would mean writing a setting on a customer's gateway on no
        evidence. Re-checked on every coordinator update, so a setting changed
        back on the GX itself is put right again rather than being enforced
        once at startup and then quietly drifting.
        """
        hass = getattr(self, "hass", None)
        if hass is None:
            return
        if not cerbo_target_version(hass, self._entry_id):
            return
        current = self.coordinator.auto_update_value()
        if current is None:
            return
        try:
            if int(current) != AUTO_UPDATE_CHECK_AND_UPDATE:
                return
        except (TypeError, ValueError):
            return
        _LOGGER.info(
            "Cerbo %s is pinned but set to install Venus OS by itself; "
            "moving it to check-only so the pin holds",
            self._device_id,
        )
        hass.async_create_task(
            self.coordinator.async_set_auto_update(AUTO_UPDATE_SAFE_FOR_PIN),
        )

    @callback
    def _maybe_auto_install(self) -> None:
        """Install when Venus is offering exactly the version we were told to.

        The group's target is a CEILING, not a shopping list. Venus only ever
        offers something newer than what is installed, so "install anything it
        offers that is at or below the target" walks a van UP to the ceiling
        and then holds it there for good:

            pinned at 3.2, running 2.45
              Victron release 3.0   -> installs (3.0 is below the ceiling)
              Victron release 3.12  -> installs (3.12 is below 3.20)
              Victron release 3.47  -> refused, and stays refused

        Venus cannot be told to install a NAMED version, which is why the rule
        is expressed this way round: its only lever installs whatever it is
        currently offering, so all we can do is decide whether to pull it.

        With NO ceiling there is no ring to hold and the customer's toggle
        decides, so anything Venus offers is allowed through to it. The GX is
        left on its own auto-update setting in that case (`_enforce_no_self_
        install` only intervenes on a pinned van), which is the same outcome
        by a different route.
        """
        offered = _version_text(
            self.coordinator.firmware_value(FIRMWARE_ONLINE_AVAILABLE_VERSION),
        )
        if not offered or same_version(offered, self._auto_installed):
            return
        hass = getattr(self, "hass", None)
        if hass is None:
            return
        target = cerbo_target_version(hass, self._entry_id)
        from ..update import (
            INSTALL_SLOT_CERBO,
            note_install_pending,
            start_auto_install,
        )

        # No ceiling is not a ceiling of zero: an unpinned van is one this
        # group is not managing, and the customer's toggle below is the whole
        # decision. A pinned one is held to the ring.
        allowed = self._offer() is not None
        # The customer's pause switch (System -> Software updates). Read before
        # the queue is told anything, because a PAUSED Cerbo is not waiting its
        # turn: it is not going to install at all, and marking it as waiting
        # would hold anything below it back for as long as the pause lasted.
        from ..update import auto_update_allowed

        unpaused = auto_update_allowed(
            hass, self._entry_id, RUNTIME_VICTRON_AUTO_UPDATE,
        )
        # The Cerbo is LAST in the install order, so nothing waits on this mark;
        # it is written anyway so the queue's picture of the van is complete and
        # a slot added below it later does not have to remember to.
        note_install_pending(
            hass, self._entry_id, INSTALL_SLOT_CERBO,
            allowed and unpaused and not self.in_progress,
        )
        if not allowed:
            return
        if self.in_progress:
            return
        if not unpaused:
            _LOGGER.debug(
                "Cerbo %s: Venus OS %s allowed by the rollout group but "
                "automatic Victron updates are paused by the customer",
                self._device_id, offered,
            )
            return
        # One install at a time, in layer order: a Venus flash reboots the GX
        # and takes every Victron reading on the van away for minutes, so it
        # waits for the operating system, Sensa itself and the network kit.
        started = start_auto_install(
            hass, self._entry_id, INSTALL_SLOT_CERBO,
            self._async_auto_install,
            what=f"Cerbo {self._device_id} Venus OS {offered}",
        )
        if not started:
            return
        self._auto_installed = offered
        _LOGGER.info(
            "Cerbo %s: Venus OS %s is on offer and %s; installing",
            self._device_id, offered,
            f"the rollout group allows up to {target}" if target
            else "the rollout group sets no ceiling, so automatic updates are "
                 "the customer's setting",
        )

    # -- the one write ------------------------------------------------------

    async def _async_auto_install(self) -> None:
        """The automatic path's install, with a refusal swallowed: nothing is
        waiting on it, and the ceiling can move between the check and the slot."""
        try:
            await self.async_install(None, False)
        except HomeAssistantError as err:
            _LOGGER.warning("Cerbo %s: automatic Venus OS install refused: %s", self._device_id, err)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any,
    ) -> None:
        """Install the update Venus has already found.

        Guarded on Venus actually offering a newer version. A write to
        `Firmware/Online/Install` acts whatever the payload says and reboots
        the GX, so "install whatever is there" must never be reachable when
        there is nothing there: without this guard a stray service call on a
        current GX would still reboot the customer's van.
        """
        offered = _version_text(
            self.coordinator.firmware_value(FIRMWARE_ONLINE_AVAILABLE_VERSION),
        )
        if not offered:
            _LOGGER.warning(
                "Cerbo %s: install requested but Venus is offering no update "
                "(Firmware/Online/AvailableVersion is empty); ignoring",
                self._device_id,
            )
            return
        hass = getattr(self, "hass", None)
        if self._offer() is None:
            # Past the rollout ceiling. Raised, not returned: HA awaits this for
            # the Update button, and a quiet return reads as a finished install.
            raise HomeAssistantError(
                f"Venus OS {offered} is newer than this van's rollout group allows "
                f"({cerbo_target_version(hass, self._entry_id)}).",
            )
        ok = await self.coordinator.async_install_firmware()
        if not ok:
            _LOGGER.warning(
                "Cerbo %s: Venus OS install publish failed; the MQTT link is "
                "not writable yet",
                self._device_id,
            )
            if hass is not None:
                self._log(
                    hass, "warning",
                    f"Victron Cerbo firmware (Venus OS) {offered} did not "
                    "start installing; the Cerbo could not be reached. Sensa "
                    "will try again on the next check.",
                )
            return
        _LOGGER.info(
            "Cerbo %s: asked Venus OS to install %s; the GX reboots to finish",
            self._device_id, offered,
        )
        self._awaiting_version = offered
        if hass is not None:
            self._log(
                hass, "info",
                f"Installing Victron Cerbo firmware (Venus OS) {offered}. "
                "Victron readings drop for a few minutes while the Cerbo "
                "restarts.",
            )

    def _log(self, hass: Any, level: str, message: str) -> None:
        """One customer-facing line in Settings -> System Log."""
        try:
            from .._log import sensa_log

            sensa_log(
                hass, level, message,
                source=SENSA_LOG_SOURCE_SYSTEM,
                context={"device_id": self._device_id, "area": "cerbo_firmware"},
            )
        except Exception:
            _LOGGER.debug(
                "Cerbo %s: sensa_log write failed", self._device_id,
                exc_info=True,
            )


def build_cerbo_update_entities(
    coordinator: Any, entry_id: str, device_id: str,
) -> list[VictronCerboUpdate]:
    """The update entity set for one registered Cerbo GX.

    A list of one, shaped like the other vendor builders so the platform and
    the add-dispatch call it the same way as everything else.
    """
    return [VictronCerboUpdate(coordinator, entry_id, device_id)]
