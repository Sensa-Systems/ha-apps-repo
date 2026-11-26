"""Breakdown recovery: the vehicle's cover, as the Breakdown button shows it.

The customer enters their cover in MySensa. Hub resolves the provider's current
phone number into it, the App's licence phone-home caches it on the box, and
this sensor reads that cache, so the button works with no signal at the side of
a road.

Released through Hub's ``breakdown`` feature gate. The App's cache carries the
gate as ``enabled``; the Cab builder places the tile only when it is true
(``tile_enabled``, the one predicate), and this sensor asks for a dashboard
rebuild whenever the answer differs from what the last build used.

Told vs could-not-ask: an App that cannot be reached, or has never been told,
keeps the previous answer; before any answer the sensor is unavailable, never a
confident "no cover".
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SIGNAL_DASHBOARD_REGEN_REQUESTED

_LOGGER = logging.getLogger(__name__)

ENTITY_ID = "sensor.sensa_nomad_breakdown_cover"

# What the last dashboard build assumed, so a flip of the gate rebuilds once.
_BUILT_KEY = "breakdown_tile_built"

_REFRESH_SECONDS = 60

STATE_COVERED = "covered"
STATE_EXPIRED = "expired"
STATE_NONE = "none"

# Reminder thresholds, days before the renewal date.
REMIND_DAYS = (30, 7)


async def async_fetch(hass: HomeAssistant) -> dict[str, Any] | None:
    """The App's cached answer, or None when it could not be asked (or has
    never been told anything)."""
    import socket

    import aiohttp

    from .backend_url import resolve_backend_url

    try:
        base = resolve_backend_url(hass)
    except Exception:  # noqa: BLE001
        return None
    url = f"{base.rstrip('/')}/api/v1/breakdown"
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session, session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("breakdown: App lookup failed", exc_info=True)
        return None
    if not isinstance(data, dict) or not data.get("known"):
        return None
    cover = data.get("cover")
    return {
        "enabled": bool(data.get("enabled")),
        "cover": cover if isinstance(cover, dict) else None,
    }


def tile_enabled(hass: Any) -> bool:
    """Whether the Breakdown tile belongs on the Cab. The only reader of the
    gate on the integration side; the builder and the alert rules call it."""
    if hass is None:
        return False
    st = hass.states.get(ENTITY_ID)
    return bool(st and st.attributes.get("enabled"))


def record_built(hass: Any, enabled: bool) -> None:
    """Called by the Cab builder with what it placed."""
    if hass is not None:
        hass.data.setdefault(DOMAIN, {})[_BUILT_KEY] = enabled


def days_left(expires_on: str | None, today: date | None = None) -> int | None:
    if not expires_on:
        return None
    try:
        end = date.fromisoformat(str(expires_on)[:10])
    except ValueError:
        return None
    return (end - (today or date.today())).days


def reminder_due(hass: Any, days: int) -> bool:
    """True inside the window ``(next threshold, days]`` before renewal, so each
    threshold fires once as the date approaches."""
    if not tile_enabled(hass):
        return False
    st = hass.states.get(ENTITY_ID)
    left = st.attributes.get("days_left") if st else None
    if not isinstance(left, int):
        return False
    lower = max([d for d in REMIND_DAYS if d < days], default=-1)
    return lower < left <= days


def expired_due(hass: Any) -> bool:
    if not tile_enabled(hass):
        return False
    st = hass.states.get(ENTITY_ID)
    return bool(st and st.state == STATE_EXPIRED)


def reminder_message(hass: Any) -> str:
    st = hass.states.get(ENTITY_ID)
    attrs = st.attributes if st else {}
    who = attrs.get("provider_name") or "Your breakdown cover"
    left = attrs.get("days_left")
    if isinstance(left, int) and left < 0:
        return (
            f"{who} ran out on {attrs.get('expires_on')}. Renew it, then update "
            "the date in MySensa."
        )
    return (
        f"{who} renews in {left} days ({attrs.get('expires_on')}). Once renewed, "
        "update the date in MySensa."
    )


class SensaBreakdownCoverSensor(SensorEntity, RestoreEntity):
    """The vehicle's breakdown cover. State ``covered`` / ``expired`` / ``none``;
    attributes carry what the Breakdown popup shows, plus ``enabled`` (the gate).

    Restored across a restart, so the tile and the number are there at boot
    before the App has been asked.
    """

    _attr_has_entity_name = False
    _attr_name = "Sensa Nomad Breakdown Cover"
    _attr_icon = "mdi:tow-truck"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True
    # A policy number has no business in long-term statistics.
    _unrecorded_attributes = frozenset(
        {"policy_number", "phone", "abroad_phone", "vehicle_reg", "notes"}
    )

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_breakdown_cover"
        self.entity_id = ENTITY_ID
        self._answer: dict[str, Any] | None = None
        self._last_fetch = -float(_REFRESH_SECONDS)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    @property
    def available(self) -> bool:
        return self._answer is not None

    @property
    def native_value(self) -> str | None:
        if self._answer is None:
            return None
        cover = self._answer.get("cover")
        if not self._answer.get("enabled") or not cover:
            return STATE_NONE
        left = days_left(cover.get("expires_on"))
        return STATE_EXPIRED if left is not None and left < 0 else STATE_COVERED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        answer = self._answer or {}
        cover = answer.get("cover") or {}
        return {
            "enabled": bool(answer.get("enabled")),
            "provider_name": cover.get("provider_name") or "",
            "phone": cover.get("phone") or "",
            "abroad_phone": cover.get("abroad_phone") or "",
            "policy_number": cover.get("policy_number") or "",
            "vehicle_reg": cover.get("vehicle_reg") or "",
            "expires_on": cover.get("expires_on"),
            "days_left": days_left(cover.get("expires_on")),
            "notes": cover.get("notes") or "",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unavailable", "unknown"):
            a = last.attributes
            self._answer = {
                "enabled": bool(a.get("enabled")),
                "cover": (
                    {
                        k: a.get(k)
                        for k in (
                            "provider_name",
                            "phone",
                            "abroad_phone",
                            "policy_number",
                            "vehicle_reg",
                            "expires_on",
                            "notes",
                        )
                    }
                    if last.state != STATE_NONE
                    else None
                ),
            }

    async def async_update(self) -> None:
        now = time.monotonic()
        if now - self._last_fetch < _REFRESH_SECONDS:
            return
        self._last_fetch = now
        answer = await async_fetch(self.hass)
        if answer is not None:
            # Told: replace. Could not ask: keep what we had.
            self._answer = answer
        enabled = bool(self._answer and self._answer.get("enabled"))
        built = self.hass.data.get(DOMAIN, {}).get(_BUILT_KEY)
        if built is not None and built != enabled:
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            # Written first, so the rebuild reads the new answer.
            self.async_write_ha_state()
            async_dispatcher_send(self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED)
