"""Appliance authorization — read-only role checks for the shipped product.

The Sensa product is a **single software build** for both packages:

  * **BYO** — the customer installs on their own Home Assistant and stays
    a full HA admin. Nothing here changes their system.
  * **Appliance** — a Sensa-shipped device. The lockdown (creating the
    non-admin customer users, keeping the single HA admin as Sensa's
    `sensa_support` backdoor, flipping ``deployment_mode``) is **baked into
    the master image by the build script** (`tools/images/
    build-appliance.py`), never by this product. Being locked-down is a
    property of how the image was built, so no code shipped to a live box
    can ever lock a BYO customer out of their own HA.

This module therefore only ever **reads** state and answers one question:
*may this user use the Settings panel and the privileged Sensa admin
services?* It never creates, promotes, or demotes an account.

Who passes:

  * any **HA admin**, BYO admins, and the Sensa support backdoor;
  * on a **HAOS appliance**, the ``admin`` login, via the
    :data:`APPLIANCE_SETTINGS_USERNAMES` allowlist (the Container builder
    stopped creating that account on 2026-09-03 and the allowlist is not
    consulted there, so a leftover ``admin`` on an older Container box needs
    the PIN like any kiosk login); and
  * **any login holding a live Settings-PIN elevation** (see
    :mod:`settings_pin`). This is what opens Settings to the kiosk tablets
    and to the companion app: the customer types the PIN instead of logging
    out and back in. The elevation is bounded, in memory only, and never
    satisfies :func:`user_is_sensa_staff`.

The privileged admin tasks (rotate a password, restart, reboot, set
timezone/units, change the network) all run **server-side** inside the
integration, gated on this check — so a non-admin kiosk login can drive
them from the Settings → System tab without ever being an HA admin.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    APPLIANCE_SETTINGS_USERNAMES,
    DEPLOYMENT_MODE_APPLIANCE,
    DEPLOYMENT_MODE_KEY,
    DOMAIN,
    SENSA_SUPPORT_USERNAME,
)

_LOGGER = logging.getLogger(__name__)


def _hass_username(user: Any) -> str | None:
    """Return a user's built-in homeassistant-provider username, or None.

    `user.name` is the display name, not the login — the username lives on
    the homeassistant credential. Mirrors the resolution in
    `dashboard._async_build_user_default_panels`.
    """
    for cred in getattr(user, "credentials", []):
        if getattr(cred, "auth_provider_type", None) != "homeassistant":
            continue
        return (getattr(cred, "data", None) or {}).get("username")
    return None


def _is_appliance(hass: HomeAssistant) -> bool:
    return (
        hass.data.get(DOMAIN, {}).get(DEPLOYMENT_MODE_KEY) == DEPLOYMENT_MODE_APPLIANCE
    )


def is_supervised(hass: HomeAssistant) -> bool:
    """True when HA runs under Supervisor (HAOS / supervised), False on a bare
    Container/compose install. The discriminator for HAOS-vs-Container features
    (e.g. the Debian-`apt` Host-OS entities are Container-only). NB: `is_hassio`
    lives in the hassio HELPER, not the component — importing it from the
    component raises ImportError (see __init__.py's inline uses)."""
    try:
        from homeassistant.helpers.hassio import is_hassio

        return bool(is_hassio(hass))
    except ImportError:
        return False


def user_is_sensa_admin(hass: HomeAssistant, user: Any | None) -> bool:
    """True if `user` (a HA User object) may use the Settings panel + Sensa
    admin services. Read-only — the core role check.

    Real HA admins always pass (BYO admins + the Sensa support backdoor), as
    does anyone holding a live Settings-PIN elevation. On a HAOS appliance the
    ``admin`` login also passes via the
    :data:`APPLIANCE_SETTINGS_USERNAMES` allowlist. A missing user, or a
    non-admin on BYO with no elevation, is False.

    Sync so a `@callback` WS handler (which already holds the connection's
    User object) can call it without an auth round-trip.
    """
    if user is None:
        return False
    if getattr(user, "is_admin", False):
        return True
    # A live Settings-PIN elevation counts on EVERY flavour, and is checked
    # before the BYO bail below so it is not silently appliance-only. It is
    # deliberately the same verdict an HA admin gets: a frontend-only reveal
    # would hand a kiosk login a Settings screen where every action is
    # refused. See settings_pin for the bounds on it.
    from . import settings_pin

    if settings_pin.is_elevated(hass, getattr(user, "id", None)):
        return True
    if not _is_appliance(hass):
        # BYO: the only privileged users are HA admins. A non-admin login
        # named "admin" on someone's own HA must not gain admin services.
        return False
    if not is_supervised(hass) and not settings_pin.pin_enabled(hass):
        # Container appliance with Admin Lock OFF: the whole point of the
        # toggle is that nobody has to elevate at all, so every kiosk login
        # gets the same verdict a PIN elevation would have given it. Same
        # scope predicate as settings_pin._pin_scope in __init__.py — must
        # not drift from it, or the tab and the services it calls disagree.
        return True
    # The allowlist is HAOS only. On Container the `admin` login is retired
    # (Richard, 2026-10-01), so one left on a box built before 2026-09-03 is a
    # kiosk login like any other and asks for the PIN.
    return is_supervised(hass) and _hass_username(user) in APPLIANCE_SETTINGS_USERNAMES


def user_is_sensa_staff(hass: HomeAssistant, user: Any | None) -> bool:
    """True only for SENSA'S OWN support login, not for the customer.

    Deliberately NOT `user_is_sensa_admin`: that returns True for any HA admin,
    which on BYO is the customer themselves, and for anyone who has typed the
    Settings PIN. This gates the staff-only Settings tab, so it must be the
    narrower question "is this Sensa, remotely logged in?" and the only account
    that can answer yes is the `sensa_support` backdoor, whose password is never
    given to a customer (see const.py APPLIANCE_* notes).

    Username-based on purpose: HA has no group/role concept to hang this on, and
    keying on `is_admin` would hand every BYO customer a Sensa-internal tab.

    Sync so a `@callback` WS handler (which already holds the connection's User
    object) can call it without an auth round-trip.
    """
    if user is None:
        return False
    return _hass_username(user) == SENSA_SUPPORT_USERNAME


async def async_user_is_sensa_admin(
    hass: HomeAssistant, user_id: str | None
) -> bool:
    """True if `user_id` may use the Settings panel + Sensa admin services.

    The async entry point for callers that only have a user id (e.g. a
    service call's `context.user_id`): resolves the User then defers to
    :func:`user_is_sensa_admin`.
    """
    if not user_id:
        return False
    try:
        user = await hass.auth.async_get_user(user_id)
    except Exception:
        _LOGGER.debug("async_user_is_sensa_admin: user lookup failed", exc_info=True)
        return False
    return user_is_sensa_admin(hass, user)
