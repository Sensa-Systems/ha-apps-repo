"""Velit Rooftop Air Conditioner — BLE GATT client.

The Velit AC is controlled over a UART-over-BLE protocol the manufacturer
supplied. Unlike the BMS parsers (read-only), the AC is **command-driven**:
we query its state and write power / mode / temperature / fan / swing
commands.

  Service UUID   0000FFE0-0000-1000-8000-00805F9B34FB
  Write (cmd)    0000FFE2-…   APP → AC
  Notify (resp)  0000FFE1-…   AC → APP

  Frame    5A 5A | LEN | 01(product) | FUNC | DATA… | CHECKSUM | 0D 0A
  LEN       byte count the checksum covers — header(2)+len(1)+product(1)
            +func(1)+data(N) = 5 + N (spec example: 5A5A 06 01 01 01 BD 0D0A).
  CHECKSUM  low 8 bits of the sum of every byte from the first header byte
            through the last data byte.

Function codes:
  01 power      01 off, 02 on
  02 mode       00 query; 01 cool 02 heat 03 fan 04 eco 05 sleep
                06 turbo 07 dehumidify 08 vent
  03 temp       00 query; 01 −1, 02 +1; 0x10–0x1E absolute °C; 0x3D–0x56 °F
  04 fan speed  00 query; 01–05
  07 inlet temp 00 query (intake air temperature)
  10 swing      00 query; 01 start, 02 stop
  0b fault      00 query

**Cross-repo contract:** this frame codec MUST stay byte-for-byte identical
to the simulator's peripheral (`Sensa.Control.Simulator` →
`ha-addons/.../protocols/velit_ac.py`). Sim + integration ship in the same
release; `tests/test_velit_rooftop_ac.py` cross-checks the two.

The class mirrors the `aiobmsble.BMS` shape used by the other BLE vendors
(ctor takes a BLEDevice, `async_update()` returns a dict, `device_info()`
returns name/model, `disconnect()` cleans up) so the coordinator dispatches
to it the same way.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# GATT UUIDs (lowercase — bleak is case-sensitive on some backends).
VELIT_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
VELIT_WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"
VELIT_NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Framing constants.
_HEADER = 0x5A
_PRODUCT_AC = 0x01
_TRAILER = bytes([0x0D, 0x0A])

# Function codes.
FUNC_POWER = 0x01
FUNC_MODE = 0x02
FUNC_TEMP = 0x03
FUNC_FAN = 0x04
FUNC_INLET = 0x07
FUNC_SWING = 0x10
FUNC_FAULT = 0x0B

_QUERY = 0x00

# Mode name ⇄ wire code (matches the simulator + the manufacturer spec).
MODE_TO_CODE: dict[str, int] = {
    "cool": 0x01, "heat": 0x02, "fan": 0x03, "eco": 0x04,
    "sleep": 0x05, "turbo": 0x06, "dehumidify": 0x07, "vent": 0x08,
}
CODE_TO_MODE: dict[int, str] = {v: k for k, v in MODE_TO_CODE.items()}

# Absolute-temperature command ranges (the data byte IS the value).
_C_MIN, _C_MAX = 0x10, 0x1E   # 16–30 °C
_F_MIN, _F_MAX = 0x3D, 0x56   # 61–86 °F
SETPOINT_MIN_C, SETPOINT_MAX_C = 17, 30

_RESPONSE_TIMEOUT = 3.0       # spec: resend after 3 s with no response
_NOTIFY_SETTLE_SECS = 0.3     # let the CCCD enable register before writing


# ---------------------------------------------------------------------------
# Frame codec — identical to the simulator peripheral.
# ---------------------------------------------------------------------------


def _checksum(body: bytes) -> int:
    return sum(body) & 0xFF


def build_frame(func: int, data: bytes) -> bytes:
    """Wrap a command/query payload in the Velit frame envelope."""
    length = 5 + len(data)
    body = bytes([_HEADER, _HEADER, length, _PRODUCT_AC, func]) + data
    return body + bytes([_checksum(body)]) + _TRAILER


def parse_frame(raw: bytes) -> tuple[int, bytes] | None:
    """Decode a Velit frame; return (func, data) or None on framing failure."""
    if len(raw) < 8:
        return None
    if raw[0] != _HEADER or raw[1] != _HEADER:
        return None
    if raw[-2:] != _TRAILER:
        return None
    length = raw[2]
    n_data = length - 5
    if n_data < 0 or len(raw) != length + 3:
        return None
    payload = raw[5:5 + n_data]
    if _checksum(raw[:5 + n_data]) != raw[5 + n_data]:
        return None
    return raw[4], payload


def _c_to_f(celsius: float) -> int:
    return round(1.8 * celsius + 32)


def _f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32) / 1.8


def _decode_temp_byte(value: int) -> float:
    """Interpret a temperature response byte as °C.

    The device echoes temps in its active unit. The °C and °F realistic
    ranges don't overlap (setpoint 17–30 °C vs 61–86 °F; cabin air well
    under 45 °C), so a single threshold separates them cleanly without
    tracking unit state across a flaky BLE link.
    """
    return float(value) if value <= 45 else round(_f_to_c(value), 1)


class VelitAC:
    """BLE GATT client for one Velit Rooftop AC unit.

    State model mirrors the VIP BMS client: a persistent bleak client held
    across `async_update()` calls; the coordinator drops it on error.
    """

    def __init__(self, ble_device: Any) -> None:
        self._ble_device = ble_device
        self._client: Any | None = None
        self._latest_by_func: dict[int, bytes] = {}
        self._event = asyncio.Event()
        # Whether we've pinned the device to °C yet (the spec switches unit
        # when the APP writes a C value once — see async_update).
        self._unit_pinned_c = False

    # --- connection ------------------------------------------------------

    async def _ensure_client(self) -> Any:
        if self._client is not None and getattr(self._client, "is_connected", False):
            return self._client
        from bleak import BleakClient
        from bleak_retry_connector import establish_connection

        client = await establish_connection(
            BleakClient,
            self._ble_device,
            f"Velit AC {self._ble_device.address}",
            max_attempts=3,
        )
        await client.start_notify(VELIT_NOTIFY_UUID, self._on_notify)
        await asyncio.sleep(_NOTIFY_SETTLE_SECS)
        self._client = client
        self._unit_pinned_c = False
        return client

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        parsed = parse_frame(bytes(data))
        if parsed is None:
            return
        func, _payload = parsed
        self._latest_by_func[func] = bytes(data)
        self._event.set()

    async def _exchange(self, func: int, arg: int) -> bytes | None:
        """Write one frame, wait for the matching-func response payload."""
        client = await self._ensure_client()
        self._latest_by_func.pop(func, None)
        self._event.clear()
        await client.write_gatt_char(
            VELIT_WRITE_UUID, build_frame(func, bytes([arg])), response=False
        )
        try:
            await asyncio.wait_for(self._event.wait(), timeout=_RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.debug("Velit: no response to func 0x%02X within %.1fs",
                          func, _RESPONSE_TIMEOUT)
            return None
        raw = self._latest_by_func.get(func)
        if raw is None:
            return None
        parsed = parse_frame(raw)
        return parsed[1] if parsed else None

    # --- poll ------------------------------------------------------------

    async def async_update(self) -> dict[str, Any]:
        """One poll cycle — query every field, return a normalised dict.

        Keys (all optional, missing on a failed sub-query):
          power_on, mode, target_temp_c, fan_speed, swing_on,
          current_temp_c, fault_code
        """
        sample: dict[str, Any] = {}

        power = await self._exchange(FUNC_POWER, _QUERY)
        if power:
            sample["power_on"] = power[0] == 0x02

        mode = await self._exchange(FUNC_MODE, _QUERY)
        if mode:
            sample["mode"] = CODE_TO_MODE.get(mode[0])

        # Pin the device to Celsius once (the spec switches unit when the
        # APP writes a C value): query the setpoint, then write it back as
        # an absolute °C value so subsequent queries echo °C. The value is
        # unchanged — only the unit flag flips.
        temp = await self._exchange(FUNC_TEMP, _QUERY)
        if temp:
            target_c = _decode_temp_byte(temp[0])
            sample["target_temp_c"] = target_c
            if not self._unit_pinned_c:
                clamped = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, round(target_c)))
                await self._exchange(FUNC_TEMP, clamped)  # 0x10–0x1E → unit=C
                self._unit_pinned_c = True
                sample["target_temp_c"] = float(clamped)

        fan = await self._exchange(FUNC_FAN, _QUERY)
        if fan:
            sample["fan_speed"] = fan[0]

        inlet = await self._exchange(FUNC_INLET, _QUERY)
        if inlet:
            sample["current_temp_c"] = _decode_temp_byte(inlet[0])

        swing = await self._exchange(FUNC_SWING, _QUERY)
        if swing:
            sample["swing_on"] = swing[0] == 0x01

        fault = await self._exchange(FUNC_FAULT, _QUERY)
        if fault:
            sample["fault_code"] = fault[0]

        return sample

    # --- commands --------------------------------------------------------

    async def async_set_power(self, on: bool) -> None:
        await self._exchange(FUNC_POWER, 0x02 if on else 0x01)

    async def async_set_mode(self, mode: str) -> None:
        code = MODE_TO_CODE.get(mode)
        if code is None:
            raise ValueError(f"unknown velit mode: {mode!r}")
        await self._exchange(FUNC_MODE, code)

    async def async_set_temperature(self, celsius: float) -> None:
        value = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, round(celsius)))
        await self._exchange(FUNC_TEMP, value)  # 0x10–0x1E absolute °C
        self._unit_pinned_c = True

    async def async_set_fan_speed(self, speed: int) -> None:
        value = max(1, min(5, int(speed)))
        await self._exchange(FUNC_FAN, value)

    async def async_set_swing(self, on: bool) -> None:
        await self._exchange(FUNC_SWING, 0x01 if on else 0x02)

    # --- identity / teardown --------------------------------------------

    async def device_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"model": "Velit Rooftop AC"}
        name = getattr(self._ble_device, "name", None)
        if name:
            info["name"] = str(name)
        return info

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.stop_notify(VELIT_NOTIFY_UUID)
        except Exception:
            _LOGGER.debug("Velit: stop_notify failed", exc_info=True)
        try:
            await client.disconnect()
        except Exception:
            _LOGGER.debug("Velit: disconnect failed", exc_info=True)
