"""The Sensa alarm subsystem — managed, stateful, re-sounding alerts.

Distinct from the customer message log (`_log.py`): a log entry is one-shot and
append-only, whereas an **alarm** is a *managed* condition. Once raised it stays
**active** and **re-asserts (re-sounds) every N minutes until the customer
cancels or snoozes it** — e.g. a water leak you want to keep nagging about.

Design (see `docs/system-design/notifications.md`):
  • Store = a dict of active alarms keyed by a stable `key`, held on
    `hass.data[DOMAIN][SENSA_ALARMS_STORE_KEY]` and persisted to
    `nomad/.sensa_alarms.json` (a JSON object) so live alarms survive a restart
    — unlike the in-memory log ring.
  • Lifecycle: ACTIVE ⇄ SNOOZED → CANCELLED (terminal). A re-raise of an
    existing `key` refreshes it rather than stacking a duplicate.
  • IGNORED is the customer's Ignore: the alarm goes, and its key is remembered
    so the producer's next raise of it is silent. Only the producer's own
    cancel (the condition cleared) forgets the key, so a sensor battery that is
    still flat stays quiet, and the next time it goes flat it sounds again.
  • Delivery on every (re)assert: a STICKY HA persistent_notification (kept until
    cancelled, re-asserted on each sound), a best-effort mobile push, and an
    audible hook; the cross-panel banner is driven by
    `binary_sensor.sensa_nomad_alarm_active`. A snoozed alarm suppresses delivery
    until it expires; a cancelled alarm clears it.
  • Log mirror: an alarm's STATE TRANSITIONS (raised / snoozed / cancelled) are
    written to the Sensa log as `alarm`-type messages so the log keeps the full
    history. Re-sounds are NOT logged (they'd spam).

Kept parallel to `_log.py` so the lifecycle/serialisation logic can be
unit-tested without the HA platform / scheduler machinery — the store runs on
the event loop and takes an injectable `now` for tests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._io import atomic_write_text
from ._log import sensa_log
from .const import (
    DOMAIN,
    EVENT_PANEL_CONFIG_CHANGED,
    EVENT_SENSA_ALARMS_CHANGED,
    SENSA_ALARM_DEFAULT_RESOUND_MIN,
    SENSA_ALARM_DEFAULT_SNOOZE_MIN,
    SENSA_ALARMS_PATH,
    SENSA_ALARMS_STORE_KEY,
    SENSA_LOG_SOURCE_VEHICLE,
)

_LOGGER = logging.getLogger(__name__)

STATE_ACTIVE = "active"
STATE_SNOOZED = "snoozed"
# Reserved top-level key in the alarms file holding the ignored keys. A list,
# not a dict, so an older integration's loader skips it rather than reading it
# as an alarm.
_IGNORED_FILE_KEY = "_ignored"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


class SensaAlarmStore:
    """The active-alarm map + its durable JSON backing.

    One per HA instance, on `hass.data[DOMAIN][SENSA_ALARMS_STORE_KEY]`.
    All mutators run on the event loop and accept an injectable `now`.
    """

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        # key -> alarm dict {key, message, raised_at, state, snoozed_until,
        #                     resound_every, last_sounded}
        self.alarms: dict[str, dict[str, Any]] = {}
        # Keys the customer ignored whose producer has not yet cleared them.
        self.ignored: set[str] = set()
        self._sensor: Any | None = None
        self._binary: Any | None = None
        # Tracks whether any alarm was live last commit, so we only rebuild the
        # panel config (to add/remove the conditional Alarms tab) on the 0↔>0
        # boundary — not on every re-sound tick.
        self._had_live = False

    # -- sensor wiring -------------------------------------------------------
    def bind_sensor(self, sensor: Any) -> None:
        self._sensor = sensor
        self._push_sensors()

    def unbind_sensor(self, sensor: Any) -> None:
        if self._sensor is sensor:
            self._sensor = None

    def bind_binary(self, binary: Any) -> None:
        self._binary = binary
        self._push_sensors()

    def unbind_binary(self, binary: Any) -> None:
        if self._binary is binary:
            self._binary = None

    def _push_sensors(self) -> None:
        snap = self.snapshot()
        active = self.active_count()
        sensor = self._sensor
        if sensor is not None:
            try:
                sensor.set_alarms(snap, active)
            except Exception:  # pragma: no cover - sensor not fully set up yet
                _LOGGER.debug("Sensa alarms: sensor push failed", exc_info=True)
        binary = self._binary
        if binary is not None:
            try:
                binary.set_active(active > 0)
            except Exception:  # pragma: no cover
                _LOGGER.debug("Sensa alarms: binary push failed", exc_info=True)

    # -- loading / persistence ----------------------------------------------
    def load_sync(self) -> None:
        """Populate the map from the backing JSON file. Runs in an executor."""
        p = self.path
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if isinstance(data, dict):
            ignored = data.get(_IGNORED_FILE_KEY)
            if isinstance(ignored, list):
                self.ignored = {str(k) for k in ignored if isinstance(k, str)}
            for key, alarm in data.items():
                if isinstance(alarm, dict) and isinstance(key, str):
                    alarm.setdefault("key", key)
                    self.alarms[key] = alarm
        # Seed the boundary tracker so the first post-restart mutation doesn't
        # spuriously fire a panel rebuild when the live-set didn't actually flip.
        self._had_live = bool(self.alarms)

    def _persist(self) -> None:
        snapshot: dict[str, Any] = dict(self.alarms)
        if self.ignored:
            snapshot[_IGNORED_FILE_KEY] = sorted(self.ignored)
        try:
            self.hass.async_create_task(self._async_persist(snapshot))
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Sensa alarms: persist scheduling failed", exc_info=True)

    async def _async_persist(self, snapshot: dict[str, Any]) -> None:
        try:
            body = json.dumps(snapshot, separators=(",", ":"))
            await self.hass.async_add_executor_job(atomic_write_text, self.path, body)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa alarms: persist failed", exc_info=True)

    # -- the write paths -----------------------------------------------------
    def raise_alarm(
        self,
        key: str,
        message: str,
        *,
        resound_every: int | None = None,
        title: str | None = None,
        guidance: str | None = None,
        context: list[dict[str, Any]] | None = None,
        severity: str | None = None,
        log_source: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Raise (or refresh) a managed alarm. A re-raise of an existing key
        updates it in place rather than stacking a duplicate.

        The optional richer fields drive the enhanced banner + the right-side
        detail card; all default to None so a plain message-only alarm renders
        exactly as before:
          • `title`    — short trigger headline ("Engine started"); surfaces
                         ahead of the message, which falls back to the message.
          • `guidance` — the action line ("Unplug shore power before driving").
          • `context`  — status rows, each {label, entity?, value?, icon?}; an
                         `entity` is read LIVE by the card, else `value` is shown.
          • `severity` — "warning" | "critical" (colour hint; default critical).
          • `log_source` — which log the mirror lands in; defaults to the
                         vehicle log. Stored on the alarm so every later
                         transition (snoozed, cleared) follows it.
        """
        stamp = now or datetime.now(timezone.utc)
        if key in self.ignored and key not in self.alarms:
            # The customer ignored this one and its condition has not cleared:
            # nothing is stored, delivered or logged.
            return {"key": key, "message": str(message), "state": "ignored"}
        existing = self.alarms.get(key)
        if existing is not None:
            # Refresh message; keep state/cadence. No fresh delivery beyond the
            # re-sound cadence (a snoozed alarm stays so). The rich fields are
            # only OVERWRITTEN when a new value is supplied — a message-only
            # re-raise (e.g. a code-rule re-fire, or set_automation's enable
            # path on a different producer) must NOT strip the title/guidance/
            # status rows from a live rich alarm mid-incident.
            existing["message"] = str(message)
            if resound_every is not None:
                existing["resound_every"] = int(resound_every)
            if title is not None:
                existing["title"] = str(title)
            if guidance is not None:
                existing["guidance"] = str(guidance)
            if context is not None:
                existing["context"] = list(context)
            if severity is not None:
                existing["severity"] = str(severity)
            if log_source is not None:
                existing["log_source"] = str(log_source)
            # Ensure the keys exist for an alarm loaded from an older on-disk file.
            for _k in ("title", "guidance", "context", "severity", "log_source"):
                existing.setdefault(_k, None)
            self._commit()
            return existing
        alarm = {
            "key": key,
            "message": str(message),
            "title": str(title) if title else None,
            "guidance": str(guidance) if guidance else None,
            "context": list(context) if context else None,
            "severity": str(severity) if severity else None,
            "log_source": str(log_source) if log_source else None,
            "raised_at": _iso(stamp),
            "state": STATE_ACTIVE,
            "snoozed_until": None,
            "resound_every": int(resound_every or SENSA_ALARM_DEFAULT_RESOUND_MIN),
            "last_sounded": _iso(stamp),
        }
        self.alarms[key] = alarm
        self._deliver(alarm)
        self._mirror(message, f"Alarm: {message}", key,
                     source=alarm.get("log_source"), event="raised")
        self._commit()
        return alarm

    def snooze(
        self, key: str, minutes: int | None = None, *, now: datetime | None = None,
    ) -> bool:
        """Snooze an alarm — suppress delivery until it expires. False if no such
        alarm."""
        alarm = self.alarms.get(key)
        if alarm is None:
            return False
        stamp = now or datetime.now(timezone.utc)
        mins = int(minutes or SENSA_ALARM_DEFAULT_SNOOZE_MIN)
        alarm["state"] = STATE_SNOOZED
        alarm["snoozed_until"] = _iso(stamp + timedelta(minutes=mins))
        self._dismiss_notification(key)
        self._mirror(alarm["message"], f"Alarm snoozed ({mins} min): {alarm['message']}", key,
                     source=alarm.get("log_source"), event="snoozed")
        self._commit()
        return True

    def cancel(self, key: str, *, now: datetime | None = None) -> bool:
        """Cancel an alarm (terminal). False if no such alarm.

        This is the PRODUCER's clear (the condition is over), so it also
        forgets an Ignore: the next time the condition holds, it sounds."""
        was_ignored = key in self.ignored
        self.ignored.discard(key)
        alarm = self.alarms.pop(key, None)
        if alarm is None:
            if was_ignored:
                self._persist()
            return False
        self._dismiss_notification(key)
        # `alarm` was popped above, so its source is read from the copy in hand.
        self._mirror(alarm["message"], f"Alarm cleared: {alarm['message']}", key,
                     source=alarm.get("log_source"), event="cleared")
        self._commit()
        return True

    def ignore(self, key: str, *, now: datetime | None = None) -> bool:
        """The customer's Ignore: clear the alarm and stay quiet until its
        producer cancels it. False if no such alarm."""
        alarm = self.alarms.pop(key, None)
        if alarm is None:
            return False
        self.ignored.add(key)
        self._dismiss_notification(key)
        self._mirror(alarm["message"], f"Alarm ignored: {alarm['message']}", key,
                     source=alarm.get("log_source"), event="ignored")
        self._commit()
        return True

    def tick(self, now: datetime | None = None) -> bool:
        """Periodic tick — un-snooze expired alarms and re-sound active ones
        whose cadence has elapsed. Returns True if anything changed."""
        stamp = now or datetime.now(timezone.utc)
        changed = False
        for alarm in list(self.alarms.values()):
            if alarm.get("state") == STATE_SNOOZED:
                until = _parse(alarm.get("snoozed_until"))
                if until is not None and stamp >= until:
                    alarm["state"] = STATE_ACTIVE
                    alarm["snoozed_until"] = None
                    alarm["last_sounded"] = _iso(stamp)
                    self._deliver(alarm)
                    changed = True
                continue
            # Active — re-sound if the cadence has elapsed.
            last = _parse(alarm.get("last_sounded")) or stamp
            cadence = timedelta(minutes=int(alarm.get("resound_every") or
                                            SENSA_ALARM_DEFAULT_RESOUND_MIN))
            if stamp - last >= cadence:
                alarm["last_sounded"] = _iso(stamp)
                self._deliver(alarm)
                changed = True
        if changed:
            self._commit()
        return changed

    def prune_orphans(self, entity_exists: Callable[[str], bool]) -> int:
        """Cancel every alarm about an entity that no longer exists. Returns how
        many were cancelled.

        A per-entity alarm outlives the thing it watches: delete the device and
        the alarm stays ACTIVE, re-sounding on every tick for ever, because the
        only other route out is the customer pressing cancel. Swept centrally
        rather than in each device-removal path, because an entity can vanish
        several ways (Sensa remove, a native integration removed, a Z2M device
        unpaired) and one predicate written per route would drift.

        `entity_exists` is injected so the store keeps testing without the HA
        platform, and so the caller owns the "could not ask" case: a caller that
        cannot reach the registry must not call this at all rather than pass a
        predicate that answers False, which would silence live alarms."""
        from .instincts_catalog import alarm_key_entity

        cancelled = 0
        for key in list(self.alarms):
            entity_id = alarm_key_entity(key)
            if entity_id is None or entity_exists(entity_id):
                continue
            _LOGGER.info(
                "Sensa alarms: cancelling %s — %s no longer exists",
                key, entity_id,
            )
            if self.cancel(key):
                cancelled += 1
        for key in list(self.ignored):
            entity_id = alarm_key_entity(key)
            if entity_id is not None and not entity_exists(entity_id):
                self.cancel(key)
        return cancelled

    # -- reads ---------------------------------------------------------------
    def snapshot(self) -> list[dict[str, Any]]:
        """Live alarms, newest-raised first."""
        items = list(self.alarms.values())
        items.sort(key=lambda a: a.get("raised_at", ""), reverse=True)
        return items

    def active_count(self) -> int:
        """Count of ACTIVE (un-snoozed) alarms — drives the banner."""
        return sum(1 for a in self.alarms.values() if a.get("state") == STATE_ACTIVE)

    # -- side effects --------------------------------------------------------
    def _commit(self) -> None:
        """Shared post-mutation work: persist, push sensors, fire the event, and
        — only when the live/none boundary crosses — ask open panels to rebuild
        so the conditional Alarms tab appears/disappears."""
        self._persist()
        self._push_sensors()
        try:
            self.hass.bus.async_fire(EVENT_SENSA_ALARMS_CHANGED)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa alarms: change-event fire failed", exc_info=True)
        live = bool(self.alarms)
        if live != self._had_live:
            self._had_live = live
            try:
                self.hass.bus.async_fire(EVENT_PANEL_CONFIG_CHANGED)
            except Exception:  # pragma: no cover
                _LOGGER.debug(
                    "Sensa alarms: panel-config event fire failed", exc_info=True,
                )

    def _deliver(self, alarm: dict[str, Any]) -> None:
        """(Re)assert an alarm: a sticky persistent_notification (kept until
        cancelled), a best-effort mobile push, and the audible hook. The banner
        is driven by the binary sensor via `_push_sensors`."""
        key = alarm["key"]
        message = alarm["message"]
        notif_id = f"sensa_alarm_{key}"
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification", "create",
                    {
                        "notification_id": notif_id,
                        "title": "Sensa alarm",
                        "message": message,
                    },
                    blocking=False,
                )
            )
            if self.hass.services.has_service("notify", "notify"):
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "notify", "notify",
                        {"title": "Sensa alarm", "message": message},
                        blocking=False,
                    )
                )
        except Exception:  # pragma: no cover - delivery is best-effort
            _LOGGER.debug("Sensa alarms: notification dispatch failed", exc_info=True)
        self._deliver_audible(alarm)

    def _deliver_audible(self, alarm: dict[str, Any]) -> None:
        """Audible re-sound hook. Best-effort TTS to any media_player so a real
        alarm is heard, not just shown. No media_player → silently skipped (the
        sticky notification + banner still carry it). A configurable target /
        chime is a future refinement."""
        try:
            players = [
                eid for eid in getattr(self.hass, "states", None).async_entity_ids("media_player")
            ] if hasattr(getattr(self.hass, "states", None), "async_entity_ids") else []
        except Exception:  # pragma: no cover  # noqa: BLE001
            players = []
        if not players:
            return
        call = self._audible_call(players[0], f"Alarm. {alarm['message']}")
        if call is None:
            return
        try:  # pragma: no cover - depends on a live tts + media_player
            self.hass.async_create_task(
                self.hass.services.async_call(*call, blocking=False)
            )
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa alarms: audible dispatch failed", exc_info=True)

    def _audible_call(self, player: str, message: str) -> tuple[str, str, dict[str, Any]] | None:
        """(domain, service, data) to speak `message` on `player`, or None.

        A local engine first: `tts.speak` through a `tts.*` entity, preferring
        Piper (the stack's own, which works with no internet, the case an alarm
        matters most in). Google's online say is the fallback. The old code
        accepted `tts.speak` as present and then always called Google, so a box
        with only Piper stayed silent.
        """
        services = self.hass.services
        if services.has_service("tts", "speak"):
            try:
                engines = sorted(self.hass.states.async_entity_ids("tts"))
            except Exception:  # noqa: BLE001
                engines = []
            if engines:
                engine = next((e for e in engines if "piper" in e), engines[0])
                return ("tts", "speak", {
                    "entity_id": engine,
                    "media_player_entity_id": player,
                    "message": message,
                })
        if services.has_service("tts", "google_translate_say"):
            return ("tts", "google_translate_say", {"entity_id": player, "message": message})
        return None

    def _dismiss_notification(self, key: str) -> None:
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification", "dismiss",
                    {"notification_id": f"sensa_alarm_{key}"}, blocking=False,
                )
            )
        except Exception:  # pragma: no cover  # noqa: BLE001, S110
            pass

    def _mirror(
        self,
        raw_message: str,
        log_message: str,
        key: str | None = None,
        *,
        source: str | None = None,
        event: str | None = None,
    ) -> None:
        """Write an alarm state transition to the Sensa log as an `alarm`-type
        message (the log keeps the history; the alarm subsystem owns delivery).

        Almost every alarm is about the VAN (a leak, cold charging, engine on
        shore power), so the mirror defaults to Intelligence, beside the
        instincts that raised them. An alarm about the BOX itself (the Zigbee
        radio being down) passes `source` and lands in Settings -> Log instead.
        The source travels on the alarm, so the "snoozed" and "cleared" lines
        follow the "raised" line into the same tab rather than splitting the
        incident across two.
        """
        context: dict[str, Any] | None = None
        if key:
            # Which alarm and what happened to it, so a chart can find one
            # alarm's history without reading the wording (SensaClimate's
            # "Off target" tab marks each cannot-reach alarm, 2026-10-09).
            context = {"alarm_key": key}
            if event:
                context["alarm_event"] = event
            # Late import: the catalog imports alerts, which this module feeds.
            from .instincts_catalog import instinct_for_alarm_key
            owner = instinct_for_alarm_key(key)
            if owner:
                context["automation"] = owner
        try:
            sensa_log(self.hass, "alarm", log_message,
                      source=source or SENSA_LOG_SOURCE_VEHICLE,
                      context=context)
        except Exception:  # pragma: no cover
            _LOGGER.debug("Sensa alarms: log mirror failed", exc_info=True)


# -- module-level helpers (the public surface) -------------------------------

def get_alarm_store(hass: Any) -> SensaAlarmStore | None:
    data = getattr(hass, "data", None) or {}
    domain = data.get(DOMAIN)
    if not isinstance(domain, dict):
        return None
    store = domain.get(SENSA_ALARMS_STORE_KEY)
    return store if isinstance(store, SensaAlarmStore) else None


async def async_setup_alarm_store(hass: Any) -> SensaAlarmStore:
    """Create + load the singleton alarm store. Idempotent across entries."""
    existing = get_alarm_store(hass)
    if existing is not None:
        return existing
    path = hass.config.path(SENSA_ALARMS_PATH)
    store = SensaAlarmStore(hass, path)
    await hass.async_add_executor_job(store.load_sync)
    hass.data.setdefault(DOMAIN, {})[SENSA_ALARMS_STORE_KEY] = store
    return store


def raise_alarm(
    hass: Any,
    key: str,
    message: str,
    *,
    resound_every: int | None = None,
    title: str | None = None,
    guidance: str | None = None,
    context: list[dict[str, Any]] | None = None,
    severity: str | None = None,
    log_source: str | None = None,
) -> bool:
    """Raise/refresh a managed alarm. No-op (False) if the store isn't up."""
    store = get_alarm_store(hass)
    if store is None:
        return False
    store.raise_alarm(
        key, message, resound_every=resound_every, title=title,
        guidance=guidance, context=context, severity=severity,
        log_source=log_source,
    )
    return True


def snooze_alarm(hass: Any, key: str, minutes: int | None = None) -> bool:
    store = get_alarm_store(hass)
    return bool(store and store.snooze(key, minutes))


def cancel_alarm(hass: Any, key: str) -> bool:
    store = get_alarm_store(hass)
    return bool(store and store.cancel(key))


def ignore_alarm(hass: Any, key: str) -> bool:
    store = get_alarm_store(hass)
    return bool(store and store.ignore(key))


def ignored_alarm_keys(hass: Any) -> list[str]:
    store = get_alarm_store(hass)
    return sorted(store.ignored) if store is not None else []


def get_alarms(hass: Any) -> list[dict[str, Any]]:
    store = get_alarm_store(hass)
    return store.snapshot() if store is not None else []


def tick_alarms(hass: Any) -> bool:
    store = get_alarm_store(hass)
    return bool(store and store.tick())


def prune_orphaned_alarms(hass: Any) -> int:
    """Cancel alarms whose entity has gone. Runs on the alarm tick, before the
    re-sound, so a stale alarm doesn't nag one more time on its way out.

    An entity counts as present if the entity REGISTRY knows it OR it has a
    state. The registry is the primary answer because it survives both a dead
    sensor (a flat coin cell strips the state's attributes but not the registry
    row) and a restart (it is restored before the owning integration starts, so
    a boot-time sweep can't mistake "not set up yet" for "deleted"). The state
    fallback covers registry-less entities, which the quiet watch can pick up
    via its own state-scan fallback.

    If the registry can't be read at all we return without cancelling anything:
    "could not ask" is not "the entity is gone"."""
    store = get_alarm_store(hass)
    if store is None:
        return 0
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
    except Exception:
        _LOGGER.debug("Sensa alarms: orphan sweep skipped", exc_info=True)
        return 0

    def _exists(entity_id: str) -> bool:
        try:
            if registry.async_get(entity_id) is not None:
                return True
            return hass.states.get(entity_id) is not None
        except Exception:  # noqa: BLE001 - could not ask; assume it's there
            return True

    return store.prune_orphans(_exists)
