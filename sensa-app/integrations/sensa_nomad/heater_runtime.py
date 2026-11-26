"""Each Autoterm heater's lifetime Total Run Time, from its own memory report.

The heater keeps its total run time in the 0x0B black-box report (decoded in
`autoterm_diag.decode_report`, verified against Autoterm's own software: Living
Heater 1, 226 h, 2026-10-07). The controller reads that report when its Read
Report button is pressed and publishes it a 64-byte page at a time on its
Report Frame text sensor (`<address> <hex>`). The counter sits in the first
page (0x17, or 0x18 on an Air), so that page alone is enough.

Two ways a report gets read, and this module keeps the figure from either:

- the Diagnostics popup's Read report (an engineer), and
- this module, which presses Read Report itself READ_DELAY after a heater's
  run ends and once at start-up for a heater it has no figure for. The heater
  answers the report only while it is off, so never during a run.

The figure is the heater's own counter as of the last read, so it moves after
each run, not during one. It lives in an HA Store so it survives a restart
before the next read. An Air reads nothing until its Heater Model is chosen:
an Air 4D's timers sit one byte earlier than a 2D's, and a guessed layout
would show the wrong hours.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, _relay_device_id_safe

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.heater_runtime"
SIGNAL_RUNTIME_UPDATED = f"{DOMAIN}_heater_runtime_updated"
# After a run ends: long enough for the heater's cool-down to finish, so the
# read lands on a heater that is fully off.
READ_DELAY = timedelta(minutes=5)
# Never press Read Report more often than this for one heater.
MIN_READ_GAP = timedelta(minutes=10)
# How often the heater list is re-checked for new heaters and run states.
SCAN_INTERVAL = timedelta(minutes=1)
FIRST_PAGE = "0000"
# A run state that means the heater is fully off. Not the Flow / Baltica's
# "standby" (stopped by the box's thermostat, still switched on): it restarts
# by itself, and a report is not answered while the heater is streaming.
OFF_STATES = frozenset({"off"})


def runtime_entity_id(device_id: str) -> str:
    return f"sensor.sensa_{_relay_device_id_safe(device_id)}_total_run_time"


def total_run_time_s(page_state: str | None, model: str | None) -> int | None:
    """The total run time in a Report Frame state, when it is the first page
    of a report this module can read; else None.

    An Air needs its model: None for an Air with none chosen."""
    from .autoterm_diag import REPORT_LAYOUT_AIR, decode_report

    text = (page_state or "").strip()
    addr, _, page = text.partition(" ")
    if addr.lower() != FIRST_PAGE or not page:
        return None
    try:
        layout = bytes.fromhex(page)[2]
    except (ValueError, IndexError):
        return None
    if layout == REPORT_LAYOUT_AIR and model not in ("air_2d", "air_4d"):
        return None
    decoded = decode_report(page, model)
    for field in (decoded or {}).get("fields") or []:
        if field.get("key") == "total_run_time_s":
            value = field.get("value")
            return int(value) if isinstance(value, int) else None
    return None


class RuntimeTracker:
    """Keeps every Autoterm heater's last read Total Run Time, and reads it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, STORE_VERSION, f"{STORE_KEY}.{entry.entry_id}")
        # device_id -> {"total_run_time_s": int, "read_at": iso, "name": str}
        self.heaters: dict[str, dict[str, Any]] = {}
        # report entity -> device_id, and the unsubscribe for that watch
        self._watched: dict[str, str] = {}
        self._unsub_watch: Any = None
        self._unsub_tick: Any = None
        # device_id -> the run state last seen, and when Read Report was pressed
        self._last_state: dict[str, str] = {}
        self._last_press: dict[str, Any] = {}
        self._pending: dict[str, Any] = {}

    async def async_load(self) -> None:
        try:
            data = await self._store.async_load()
        except Exception:  # a bad file starts afresh
            _LOGGER.warning("Heater run time: could not read the saved figures", exc_info=True)
            data = None
        self.heaters = dict((data or {}).get("heaters") or {})

    def known_heaters(self) -> dict[str, str]:
        return {k: str(v.get("name") or "Heater") for k, v in self.heaters.items()}

    def seconds(self, device_id: str) -> int | None:
        value = (self.heaters.get(device_id) or {}).get("total_run_time_s")
        return value if isinstance(value, int) else None

    def read_at(self, device_id: str) -> str | None:
        return (self.heaters.get(device_id) or {}).get("read_at")

    def start(self) -> None:
        if self._unsub_tick is None:
            self._unsub_tick = async_track_time_interval(self.hass, self._async_scan, SCAN_INTERVAL)
            self.hass.async_create_task(self._async_scan())

    def stop(self) -> None:
        for unsub in (self._unsub_tick, self._unsub_watch, *self._pending.values()):
            if unsub is not None:
                unsub()
        self._unsub_tick = self._unsub_watch = None
        self._pending.clear()

    # -- the heaters ------------------------------------------------------------

    def _rows(self) -> list[dict[str, Any]]:
        from .diesel_use import is_autoterm
        from .heater_diagnostics import heater_rows

        try:
            return [r for r in heater_rows(self.hass, self.entry) if r.get("device_id") and is_autoterm(r)]
        except Exception:  # a resolver hiccup: try again next minute
            _LOGGER.debug("Heater run time: heater rows failed", exc_info=True)
            return []

    async def _async_scan(self, _now: Any = None) -> None:
        rows = self._rows()
        watched: dict[str, str] = {}
        for row in rows:
            device_id = str(row["device_id"])
            roles = row.get("service_entities") or {}
            report = roles.get("report_frame")
            if report:
                watched[report] = device_id
                # A page already there (a restart, or a read made while this
                # was not watching) counts too.
                self._take_page(device_id, row, self._state(report))
            self._check_run_state(device_id, row, roles)
        if watched != self._watched:
            if self._unsub_watch is not None:
                self._unsub_watch()
            self._watched = watched
            self._unsub_watch = (
                async_track_state_change_event(self.hass, list(watched), self._on_page)
                if watched else None
            )

    def _state(self, entity_id: str | None) -> str | None:
        st = self.hass.states.get(entity_id) if entity_id else None
        return None if st is None else str(st.state)

    @callback
    def _on_page(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        device_id = self._watched.get(entity_id or "")
        new = event.data.get("new_state")
        if not device_id or new is None:
            return
        row = next((r for r in self._rows() if str(r["device_id"]) == device_id), None)
        if row is not None:
            self._take_page(device_id, row, str(new.state))

    def _take_page(self, device_id: str, row: dict[str, Any], state: str | None) -> None:
        from .heater_diagnostics import model_for

        seconds = total_run_time_s(state, model_for(self.entry, row))
        if seconds is None:
            return
        name = str(row.get("display_name") or row.get("name") or "Heater")
        old = self.heaters.get(device_id) or {}
        if old.get("total_run_time_s") == seconds and old.get("name") == name:
            return
        self.heaters[device_id] = {
            "total_run_time_s": seconds,
            "read_at": dt_util.utcnow().isoformat(),
            "name": name,
        }
        self._store.async_delay_save(lambda: {"heaters": self.heaters}, 5)
        async_dispatcher_send(self.hass, SIGNAL_RUNTIME_UPDATED)
        _LOGGER.info("Heater %s: total run time %.1f h", device_id, seconds / 3600)

    # -- reading ----------------------------------------------------------------

    def _check_run_state(self, device_id: str, row: dict[str, Any], roles: dict[str, Any]) -> None:
        """Read after a run ends, and once for a heater with no figure yet."""
        state = (self._state(roles.get("run_state")) or "").strip().lower()
        if not state or state in ("unknown", "unavailable"):
            return  # could not ask: nothing is read on a guess
        was = self._last_state.get(device_id)
        self._last_state[device_id] = state
        if state not in OFF_STATES:
            return
        ended = was is not None and was not in OFF_STATES
        if ended or (self.seconds(device_id) is None and was is None):
            self._schedule_read(device_id)

    def _schedule_read(self, device_id: str) -> None:
        if device_id in self._pending:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._pending.pop(device_id, None)
            self.hass.async_create_task(self.async_read(device_id))

        self._pending[device_id] = async_call_later(self.hass, READ_DELAY, _fire)

    async def async_read(self, device_id: str) -> bool:
        """Press the heater's Read Report, if it is off and not read lately."""
        row = next((r for r in self._rows() if str(r["device_id"]) == device_id), None)
        if row is None:
            return False
        roles = row.get("service_entities") or {}
        button = roles.get("read_report")
        state = (self._state(roles.get("run_state")) or "").strip().lower()
        if not button or state not in OFF_STATES:
            return False  # started again meanwhile: the next stop reads it
        now = dt_util.utcnow()
        last = self._last_press.get(device_id)
        if last is not None and now - last < MIN_READ_GAP:
            return False
        self._last_press[device_id] = now
        await self.hass.services.async_call("button", "press", {"entity_id": button}, blocking=False)
        return True


def tracker(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeTracker | None:
    return ((hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}).get("runtime_tracker")


async def async_setup_tracker(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeTracker:
    """The entry's tracker, loaded and running; stopped when the entry unloads."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    existing = runtime.get("runtime_tracker")
    if existing is not None:
        return existing
    rt = RuntimeTracker(hass, entry)
    await rt.async_load()
    runtime["runtime_tracker"] = rt
    rt.start()
    entry.async_on_unload(rt.stop)
    return rt


class SensaHeaterTotalRunTimeSensor(SensorEntity):
    """A heater's lifetime run time, in hours, as of its last report read."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 0

    def __init__(self, entry: ConfigEntry, device_id: str, name: str) -> None:
        self._entry = entry
        self._device_id = device_id
        self._attr_name = f"{name} Total Run Time"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_total_run_time"
        self.entity_id = runtime_entity_id(device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        from .diesel_use import _hub_device_info

        return _hub_device_info(self._entry)

    def _tracker(self) -> RuntimeTracker | None:
        return tracker(self.hass, self._entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_RUNTIME_UPDATED, self.async_write_ha_state)
        )

    @property
    def native_value(self) -> float | None:
        rt = self._tracker()
        seconds = rt.seconds(self._device_id) if rt else None
        return None if seconds is None else round(seconds / 3600, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rt = self._tracker()
        return {"read_at": rt.read_at(self._device_id) if rt else None}
