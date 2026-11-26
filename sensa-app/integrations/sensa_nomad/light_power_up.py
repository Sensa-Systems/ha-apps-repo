"""The power-up routine for a relay-fed / addressable lighting controller.

A pixel controller is a computer on the end of a switched supply. Turning it on
is not one command, it is a SEQUENCE:

1. if a relay channel supplies it and that relay is off, close the relay;
2. give the controller its Power-On Delay to boot and rejoin Zigbee;
3. send the turn-on, and re-send it until the light reports on, because the
   first command routinely lands while the controller is still rejoining and is
   simply lost.

That sequence lived ONLY in the cards (sensa-led-card, sensa-light-card,
sensa-light-summary), which is why turning on a light GROUP containing an
addressable left the addressable dark (Richard, 2026-09-17, proven from an MQTT
capture: the group sent one bare state:ON to both members, the bulb came on, the
controller did not). A group turns its members on at the HA level, so none of
the card's routine applied.

So it lives here, in the integration, where every caller gets it: the group
today, and anything else that turns a light on tomorrow. The cards keep their
own copy for now because they also drive the optimistic on-screen state only a
card can show; collapsing them onto this is the follow-up recorded in
decisions.md.

Nothing here raises. A light that will not answer must not take the caller down
with it: the customer pressed a button, and the worst honest outcome is a light
that stayed off, not a failed service call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant

from .const import (
    LIGHT_POWER_ON_DELAY_DEFAULT_S,
    LIGHT_POWER_ON_DELAY_MAX_S,
    light_leave_relay_on_switch_entity_id,
    light_power_on_delay_number_entity_id,
)

_LOGGER = logging.getLogger(__name__)

# Re-send cadence and window. KEEP IN SYNC with the cards' POWER_UP_RETRY_MS /
# POWER_UP_WINDOW_MS (webcards/src/sensa-led-card.ts and friends): a customer
# watching the card must not see it give up at a different moment from the
# integration driving the same light.
POWER_UP_RETRY_S: float = 2.0
POWER_UP_WINDOW_S: float = 15.0


def _ha_device_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """The HA device-registry id that owns `entity_id`, or None.

    The Power-On Delay number and the Leave Relay On switch are both keyed on
    this id (see `const.light_power_on_delay_number_entity_id`), so it is the
    hop from "a light" to "that light's Sensa settings".
    """
    try:
        from homeassistant.helpers import entity_registry as er

        entry = er.async_get(hass).async_get(entity_id)
    except Exception:  # noqa: BLE001 - a registry miss must not break a press
        return None
    return getattr(entry, "device_id", None) if entry is not None else None


def power_on_delay_for_light(hass: HomeAssistant, entity_id: str) -> float:
    """The customer's Power-On Delay for this light, in seconds.

    Falls back to the default when the number does not exist or cannot be read:
    "could not ask" is not "no delay", and a controller told to wait 0s is the
    bug the setting exists to fix.
    """
    device_id = _ha_device_id(hass, entity_id)
    if not device_id:
        return LIGHT_POWER_ON_DELAY_DEFAULT_S
    state = hass.states.get(light_power_on_delay_number_entity_id(device_id))
    if state is None:
        return LIGHT_POWER_ON_DELAY_DEFAULT_S
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return LIGHT_POWER_ON_DELAY_DEFAULT_S
    return max(0.0, min(LIGHT_POWER_ON_DELAY_MAX_S, value))


def leave_relay_on_for_light(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether turning this light off should LEAVE its supply relay powered.

    Anything unreadable is False: cutting the relay is the default, so "could
    not ask" must never leave a van's relay energised.
    """
    device_id = _ha_device_id(hass, entity_id)
    if not device_id:
        return False
    state = hass.states.get(light_leave_relay_on_switch_entity_id(device_id))
    return state is not None and state.state == STATE_ON


def power_relay_for_light(hass: HomeAssistant, entity_id: str) -> str | None:
    """The relay switch entity supplying this light, or None.

    Resolved the same way the cards get it: a relay channel assigned to the
    device as an Integrated Device, stamped onto the enriched device list as
    `power_relay` by `_attach_power_relays`. Read from the dashboard pipeline's
    cached list, the same source `cold_charge._dcdc_power_relays` uses -- one
    reader, so a card and the integration cannot disagree about whether a light
    is relay-fed.
    """
    device_id = _ha_device_id(hass, entity_id)
    if not device_id:
        return None
    try:
        from .dashboard import get_dashboard_devices
        from .vendors.zigbee2mqtt import zigbee2mqtt_ha_device_id

        for device in get_dashboard_devices():
            relay = device.get("power_relay")
            if not relay:
                continue
            if zigbee2mqtt_ha_device_id(hass, device) == device_id:
                return str(relay)
    except Exception:
        _LOGGER.debug("power_relay lookup failed for %s", entity_id, exc_info=True)
    return None


def _is_light(entity_id: str) -> bool:
    """Whether this member is a real `light.*` entity.

    A light group may also contain a plain light wired to a relay channel and
    declared as lighting (a `switch.*` entity). That member IS its own supply:
    it has no Power-On Delay, no relay behind it and no brightness or colour,
    so the routine below sends `switch.turn_on` with nothing attached rather
    than a `light.turn_on` the entity would reject.
    """
    return entity_id.startswith("light.")


async def async_power_up(hass: HomeAssistant, entity_id: str, **attrs: Any) -> None:
    """Turn one light on, honouring its relay, delay and re-sends.

    `attrs` are the light attributes the caller wants applied (brightness,
    colour, ...). They ride on every attempt, so a colour asked for at press
    time is not lost by the attempt that finally lands. A `switch.*` member
    takes none of them.
    """
    domain = entity_id.partition(".")[0]
    is_light = _is_light(entity_id)
    if not is_light:
        # Nothing to boot and nothing to colour: the retry loop below still
        # covers a relay board that drops the first command.
        attrs = {}
    relay = power_relay_for_light(hass, entity_id) if is_light else None
    if relay:
        relay_state = hass.states.get(relay)
        if relay_state is not None and relay_state.state != STATE_ON:
            await hass.services.async_call(
                relay.split(".", 1)[0], "turn_on",
                {ATTR_ENTITY_ID: relay}, blocking=True,
            )
            # Only after CLOSING the relay: a controller already powered needs
            # no boot time, and delaying it would make every press feel slow.
            await asyncio.sleep(power_on_delay_for_light(hass, entity_id))

    waited = 0.0
    while True:
        await hass.services.async_call(
            domain, "turn_on",
            {ATTR_ENTITY_ID: entity_id, **attrs}, blocking=True,
        )
        state = hass.states.get(entity_id)
        if state is not None and state.state == STATE_ON:
            return
        if waited >= POWER_UP_WINDOW_S:
            _LOGGER.info(
                "%s did not report on within %ss of being told to; giving up",
                entity_id, POWER_UP_WINDOW_S,
            )
            return
        await asyncio.sleep(POWER_UP_RETRY_S)
        waited += POWER_UP_RETRY_S


async def async_power_down(hass: HomeAssistant, entity_id: str) -> None:
    """Turn one light off, then cut its supply relay unless the customer asked
    for the relay to stay on. The relay follows the light both ways, so nobody
    has to switch a relay channel separately from the light it feeds."""
    await hass.services.async_call(
        entity_id.partition(".")[0], "turn_off",
        {ATTR_ENTITY_ID: entity_id}, blocking=True,
    )
    if not _is_light(entity_id):
        # A relay-channel member is its own supply; there is nothing further
        # to cut, and no Leave Relay On setting to consult.
        return
    relay = power_relay_for_light(hass, entity_id)
    if not relay or leave_relay_on_for_light(hass, entity_id):
        return
    await hass.services.async_call(
        relay.split(".", 1)[0], "turn_off",
        {ATTR_ENTITY_ID: relay}, blocking=True,
    )
