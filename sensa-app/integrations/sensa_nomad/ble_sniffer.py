"""Drive a Nordic nRF Sniffer for Bluetooth LE dongle plugged into the box.

The box's own Bluetooth adapter cannot overhear a phone talking to a device: a
connection is a private, frequency-hopping link. A Nordic dongle running the
nRF Sniffer firmware can, provided it is listening when the phone connects (it
follows the link from the CONNECT_IND). Support posts one to a customer, they
plug it into the box, and BLE Capture streams what it hears.

The serial protocol is Nordic's (nRF Sniffer 4.x, UART protocol v3): SLIP-framed
packets, a 6-byte header, then a per-type payload. Reimplemented here from
Nordic's SnifferAPI rather than vendored, because only scan, follow and the
packet decode are needed. Nordic's licence restricts the API to use with a
Nordic chip, which this is.

Everything that is not I/O is a pure function so it can be tested without a
dongle: :func:`slip_encode`, :class:`SlipDecoder`, :func:`build_request`,
:func:`parse_packet`, :func:`decode_ble` and the pcap helpers.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .usb_serial import ensure_dev_node, matching_ttys

_LOGGER = logging.getLogger(__name__)

# USB vendor ids the sniffer firmware enumerates under: Nordic's own (the
# nRF52840 dongle) and SEGGER's (a DK board's J-Link CDC port).
SNIFFER_USB_VENDORS = frozenset({"1915", "1366"})
BAUD_RATES = (1_000_000, 460_800)

SLIP_START = 0xAB
SLIP_END = 0xBC
SLIP_ESC = 0xCD
SLIP_ESC_START = SLIP_START + 1
SLIP_ESC_END = SLIP_END + 1
SLIP_ESC_ESC = SLIP_ESC + 1

PROTOVER_V1 = 1
HEADER_LENGTH = 6
BLE_HEADER_LENGTH = 10

REQ_FOLLOW = 0x00
EVENT_FOLLOW = 0x01
EVENT_PACKET_ADV_PDU = 0x02
EVENT_CONNECT = 0x05
EVENT_PACKET_DATA_PDU = 0x06
REQ_SCAN_CONT = 0x07
EVENT_DISCONNECT = 0x09
SET_TEMPORARY_KEY = 0x0C
REQ_VERSION = 0x1B
RESP_VERSION = 0x1C

PHY_CODED = 2

# pcap, LINKTYPE_NORDIC_BLE (272): each record is the board id followed by the
# sniffer's UART packet, which Wireshark's nordic_ble dissector decodes.
PCAP_GLOBAL_HEADER = struct.pack(
    "<LHHIILL", 0xA1B2C3D4, 2, 4, 0, 0, 0x0000FFFF, 272
)

# ATT opcodes worth naming in the live feed. Writes go phone -> device,
# notifications and indications come back.
_ATT_OPS: dict[int, tuple[str, str, bool]] = {
    # opcode: (name, direction, carries a handle)
    0x0A: ("read_req", "phone_to_device", True),
    0x0B: ("read_rsp", "device_to_phone", False),
    0x12: ("write_req", "phone_to_device", True),
    0x13: ("write_rsp", "device_to_phone", False),
    0x16: ("prepare_write", "phone_to_device", True),
    0x1B: ("notify", "device_to_phone", True),
    0x1D: ("indicate", "device_to_phone", True),
    0x52: ("write_cmd", "phone_to_device", True),
}


def slip_encode(data: bytes) -> bytes:
    out = bytearray([SLIP_START])
    for b in data:
        if b == SLIP_START:
            out += bytes([SLIP_ESC, SLIP_ESC_START])
        elif b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


class SlipDecoder:
    """Feed raw serial bytes, get whole decoded frames back."""

    def __init__(self) -> None:
        self._buf: bytearray | None = None
        self._esc = False

    def feed(self, data: bytes) -> list[bytes]:
        frames: list[bytes] = []
        for b in data:
            if self._buf is None:
                if b == SLIP_START:
                    self._buf = bytearray()
                continue
            if self._esc:
                self._esc = False
                self._buf.append(
                    {SLIP_ESC_START: SLIP_START, SLIP_ESC_END: SLIP_END,
                     SLIP_ESC_ESC: SLIP_ESC}.get(b, SLIP_END)
                )
            elif b == SLIP_ESC:
                self._esc = True
            elif b == SLIP_END:
                frames.append(bytes(self._buf))
                self._buf = None
            elif b == SLIP_START:
                # A lost END: resynchronise on the new frame.
                self._buf = bytearray()
            else:
                self._buf.append(b)
        return frames


def build_request(counter: int, packet_id: int, payload: bytes = b"") -> bytes:
    """One host-to-sniffer request, SLIP-framed and ready to write."""
    header = bytes([
        HEADER_LENGTH, len(payload), PROTOVER_V1,
        counter & 0xFF, (counter >> 8) & 0xFF, packet_id,
    ])
    return slip_encode(header + payload)


def follow_payload(address: str, address_type: int) -> bytes:
    """REQ_FOLLOW payload: address MSB first, its type, then flags (0 = follow
    advertising AND connections, legacy and extended, 1M PHY)."""
    return bytes.fromhex(address.replace(":", "")) + bytes([address_type & 1, 0])


@dataclass
class SnifferPacket:
    packet_id: int
    counter: int
    protover: int
    # The UART packet as it belongs in the pcap: the radio's padding byte
    # removed and the length patched to match, exactly as Nordic's own capture.
    raw: bytes
    # Set on ADV / DATA PDUs only.
    channel: int | None = None
    rssi: int | None = None
    crc_ok: bool = False
    encrypted: bool = False
    mic_ok: bool = False
    phy: int = 0
    ble: bytes = b""
    version: str | None = None


def parse_packet(frame: bytes) -> SnifferPacket | None:
    """Decode one SLIP-decoded frame, or None when it is malformed."""
    if len(frame) < HEADER_LENGTH:
        return None
    protover = frame[2]
    if protover == PROTOVER_V1:
        length = frame[1]
    else:
        length = frame[0] | (frame[1] << 8)
    if length + HEADER_LENGTH != len(frame):
        return None
    packet_id = frame[5]
    counter = frame[3] | (frame[4] << 8)
    pkt = SnifferPacket(packet_id, counter, protover, bytes(frame))
    if packet_id == RESP_VERSION:
        pkt.version = frame[HEADER_LENGTH:].decode("ascii", "replace").rstrip("\x00")
        return pkt
    if packet_id not in (EVENT_PACKET_ADV_PDU, EVENT_PACKET_DATA_PDU):
        return pkt
    if frame[6] != BLE_HEADER_LENGTH or len(frame) < HEADER_LENGTH + BLE_HEADER_LENGTH + 6:
        return None
    flags = frame[7]
    pkt.crc_ok = bool(flags & 1)
    pkt.encrypted = bool(flags & 4)
    pkt.mic_ok = bool(flags & 8)
    pkt.phy = (flags >> 4) & 7
    pkt.channel = frame[8]
    pkt.rssi = -frame[9]
    ble_pos = HEADER_LENGTH + BLE_HEADER_LENGTH
    # The hardware inserts one padding byte after the access address and the
    # PDU header (one later on coded PHY, after the coding indicator) that was
    # never on air.
    pad = ble_pos + 6 + (1 if pkt.phy == PHY_CODED else 0)
    fixed = bytearray(frame[:pad] + frame[pad + 1:])
    new_len = length - 1
    if protover == PROTOVER_V1:
        fixed[1] = new_len & 0xFF
    else:
        fixed[0] = new_len & 0xFF
        fixed[1] = (new_len >> 8) & 0xFF
    pkt.raw = bytes(fixed)
    ble = bytes(fixed[ble_pos:])
    if pkt.phy == PHY_CODED:
        ble = ble[:4] + ble[5:]
    pkt.ble = ble
    return pkt


def _addr(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in reversed(raw))


def decode_ble(pkt: SnifferPacket) -> dict[str, Any] | None:
    """What a person reading the live feed cares about, or None for noise.

    ``ble`` is access address (4) + PDU header (1) + length (1) + payload.
    """
    ble = pkt.ble
    if len(ble) < 6 or not pkt.crc_ok:
        return None
    header, payload = ble[4], ble[6:6 + ble[5]]
    if pkt.packet_id == EVENT_PACKET_ADV_PDU:
        adv_type = header & 0x0F
        if adv_type == 0x05 and len(payload) >= 12:  # CONNECT_IND
            return {
                "op": "connect",
                "initiator": _addr(payload[0:6]),
                "advertiser": _addr(payload[6:12]),
            }
        if adv_type in (0, 2, 4, 6) and len(payload) >= 6:
            return {
                "op": "advert",
                "advertiser": _addr(payload[0:6]),
                "address_type": (header >> 6) & 1,
            }
        return None
    if pkt.encrypted and not pkt.mic_ok:
        return {"op": "encrypted"}
    llid = header & 0x03
    if llid == 0x03 and payload:  # LL control PDU
        if payload[0] == 0x02:
            return {"op": "terminate"}
        if payload[0] == 0x03:
            return {"op": "encryption_start"}
        return None
    if llid != 0x02 or len(payload) < 5:
        return None
    cid = payload[2] | (payload[3] << 8)
    if cid != 0x0004:  # ATT only; SMP (6) is surfaced as pairing
        if cid == 0x0006:
            return {"op": "pairing"}
        return None
    att = payload[4:]
    known = _ATT_OPS.get(att[0])
    if known is None:
        return None
    name, direction, has_handle = known
    out: dict[str, Any] = {"op": name, "direction": direction}
    if has_handle and len(att) >= 3:
        out["handle"] = f"0x{att[1] | (att[2] << 8):04X}"
        out["hex"] = att[3:].hex()
    else:
        out["hex"] = att[1:].hex()
    return out


def pcap_record(raw: bytes, ts: float, board_id: int = 0) -> bytes:
    body = bytes([board_id]) + raw
    sec = int(ts)
    return struct.pack("<LLLL", sec, int((ts - sec) * 1_000_000), len(body), len(body)) + body


def find_sniffer_port() -> tuple[str | None, str | None]:
    """(device path, problem). Blocking: call from an executor.

    Looks for a USB serial port from a sniffer vendor (see `usb_serial`). The
    problem string says when the box lacks the stack's device rules.
    """
    try:
        names = matching_ttys(SNIFFER_USB_VENDORS)
    except OSError:
        return None, "No USB serial devices are visible to Home Assistant."
    for name in names:
        path, err = ensure_dev_node(name)
        if err is not None:
            return None, (
                f"The sniffer is plugged in ({name}) but Home Assistant cannot open it "
                f"({err}). The box needs the current stack update."
            )
        return path, None
    return None, "No sniffer dongle found. Plug it into the box and try again."


class Sniffer:
    """One sniffer session following one address. Runs its own thread.

    ``on_event(kind, data)`` and ``on_packet(record)`` are called from that
    thread; the caller marshals them onto the event loop.
    """

    def __init__(
        self,
        port: str,
        address: str,
        on_event: Callable[[str, dict[str, Any]], None],
        on_packet: Callable[[bytes], None],
    ) -> None:
        self._port = port
        self._address = address.upper()
        self._on_event = on_event
        self._on_packet = on_packet
        self._serial: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._counter = 0
        self.version: str | None = None
        self.following = False
        self.packets = 0
        self._said_encrypted = False

    def _send(self, packet_id: int, payload: bytes = b"") -> None:
        self._serial.write(build_request(self._counter, packet_id, payload))
        self._counter = (self._counter + 1) & 0xFFFF

    def open(self) -> None:
        """Open the port and confirm sniffer firmware answers. Blocking."""
        import serial  # pyserial ships with Home Assistant (the usb integration)

        last: Exception | None = None
        for baud in BAUD_RATES:
            ser = None
            try:
                ser = serial.Serial(self._port, baudrate=baud, rtscts=True,
                                    exclusive=True, timeout=0.2)
                ser.reset_input_buffer()
                self._serial = ser
                self._send(REQ_VERSION)
                decoder = SlipDecoder()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    for frame in decoder.feed(ser.read(256)):
                        pkt = parse_packet(frame)
                        if pkt is not None and pkt.packet_id == RESP_VERSION:
                            self.version = pkt.version
                            return
                ser.close()
            except Exception as err:  # noqa: BLE001 - reported to the operator
                last = err
                if ser is not None:
                    ser.close()
        self._serial = None
        raise RuntimeError(
            f"The dongle on {self._port} did not answer as an nRF Sniffer"
            + (f" ({last})" if last else "")
            + ". Check it is flashed with the nRF Sniffer firmware."
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sensa-ble-sniffer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop and close. Blocking: call from an executor."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                pass
            self._serial = None

    def _follow(self, address_type: int) -> None:
        self._send(REQ_FOLLOW, follow_payload(self._address, address_type))
        self.following = True
        self._on_event("sniffer", {
            "op": "following",
            "address_type": "random" if address_type else "public",
        })

    def _run(self) -> None:
        decoder = SlipDecoder()
        try:
            # Scan first: the advertiser's address type is in its own adverts,
            # and following with the wrong type follows nothing. Give up
            # waiting after 10 s and assume public.
            self._send(REQ_SCAN_CONT, b"\x00")
            self._send(SET_TEMPORARY_KEY, bytes(16))
            scan_deadline = time.monotonic() + 10
            while not self._stop.is_set():
                data = self._serial.read(self._serial.in_waiting or 1)
                if not self.following and time.monotonic() > scan_deadline:
                    self._follow(0)
                for frame in decoder.feed(data):
                    self._handle(frame)
        except Exception as err:  # noqa: BLE001 - the dongle was pulled, mostly
            if not self._stop.is_set():
                _LOGGER.warning("BLE sniffer stopped: %s", err)
                self._on_event("sniffer", {"op": "error", "message": str(err)})

    def _handle(self, frame: bytes) -> None:
        pkt = parse_packet(frame)
        if pkt is None:
            return
        if pkt.packet_id == EVENT_DISCONNECT:
            self._on_event("sniffer", {"op": "disconnect"})
            return
        if pkt.packet_id not in (EVENT_PACKET_ADV_PDU, EVENT_PACKET_DATA_PDU):
            return
        info = decode_ble(pkt)
        if not self.following:
            if info and info.get("op") == "advert" and info["advertiser"] == self._address:
                self._follow(info["address_type"])
            return
        self.packets += 1
        self._on_packet(pcap_record(pkt.raw, time.time()))
        if info is None or info["op"] == "advert":
            return
        if info["op"] == "connect" and info.get("advertiser") != self._address:
            return
        if info["op"] == "encrypted":
            # Every packet of an encrypted link says so; once is the news.
            if self._said_encrypted:
                return
            self._said_encrypted = True
        self._on_event("sniffer", {**info, "rssi": pkt.rssi})
