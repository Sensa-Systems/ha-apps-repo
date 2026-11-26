"""DJI Power portable power stations over Bluetooth LE.

Every unit speaks DJI's DUML framing over one vendor GATT service (0xA002),
with replies on characteristic 0xC305 (notify). Two protocols live here; see
docs/devices/dji-power-1000-v2.md for the captures and field maps.

**Battery records, no key** (Power 1000 V2, reverse engineered on the bench
4 September 2026): requests go to 0xC303 (write-without-response) for the
battery module (DUML address 0x0B), the standard DJI smart-battery records
(cmd_set 0x0D) plus the general version query. Read-only.

**Station session, pair key** (Power 2000, captured from DJI Home with the
nRF sniffer 9 October 2026; the same protocol the community decoder
hiromo/ha-dji-power-bt documents): requests go to 0xC304 on cmd_set 0x5A for
the station (address 0xAB). Nothing is answered until the session is
authenticated with the unit's 32-character hex pair key (cmd 0x6A); the
station then pushes realtime telemetry (0x61) about once a second, answers
config reads (0x60) and accepts config writes (0x63), of which the AC output
switch is the one used here. Chosen when the device row's config carries a
`pair_key`.

Scale is applied ONCE, here. `voltage_v`, `current_a`, `*_capacity_ah` and
`temperature_c` are already in customer units when they leave `parse_*`;
the entities and the card never rescale.
"""
from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=30)

# GATT: one vendor service, two characteristics that matter.
DJI_WRITE_UUID = "0000c303-0000-1000-8000-00805f9b34fb"
DJI_NOTIFY_UUID = "0000c305-0000-1000-8000-00805f9b34fb"

# Bluetooth SIG company identifier for SZ DJI Technology, as advertised.
DJI_MANUFACTURER_ID = 0x08AA

# DUML addressing and command vocabulary (the subset the unit answered).
DUML_MAGIC = 0x55
SENDER_APP = 0x02
RECEIVER_BATTERY = 0x0B
CMD_TYPE_REQUEST = 0x40   # request, ack wanted
CMD_TYPE_ACK_BIT = 0x80   # set on every reply
CMD_SET_GENERAL = 0x00
CMD_SET_BATTERY = 0x0D
CMD_GENERAL_VERSION = 0x01
CMD_BATTERY_STATIC = 0x01
CMD_BATTERY_DYNAMIC = 0x02
CMD_BATTERY_CELLS = 0x03
CMD_BATTERY_BARCODE = 0x04

# Station session (pair key). Write handle 0x0036 in the capture is 0xC304.
DJI_STATION_WRITE_UUID = "0000c304-0000-1000-8000-00805f9b34fb"
RECEIVER_STATION = 0xAB
CMD_SET_STATION = 0x5A
CMD_TYPE_STATION_REQUEST = 0x20
CMD_TYPE_PUSH_WANTS_ACK = 0x40
CMD_STATION_CONFIG_READ = 0x60
CMD_STATION_REALTIME = 0x61
CMD_STATION_CONFIG_REPORT = 0x62
CMD_STATION_CONFIG_WRITE = 0x63
CMD_STATION_AUTH = 0x6A

# DJI Home's own 0x60 read, byte for byte from the 9 October capture.
STATION_CONFIG_READ_PAYLOAD = bytes.fromhex(
    "00001002100310051006100710081009100a100b100c100d100e1015101610181019"
    "101b101c101d101e10201021102210231024102510261027102810"
)
# The companion record DJI Home sends alongside every 0x63 write to the unit.
_WRITE_COMPANION_100E = b"\x0c\x00" + b"1e00efffff3f"

# Interface types inside the power and output tables.
INTERFACE_AC_INLET = 1
INTERFACE_AC_OUTLET = 2
# 0x100D output-table state byte: what DJI Home wrote for AC on and AC off.
_OUTPUT_STATE_ON = 0x01
_OUTPUT_STATE_OFF = 0x02

_PAIR_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
# Telemetry older than this is not the unit's current state.
_REALTIME_MAX_AGE_SECS = 60.0
_FIRST_REALTIME_WAIT_SECS = 5.0

_RESPONSE_TIMEOUT = 3.0
_NOTIFY_SETTLE_SECS = 0.3
# Below this the pack is idle: the display and radio alone draw about 90 mA,
# the inverter with no load about 350 mA, so the charging / discharging flags
# need a threshold well above both.
_FLOW_THRESHOLD_A = 0.5


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def crc8(data: bytes) -> int:
    """DJI header CRC-8: reflected poly 0x31 (0x8C), seed 0x77."""
    crc = 0x77
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def crc16(data: bytes) -> int:
    """DJI frame CRC-16: KERMIT (reflected poly 0x1021 = 0x8408), seed 0x3692."""
    crc = 0x3692
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


@dataclass(frozen=True)
class DumlFrame:
    sender: int
    receiver: int
    seq: int
    cmd_type: int
    cmd_set: int
    cmd_id: int
    payload: bytes

    @property
    def is_ack(self) -> bool:
        return bool(self.cmd_type & CMD_TYPE_ACK_BIT)


def build_frame(
    cmd_set: int,
    cmd_id: int,
    seq: int,
    payload: bytes = b"",
    *,
    receiver: int = RECEIVER_BATTERY,
    sender: int = SENDER_APP,
    cmd_type: int = CMD_TYPE_REQUEST,
) -> bytes:
    """One DUML request frame, checksummed."""
    length = 11 + len(payload) + 2
    head = bytes([DUML_MAGIC, length & 0xFF, ((length >> 8) & 0x03) | (1 << 2)])
    head += bytes([crc8(head)])
    body = head + bytes([
        sender, receiver, seq & 0xFF, (seq >> 8) & 0xFF, cmd_type, cmd_set, cmd_id,
    ]) + payload
    crc = crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def parse_frame(raw: bytes) -> DumlFrame | None:
    """Validate magic, length and both checksums; None on anything off."""
    if len(raw) < 13 or raw[0] != DUML_MAGIC:
        return None
    length = raw[1] | ((raw[2] & 0x03) << 8)
    if length != len(raw):
        return None
    if crc8(raw[:3]) != raw[3]:
        return None
    if crc16(raw[:-2]) != (raw[-2] | (raw[-1] << 8)):
        return None
    return DumlFrame(
        sender=raw[4],
        receiver=raw[5],
        seq=raw[6] | (raw[7] << 8),
        cmd_type=raw[8],
        cmd_set=raw[9],
        cmd_id=raw[10],
        payload=bytes(raw[11:-2]),
    )


class DumlReassembler:
    """Notifications are not frames: the Power 2000's 342-byte config reply
    arrives split across several notifications, and one notification can
    carry more than one frame. Buffer the byte stream and cut frames out by
    magic, length and both checksums, skipping a byte on anything that does
    not validate."""

    _MAX_BUFFER = 4096

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[DumlFrame]:
        self._buf += data
        frames: list[DumlFrame] = []
        while True:
            start = self._buf.find(DUML_MAGIC)
            if start < 0:
                self._buf.clear()
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < 4:
                break
            length = self._buf[1] | ((self._buf[2] & 0x03) << 8)
            if length < 13 or crc8(bytes(self._buf[:3])) != self._buf[3]:
                del self._buf[0]
                continue
            if len(self._buf) < length:
                break
            frame = parse_frame(bytes(self._buf[:length]))
            if frame is None:
                del self._buf[0]
                continue
            frames.append(frame)
            del self._buf[:length]
        if len(self._buf) > self._MAX_BUFFER:
            self._buf.clear()
        return frames


# ---------------------------------------------------------------------------
# Station session (pair key): TLV records, parsers and request builders.
# Records are `u16le id, u16le length, value`; payloads open with a header
# that varies by command, so records are found by scanning, not by offset.
# ---------------------------------------------------------------------------


def normalise_pair_key(value: Any) -> str | None:
    """The 32-character hex pair key, lowercased, or None if it is not one."""
    key = str(value or "").strip().lower()
    return key if _PAIR_KEY_RE.match(key) else None


def uses_station_session(device: dict[str, Any]) -> bool:
    """True when this device row runs the station session (a valid pair key in
    its config). The one test for it: the coordinator and the card agree."""
    return normalise_pair_key((device.get("config") or {}).get("pair_key")) is not None


def iter_tlvs(data: bytes) -> list[tuple[int, bytes]]:
    """Consecutive records from the start of `data`; stops at the first that overruns."""
    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos + 4 <= len(data):
        tlv_id, length = struct.unpack_from("<HH", data, pos)
        end = pos + 4 + length
        if end > len(data):
            break
        out.append((tlv_id, data[pos + 4:end]))
        pos = end
    return out


def find_tlv(data: bytes, tlv_id: int, min_length: int = 0) -> bytes | None:
    """The first record with this id whose declared length fits the payload."""
    needle = struct.pack("<H", tlv_id)
    pos = data.find(needle)
    while pos >= 0:
        if pos + 4 <= len(data):
            length = struct.unpack_from("<H", data, pos + 2)[0]
            end = pos + 4 + length
            if end <= len(data) and length >= min_length:
                return data[pos + 4:end]
        pos = data.find(needle, pos + 1)
    return None


def parse_station_realtime(payload: bytes) -> dict[str, Any]:
    """Realtime telemetry (0x5A / 0x61), pushed about once a second.

    0x3020 is the battery record: SoC x100 at [0:2] (5700 read 57 % with
    DJI Home showing 57 %), minutes remaining at [2:4], temperature x100 at
    [9:11]. 0x3030 opens with total output and input watts (u16 each; 13 W
    with DJI Home showing 13 W), then nests 0x3031 > 0x3032 (group) >
    0x3033 > 0x3034 (one interface: port, type, state, out W, in W). An
    interface record is present only while that interface is live, so a
    missing AC record means 0 W, not unknown, once 0x3030 itself arrived.
    """
    data: dict[str, Any] = {}
    battery = find_tlv(payload, 0x3020, min_length=4)
    if battery is not None:
        data["state_of_charge"] = round(_u16(battery, 0) / 100, 1)
        data["remaining_minutes"] = _u16(battery, 2)
        if len(battery) >= 11:
            data["temperature_c"] = round(_u16(battery, 9) / 100, 1)
    power = find_tlv(payload, 0x3030, min_length=4)
    if power is not None:
        data["output_power_w"] = _u16(power, 0)
        data["input_power_w"] = _u16(power, 2)
        ac_out = ac_in = 0
        for outer_id, outer in iter_tlvs(power[4:]):
            if outer_id != 0x3031:
                continue
            for group_id, group in iter_tlvs(outer):
                if group_id != 0x3032 or not group:
                    continue
                for inner_id, inner in iter_tlvs(group[1:]):
                    if inner_id != 0x3033:
                        continue
                    for leaf_id, leaf in iter_tlvs(inner):
                        if leaf_id != 0x3034 or len(leaf) < 7:
                            continue
                        if leaf[1] == INTERFACE_AC_OUTLET:
                            ac_out += _u16(leaf, 3)
                        elif leaf[1] == INTERFACE_AC_INLET:
                            ac_in += _u16(leaf, 5)
        data["ac_output_power_w"] = ac_out
        data["ac_input_power_w"] = ac_in
    return data


def parse_station_config(payload: bytes) -> dict[str, Any]:
    """Config read reply (0x60) or pushed report (0x62).

    0x1000 carries the main and communication-module firmware strings at
    [7:23] and [24:40]. 0x100D is the output table: 0x1014 records of
    (interface type, port, state), state 1 on and 2 off.
    """
    data: dict[str, Any] = {}
    base = find_tlv(payload, 0x1000, min_length=40)
    if base is not None:
        firmware = _ascii(base, 7, 23)
        if firmware:
            data["firmware_version"] = firmware
        comms = _ascii(base, 24, 40)
        if comms:
            data["comms_firmware_version"] = comms
    outputs = find_tlv(payload, 0x100D)
    if outputs is not None:
        for rec_id, rec in iter_tlvs(outputs):
            if rec_id == 0x1014 and len(rec) >= 3 and rec[0] == INTERFACE_AC_OUTLET:
                if rec[2] in (_OUTPUT_STATE_ON, _OUTPUT_STATE_OFF):
                    data["ac_output_on"] = rec[2] == _OUTPUT_STATE_ON
                    data["_ac_port"] = rec[1]
                break
    return data


def write_result_ok(payload: bytes, tlv_id: int) -> bool:
    """A 0x63 reply carries one u32 result per record written; 0 is success."""
    result = find_tlv(payload, tlv_id, min_length=4)
    return result is not None and _u32(result, 0) == 0


def build_station_frame(cmd_id: int, seq: int, payload: bytes = b"", *,
                        cmd_type: int = CMD_TYPE_STATION_REQUEST) -> bytes:
    return build_frame(
        CMD_SET_STATION, cmd_id, seq, payload,
        receiver=RECEIVER_STATION, cmd_type=cmd_type,
    )


def build_auth_payload(session: bytes, pair_key: str) -> bytes:
    """Second 0x6A request: 01, the four session bytes the first reply gave,
    the key as 32 ASCII hex characters, 00."""
    return b"\x01" + session[:4] + pair_key.encode("ascii") + b"\x00"


def _keyed_header(timestamp_ms: int | None = None) -> bytes:
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    return b"\x00\x00\x10\x00" + timestamp_ms.to_bytes(8, "little") + b"\x00" * 4


def _tlv(tlv_id: int, value: bytes) -> bytes:
    return struct.pack("<HH", tlv_id, len(value)) + value


def build_ac_output_payload(on: bool, port: int = 1, *, timestamp_ms: int | None = None) -> bytes:
    """The 0x63 write DJI Home sends for the AC switch: one 0x000D record
    (AC outlet, port, state) inside 0x100D, then its 0x100E companion."""
    state = _OUTPUT_STATE_ON if on else _OUTPUT_STATE_OFF
    record = _tlv(0x000D, bytes([INTERFACE_AC_OUTLET, port, state]))
    return (
        _keyed_header(timestamp_ms)
        + _tlv(0x100D, record)
        + _tlv(0x100E, _WRITE_COMPANION_100E)
    )


# ---------------------------------------------------------------------------
# Record decoders. Offsets are into the reply payload, whose byte 0 is a
# status byte (0x00 on every reply seen).
# ---------------------------------------------------------------------------


def _u16(p: bytes, off: int) -> int:
    return struct.unpack_from("<H", p, off)[0]


def _u32(p: bytes, off: int) -> int:
    return struct.unpack_from("<I", p, off)[0]


def _i32(p: bytes, off: int) -> int:
    return struct.unpack_from("<i", p, off)[0]


def _ascii(p: bytes, start: int, end: int) -> str:
    return p[start:end].decode("ascii", errors="ignore").strip("\x00 ")


def _version_quad(p: bytes, off: int) -> str:
    """DJI stores a version as four little-endian bytes: `03 00 01 0a` reads 10.1.0.3."""
    b0, b1, b2, b3 = p[off:off + 4]
    return f"{b3}.{b2}.{b1}.{b0}"


def parse_dynamic(payload: bytes) -> dict[str, Any]:
    """Battery dynamic data (0x0D / 0x02), 41 bytes on the Power 1000 V2."""
    if len(payload) < 33:
        raise ValueError(f"dynamic record too short: {len(payload)} bytes")
    voltage_v = _u32(payload, 1) / 256 / 1000
    current_a = _i32(payload, 6) / 1000
    data: dict[str, Any] = {
        "voltage_v": round(voltage_v, 3),
        "current_a": round(current_a, 3),
        "power_w": round(voltage_v * current_a, 1),
        "full_capacity_ah": round(_u32(payload, 10) / 1000, 3),
        "remaining_capacity_ah": round(_u32(payload, 14) / 1000, 3),
        "temperature_c": round(_u16(payload, 18) / 10, 1),
        "cell_count": payload[20],
        "state_of_charge": payload[21],
        "state_of_health": payload[32],
        "flags": _u16(payload, 26),
        "charging": current_a > _FLOW_THRESHOLD_A,
        "discharging": current_a < -_FLOW_THRESHOLD_A,
    }
    return data


def parse_cells(payload: bytes) -> list[float]:
    """Cell voltages (0x0D / 0x03): `00 00 <count>` then count × u16 LE mV."""
    if len(payload) < 3:
        return []
    count = payload[2]
    if len(payload) < 3 + 2 * count:
        return []
    return [round(_u16(payload, 3 + 2 * i) / 1000, 3) for i in range(count)]


def parse_static(payload: bytes) -> dict[str, Any]:
    """Battery static data (0x0D / 0x01), 45 bytes on the Power 1000 V2."""
    if len(payload) < 39:
        raise ValueError(f"static record too short: {len(payload)} bytes")
    return {
        "design_capacity_ah": round(_u32(payload, 2) / 1000, 3),
        "design_voltage_v": round(_u32(payload, 8) / 1000, 3),
        "product_code": _ascii(payload, 26, 31),
        "firmware_version": _version_quad(payload, 31),
        "firmware_version_2": _version_quad(payload, 35),
    }


def parse_version(payload: bytes) -> dict[str, Any]:
    """General version query (0x00 / 0x01): status, 0x01, 14-char serial, versions."""
    if len(payload) < 16:
        raise ValueError(f"version record too short: {len(payload)} bytes")
    data: dict[str, Any] = {"serial_number": _ascii(payload, 2, 16)}
    if len(payload) >= 26:
        data["firmware_version"] = _version_quad(payload, 18)
        data["firmware_version_2"] = _version_quad(payload, 22)
    return data


def classify_dji_model(device_name: str | None) -> tuple[str | None, str | None, str | None]:
    """Resolve an advertised name to (slug, display name, image file).

    The advert name is the marketing product name with the spaces removed,
    then a dash and a per-unit suffix: `Power1000V2-1001UC`. Matching is
    LONGEST-prefix, so a 1000 V2 is not downgraded to a plain 1000 (the same
    trap the VE.Bus family detection has with MultiPlus vs MultiPlus-II).

    An unrecognised DJI Power unit returns all None on purpose: it is still
    discovered, added and read (every model answers the same records), it
    just keeps the generic catalog name rather than being labelled as a
    model it might not be.
    """
    if not device_name:
        return (None, None, None)

    from ..device_catalog import PHYSICAL_DEVICE_CATALOG

    name = str(device_name).strip()
    best: tuple[str | None, str | None, str | None] = (None, None, None)
    best_len = -1
    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("integration") != "sensa_dji_ble":
            continue
        detection = entry.get("model_detection") or {}
        if detection.get("source") != "device_name":
            continue
        display_names = detection.get("display_names") or {}
        model_images = detection.get("model_images") or {}
        for prefix, slug in (detection.get("device_name_prefixes") or {}).items():
            if name.startswith(prefix) and len(prefix) > best_len:
                best = (slug, display_names.get(slug), model_images.get(slug))
                best_len = len(prefix)
    return best


def cell_stats(cells: list[float]) -> dict[str, Any]:
    if not cells:
        return {}
    lo, hi = min(cells), max(cells)
    return {
        "cell_voltages": cells,
        "cell_min_v": lo,
        "cell_max_v": hi,
        "cell_delta_v": round(hi - lo, 3),
    }


# ---------------------------------------------------------------------------
# GATT client
# ---------------------------------------------------------------------------


class DjiPowerStation:
    """One connected unit. Holds the bleak client across polls; the
    coordinator drops it on error so the next poll reconnects clean."""

    def __init__(self, ble_device: Any) -> None:
        self._ble_device = ble_device
        self._client: Any | None = None
        self._seq = 1
        self._pending: dict[int, asyncio.Future[DumlFrame]] = {}
        self._static: dict[str, Any] | None = None

    async def _ensure_client(self) -> Any:
        if self._client is not None and getattr(self._client, "is_connected", False):
            return self._client
        from bleak import BleakClient
        from bleak_retry_connector import establish_connection

        client = await establish_connection(
            BleakClient,
            self._ble_device,
            f"DJI Power {self._ble_device.address}",
            max_attempts=3,
        )
        await client.start_notify(DJI_NOTIFY_UUID, self._on_notify)
        await asyncio.sleep(_NOTIFY_SETTLE_SECS)
        self._client = client
        return client

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        frame = parse_frame(bytes(data))
        if frame is None or not frame.is_ack:
            return
        fut = self._pending.pop(frame.seq, None)
        if fut is not None and not fut.done():
            fut.set_result(frame)

    async def _exchange(
        self, cmd_set: int, cmd_id: int, payload: bytes = b"",
    ) -> bytes | None:
        """Write one request, return the matching reply's payload or None."""
        client = await self._ensure_client()
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[DumlFrame] = loop.create_future()
        self._pending[seq] = fut
        try:
            await client.write_gatt_char(
                DJI_WRITE_UUID, build_frame(cmd_set, cmd_id, seq, payload), response=False,
            )
            frame = await asyncio.wait_for(fut, timeout=_RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(seq, None)
            _LOGGER.debug(
                "DJI: no reply to set 0x%02X cmd 0x%02X within %.1fs",
                cmd_set, cmd_id, _RESPONSE_TIMEOUT,
            )
            return None
        return frame.payload

    async def async_update(self) -> dict[str, Any]:
        """One poll: dynamic data (required), cells, and the static records once."""
        dynamic = await self._exchange(CMD_SET_BATTERY, CMD_BATTERY_DYNAMIC)
        if not dynamic:
            raise UpdateFailed("no reply to the battery dynamic-data request")
        sample = parse_dynamic(dynamic)

        cells = await self._exchange(CMD_SET_BATTERY, CMD_BATTERY_CELLS)
        if cells:
            sample.update(cell_stats(parse_cells(cells)))

        if self._static is None:
            static: dict[str, Any] = {}
            raw_static = await self._exchange(CMD_SET_BATTERY, CMD_BATTERY_STATIC)
            if raw_static:
                try:
                    static.update(parse_static(raw_static))
                except ValueError as err:
                    _LOGGER.debug("DJI: static record not decoded: %s", err)
            raw_version = await self._exchange(CMD_SET_GENERAL, CMD_GENERAL_VERSION)
            if raw_version:
                try:
                    static.update(parse_version(raw_version))
                except ValueError as err:
                    _LOGGER.debug("DJI: version record not decoded: %s", err)
            if static:
                self._static = static
        if self._static:
            sample.update(self._static)
        return sample

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if client is None:
            return
        try:
            await client.stop_notify(DJI_NOTIFY_UUID)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001, S110
            pass


class DjiPairKeyRejected(UpdateFailed):
    """The station answered the pair key with a non-zero status."""


class DjiKeyedPowerStation:
    """One authenticated station session (Power 2000). The link is held
    across polls because the unit pushes telemetry; while it is held DJI
    Home cannot use Bluetooth to the same unit (Wi-Fi and cloud still work).
    The coordinator drops it on error so the next poll reconnects clean."""

    def __init__(self, ble_device: Any, pair_key: str) -> None:
        self._ble_device = ble_device
        self._pair_key = pair_key
        self._client: Any | None = None
        self._seq = 1
        self._pending: dict[tuple[int, int], asyncio.Future[DumlFrame]] = {}
        self._reassembler = DumlReassembler()
        self._realtime: dict[str, Any] = {}
        self._realtime_at: float | None = None
        self._config: dict[str, Any] = {}
        self._ready = False

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return seq

    async def _ensure_session(self) -> Any:
        if (
            self._ready
            and self._client is not None
            and getattr(self._client, "is_connected", False)
        ):
            return self._client
        if self._client is not None:
            await self.disconnect()
        from bleak import BleakClient
        from bleak_retry_connector import establish_connection

        self._reassembler = DumlReassembler()
        self._realtime, self._realtime_at = {}, None
        client = await establish_connection(
            BleakClient,
            self._ble_device,
            f"DJI Power {self._ble_device.address}",
            max_attempts=3,
        )
        self._client = client
        await client.start_notify(DJI_NOTIFY_UUID, self._on_notify)
        await asyncio.sleep(_NOTIFY_SETTLE_SECS)

        session = await self._exchange(CMD_STATION_AUTH, b"\x00")
        if not session or session[0] != 0 or len(session) < 5:
            raise UpdateFailed("no session reply to the authentication request")
        result = await self._exchange(
            CMD_STATION_AUTH, build_auth_payload(session[1:5], self._pair_key),
        )
        if not result:
            raise UpdateFailed("no reply to the pair key")
        if result[0] != 0:
            raise DjiPairKeyRejected("the station rejected the pair key")

        config = await self._exchange(CMD_STATION_CONFIG_READ, STATION_CONFIG_READ_PAYLOAD)
        if config:
            self._config.update(parse_station_config(config))
        self._ready = True
        return client

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        for frame in self._reassembler.feed(bytes(data)):
            if frame.cmd_set != CMD_SET_STATION:
                continue
            if frame.is_ack:
                fut = self._pending.pop((frame.seq, frame.cmd_id), None)
                if fut is not None and not fut.done():
                    fut.set_result(frame)
                continue
            if frame.cmd_id == CMD_STATION_REALTIME:
                parsed = parse_station_realtime(frame.payload)
                if parsed:
                    self._realtime = parsed
                    self._realtime_at = time.monotonic()
            elif frame.cmd_id == CMD_STATION_CONFIG_REPORT:
                self._config.update(parse_station_config(frame.payload))
                if frame.cmd_type == CMD_TYPE_PUSH_WANTS_ACK:
                    asyncio.get_running_loop().create_task(self._ack_report(frame.seq))

    async def _ack_report(self, seq: int) -> None:
        """DJI Home acks every 0x62 that asks (type 0x40) with payload 01."""
        client = self._client
        if client is None:
            return
        try:
            await client.write_gatt_char(
                DJI_STATION_WRITE_UUID,
                build_station_frame(
                    CMD_STATION_CONFIG_REPORT, seq, b"\x01",
                    cmd_type=CMD_TYPE_ACK_BIT,
                ),
                response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("DJI: 0x62 ack failed: %s", err)

    async def _exchange(self, cmd_id: int, payload: bytes = b"") -> bytes | None:
        """Write one station request, return the matching reply's payload or None."""
        client = self._client
        if client is None:
            return None
        seq = self._next_seq()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[DumlFrame] = loop.create_future()
        self._pending[(seq, cmd_id)] = fut
        try:
            await client.write_gatt_char(
                DJI_STATION_WRITE_UUID, build_station_frame(cmd_id, seq, payload), response=True,
            )
            frame = await asyncio.wait_for(fut, timeout=_RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop((seq, cmd_id), None)
            _LOGGER.debug(
                "DJI: no reply to station cmd 0x%02X within %.1fs", cmd_id, _RESPONSE_TIMEOUT,
            )
            return None
        return frame.payload

    async def async_update(self) -> dict[str, Any]:
        """The latest pushed telemetry, waiting briefly for the first after a connect."""
        await self._ensure_session()
        deadline = time.monotonic() + _FIRST_REALTIME_WAIT_SECS
        while self._realtime_at is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if self._realtime_at is None:
            raise UpdateFailed("authenticated, but no realtime telemetry arrived")
        if time.monotonic() - self._realtime_at > _REALTIME_MAX_AGE_SECS:
            raise UpdateFailed("realtime telemetry stopped arriving")
        sample = dict(self._realtime)
        sample.update({k: v for k, v in self._config.items() if not k.startswith("_")})
        return sample

    async def async_set_ac_output(self, on: bool) -> None:
        """Switch the AC outlets, the write DJI Home sends; raises on a refusal."""
        await self._ensure_session()
        port = int(self._config.get("_ac_port") or 1)
        result = await self._exchange(
            CMD_STATION_CONFIG_WRITE, build_ac_output_payload(on, port),
        )
        if result is None:
            raise UpdateFailed("no reply to the AC output write")
        if not write_result_ok(result, 0x100D):
            raise UpdateFailed("the station refused the AC output write")
        self._config["ac_output_on"] = on

    async def disconnect(self) -> None:
        self._ready = False
        client = self._client
        self._client = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if client is None:
            return
        try:
            await client.stop_notify(DJI_NOTIFY_UUID)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001, S110
            pass


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def _canonical_mac(value: str) -> str:
    """Colon-separated uppercase MAC for `async_ble_device_from_address`.

    The backend stores whatever the picker sent (often no separators);
    HA's scanner registry only matches the canonical form. Same trap as
    `bms_ble._canonical_mac`, same fix.
    """
    cleaned = str(value or "").upper().replace(":", "").replace("-", "").replace(" ", "")
    if len(cleaned) != 12:
        return str(value or "").upper()
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))


class DjiPowerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls one DJI Power station over BLE GATT."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        device_id: str,
        mac_address: str,
        display_name: str,
        pair_key: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"sensa_dji_power_{device_id}",
            update_interval=POLL_INTERVAL,
        )
        self._device_id = device_id
        self._mac_address = _canonical_mac(mac_address)
        self._display_name = display_name
        self._pair_key = normalise_pair_key(pair_key)
        self._station: DjiPowerStation | DjiKeyedPowerStation | None = None
        self._logged_first_sample = False

    @property
    def mac_address(self) -> str:
        return self._mac_address

    @property
    def has_pair_key(self) -> bool:
        return self._pair_key is not None

    async def async_set_pair_key(self, pair_key: str | None) -> None:
        """A changed key takes effect on the next connect; the session is dropped now."""
        key = normalise_pair_key(pair_key)
        if key == self._pair_key:
            return
        self._pair_key = key
        self._logged_first_sample = False
        await self._drop_station()
        await self.async_request_refresh()

    async def async_set_ac_output(self, on: bool) -> None:
        if self._pair_key is None:
            raise UpdateFailed("AC output needs the station's pair key")
        station = await self._ensure_station()
        try:
            await station.async_set_ac_output(on)  # type: ignore[union-attr]
        except Exception:
            await self._drop_station()
            raise
        if self.data is not None:
            self.async_set_updated_data({**self.data, "ac_output_on": on})

    async def _ensure_station(self) -> DjiPowerStation | DjiKeyedPowerStation:
        if self._station is not None:
            return self._station
        try:
            from homeassistant.components.bluetooth import (
                async_ble_device_from_address,
            )
        except ImportError as err:
            raise UpdateFailed("bluetooth integration unavailable") from err

        ble_device = async_ble_device_from_address(
            self.hass, self._mac_address, connectable=True
        )
        if ble_device is None:
            ble_device = async_ble_device_from_address(self.hass, self._mac_address)
        if ble_device is None:
            raise UpdateFailed(
                f"No BLE device found for {self._mac_address}, out of range "
                "of any scanner"
            )
        if self._pair_key is not None:
            self._station = DjiKeyedPowerStation(ble_device, self._pair_key)
        else:
            self._station = DjiPowerStation(ble_device)
        return self._station

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            station = await self._ensure_station()
            sample = await station.async_update()
        except UpdateFailed:
            await self._drop_station()
            raise
        except Exception as err:
            await self._drop_station()
            raise UpdateFailed(f"DJI read failed: {err}") from err

        data = dict(sample)
        data["_mac"] = self._mac_address
        data["_display_name"] = self._display_name
        if not self._logged_first_sample:
            self._logged_first_sample = True
            _LOGGER.info("DJI Power %s first sample: %s", self._device_id, data)
        return data

    async def _drop_station(self) -> None:
        station = self._station
        self._station = None
        if station is not None:
            await station.disconnect()

    async def async_shutdown(self) -> None:
        await self._drop_station()
        await super().async_shutdown()


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

# (key, name, unit, device_class, state_class, icon, precision, diagnostic)
_SENSOR_FIELDS: tuple[tuple[str, str, str | None, str | None, str | None, str, int | None, bool], ...] = (
    ("state_of_charge", "State of Charge", "%", "battery", "measurement", "mdi:battery", 0, False),
    ("state_of_health", "State of Health", "%", None, "measurement", "mdi:battery-heart-variant", 0, False),
    ("voltage_v", "Pack Voltage", "V", "voltage", "measurement", "mdi:flash", 2, False),
    ("current_a", "Pack Current", "A", "current", "measurement", "mdi:current-dc", 2, False),
    ("power_w", "Pack Power", "W", "power", "measurement", "mdi:lightning-bolt", 0, False),
    ("remaining_capacity_ah", "Remaining Capacity", "Ah", None, "measurement", "mdi:battery-charging-60", 2, False),
    ("full_capacity_ah", "Full Charge Capacity", "Ah", None, "measurement", "mdi:battery-high", 2, False),
    ("design_capacity_ah", "Design Capacity", "Ah", None, None, "mdi:battery-outline", 1, True),
    ("design_voltage_v", "Design Voltage", "V", "voltage", None, "mdi:flash-outline", 1, True),
    ("temperature_c", "Temperature", "°C", "temperature", "measurement", "mdi:thermometer", 1, False),
    ("cell_count", "Cell Count", None, None, None, "mdi:battery-sync", 0, True),
    ("cell_min_v", "Min Cell Voltage", "V", "voltage", "measurement", "mdi:arrow-collapse-down", 3, False),
    ("cell_max_v", "Max Cell Voltage", "V", "voltage", "measurement", "mdi:arrow-collapse-up", 3, False),
    ("cell_delta_v", "Cell Voltage Delta", "V", "voltage", "measurement", "mdi:sine-wave", 3, False),
    ("serial_number", "Serial Number", None, None, None, "mdi:barcode", None, True),
    ("product_code", "Product Code", None, None, None, "mdi:tag", None, True),
    ("firmware_version", "Firmware Version", None, None, None, "mdi:chip", None, True),
    # Station session only (pair key); unavailable on the battery-records path.
    ("remaining_minutes", "Remaining Time", "min", "duration", "measurement", "mdi:timer-sand", 0, False),
    ("output_power_w", "Output Power", "W", "power", "measurement", "mdi:export", 0, False),
    ("input_power_w", "Input Power", "W", "power", "measurement", "mdi:import", 0, False),
    ("ac_output_power_w", "AC Output Power", "W", "power", "measurement", "mdi:power-socket", 0, False),
    ("ac_input_power_w", "AC Input Power", "W", "power", "measurement", "mdi:power-plug", 0, False),
    ("comms_firmware_version", "Comms Firmware Version", None, None, None, "mdi:chip", None, True),
)

_BINARY_FIELDS: tuple[tuple[str, str, str | None, str], ...] = (
    ("charging", "Charging", "battery_charging", "mdi:battery-charging"),
    ("discharging", "Discharging", None, "mdi:battery-arrow-down"),
)


def _device_info(coordinator: DjiPowerCoordinator, device_id: str, display_name: str) -> dict[str, Any]:
    data = coordinator.data or {}
    # The model shown on the HA device page comes from the advertised name via
    # the catalog, so a 2000 does not read as a 1000 V2. An unrecognised unit
    # gets the family name rather than a guess.
    _slug, model, _image = classify_dji_model(display_name)
    info: dict[str, Any] = {
        "identifiers": {("sensa_nomad", f"dji_power_{device_id}")},
        "name": display_name,
        "manufacturer": "DJI",
        "model": model or "Power Station",
    }
    if data.get("serial_number"):
        info["serial_number"] = data["serial_number"]
    if data.get("firmware_version"):
        info["sw_version"] = data["firmware_version"]
    return info


def build_dji_sensor_entities(
    coordinator: DjiPowerCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
    *,
    include_static: bool = True,
    include_dynamic: bool = True,
) -> list[Any]:
    """Scalar sensors (fixed set) plus one sensor per cell (from the sample)."""
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    try:
        from homeassistant.const import EntityCategory
        diagnostic_category: Any = EntityCategory.DIAGNOSTIC
    except (ImportError, AttributeError):  # partial HA stub under test
        diagnostic_category = None

    class _DjiScalarSensor(CoordinatorEntity[DjiPowerCoordinator], SensorEntity):
        _attr_has_entity_name = True

        def __init__(self, key: str, name: str, unit: str | None, device_class: str | None,
                     state_class: str | None, icon: str, precision: int | None, diagnostic: bool) -> None:
            super().__init__(coordinator)
            self._key = key
            self._attr_unique_id = f"{entry_id}_dji_power_{device_id}_{key}"
            self._attr_name = name
            self._attr_icon = icon
            if unit:
                self._attr_native_unit_of_measurement = unit
            if device_class:
                self._attr_device_class = device_class
            if state_class:
                self._attr_state_class = state_class
            if precision is not None:
                self._attr_suggested_display_precision = precision
            if diagnostic and diagnostic_category is not None:
                self._attr_entity_category = diagnostic_category

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def native_value(self) -> Any:
            return (self.coordinator.data or {}).get(self._key)

        @property
        def available(self) -> bool:
            return (
                self.coordinator.last_update_success
                and self.coordinator.data is not None
                and self._key in self.coordinator.data
            )

    class _DjiCellSensor(CoordinatorEntity[DjiPowerCoordinator], SensorEntity):
        _attr_has_entity_name = True
        _attr_device_class = "voltage"
        _attr_state_class = "measurement"
        _attr_native_unit_of_measurement = "V"
        _attr_suggested_display_precision = 3
        _attr_icon = "mdi:sine-wave"

        def __init__(self, index: int) -> None:
            super().__init__(coordinator)
            self._index = index
            self._attr_unique_id = f"{entry_id}_dji_power_{device_id}_cell_{index + 1}"
            self._attr_name = f"Cell {index + 1} Voltage"

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def native_value(self) -> Any:
            cells = (self.coordinator.data or {}).get("cell_voltages")
            if not isinstance(cells, (list, tuple)) or self._index >= len(cells):
                return None
            return cells[self._index]

    entities: list[Any] = []
    if include_static:
        entities.extend(_DjiScalarSensor(*field) for field in _SENSOR_FIELDS)
    if include_dynamic:
        cells = (coordinator.data or {}).get("cell_voltages") or []
        entities.extend(_DjiCellSensor(i) for i in range(len(cells)))
    return entities


def build_dji_dynamic_sensor_entities(
    coordinator: DjiPowerCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
) -> list[Any]:
    """The per-cell sensors; empty until the first sample carries cells."""
    if not (coordinator.data or {}).get("cell_voltages"):
        return []
    return build_dji_sensor_entities(
        coordinator, entry_id, device_id, display_name,
        include_static=False, include_dynamic=True,
    )


def build_dji_binary_sensor_entities(
    coordinator: DjiPowerCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
) -> list[Any]:
    from homeassistant.components.binary_sensor import BinarySensorEntity
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    class _DjiFlagSensor(CoordinatorEntity[DjiPowerCoordinator], BinarySensorEntity):
        _attr_has_entity_name = True

        def __init__(self, key: str, name: str, device_class: str | None, icon: str) -> None:
            super().__init__(coordinator)
            self._key = key
            self._attr_unique_id = f"{entry_id}_dji_power_{device_id}_{key}"
            self._attr_name = name
            self._attr_icon = icon
            if device_class:
                self._attr_device_class = device_class

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def is_on(self) -> bool | None:
            value = (self.coordinator.data or {}).get(self._key)
            return None if value is None else bool(value)

        @property
        def available(self) -> bool:
            return (
                self.coordinator.last_update_success
                and self.coordinator.data is not None
                and self._key in self.coordinator.data
            )

    return [_DjiFlagSensor(*field) for field in _BINARY_FIELDS]


def build_dji_switch_entities(
    coordinator: DjiPowerCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
) -> list[Any]:
    """The AC output switch. Created on every unit; available only once the
    station session has reported the AC outlet's state (needs the pair key)."""
    from homeassistant.components.switch import SwitchEntity
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    class _DjiAcOutputSwitch(CoordinatorEntity[DjiPowerCoordinator], SwitchEntity):
        _attr_has_entity_name = True
        _attr_name = "AC Output"
        _attr_icon = "mdi:power-socket"

        def __init__(self) -> None:
            super().__init__(coordinator)
            self._attr_unique_id = f"{entry_id}_dji_power_{device_id}_ac_output"

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def is_on(self) -> bool | None:
            value = (self.coordinator.data or {}).get("ac_output_on")
            return None if value is None else bool(value)

        @property
        def available(self) -> bool:
            return (
                self.coordinator.last_update_success
                and "ac_output_on" in (self.coordinator.data or {})
            )

        async def async_turn_on(self, **_kwargs: Any) -> None:
            await self.coordinator.async_set_ac_output(True)

        async def async_turn_off(self, **_kwargs: Any) -> None:
            await self.coordinator.async_set_ac_output(False)

    return [_DjiAcOutputSwitch()]
