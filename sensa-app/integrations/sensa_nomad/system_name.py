"""Customer-set name for this system / vehicle, reported on phone-home.

The customer types a name (max :data:`~.const.SYSTEM_NAME_MAX_LENGTH` chars) on
the Settings → Diagnostics tab, at the top of the Resources panel. HA is the source of
truth: the value is persisted in ``entry.options["system_name"]`` by
``text.sensa_nomad_system_name``, so it travels in HA backups and survives an
App reinstall.

The App, not HA, owns the phone-home (its daemon runs even while HA Core is
down), so the integration PUSHES the name to the App, which persists its own
copy in ``/data/system.json`` and folds it into ``telemetry.system_name`` on
every ``/validate``. From there the licensing Function stores it verbatim and
the Hub Deployments pages show it.

Pushes are best-effort but VERIFIED. A failed push is retried every minute until
the App accepts it (the App is often still starting when HA sets up), and every
reload re-asserts the current name — including an EMPTY one, so clearing the name
reaches the App even if the clearing push was lost. That makes a wiped ``/data``,
a stale name, and an App that was down all self-heal.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, SYSTEM_NAME_MAX_LENGTH

_LOGGER = logging.getLogger(__name__)

OPTIONS_KEY = "system_name"
_RETRY_KEY = "system_name_retry_cancel"


def get_name(entry: ConfigEntry) -> str:
    """The stored name, or "" when the customer hasn't set one."""
    return str(entry.options.get(OPTIONS_KEY) or "")


def clean(value: str | None) -> str:
    """Trim + cap a candidate name. Also the store-side guard for a direct
    ``text.set_value`` that bypassed the card's maxlength."""
    return (value or "").strip()[:SYSTEM_NAME_MAX_LENGTH]


async def async_set_name(hass: HomeAssistant, entry: ConfigEntry, value: str) -> None:
    """Persist the name to entry options and push it to the App."""
    name = clean(value)
    if name != get_name(entry):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, OPTIONS_KEY: name}
        )
    # Push even when the name is unchanged: a Save on a box whose App missed the
    # last push (or lost /data) must re-deliver it rather than silently no-op.
    await async_push(hass, entry)


async def async_push(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """POST the current name to the App. Arms a retry when it doesn't land.

    An EMPTY name is pushed too — it's how a cleared name reaches the App.
    """
    runtime: dict[str, Any] = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    client = runtime.get("client")
    if client is None:
        return False
    try:
        result = await client.async_post_system_name(name=get_name(entry))
    except (ConnectionError, TimeoutError):
        _LOGGER.warning(
            "Could not reach the Sensa App to report the system name -- "
            "will retry; Sensa won't see the new name until then."
        )
        _arm_retry(hass, entry)
        return False
    except Exception:
        _LOGGER.debug("System-name push failed", exc_info=True)
        _arm_retry(hass, entry)
        return False

    # The client only RAISES for 5xx; a 4xx comes back as a parsed body carrying
    # `http_status` (see api/client.py). The one that matters is the 404 from an
    # App build older than this route: treating that as delivered would cancel the
    # retry and lose the name silently, with Hub's Name column blank forever and
    # nothing in the log. Keep retrying — it heals the moment the App updates.
    if isinstance(result, dict) and (
        result.get("http_status", 200) >= 400 or result.get("error")
    ):
        _LOGGER.warning(
            "The Sensa App rejected the system name (%s) -- will retry; it may be "
            "running a build older than this integration.",
            result.get("error") or result.get("http_status"),
        )
        _arm_retry(hass, entry)
        return False

    _cancel_retry(hass, entry)
    return True


def _arm_retry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None or runtime.get(_RETRY_KEY) is not None:
        return

    async def _tick(_now) -> None:
        # async_push cancels this timer on success and leaves it armed on failure.
        await async_push(hass, entry)

    runtime[_RETRY_KEY] = async_track_time_interval(hass, _tick, timedelta(minutes=1))
    # Tie the timer to the entry's lifetime. Without this an unload/reload discards
    # the runtime dict holding the cancel handle while the timer keeps firing, so
    # every reload with the App down orphans another one that POSTs (and warns)
    # every minute for the life of the HA process. `_cancel_retry` is idempotent,
    # so cancelling on success and again on unload is safe.
    entry.async_on_unload(lambda: _cancel_retry(hass, entry))


def _cancel_retry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return
    cancel = runtime.pop(_RETRY_KEY, None)
    if cancel is not None:
        cancel()
