"""BLE Capture: record what a customer's phone app says to a Bluetooth device.

The customer turns debug on in Settings -> Discovery (which lists Bluetooth
devices we do not support), presses Start BLE Capture on the device, uses
the manufacturer's own app as normal (pressing a marker button for each thing
they do), presses Stop and downloads the bundle. The box's own Bluetooth
adapter cannot overhear a phone's connection; a Nordic nRF Sniffer dongle
posted by support can (see :mod:`.ble_sniffer`, the driver used here).

This module is the capture session and has no Home Assistant import, so it is
testable with a simulated driver. Everything in it is blocking (serial, files,
threads) and is called from an executor; :mod:`.ble_capture_ws` is the Home
Assistant side.

One session directory holds:

* ``capture.pcap``: every packet the dongle reported for the target, DLT 272
  (``nordic_ble``), which Wireshark decodes;
* ``events.jsonl``: one line per state change, decoded driver event and action
  marker, each with an epoch timestamp on the same clock as the pcap;
* ``markers.jsonl``: the action markers alone;
* ``metadata.json``: target, dongle, timings, counts, outcome and the
  limitations that apply to this capture.

Nothing here writes to the target or injects anything: the dongle only listens.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from .ble_sniffer import PCAP_GLOBAL_HEADER, Sniffer, find_sniffer_port

_LOGGER = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_WAITING = "waiting_for_target"
STATE_FOLLOWING = "following"
STATE_CONNECTED = "connected"
STATE_ENCRYPTED = "encrypted"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"
ACTIVE_STATES = frozenset({
    STATE_STARTING, STATE_WAITING, STATE_FOLLOWING, STATE_CONNECTED, STATE_ENCRYPTED,
})

# The action-marker buttons. A closed list on purpose: no typing on a van
# screen, and a fixed vocabulary reads the same across every bundle.
MARKERS: dict[str, str] = {
    "app_connected": "App connected",
    "turned_on": "Turned on",
    "turned_off": "Turned off",
    "speed_up": "Speed or level up",
    "speed_down": "Speed or level down",
    "mode_changed": "Mode changed",
    "setting_changed": "Setting changed",
    "reading_changed": "Reading changed on the device",
    "other": "Something else",
}

DEFAULT_MAX_SECONDS = 15 * 60
MAX_MAX_SECONDS = 60 * 60
MAX_PCAP_BYTES = 16 * 1024 * 1024
KEEP_CAPTURES = 5

PCAP_FILE = "capture.pcap"
EVENTS_FILE = "events.jsonl"
MARKERS_FILE = "markers.jsonl"
METADATA_FILE = "metadata.json"

_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_SESSION_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9A-F]{12}$")

# Stated in every bundle so whoever reads it does not over-trust it. The
# driver review behind them is in docs/decisions.md (2026-10-03).
LIMITATIONS = [
    (
        "The dongle follows one address. A device that advertises with a rotating "
        "private address cannot be followed once the address changes."
    ),
    (
        "The driver recognises legacy adverts only. A device that advertises only "
        "with extended advertising is not recognised from its advert; after 10 s "
        "the driver follows the address as a public one anyway, which follows "
        "nothing if the address is random."
    ),
    (
        "A connection is only captured if the dongle hears the phone's CONNECT_IND. "
        "If the app was already connected when capture started, disconnect it and "
        "connect again."
    ),
    (
        "Packets are recorded only from the moment the dongle starts following the "
        "target; adverts heard while it is still looking are not in the pcap."
    ),
    (
        "An encrypted link is readable only if the sniffer decrypts it: legacy "
        "Just Works pairing observed in full. LE Secure Connections, a passkey, or "
        "a bond made before the capture leave the payload encrypted. The pcap still "
        "holds timing, lengths and LL control traffic."
    ),
    (
        "Only ATT reads, writes, notifications and indications are decoded live. "
        "Everything else is in the pcap for Wireshark."
    ),
    (
        "The live 'link encrypted' notice comes from the LL encryption request. "
        "Undecodable packets are reported by the driver once per capture, not once "
        "per connection."
    ),
]


class CaptureError(Exception):
    """A capture could not start. The message is for the customer."""


class CaptureBusy(CaptureError):
    """Another session holds the dongle or the manager."""


class SnifferDriver(Protocol):
    version: str | None

    def open(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


DriverFactory = Callable[
    [str, str, Callable[[str, dict[str, Any]], None], Callable[[bytes], None]],
    SnifferDriver,
]

# Ports with a live session, across every manager in the process: two
# sessions on one dongle would fight over the serial line.
_PORTS_LOCK = threading.Lock()
_PORTS_IN_USE: set[str] = set()


def _claim_port(port: str) -> None:
    with _PORTS_LOCK:
        if port in _PORTS_IN_USE:
            raise CaptureBusy("The sniffer dongle is already recording.")
        _PORTS_IN_USE.add(port)


def _release_port(port: str) -> None:
    with _PORTS_LOCK:
        _PORTS_IN_USE.discard(port)


def normalise_address(address: str) -> str:
    """Upper-case colon MAC, or CaptureError."""
    addr = (address or "").strip().upper().replace("-", ":")
    if not _MAC_RE.match(addr):
        raise CaptureError(f"{address!r} is not a Bluetooth address.")
    return addr


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


class CaptureSession:
    """One capture of one target on one dongle. Thread-safe.

    The driver calls :meth:`_on_event` and :meth:`_on_packet` from its own
    thread; :meth:`start`, :meth:`stop` and :meth:`mark` come from an executor.
    ``on_change(status)`` is called after every state change, outside the lock.
    """

    def __init__(
        self,
        *,
        session_id: str,
        session_dir: str,
        port: str,
        address: str,
        name: str,
        driver_factory: DriverFactory,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        max_pcap_bytes: int = MAX_PCAP_BYTES,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session_id = session_id
        self.session_dir = session_dir
        self.port = port
        self.address = address
        self.name = name
        self._driver_factory = driver_factory
        self._max_seconds = max_seconds
        self._max_pcap_bytes = max_pcap_bytes
        self._on_change = on_change
        self._clock = clock
        self._lock = threading.RLock()
        self._driver: SnifferDriver | None = None
        self._timer: threading.Timer | None = None
        self._pcap: Any = None
        self._events: Any = None
        self._markers: Any = None
        self._port_claimed = False
        self._seq = 0
        self.state = STATE_IDLE
        self.detail = ""
        self.stop_reason: str | None = None
        self.started: float | None = None
        self.stopped: float | None = None
        self.firmware: str | None = None
        self.address_type: str | None = None
        self.packets = 0
        self.pcap_bytes = 0
        self.connections = 0
        self.att_events = 0
        self.marker_count = 0
        self.encryption_seen = False
        self.undecodable_seen = False
        self.att_after_encryption = False
        self._encrypted_link = False

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Claim the dongle, open the files and the driver. Blocking.

        Raises CaptureBusy or CaptureError; on any failure nothing is left
        open and the port is released.
        """
        try:
            _claim_port(self.port)
            self._port_claimed = True
            os.makedirs(self.session_dir, exist_ok=True)
            with self._lock:
                self.started = self._clock()
                self._pcap = open(os.path.join(self.session_dir, PCAP_FILE), "wb")  # noqa: SIM115
                self._pcap.write(PCAP_GLOBAL_HEADER)
                self.pcap_bytes = len(PCAP_GLOBAL_HEADER)
                self._events = open(  # noqa: SIM115
                    os.path.join(self.session_dir, EVENTS_FILE), "w", encoding="utf-8")
                self._markers = open(  # noqa: SIM115
                    os.path.join(self.session_dir, MARKERS_FILE), "w", encoding="utf-8")
            self._set_state(STATE_STARTING, "Opening the sniffer dongle")
            self._write_metadata()
            driver = self._driver_factory(self.port, self.address, self._on_event, self._on_packet)
            driver.open()
            self.firmware = getattr(driver, "version", None)
            with self._lock:
                stopped_meanwhile = self.stopped is not None
                if not stopped_meanwhile:
                    self._driver = driver
            if stopped_meanwhile:
                # Stop was pressed while the dongle was still opening.
                driver.stop()
                return
            self._log("info", message="Sniffer firmware answered", firmware=self.firmware)
            driver.start()
            self._set_state(STATE_WAITING, "Waiting to hear the device")
            timer = threading.Timer(self._max_seconds, self.stop, args=("timeout",))
            timer.daemon = True
            with self._lock:
                if self.stopped is None:
                    self._timer = timer
                    timer.start()
        except Exception as err:
            message = str(err) if isinstance(err, CaptureError | RuntimeError) else (
                f"The capture could not start ({err})."
            )
            self._fail(message)
            if isinstance(err, CaptureError):
                raise
            raise CaptureError(message) from err

    def stop(self, reason: str = "stopped") -> None:
        """Stop the driver, close the files, release the dongle. Idempotent."""
        with self._lock:
            if self.stopped is not None:
                return
            self.stopped = self._clock()
            self.stop_reason = reason
            driver, self._driver = self._driver, None
            timer, self._timer = self._timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        if driver is not None:
            try:
                driver.stop()
            except Exception:  # noqa: BLE001 - the dongle may already be gone
                _LOGGER.debug("BLE capture: driver stop raised", exc_info=True)
        # The serial line is closed: free the dongle before anyone is told
        # the capture stopped, so an immediate restart is not refused.
        if self._port_claimed:
            self._port_claimed = False
            _release_port(self.port)
        final = STATE_ERROR if self.state == STATE_ERROR else STATE_STOPPED
        detail = self.detail if final == STATE_ERROR else _STOP_DETAIL.get(reason, reason)
        self._set_state(final, detail, reason=reason)
        with self._lock:
            for handle in (self._pcap, self._events, self._markers):
                if handle is not None:
                    with contextlib.suppress(OSError):
                        handle.close()
            self._pcap = self._events = self._markers = None
        self._write_metadata()

    def _fail(self, message: str) -> None:
        self._set_state(STATE_ERROR, message)
        if self.started is None:
            # Failed before anything was opened (the dongle is busy): leave
            # no directory behind, there is nothing to download.
            with self._lock:
                self.stopped = self._clock()
                self.stop_reason = "error"
            if self._port_claimed:
                self._port_claimed = False
                _release_port(self.port)
            self._notify()
            return
        self.stop("error")

    def _stop_soon(self, reason: str) -> None:
        # Called from the driver's own thread, which driver.stop() joins: stop
        # from a helper thread or the join would wait on itself.
        threading.Thread(
            target=self.stop, args=(reason,), name="sensa-ble-capture-stop", daemon=True,
        ).start()

    @property
    def active(self) -> bool:
        return self.stopped is None and self.state in ACTIVE_STATES

    # -- markers ----------------------------------------------------------

    def mark(self, key: str) -> dict[str, Any]:
        if key not in MARKERS:
            raise CaptureError(f"Unknown marker {key!r}.")
        with self._lock:
            if not self.active:
                raise CaptureError("No capture is running.")
            self.marker_count += 1
            entry = self._log(
                "marker", marker=key, label=MARKERS[key], number=self.marker_count,
            )
            self._write_line(self._markers, entry)
        self._notify()
        return entry

    # -- driver callbacks (driver thread) ---------------------------------

    def _on_packet(self, record: bytes) -> None:
        first = False
        over = False
        with self._lock:
            if self._pcap is None:
                return
            if self.pcap_bytes + len(record) > self._max_pcap_bytes:
                over = True
            else:
                try:
                    self._pcap.write(record)
                except OSError as err:
                    self._log("error", message=f"Writing the pcap failed: {err}")
                    over = True
                else:
                    self.pcap_bytes += len(record)
                    self.packets += 1
                    first = self.state == STATE_WAITING
        if over:
            self._stop_soon("size_limit")
        elif first:
            self._set_state(STATE_FOLLOWING, "Following the device; connect the app now")

    def _on_event(self, _kind: str, data: dict[str, Any]) -> None:
        op = data.get("op")
        with self._lock:
            if self.stopped is not None:
                return
            self._log("sniffer", **data)
            if op in _ATT_OPS:
                self.att_events += 1
                if self._encrypted_link:
                    self.att_after_encryption = True
        if op == "following":
            self.address_type = data.get("address_type")
            self._notify()
        elif op == "connect":
            with self._lock:
                self.connections += 1
                self._encrypted_link = False
            self._set_state(STATE_CONNECTED, "The app is connected; do your actions now")
        elif op == "encryption_start":
            with self._lock:
                self.encryption_seen = True
                self._encrypted_link = True
            self._set_state(STATE_ENCRYPTED, "The link is encrypted")
        elif op == "encrypted":
            with self._lock:
                self.undecodable_seen = True
                self._encrypted_link = True
            self._set_state(
                STATE_ENCRYPTED,
                "The link is encrypted and the sniffer cannot read it. Keep going: "
                "timing and lengths are still recorded.",
            )
        elif op in ("terminate", "disconnect"):
            with self._lock:
                self._encrypted_link = False
            if self.state in (STATE_CONNECTED, STATE_ENCRYPTED):
                self._set_state(STATE_FOLLOWING, "The app disconnected; still following")
        elif op == "error":
            self._set_state(
                STATE_ERROR,
                f"The sniffer dongle stopped ({data.get('message') or 'no reason given'}). "
                "Check it is still plugged in. Everything up to now is saved.",
            )
            self._stop_soon("dongle_error")
        else:
            self._notify()

    # -- output -----------------------------------------------------------

    def _log(self, kind: str, **data: Any) -> dict[str, Any]:
        """Append one events.jsonl line. Caller may hold the lock."""
        with self._lock:
            now = self._clock()
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": round(now, 6),
                "time": _iso(now),
                "t": round(now - (self.started or now), 3),
                "kind": kind,
                "packets": self.packets,
                **data,
            }
            self._write_line(self._events, entry)
            return entry

    def _write_line(self, handle: Any, entry: dict[str, Any]) -> None:
        if handle is None:
            return
        try:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
            handle.flush()
        except OSError:
            _LOGGER.warning("BLE capture: could not write %s", getattr(handle, "name", "?"))

    def _set_state(self, state: str, detail: str, **extra: Any) -> None:
        with self._lock:
            if self.stopped is not None and state in ACTIVE_STATES:
                # A late driver event or the tail of start() racing a stop:
                # a finished capture never goes back to live.
                return
            changed = (state, detail) != (self.state, self.detail)
            self.state, self.detail = state, detail
            if changed:
                self._log("state", state=state, detail=detail, **extra)
        if changed:
            self._notify()

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(self.status())
        except Exception:  # noqa: BLE001 - a listener must never kill the capture
            _LOGGER.debug("BLE capture: on_change raised", exc_info=True)

    def _write_metadata(self) -> None:
        meta = {**self.status(), "limitations": LIMITATIONS, "files": {
            "pcap": PCAP_FILE, "events": EVENTS_FILE, "markers": MARKERS_FILE,
        }, "pcap_linktype": "272 (nordic_ble)", "clock": (
            "pcap timestamps, events.jsonl `ts` and markers share the box's "
            "wall clock (epoch seconds, UTC)"
        )}
        path = os.path.join(self.session_dir, METADATA_FILE)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp, path)
        except OSError:
            _LOGGER.warning("BLE capture: could not write %s", path)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "state": self.state,
                "detail": self.detail,
                "address": self.address,
                "name": self.name,
                "port": self.port,
                "firmware": self.firmware,
                "address_type": self.address_type,
                "started": self.started,
                "stopped": self.stopped,
                "stop_reason": self.stop_reason,
                "max_seconds": self._max_seconds,
                "packets": self.packets,
                "pcap_bytes": self.pcap_bytes,
                "connections": self.connections,
                "att_events": self.att_events,
                "markers": self.marker_count,
                "encryption_seen": self.encryption_seen,
                "undecodable_seen": self.undecodable_seen,
                "readable": not self.encryption_seen or self.att_after_encryption,
            }


_ATT_OPS = frozenset({
    "read_req", "read_rsp", "write_req", "write_rsp", "prepare_write",
    "notify", "indicate", "write_cmd",
})

_STOP_DETAIL = {
    "stopped": "Stopped",
    "timeout": "Stopped automatically: the time limit was reached",
    "size_limit": "Stopped automatically: the capture reached its size limit",
    "shutdown": "Stopped because Home Assistant is shutting down",
}


class CaptureManager:
    """At most one session per manager, and per dongle. Blocking API."""

    def __init__(
        self,
        base_dir: str,
        *,
        driver_factory: DriverFactory = Sniffer,
        port_finder: Callable[[], tuple[str | None, str | None]] = find_sniffer_port,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        keep: int = KEEP_CAPTURES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_dir = base_dir
        self._driver_factory = driver_factory
        self._port_finder = port_finder
        self._on_change = on_change
        self._keep = keep
        self._clock = clock
        self._lock = threading.Lock()
        self.session: CaptureSession | None = None

    def dongle(self) -> dict[str, Any]:
        port, problem = self._port_finder()
        return {"port": port, "problem": problem}

    def start(
        self, address: str, name: str = "", max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> dict[str, Any]:
        addr = normalise_address(address)
        max_seconds = max(60.0, min(float(max_seconds), MAX_MAX_SECONDS))
        with self._lock:
            if self.session is not None and self.session.active:
                raise CaptureBusy("A capture is already running. Stop it first.")
            port, problem = self._port_finder()
            if port is None:
                raise CaptureError(problem or "No sniffer dongle found.")
            now = self._clock()
            stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            session_id = f"{stamp}-{addr.replace(':', '')}"
            self._prune(self._keep - 1)
            session = CaptureSession(
                session_id=session_id,
                session_dir=os.path.join(self.base_dir, session_id),
                port=port,
                address=addr,
                name=(name or "").strip()[:80],
                driver_factory=self._driver_factory,
                max_seconds=max_seconds,
                on_change=self._on_change,
                clock=self._clock,
            )
            self.session = session
        session.start()
        return session.status()

    def stop(self, reason: str = "stopped") -> dict[str, Any] | None:
        session = self.session
        if session is None:
            return None
        session.stop(reason)
        return session.status()

    def mark(self, key: str) -> dict[str, Any]:
        session = self.session
        if session is None:
            raise CaptureError("No capture is running.")
        return session.mark(key)

    def status(self) -> dict[str, Any]:
        return {
            "session": self.session.status() if self.session is not None else None,
            "captures": self.captures(),
            "markers": [{"key": k, "label": v} for k, v in MARKERS.items()],
        }

    def captures(self) -> list[dict[str, Any]]:
        """Finished captures on disk, newest first."""
        out: list[dict[str, Any]] = []
        try:
            names = os.listdir(self.base_dir)
        except OSError:
            return out
        active = self.session.session_id if self.session and self.session.active else None
        for sid in sorted(names, reverse=True):
            if not _SESSION_ID_RE.match(sid) or sid == active:
                continue
            try:
                with open(os.path.join(self.base_dir, sid, METADATA_FILE), encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = {}
            out.append({
                "session_id": sid,
                "name": meta.get("name") or "",
                "address": meta.get("address") or "",
                "started": meta.get("started"),
                "stopped": meta.get("stopped"),
                "state": meta.get("state"),
                "packets": meta.get("packets", 0),
                "markers": meta.get("markers", 0),
                "readable": meta.get("readable", True),
            })
        return out

    def bundle(self, session_id: str) -> tuple[str, bytes]:
        """(filename, zip bytes) for one finished capture."""
        if not _SESSION_ID_RE.match(session_id or ""):
            raise CaptureError("Unknown capture.")
        if self.session is not None and self.session.active and \
                self.session.session_id == session_id:
            raise CaptureError("Stop the capture before downloading it.")
        session_dir = os.path.join(self.base_dir, session_id)
        if not os.path.isdir(session_dir):
            raise CaptureError("That capture is no longer on the box.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in (METADATA_FILE, EVENTS_FILE, MARKERS_FILE, PCAP_FILE):
                path = os.path.join(session_dir, fname)
                if os.path.isfile(path):
                    zf.write(path, f"{session_id}/{fname}")
            zf.writestr(f"{session_id}/README.txt", _readme(session_dir))
        return f"sensa-ble-capture-{session_id}.zip", buf.getvalue()

    def _prune(self, keep: int) -> None:
        try:
            names = sorted(
                (n for n in os.listdir(self.base_dir) if _SESSION_ID_RE.match(n)),
                reverse=True,
            )
        except OSError:
            return
        for old in names[max(keep, 0):]:
            shutil.rmtree(os.path.join(self.base_dir, old), ignore_errors=True)


def _readme(session_dir: str) -> str:
    try:
        with open(os.path.join(session_dir, METADATA_FILE), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    lines = [
        "Sensa BLE capture",
        "",
        f"Device: {meta.get('name') or '(unnamed)'} {meta.get('address') or ''}",
        f"Outcome: {meta.get('state')} ({meta.get('detail') or ''})",
        (
            f"Packets: {meta.get('packets', 0)}, connections seen: "
            f"{meta.get('connections', 0)}, markers: {meta.get('markers', 0)}"
        ),
    ]
    if meta.get("encryption_seen") and not meta.get("readable", True):
        lines += ["", (
            "The link was encrypted and the sniffer could not decrypt it. "
            "Payloads are not readable; timing, lengths and markers are."
        )]
    lines += [
        "",
        (
            "Files: capture.pcap (Wireshark, nordic_ble), events.jsonl (state, decoded "
            "ATT, markers), markers.jsonl, metadata.json. All timestamps share the box's "
            "clock."
        ),
        "",
        "Limitations:",
        *(f"- {item}" for item in LIMITATIONS),
        "",
    ]
    return "\n".join(lines)
