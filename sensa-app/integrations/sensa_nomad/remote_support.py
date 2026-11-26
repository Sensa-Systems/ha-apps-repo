"""Customer-consented Sensa remote support.

The customer's ``switch.sensa_nomad_remote_support`` toggle (default off) grants
Sensa time-boxed access to their unit. Only HA-side code can touch HA users, so
the activate/rotate/expire logic lives here in the integration; the resulting
consent state (enabled flag + the per-enable credential + expiry) is pushed to
the Sensa App add-on, which carries it up to Sensa over the authenticated
phone-home channel for the Hub to display (see remote-access.md §7.2).

Hard rule: this module only ever touches the ``sensa_support`` account. It
NEVER creates, demotes or mutates the customer's own logins (``owner`` / ``hab``
/ ``cab`` / ``mobile``) or the onboarding admin; those are baked by the
master-image build script and must never be moved by shipped code.

The ``sensa_support`` user is created INACTIVE (an HA admin) by
``tools/build-appliance.py``, and by ``async_ensure_support_user`` on a Container
appliance built without it. On a BYO install it doesn't exist, so the toggle
isn't offered (the switch is gated on the account being present).

State persisted to HA's Store survives restart as ``{enabled, enabled_until}``
only — the password is held in memory and on the add-on, never duplicated into
``.storage``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from ._log import sensa_log
from .const import (
    DOMAIN,
    EVENT_SENSA_REMOTE_SUPPORT_CHANGED,
    REMOTE_SUPPORT_DEFAULT_WINDOW_HOURS,
    REMOTE_SUPPORT_MAX_WINDOW_HOURS,
    REMOTE_SUPPORT_MIN_WINDOW_HOURS,
    REMOTE_SUPPORT_STORAGE_KEY,
    REMOTE_SUPPORT_STORAGE_VERSION,
    SENSA_LOG_SOURCE_SYSTEM,
    SENSA_SUPPORT_USERNAME,
)

_LOGGER = logging.getLogger(__name__)

_STORE_RUNTIME_KEY = "remote_support_state"
_NOTIFICATION_ID = "sensa_nomad_remote_support"

# Guards every read-modify-write of the RemoteSupportState singleton.
# async_set_enabled() is entered independently from the switch entity
# (customer toggle), the periodic tick (auto-expiry), and Hub grants/revokes
# -- none of those are serialised against each other by HA -- and the body
# awaits real I/O (activating/deactivating the sensa_support user) between
# reading and writing `enabled`/`enabled_until`. Without this lock, a tick
# firing mid-enable can null out `enabled_until` after the enable call has
# already read it as non-None, crashing on `.isoformat()`.
_STATE_LOCK = asyncio.Lock()

# Who opened the current session. The customer's own toggle, or a break-glass
# grant issued from the Hub -- which needs no action on the box, so it is the
# case the customer most needs told apart from their own.
OPENED_BY_CUSTOMER = "customer"
OPENED_BY_SENSA = "sensa"

# How a session finished, in the customer's words. Keyed by the ``ended``
# argument to ``async_set_enabled`` so the three ways out cannot drift into
# saying the same thing: the customer closing it, the window running out, and
# Sensa withdrawing it because the support case it belonged to was closed.
_CLOSED_TEXT = {
    "customer": "Sensa Support access closed.",
    "expiry": "Sensa Support access ended automatically.",
    "revoked": "Sensa closed its support access because your support case was closed.",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean_text(value: Any) -> str | None:
    """A non-empty trimmed string, or None.

    One spelling of "absent" for every free-text field that rides down on a
    grant. Blank, whitespace and a non-string all collapse to None, so a reader
    only ever has to ask whether the value is there.
    """
    return value.strip() if isinstance(value, str) and value.strip() else None


def clamp_window_hours(value: Any) -> float:
    """Coerce + clamp a requested window to the allowed range, default on junk."""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return float(REMOTE_SUPPORT_DEFAULT_WINDOW_HOURS)
    if hours < REMOTE_SUPPORT_MIN_WINDOW_HOURS:
        return float(REMOTE_SUPPORT_MIN_WINDOW_HOURS)
    if hours > REMOTE_SUPPORT_MAX_WINDOW_HOURS:
        return float(REMOTE_SUPPORT_MAX_WINDOW_HOURS)
    return hours


class RemoteSupportState:
    """Live consent state + its durable backing.

    One per HA instance, held on ``hass.data[DOMAIN][_STORE_RUNTIME_KEY]``.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(
            hass, REMOTE_SUPPORT_STORAGE_VERSION, REMOTE_SUPPORT_STORAGE_KEY
        )
        self.enabled: bool = False
        self.enabled_until: datetime | None = None
        # WHO opened the current session, and why. Persisted, because the panel
        # has to keep telling the customer "Sensa opened this remotely" after a
        # restart -- a session that survives a reboot but forgets it was opened
        # by us would read as one the customer opened themselves.
        self.opened_by: str = OPENED_BY_CUSTOMER
        self.reason: str | None = None
        # The named operator who issued a break-glass grant. Absent on a session
        # the customer opened, and absent on a grant from a Function that does
        # not send it yet -- which is why every reader must treat it as "not
        # told" rather than assume it will be there.
        self.granted_by: str | None = None
        # Held in memory only (never persisted to .storage). Lets the expiry
        # tick re-push to the add-on if the original push raced a restarting
        # add-on. After an HA restart this is None and we don't re-push — the
        # add-on already persisted it at first enable.
        self._password: str | None = None
        self._push_pending: bool = False

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.enabled = bool(data.get("enabled"))
        self.enabled_until = _parse_iso(data.get("enabled_until"))
        # A store written before this field existed says nothing about who
        # opened the session, and the honest reading of that is the customer:
        # claiming Sensa opened a session we cannot account for would be worse
        # than the reverse.
        opened_by = data.get("opened_by")
        self.opened_by = (
            opened_by if opened_by in (OPENED_BY_CUSTOMER, OPENED_BY_SENSA)
            else OPENED_BY_CUSTOMER
        )
        self.reason = _clean_text(data.get("reason"))
        self.granted_by = _clean_text(data.get("granted_by"))

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "enabled": self.enabled,
                "enabled_until": (
                    self.enabled_until.isoformat() if self.enabled_until else None
                ),
                "opened_by": self.opened_by,
                "reason": self.reason,
                "granted_by": self.granted_by,
            }
        )


def get_state(hass: HomeAssistant) -> RemoteSupportState | None:
    return hass.data.get(DOMAIN, {}).get(_STORE_RUNTIME_KEY)


async def async_setup(hass: HomeAssistant, entry: Any) -> RemoteSupportState:
    """Create + load the singleton state, then reconcile on startup.

    If a window was open when HA stopped and has since elapsed, close it now
    (deactivate the account). If still within the window, leave it as the
    add-on already holds the credential and the account is still active.
    """
    # Before the switch platform asks whether the account exists.
    await async_ensure_support_user(hass)

    state = get_state(hass)
    if state is None:
        state = RemoteSupportState(hass)
        await state.async_load()
        hass.data.setdefault(DOMAIN, {})[_STORE_RUNTIME_KEY] = state

    if state.enabled and state.enabled_until and _now() >= state.enabled_until:
        _LOGGER.info("Remote support window had elapsed during downtime — closing it")
        await async_set_enabled(hass, entry, False, ended="expiry")
    # NOTE: we don't push OFF on every restart to heal a possibly-lost OFF push.
    # The add-on self-expires (it stops reporting the credential once the window
    # closes), which bounds any stale broadcast to the window, and the tick
    # retries a failed push within the session — so a per-restart push would be
    # cost with little benefit.
    return state


async def async_set_enabled(
    hass: HomeAssistant,
    entry: Any,
    enabled: bool,
    *,
    window_hours: float | None = None,
    opened_by: str = OPENED_BY_CUSTOMER,
    reason: str | None = None,
    granted_by: str | None = None,
    ended: str = "customer",
) -> dict[str, Any]:
    """Enable or disable remote support. Returns a small status dict.

    Enabling activates ``sensa_support`` with a fresh random password and a
    bounded window; disabling (or expiry) deactivates it and invalidates its
    sessions. Both push the new state to the add-on for phone-home reporting.

    ``opened_by`` / ``reason`` / ``granted_by`` describe how the session came
    about, and ``ended`` how it finished (``customer`` or ``expiry``). They are
    arguments rather than a second code path deliberately: the credential
    rotation, the expiry and the add-on push must be identical however a session
    starts, and two entry points would eventually drift on one of them.
    """
    state = get_state(hass)
    if state is None:
        # async_setup() may itself call back into async_set_enabled() to
        # reconcile an elapsed window, so this has to happen before the lock
        # below is taken -- asyncio.Lock isn't reentrant, and taking it here
        # would deadlock that reconciliation call against itself.
        state = await async_setup(hass, entry)

    async with _STATE_LOCK:
        if enabled and opened_by != OPENED_BY_SENSA:
            # Seeded passwords hold the tunnel shut (`weak_credentials.weak_logins`
            # -> `routes/cloudflare.py`), so a session opened now would hand Sensa a
            # credential for a box nothing can reach, and the customer's only clue
            # would be the Hub telling US it was blocked. Refuse it here, where they
            # can read why.
            #
            # A break-glass grant from Sensa is deliberately NOT gated: it is not the
            # customer's action to be refused, they may be unable to reach this
            # screen at all, and the tunnel applies the same rule to it regardless.
            from . import default_logins

            weak = default_logins.blocking(hass)
            if weak:
                raise HomeAssistantError(default_logins.refusal_text(weak))

        if enabled:
            hours = clamp_window_hours(
                window_hours
                if window_hours is not None
                else REMOTE_SUPPORT_DEFAULT_WINDOW_HOURS
            )
            password = _generate_password()
            await _async_activate_support_user(hass, password)
            state.enabled = True
            state.enabled_until = _now() + timedelta(hours=hours)
            state.opened_by = (
                OPENED_BY_SENSA if opened_by == OPENED_BY_SENSA else OPENED_BY_CUSTOMER
            )
            state.reason = _clean_text(reason)
            # Only ever set on a session Sensa opened. A customer's own toggle has
            # nobody to name, and carrying a stale name onto their session would
            # tell them an operator was involved when none was.
            state.granted_by = (
                _clean_text(granted_by) if state.opened_by == OPENED_BY_SENSA else None
            )
            state._password = password
            await state._async_save()
            await _async_push_to_addon(hass, entry, state)
            _async_notify_customer(
                hass,
                state.enabled_until,
                state.opened_by,
                state.reason,
                state.granted_by,
            )
            _LOGGER.warning(
                "Remote support ENABLED until %s (window %.1fh, by %s)",
                state.enabled_until.isoformat(),
                hours,
                state.opened_by,
            )
            # The customer log is the ONLY channel an appliance customer can actually
            # read back: HA's own log needs Settings, which is admin-gated, and the
            # `admin` login is not an HA admin on the Container appliance. Warning
            # rather than info so it stands out among routine system entries, and so
            # the log's type chips can find it later.
            sensa_log(
                hass,
                "warning",
                (
                    f"Sensa opened support access remotely"
                    f"{_by_text(state.granted_by)}"
                    f"{_until_text(hass, state.enabled_until)}"
                    f"{_reason_text(state.reason)}"
                    if state.opened_by == OPENED_BY_SENSA
                    else f"Sensa Support access opened{_until_text(hass, state.enabled_until)}"
                ),
                source=SENSA_LOG_SOURCE_SYSTEM,
            )
        else:
            was_open = state.enabled
            await _async_deactivate_support_user(hass)
            state.enabled = False
            state.enabled_until = None
            state.opened_by = OPENED_BY_CUSTOMER
            state.reason = None
            state.granted_by = None
            state._password = None
            await state._async_save()
            await _async_push_to_addon(hass, entry, state)
            _async_dismiss_notification(hass)
            _LOGGER.info("Remote support DISABLED")
            # Only when there was something to close. A disable of an already-closed
            # session happens on startup reconciliation and on a stray service call,
            # and logging those would train the customer to ignore the entries that
            # matter.
            if was_open:
                sensa_log(
                    hass,
                    "info",
                    _CLOSED_TEXT.get(ended, _CLOSED_TEXT["customer"]),
                    source=SENSA_LOG_SOURCE_SYSTEM,
                )

        hass.bus.async_fire(
            EVENT_SENSA_REMOTE_SUPPORT_CHANGED, {"enabled": state.enabled}
        )
        return {
            "enabled": state.enabled,
            "enabled_until": (
                state.enabled_until.isoformat() if state.enabled_until else None
            ),
        }


async def async_tick(hass: HomeAssistant, entry: Any) -> None:
    """Periodic check: close an elapsed window; retry a missed add-on push.

    The push retry runs for BOTH an enable and a disable that didn't reach the
    add-on — a lost OFF push would otherwise leave the add-on reporting the unit
    as support-open with a stale credential until the next restart.
    """
    state = get_state(hass)
    if state is None:
        return
    if state.enabled and state.enabled_until and _now() >= state.enabled_until:
        _LOGGER.info("Remote support window elapsed — auto-disabling")
        await async_set_enabled(hass, entry, False, ended="expiry")
        return
    if state._push_pending:
        await _async_push_to_addon(hass, entry, state)


async def async_honour_grant(
    hass: HomeAssistant, entry: Any, grant: dict[str, Any]
) -> None:
    """Open support in response to a break-glass grant issued from the Hub.

    Used when the customer can't reach the on-device toggle (their HA is
    glitchy, or they granted from the Hub Portal / an operator granted for
    them). Idempotent: a no-op when support is already open (so we don't
    re-rotate the credential on every poll while the cloud still advertises the
    grant — the cloud clears it once the device confirms enabled). Skipped when
    the `sensa_support` account isn't provisioned (BYO).
    """
    state = get_state(hass)
    if state is not None and state.enabled:
        return  # already open — don't re-activate / re-rotate
    if not await async_support_user_exists(hass):
        # The grant is good and we cannot act on it. Saying so is the whole
        # difference between an operator seeing "closed" and seeing "this box
        # has no sensa_support account", and it was a bare `return`: the cloud
        # re-advertised the grant on every check-in, the box collected it every
        # time, and the console showed nothing. One fault produced four
        # identical grants that way.
        _LOGGER.warning(
            "Cannot honour the Hub remote-support grant: this box has no "
            "%s account provisioned",
            SENSA_SUPPORT_USERNAME,
        )
        await async_report_blocked(hass, entry, "no_support_account")
        return
    window = clamp_window_hours(grant.get("window_hours"))
    # Both are optional on the wire: a Function older than the field sends
    # neither, and the session must open regardless. Absent means "not told",
    # never a placeholder -- see ``_reason_text`` / ``_by_text``.
    reason = _clean_text(grant.get("reason"))
    granted_by = _clean_text(grant.get("granted_by"))
    _LOGGER.warning(
        "Honouring Hub remote-support grant (window %.1fh, by %s)",
        window,
        granted_by or "unnamed operator",
    )
    await async_set_enabled(
        hass,
        entry,
        True,
        window_hours=window,
        opened_by=OPENED_BY_SENSA,
        reason=reason,
        granted_by=granted_by,
    )


async def async_report_blocked(
    hass: HomeAssistant, entry: Any, reason: str
) -> None:
    """Tell the cloud why a grant could not be honoured.

    Pushed as an OFF report carrying ``blocked_reason``, which the add-on
    persists and carries up on the existing phone-home channel. Never raises:
    this runs on the refresh path, and a box that cannot explain itself must
    still check in.

    Deliberately does NOT touch the session state. Nothing was opened, so there
    is nothing to close, and writing an enabled=False state here would look
    like a session that ended.
    """
    entry_id = entry if isinstance(entry, str) else getattr(entry, "entry_id", None)
    runtime = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    client = runtime.get("client")
    if client is None:
        return
    try:
        await client.async_post_remote_support_state(
            enabled=False,
            username=SENSA_SUPPORT_USERNAME,
            password=None,
            enabled_until=None,
            blocked_reason=reason,
        )
    except Exception:
        _LOGGER.debug("Could not report the remote-support block", exc_info=True)


async def async_honour_revoke(hass: HomeAssistant, entry: Any) -> None:
    """Close a Sensa-opened session because the cloud has withdrawn it.

    A break-glass session is bound to the support case it was opened for, and a
    closed case must not leave a door open for the rest of its window. The
    cloud cannot reach into the box, so a withdrawal rides down on the
    phone-home the same way the grant rode down.

    **Only ever closes a session Sensa opened.** A customer who turned their own
    toggle on while we happened to be looking at their case has made their own
    decision, and a stale withdrawal must not undo it. Idempotent: a no-op when
    nothing is open.
    """
    state = get_state(hass)
    if state is None or not state.enabled:
        return
    if state.opened_by != OPENED_BY_SENSA:
        _LOGGER.debug(
            "Ignoring a remote-support withdrawal: this session is the "
            "customer's own, not ours to close"
        )
        return
    _LOGGER.warning("Closing remote support: the cloud has withdrawn the grant")
    await async_set_enabled(hass, entry, False, ended="revoked")


def break_glass_opted_out(hass: HomeAssistant) -> bool | None:
    """The customer's standing answer to "may Sensa open support unasked".

    Read from the cloud's last word (the cached licence status), because the
    customer sets it in MySensa and the Licensing Function is what enforces it.
    **Display only.** Re-checking it on the box before honouring a grant would
    be the same predicate in two places, and the wrong one would eventually win.

    Three-valued on purpose. ``None`` means we have never been told (an older
    Function, a box that has not phoned home), and the panel shows nothing
    rather than asserting a position the customer never took.
    """
    from .const import RUNTIME_LICENCE_STATUS

    try:
        for runtime in (hass.data.get(DOMAIN) or {}).values():
            if not isinstance(runtime, dict):
                continue
            status = runtime.get(RUNTIME_LICENCE_STATUS) or {}
            value = status.get("break_glass_opted_out")
            if value is not None:
                return bool(value)
    except Exception:  # noqa: BLE001 — a display hint must never raise
        return None
    return None


async def async_support_user_exists(hass: HomeAssistant) -> bool:
    """True when the pre-provisioned ``sensa_support`` account is present.

    Gates the consent toggle: only offered where there's an account to activate
    (appliance images), never on a BYO customer's own HA.
    """
    return await _async_find_support_user(hass) is not None


async def async_ensure_support_user(hass: HomeAssistant) -> None:
    """Create the inactive ``sensa_support`` account on a Container appliance
    that lacks it.

    Only the build script used to create it, so a box built before that step (or
    from an image that skipped it) showed a Remote Support toggle that could
    never turn on, and checking in did not help. Creating it here means the
    next update repairs every such box. Created as the build script does it: an
    HA admin, deactivated, with a throwaway password that every enable rotates.

    Container appliance only: BYO must never gain an admin it did not ask for,
    and HAOS is not sold. Never raises: a box that cannot make the account
    still starts, and simply keeps the dead toggle until the next try.
    """
    from .appliance import _is_appliance, is_supervised

    if not _is_appliance(hass) or is_supervised(hass):
        return
    if await _async_find_support_user(hass) is not None:
        return
    from homeassistant.auth.const import GROUP_ID_ADMIN
    from homeassistant.auth.providers.homeassistant import InvalidUsername

    from .admin import _get_hass_provider

    user = None
    try:
        provider = _get_hass_provider(hass)
        user = await hass.auth.async_create_user(
            "Sensa Support", group_ids=[GROUP_ID_ADMIN]
        )
        # Dormant before it can hold a login, so a failure below never leaves
        # an active admin behind.
        await hass.auth.async_deactivate_user(user)
        try:
            await provider.async_add_auth(SENSA_SUPPORT_USERNAME, _generate_password())
        except InvalidUsername:
            # The login name is already taken by a login with no user behind it
            # (left by an earlier half-made attempt). Refusing here failed on
            # every start for ever, so take that login over with a fresh password.
            await provider.async_change_password(
                SENSA_SUPPORT_USERNAME, _generate_password()
            )
        credentials = await provider.async_get_or_create_credentials(
            {"username": SENSA_SUPPORT_USERNAME}
        )
        await hass.auth.async_link_user(user, credentials)
    except Exception:
        _LOGGER.warning(
            "Could not create the %r account, so Remote Support stays off",
            SENSA_SUPPORT_USERNAME,
            exc_info=True,
        )
        if user is not None:
            try:
                await hass.auth.async_remove_user(user)
            except Exception:
                _LOGGER.debug("Could not remove the half-made account", exc_info=True)
        return
    _LOGGER.info("Created the inactive %r account for Remote Support", SENSA_SUPPORT_USERNAME)


# --- internals -------------------------------------------------------------


def _generate_password() -> str:
    # URL-safe, ~16 chars of entropy. Rotated on every enable; the customer
    # never sees it — it travels to Sensa over phone-home.
    return secrets.token_urlsafe(12)


async def _async_find_support_user(hass: HomeAssistant) -> Any | None:
    from .appliance import _hass_username

    try:
        users = await hass.auth.async_get_users()
    except Exception:
        _LOGGER.debug("Could not enumerate HA users", exc_info=True)
        return None
    for user in users:
        if _hass_username(user) == SENSA_SUPPORT_USERNAME:
            return user
    return None


async def _async_activate_support_user(hass: HomeAssistant, password: str) -> None:
    """Activate ``sensa_support`` and set its fresh password. Raises if absent."""
    from .admin import _get_hass_provider

    user = await _async_find_support_user(hass)
    if user is None:
        raise HomeAssistantError(
            "Remote support isn't available on this install — the "
            f"{SENSA_SUPPORT_USERNAME!r} account isn't provisioned."
        )
    provider = _get_hass_provider(hass)
    try:
        await provider.async_change_password(SENSA_SUPPORT_USERNAME, password)
    except Exception:  # noqa: BLE001 — InvalidUser (no credential yet) lands here
        # The account exists but has no homeassistant credential linked — create
        # one. (Shouldn't happen: the build script links it, but be resilient.)
        try:
            await provider.async_add_auth(SENSA_SUPPORT_USERNAME, password)
            credentials = await provider.async_get_or_create_credentials(
                {"username": SENSA_SUPPORT_USERNAME}
            )
            await hass.auth.async_link_user(user, credentials)
        except Exception as exc:
            raise HomeAssistantError(
                f"Could not set the {SENSA_SUPPORT_USERNAME!r} password: {exc}"
            ) from exc
    await hass.auth.async_activate_user(user)


async def _async_deactivate_support_user(hass: HomeAssistant) -> None:
    """Deactivate ``sensa_support`` and invalidate its live sessions."""
    user = await _async_find_support_user(hass)
    if user is None:
        return
    try:
        await hass.auth.async_deactivate_user(user)
    except Exception:
        _LOGGER.debug("Deactivating sensa_support failed", exc_info=True)
    # Kill any active sessions so an OFF (or expiry) locks support out at once.
    for token in list(getattr(user, "refresh_tokens", {}).values()):
        try:
            await hass.auth.async_remove_refresh_token(token)
        except Exception:
            _LOGGER.debug("Removing a sensa_support refresh token failed", exc_info=True)


async def _async_push_to_addon(
    hass: HomeAssistant, entry: Any, state: RemoteSupportState
) -> None:
    """Send the consent state to the add-on so phone-home reports it.

    Best-effort: if the add-on is briefly unreachable the tick retries (for both
    enable AND disable). A failed OFF push must retry too, or the add-on keeps
    reporting a stale credential.
    """
    # Can't safely re-push an ENABLE without the password: after a restart we no
    # longer hold it, and the add-on already persisted the right one — pushing
    # enabled with a null password would wipe the credential it reports. Leave
    # the add-on's good state untouched.
    if state.enabled and state._password is None:
        state._push_pending = False
        return
    entry_id = entry if isinstance(entry, str) else getattr(entry, "entry_id", None)
    runtime = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    client = runtime.get("client")
    if client is None:
        state._push_pending = True
        return
    try:
        # The Zigbee page's own password, read off Z2M's config rather than
        # remembered. It rides the support state because that is exactly when
        # support needs it, and because the add-on already drops everything in
        # this payload on an OFF report. Best-effort: no Zigbee, no token, and
        # the console says so rather than showing a blank field.
        zigbee_token = None
        if state.enabled:
            try:
                from . import async_read_z2m_auth_token

                zigbee_token = await async_read_z2m_auth_token(hass)
            except Exception:
                _LOGGER.debug("Reading the Zigbee frontend token failed", exc_info=True)
        await client.async_post_remote_support_state(
            enabled=state.enabled,
            username=SENSA_SUPPORT_USERNAME,
            password=state._password,
            enabled_until=(
                state.enabled_until.isoformat() if state.enabled_until else None
            ),
            zigbee_token=zigbee_token,
        )
        state._push_pending = False
    except (ConnectionError, TimeoutError):
        _LOGGER.warning(
            "Could not reach the Sensa App to report remote-support state — "
            "will retry; Sensa won't see the change until then."
        )
        state._push_pending = True


def _until_text(hass: HomeAssistant, until: datetime | None) -> str:
    """" until HH:MM on DD Mon", or "" when there is no usable time.

    Rendered LOCAL: every timestamp on this box is stored UTC, and a customer
    reading "13:30" for a 14:30 expiry under BST is worse than no time at all."""
    if until is None:
        return ""
    try:
        from homeassistant.util import dt as dt_util

        return f" until {dt_util.as_local(until).strftime('%H:%M on %d %b')}"
    except Exception:  # noqa: BLE001
        return ""


def _reason_text(reason: str | None) -> str:
    """The operator's stated reason, or nothing at all.

    Never a placeholder: an older Function does not send a reason, and
    "...remotely. Reason: " reads as a bug rather than as an absence."""
    return f" Reason given: {reason}" if reason else ""


def _by_text(granted_by: str | None) -> str:
    """" by <named operator>", or nothing at all.

    Same rule as ``_reason_text``: a Function that does not send a name yet must
    read as an absence, never as "by (unknown)". Being able to say who is the
    point of carrying it, so a placeholder would be worse than silence."""
    return f" by {granted_by}" if granted_by else ""


def _async_notify_customer(
    hass: HomeAssistant,
    until: datetime | None,
    opened_by: str = OPENED_BY_CUSTOMER,
    reason: str | None = None,
    granted_by: str | None = None,
) -> None:
    from homeassistant.components import persistent_notification

    when = _until_text(hass, until)
    if opened_by == OPENED_BY_SENSA:
        # Say plainly that this was not them, and by whom. The old text was
        # identical either way, so a customer had no way to tell a session they
        # opened from one opened for them, which is exactly the thing they would
        # want to know.
        body = (
            f"Sensa has opened support access to this system remotely"
            f"{_by_text(granted_by)}{when}."
            f"{_reason_text(reason)} "
            "You can close it any time from Settings → Admin."
        )
        title = "Sensa opened support access"
    else:
        body = (
            f"Sensa Support can now access this system{when}. "
            "Turn it off any time from Settings → Admin."
        )
        title = "Sensa Support access enabled"
    persistent_notification.async_create(
        hass, body, title=title, notification_id=_NOTIFICATION_ID
    )


def _async_dismiss_notification(hass: HomeAssistant) -> None:
    from homeassistant.components import persistent_notification

    persistent_notification.async_dismiss(hass, _NOTIFICATION_ID)
