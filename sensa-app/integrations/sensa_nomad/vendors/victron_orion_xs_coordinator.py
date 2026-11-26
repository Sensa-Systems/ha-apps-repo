"""Coordinator for a Sensa-owned Victron Orion XS DC-DC charger.

Subscribes to BLE adverts for one Orion XS MAC via HA's bluetooth
integration, decrypts each frame with the customer-supplied encryption
key, and pushes the decoded sensor values to entities via the standard
`DataUpdateCoordinator` plumbing.

Push-based by design — Orion XS adverts land every ~5 s on their own
cadence, so we don't poll. The coordinator's `update_interval` is set
long (5 min) purely to keep HA's "last update" timestamp fresh and to
serve as a staleness signal: if no advert has arrived for a while,
we publish an empty cache and entities mark themselves Unavailable.

Data shape returned to entities:

    {
        "device_state": int | None,
        "charger_error": int | None,
        "output_voltage": float | None,     # V
        "output_current": float | None,     # A
        "input_voltage": float | None,
        "input_current": float | None,
        "off_reason_bits": int | None,
        "off_reason_flags": list[str],
        "rssi": int | None,
        "last_advert_ts": float | None,      # time.monotonic() of last OK decode
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

from .victron_orion_xs import (
    OrionXsDecodeError,
    OrionXsKeyMismatchError,
    parse_orion_xs_advert,
)

_LOGGER = logging.getLogger(__name__)

# Orion XS broadcasts every ~5 s. A stream outage of 60 s means the
# customer has moved out of range / switched the charger off / the
# BT proxy is down — mark values Unavailable so the dashboard shows
# stale-data state rather than reporting last-known values as current.
_STALE_ADVERT_AFTER_SECS = 60.0

# Coordinator housekeeping tick. Push-based updates come through the
# BLE callback; this interval just fires `_async_update_data` so HA's
# "last updated" clock doesn't go stale and we can prune values on
# staleness even when no adverts arrive.
SCAN_INTERVAL = timedelta(seconds=30)


class VictronOrionXsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-based coordinator for one Orion XS BLE device."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        device_id: str,
        mac: str,
        encryption_key_hex: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"victron_orion_xs_{device_id}",
            update_interval=SCAN_INTERVAL,
        )
        self._device_id = device_id
        self._mac = mac.upper()
        self._encryption_key_hex = encryption_key_hex
        # Cached reading values. Kept across ticks so entities don't
        # flicker to Unavailable between advert landings (Victron's
        # Instant Readout cadence is ~5 s; gaps up to
        # `_STALE_ADVERT_AFTER_SECS` are expected and fine).
        self._values: dict[str, Any] = {}
        self._last_ok_ts: float | None = None
        self._unregister_ble: Callable[[], None] | None = None
        self._logged_key_mismatch = False

    async def async_start(self) -> None:
        """Register the BLE-advert callback + seed entity state.

        Seeds the data dict so entities created before the first
        advert have something to read — all values None, staleness
        flag in place. HA marks them Unavailable until a real frame
        lands."""
        try:
            from homeassistant.components.bluetooth import (
                BluetoothCallbackMatcher,
                BluetoothScanningMode,
                async_register_callback,
            )
        except ImportError:
            _LOGGER.error(
                "Orion XS %s: HA bluetooth integration not available; "
                "coordinator cannot start",
                self._device_id,
            )
            return

        def _on_advert(service_info: Any, change: Any) -> None:
            # Defensive — HA calls us on the event loop, but our
            # decode + set_updated_data is synchronous-safe.
            try:
                self._handle_service_info(service_info)
            except Exception:
                _LOGGER.exception(
                    "Orion XS %s: unhandled error in advert callback",
                    self._device_id,
                )

        matcher = BluetoothCallbackMatcher(address=self._mac)
        self._unregister_ble = async_register_callback(
            self.hass,
            _on_advert,
            matcher,
            BluetoothScanningMode.PASSIVE,
        )
        self.async_set_updated_data(dict(self._values))
        _LOGGER.info(
            "Orion XS %s: subscribed to BLE adverts for %s",
            self._device_id, self._mac,
        )

    async def async_stop(self) -> None:
        """Unregister the BLE callback. Called on coordinator teardown
        (integration unload, credential update, device removal)."""
        if self._unregister_ble is not None:
            try:
                self._unregister_ble()
            except Exception:
                _LOGGER.debug(
                    "Orion XS %s: unregister callback raised",
                    self._device_id, exc_info=True,
                )
            self._unregister_ble = None

    # ------------------------------------------------------------------
    # Internal plumbing
    # ------------------------------------------------------------------

    @callback
    def _handle_service_info(self, service_info: Any) -> None:
        """Decode one advert and push values to entities."""
        mfr_data = getattr(service_info, "manufacturer_data", None) or {}
        # Victron's company id — Orion XS advertises here.
        payload = mfr_data.get(0x02E1)
        if not payload:
            return

        try:
            reading = parse_orion_xs_advert(payload, self._encryption_key_hex)
        except OrionXsKeyMismatchError:
            # Log ONCE per coordinator life — a stream of wrong-key
            # adverts would otherwise flood the log every ~5 s.
            # Customer fixes this by re-entering the key on the device
            # card and saving, which restarts the coordinator and
            # resets this flag.
            if not self._logged_key_mismatch:
                self._logged_key_mismatch = True
                _LOGGER.warning(
                    "Orion XS %s: encryption key doesn't match — "
                    "re-enter the key from VictronConnect (Settings → "
                    "Product info → Instant readout via Bluetooth → "
                    "Show) and save.",
                    self._device_id,
                )
            return
        except OrionXsDecodeError as err:
            _LOGGER.debug(
                "Orion XS %s: discarding malformed advert: %s",
                self._device_id, err,
            )
            return

        # Successful decode — reset the key-mismatch flag in case
        # the coordinator was restarted with a fresh key and the
        # next frame is valid.
        self._logged_key_mismatch = False
        self._last_ok_ts = time.monotonic()
        self._values = {
            **reading.as_dict(),
            "rssi": getattr(service_info, "rssi", None),
            "last_advert_ts": self._last_ok_ts,
        }
        self.async_set_updated_data(dict(self._values))

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic tick — prune stale values, return current cache.

        No network I/O here; everything arrives via `_on_advert`.
        This hook exists so entities go Unavailable once an Orion
        XS stops broadcasting (out of range, powered off, encryption
        key changed without Sensa being told).
        """
        if self._last_ok_ts is None:
            return dict(self._values)
        if time.monotonic() - self._last_ok_ts > _STALE_ADVERT_AFTER_SECS:
            # Wipe sensor values so entities drop to Unavailable.
            # Keep the last_advert_ts so a diagnostic can tell how
            # long it's been since a frame landed.
            stale_ts = self._last_ok_ts
            self._values = {
                "device_state": None,
                "charger_error": None,
                "output_voltage": None,
                "output_current": None,
                "input_voltage": None,
                "input_current": None,
                "off_reason_bits": None,
                "off_reason_flags": [],
                "rssi": None,
                "last_advert_ts": stale_ts,
            }
        return dict(self._values)
