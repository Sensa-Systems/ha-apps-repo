"""Coordinator for the Cerbo GX MQTT vendor module.

MQTT is push, not poll. Values are pushed to entities via
`async_set_updated_data` as messages arrive, so this coordinator has no
update interval. The keepalive that keeps Venus streaming is driven by the
MQTT client's own per-connection loop (`VictronMqttClient._keepalive_loop`),
NOT by this coordinator. `_async_update_data` remains only as the base
class's required refresh hook (it just surfaces the value cache).

Data shape returned to entities:

    {
        "system:0": {"Dc/Battery/Voltage": 13.42, ...},
        "battery:256": {"Dc/0/Voltage": 13.42, "Soc": 87.0, ...},
        "solarcharger:288": {"Yield/Power": 312.5, "State": "bulk", ...},
        "tank:20": {"Level": 64.0, "FluidType": "fresh-water", ...},
        ...
    }

Each top-level key is `"<service>:<instance>"`. Each value is the
value-tail dict for that one device. Entities pick their specific
(service, instance, tail) triple — lookups are constant-time and
entities for one device don't churn when another device's value
changes.

As new `(service, instance)` pairs arrive from the broker (customer
plugs in a new MPPT, powers on the Multi, attaches a tank sender),
the coordinator fires `on_new_device` so the integration can
dynamically add the HA device + its per-template entity set.
`on_new_device` is called exactly once per pair per coordinator life.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .victron_mqtt import VictronMqttClient
from .victron_mqtt_services import (
    EMERGENCY_CHARGE_CONTROL,
    EXTRA_CACHED_TAILS,
    FIRMWARE_ONLINE_CHECK,
    FIRMWARE_ONLINE_INSTALL,
    PLATFORM_FIRMWARE_TAILS,
    PLATFORM_INSTANCE,
    PLATFORM_SERVICE,
    SETTING_AUTO_UPDATE,
    SETTINGS_INSTANCE,
    SETTINGS_SERVICE,
    SETTINGS_TAILS,
    VENUS_SERVICE_TEMPLATES,
)

# Delay between first-sighting of a (service, instance) pair and firing
# `on_new_device`. Gives Venus's keepalive-response burst time to deliver
# the CustomName topic so the HA device card's display name shows
# "House Battery" instead of "Battery #278". 2 s comfortably covers the
# few-hundred-millisecond burst window while still feeling live on
# first add.
NEW_DEVICE_SETTLE_SECS = 2.0

# How long `async_rescan` listens before deciding what the Cerbo currently has
# connected. The MQTT client's keepalive (KEEPALIVE_INTERVAL_SECS, 5 s) makes
# Venus re-broadcast every live topic, so two cycles plus margin sees every
# device that is still there; a device that has been unplugged publishes
# nothing in that window.
RESCAN_SETTLE_SECS = 12.0

# Tails that name a device rather than measure it. Venus keeps these as
# retained topics on the broker, so a fresh subscribe replays them for a
# device that was unplugged long ago; they are NOT evidence the device is
# present. `ProductId` / `ProductName` never reach the cache and need no entry.
_NAMING_TAILS: frozenset[str] = frozenset({"CustomName", "Serial"})

# Venus refreshes `Firmware/Online/AvailableVersion` only when something
# asks it to, so Nomad asks: once shortly after connecting, then daily.
# The first ask is DELAYED because the MQTT client has no VRM portal id
# until the broker's first message lands, and a write before that has no
# addressable topic and silently does nothing.
FIRMWARE_FIRST_CHECK_SECS = 45.0
FIRMWARE_CHECK_INTERVAL = timedelta(hours=24)

_LOGGER = logging.getLogger(__name__)

# No update_interval: this coordinator is pure push. Values reach entities via
# `async_set_updated_data` on each MQTT message, and the keepalive that keeps
# Venus re-broadcasting (the "dc load seems cached / not real time" fix) is now
# driven by the MQTT client's per-connection loop at KEEPALIVE_INTERVAL_SECS, so
# a periodic poll here would only re-fan an unchanged cache and is omitted.

# The `system` branch is the one we kept for the original 10 system-
# level aggregate sensors. It doesn't live under VENUS_SERVICE_TEMPLATES
# (which covers per-device branches) but rows under it are still cached
# under a canonical `("system", "0")` key so `VictronSensor` entities
# (the pre-existing aggregate set) can look them up.
SYSTEM_SERVICE = "system"
SYSTEM_INSTANCE = "0"

# Services that describe the GX itself rather than something plugged into
# it: `system` (the cross-device aggregates) and `platform` (the GX's own
# firmware, network and notification state). Neither may become a child
# device card, and neither counts as a connected device when the discovery
# reconcile decides what is still plugged in. One set, read by every place
# that asks the question, so a third such service cannot be added to only
# half of them.
NON_DEVICE_SERVICES: frozenset[str] = frozenset({
    SYSTEM_SERVICE, PLATFORM_SERVICE, SETTINGS_SERVICE,
})

# AC-input flow topics Venus republishes as `{"value": null}` the moment the
# shore lead comes out (no AC waveform, so no reading). For these a null is
# "nothing flowing", not "no sensor", so it is cached as 0 W. Without this the
# null-preserve rule in `_on_value` kept the last plugged-in wattage and the
# Electrical card showed shore power that was no longer there. The simulator
# publishes 0 here already, so only a real Cerbo shows the difference.
NULL_MEANS_ZERO_TAILS: dict[str, frozenset[str]] = {
    SYSTEM_SERVICE: frozenset({
        "Ac/Grid/L1/Power",
        "Ac/ConsumptionOnInput/L1/Power",
    }),
    "grid": frozenset({"Ac/L1/Power"}),
    "vebus": frozenset({"Ac/ActiveIn/L1/P"}),
}


def _coerce_product_id(value: Any) -> int | None:
    """Venus `ProductId` payload → a 16-bit int, or None.

    Venus publishes it as a JSON number (e.g. ``41865`` == ``0xA389``); some
    bridges hand it over as a string. Accept both; reject anything else.
    """
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 0) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    return None


def _cache_key(service: str, instance: str) -> str:
    """Flatten a (service, instance) pair to a single dict key.

    Matches the shape sensor entities read — string keys survive the
    dict-copy `async_set_updated_data` does, and the two-level
    nesting makes change detection cheap (one tuple comparison per
    device rather than a scan across all values).
    """
    return f"{service}:{instance}"


class VictronCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Holds the latest Cerbo values — keyed per (service, instance)."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        *,
        host: str,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool | None = False,
        sensa_client: Any = None,
        on_new_device: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"victron_cerbo_{device_id}",
            update_interval=None,
        )
        self._device_id = device_id
        self._sensa_client = sensa_client
        # `on_new_device(service, instance)` fires once per
        # (service, instance) pair the first time that pair appears.
        # The integration uses it to spin up the HA device + entity set
        # dynamically — new Victron devices plugged into the Cerbo
        # appear as HA entities within a poll cycle without a reload.
        self._on_new_device = on_new_device
        self._seen_devices: set[tuple[str, str]] = set()
        # (service, instance) pairs we've seen a topic for but haven't
        # fired `on_new_device` for yet — they're waiting for the
        # keepalive-burst settle window (see NEW_DEVICE_SETTLE_SECS).
        self._pending_new_devices: set[tuple[str, str]] = set()
        # (service, instance) pairs that published a measurement (not a naming
        # tail) since `async_rescan` started listening. None outside a rescan,
        # so the per-message cost is one `is not None` test.
        self._rescan_seen: set[tuple[str, str]] | None = None
        # Services we've already warned about not having a template
        # for — keeps the log quiet once we know the gap. Cleared
        # automatically when a template is added (process restart).
        self._logged_unknown_services: set[str] = set()
        # Cache structure: {cache_key → {tail → value}}.
        # Entities index `coordinator.data[cache_key].get(tail)` — so
        # adding a new tail or service is additive, never breaks an
        # existing entity's read path.
        self._values: dict[str, dict[str, Any]] = {}
        # Victron product number per connected (service, instance) — captured
        # from the `ProductId` topic Venus publishes per device. NOT surfaced
        # as a sensor (it isn't in the templates); it's the key that lets us
        # match a Cerbo's connected devices to their standalone Bluetooth
        # adverts (Victron's `ProductId` IS the same 16-bit number as the BLE
        # advert `model_id`), so we can auto-ignore the BLE duplicates of
        # whatever is actually wired to THIS Cerbo. See
        # `_async_reconcile_cerbo_ble_ignores` in __init__.py.
        self._product_ids: dict[str, int] = {}
        # Venus `ProductName` per connected (service, instance) — the model
        # string as the device itself reports it ("MultiPlus-II 12/3000/120",
        # "Quattro 24/5000/120-2x100", "EasyPlus 12/1600/70"). Captured the
        # same way as ProductId (intercepted before the template filter, not a
        # sensor). It is the ONLY thing that distinguishes the VE.Bus family
        # members: an EasyPlus, a MultiPlus and a Quattro all publish the
        # identical `vebus` service with the identical paths, so without this
        # every one of them reads as "Multiplus". Consumed by
        # `victron_entities.device_info` for the HA device name/model, which
        # the Devices picker then reads back for the child row's label + art.
        self._product_names: dict[str, str] = {}
        # `time.monotonic()` of the last inbound MQTT message. Read by the
        # discovery sensor's `_reconcile_cerbo_hosts` so it does NOT tear down a
        # coordinator that's actively receiving data just because discovery
        # reports a different (often stale/flapping) IP. None until the first
        # message arrives.
        self.last_message_monotonic: float | None = None
        self.client = VictronMqttClient(
            hass=hass,
            on_value=self._on_value,
            device_id=device_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_tls,
        )

    @property
    def host(self) -> str | None:
        """The Cerbo's LAN host/IP this coordinator connects to. Surfaced so
        the dashboard's Remote Console card has a reliable address even when
        the backend device list didn't carry `ip_address`/`config.host`."""
        return getattr(self.client, "host", None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Connect to the Cerbo's broker + subscribe. Safe to call
        once per coordinator life."""
        await self.client.async_subscribe()
        # Seed the data dict so entities created before the first
        # message have something to read (all Nones, but registered).
        self.async_set_updated_data(dict(self._values))

    async def async_stop(self) -> None:
        """Close the MQTT connection. Called when the customer edits
        Cerbo config and the coordinator is torn down for a
        replacement."""
        await self.client.async_disconnect()

    # ------------------------------------------------------------------
    # Broker → cache plumbing
    # ------------------------------------------------------------------

    def _on_value(
        self, vrm: str, service: str, instance: str, tail: str, value: Any,
    ) -> None:
        """MQTT message → cache update → entity refresh.

        Filters to known services only — Venus publishes a lot of
        internal state we don't surface (e.g. `N/+/keepalive`, device-
        level config), so only topics with a template entry make it
        into the cache. First appearance of a new `(service, instance)`
        fires `on_new_device` so the integration can add its entities.
        """
        # Any inbound message means the broker connection is alive; stamp it so
        # the discovery host-reconcile won't tear down a working coordinator.
        self.last_message_monotonic = time.monotonic()
        # Capture each connected device's Victron product number (the
        # `ProductId` topic) into a side map BEFORE the template filter drops
        # it — it isn't a sensor, it's the BLE-match key. Skip the `system`
        # branch (that's the Cerbo GX itself, not a sub-device).
        if tail == "ProductId" and service != SYSTEM_SERVICE:
            pid = _coerce_product_id(value)
            if pid is not None:
                self._product_ids[_cache_key(service, instance)] = pid
            return

        # Same treatment for `ProductName` — the device's own model string,
        # which is what tells a Quattro from a MultiPlus from an EasyPlus (they
        # share the `vebus` service and every path under it). Skip `system`
        # (that's the Cerbo itself). Captured on arrival so it is already in
        # hand when `on_new_device` fires after the settle window and the HA
        # device card is built.
        if tail == "ProductName" and service != SYSTEM_SERVICE:
            text = str(value or "").strip()
            if text:
                self._product_names[_cache_key(service, instance)] = text
            return

        # `system` is handled alongside the templated per-device
        # services — the system branch has its own pre-existing
        # aggregate-sensor set (VictronSensor) that reads the cached
        # value by tail. Other services must be in the template map
        # to be surfaced.
        if service == SETTINGS_SERVICE:
            # One curated tail (the GX's own auto-update choice). `settings`
            # carries a few thousand topics on a real Cerbo and none of the
            # rest is ours.
            if tail not in SETTINGS_TAILS:
                return
        elif service == PLATFORM_SERVICE:
            # The GX's own firmware state. Curated like a device template (see
            # PLATFORM_FIRMWARE_TAILS), since `platform` carries a lot of
            # Venus internals we have no business caching, and cached under
            # normal `platform:0` key so the update entity reads it the same
            # way every other entity reads its values.
            if tail not in PLATFORM_FIRMWARE_TAILS:
                return
        elif service != SYSTEM_SERVICE and service not in VENUS_SERVICE_TEMPLATES:
            # One-shot WARNING per service name so we can see what
            # Venus publishes that we don't have a template for —
            # adding a new service (e.g. `pump`, `charger`) is a
            # two-line edit in victron_mqtt_services.py driven by
            # whichever names show up here on customer installs.
            if service not in self._logged_unknown_services:
                self._logged_unknown_services.add(service)
                # DEBUG, not WARNING: the Cerbo publishes many internal
                # config/system services (modbusclient, platform, settings,
                # logger, shelly, …) that aren't telemetry and we intentionally
                # don't surface — a customer-facing WARNING for each was just
                # noise. Still one-shot per service so it's there in debug logs
                # when we want to spot a genuinely useful service to template.
                _LOGGER.debug(
                    "Victron Cerbo %s: no template for service %r "
                    "(first seen on instance %s, tail %s) — drop "
                    "an entry into VENUS_SERVICE_TEMPLATES to surface it",
                    self._device_id, service, instance, tail,
                )
            return
        # For templated services, reject tails not in the template so
        # unknown Venus fields don't inflate the cache.
        if service not in NON_DEVICE_SERVICES:
            template = VENUS_SERVICE_TEMPLATES.get(service) or {}
            if tail not in template and tail not in EXTRA_CACHED_TAILS.get(
                service, (),
            ):
                return

        key = _cache_key(service, instance)
        existing = self._values.setdefault(key, {})

        # Rescan evidence is recorded BEFORE the unchanged-value dedup below:
        # the keepalive re-broadcast of a steady reading is exactly the traffic
        # that proves a device is still connected, and the dedup drops it.
        if self._rescan_seen is not None and tail not in _NAMING_TAILS:
            self._rescan_seen.add((service, instance))

        if value is None and tail in NULL_MEANS_ZERO_TAILS.get(service, ()):
            value = 0.0

        if value is None and tail in existing:
            # Null-payload republishes preserve the cached reading rather than
            # wiping it, common for optional fields Venus leaves unset (e.g.
            # Dc/0/Temperature on a shunt without a probe attached). The reading
            # keeps its last value until Venus republishes a real one.
            return

        if existing.get(tail) == value:
            # Dedup unchanged republishes — Venus resends on its own
            # cadence even when the value hasn't moved.
            return

        existing[tail] = value

        if tail == "CustomName" and service in VENUS_SERVICE_TEMPLATES:
            self._apply_custom_name(service, instance, value)

        pair = (service, instance)
        if (
            pair not in self._seen_devices
            and pair not in self._pending_new_devices
            and self._on_new_device is not None
        ):
            # Schedule on_new_device to fire after the settle window
            # so the keepalive-response burst has landed and the
            # cache contains CustomName / other naming topics. Firing
            # synchronously on the first message would build the HA
            # device card with just a "Battery #278"-style fallback.
            self._pending_new_devices.add(pair)
            # Use HA's event loop directly. `asyncio.get_event_loop()` is
            # deprecated and raises when no loop is current in the calling
            # thread (e.g. under the test runner once another test has closed
            # the default loop), which made this path order-dependent.
            self.hass.loop.call_later(
                NEW_DEVICE_SETTLE_SECS,
                self._fire_new_device_after_settle,
                service,
                instance,
            )

        # Push snapshot to entities. Copy once at the top level so
        # downstream mutations don't leak into the data HA sees; the
        # per-device dicts are never mutated in place by consumers.
        self.async_set_updated_data({k: dict(v) for k, v in self._values.items()})

    def _apply_custom_name(self, service: str, instance: str, value: Any) -> None:
        """Name the sub-device's HA card after the Cerbo's `CustomName`
        whenever it arrives or changes.

        The card is created from whatever the cache holds at the time, and
        after a restart the name can land seconds later: box .116, 2026-10-08,
        a tank the Cerbo calls "Heater fuel" registered at 10:09:51 as
        "Tank #23" and its CustomName arrived at 10:10:02. Without this the
        card (and the Devices list, which reads it) kept the fallback until
        the next reload. A name the customer set in HA (`name_by_user`) still
        wins, since HA shows it over `name`."""
        name = str(value or "").strip()
        if not name:
            return
        from homeassistant.helpers import device_registry as dr

        from ..const import DOMAIN

        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(
            identifiers={
                (DOMAIN, f"victron_cerbo_{self._device_id}_{service}_{instance}"),
            },
        )
        if device is None or device.name == name:
            return
        dev_reg.async_update_device(device.id, name=name)
        # The dashboards carry device names (a tank's Home dial), built when
        # they were generated: ask for a rebuild (debounced) so they follow.
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        from ..const import SIGNAL_DASHBOARD_REGEN_REQUESTED

        async_dispatcher_send(self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED)
        _LOGGER.info(
            "Victron Cerbo %s: %s #%s renamed to %r from its CustomName",
            self._device_id, service, instance, name,
        )

    def _fire_new_device_after_settle(
        self, service: str, instance: str,
    ) -> None:
        """Called by the event loop `NEW_DEVICE_SETTLE_SECS` after the
        first topic for `(service, instance)` landed. Elevates the
        pair from `_pending_new_devices` to `_seen_devices` and fires
        the `on_new_device` hook — by now the cache should hold any
        CustomName / Product topic Venus published in its keepalive
        burst so the HA device card gets the customer-friendly name
        at creation rather than the instance-number fallback.
        """
        pair = (service, instance)
        self._pending_new_devices.discard(pair)
        if pair in self._seen_devices:
            return
        self._seen_devices.add(pair)
        if self._on_new_device is None:
            return
        try:
            self._on_new_device(service, instance)
        except Exception:
            _LOGGER.warning(
                "Victron Cerbo %s: on_new_device(%s, %s) raised",
                self._device_id, service, instance, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Reload (in-place rescan; the coordinator and its entities stay bound)
    # ------------------------------------------------------------------

    async def async_rescan(self) -> set[tuple[str, str]] | None:
        """Listen for RESCAN_SETTLE_SECS and return the (service, instance)
        pairs that published a measurement in that window, `system` excluded.

        Returns None when NOTHING arrived, `system` included: that is "could
        not ask" (the Cerbo is unreachable), not "nothing connected", and the
        caller must change nothing. A nested call joins the running window.
        """
        if self._rescan_seen is None:
            self._rescan_seen = set()
        await asyncio.sleep(RESCAN_SETTLE_SECS)
        seen = self._rescan_seen
        self._rescan_seen = None
        if seen is None or not seen:
            return None
        return {p for p in seen if p[0] not in NON_DEVICE_SERVICES}

    def known_pairs(self) -> set[tuple[str, str]]:
        """The (service, instance) pairs currently in the cache, minus `system`."""
        pairs: set[tuple[str, str]] = set()
        for key in self._values:
            service, _, instance = key.partition(":")
            if service and service not in NON_DEVICE_SERVICES:
                pairs.add((service, instance))
        return pairs

    def forget_pair(self, service: str, instance: str) -> None:
        """Drop everything cached for one (service, instance) so that, if the
        device comes back, it is treated as new and `on_new_device` fires again
        through the normal settle path."""
        pair = (service, instance)
        key = _cache_key(service, instance)
        self._seen_devices.discard(pair)
        self._pending_new_devices.discard(pair)
        self._values.pop(key, None)
        self._product_ids.pop(key, None)
        self._product_names.pop(key, None)
        self.async_set_updated_data({k: dict(v) for k, v in self._values.items()})

    def refire_new_device(self, service: str, instance: str) -> None:
        """Fire `on_new_device` again for a pair the coordinator already knows.
        The cache already holds its naming topics, so no settle window."""
        self._seen_devices.discard((service, instance))
        self._fire_new_device_after_settle(service, instance)

    def product_name(self, service: str, instance: str) -> str:
        """Venus `ProductName` for one (service, instance), or "".

        "" means Venus has not published it (yet) — callers must fall back to
        the generic service label rather than treating absence as a verdict.
        """
        return self._product_names.get(_cache_key(service, instance), "")

    def connected_product_ids(self) -> dict[int, int]:
        """`{product_id: count}` for the Victron devices wired to this Cerbo.

        Built from each connected (service, instance)'s `ProductId`. The same
        16-bit number identifies a Victron BLE advert's `model_id`, so this is
        the set we match standalone Bluetooth devices against to auto-ignore the
        duplicates of whatever is actually on THIS Cerbo. Count is kept so the
        caller can fail safe when there are more BLE units of a model than the
        Cerbo reports (can't tell which is which → ignore none).
        """
        counts: dict[int, int] = {}
        for pid in self._product_ids.values():
            counts[pid] = counts.get(pid, 0) + 1
        return counts

    async def async_set_vebus_mode(self, instance: str, mode_int: int) -> bool:
        """Write the Multiplus/Quattro VE.Bus Mode for one vebus instance.

        Mode ints are Victron's register values: 1=charger-only,
        2=inverter-only, 3=on (inverter+charger), 4=off. Publishes to
        `W/<vrm>/vebus/<instance>/Mode` and, on success, optimistically
        seeds the cached `Mode` so the dashboard control reflects the
        change immediately — Venus republishes `N/<vrm>/vebus/<instance>/
        Mode` a beat later and `_on_value` reconciles (a matching value
        dedups to a no-op). Returns False when the broker isn't writable
        yet (no client / no VRM id captured).
        """
        ok = await self.client.async_publish_value(
            "vebus", instance, "Mode", mode_int,
        )
        if ok:
            key = _cache_key("vebus", instance)
            self._values.setdefault(key, {})["Mode"] = mode_int
            self.async_set_updated_data(
                {k: dict(v) for k, v in self._values.items()},
            )
        return ok

    async def async_set_solarcharger_mode(
        self, instance: str, mode_int: int,
    ) -> bool:
        """Write an MPPT's Mode register (1=on, 4=off) for one solarcharger
        instance — the on/off switch, distinct from the charge-algorithm State.
        Same publish + optimistic-seed contract as `async_set_vebus_mode`.
        Used by the cold-charging protection to stop solar charge into a
        freezing pack.
        """
        ok = await self.client.async_publish_value(
            "solarcharger", instance, "Mode", mode_int,
        )
        if ok:
            key = _cache_key("solarcharger", instance)
            self._values.setdefault(key, {})["Mode"] = mode_int
            self.async_set_updated_data(
                {k: dict(v) for k, v in self._values.items()},
            )
        return ok

    async def async_set_emergency_charge(
        self, service: str, instance: str, on: bool,
    ) -> bool:
        """Start (1) or stop (0) an Orion XS emergency reverse charge.

        Same publish + optimistic-seed contract as `async_set_vebus_mode`,
        except the seed only flips the "started" bit: the "possible" bit is
        the Orion's to report. Starting while it reports 0 (not possible)
        does nothing on the Orion, so nothing is seeded either.
        """
        ok = await self.client.async_publish_value(
            service, instance, EMERGENCY_CHARGE_CONTROL, 1 if on else 0,
        )
        if ok:
            key = _cache_key(service, instance)
            cache = self._values.setdefault(key, {})
            try:
                raw = int(cache.get(EMERGENCY_CHARGE_CONTROL))
            except (TypeError, ValueError):
                raw = None
            if raw:
                cache[EMERGENCY_CHARGE_CONTROL] = raw | 1 if on else raw & ~1
                self.async_set_updated_data(
                    {k: dict(v) for k, v in self._values.items()},
                )
        return ok

    async def async_check_firmware(self) -> bool:
        """Ask the GX to look for a Venus OS update.

        Writes `W/<vrm>/platform/0/Firmware/Online/Check`. venus-platform acts
        on the write itself and ignores the payload, so this only ever runs
        `check-updates.sh` on the GX; it downloads nothing the customer has not
        already allowed through the GX's own Auto-update setting, and installs
        nothing. The answer lands asynchronously on
        `Firmware/Online/AvailableVersion`, which stays null when the GX is
        already current.
        """
        return await self.client.async_publish_value(
            PLATFORM_SERVICE, PLATFORM_INSTANCE, FIRMWARE_ONLINE_CHECK, 1,
        )

    async def async_install_firmware(self) -> bool:
        """Install the Venus OS update the GX has already found.

        Writes `W/<vrm>/platform/0/Firmware/Online/Install`. This REBOOTS the
        GX and every value it feeds goes unavailable for a few minutes. Venus
        acts on the write whatever the payload says, so there is no harmless
        version of this call: only the update entity's `async_install` may
        reach it, and only when a newer version is actually on offer.
        """
        return await self.client.async_publish_value(
            PLATFORM_SERVICE, PLATFORM_INSTANCE, FIRMWARE_ONLINE_INSTALL, 1,
        )

    async def async_set_auto_update(self, value: int) -> bool:
        """Write the GX's own Venus OS auto-update choice.

        Used to stop a pinned GX installing Venus OS behind our back. See
        `AUTO_UPDATE_*` for why the numbers are not the menu order.
        """
        return await self.client.async_publish_value(
            SETTINGS_SERVICE, SETTINGS_INSTANCE, SETTING_AUTO_UPDATE, value,
        )

    def auto_update_value(self) -> Any:
        """The GX's cached auto-update choice, or None when it has not said.

        None is "could not ask", never "it is safe": nothing may conclude the
        GX will not install by itself just because the topic has not arrived.
        """
        return self.cached_value(
            SETTINGS_SERVICE, SETTINGS_INSTANCE, SETTING_AUTO_UPDATE,
        )

    def firmware_value(self, tail: str) -> Any:
        """One cached `platform/0` firmware tail, or None when the GX has not
        published it yet. None means "not asked yet", never "up to date"."""
        return self.cached_value(PLATFORM_SERVICE, PLATFORM_INSTANCE, tail)

    def service_instances(self, service: str) -> list[str]:
        """The cached instances of one Venus service (e.g. every MPPT)."""
        prefix = f"{service}:"
        return sorted(
            key[len(prefix):] for key in self._values if key.startswith(prefix)
        )

    def cached_value(self, service: str, instance: str, tail: str) -> Any:
        """The last cached raw value of one topic tail, or None."""
        return (self._values.get(_cache_key(service, instance)) or {}).get(tail)

    async def _async_update_data(self) -> dict[str, Any]:
        """Surface the live cache. The base class requires this hook, but the
        coordinator has no `update_interval`, so it is not called on a timer:
        data reaches entities via `async_set_updated_data` on each MQTT message.

        The keepalive that keeps Venus streaming is driven by the MQTT client's
        own per-connection loop (`VictronMqttClient._keepalive_loop`), which runs
        for the life of the connection and can recycle a dead link. It is
        deliberately NOT published here: doing both doubled the keepalive I/O
        and, worse, tied a critical lifeline to whether a HA entity happened to
        be subscribed (a listener-gated poll).
        """
        return {k: dict(v) for k, v in self._values.items()}
