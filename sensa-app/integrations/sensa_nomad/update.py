"""Update platform for Sensa Nomad.

Device firmware, one entity per device:

  * the Venus OS of a registered Cerbo GX (`vendors/victron_update.py`)
  * the RutOS of a registered Teltonika router (`vendors/teltonika_update.py`)
  * the firmware of each Sensa ESP device we build (`esp_update.py`), installed
    over the van LAN from the App's checked copy
  * the firmware of a Zigbee device, through Zigbee2MQTT and only from the list
    Sensa has approved (`zigbee_firmware.py`)

Sensa's OWN stack update is not here; that is the appliance OTA path, driven by
`button.sensa_nomad_apply_update` and the Admin card, and the two must not be
confused. The design for all of it is
`docs/system-design/firmware-updates.md`.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.update import UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def within_update_window(hass: Any) -> bool:
    """Is the local time inside the customer's update window (Settings ->
    System, `select.sensa_nomad_host_update_window`)?

    The ONE window check for every automatic install: Sensa's own update
    (`__init__._ota_within_update_window` defers to this) and every device's
    firmware (`start_auto_install`), so the window the customer picked governs
    them all. Until 2026-10-08 device firmware never asked, and a newly
    published Sensa device release flashed vans mid-afternoon.

    Fails OPEN when the window cannot be resolved (the select not registered
    yet, `unknown` / `unavailable`, an unparseable value): unresolved means
    ungated, not gated by a window nobody chose.
    """
    from homeassistant.util import dt as dt_util

    from .backup_config import _parse_window, _within
    from .const import HOST_UPDATE_WINDOW_SELECT_ENTITY_ID

    try:
        state = hass.states.get(HOST_UPDATE_WINDOW_SELECT_ENTITY_ID)
    except Exception:  # noqa: BLE001 - could not ask: not a refusal
        return True
    raw = state.state if state is not None else None
    if not isinstance(raw, str) or raw in ("", "unknown", "unavailable"):
        return True
    parsed = _parse_window(raw)
    if parsed is None:
        return True
    start, end = parsed
    return _within(dt_util.now().time(), start, end)


def auto_update_allowed(hass: Any, entry_id: str, runtime_key: str) -> bool:
    """Is automatic firmware install allowed for this device family right now?

    The one reader of the customer's pause switches (`switch.sensa_nomad_
    victron_auto_update` / `..._teltonika_auto_update`), kept here rather than in
    each vendor module so the two cannot drift: one predicate, one place.

    Fails OPEN. A missing runtime or a missing flag means the switch platform has
    not restored yet, which is not the same as the customer having said no, and
    the same rule the appliance OTA poller uses. The rollout ceiling decides WHAT
    may be installed; where a group names no ceiling this switch is the whole
    decision, because it is the only one of the two the customer can see.

    Only the automatic path consults this. `async_install` still installs when
    called, because a service call is somebody deliberately overriding; what
    the CUSTOMER sees is the Admin panel's Update now, and that reads the same
    switch and does not offer a paused row (firmware-updates.md section 6).
    """
    try:
        runtime = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    except Exception:  # noqa: BLE001 - no runtime is not a refusal
        return True
    value = runtime.get(runtime_key)
    return True if value is None else bool(value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sensa Nomad update entities from a config entry."""
    runtime: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]

    # Expose the add callback so a Cerbo registered post-setup (through the
    # Discovery picker) gets its update entity without an integration reload,
    # mirrors vendor_add_sensors / vendor_add_selects.
    runtime["vendor_add_update"] = async_add_entities

    # Drain anything the Victron start path queued before this platform
    # registered. The Cerbo coordinator routinely starts first, and without
    # this drain its update entity would be dropped for the session, the same
    # trap that once stranded the Cerbo's system-aggregate sensors.
    pending = runtime.pop("vendor_pending_update", None)
    if pending:
        _LOGGER.info(
            "Sensa Nomad update setup: flushing %d entity(s) queued before "
            "the platform was ready",
            len(pending),
        )

    entities: list[UpdateEntity] = list(pending or [])
    if entities:
        async_add_entities(entities)

    # Sensa's own ESP devices: one read-only row each, so the phone-home carries
    # what the van's own kit is running. Registers its own coordinator listener
    # rather than being built here, because these devices have no vendor start
    # path to queue them — see `esp_update.async_register_esp_firmware`.
    from .esp_update import async_register_esp_firmware

    async_register_esp_firmware(hass, entry, runtime, async_add_entities)

    # Zigbee devices whose firmware Sensa has approved (`zigbee_firmware.py`).
    from .zigbee_firmware import async_register_zigbee_firmware

    async_register_zigbee_firmware(hass, entry, runtime, async_add_entities)


# -- One install at a time, in a fixed order ---------------------------------
#
# Several things on a van can reflash themselves, and left alone they all decide
# independently, on their own clocks. Two at once is the case that hurts: a
# router flash drops the LAN for a couple of minutes, which is the same LAN the
# Sensa OTA is pulling its image over and the same one the Cerbo is reached on.
#
# So automatic installs are serialised, and the order is fixed rather than
# first-to-notice-wins, because the layers underneath have to move first: the
# operating system carries everything above it, then Sensa's own stack, then the
# equipment that stack talks to.
INSTALL_SLOT_HOST = "host"
INSTALL_SLOT_SENSA = "sensa"
INSTALL_SLOT_TELTONIKA_ROUTER = "teltonika_router"
INSTALL_SLOT_TELTONIKA_SWITCH = "teltonika_switch"
INSTALL_SLOT_CERBO = "cerbo"
# Sensa's own ESP devices, relay controllers after the rest: a relay flash drops
# every channel for the reboot, so it waits for everything else to be done.
INSTALL_SLOT_SENSA_ESP = "sensa_esp"
INSTALL_SLOT_SENSA_ESP_RELAY = "sensa_esp_relay"

INSTALL_ORDER: tuple[str, ...] = (
    INSTALL_SLOT_HOST,
    INSTALL_SLOT_SENSA,
    INSTALL_SLOT_TELTONIKA_ROUTER,
    INSTALL_SLOT_TELTONIKA_SWITCH,
    INSTALL_SLOT_CERBO,
    INSTALL_SLOT_SENSA_ESP,
    INSTALL_SLOT_SENSA_ESP_RELAY,
)

RUNTIME_INSTALL_HOLDER = "install_slot_holder"
RUNTIME_INSTALL_PENDING = "install_slot_pending"

# A slot that claimed the lock and never released it would stall every slot
# below it for the life of the session. Nothing here takes anywhere near this
# long; the slowest is a container OTA pulling an image over a van's 4G, which
# is minutes. Past this the hold is treated as abandoned, and said out loud.
_INSTALL_HOLD_MAX = 7200  # seconds

# How long a "this slot has work waiting" mark is believed. Every producer that
# writes one re-evaluates on a timer well inside this (a router polls every 30
# seconds, the backup poller a few times an hour), so a mark this old means the
# producer stopped reporting. Erring towards forgetting: the cost of forgetting
# is two installs landing in the wrong order once, the cost of remembering for
# ever is a van that never updates its equipment again.
_INSTALL_PENDING_TTL = 1800  # seconds


def _install_runtime(hass: Any, entry_id: str) -> dict[str, Any] | None:
    try:
        runtime = (hass.data.get(DOMAIN) or {}).get(entry_id)
    except Exception:  # noqa: BLE001 - no runtime is no gate
        return None
    return runtime if isinstance(runtime, dict) else None


def note_install_pending(hass: Any, entry_id: str, slot: str, pending: bool) -> None:
    """Record whether `slot` has an automatic install waiting to start.

    Written by each producer as it evaluates, and read only by the slots BELOW
    it: this is what makes the order mean "the router waits for Sensa" rather
    than "whoever polled first wins". The host and Sensa slots are not marked
    here, because their answer is already on the runtime to be read.
    """
    runtime = _install_runtime(hass, entry_id)
    if runtime is None:
        return
    marks: dict[str, float] = runtime.setdefault(RUNTIME_INSTALL_PENDING, {})
    if pending:
        marks[slot] = time.monotonic()
    else:
        marks.pop(slot, None)


def _slot_has_work(hass: Any, entry_id: str, slot: str) -> bool:
    """Does `slot` have an automatic install waiting?"""
    runtime = _install_runtime(hass, entry_id)
    if runtime is None:
        return False
    if slot == INSTALL_SLOT_HOST:
        # The host's apt agent is an on-HOST systemd timer: Sensa cannot start
        # it, cannot wait on it, and the status.json it publishes carries no
        # in-progress flag at all (last_run / last_result / next_run /
        # reboot_pending / auto). So the OS's turn is expressed with the one
        # field that does say it has unfinished business: a box waiting to
        # reboot into new packages is not a box to start flashing a router on.
        from .const import RUNTIME_HOST_UPDATE_STATUS

        status = runtime.get(RUNTIME_HOST_UPDATE_STATUS) or {}
        return bool(status.get("reboot_pending"))
    if slot == INSTALL_SLOT_SENSA:
        # True while the update is outstanding AND while it is being applied:
        # the installed version only changes once the new stack is running. So
        # this one answer covers both halves, which is why the Sensa path takes
        # no slot of its own.
        from . import ota_update_pending  # inline: __init__ imports this module

        try:
            return bool(ota_update_pending(hass, entry_id))
        except Exception:  # noqa: BLE001 - an unanswerable gate never blocks
            return False
    marked = (runtime.get(RUNTIME_INSTALL_PENDING) or {}).get(slot)
    if marked is None:
        return False
    return (time.monotonic() - marked) < _INSTALL_PENDING_TTL


def _install_holder(hass: Any, entry_id: str) -> str | None:
    """Which slot is installing right now, or None. Clears an abandoned hold."""
    runtime = _install_runtime(hass, entry_id)
    if runtime is None:
        return None
    held = runtime.get(RUNTIME_INSTALL_HOLDER)
    if not isinstance(held, tuple) or len(held) != 2:
        return None
    slot, since = held
    if (time.monotonic() - since) >= _INSTALL_HOLD_MAX:
        _LOGGER.warning(
            "Update slot '%s' has been held for over %d minutes and is being "
            "treated as abandoned; the next update may start",
            slot, _INSTALL_HOLD_MAX // 60,
        )
        runtime.pop(RUNTIME_INSTALL_HOLDER, None)
        return None
    return str(slot)


def install_slot_ready(hass: Any, entry_id: str, slot: str) -> bool:
    """May `slot` start an automatic install right now?

    False while anything at all is installing (one at a time), and false while
    anything EARLIER in `INSTALL_ORDER` has work waiting. It says nothing about
    whether this slot HAS an update, whether the customer paused it, or whether
    the rollout group allows it: those are each producer's own checks and they
    all still run.

    Fails OPEN on anything it cannot read. An unanswerable gate is "could not
    ask", and holding a van's updates for ever on that is worse than two
    installs landing in the wrong order.
    """
    if slot not in INSTALL_ORDER:
        return True
    if _install_runtime(hass, entry_id) is None:
        return True
    holder = _install_holder(hass, entry_id)
    if holder is not None:
        _LOGGER.debug("Update slot '%s' waiting: '%s' is installing", slot, holder)
        return False
    for earlier in INSTALL_ORDER[: INSTALL_ORDER.index(slot)]:
        if _slot_has_work(hass, entry_id, earlier):
            _LOGGER.debug(
                "Update slot '%s' waiting its turn: '%s' has work outstanding",
                slot, earlier,
            )
            return False
    return True


def start_auto_install(
    hass: Any, entry_id: str, slot: str, coro_factory: Any, *, what: str = "",
) -> bool:
    """Claim `slot` and run its install, releasing when that finishes.

    Returns False without running anything when the slot is not ready, so the
    caller leaves its own "already started this version" latch unset and offers
    the same build again on its next tick.

    Only the AUTOMATIC path comes through here. A customer pressing Update now
    is somebody deliberately overriding, and is never queued behind anything,
    nor held for the window.

    Outside the customer's update window nothing starts: every device's
    automatic firmware install waits for it, as Sensa's own update does.
    """
    if not within_update_window(hass):
        _LOGGER.debug("Update slot '%s' waiting for the update window%s", slot, f" ({what})" if what else "")
        return False
    if not install_slot_ready(hass, entry_id, slot):
        return False
    runtime = _install_runtime(hass, entry_id)
    if runtime is None:
        return False
    runtime[RUNTIME_INSTALL_HOLDER] = (slot, time.monotonic())
    _LOGGER.info(
        "Update slot '%s' taken%s; other automatic updates wait until it "
        "finishes", slot, f" for {what}" if what else "",
    )

    async def _runner() -> None:
        try:
            await coro_factory()
        finally:
            current = _install_runtime(hass, entry_id)
            if current is not None:
                held = current.get(RUNTIME_INSTALL_HOLDER)
                if isinstance(held, tuple) and held[0] == slot:
                    current.pop(RUNTIME_INSTALL_HOLDER, None)
            _LOGGER.debug("Update slot '%s' released", slot)

    hass.async_create_task(_runner())
    return True
