"""Diesel burned by the Autoterm heaters, and how long the tank lasts at it.

Every Autoterm controller publishes its heater's fuel pump frequency (the Air
from its status frame, the Flow and Baltica from the extended diagnostics
stream, firmware 1.1.4 / 1.1.11). Each pump stroke meters a fixed dose, printed
on the pump's label per 100 strokes (Autoterm's fuel pump tester manual), so

    litres per hour = strokes per second x dose per stroke x 3600

Per heater this makes two sensors: Diesel Use (litres per hour, live) and
Diesel Per Day (the average over the last `AVERAGE_DAYS` calendar days). One
van-wide sensor, Diesel Days Left, divides the diesel in the tank by the
heaters' combined daily average. It counts the heaters only: an engine on the
same tank is not in the average.

"Could not ask" is not zero, as in `energy_counters`: an interval is only
counted when both ends were read, and a gap longer than `MAX_GAP_S` adds
nothing. The daily litres live in an HA Store, not in entity state, so they
survive a restart on which the backend has not answered yet.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, _relay_device_id_safe

_LOGGER = logging.getLogger(__name__)

# Dose per pump stroke, ml, by heater model (`heater_diagnostics.model_for`).
# Pump labels give ml per 100 strokes: the TH-9 for the Air 2D and the Flow 5
# meters 4.4, the one for the Air 4D 6.8 (Autoterm's part listings); the Air
# 9D takes the 4D's TH-11, 6.8 (retailers list it for the 4D, 8D and 9D).
# Checked against the retailers' listings, NOT measured on our heaters.
DOSE_ML_PER_STROKE: dict[str, float] = {
    "air_2d": 0.044,
    "air_4d": 0.068,
    "air_9d": 0.068,
    "flow_5": 0.044,
}

# The Autoterm catalog lines: Air D, Air 9D, Flow 5D, Baltica and the Baltica
# Pro (PoE).
AUTOTERM_CATALOG_IDS = frozenset({
    "sensa_autoterm_adh", "sensa_autoterm_a9d", "sensa_autoterm_flow",
    "sensa_autoterm_abal", "sensa_autoterm_abalp",
})

AVERAGE_DAYS = 7
# Daily litres are kept this long, for the SensaClimate Diesel cost chart; the
# Diesel Per Day average still reads only the last AVERAGE_DAYS of them.
HISTORY_DAYS = 90
SAMPLE_INTERVAL = timedelta(seconds=10)
MAX_GAP_S = 300.0
SAVE_DELAY_S = 60
STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.diesel_use"
SIGNAL_DIESEL_UPDATED = f"{DOMAIN}_diesel_updated"

DAYS_LEFT_ENTITY_ID = "sensor.sensa_nomad_diesel_days_left"
TOTAL_COST_ENTITY_ID = "sensor.sensa_nomad_diesel_cost"


def cost_entity_id(device_id: str) -> str:
    return f"sensor.sensa_{_relay_device_id_safe(device_id)}_diesel_cost"


# Radar's price units (a fuel price's `price_unit`): the currency each MINOR
# unit belongs to. Absent means pence (Radar's own rule). Anything else is not
# a currency this knows, and gives no cost rather than a wrong one.
PRICE_UNIT_CURRENCY: dict[str | None, str] = {None: "GBP", "p": "GBP", "c": "EUR"}


def diesel_price(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any] | None:
    """The nearest priced diesel (Radar, `radar_coordinator.RADAR_DIESEL_PRICE_KEY`):
    {"per_litre": major units, "currency": ISO code, "minor": the raw price,
    "station": name, "distance_m": metres}, or None with no price nearby."""
    from .radar_coordinator import DIESEL_PRICE_FUEL, RADAR_DIESEL_PRICE_KEY

    runtime = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    coordinator = runtime.get("radar_coordinator")
    place = ((getattr(coordinator, "data", None) or {}).get(RADAR_DIESEL_PRICE_KEY)) or None
    if not isinstance(place, dict):
        return None
    minor = (place.get("prices") or {}).get(DIESEL_PRICE_FUEL)
    currency = PRICE_UNIT_CURRENCY.get(place.get("price_unit"))
    if not isinstance(minor, (int, float)) or minor <= 0 or currency is None:
        return None
    return {
        "per_litre": minor / 100.0,
        "currency": currency,
        "minor": minor,
        "station": place.get("name"),
        "distance_m": place.get("distance_m"),
    }


def use_entity_id(device_id: str) -> str:
    return f"sensor.sensa_{_relay_device_id_safe(device_id)}_diesel_use"


def fan_entity_id(device_id: str) -> str:
    return f"sensor.sensa_{_relay_device_id_safe(device_id)}_fan_speed"


def per_day_entity_id(device_id: str) -> str:
    return f"sensor.sensa_{_relay_device_id_safe(device_id)}_diesel_per_day"


def is_autoterm(device: dict[str, Any]) -> bool:
    """An Autoterm controller row, by catalog id, else by the catalog entry its
    model matches (a registered record can carry `model` only)."""
    if device.get("catalog_id") in AUTOTERM_CATALOG_IDS:
        return True
    model = str(device.get("vendor_model") or device.get("model") or "")
    if not model:
        return False
    try:
        from .sensor import _find_catalog_by_vendor_model

        return (_find_catalog_by_vendor_model(model) or {}).get("id") in AUTOTERM_CATALOG_IDS
    except Exception:  # noqa: BLE001 - never fatal: not an Autoterm
        return False


def dose_ml(entry: ConfigEntry, row: dict[str, Any]) -> float | None:
    """ml of diesel per pump stroke for a heater row, or None when unknown (not
    an Autoterm, or an Air whose Heater Model has not been chosen)."""
    from .heater_diagnostics import model_for

    if not is_autoterm(row):
        return None
    return DOSE_ML_PER_STROKE.get(model_for(entry, row) or "")


def litres_per_hour(pump_hz: float, dose: float) -> float:
    return pump_hz * dose * 3600.0 / 1000.0


def _hz(state: Any) -> float | None:
    """A fuel pump state in Hz, or None when it cannot be read."""
    if state is None:
        return None
    unit = (getattr(state, "attributes", None) or {}).get("unit_of_measurement")
    if unit not in ("Hz", None):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


# A diagnostics frame older than this is not a pump reading: the stream has
# stopped, and a held rate would read as diesel still burning.
FRAME_FRESH_S = 60.0


def hz_from_frame(state: Any, model: str | None, now: float | None = None) -> float | None:
    """The pump rate decoded from a fresh extended diagnostics frame (the same
    decode the Diagnostics popup uses, `autoterm_diag.decode`), or None.

    For a controller that publishes the frame but no pump reading: a Flow 5D
    on heater firmware 10.2.4.1 sends a 56-byte frame that controller firmware
    up to Flow 1.1.12 will not read a rate from (it wants 58), though its [15]
    is the pump exactly as in the 58-byte layout (the Engine PreHeater on .116,
    2026-10-08). Flow 1.1.13 reads it itself; this covers the box until then."""
    return _frame_reading(state, model, "fuel_pump_hz", now)


# The Flow family's fan reading is `measured_rev` (p[12]) in the diagnostics
# frame. Its scale is not in Autoterm's map; x60 rpm is the Air's status-frame
# scale for the same byte, UNVERIFIED on a Flow (Richard, 2026-10-11).
FAN_REV_TO_RPM = 60


def fan_rpm_from_frame(state: Any, model: str | None, now: float | None = None) -> int | None:
    """The measured fan speed in rpm from a fresh diagnostics frame, or None."""
    rev = _frame_reading(state, model, "measured_rev", now)
    return None if rev is None else int(rev) * FAN_REV_TO_RPM


def _frame_reading(state: Any, model: str | None, key: str, now: float | None) -> float | None:
    """One reading from a fresh diagnostics frame (`autoterm_diag.decode`), or None."""
    if state is None or not model:
        return None
    raw = str(getattr(state, "state", "") or "")
    if raw in ("", "unknown", "unavailable"):
        return None
    updated = getattr(state, "last_updated", None)
    if updated is not None and hasattr(updated, "timestamp"):
        age = (now if now is not None else time.time()) - updated.timestamp()
        if age > FRAME_FRESH_S:
            return None
    from .autoterm_diag import decode

    decoded = decode(model, raw) or {}
    value = (decoded.get("readings") or {}).get(key)
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


class DieselLedger:
    """Litres per heater per calendar day, made by trapezoidal integration of
    litres per hour. Pure (no HA), so the maths is testable on its own."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.heaters: dict[str, dict[str, Any]] = {}
        # date -> {"per_litre": major units, "currency": ISO}: the nearest
        # priced diesel last seen that day, so a day is costed at its own price.
        self.prices: dict[str, dict[str, Any]] = {
            str(k): dict(v) for k, v in ((data or {}).get("prices") or {}).items() if isinstance(v, dict)
        }
        for device_id, rec in ((data or {}).get("heaters") or {}).items():
            days = {str(k): float(v) for k, v in (rec.get("days") or {}).items()}
            self.heaters[str(device_id)] = {
                "name": rec.get("name"),
                "since": float(rec["since"]) if rec.get("since") is not None else None,
                "days": days,
            }
            if rec.get("retired"):
                self.heaters[str(device_id)]["retired"] = True
        self._last: dict[str, tuple[float, float]] = {}

    def to_dict(self) -> dict[str, Any]:
        return {"heaters": self.heaters, "prices": self.prices}

    def record_price(self, today: date, per_litre: float, currency: str) -> None:
        """The day's diesel price: the last one seen that day wins."""
        self.prices[today.isoformat()] = {"per_litre": float(per_litre), "currency": currency}
        oldest = (today - timedelta(days=HISTORY_DAYS)).isoformat()
        for key in [k for k in self.prices if k < oldest]:
            del self.prices[key]

    def remember(self, device_id: str, name: str | None) -> None:
        rec = self.heaters.setdefault(device_id, {"name": name, "since": None, "days": {}})
        if name:
            rec["name"] = name

    def sample(
        self, device_id: str, t: float, wall: float, today: date, lph: float | None,
    ) -> float:
        """Add the litres since the previous reading to `today`; return them.
        `t` is a monotonic clock, `wall` epoch seconds (when tracking began)."""
        if lph is None:
            self._last.pop(device_id, None)  # could not ask: bridge nothing
            return 0.0
        rec = self.heaters.setdefault(device_id, {"name": None, "since": None, "days": {}})
        if rec["since"] is None:
            rec["since"] = wall
        added = 0.0
        last = self._last.get(device_id)
        if last is not None:
            t0, lph0 = last
            dt = t - t0
            if 0.0 < dt <= MAX_GAP_S:
                added = (lph0 + lph) / 2.0 * dt / 3600.0
                key = today.isoformat()
                rec["days"][key] = rec["days"].get(key, 0.0) + added
        self._last[device_id] = (t, lph)
        self._prune(rec, today)
        return added

    @staticmethod
    def _prune(rec: dict[str, Any], today: date) -> None:
        oldest = (today - timedelta(days=HISTORY_DAYS)).isoformat()
        for key in [k for k in rec["days"] if k < oldest]:
            del rec["days"][key]

    def today(self, device_id: str, today: date) -> float:
        rec = self.heaters.get(device_id) or {}
        return float((rec.get("days") or {}).get(today.isoformat(), 0.0))

    def per_day(self, device_id: str, wall: float, today: date) -> float | None:
        """Litres per day over the last AVERAGE_DAYS days (today included), or
        None before the first reading. A heater tracked for less than that is
        averaged over the time it has been tracked, and never under one day,
        so the first hour's burn is not multiplied up to a day."""
        rec = self.heaters.get(device_id)
        if not rec or rec.get("since") is None:
            return None
        first = (today - timedelta(days=AVERAGE_DAYS - 1)).isoformat()
        litres = sum(v for k, v in rec["days"].items() if k >= first)
        tracked_days = (wall - float(rec["since"])) / 86400.0
        return litres / min(float(AVERAGE_DAYS), max(1.0, tracked_days))


# Beyond this the "estimate" is a trickle of burn divided into a full tank (box
# .236 showed 2,665,180 days): not a number anyone can use, so it is unknown.
MAX_DAYS_LEFT = 365.0


def days_left(litres: float | None, per_day_total: float) -> float | None:
    """Days the diesel lasts at the combined average, or None when there is no
    figure or it is past MAX_DAYS_LEFT."""
    if litres is None or per_day_total <= 0:
        return None
    days = litres / per_day_total
    return days if days <= MAX_DAYS_LEFT else None


class DieselTracker:
    """Samples every Autoterm heater's fuel pump every SAMPLE_INTERVAL into one
    `DieselLedger` per entry, and tells the sensors."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._clock = clock
        self._store: Store = Store(hass, STORE_VERSION, f"{STORE_KEY}.{entry.entry_id}")
        self.ledger = DieselLedger()
        # device_id -> (litres per hour or None, pump Hz or None, why None)
        self.live: dict[str, tuple[float | None, float | None, str | None]] = {}
        # device_id -> measured fan rpm from the diagnostics frame, or None.
        self.fan: dict[str, int | None] = {}
        self._unsub: Callable[[], None] | None = None

    async def async_load(self) -> None:
        try:
            data = await self._store.async_load()
        except Exception:  # a bad file starts a fresh ledger
            _LOGGER.warning("Diesel use: could not read the saved ledger", exc_info=True)
            data = None
        self.ledger = DieselLedger(data)

    def start(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_interval(self.hass, self._async_tick, SAMPLE_INTERVAL)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def known_heaters(self) -> dict[str, str]:
        """device_id -> name for every heater the ledger has seen and the
        customer has not removed."""
        return {
            k: str(v.get("name") or "Heater")
            for k, v in self.ledger.heaters.items()
            if not v.get("retired")
        }

    @callback
    def retire_heater(self, device_id: str) -> None:
        """The customer deleted this heater: stop offering it, or setup rebuilds
        its sensors from the ledger on every start. Its days stay, because the
        cost history is diesel that was really bought."""
        rec = self.ledger.heaters.get(device_id)
        if rec is None or rec.get("retired"):
            return
        rec["retired"] = True
        self.live.pop(device_id, None)
        self.fan.pop(device_id, None)
        self._store.async_delay_save(self.ledger.to_dict, SAVE_DELAY_S)

    @callback
    def revive_heater(self, device_id: str) -> None:
        """Only the add path clears `retired`, never a sample: a tick can still
        see the deleted heater in the coordinator's last answer."""
        rec = self.ledger.heaters.get(device_id)
        if rec is not None and rec.pop("retired", None):
            self._store.async_delay_save(self.ledger.to_dict, SAVE_DELAY_S)

    async def _async_tick(self, _now: Any = None) -> None:
        # A coroutine: HA runs a sync timer callback in an executor thread.
        self.sample_once()

    @callback
    def sample_once(self) -> None:
        from .heater_diagnostics import heater_rows

        try:
            rows = heater_rows(self.hass, self.entry)
        except Exception:  # a resolver hiccup is "could not ask"
            _LOGGER.debug("Diesel use: heater rows failed", exc_info=True)
            rows = []
        t = self._clock()
        wall = time.time()
        today = dt_util.now().date()
        seen: set[str] = set()
        for row in rows:
            device_id = str(row.get("device_id") or "")
            if not device_id or not is_autoterm(row):
                continue
            seen.add(device_id)
            self.ledger.remember(device_id, row.get("display_name") or row.get("name"))
            dose = dose_ml(self.entry, row)
            roles = row.get("service_entities") or {}
            pump_eid = roles.get("fuel_pump")
            hz = _hz(self.hass.states.get(pump_eid)) if pump_eid else None
            frame_eid = roles.get("diagnostics_frame")
            from .heater_diagnostics import model_for

            frame = self.hass.states.get(frame_eid) if frame_eid else None
            model = model_for(self.entry, row)
            if hz is None and frame_eid:
                # No pump reading from the controller: the frame it does send.
                hz = hz_from_frame(frame, model)
            self.fan[device_id] = fan_rpm_from_frame(frame, model)
            if dose is None:
                why = "Choose the Heater Model"
            elif not pump_eid and hz is None:
                why = "This controller reports no fuel pump (update its firmware)"
            elif hz is None:
                why = "No fuel pump reading"
            else:
                why = None
            lph = litres_per_hour(hz, dose) if dose is not None and hz is not None else None
            self.live[device_id] = (lph, hz, why)
            self.ledger.sample(device_id, t, wall, today, lph)
        # A heater the backend did not list this time: nothing known, nothing bridged.
        for device_id in list(self.live):
            if device_id not in seen:
                self.live[device_id] = (None, None, "Heater not reporting")
                self.fan[device_id] = None
                self.ledger.sample(device_id, t, wall, today, None)
        price = diesel_price(self.hass, self.entry)
        if price is not None:
            self.ledger.record_price(today, price["per_litre"], price["currency"])
        self._store.async_delay_save(self.ledger.to_dict, SAVE_DELAY_S)
        async_dispatcher_send(self.hass, SIGNAL_DIESEL_UPDATED)

    def per_day(self, device_id: str) -> float | None:
        return self.ledger.per_day(device_id, time.time(), dt_util.now().date())

    def today(self, device_id: str) -> float:
        return self.ledger.today(device_id, dt_util.now().date())


def cost_history(
    ledger: DieselLedger, today: date, days: int, price_now: dict[str, Any] | None,
) -> dict[str, Any]:
    """Each heater's diesel cost per day over the last `days` days (today
    included), for SensaClimate's Diesel cost chart. Pure, so testable.

    A day is costed at the price recorded that day; a day with none (before
    prices were recorded, or no price nearby that day) at today's, and listed
    in `estimated`. With no price at all, there are no costs: `cost` is None
    and only the litres are given. One currency: a day recorded in another
    (the van crossed a border) is costed at today's price instead."""
    days = max(1, min(int(days), HISTORY_DAYS))
    dates = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    currency = (price_now or {}).get("currency")
    if currency is None:
        recorded = [v.get("currency") for v in ledger.prices.values() if v.get("currency")]
        currency = recorded[-1] if recorded else None
    estimated: list[str] = []
    per_litre: list[float | None] = []
    for d in dates:
        rec = ledger.prices.get(d)
        if rec and rec.get("currency") == currency and isinstance(rec.get("per_litre"), (int, float)):
            per_litre.append(float(rec["per_litre"]))
        elif price_now is not None and price_now.get("currency") == currency:
            per_litre.append(float(price_now["per_litre"]))
            estimated.append(d)
        else:
            per_litre.append(None)
    heaters = []
    for device_id, rec in ledger.heaters.items():
        litres = [round(float((rec.get("days") or {}).get(d, 0.0)), 3) for d in dates]
        if not any(litres):
            continue  # nothing burned in the period: no band
        cost = [None if p is None else round(l * p, 2) for l, p in zip(litres, per_litre)]
        heaters.append({
            "id": device_id,
            "name": str(rec.get("name") or "Heater"),
            "litres": litres,
            "cost": cost,
        })
    heaters.sort(key=lambda h: h["name"].lower())  # colour follows the heater, never its rank
    return {
        "dates": dates,
        "currency": currency,
        "per_litre": per_litre,
        "estimated": estimated,
        "heaters": heaters,
    }


def tracker(hass: HomeAssistant, entry: ConfigEntry) -> DieselTracker | None:
    return ((hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}).get("diesel_tracker")


def diesel_for(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> dict[str, Any] | None:
    """One heater's diesel for the Diagnostics popup and the service report:
    the same tracker the sensors read, plus why there is no figure. None for
    a heater the tracker does not follow (not an Autoterm)."""
    dt = tracker(hass, entry)
    if dt is None or device_id not in dt.known_heaters():
        return None
    lph, hz, why = dt.live.get(device_id, (None, None, None))
    per_day = dt.per_day(device_id)
    return {
        "use_lph": None if lph is None else round(lph, 3),
        "pump_hz": hz,
        "today_l": round(dt.today(device_id), 2),
        "per_day_l": None if per_day is None else round(per_day, 2),
        "average_days": AVERAGE_DAYS,
        "reason": why,
    }


async def async_setup_tracker(hass: HomeAssistant, entry: ConfigEntry) -> DieselTracker:
    """The entry's tracker, loaded and ticking; stopped when the entry unloads."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    existing = runtime.get("diesel_tracker")
    if existing is not None:
        return existing
    dt = DieselTracker(hass, entry)
    await dt.async_load()
    runtime["diesel_tracker"] = dt
    dt.start()
    entry.async_on_unload(dt.stop)
    return dt


# -- the tank ------------------------------------------------------------------


def _float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    st = hass.states.get(entity_id) if entity_id else None
    try:
        return float(st.state) if st is not None else None
    except (TypeError, ValueError):
        return None


def tank_litres(hass: HomeAssistant, entry: ConfigEntry) -> tuple[float | None, list[str], str | None]:
    """(litres, tank names, why None) in the diesel tanks the heaters draw from:
    the tanks nominated in Climate Control when any are, else every diesel
    tank. A tank that cannot give litres makes the total unknown, never a
    smaller number."""
    from .climate_control import EXTRA_HEATERS, nominated
    from .dashboard import _device_role_map, _tank_levels_by_fluid
    from .mopeka import capacity_for, is_mopeka

    tanks: list[tuple[str, dict[str, Any]]] = []
    try:
        by_fluid = _tank_levels_by_fluid()
    except Exception:  # noqa: BLE001 - dashboard not built yet
        by_fluid = {}
    for fluid in ("diesel", "fuel"):
        tanks.extend(by_fluid.get(fluid) or [])
    chosen = {nominated(entry, f"{slot}_tank") for slot in ("heater", *EXTRA_HEATERS)} - {None}
    if chosen:
        tanks = [(eid, d) for eid, d in tanks if eid in chosen]
    if not tanks:
        return None, [], "No diesel tank level"
    total = 0.0
    names: list[str] = []
    for level_eid, device in tanks:
        name = str(device.get("display_name") or device.get("name") or "Diesel tank")
        names.append(name)
        remaining = _float_state(hass, _device_role_map(device).get("remaining"))
        if remaining is None and is_mopeka(device):
            level = _float_state(hass, level_eid)
            capacity = capacity_for(entry, str(device.get("device_id") or ""))
            if capacity is None:
                return None, names, f"Set the Tank Capacity of {name}"
            if level is not None:
                remaining = level / 100.0 * capacity
        if remaining is None:
            return None, names, f"No reading from {name}"
        total += remaining
    return total, names, None


# -- sensors -------------------------------------------------------------------


def _hub_device_info(entry: ConfigEntry) -> dict[str, Any]:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": "Sensa Nomad",
        "manufacturer": "Sensa",
        "model": "Nomad Integration",
    }


class _DieselSensor(SensorEntity):
    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    @property
    def device_info(self) -> dict[str, Any]:
        return _hub_device_info(self._entry)

    def _tracker(self) -> DieselTracker | None:
        return tracker(self.hass, self._entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_DIESEL_UPDATED, self.async_write_ha_state)
        )


class SensaDieselUseSensor(_DieselSensor):
    """Litres per hour one heater is burning now."""

    _attr_icon = "mdi:fuel"
    _attr_native_unit_of_measurement = "L/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, entry: ConfigEntry, device_id: str, name: str) -> None:
        super().__init__(entry)
        self._device_id = device_id
        self._attr_name = f"{name} Diesel Use"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_diesel_use"
        self.entity_id = use_entity_id(device_id)

    def _live(self) -> tuple[float | None, float | None, str | None]:
        dt = self._tracker()
        return dt.live.get(self._device_id, (None, None, None)) if dt else (None, None, None)

    @property
    def native_value(self) -> float | None:
        lph = self._live()[0]
        return None if lph is None else round(lph, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _lph, hz, why = self._live()
        return {"pump_hz": hz, "reason": why}


class SensaHeaterFanSpeedSensor(_DieselSensor):
    """A heater's measured fan speed from its diagnostics frame (rpm): the
    Flow and Baltica cards' Fan speed row, since their controllers publish
    none of their own. Unknown while no fresh frame arrives."""

    _attr_icon = "mdi:fan-auto"
    _attr_native_unit_of_measurement = "rpm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, device_id: str, name: str) -> None:
        super().__init__(entry)
        self._device_id = device_id
        self._attr_name = f"{name} Fan Speed"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_fan_speed"
        self.entity_id = fan_entity_id(device_id)

    @property
    def native_value(self) -> int | None:
        dt = self._tracker()
        return dt.fan.get(self._device_id) if dt else None


class SensaDieselPerDaySensor(_DieselSensor):
    """One heater's average litres per day over the last AVERAGE_DAYS days."""

    _attr_icon = "mdi:fuel"
    _attr_native_unit_of_measurement = "L/day"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, entry: ConfigEntry, device_id: str, name: str) -> None:
        super().__init__(entry)
        self._device_id = device_id
        self._attr_name = f"{name} Diesel Per Day"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_diesel_per_day"
        self.entity_id = per_day_entity_id(device_id)

    @property
    def native_value(self) -> float | None:
        dt = self._tracker()
        value = dt.per_day(self._device_id) if dt else None
        return None if value is None else round(value, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dt = self._tracker()
        return {
            "litres_today": round(dt.today(self._device_id), 2) if dt else None,
            "average_days": AVERAGE_DAYS,
        }


class SensaDieselCostSensor(_DieselSensor):
    """What one heater's diesel costs a day: its Diesel Per Day at the nearest
    fuel station's diesel price (Sensa Radar). No price nearby, or no average
    yet, is no figure, never zero."""

    _attr_icon = "mdi:cash"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(self, entry: ConfigEntry, device_id: str, name: str) -> None:
        super().__init__(entry)
        self._device_id = device_id
        self._attr_name = f"{name} Diesel Cost"
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_diesel_cost"
        self.entity_id = cost_entity_id(device_id)

    def _compute(self) -> tuple[float | None, dict[str, Any] | None, float | None]:
        dt = self._tracker()
        per_day = dt.per_day(self._device_id) if dt else None
        price = diesel_price(self.hass, self._entry)
        if per_day is None or price is None:
            return None, price, per_day
        return per_day * price["per_litre"], price, per_day

    @property
    def native_unit_of_measurement(self) -> str | None:
        price = diesel_price(self.hass, self._entry)
        return price["currency"] if price else None

    @property
    def native_value(self) -> float | None:
        cost = self._compute()[0]
        return None if cost is None else round(cost, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _cost, price, per_day = self._compute()
        return {
            "per": "day",
            "litres_per_day": None if per_day is None else round(per_day, 2),
            "price_per_litre": None if price is None else round(price["per_litre"], 3),
            "station": None if price is None else price["station"],
            "station_distance_m": None if price is None else price["distance_m"],
        }


def heaters_per_day(dt: DieselTracker | None) -> tuple[float, list[str]]:
    """(litres a day all heaters burn together, the heaters left out because
    they have no average yet). The one sum Diesel Days Left and the van's
    Diesel Cost both use, so they cannot count different heaters."""
    total = 0.0
    not_counted: list[str] = []
    for device_id, name in (dt.known_heaters() if dt else {}).items():
        value = dt.per_day(device_id) if dt else None
        if value is None:
            not_counted.append(name)
        else:
            total += value
    return total, not_counted


class SensaDieselTotalCostSensor(_DieselSensor):
    """What all the heaters' diesel costs a day together, at the nearest fuel
    station's diesel price (Sensa Radar), for SensaClimate's thermostat box.
    A heater with no average yet is left out and named in `not_counted`, as
    Diesel Days Left does. No price, or no heater counted, is no figure."""

    _attr_icon = "mdi:cash"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2
    _attr_name = "Diesel Cost"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_diesel_cost"
        self.entity_id = TOTAL_COST_ENTITY_ID

    def _compute(self) -> dict[str, Any]:
        per_day, not_counted = heaters_per_day(self._tracker())
        price = diesel_price(self.hass, self._entry)
        counted = per_day > 0
        cost = per_day * price["per_litre"] if price is not None and counted else None
        return {
            "cost": cost,
            "currency": price["currency"] if price else None,
            "per": "day",
            "litres_per_day": round(per_day, 2),
            "not_counted": not_counted,
            "price_per_litre": None if price is None else round(price["per_litre"], 3),
            "station": None if price is None else price["station"],
            "station_distance_m": None if price is None else price["distance_m"],
        }

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._compute()["currency"]

    @property
    def native_value(self) -> float | None:
        cost = self._compute()["cost"]
        return None if cost is None else round(cost, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        out = self._compute()
        out.pop("cost")
        out.pop("currency")
        return out


class SensaDieselDaysLeftSensor(_DieselSensor):
    """Days the heaters' diesel lasts at their combined daily average."""

    _attr_icon = "mdi:timer-sand"
    _attr_native_unit_of_measurement = "d"  # UnitOfTime.DAYS
    # Long-term statistics: SensaClimate's Diesel days left chart reads 90 days.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_name = "Diesel Days Left"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_diesel_days_left"
        self.entity_id = DAYS_LEFT_ENTITY_ID

    def _compute(self) -> dict[str, Any]:
        litres, tanks, why = tank_litres(self.hass, self._entry)
        per_day_total, not_counted = heaters_per_day(self._tracker())
        days = days_left(litres, per_day_total)
        if why is None and days is None:
            why = (
                "Too little heating yet to estimate"
                if litres is not None and per_day_total > 0
                else f"No diesel heating in the last {AVERAGE_DAYS} days"
            )
        return {
            "days": days,
            "litres": None if litres is None else round(litres, 1),
            "litres_per_day": round(per_day_total, 2),
            "tanks": tanks,
            "not_counted": not_counted,
            "reason": None if days is not None else why,
        }

    @property
    def native_value(self) -> float | None:
        days = self._compute()["days"]
        return None if days is None else round(days, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        out = self._compute()
        out.pop("days")
        return out


def heater_ids_for(hass: HomeAssistant, entry: ConfigEntry, devices: list[dict[str, Any]]) -> dict[str, str]:
    """device_id -> name for every Autoterm heater: the live device list plus
    the ledger, so a heater keeps its sensors on a restart the backend has not
    answered yet."""
    out: dict[str, str] = {}
    dt = tracker(hass, entry)
    if dt is not None:
        out.update(dt.known_heaters())
    for device in devices:
        device_id = str(device.get("device_id") or device.get("id") or "")
        if device_id and is_autoterm(device):
            out[device_id] = str(device.get("display_name") or device.get("name") or out.get(device_id) or "Heater")
    return out


def build_heater_sensors(entry: ConfigEntry, device_id: str, name: str) -> list[SensorEntity]:
    """Every Autoterm heater's own sensors: diesel use, diesel per day, its
    diesel cost a day, and its lifetime Total Run Time (`heater_runtime.py`). One list, so setup and a
    heater added later make the same set."""
    from .heater_runtime import SensaHeaterTotalRunTimeSensor

    return [
        SensaDieselUseSensor(entry, device_id, name),
        SensaDieselPerDaySensor(entry, device_id, name),
        SensaDieselCostSensor(entry, device_id, name),
        SensaHeaterTotalRunTimeSensor(entry, device_id, name),
        SensaHeaterFanSpeedSensor(entry, device_id, name),
    ]
