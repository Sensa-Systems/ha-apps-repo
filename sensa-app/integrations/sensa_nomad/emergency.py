"""SOS: the gate that shows the scene, and the alert when it is pressed.

Hub's ``emergency`` feature gate decides whether this van offers SOS at all
(`modes_catalog.GATED_MODES`). The App caches Hub's answer on its phone-home;
this polls the App's cache and records it on the overrides store, which leaves
SOS out of the scene list every card renders from while the gate is off.

Pressing SOS asks the App to send the alert. The App, not the integration, owns
delivery: it holds the licence that authenticates to Hub and keeps retrying
until Hub answers, so a press with no signal still reaches the contacts once
the van is back in range. Hub emails the contacts and the account.
"""

from __future__ import annotations

import logging
import socket
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import SIGNAL_MODES_CHANGED

_LOGGER = logging.getLogger(__name__)

GATE = "emergency"
SOS_SLUG = "sos"

_POLL = timedelta(seconds=60)


async def _app(hass: HomeAssistant, method: str, path: str) -> dict[str, Any] | None:
    """One call to the App. None when it could not be asked."""
    import aiohttp

    from .backend_url import resolve_backend_url

    try:
        base = resolve_backend_url(hass)
    except Exception:  # noqa: BLE001
        return None
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session, session.request(
            method, f"{base.rstrip('/')}{path}", json={} if method == "POST" else None
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                _LOGGER.warning("emergency: App %s %s answered %s", method, path, resp.status)
                return None
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        _LOGGER.debug("emergency: App %s %s failed", method, path, exc_info=True)
        return None


async def async_fetch_gate(hass: HomeAssistant) -> bool | None:
    """Hub's answer as the App last heard it. None = could not ask, or the App
    has never been told: keep what we had."""
    data = await _app(hass, "GET", "/api/v1/emergency")
    if data is None or not data.get("known"):
        return None
    return bool(data.get("enabled"))


async def async_send_sos(hass: HomeAssistant) -> bool:
    """Ask the App to alert the emergency contacts. True once the App has
    queued it; delivery to Hub is the App's job from there."""
    data = await _app(hass, "POST", "/api/v1/emergency/sos")
    if data is None:
        _LOGGER.error("SOS: the App could not be asked to alert the emergency contacts")
        return False
    _LOGGER.warning("SOS pressed: alerting the emergency contacts (%s)", data.get("sos_id"))
    return True


def async_start_gate_poll(hass: HomeAssistant, entry: ConfigEntry, overrides: Any) -> None:
    """Follow the gate: re-publish the scene list whenever it flips."""

    async def _tick(_now: Any = None) -> None:
        on = await async_fetch_gate(hass)
        if on is None:
            return
        if await overrides.async_set_feature_gate(GATE, on):
            _LOGGER.info("emergency: SOS %s", "offered" if on else "withdrawn")
            async_dispatcher_send(hass, SIGNAL_MODES_CHANGED, overrides.modes_payload())

    entry.async_on_unload(async_track_time_interval(hass, _tick, _POLL))
    hass.async_create_task(_tick())
