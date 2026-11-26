"""Miner autopilot: mine on spare solar, stop before the hab cooks.

The Crypto tab's miners are three things at once: a discretionary load, a
heater that pays the customer back, and a thing that makes money. This engine
decides WHEN they run, from four thresholds on the card's Controls panel:

* start when the leisure battery is at or above `soc_start` (a full battery is
  the surplus signal: an MPPT in float under-reports PV, so "solar is low" at
  100 % is not "no sun"), or one miner per `solar_per_miner` watts of PV while
  the battery is still absorbing;
* stop at once when the battery falls to `soc_stop` or the habitation reaches
  `room_max`; stop one miner at a time when solar falls away.

A miner has no software off, so the engine drives the relay channel assigned
to it as an Integrated Device (`device["power_relay"]`), exactly as
`cold_charge.py` drives a DC-DC charger's relay, and it borrows that module's
rules: a durable store of what it changed, restore only what it changed, and
the customer's own switch flips always win. Cutting a miner's relay by hand
switches the autopilot off rather than being fought.

Every reading is "could not ask" when None. A single lost sample changes
nothing; a reading stale for five minutes pauses the engine (miners off, one
log line on the edge). Starts need a fresh reading, and the solar-fall timer
lives in memory so a restart re-primes before it can fire.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._io import atomic_write_text
from ._log import sensa_log
from .const import (
    CRYPTO_AUTOPILOT_NUMBERS,
    CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID,
    CRYPTO_HEATER_OFF_SWITCH_ENTITY_ID,
    CRYPTO_ROOM_NOT_MONITORED,
    CRYPTO_ROOM_SENSOR_SELECT_ENTITY_ID,
    DOMAIN,
    HEATER_SERVICE_CLASSES,
    SENSA_CRYPTO_AUTOPILOT_KEY,
    SENSA_CRYPTO_AUTOPILOT_PATH,
    SENSA_LOG_SOURCE_VEHICLE,
    crypto_autopilot_number_entity_id,
)

_LOGGER = logging.getLogger(__name__)

STATE_OFF = "off"
STATE_WAITING = "waiting"
STATE_MINING = "mining"
STATE_PAUSED = "paused"

TICK = timedelta(seconds=60)
# A miner starts the moment its condition is met (Richard: "why wait 5
# mins?"); the per-relay minimum run and rest below are the chatter guard. A
# fall caused by solar must hold this long before a miner stops.
SUSTAIN_SOLAR_STOP = timedelta(minutes=10)
# A reading older than this is lost, not merely late.
STALE_AFTER = timedelta(minutes=5)
# Per relay, so nothing cycles faster than the ESPHome board and the miner's
# PSU like.
MIN_RUN = timedelta(minutes=10)
MIN_OFF = timedelta(minutes=5)
# A relay we commanded on that still reads off after this long never took the
# write (board offline, channel disabled).
WRITE_CONFIRM = timedelta(seconds=150)
# "All miners" entered at soc_start holds until SoC falls this far below it,
# and soc_stop must sit at least this far below soc_start.
SOC_BAND = 5.0
# After a temperature stop the room must cool this far below room_max first.
TEMP_RESTART_MARGIN = 2.0

_ON = "on"
_OFF = "off"
_DEAD = (None, "", "unknown", "unavailable")


def temperature_celsius(state: Any) -> float | None:
    """A temperature state in degC, or None when unavailable / not a number.
    °F and K probes are converted so one threshold serves a mixed van."""
    if state is None:
        return None
    raw = getattr(state, "state", None)
    if raw in _DEAD:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    attrs = getattr(state, "attributes", None) or {}
    unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
    if unit == "°f":
        return (value - 32) * 5 / 9
    if unit == "k":
        return value - 273.15
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _minutes_left(sustain: timedelta, elapsed: timedelta) -> int:
    """Whole minutes until a sustain window closes, never below one: the
    card says "in 1 min" right up to the tick that acts."""
    remaining = (sustain - elapsed).total_seconds()
    return max(1, int(-(-remaining // 60)))


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


def _parse(raw: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


class CryptoAutopilotStore:
    """Durable record of what the engine changed.

    `relays`: relay entity_id -> {"started_at": iso}; the miners we own, in
    start order. `heater`: {"entity": id, "we_cut": bool}; whether the diesel
    heater is off because of us. Persisted BEFORE every relay write so a
    restart in between cannot forget a miner it switched on.
    """

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        self.relays: dict[str, dict[str, Any]] = {}
        self.heater: dict[str, Any] = {}

    def load_sync(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            _LOGGER.debug("Crypto autopilot store: unreadable, starting fresh", exc_info=True)
            return
        if not isinstance(raw, dict):
            return
        relays = raw.get("relays")
        if isinstance(relays, dict):
            self.relays = {
                str(k): dict(v) for k, v in relays.items() if isinstance(v, dict)
            }
        heater = raw.get("heater")
        if isinstance(heater, dict):
            self.heater = dict(heater)

    async def async_persist(self) -> None:
        try:
            body = json.dumps(
                {"relays": self.relays, "heater": self.heater},
                separators=(",", ":"),
            )
            await self.hass.async_add_executor_job(atomic_write_text, self.path, body)
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Crypto autopilot store: persist failed", exc_info=True)

    def owned_in_start_order(self) -> list[str]:
        return sorted(
            self.relays,
            key=lambda eid: _parse(self.relays[eid].get("started_at")) or datetime.min.replace(tzinfo=timezone.utc),
        )


class CryptoAutopilot:
    """The engine. One per HA run, kept at `hass.data[DOMAIN][SENSA_CRYPTO_AUTOPILOT_KEY]`.

    `async_evaluate` is the whole decision, run under one lock from the 60 s
    tick and from a state change on any input it depends on, so a verdict is
    immediate either way. One miner starts per evaluation, so a fleet comes
    up staggered rather than all on one PSU inrush.
    """

    def __init__(self, hass: Any, store: CryptoAutopilotStore) -> None:
        self.hass = hass
        self.store = store
        self.state: str = STATE_OFF
        self.reason: str = ""
        self.snapshot: dict[str, Any] = {}
        self.listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._interval_cancel: Callable[[], None] | None = None
        self._state_cancel: Callable[[], None] | None = None
        self._watched: frozenset[str] = frozenset()
        # Carried readings: (value, seen_at). A None sample never overwrites.
        self._soc: tuple[float, datetime] | None = None
        self._room: tuple[float, datetime] | None = None
        self._fall_since: datetime | None = None
        self._soc_full = False
        self._temp_stopped = False
        # Relay writes in flight: eid -> (state we asked for, when).
        self._commanded: dict[str, tuple[str, datetime]] = {}
        # Last on/off we saw per owned relay (unavailable is not recorded).
        self._observed: dict[str, str] = {}
        # When we last stopped each relay, for MIN_OFF.
        self._stopped_at: dict[str, datetime] = {}
        # When we cut the heater, so its own echo is not read as the customer.
        self._heater_cut_at: datetime | None = None
        # Edge-logged conditions, so a fault is one line, not one per tick.
        self._edges: set[str] = set()
        self._was_on = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> Callable[[], None]:
        # Imported here so the module loads under the test stubs, which only
        # provide the timer helper.
        from homeassistant.helpers.event import async_track_time_interval

        self._interval_cancel = async_track_time_interval(
            self.hass, self._on_tick, TICK,
        )
        self._refresh_watch()
        return self.stop

    def stop(self) -> None:
        for attr in ("_interval_cancel", "_state_cancel"):
            cancel = getattr(self, attr)
            if cancel is not None:
                try:
                    cancel()
                except Exception:  # pragma: no cover  # noqa: BLE001, S110
                    pass
                setattr(self, attr, None)

    async def _on_tick(self, _now: Any = None) -> None:
        await self.async_evaluate()

    async def _on_state_change(self, _event: Any = None) -> None:
        await self.async_evaluate()

    def _refresh_watch(self) -> None:
        desired = frozenset(self._watch_entities())
        if desired == self._watched:
            return
        if self._state_cancel is not None:
            try:
                self._state_cancel()
            except Exception:  # pragma: no cover  # noqa: BLE001, S110
                pass
            self._state_cancel = None
        self._watched = desired
        if not desired:
            return
        try:
            from homeassistant.helpers.event import async_track_state_change_event
        except ImportError:  # pragma: no cover - test stubs
            return
        self._state_cancel = async_track_state_change_event(
            self.hass, sorted(desired), self._on_state_change,
        )

    def _watch_entities(self) -> set[str]:
        # The settings are watched too, so a slider moved on the card answers
        # at once instead of a minute later.
        out = {
            CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID,
            CRYPTO_HEATER_OFF_SWITCH_ENTITY_ID,
            CRYPTO_ROOM_SENSOR_SELECT_ENTITY_ID,
            *(crypto_autopilot_number_entity_id(key) for key in CRYPTO_AUTOPILOT_NUMBERS),
        }
        soc = self._soc_entity()
        if soc:
            out.add(soc)
        room = self._room_entity()
        if room:
            out.add(room)
        heater = self._heater_entity()
        if heater:
            out.add(heater)
        out.update(self.store.relays)
        return out

    def _publish(self) -> None:
        for cb in list(self.listeners):
            try:
                cb()
            except Exception:
                _LOGGER.debug("crypto autopilot listener raised", exc_info=True)

    # -- reading HA ----------------------------------------------------------

    def _state(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        return getattr(st, "state", None) if st is not None else None

    def _num(self, entity_id: str | None) -> float | None:
        raw = self._state(entity_id)
        if raw in _DEAD:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _setting(self, key: str) -> float:
        value = self._num(crypto_autopilot_number_entity_id(key))
        return value if value is not None else CRYPTO_AUTOPILOT_NUMBERS[key][5]

    @staticmethod
    def _devices() -> list[dict[str, Any]]:
        try:
            from .dashboard import get_dashboard_devices

            return get_dashboard_devices()
        except Exception:  # pragma: no cover - dashboard module always importable  # noqa: BLE001
            return []

    def _any_miner_registered(self) -> bool:
        """True if the van has a miner at all, relay assigned or not."""
        from .dashboard import _is_bitcoin_miner

        return any(_is_bitcoin_miner(device) for device in self._devices())

    def _miners(self) -> list[dict[str, Any]]:
        """Every registered miner with a relay channel assigned to it, as
        {"name", "relay", "problem"} in device-list order."""
        from .dashboard import _is_bitcoin_miner, _luckyminer_entities

        out: list[dict[str, Any]] = []
        for device in self._devices():
            if not _is_bitcoin_miner(device):
                continue
            relay = device.get("power_relay")
            if not isinstance(relay, str) or not relay:
                continue
            try:
                entities = _luckyminer_entities(device) or {}
            except Exception:  # noqa: BLE001 - registry hiccup, no problem sensor this tick
                entities = {}
            out.append({
                "name": device.get("display_name") or "Miner",
                "relay": relay,
                "problem": entities.get("problem"),
            })
        return out

    def _soc_entity(self) -> str | None:
        try:
            from .dashboard import resolve_role

            eid = resolve_role("battery_soc")
        except Exception:  # noqa: BLE001
            return None
        return eid if isinstance(eid, str) and eid else None

    def _solar_entities(self) -> list[str]:
        try:
            from .dashboard import resolve_role

            found = resolve_role("solar_power")
        except Exception:  # noqa: BLE001
            return []
        if isinstance(found, str):
            return [found]
        return [e for e in (found or []) if isinstance(e, str)]

    def _room_entity(self) -> str | None:
        """The chosen habitation probe, or None for "Not monitored" / unset."""
        choice = self._state(CRYPTO_ROOM_SENSOR_SELECT_ENTITY_ID)
        if choice in _DEAD or choice == CRYPTO_ROOM_NOT_MONITORED:
            return None
        return str(choice)

    def _heater_entity(self) -> str | None:
        from .dashboard import _device_role_map, _device_service_class

        for device in self._devices():
            if _device_service_class(device) not in HEATER_SERVICE_CLASSES:
                continue
            eid = _device_role_map(device).get("power")
            if isinstance(eid, str) and eid:
                return eid
        return None

    # -- the decision --------------------------------------------------------

    async def async_evaluate(self, *, now: datetime | None = None) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._evaluate(now or _now())
            finally:
                self._refresh_watch()
                self._publish()

    async def _evaluate(self, now: datetime) -> None:
        # No miner in the van: nothing to drive, so the engine stays dormant
        # rather than announcing itself and pausing once per restart. Anything
        # still owned falls through to the normal path, which stops it.
        if (
            not self._any_miner_registered()
            and not self.store.relays
            and not self.store.heater.get("we_cut")
        ):
            self._reset_counters()
            self._observed.clear()
            self._commanded.clear()
            self._edge_clear("paused")
            self._was_on = False
            self._set(STATE_OFF, "No miner registered", {})
            return

        master_on = self._state(CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID) == _ON
        if not master_on:
            if self.store.relays or self.store.heater.get("we_cut"):
                await self._stop_all(now, "autopilot switched off")
            self._reset_counters()
            self._observed.clear()
            self._commanded.clear()
            if self._was_on:
                self._log("info", "Miner autopilot switched off.")
            self._was_on = False
            self._set(STATE_OFF, "Autopilot off", {})
            return
        if not self._was_on:
            self._was_on = True
            self._log("info", "Miner autopilot switched on.")

        miners = self._miners()
        by_relay = {m["relay"]: m for m in miners}

        # 1. Reconcile what we own against what the van says.
        if not await self._reconcile_owned(now, by_relay):
            return  # customer cut: the master is now off

        # 2. The heater the customer turned back on is theirs again.
        heater = self._heater_entity()
        if (
            self.store.heater.get("we_cut")
            and self._state(heater) == _ON
            and (self._heater_cut_at is None or now - self._heater_cut_at >= WRITE_CONFIRM)
        ):
            self.store.heater = {}
            await self.store.async_persist()

        # 3. Readings, carried forward; a None sample never overwrites.
        soc_now = self._num(self._soc_entity())
        if soc_now is not None:
            self._soc = (soc_now, now)
        room_entity = self._room_entity()
        room_now = temperature_celsius(self.hass.states.get(room_entity)) if room_entity else None
        if room_now is not None:
            self._room = (room_now, now)
        elif room_entity is None:
            self._room = None
        solar = 0.0
        solar_seen = False
        for eid in self._solar_entities():
            value = self._num(eid)
            if value is not None:
                solar += value
                solar_seen = True

        soc_start = self._setting("soc_start")
        soc_stop = self._setting("soc_stop")
        per_miner = self._setting("solar_per_miner")
        room_max = self._setting("room_max")
        snapshot: dict[str, Any] = {
            "battery_pct": self._soc[0] if self._soc else None,
            "room_c": self._room[0] if self._room else None,
            "solar_w": solar if solar_seen else None,
            "miners": len(miners),
        }

        # 4. Settings that cannot be obeyed.
        if soc_stop >= soc_start - SOC_BAND:
            await self._pause(now, "Stop level must be below start level", snapshot)
            return
        if per_miner <= 0:
            await self._pause(now, "Solar per miner must be above zero", snapshot)
            return

        # 5. A reading we cannot get is a reason to stop, not to guess.
        if not miners:
            await self._pause(now, "No miner has a relay channel assigned", snapshot)
            return
        if self._soc_entity() is None or self._soc is None or now - self._soc[1] > STALE_AFTER:
            await self._pause(now, "Battery reading lost", snapshot)
            return
        if room_entity is not None and (self._room is None or now - self._room[1] > STALE_AFTER):
            await self._pause(now, "Room temperature reading lost", snapshot)
            return
        self._edge_clear("paused")

        soc = self._soc[0]
        soc_fresh = soc_now is not None
        room = self._room[0] if self._room else None
        room_fresh = room_entity is None or room_now is not None

        # 6. Hard stops, immediate.
        if room is not None and room >= room_max:
            self._temp_stopped = True
            await self._stop_all(now, f"hab {room:.0f} C")
            self._set(STATE_WAITING, f"Waiting: hab {room:.0f} C, must cool to {room_max - TEMP_RESTART_MARGIN:.0f} C", snapshot)
            return
        if soc <= soc_stop:
            self._soc_full = False
            await self._stop_all(now, f"battery {soc:.0f}%")
            self._set(STATE_WAITING, f"Waiting: battery {soc:.0f}%, needs {soc_start:.0f}%", snapshot)
            return
        if self._temp_stopped:
            if room is not None and room > room_max - TEMP_RESTART_MARGIN:
                self._set(STATE_WAITING, f"Waiting: hab {room:.0f} C, must cool to {room_max - TEMP_RESTART_MARGIN:.0f} C", snapshot)
                return
            self._temp_stopped = False

        # 7. The target: a full battery runs everything, else one per step of solar.
        available = [
            m for m in miners
            if self._state(m["relay"]) in (_ON, _OFF)
            and self._state(m["problem"]) != _ON
        ]
        running = [eid for eid in self.store.owned_in_start_order() if eid in by_relay]
        n = len(running)
        # A miner we own whose relay has gone quiet is still ours: dropping it
        # would leave it running unmanaged. It just cannot be started.
        capacity = len({m["relay"] for m in available} | set(running))
        if soc >= soc_start:
            self._soc_full = True
        elif soc < soc_start - SOC_BAND:
            self._soc_full = False
        solar_reason = False
        if self._soc_full:
            target = capacity
            why = f"battery {soc:.0f}%"
        else:
            up = int(solar // per_miner) if solar_seen else 0
            keep = int((solar + per_miner / 2) // per_miner) if solar_seen else 0
            if up > n:
                target = up
            elif keep < n:
                target = keep
                solar_reason = True
            else:
                target = n
            target = min(target, capacity)
            why = f"battery {soc:.0f}%, {solar:.0f} W solar" if solar_seen else f"battery {soc:.0f}%"
        snapshot["target"] = target
        snapshot["running"] = n

        # 8. Down: solar must stay low for a while, and a miner runs a minimum.
        if target < n:
            if not solar_reason:
                self._fall_since = None
                while len(running) > target:
                    await self._stop_one(now, running.pop(), why)
            else:
                self._fall_since = self._fall_since or now
                if now - self._fall_since >= SUSTAIN_SOLAR_STOP:
                    last = running[-1]
                    started = _parse(self.store.relays.get(last, {}).get("started_at"))
                    if started is None or now - started >= MIN_RUN:
                        await self._stop_one(now, last, f"{solar:.0f} W solar")
                        running.pop()
                        self._fall_since = None
                else:
                    left = _minutes_left(SUSTAIN_SOLAR_STOP, now - self._fall_since)
                    self._set(STATE_MINING, f"Mining, {n} of {len(miners)}; solar dropping, stopping one in {left} min", snapshot)
                    return
        else:
            self._fall_since = None

        # 9. Up: at once, on fresh readings, one miner per evaluation, honouring
        # each relay's rest after its last stop.
        n = len(running)
        resting: tuple[str, int] | None = None
        if target > n and soc_fresh and room_fresh:
            for m in available:
                eid = m["relay"]
                if eid in self.store.relays:
                    continue
                last_off = self._stopped_at.get(eid)
                if last_off is not None and now - last_off < MIN_OFF:
                    resting = resting or (m["name"], _minutes_left(MIN_OFF, now - last_off))
                    continue
                await self._start_one(now, m, why, first=not running)
                running.append(eid)
                break

        n = len(running)
        if n:
            note = f"Mining on surplus, {n} of {len(miners)}"
            if self._soc_full and (not solar_seen or solar < per_miner):
                note = f"Mining on battery, {n} of {len(miners)}: {soc:.0f}%, stops at {soc_stop:.0f}%"
            if target > n and resting:
                note += f"; {resting[0]} rests {resting[1]} min more"
            elif target > n:
                note += "; starting another"
            self._set(STATE_MINING, note, snapshot)
        elif target > 0 and resting:
            self._set(STATE_WAITING, f"{resting[0]} rests {resting[1]} min more: {why}", snapshot)
        elif target > 0:
            self._set(STATE_WAITING, f"Starting: {why}", snapshot)
        else:
            self._set(STATE_WAITING, self._waiting_reason(soc, soc_start, solar, solar_seen, per_miner, available, miners), snapshot)

    def _waiting_reason(self, soc, soc_start, solar, solar_seen, per_miner, available, miners) -> str:
        if not available:
            latched = [m for m in miners if self._state(m["problem"]) == _ON]
            if latched:
                return "Waiting: overheat latched, clear it on the card"
            return "Waiting: miner relay unreachable"
        if solar_seen:
            return f"Waiting: battery {soc:.0f}%, needs {soc_start:.0f}%; solar {solar:.0f} W, needs {per_miner:.0f} W"
        return f"Waiting: battery {soc:.0f}%, needs {soc_start:.0f}%"

    # -- reconcile -----------------------------------------------------------

    async def _reconcile_owned(self, now: datetime, by_relay: dict[str, dict[str, Any]]) -> bool:
        """Adopt, drop or surrender each relay in the store. Returns False when
        the customer cut one by hand and the autopilot has stood down."""
        for eid in list(self.store.relays):
            state = self._state(eid)
            miner = by_relay.get(eid)
            if miner is None:
                # Miner deleted or the channel reassigned: never leave it on unmanaged.
                if state == _ON:
                    await self._write_relay(eid, _OFF, now)
                self.store.relays.pop(eid, None)
                self._observed.pop(eid, None)
                await self.store.async_persist()
                self._log("info", "Miner autopilot released a relay no longer assigned to a miner.")
                continue
            if state not in (_ON, _OFF):
                continue  # unreachable: we do not know, so we do not act
            if state == _ON:
                self._observed[eid] = _ON
                self._commanded.pop(eid, None)
                self._edge_clear(f"write_failed:{eid}")
                if self._state(miner["problem"]) == _ON:
                    await self._stop_one(now, eid, "overheat latched")
                continue
            # state == off
            cmd = self._commanded.get(eid)
            if cmd is not None and cmd[0] == _ON:
                if now - cmd[1] < WRITE_CONFIRM:
                    continue  # our own write, still landing
                self._commanded.pop(eid, None)
                self.store.relays.pop(eid, None)
                await self.store.async_persist()
                # Counts as a stop, so the retry waits out the relay's rest
                # rather than hammering a channel that is not answering.
                self._stopped_at[eid] = now
                self._edge_log(f"write_failed:{eid}", "warning", f"{miner['name']} did not power on; its relay channel may be disabled or offline.")
                continue
            # Off, and not by us: the customer's cut. Their choice wins.
            await self._surrender(now, miner["name"])
            return False
        return True

    async def _surrender(self, now: datetime, name: str) -> None:
        self.store.relays.clear()
        await self._restore_heater()
        await self.store.async_persist()
        self._observed.clear()
        self._commanded.clear()
        self._reset_counters()
        await self.hass.services.async_call(
            "switch", "turn_off", {"entity_id": CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID},
        )
        self._was_on = False
        self._log("info", f"Miner autopilot switched off: {name}'s power was cut by hand.")
        self._set(STATE_OFF, "Autopilot off: power was cut by hand", {})

    # -- actions -------------------------------------------------------------

    async def _write_relay(self, eid: str, state: str, now: datetime) -> None:
        self._commanded[eid] = (state, now)
        await self.hass.services.async_call(
            "switch", f"turn_{state}", {"entity_id": eid},
        )

    async def _start_one(self, now: datetime, miner: dict[str, Any], why: str, *, first: bool) -> None:
        eid = miner["relay"]
        self.store.relays[eid] = {"started_at": _iso(now)}
        await self.store.async_persist()
        await self._write_relay(eid, _ON, now)
        self._observed.pop(eid, None)
        self._log("info", f"{miner['name']} started: {why}.")
        if first:
            await self._cut_heater(now)

    async def _stop_one(self, now: datetime, eid: str, why: str) -> None:
        self.store.relays.pop(eid, None)
        await self.store.async_persist()
        if self._state(eid) == _ON:
            await self._write_relay(eid, _OFF, now)
        self._stopped_at[eid] = now
        self._observed.pop(eid, None)
        name = next((m["name"] for m in self._miners() if m["relay"] == eid), "Miner")
        self._log("info", f"{name} stopped: {why}.")
        if not self.store.relays:
            await self._restore_heater()

    async def _stop_all(self, now: datetime, why: str) -> None:
        for eid in reversed(self.store.owned_in_start_order()):
            await self._stop_one(now, eid, why)
        self._fall_since = None

    async def _pause(self, now: datetime, reason: str, snapshot: dict[str, Any]) -> None:
        if self.store.relays:
            await self._stop_all(now, reason.lower())
        self._edge_log(f"paused:{reason}", "warning", f"Miner autopilot paused: {reason}.")
        self._set(STATE_PAUSED, f"Paused: {reason}", snapshot)

    async def _cut_heater(self, now: datetime) -> None:
        if self._state(CRYPTO_HEATER_OFF_SWITCH_ENTITY_ID) != _ON:
            return
        heater = self._heater_entity()
        if heater is None or self._state(heater) != _ON:
            return
        self.store.heater = {"entity": heater, "we_cut": True}
        await self.store.async_persist()
        self._heater_cut_at = now
        await self.hass.services.async_call(
            "switch", "turn_off", {"entity_id": heater},
        )
        self._log("info", "Heater off: the miners are heating the hab.")

    async def _restore_heater(self) -> None:
        heater = self.store.heater
        if not heater.get("we_cut"):
            self.store.heater = {}
            return
        eid = heater.get("entity")
        self.store.heater = {}
        await self.store.async_persist()
        if isinstance(eid, str) and self._state(eid) == _OFF:
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": eid},
            )
            self._log("info", "Heater back on: the miners have stopped.")

    # -- bookkeeping ---------------------------------------------------------

    def _reset_counters(self) -> None:
        self._fall_since = None
        self._soc_full = False
        self._temp_stopped = False

    def _set(self, state: str, reason: str, snapshot: dict[str, Any]) -> None:
        self.state = state
        self.reason = reason
        self.snapshot = dict(snapshot)

    def _log(self, level: str, message: str) -> None:
        try:
            sensa_log(self.hass, level, message, source=SENSA_LOG_SOURCE_VEHICLE)
        except Exception:
            _LOGGER.debug("crypto autopilot log failed", exc_info=True)

    def _edge_log(self, key: str, level: str, message: str) -> None:
        if key in self._edges:
            return
        self._edges.add(key)
        self._log(level, message)

    def _edge_clear(self, prefix: str) -> None:
        self._edges = {k for k in self._edges if not k.startswith(prefix)}


# -- module API --------------------------------------------------------------

def get_crypto_autopilot(hass: Any) -> CryptoAutopilot | None:
    return (hass.data.get(DOMAIN) or {}).get(SENSA_CRYPTO_AUTOPILOT_KEY)


def heater_held_off(hass: Any, entity_id: str) -> bool:
    """True while the miners are heating the hab in place of this heater
    ("Miners replace the heater"), so Climate Control does not relight it."""
    engine = get_crypto_autopilot(hass)
    held = engine.store.heater if engine is not None else {}
    return bool(held.get("we_cut")) and held.get("entity") == entity_id


async def async_setup_crypto_autopilot(hass: Any) -> Callable[[], None]:
    """Load the store, build the engine, start its tick. Returns a cancel."""
    existing = get_crypto_autopilot(hass)
    if existing is not None:
        return existing.stop
    store = CryptoAutopilotStore(hass, hass.config.path(SENSA_CRYPTO_AUTOPILOT_PATH))
    await hass.async_add_executor_job(store.load_sync)
    engine = CryptoAutopilot(hass, store)
    hass.data.setdefault(DOMAIN, {})[SENSA_CRYPTO_AUTOPILOT_KEY] = engine
    cancel = engine.start()

    def _teardown() -> None:
        cancel()
        (hass.data.get(DOMAIN) or {}).pop(SENSA_CRYPTO_AUTOPILOT_KEY, None)

    return _teardown
