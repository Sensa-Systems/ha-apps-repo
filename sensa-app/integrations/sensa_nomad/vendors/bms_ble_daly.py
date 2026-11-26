"""Daly BMS parser with the pack's rated capacity added.

aiobmsble's `daly_bms` reads live values only, so a pack's size has to be
guessed from remaining Ah over SoC. That guess drifts (it rounds to 104 on a
105 Ah pack at low charge) and fails outright when a pack reports 0 Ah
remaining, which a Foxygen 105Ah did on the bench 2026-10-30.

A Daly D2 pack holds its rated capacity in holding register 0x0080, in
0.1 Ah (the Foxygen 105Ah answered 1050). This subclass reads it after a normal
update, until it gets a valid answer (at most three tries per parser
instance), and reports it as `design_capacity`, the BMSSample key
`BmsCoordinator._maybe_enrich` already prefers. Foxygen sells
105, 230, 300 and 460 Ah packs, so the exact figure is what tells them apart
(see the `foxygen` catalog entry's `model_detection`).

Only the D2 protocol is touched: the X81 variant newer aiobmsble also probes
has a different register map. Any failure (timeout, an older aiobmsble
without the internals used here, a pack that does not answer) leaves the
sample as aiobmsble built it, so the capacity falls back to the estimate
rather than failing the poll.
"""

from __future__ import annotations

import logging
from typing import Any

from aiobmsble.bms import daly_bms  # type: ignore[import-not-found]

_LOGGER = logging.getLogger(__name__)

_D2 = 0xD2
_FCT_READ = 0x03
_REG_RATED_CAPACITY = 0x0080
# A pack that never answers stops costing a round trip per poll after this.
_RATED_MAX_ATTEMPTS = 3


def _crc_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def rated_capacity_request() -> bytes:
    """D2 read of one holding register at 0x0080."""
    frame = bytes([_D2, _FCT_READ]) + _REG_RATED_CAPACITY.to_bytes(2, "big") + (1).to_bytes(2, "big")
    return frame + _crc_modbus(frame).to_bytes(2, "little")


def parse_rated_capacity(msg: bytes) -> int | None:
    """Whole Ah from a validated one-register reply, or None.

    aiobmsble's notification handler has already checked the header, length
    and CRC before `_msg` is set. Zero and 0xFFFF are "not set", not a size.
    """
    if len(msg) < 5 or msg[0] != _D2 or msg[1] != _FCT_READ or msg[2] != 2:
        return None
    raw = int.from_bytes(msg[3:5], "big")
    if raw in (0, 0xFFFF):
        return None
    return round(raw / 10)


class BMS(daly_bms.BMS):
    """`daly_bms.BMS` that also reports `design_capacity` on D2 packs."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rated_ah: int | None = None
        self._rated_attempts = 0

    async def _async_update(self) -> Any:
        result = await super()._async_update()
        # Older aiobmsble (0.26.0) speaks only D2 and has no `_proto`, so a
        # missing attribute means D2.
        #
        # Retried on later polls until it answers: a Foxygen sometimes sends
        # a reply twice, and the stray copy lands on this read (seen on .207,
        # 2026-10-30), which `parse_rated_capacity` rightly rejects.
        if (
            self._rated_ah is None
            and self._rated_attempts < _RATED_MAX_ATTEMPTS
            and getattr(self, "_proto", _D2) == _D2
        ):
            self._rated_attempts += 1
            try:
                await self._await_msg(rated_capacity_request())
                self._rated_ah = parse_rated_capacity(bytes(self._msg))
            except Exception:  # the remaining/SoC estimate is the fallback
                _LOGGER.debug("Daly rated capacity read failed", exc_info=True)
        if self._rated_ah:
            result["design_capacity"] = self._rated_ah
        return result
