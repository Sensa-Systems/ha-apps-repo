"""Data coordinator for Starlink dish polling.

Reads gRPC responses from the dish, normalises into a single sample dict,
then runs a one-shot enrichment on first successful poll: classifies the
dish model from `hardware_version` (or the `DishyHWGeneration` enum when
the dish exposes it) using rules declared in `device_catalog.json`, and
writes the result back to the Sensa backend so the device picker can
swap in the correct product image.

Classification is catalog-driven — see the `model_detection` block in
`device_catalog.json` for the starlink_terminal entry. Adding a new
Starlink variant is a JSON edit (new enum value + prefix + display
name + model_images row), not a code change.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .starlink_grpc import (
    StarlinkConnectionError,
    StarlinkError,
    StarlinkGrpcClient,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)
HISTORY_SCAN_INTERVAL = timedelta(seconds=15)


class StarlinkHistoryCoordinator(DataUpdateCoordinator):
    """Faster coordinator dedicated to the DishGetHistory call.

    Powers the Power and Packet Drop Rate sensors at 15s while the
    main coordinator stays on 30s for the heavier status / diagnostics
    / device-info polls. The history call is the cheapest of the four
    (single packed-fixed32 response, no nested message walk), so
    doubling its rate is comfortable for the dish.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: StarlinkGrpcClient,
        device_id: str,
    ) -> None:
        self.client = client
        self.device_id = device_id
        super().__init__(
            hass,
            _LOGGER,
            name=f"starlink_history_{device_id}",
            update_interval=HISTORY_SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.client.async_get_starlink_history()
        except StarlinkConnectionError as err:
            raise UpdateFailed(f"History connection failed: {err}") from err
        except StarlinkError as err:
            raise UpdateFailed(f"History error: {err}") from err


def _starlink_catalog_entry() -> dict[str, Any] | None:
    """Return the starlink_terminal entry from the device catalog, or None."""
    from ..device_catalog import PHYSICAL_DEVICE_CATALOG

    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("integration") == "sensa_starlink_grpc":
            return entry
    return None


def classify_starlink_model(
    *,
    hardware_version: str | None = None,
    hw_generation: str | None = None,
) -> tuple[str | None, str | None]:
    """Map a dish's hardware identifiers to a catalog model slug + display name.

    Resolution order:
      1. `hw_generation` matched against `model_detection.hw_generation_enum`
         (preferred — stable across firmware revisions because SpaceX keeps
         the enum value even when they rename the free-text hardware string).
      2. `hardware_version` matched by prefix against
         `model_detection.hw_version_prefixes` (fallback — free-text string,
         but we match on the leading token so revision / region suffixes
         like `_proto1`, `_v1`, `_maritime` don't break the match).

    Returns `(slug, display_name)`. Either can be None if no rule matches —
    the caller is responsible for deciding what to do with a miss (log, skip
    enrichment, keep the generic image, etc.).

    Adding a new dish model is a catalog-only change — no code edits here.
    """
    entry = _starlink_catalog_entry()
    if entry is None:
        return (None, None)

    detection = entry.get("model_detection") or {}
    enum_map = detection.get("hw_generation_enum") or {}
    prefix_map = detection.get("hw_version_prefixes") or {}
    display_names = detection.get("display_names") or {}

    slug: str | None = None

    gen = (hw_generation or "").strip().upper()
    if gen and gen != "UNKNOWN":
        slug = enum_map.get(gen)

    if not slug:
        hw = (hardware_version or "").strip().lower()
        if hw:
            # Longest prefix wins — guards against a future nested prefix
            # like `mini1b_` overlapping `mini1_`.
            for prefix in sorted(prefix_map, key=len, reverse=True):
                if hw.startswith(prefix):
                    slug = prefix_map[prefix]
                    break

    display = display_names.get(slug) if slug else None
    return (slug, display)


class StarlinkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls a Starlink dish via gRPC."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: StarlinkGrpcClient,
        device_id: str,
        *,
        fetch_gps: bool = False,
        sensa_client: Any | None = None,
        name_locked: bool = False,
    ) -> None:
        self.client = client
        self.device_id = device_id
        self._fetch_gps = fetch_gps
        self._sensa_client = sensa_client
        # True once the customer has renamed this dish. `_enriched` only
        # lives as long as this coordinator object, so without a durable
        # flag read from the backend record every HA restart re-ran the
        # classification and wrote the catalog name straight over the
        # customer's ("Starlink" reverting to "Starlink Mini" minutes
        # later). See `_maybe_enrich`.
        self._name_locked = name_locked
        self._enriched = False
        super().__init__(
            hass,
            _LOGGER,
            name=f"starlink_{device_id}",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the Starlink dish."""
        try:
            data = await self.client.async_get_starlink_data()

            if self._fetch_gps:
                gps = await self.client.async_get_location()
                data.update(gps)
        except StarlinkConnectionError as err:
            raise UpdateFailed(f"Connection failed: {err}") from err
        except StarlinkError as err:
            raise UpdateFailed(f"Starlink error: {err}") from err

        if not self._enriched and self._sensa_client is not None:
            await self._maybe_enrich(data)

        return data

    async def _maybe_enrich(self, data: dict[str, Any]) -> None:
        """One-shot: classify the dish and write model metadata to the backend.

        Runs after the first successful poll. Fires the Sensa backend's
        `async_update_device` with the classified slug in `config.model` —
        that's the field `_pick_image` reads to swap the generic dish image
        for the model-specific one (e.g. `starlink-mini.webp`).

        Also pushes the raw hardware / software version strings as diagnostic
        metadata. Guarded so the enrichment only happens once per coordinator
        lifetime; on classifier miss we still mark enriched so we don't hammer
        the backend on every poll.

        The display name is the ONE field a customer can own, so it is
        written only while `_name_locked` is False. The config half still
        runs either way: the model slug drives the product image, and a
        rename must not freeze that at whatever was known on rename day.
        """
        hw = data.get("hardware_version") or data.get("diag_hardware_version")
        gen = data.get("hw_generation") or data.get("dish_generation")

        slug, display_name = classify_starlink_model(
            hardware_version=hw,
            hw_generation=gen,
        )

        config_update: dict[str, Any] = {}
        if slug:
            config_update["model"] = slug
        if hw:
            config_update["hardware_version_raw"] = str(hw)
        sw = data.get("software_version") or data.get("diag_software_version")
        if sw:
            config_update["software_version_raw"] = str(sw)

        if not config_update:
            _LOGGER.info(
                "Starlink %s: no hardware/software info in sample yet; "
                "enrichment deferred",
                self.device_id,
            )
            return

        write_name = bool(display_name) and not self._name_locked
        try:
            await self._sensa_client.async_update_device(
                device_id=self.device_id,
                config=config_update,
                **({"display_name": display_name} if write_name else {}),
            )
        except Exception:
            _LOGGER.warning(
                "Starlink %s: enrichment write failed; will retry next poll",
                self.device_id,
                exc_info=True,
            )
            return

        self._enriched = True
        _LOGGER.info(
            "Starlink %s: enriched as model=%s display=%s hw=%s",
            self.device_id,
            slug or "unknown",
            display_name if write_name else "(kept)",
            hw or "(unknown)",
        )
