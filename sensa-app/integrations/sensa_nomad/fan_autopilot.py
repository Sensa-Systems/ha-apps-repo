"""Sensa Fan Temperature Control autopilot: fan speed from temperature, quiet in Sleep.

Each of the controller's two fans follows one van temperature sensor, chosen
on the fan controller's Settings -> Devices row with four numbers beside it:

* below Start the fan is off; from Start to Max its speed rises in a straight
  line to 100%; at or above Max it runs flat out. Once running it stays on
  down to one degree under Start, so a reading hovering on the line does not
  flick the fan on and off every minute;
* while Sleep mode is on, the speed is capped at the device's Night Cap
  (0 means off at night);
* at or above the fan's Critical temperature the Night Cap is ignored.

The settings live on the config entry (`FAN_AUTOPILOT_OPTION`, written by the
select and number entities). A fan left on "Manual" is never touched, so the
customer's own speed slider works as before.

Everything is re-asserted on a one-minute timer as well as on a sensor change,
because the firmware forgets its speed on reboot. A sensor with no reading is
"could not ask", not a temperature: the fan is left at its last speed, and the
drop-out is logged once on the edge.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    FAN_AUTOPILOT_OPTION,
    FAN_CHANNELS,
    FAN_NIGHT_CAP_DEFAULT,
    FAN_SOURCE_MANUAL,
    FAN_TEMP_NUMBERS,
    fan_autopilot_key,
)

_LOGGER = logging.getLogger(__name__)

TICK = timedelta(seconds=60)
HYSTERESIS_C = 1.0
SLEEP_MODE_SLUG = "sleep"

# (entry_id, device_id, channel) -> whether the last tick had a reading, so a
# sensor dropping out is logged once on the edge, not every minute.
_reading_ok: dict[tuple[str, str, int], bool] = {}
# entry_id -> the last definite Sleep answer, carried forward while the mode
# sensor is unavailable (a None is never read as "not sleeping").
_last_sleeping: dict[str, bool] = {}


def is_fan_controller(device: dict[str, Any]) -> bool:
    """True for a Sensa Fan Temperature Control row; the one test every caller uses."""
    from .vendors.service_classes import classify

    return classify(device) == "fan_controller"


def setting(entry: ConfigEntry, device_id: str, ledger_key: str) -> Any:
    """One stored autopilot setting, or its default when never set."""
    stored = ((entry.options.get(FAN_AUTOPILOT_OPTION) or {}).get(device_id) or {})
    if ledger_key in stored:
        return stored[ledger_key]
    if ledger_key == "night_cap":
        return FAN_NIGHT_CAP_DEFAULT
    if ledger_key.endswith("_source"):
        return FAN_SOURCE_MANUAL
    if ledger_key.endswith("_auto"):
        return True
    if ledger_key.endswith("_name"):
        return ""
    key = ledger_key.split("_", 2)[-1]  # fan_1_start -> start
    spec = FAN_TEMP_NUMBERS.get(key)
    return spec[4] if spec else None


def set_setting(
    hass: HomeAssistant, entry: ConfigEntry, device_id: str, ledger_key: str, value: Any,
) -> None:
    """Store one autopilot setting on the entry (options-as-truth)."""
    ledger = dict(entry.options.get(FAN_AUTOPILOT_OPTION) or {})
    ledger[device_id] = {**(ledger.get(device_id) or {}), ledger_key: value}
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, FAN_AUTOPILOT_OPTION: ledger},
    )


def fan_speed(
    temp: float,
    *,
    start: float,
    max_temp: float,
    critical: float,
    night_cap: float,
    sleeping: bool,
    running: bool,
) -> int:
    """The speed (0-100%) one fan should run at. Pure, so it is tested alone."""
    off_below = start - HYSTERESIS_C if running else start
    if temp < off_below:
        pct = 0
    elif temp >= max_temp or max_temp <= start:
        pct = 100
    elif temp <= start:
        pct = 1  # inside the hysteresis band: slowest speed, not off
    else:
        pct = math.ceil(100 * (temp - start) / (max_temp - start))
        pct = max(1, min(100, pct))
    if sleeping and temp < critical:
        pct = min(pct, max(0, int(night_cap)))
    return pct


def fan_entities(hass: HomeAssistant, device: dict[str, Any]) -> dict[int, str]:
    """Channel -> the ESPHome `fan.*` entity for that channel, or empty."""
    from homeassistant.helpers import entity_registry as er

    from . import _esphome_candidate_entry_ids

    candidates = set(_esphome_candidate_entry_ids(hass, device))
    if not candidates:
        return {}
    names = {f"fan {ch}": ch for ch in FAN_CHANNELS}
    found: dict[int, str] = {}
    for ent in er.async_get(hass).entities.values():
        if ent.config_entry_id not in candidates or ent.disabled_by is not None:
            continue
        if ent.entity_id.split(".", 1)[0] != "fan":
            continue
        channel = names.get((ent.original_name or ent.name or "").strip().lower())
        if channel is not None:
            found.setdefault(channel, ent.entity_id)
    return found


def _fan_controllers(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    devices = ((getattr(coordinator, "data", None) or {}).get("devices")) or []
    return [d for d in devices if d.get("device_id") and is_fan_controller(d)]


def _sleeping(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .modes_catalog import mode_active_entity_id

    st = hass.states.get(mode_active_entity_id(SLEEP_MODE_SLUG))
    if st is not None and st.state in ("on", "off"):
        _last_sleeping[entry.entry_id] = st.state == "on"
    return _last_sleeping.get(entry.entry_id, False)


async def _async_drive_one(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any], sleeping: bool,
) -> None:
    from .crypto_autopilot import temperature_celsius

    device_id = str(device.get("device_id"))
    fans = fan_entities(hass, device)
    for channel, fan_eid in fans.items():
        source = setting(entry, device_id, fan_autopilot_key(channel, "source"))
        if not source or source == FAN_SOURCE_MANUAL:
            continue
        # Set by hand on the fan card: leave it until the card's Auto hands it back.
        if not setting(entry, device_id, fan_autopilot_key(channel, "auto")):
            continue
        fan_state = hass.states.get(fan_eid)
        if fan_state is None or fan_state.state not in ("on", "off"):
            continue  # the controller is offline: nothing to drive
        temp = temperature_celsius(hass.states.get(source))
        key = (entry.entry_id, device_id, channel)
        ok = temp is not None
        if ok != _reading_ok.get(key, True):
            if ok:
                _LOGGER.info("Fan %s/%s: reading from %s again", device_id, channel, source)
            else:
                _LOGGER.warning(
                    "Fan %s/%s: no reading from %s; holding the fan at its "
                    "last speed", device_id, channel, source,
                )
        _reading_ok[key] = ok
        if temp is None:
            continue
        running = fan_state.state == "on"
        pct = fan_speed(
            temp,
            start=float(setting(entry, device_id, fan_autopilot_key(channel, "start"))),
            max_temp=float(setting(entry, device_id, fan_autopilot_key(channel, "max"))),
            critical=float(setting(entry, device_id, fan_autopilot_key(channel, "critical"))),
            night_cap=float(setting(entry, device_id, "night_cap")),
            sleeping=sleeping,
            running=running,
        )
        if pct == 0:
            if running:
                await hass.services.async_call(
                    "fan", "turn_off", {"entity_id": fan_eid}, blocking=False,
                )
        elif not running or fan_state.attributes.get("percentage") != pct:
            await hass.services.async_call(
                "fan", "turn_on", {"entity_id": fan_eid, "percentage": pct},
                blocking=False,
            )


async def async_drive_fans(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring every autopiloted fan to the speed its temperature calls for."""
    sleeping = _sleeping(hass, entry)
    for device in _fan_controllers(hass, entry):
        try:
            await _async_drive_one(hass, entry, device, sleeping)
        except Exception:  # one controller must not stop the rest
            _LOGGER.warning(
                "Fan autopilot failed for %s", device.get("device_id"), exc_info=True,
            )


@callback
def async_setup_fan_autopilot(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drive every minute, and at once when a chosen sensor or Sleep changes."""
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_interval,
    )

    from .modes_catalog import mode_active_entity_id

    tracked: dict[str, Any] = {"ids": frozenset(), "unsub": None}

    def _watched() -> frozenset[str]:
        ledger = entry.options.get(FAN_AUTOPILOT_OPTION) or {}
        ids = {
            value
            for settings in ledger.values()
            for key, value in (settings or {}).items()
            if key.endswith("_source") and value and value != FAN_SOURCE_MANUAL
        }
        if ids:
            ids.add(mode_active_entity_id(SLEEP_MODE_SLUG))
        return frozenset(ids)

    async def _on_change(_event: Event) -> None:
        await async_drive_fans(hass, entry)

    def _retrack() -> None:
        ids = _watched()
        if ids == tracked["ids"]:
            return
        if tracked["unsub"] is not None:
            tracked["unsub"]()
            tracked["unsub"] = None
        tracked["ids"] = ids
        if ids:
            tracked["unsub"] = async_track_state_change_event(hass, list(ids), _on_change)

    async def _tick(_now: Any) -> None:
        _retrack()  # a newly chosen sensor is watched within a minute
        await async_drive_fans(hass, entry)

    _retrack()
    entry.async_on_unload(async_track_time_interval(hass, _tick, TICK))

    @callback
    def _stop() -> None:
        if tracked["unsub"] is not None:
            tracked["unsub"]()

    entry.async_on_unload(_stop)
