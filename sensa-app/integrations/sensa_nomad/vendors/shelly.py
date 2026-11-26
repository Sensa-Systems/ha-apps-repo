"""Shelly devices adopted into HA's own `shelly` integration.

Sensa finds a Shelly with its own `/shelly` HTTP sweep (the `http_probe`
catalog blocks on the `shelly_*` entries) and, on add, creates the
HA `shelly` config entry for it. HA then owns the device and its entities; this
module only answers "which HA entry, and which entity, is this Sensa row's".

Kept free of package-root imports so both `__init__` and `dashboard` can use it.
"""
from __future__ import annotations

import re
from typing import Any


def _canon(value: Any) -> str:
    """Upper-cased, separator-stripped form for MAC comparison."""
    return str(value or "").upper().replace(":", "").replace("-", "").replace(" ", "")


def shelly_entry_ids(hass: Any, device: dict[str, Any]) -> set[str]:
    """The HA `shelly` config entries that belong to this Sensa device row.

    HA keys a Shelly entry on the device MAC (its `unique_id`), so that is the
    match; the host (IP or `<id>.local`) is the fallback for a row that carries
    no MAC. The ONE place a Sensa device is tied to its Shelly entry: the relay
    resolver, the Security tile, the smoke instinct and the add path all ask
    here, so they cannot disagree about which entry is whose.
    """
    mac = _canon(
        device.get("linked_mac_address")
        or device.get("mac_address")
        or device.get("address")
        or ""
    )
    ip = device.get("ip_address") or ""
    hostname = device.get("linked_hostname") or device.get("hostname") or ""

    entry_ids: set[str] = set()
    for shelly_entry in hass.config_entries.async_entries("shelly"):
        data = shelly_entry.data or {}
        host = str(data.get("host") or "")
        uid = _canon(shelly_entry.unique_id or "")
        if mac and uid and mac in uid or host and host in (ip, hostname, f"{hostname}.local"):
            entry_ids.add(shelly_entry.entry_id)
    return entry_ids


_CHANNEL_POWER_UID = re.compile(r"-switch:(\d+)-power$")


def shelly_channel_power_entities(
    hass: Any, ent_reg: Any, device: dict[str, Any],
) -> dict[str, str]:
    """`relay_N` -> the live power `sensor.*` of that circuit, for a metered
    Shelly (the Pro 4PM).

    Keyed on the entity's unique_id, `<mac>-switch:<N>-power` (HA 2026.8.3's
    shelly `ShellyRpcAttributeEntity`: `{mac}-{key}-{attribute}`), for the same
    reason the relay resolver keys on `<mac>-switch:<N>`: the entity_id carries
    the customer's name and moves, the unique_id does not. Channel numbers are
    the vendor's, from 0, so `switch:0` is `relay_1`, matching the switches.
    """
    entry_ids = shelly_entry_ids(hass, device)
    if not entry_ids:
        return {}
    out: dict[str, str] = {}
    for ent in ent_reg.entities.values():
        if ent.config_entry_id not in entry_ids:
            continue
        if not ent.entity_id.startswith("sensor."):
            continue
        match = _CHANNEL_POWER_UID.search(str(getattr(ent, "unique_id", "") or ""))
        if match:
            out[f"relay_{int(match.group(1)) + 1}"] = ent.entity_id
    return out


# The per-circuit protection flags HA's shelly integration publishes for a
# metered switch (binary_sensor.py RPC_SENSORS, HA 2026.8.3): each is a
# `problem` binary_sensor that reads "on" while the device reports that error
# on the circuit. Keyed to the words a customer reads.
CIRCUIT_FAULTS: dict[str, str] = {
    "overtemp": "Overheating",
    "overpower": "Overpowering",
    "overvoltage": "Overvoltage",
    "overcurrent": "Overcurrent",
}
_CHANNEL_FAULT_UID = re.compile(
    r"-switch:(\d+)-(" + "|".join(CIRCUIT_FAULTS) + r")$"
)


def shelly_channel_fault_entities(
    hass: Any, ent_reg: Any, device: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """`(relay_N, fault, entity_id)` for every protection flag on a metered
    Shelly's circuits, by unique_id `<mac>-switch:<N>-<fault>`, in circuit
    then fault order. Empty until the entry and its entities exist."""
    entry_ids = shelly_entry_ids(hass, device)
    if not entry_ids:
        return []
    out: list[tuple[int, str, str]] = []
    for ent in ent_reg.entities.values():
        if ent.config_entry_id not in entry_ids:
            continue
        if not ent.entity_id.startswith("binary_sensor."):
            continue
        match = _CHANNEL_FAULT_UID.search(str(getattr(ent, "unique_id", "") or ""))
        if match:
            out.append((int(match.group(1)), match.group(2), ent.entity_id))
    order = list(CIRCUIT_FAULTS)
    out.sort(key=lambda t: (t[0], order.index(t[1])))
    return [(f"relay_{n + 1}", fault, eid) for n, fault, eid in out]


def shelly_binary_entity(
    hass: Any, ent_reg: Any, device: dict[str, Any], prefer_classes: frozenset[str],
) -> str | None:
    """The `binary_sensor.*` of an adopted Shelly whose device_class is one of
    `prefer_classes` (a Plus Smoke's `smoke`). None when the entry or the
    entity is not there yet, so the caller omits the tile rather than drawing a
    placeholder. Unlike the Z2M resolver there is no "first binary_sensor"
    fallback: a Shelly also publishes cloud / firmware binary_sensors, and a
    smoke tile bound to "Cloud connected" would be worse than no tile.
    """
    entry_ids = shelly_entry_ids(hass, device)
    if not entry_ids:
        return None
    matches = sorted(
        ent.entity_id
        for ent in ent_reg.entities.values()
        if ent.config_entry_id in entry_ids
        and ent.entity_id.startswith("binary_sensor.")
        and (
            getattr(ent, "device_class", None)
            or getattr(ent, "original_device_class", None)
            or ""
        ) in prefer_classes
    )
    return matches[0] if matches else None


# The RPC light components HA's shelly integration turns into `light.*`
# entities (light.py LIGHTS, HA dev 2026-10): which ones a unit has depends on
# the PROFILE set on the device. A Plus RGBW PM in "light" profile reports four
# dimmable channels `light:0`..`light:3`; in "rgbw" profile one `rgbw:0`; in
# "rgb" one `rgb:0`. The unique_id is `<mac>-<component>:<N>`.
_LIGHT_UID = re.compile(r"-(light|rgbw|rgbcct|rgb|cct):(\d+)$")


def _shelly_light_entries(
    hass: Any, ent_reg: Any, device: dict[str, Any],
) -> list[tuple[int, Any]]:
    """`(vendor channel, registry entry)` for every `light.*` of an adopted
    Shelly light controller, in channel order, keyed on the unique_id and never
    the entity_id (the same reason the relay resolver keys on
    `<mac>-switch:<N>`). Empty until the entry and its entities exist."""
    entry_ids = shelly_entry_ids(hass, device)
    if not entry_ids:
        return []
    found: list[tuple[str, int, Any]] = []
    for ent in ent_reg.entities.values():
        if ent.config_entry_id not in entry_ids:
            continue
        if not ent.entity_id.startswith("light."):
            continue
        match = _LIGHT_UID.search(str(getattr(ent, "unique_id", "") or ""))
        if match:
            found.append((match.group(1), int(match.group(2)), ent))
    found.sort(key=lambda item: (item[0], item[1], item[2].entity_id))
    return [(n, ent) for _kind, n, ent in found]


def shelly_light_entities(
    hass: Any, ent_reg: Any, device: dict[str, Any],
) -> list[str]:
    """Every `light.*` of an adopted Shelly light controller, in the vendor's
    channel order. Empty until the entry and its entities exist, so the caller
    omits the card rather than drawing a placeholder. Switching the device's
    profile replaces the entities, and the `light.*` create listener rebuilds
    the dashboard with the new set."""
    return [ent.entity_id for _n, ent in _shelly_light_entries(hass, ent_reg, device)]


def shelly_light_outputs(
    hass: Any, ent_reg: Any, device: dict[str, Any],
) -> list[tuple[int, str, str]]:
    """`(vendor channel, light entity, name)` per output, the ONE place an
    output gets its name: the Devices child rows, the Lighting tab, Home and the
    group picker all read it, so they cannot disagree.

    A one-light profile (rgbw / rgb) is named after the device. In four-channel
    "light" profile each output takes the name the customer gave it, which is
    the HA entity registry name, else "<device> <position>". The registry is the
    only store: the Mode Editor's `rename_entity` writes the same field, so a
    rename from either place is the same name.
    """
    entries = _shelly_light_entries(hass, ent_reg, device)
    device_name = device.get("display_name") or device.get("name") or "Light"
    if len(entries) == 1:
        n, ent = entries[0]
        return [(n, ent.entity_id, device_name)]
    return [
        (
            n,
            ent.entity_id,
            str(getattr(ent, "name", None) or "").strip() or f"{device_name} {pos}",
        )
        for pos, (n, ent) in enumerate(entries, start=1)
    ]


# A four-channel output's picker row is `<parent row id>:light:<channel>`. The
# parent id can be a MAC, colons and all, so parse from the right.
_LIGHT_ROW_ID = re.compile(r"^(.+):light:(\d+)$")


def shelly_light_row_id(parent_row_id: str, channel: int) -> str:
    """The picker row id of one output of a Shelly light controller."""
    return f"{parent_row_id}:light:{channel}"


def parse_shelly_light_row_id(row_id: str) -> tuple[str, int] | None:
    """`(parent row id, channel)` for an output's row id, else None."""
    match = _LIGHT_ROW_ID.match(row_id)
    return (match.group(1), int(match.group(2))) if match else None
