"""Zigbee2MQTT (Z2M) device enumeration + classification.

Z2M publishes each paired Zigbee device over MQTT; HA's *shared* `mqtt`
integration auto-discovers them as HA devices/entities. This module reads HA's
device/entity registries to surface those Zigbee devices in Sensa's Discovery
picker — the FIRST Sensa device source that lives behind a shared integration
(everything else is a per-device integration or a per-device cooperate-flow).

We key strictly on the Z2M device identifier ``("mqtt", "zigbee2mqtt_0x...")``
so other mqtt-discovered devices (and the Z2M *Bridge* device — a software
bridge, not a customer device; the physical coordinator is surfaced separately
via the `smlight` cooperate-flow) are ignored. The Zigbee IEEE address is the
stable per-device key; it is stored MAC-style (uppercase, no ``0x``) so it
round-trips through the discovery sensor's `_canonical_mac` and the backend's
MAC-keyed registered-device matching unchanged.

Sensa never *owns* these devices: HA's mqtt integration creates and manages the
HA device + entities. So a Sensa-side removal must NOT delete the HA device
(`_native_domain_for_integration("zigbee2mqtt")` returns None → the native-entry
removal guard no-ops; no `_SENSA_DEVICE_IDENTIFIER_PREFIXES` entry → the purge
no-ops). This module only reads the registries and registers a backend row.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import callback

from ..const import INTEGRATION_ZIGBEE2MQTT, ZIGBEE_UNRECOGNISED_SUBTYPE

_LOGGER = logging.getLogger(__name__)

# Identifier prefix Z2M stamps on every device it publishes (incl. the bridge,
# whose tail begins "bridge"). HA stores it as ``("mqtt", "zigbee2mqtt_<tail>")``.
_Z2M_IDENTIFIER_PREFIX = "zigbee2mqtt_"

# device_class of the ONE bridge entity that reports whether Z2M is running
# (its "Connection state" binary_sensor). Matched on the class rather than the
# entity_id slug so a rename cannot lose it. See `_zigbee_bridge_online`.
_Z2M_BRIDGE_CONNECTION_DEVICE_CLASS = "connectivity"

# device_class values that mark an environment (temperature/humidity/…) sensor.
_ENVIRONMENT_SENSOR_CLASSES = frozenset({
    "temperature", "humidity", "pressure", "carbon_dioxide", "pm25",
    "volatile_organic_compounds", "illuminance",
})
# binary_sensor device_class buckets → Sensa security service class.
_MOTION_CLASSES = frozenset({"motion", "occupancy", "moving", "presence"})
_CONTACT_CLASSES = frozenset({"door", "window", "opening", "garage_door"})
_LEAK_CLASSES = frozenset({"moisture"})
_VIBRATION_CLASSES = frozenset({"vibration"})

# Service classes Sensa currently surfaces for Z2M devices. A device that
# classifies to anything NOT in this set is left out of the picker (it would
# register fine but have no dashboard surface yet — conditional cards render
# nothing when absent, so an addable-but-invisible device would only confuse).
# Phase 1 shipped lights; Phase 2 added `environment_sensor` (temp/humidity →
# header chips + Climate dials); Phase 3 adds the `security` classes
# (motion / contact / leak → the conditional Security tab); Phase 4 adds
# `scene_button` (SNZB-01M), whose surface is the Routine Editor's When picker
# rather than a card — a press is an input, not a reading.
_ENABLED_SERVICE_CLASSES = frozenset({
    "lighting",
    "environment_sensor",
    "motion_sensor",
    "contact_sensor",
    "leak_sensor",
    "vibration_sensor",
    "scene_button",
})


def known_zigbee_models() -> list[str]:
    """Catalog model strings for the Zigbee devices Sensa officially supports.

    The Zigbee allowlist is DATA, not code: a catalog entry with a
    ``discovery.zigbee.model`` block declares a supported model. A Z2M device
    whose model matches one of these is shown in the Discovery picker's
    *Discovered* group (addable); anything else falls to *Others* (debug-gated,
    not addable) — exactly how Bluetooth treats unknown adverts. Grow support
    by adding a catalog entry, no code change.
    """
    try:
        from ..device_catalog import PHYSICAL_DEVICE_CATALOG
    except Exception:  # noqa: BLE001
        return []
    models: list[str] = []
    for entry in PHYSICAL_DEVICE_CATALOG:
        zb = (entry.get("discovery") or {}).get("zigbee") or {}
        model = str(zb.get("model") or "").strip()
        if model:
            models.append(model)
    return models


# ---------------------------------------------------------------------------
# Z2M bridge cache - the raw Zigbee identity HA never sees
# ---------------------------------------------------------------------------
#
# Everything else in this module reads HA's registries. That is enough right up
# until Z2M cannot resolve a device, and then it is enough for NOTHING: Z2M
# publishes HA discovery only for a device it has a definition for, so an
# unrecognised device has no HA device, no entities, and no registry row to
# read. It is not mislabelled, it is invisible.
#
# Tuya makes that a routine event rather than an edge case. Its devices nearly
# all announce the same generic modelID (`TS0601`) and are told apart only by
# the `manufacturerName` (`_TZE204_8fffc3kb`), which the factory CHANGES between
# production runs with no change to the product. A batch whose code is not yet
# in zigbee-herdsman-converters simply disappears from Sensa, silently.
#
# So we subscribe to Z2M's own retained bridge topics and keep the raw identity
# of every paired device, resolved or not. Read-only: this cache never drives
# control, only identification, the two alerts that report an unknown batch, and
# the catalog's second match key.
def _bridge_cache(hass: Any) -> dict[str, Any]:
    """The mutable bridge cache, created on first use."""
    from ..const import Z2M_BRIDGE_DATA_KEY

    return hass.data.setdefault(
        Z2M_BRIDGE_DATA_KEY, {"devices": {}, "info": {}},
    )


def bridge_devices(hass: Any) -> dict[str, dict[str, Any]]:
    """Every paired Z2M device from `bridge/devices`, keyed by Sensa-style IEEE.

    Sensa-style = uppercase, no ``0x``, matching `zigbee2mqtt_device_ieee` so a
    bridge row and a registry row for the same device join on one key. Empty
    until the first retained payload lands (or for ever, if Z2M isn't running).
    """
    return dict(_bridge_cache(hass).get("devices") or {})


def bridge_info(hass: Any) -> dict[str, Any]:
    """Z2M's `bridge/info` payload - version, converters version, coordinator."""
    return dict(_bridge_cache(hass).get("info") or {})


def bridge_action_values(hass: Any, ieee: str) -> list[str]:
    """Every raw `action` a Z2M device can send (`1_single`, `single_button_1`).

    The event entity's own `event_types` cannot stand in for this. Z2M's
    template splits an action shaped `<button>_<press>`, so a Moes 4 gang lists
    only `single` / `double` / `hold` and carries the button in a `button`
    attribute: four buttons, one list, and nothing to pick a button with.
    Empty when the bridge cache has no such device or expose.
    """
    definition = (bridge_devices(hass).get(ieee) or {}).get("definition") or {}
    for expose in definition.get("exposes") or ():
        if isinstance(expose, dict) and expose.get("name") == "action":
            return [str(v) for v in expose.get("values") or ()]
    return []


def bridge_side_values(hass: Any, ieee: str) -> list[str]:
    """The faces an Aqara Cube T1 Pro's `side` can name, for a routine to pick.

    Empty for anything else, including a device still interviewing, which has
    no definition yet.
    """
    from ..const import CUBE_T1_PRO_MODEL

    definition = (bridge_devices(hass).get(ieee) or {}).get("definition") or {}
    return ["1", "2", "3", "4", "5", "6"] if definition.get("model") == CUBE_T1_PRO_MODEL else []


def bridge_press_values(hass: Any, ieee: str) -> list[str]:
    """What the When picker offers for a Z2M device: its raw actions, and on the
    cube `CUBE_TURN_PRESS` straight after the two it stands for."""
    from ..const import CUBE_T1_PRO_MODEL, CUBE_TURN_PRESS

    raw = bridge_action_values(hass, ieee)
    definition = (bridge_devices(hass).get(ieee) or {}).get("definition") or {}
    if definition.get("model") != CUBE_T1_PRO_MODEL or "rotate_right" not in raw:
        return raw
    at = raw.index("rotate_right") + 1
    return [*raw[:at], CUBE_TURN_PRESS, *raw[at:]]


def _bridge_ieee(raw: Any) -> str:
    """A bridge payload's `ieee_address` in Sensa's MAC-style form."""
    return str(raw or "").strip().lower().removeprefix("0x").upper()


def _store_bridge_devices(hass: Any, payload: Any) -> None:
    """Replace the device cache from a decoded `bridge/devices` payload.

    Z2M republishes the WHOLE list on every change, so this replaces rather than
    merges: a device removed from the network must leave the cache, or a stale
    row keeps alerting about hardware that is no longer there.
    """
    if not isinstance(payload, list):
        return
    devices: dict[str, dict[str, Any]] = {}
    for dev in payload:
        if not isinstance(dev, dict):
            continue
        # The coordinator is the radio, not a customer device: it has no
        # definition and never will, so keeping it would alert for ever.
        if str(dev.get("type") or "") == "Coordinator":
            continue
        ieee = _bridge_ieee(dev.get("ieee_address"))
        if ieee:
            devices[ieee] = dev
    _bridge_cache(hass)["devices"] = devices
    _bridge_cache(hass)["devices_received"] = True


def bridge_knows_device(hass: Any, ieee: str) -> bool | None:
    """Whether Z2M's own device list holds `ieee` (Sensa-style). None until a
    `bridge/devices` payload has landed: an empty cache is "could not ask", not
    "Z2M has no such device"."""
    cache = _bridge_cache(hass)
    if not cache.get("devices_received"):
        return None
    return _bridge_ieee(ieee) in (cache.get("devices") or {})


def unrecognised_bridge_devices(hass: Any) -> list[dict[str, Any]]:
    """Paired devices Z2M itself could not identify.

    `supported: false` means Z2M resolved no definition, so the device exposes
    nothing and Sensa cannot make it work - only a converters table that knows
    its code can. These are reported, never rendered as addable: an addable row
    that cannot do anything is a support call of its own.

    A device still interviewing is skipped: `supported` is not meaningful until
    the interview finishes, and a freshly-paired device would otherwise alert
    for the minute or two before it settles.
    """
    out: list[dict[str, Any]] = []
    for dev in bridge_devices(hass).values():
        if dev.get("supported"):
            continue
        if dev.get("disabled"):
            continue
        state = str(dev.get("interview_state") or "")
        if state and state != "SUCCESSFUL":
            continue
        if not state and not dev.get("interview_completed"):
            continue
        out.append(dev)
    return out


def _interview_failed(dev: dict[str, Any]) -> bool:
    """True when Z2M's interview of this device ended in failure.

    `interview_state` is Z2M 2.x; older Z2M only has the two booleans, where
    "not interviewing and not completed" is the same failed state.
    """
    state = str(dev.get("interview_state") or "")
    if state:
        return state == "FAILED"
    return not dev.get("interviewing") and not dev.get("interview_completed")


def failed_interview_bridge_devices(hass: Any) -> list[dict[str, Any]]:
    """Paired devices whose interview failed, so Z2M never learned what they are.

    Not the same as `unrecognised_bridge_devices`: there Z2M asked and did not
    recognise the answer; here it never got an answer (the device slept), so a
    re-interview while it is awake fixes it with nothing to update.
    """
    return [
        dev for dev in bridge_devices(hass).values()
        if not dev.get("disabled") and _interview_failed(dev)
    ]


async def async_request_reinterviews(hass: Any) -> int:
    """Ask Z2M to re-interview every failed device. Returns how many were asked.

    A sleeping device makes the attempt time out inside Z2M, which costs nothing
    but a log line there; the next attempt is the one that may catch it awake.
    A device already IN_PROGRESS is not failed, so it is never asked twice.
    """
    import json as _json

    from ..const import Z2M_REINTERVIEW_INTERVAL_SECONDS, Z2M_REINTERVIEW_TOPIC

    failed = failed_interview_bridge_devices(hass)
    if not failed or not hass.services.has_service("mqtt", "publish"):
        return 0
    logged: set[str] = _bridge_cache(hass).setdefault("reinterview_logged", set())
    asked = 0
    for dev in failed:
        raw_ieee = str(dev.get("ieee_address") or "").strip()
        if not raw_ieee:
            continue
        try:
            await hass.services.async_call(
                "mqtt",
                "publish",
                {"topic": Z2M_REINTERVIEW_TOPIC, "payload": _json.dumps({"id": raw_ieee})},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - one device must not stop the rest
            _LOGGER.debug("Z2M re-interview request for %s failed: %s", raw_ieee, err)
            continue
        asked += 1
        # Logged on the edge, not every 30 s for as long as it stays failed.
        if raw_ieee not in logged:
            logged.add(raw_ieee)
            _LOGGER.info(
                "Z2M: %s (%s) did not finish pairing; re-interviewing every %ss "
                "until it answers",
                dev.get("friendly_name") or raw_ieee, raw_ieee,
                Z2M_REINTERVIEW_INTERVAL_SECONDS,
            )
    return asked


def _interview_succeeded(dev: dict[str, Any]) -> bool:
    state = str(dev.get("interview_state") or "")
    if state:
        return state == "SUCCESSFUL"
    return bool(dev.get("interview_completed"))


def _recovered_interviews(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]],
) -> list[str]:
    """IEEEs (Sensa-style) that were failed in `before` and succeeded in `after`."""
    return [
        ieee for ieee, dev in after.items()
        if ieee in before
        and _interview_failed(before[ieee])
        and _interview_succeeded(dev)
    ]


def _version_tuple(value: str) -> tuple[int, ...]:
    """A dotted version as a comparable tuple; non-numeric parts drop out."""
    parts: list[int] = []
    for part in str(value or "").split("."):
        digits = "".join(c for c in part if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def converters_version_behind(hass: Any) -> tuple[str, str] | None:
    """`(running, floor)` when Z2M's identity table is older than our floor.

    zigbee-herdsman-converters, NOT the Z2M version that wraps it: the converters
    package is the table that decides whether a Tuya batch code resolves at all,
    so it is the only version whose age explains a missing device. None when it
    is current, or when we have no `bridge/info` yet - "could not ask" is not
    "out of date", and telling a customer to update on the strength of a payload
    that never arrived is the failure this repo bans everywhere else.
    """
    from ..const import Z2M_MIN_CONVERTERS_VERSION

    info = bridge_info(hass)
    raw = info.get("zigbee_herdsman_converters")
    running = str(
        (raw.get("version") if isinstance(raw, dict) else "") or ""
    ).strip()
    if not running:
        return None
    have = _version_tuple(running)
    floor = _version_tuple(Z2M_MIN_CONVERTERS_VERSION)
    if not have or not floor or have >= floor:
        return None
    return (running, Z2M_MIN_CONVERTERS_VERSION)


async def async_start_bridge_watch(hass: Any) -> Any:
    """Subscribe to Z2M's retained bridge topics. Returns an unsubscribe callable.

    Returns None (quietly) when HA's mqtt integration isn't loaded - a DIY box
    with no Zigbee at all is the common case, not an error.
    """
    from ..const import Z2M_BRIDGE_DEVICES_TOPIC, Z2M_BRIDGE_INFO_TOPIC

    try:
        from homeassistant.components import mqtt
    except Exception:  # noqa: BLE001
        return None
    import json as _json

    def _decode(msg: Any) -> Any:
        try:
            return _json.loads(msg.payload)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Z2M bridge: undecodable payload on %s", msg.topic)
            return None

    @callback
    def _on_devices(msg: Any) -> None:
        payload = _decode(msg)
        if payload is not None:
            before = bridge_devices(hass)
            _store_bridge_devices(hass, payload)
            _schedule_state_prime(hass)
            recovered = _recovered_interviews(before, bridge_devices(hass))
            if recovered:
                from homeassistant.helpers.dispatcher import async_dispatcher_send

                from ..const import SIGNAL_Z2M_INTERVIEW_RECOVERED

                logged = _bridge_cache(hass).setdefault("reinterview_logged", set())
                for ieee in recovered:
                    raw = str(before[ieee].get("ieee_address") or "")
                    logged.discard(raw)
                    _LOGGER.info(
                        "Z2M: %s finished pairing on a re-interview",
                        before[ieee].get("friendly_name") or raw,
                    )
                async_dispatcher_send(hass, SIGNAL_Z2M_INTERVIEW_RECOVERED, recovered)

    async def _reinterview_tick(_now: Any = None) -> None:
        try:
            await async_request_reinterviews(hass)
        except Exception:
            _LOGGER.debug("Z2M re-interview tick failed", exc_info=True)

    @callback
    def _on_info(msg: Any) -> None:
        payload = _decode(msg)
        if isinstance(payload, dict):
            _bridge_cache(hass)["info"] = payload

    try:
        unsub_devices = await mqtt.async_subscribe(
            hass, Z2M_BRIDGE_DEVICES_TOPIC, _on_devices,
        )
        unsub_info = await mqtt.async_subscribe(
            hass, Z2M_BRIDGE_INFO_TOPIC, _on_info,
        )
    except Exception:
        # No broker configured yet, or mqtt not set up. Not fatal: the cache
        # stays empty and every reader degrades to the registry-only behaviour
        # it had before this existed.
        _LOGGER.debug("Z2M bridge watch not started", exc_info=True)
        return None

    from datetime import timedelta

    from homeassistant.helpers.event import async_track_time_interval

    from ..const import Z2M_REINTERVIEW_INTERVAL_SECONDS

    # A timer rather than a reaction to the device: Z2M publishes nothing at all
    # for a device it has not interviewed, so there is no "it woke up" to react to.
    unsub_reinterview = async_track_time_interval(
        hass, _reinterview_tick, timedelta(seconds=Z2M_REINTERVIEW_INTERVAL_SECONDS),
    )

    def _unsub() -> None:
        _cancel_state_prime(hass)
        for unsub in (unsub_devices, unsub_info, unsub_reinterview):
            try:
                unsub()
            except Exception:  # pragma: no cover - defensive  # noqa: BLE001, S110
                pass

    _LOGGER.debug("Z2M bridge watch subscribed")
    return _unsub


# Retry cadence for `async_start_bridge_watch_with_retry`: 30s x 20 = ten
# minutes, long enough to outlast a Container first boot where the mqtt entry is
# CREATED by the containers poller (every 30s) after a slow broker comes up.
BRIDGE_WATCH_RETRY_SECONDS = 30
BRIDGE_WATCH_RETRY_ATTEMPTS = 20


async def async_start_bridge_watch_with_retry(
    hass: Any,
    *,
    attempts: int = BRIDGE_WATCH_RETRY_ATTEMPTS,
    delay: float = BRIDGE_WATCH_RETRY_SECONDS,
) -> Any:
    """`async_start_bridge_watch`, retried until the subscribe takes.

    HA sets integrations up concurrently and nothing orders ours after mqtt's,
    so at our setup the mqtt entry is not necessarily loaded, and on a Container
    appliance it may not even EXIST yet (the containers poller creates it). HA's
    `mqtt.async_subscribe` refuses a subscribe before mqtt is set up, the watch
    swallowed that to None, and nothing tried again: the cache stayed cold for
    the whole boot. Proven on a customer box 2026-09-08: the first import of
    HA's mqtt module that boot happened INSIDE the watch, ten seconds before
    mqtt finished loading; every Skydance WZ5 then lost its photo, its five
    Light Type options and its Tunable White detection (all three read the raw
    Tuya code off this cache), and an integration reload brought them back.

    Returns the unsubscribe callable, or None once every attempt is spent (a DIY
    box with no MQTT at all: cheap, and everything downstream already treats a
    cold cache as "could not ask").
    """
    import asyncio

    for attempt in range(max(1, attempts)):
        unsub = await async_start_bridge_watch(hass)
        if unsub is not None:
            if attempt:
                _LOGGER.info(
                    "Z2M bridge watch subscribed on attempt %d (mqtt was not "
                    "set up at integration start)", attempt + 1,
                )
            return unsub
        if attempt == 0:
            _LOGGER.debug(
                "Z2M bridge watch: mqtt not set up yet, retrying every %ss",
                delay,
            )
        await asyncio.sleep(delay)
    _LOGGER.debug("Z2M bridge watch: gave up after %d attempts (no MQTT?)", attempts)
    return None


# --- state prime -------------------------------------------------------------
#
# Z2M does not retain device states on the broker, so after a Z2M restart every
# HA entity it owns reads `unknown` until the device next reports, and after an
# HA restart likewise until Z2M's cached-state replay lands. A light that is on
# the mesh but idle can sit `unknown` for hours: Sensa's reachability dot counts
# `unknown` as dead, so the customer sees RED on a working strip. Proven on a
# customer box 2026-09-08: two Gledopto GL-SPI-206P went red after the Z2M
# repair and green the instant one toggle from Z2M's page made them report.
#
# Nobody should have to do that toggle. So, a little after Z2M announces its
# device list (which it does on every start), ask each MAINS-POWERED device that
# is still `unknown` to read its state back (`<base>/<friendly_name>/get`).
# Routers answer at once; battery devices sleep, so a get to them would sit
# queued for ever and is not sent. The delay lets Z2M's own cached-state replay
# land first, so only what is genuinely still unknown gets asked.
STATE_PRIME_DELAY_SECONDS = 20
_STATE_PRIME_DOMAINS = frozenset({"light", "switch"})


def unknown_state_devices(hass: Any) -> list[tuple[str, list[str]]]:
    """`(friendly_name, [entity_ids])` for each mains Z2M device still unknown.

    A device qualifies when it is a Router in Z2M's bridge list, has at least
    one light/switch entity in HA, and EVERY such entity reads `unknown` (or
    has no state yet). One entity with a real state means the device has
    reported and needs nothing. `unavailable` is deliberately not included:
    that is the bridge being down, which no get can fix.
    """
    bridge = bridge_devices(hass)
    if not bridge:
        return []
    mqtt_entries = _mqtt_entry_ids(hass)
    if not mqtt_entries:
        return []
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, list[str]]] = []
    for ha_dev in list(dev_reg.devices.values()):
        if not mqtt_entries.intersection(ha_dev.config_entries or ()):
            continue
        ieee = zigbee2mqtt_device_ieee(ha_dev)
        if not ieee:
            continue
        raw = bridge.get(ieee) or {}
        if str(raw.get("type") or "") != "Router":
            continue
        friendly = str(raw.get("friendly_name") or "").strip()
        if not friendly:
            continue
        try:
            ents = er.async_entries_for_device(
                ent_reg, ha_dev.id, include_disabled_entities=False,
            )
        except Exception:  # noqa: BLE001
            ents = []
        wanted = [e for e in ents if getattr(e, "domain", "") in _STATE_PRIME_DOMAINS]
        if not wanted:
            continue
        stale: list[str] = []
        for e in wanted:
            st = hass.states.get(e.entity_id)
            if st is None or str(getattr(st, "state", "")) == "unknown":
                stale.append(e.entity_id)
        if len(stale) != len(wanted):
            continue
        out.append((friendly, stale))
    return out


async def async_prime_unknown_states(hass: Any) -> int:
    """Ask Z2M to read state from every device `unknown_state_devices` lists.

    Returns how many devices were asked. Best-effort: no mqtt publish service
    (a box with no broker) means nothing to do, and a failed publish is logged
    at debug and skipped, never raised.
    """
    import json as _json

    from ..const import Z2M_BASE_TOPIC

    targets = unknown_state_devices(hass)
    if not targets:
        return 0
    try:
        if not hass.services.has_service("mqtt", "publish"):
            return 0
    except Exception:  # noqa: BLE001
        return 0
    asked = 0
    for friendly, entity_ids in targets:
        try:
            await hass.services.async_call(
                "mqtt",
                "publish",
                {
                    "topic": f"{Z2M_BASE_TOPIC}/{friendly}/get",
                    "payload": _json.dumps({"state": ""}),
                },
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - one device must not stop the rest
            _LOGGER.debug("Z2M state prime: get for %s failed: %s", friendly, err)
            continue
        asked += 1
        _LOGGER.debug("Z2M state prime: asked %s for its state (%s)", friendly, entity_ids)
    if asked:
        _LOGGER.info(
            "Z2M state prime: asked %d device(s) still reading unknown for their state",
            asked,
        )
    return asked


def _cancel_state_prime(hass: Any) -> None:
    cancel = _bridge_cache(hass).pop("prime_cancel", None)
    if cancel is not None:
        try:
            cancel()
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001, S110
            pass


def _schedule_state_prime(hass: Any) -> None:
    """Debounced: Z2M republishes `bridge/devices` on every change, so a burst
    (a start, a rename, a pairing) collapses into one prime after the delay."""
    from homeassistant.helpers.event import async_call_later

    _cancel_state_prime(hass)

    async def _run(_now: Any = None) -> None:
        _bridge_cache(hass).pop("prime_cancel", None)
        try:
            await async_prime_unknown_states(hass)
        except Exception:
            _LOGGER.debug("Z2M state prime failed", exc_info=True)

    _bridge_cache(hass)["prime_cancel"] = async_call_later(
        hass, STATE_PRIME_DELAY_SECONDS, _run,
    )


def _matching_zigbee_catalog_entry(
    model: str, manufacturer: str, zigbee_manufacturer: str = "",
) -> dict[str, Any] | None:
    """The catalog entry whose Zigbee model matches this device, or None.

    Z2M reports the HA device model loosely — sometimes the bare model
    (``DJT11LM``), sometimes a description that contains it (``Vibration sensor
    (DJT11LM)``) — so a catalog model matches if it appears in the device's
    model string.

    The model id is the strong signal (Z2M model ids like ``DJT11LM`` are
    vendor-unique). The catalog's ``manufacturer`` is only a TIEBREAKER for the
    rare case of two catalog entries sharing a model id — NOT a hard gate. Z2M
    reports the *vendor* inconsistently for the same physical device: the Aqara
    DJT11LM / SJCGQ11LM are both ``lumi.*`` parts and come through as "Xiaomi"
    or "LUMI", not "Aqara". Gating on an exact manufacturer substring silently
    dropped these supported devices to *Others* with no product image (and,
    once added, an image-less Added row — the bug this fixes).

    `zigbee_manufacturer` is the RAW Zigbee `manufacturerName` from Z2M's bridge
    payload (`_TZE204_8fffc3kb`), a SECOND match key for the Tuya case the model
    id cannot survive: nearly every Tuya device announces the same generic
    `TS0601`, and the factory changes the manufacturerName between production
    runs. A catalog entry lists the codes it has been seen under in
    `discovery.zigbee.manufacturer_names`, and a new batch is then a one-line
    catalog edit rather than a code change. Model first, always: the code is the
    fallback, because a device whose model DOES resolve is better identified by
    it (a WZ5's model id IS its output mode, which the manufacturerName only
    implies).
    """
    model_l = str(model or "").lower()
    manuf_l = str(manufacturer or "").lower()
    if not model_l:
        return None
    try:
        from ..device_catalog import PHYSICAL_DEVICE_CATALOG
    except Exception:  # noqa: BLE001
        return None  # pragma: no cover
    matches: list[dict[str, Any]] = []
    for entry in PHYSICAL_DEVICE_CATALOG:
        zb = (entry.get("discovery") or {}).get("zigbee") or {}
        cat_model = str(zb.get("model") or "").strip()
        if not cat_model:
            continue
        if cat_model.lower() not in model_l and model_l != cat_model.lower():
            continue
        matches.append(entry)
    if not matches:
        # No model match. Try the raw Tuya batch code — EXACT equality, never a
        # substring: these codes share long prefixes (`_TZE204_…` / `_TZE284_…`)
        # and a loose match would hand one product's entry to another's batch.
        return _entry_by_zigbee_manufacturer(zigbee_manufacturer)
    if len(matches) == 1:
        # Single model match — accept regardless of the reported vendor, so a
        # device Z2M labels "Xiaomi"/"LUMI" still resolves to its catalog entry.
        return matches[0]
    # LONGEST match wins before anything else. Catalog model ids nest: the
    # Skydance `WZ5_rgb` is a substring of `WZ5_rgb_1`, `WZ5_rgbw` AND
    # `WZ5_rgbcct`, so taking the first hit resolved every RGBW/RGBCCT
    # controller as plain RGB — and since the WZ5's model id IS its output
    # mode, that silently reported the wrong mode for three of the five.
    # `sensor._match_model_image` / `_match_model_display_name` have already
    # shipped this exact bug once between them; keep every model matcher in
    # this repo on longest-match.
    longest = max(
        len(str((e.get("discovery") or {}).get("zigbee", {}).get("model") or ""))
        for e in matches
    )
    matches = [
        e for e in matches
        if len(str((e.get("discovery") or {}).get("zigbee", {}).get("model") or "")) == longest
    ]
    if len(matches) == 1:
        return matches[0]
    # Model id collides across vendors: disambiguate on the manufacturer pin.
    for entry in matches:
        zb = (entry.get("discovery") or {}).get("zigbee") or {}
        cat_manuf = str(zb.get("manufacturer") or "").strip().lower()
        if cat_manuf and manuf_l and cat_manuf in manuf_l:
            return entry
    # Ambiguous with no manufacturer match — fall back to the first entry.
    return matches[0]


def _entry_by_zigbee_manufacturer(zigbee_manufacturer: str) -> dict[str, Any] | None:
    """The catalog entry claiming this raw Zigbee `manufacturerName`, or None.

    The second match key, for Tuya products whose model id is the useless
    generic `TS0601`. Exact, case-insensitive equality on a code listed in
    `discovery.zigbee.manufacturer_names`.
    """
    code = str(zigbee_manufacturer or "").strip().lower()
    if not code:
        return None
    try:
        from ..device_catalog import PHYSICAL_DEVICE_CATALOG
    except Exception:  # noqa: BLE001
        return None  # pragma: no cover
    for entry in PHYSICAL_DEVICE_CATALOG:
        zb = (entry.get("discovery") or {}).get("zigbee") or {}
        names = zb.get("manufacturer_names") or ()
        if any(str(n or "").strip().lower() == code for n in names):
            return entry
    return None


def match_known_zigbee_model(
    model: str, manufacturer: str, zigbee_manufacturer: str = "",
) -> str | None:
    """The catalog model string for a supported Z2M device, or None."""
    entry = _matching_zigbee_catalog_entry(model, manufacturer, zigbee_manufacturer)
    if entry is None:
        return None
    zb = (entry.get("discovery") or {}).get("zigbee") or {}
    return str(zb.get("model") or "").strip() or None


def zigbee_name_is_bare_ieee(name: Any, ieee: Any) -> bool:
    """True when a Zigbee device's name is only the IEEE Z2M gave it on joining."""
    bare = str(ieee or "").replace(":", "").replace("-", "").strip().lower()
    return bool(bare) and str(name or "").strip().lower().removeprefix("0x") == bare


def friendly_zigbee_model_name(
    model: str, manufacturer: str = "", zigbee_manufacturer: str = "",
) -> str | None:
    """The catalog's friendly product name for a supported Z2M model, or None.

    Used to label a freshly-paired Zigbee device in the Discovery picker: Z2M
    gives a newly-joined device a friendly_name of its bare IEEE (a meaningless
    hex string like ``0x00158d…``), so the picker can show the catalog product
    name in brackets after it — ``0x00158d… (Aqara Vibration Sensor)`` — so the
    customer can tell what the device is before adding it. Matches loosely, like
    `match_known_zigbee_model`.
    """
    entry = _matching_zigbee_catalog_entry(model, manufacturer, zigbee_manufacturer)
    if entry is None:
        return None
    return str(entry.get("name") or "").strip() or None


# Which "Light Type" a Z2M lighting model implies — keyed on the model name
# Zigbee2MQTT REPORTS for the device, never on the catalog's own model string.
#
# A 5-in-1 controller like the Skydance WZ5 has its output mode chosen physically
# (the match button). That mode is part of the device's Zigbee IDENTITY: it changes
# the Tuya `manufacturerName`, so Z2M resolves a different definition with a
# different model name. Upstream: "This device can be reconfigured to one of 5
# modes, each is discovered as different device and have different capabilities"
# (Koenkk/zigbee2mqtt#6867). So the model name TELLS US the mode.
#
# The advertised CAPABILITY does not. Tuya re-flashes firmware between batches and
# the resulting definitions disagree: `WZ5_dim_1` and `WZ5_dim_2` are both
# "5 in 1 LED controller (DIM mode)", but dim_1 exposes brightness alone while
# dim_2 also exposes color_temp AND color_hs. Reading `supported_color_modes` would
# therefore put a colour mixer on a dim-mode controller that cannot drive one —
# which is exactly the bug the manual "Light Type" select was added to paper over.
def _wz5_model_map(key: str) -> dict[str, str]:
    """model -> `key` ("behaviour" or "led"), derived from `WZ5_MODE_SPECS`.

    Derived rather than restated so a mode's models, its render behaviour and
    its RUN LED colour cannot drift apart.
    """
    from ..const import WZ5_MODE_SPECS

    return {
        model: spec[key]
        for spec in WZ5_MODE_SPECS
        for model in spec["models"]
    }


def _wz5_manufacturer_map(key: str) -> dict[str, str]:
    """raw manufacturerName -> `key`, from `WZ5_MODE_SPECS`.

    The WZ5's mode IS its Zigbee identity: pressing the match button rewrites
    the Tuya `manufacturerName`, which is what makes Z2M resolve a different
    definition. So when Z2M has no definition for a batch (its model id falls
    back to a generic `TS0601`), the raw code still names the mode exactly.
    Populating a mode's `manufacturer_names` therefore restores detection, the
    RUN LED colour AND the product image for that batch, with no code change.
    """
    from ..const import WZ5_MODE_SPECS

    out = {
        str(code).strip().lower(): spec[key]
        for spec in WZ5_MODE_SPECS
        for code in spec.get("manufacturer_names", ())
    }
    # The catalog's `discovery.zigbee.manufacturer_names` records the SAME fact
    # (a batch code seen on a real device) where the picker's matcher reads it
    # for the product image. Fold it in, keyed through the entry's Zigbee model
    # (`WZ5_cct` -> the CCT spec), so a batch is a one-line CATALOG edit and the
    # image, the Light Type, the RUN LED colour and the unmapped-model alert all
    # agree. On 2026-09-07 the two lists disagreed: the catalog knew
    # `_TZB210_nfzrlz29`, this table did not, and six controllers showed the
    # right photo while still being reported as unrecognised. A code listed in
    # both places takes the spec's answer.
    try:
        from ..device_catalog import PHYSICAL_DEVICE_CATALOG
    except Exception:  # noqa: BLE001 - the spec table alone is still an answer
        return out
    by_model = {
        str(model).strip().lower(): spec[key]
        for spec in WZ5_MODE_SPECS
        for model in spec["models"]
    }
    for entry in PHYSICAL_DEVICE_CATALOG:
        zb = (entry.get("discovery") or {}).get("zigbee") or {}
        value = by_model.get(str(zb.get("model") or "").strip().lower())
        if value is None:
            continue
        for code in zb.get("manufacturer_names") or ():
            code_l = str(code or "").strip().lower()
            if code_l:
                out.setdefault(code_l, value)
    return out


def _light_mode_by_model() -> dict[str, str]:
    return {
        **_wz5_model_map("behaviour"),
        # Gledopto addressable/pixel controller, always full colour. Not a WZ5,
        # so it has no output mode and no RUN LED to report.
        "gl-spi-206p": "Colour",
        # Gledopto Pro 5-in-1 (GL-C-301P and its siblings). Unlike the WZ5 it
        # reports each output mode as a real model id, over standard clusters:
        # Z2M lists the 5-in-1s as white labels of these five, with no
        # fingerprint of their own, so a 301P in RGB+CCT mode reports GL-C-008P.
        "gl-c-008p": "Colour",
        "gl-c-007p": "Colour",
        "gl-c-003p": "Colour",
        "gl-c-006p": "Tunable White",
        "gl-c-009p": "White",
    }


def light_mode_for_model(model: str, zigbee_manufacturer: str = "") -> str | None:
    """The "Light Type" a Z2M lighting model implies, or None if unrecognised.

    Returns one of `const.LIGHT_TYPE_OPTIONS` so nothing downstream needs a new
    vocabulary. Matched on the LONGEST key contained in the reported model, for
    the same reason `sensor._match_model_image` does: Z2M reports the model
    loosely (sometimes bare, sometimes inside a description), and a shorter key
    (`wz5_rgb`) is a prefix of a longer one (`wz5_rgbw`, `wz5_rgbcct`) — taking
    the first substring hit would resolve every RGBW/RGBCCT controller as plain
    `wz5_rgb`. It happens to agree here (both are "Colour"), but the two matchers
    in `sensor.py` disagreeing on exactly this is a bug this repo has already
    shipped once, so do not leave it to luck.

    **None means "we do not know" and must never be defaulted.** It is the signal
    that surfaces the manual override on the device row and logs the unmapped
    model, so a new Tuya batch announces itself instead of rendering wrongly.

    `zigbee_manufacturer` is the raw Tuya batch code, tried only when the model
    resolves nothing — see `_wz5_manufacturer_map`.
    """
    return (
        _longest_model_match(model, _light_mode_by_model())
        or _exact_code_match(zigbee_manufacturer, _wz5_manufacturer_map("behaviour"))
    )


def _exact_code_match(code: str, table: dict[str, str]) -> str | None:
    """`table` value for this raw manufacturerName. EXACT, case-insensitive.

    Never a substring match: Tuya codes share long prefixes (`_TZE204_…`,
    `_TZE284_…`), so a loose match would hand one mode's answer to another's
    batch — the same class of bug longest-match exists to prevent on models.
    """
    key = str(code or "").strip().lower()
    return table.get(key) if key else None


def _longest_model_match(model: str, table: dict[str, str]) -> str | None:
    """`table` value for the LONGEST key contained in the reported model.

    Longest wins for the same reason `sensor._match_model_image` does: Z2M
    reports the model loosely (sometimes bare, sometimes inside a description),
    and a shorter key (`wz5_rgb`) is a prefix of longer ones (`wz5_rgbw`,
    `wz5_rgbcct`). Taking the first substring hit resolves every RGBW/RGBCCT
    controller as plain RGB. That is harmless for the render behaviour (all
    three are "Colour") but WRONG for the RUN LED colour, which differs per
    mode, so this must not be left to luck.
    """
    model_l = str(model or "").strip().lower()
    if not model_l:
        return None
    best: str | None = None
    for key in table:
        if key in model_l and (best is None or len(key) > len(best)):
            best = key
    return table[best] if best else None


def is_wz5_model(model: str, zigbee_manufacturer: str = "") -> bool:
    """True for any Skydance WZ5 variant, whichever mode it is switched to.

    The raw Tuya code is checked too, so a batch whose definition Z2M cannot
    resolve (model id back to a generic `TS0601`) is still offered its five
    hardware modes rather than the three generic behaviours — provided that code
    has been added to the mode's `manufacturer_names`.
    """
    if "wz5_" in str(model or "").strip().lower():
        return True
    return _exact_code_match(zigbee_manufacturer, _wz5_manufacturer_map("led")) is not None


def led_colour_for_model(model: str, zigbee_manufacturer: str = "") -> str | None:
    """The RUN LED colour a WZ5's model implies, or None.

    None for a non-WZ5 light (a bulb has no match button and no indicator) and
    for an unmapped WZ5 batch. Distinct from `light_mode_for_model`, which
    collapses the five modes onto three render behaviours: RGB, RGBW and
    RGB+CCT all render the same but light red, green and blue respectively.
    """
    return (
        _longest_model_match(model, _wz5_model_map("led"))
        or _exact_code_match(zigbee_manufacturer, _wz5_manufacturer_map("led"))
    )


def resolve_wz5_mode_label(
    option: str | None, model: str, zigbee_manufacturer: str = "",
) -> str | None:
    """Which of the five WZ5 modes is in force, as its customer-facing label.

    Same precedence as everything else here: an explicit pick wins, `Auto`
    follows detection. None for a non-WZ5 light, an unmapped batch, or a pick
    stored by an earlier build (one of the three plain behaviours, which names
    no single mode).
    """
    from ..const import LIGHT_TYPE_AUTO, WZ5_MODE_LED_COLOURS

    opt = str(option or "").strip()
    if opt and opt != LIGHT_TYPE_AUTO:
        return opt if opt in WZ5_MODE_LED_COLOURS else None
    colour = led_colour_for_model(model, zigbee_manufacturer)
    if colour is None:
        return None
    return next(
        (
            label
            for label, led in WZ5_MODE_LED_COLOURS.items()
            if led == colour
        ),
        None,
    )


def resolve_led_colour(
    option: str | None, model: str, zigbee_manufacturer: str = "",
) -> str | None:
    """The RUN LED colour to show for a controller, or None if unknown.

    Mirrors `resolve_light_type` exactly, so the indicator and the controls can
    never disagree about which mode is in force: an explicit pick wins, `Auto`
    (or nothing picked) follows detection.

    A pick stored by an earlier build is one of the three plain behaviours,
    which names no single mode ("Colour" is red, green OR blue), so it resolves
    to None rather than guessing a colour.
    """
    from ..const import LIGHT_TYPE_AUTO, WZ5_MODE_LED_COLOURS

    opt = str(option or "").strip()
    if opt and opt != LIGHT_TYPE_AUTO and opt in WZ5_MODE_LED_COLOURS:
        return WZ5_MODE_LED_COLOURS[opt]
    if opt and opt != LIGHT_TYPE_AUTO and opt not in WZ5_MODE_LED_COLOURS:
        return None
    return led_colour_for_model(model, zigbee_manufacturer)


def light_type_options_for_model(
    model: str, zigbee_manufacturer: str = "",
) -> list[str]:
    """The "Light Type" options to offer for a lighting controller.

    A WZ5 is offered its OWN five modes, each named with the RUN LED colour the
    unit lights, because the customer can read the current mode straight off the
    hardware and match it. Any other Z2M light gets the three generic render
    behaviours — a bulb has no match button and no indicator to name.

    An UNIDENTIFIED Tuya light gets the five modes too, and this is the whole
    point of the batch-code work: detection has failed, so the customer is the
    only one who can say what the controller is, and the five labels are the
    only options that let them give the RUN LED colour as well as the render
    behaviour. Picking "CCT (yellow LED)" resolves to Tunable White AND yellow;
    picking the plain "Tunable White" resolves the behaviour and leaves the LED
    unknown for ever, because one behaviour names no single mode. Nothing is
    lost by offering the five: they cover all three behaviours between them.

    Scoped to `_TZ…` because Tuya is the family whose codes churn. A bulb that
    identifies itself honestly keeps the three plain behaviours, which is the
    right vocabulary for a device with no match button.

    `LIGHT_TYPE_AUTO` leads either list and is the default: it follows what Z2M
    reports, so the common case needs no choice at all.
    """
    from ..const import LIGHT_TYPE_AUTO, LIGHT_TYPE_OPTIONS, WZ5_MODE_LABELS

    if is_wz5_model(model, zigbee_manufacturer):
        return [LIGHT_TYPE_AUTO, *WZ5_MODE_LABELS]
    if unidentified_tuya_light(model, zigbee_manufacturer):
        return [LIGHT_TYPE_AUTO, *WZ5_MODE_LABELS]
    return [LIGHT_TYPE_AUTO, *LIGHT_TYPE_OPTIONS]


def unidentified_tuya_light(model: str, zigbee_manufacturer: str) -> bool:
    """True for a Tuya light whose batch neither the model nor the code names.

    The state the customer has to resolve by hand: Z2M exposes a working light,
    but its batch code is in nobody's table yet, so we cannot tell a CCT
    controller from an RGB one. Never true once the code is mapped - adding it
    to a mode's `manufacturer_names` moves the device back onto detection.
    """
    if not str(zigbee_manufacturer or "").strip().lower().startswith("_tz"):
        return False
    return light_mode_for_model(model, zigbee_manufacturer) is None


def migrate_light_type_option(
    option: str, model: str, zigbee_manufacturer: str = "",
) -> str | None:
    """Translate a stored "Light Type" value into an option this model offers.

    Before detection existed the select offered only the three plain behaviours,
    so an upgraded box restores a value a WZ5's new list does not contain, and
    dropping it would silently hand every hand-narrowed light its colour
    controls back. Each of the two narrowing behaviours maps to exactly ONE WZ5
    mode, so the translation is unambiguous.

    Returns None when the value carries no customer intent worth keeping: an
    unrecognised label, or the old "Colour" default (which was what an untouched
    select already read, so restoring it as an override would pin every light to
    full controls and defeat detection entirely).
    """
    from ..const import LIGHT_TYPE_DEFAULT, WZ5_MODE_LABELS

    offered = light_type_options_for_model(model, zigbee_manufacturer)
    if option in offered:
        return option
    if option == LIGHT_TYPE_DEFAULT:
        return None
    matches = [
        label for label, behaviour in WZ5_MODE_LABELS.items()
        if behaviour == option and label in offered
    ]
    return matches[0] if len(matches) == 1 else None


def resolve_light_type(
    option: str | None, model: str, zigbee_manufacturer: str = "",
) -> str | None:
    """Resolve a customer's "Light Type" pick to a card render behaviour.

    Returns one of `const.LIGHT_TYPE_OPTIONS`, or None when it cannot be
    resolved — i.e. `Auto` (or nothing picked) on a model we have no mapping
    for. None keeps the card on the entity's advertised capability, which is the
    only honest answer for an unrecognised device, and is what the unmapped-model
    warning tells us to come and map.
    """
    from ..const import LIGHT_TYPE_AUTO, LIGHT_TYPE_OPTIONS, WZ5_MODE_LABELS

    opt = str(option or "").strip()
    if opt and opt != LIGHT_TYPE_AUTO:
        # An explicit pick wins over detection: the customer can see the strip
        # and nothing on the network can.
        #
        # A recognised label is honoured whatever `model` currently says,
        # because the label's meaning is self-contained ("RGBW (green LED)" is
        # a colour output wherever it came from) and because a pick made on an
        # earlier build stored one of the three plain behaviours, and dropping
        # those would silently un-narrow every light that had been set by hand.
        if opt in WZ5_MODE_LABELS:
            return WZ5_MODE_LABELS[opt]
        if opt in LIGHT_TYPE_OPTIONS:
            return opt
        # An unrecognisable label (a build that renamed its options) is not
        # something to guess at, so fall through to detection.
    return light_mode_for_model(model, zigbee_manufacturer)


# Model-name stems of the lighting controllers Sensa supports. Used ONLY to
# recognise a device as "one of ours" when its exact model is unmapped, i.e. a
# firmware batch Tuya has shipped since this build. Deliberately not used for
# matching or rendering: a stem tells us the family, never the mode.
_SENSA_LIGHT_MODEL_FAMILIES: tuple[str, ...] = ("wz5_", "gl-spi-")


def unmapped_sensa_light_models(hass: Any) -> list[tuple[str, str]]:
    """`(device name, model)` for each Sensa lighting controller we can't map.

    Catches the case Tuya creates for us: a NEW firmware batch reports a model
    we have never seen (`WZ5_dim_2` and `WZ5_dim_1` are already two batches of
    the same product in the same mode, advertising different capabilities), so
    neither the catalog nor the light-mode map knows it.

    TWO scopes, because the model-name one alone misses the very case it was
    written for. A batch Z2M cannot fully resolve reports a GENERIC model id
    (`TS0601`, `TS0505B`) with no `wz5_` in it anywhere, so a customer's WZ5s go
    unreported while rendering the wrong controls. The second scope catches
    those on the raw Tuya manufacturerName (`_TZ…`) instead, which every such
    device carries. Both stay narrow deliberately: an Ikea or Philips bulb
    matches neither, so a customer's own third-party lighting never nags.
    """
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
    except Exception:  # pragma: no cover - registries always present at runtime  # noqa: BLE001
        return []

    with_light = {
        ent.device_id
        for ent in ent_reg.entities.values()
        if ent.device_id and ent.entity_id.startswith("light.")
    }
    out: list[tuple[str, str]] = []
    for dev_id in sorted(with_light):
        ha_dev = dev_reg.devices.get(dev_id)
        if ha_dev is None or not zigbee2mqtt_device_ieee(ha_dev):
            continue
        model = zigbee2mqtt_reported_model(hass, dev_id)
        code = zigbee2mqtt_reported_manufacturer(hass, dev_id)
        model_l = model.lower()
        ours = any(fam in model_l for fam in _SENSA_LIGHT_MODEL_FAMILIES)
        tuya = code.lower().startswith("_tz")
        if not ours and not tuya:
            continue
        if light_mode_for_model(model, code) is not None:
            continue
        name = ha_dev.name_by_user or ha_dev.name or "Lighting controller"
        # Report the raw code alongside the model: on a generic `TS0601` the
        # model names nothing, and the code is the only thing that identifies
        # the batch well enough to map it in the next build.
        out.append((name, f"{model} {code}".strip() if code else model))
    return out


def zigbee2mqtt_ha_device_id(hass: Any, device: dict[str, Any]) -> str | None:
    """Resolve the HA device id for a registered Zigbee2MQTT device, or None.

    The HA device is created and owned by HA's shared `mqtt` integration (Z2M
    published it) — there's no Sensa role map. Resolve it by its Z2M identifier
    (`("mqtt","zigbee2mqtt_<ieee>")`) rather than a MAC connection: Z2M keys its
    HA device on the IEEE identifier and the connection set is typically empty.

    Lives here rather than in the dashboard builder because the Discovery sensor
    needs the same Sensa-device -> HA-device bridge to resolve a per-device
    select, and this repo's recurring bug is one rule written in two places.
    """
    ieee = canon_ieee(
        device.get("linked_mac_address")
        or device.get("mac_address")
        or device.get("address")
        or device.get("mac")
    )
    if not ieee:
        return None
    try:
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(hass)
    except Exception:  # noqa: BLE001
        return None

    for ha_dev in dev_reg.devices.values():
        if zigbee2mqtt_device_ieee(ha_dev) == ieee:
            return ha_dev.id
    return None


def zigbee2mqtt_reported_model(hass: Any, ha_device_id: str) -> str:
    """The model string Z2M reports for an HA device, or "" if unresolvable.

    Concatenates the model CODE and the description exactly as the discovery
    row builder does (`model_match`), so every consumer matches the catalog and
    the light-mode map against the same string.
    """
    if not ha_device_id:
        return ""
    try:
        from homeassistant.helpers import device_registry as dr

        ha_dev = dr.async_get(hass).devices.get(ha_device_id)
    except Exception:  # noqa: BLE001
        return ""
    if ha_dev is None:
        return ""
    return f"{getattr(ha_dev, 'model_id', None) or ''} {ha_dev.model or ''}".strip()


def zigbee2mqtt_reported_manufacturer(hass: Any, ha_device_id: str) -> str:
    """The RAW Zigbee `manufacturerName` for an HA device, or "" if unknown.

    Comes from the bridge cache, never the HA registry: Z2M sets the HA
    device's `manufacturer` to the definition VENDOR ("TuYa", "Gledopto"), which
    is the resolved product's brand and identifies no batch. The raw
    `_TZE204_…` code exists only in `bridge/devices`.

    "" whenever the bridge cache is cold (no MQTT, Z2M down, first seconds after
    a restart), which every caller must treat as "could not ask" — the code is
    only ever a FALLBACK key, so a cold cache degrades to the model-only
    behaviour rather than to a wrong answer.
    """
    if not ha_device_id:
        return ""
    try:
        from homeassistant.helpers import device_registry as dr

        ha_dev = dr.async_get(hass).devices.get(ha_device_id)
    except Exception:  # noqa: BLE001
        return ""
    if ha_dev is None:
        return ""
    ieee = zigbee2mqtt_device_ieee(ha_dev)
    if not ieee:
        return ""
    dev = bridge_devices(hass).get(ieee) or {}
    return str(dev.get("manufacturer") or "").strip()


def canon_ieee(identifier: str) -> str:
    """Normalise a Z2M IEEE / device identifier to a bare uppercase hex token.

    Strips the ``zigbee2mqtt_`` prefix and the ``0x`` so the result matches the
    form the discovery sensor's `_canonical_mac` produces (uppercase, no
    separators) — letting an IEEE be compared MAC-style throughout.
    """
    value = str(identifier or "")
    value = value.removeprefix(_Z2M_IDENTIFIER_PREFIX)
    value = value.upper()
    value = value.removeprefix("0X")
    return value


def zigbee2mqtt_device_ieee(ha_dev: Any) -> str | None:
    """The IEEE for a Z2M-owned HA device, or None when it isn't one.

    Returns None for non-Z2M mqtt devices (no ``zigbee2mqtt_`` identifier) and
    for the Z2M *Bridge* device (identifier tail begins ``bridge``).
    """
    for domain, value in (getattr(ha_dev, "identifiers", None) or set()):
        if domain != "mqtt":
            continue
        text = str(value)
        if not text.startswith(_Z2M_IDENTIFIER_PREFIX):
            continue
        tail = text[len(_Z2M_IDENTIFIER_PREFIX):]
        if tail.lower().startswith("bridge"):
            return None  # the Z2M software bridge, not a customer device
        return canon_ieee(text)
    return None


def _mqtt_entry_ids(hass: Any) -> set[str]:
    try:
        return {ce.entry_id for ce in hass.config_entries.async_entries("mqtt")}
    except Exception:  # noqa: BLE001
        return set()


def zigbee2mqtt_present(hass: Any) -> bool:
    """True when a Zigbee2MQTT bridge has been adopted by HA's mqtt integration.

    Detects the Z2M *Bridge* device specifically (identifier tail begins
    ``bridge``) rather than any Z2M end-device, because the bridge exists the
    moment Z2M is running and talking to HA — before any Zigbee device is
    paired. Used to gate the Discovery picker's "Pair Zigbee" control: there's
    no point offering pairing when there's no bridge to open permit-join on.
    Returns False gracefully when mqtt / the registries aren't available.
    """
    mqtt_entries = _mqtt_entry_ids(hass)
    if not mqtt_entries:
        return False
    try:
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(hass)
    except Exception:  # noqa: BLE001
        return False
    for ha_dev in dev_reg.devices.values():
        if not mqtt_entries.intersection(ha_dev.config_entries or ()):
            continue
        for domain, value in (getattr(ha_dev, "identifiers", None) or set()):
            if domain != "mqtt":
                continue
            text = str(value)
            if not text.startswith(_Z2M_IDENTIFIER_PREFIX):
                continue
            tail = text[len(_Z2M_IDENTIFIER_PREFIX):]
            if tail.lower().startswith("bridge"):
                return True
    # Fallback: the bridge device's identifier shape can vary by Z2M version, but
    # the bridge ALWAYS exposes a `permit_join` control (the very thing the Pair
    # button drives). If HA has that entity under the mqtt integration, Z2M is
    # present — independent of how the bridge device is identified.
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        for ent in ent_reg.entities.values():
            if ent.config_entry_id not in mqtt_entries:
                continue
            hay = f"{ent.entity_id} {ent.unique_id or ''}".lower()
            if "permit_join" in hay:
                return True
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def zigbee_coordinator_domains() -> list[str]:
    """Native HA domains for every Zigbee coordinator Sensa supports.

    Catalog-driven (DATA, not code): any `device_catalog.json` entry whose
    `service_class` is `zigbee_coordinator` contributes its native HA domain
    (`accept_native`, falling back to `discovery.native.domain`). Today that's
    just the SLZB-06U (`smlight`), but a second coordinator model is added by
    dropping in another catalog entry — no change here. Never hardcode a single
    coordinator domain anywhere; resolve through this list.
    """
    try:
        from ..device_catalog import PHYSICAL_DEVICE_CATALOG
    except Exception:  # noqa: BLE001
        return []
    domains: list[str] = []
    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("service_class") != "zigbee_coordinator":
            continue
        domain = entry.get("accept_native") or (
            (entry.get("discovery") or {}).get("native") or {}
        ).get("domain")
        if domain and domain not in domains:
            domains.append(str(domain))
    return domains


def zigbee_coordinator_integrations() -> list[str]:
    """Backend `integration` slugs for every Zigbee coordinator in the catalog.

    The Discovery picker tags rows by their `integration` field. A coordinator
    WITHOUT a native HA integration (the SONOFF Dongle-M) has no domain, so it
    can't be matched via `zigbee_coordinator_domains`; its discovery row instead
    carries its synthetic catalog `integration` (`sonoff_dongle_m`). The picker's
    COORDINATOR filter rule must list every coordinator integration or an
    unmatched row falls to the debug-gated *Others* group (hidden) — the trap a
    Dongle-M hit on first install. Catalog-driven, so a new coordinator model is
    picker-visible from its catalog entry alone, no code change.
    """
    try:
        from ..device_catalog import PHYSICAL_DEVICE_CATALOG
    except Exception:  # noqa: BLE001
        return []
    integrations: list[str] = []
    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("service_class") != "zigbee_coordinator":
            continue
        integ = entry.get("integration")
        if integ and integ not in integrations:
            integrations.append(str(integ))
    return integrations


def zigbee_coordinator_present(hass: Any) -> bool:
    """True when ANY supported Zigbee coordinator is adopted in HA.

    The coordinator's radio is what makes Zigbee work at all — Z2M only talks to
    HA *through* it. The Z2M *Bridge* device (what `zigbee2mqtt_present` detects)
    is a software bridge that lingers in HA's mqtt registry even after the
    physical coordinator is removed, so bridge-presence alone is not enough to
    decide whether pairing — or adding any Z2M device — is possible. Removing the
    coordinator from Sensa Discovery deletes its native config entry, so this
    flips to False and Z2M devices stop being Discovered / pairing is hidden.

    Broad by design: it checks the native config entry of every coordinator in
    the catalog (see `zigbee_coordinator_domains`), not one hardcoded model, so
    additional coordinator models are picked up via their catalog entry alone.
    Returns False gracefully when config entries aren't available.

    Backend fallback: a coordinator WITHOUT a native HA integration (the SONOFF
    Dongle-M — a pure TCP radio with no `smlight`-style entry) has no domain to
    match, so the config-entry check above can't see it. For that case we also
    scan the cached Sensa backend device registry
    (`coordinator.data["devices"]`) for a `zigbee_coordinator` row. Without this
    the Pair Zigbee button would stay hidden even with a Dongle-M adopted.

    Those two are `zigbee_coordinator_registered` — the DURABLE answer, true
    whether or not Zigbee is currently working. This function adds a third,
    LIVENESS-derived signal on top, and the two must not be confused: see the
    warning on `zigbee_coordinator_registered`.
    """
    if zigbee_coordinator_registered(hass):
        return True
    # Z2M running with a connected radio (its bridge is ONLINE) means a
    # coordinator IS present and pairing will work — even when that coordinator
    # has no native HA integration AND was never registered as a Sensa device
    # (a SONOFF Dongle-M configured straight in Z2M).
    try:
        if _zigbee_bridge_online(hass):
            return True
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def zigbee_coordinator_registered(hass: Any) -> bool:
    """True when a Zigbee coordinator is ADOPTED — a native config entry, or a
    `zigbee_coordinator` row in the cached Sensa backend device registry.

    The durable half of `zigbee_coordinator_present`, and separate from it on
    purpose. `zigbee_coordinator_present` also answers True for a radio it can
    only see THROUGH a running Z2M, which makes it useless — worse, actively
    self-defeating — as the "does this van have Zigbee at all" gate on anything
    that REPORTS Zigbee being broken: the moment Z2M dies that gate goes False
    and the report is suppressed exactly when it is due. That circle is why the
    bridge-down alarm reads this and not that.
    """
    try:
        for domain in zigbee_coordinator_domains():
            if hass.config_entries.async_entries(domain):
                return True
    except Exception:  # noqa: BLE001, S110
        pass
    # Backend-registered coordinator with no native HA entry (e.g. Dongle-M).
    # Resolved through `classify()`, the one function that knows how a backend
    # row becomes a service class, rather than a hand-rolled scan of the five
    # fields it might hide in. The scan that used to live here looked right and
    # matched nothing: the Discovery picker drops the catalog `type`, so an
    # added Dongle-M reaches the backend with `role: "unknown"` and every field
    # the scan read said "unknown". `classify()` falls through to the
    # integration slug, which is the only signal such a row carries.
    try:
        from ..const import DOMAIN
        from .service_classes import classify

        for runtime in (hass.data.get(DOMAIN, {}) or {}).values():
            if not isinstance(runtime, dict):
                continue
            coordinator = runtime.get("coordinator")
            devices = (getattr(coordinator, "data", None) or {}).get("devices", [])
            for device in devices or []:
                if classify(device) == "zigbee_coordinator":
                    return True
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def _zigbee_bridge_online(hass: Any) -> bool:
    """True when Zigbee2MQTT is actually RUNNING and talking to its radio.

    This is the precondition for pairing that doesn't depend on the coordinator
    having a native HA integration or a Sensa backend row, and it is what the
    "Zigbee has stopped" alarm reads. `zigbee2mqtt_present` only checks the
    bridge DEVICE exists, which it does long after Z2M stops.

    **Z2M's own connection_state binary_sensor is the answer when it exists.**
    Z2M publishes it against `zigbee2mqtt/bridge/state` with `payload_on:
    online` / `payload_off: offline`, and — unlike its other bridge entities —
    with NO availability block. So when Z2M dies the retained payload flips to
    `offline` and the entity sits there perfectly AVAILABLE reading `off`.

    That is what broke the original "any entity that isn't unavailable" test,
    and it broke it in the direction that hides the fault: Z2M was dead for two
    days on the dev box, this returned True throughout, the Pair button stayed
    on offer, and pressing it published permit_join into a broker with nobody
    listening. An available entity is not a live service; that entity's VALUE
    is the liveness signal, and reading anything else here is reading the wrong
    thing.

    The old any-live-entity heuristic survives as the fallback for a bridge
    with no connection_state entity (an older Z2M, or one whose discovery has
    not fully landed), where it is the only signal there is.
    """
    mqtt_entries = _mqtt_entry_ids(hass)
    if not mqtt_entries:
        return False
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return False
    for ha_dev in dev_reg.devices.values():
        if not mqtt_entries.intersection(ha_dev.config_entries or ()):
            continue
        is_bridge = False
        for domain, value in (getattr(ha_dev, "identifiers", None) or set()):
            if domain != "mqtt":
                continue
            text = str(value)
            if not text.startswith(_Z2M_IDENTIFIER_PREFIX):
                continue
            if text[len(_Z2M_IDENTIFIER_PREFIX):].lower().startswith("bridge"):
                is_bridge = True
                break
        if not is_bridge:
            continue
        entities = er.async_entries_for_device(
            ent_reg, ha_dev.id, include_disabled_entities=False
        )
        # The authoritative answer first, if Z2M published one.
        for ent in entities:
            dc = (
                getattr(ent, "device_class", None)
                or getattr(ent, "original_device_class", None)
                or ""
            )
            if dc != _Z2M_BRIDGE_CONNECTION_DEVICE_CLASS:
                continue
            st = hass.states.get(ent.entity_id)
            if st is None or st.state in ("unavailable", "unknown"):
                # Could not ask (HA restarting, discovery not landed). Fall
                # through to the heuristic rather than calling Zigbee dead.
                break
            return st.state == "on"
        for ent in entities:
            st = hass.states.get(ent.entity_id)
            if st is not None and st.state not in ("unavailable", "unknown"):
                return True
    return False


def _ha_fully_started(hass: Any) -> bool:
    """True once HA has finished starting.

    `hass.is_running` is NOT this test: it is true during `starting` too, which
    is exactly the window that has to be excluded. Unknowable (a stub, an HA
    without CoreState) reads True rather than False, so a bad read can only
    make the watcher work, never silence it.
    """
    try:
        from homeassistant.core import CoreState
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return True
    state = getattr(hass, "state", None)
    if state is None:
        return True
    return state is CoreState.running or state == "running"


def evaluate_zigbee_bridge_health(hass: Any, *, now: float | None = None) -> bool:
    """Raise or clear the "Zigbee has stopped" alarm. Returns True when down.

    Zigbee2MQTT dying is a total, permanent loss of every Zigbee device on the
    van, and until now it was SILENT: the devices simply went unavailable, the
    Pair button vanished (it gates on a live bridge), and nothing said why. On
    the dev box that state ran for two days and 912 crash-loops before anyone
    looked. Silence is the defect; this is the fix.

    Three rules the repo has paid for, applied here:

    * **Told no vs could not ask.** The alarm needs a coordinator to be adopted.
      A van with no Zigbee radio is not a van whose Zigbee is broken, so with no
      coordinator we clear and say nothing.
    * **A confident negative, not an unprimed one.** The bridge must be down for
      `Z2M_BRIDGE_DOWN_GRACE_SECONDS` of continuous checks before it counts;
      everything about a cold boot looks like a dead bridge for the first
      minute.
    * **The producer clears it.** Nothing else knows Zigbee came back, so the
      same function that raised it cancels it, or the tick re-sounds it for
      ever.

    Re-raised on every tick rather than only on the edge that broke it: a
    re-assert is cheap (an existing key refreshes in place) and one missed edge
    otherwise reads healthy for ever.
    """
    import time as _time

    from .._alarms import cancel_alarm, raise_alarm
    from ..const import (
        SENSA_LOG_SOURCE_SYSTEM,
        Z2M_BRIDGE_DOWN_ALARM_KEY,
        Z2M_BRIDGE_DOWN_DATA_KEY,
        Z2M_BRIDGE_DOWN_GRACE_SECONDS,
    )

    stamp = _time.monotonic() if now is None else now

    def _clear() -> bool:
        if hass.data.pop(Z2M_BRIDGE_DOWN_DATA_KEY, None) is not None:
            cancel_alarm(hass, Z2M_BRIDGE_DOWN_ALARM_KEY)
        return False

    # Nothing counts until Home Assistant has finished starting. The tick is
    # registered during entry setup, so it fires while HA is still `starting`
    # and integrations are still coming up: mqtt may not have delivered its
    # retained topics, and the bridge legitimately reads down. Accruing that
    # time would make every restart of Sensa a head start towards an alarm.
    # Clearing rather than merely skipping means the window is measured from
    # HA being up, not from whenever the first tick happened to land.
    if not _ha_fully_started(hass):
        return _clear()

    # REGISTERED, not "present": `zigbee_coordinator_present` folds in "Z2M's
    # bridge is online", so gating on it would mean this alarm can only fire
    # while Zigbee still works. See `zigbee_coordinator_registered`.
    coordinator = zigbee_coordinator_registered(hass)
    online = _zigbee_bridge_online(hass) if coordinator else None
    # Both gates, every tick. A watcher whose whole job is to break a silence
    # can itself go quiet in exactly two ways, and without this line the only
    # symptom of either is the same nothing it exists to prevent.
    _LOGGER.debug(
        "Zigbee bridge health: coordinator=%s online=%s down_since=%s",
        coordinator, online, hass.data.get(Z2M_BRIDGE_DOWN_DATA_KEY),
    )
    if not coordinator:
        return _clear()
    if online:
        return _clear()

    down_since = hass.data.get(Z2M_BRIDGE_DOWN_DATA_KEY)
    if down_since is None:
        hass.data[Z2M_BRIDGE_DOWN_DATA_KEY] = stamp
        return False
    if stamp - down_since < Z2M_BRIDGE_DOWN_GRACE_SECONDS:
        return False

    raise_alarm(
        hass,
        Z2M_BRIDGE_DOWN_ALARM_KEY,
        "Zigbee has stopped. Zigbee devices will not respond.",
        title="Zigbee offline",
        # The action, not the diagnosis. Restart is what fixes the common
        # cases (a wedged radio link, a coordinator that was unplugged) and is
        # something the customer can actually do.
        guidance=(
            "Check the Zigbee coordinator is powered and on the network, then "
            "restart the system from Settings."
        ),
        severity="critical",
        # The box talking about itself, so Settings -> Log, not Intelligence.
        log_source=SENSA_LOG_SOURCE_SYSTEM,
    )
    return True


def classify_zigbee2mqtt_device(
    entities: list[tuple[str, str | None]],
) -> str | None:
    """Map a Z2M device's HA entities to a Sensa service class.

    `entities` is a list of ``(domain, device_class)`` for every entity on the
    HA device. Priority order (a multi-function device registers once under its
    dominant class; its secondary entities still resolve on their own surface
    via the shared-HA-device entity expansion — e.g. a motion sensor that also
    reports temperature still reaches the temperature header chips):

        lighting > scene_button > motion_sensor > contact_sensor >
        leak_sensor > environment_sensor
    """
    domains = {dom for dom, _dc in entities}
    if "light" in domains:
        return "lighting"

    # An `event` entity is how Z2M surfaces a button's presses (it needs
    # `homeassistant.experimental_event_entities`, which Sensa seeds — see
    # `_async_seed_z2m_configuration_yaml`). Nothing else Sensa supports
    # publishes one, and a scene button has no other entity but its battery,
    # so the domain alone is the whole test. Ranked above the sensors because
    # a button that also reported temperature would still be a button.
    if "event" in domains:
        return "scene_button"

    binary_classes = {
        dc for dom, dc in entities if dom == "binary_sensor" and dc
    }
    if binary_classes & _MOTION_CLASSES:
        return "motion_sensor"
    if binary_classes & _CONTACT_CLASSES:
        return "contact_sensor"
    if binary_classes & _LEAK_CLASSES:
        return "leak_sensor"
    if binary_classes & _VIBRATION_CLASSES:
        return "vibration_sensor"

    sensor_classes = {dc for dom, dc in entities if dom == "sensor" and dc}
    if sensor_classes & _ENVIRONMENT_SENSOR_CLASSES:
        return "environment_sensor"
    return None


def discover_zigbee2mqtt_devices(hass: Any) -> list[dict[str, Any]]:
    """Discovery-source rows for every Z2M device HA has adopted.

    Reads the HA device/entity registries (no MQTT subscription, no IO): Z2M
    devices already exist as HA devices once Z2M's HA discovery is on. Returns
    `[]` gracefully when the `mqtt` integration / Z2M isn't present. Rows carry
    `source="mqtt_zigbee"`, the IEEE as `mac_address` + `discovery_id`, a stashed
    `service_class` (so `classify()` resolves it directly) and `device_type` set
    to the service-class key (so the backend `role` round-trips post-add).
    """
    mqtt_entries = _mqtt_entry_ids(hass)
    if not mqtt_entries:
        return []
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return []

    from .service_classes import category_of

    # Without a Zigbee coordinator there's no radio to add a device through, so
    # no Z2M device can be Discovered (addable) — even if its bridge + end-device
    # rows still linger in HA's mqtt registry after the coordinator was removed.
    # Treat every device as unsupported in that state: it still appears in the
    # picker's debug-gated *Others* group (identifiable), never in *Discovered*.
    coordinator = zigbee_coordinator_present(hass)
    # Z2M's own view, keyed by the same IEEE form the registry rows use. Empty
    # when the bridge watch has not landed a payload yet, which every read below
    # degrades through cleanly (the raw code becomes "", i.e. "could not ask").
    bridge_by_ieee = bridge_devices(hass)

    rows: list[dict[str, Any]] = []
    for ha_dev in dev_reg.devices.values():
        if not mqtt_entries.intersection(ha_dev.config_entries or ()):
            continue
        ieee = zigbee2mqtt_device_ieee(ha_dev)
        if not ieee:
            continue
        try:
            dev_entities = er.async_entries_for_device(
                ent_reg, ha_dev.id, include_disabled_entities=True,
            )
        except Exception:  # noqa: BLE001
            dev_entities = []
        entities = [
            (e.domain, e.device_class or e.original_device_class)
            for e in dev_entities
        ]
        service_class = classify_zigbee2mqtt_device(entities)
        # Zigbee2MQTT sets the HA device `model` field to the human-readable
        # DESCRIPTION ("Vibration sensor", "Water leak sensor") and puts the
        # bare model CODE ("DJT11LM", "SJCGQ11LM") in the SEPARATE `model_id`
        # field (HA device registry 2024.8+). Our catalog matches on the code,
        # so read both. Without `model_id` the description alone never contains
        # "DJT11LM", so EVERY Aqara device failed the catalog match → was
        # emitted unsupported with no `device_subtype` and no product image
        # (THE bug: "vibration image not displayed"). `model_id` is absent on
        # older HA, where old Z2M put the code in the description — the loose
        # combined match below covers that too.
        model = ha_dev.model or ""
        model_id = str(getattr(ha_dev, "model_id", None) or "")
        # The string we match the catalog against: the code first (exact), then
        # the description (loose substring, for old HA/Z2M shapes).
        model_match = f"{model_id} {model}".strip()
        manufacturer = ha_dev.manufacturer or ""
        name = (
            ha_dev.name_by_user
            or ha_dev.name
            or f"Zigbee {ieee[-4:]}"
        )
        # SUPPORTED = its model is on the catalog allowlist AND it classifies
        # to a class we surface. Such a row carries `service_class` + a
        # `device_subtype` = the catalog model, which the Discovery picker's
        # known-model rule matches → it lands in *Discovered* (addable).
        # Everything else is emitted with NO service_class / device_subtype →
        # the picker's "no rule matched" path drops it into *Others*
        # (debug-gated, not addable) — same as an unknown Bluetooth advert.
        #
        # Resolve the matching catalog entry ONCE and read model / friendly name
        # / image off it. We match on `model_match` (code + description), and
        # stamp the catalog `image` from here: `_enrich_from_catalog` matches
        # `vendor_model` by EXACT normalised equality, so the Z2M model string
        # never matches it and the picker would show no image without this.
        # The raw Tuya batch code from Z2M's bridge cache — the second match
        # key, and the ONLY identity a generic `TS0601` device has.
        zb_manufacturer = str(
            (bridge_by_ieee.get(ieee) or {}).get("manufacturer") or ""
        ).strip()
        cat_entry = _matching_zigbee_catalog_entry(
            model_match, manufacturer, zb_manufacturer,
        )
        cat_zigbee = (cat_entry.get("discovery") or {}).get("zigbee") or {} if cat_entry else {}
        known_model = str(cat_zigbee.get("model") or "").strip() or None
        catalog_image = str((cat_entry or {}).get("image") or "").strip()
        # For a catalog-KNOWN model the catalog's declared `service_class` is
        # AUTHORITATIVE — it's curated data: we know what a DJT11LM is. Entity
        # classification is only a fallback (and the sole signal for UNKNOWN
        # paired models). This matters because the DJT11LM exposes its
        # vibration/tilt as an `action` — an MQTT device TRIGGER, not a
        # `binary_sensor` with `device_class: vibration` — so
        # `classify_zigbee2mqtt_device` returns None for it, and gating
        # `supported` on entity classification left the DJT11LM a bare-IEEE
        # *Others* row with no image, while the SJCGQ11LM (whose leak IS a
        # `binary_sensor` device_class `moisture`) worked. Prefer the catalog
        # class for known models so both behave the same.
        cat_service_class = str((cat_entry or {}).get("service_class") or "").strip() or None
        effective_service_class = (
            cat_service_class if known_model is not None else None
        ) or service_class
        # A device whose model matches no catalog entry but whose HA entities
        # classify cleanly is a NEW BATCH of something we do support: Z2M
        # resolved it well enough to expose working entities, only our allowlist
        # is behind. Hiding it in the debug-gated *Others* group is the failure
        # this exists to end — the customer sees nothing at all, and the first
        # anyone hears of it is a support call. So it is addable, tagged as
        # unrecognised, and its Light Type override is how the customer declares
        # what our catalog cannot yet tell them.
        #
        # Note what this does NOT do: infer the WZ5's output mode from the
        # entity's advertised capability. That inference is banned (Tuya ships
        # dim-mode firmware advertising a colour mixer), so an unrecognised
        # batch resolves to None and the card keeps full controls until the
        # customer picks.
        # NARROW deliberately, on two counts. Only LIGHTING, because that is
        # the one class whose Sensa surface is generic: any Z2M light drives
        # `sensa-light-card` and carries a Light Type override, so an
        # unrecognised one is usable. A sensor class has no such fallback, and
        # letting entity classification alone make sensors addable would delete
        # the curated allowlist outright.
        #
        # And only a TUYA device (`_TZ…`), because Tuya is what makes this a
        # recurring failure rather than a one-off: its batch codes churn and its
        # model ids are the generic `TS0601`. An IKEA or Philips bulb identifies
        # itself honestly and stays in Others, where the allowlist wants it.
        unrecognised_batch = bool(
            coordinator
            and known_model is None
            and service_class == "lighting"
            and zb_manufacturer.lower().startswith("_tz")
        )
        supported = (
            coordinator
            and known_model is not None
            and effective_service_class in _ENABLED_SERVICE_CLASSES
        ) or unrecognised_batch
        # A freshly-paired Zigbee device's name is its bare IEEE (a meaningless
        # hex string), so when we recognise the model it takes the catalog name,
        # "Moes 4 Gang Zigbee Switch (#4F7C)", the same shape reset_device_name
        # restores. A name Z2M already has keeps it, with the product and model
        # code in brackets, "Garage (Aqara Vibration Sensor DJT11LM)", unless it
        # already contains them. Only for supported (catalog-known) devices.
        if supported:
            friendly = str((cat_entry or {}).get("name") or "").strip() or None
            label = " ".join(p for p in (friendly, known_model) if p)
            if friendly and zigbee_name_is_bare_ieee(name, ieee):
                name = f"{friendly} (#{ieee[-4:]})"
            elif label and label.lower() not in name.lower():
                name = f"{name} ({label})"
        rows.append({
            "source": "mqtt_zigbee",
            "discovery_id": f"zigbee2mqtt-{ieee}",
            "name": name,
            "mac_address": ieee,
            "integration": INTEGRATION_ZIGBEE2MQTT,
            # Backend `role` = the service-class key, so a re-read of the
            # registered row re-classifies via classify() step 3. Only set for
            # supported devices — an unsupported one isn't addable. Uses the
            # EFFECTIVE class (catalog-authoritative for known models), so a
            # DJT11LM whose vibration isn't a classifiable entity still gets
            # `vibration_sensor` here.
            "service_class": effective_service_class if supported else None,
            # The RAW entity-based classification, independent of the model
            # allowlist. The picker uses `service_class` (allowlist-gated); the
            # pair-auto-add path uses this so a deliberately-paired device gets
            # its correct role + dashboard surface even when its model isn't on
            # the catalog allowlist. None only for genuinely unclassifiable
            # devices (entity types Sensa can't surface).
            "classified_service_class": service_class,
            "category": (
                (category_of(effective_service_class) or "unknown") if supported
                else "unknown"
            ),
            # No catalog entry means no curated product photo. Deliberately not
            # substituted with a generic one: a wrong picture reads as a
            # different product and has cost us a misdiagnosis before.
            "device_type": effective_service_class if supported else "",
            # The picker's known-model filter rule keys on this — the catalog
            # model for a supported device, blank for an unsupported one. An
            # unrecognised batch carries the sentinel instead, which its own
            # filter rule matches, so it lands in *Discovered* rather than the
            # hidden Others group.
            "device_subtype": (
                ZIGBEE_UNRECOGNISED_SUBTYPE if unrecognised_batch
                else (known_model if supported else "")
            ),
            # True only for the new-batch path. Carried on the row so a surface
            # can say WHY a device has no product image, and so the alert that
            # reports the batch reads the same predicate the picker rendered
            # rather than re-deriving it.
            "unrecognised_batch": bool(unrecognised_batch),
            # The raw Zigbee identity, straight from Z2M's bridge. Stamped on
            # every row (not just unrecognised ones) because it is what a
            # support call needs and what maps the batch in the next build.
            "zigbee_manufacturer": zb_manufacturer,
            # Catalog product image, stamped from the loosely-matched catalog
            # entry (see the cat_entry note above) so the picker shows the
            # device photo. `_pick_image` turns a bare filename into a
            # `/local/sensa/images/<file>` URL.
            "image": catalog_image if supported else "",
            "vendor": manufacturer,
            "manufacturer": manufacturer,
            "model": model,
            # Prefer the bare model CODE (model_id) so the registered-device
            # backfill / subtype catalog lookups in the discovery sensor — which
            # match `vendor_model` against the catalog's `vendor_model` (also the
            # code) — resolve too. Falls back to the description on older HA.
            "vendor_model": model_id or model,
        })
    _LOGGER.debug(
        "zigbee2mqtt discovery: %d mqtt entry(ies), %d Z2M device row(s)",
        len(mqtt_entries), len(rows),
    )
    return rows
