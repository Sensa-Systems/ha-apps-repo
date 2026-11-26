"""Firmware update entity for a registered Teltonika router or managed switch.

Both kinds, one class. They differ in exactly two things and neither justifies a
second implementation:

  * the FIRMWARE FAMILY: a router reports `RUTX_R_00.07.24.3`, a TSW switch
    `TSW2_R_00.01.10.2`. Two unrelated streams on their own numbering, which is
    why every comparison here carries the family and refuses to compare across
    it, and why the rollout group holds a separate ceiling for each.
  * WHICH ceiling it reads: `teltonika_target_version` for a router,
    `teltonika_switch_target_version` for a switch.

The FOTA contract itself was read off a live RUTX11. A TSW202's has NOT been
checked on real hardware: if a switch answers the endpoints differently, or not
at all, the reads degrade to "no answer" (the entity reports unknown) and the
install refuses on the first 404 rather than doing something odd. That is the
behaviour a shared implementation buys; it is not the same as verified.

Teltonika hosts the images and the router's own FOTA client fetches them, so
nothing here proxies a binary. Nomad's job is to read what the router has been
offered, decide whether this box's rollout group is allowed to take it, and drive
the four-step RutOS sequence (download, wait, verify, flash). The contract is
`vendors/teltonika_api.py`, read off a live RUTX11 rather than from the public
docs, which describe only the installed-version endpoint.

The rollout mechanism is the Cerbo's (`vendors/victron_update.py`): the rollout
group's target is a CEILING, and the device installs an offered build that sits at
or below it. RutOS, like Venus, cannot be told "install version X" (it takes a
STREAM and installs whatever that stream currently offers), so a ceiling is the
only shape a pin can take: it walks a van up to the named version and then holds
it there.

An EMPTY column is not a ceiling of zero, it is no ceiling: the group is not
managing this kind of device and the customer's own toggle decides. It has to
work that way round because the customer cannot see the column. They see a
switch labelled "Automatic router updates" sitting ON, and a switch that is on
and does nothing is worse than either answer; leaving a van unattended because
nobody filled a box in Admin is not something we can explain to them. Fill the
column to take that decision away from them and hold the ring; clear it to hand
it back.

Unlike the GX, a router has no setting that makes it install firmware on its own
schedule, so there is nothing here matching the Cerbo's "move a pinned GX to
check-only". `rut_fota.enabled` registers the router WITH Teltonika's FOTA service
(`rut.teltonika.lt`); it does not authorise unattended installs. What that
registration does leave open is a push from Teltonika's own FOTA / RMS portal,
which is somebody with that account acting deliberately and is not something Nomad
can fence off.
"""
from __future__ import annotations

import logging
import time
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
    RUNTIME_TELTONIKA_AUTO_UPDATE,
    RUNTIME_TELTONIKA_SWITCH_AUTO_UPDATE,
    SENSA_LOG_SOURCE_SYSTEM,
)
from .teltonika_api import FOTA_STREAM_LATEST, FOTA_STREAM_STABLE
from .teltonika_coordinator import FOTA_CHECK_INTERVAL
from .teltonika_entities import _device_info, teltonika_unique_id

_LOGGER = logging.getLogger(__name__)

# How long a failed install holds the automatic path off the same build. The
# offer is republished on every 30 s poll, so without a hold a device that keeps
# refusing is asked again, and the System Log written to, twice a minute. The
# FOTA check's own cadence, because nothing it knows changes faster than that.
_AUTO_RETRY_HOLD = FOTA_CHECK_INTERVAL.total_seconds()


# Which rollout-group column governs which kind of device. Two columns because
# the two firmware streams are unrelated: one value could only ever be correct
# for one of them, and aiming a router version at a switch is the mistake this
# split exists to make impossible.
PIN_KEY_ROUTER = "teltonika_target_version"
PIN_KEY_SWITCH = "teltonika_switch_target_version"


def teltonika_target_version(
    hass: Any, entry_id: str, pin_key: str = PIN_KEY_ROUTER,
) -> str | None:
    """The HIGHEST firmware version this box's rollout group allows, or None.

    Set per rollout group in Hub Admin (Control -> Rollout Groups, the
    "Teltonika Router" and "Teltonika Switch" columns), handed down on the
    licence check-in and passed through by the App beside the Sensa pin and the
    Cerbo's. A CEILING, so the device takes anything on offer at or below it.
    None means the group names no ceiling, which hands the decision to the
    customer's own pause toggle rather than refusing outright (see the module
    docstring). It is still never "install whatever you find": with no ceiling
    the device takes the STABLE build only.

    Read fresh on each use rather than cached, so clearing the column in Admin
    stops the next install without waiting for anything to be rebuilt.
    """
    try:
        runtime = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
        addon = (runtime.get(RUNTIME_BACKUP_STATUS) or {}).get("addon") or {}
    except Exception:  # noqa: BLE001 - a missing runtime is "no target"
        return None
    text = str(addon.get(pin_key) or "").strip()
    return text or None


def parse_rutos_version(raw: Any) -> tuple[str, tuple[int, ...]] | None:
    """Split a RutOS version string into (family, numbers), or None.

    `RUTX_R_00.07.24.3` -> ("rutx_r", (0, 7, 24, 3))
    `TSW2_R_00.01.10.2` -> ("tsw2_r", (0, 1, 10, 2))
    `00.07.24.3`        -> ("", (0, 7, 24, 3))

    The family prefix is what makes the whole per-device-family question
    tractable: a version string carries the image family it belongs to, so a
    router's build can never be compared against a switch's by accident. A bare
    numeric string keeps an empty family and is treated as matching any, because
    that is what somebody types into an admin column when the column is already
    labelled "router".
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    head, _, tail = text.rpartition("_")
    family = head.lower()
    parts = tail.split(".")
    numbers: list[int] = []
    for part in parts:
        part = part.strip()
        if not part.isdigit():
            return None
        numbers.append(int(part))
    if not numbers:
        return None
    return family, tuple(numbers)


def _padded(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[list[int], list[int]]:
    """Two number tuples padded to the same length, so 00.07.24 sorts against
    00.07.24.3 rather than comparing short."""
    width = max(len(a), len(b))
    return (
        list(a) + [0] * (width - len(a)),
        list(b) + [0] * (width - len(b)),
    )


def same_release(a: Any, b: Any) -> bool:
    """Do two RutOS version strings name the same build?

    Used to match a rollout group's target against what the router is offered.
    An unparseable value on either side is never a match: this decides whether a
    customer's router reboots, so a string we do not understand must not be read
    as agreement. Families must agree unless one side omits it entirely.
    """
    left, right = parse_rutos_version(a), parse_rutos_version(b)
    if left is None or right is None:
        return False
    if left[0] and right[0] and left[0] != right[0]:
        return False
    padded_left, padded_right = _padded(left[1], right[1])
    return padded_left == padded_right


def is_newer(candidate: Any, installed: Any) -> bool:
    """Is `candidate` a strictly newer build of the SAME family as `installed`?

    A different family is never newer, only different: `TSW2_R_00.01.10.2` is not
    an upgrade for a router, however the numbers sort. Anything unparseable is
    not newer either, which keeps a junk value from starting a flash.
    """
    left, right = parse_rutos_version(candidate), parse_rutos_version(installed)
    if left is None or right is None:
        return False
    if left[0] and right[0] and left[0] != right[0]:
        return False
    padded_left, padded_right = _padded(left[1], right[1])
    return padded_left > padded_right


def at_or_below(candidate: Any, ceiling: Any) -> bool:
    """Is `candidate` at or below the rollout group's ceiling?

    The same rule as the Cerbo's `at_or_below`, and for the same reason: the
    router's only lever installs whatever a stream is currently offering, so a
    pin can only ever be a ceiling. Because an offer is always newer than what is
    installed, "take anything on offer at or below the ceiling" walks a van UP to
    the pinned version and then holds it there:

        pinned at RUTX_R_00.07.24.5, running 00.07.22.1
          Teltonika publishes 00.07.23.0  -> installs (below the ceiling)
          Teltonika publishes 00.07.24.5  -> installs (at the ceiling)
          Teltonika publishes 00.07.25.2  -> refused, and stays refused

    A missing or unparseable ceiling refuses everything here, and so does a
    build from another image family. That is this helper's contract, not the
    policy: `_offer` never asks it when the column is blank, because a blank
    column is no ceiling rather than a ceiling nothing can clear.
    """
    left, right = parse_rutos_version(candidate), parse_rutos_version(ceiling)
    if left is None or right is None:
        return False
    if left[0] and right[0] and left[0] != right[0]:
        return False
    padded_left, padded_right = _padded(left[1], right[1])
    return padded_left <= padded_right


class TeltonikaRouterUpdate(CoordinatorEntity, UpdateEntity):
    """Firmware update for one Teltonika router or managed switch.

    `latest_version` follows HA's contract: equal to `installed_version` means up
    to date, and None means unknown. The router distinguishes three answers per
    stream and so does this entity:

      a version string  there is something to install
      "newest"          confidently current
      "N/A" / absent    the FOTA client has no answer

    The third is "could not ask" and is reported as unknown, never as up to date.
    A disabled FOTA client lands there too: a router that was never able to ask
    Teltonika is not a router that is current.
    """

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(
        self, coordinator: Any, entry_id: str, device_id: str, mac: str = "",
        *, is_switch: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry_id = entry_id
        self._mac = mac
        self._is_switch = is_switch
        self._pin_key = PIN_KEY_SWITCH if is_switch else PIN_KEY_ROUTER
        # The customer's pause switch, split the same way the pin is: the router
        # and the switches are held independently, so pausing one never holds
        # the other.
        self._pause_key = (
            RUNTIME_TELTONIKA_SWITCH_AUTO_UPDATE
            if is_switch
            else RUNTIME_TELTONIKA_AUTO_UPDATE
        )
        # Which place this device takes in the one-at-a-time install order. A
        # router goes before a switch: the switch's uplink is the router.
        # Imported here rather than at module scope, matching every other use
        # of `..update` in this file.
        from ..update import (
            INSTALL_SLOT_TELTONIKA_ROUTER,
            INSTALL_SLOT_TELTONIKA_SWITCH,
        )

        self._slot = (
            INSTALL_SLOT_TELTONIKA_SWITCH if is_switch
            else INSTALL_SLOT_TELTONIKA_ROUTER
        )
        # "RutOS" is what a router calls its firmware and what its WebUI shows.
        # A switch is named plainly, because what the TSW line brands its
        # firmware as has not been read off one.
        self._attr_name = "Firmware" if is_switch else "RutOS"
        self._attr_unique_id = teltonika_unique_id(entry_id, device_id, "firmware")
        # The version we have already fired an install for. The offered version
        # keeps being republished on every poll, and for the minutes the router
        # spends downloading and rebooting; without this latch the same update
        # would be started again on every one of those polls.
        self._auto_installed: str | None = None
        # When a FAILED install's latch lets go (monotonic seconds). None on a
        # latch set by an install that started, which holds for good.
        self._auto_retry_at: float | None = None

    # -- what the router has told us -----------------------------------------

    @property
    def installed_version(self) -> str | None:
        data = self.coordinator.data or {}
        text = str(data.get("firmware_version") or "").strip()
        return text or None

    def _fota_answered(self) -> bool:
        """Did the router answer the offered-version question at all?

        The line between "told no" and "could not ask". A stream that named a
        build answered; so did one that said "newest". A stream that is absent,
        empty or "N/A" did not, and a device that answered neither was never
        asked successfully: a TSW202 does exactly that, publishing no FOTA
        answer of any kind.
        """
        data = self.coordinator.data or {}
        return any(
            data.get(key) is not None
            for key in (
                "fota_stable_version", "fota_latest_version",
                "fota_stable_newest", "fota_latest_newest",
            )
        )

    @property
    def latest_version(self) -> str | None:
        offer = self._offer()
        if offer is not None:
            return offer[1]
        # Nothing this device may install. That is "up to date" whenever the
        # router actually answered, and there are three ways to get here:
        # it said "newest"; the builds on offer are not newer than what is
        # running; or the rollout group's ceiling refuses every one of them,
        # which is a van HELD at its pinned version and has nothing to do.
        # Reporting unknown for that third case put "Not known" on the Admin
        # card for a router doing exactly what its ring told it to, which is
        # indistinguishable from a router whose FOTA client never reached
        # Teltonika. Only the never-answered case stays unknown.
        if self._fota_answered():
            return self.installed_version
        return None

    @property
    def available(self) -> bool:
        # Unprimed reads as unavailable, not as a confident "up to date". The
        # installed version is the first thing every poll returns, so this is
        # only ever true before the first successful poll of a session.
        return super().available and self.installed_version is not None

    @property
    def in_progress(self) -> bool:
        return bool(getattr(self.coordinator, "install_in_progress", False))

    @property
    def update_percentage(self) -> int | None:
        percent = getattr(self.coordinator, "install_percent", None)
        if percent is None:
            return None
        return max(0, min(100, int(percent)))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the router was offered, and what we are allowed to take.

        Surfaced because the two streams and the rollout pin are the whole story
        of why a router is or is not updating, and a support call should not need
        a debug log to see it.

        Every key carries the `fota_` prefix of the coordinator field behind it,
        and that is not cosmetic: HA merges `extra_state_attributes` OVER the
        update platform's own, so a key called `latest_version` here REPLACES the
        `latest_version` attribute that carries the entity's verdict. A router
        answering "newest" publishes no version on the latest stream, so the
        clobbered attribute read empty and the Admin card said "Not known" for a
        router that was perfectly up to date.
        """
        data = self.coordinator.data or {}
        offer = self._offer()
        return {
            "fota_enabled": data.get("fota_enabled"),
            "fota_stable_version": data.get("fota_stable_version"),
            "fota_stable_date": data.get("fota_stable_date"),
            "fota_latest_version": data.get("fota_latest_version"),
            "fota_latest_date": data.get("fota_latest_date"),
            "target_version": teltonika_target_version(
                getattr(self, "hass", None), self._entry_id, self._pin_key,
            ) if getattr(self, "hass", None) is not None else None,
            "install_stream": offer[0] if offer else None,
            # The build this device is being offered but is not allowed to take.
            # `latest_version` reads as up to date while a ring holds a device
            # at its pinned build, because there IS nothing to install; that is
            # only honest while the withheld build stays visible, and the Admin
            # card shows this in the Available cell instead of "Up to date".
            "held_version": self._held_by_ceiling(),
            "build_date": data.get("firmware_build_date"),
            # What this is and which Sensa device it belongs to, for the App's
            # phone-home: Hub's Deployments page lists every van's equipment
            # firmware from these and matches the kind to the rollout group's
            # ceiling column. The App reads entity STATES (it has no access to
            # HA's registries), so the kind has to travel on the state.
            "sensa_firmware_kind": (
                "teltonika_switch" if self._is_switch else "teltonika_router"
            ),
            "sensa_device_id": self._device_id,
            # The runtime key of the pause switch this entity obeys, so the
            # firmware report reads the same flag the auto-install does.
            "sensa_auto_update_key": self._pause_key,
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)

    # -- which build, from which stream --------------------------------------

    def _newer_offers(self) -> list[tuple[str, str]]:
        """Every (stream, version) on offer that is strictly newer than what is
        installed, stable first. The ceiling has NOT been applied yet: what a
        device is offered and what its ring will let it take are two different
        questions, asked in two places, from this one list.
        """
        data = self.coordinator.data or {}
        installed = self.installed_version
        if installed is None:
            return []
        candidates = [
            (FOTA_STREAM_STABLE, data.get("fota_stable_version")),
            (FOTA_STREAM_LATEST, data.get("fota_latest_version")),
        ]
        return [
            (stream, str(version))
            for stream, version in candidates
            if version and is_newer(version, installed)
        ]

    def _held_by_ceiling(self) -> str | None:
        """The newest offered build the rollout group will NOT let this device
        take, or None.

        None covers both "nothing newer is on offer" and "the ring allows what
        is on offer", because in neither case is anything being withheld. Only
        reachable when a ceiling is set: with no ceiling `_offer` takes the
        stable build and there is nothing held.
        """
        newer = self._newer_offers()
        if not newer:
            return None
        hass = getattr(self, "hass", None)
        target = (
            teltonika_target_version(hass, self._entry_id, self._pin_key)
            if hass is not None else None
        )
        if not target:
            return None
        if any(at_or_below(version, target) for _stream, version in newer):
            return None
        return max(newer, key=lambda pair: parse_rutos_version(pair[1])[1])[1]

    def _offer(self) -> tuple[str, str] | None:
        """The (stream, version) this entity would install, or None.

        With NO ceiling: stable, because Teltonika promotes latest to stable only
        after several weeks without a critical finding and a van's router is not
        a test rig. The same answer for the customer's button and for the
        automatic path, since with no ring holding the van the two are the same
        decision made by the same person.

        With a ceiling it is the HIGHEST offered build at or below it. Highest,
        not stable-first, so a ring walks up to its pinned version in one reboot
        rather than taking stable now and latest on the next poll.

        Only ever returns a build strictly newer than what is installed, so a
        reflash of the running version is unreachable from here.
        """
        newer = self._newer_offers()
        if not newer:
            return None

        hass = getattr(self, "hass", None)
        target = (
            teltonika_target_version(hass, self._entry_id, self._pin_key)
            if hass is not None else None
        )
        if not target:
            return newer[0]

        allowed = [
            (stream, version)
            for stream, version in newer
            if at_or_below(version, target)
        ]
        if not allowed:
            # Everything on offer is past the ceiling. That is a ring being held
            # back, not a reason to install something else.
            return None
        return max(allowed, key=lambda pair: parse_rutos_version(pair[1])[1])

    # -- the rollout group's target ------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        parent = getattr(super(), "_handle_coordinator_update", None)
        if parent is not None:
            parent()
        self._maybe_auto_install()

    @callback
    def _maybe_auto_install(self) -> None:
        """Install an offered build this box is allowed to take.

        `_offer` has already refused everything that is not both newer than the
        installed build and at or below the ceiling, so reaching here with an
        offer means it is permitted: by the ring where one is pinned, and by the
        stable-only rule where the column is blank. Which of the two it was makes
        no difference here, and deliberately so; the customer's pause toggle is
        the only thing left to ask, and it is asked below.
        """
        hass = getattr(self, "hass", None)
        if hass is None or self.in_progress:
            return
        if (
            self._auto_retry_at is not None
            and time.monotonic() >= self._auto_retry_at
        ):
            self._auto_installed = None
            self._auto_retry_at = None
        offer = self._offer()
        # The customer's pause switch (System -> Software updates). Read before
        # the queue is told anything, because a PAUSED device is not waiting its
        # turn: it is not going to install at all, and marking it as waiting
        # would hold every slot below it back for as long as the pause lasted.
        from ..update import (
            auto_update_allowed,
            note_install_pending,
            start_auto_install,
        )

        allowed = auto_update_allowed(hass, self._entry_id, self._pause_key)
        # What the slots below this one wait on: a build this device is going to
        # take as soon as it is given the chance.
        note_install_pending(
            hass, self._entry_id, self._slot,
            allowed
            and offer is not None
            and not same_release(offer[1], self._auto_installed),
        )
        if offer is None:
            return
        stream, version = offer
        if same_release(version, self._auto_installed):
            return
        if not allowed:
            _LOGGER.debug(
                "Teltonika %s: firmware %s allowed by the rollout group but "
                "automatic updates for this kind of device (%s) are paused by "
                "the customer",
                self._device_id, version,
                "network switch" if self._is_switch else "router",
            )
            return
        # One install at a time, in layer order. A flash drops the van's LAN,
        # which is the network every other update is running over. The latch is
        # set only once the slot is actually taken, so a device made to wait
        # offers the same build again on its next poll.
        started = start_auto_install(
            hass, self._entry_id, self._slot,
            self._async_auto_install,
            what=f"{self._device_id} firmware {version}",
        )
        if not started:
            return
        self._auto_installed = version
        self._auto_retry_at = None
        ceiling = teltonika_target_version(hass, self._entry_id, self._pin_key)
        _LOGGER.info(
            "Teltonika %s: RutOS %s is on offer on the %s stream and %s; "
            "installing",
            self._device_id, version, stream,
            f"the rollout group allows up to {ceiling}" if ceiling
            else "the rollout group sets no ceiling, so automatic updates are "
                 "the customer's setting",
        )

    # -- the install ---------------------------------------------------------

    async def _async_auto_install(self) -> None:
        """The automatic path's install, with the failure swallowed.

        `async_install` RAISES so that a customer pressing Update is told it did
        not work, which is the whole point of it raising. Nothing is waiting on
        this one: it runs in the slot runner's task, the customer-facing line is
        already in the System Log, and letting it out only adds a traceback to
        `homeassistant.core`. The slot is released either way, by the runner's
        own `finally`.
        """
        try:
            await self.async_install(None, False)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Teltonika %s: automatic firmware install failed: %s",
                self._device_id, err,
            )

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any,
    ) -> None:
        """Run the RutOS update sequence and wait out the reboot.

        Returns only once the device is back and running the new build, so the
        Update button's spinner covers the whole thing and what follows it is
        the real answer. That is minutes, most of it the download.

        Refused while a remote-support session is open: the flash drops the van's
        LAN and its internet for a couple of minutes, which takes the tunnel out
        from under whoever is working on the box.

        RAISES `HomeAssistantError` on every refusal and every failure, rather
        than returning. HA awaits this call for the customer's Update button, so
        a quiet `return` reports the install as done: the spinner stops, the
        version does not move, and nothing says why. The automatic path goes
        through `_async_auto_install`, which catches it.
        """
        hass = getattr(self, "hass", None)
        offer = self._offer()
        if offer is None:
            raise HomeAssistantError(
                "There is no newer firmware this device is allowed to install "
                "right now.",
            )
        stream, target = offer

        if hass is not None and _remote_support_open(hass):
            self._log(
                hass, "warning",
                f"Router firmware {target} was not installed: a Sensa remote "
                "support session is open, and the router restart would cut it "
                "off. It will be offered again once the session ends.",
            )
            # Clear the latch so the next poll after the session closes retries,
            # rather than this version being considered done.
            self._auto_installed = None
            self._auto_retry_at = None
            raise HomeAssistantError(
                f"Firmware {target} was not installed: a Sensa remote support "
                "session is open and the restart would cut it off.",
            )

        if hass is not None:
            self._log(
                hass, "info",
                f"Installing router firmware {target}. The van's network drops "
                "for a couple of minutes while the router restarts.",
            )

        ok = await self.coordinator.async_install_firmware(stream)
        if not ok:
            # Hold the automatic path off this build until the next FOTA check,
            # rather than letting the next 30 s poll start it again.
            self._auto_installed = target
            self._auto_retry_at = time.monotonic() + _AUTO_RETRY_HOLD
            if getattr(self.coordinator, "install_forbidden", False) is True:
                user = getattr(
                    getattr(self.coordinator, "client", None), "username", "",
                )
                who = (
                    f"the router user '{user}'" if isinstance(user, str) and user
                    else "the router user"
                )
                reason = (
                    f"The router refused it: {who} Sensa signs in as is not "
                    "allowed to update firmware. Give that user firmware "
                    "permission on the router, or register the router with its "
                    "admin account, then press Update again."
                )
                if hass is not None:
                    self._log(
                        hass, "warning",
                        f"Router firmware {target} was not installed. {reason}",
                    )
                raise HomeAssistantError(
                    f"Firmware {target} was not installed. {reason}",
                )
            if hass is not None:
                self._log(
                    hass, "warning",
                    f"Router firmware {target} did not install. The router is "
                    "still running its current version, or did not come back "
                    "in time; Sensa will try again on the next check.",
                )
            raise HomeAssistantError(
                f"Firmware {target} did not install. The device is still "
                "running its current version, or did not come back in time; "
                "the reason is in the log.",
            )

        if hass is not None:
            self._log(
                hass, "info",
                f"Router firmware {target} is installed and the router is back "
                "on the network.",
            )
        # Ask again now that it is back: the offer it was still advertising is
        # the build now installed. A failure here only leaves the check due and
        # re-runs it on the first poll that answers, so it is never the reason
        # an install that worked reports a failure.
        try:
            await self.coordinator.async_refresh_fota()
        except Exception:
            _LOGGER.debug(
                "Teltonika %s: post-install FOTA re-read failed; it will be "
                "re-checked on the next poll",
                self._device_id, exc_info=True,
            )

    def _log(self, hass: Any, level: str, message: str) -> None:
        """One customer-facing line in Settings -> System Log."""
        try:
            from .._log import sensa_log

            sensa_log(
                hass, level, message,
                source=SENSA_LOG_SOURCE_SYSTEM,
                context={"device_id": self._device_id, "area": "router_firmware"},
            )
        except Exception:
            _LOGGER.debug(
                "Teltonika %s: sensa_log write failed", self._device_id,
                exc_info=True,
            )


def _remote_support_open(hass: Any) -> bool:
    """Is a Sensa remote-support session open right now?

    False when the module or its state is not up: an unknown answer must not
    block a customer's own Update button, and an engineer who is mid-session has
    a live tunnel that will tell them what happened either way.
    """
    try:
        from .. import remote_support

        state = remote_support.get_state(hass)
    except Exception:  # noqa: BLE001
        return False
    return bool(state is not None and getattr(state, "enabled", False))


def build_teltonika_update_entities(
    coordinator: Any, entry_id: str, device_id: str, mac: str = "",
) -> list[TeltonikaRouterUpdate]:
    """The update entity set for one registered Teltonika device: a list of one.

    `hw_mobile` is the router/switch test the rest of the integration uses (a
    switch has no cellular modem), and it decides which rollout-group ceiling
    this entity obeys. It defaults to True to match the client, so a device whose
    first poll has not landed is treated as the router it almost certainly is; a
    switch that came up as a router for one poll reads its own pin from the next
    one, because the entity is rebuilt on reload and the value is read fresh.
    """
    data = coordinator.data or {}
    is_switch = not data.get("hw_mobile", True)
    return [
        TeltonikaRouterUpdate(
            coordinator, entry_id, device_id, mac, is_switch=is_switch,
        )
    ]
