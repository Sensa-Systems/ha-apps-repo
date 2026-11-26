"""The Nomad phone app's API: sign-in, tokens, registration, location, push.

The phone app talks only to these endpoints, never to HA's own auth,
`mobile_app` or webhook APIs, so the app is written once and does not change
when HA goes (`docs/system-design/ha-removal-roadmap.md`). Today each endpoint
is a thin adapter over HA:

- The tokens are HA tokens, because the screens are still HA's frontend in the
  app's WebView (external auth).
- A phone is still an HA `mobile_app` registration, so its `device_tracker`,
  `notify.notify`, the People tab and the driver picker work unchanged. The
  device key handed to the app is that registration's webhook id.

All under ``/api/sensa/mobile/``:

    GET  ping               no auth   {"app": "sensa"}; the app's reachability probe
    POST sign_in            no auth   {username, password} -> tokens + addresses
    POST token              no auth   {refresh_token} -> {access_token, expires_in}
    POST sign_out           no auth   {refresh_token} -> {}
    GET  addresses          bearer    {local_url, remote_url}
    POST register           bearer    this phone -> {device_key}
    POST location           key       {device_key, latitude, longitude, accuracy, ...}
    GET  notifications      WebSocket first message {type: auth, access_token, device_key}
    POST test_notification  bearer    {title?, message} through notify.notify

Notifications WebSocket, after ``auth_ok``: the box sends
``{type: notification, id, title, message, data}``; the app answers
``{type: confirm, id}``. An unconfirmed notification falls back after 10 s to
the push relay (Licensing ``/v1/push``, through Firebase), as does every
notification while the app is closed.
"""
from __future__ import annotations

import logging
import math
import secrets
from typing import Any

_LOGGER = logging.getLogger(__name__)

URL_PREFIX = "/api/sensa/mobile"

# The client every app refresh token is bound to. Fixed rather than the address
# signed in at, so one token refreshes over the LAN and through the tunnel.
CLIENT_ID = "https://sensa.systems/nomad-app/"
CLIENT_NAME = "Nomad app"

APP_ID = "com.sensasystems.nomad"
APP_NAME = "Nomad"

_REGISTERED_KEY = "sensa_nomad_mobile_api_registered"


def _addresses(hass: Any) -> dict[str, str | None]:
    """The box's LAN and remote addresses. The box writes the tunnel hostname
    into `external_url` (`_external_url.py`)."""
    def trim(url: str | None) -> str | None:
        return url.rstrip("/") if url else None

    return {
        "local_url": trim(hass.config.internal_url),
        "remote_url": trim(hass.config.external_url),
    }


async def _push_url(hass: Any) -> str | None:
    """The push relay of this box's channel, from the App, which is the one
    place the Licensing URL lives. None when the App cannot be asked: the
    phone then gets notifications only while the app is open."""
    from .api.client import SensaApiClient
    from .backend_url import resolve_backend_url

    try:
        body = await SensaApiClient(hass, resolve_backend_url(hass)).async_get_firmware(codes=[])
    except Exception as err:  # noqa: BLE001 - any failure means "could not ask"
        _LOGGER.warning("Push relay unknown, the App did not answer: %s", err)
        return None
    base = str((body or {}).get("base_url") or "").rstrip("/")
    return f"{base}/push" if base else None


def async_setup(hass: Any) -> None:
    """Register the views, once per process."""
    if hass.data.get(_REGISTERED_KEY):
        return
    hass.data[_REGISTERED_KEY] = True

    import voluptuous as vol
    from aiohttp import WSMsgType, web
    from homeassistant.auth import InvalidAuthError
    from homeassistant.auth.models import TOKEN_TYPE_NORMAL
    from homeassistant.auth.providers.homeassistant import InvalidAuth
    from homeassistant.components.http import HomeAssistantView
    from homeassistant.components.http.auth_util import async_user_not_allowed_do_auth
    from homeassistant.components.http.ban import (
        log_invalid_auth,
        process_success_login,
    )
    from homeassistant.components.mobile_app.const import (
        DATA_CONFIG_ENTRIES,
        DATA_PUSH_CHANNEL,
        SIGNAL_LOCATION_UPDATE,
    )
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.dispatcher import async_dispatcher_send
    from homeassistant.setup import async_setup_component
    from homeassistant.util import slugify

    def _error(code: str, status: int, description: str | None = None) -> web.Response:
        body = {"error": code}
        if description:
            body["error_description"] = description
        return web.json_response(body, status=status)

    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _access(refresh_token: Any, request: web.Request) -> dict[str, Any]:
        return {
            "access_token": hass.auth.async_create_access_token(refresh_token, request.remote),
            "expires_in": int(refresh_token.access_token_expiration.total_seconds()),
        }

    def _app_refresh_token(token: str) -> Any:
        """A refresh token this API issued, or None. Tokens of HA's own clients
        are refused, so this endpoint cannot mint access for them."""
        rt = hass.auth.async_get_refresh_token_by_token(token) if token else None
        return rt if rt is not None and rt.client_id == CLIENT_ID else None

    def _registration(device_key: str) -> Any:
        """The `mobile_app` entry this device key (its webhook id) names, or None."""
        entries = (hass.data.get("mobile_app") or {}).get(DATA_CONFIG_ENTRIES) or {}
        return entries.get(device_key) if device_key else None

    class PingView(HomeAssistantView):
        url = URL_PREFIX + "/ping"
        name = "api:sensa:mobile:ping"
        requires_auth = False

        async def get(self, request):
            return self.json({"app": "sensa"})

    class SignInView(HomeAssistantView):
        url = URL_PREFIX + "/sign_in"
        name = "api:sensa:mobile:sign_in"
        requires_auth = False

        @log_invalid_auth
        async def post(self, request):
            body = await _json(request)
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            provider = next(
                (p for p in hass.auth.auth_providers if p.type == "homeassistant"), None,
            )
            if provider is None or not username or not password:
                return _error("invalid_auth", 401)
            try:
                await provider.async_validate_login(username, password)
            except InvalidAuth:
                return _error("invalid_auth", 401)
            credentials = await provider.async_get_or_create_credentials({"username": username})
            user = await hass.auth.async_get_or_create_user(credentials)
            if reason := async_user_not_allowed_do_auth(hass, user, request):
                return _error("access_denied", 403, reason)
            refresh_token = await hass.auth.async_create_refresh_token(
                user, CLIENT_ID, client_name=CLIENT_NAME,
                token_type=TOKEN_TYPE_NORMAL, credential=credentials,
            )
            process_success_login(request)
            return self.json({
                **_access(refresh_token, request),
                "refresh_token": refresh_token.token,
                **_addresses(hass),
            })

    class TokenView(HomeAssistantView):
        url = URL_PREFIX + "/token"
        name = "api:sensa:mobile:token"
        requires_auth = False

        @log_invalid_auth
        async def post(self, request):
            body = await _json(request)
            refresh_token = _app_refresh_token(str(body.get("refresh_token") or ""))
            if refresh_token is None:
                return _error("invalid_grant", 400)
            if reason := async_user_not_allowed_do_auth(hass, refresh_token.user, request):
                return _error("access_denied", 403, reason)
            try:
                return self.json(_access(refresh_token, request))
            except InvalidAuthError as err:
                return _error("access_denied", 403, str(err))

    class SignOutView(HomeAssistantView):
        url = URL_PREFIX + "/sign_out"
        name = "api:sensa:mobile:sign_out"
        requires_auth = False

        async def post(self, request):
            body = await _json(request)
            refresh_token = _app_refresh_token(str(body.get("refresh_token") or ""))
            if refresh_token is not None:
                hass.auth.async_remove_refresh_token(refresh_token)
            return self.json({})

    class AddressesView(HomeAssistantView):
        url = URL_PREFIX + "/addresses"
        name = "api:sensa:mobile:addresses"

        async def get(self, request):
            return self.json(_addresses(hass))

    register_schema = vol.Schema({
        vol.Required("device_id"): vol.All(str, vol.Length(min=1)),
        vol.Required("device_name"): str,
        vol.Required("manufacturer"): str,
        vol.Required("model"): str,
        vol.Required("os_name"): str,
        vol.Optional("os_version", default=""): str,
        vol.Required("app_version"): str,
        vol.Optional("push_token"): vol.Any(None, str),
    }, extra=vol.REMOVE_EXTRA)

    class RegisterView(HomeAssistantView):
        url = URL_PREFIX + "/register"
        name = "api:sensa:mobile:register"

        async def post(self, request):
            try:
                phone = register_schema(await _json(request))
            except vol.Invalid as err:
                return _error("invalid_request", 400, str(err))
            if "mobile_app" not in hass.config.components:
                await async_setup_component(hass, "mobile_app", {})
            app_data: dict[str, Any] = {"push_websocket_channel": True}
            if phone.get("push_token") and (push_url := await _push_url(hass)):
                app_data.update(push_url=push_url, push_token=phone["push_token"])
            webhook_id = secrets.token_hex()
            data = {
                "app_id": APP_ID,
                "app_name": APP_NAME,
                "app_version": phone["app_version"],
                "device_id": phone["device_id"],
                "device_name": phone["device_name"],
                "manufacturer": phone["manufacturer"],
                "model": phone["model"],
                "os_name": phone["os_name"],
                "os_version": phone["os_version"],
                "supports_encryption": False,
                "app_data": app_data,
                "webhook_id": webhook_id,
                "user_id": request["hass_user"].id,
            }
            if not slugify(data["device_name"], separator=""):
                data["device_name"] = data["device_id"]
            # The same app and device id replaces the earlier registration
            # (HA's mobile_app exemption in `async_finish_flow`), so
            # registering again is safe and retires the old device key.
            await hass.config_entries.flow.async_init(
                "mobile_app", data=data, context={"source": "registration"},
            )
            return self.json({"device_key": webhook_id}, status_code=201)

    def _number(value: Any, *, whole: bool = False, positive: bool = False) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value):
            return None
        if positive and value < 0:
            return None
        return round(value) if whole else float(value)

    class LocationView(HomeAssistantView):
        url = URL_PREFIX + "/location"
        name = "api:sensa:mobile:location"
        # The device key is the credential, as HA's webhook id is: the
        # background task posts with the app closed and no access token.
        requires_auth = False

        async def post(self, request):
            body = await _json(request)
            entry = _registration(str(body.get("device_key") or ""))
            if entry is None:
                return _error("unknown_device", 404)
            lat, lon = _number(body.get("latitude")), _number(body.get("longitude"))
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return _error("invalid_request", 400, "latitude and longitude are required")
            data: dict[str, Any] = {
                "gps": [lat, lon],
                "gps_accuracy": _number(body.get("accuracy"), positive=True) or 0.0,
            }
            for key, whole in (
                ("speed", True), ("course", True), ("vertical_accuracy", True), ("altitude", False),
            ):
                value = _number(body.get(key), whole=whole, positive=whole)
                if value is not None:
                    data[key] = value
            # What HA's `update_location` webhook does with a validated body.
            async_dispatcher_send(hass, SIGNAL_LOCATION_UPDATE.format(entry.entry_id), data)
            return self.json({})

    class NotificationsView(HomeAssistantView):
        url = URL_PREFIX + "/notifications"
        name = "api:sensa:mobile:notifications"
        requires_auth = False  # authenticated by the first message

        async def get(self, request):
            from homeassistant.components.mobile_app.push_notification import (
                PushChannel,
            )

            ws = web.WebSocketResponse(heartbeat=30)
            await ws.prepare(request)
            try:
                hello = await ws.receive_json(timeout=10)
            except (TypeError, ValueError, TimeoutError):
                await ws.close()
                return ws
            hello = hello if isinstance(hello, dict) else {}
            refresh_token = hass.auth.async_validate_access_token(str(hello.get("access_token") or ""))
            webhook_id = str(hello.get("device_key") or "")
            entry = _registration(webhook_id)
            if (
                refresh_token is None
                or entry is None
                or entry.data.get("user_id") != refresh_token.user.id
            ):
                await ws.send_json({"type": "auth_invalid"})
                await ws.close()
                return ws

            # Registered exactly as HA's `mobile_app/push_notification_channel`
            # WebSocket command does, so `notify.notify` finds this phone.
            channels = hass.data["mobile_app"][DATA_PUSH_CHANNEL]
            if webhook_id in channels:
                await channels[webhook_id].async_teardown()
            channel: Any = None

            def send(data: dict[str, Any]) -> None:
                confirm_id = data.get("hass_confirm_id")
                # mobile_app's "clear_notification" is a command to the
                # Companion apps, not something to show.
                if data.get("message") == "clear_notification":
                    if confirm_id:
                        channel.async_confirm_notification(confirm_id)
                    return
                hass.async_create_task(ws.send_json({
                    "type": "notification",
                    "id": confirm_id,
                    "title": data.get("title"),
                    "message": data.get("message"),
                    "data": data.get("data") or {},
                }))

            def on_teardown() -> None:
                if channels.get(webhook_id) is channel:
                    channels.pop(webhook_id)

            channel = channels[webhook_id] = PushChannel(hass, webhook_id, True, send, on_teardown)
            await ws.send_json({"type": "auth_ok"})
            try:
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        continue
                    try:
                        body = msg.json()
                    except ValueError:
                        continue
                    if isinstance(body, dict) and body.get("type") == "confirm":
                        channel.async_confirm_notification(str(body.get("id") or ""))
            finally:
                await channel.async_teardown()
            return ws

    class TestNotificationView(HomeAssistantView):
        url = URL_PREFIX + "/test_notification"
        name = "api:sensa:mobile:test_notification"

        async def post(self, request):
            body = await _json(request)
            message = str(body.get("message") or "").strip()
            if not message:
                return _error("invalid_request", 400, "message is required")
            service_data = {"message": message}
            if body.get("title"):
                service_data["title"] = str(body["title"])
            try:
                await hass.services.async_call("notify", "notify", service_data, blocking=True)
            except HomeAssistantError as err:
                return _error("not_sent", 502, str(err))
            return self.json({})

    for view in (
        PingView, SignInView, TokenView, SignOutView, AddressesView,
        RegisterView, LocationView, NotificationsView, TestNotificationView,
    ):
        hass.http.register_view(view())
