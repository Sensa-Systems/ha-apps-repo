"""Starlink gRPC client.

Communicates with a Starlink dish at 192.168.100.1:9200 via the gRPC
Device.Handle RPC. Uses two API layers:

- Official device API (get_diagnostics): alerts, self-test, stow, alignment.
  Stable and documented by SpaceX.
- Consumer API (get_status / get_history / get_device_info / dish config).
  Undocumented but widely used by the community. Treated as optional --
  if it fails the integration degrades gracefully.

No authentication required -- the dish serves an insecure gRPC channel on
the local network.

**Field numbers are checked against the reflection dump of firmware
`2026.07.24.mr83021`** (clarkzjw/starlink-grpc-golang `proto/spacex_api/device/`).
When a firmware bump moves something, re-read that proto rather than
guessing: several fields below moved or were reserved between
`2026.04.10.mr77882.1` (the previous reference) and now, and a wrong field
number decodes silently rather than raising.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

import grpc

_LOGGER = logging.getLogger(__name__)

# gRPC service path
_HANDLE_METHOD = "/SpaceX.API.Device.Device/Handle"


class StarlinkError(Exception):
    """Base exception for Starlink client errors."""


class StarlinkConnectionError(StarlinkError):
    """Connection to the dish failed."""


class StarlinkRefusedError(StarlinkError):
    """The dish understood the call and refused it (permission / policy).

    Distinct from a transport failure so callers can tell "this dish will
    never answer that" (GPS on a non-Priority plan, a write the firmware
    rejects) from "try again in 30 seconds".
    """


# -- Protobuf wire format helpers ---------------------------------------------
# Lightweight encoder/decoder for the known message structures. Avoids the
# need to compile .proto files while still supporting the full wire format.


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    parts = []
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value & 0x7F)
    return bytes(parts)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint starting at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, pos


def _tag(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _varint_field(field_number: int, value: int | bool) -> bytes:
    """Encode a varint-typed field (bool / uint32 / enum)."""
    return _tag(field_number, 0) + _encode_varint(int(value))


def _len_field(field_number: int, payload: bytes) -> bytes:
    """Encode a length-delimited field (embedded message / string / bytes)."""
    return _tag(field_number, 2) + _encode_varint(len(payload)) + payload


def _encode_request(field_number: int, payload: bytes = b"") -> bytes:
    """Encode a Request with a sub-message at the given oneof field.

    An empty payload is the common case (`get_status`, `get_history`, …):
    the oneof member still has to be PRESENT on the wire for the dish to
    dispatch, which is why this writes a zero-length embedded message
    rather than nothing at all.
    """
    return _len_field(field_number, payload)


def _decode_fields(data: bytes) -> dict[int, list[Any]]:
    """Decode a protobuf message into ``{field_number: [value, …]}``.

    Every occurrence is kept, because a repeated field (``DishGetHistory
    .outages``) carries one wire entry per element and a last-wins decode
    would silently return only the final one. :func:`_decode_message` is
    the last-wins view over this same parser -- ONE parser, two views, so
    the two cannot drift.
    """
    fields: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            value, pos = _decode_varint(data, pos)
        elif wire_type == 1:  # 64-bit (double, fixed64)
            value = struct.unpack("<d", data[pos : pos + 8])[0]
            pos += 8
        elif wire_type == 2:  # length-delimited (string, bytes, embedded msg)
            length, pos = _decode_varint(data, pos)
            value = data[pos : pos + length]
            pos += length
        elif wire_type == 5:  # 32-bit (float, fixed32)
            value = struct.unpack("<f", data[pos : pos + 4])[0]
            pos += 4
        else:
            # Wire type 3/4 are deprecated protobuf groups; anything
            # else means we mis-decoded a non-protobuf byte run (e.g.
            # packed fixed32 array bytes mistakenly recursed into).
            # Bail silently -- the noise hides real errors.
            _LOGGER.debug("Unknown wire type %d at field %d", wire_type, field_number)
            break

        fields.setdefault(field_number, []).append(value)

    return fields


def _decode_message(data: bytes) -> dict[int, Any]:
    """Last-wins view of :func:`_decode_fields`, for singular fields."""
    return {number: values[-1] for number, values in _decode_fields(data).items()}


def _decode_nested(fields: dict[int, Any], field_number: int) -> dict[int, Any]:
    """Decode a nested message from raw bytes at the given field number."""
    raw = fields.get(field_number)
    if isinstance(raw, bytes) and raw:
        return _decode_message(raw)
    return {}


def _decode_string(fields: dict[int, Any], field_number: int) -> str:
    """Decode a string field."""
    raw = fields.get(field_number)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return ""


def _decode_bool(fields: dict[int, Any], field_number: int) -> bool:
    """Decode a bool field (varint 0/1)."""
    return bool(fields.get(field_number, 0))


def _set_if_present(data: dict[str, Any], key: str, value: Any) -> None:
    """Store ``value`` under ``key`` only when the dish actually reported it.

    Writing ``None`` would defeat the entity layer's presence gating, which
    asks "is this key in the sample?" -- a key present with a None value
    reads as "this hardware exists and is saying nothing", which is exactly
    the permanently-Unknown row the gating exists to prevent.
    """
    if value is not None:
        data[key] = value


def _enum_name(names: dict[int, str], value: Any) -> str | None:
    """Map an enum varint to its display name, or ``Code N`` when unmapped.

    Unmapped is deliberately visible rather than swallowed: SpaceX adds
    enum members between firmware revisions, and "Code 17" tells support
    exactly what to look up while ``None`` would read as "the dish did not
    say".
    """
    if not isinstance(value, int):
        return None
    return names.get(value, f"Code {value}")


def _decode_packed_fixed32(raw: bytes) -> list[float]:
    """Decode a packed-repeated fixed32 field as a list of float32s.

    DishGetHistory uses packed encoding for its rolling sample arrays
    (power_in, latency, drop rate, …) -- wire type 2 (length-delimited)
    where the bytes are simply N x 4 little-endian float32 values
    concatenated together.
    """
    floats: list[float] = []
    for offset in range(0, len(raw) - 3, 4):
        floats.append(struct.unpack("<f", raw[offset : offset + 4])[0])
    return floats


# -- Enum name maps -----------------------------------------------------------
# All transcribed from the firmware 2026.07.24.mr83021 protos. Keep the
# numeric keys; the dish sends the number and the name is ours.

# SpaceX.API.Satellites.Network.UtDisablementCode -- carried BOTH on
# DishGetStatusResponse.disablement_code (1024) and, with an identical set of
# members, on DishGetDiagnosticsResponse.disablement_code (6). One map, so the
# live value and the diagnostics value can never disagree about what code 10
# means. Every member below exists in the current proto; a roaming van
# realistically reaches ROAM_RESTRICTED, MOVING_TOO_FAST_FOR_POLICY and
# BLOCKED_AREA, none of which used to be mapped.
DISABLEMENT_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "OK",
    2: "No Active Account",
    3: "Too Far From Service Address",
    4: "In Ocean",
    6: "Blocked Country",
    7: "Data Overage",
    8: "Cell Disabled",
    10: "Roaming Restricted",
    11: "Unknown Location",
    12: "Account Disabled",
    13: "Unsupported Version",
    14: "Moving Too Fast",
    15: "Aviation Flyover Limits",
    16: "Blocked Area",
}

# DishOutage.Cause. The customer-facing wording deliberately follows the
# Starlink app rather than the enum spelling (NO_SCHEDULE reads as
# "Searching", ACTUATOR_ACTIVITY as "Aligning") -- the numbers are the
# contract, the words are ours.
OUTAGE_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "Booting",
    2: "Stowed",
    3: "Thermal Shutdown",
    4: "Searching",
    5: "No Satellites",
    6: "Obstructed",
    7: "No Downlink",
    8: "No Pings",
    9: "Aligning",
    10: "Cable Test",
    11: "Sleeping",
    13: "Sky Search",
    14: "RF Inhibited",
}

# HasActuators. NOT "NONE/MOTORS/TILT" -- that was a wrong guess carried in a
# comment here for months. A Mini (electronically steered) reports 2 = NO.
HAS_ACTUATORS_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "Yes",
    2: "No",
}

# UserMobilityClass
MOBILITY_NAMES: dict[int, str] = {0: "Stationary", 1: "Nomadic", 2: "Mobile"}

# UserClassOfService
SERVICE_CLASS_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "Consumer",
    2: "Business",
    3: "Business Plus",
    4: "Commercial Aviation",
}

# SoftwareUpdateState
UPDATE_STATE_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "Idle",
    2: "Fetching",
    3: "Pre-check",
    4: "Writing",
    5: "Post-check",
    6: "Reboot Required",
    7: "Disabled",
    8: "Faulted",
}

# SpaceX.API.Telemetron.Public.Integrations.RateLimitReason -- why the link is
# throttled. LOW_SPEED_POLICY_LIMIT arrived in firmware 2025.12.16.
RATE_LIMIT_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "No Limit",
    2: "Policy Limit",
    3: "Custom Limit",
    5: "Data Overage",
    6: "Low-Speed Policy",
}

# PowerSource (DishBatteryStats.power_source), new in firmware 2026.05.07.
POWER_SOURCE_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "USB-C",
    2: "Battery",
    3: "USB-C + Battery",
}

# ActuatorState (AlignmentStats.actuator_state)
ACTUATOR_STATE_NAMES: dict[int, str] = {
    0: "Idle",
    1: "Full Tilt",
    2: "Rotating",
    3: "Tilting",
    4: "Unwrapping",
    5: "Unwrapping",
    6: "Tilting To Stowed",
    7: "Faulted",
    8: "Waiting To Settle",
    9: "Driving To Mobile Position",
    10: "Mobile Wait",
}

# AttitudeEstimationState -- also DishGpsStats.pnt_filter_convergence_state.
ATTITUDE_STATE_NAMES: dict[int, str] = {
    0: "Reset",
    1: "Unconverged",
    2: "Converged",
    3: "Faulted",
    4: "Invalid",
}

# RebootReason
REBOOT_REASON_NAMES: dict[int, str] = {
    0: "None",
    1: "Manual",
    2: "Loss Of Comms",
    3: "Software Update",
    4: "Scheduled Update",
    5: "Application",
    6: "EMC Test",
    7: "Factory Reset",
    8: "Test Case",
    9: "Thermal Power Cut",
    10: "Critical Process Died",
    11: "No RF Ready",
    12: "Postponed Loss Of Comms",
    13: "Stationary Update",
    14: "AAP Crash",
    15: "XP70 SACS",
    16: "INE Failed",
    17: "Kernel Tainted",
}

# DishGetDiagnosticsResponse.TestResult (field 7). This field used to be
# decoded here as the `stowed` bool, which it is not -- stowed is field 10.
SELF_TEST_NAMES: dict[int, str] = {0: "Not Run", 1: "Passed", 2: "Failed"}

# DishConfig.SnowMeltMode / LevelDishMode. Decode only: the firmware refuses
# `dish_set_config` (see `async_set_power_save`), so these settings are
# readable and not writable. The reverse maps that encoded a chosen option
# went with the controls.
SNOW_MELT_NAMES: dict[int, str] = {0: "Auto", 1: "Always On", 2: "Always Off"}

LEVEL_DISH_NAMES: dict[int, str] = {0: "Follow Tilt", 1: "Force Level"}


# -- Pre-encoded requests ----------------------------------------------------

# Request.reboot (field 1001)
_REBOOT_REQUEST = _encode_request(1001)

# Request.get_status (field 1004) -- consumer API
_GET_STATUS_REQUEST = _encode_request(1004)

# Request.get_device_info (field 1008) -- consumer API
_GET_DEVICE_INFO_REQUEST = _encode_request(1008)

# Request.get_history (field 1007) -- consumer API. The Mini removed
# `power_in_w` from the status response, so dish power has to come
# from the rolling history sample array (`DishGetHistory.power_in`,
# field 1010, packed-repeated fixed32) -- confirmed by Sparky's
# starlink-grpc-tools as the only authoritative live-power source on
# current Mini firmware.
_GET_HISTORY_REQUEST = _encode_request(1007)

# Request.get_diagnostics (field 6000) -- official device API
_GET_DIAGNOSTICS_REQUEST = _encode_request(6000)

# Request.get_location (field 1017) -- consumer API
_GET_LOCATION_REQUEST = _encode_request(1017)

# Request.dish_get_config (field 2011) -- consumer API. Only needed as a
# FALLBACK: `DishGetStatusResponse.config` (field 2000) carries the same
# DishConfig on current firmware, so the normal 30s poll already has it and
# this call is reserved for the read-back after a write.
_GET_CONFIG_REQUEST = _encode_request(2011)


class StarlinkGrpcClient:
    """Async gRPC client for a Starlink dish."""

    def __init__(self, host: str = "192.168.100.1", port: int = 9200) -> None:
        self._target = f"{host}:{port}"
        self._channel: grpc.aio.Channel | None = None
        self._diag_failing = False

    async def _ensure_channel(self) -> grpc.aio.Channel:
        """Create or return the gRPC channel."""
        if self._channel is None:
            self._channel = grpc.aio.insecure_channel(self._target)
        return self._channel

    async def async_close(self) -> None:
        """Close the gRPC channel."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    async def _async_call(self, request_bytes: bytes) -> bytes:
        """Make a raw unary call to Device.Handle."""
        channel = await self._ensure_channel()
        call = channel.unary_unary(
            _HANDLE_METHOD,
            request_serializer=lambda x: x,
            response_deserializer=lambda x: x,
        )
        try:
            response = await asyncio.wait_for(call(request_bytes), timeout=10)
            return response
        except grpc.RpcError as err:
            code = err.code() if hasattr(err, "code") else None
            if code == grpc.StatusCode.UNAVAILABLE:
                raise StarlinkConnectionError(
                    f"Dish unavailable at {self._target}"
                ) from err
            if code == grpc.StatusCode.UNIMPLEMENTED:
                _LOGGER.debug("RPC not implemented (firmware too old?)")
                return b""
            if code in (
                grpc.StatusCode.PERMISSION_DENIED,
                grpc.StatusCode.UNAUTHENTICATED,
                grpc.StatusCode.FAILED_PRECONDITION,
            ):
                # A refusal, not a fault. `get_location` answers this way on
                # every non-Priority plan since May 2026, and a write the
                # firmware will not accept answers the same way -- neither is
                # worth a retry or a warning.
                raise StarlinkRefusedError(f"Dish refused the call: {err}") from err
            raise StarlinkError(f"gRPC error: {err}") from err
        except asyncio.TimeoutError as err:
            raise StarlinkConnectionError(
                f"Dish timed out at {self._target}"
            ) from err

    # -- Public data fetchers -------------------------------------------------

    async def async_get_starlink_data(self) -> dict[str, Any]:
        """Fetch all dish data. Consumer API failures degrade gracefully."""
        data: dict[str, Any] = {}

        # Official device API -- reliable. This runs every 30s, so a dish that
        # is stowed, asleep or unplugged would otherwise log a bare warning on
        # every poll (600+ a day on one van). Warn once with the reason, then
        # stay at debug until a fetch succeeds again.
        try:
            diag = await self._async_fetch_diagnostics()
            data.update(diag)
            if self._diag_failing:
                _LOGGER.info("Starlink diagnostics fetch recovered")
                self._diag_failing = False
        except StarlinkError as err:
            if self._diag_failing or isinstance(err, StarlinkRefusedError):
                _LOGGER.debug("Starlink diagnostics fetch failed: %s", err)
            else:
                _LOGGER.warning(
                    "Starlink diagnostics fetch failed: %s (further failures "
                    "are logged at debug until it recovers)", err,
                )
            self._diag_failing = True

        # Consumer API -- best-effort
        try:
            status = await self._async_fetch_status()
            data.update(status)
        except StarlinkError:
            _LOGGER.debug("Starlink consumer status fetch failed (expected if firmware changed)")

        try:
            info = await self._async_fetch_device_info()
            data.update(info)
        except StarlinkError:
            _LOGGER.debug("Starlink device info fetch failed")

        # History (power_in_w, pop_ping_drop_rate) is fetched by a
        # dedicated faster coordinator at 15s -- see
        # `async_get_starlink_history` below -- so it is intentionally
        # skipped here. Including it again at 30s would double the
        # gRPC traffic for the same data.

        return data

    async def async_get_starlink_history(self) -> dict[str, Any]:
        """Public wrapper around the history fetch -- bound to its own
        15s DataUpdateCoordinator so dish power refreshes twice as
        often as the rest of the sensor set without doubling the rest
        of the API traffic."""
        try:
            return await self._async_fetch_history()
        except StarlinkError:
            _LOGGER.debug("Starlink history fetch failed")
            return {}

    async def async_get_location(self) -> dict[str, Any]:
        """Fetch GPS location (separate call).

        **Expected to be refused on most plans.** SpaceX disabled local-API
        location from 20 May 2026 for everything but Priority, and the dish
        answers `PermissionDenied: Disabled due to policy` -- surfaced here
        as :class:`StarlinkRefusedError` and swallowed, because it is a
        subscription fact and not a fault. The satellite COUNT
        (`gps_satellites`, from `DishGetStatusResponse.gps_stats`) is not
        policy-gated and is what the dashboard uses instead.
        """
        try:
            raw = await self._async_call(_GET_LOCATION_REQUEST)
        except StarlinkRefusedError:
            _LOGGER.debug(
                "Starlink location: refused by policy (expected off Priority plans)"
            )
            return {}
        except StarlinkError:
            _LOGGER.debug("Starlink location: request failed")
            return {}

        if not raw:
            _LOGGER.debug("Starlink location: empty response")
            return {}

        response = _decode_message(raw)

        # Response field 1017 = GetLocationResponse
        location_bytes = response.get(1017)
        if not isinstance(location_bytes, bytes):
            return {}

        location = _decode_message(location_bytes)

        # GetLocationResponse: 1=LLAPosition, 3=source, 4=sigma_m,
        # 5=horizontal_speed_mps, 6=vertical_speed_mps (the speeds arrived in
        # firmware 2025.06.24).
        lla = _decode_nested(location, 1)
        if not lla:
            return {}

        out: dict[str, Any] = {
            "gps_latitude": lla.get(1),
            "gps_longitude": lla.get(2),
            "gps_altitude": lla.get(3),
        }
        if 4 in location:
            out["gps_sigma_m"] = location.get(4)
        if 5 in location:
            out["gps_horizontal_speed_mps"] = location.get(5)
        if 6 in location:
            out["gps_vertical_speed_mps"] = location.get(6)
        return out

    # -- Configuration read / write -------------------------------------------

    async def async_get_dish_config(self) -> dict[str, Any]:
        """Read DishConfig via `dish_get_config` (Request field 2011).

        The 30s status poll already carries the same message
        (`DishGetStatusResponse.config`, field 2000), so this exists for the
        read-back straight after a write, where waiting up to 30s for the
        next poll would leave the control showing the old value.
        """
        raw = await self._async_call(_GET_CONFIG_REQUEST)
        if not raw:
            return {}
        response = _decode_message(raw)
        # Response.dish_get_config = 2011 -> DishGetConfigResponse.dish_config = 1
        config_bytes = response.get(2011)
        if not isinstance(config_bytes, bytes):
            return {}
        inner = _decode_message(config_bytes)
        return _decode_dish_config(_decode_nested(inner, 1))

    async def async_set_power_save(
        self, *, start_minutes: int, duration_minutes: int, enable: bool
    ) -> dict[str, Any]:
        """Write the sleep schedule via `dish_power_save` (Request field 2013).

        **Not `dish_set_config` (2010).** Current firmware refuses that RPC
        outright for a client on the LAN -- a bare `PERMISSION_DENIED`, raised
        before the payload is looked at (an EMPTY DishConfig is refused
        identically). The privileged path is the mutual-TLS listener on port
        9202, which presents a per-terminal certificate from `UT Catapult CA
        v1` and wants a client certificate only SpaceX can issue. So every
        setting that exists ONLY in DishConfig -- snow melt, levelling, the
        update deferral and reboot hour -- is unreachable from the van, and
        the controls for them were removed rather than left to fail.

        The sleep schedule survives because it has its own RPC, and the dish
        still accepts that one. Proven against firmware
        `2026.07.29.cr83366.1` on 2026-08-10: `dish_set_config` refused,
        `dish_power_save` written and read back.

        Two consequences of using this RPC, both load-bearing:

        - It carries **all three** values at once, so a caller changing one
          must send the current other two or it silently resets them.
        - `duration_minutes` must be at least 1. The dish answers 0 with
          `INVALID_ARGUMENT: Invalid duration provided`, even when the
          schedule is being switched OFF, so it is clamped here.

        The reply carries no config echo, so the caller re-reads
        :meth:`async_get_dish_config`; that read is returned here. A failure
        of that READ is not a failure of the WRITE, so it returns `{}` rather
        than raising -- the write has already landed, and raising would tell
        the customer their change was rejected when it was not. The empty
        return is the caller's signal to refresh the normal way.
        """
        payload = (
            _varint_field(1, max(0, int(start_minutes)))
            + _varint_field(2, max(1, int(duration_minutes)))
            + _varint_field(3, int(bool(enable)))
        )
        await self._async_call(_encode_request(2013, payload))
        # DishPowerSaveResponse is empty, so the only way to show the customer
        # what actually landed is to ask.
        try:
            return await self.async_get_dish_config()
        except StarlinkError:
            _LOGGER.debug("Starlink: sleep schedule written, read-back failed")
            return {}

    # -- Commands -------------------------------------------------------------

    async def async_stow(self, unstow: bool = False) -> None:
        """Stow (or unstow) an actuated dish. `DishStowRequest.unstow = 1`.

        A no-op on a Mini, which has no actuators -- callers gate on
        `has_actuators` rather than relying on the dish to complain.
        """
        request = _encode_request(2002, _varint_field(1, unstow) if unstow else b"")
        await self._async_call(request)

    async def async_reboot(self) -> None:
        """Reboot the dish. `RebootRequest` is empty.

        The dish drops the connection while rebooting, so the coordinator's
        next poll fails and recovers on its own; nothing here waits for it.
        """
        await self._async_call(_REBOOT_REQUEST)

    # -- Internal fetchers ----------------------------------------------------

    async def _async_fetch_status(self) -> dict[str, Any]:
        """Fetch dish status via the consumer API (get_status).

        Field numbers reflect firmware 2026.07.24.mr83021:
          - State moved from `state` (1006, reserved) to `outage.cause`
            (status.outage at 1014, .cause at 1).
          - Power lives on `aps_stats.dish_power` (status 1048, .6) for
            APS-equipped terminals (Mini), with `upsu_stats.dish_power`
            (status 1043, .6) as the fallback for older actuated dishes.
          - SNR float at 1001 was reserved; replaced by booleans at 1018
            (`is_snr_above_noise_floor`) and 1022 (`is_snr_persistently_low`).
          - `has_actuators` at 1023 is HasActuators (UNKNOWN/YES/NO), which
            lets callers suppress motor-related controls on a Mini.
          - `config` at 2000 embeds the whole DishConfig, so the sleep
            schedule / snow melt / levelling settings ride the poll we
            already make.
        """
        raw = await self._async_call(_GET_STATUS_REQUEST)
        if not raw:
            _LOGGER.info("Starlink get_status: empty response")
            return {}

        response = _decode_message(raw)
        _LOGGER.debug("Starlink get_status response fields: %s", list(response.keys()))

        # Response field 2004 = DishGetStatusResponse on current firmware.
        # Older firmware put the status reply at field 1004 -- fall back to
        # it but only if 2004 is missing, since on new firmware field 1004
        # in a Response is `get_device_info` and parsing those bytes as
        # status would silently zero everything out.
        status_bytes = response.get(2004) or response.get(1004)
        if not isinstance(status_bytes, bytes):
            _LOGGER.info(
                "Starlink get_status: no status field found, available: %s",
                {k: type(v).__name__ for k, v in response.items()},
            )
            return {}

        status = _decode_message(status_bytes)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Starlink status fields: %s",
                {
                    k: (v if isinstance(v, (int, float)) else f"<bytes:{len(v)}>")
                    for k, v in status.items()
                },
            )

            # Recursive sub-message field map. This is the tool that finds
            # where a field WENT when a firmware bump moves it -- the exact
            # job that had to be done by hand to map power to
            # DishGetHistory.power_in, and the one this whole module needs
            # again at the next bump. Debug-gated, and it deliberately skips
            # long byte runs so a packed-fixed32 sample array is not
            # mis-recursed into as a message (which used to produce
            # "Unknown wire type 4" noise).
            def _dump_msg(prefix: str, raw: bytes, depth: int = 0) -> None:
                if depth > 2 or not raw:
                    return
                try:
                    msg = _decode_message(raw)
                except Exception:  # noqa: BLE001
                    return
                if not msg:
                    return
                scalars = {
                    k: (v if isinstance(v, (int, float)) else f"<bytes:{len(v)}>")
                    for k, v in msg.items()
                }
                _LOGGER.debug("%s (%dB): %s", prefix, len(raw), scalars)
                for k, v in msg.items():
                    if isinstance(v, bytes) and 0 < len(v) <= 64:
                        _dump_msg(f"{prefix}.{k}", v, depth + 1)

            for fnum, fval in status.items():
                if isinstance(fval, bytes) and 0 < len(fval) <= 64:
                    _dump_msg(f"Starlink status[{fnum}]", fval, depth=0)

        # DeviceState (field 2) contains uptime
        device_state = _decode_nested(status, 2)
        uptime = device_state.get(1, 0)  # uptime_s (varint)

        # State derivation: presence of `outage` sub-message (field 1014)
        # means dish is NOT serving; absence implies CONNECTED. Outage.cause
        # (field 1, varint enum) carries the specific reason.
        outage = _decode_nested(status, 1014)
        if outage:
            dish_state = _enum_name(OUTAGE_NAMES, outage.get(1, 0))
        else:
            dish_state = "Connected"

        # Power input. `aps_stats` (1048) is the Mini-class path, `upsu_stats`
        # (1043) the actuated-dish one; both nest dish_power as a float at
        # field 6. Neither is authoritative on every model, which is why the
        # Power sensor prefers the DishGetHistory sample -- see
        # `_async_fetch_history`.
        aps_stats = _decode_nested(status, 1048)
        upsu_stats = _decode_nested(status, 1043)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            # The power-supply field maps, kept from the exercise that found
            # where dish power lives on each model. Moved from INFO to DEBUG:
            # they fired on EVERY 30s poll, which is log spam once the answer
            # is known, but they are still what to turn on when a new model
            # reports no power.
            for label, stats in (
                ("aps_stats (1048)", aps_stats),
                ("upsu_stats (1043)", upsu_stats),
                ("device_state (2)", device_state),
            ):
                if stats:
                    _LOGGER.debug(
                        "Starlink %s fields: %s",
                        label,
                        {
                            k: (v if isinstance(v, (int, float)) else f"<bytes:{len(v)}>")
                            for k, v in stats.items()
                        },
                    )
        power_in = (
            (aps_stats.get(6) if aps_stats else None)
            or (upsu_stats.get(6) if upsu_stats else None)
        )
        router_power = upsu_stats.get(7) if upsu_stats else None

        data: dict[str, Any] = {
            "uptime_s": uptime,
            "pop_ping_drop_rate": status.get(1003),  # float
            "downlink_throughput_bps": status.get(1007),  # float
            "uplink_throughput_bps": status.get(1008),  # float
            "pop_ping_latency_ms": status.get(1009),  # float
            "dish_state": dish_state,
            "stow_requested": _decode_bool(status, 1010),
            "is_snr_above_noise_floor": _decode_bool(status, 1018),
            "is_snr_persistently_low": _decode_bool(status, 1022),
            # Raw enum kept for gating (a Mini reports 2 = NO); the display
            # string is what any sensor shows.
            "has_actuators": status.get(1023, 0),
            "has_actuators_name": _enum_name(
                HAS_ACTUATORS_NAMES, status.get(1023, 0)
            ),
            "seconds_to_first_slot": status.get(1002),
            "is_cell_disabled": _decode_bool(status, 1029),
            "is_moving_fast_persisted": _decode_bool(status, 1042),
            # New in firmware 2026.06.12 -- the dish telling us the connection
            # is being treated as metered.
            "treat_as_metered": _decode_bool(status, 1056),
        }

        if power_in is not None:
            data["power_in_w"] = power_in
        if router_power is not None:
            data["router_power_in_w"] = router_power

        if 1016 in status:
            data["eth_speed_mbps"] = status.get(1016)
        if 1017 in status:
            data["mobility_class"] = _enum_name(MOBILITY_NAMES, status.get(1017))
        if 1020 in status:
            data["class_of_service"] = _enum_name(
                SERVICE_CLASS_NAMES, status.get(1020)
            )
        if 1032 in status:
            data["reboot_reason"] = _enum_name(REBOOT_REASON_NAMES, status.get(1032))

        # Disablement code on status (field 1024, UtDisablementCode). This is
        # the LIVE value; diagnostics carries its own copy of the same enum.
        if 1024 in status:
            data["disablement_code_status"] = _enum_name(
                DISABLEMENT_NAMES, status.get(1024)
            )

        # Why the link is rate-limited, each way (fields 1044 / 1045). On a
        # metered Roam plan this is the difference between "the internet is
        # slow" and "you are past your priority data".
        if 1044 in status:
            data["dl_restricted_reason"] = _enum_name(
                RATE_LIMIT_NAMES, status.get(1044)
            )
        if 1045 in status:
            data["ul_restricted_reason"] = _enum_name(
                RATE_LIMIT_NAMES, status.get(1045)
            )

        # Software update progress (state 1021 + stats 1026 + the two reboot
        # fields). The pre-existing `diag_software_update_pending` alert only
        # says "there is one"; these say how far along it is.
        if 1021 in status:
            data["software_update_state"] = _enum_name(
                UPDATE_STATE_NAMES, status.get(1021)
            )
        update_stats = _decode_nested(status, 1026)
        if update_stats:
            progress = update_stats.get(2)
            if progress is not None:
                data["software_update_progress"] = progress
            data["software_update_requires_reboot"] = _decode_bool(update_stats, 3)
        if 1030 in status:
            data["software_update_reboot_ready"] = _decode_bool(status, 1030)
        if 1031 in status:
            data["seconds_until_update_reboot"] = status.get(1031)

        # Obstruction stats (field 1004). `time_obstructed` is field 9;
        # field 1006 (the old 24h total this used to read) is RESERVED on
        # current firmware and always decoded as absent.
        obstruction = _decode_nested(status, 1004)
        if obstruction:
            data["fraction_obstructed"] = obstruction.get(1)
            data["currently_obstructed"] = _decode_bool(obstruction, 5)
            data["obstruction_time_s"] = obstruction.get(9)
            data["obstruction_valid_s"] = obstruction.get(4)
            data["obstruction_patches_valid"] = obstruction.get(10)
            if _decode_bool(obstruction, 8):
                # Only meaningful once the dish says the averages are valid;
                # before that they are zeros that read like "never obstructed".
                _set_if_present(data, "obstruction_avg_duration_s", obstruction.get(6))
                _set_if_present(data, "obstruction_avg_interval_s", obstruction.get(7))

        # GPS stats (field 1015). Satellite count and validity are NOT
        # policy-gated the way coordinates are, so these keep working on a
        # plan where `get_location` is refused.
        gps_stats = _decode_nested(status, 1015)
        if gps_stats:
            data["gps_valid"] = _decode_bool(gps_stats, 1)
            data["gps_satellites"] = gps_stats.get(2, 0)
            data["gps_inhibited"] = _decode_bool(gps_stats, 4)
            if 5 in gps_stats:
                data["gps_filter_state"] = _enum_name(
                    ATTITUDE_STATE_NAMES, gps_stats.get(5)
                )

        # Battery stats (field 1054), new in firmware 2026.05.07.
        battery = _decode_nested(status, 1054)
        if battery:
            data["battery_soc"] = battery.get(1, 0)
            data["battery_charging"] = _decode_bool(battery, 2)
            data["battery_power_source"] = _enum_name(
                POWER_SOURCE_NAMES, battery.get(3, 0)
            )

        # Alignment (field 1027) on an actuated dish; the flat boresight
        # fields (1011 / 1012) are present regardless.
        alignment = _decode_nested(status, 1027)
        if alignment:
            _set_if_present(data, "tilt_angle_deg", alignment.get(3))
            _set_if_present(data, "attitude_uncertainty_deg", alignment.get(7))
            if 2 in alignment:
                data["actuator_state"] = _enum_name(
                    ACTUATOR_STATE_NAMES, alignment.get(2)
                )
        _set_if_present(data, "boresight_azimuth_deg", status.get(1011))
        _set_if_present(data, "boresight_elevation_deg", status.get(1012))

        # Alerts (DishGetStatusResponse.alerts, field 1005). Every member of
        # the current DishAlerts is decoded -- twelve of these were simply
        # never read, including both water-detection flags.
        alerts = _decode_nested(status, 1005)
        if alerts:
            data["alert_motors_stuck"] = _decode_bool(alerts, 1)
            data["alert_thermal_shutdown"] = _decode_bool(alerts, 2)
            data["alert_thermal_throttle"] = _decode_bool(alerts, 3)
            data["alert_unexpected_location"] = _decode_bool(alerts, 4)
            data["alert_mast_not_near_vertical"] = _decode_bool(alerts, 5)
            # Two separate slow-ethernet flags (6 = slow, 18 = stuck at 100M).
            # Folded, because the customer question is the same either way.
            data["alert_slow_ethernet"] = _decode_bool(alerts, 6) or _decode_bool(
                alerts, 18
            )
            data["alert_roaming"] = _decode_bool(alerts, 7)
            data["alert_install_pending"] = _decode_bool(alerts, 8)
            # field 9 = is_heating -- the Mini reports heating activity
            # here, not under diagnostics alerts (which is silent on
            # current firmware), so this is the canonical source.
            data["alert_is_heating"] = _decode_bool(alerts, 9)
            data["alert_power_supply_thermal_throttle"] = _decode_bool(alerts, 10)
            data["alert_power_save_idle"] = _decode_bool(alerts, 11)
            data["alert_dbf_telem_stale"] = _decode_bool(alerts, 14)
            data["alert_low_motor_current"] = _decode_bool(alerts, 16)
            data["alert_lower_signal_than_predicted"] = _decode_bool(alerts, 17)
            data["alert_obstruction_map_reset"] = _decode_bool(alerts, 19)
            data["alert_dish_water_detected"] = _decode_bool(alerts, 20)
            data["alert_router_water_detected"] = _decode_bool(alerts, 21)
            data["alert_upsu_router_port_slow"] = _decode_bool(alerts, 22)
            data["alert_no_ethernet_link"] = _decode_bool(alerts, 23)

        # DishConfig (field 2000) -- the settable half, riding the poll we
        # already make so the controls have state with no extra call.
        config = _decode_nested(status, 2000)
        if config:
            data.update(_decode_dish_config(config))

        return data

    async def _async_fetch_history(self) -> dict[str, Any]:
        """Fetch DishGetHistory: latest power / drop-rate sample + outages.

        Layout:
          Response.dish_get_history (field 2006) wraps the message.
          DishGetHistory.power_in (1010) / pop_ping_drop_rate (1001) are
          packed-repeated fixed32 -- N x 4 bytes of little-endian float32.
          DishGetHistory.current (1) is the next-write index; (current - 1)
          modulo len(samples) is the most-recent sample.
          DishGetHistory.outages (1009) is a REPEATED DishOutage, which is
          why the decode goes through `_decode_fields` -- a last-wins read
          would return only the final outage and report a count of one.
        """
        raw = await self._async_call(_GET_HISTORY_REQUEST)
        if not raw:
            return {}

        response = _decode_message(raw)
        # Response field 2006 = DishGetHistoryResponse on current firmware.
        history_bytes = response.get(2006) or response.get(1007)
        if not isinstance(history_bytes, bytes):
            _LOGGER.info(
                "Starlink get_history: no history field, available: %s",
                {k: type(v).__name__ for k, v in response.items()},
            )
            return {}

        history_multi = _decode_fields(history_bytes)
        history = {k: v[-1] for k, v in history_multi.items()}
        current = history.get(1)

        def _latest_sample(field_number: int) -> float | None:
            """Latest entry of a packed-fixed32 rolling buffer."""
            packed = history.get(field_number)
            if not isinstance(packed, bytes) or not packed:
                return None
            samples = _decode_packed_fixed32(packed)
            if not samples:
                return None
            if isinstance(current, int) and current > 0:
                return samples[(current - 1) % len(samples)]
            return next((s for s in reversed(samples) if s), samples[-1])

        out: dict[str, Any] = {}

        power_w = _latest_sample(1010)  # power_in
        if power_w is not None:
            out["power_in_w"] = power_w

        # pop_ping_drop_rate moved out of status on current firmware;
        # the rolling history at field 1001 still tracks it.
        drop_rate = _latest_sample(1001)
        if drop_rate is not None:
            out["pop_ping_drop_rate"] = drop_rate

        # Outages within the history window. Kept as a list of dicts so the
        # sensor can carry them as attributes; the state is just the count.
        outages: list[dict[str, Any]] = []
        for entry in history_multi.get(1009, []):
            if not isinstance(entry, bytes) or not entry:
                continue
            fields = _decode_message(entry)
            duration_ns = fields.get(3, 0)
            outages.append(
                {
                    "cause": _enum_name(OUTAGE_NAMES, fields.get(1, 0)),
                    "start_timestamp_ns": fields.get(2),
                    "duration_s": (
                        round(duration_ns / 1_000_000_000, 1)
                        if isinstance(duration_ns, int)
                        else None
                    ),
                    "did_switch": bool(fields.get(4, 0)),
                }
            )
        if outages:
            out["recent_outages"] = outages
            out["recent_outage_count"] = len(outages)
        else:
            # An empty list is a real answer ("no outages in the window"),
            # distinct from the field being absent, so the count is emitted
            # whenever the history decoded at all.
            out["recent_outage_count"] = 0

        return out

    async def _async_fetch_device_info(self) -> dict[str, Any]:
        """Fetch device info via the consumer API (get_device_info).

        Current firmware wraps the response in `get_device_info` at
        Response field 1004 (NOT 1008 as in legacy protos -- 1008 is now
        the Request opcode for the call).
        """
        raw = await self._async_call(_GET_DEVICE_INFO_REQUEST)
        if not raw:
            return {}

        response = _decode_message(raw)
        # Current firmware: response wraps in field 1004; legacy: field 1008.
        # Try both so the add path works on old and new dishes alike.
        info_bytes = response.get(1004) or response.get(1008)
        if not isinstance(info_bytes, bytes):
            _LOGGER.info(
                "Starlink get_device_info: no info field found, available: %s",
                {k: type(v).__name__ for k, v in response.items()},
            )
            return {}

        info = _decode_message(info_bytes)
        # GetDeviceInfoResponse field 1 = DeviceInfo (embedded)
        device_info = _decode_nested(info, 1)
        if not device_info:
            # Some firmwares flatten DeviceInfo at the top level
            device_info = info

        out: dict[str, Any] = {
            "device_id": _decode_string(device_info, 1),
            "hardware_version": _decode_string(device_info, 2),
            "software_version": _decode_string(device_info, 3),
            "country_code": _decode_string(device_info, 4),
        }
        # DeviceInfo.bootcount = 8, .utc_offset_s = 5 -- diagnostics only.
        if 8 in device_info:
            out["boot_count"] = device_info.get(8)
        return out

    async def _async_fetch_diagnostics(self) -> dict[str, Any]:
        """Fetch diagnostics via the official device API (get_diagnostics)."""
        raw = await self._async_call(_GET_DIAGNOSTICS_REQUEST)
        if not raw:
            _LOGGER.info("Starlink get_diagnostics: empty response")
            return {}

        response = _decode_message(raw)

        # Response field 6001 = DishGetDiagnosticsResponse
        diag_bytes = response.get(6001)
        if not isinstance(diag_bytes, bytes):
            _LOGGER.info(
                "Starlink get_diagnostics: field 6001 not found, available: %s",
                {k: type(v).__name__ for k, v in response.items()},
            )
            return {}

        diag = _decode_message(diag_bytes)

        data: dict[str, Any] = {}

        # Top-level fields. NOTE field 7 is `hardware_self_test` (a TestResult
        # enum), NOT `stowed` -- stowed is field 10. Reading 7 as a bool made
        # `diag_stowed` true for any dish that had ever run a self test.
        data["diag_device_id"] = _decode_string(diag, 1)
        data["diag_hardware_version"] = _decode_string(diag, 2)
        data["diag_software_version"] = _decode_string(diag, 3)
        data["diag_stowed"] = _decode_bool(diag, 10)
        if 7 in diag:
            data["diag_self_test"] = _enum_name(SELF_TEST_NAMES, diag.get(7))
        # field 14 = overage_rate_limited: the plan's priority data is spent.
        data["diag_overage_rate_limited"] = _decode_bool(diag, 14)

        # DisablementCode (field 6). Same member set as the status copy, so
        # both read from DISABLEMENT_NAMES. May arrive as a bare varint or,
        # on some firmwares, wrapped in a sub-message.
        disablement_raw = diag.get(6)
        if isinstance(disablement_raw, int):
            data["disablement_code"] = _enum_name(DISABLEMENT_NAMES, disablement_raw)
        elif isinstance(disablement_raw, bytes) and disablement_raw:
            inner = _decode_message(disablement_raw)
            data["disablement_code"] = _enum_name(
                DISABLEMENT_NAMES, inner.get(1, 0) if inner else 0
            )
        else:
            # Absent means the proto3 default, which for this enum is
            # UNKNOWN_STATE (0), not OK. Reporting "OK" here claimed a
            # verdict the dish never gave.
            data["disablement_code"] = DISABLEMENT_NAMES[0]

        # Alerts (field 5, embedded). Field 9 is RESERVED on current firmware
        # (it was `moving_too_fast_for_policy`), so nothing reads it -- the
        # live equivalent is the status disablement code MOVING_TOO_FAST.
        alerts = _decode_nested(diag, 5)
        if alerts:
            data["diag_heating"] = _decode_bool(alerts, 1)
            data["diag_thermal_throttle"] = _decode_bool(alerts, 2)
            data["diag_thermal_shutdown"] = _decode_bool(alerts, 3)
            data["diag_power_supply_thermal_throttle"] = _decode_bool(alerts, 4)
            data["diag_motors_stuck"] = _decode_bool(alerts, 5)
            data["diag_mast_not_near_vertical"] = _decode_bool(alerts, 6)
            data["diag_slow_ethernet"] = _decode_bool(alerts, 7)
            data["diag_software_update_pending"] = _decode_bool(alerts, 8)
            data["diag_obstructed"] = _decode_bool(alerts, 10)

        # AlignmentStats (field 9) -- the official API's own copy of the
        # boresight angles, used when the consumer status call is refused.
        diag_alignment = _decode_nested(diag, 9)
        if diag_alignment:
            _set_if_present(data, "boresight_azimuth_deg", diag_alignment.get(1))
            _set_if_present(data, "boresight_elevation_deg", diag_alignment.get(2))

        return data


# -- DishConfig decoder -------------------------------------------------------
# DECODE ONLY. The matching encoder was deleted with the `dish_set_config`
# write path (see `async_set_power_save`): the firmware refuses that RPC from
# the LAN, so DishConfig is something we read and never write. The dish's own
# `apply_*` flag fields are therefore not modelled here -- nothing sets them.
#
# Each row is: key -> (field number, kind).
#   "bool"  -> varint 0/1
#   "uint"  -> varint
#   "enum"  -> varint, with a name map applied on decode
_DISH_CONFIG_FIELDS: dict[str, tuple[int, str]] = {
    "snow_melt_mode": (1, "enum"),
    "location_request_mode": (2, "enum"),
    "level_dish_mode": (3, "enum"),
    "power_save_start_minutes": (4, "uint"),
    "power_save_duration_minutes": (5, "uint"),
    "power_save_mode": (6, "bool"),
    "swupdate_three_day_deferral_enabled": (7, "bool"),
    "asset_class": (8, "uint"),
    "swupdate_reboot_hour": (9, "uint"),
}

# Enum-valued config keys and the map that names them.
_DISH_CONFIG_ENUMS: dict[str, dict[int, str]] = {
    "snow_melt_mode": SNOW_MELT_NAMES,
    "level_dish_mode": LEVEL_DISH_NAMES,
}


def _decode_dish_config(config: dict[int, Any]) -> dict[str, Any]:
    """Flatten a decoded DishConfig into ``config_*`` keys.

    Every key is emitted whenever the message decoded at all, defaulting to
    the proto3 zero. Absent-means-zero is correct here and matters: a dish
    with the sleep schedule off omits `power_save_mode` entirely, and
    leaving the key out would leave the switch showing Unknown rather than
    Off.
    """
    if not config:
        return {}
    out: dict[str, Any] = {}
    for key, (field, kind) in _DISH_CONFIG_FIELDS.items():
        raw = config.get(field, 0)
        if kind == "bool":
            out[f"config_{key}"] = bool(raw)
        elif kind == "enum":
            names = _DISH_CONFIG_ENUMS.get(key)
            out[f"config_{key}"] = (
                _enum_name(names, raw) if names else raw
            )
            out[f"config_{key}_raw"] = raw
        else:
            out[f"config_{key}"] = raw
    return out
