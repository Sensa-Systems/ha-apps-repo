"""Home Assistant side of BLE Capture: WS commands, live status, shutdown.

The session itself is :mod:`.ble_capture`, all blocking, so every call into it
goes through the executor. The card (``custom:sensa-ble-capture-card`` on
the Discovery picker with debug on) subscribes to ``EVENT_BLE_CAPTURE_CHANGED`` through
``sensa_nomad/subscribe`` and re-reads ``sensa_nomad/ble_capture_status``.

Every command is gated on ``user_is_sensa_admin`` (the owner, an HA admin or a
live Settings-PIN elevation): the customer runs the capture themselves. The
bundle is downloaded the same way as the emergency kit, over the signed-in
websocket, base64 in the result.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import voluptuous as vol

from .const import DOMAIN, EVENT_BLE_CAPTURE_CHANGED, SENSA_LOG_SOURCE_SYSTEM

_LOGGER = logging.getLogger(__name__)

_DATA_KEY = "_sensa_ble_capture_manager"


def _manager(hass: Any) -> Any:
    return hass.data.get(DOMAIN, {}).get(_DATA_KEY)


def async_setup_ble_capture(hass: Any) -> None:
    """Create the manager and register the WS commands, once per HA run."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_DATA_KEY) is not None:
        return
    from homeassistant.components import websocket_api
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.core import callback

    from .appliance import user_is_sensa_admin
    from .ble_capture import (
        MARKERS,
        STATE_ERROR,
        STATE_STOPPED,
        CaptureError,
        CaptureManager,
    )

    last_state: dict[str, str | None] = {"state": None}

    @callback
    def _changed(status: dict[str, Any]) -> None:
        hass.bus.async_fire(EVENT_BLE_CAPTURE_CHANGED)
        state = status.get("state")
        if state == last_state["state"]:
            return
        last_state["state"] = state
        # On the edge only: a start and its outcome, never every packet.
        try:
            from ._log import sensa_log

            label = status.get("name") or status.get("address")
            if state == "starting":
                sensa_log(hass, "info", f"Bluetooth capture started for {label}",
                          source=SENSA_LOG_SOURCE_SYSTEM)
            elif state == STATE_STOPPED:
                sensa_log(hass, "info",
                          f"Bluetooth capture of {label} finished: {status.get('detail')}",
                          source=SENSA_LOG_SOURCE_SYSTEM)
            elif state == STATE_ERROR:
                sensa_log(hass, "warning",
                          f"Bluetooth capture of {label} failed: {status.get('detail')}",
                          source=SENSA_LOG_SOURCE_SYSTEM)
        except Exception:  # noqa: BLE001 - the log is a convenience, never fatal
            _LOGGER.debug("BLE capture: sensa_log failed", exc_info=True)

    def _on_change(status: dict[str, Any]) -> None:
        # Called from the driver, timer and executor threads.
        hass.loop.call_soon_threadsafe(_changed, status)

    manager = CaptureManager(
        hass.config.path("sensa", "ble_capture"), on_change=_on_change,
    )
    domain_data[_DATA_KEY] = manager

    async def _on_stop(_event: Any) -> None:
        session = manager.session
        if session is not None and session.active:
            await hass.async_add_executor_job(manager.stop, "shutdown")

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)

    def _allowed(connection: Any, msg: dict[str, Any]) -> bool:
        if user_is_sensa_admin(hass, connection.user):
            return True
        connection.send_error(
            msg["id"], "unauthorized", "Only the account admin can record Bluetooth.",
        )
        return False

    @websocket_api.websocket_command({vol.Required("type"): "sensa_nomad/ble_capture_status"})
    @websocket_api.async_response
    async def _ws_status(hass, connection, msg):
        if not _allowed(connection, msg):
            return
        status = await hass.async_add_executor_job(manager.status)
        status["dongle"] = await hass.async_add_executor_job(manager.dongle)
        connection.send_result(msg["id"], status)

    @websocket_api.websocket_command({
        vol.Required("type"): "sensa_nomad/ble_capture_start",
        vol.Required("address"): str,
        vol.Optional("name", default=""): str,
        vol.Optional("max_minutes", default=15): vol.All(int, vol.Range(min=1, max=60)),
    })
    @websocket_api.async_response
    async def _ws_start(hass, connection, msg):
        if not _allowed(connection, msg):
            return
        try:
            status = await hass.async_add_executor_job(
                manager.start, msg["address"], msg["name"], msg["max_minutes"] * 60,
            )
        except CaptureError as err:
            connection.send_error(msg["id"], "capture_failed", str(err))
            return
        connection.send_result(msg["id"], status)

    @websocket_api.websocket_command({vol.Required("type"): "sensa_nomad/ble_capture_stop"})
    @websocket_api.async_response
    async def _ws_stop(hass, connection, msg):
        if not _allowed(connection, msg):
            return
        connection.send_result(msg["id"], await hass.async_add_executor_job(manager.stop))

    @websocket_api.websocket_command({
        vol.Required("type"): "sensa_nomad/ble_capture_mark",
        vol.Required("marker"): vol.In(sorted(MARKERS)),
    })
    @websocket_api.async_response
    async def _ws_mark(hass, connection, msg):
        if not _allowed(connection, msg):
            return
        try:
            entry = await hass.async_add_executor_job(manager.mark, msg["marker"])
        except CaptureError as err:
            connection.send_error(msg["id"], "not_running", str(err))
            return
        connection.send_result(msg["id"], entry)

    @websocket_api.websocket_command({
        vol.Required("type"): "sensa_nomad/ble_capture_download",
        vol.Required("session_id"): str,
    })
    @websocket_api.async_response
    async def _ws_download(hass, connection, msg):
        if not _allowed(connection, msg):
            return

        def _build() -> dict[str, Any]:
            filename, data = manager.bundle(msg["session_id"])
            return {"filename": filename, "data_b64": base64.b64encode(data).decode("ascii")}

        try:
            result = await hass.async_add_executor_job(_build)
        except CaptureError as err:
            connection.send_error(msg["id"], "not_found", str(err))
            return
        connection.send_result(msg["id"], result)

    for handler in (_ws_status, _ws_start, _ws_stop, _ws_mark, _ws_download):
        websocket_api.async_register_command(hass, handler)
