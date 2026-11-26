"""Autoterm heater "Temperature Sensor Source": carrying the choice to the box.

The customer picks, on the heater's Settings -> Devices row, what the heater
regulates on in Temperature mode: the Autoterm Controller (the wall panel's
sensor), the Autoterm Heater (its own intake sensor), or one of the van's
temperature sensors. The choice lives on the config entry
(`HEATER_TEMP_SOURCE_OPTION`, written by `select.SensaHeaterTempSourceSelect`);
this module drives the controller firmware (Sensa.Control.Firmware
autoterm_uart) to match:

- its "Temperature Source" select is set to the matching firmware option, and
- for a van sensor, its "Sensa Temperature" number is written with that
  sensor's reading in degC, on every change and once a minute regardless.

The firmware forgets both on reboot and stops using a Sensa reading it has not
had for five minutes, falling back to the panel. So everything here is a
re-assertion on a timer, never a one-shot write: one lost write must not leave
the heater on the wrong sensor for ever. A van sensor that is unavailable is
"could not ask", not a reading: nothing is written, and the firmware's own
staleness fallback takes over.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    FIRMWARE_TEMP_SOURCE_SENSA,
    HEATER_TEMP_SOURCE_CONTROLLER,
    HEATER_TEMP_SOURCE_FIRMWARE,
    HEATER_TEMP_SOURCE_HEATER,
    HEATER_TEMP_SOURCE_OPTION,
)

_LOGGER = logging.getLogger(__name__)

REASSERT_INTERVAL = timedelta(seconds=60)

# (entry_id, device_id) -> whether the last push had a usable sensor reading,
# so a sensor dropping out is logged once on the edge, not every minute.
_reading_ok: dict[tuple[str, str], bool] = {}


def firmware_entities(hass: HomeAssistant, device: dict[str, Any]) -> dict[str, str]:
    """The heater's role map (controller entities by role), or empty."""
    from . import _attach_heater_entities

    try:
        rows = _attach_heater_entities(hass, [device])
    except Exception:
        _LOGGER.debug("Heater temperature source: role map failed", exc_info=True)
        return {}
    return dict((rows[0] if rows else {}).get("service_entities") or {})


def eligible(hass: HomeAssistant, device: dict[str, Any]) -> bool:
    """True when the heater's controller runs firmware that takes a source."""
    return "temp_source_select" in firmware_entities(hass, device)


def choice_for(entry: ConfigEntry, device_id: str) -> str:
    """The customer's choice for one heater; the controller when unset."""
    return (entry.options.get(HEATER_TEMP_SOURCE_OPTION) or {}).get(
        device_id, HEATER_TEMP_SOURCE_CONTROLLER,
    )


def regulating_temp_entity(role_map: dict[str, str], choice: str) -> str | None:
    """The sensor the heater is regulating on, for the card's "Now" reading.

    The one place that maps a choice to a reading, so the Climate-tab card and
    the Home summary cannot show different sensors."""
    if choice == HEATER_TEMP_SOURCE_HEATER:
        return role_map.get("current_temperature")
    if choice == HEATER_TEMP_SOURCE_CONTROLLER:
        # The panel's reading; a controller on older firmware has none, and
        # then the heater's own is the best there is.
        return role_map.get("panel_temperature") or role_map.get("current_temperature")
    return choice  # a van sensor's entity_id


def _celsius(hass: HomeAssistant, entity_id: str) -> float | None:
    """A van sensor's reading in degC, or None when it has no usable reading."""
    st = hass.states.get(entity_id)
    if st is None:
        return None
    try:
        value = float(st.state)
    except (TypeError, ValueError):
        return None  # unavailable / unknown: could not ask
    unit = str(st.attributes.get("unit_of_measurement") or "").strip()
    if unit in ("°F", "F"):
        return (value - 32.0) * 5.0 / 9.0
    if unit == "K":
        return value - 273.15
    # degC, or no unit (the header chip's rule: treat it as already right).
    return value


def _heater_devices(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinator = runtime.get("coordinator")
    devices = ((getattr(coordinator, "data", None) or {}).get("devices")) or []
    return [d for d in devices if str(d.get("device_id") or "")]


async def _async_push_one(
    hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any],
) -> None:
    device_id = str(device.get("device_id") or "")
    role_map = firmware_entities(hass, device)
    fw_select = role_map.get("temp_source_select")
    if not fw_select:
        return  # not an Autoterm controller with the source firmware
    choice = choice_for(entry, device_id)
    wanted = HEATER_TEMP_SOURCE_FIRMWARE.get(choice, FIRMWARE_TEMP_SOURCE_SENSA)

    if wanted == FIRMWARE_TEMP_SOURCE_SENSA:
        fw_number = role_map.get("sensa_temperature")
        reading = _celsius(hass, choice)
        key = (entry.entry_id, device_id)
        ok = reading is not None and fw_number is not None
        if ok != _reading_ok.get(key, True):
            if ok:
                _LOGGER.info("Heater %s: temperature from %s again", device_id, choice)
            else:
                _LOGGER.warning(
                    "Heater %s: no reading from %s; the controller falls back to "
                    "the panel's sensor after five minutes", device_id, choice,
                )
        _reading_ok[key] = ok
        if ok:
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": fw_number, "value": round(reading, 1)},
                blocking=False,
            )

    st = hass.states.get(fw_select)
    if st is None or st.state != wanted:
        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": fw_select, "option": wanted},
            blocking=False,
        )


async def async_push_heater_temp_sources(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring every Autoterm controller into line with the customer's choice."""
    for device in _heater_devices(hass, entry):
        try:
            await _async_push_one(hass, entry, device)
        except Exception:
            _LOGGER.warning(
                "Heater temperature source push failed for %s",
                device.get("device_id"), exc_info=True,
            )


@callback
def async_setup_heater_temp_source_bridge(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-assert every minute, and push at once when a chosen sensor changes."""
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_interval,
    )

    tracked: dict[str, Any] = {"ids": frozenset(), "unsub": None}

    def _chosen_sensors() -> frozenset[str]:
        ledger = entry.options.get(HEATER_TEMP_SOURCE_OPTION) or {}
        return frozenset(
            v for v in ledger.values() if v and v not in HEATER_TEMP_SOURCE_FIRMWARE
        )

    async def _on_sensor_change(_event: Event) -> None:
        await async_push_heater_temp_sources(hass, entry)

    def _retrack() -> None:
        ids = _chosen_sensors()
        if ids == tracked["ids"]:
            return
        if tracked["unsub"] is not None:
            tracked["unsub"]()
            tracked["unsub"] = None
        tracked["ids"] = ids
        if ids:
            tracked["unsub"] = async_track_state_change_event(
                hass, list(ids), _on_sensor_change,
            )

    async def _tick(_now: Any) -> None:
        _retrack()  # a new choice starts being watched within a minute
        await async_push_heater_temp_sources(hass, entry)

    _retrack()
    entry.async_on_unload(async_track_time_interval(hass, _tick, REASSERT_INTERVAL))

    @callback
    def _stop() -> None:
        if tracked["unsub"] is not None:
            tracked["unsub"]()

    entry.async_on_unload(_stop)
