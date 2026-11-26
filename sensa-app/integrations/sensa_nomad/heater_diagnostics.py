"""One heater's diagnostics, assembled once for every consumer.

The heater card's Diagnostics popup (via the `sensa_nomad/heater_diagnostics`
WS command), the service report and the fault logger all read a heater's
extended diagnostics through `diagnostics_for`, so the model choice, the
decode and the fault wording cannot differ between them.

Read-only: this reads entity states and decodes; it never writes to the
heater. "Read now" presses the controller's own Read Diagnostics button,
which only asks the heater for the same frame.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, HEATER_MODEL_OPTION, HEATER_MODEL_UNSET


def heater_rows(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """Every heater row with its role map attached (controller entities)."""
    from . import _attach_heater_entities
    from .const import HEATER_SERVICE_CLASSES
    from .vendors.service_classes import classify

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    devices = ((getattr(coordinator, "data", None) or {}).get("devices")) or []
    rows = _attach_heater_entities(hass, list(devices))
    out = []
    for row in rows:
        service_class = row.get("service_class") or classify(row)
        if service_class in HEATER_SERVICE_CLASSES:
            out.append({**row, "service_class": service_class})
    return out


def model_for(entry: ConfigEntry, row: dict[str, Any]) -> str | None:
    """The diagnostics model for a heater: the Baltica has one; an Air D
    controller has whatever the device row's Heater Model says, or None; an
    Air 9D controller is always the 9D."""
    from .autoterm_diag import is_air_9d

    if row.get("service_class") == "hydronic_heater":
        return "flow_5"
    if is_air_9d(row):
        return "air_9d"
    device_id = str(row.get("device_id") or "")
    chosen = (entry.options.get(HEATER_MODEL_OPTION) or {}).get(device_id)
    return None if chosen in (None, "", HEATER_MODEL_UNSET) else chosen


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


def _live_state(hass: HomeAssistant, entity_id: str | None) -> str | None:
    st = hass.states.get(entity_id) if entity_id else None
    if st is None or st.state in ("", "unknown", "unavailable"):
        return None
    return st.state


def _switch_on(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    """On / off for a switch, None when it is missing or not reporting."""
    state = _live_state(hass, entity_id)
    if state is None:
        return None
    return state == "on" if state in ("on", "off") else None


def _number(value: str | None) -> float | int | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def live_for(
    hass: HomeAssistant, row: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """The engineer popup's Live status: the decoded status frame plus the
    readings the controller publishes that no card shows. Read whether or not
    a diagnostics frame has arrived, because the Flow 5D never answers that
    request and this is all it has. `model` names the Air's status code."""
    from .autoterm_diag import decode_status

    role_map = row.get("service_entities") or {}
    family = "flow" if row.get("service_class") == "hydronic_heater" else "air"
    live: dict[str, Any] = {"readings": {}, "units": {}, "raw": {}}
    frame_entity = role_map.get("status_frame")
    frame = _live_state(hass, frame_entity)
    if frame is not None:
        decoded = decode_status(family, frame, model)
        if decoded is not None:
            live.update(decoded)
            st = hass.states.get(frame_entity)
            live["frame_at"] = _iso(st.last_updated if st else None)
    # Entities the Air controller already publishes; the frame decode covers
    # the same bytes, so these only fill a gap (older firmware, no frame).
    for role, key, unit in (
        ("fan_speed_set", "fan_set_rpm", "rpm"),
        ("fan_speed_actual", "fan_actual_rpm", "rpm"),
        ("external_temperature", "external_temp", "°C"),
        ("glow_current", "glow_plug_current", "A"),
    ):
        value = _number(_live_state(hass, role_map.get(role)))
        if value is not None and key not in live["readings"]:
            live["readings"][key] = value
            live["units"][key] = unit
    firmware = _live_state(hass, role_map.get("heater_firmware"))
    if firmware is not None:
        live["heater_firmware"] = firmware
    # The controller's Live Status switch (firmware 1.0.1): absent on older
    # firmware, and then the popup offers no Live button.
    live_switch = role_map.get("live_status")
    if live_switch:
        live["live_switch"] = live_switch
        live["live_on"] = _live_state(hass, live_switch) == "on"
    return live


# The popup's History chart, in the order an engineer reads a stop: what the
# heater was told and what it said it was doing (bands), then the readings that
# explain it (lines). `group` puts two lines on one panel, so the target is
# drawn over the temperature it is chasing. Roles are `_attach_heater_entities`'.
_HISTORY_BANDS: tuple[tuple[str, str], ...] = (
    ("power", "Heater power"),
    ("run_state", "Heater status"),
    ("thermostat", "Thermostat"),
)
_HISTORY_LINES: tuple[tuple[str, str, str], ...] = (
    # (role, label, group)
    ("flame_sensor", "Combustion temperature", "flame"),
    # The Air's glow plug current (status [17], amps, scale UNVERIFIED): the
    # start-up draw.
    ("glow_current", "Glow plug current", "glow"),
    # What the heater regulates on when a van sensor is the Temperature
    # Source: that sensor's reading as Nomad sends it (`heater_temp_source`).
    ("sensa_temperature", "Temperature sent to heater", "temp"),
    ("target_temperature", "Target temperature", "temp"),
    # On an Air this is status byte [3], the heater's OWN sensor at its air
    # intake (firmware "Heater Intake Temperature", misnamed "Cabin
    # Temperature" before 1.1.2): not the cabin and not the van sensor it may
    # be regulating on. Its own panel, so the two are never read as one.
    ("current_temperature", "{intake}", "intake"),
    # Flow family: the heater's own coolant sensor, which it regulates on.
    ("coolant_temperature", "Coolant temperature", "coolant"),
    ("fault_code", "Fault code", "fault"),
    ("fan_speed_actual", "Fan speed", "fan"),
    ("fuel_pump", "Fuel pump", "pump"),
    ("supply_voltage", "Supply voltage", "volt"),
)


def history_for(row: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """The entities the History chart plots for this heater. The card reads
    their history from HA's recorder; this only says which ones and how to
    label them, so the card never guesses at entity names."""
    role_map = row.get("service_entities") or {}
    # On a Flow-family heater `current_temperature` is the PANEL's room
    # reading (the coolant has its own role); on an Air it is the intake.
    intake = (
        "Panel temperature"
        if row.get("service_class") == "hydronic_heater"
        else "Heater intake temperature"
    )
    bands = [
        {"entity": role_map[role], "label": label}
        for role, label in _HISTORY_BANDS
        if role_map.get(role)
    ]
    lines = [
        {"entity": role_map[role], "label": label.format(intake=intake), "group": group}
        for role, label, group in _HISTORY_LINES
        if role_map.get(role)
    ]
    return {"bands": bands, "lines": lines}


def diagnostics_for(
    hass: HomeAssistant, entry: ConfigEntry, row: dict[str, Any],
) -> dict[str, Any]:
    """Everything the popup and the report show for one heater. Always a
    dict: what is missing is said (`status`), never an exception."""
    from .autoterm_diag import decode
    from .autoterm_faults import fault_info
    from .diesel_use import diesel_for

    role_map = row.get("service_entities") or {}
    frame_entity = role_map.get("diagnostics_frame")
    result: dict[str, Any] = {
        "device_id": str(row.get("device_id") or ""),
        "name": row.get("display_name") or row.get("name") or "Heater",
        "service_class": row.get("service_class"),
        "read_button": role_map.get("read_diagnostics"),
        # The black-box report: the popup presses the button and gathers the
        # pages from this entity (one page per state, `<address> <hex>`).
        "report_button": role_map.get("read_report"),
        "report_entity": role_map.get("report_frame"),
        # The heater's power switch: the firmware refuses Read Report while it
        # is on, so the popup does too. The switch, not the status code: a
        # stopped Air D has sat at 0x0305. None when it cannot be read.
        "power_on": _switch_on(hass, role_map.get("power")),
        # Only a Flow-family heater locks out (fault 13 three times in a row).
        "unlock_button": (
            role_map.get("unlock") if row.get("service_class") == "hydronic_heater" else None
        ),
        # Flow family only: the coolant pump alone, and the general Stop that
        # ends a water pump, fan or fuel pump test.
        "pump_run_button": (
            role_map.get("pump_run") if row.get("service_class") == "hydronic_heater" else None
        ),
        # The general Stop, both families (the Air's fuel test ends with it).
        "stop_button": role_map.get("test_stop"),
        # Air only: fan test run / stop and the fuel pump test.
        "fan_run_button": role_map.get("fan_test_run"),
        "fan_stop_button": role_map.get("fan_test_stop"),
        "fuel_run_button": role_map.get("fuel_test_run"),
        "model": model_for(entry, row),
        "status": "ok",
        "live": live_for(hass, row, model_for(entry, row)),
        "history": history_for(row),
        # Diesel comes from the fuel pump entity, not the diagnostics frame,
        # so it is set before any of the frame's early returns.
        "diesel": diesel_for(hass, entry, str(row.get("device_id") or "")),
    }
    if not frame_entity:
        result["status"] = "no_firmware"  # controller predates the diagnostics read
        return result
    if result["model"] is None:
        result["status"] = "model_not_set"
        return result
    st = hass.states.get(frame_entity)
    frame = st.state if st else None
    if frame in (None, "", "unknown", "unavailable"):
        result["status"] = "no_frame"  # could not ask yet: not a fault
        return result
    decoded = decode(result["model"], frame)
    if decoded is None:
        result["status"] = "unreadable"
        return result
    result.update(decoded)
    result["frame_at"] = _iso(st.last_updated if st else None)
    code = decoded["readings"].get("fault_code")
    if isinstance(code, int):
        result["fault_code"] = code
        if code == 0:
            result["fault_text"] = "No fault"
        else:
            info = fault_info(row.get("service_class"), code, result["model"])
            result["fault_text"] = info["name"]
            result["fault_description"] = info["description"]
            result["fault_steps"] = info["troubleshooting"]
    return result
