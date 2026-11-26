"""Data coordinator for Sensa Nomad backend data."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.client import SensaApiClient
from .const import CONF_BACKEND_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)


class SensaDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Sensa backend data."""

    def __init__(
        self,
        hass: HomeAssistant,
        backend_url: str,
    ) -> None:
        """Initialize coordinator."""
        self._client = SensaApiClient(hass, backend_url)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_coordinator",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the Sensa backend."""
        try:
            _LOGGER.debug("Sensa coordinator: fetching backend state")

            summary = await self._client.async_get_summary()
            discoveries = await self._client.async_get_discoveries()
            devices = await self._client.async_get_devices()

            data = {
                "summary": summary.get("summary", {}),
                "discoveries": discoveries.get("discoveries", []),
                "devices": devices.get("devices", []),
            }

            _LOGGER.debug(
                "Sensa coordinator: fetched summary=%s discoveries=%s devices=%s",
                list(data["summary"].keys()),
                len(data["discoveries"]),
                len(data["devices"]),
            )

            return data

        except (ConnectionError, TimeoutError) as err:
            # Add-on unreachable / slow (restarting, or stopped in dev). Expected
            # and transient — HA's DataUpdateCoordinator already logs the
            # UpdateFailed message once (then dedups to DEBUG, INFO on recovery),
            # so don't pile on a redundant ERROR + full aiohttp traceback here.
            _LOGGER.debug("Sensa coordinator update failed: %s", err)
            raise UpdateFailed(f"Sensa backend update failed: {err}") from err
        except Exception as err:
            # Genuinely unexpected — keep the traceback so real bugs stay visible.
            _LOGGER.exception("Sensa coordinator update failed")
            raise UpdateFailed(f"Sensa backend update failed: {err}") from err


async def async_get_coordinator(
    hass: HomeAssistant,
    entry,
) -> SensaDataUpdateCoordinator:
    """Create a coordinator for a config entry without failing setup if backend is late."""
    backend_url = entry.data[CONF_BACKEND_URL]
    coordinator = SensaDataUpdateCoordinator(hass, backend_url)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        # This function deliberately does NOT fail setup when the backend is
        # late (add-on still starting / updating) — that's its whole point — so
        # a failed first refresh is the EXPECTED path, not a warning. The
        # recurring update still surfaces a genuinely persistent fault.
        _LOGGER.debug(
            "Sensa coordinator initial refresh deferred; seeding empty data and "
            "retrying on the next interval: %s",
            err,
        )
        coordinator.async_set_updated_data(
            {
                "summary": {},
                "discoveries": [],
                "devices": [],
            }
        )

    return coordinator
