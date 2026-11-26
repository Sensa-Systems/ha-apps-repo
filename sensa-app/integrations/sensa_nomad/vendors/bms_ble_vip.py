"""Roamer Gen 1 VIP BMS parser.

Gen 1 Roamer packs ship with a proprietary VIP BMS chipset. The BLE
GATT protocol isn't covered by `aiobmsble` (which handles JK, JBD,
Seplos, Daly, Ant only), so this module ports the frame format from an
ESPHome reference firmware that was verified against a Roamer Xtreme
1.0 pack. See `docs/devices/roamer-battery.md` "VIP protocol reference"
for the full spec.

**Wire protocol — frame shape:**

    3B 16 TYPE LEN <data...> CHKSUM 00 0D 0A

- `3B 16`           start bytes (2)
- `TYPE`            frame type (0x2A = summary, 0x24 = cell voltages)
- `LEN`             length of the data payload
- `<data...>`       LEN bytes of type-specific payload
- `CHKSUM`          sum of `(0x16, TYPE, LEN, <data...>)` modulo 256
- `00 0D 0A`        end bytes (3)

**Two probe requests run every 5 s**:

    Summary:       3B 16 2A 01 00 41 00 0D 0A
    Cell voltages: 3B 16 24 01 00 3B 00 0D 0A

**Summary response (type 0x2A, ≥24 bytes payload):**

| Offset | Type      | Field                               | Unit       |
|--------|-----------|-------------------------------------|------------|
| 0      | int32 LE  | Pack current (signed)               | mA         |
| 4      | uint32 LE | Pack voltage                        | mV         |
| 8      | int8      | BMS Temp 1                          | °C         |
| 9      | int8      | BMS Temp 2                          | °C         |
| 10     | int8      | Cell Temp 1                         | °C         |
| 11     | int8      | Cell Temp 2                         | °C         |
| 12     | uint16 LE | Remaining capacity                  | mAh        |
| 14     | uint16 LE | Cycle count                         | count      |
| 16     | uint32 LE | BMS flags bitmask                   | —          |
| 20     | uint32 LE | State of charge                     | %          |

Flag bits observed: 0x00000080 = charge inhibited, 0x00008000 =
discharge inhibited. Remaining bits are unmapped — preserved as
`problem_code` in the sample dict for diagnostics.

**Cell voltage response (type 0x24):**
    4 × uint16 LE, values in mV

GATT UUIDs:
  Service : 0000fa00-0000-1000-8000-00805f9b34fb
  Write   : 0000fa02-0000-1000-8000-00805f9b34fb
  Notify  : 0000fa01-0000-1000-8000-00805f9b34fb

The class mirrors `aiobmsble.BMS` interface (ctor takes a BLEDevice,
`async_update()` returns a dict, `device_info()` returns name/model
strings, `disconnect()` cleans up) so `BmsCoordinator` can dispatch
to it the same way it dispatches to the JK parser.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Advert name format: `Roamer-{capacity}A-{last-4-MAC-hex}` — e.g.
# `Roamer-200A-2493` for a 200 Ah pack. Used to extract the declared
# capacity without an extra BLE round-trip (VIP has no capacity info
# frame; the advert name is the only source).
_ADVERT_CAPACITY_RE = re.compile(r"Roamer-(\d+)A", re.IGNORECASE)

# GATT UUIDs (lowercase — aiobleak is case-sensitive on some backends).
VIP_SERVICE_UUID = "0000fa00-0000-1000-8000-00805f9b34fb"
VIP_WRITE_UUID = "0000fa02-0000-1000-8000-00805f9b34fb"
VIP_NOTIFY_UUID = "0000fa01-0000-1000-8000-00805f9b34fb"

# Frame constants
_FRAME_START = bytes([0x3B, 0x16])
_FRAME_END = bytes([0x00, 0x0D, 0x0A])

# Frame types — consumer-to-BMS requests. Responses arrive with the same
# TYPE byte as the request they answer.
FRAME_TYPE_SUMMARY = 0x2A
FRAME_TYPE_CELLS = 0x24

# Flag bits in the summary response's 32-bit flags field
FLAG_CHARGE_INHIBITED = 0x00000080
FLAG_DISCHARGE_INHIBITED = 0x00008000

# How long to wait for a complete response before giving up. The pack
# answers in well under a second on the bench; generous here so an
# oversized probe cycle or BLE retry still completes.
_RESPONSE_TIMEOUT = 5.0

# Delay after subscribing to the notify characteristic before writing
# the first probe. The BMS needs this window to register the CCCD
# enable internally — without it, probes are sent before the BMS
# acknowledges the subscription and responses never come. Value
# matches the ESPHome reference firmware exactly.
_NOTIFY_SETTLE_SECS = 0.7


def _checksum(type_byte: int, data: bytes) -> int:
    """VIP frame checksum — sum of `(0x16, TYPE, LEN, <data>)` mod 256.

    Derived by inspecting the two known request frames:
      - `3B 16 2A 01 00 41 …`  →  0x16+0x2A+0x01+0x00 = 0x41 ✓
      - `3B 16 24 01 00 3B …`  →  0x16+0x24+0x01+0x00 = 0x3B ✓

    Note that the first start byte `0x3B` is NOT included and the
    checksum covers LEN itself. Handy as a one-liner helper used by
    both the encoder and the decoder's integrity check.
    """
    return (0x16 + type_byte + len(data) + sum(data)) & 0xFF


def build_frame(type_byte: int, data: bytes = b"\x00") -> bytes:
    """Build a wire-format VIP frame for a given TYPE + data payload.

    Default data is a single `0x00` byte — that's what the ESPHome
    reference uses for both probe requests. LEN is derived from data.
    """
    length = len(data)
    chk = _checksum(type_byte, data)
    return (
        _FRAME_START
        + bytes([type_byte, length])
        + data
        + bytes([chk])
        + _FRAME_END
    )


def parse_frame(raw: bytes) -> tuple[int, bytes] | None:
    """Decode a VIP frame. Returns (type, data) on success, None on
    framing failure.

    Match the ESPHome reference firmware's tolerance exactly — it
    validates the start bytes and the declared length, but does NOT
    check the trailing bytes or the checksum. Real BMS responses may
    use a slightly different trailing byte sequence than the request
    format (observed: 2 trailing bytes after data, not the spec's 4);
    strict validation here was silently dropping valid responses.

    Layout we DO validate:
      - 2 start bytes: 0x3B 0x16
      - TYPE (1 byte)
      - LEN (1 byte) — payload length
      - DATA (LEN bytes)
      - at least 2 trailing bytes present (not validated)

    The ESP32 BLE stack sometimes fragments or coalesces notifications,
    so callers should be tolerant. Short frames and mid-stream garbage
    return None so the caller can keep accumulating.
    """
    # 2 (start) + 1 (type) + 1 (len) + 0 (data) + 2 (trailing) = 6 min
    if len(raw) < 6:
        return None
    if not raw.startswith(_FRAME_START):
        return None
    type_byte = raw[2]
    length = raw[3]
    # Need at least header (4) + declared data length + 2 trailing bytes.
    if len(raw) < 4 + length + 2:
        return None
    data = raw[4 : 4 + length]
    return type_byte, data


def parse_summary(
    data: bytes,
    design_capacity_ah: int | None = None,
) -> dict[str, Any] | None:
    """Decode a type-0x2A summary payload into a normalised sample dict.

    Field order locked to the protocol reference. Returns None when the
    payload is truncated (< 24 bytes) — partial frames from a pack just
    powering up can arrive short, and a None return lets the caller
    keep the previous sample rather than overwriting with zeros.

    `design_capacity_ah` is the pack's nameplate capacity (from the
    BLE advert name on Gen 1 — see `_advert_capacity_ah()`). When
    supplied, we derive Remaining Capacity as `capacity × SoC%` rather
    than reading the on-wire `remaining_mAh` field directly. Why: the
    wire field is a uint16 in milliamp-hours, so it wraps at 65 535
    mAh — any pack bigger than ~65 Ah (all current Roamers: 105 Ah /
    200 Ah / 280 Ah …) reports wildly wrong remaining values once SoC
    climbs past the wrap point. The two quantities are the same
    measurement via different paths; using the one that doesn't
    overflow gives an accurate reading without needing a firmware
    fix on the BMS. When capacity is unknown we fall back to the raw
    field so small packs (or diagnostic usage with a pack we haven't
    classified yet) still get a value.
    """
    if len(data) < 24:
        _LOGGER.debug("VIP summary truncated — got %d bytes, need 24", len(data))
        return None

    # struct format reference:
    #   <  = little-endian
    #   i  = int32 (signed)
    #   I  = uint32 (unsigned)
    #   b  = int8 (signed)
    #   H  = uint16 (unsigned)
    (
        current_ma, voltage_mv,
        bms_temp_1, bms_temp_2, cell_temp_1, cell_temp_2,
        remaining_mah, cycle_count,
        flags, soc_raw,
    ) = struct.unpack_from("<iIbbbbHHII", data, 0)

    # Prefer capacity × SoC when capacity is known; fall back to the
    # raw uint16 field otherwise (subject to wrap on packs > ~65 Ah).
    if design_capacity_ah is not None and 0 <= soc_raw <= 100:
        remaining_ah = design_capacity_ah * soc_raw / 100.0
    else:
        remaining_ah = remaining_mah / 1000.0

    voltage_v = voltage_mv / 1000.0
    current_a = current_ma / 1000.0

    # Normalise to BMSSample-compatible units. aiobmsble returns
    # volts / amps / Ah — stay consistent so entity descriptors can read
    # VIP packs with the same code as JK packs downstream.
    #
    # Key names match the aiobmsble BMSSample convention used by the
    # entity factory in `bms_ble.py` — notably `chrg_mosfet` /
    # `dischrg_mosfet` for the two MOSFET state flags (the
    # "Charging Allowed" / "Discharging Allowed" binary sensors read
    # those keys directly). VIP has no active balancer so `balancer`
    # is deliberately absent.
    sample: dict[str, Any] = {
        "voltage": voltage_v,
        "current": current_a,
        # VIP doesn't report pack power as its own field; derive from
        # the instantaneous V × I so the Pack Power entity has a
        # sensible reading instead of sitting "Unknown" forever.
        "power": voltage_v * current_a,
        "cycle_charge": remaining_ah,
        "cycles": cycle_count,
        "battery_level": soc_raw,
        "temp_values": [bms_temp_1, bms_temp_2, cell_temp_1, cell_temp_2],
        "problem_code": flags,
        "_raw_flags": flags,
        "_raw_soc": soc_raw,
        # Preserved so anyone diffing against a wire capture / the
        # ESPHome reference firmware (which publishes raw mAh) can
        # still see what the BMS actually put on the air.
        "_raw_remaining_mah": remaining_mah,
        # Matches the aiobmsble key names so
        # `BINARY_SENSOR_FIELDS[chrg_mosfet / dischrg_mosfet]` resolve
        # against the same sample dict that JK packs populate.
        "chrg_mosfet": not bool(flags & FLAG_CHARGE_INHIBITED),
        "dischrg_mosfet": not bool(flags & FLAG_DISCHARGE_INHIBITED),
    }

    return sample


def parse_cells(data: bytes) -> dict[str, Any] | None:
    """Decode a type-0x24 cell-voltages payload.

    Gen 1 packs are 4s (4 cells in series). Each cell is a uint16 LE
    in mV. Returns a dict slot that merges into the summary sample so
    the coordinator's combined poll produces one record per tick.
    """
    if len(data) < 8:
        _LOGGER.debug("VIP cell frame truncated — got %d bytes, need 8", len(data))
        return None

    cells_mv = struct.unpack_from("<4H", data, 0)
    cells_v = [mv / 1000.0 for mv in cells_mv]

    sample: dict[str, Any] = {
        "cell_voltages": cells_v,
        "cell_count": len(cells_v),
    }
    if cells_v:
        sample["delta_voltage"] = max(cells_v) - min(cells_v)
    return sample


class BMS:
    """aiobmsble-compatible VIP BMS parser.

    Named `BMS` to match the attribute aiobmsble.bms.* modules expose,
    so the same dispatch code in `BmsCoordinator._ensure_bms` works.

    State model: a single persistent bleak client, held across calls
    via `async_update()` → `disconnect()`. The coordinator invokes
    `disconnect()` after `device_info()` so next poll reconnects fresh
    (matches the JK parser's pattern — see bms_ble.py::_maybe_enrich).
    """

    def __init__(self, ble_device: Any) -> None:
        self._ble_device = ble_device
        self._client: Any | None = None
        # Accumulates the latest frame for each TYPE so we can merge
        # summary + cell frames into one sample regardless of the order
        # the pack sends them.
        self._latest_by_type: dict[int, bytes] = {}
        self._notification_event = asyncio.Event()

    async def _ensure_client(self) -> Any:
        """Lazy-connect. bleak's BleakClient is imported inside the
        function so test environments without bluez don't hit an import
        error just by importing this module.

        Critical timing: after `start_notify` subscribes to FA01, the
        BMS needs a beat to register the CCCD enable before it'll
        actually emit notifications. The ESPHome reference firmware
        (see `docs/devices/roamer-battery.md`) sleeps 700 ms here and
        the pack never responds to probes without it. This is the
        "handshake" — not a magic frame, just settle time.
        """
        if self._client is not None and getattr(self._client, "is_connected", False):
            return self._client

        # Use bleak_retry_connector instead of `BleakClient.connect()`
        # directly — HA's habluetooth wrappers emit a WARNING every time
        # a direct connect is attempted, and the retry connector handles
        # the "random, often transient, connection failures" that are
        # common on BlueZ / ESPHome proxies. Same pattern HA's own
        # bluetooth integrations use.
        from bleak import BleakClient
        from bleak_retry_connector import establish_connection

        client = await establish_connection(
            BleakClient,
            self._ble_device,
            f"Roamer VIP {self._ble_device.address}",
            max_attempts=3,
        )
        await client.start_notify(VIP_NOTIFY_UUID, self._on_notify)
        await asyncio.sleep(_NOTIFY_SETTLE_SECS)
        self._client = client
        return client

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        """Notification callback — stash the frame and wake the waiter.

        We don't try to validate the frame inline (that happens during
        poll assembly) so a partial / garbled frame doesn't crash the
        callback. The whole bytes() go into the buffer and poll-time
        `parse_frame` rejects anything invalid cleanly.
        """
        parsed = parse_frame(bytes(data))
        if parsed is None:
            return
        type_byte, _ = parsed
        self._latest_by_type[type_byte] = bytes(data)
        self._notification_event.set()

    async def _request(self, type_byte: int) -> bytes | None:
        """Send a probe frame, wait for its response, return the raw
        response bytes (or None on timeout). Clears the event before
        sending so an old notification can't satisfy this request.
        """
        client = await self._ensure_client()
        self._latest_by_type.pop(type_byte, None)
        self._notification_event.clear()

        frame = build_frame(type_byte)
        await client.write_gatt_char(VIP_WRITE_UUID, frame, response=False)

        try:
            await asyncio.wait_for(
                self._notification_event.wait(), timeout=_RESPONSE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            _LOGGER.debug("VIP: no response to type 0x%02X within %.1fs", type_byte, _RESPONSE_TIMEOUT)
            return None

        return self._latest_by_type.get(type_byte)

    async def async_update(self) -> dict[str, Any]:
        """One poll cycle — summary + cell voltages, merged.

        Matches aiobmsble.BMS.async_update's contract (returns a
        BMSSample-shaped dict). Empty dict on total failure so the
        coordinator treats the poll as no-data rather than crashing.

        Also seeds `design_capacity` from the BLE advert name (e.g.
        `Roamer-200A-2493` → 200 Ah) so the coordinator's
        capacity-detection path doesn't fall back to computing from
        cycle_charge/SOC — the VIP's `remaining_mAh` field is uint16 and
        overflows for a 200 Ah pack, producing a bogus ~8 Ah estimate
        that used to leak into the display name as "Roamer 8Ah …".
        """
        sample: dict[str, Any] = {}

        # Advert-derived capacity overrides anything the coordinator
        # would otherwise compute from cycle_charge.
        advert_capacity = self._advert_capacity_ah()
        if advert_capacity is not None:
            sample["design_capacity"] = advert_capacity

        summary_raw = await self._request(FRAME_TYPE_SUMMARY)
        if summary_raw is not None:
            parsed = parse_frame(summary_raw)
            if parsed is not None:
                type_byte, data = parsed
                if type_byte == FRAME_TYPE_SUMMARY:
                    summary = parse_summary(
                        data, design_capacity_ah=advert_capacity,
                    )
                    if summary is not None:
                        sample.update(summary)

        cells_raw = await self._request(FRAME_TYPE_CELLS)
        if cells_raw is not None:
            parsed = parse_frame(cells_raw)
            if parsed is not None:
                type_byte, data = parsed
                if type_byte == FRAME_TYPE_CELLS:
                    cells = parse_cells(data)
                    if cells is not None:
                        sample.update(cells)

        return sample

    def _advert_name(self) -> str | None:
        """Return the BLE advertised local name, if bleak surfaced one."""
        name = getattr(self._ble_device, "name", None)
        return str(name) if name else None

    def _advert_capacity_ah(self) -> int | None:
        """Pull `{capacity}A` out of the Roamer Gen 1 advert name, if present."""
        name = self._advert_name()
        if not name:
            return None
        match = _ADVERT_CAPACITY_RE.search(name)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    async def device_info(self) -> dict[str, Any]:
        """Return what device-identity info VIP can surface.

        The chipset has no equivalent of JK's type-0x03 device-info
        frame, so the only identifier we can produce is the BLE
        advert name (`Roamer-200A-2493`). That's plenty for the
        coordinator's enrichment + display-name logic — model slug is
        derived from the name, capacity from the `{N}A` segment.

        Returns a non-empty dict on purpose: if this returned `{}` the
        coordinator would treat it as "still need to fetch device info"
        and force a disconnect after every poll. A non-empty return
        caches and breaks that reconnect loop.

        Note: `sw_version` / `hw_version` / `serial_number` are NOT
        returned — VIP doesn't expose them. Those entities are
        suppressed per-family in `bms_ble.FAMILY_EXCLUDED_FIELDS` so
        they don't show up as permanently-unavailable.
        """
        info: dict[str, Any] = {"model": "Roamer Gen 1 VIP"}
        advert = self._advert_name()
        if advert:
            info["name"] = advert
        return info

    async def disconnect(self) -> None:
        """Drop the BLE connection. Called by the coordinator after
        each poll cycle so the pack stays reachable by other clients
        (the official Roamer app) between our 30 s polls.
        """
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.stop_notify(VIP_NOTIFY_UUID)
        except Exception:
            _LOGGER.debug("VIP: stop_notify failed", exc_info=True)
        try:
            await client.disconnect()
        except Exception:
            _LOGGER.debug("VIP: disconnect failed", exc_info=True)
