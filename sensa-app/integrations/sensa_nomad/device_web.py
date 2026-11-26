"""Open a Sensa ESP device's own web page, live log included, through the box.

A Sensa ESP device (bridge, relay, Van Level, heater, fan) serves its own
ESPHome web page with a live log, but only on the van's LAN. Support reaches the
van through its support address, not its LAN, so on 2026-10-14 a FreshJet bridge
that could have said why it was not reaching its AC could not be read at all.

This passes that page through Home Assistant:

* `sensa_nomad/device_web_open {mac}` (Sensa support only, `user_is_sensa_staff`,
  never `can_admin`: the page has Restart, Factory Reset and Forget on it) mints
  a random link to that one device, good for `LINK_LIFETIME`.
* `/api/sensa_nomad/device_web/<token>/...` forwards to the device's port 80
  with the web login every Sensa firmware shares, and streams `/events` (the
  live log) back. The ESPHome v3 page builds every URL from its own path
  (`getBasePath()` is `location.pathname`) and `local: true` inlines the whole
  UI, so it works under this prefix unchanged.

The token is the only credential on the proxy route: a browser tab cannot send
HA's bearer, so the view is `requires_auth = False` and refuses anything without
a live token minted by a staff login.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

URL_PREFIX = "/api/sensa_nomad/device_web"
LINK_LIFETIME = 3600  # seconds
_TOKENS_KEY = "sensa_nomad_device_web_tokens"
_SESSION_KEY = "sensa_nomad_device_web_session"
_REGISTERED_KEY = "sensa_nomad_device_web_registered"

# Response headers passed back from the device. Content-Encoding is kept: the
# page is sent pre-compressed and is forwarded byte for byte.
_PASS_HEADERS = ("Content-Type", "Content-Encoding", "Content-Disposition")


def _norm_mac(mac: Any) -> str:
    return "".join(c for c in str(mac or "").lower() if c in "0123456789abcdef")


def _devices(hass: Any) -> list[dict[str, Any]]:
    from .const import DOMAIN

    out: list[dict[str, Any]] = []
    for runtime in (hass.data.get(DOMAIN) or {}).values():
        coordinator = runtime.get("coordinator") if isinstance(runtime, dict) else None
        data = getattr(coordinator, "data", None)
        devices = data.get("devices") if isinstance(data, dict) else None
        if isinstance(devices, list):
            out.extend(d for d in devices if isinstance(d, dict))
    return out


def device_address_for_mac(hass: Any, mac: str) -> str | None:
    """The LAN address of the registered ESPHome device with this MAC, or None."""
    from .esp_update import device_address, esphome_entry

    want = _norm_mac(mac)
    if len(want) != 12:
        return None
    for dev in _devices(hass):
        if _norm_mac(dev.get("mac_address")) != want:
            continue
        integration = dev.get("integration_type") or dev.get("integration") or ""
        if integration != "esphome":
            continue
        address = device_address(dev, esphome_entry(hass, dev))
        if address:
            return address
    return None


def mint_link(hass: Any, address: str, *, now: float | None = None) -> str:
    stamp = time.monotonic() if now is None else now
    tokens: dict[str, tuple[str, float]] = hass.data.setdefault(_TOKENS_KEY, {})
    for key in [k for k, (_a, exp) in tokens.items() if exp <= stamp]:
        tokens.pop(key, None)
    token = secrets.token_urlsafe(24)
    tokens[token] = (address, stamp + LINK_LIFETIME)
    return f"{URL_PREFIX}/{token}/"


def address_for_token(hass: Any, token: str, *, now: float | None = None) -> str | None:
    stamp = time.monotonic() if now is None else now
    entry = (hass.data.get(_TOKENS_KEY) or {}).get(token)
    if entry is None or entry[1] <= stamp:
        return None
    return entry[0]


def _session(hass: Any) -> Any:
    """One client session that does NOT decompress, so the page's own
    Content-Encoding stays true for the bytes we pass on."""
    import aiohttp

    session = hass.data.get(_SESSION_KEY)
    if session is None or session.closed:
        session = aiohttp.ClientSession(auto_decompress=False)
        hass.data[_SESSION_KEY] = session

        async def _close(_event: Any) -> None:
            await session.close()

        hass.bus.async_listen_once("homeassistant_stop", _close)
    return session


def async_setup(hass: Any) -> None:
    """Register the WS command and the proxy view, once per process."""
    if hass.data.get(_REGISTERED_KEY):
        return
    hass.data[_REGISTERED_KEY] = True

    import voluptuous as vol
    from aiohttp import ClientError, ClientTimeout, web
    from homeassistant.components import websocket_api
    from homeassistant.components.http import HomeAssistantView

    from .appliance import user_is_sensa_staff
    from .esp_update import _WEB_PASSWORD, _WEB_USER

    @websocket_api.websocket_command({
        vol.Required("type"): "sensa_nomad/device_web_open",
        vol.Required("mac"): str,
    })
    @websocket_api.async_response
    async def _ws_open(hass, connection, msg):
        if not user_is_sensa_staff(hass, connection.user):
            connection.send_error(msg["id"], "unauthorized", "Sensa support only.")
            return
        address = device_address_for_mac(hass, msg["mac"])
        if not address:
            connection.send_error(
                msg["id"], "not_found",
                "This device's address on the van network is not known.",
            )
            return
        connection.send_result(msg["id"], {"url": mint_link(hass, address)})

    websocket_api.async_register_command(hass, _ws_open)

    class DeviceWebView(HomeAssistantView):
        url = URL_PREFIX + "/{token}/{path:.*}"
        name = "api:sensa_nomad:device_web"
        requires_auth = False  # the token is the credential, see the module doc

        def __init__(self, hass):
            self._hass = hass

        async def get(self, request, token, path):
            return await self._proxy(request, token, path)

        async def post(self, request, token, path):
            return await self._proxy(request, token, path)

        async def _proxy(self, request, token, path):
            hass = self._hass
            address = address_for_token(hass, token)
            if address is None:
                return web.Response(status=404, text="This device link has expired.")
            import aiohttp

            events = path == "events"
            body = await request.read() if request.method == "POST" else None
            headers = {}
            if "Content-Type" in request.headers:
                headers["Content-Type"] = request.headers["Content-Type"]
            url = f"http://{address}/{path}"
            if request.query_string:
                url += "?" + request.query_string
            try:
                upstream = await _session(hass).request(
                    request.method, url, data=body, headers=headers,
                    auth=aiohttp.BasicAuth(_WEB_USER, _WEB_PASSWORD),
                    allow_redirects=False,
                    # The live log never ends; everything else should be quick.
                    timeout=ClientTimeout(total=None, sock_read=None) if events
                    else ClientTimeout(total=60),
                )
            except (ClientError, TimeoutError) as err:
                return web.Response(
                    status=502, text=f"The device at {address} did not answer: {err}",
                )
            out = web.StreamResponse(status=upstream.status)
            for name in _PASS_HEADERS:
                if name in upstream.headers:
                    out.headers[name] = upstream.headers[name]
            out.headers["Cache-Control"] = "no-store"
            await out.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await out.write(chunk)
            except (ClientError, ConnectionResetError):
                pass
            finally:
                upstream.release()
            return out

    hass.http.register_view(DeviceWebView(hass))
