"""Camera platform for Sensa Nomad: the live picture of a registered IP camera.

One ``camera.*`` entity per registered IP camera (catalog ``ip_camera``,
integration ``sensa_rtsp_camera``). Home Assistant's own ``stream`` component
turns the RTSP source into HLS for the browser, so the Habitation Dump tab's
``custom:sensa-camera-card`` only has to ask for ``camera/stream`` and play it.

The source is Frigate's restream when Frigate is on this box (the Container
stack at Pro and above, see ``frigate_seed.py``), and the camera's own RTSP
URL otherwise, so the picture shows on a box whose stack has not caught up
yet and on a HAOS box that never runs Frigate. The choice is made per stream
start and cached briefly; nothing is baked in.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import frigate_seed
from .const import DOMAIN, IP_CAMERA_IDENTIFIER_PREFIX, ip_camera_entity_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the camera platform.

    Entities are created by the vendor start path (``_async_start_ip_camera``
    in ``__init__``), which runs for every registered camera at startup and on
    add. This only registers the add callback and drains anything queued
    before the platform was ready, the same deal as every other platform.
    """
    runtime = hass.data[DOMAIN][entry.entry_id]
    runtime["vendor_add_camera"] = async_add_entities
    pending = runtime.pop("vendor_pending_camera", None)
    if pending:
        _LOGGER.info(
            "Sensa Nomad camera setup: flushing %d camera entity(ies) queued "
            "before the platform was ready", len(pending),
        )
        async_add_entities(pending)


def build_camera_entities(entry: ConfigEntry, device: dict[str, Any]) -> list[Camera]:
    """The camera entity for one registered IP camera."""
    return [SensaIpCamera(entry, device)]


class SensaIpCamera(Camera):
    """The live stream of one IP camera, through Frigate where it runs."""

    _attr_has_entity_name = True
    _attr_name = "Live"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: ConfigEntry, device: dict[str, Any]) -> None:
        super().__init__()
        self._entry = entry
        self._device = dict(device)
        self._device_id = str(device.get("device_id") or "")
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_camera"
        # Pinned entity_id: the Dump tab's card names it without a registry
        # lookup, the same rule as the Velit climate entity.
        self.entity_id = ip_camera_entity_id(self._device_id)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{IP_CAMERA_IDENTIFIER_PREFIX}{self._device_id}")},
            "name": self._device.get("display_name") or self._device.get("name") or "IP Camera",
            "manufacturer": self._device.get("vendor") or "IP Camera",
            "model": self._device.get("model") or "RTSP camera",
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_id": self._device_id,
            "host": (self._device.get("config") or {}).get("host") or self._device.get("ip_address") or "",
            "restream": frigate_seed.restream_url(self._device),
        }

    def update_device(self, device: dict[str, Any]) -> None:
        """Take a fresh device record (the config changed) and drop the
        running stream so the next start reads the new source."""
        self._device = dict(device)
        frigate_seed.reset_restream_cache()
        self.hass.async_create_task(self._async_drop_stream())

    async def _async_drop_stream(self) -> None:
        stream = self.stream
        self.stream = None
        if stream is not None:
            try:
                await stream.stop()
            except Exception:
                _LOGGER.debug("Camera %s: old stream did not stop cleanly", self.entity_id, exc_info=True)
        self.async_write_ha_state()

    async def stream_source(self) -> str | None:
        """Frigate's restream when Frigate answers on this box, else the
        camera itself. Decided at stream start, never baked in."""
        if await frigate_seed.async_restream_reachable():
            return frigate_seed.restream_url(self._device)
        direct = frigate_seed.camera_rtsp_url(self._device)
        _LOGGER.debug("Camera %s: Frigate restream not reachable, reading the camera directly", self.entity_id)
        return direct or None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None,
    ) -> bytes | None:
        """A still from the live stream (the entity picture, snapshots).
        Same approach as HA's generic camera: pull a frame off the stream
        rather than run a second decoder."""
        stream = self.stream
        if stream is None:
            stream = await self.async_create_stream()
        if stream is None:
            return None
        try:
            return await stream.async_get_image(width, height)
        except Exception:
            _LOGGER.debug("Camera %s: no still available", self.entity_id, exc_info=True)
            return None
