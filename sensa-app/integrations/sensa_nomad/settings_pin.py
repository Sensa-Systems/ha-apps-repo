"""Settings PIN, the elevation that opens Settings to every login.

The Settings dashboard used to be reachable only by the ``admin`` login. Getting
to it from a kiosk tablet, or from the companion app signed in as ``mobile``,
meant logging out and back in as ``admin``, which makes the companion app re-run
most of its setup. So Settings is now open to everyone and guarded by a PIN
instead: enter it and you are a full Sensa admin for a bounded idle window.

**The elevation is server-side.** Every privileged surface in the product
resolves through :func:`appliance.user_is_sensa_admin`, which consults the
elevation table here. A frontend-only gate would have shown a kiosk login a
Settings screen where every Admin action was refused, which is worse than not
opening it at all.

Two boundaries this module does NOT cross:

  * It never touches :func:`appliance.user_is_sensa_staff`. The staff-only
    Sensa tab is resolved by USERNAME and a PIN must never satisfy it.
  * It never makes anyone an HA admin. A PIN-elevated kiosk login still cannot
    reach Developer Tools, add-ons or HA's own config pages, HA's own
    permission model is untouched by any of this.

Elevations live in memory only, never in ``.storage``: a restart re-locks every
session. The PIN itself is persisted as a salted PBKDF2 hash (never the digits),
in ``.storage`` so it travels in HA backups and a restore keeps the customer's
PIN.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    SETTINGS_PIN_DEFAULT,
    SETTINGS_PIN_IDLE_SECONDS,
    SETTINGS_PIN_LOCKOUT_MAX_SECONDS,
    SETTINGS_PIN_LOCKOUT_SECONDS,
    SETTINGS_PIN_MAX_ATTEMPTS,
    SETTINGS_PIN_MAX_LEN,
    SETTINGS_PIN_MIN_LEN,
    SETTINGS_PIN_REMEMBER_OPTIONS_S,
    SETTINGS_PIN_STORAGE_KEY,
    SETTINGS_PIN_STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# hass.data keys. All four are plain dicts under hass.data[DOMAIN], created
# lazily, and none of them is persisted.
_STORE_KEY = "_settings_pin_store"
_RECORD_KEY = "_settings_pin_record"
_ELEVATED_KEY = "_settings_pin_elevated"
# Per-user window length (seconds) an elevation was granted with, keyed the
# same as _ELEVATED_KEY. Separate dict rather than widening the expiry entry
# to a tuple, so `is_elevated`'s hot path stays a plain float compare.
_ELEVATED_WINDOW_KEY = "_settings_pin_elevated_window"
_ATTEMPTS_KEY = "_settings_pin_attempts"

# PBKDF2 cost. Sized for a Pi: the verify runs in the executor, so this costs a
# thread for a moment rather than blocking the event loop.
_ITERATIONS = 200_000
_SALT_BYTES = 16


class PinError(Exception):
    """A PIN that cannot be stored (wrong length, not digits)."""


def _domain_data(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.setdefault(DOMAIN, {})


def clean_pin(value: Any) -> str:
    """Return `value` as a valid PIN string, or raise :class:`PinError`.

    Digits only and length-bounded. Deliberately strict rather than coercing:
    a PIN the customer thinks they set and cannot type is worse than a refusal.
    """
    text = str(value or "").strip()
    if not text.isdigit():
        raise PinError("A PIN must be digits only.")
    if not SETTINGS_PIN_MIN_LEN <= len(text) <= SETTINGS_PIN_MAX_LEN:
        raise PinError(
            f"A PIN must be {SETTINGS_PIN_MIN_LEN} to {SETTINGS_PIN_MAX_LEN} digits."
        )
    return text


def _hash(pin: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt), iterations
    ).hex()


def _build_record(pin: str, *, is_default: bool, enabled: bool = False) -> dict[str, Any]:
    salt = secrets.token_hex(_SALT_BYTES)
    return {
        "salt": salt,
        "hash": _hash(pin, salt, _ITERATIONS),
        "iterations": _ITERATIONS,
        "is_default": is_default,
        # Admin Lock: whether Settings asks for this PIN at all. OFF by
        # default (a fresh box, and a box from before this flag existed,
        # both open) — see `pin_enabled`.
        "enabled": enabled,
    }


def _store(hass: HomeAssistant) -> Store:
    data = _domain_data(hass)
    store = data.get(_STORE_KEY)
    if store is None:
        store = Store(hass, SETTINGS_PIN_STORAGE_VERSION, SETTINGS_PIN_STORAGE_KEY)
        data[_STORE_KEY] = store
    return store


async def async_ensure_pin(hass: HomeAssistant) -> None:
    """Load the stored PIN, seeding the default on a box that has none.

    Seeding here rather than asking the customer to set one is what makes the
    feature live the moment it deploys: an unset PIN would leave every existing
    box needing an ``admin`` login to switch it on, which is the exact thing
    this feature exists to avoid.
    """
    data = _domain_data(hass)
    record = await _store(hass).async_load()
    if not isinstance(record, dict) or not record.get("hash"):
        record = _build_record(SETTINGS_PIN_DEFAULT, is_default=True)
        await _store(hass).async_save(record)
        _LOGGER.info(
            "Settings PIN: seeded the default PIN. The customer is prompted to "
            "change it from Settings -> Admin."
        )
    data[_RECORD_KEY] = record


def pin_is_default(hass: HomeAssistant) -> bool:
    """True while the box is still on the seeded PIN (drives the banner).

    Unknown (no record loaded yet) reads False: nagging a customer whose PIN we
    have not read would be a confident claim we cannot support.
    """
    record = _domain_data(hass).get(_RECORD_KEY) or {}
    return bool(record.get("is_default"))


def pin_enabled(hass: HomeAssistant) -> bool:
    """True while Admin Lock is ON: Settings requires this PIN at all.

    OFF by default — the box opens Settings to every login (the solo/couple
    traveller case) until the customer switches Admin Lock on. Also the read
    for a box that predates this flag: a saved record with no `enabled` key
    reads OFF, not ON, because a PIN that quietly stayed mandatory across an
    update would be indistinguishable from a bug. Unknown (no record loaded
    yet) reads False for the same "haven't asked" reason `pin_is_default`
    does.
    """
    record = _domain_data(hass).get(_RECORD_KEY) or {}
    return bool(record.get("enabled", False))


async def async_set_pin_enabled(hass: HomeAssistant, enabled: bool) -> None:
    """Turn Admin Lock on or off, independent of the PIN value itself."""
    record = dict(_domain_data(hass).get(_RECORD_KEY) or {})
    record["enabled"] = bool(enabled)
    await _store(hass).async_save(record)
    _domain_data(hass)[_RECORD_KEY] = record
    _LOGGER.info("Settings PIN: Admin Lock turned %s", "on" if enabled else "off")


async def async_set_pin(hass: HomeAssistant, pin: str) -> None:
    """Persist a new PIN. Raises :class:`PinError` on an unusable one."""
    cleaned = clean_pin(pin)
    current = _domain_data(hass).get(_RECORD_KEY) or {}
    record = _build_record(cleaned, is_default=False, enabled=bool(current.get("enabled", False)))
    await _store(hass).async_save(record)
    _domain_data(hass)[_RECORD_KEY] = record
    # Everyone re-authenticates against the new PIN. Leaving live sessions up
    # would mean a customer changing the PIN because someone learnt it had not
    # actually shut that person out.
    revoke_all(hass)
    _LOGGER.info("Settings PIN: changed; all elevated sessions were ended")


async def async_reset_pin(hass: HomeAssistant) -> None:
    """Put the PIN back to the shipped default (the factory-reset path).

    Writes the seeded record rather than deleting the store, so the box comes
    back on a known PIN instead of on whatever `async_ensure_pin` would seed
    next boot -- the same value, but only if the seeding path runs, and a box
    that hands its new owner an unknown PIN is a support call. `is_default` goes
    back to True so the "change your PIN" prompt returns for them.
    """
    record = _build_record(SETTINGS_PIN_DEFAULT, is_default=True)
    await _store(hass).async_save(record)
    _domain_data(hass)[_RECORD_KEY] = record
    revoke_all(hass)
    _LOGGER.warning("Settings PIN: reset to the default by a factory reset")


# --- Rate limiting ---------------------------------------------------------
#
# Per user id, because that is the identity a service call carries. The counter
# is in memory, so a restart forgives it -- acceptable: a restart is far more
# expensive to an attacker than the lockout it clears.


def _attempts(hass: HomeAssistant) -> dict[str, dict[str, float]]:
    return _domain_data(hass).setdefault(_ATTEMPTS_KEY, {})


def lockout_remaining(hass: HomeAssistant, user_id: str | None) -> float:
    """Seconds until `user_id` may try again. 0 when they may try now."""
    if not user_id:
        return 0.0
    entry = _attempts(hass).get(user_id)
    if not entry:
        return 0.0
    return max(0.0, float(entry.get("until", 0.0)) - time.monotonic())


def record_failure(hass: HomeAssistant, user_id: str | None) -> float:
    """Count a wrong PIN; return the lockout that now applies (0 = none)."""
    if not user_id:
        return 0.0
    entry = _attempts(hass).setdefault(user_id, {"count": 0.0, "until": 0.0})
    entry["count"] = float(entry.get("count", 0.0)) + 1
    if entry["count"] < SETTINGS_PIN_MAX_ATTEMPTS:
        return 0.0
    # Doubling from the base, capped. The exponent counts lockouts served, not
    # attempts, so the second lockout is 2x the first rather than 2^5.
    served = int(entry["count"]) - SETTINGS_PIN_MAX_ATTEMPTS
    penalty = min(
        SETTINGS_PIN_LOCKOUT_SECONDS * (2 ** served),
        SETTINGS_PIN_LOCKOUT_MAX_SECONDS,
    )
    entry["until"] = time.monotonic() + penalty
    # Logged on the EDGE (the attempt that trips the lockout), never per
    # attempt -- a repeated failure must not fill the log.
    _LOGGER.warning(
        "Settings PIN: %d failed attempts; locked out for %ds",
        int(entry["count"]), int(penalty),
    )
    return float(penalty)


def _clear_failures(hass: HomeAssistant, user_id: str | None) -> None:
    if user_id:
        _attempts(hass).pop(user_id, None)


# --- Verification + elevation ----------------------------------------------


async def async_verify_pin(hass: HomeAssistant, pin: Any) -> bool:
    """True when `pin` matches the stored PIN.

    Constant-time compare, and the PBKDF2 runs in the executor so the event
    loop is not blocked for the length of the hash.
    """
    record = _domain_data(hass).get(_RECORD_KEY)
    if not isinstance(record, dict) or not record.get("hash"):
        # Could not ask, not "told no": with no record loaded there is nothing
        # to check against, so refuse rather than admit.
        _LOGGER.debug("Settings PIN: no stored record; refusing")
        return False
    candidate = str(pin or "")
    if not candidate.isdigit():
        return False
    salt = str(record.get("salt") or "")
    iterations = int(record.get("iterations") or _ITERATIONS)
    computed = await hass.async_add_executor_job(
        _hash, candidate, salt, iterations
    )
    return hmac.compare_digest(computed, str(record.get("hash")))


def _elevated(hass: HomeAssistant) -> dict[str, float]:
    return _domain_data(hass).setdefault(_ELEVATED_KEY, {})


def is_elevated(hass: HomeAssistant, user_id: str | None) -> bool:
    """True while `user_id` holds a live PIN elevation.

    Sync, so the `@callback` WS handlers and the sync role check can call it
    without an auth round-trip. Prunes as it reads.
    """
    if not user_id:
        return False
    table = _elevated(hass)
    expiry = table.get(user_id)
    if expiry is None:
        return False
    if expiry <= time.monotonic():
        table.pop(user_id, None)
        return False
    return True


def _elevated_windows(hass: HomeAssistant) -> dict[str, float]:
    return _domain_data(hass).setdefault(_ELEVATED_WINDOW_KEY, {})


def elevate(
    hass: HomeAssistant, user_id: str | None, remember_s: int | None = None
) -> None:
    """Start (or restart) `user_id`'s idle window.

    `remember_s` is the "remember me" choice from the keypad
    (SETTINGS_PIN_REMEMBER_OPTIONS_S) -- unrecognised or omitted falls back to
    the standard SETTINGS_PIN_IDLE_SECONDS window, never a customer-supplied
    number verbatim, so a WS caller can't stretch the window past what the
    keypad itself offers.
    """
    if not user_id:
        return
    # 0 ("just this time") is a valid CHOICE but not a valid WINDOW -- it
    # means "use the standard idle window server-side; the frontend is the
    # one that won't carry this across a navigation away from Settings" (see
    # the const.py comment). Anything else recognised replaces the window;
    # anything unrecognised (a stale/hostile caller) falls back the same way.
    window = (
        remember_s
        if remember_s and remember_s in SETTINGS_PIN_REMEMBER_OPTIONS_S
        else SETTINGS_PIN_IDLE_SECONDS
    )
    _elevated(hass)[user_id] = time.monotonic() + window
    _elevated_windows(hass)[user_id] = window
    _clear_failures(hass, user_id)
    _LOGGER.info(
        "Settings PIN: unlocked for user %s (%ds window)", user_id, window
    )


def touch(hass: HomeAssistant, user_id: str | None) -> bool:
    """Slide the idle window on activity. False if it had already expired.

    Deliberately will NOT resurrect an expired elevation -- the frontend has to
    ask for the PIN again, which is the whole point of the idle window. Slides
    by the SAME window length the session was elevated with (a 1-minute
    "remember" choice must not quietly grow to 15 minutes the first time the
    customer taps something), falling back to the standard idle window for a
    session that predates this per-session window (a restart-less upgrade).
    """
    if not is_elevated(hass, user_id):
        return False
    uid = str(user_id)
    window = _elevated_windows(hass).get(uid, SETTINGS_PIN_IDLE_SECONDS)
    _elevated(hass)[uid] = time.monotonic() + window
    return True


def revoke(hass: HomeAssistant, user_id: str | None) -> None:
    """End `user_id`'s elevation (the Lock button, or leaving Settings)."""
    if user_id:
        _elevated(hass).pop(user_id, None)
        _elevated_windows(hass).pop(user_id, None)


def revoke_all(hass: HomeAssistant) -> None:
    """End every elevation (a PIN change)."""
    _elevated(hass).clear()
    _elevated_windows(hass).clear()


def elevation_remaining(hass: HomeAssistant, user_id: str | None) -> float:
    """Seconds left on `user_id`'s window, 0 when not elevated."""
    if not is_elevated(hass, user_id):
        return 0.0
    return max(0.0, _elevated(hass)[str(user_id)] - time.monotonic())
