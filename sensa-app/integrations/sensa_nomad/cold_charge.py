"""Cold-charging protection: cut controllable chargers, restore when warm.

Lithium packs must not be charged below freezing. The "Cold-charging
protection" instinct (`instincts_catalog.py`, id `cold_charging`) detects charge
current flowing into a near-freezing pack; when its Charge cut-off setting is
on, the generated automation calls `sensa_nomad.cold_charge_protect`, and a
companion automation calls `sensa_nomad.cold_charge_restore` once every pack
has warmed back up. This module is that protect/restore engine.

Three legs, covering every charge source Sensa can control:

1. **MultiPlus (vebus)** — step the inverter-mode select down so the AC charger
   stops but nothing else changes: "Charger only" -> "Off", "On" ->
   "Inverter only". "Off"/"Inverter only" already have the charger off.
2. **MPPT solar chargers** — write the Venus solarcharger Mode register to 4
   (off) via the Victron coordinator, remembering the previous mode.
3. **DC-DC charger power relay** — an Orion has no software control (BLE is
   advertisement-only, Venus `alternator` is monitoring-only), but a customer
   can wire it through a relay channel assigned to it as an Integrated Device;
   that assignment surfaces as the device's `power_relay` switch, which this
   leg turns off.

What was changed is remembered in a small durable store (same pattern as
`_support_notice.py`) so a restart mid-cold-snap cannot forget how to put
things back. Restore only reverses a change the customer has NOT overridden in
the meantime: if the current state differs from what we set, the entry is
dropped silently — the customer's choice wins. Restore also cancels the
`cold_charging` alarm and writes one info log line.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ._alarms import cancel_alarm
from ._io import atomic_write_text
from ._log import sensa_log
from .alerts import _registry_entities_by_uid_suffix
from .const import (
    DOMAIN,
    SENSA_COLD_CHARGE_PATH,
    SENSA_COLD_CHARGE_STORE_KEY,
    SENSA_LOG_SOURCE_VEHICLE,
)

_LOGGER = logging.getLogger(__name__)

# The alarm key the cold-charging routine raises under (its dedup_key), so
# restore can cancel the exact alarm.
COLD_CHARGING_ALARM_KEY = "cold_charging"

# vebus step-down: the mode that stops the AC charger while changing nothing
# else the customer had on. Modes not in this map already have the charger off.
VEBUS_STEP_DOWN: dict[str, str] = {
    "Charger only": "Off",
    "On": "Inverter only",
}

# Venus solarcharger Mode register values (the on/off switch, distinct from the
# charge-algorithm State).
SOLAR_MODE_ON = 1
SOLAR_MODE_OFF = 4

RESTORED_MESSAGE = "Battery warmed up; charging restored."
CLEARED_MESSAGE = "Battery warmed up; cold-charging cut-off cleared."

# The device rows whose power relay the DC-DC leg may switch. Deliberately
# narrow: a power relay on any other device (router, NAS, lighting) is not a
# charger and must never be touched by this engine.
_DCDC_SERVICE_CLASS = "dc_dc_charger"


class ColdChargeStore:
    """Durable record of the charger states this engine changed.

    Entries are keyed by kind so restore knows how to reverse each one:
    `select:<entity_id>` -> `{"previous_mode", "set_mode"}` (vebus select),
    `solarcharger:<device_id>:<instance>` -> `{"previous_mode": int}`,
    `relay:<entity_id>` -> `{}` (was on, we turned it off).
    """

    def __init__(self, hass: Any, path: Path | str) -> None:
        self.hass = hass
        self.path = Path(path)
        self.entries: dict[str, dict[str, Any]] = {}

    def load_sync(self) -> None:
        """Read state from disk. Corruption leaves the default in place —
        best case one missed restore, never a crashed integration."""
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            _LOGGER.debug(
                "Cold-charge store: unreadable, starting fresh", exc_info=True,
            )
            return
        if isinstance(raw, dict):
            self.entries = {
                str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
            }

    async def async_persist(self) -> None:
        try:
            body = json.dumps(self.entries, separators=(",", ":"))
            await self.hass.async_add_executor_job(
                atomic_write_text, self.path, body,
            )
        except Exception:  # pragma: no cover - best-effort durability
            _LOGGER.debug("Cold-charge store: persist failed", exc_info=True)


def get_cold_charge_store(hass: Any) -> ColdChargeStore | None:
    return (hass.data.get(DOMAIN) or {}).get(SENSA_COLD_CHARGE_STORE_KEY)


async def async_get_cold_charge_store(hass: Any) -> ColdChargeStore:
    existing = get_cold_charge_store(hass)
    if existing is not None:
        return existing
    store = ColdChargeStore(hass, hass.config.path(SENSA_COLD_CHARGE_PATH))
    await hass.async_add_executor_job(store.load_sync)
    hass.data.setdefault(DOMAIN, {})[SENSA_COLD_CHARGE_STORE_KEY] = store
    return store


# -- the charge sources ------------------------------------------------------

def _victron_coordinators(hass: Any) -> list[tuple[str, Any]]:
    """Every running VictronCoordinator as `(backend_device_id, coordinator)`.

    They live in each entry runtime's `vendor_coordinators` map alongside
    other vendor types, so filter by the duck-typed solarcharger API rather
    than importing the class (keeps this module import-light for tests).
    """
    found: list[tuple[str, Any]] = []
    for runtime in (hass.data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        for device_id, coord in (runtime.get("vendor_coordinators") or {}).items():
            if hasattr(coord, "async_set_solarcharger_mode") and hasattr(
                coord, "service_instances",
            ):
                found.append((str(device_id), coord))
    return found


def _dcdc_power_relays(hass: Any) -> list[str]:
    """The power-relay switch of every registered DC-DC charger, if the
    customer has assigned one (Settings -> Switching -> Device Type)."""
    try:
        from .dashboard import get_dashboard_devices

        devices = get_dashboard_devices()
    except Exception:  # pragma: no cover - dashboard module always importable  # noqa: BLE001
        return []
    relays: list[str] = []
    for device in devices:
        if device.get("service_class") != _DCDC_SERVICE_CLASS:
            continue
        relay = device.get("power_relay")
        if isinstance(relay, str) and relay:
            relays.append(relay)
    return relays


def _state_of(hass: Any, entity_id: str) -> str | None:
    st = hass.states.get(entity_id)
    return getattr(st, "state", None) if st is not None else None


# -- protect -----------------------------------------------------------------

async def async_cold_charge_protect(hass: Any) -> bool:
    """Switch off every controllable charge source, remembering what changed.

    Idempotent: sources already off, or already recorded in the store, are
    left alone, so the automation re-firing (alarm re-raise, HA restart with
    the condition still true) never loses the original state. Returns True
    when anything was changed on this call.
    """
    store = await async_get_cold_charge_store(hass)
    changed = False

    # Leg 1: MultiPlus inverter-mode selects.
    for entity_id in _registry_entities_by_uid_suffix(
        hass, "select", "_inverter_mode",
    ):
        key = f"select:{entity_id}"
        mode = _state_of(hass, entity_id)
        target = VEBUS_STEP_DOWN.get(mode or "")
        if target is None or key in store.entries:
            continue
        store.entries[key] = {"previous_mode": mode, "set_mode": target}
        changed = True
        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": entity_id, "option": target},
        )

    # Leg 2: MPPT solar chargers via the Victron coordinator.
    for device_id, coord in _victron_coordinators(hass):
        for instance in coord.service_instances("solarcharger"):
            key = f"solarcharger:{device_id}:{instance}"
            raw = coord.cached_value("solarcharger", instance, "Mode")
            try:
                previous = int(raw) if raw is not None else SOLAR_MODE_ON
            except (TypeError, ValueError):
                previous = SOLAR_MODE_ON
            if previous == SOLAR_MODE_OFF or key in store.entries:
                continue
            store.entries[key] = {"previous_mode": previous}
            changed = True
            await coord.async_set_solarcharger_mode(instance, SOLAR_MODE_OFF)

    # Leg 3: DC-DC chargers wired through an assigned power relay.
    for entity_id in _dcdc_power_relays(hass):
        key = f"relay:{entity_id}"
        if _state_of(hass, entity_id) != "on" or key in store.entries:
            continue
        store.entries[key] = {}
        changed = True
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id},
        )

    if changed:
        await store.async_persist()
    return changed


# -- restore -----------------------------------------------------------------

async def async_cold_charge_restore(hass: Any) -> bool:
    """Reverse every recorded cut-off the customer has not overridden.

    Every entry is cleared whether or not it is restored: a source whose state
    no longer matches what we set was changed by the customer, and their
    choice wins — restoring over it would be Sensa fighting the driver.
    Returns True when the store held anything.
    """
    store = await async_get_cold_charge_store(hass)
    if not store.entries:
        return False

    restored = False
    for key, entry in list(store.entries.items()):
        kind, _, rest = key.partition(":")
        if kind == "select":
            previous = entry.get("previous_mode")
            if (
                previous
                and _state_of(hass, rest) == entry.get("set_mode")
            ):
                restored = True
                await hass.services.async_call(
                    "select", "select_option",
                    {"entity_id": rest, "option": previous},
                )
        elif kind == "solarcharger":
            device_id, _, instance = rest.partition(":")
            coord = next(
                (c for d, c in _victron_coordinators(hass) if d == device_id),
                None,
            )
            if coord is not None and instance:
                raw = coord.cached_value("solarcharger", instance, "Mode")
                try:
                    current = int(raw) if raw is not None else None
                except (TypeError, ValueError):
                    current = None
                if current == SOLAR_MODE_OFF:
                    restored = True
                    await coord.async_set_solarcharger_mode(
                        instance, int(entry.get("previous_mode") or SOLAR_MODE_ON),
                    )
        elif kind == "relay":
            if _state_of(hass, rest) == "off":
                restored = True
                await hass.services.async_call(
                    "switch", "turn_on", {"entity_id": rest},
                )
        store.entries.pop(key, None)

    await store.async_persist()
    # The pack is warm again, so the alarm's condition is over — cancel it
    # (no-op when the customer chose Warn or already cancelled) and leave one
    # quiet log line saying what happened.
    cancel_alarm(hass, COLD_CHARGING_ALARM_KEY)
    sensa_log(
        hass, "info",
        RESTORED_MESSAGE if restored else CLEARED_MESSAGE,
        # The van's own behaviour, so the customer reads it on Intelligence.
        source=SENSA_LOG_SOURCE_VEHICLE,
        context={"cold_charge_restored": restored},
    )
    return True
