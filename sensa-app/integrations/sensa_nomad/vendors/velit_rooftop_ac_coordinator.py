"""Coordinator for one Velit Rooftop Air Conditioner over BLE GATT.

Polls the AC via :class:`~.velit_rooftop_ac.VelitAC` (GATT connect →
query) and exposes setter methods the climate entity calls to write
commands. Mirrors `bms_ble.BmsCoordinator`'s shape: resolve the BLE
device by MAC through HA's bluetooth adapter (so Sensa Bluetooth Proxies
/ the virtual scanner are picked up transparently), poll on an interval,
and drop the client on error so the next poll reconnects clean.

Data shape (consumed by the climate entity):

    {
        "power_on": bool | None,
        "mode": str | None,            # cool / heat / fan / eco / …
        "target_temp_c": float | None,
        "current_temp_c": float | None,
        "fan_speed": int | None,       # 1–5
        "swing_on": bool | None,
        "fault_code": int | None,
        "_mac": str,
        "_display_name": str,
    }
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .velit_rooftop_ac import VelitAC

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=30)


def _canonical_mac(value: str) -> str:
    """Colon-separated uppercase MAC for `async_ble_device_from_address`."""
    cleaned = str(value or "").upper().replace(":", "").replace("-", "").replace(" ", "")
    if len(cleaned) != 12:
        return str(value or "").upper()
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))


class VelitRooftopACCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls a single Velit Rooftop AC over BLE GATT."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        device_id: str,
        mac_address: str,
        display_name: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"velit_rooftop_ac_{device_id}",
            update_interval=POLL_INTERVAL,
        )
        self._device_id = device_id
        self._mac_address = _canonical_mac(mac_address)
        self._display_name = display_name
        self._ac: VelitAC | None = None

    async def _ensure_ac(self) -> VelitAC:
        if self._ac is not None:
            return self._ac
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
                f"No BLE device found for {self._mac_address} — out of range "
                "of any scanner"
            )
        self._ac = VelitAC(ble_device)
        return self._ac

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            ac = await self._ensure_ac()
            sample = await ac.async_update()
        except UpdateFailed:
            self._ac = None
            raise
        except Exception as err:
            self._ac = None
            raise UpdateFailed(f"Velit read failed: {err}") from err

        data = dict(sample) if sample else {}
        data["_mac"] = self._mac_address
        data["_display_name"] = self._display_name
        return data

    # --- command helpers (called by the climate entity) ------------------

    def _push_optimistic(self, changes: dict[str, Any]) -> None:
        """Reflect a just-sent command in the entity state IMMEDIATELY.

        BLE GATT write + read-back takes several seconds, so waiting for the
        next poll to show a toggle felt like a 6–31s lag (the read raced the
        device applying the command and often read the stale value, so the card
        only updated on the next scheduled poll). Merging the expected change
        into the coordinator data and pushing it makes the card flip instantly;
        the scheduled poll then confirms (or, on a failed write, corrects).
        """
        base = dict(self.data) if self.data else {}
        base.update(changes)
        self.async_set_updated_data(base)

    async def _command(self, coro_factory, optimistic: dict[str, Any] | None = None) -> None:
        """Optimistically reflect the change, then run the setter on the client.

        Reuses the existing connection. On SUCCESS we do NOT force an immediate
        read-back — that read races the device applying the command and would
        clobber the optimistic value back to stale (the 6/31s flip the user
        saw); the next scheduled poll (POLL_INTERVAL) confirms instead. On
        ERROR we drop the client and refresh now to correct the optimistic
        value (the next poll reconnects clean, matching the poll path).
        """
        if optimistic:
            self._push_optimistic(optimistic)
        try:
            ac = await self._ensure_ac()
            await coro_factory(ac)
        except Exception as err:  # noqa: BLE001
            self._ac = None
            _LOGGER.warning("Velit %s: command failed: %s", self._device_id, err)
            await self.async_request_refresh()
            return

    async def async_set_power(self, on: bool) -> None:
        await self._command(lambda ac: ac.async_set_power(on), {"power_on": on})

    async def async_set_mode(self, mode: str) -> None:
        # Selecting a mode also implies the unit is on.
        await self._command(
            lambda ac: ac.async_set_mode(mode), {"mode": mode, "power_on": True}
        )

    async def async_set_temperature(self, celsius: float) -> None:
        await self._command(
            lambda ac: ac.async_set_temperature(celsius), {"target_temp_c": celsius}
        )

    async def async_set_fan_speed(self, speed: int) -> None:
        await self._command(
            lambda ac: ac.async_set_fan_speed(speed), {"fan_speed": speed}
        )

    async def async_set_swing(self, on: bool) -> None:
        await self._command(lambda ac: ac.async_set_swing(on), {"swing_on": on})

    async def async_stop(self) -> None:
        """Disconnect the BLE client (called on unload)."""
        ac, self._ac = self._ac, None
        if ac is not None:
            try:
                await ac.disconnect()
            except Exception:
                _LOGGER.debug("Velit %s: disconnect on stop failed",
                              self._device_id, exc_info=True)
