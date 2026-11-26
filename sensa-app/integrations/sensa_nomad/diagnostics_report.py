"""Customer-opt-in diagnostic snapshot, carried up on the phone-home.

Split into **five independent consent toggles** (2026-07; was a single
``switch.sensa_nomad_diagnostics``), one per section so a customer can share only
the categories they want -- the ``switch.sensa_nomad_diag_<category>`` switches
(see :data:`CATEGORIES`). :data:`DEFAULT_ON` says which start on. When any is ON, the
integration gathers a snapshot of ONLY the consented sections and POSTs it to the
Sensa App (``POST /api/v1/diagnostics/state``); the App caches it and attaches it
to the next ``/validate`` phone-home as ``telemetry["diagnostics"]``. So support
can see what's wrong without a live remote-access session, and Sensa gets a
fleet-wide view of what hardware people actually run.

**Why the integration gathers it (not the App):** the App's always-on telemetry
(``deployment_telemetry.py``) pulls everything through the Supervisor API, which
Container appliances don't have — so it goes thin there. The Nomad integration
runs inside HA on *every* flavour (HAOS / Container / BYO) with native access to
the registries, states, ``system_log`` and our own resource sensors, so it can
build the same snapshot everywhere.

The snapshot can carry (one section per toggle):

* **devices** — every Sensa device by type (catalogue id / category / vendor / model /
  protocol), never the name the customer gave it,
  the fleet-inventory view.
* **unavailable** — the entities currently ``unavailable``/``unknown`` by type
  (kind of entity, device make and model), never a name, capped.
* **errors** — the recent HA ``system_log`` error/warning tail, secrets redacted.
* **resources** — CPU / memory / disk % (from our resource sensors).
* **uptime** -- integration uptime (a restart-loop proxy).

Excluded even when ON: GPS/Schengen coordinates, credentials, Wi-Fi secrets.
All gathering is **best-effort** -- every section degrades to omitted rather than
raising, so a missing source just sends a leaner snapshot.

The App/Hub contract is unchanged: the App still stores ``{enabled, snapshot}``
with ``enabled = any category on``. The per-entry :class:`DiagnosticsCoordinator`
owns the durable push machinery (15-min heartbeat, off-retry self-heal) and reads
the union of consented categories from the switches so the section set follows the
customer's choices.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Refresh cadence while the toggle is ON. Phone-home is ~hourly; re-posting a
# fresh snapshot every 15 min keeps the App's cached copy recent without a
# heavyweight push. Chosen to bound staleness, not for real-time.
REFRESH_MINUTES = 15

# Bounds so an opted-in device can't push an unbounded payload.
_MAX_UNAVAILABLE = 100
_MAX_ERRORS = 25
_MAX_DEVICES = 200

# Redact anything that looks like a token/secret from a log message before it
# leaves the box. Opt-in consent buys us richer data, never credentials.
_SECRET_RE = re.compile(
    r"(?i)\b(?:token|password|secret|api[_-]?key|bearer|authorization)\b\s*[:=]?\s*\S+"
)

# Location redaction — the card copy + this module's docstring both promise no
# LOCATIONS are ever sent, so strip coordinate data from a log line even under
# opt-in. Two shapes: a LABELLED value (`lat=51`, `longitude: -0.12`, GPS
# coords — integers allowed since the label makes it unambiguous), and a bare
# decimal-degree PAIR (`51.5, -0.12`, `51.5074, -0.1278` — at least one decimal
# so a plain integer pair like "1, 2" in a log isn't nuked). Best-effort like the
# rest — redaction is never a guarantee, but it honours the promise.
_LOCATION_RE = re.compile(
    r"(?i)\b(?:lat(?:itude)?|lon(?:g(?:itude)?)?|gps|coord(?:inate)?s?|geo)\b"
    r"\s*[:=]?\s*[-+]?\d{1,3}(?:\.\d+)?"
)
_LATLON_PAIR_RE = re.compile(
    r"[-+]?\d{1,3}\.\d+\s*,\s*[-+]?\d{1,3}\.\d+"
)

# The consent categories, 1:1 with the snapshot sections built below and with the
# ``switch.sensa_nomad_diag_<category>`` toggles on Settings -> Admin. A
# snapshot carries only the sections the customer has consented to.
CATEGORIES = ("devices", "unavailable", "errors", "resources", "uptime", "solar")

# On by default on a new or factory-reset system: every category (Richard,
# 2026-10-21, to encourage sharing; the Setup Wizard shows them on its first
# page so each can be turned off). Was the four fault-detection categories,
# with the device inventory and solar forecast accuracy off as opt-ins. The
# privacy policy says which start on and on what basis; change both together.
DEFAULT_ON = frozenset(CATEGORIES)


def category_switch_entity_id(category: str) -> str:
    """The switch entity backing a diagnostics consent category."""
    return f"switch.sensa_nomad_diag_{category}"


def build_snapshot(
    hass: HomeAssistant, entry: Any, categories: set[str] | None = None
) -> dict[str, Any]:
    """Assemble the diagnostic snapshot. Never raises — each section degrades."""
    out: dict[str, Any] = {"collected_at": datetime.now(timezone.utc).isoformat()}
    for key, fn in (
        ("devices", _device_inventory),
        ("unavailable", _unavailable_entities),
        ("errors", _recent_errors),
        ("resources", _resources),
        ("uptime", _uptime),
        ("solar", _solar_accuracy),
    ):
        if categories is not None and key not in categories:
            continue
        try:
            value = fn(hass, entry)
        except Exception:
            _LOGGER.debug("diagnostics section %s failed", key, exc_info=True)
            continue
        if value:
            out[key] = value
    return out


async def _post(
    hass: HomeAssistant,
    entry: Any,
    enabled: bool,
    snapshot: dict[str, Any] | None,
    consent_changed: bool = False,
) -> bool:
    """POST the consent state (+ snapshot when enabled) to the App.

    ``consent_changed`` marks a genuine customer change (a toggle flip) vs a
    heartbeat/reload re-sync, so the App can fire an immediate phone-home when the
    consented category set changes even though the master enabled flag doesn't.

    Returns True when the App accepted it, False on any failure (so the coordinator
    can retry). Best-effort: a briefly-unreachable App just means Sensa sees the
    change on the next push / reload."""
    entry_id = entry if isinstance(entry, str) else getattr(entry, "entry_id", None)
    runtime = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    client = runtime.get("client")
    if client is None:
        return False
    try:
        await client.async_post_diagnostics_state(
            enabled=enabled, snapshot=snapshot, consent_changed=consent_changed
        )
        return True
    except (ConnectionError, TimeoutError):
        _LOGGER.warning(
            "Could not reach the Sensa App to report diagnostics state -- "
            "will retry; Sensa won't see the change until then."
        )
        return False
    except Exception:
        _LOGGER.debug("Diagnostics push failed", exc_info=True)
        return False


class DiagnosticsCoordinator:
    """Owns the durable diagnostics push machinery for one config entry.

    The five per-category consent switches are thin: on any change they call
    :meth:`async_set_category`, which recomputes the union of consented categories,
    builds a snapshot of ONLY those sections, and pushes ``{enabled, snapshot}`` to
    the App (``enabled = any category on``). While anything is on, a 15-min
    heartbeat re-pushes a fresh snapshot so the App's cached copy never goes stale;
    when everything goes off, a 1-min retry re-asserts OFF until the App confirms,
    so a lost 'off' self-heals. All best-effort -- a briefly-unreachable App is
    caught up on the next tick / reload."""

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        self.hass = hass
        self._entry = entry
        self._categories: set[str] = set()
        self._unsub_heartbeat = None
        self._unsub_retry = None
        self._unsub_prime = None

    def _update(self, category: str, enabled: bool) -> None:
        if enabled:
            self._categories.add(category)
        else:
            self._categories.discard(category)

    async def async_set_category(self, category: str, enabled: bool) -> None:
        """A customer toggled a category — record it and push immediately.

        Marked ``consent_changed`` so the App fires a phone-home even when the
        master enabled flag is unchanged (a category added/removed mid-session)."""
        self._update(category, enabled)
        await self._resync(consent_changed=True)

    async def async_send_now(self) -> bool:
        """Force an immediate push + phone-home of the current consented snapshot.

        Drives the one-off "Send now" button — rebuilds a fresh snapshot of the
        consented sections and pushes it as a consent change, so the App fires an
        immediate phone-home rather than waiting for the next scheduled one. Returns
        whether anything was actually sent (False when no category is on)."""
        await self._resync(consent_changed=True)
        return bool(self._categories)

    def note_restored_category(self, category: str, enabled: bool) -> None:
        """A switch reported its RESTORED consent on startup.

        The five switches each call this as they're added, so the pushes are
        COALESCED into a single re-sync (``async_call_later(0)`` debounce) — without
        it the five would race, each POSTing a progressively-different snapshot, and
        last-write-wins on the App could settle on a subset of consented sections."""
        self._update(category, enabled)
        self._schedule_prime()

    def _schedule_prime(self) -> None:
        if self._unsub_prime is not None:
            self._unsub_prime()
        self._unsub_prime = async_call_later(self.hass, 0, self._prime_now)

    async def _prime_now(self, _now) -> None:
        self._unsub_prime = None
        # A reload just re-establishes the existing consent — not a NEW customer
        # change — so don't force an immediate phone-home (avoids one per reboot).
        await self._resync(consent_changed=False)

    async def _resync(self, *, consent_changed: bool) -> None:
        master = bool(self._categories)
        snapshot = (
            build_snapshot(self.hass, self._entry, set(self._categories))
            if master
            else None
        )
        ok = await _post(self.hass, self._entry, master, snapshot, consent_changed)
        if master:
            self._arm_heartbeat()
        else:
            self._disarm_heartbeat()
        # Prompt retry on ANY failed delivery (widen / narrow / full-off), so a
        # briefly-unreachable App is caught within a minute rather than lingering on
        # a stale snapshot until the 15-min heartbeat.
        if ok:
            self._cancel_retry()
        else:
            self._arm_retry()

    def _arm_heartbeat(self) -> None:
        if self._unsub_heartbeat is not None:
            return
        self._unsub_heartbeat = async_track_time_interval(
            self.hass, self._heartbeat, timedelta(minutes=REFRESH_MINUTES)
        )

    def _disarm_heartbeat(self) -> None:
        if self._unsub_heartbeat is not None:
            self._unsub_heartbeat()
            self._unsub_heartbeat = None

    async def _heartbeat(self, _now) -> None:
        # Re-push a fresh snapshot of the still-consented sections (also re-delivers
        # the state, so a lost push self-heals here).
        await self._resync(consent_changed=False)

    def _arm_retry(self) -> None:
        if self._unsub_retry is not None:
            return
        self._unsub_retry = async_track_time_interval(
            self.hass, self._retry_tick, timedelta(minutes=1)
        )

    async def _retry_tick(self, _now) -> None:
        # _resync cancels this timer on success and leaves it armed on failure.
        await self._resync(consent_changed=False)

    def _cancel_retry(self) -> None:
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None

    async def async_shutdown(self) -> None:
        """Cancel all timers (registered via ``entry.async_on_unload``)."""
        self._disarm_heartbeat()
        self._cancel_retry()
        if self._unsub_prime is not None:
            self._unsub_prime()
            self._unsub_prime = None


def get_coordinator(hass: HomeAssistant, entry: Any) -> DiagnosticsCoordinator:
    """Fetch-or-create the per-entry diagnostics coordinator."""
    entry_id = entry if isinstance(entry, str) else getattr(entry, "entry_id", None)
    runtime = hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})
    coordinator = runtime.get("diagnostics_coordinator")
    if coordinator is None:
        coordinator = DiagnosticsCoordinator(hass, entry)
        runtime["diagnostics_coordinator"] = coordinator
    return coordinator


# ---------------------------------------------------------------------------
# Section gatherers (each returns a plain-JSON value; None/empty is dropped)
# ---------------------------------------------------------------------------


def _coordinator_devices(hass: HomeAssistant, entry: Any) -> list[dict]:
    entry_id = entry if isinstance(entry, str) else getattr(entry, "entry_id", None)
    runtime = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    coordinator = runtime.get("coordinator")
    data = getattr(coordinator, "data", None) or {}
    devices = data.get("devices") if isinstance(data, dict) else None
    return devices if isinstance(devices, list) else []


def _device_inventory(hass: HomeAssistant, entry: Any) -> list[dict]:
    """Which devices the customer has, by TYPE: catalogue id / category / vendor /
    model / protocol. Never the name the customer gave it (Richard, 2026-10-21):
    the inventory is for counting which devices are popular, and a name can be
    personal."""
    from .device_catalog import catalog_id_for

    out: list[dict] = []
    for d in _coordinator_devices(hass, entry)[:_MAX_DEVICES]:
        if not isinstance(d, dict):
            continue
        if d.get("ignored"):
            continue
        out.append(
            {
                # Resolved, not read: the row itself almost never carries it.
                "catalog_id": catalog_id_for(d) or None,
                "category": d.get("category"),
                "vendor": d.get("vendor"),
                "model": d.get("model") or d.get("vendor_model"),
                "protocol": d.get("integration_type"),
                "enabled": bool(d.get("enabled", True)),
            }
        )
    return out


def _unavailable_entities(hass: HomeAssistant, entry: Any) -> list[dict]:
    """The entities currently unavailable/unknown, by TYPE: the kind of entity
    and the make and model of its device. Never a name or an entity id, both of
    which the customer can type (Richard, 2026-10-21)."""
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    try:
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(hass)
    except Exception:  # noqa: BLE001
        dev_reg = None

    out: list[dict] = []
    for state in hass.states.async_all():
        if state.state not in ("unavailable", "unknown"):
            continue
        entity_id = state.entity_id
        manufacturer: str | None = None
        model: str | None = None
        entry_ent = ent_reg.async_get(entity_id)
        if entry_ent is not None and entry_ent.device_id and dev_reg is not None:
            dev = dev_reg.async_get(entry_ent.device_id)
            if dev is not None:
                # Set by the integration that made the device, not the customer.
                manufacturer = dev.manufacturer
                model = dev.model
        out.append(
            {
                "domain": entity_id.split(".", 1)[0],
                "state": state.state,
                "manufacturer": manufacturer,
                "model": model,
            }
        )
        if len(out) >= _MAX_UNAVAILABLE:
            break
    return out


def _redact(message: str) -> str:
    message = _SECRET_RE.sub("[redacted]", message)
    message = _LOCATION_RE.sub("[location]", message)
    message = _LATLON_PAIR_RE.sub("[location]", message)
    return message


def _recent_errors(hass: HomeAssistant, entry: Any) -> list[dict]:
    """The recent HA ``system_log`` error/warning tail, secrets redacted."""
    handler = hass.data.get("system_log")
    records = getattr(handler, "records", None)
    if records is None:
        return []
    try:
        items = list(records.values())
    except Exception:  # noqa: BLE001 - DedupStore internals vary across HA versions
        try:
            items = list(records)
        except Exception:  # noqa: BLE001
            return []

    out: list[dict] = []
    for rec in reversed(items):
        try:
            data = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
        except Exception:  # noqa: BLE001, S112
            continue
        level = str(data.get("level", "")).upper()
        if level not in ("ERROR", "WARNING", "CRITICAL"):
            continue
        message = data.get("message")
        if isinstance(message, (list, tuple)):
            message = message[0] if message else ""
        out.append(
            {
                "level": level,
                "source": (data.get("source") or [None])[0]
                if isinstance(data.get("source"), (list, tuple))
                else data.get("source"),
                "name": data.get("name"),
                "message": _redact(str(message or ""))[:500],
                "count": data.get("count"),
                "timestamp": data.get("timestamp"),
            }
        )
        if len(out) >= _MAX_ERRORS:
            break
    return out


def _num(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        return round(float(state.state), 1)
    except (TypeError, ValueError):
        return None


def _resources(hass: HomeAssistant, entry: Any) -> dict:
    """CPU / memory / disk % from our own resource sensors (works sans Supervisor)."""
    out: dict[str, Any] = {}
    for key, entity_id in (
        ("cpu_percent", "sensor.sensa_nomad_cpu_use"),
        ("memory_percent", "sensor.sensa_nomad_memory_use"),
        ("disk_percent", "sensor.sensa_nomad_disk_use"),
    ):
        value = _num(hass, entity_id)
        if value is not None:
            out[key] = value
    mem = hass.states.get("sensor.sensa_nomad_memory_use")
    if mem is not None and mem.attributes.get("total_gb") is not None:
        out["memory_total_gb"] = mem.attributes.get("total_gb")
    return out


def _solar_accuracy(hass: HomeAssistant, entry: Any) -> dict:
    """Solar forecast against actual, by day, so Sensa can tune the forecast
    (solar_accuracy.py). The last 14 days are sent every time, so a missed
    phone-home loses nothing. Position only to 0.1 degrees (~10 km), the same
    rounding the forecast request itself uses; nothing when no rating is set."""
    from homeassistant.util import dt as dt_util

    from . import solar_accuracy
    from .const import DOMAIN as _DOMAIN
    from .solar_forecast import PERFORMANCE_RATIO

    entry_id = entry if isinstance(entry, str) else getattr(entry, "entry_id", None)
    runtime = hass.data.get(_DOMAIN, {}).get(entry_id) or {}
    state = runtime.get("solar_accuracy") or {}
    coordinator = runtime.get("solar_coordinator")
    rating = coordinator.rating_wp() if coordinator is not None else 0.0
    if rating <= 0:
        return {}
    today = dt_util.now().date()
    days = [
        r
        for r in solar_accuracy.day_rows(state, today, solar_accuracy.SHARE_DAYS + 1)
        if r["date"] != today.isoformat()  # complete days only
    ]
    if not days:
        return {}
    position = (coordinator.data or {}).get("position") if coordinator else None
    out: dict[str, Any] = {
        "panel_rating_w": rating,
        "performance_ratio": PERFORMANCE_RATIO,
        "days": days,
    }
    if position:
        out["position"] = [round(position[0], 1), round(position[1], 1)]
    return out


def _uptime(hass: HomeAssistant, entry: Any) -> dict:
    """Integration uptime (a proxy for restart-looping)."""
    entry_id = entry if isinstance(entry, str) else getattr(entry, "entry_id", None)
    runtime = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    setup_utc = runtime.get("setup_utc")
    if not setup_utc:
        return {}
    try:
        started = datetime.fromisoformat(setup_utc)
    except (TypeError, ValueError):
        return {}
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - started).total_seconds()
    return {"integration_started_at": setup_utc, "integration_uptime_hours": round(seconds / 3600, 1)}
