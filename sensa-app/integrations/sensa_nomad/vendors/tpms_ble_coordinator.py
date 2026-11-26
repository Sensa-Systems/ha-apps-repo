"""Coordinator for one Sensa-owned BLE tyre-pressure (TPMS) sensor.

Subscribes to BLE adverts for one sensor MAC via HA's bluetooth
integration, decodes each frame with `tpms_ble.decode_service_info`
pinned to the sensor's format, and pushes the reading to entities.

Push-based like the Orion XS coordinator, with one deliberate
difference: a quiet sensor is NOT stale. Cap sensors sleep while the
vehicle is parked and wake on motion or a pressure change, so the last
reading stays on the entities and `last_seen` tells the card how old
it is. Only RSSI is cleared after a quiet spell, because a signal
strength from hours ago means nothing.

Data shape returned to entities:

    {
        "format": str | None,
        "pressure_kpa": float | None,     # gauge
        "temperature_c": float | None,
        "battery_v": float | None,
        "battery_pct": int | None,
        "alarm": bool | None,
        "rssi": int | None,
        "last_seen": datetime | None,     # UTC, last successful decode
    }
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .tpms_ble import decode_tpms_advert

_LOGGER = logging.getLogger(__name__)

# After this long without a frame the RSSI is dropped (the reading is
# kept). UNVERIFIED cadence: measure the OBEST kit parked and moving.
_RSSI_STALE_AFTER_SECS = 300.0

SCAN_INTERVAL = timedelta(seconds=60)


class TpmsBleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-based coordinator for one TPMS BLE sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        device_id: str,
        mac: str,
        tpms_format: str | None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"tpms_ble_{device_id}",
            update_interval=SCAN_INTERVAL,
        )
        self._device_id = device_id
        self._mac = mac.upper()
        self._format = tpms_format or None
        self._values: dict[str, Any] = {}
        self._last_ok_ts: float | None = None
        self._unregister_ble: Callable[[], None] | None = None
        # Payload last decoded per company ID. HA merges manufacturer_data
        # across adverts, and a DJTPMS sensor changes its "company ID", so
        # the dict fills with old frames. Only a payload that changed since
        # the last callback is news.
        self._seen_mfr: dict[int, bytes] = {}

    async def async_start(self) -> None:
        """Register the BLE-advert callback and seed entity state."""
        try:
            from homeassistant.components.bluetooth import (
                BluetoothCallbackMatcher,
                BluetoothScanningMode,
                async_register_callback,
            )
        except ImportError:
            _LOGGER.error(
                "TPMS %s: HA bluetooth integration not available; "
                "coordinator cannot start",
                self._device_id,
            )
            return

        def _on_advert(service_info: Any, change: Any) -> None:
            try:
                self._handle_service_info(service_info)
            except Exception:
                _LOGGER.exception(
                    "TPMS %s: unhandled error in advert callback",
                    self._device_id,
                )

        self._unregister_ble = async_register_callback(
            self.hass,
            _on_advert,
            BluetoothCallbackMatcher(address=self._mac),
            BluetoothScanningMode.PASSIVE,
        )
        self.async_set_updated_data(dict(self._values))
        _LOGGER.info(
            "TPMS %s: subscribed to BLE adverts for %s (format %s)",
            self._device_id, self._mac, self._format or "any",
        )

    async def async_stop(self) -> None:
        """Unregister the BLE callback (unload, device removal)."""
        if self._unregister_ble is not None:
            try:
                self._unregister_ble()
            except Exception:
                _LOGGER.debug(
                    "TPMS %s: unregister callback raised",
                    self._device_id, exc_info=True,
                )
            self._unregister_ble = None

    async def async_shutdown(self) -> None:
        """The BLE remove path calls this, not `async_stop`."""
        await self.async_stop()
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Internal plumbing
    # ------------------------------------------------------------------

    def _fresh_manufacturer_data(self, mfr: dict[int, bytes]) -> dict[int, bytes]:
        fresh = {
            cid: bytes(payload)
            for cid, payload in mfr.items()
            if self._seen_mfr.get(cid) != bytes(payload)
        }
        self._seen_mfr = {cid: bytes(payload) for cid, payload in mfr.items()}
        return fresh

    @callback
    def _handle_service_info(self, service_info: Any) -> None:
        """Decode one advert and push the reading to entities."""
        mfr = getattr(service_info, "manufacturer_data", None) or {}
        fresh = self._fresh_manufacturer_data(mfr)
        service_data = getattr(service_info, "service_data", None) or {}
        if not fresh and not service_data:
            # Same frame again: the sensor is alive and unchanged.
            if self._values:
                self._last_ok_ts = time.monotonic()
                self._values = {
                    **self._values,
                    "rssi": getattr(service_info, "rssi", None),
                    "last_seen": dt_util.utcnow(),
                }
                self.async_set_updated_data(dict(self._values))
            return
        reading = decode_tpms_advert(
            manufacturer_data=fresh,
            service_data=service_data,
            service_uuids=getattr(service_info, "service_uuids", None),
            name=getattr(service_info, "name", None),
            expected_format=self._format,
        )
        if reading is None:
            return
        self._last_ok_ts = time.monotonic()
        self._values = {
            **reading.as_dict(),
            "rssi": getattr(service_info, "rssi", None),
            "last_seen": dt_util.utcnow(),
        }
        self.async_set_updated_data(dict(self._values))

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic tick: drop a stale RSSI, keep the reading."""
        if (
            self._last_ok_ts is not None
            and self._values.get("rssi") is not None
            and time.monotonic() - self._last_ok_ts > _RSSI_STALE_AFTER_SECS
        ):
            self._values = {**self._values, "rssi": None}
        return dict(self._values)
