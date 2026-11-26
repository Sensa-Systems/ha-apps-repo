"""Server-side admin actions for the Settings → System tab.

On an appliance the customer's elevated login (``owner``) is a **non-admin**
HA user, so it can't use HA's own admin pages (Settings → People, System →
Restart, the Supervisor host controls). These services let the System tab do
those tasks anyway: each one runs **inside the integration** — which acts
with system authority — after checking the *caller* is a Sensa admin
(``owner`` or any HA admin / the Sensa support backdoor). So the non-admin
owner can rotate a password or restart the box, while a kiosk tablet can't.

The same services are available on BYO (one software build); there the
caller is a real HA admin, so the gate passes and the tab is a convenience.

Every handler RAISES on failure (HomeAssistantError / Unauthorized) so the
card shows a real error instead of a silent no-op — the lesson from the
integrity-check "Run Check does nothing" saga.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from .appliance import async_user_is_sensa_admin
from .const import (
    APPLIANCE_ADMIN_USERNAME,
    APPLIANCE_KIOSK_USERNAMES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Passwords may only be rotated for the customer's own logins — never the
# Sensa support admin (the backdoor stays Sensa's). The owner + kiosk
# tablets are the full set of customer accounts.
_PASSWORD_TARGETS: frozenset[str] = frozenset(
    (APPLIANCE_ADMIN_USERNAME, *APPLIANCE_KIOSK_USERNAMES)
)
_MIN_PASSWORD_LEN = 4

# Unit systems the System card may set. HA couples temperature with the
# rest of the unit system (metric vs US customary); there's no per-unit
# split in core config.
_UNIT_SYSTEMS = ("metric", "us_customary")

# Container control policy — the authoritative copy is in the App's
# routes/system.py (and re-enforced in the updater); mirror it here so a
# disallowed action is rejected before it ever reaches the App. homeassistant /
# updater = read-only (a stop would kill the panel / self-terminate the updater);
# sensa-app = restart only (a stop self-locks the control API the panel calls).
_CONTAINER_POLICY: dict[str, tuple[str, ...]] = {
    "homeassistant": (),
    "updater": (),
    "sensa-app": ("restart",),
}
_DEFAULT_CONTAINER_ACTIONS = ("start", "stop", "restart")
_KNOWN_SERVICES = frozenset(
    ("homeassistant", "sensa-app", "mosquitto", "zigbee2mqtt",
     "cloudflared", "updater", "login-branding", "frigate")
)


def _container_actions_for(service: str) -> tuple[str, ...]:
    return _CONTAINER_POLICY.get(service, _DEFAULT_CONTAINER_ACTIONS)


async def _require_sensa_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Raise Unauthorized unless the service caller is a Sensa admin.

    A service call from the UI carries the user in `call.context.user_id`.
    A call with no user (system/automation context) is trusted and passes
    — these services are only reachable from the System tab anyway.
    """
    user_id = call.context.user_id
    if user_id is None:
        return
    if not await async_user_is_sensa_admin(hass, user_id):
        raise Unauthorized(context=call.context)


def _get_hass_provider(hass: HomeAssistant) -> Any:
    """Return the built-in homeassistant auth provider, or raise."""
    try:
        from homeassistant.auth.providers.homeassistant import async_get_provider
    except ImportError as exc:  # pragma: no cover - real HA always has it
        raise HomeAssistantError("Auth provider unavailable") from exc
    try:
        return async_get_provider(hass)
    except RuntimeError as exc:
        raise HomeAssistantError(
            "The Home Assistant username/password auth provider isn't loaded."
        ) from exc


async def _handle_set_user_password(hass: HomeAssistant, call: ServiceCall) -> None:
    await _require_sensa_admin(hass, call)
    username = call.data["username"]
    password = call.data["password"]
    if username not in _PASSWORD_TARGETS:
        raise HomeAssistantError(
            f"Refusing to set the password for {username!r}: only the "
            f"customer logins ({', '.join(sorted(_PASSWORD_TARGETS))}) can be "
            "changed here."
        )
    if len(password) < _MIN_PASSWORD_LEN:
        raise HomeAssistantError(
            f"Password too short (minimum {_MIN_PASSWORD_LEN} characters)."
        )
    provider = _get_hass_provider(hass)
    try:
        # async_change_password persists the provider store itself (it calls
        # data.async_save internally), so the new hash survives a restart.
        await provider.async_change_password(username, password)
    except Exception as exc:
        # InvalidUser (user not provisioned) lands here too.
        raise HomeAssistantError(
            f"Could not change the password for {username!r}: {exc}"
        ) from exc
    _LOGGER.info("Admin: password rotated for user %r", username)
    # Re-ask the App who is still on a default password, rather than assuming
    # this rotation cleared the last one. The remote-access and remote-support
    # toggles are gated on that answer, so without this the customer fixes the
    # thing the panel told them to fix and the toggles stay greyed until the next
    # status poll. Best-effort: the poll will catch up if this fails.
    await _async_recheck_default_logins(hass)


async def _async_recheck_default_logins(hass: HomeAssistant) -> None:
    """Pull a fresh `/api/license/status`, which re-runs the App's check."""
    client = None
    entry_id: str | None = None
    for eid, data in hass.data.get(DOMAIN, {}).items():
        if isinstance(data, dict) and data.get("client") is not None:
            entry_id, client = eid, data["client"]
            break
    if client is None or entry_id is None:
        return
    try:
        from . import _async_refresh_licence_status

        await _async_refresh_licence_status(hass, entry_id, client)
    except Exception:
        _LOGGER.debug(
            "Could not refresh the default-login gate after a rotation",
            exc_info=True,
        )


async def _handle_set_settings_pin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Change the Settings PIN.

    Gated like every other admin service, so the caller is already a Sensa
    admin, which is why no current-PIN confirmation is asked for: they either
    logged in as `admin`, are an HA admin, or typed the PIN to get here.
    """
    await _require_sensa_admin(hass, call)
    from . import settings_pin

    try:
        await settings_pin.async_set_pin(hass, call.data["pin"])
    except settings_pin.PinError as exc:
        # Raise rather than swallow, so the card shows the real reason instead
        # of looking like a silent no-op.
        raise HomeAssistantError(str(exc)) from exc


async def _handle_complete_setup_wizard(
    hass: HomeAssistant, call: ServiceCall,
) -> None:
    """The Setup Wizard's Finish. Its answers were written to their own
    entities as they were made, so this only records that it is done."""
    await _require_sensa_admin(hass, call)
    from . import setup_wizard

    await setup_wizard.async_complete(hass)


async def _handle_restart_system(hass: HomeAssistant, call: ServiceCall) -> None:
    await _require_sensa_admin(hass, call)
    _LOGGER.warning("Admin: Home Assistant restart requested from the System tab")
    # Recorded so the boot on the other side can say who asked. Without it a
    # deliberate restart reads as an unexplained one on the customer's Log.
    from .boot_log import async_record_shutdown_intent

    await async_record_shutdown_intent(hass, "restarted from Settings")
    # Call homeassistant.restart with a fresh (system) context so HA's own
    # admin gate on that service doesn't reject the non-admin owner — the
    # role check above is the real authorization.
    await hass.services.async_call("homeassistant", "restart", {}, blocking=False)


async def _handle_reboot_device(hass: HomeAssistant, call: ServiceCall) -> None:
    await _require_sensa_admin(hass, call)
    if not hass.services.has_service("hassio", "host_reboot"):
        raise HomeAssistantError(
            "Host reboot isn't available — this isn't a Home Assistant OS / "
            "Supervised system. Use Restart instead."
        )
    _LOGGER.warning("Admin: host reboot requested from the System tab")
    from .boot_log import async_record_shutdown_intent

    await async_record_shutdown_intent(
        hass, "the device was rebooted from Settings", action="reboot",
    )
    await hass.services.async_call("hassio", "host_reboot", {}, blocking=False)


async def _handle_factory_reset(hass: HomeAssistant, call: ServiceCall) -> None:
    """Wipe everything this box learned and restart it.

    Gated like the rest of this module, so the appliance `admin` login can run
    it without being an HA admin — which is the point: the dealer finishing a
    handover and the owner starting fresh are both signed in as that user, and
    on an appliance neither of them can reach Home Assistant's own admin pages.
    The confirmation is the card's job; by the time a call arrives here the
    customer has pressed twice and been told what goes.
    """
    await _require_sensa_admin(hass, call)
    from .factory_reset import async_handle_factory_reset

    _LOGGER.warning("Admin: FACTORY RESET requested from the Diagnostics tab")
    await async_handle_factory_reset(hass, call)


async def _handle_set_system_config(hass: HomeAssistant, call: ServiceCall) -> None:
    await _require_sensa_admin(hass, call)
    updates: dict[str, Any] = {}
    if (tz := call.data.get("time_zone")) is not None:
        updates["time_zone"] = tz
    if (unit_system := call.data.get("unit_system")) is not None:
        updates["unit_system"] = unit_system
    # Country and currency are core config like the rest, but they are not
    # cosmetic: Home Assistant uses `country` for energy tariffs and for the
    # sun / holiday integrations, and it is the field the master image shipped
    # UNSET (2026-08-18), because the Container build path never ran the
    # neutral-location step. Exposed here so a non-HA-admin appliance owner can
    # set it without HA's admin pages, the same reason the two above are.
    if (country := call.data.get("country")) is not None:
        updates["country"] = country
    if (currency := call.data.get("currency")) is not None:
        updates["currency"] = currency
    if (lat := call.data.get("latitude")) is not None:
        updates["latitude"] = lat
    if (lon := call.data.get("longitude")) is not None:
        updates["longitude"] = lon
    if (elev := call.data.get("elevation")) is not None:
        updates["elevation"] = elev
    if not updates:
        raise HomeAssistantError("No system settings supplied to update.")
    try:
        await hass.config.async_update(**updates)
    except (ValueError, HomeAssistantError) as exc:
        raise HomeAssistantError(f"Could not update system settings: {exc}") from exc
    _LOGGER.info("Admin: system config updated (%s)", ", ".join(sorted(updates)))


async def _handle_set_theme(hass: HomeAssistant, call: ServiceCall) -> None:
    """Set the instance-wide frontend theme (applies to ALL logins). Gated to a
    Sensa admin, then delegates to `frontend.set_theme`, which the integration
    (running as the system user) may call without the caller being an HA admin —
    so the non-HA-admin `admin` login on Container can still drive it."""
    await _require_sensa_admin(hass, call)
    theme = call.data["theme"]
    # Gate the theme to this licence's allow-list (public Sensa themes + the
    # dealer's own theme per theme_mode). A client-only picker filter can't stop
    # a direct service call, so this is the real enforcement seam — it stops an
    # appliance customer setting a theme they aren't entitled to (e.g. an
    # exclusive dealer's box picking a public theme, or any box naming a rival
    # dealer's theme). Mirrors instincts_catalog.routine_allowed.
    from .theme_catalog import (
        allowed_theme_names,
        current_default_theme,
        registered_theme_names,
    )

    allowed = allowed_theme_names(hass)
    if theme not in allowed:
        raise HomeAssistantError(
            f"Theme {theme!r} isn't available on this system."
        )
    data: dict[str, Any] = {"name": theme}
    if (mode := call.data.get("mode")) is not None:
        data["mode"] = mode
    try:
        await hass.services.async_call("frontend", "set_theme", data, blocking=True)
    except (ValueError, HomeAssistantError) as exc:
        raise HomeAssistantError(f"Could not set theme: {exc}") from exc

    # `frontend.set_theme` is FAIL-SOFT: a name HA has not REGISTERED returns
    # success and only logs a warning, so without this the System panel cheerily
    # reported "Theme set for all users" over a no-op. Allowed and registered are
    # two different sets (bundled dir + licence vs `/config/themes/` covered by
    # the `frontend: themes:` include), and the gap between them is the bug.
    #
    # Only a POSITIVE miss raises. `registered_theme_names` returns None when it
    # could not read HA's frontend state at all, which is "could not ask" and
    # must keep the old behaviour rather than failing a set that worked.
    registered = registered_theme_names(hass)
    if registered is not None and theme not in registered:
        raise HomeAssistantError(
            f"Theme {theme!r} is not installed on this system: the theme files "
            "may be missing, or `configuration.yaml` may no longer include "
            "`themes: !include_dir_merge_named themes`."
        )
    landed = current_default_theme(hass)
    if landed is not None and landed != theme:
        raise HomeAssistantError(
            f"Theme {theme!r} did not stick (still {landed!r})."
        )

    # One instance, one theme. HA's per-user Profile theme overrides the default
    # we just set, so without this the login that changed it often sees nothing
    # change, which is the whole reason this row looked broken.
    from .frontend import async_clear_pinned_user_themes

    cleared = await async_clear_pinned_user_themes(hass)
    _LOGGER.info(
        "Admin: frontend theme set to %r (cleared %d pinned profile theme(s))",
        theme, cleared,
    )


async def _handle_container_action(hass: HomeAssistant, call: ServiceCall) -> None:
    """Start/stop/restart a compose-stack container (Settings → Supervisor).

    Gated to a Sensa admin, then re-enforces the per-service control policy
    before asking the App (which signals the socket-holding updater sidecar).
    Container-appliance only — a no-op elsewhere returns cleanly."""
    await _require_sensa_admin(hass, call)
    service = call.data["service"]
    action = call.data["action"]
    if service not in _KNOWN_SERVICES:
        raise HomeAssistantError(f"Unknown container {service!r}.")
    if action not in _container_actions_for(service):
        allowed = ", ".join(_container_actions_for(service)) or "none"
        raise HomeAssistantError(
            f"{action.title()} isn't allowed for {service!r} (allowed: {allowed})."
        )

    # Find the entry runtime that owns the App client (usually the one entry).
    entry_id: str | None = None
    client: Any = None
    runtime: dict[str, Any] | None = None
    for eid, data in hass.data.get(DOMAIN, {}).items():
        if isinstance(data, dict) and data.get("client") is not None:
            entry_id, client, runtime = eid, data["client"], data
            break
    if client is None:
        raise HomeAssistantError("Sensa App client isn't ready — try again shortly.")

    try:
        result = await client.async_container_action(service=service, action=action)
    except Exception as exc:
        raise HomeAssistantError(
            f"Could not {action} {service!r}: {exc}"
        ) from exc
    # A 4xx from the App (e.g. its policy rejecting the action) comes back as a
    # structured body, NOT an exception (only 5xx raises — see the API client), so
    # check for an error key. Without this a policy drift between admin.py and the
    # App would show a green "sent" notice while nothing happened. NB: `status ==
    # "noop"` is NOT an error — it's the App's normal 200 reply on a non-container
    # install (the service is registered on every flavour), so treat it as a
    # clean no-op, not a failure.
    if isinstance(result, dict) and result.get("error"):
        raise HomeAssistantError(
            f"The Sensa App refused to {action} {service!r}: {result['error']}"
        )
    _LOGGER.info("Admin: container %s %s (result=%s)", action, service, result)

    # Schedule a couple of delayed list refreshes so the panel reflects the flip
    # promptly: the updater applies the action on its own <=5s tick and re-collects
    # the list only AFTER a `docker compose <action>` that can itself take a few
    # seconds, so an immediate re-poll would read pre-action state. Two shots (~7s
    # + ~16s) cover both a fast action and a slow restart; the 30s poller is the
    # final backstop. The cancel handles are stashed on the runtime so a config-
    # entry unload/reload cancels them (they'd otherwise fire with a stale client).
    # Best-effort — a scheduling hiccup never fails the action the App accepted.
    if entry_id is not None and runtime is not None:
        try:
            from homeassistant.helpers.event import async_call_later

            # Coalesce: cancel any still-pending refreshes from a previous tap
            # before scheduling this one's, so rapid repeat taps don't burst N×2
            # fetches and the list can't grow unbounded over a weeks-long session
            # (it holds at most the current 2 handles).
            prior = runtime.get("container_action_refresh_cancels") or []
            for c in prior:
                if c:
                    c()

            def _delayed_refresh(_now: Any) -> None:
                # If the entry reloaded in the gap after this timer fired, the
                # runtime under entry_id is a NEW dict (or gone) — skip so we don't
                # refresh with the torn-down entry's stale client.
                if hass.data.get(DOMAIN, {}).get(entry_id) is not runtime:
                    return
                from . import _async_refresh_containers
                hass.async_create_task(
                    _async_refresh_containers(hass, entry_id, client)
                )

            runtime["container_action_refresh_cancels"] = [
                async_call_later(hass, 7, _delayed_refresh),
                async_call_later(hass, 16, _delayed_refresh),
            ]
        except Exception:
            _LOGGER.debug("Post-action container refresh scheduling failed", exc_info=True)


async def _handle_install_update(hass: HomeAssistant, call: ServiceCall) -> None:
    """Install one `update.*` entity on behalf of a Sensa admin.

    From HA 2026.9 `update.install` refuses a non-HA-admin caller, and the
    appliance kiosk logins are never HA admins. The call is made with no
    context, so HA runs it as the system; the Sensa-admin check above it is
    the gate. Blocking, so "already in progress" still reaches the card.
    """
    await _require_sensa_admin(hass, call)
    entity_id = call.data["entity_id"]
    if not entity_id.startswith("update."):
        raise HomeAssistantError(f"{entity_id!r} is not an update entity.")
    await hass.services.async_call(
        "update", "install", {"entity_id": entity_id}, blocking=True
    )


def async_register_admin_services(hass: HomeAssistant) -> None:
    """Register the gated Settings → Admin services (idempotent)."""
    registrations: tuple[tuple[str, Any, Any], ...] = (
        (
            "set_user_password",
            _handle_set_user_password,
            vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("password"): str,
                }
            ),
        ),
        (
            "set_settings_pin",
            _handle_set_settings_pin,
            vol.Schema({vol.Required("pin"): str}),
        ),
        ("restart_system", _handle_restart_system, None),
        ("reboot_device", _handle_reboot_device, None),
        ("factory_reset", _handle_factory_reset, None),
        ("complete_setup_wizard", _handle_complete_setup_wizard, None),
        (
            "set_system_config",
            _handle_set_system_config,
            vol.Schema(
                {
                    vol.Optional("time_zone"): str,
                    vol.Optional("unit_system"): vol.In(_UNIT_SYSTEMS),
                    vol.Optional("latitude"): vol.Coerce(float),
                    vol.Optional("longitude"): vol.Coerce(float),
                    vol.Optional("elevation"): vol.Coerce(float),
                }
            ),
        ),
        (
            "set_theme",
            _handle_set_theme,
            vol.Schema(
                {
                    vol.Required("theme"): str,
                    vol.Optional("mode"): vol.In(("dark", "light")),
                }
            ),
        ),
        (
            "container_action",
            _handle_container_action,
            vol.Schema(
                {
                    vol.Required("service"): str,
                    vol.Required("action"): vol.In(("start", "stop", "restart")),
                }
            ),
        ),
        (
            "install_update",
            _handle_install_update,
            vol.Schema({vol.Required("entity_id"): str}),
        ),
    )
    for name, handler, schema in registrations:
        if hass.services.has_service(DOMAIN, name):
            continue

        # Bind handler per loop so the closure captures the right callable.
        def _make(h: Any):
            async def _service(call: ServiceCall) -> None:
                await h(hass, call)

            return _service

        hass.services.async_register(DOMAIN, name, _make(handler), schema=schema)
