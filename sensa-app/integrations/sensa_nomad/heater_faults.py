"""Autoterm heater faults and thermostat switching into the vehicle log.

A heater's fault code reaches Nomad two ways, and this watches both:

- the controller's "Fault Code" sensor (raw, 0 = no fault), which the Flow
  (Baltica) fills from every status frame;
- the controller's "Diagnostics Frame" (the vendor tool's extended read),
  decoded by `heater_diagnostics.diagnostics_for`. This is the ONLY way an Air
  2D / 4D reports its code, and it needs the device row's Heater Model set.

Either way it writes one customer-readable line to the vehicle log
(Intelligence -> Log), translated by `autoterm_faults.describe_fault`:

- a new code, or a different one:  error  "<heater>: fault 78, Flame went out
  while running"  (an error, so it also raises a notification);
- back to 0 after a fault:         info   "<heater>: fault cleared".

Edge-triggered on the code and keyed by HEATER, not entity, so a Baltica
reporting the same code through both sources is still one line. An
unavailable, unknown or undecodable reading is "could not ask", never a clear:
it neither logs nor forgets the last code. The last code is held in memory, so
a fault still latched across a Home Assistant restart is logged once more
after it: a repeated line rather than a missed fault.

Thermostat switching is logged as info, one line per switch:

- the Flow: its controller runs the thermostat and reports it on "Thermostat"
  (`heating` = restarted below the target, `holding` = stopped at it);
- the Air 2D / 4D: the heater runs its own, read from the decoded run state
  (`autoterm_diag.thermostat_phase`): working or re-igniting into the idle is
  "holding", the idle into re-ignition is "heating". Needs the Heater Model.

The first reading after start-up only primes: a state already in force is not
a switch.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback

from .const import SENSA_LOG_SOURCE_VEHICLE

_LOGGER = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(seconds=60)


def _code(state: str | None) -> int | None:
    """The fault code in a Fault Code state, or None when there is no reading."""
    if state in (None, "", "unknown", "unavailable"):
        return None
    try:
        return int(float(state))
    except (TypeError, ValueError):
        return None


def thermostat_message(
    name: str, phase: str | None, previous: str | None, *, liquid: bool, baltica: bool,
    coolant: str | None, current: str | None, target: str | None,
) -> str | None:
    """The vehicle-log line for a thermostat switch, or None for no switch.

    A liquid heater's "now" is its coolant: its current_temperature can be
    the panel's room reading. A Baltica's target_temperature is the ROOM (its
    Heating Temperature, 5-30), while its coolant stops at a fixed 88 / 70 in
    the firmware, so it has no coolant target to print: the log said "coolant
    reached the target of 5 °C" for a stop at 88 (2026-10-17)."""
    now = (coolant if liquid else None) or current
    what = "coolant" if liquid else "cabin"
    if phase == "holding" and previous in ("working", "heating", "on"):
        if baltica:
            message = f"{name}: coolant reached its limit"
            if now:
                message += f" ({now} °C)"
        else:
            message = f"{name}: {what} reached the target"
            if target:
                message += f" of {target} °C"
        return message + (", heater paused" if liquid else ", heater idling")
    if phase == "heating" and previous == "holding":
        message = f"{name}: {what} dropped"
        if now:
            message += f" to {now} °C"
        return message + ", heater restarted"
    return None


@callback
def async_setup_heater_fault_logger(hass: HomeAssistant, entry: ConfigEntry) -> None:
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_interval,
    )

    from ._log import sensa_log
    from .autoterm_diag import thermostat_phase
    from .autoterm_faults import describe_fault
    from .heater_diagnostics import diagnostics_for, heater_rows, model_for

    # entity_id -> (kind, heater row); kind is "code", "frame" or "thermostat".
    watched: dict[str, tuple[str, dict[str, Any]]] = {}
    last: dict[str, int] = {}  # device_id -> last code logged (0 = clear)
    phases: dict[str, str | None] = {}  # device_id -> last thermostat phase
    tracker: dict[str, Any] = {"unsub": None}

    def _read(entity_id: str, state: str | None) -> int | None:
        kind, row = watched[entity_id]
        if kind == "code":
            return _code(state)
        # A frame: decoded with the heater's model; None until it is chosen.
        code = diagnostics_for(hass, entry, row).get("fault_code")
        return code if isinstance(code, int) else None

    def _reading(row: dict[str, Any], role: str) -> str | None:
        """A role's state as a whole number of degrees, or None."""
        entity_id = (row.get("service_entities") or {}).get(role)
        st = hass.states.get(entity_id) if entity_id else None
        try:
            return f"{float(st.state):.0f}" if st else None
        except (TypeError, ValueError):
            return None

    @callback
    def _thermostat(entity_id: str, state: str | None) -> None:
        kind, row = watched[entity_id]
        if kind == "thermostat":
            phase = state if state in ("off", "on", "heating", "holding") else None
            if phase is None:
                return  # could not ask
        else:
            if (row.get("service_entities") or {}).get("thermostat"):
                return  # the controller reports its own thermostat
            info = diagnostics_for(hass, entry, row)
            if info.get("status") != "ok":
                return
            phase = thermostat_phase(info.get("model"), info.get("state_index"))
        device_id = str(row.get("device_id") or "")
        primed = device_id in phases
        previous = phases.get(device_id)
        if phase == previous:
            return
        name = row.get("display_name") or row.get("name") or "Heater"
        liquid = row.get("service_class") == "hydronic_heater"
        baltica = "baltica_mode" in (row.get("service_entities") or {})
        message = thermostat_message(
            name, phase, previous, liquid=liquid, baltica=baltica,
            coolant=_reading(row, "coolant_temperature") if liquid else None,
            current=_reading(row, "current_temperature"),
            target=_reading(row, "target_temperature"),
        )
        written = True
        if primed and message:
            written = sensa_log(
                hass, "info", message,
                source=SENSA_LOG_SOURCE_VEHICLE,
                context={"device_id": device_id, "thermostat": phase},
            )
        if written:
            phases[device_id] = phase

    @callback
    def _check(entity_id: str, state: str | None) -> None:
        if entity_id not in watched:
            return
        kind, _ = watched[entity_id]
        if kind in ("thermostat", "frame"):
            _thermostat(entity_id, state)
        if kind == "thermostat":
            return
        code = _read(entity_id, state)
        if code is None:
            return  # could not ask: keep the last code
        _, row = watched[entity_id]
        device_id = str(row.get("device_id") or "")
        name = row.get("display_name") or row.get("name") or "Heater"
        previous = last.get(device_id)
        if code == previous:
            return
        written = True
        if code > 0:
            fault_text = describe_fault(row.get("service_class"), code, model_for(entry, row))
            written = sensa_log(
                hass, "error",
                f"{name}: fault {code}, {fault_text}",
                source=SENSA_LOG_SOURCE_VEHICLE,
                context={"device_id": device_id, "fault_code": code},
            )
        elif previous:
            written = sensa_log(
                hass, "info", f"{name}: fault cleared",
                source=SENSA_LOG_SOURCE_VEHICLE,
                context={"device_id": device_id, "fault_code": 0},
            )
        # Only remember a code once its line is written: early in setup the
        # log store is not up yet and sensa_log returns False, and a fault
        # marked as seen then would never reach the log.
        if written:
            last[device_id] = code

    @callback
    def _on_change(event: Event) -> None:
        new = event.data.get("new_state")
        _check(event.data.get("entity_id", ""), new.state if new else None)

    @callback
    def _refresh(_now: Any = None) -> None:
        """Pick up heaters added (or reflashed) since the last look."""
        try:
            rows = heater_rows(hass, entry)
        except Exception:
            _LOGGER.debug("Heater faults: role map failed", exc_info=True)
            return
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        for row in rows:
            roles = row.get("service_entities") or {}
            if roles.get("fault_code"):
                found[roles["fault_code"]] = ("code", row)
            if roles.get("diagnostics_frame"):
                found[roles["diagnostics_frame"]] = ("frame", row)
            if roles.get("thermostat"):
                found[roles["thermostat"]] = ("thermostat", row)
        changed = set(found) != set(watched)
        watched.clear()
        watched.update(found)  # rows (names, model choice) refresh every minute
        if changed:
            if tracker["unsub"] is not None:
                tracker["unsub"]()
                tracker["unsub"] = None
            if watched:
                tracker["unsub"] = async_track_state_change_event(
                    hass, list(watched), _on_change,
                )
        # A fault already latched when we start watching, one whose line could
        # not be written yet, or one that becomes readable once the Heater
        # Model is chosen, is logged now.
        for entity_id, (kind, row) in watched.items():
            if kind == "thermostat":
                if str(row.get("device_id") or "") not in phases:
                    st = hass.states.get(entity_id)
                    _thermostat(entity_id, st.state if st else None)
                continue
            if str(row.get("device_id") or "") not in last:
                st = hass.states.get(entity_id)
                _check(entity_id, st.state if st else None)

    _refresh()
    entry.async_on_unload(async_track_time_interval(hass, _refresh, REFRESH_INTERVAL))

    @callback
    def _stop() -> None:
        if tracker["unsub"] is not None:
            tracker["unsub"]()

    entry.async_on_unload(_stop)
