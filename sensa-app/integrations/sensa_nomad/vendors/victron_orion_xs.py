"""Standalone Orion XS BLE Instant Readout decoder.

Home Assistant's built-in `victron_ble` integration (via the pinned
`victron-ble-ha-parser` library) doesn't yet support the Orion XS DC-DC
charger family — Victron hasn't documented the record format publicly,
so HA core doesn't ship a decoder for the `mode=0x0F` frames the Orion
XS broadcasts. This module fills that gap: a Sensa-owned parser that
takes the raw `manufacturer_data[0x02E1]` bytes + the customer's
Instant-Readout encryption key and returns the decoded sensor values.

Cross-reference implementations (both reverse-engineered, neither
authoritative):
  * `keshavdv/victron-ble` Python library — OrionXS class in
    `victron_ble/devices/orion_xs.py`. Uses `pycryptodome`'s
    AES-CTR with a 128-bit little-endian counter; we produce the
    same keystream via `cryptography`'s CTR mode pre-built with a
    byte-matched nonce (safe within a single 16-byte block, which
    covers the full 14-byte Orion XS plaintext).
  * `Fabian-Schmidt/esphome-victron_ble` PR #54 — original
    reverse-engineering of the record format.

Frame layout (per `base.py::parse_container` in upstream):

    bytes 0-1  : prefix (first byte always 0x10 for Instant Readout)
    bytes 2-3  : model_id (u16 LE — informational; we gate on mode)
    byte  4    : mode byte. 0x0F = Orion XS.
    bytes 5-6  : AES-CTR initial counter value (u16 LE, zero-extended
                 to 128 bits).
    byte  7    : encryption-key verification byte (must match the
                 first byte of the 16-byte encryption key).
    bytes 8+   : AES-CTR encrypted plaintext (14 bytes for Orion XS).

Decrypted payload (14 bytes, all little-endian):

    byte  0    : device_state (enum, 0xFF = sensor unavailable)
    byte  1    : charger_error (enum, 0xFF = sensor unavailable)
    bytes 2-3  : output_voltage (u16 /100 → V; 0xFFFF = unavailable)
    bytes 4-5  : output_current (u16 /10 → A; 0xFFFF = unavailable.
                 NOTE: the upstream library treats this as UNSIGNED
                 per the decompiled ESPHome PR, even though a DC-DC
                 current can be signed. Matching upstream to stay
                 compatible — if Victron documents signed semantics
                 later we re-evaluate.)
    bytes 6-7  : input_voltage (u16 /100 → V; 0xFFFF = unavailable)
    bytes 8-9  : input_current (u16 /10 → A; 0xFFFF = unavailable)
    bytes 10-13: off_reason (u32 bitfield — see OFF_REASON_FLAGS)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

# Stable constants lifted from upstream's `base.py` / `orion_xs.py`.
# Keeping them here as literals rather than importing pycryptodome /
# `victron_ble` — we don't want to drag either in as a runtime dep
# when HA core already ships `cryptography`.
ORION_XS_MODE_BYTE = 0x0F
ADVERT_PREFIX_BYTE = 0x10
VICTRON_COMPANY_ID = 0x02E1

# Sentinels (Victron-spec "sensor unavailable" values).
DEVICE_STATE_UNAVAILABLE = 0xFF
CHARGER_ERROR_UNAVAILABLE = 0xFF
U16_UNAVAILABLE = 0xFFFF

# Full plaintext length for an Orion XS record — the decoded section
# after the key-check byte is always exactly 14 bytes (enforced by the
# upstream BitReader field list). If that changes in a future firmware,
# the parser needs updating — we validate length so a mismatch raises
# rather than silently producing garbage values.
DECRYPTED_PAYLOAD_BYTES = 14


class OrionXsDeviceState(IntEnum):
    """Per upstream `base.py::OperationMode` (Victron's charger state
    enum, shared across SmartSolar / Orion XS). Values outside this
    list fall through to the raw int — a future firmware could add a
    new state and we don't want to lose the reading just because the
    enum is short."""
    OFF = 0
    LOW_POWER = 1
    FAULT = 2
    BULK = 3
    ABSORPTION = 4
    FLOAT = 5
    STORAGE = 6
    EQUALIZE_MANUAL = 7
    INVERTING = 9
    POWER_SUPPLY = 11
    STARTING_UP = 245
    REPEATED_ABSORPTION = 246
    RECONDITION = 247
    BATTERY_SAFE = 248
    ACTIVE = 249
    EXTERNAL_CONTROL = 252
    NOT_AVAILABLE = 255


# Each bit meaning for the 32-bit off_reason field. Stored as
# `(bit, human_name)` tuples — we surface the live flags as a
# comma-separated list of names so the customer's dashboard shows
# "no-input-power, engine-shutdown" instead of a 32-bit integer.
#
# Upstream (`base.py::OffReason`) treats the field as an Enum with
# pre-combined constants (e.g. 0x14 = 0x10 | 0x04), which breaks on
# any unknown combination — we use an IntFlag-style bit decomposition
# here so the decoder is robust to new bits and combinations Victron
# adds later.
OFF_REASON_FLAGS: tuple[tuple[int, str], ...] = (
    (0x01, "no-input-power"),
    (0x02, "switched-off-switch"),
    (0x04, "switched-off-register"),
    (0x08, "remote-input"),
    (0x10, "protection-active"),
    (0x20, "pay-as-you-go-out-of-credit"),
    (0x40, "bms"),
    (0x80, "engine-shutdown"),
    (0x100, "analysing-input-voltage"),
)


class OrionXsDecodeError(Exception):
    """Raised when a frame can't be decoded. Callers should log at
    DEBUG (not ERROR) — a decryption mismatch from a stale advert or
    a crossed-key typo is a legitimate operational state, not a bug."""


class OrionXsKeyMismatchError(OrionXsDecodeError):
    """Encryption-key verification byte didn't match. Almost always
    means the customer's typed key doesn't match this Orion XS.
    Surface this distinctly so the UI can prompt the customer to
    re-enter the key from VictronConnect."""


@dataclass(frozen=True)
class OrionXsReading:
    """Decoded sensor values from one Orion XS advert.

    Every field is `None` when the advert's sentinel says the sensor
    reported unavailable for this frame — preserves the upstream's
    Victron-spec semantics (a cached `None` tells the entity to go
    Unavailable rather than report 0, which would be wrong for an
    off charger).
    """
    device_state: int | None
    charger_error: int | None
    output_voltage: float | None
    output_current: float | None
    input_voltage: float | None
    input_current: float | None
    off_reason_bits: int | None
    off_reason_flags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Flatten to a `{key: value}` dict keyed on the same names
        the coordinator / entity descriptions use. Callers can hand
        this straight to `async_set_updated_data`."""
        return {
            "device_state": self.device_state,
            "charger_error": self.charger_error,
            "output_voltage": self.output_voltage,
            "output_current": self.output_current,
            "input_voltage": self.input_voltage,
            "input_current": self.input_current,
            "off_reason_bits": self.off_reason_bits,
            "off_reason_flags": list(self.off_reason_flags),
        }


def parse_orion_xs_advert(
    manufacturer_data: bytes, encryption_key_hex: str,
) -> OrionXsReading:
    """Decode one Orion XS BLE advert into an OrionXsReading.

    Raises `OrionXsDecodeError` (or the `OrionXsKeyMismatchError`
    subclass) on any structural problem. Callers catch the base
    class — the subclass is only useful when the UI needs to
    distinguish "wrong key" from "truncated frame".
    """
    if not manufacturer_data or len(manufacturer_data) < 8:
        raise OrionXsDecodeError(
            f"manufacturer_data too short: {len(manufacturer_data)} bytes "
            "(need 8+ for prefix/model_id/mode/iv/key-check)"
        )
    if manufacturer_data[0] != ADVERT_PREFIX_BYTE:
        raise OrionXsDecodeError(
            f"unexpected prefix byte 0x{manufacturer_data[0]:02X} "
            f"(expected 0x{ADVERT_PREFIX_BYTE:02X})"
        )
    mode = manufacturer_data[4]
    if mode != ORION_XS_MODE_BYTE:
        raise OrionXsDecodeError(
            f"not an Orion XS frame: mode=0x{mode:02X} "
            f"(expected 0x{ORION_XS_MODE_BYTE:02X})"
        )

    try:
        key_bytes = bytes.fromhex(encryption_key_hex)
    except ValueError as err:
        raise OrionXsDecodeError(
            f"encryption key isn't valid hex: {err}"
        ) from err
    if len(key_bytes) != 16:
        raise OrionXsDecodeError(
            f"encryption key must decode to 16 bytes (got {len(key_bytes)})"
        )

    iv = int.from_bytes(manufacturer_data[5:7], "little")
    key_check = manufacturer_data[7]
    if key_check != key_bytes[0]:
        raise OrionXsKeyMismatchError(
            f"key-check byte 0x{key_check:02X} != key[0] "
            f"0x{key_bytes[0]:02X} — customer's encryption key doesn't "
            "match this Orion XS"
        )

    ciphertext = manufacturer_data[8:]
    if len(ciphertext) < DECRYPTED_PAYLOAD_BYTES:
        raise OrionXsDecodeError(
            f"ciphertext too short: {len(ciphertext)} bytes "
            f"(need {DECRYPTED_PAYLOAD_BYTES}+)"
        )

    # AES-CTR single-block decrypt. Orion XS plaintext is 14 bytes —
    # fits in one AES block — so we sidestep `cryptography`'s CTR
    # mode (which increments counters big-endian while upstream
    # expects little-endian) by doing the keystream/XOR by hand:
    # encrypt one 16-byte counter block in ECB mode and XOR the
    # first DECRYPTED_PAYLOAD_BYTES bytes of ciphertext against it.
    # Both pycryptodome (upstream) and `cryptography`'s ECB produce
    # identical output for the same counter-block bytes, so our
    # output matches the reference implementation exactly.
    plaintext = _aes_ctr_decrypt_single_block(
        ciphertext[:DECRYPTED_PAYLOAD_BYTES], key_bytes, iv,
    )
    return _decode_payload(plaintext)


def _aes_ctr_decrypt_single_block(
    ciphertext: bytes, key: bytes, iv: int,
) -> bytes:
    """AES-CTR decrypt where the counter fits in a single block.

    The counter block bytes are `iv.to_bytes(16, 'little')` so the
    initial counter matches pycryptodome's `Counter.new(128,
    initial_value=iv, little_endian=True)`. We encrypt that block
    in ECB mode to get the keystream, then XOR against ciphertext.

    Safe only for messages ≤ 16 bytes — beyond that, counter
    increment semantics matter and the `cryptography` + ECB trick
    no longer matches upstream. The Orion XS plaintext is 14 bytes,
    so we're comfortably inside that limit; assert explicitly.
    """
    if len(ciphertext) > 16:
        raise OrionXsDecodeError(
            f"ciphertext ({len(ciphertext)} bytes) exceeds single-block "
            "decrypt limit — upstream's LE counter semantics diverge from "
            "cryptography's BE increment beyond one block"
        )
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    counter_block = iv.to_bytes(16, "little")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    keystream = encryptor.update(counter_block) + encryptor.finalize()
    return bytes(c ^ k for c, k in zip(ciphertext, keystream))


def _decode_payload(plaintext: bytes) -> OrionXsReading:
    """Turn 14 bytes of decrypted payload into a typed reading."""
    if len(plaintext) < DECRYPTED_PAYLOAD_BYTES:
        raise OrionXsDecodeError(
            f"decrypted payload too short: {len(plaintext)} "
            f"(need {DECRYPTED_PAYLOAD_BYTES})"
        )
    device_state_raw = plaintext[0]
    charger_error_raw = plaintext[1]
    output_v_raw = int.from_bytes(plaintext[2:4], "little")
    output_i_raw = int.from_bytes(plaintext[4:6], "little")
    input_v_raw = int.from_bytes(plaintext[6:8], "little")
    input_i_raw = int.from_bytes(plaintext[8:10], "little")
    off_reason_raw = int.from_bytes(plaintext[10:14], "little")

    return OrionXsReading(
        device_state=(
            None if device_state_raw == DEVICE_STATE_UNAVAILABLE
            else device_state_raw
        ),
        charger_error=(
            None if charger_error_raw == CHARGER_ERROR_UNAVAILABLE
            else charger_error_raw
        ),
        output_voltage=(
            None if output_v_raw == U16_UNAVAILABLE else output_v_raw / 100.0
        ),
        output_current=(
            None if output_i_raw == U16_UNAVAILABLE else output_i_raw / 10.0
        ),
        input_voltage=(
            None if input_v_raw == U16_UNAVAILABLE else input_v_raw / 100.0
        ),
        input_current=(
            None if input_i_raw == U16_UNAVAILABLE else input_i_raw / 10.0
        ),
        off_reason_bits=off_reason_raw,
        off_reason_flags=_off_reason_flag_names(off_reason_raw),
    )


def _off_reason_flag_names(bits: int) -> tuple[str, ...]:
    """Decompose the off_reason bitfield into its active flag names.

    IntFlag-style — every bit in OFF_REASON_FLAGS is tested
    independently. Unknown bits get surfaced as hex so a new
    firmware's extra flags show up in the UI rather than getting
    silently dropped."""
    if bits == 0:
        return ()
    names: list[str] = []
    handled = 0
    for bit, name in OFF_REASON_FLAGS:
        if bits & bit:
            names.append(name)
            handled |= bit
    leftover = bits & ~handled
    if leftover:
        names.append(f"unknown-0x{leftover:X}")
    return tuple(names)


def device_state_label(value: int | None) -> str:
    """Human-readable label for a decoded device_state.

    None → "unavailable". Known values → upstream enum names
    lowercased + hyphenated (`bulk`, `absorption`, `external-control`).
    Unknown ints → `state-0xNN` so the reading still surfaces
    meaningfully even if Victron adds a new state.
    """
    if value is None:
        return "unavailable"
    try:
        member = OrionXsDeviceState(value)
    except ValueError:
        return f"state-0x{value:02X}"
    return member.name.lower().replace("_", "-")
