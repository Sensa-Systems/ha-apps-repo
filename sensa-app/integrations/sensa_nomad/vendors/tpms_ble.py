"""Passive BLE tyre-pressure (TPMS) advert decoders.

Cheap external-cap TPMS sensors never accept a connection — each one
broadcasts its reading in the advert and the phone app just listens.
There is no common protocol: every sensor family packs the bytes its
own way, so this module keeps one pure `_parse_<format>` per family and
`decode_tpms_advert` tries them in a fixed order.

Formats and where they came from (both reverse-engineered, neither
vendor-documented):

  * DJTPMS (OBEST kits, "DJTPMS" app) — Zen3515/djtpms, Apache-2.0.
    VERIFIED on Sensa hardware. The only format carrying a checksum.
  * TPMSII, SYTPMS, Michelin TMS, ITPMS (WODHMIEY), Salutica FOBO —
    bkbilly/tpms_ble, MIT (c) 2022 Vasilis Koulis. UNVERIFIED: no
    Sensa capture yet, so their catalog rows ship experimental.

Every decoder returns GAUGE pressure in kPa (what a tyre gauge reads;
0 = flat) clamped at 0. None of these formats carries a wheel position
— the customer assigns one per sensor.

`manufacturer_data` is HA's `{company_id: payload}` dict: bleak strips
the first two bytes of the manufacturer-specific AD and uses them,
little-endian, as the key.
"""
from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

# Format identifiers. Also the suffix of the catalog `device_subtype`
# (`tpms_<format>`), so renaming one orphans registered devices.
FORMAT_DJTPMS = "djtpms"
FORMAT_TPMSII = "tpmsii"
FORMAT_SYTPMS = "sytpms"
FORMAT_MICHELIN = "michelin"
FORMAT_ITPMS = "itpms"
FORMAT_FOBO = "fobo"

DJTPMS_LOCAL_NAME = "DJTPMS"
TPMSII_COMPANY_ID = 0x0100
MICHELIN_COMPANY_ID = 0x0828
ITPMS_COMPANY_ID = 0x00AC
FOBO_COMPANY_ID = 0x0127
SYTPMS_SERVICE_UUID = "000027a5-0000-1000-8000-00805f9b34fb"

# Standard atmosphere, for formats that report absolute pressure.
_ATMOSPHERE_KPA = 101.0
_KPA_PER_PSI = 6.89476

# DJTPMS CRC8: poly 0x2F, init 0xDF, MSB-first, no final xor.
_DJTPMS_CRC_POLY = 0x2F
_DJTPMS_CRC_INIT = 0xDF

# Discharge curve for the 3 V lithium coin cells in cap sensors
# (voltage, percent), highest first. From djtpms; bkbilly uses the same.
_COIN_CELL_CURVE: tuple[tuple[float, float], ...] = (
    (3.3, 100.0),
    (3.05, 97.0),
    (2.94, 91.0),
    (2.9, 75.0),
    (2.85, 25.0),
    (2.8, 17.0),
    (2.6, 0.0),
)


@dataclass(frozen=True)
class TpmsReading:
    """One decoded tyre reading. Fields a format doesn't carry are None."""
    format: str
    pressure_kpa: float | None
    temperature_c: float | None
    battery_v: float | None
    battery_pct: int | None
    alarm: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "pressure_kpa": self.pressure_kpa,
            "temperature_c": self.temperature_c,
            "battery_v": self.battery_v,
            "battery_pct": self.battery_pct,
            "alarm": self.alarm,
        }


def _gauge(kpa: float) -> float:
    return round(max(kpa, 0.0), 1)


def _int8(value: int) -> int:
    return value - 256 if value >= 128 else value


def coin_cell_percent(volts: float) -> int:
    """Battery % for a 3 V coin cell, piecewise-linear on the curve."""
    if volts >= _COIN_CELL_CURVE[0][0]:
        return 100
    if volts <= _COIN_CELL_CURVE[-1][0]:
        return 0
    for (v_hi, p_hi), (v_lo, p_lo) in pairwise(_COIN_CELL_CURVE):
        if v_lo <= volts <= v_hi:
            return round(p_lo + (volts - v_lo) * (p_hi - p_lo) / (v_hi - v_lo))
    return 0


def djtpms_crc8(data: bytes) -> int:
    crc = _DJTPMS_CRC_INIT
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) & 0xFF) ^ _DJTPMS_CRC_POLY
            else:
                crc = (crc << 1) & 0xFF
    return crc


# ----------------------------------------------------------------------
# Per-format parsers. Each takes the advert pieces and returns a reading
# or None — never raises on foreign bytes, because every advert near the
# van is offered to every parser.
# ----------------------------------------------------------------------


def _djtpms_frames(company_id: int, payload: bytes):
    """Yield (cid0, cid1, body12) candidates for one manufacturer entry.

    The DJTPMS "company ID" is not a company ID: it changes between
    adverts and feeds the CRC, so both byte orders are tried and the
    CRC decides. Mirrors djtpms `_extract_frames`.
    """
    cid0 = company_id & 0xFF
    cid1 = (company_id >> 8) & 0xFF
    pairs = [(cid0, cid1)]
    if cid0 != cid1:
        pairs.append((cid1, cid0))
    if len(payload) >= 14:
        frame14 = payload[-14:]
        for a, b in pairs:
            if frame14[0] == a and frame14[1] == b:
                yield a, b, frame14[2:]
    if len(payload) >= 12:
        for a, b in pairs:
            yield a, b, payload[-12:]


def _djtpms_body(cid0: int, cid1: int, body: bytes) -> TpmsReading | None:
    if djtpms_crc8(bytes((cid0, cid1)) + body[0:4]) != body[5]:
        return None
    battery_v = body[0] / 10.0
    absolute_kpa = (body[2] << 8) | body[3]
    return TpmsReading(
        format=FORMAT_DJTPMS,
        pressure_kpa=_gauge(absolute_kpa - _ATMOSPHERE_KPA),
        temperature_c=float(_int8(body[1])),
        battery_v=battery_v,
        battery_pct=coin_cell_percent(battery_v),
        # body[4] is an undecoded flags byte — not trusted as an alarm.
        alarm=None,
    )


def _parse_djtpms(
    manufacturer_data: Mapping[int, bytes],
    service_data: Mapping[str, bytes],
) -> TpmsReading | None:
    for company_id, payload in manufacturer_data.items():
        for cid0, cid1, body in _djtpms_frames(company_id, bytes(payload)):
            reading = _djtpms_body(cid0, cid1, body)
            if reading is not None:
                return reading
    for payload in service_data.values():
        payload = bytes(payload)
        if len(payload) >= 14:
            frame14 = payload[-14:]
            reading = _djtpms_body(frame14[0], frame14[1], frame14[2:])
            if reading is not None:
                return reading
    return None


def _parse_tpmsii(manufacturer_data: Mapping[int, bytes]) -> TpmsReading | None:
    payload = manufacturer_data.get(TPMSII_COMPANY_ID)
    if payload is None or len(payload) != 16:
        return None
    # Bytes 0-5 are unused (probably the sensor address).
    pressure_pa, temp_centi, battery_pct, alarm = struct.unpack(
        "<iib?", bytes(payload[6:16])
    )
    return TpmsReading(
        format=FORMAT_TPMSII,
        pressure_kpa=_gauge(pressure_pa / 1000.0),
        temperature_c=temp_centi / 100.0,
        battery_v=None,
        battery_pct=max(0, min(100, battery_pct)),
        alarm=bool(alarm),
    )


def _parse_sytpms(
    manufacturer_data: Mapping[int, bytes], service_uuids: Sequence[str]
) -> TpmsReading | None:
    if SYTPMS_SERVICE_UUID not in {str(u).lower() for u in service_uuids}:
        return None
    for company_id, payload in manufacturer_data.items():
        if len(payload) != 5:
            continue
        # The "company ID" carries data: its high byte is battery V x10.
        battery_v = ((company_id >> 8) & 0xFF) / 10.0
        absolute_psi = ((payload[1] << 8) | payload[2]) / 10.0
        return TpmsReading(
            format=FORMAT_SYTPMS,
            pressure_kpa=_gauge(absolute_psi * _KPA_PER_PSI - _ATMOSPHERE_KPA),
            temperature_c=float(_int8(payload[0])),
            battery_v=battery_v,
            battery_pct=coin_cell_percent(battery_v),
            alarm=None,
        )
    return None


# Michelin frame type -> (required length, offset of the LE u16 mbar field
# or None when that frame type carries no pressure).
_MICHELIN_FRAMES: dict[int, tuple[int, int | None]] = {
    0x02: (12, None),
    0x06: (12, None),
    0x03: (14, 4),
    0x04: (14, 4),
    0x05: (14, 12),
    0x0C: (17, 4),
}


def _parse_michelin(manufacturer_data: Mapping[int, bytes]) -> TpmsReading | None:
    payload = manufacturer_data.get(MICHELIN_COMPANY_ID)
    if payload is None or len(payload) < 4 or payload[0] != 0x01:
        return None
    frame = _MICHELIN_FRAMES.get(payload[1])
    if frame is None or len(payload) != frame[0]:
        return None
    pressure_kpa = None
    if frame[1] is not None:
        absolute_mbar = int.from_bytes(payload[frame[1]:frame[1] + 2], "little")
        pressure_kpa = _gauge(absolute_mbar / 10.0 - 100.0)
    battery_v = round(payload[3] / 100.0 + 1.0, 2)
    return TpmsReading(
        format=FORMAT_MICHELIN,
        pressure_kpa=pressure_kpa,
        temperature_c=float(payload[2] - 60),
        battery_v=battery_v,
        battery_pct=coin_cell_percent(battery_v),
        alarm=None,
    )


def _parse_itpms(manufacturer_data: Mapping[int, bytes]) -> TpmsReading | None:
    payload = manufacturer_data.get(ITPMS_COMPANY_ID)
    if payload is None or len(payload) < 7 or payload[6] not in (0, 1):
        return None
    raw_pressure = payload[1] + (256 if payload[6] == 1 else 0)
    battery_v = round(payload[0] * 0.01 + 1.22, 2)
    return TpmsReading(
        format=FORMAT_ITPMS,
        pressure_kpa=_gauge(raw_pressure * 3.144),
        temperature_c=float(payload[2] - 55),
        battery_v=battery_v,
        battery_pct=max(0, min(100, int((battery_v - 1.8) / 1.2 * 100))),
        # 0 normal, 1 leak, 2 over-inflation, ...
        alarm=payload[3] != 0,
    )


def _parse_fobo(manufacturer_data: Mapping[int, bytes]) -> TpmsReading | None:
    payload = manufacturer_data.get(FOBO_COMPANY_ID)
    if payload is None or len(payload) < 4:
        return None
    tail = payload[-4:]
    raw = (tail[2] << 8) | tail[3]
    battery_v = ((raw >> 10) & 0x3F) / 10.0
    return TpmsReading(
        format=FORMAT_FOBO,
        pressure_kpa=_gauge(float(raw & 0x3FF)),
        temperature_c=float(tail[1] - 50),
        battery_v=battery_v,
        battery_pct=coin_cell_percent(battery_v),
        alarm=None,
    )


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

_Parser = Callable[
    [Mapping[int, bytes], Mapping[str, bytes], Sequence[str]], "TpmsReading | None"
]

# Checksummed format first; the rest are keyed on their own company ID
# or service UUID so the order between them does not matter.
_PARSERS: tuple[tuple[str, _Parser], ...] = (
    (FORMAT_DJTPMS, lambda m, s, u: _parse_djtpms(m, s)),
    (FORMAT_TPMSII, lambda m, s, u: _parse_tpmsii(m)),
    (FORMAT_SYTPMS, lambda m, s, u: _parse_sytpms(m, u)),
    (FORMAT_MICHELIN, lambda m, s, u: _parse_michelin(m)),
    (FORMAT_ITPMS, lambda m, s, u: _parse_itpms(m)),
    (FORMAT_FOBO, lambda m, s, u: _parse_fobo(m)),
)

TPMS_FORMATS: tuple[str, ...] = tuple(name for name, _ in _PARSERS)


def decode_tpms_advert(
    *,
    manufacturer_data: Mapping[int, bytes] | None = None,
    service_data: Mapping[str, bytes] | None = None,
    service_uuids: Sequence[str] | None = None,
    name: str | None = None,
    expected_format: str | None = None,
) -> TpmsReading | None:
    """Decode one advert, or None if it is not a TPMS frame we know.

    Without `expected_format` (discovery) a DJTPMS frame also needs the
    `DJTPMS` local name: an 8-bit CRC alone would pass one random
    foreign payload in 256. With `expected_format` (a registered sensor,
    already pinned to its MAC) only that format is tried and the name
    is not required — passive scans often arrive nameless.
    """
    mfr = manufacturer_data or {}
    svc = service_data or {}
    uuids = service_uuids or ()
    for fmt, parser in _PARSERS:
        if expected_format is not None and fmt != expected_format:
            continue
        if (
            fmt == FORMAT_DJTPMS
            and expected_format is None
            and (name or "") != DJTPMS_LOCAL_NAME
        ):
            continue
        reading = parser(mfr, svc, uuids)
        if reading is not None:
            return reading
    return None


def decode_service_info(
    service_info: Any, expected_format: str | None = None
) -> TpmsReading | None:
    """`decode_tpms_advert` over an HA BluetoothServiceInfo(Bleak)."""
    return decode_tpms_advert(
        manufacturer_data=getattr(service_info, "manufacturer_data", None),
        service_data=getattr(service_info, "service_data", None),
        service_uuids=getattr(service_info, "service_uuids", None),
        name=getattr(service_info, "name", None),
        expected_format=expected_format,
    )
