"""Autoterm extended diagnostics: decoding the vendor tool's frame.

The Air D / Baltica controller firmware asks the heater for its extended
diagnostics frame (the vendor diagnostic tool's own read) and publishes the
reply raw, as hex, on "Diagnostics Frame". This module is the ONE place that
turns those bytes into readings, so a field-map correction is a Nomad update
and never a reflash.

The maps and run-state tables are transcribed from
docs/devices/autoterm/autoterm-uart-protocol.md ("Extended-telemetry field
maps by model group" and "Run-state tables"). Only the groups we ship are here:
G2 (Air 2D), G3 (Air 4D), G5 (Air 9D) and G6 (Flow 5 / Baltica). The Flow
and Air 2D groups are hardware-verified; the Air 4D and Air 9D groups come
from the vendor tool's data and are UNVERIFIED until a bench capture. `p[n]` is payload byte n (0-based), which is
exactly what the firmware publishes.

Read-only by construction: nothing here writes to the heater.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Payload = bytes
Field = tuple[str, str, Callable[[Payload], Any]]  # (key, unit, decoder)


def _u16(p: Payload, hi: int) -> int:
    return p[hi] * 256 + p[hi + 1]


def _probe_temp(b: int) -> int | str:
    # 0x7F is the heater's "no probe fitted", the same as the status frame's.
    return "No probe" if b == 0x7F else b


# (key, unit, decoder). Units only where the formula gives one: the fan
# "rev" fields' scale is not recorded, so they carry none rather than a guess.
_G2: list[Field] = [
    ("running_time_s", "s", lambda p: p[2] * 65536 + p[3] * 256 + p[4]),
    ("defined_rev", "", lambda p: p[11]),
    ("measured_rev", "", lambda p: p[12]),
    ("fuel_pump_hz", "Hz", lambda p: _u16(p, 13) / 100),
    ("flame_temp", "°C", lambda p: _u16(p, 18) - 273),
    # No glow plug: the recorded p[21] reads 0xFF burning and off alike on
    # two real Air 2Ds (heater fw 2.2.7.3, 2026-10-06). The popup's glow plug
    # current comes from the status frame instead.
    ("liquid_temp", "°C", lambda p: _probe_temp(p[24])),
    ("board_temp", "°C", lambda p: p[25]),
    ("voltage", "V", lambda p: _u16(p, 26) / 10),
    ("fault_code", "", lambda p: p[52]),
    # Not in the vendor map; seen moving on two real Air 2Ds (2026-10-06) and
    # shown raw, keyed by byte, until we know what they are. p[5..7] and
    # p[8..10] count seconds with the run time but about 465 s behind it
    # (time since the flame lit?). p[60..61] and p[62..63] read as Kelvin.
    # p[28..41] look like 10-bit ADC readings (0x03FF the ceiling).
    ("timer p5-7", "s", lambda p: p[5] * 65536 + p[6] * 256 + p[7]),
    ("timer p8-10", "s", lambda p: p[8] * 65536 + p[9] * 256 + p[10]),
    ("temp p60-61", "°C", lambda p: _u16(p, 60) - 273),
    ("temp p62-63", "°C", lambda p: _u16(p, 62) - 273),
    *[(f"raw p{i}-{i + 1}", "", lambda p, i=i: _u16(p, i)) for i in range(28, 42, 2)],
]

# G3 as recorded. NB the recorded map reads the glow plug from p[21], which is
# also the low byte of flame_temp here: kept as the vendor data has it and
# flagged, to be settled by the bench capture.
_G3: list[Field] = [
    ("running_time_s", "s", lambda p: p[2] * 65536 + p[3] * 256 + p[4]),
    ("defined_rev", "", lambda p: p[11]),
    ("measured_rev", "", lambda p: p[12]),
    ("fuel_pump_hz", "Hz", lambda p: _u16(p, 13) / 100),
    ("flame_temp", "°C", lambda p: _u16(p, 20) - 273),
    ("glow_plug", "", lambda p: p[21] > 0),
    ("liquid_temp", "°C", lambda p: p[24]),
    ("board_temp", "°C", lambda p: p[25]),
    ("voltage", "V", lambda p: _u16(p, 26) / 10),
    ("fault_code", "", lambda p: p[52]),
]

# G5 as recorded (vendor tool data, untested): the Air 9D.
_G5: list[Field] = [
    ("running_time_s", "s", lambda p: p[2] * 65536 + p[3] * 256 + p[4]),
    ("defined_rev", "", lambda p: p[11]),
    ("measured_rev", "", lambda p: p[12]),
    ("fuel_pump_hz", "Hz", lambda p: _u16(p, 13) / 100),
    ("glow_plug", "", lambda p: p[17] > 0),
    ("flame_temp", "°C", lambda p: _u16(p, 20) - 273),
    ("liquid_temp", "°C", lambda p: p[22]),
    ("board_temp", "°C", lambda p: p[23]),
    ("overheat_temp", "°C", lambda p: _u16(p, 25) - 273),
    ("voltage", "V", lambda p: _u16(p, 27) / 10),
    ("fault_code", "", lambda p: p[57]),
]

_G6: list[Field] = [
    ("running_time_s", "s", lambda p: p[2] * 65536 + p[3] * 256 + p[4]),
    ("defined_rev", "", lambda p: p[11]),
    ("measured_rev", "", lambda p: p[12]),
    ("glow_plug", "", lambda p: p[13] > 0),
    ("fuel_pump_hz", "Hz", lambda p: p[15] / 10),
    ("flame_temp", "°C", lambda p: _u16(p, 17) - 273),
    ("liquid_temp", "°C", lambda p: p[19]),
    ("overheat_temp", "°C", lambda p: p[20]),
    ("board_temp", "°C", lambda p: p[21]),
    ("voltage", "V", lambda p: _u16(p, 22) / 10),
    ("fault_code", "", lambda p: p[36]),
    ("engine_state", "", lambda p: p[51]),
    ("relay_state", "", lambda p: p[52]),
    ("fan_current", "", lambda p: _u16(p, 54)),
]

# Run-state names, indexed by state * state_mult + substate (p[0], p[1]).
_AIR_2D_STATES: dict[int, str] = {
    1: "waiting for a command", 2: "air blowing", 3: "cooling the flame sensor",
    4: "fuel pumping", 13: "cooling the flame sensor", 14: "ventilation",
    26: "glow plug warming up", 27: "preparation for ignition", 28: "ignition 1",
    29: "ignition 2", 30: "combustion chamber heating", 31: "blowing",
    39: "working", 43: "blowdown before ventilation mode", 44: "ventilation",
    45: "cooling the flame sensor", 46: "glow plug warming up",
    47: "glow plug warming up", 48: "ignition 1", 49: "ignition 2",
    50: "combustion chamber heating", 51: "blowing", 52: "shutting down",
    53: "blowing", 54: "blowing", 55: "blowing",
}

_AIR_4D_STATES: dict[int, str] = {
    1: "waiting for a command", 2: "air blowing", 3: "cooling the flame sensor",
    4: "fuel pumping", 12: "cooling the flame sensor", 13: "ventilation",
    24: "glow plug warming up", 25: "preparation for ignition", 26: "ignition 1",
    27: "ignition 2", 28: "combustion chamber heating", 29: "blowing",
    30: "cooling the flame sensor", 36: "working",
    37: "blowdown before ventilation mode", 38: "ventilation", 39: "blowing",
    48: "shutting down", 49: "blowing", 50: "blowing", 51: "blowing",
}

_FLOW_5_STATES: dict[int, str] = {
    1: "waiting for a command", 2: "cooling the flame sensor", 3: "air blowing",
    4: "fuel pumping", 10: "waiting for temperature reduction", 11: "locked",
    20: "cooling", 21: "glow plug warming up", 22: "preparation for ignition",
    23: "ignition 1", 24: "ignition 2", 25: "blowing",
    26: "combustion chamber heating", 27: "blowing", 30: "low", 32: "high",
    34: "blowing", 35: "waiting", 36: "blowing", 37: "pump only", 38: "middle",
    40: "blowing", 41: "blowing", 42: "blowing", 43: "shutting down",
}

# Model id -> label, field map, state multiplier, state names, verified?
MODELS: dict[str, dict[str, Any]] = {
    "air_2d": {"label": "Autoterm Air 2D", "fields": _G2, "mult": 13,
               "states": _AIR_2D_STATES, "verified": True},
    "air_4d": {"label": "Autoterm Air 4D", "fields": _G3, "mult": 12,
               "states": _AIR_4D_STATES, "verified": False},
    # The vendor tool gives the 9D the Air 4D's run-state table.
    "air_9d": {"label": "Autoterm Air 9D", "fields": _G5, "mult": 12,
               "states": _AIR_4D_STATES, "verified": False},
    "flow_5": {"label": "Autoterm Flow 5 (Baltica)", "fields": _G6, "mult": 10,
               "states": _FLOW_5_STATES, "verified": True},
}

# The Air's own thermostat, read from its run state (Temperature mode: the
# heater idles once the cabin reaches the target and re-ignites below it).
# "holding" is the idle, "heating" the re-ignition from it, "working" the
# steady burn. From the vendor tables above; UNVERIFIED on our bench. The Flow
# is absent: its controller runs the thermostat and reports it directly.
_THERMOSTAT_PHASES: dict[str, dict[str, frozenset[int]]] = {
    "air_2d": {
        "working": frozenset({39}),
        "holding": frozenset({43, 44}),
        "heating": frozenset(range(45, 52)) | frozenset(range(26, 32)),
    },
    "air_4d": {
        "working": frozenset({36}),
        "holding": frozenset({37, 38}),
        "heating": frozenset(range(24, 31)),
    },
}
# Same run-state table as the 4D, so the same phases.
_THERMOSTAT_PHASES["air_9d"] = _THERMOSTAT_PHASES["air_4d"]


def thermostat_phase(model: str | None, state_index: int | None) -> str | None:
    """"working", "holding", "heating" or None for an Air heater's run state."""
    phases = _THERMOSTAT_PHASES.get(model or "")
    if phases is None or state_index is None:
        return None
    for phase, indexes in phases.items():
        if state_index in indexes:
            return phase
    return None


# The two models the Air D controller can be fitted to (the device row's
# Heater Model choice); the Baltica has one.
AIR_MODELS: tuple[str, ...] = ("air_2d", "air_4d")

# The Air 9D controller is its own firmware SKU, so its model is fixed by the
# box, never chosen: by catalog id, else by the Sensa Model it reports (a
# registered record can carry `model` only).
AIR_9D_CATALOG_ID = "sensa_autoterm_a9d"
AIR_9D_SENSA_MODEL = "autoterm-a9d-c124"


def is_air_9d(row: dict[str, Any]) -> bool:
    """An Air 9D controller's device row."""
    if row.get("catalog_id") == AIR_9D_CATALOG_ID:
        return True
    return AIR_9D_SENSA_MODEL in (row.get("vendor_model"), row.get("model"))


def decode(model: str, frame_hex: str) -> dict[str, Any] | None:
    """Readings from one diagnostics frame, or None when the model is unknown
    or the frame is not usable hex. A field whose byte lies beyond a short
    frame is left out rather than guessed."""
    spec = MODELS.get(model)
    if spec is None:
        return None
    try:
        p = bytes.fromhex((frame_hex or "").strip())
    except ValueError:
        return None
    if len(p) < 2:
        return None
    index = p[0] * spec["mult"] + p[1]
    readings: dict[str, Any] = {}
    units: dict[str, str] = {}
    for key, unit, fn in spec["fields"]:
        try:
            readings[key] = fn(p)
        except IndexError:
            continue
        if unit:
            units[key] = unit
    return {
        "model": model,
        "model_label": spec["label"],
        "verified": spec["verified"],
        "state_index": index,
        "state": spec["states"].get(index, f"state {p[0]}/{p[1]}"),
        "readings": readings,
        "units": units,
    }


# The 0x0F status frame, published raw on "Status Frame" (at most every 10 s)
# for the engineer popup. The controller firmware already reads a few of these
# bytes for the card; this decodes the rest. Byte maps from
# docs/devices/autoterm/autoterm-uart-protocol.md ("Status frame 0x0F"). The
# Flow map is hardware-verified (sinise); the Air map is UNVERIFIED. Bytes
# neither map names come back raw, so an engineer can watch them move.


def _signed(b: int) -> int:
    return b - 255 if b > 127 else b


_STATUS_FLOW: list[Field] = [
    ("main_state", "", lambda p: p[0]),
    ("sub_stage", "", lambda p: p[1]),
    ("fault_code", "", lambda p: p[2]),
    ("coolant_temp", "°C", lambda p: p[3]),
    ("coolant_temp_2", "°C", lambda p: p[4]),
    ("voltage_raw", "", lambda p: p[6]),
    ("exhaust_raw", "", lambda p: p[7]),
    ("elapsed_min", "min", lambda p: p[9]),
    ("elapsed_counter", "", lambda p: p[11]),
    ("burner", "", lambda p: p[12] == 0xFF),
    ("cooldown_counter", "", lambda p: p[16]),
]
_STATUS_FLOW_BYTES = frozenset({0, 1, 2, 3, 4, 6, 7, 9, 11, 12, 16})

_STATUS_AIR: list[Field] = [
    ("status_code", "", lambda p: f"0x{p[0]:02x}{p[1]:02x}"),
    ("intake_temp", "°C", lambda p: _signed(p[3])),
    ("external_temp", "°C", lambda p: None if p[4] == 0x7F else _signed(p[4])),
    # [5..6] big-endian tenths: a 24 V heater over 25.5 V sets [5].
    ("voltage", "V", lambda p: _u16(p, 5) / 10),
    ("exchanger_temp", "°C", lambda p: p[8] - 15),
    ("fan_set_rpm", "rpm", lambda p: p[11] * 60),
    ("fan_actual_rpm", "rpm", lambda p: p[12] * 60),
    ("fuel_pump_hz", "Hz", lambda p: p[14] / 100),
    # [17] "Glow Current (0-9A)" (Planar reverse engineering, grimoire314
    # 2019) in tenths: 46 / 29 / 0 through a start-up (2026-10-06). Scale
    # UNVERIFIED against a meter.
    ("glow_plug_current", "A", lambda p: p[17] / 10),
]
_STATUS_AIR_BYTES = frozenset({0, 1, 3, 4, 5, 6, 8, 11, 12, 14, 17})

# Heater family (the controller firmware's) -> map, bytes it names, verified?
STATUS_FAMILIES: dict[str, tuple[list[Field], frozenset[int], bool]] = {
    "flow": (_STATUS_FLOW, _STATUS_FLOW_BYTES, True),
    "air": (_STATUS_AIR, _STATUS_AIR_BYTES, False),
}


def state_name(model: str | None, state: int, substate: int) -> str | None:
    """The vendor's name for a run state and sub-state, from the model's table,
    or None for an unknown model or an unnamed state."""
    spec = MODELS.get(model or "")
    if spec is None:
        return None
    return spec["states"].get(state * spec["mult"] + substate)


def decode_status(family: str, frame_hex: str, model: str | None = None) -> dict[str, Any] | None:
    """Readings from one status frame, plus every byte neither map names as
    `raw` ({"index": value}). None for an unknown family or unusable hex. A
    field whose byte lies beyond a short frame, or reads "no probe", is left
    out rather than guessed. With the heater's model, the Air's status code
    carries its run-state name: "0x0204 (combustion chamber heating)"."""
    spec = STATUS_FAMILIES.get(family)
    if spec is None:
        return None
    fields, named, verified = spec
    try:
        p = bytes.fromhex((frame_hex or "").strip())
    except ValueError:
        return None
    if len(p) < 2:
        return None
    readings: dict[str, Any] = {}
    units: dict[str, str] = {}
    for key, unit, fn in fields:
        try:
            value = fn(p)
        except IndexError:
            continue
        if value is None:
            continue
        readings[key] = value
        if unit:
            units[key] = unit
    if "status_code" in readings:
        name = state_name(model, p[0], p[1])
        if name:
            readings["status_code"] = f"{readings['status_code']} ({name})"
    # String keys: this travels as JSON over the websocket.
    raw = {str(i): b for i, b in enumerate(p) if i not in named}
    return {"verified": verified, "readings": readings, "units": units, "raw": raw}


# The 0x0B black-box report: 256 bytes read in four 64-byte pages (firmware
# Read Report). Layout from two Flow-family heaters on box .116 (Baltica and a
# Flow 5D, 2026-10-07): a `55 aa` marker, then big-endian counters starting one
# byte off the even boundary. Verified by a before/after read around one
# recorded run: starts +1, total run time +2,868 s for a start-to-cooldown run
# of 45 to 50 minutes. Successful and failed starts are inferred: on both
# heaters starts = [0x11] + [0x13] + [0x15] (0x15, a third kind of start, is
# unidentified), so they are labelled unverified. The rest is unknown.
REPORT_MARKER = b"\x55\xaa"
# key, label, offset, size, unit, verified
_REPORT_FIELDS: list[tuple[str, str, int, int, str, bool]] = [
    ("starts", "Starts in all", 0x0D, 2, "", True),
    ("total_run_time_s", "Total run time", 0x17, 4, "s", True),
    # Bytes 5 to 7 as one number: the Air D's matched its vendor report; the
    # same place here, unchecked.
    ("serial", "Serial number", 0x05, 3, "", False),
    # Time at each power level, named by matching Autoterm's own software's
    # report, which shows them in hours and minutes (Richard, 2026-10-07:
    # high 33,095 = 9 h 11 min, middle 36,033 = 10 h 0 min, low 84,365 =
    # 23 h 26 min).
    ("high_mode_time_s", "High mode time", 0x1B, 4, "s", True),
    ("middle_mode_time_s", "Middle mode time", 0x1F, 4, "s", True),
    ("low_mode_time_s", "Low mode time", 0x23, 4, "s", True),
    # Matches the software's "first attempt starts" (180); 0x0F, one less at
    # 179 and also +1 per run, is a different counter.
    ("first_attempt_starts", "First attempt starts", 0x2B, 2, "", True),
    # The vendor software's own term (it confirmed this on the Air D).
    ("successful_starts", "Starts without faults", 0x11, 2, "", False),
    ("failed_starts", "Starts with faults", 0x13, 2, "", False),
    # The third count that makes successful + failed + this = starts.
    ("other_starts", "Other starts", 0x15, 2, "", False),
]
# Counters that moved during the recorded run but whose meaning is not known:
# shown raw, by offset, so the next before/after reads can be compared.
_REPORT_UNKNOWN: list[tuple[int, int]] = [(0x0F, 2)]

# Byte 2 names the layout: 01 on the Flow family (Baltica, Flow 5D), 03 on an
# Air D (Living Heater 1, heater fw 2.2.7.3, 2026-10-07). The Air's is a
# different layout and NOTHING in it is verified yet: the two named fields are
# guesses (the first count, the largest timer) to compare with the vendor
# report; the rest is shown by offset, raw.
REPORT_LAYOUT_FLOW = 0x01
REPORT_LAYOUT_AIR = 0x03
_AIR_REPORT_FIELDS: list[tuple[str, str, int, int, str, bool]] = [
    # Confirmed against the vendor report (Richard, 2026-10-07: 290).
    ("starts", "Starts in all", 0x0D, 2, "", True),
    # Bytes 5 to 7, one big-endian number: the vendor report's serial
    # (Richard, 2026-10-07: 1004537 = 0f 53 f9, the only match in the report).
    ("serial", "Serial number", 0x05, 3, "", True),
    # Confirmed against the vendor report (Richard, 2026-10-07: 226 h).
    ("total_run_time_s", "Total run time", 0x18, 4, "s", True),
    # Confirmed against the vendor report (Richard, 2026-10-07: 144).
    ("successful_starts", "Starts without faults", 0x11, 2, "", True),
    # Confirmed against the vendor report (Richard, 2026-10-07: 1).
    ("failed_starts", "Starts with faults", 0x13, 2, "", True),
    # Work time in each mode, 0 to 9, seconds (the vendor report's "work time
    # in modes", Richard 2026-10-07: 0x1C is mode 0).
    *[(f"mode_{m}_time_s", f"Work time, mode {m}", 0x1C + 4 * m, 4, "s", True) for m in range(10)],
    # Confirmed against the vendor report (Richard, 2026-10-07: 102 h 38 min).
    ("ventilation_time_s", "Ventilation mode time", 0x44, 4, "s", True),
]
# (offset, size, unit): the counts after the first, then the 4-byte run from
# 0x1C that reads as times in seconds.
_AIR_REPORT_UNKNOWN: list[tuple[int, int, str]] = [
    (0x0F, 2, ""),
]


# The Air's fault table: five 30-byte records from 0x51, one per fault code
# (the vendor report's "5 most popular faults", column 1 the latest). Every
# item below was matched against the vendor report for Living Heater 1
# (Richard, 2026-10-07): code 13, 1 repetition, stage 2, mode 3, 360 s, 131 s,
# 26.579 V, 301 C, 14 C, no probe, 19 C, 3 attempts, 0 breaks, 300 C, 30 C,
# power 5 / 0, 2, 0, 2, 100. The vendor shows the voltage twice (also as
# 2657.9, the same bytes / 10): shown once here. Air pressure reads 100 there;
# 100 kPa = 1000 mbar, so it is shown in mbar. Slot order is the vendor's
# column order, latest first (proven on the Baltica's ten records).
_AIR_FAULT_START = 0x51
_AIR_FAULT_LEN = 30
_AIR_FAULT_SLOTS = 5
AIR_FAULT_ROWS = [
    "Fault code", "Repetitions", "Stage", "Mode", "Total work time",
    "Mode work time", "Voltage", "Flame temperature", "Panel temperature",
    "External temperature", "Board temperature", "Attempts to launch",
    "Flame breaks while running", "Flame break threshold", "Setpoint temperature",
    "Defined power", "Current power", "Ventilation allowed",
    "Ventilation mode number", "Work by sensor", "Air pressure",
]


def _air_fault_column(r: bytes) -> list[str]:
    """One fault record as display values, in AIR_FAULT_ROWS order."""
    def be(a: int, b: int) -> int:
        return int.from_bytes(r[a:b], "big")

    ext = r[17]
    return [
        str(r[0]), str(r[1]), str(r[2]), str(r[3]),
        f"{be(4, 8)} s", f"{be(8, 12)} s",
        f"{be(12, 14) / 1000:.2f} V", f"{be(14, 16)} °C",
        f"{_signed(r[16])} °C",
        "No probe" if ext == 0x7F else f"{_signed(ext)} °C",
        f"{_signed(r[18])} °C",
        str(r[19]), str(r[20]), f"{be(21, 23)} °C", f"{r[23]} °C",
        str(r[24]), str(r[25]), str(r[26]), str(r[27]), str(r[28]),
        f"{r[29] * 10} mbar",
    ]


# The Flow family's fault table: ten 17-byte records from 0x38 (the vendor
# report's "10 most popular fault codes", column 1 the latest = the first
# record). All 13 items matched against the vendor report for the Baltica's
# column 1 (Richard, 2026-10-07): 17, 1, 3, 111, 1, 1, 12.6 V, 297, 22, 21,
# 24, 0, 0. Mode 111 is what the heater stores, not a misread.
_FLOW_FAULT_START = 0x38
_FLOW_FAULT_LEN = 17
_FLOW_FAULT_SLOTS = 10
FLOW_FAULT_ROWS = [
    "Fault code", "Repetitions", "Stage", "Mode", "Total work time",
    "Mode work time", "Voltage", "Flame temperature", "Liquid temperature",
    "Overheat temperature", "Board temperature", "Second attempt of ignition",
    "Flame breaks",
]


def _flow_fault_column(r: bytes) -> list[str]:
    """One Flow-family fault record as display values, in FLOW_FAULT_ROWS order."""
    def be(a: int, b: int) -> int:
        return int.from_bytes(r[a:b], "big")

    return [
        str(r[0]), str(r[1]), str(r[2]), str(r[3]),
        str(be(4, 6)), f"{be(6, 8)} s",
        f"{be(8, 10) / 10:.1f} V", f"{be(10, 12)} °C",
        f"{_signed(r[12])} °C", f"{_signed(r[13])} °C", f"{_signed(r[14])} °C",
        str(r[15]), str(r[16]),
    ]


def _fault_table(p: bytes, start: int, length: int, slots: int,
                 rows: list[str], column: Callable[[bytes], list[str]]) -> dict[str, Any] | None:
    """The records that hold a fault, as columns in stored order (column 1 the
    latest); None when there is none. An empty slot has fault code 0."""
    columns = []
    for slot in range(slots):
        r = p[start + slot * length:start + (slot + 1) * length]
        if len(r) < length or r[0] == 0:
            continue
        columns.append(column(r))
    return {"rows": rows, "columns": columns} if columns else None


# The Air 4D (heater fw 3.1.14.9, 2026-10-07) carries the same `03` layout
# byte and the same fields as the Air 2D, but every 4-byte timer (total run
# time, the ten modes, ventilation) sits one byte earlier: 0x17 not 0x18.
# Matched against the vendor report (Richard). Told apart only by the heater's
# Heater Model setting, so the caller passes it. Its fault table is not
# mapped yet.
_AIR_4D_TIMER_SHIFT = -1


def _air_report_fits(fields: list[dict[str, Any]]) -> bool | None:
    """Do an Air report's timers add up? The ten mode times and ventilation
    are parts of the total run time, so they cannot exceed it. Decoded with
    the wrong Heater Model every timer is read one byte off and they run to
    millions of hours (Hab 2kW > on box .116, a 2D set to Air 4D, 2026-10-17:
    9,321 h total, 6 million h of modes). None when the report is too short
    to tell."""
    f = {x["key"]: x["value"] for x in fields}
    total = f.get("total_run_time_s")
    parts = [v for k, v in f.items() if k.startswith("mode_") or k == "ventilation_time_s"]
    if total is None or len(parts) < 11:
        return None
    return sum(parts) <= total


def decode_report(report_hex: str, model: str | None = None) -> dict[str, Any] | None:
    """The report's known fields, or None when the bytes are not a report
    (no marker, too short, not hex). Each field says whether it is verified;
    the bytes no field names stay with the caller, raw. An Air report whose
    timers do not add up under the chosen model, but do under the other,
    carries `model_mismatch` {"chosen", "fits"}: the Heater Model is wrong."""
    decoded = _decode_report(report_hex, model)
    if decoded is None or not decoded["layout"].startswith("air") or model not in AIR_MODELS:
        return decoded
    if _air_report_fits(decoded["fields"]) is not False:
        return decoded
    other = "air_4d" if model == "air_2d" else "air_2d"
    alt = _decode_report(report_hex, other)
    if alt is not None and _air_report_fits(alt["fields"]) is True:
        decoded["model_mismatch"] = {"chosen": model, "fits": other}
    return decoded


def _decode_report(report_hex: str, model: str | None = None) -> dict[str, Any] | None:
    try:
        p = bytes.fromhex((report_hex or "").strip())
    except ValueError:
        return None
    if len(p) < 0x1B or p[:2] != REPORT_MARKER:
        return None
    air_4d = p[2] == REPORT_LAYOUT_AIR and model == "air_4d"
    # The 9D's report has never been seen: read with the Air layout, every
    # field unverified and no fault table.
    air_9d = p[2] == REPORT_LAYOUT_AIR and model == "air_9d"
    if p[2] == REPORT_LAYOUT_AIR:
        named = _AIR_REPORT_FIELDS
        if air_4d:
            named = [
                (k, label, o + _AIR_4D_TIMER_SHIFT if unit == "s" else o, n, unit, v)
                for k, label, o, n, unit, v in named
            ]
        unknown = _AIR_REPORT_UNKNOWN
    elif p[2] == REPORT_LAYOUT_FLOW:
        named = _REPORT_FIELDS
        unknown = [(o, n, "") for o, n in _REPORT_UNKNOWN]
    else:
        return None  # a layout nobody has seen: raw only
    fields = []
    for key, label, offset, size, unit, verified in named:
        if offset + size > len(p):
            continue
        fields.append({
            "key": key,
            "label": label,
            "value": int.from_bytes(p[offset:offset + size], "big"),
            "unit": unit,
            "verified": verified and not air_9d,
        })
    for offset, size, unit in unknown:
        if offset + size > len(p):
            continue
        fields.append({
            "key": f"unknown_{offset:02x}",
            "label": f"Unidentified {'timer' if unit == 's' else 'counter'} (0x{offset:02X})",
            "value": int.from_bytes(p[offset:offset + size], "big"),
            "unit": unit,
            "verified": False,
            "unknown": True,
        })
    return {
        "layout": (
            ("air_4d" if air_4d else "air_9d" if air_9d else "air")
            if p[2] == REPORT_LAYOUT_AIR else "flow"
        ),
        "fault_table": (
            None  # the 4D's and 9D's fault records are not mapped yet
            if air_4d or air_9d
            else _fault_table(p, _AIR_FAULT_START, _AIR_FAULT_LEN, _AIR_FAULT_SLOTS,
                              AIR_FAULT_ROWS, _air_fault_column)
            if p[2] == REPORT_LAYOUT_AIR
            else _fault_table(p, _FLOW_FAULT_START, _FLOW_FAULT_LEN, _FLOW_FAULT_SLOTS,
                              FLOW_FAULT_ROWS, _flow_fault_column)
        ),
        "fields": fields,
    }
