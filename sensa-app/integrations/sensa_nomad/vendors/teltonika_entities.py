"""Teltonika router entity definitions for Sensa Nomad.

Defines the 11 sensors and 1 binary sensor created when a Teltonika router
is discovered and configured with credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    DOMAIN,
    teltonika_friendly_name,
)


@dataclass(frozen=True, kw_only=True)
class TeltonikaSensorDescription(SensorEntityDescription):
    """Extended sensor description with a value extraction function."""

    value_fn: Callable[[dict[str, Any]], StateType] = lambda data: None
    min_tier: str = "lite"  # Minimum license tier required for this entity


SENSOR_DESCRIPTIONS: tuple[TeltonikaSensorDescription, ...] = (
    TeltonikaSensorDescription(
        key="signal_rssi",
        name="Signal Strength (RSSI)",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal-cellular-3",
        value_fn=lambda data: data.get("signal_rssi"),
    ),
    TeltonikaSensorDescription(
        key="signal_rsrp",
        name="Signal Quality (RSRP)",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal-cellular-2",
        value_fn=lambda data: data.get("signal_rsrp"),
    ),
    TeltonikaSensorDescription(
        key="connection_type",
        name="Connection Type",
        icon="mdi:signal-variant",
        value_fn=lambda data: data.get("connection_type"),
    ),
    TeltonikaSensorDescription(
        key="operator",
        name="Operator",
        icon="mdi:antenna",
        value_fn=lambda data: data.get("operator"),
    ),
    TeltonikaSensorDescription(
        key="data_tx_mb",
        name="Data Usage (TX)",
        native_unit_of_measurement="MB",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:upload",
        value_fn=lambda data: data.get("data_tx_mb"),
    ),
    TeltonikaSensorDescription(
        key="data_rx_mb",
        name="Data Usage (RX)",
        native_unit_of_measurement="MB",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:download",
        value_fn=lambda data: data.get("data_rx_mb"),
    ),
    TeltonikaSensorDescription(
        key="connected_clients",
        name="Connected Clients",
        icon="mdi:devices",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("connected_clients"),
    ),
    TeltonikaSensorDescription(
        key="wan_source",
        name="WAN Source",
        icon="mdi:wan",
        value_fn=lambda data: data.get("wan_source"),
    ),
    # GPS — Pro tier only
    TeltonikaSensorDescription(
        key="gps_latitude",
        name="GPS Latitude",
        icon="mdi:crosshairs-gps",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("gps_latitude"),
        min_tier="plus",
    ),
    TeltonikaSensorDescription(
        key="gps_longitude",
        name="GPS Longitude",
        icon="mdi:crosshairs-gps",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("gps_longitude"),
        min_tier="plus",
    ),
    TeltonikaSensorDescription(
        key="gps_speed",
        name="GPS Speed",
        native_unit_of_measurement="km/h",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
        value_fn=lambda data: data.get("gps_speed"),
        min_tier="plus",
    ),
    # Diagnostics — all tiers
    TeltonikaSensorDescription(
        key="device_model_id",
        name="Model",
        icon="mdi:router-wireless",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("device_model_id"),
    ),
    TeltonikaSensorDescription(
        key="firmware_version",
        name="Firmware Version",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("firmware_version"),
    ),
    TeltonikaSensorDescription(
        key="firmware_build_date",
        name="Firmware Build Date",
        icon="mdi:calendar-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("firmware_build_date"),
    ),
    TeltonikaSensorDescription(
        key="hw_usb",
        name="USB Support",
        icon="mdi:usb",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: "Yes" if data.get("hw_usb") else "No",
    ),
)


# Retired identifiers (lite/plus) share a rank with the canonical ones — every
# licence signed before the rename still carries them and always will.
TIER_ORDER = {"base": 0, "lite": 0, "pro": 1, "plus": 1, "vision": 2, "ai": 3}


def teltonika_unique_id(entry_id: str, device_id: str, key: str) -> str:
    """The unique_id for one Teltonika entity.

    Scoped per DEVICE, not per config entry. These ids used to be
    `{entry_id}_teltonika_{key}`, which is a single namespace shared by every
    Teltonika on the box -- so a van with a router AND a switch had the two
    units fighting over the same ids. HA awards each id to whichever device
    registers first and REJECTS the loser's entity outright ("does not
    generate unique IDs ... ignoring"), which on a real rig left the switch
    with no model / firmware / WAN sensors at all and the router with no PoE
    ports, while both devices' role-maps resolved to the SAME mixed set of
    entities. Verified on the test box: 36 entities existed where ~52 should
    have.

    `migrate_unique_ids` re-keys anything minted under the old scheme, so an
    upgraded box keeps its entity_ids and history.
    """
    return f"{entry_id}_teltonika_{device_id}_{key}"


def teltonika_unique_id_prefix(entry_id: str, device_id: str) -> str:
    """The `teltonika_unique_id` prefix for one device, for callers that scan
    the entity registry (`_attach_network_entity_prefixes`). Kept beside the
    minting function so the two can never drift."""
    return f"{entry_id}_teltonika_{device_id}_"


def _device_info(device_id: str, data: dict[str, Any], mac: str = "") -> dict[str, Any]:
    """Build HA device_info dict, using live model/serial when available.

    Device name strategy:
      - Pre-first-poll (data is empty): fall back to the MAC-suffix tag
        (e.g. "Teltonika a3b1c2") so the HA Devices & Services card has
        SOMETHING readable while the router is still being authenticated.
      - Post-first-poll: upgrade to "Teltonika RUTX11 (a3b1c2)" — the
        concrete model the router self-reported via /system/device/status,
        plus the MAC tag in brackets so a fleet of identical models
        stays disambiguated. The customer can still rename via HA's
        per-device "Name" override; HA prefers `name_by_user` over
        whatever we put here.
    """
    model = data.get("device_model") or "RutOS Device"
    info: dict[str, Any] = {
        "identifiers": {(DOMAIN, f"teltonika_{device_id}")},
        "name": teltonika_friendly_name(model, mac, data),
        "manufacturer": "Teltonika",
        "model": model,
    }
    serial = data.get("device_serial")
    if serial:
        info["serial_number"] = serial
    fw = data.get("firmware_version")
    if fw:
        info["sw_version"] = fw
    return info


def get_sensor_descriptions_for_tier(tier: str) -> tuple[TeltonikaSensorDescription, ...]:
    """Return sensor descriptions available for the given license tier."""
    tier_level = TIER_ORDER.get(tier, 0)
    return tuple(
        desc for desc in SENSOR_DESCRIPTIONS
        if TIER_ORDER.get(desc.min_tier, 0) <= tier_level
    )


class TeltonikaSensor(CoordinatorEntity, SensorEntity):
    """Sensor entity for a Teltonika router metric."""

    _attr_has_entity_name = True
    entity_description: TeltonikaSensorDescription

    def __init__(self, coordinator, entry_id: str, device_id: str, description: TeltonikaSensorDescription, mac: str = "") -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = teltonika_unique_id(entry_id, device_id, description.key)
        self._device_id = device_id
        self._mac = mac

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


class TeltonikaWanStatus(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor for Teltonika WAN connectivity status."""

    _attr_has_entity_name = True
    _attr_name = "WAN Status"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:web"

    def __init__(self, coordinator, entry_id: str, device_id: str, mac: str = "") -> None:
        super().__init__(coordinator)
        self._attr_unique_id = teltonika_unique_id(entry_id, device_id, "wan_status")
        self._device_id = device_id
        self._mac = mac

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        return data.get("wan_up")

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


class TeltonikaRebootButton(CoordinatorEntity, ButtonEntity):
    """Restart the router, the network card's Restart button."""

    _attr_has_entity_name = True
    _attr_name = "Restart Router"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator, entry_id: str, device_id: str, mac: str = "") -> None:
        super().__init__(coordinator)
        self._attr_unique_id = teltonika_unique_id(entry_id, device_id, "reboot")
        self._device_id = device_id
        self._mac = mac

    async def async_press(self) -> None:
        client = getattr(self.coordinator, "client", None)
        if client is None:
            raise HomeAssistantError("Teltonika client unavailable")
        try:
            await client.async_reboot()
        except Exception as err:
            raise HomeAssistantError(f"Could not restart the router: {err}") from err

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


class TeltonikaNetworks(CoordinatorEntity, SensorEntity):
    """The van's networks (VLANs) and how many clients each has.

    State is the total number of clients across the LAN segments; the
    `networks` attribute is one row per network for `sensa-vlan-card`, built
    by `network_profile.network_rows` from the router's LAN list and DHCP
    leases. Unavailable until the router has answered both reads.
    """

    _attr_has_entity_name = True
    _attr_name = "Networks"
    _attr_icon = "mdi:lan"

    def __init__(self, coordinator, entry_id: str, device_id: str, mac: str = "") -> None:
        super().__init__(coordinator)
        self._attr_unique_id = teltonika_unique_id(entry_id, device_id, "networks")
        self._entry_id = entry_id
        self._device_id = device_id
        self._mac = mac

    def _rows(self) -> list[dict[str, Any]] | None:
        from ..network_profile import layout_for_device, network_rows

        entry = self.hass.config_entries.async_get_entry(self._entry_id) if self.hass else None
        return network_rows(
            self.coordinator.data or {}, layout_for_device(entry, self._device_id),
        )

    @property
    def available(self) -> bool:
        return super().available and self._rows() is not None

    @property
    def native_value(self) -> StateType:
        rows = self._rows()
        if rows is None:
            return None
        return sum(r.get("clients") or 0 for r in rows if not r.get("wan"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"networks": self._rows() or []}

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


class TeltonikaWanPriority(CoordinatorEntity, SensorEntity):
    """The router's enabled WANs in failover order.

    State is the first-choice WAN's name; `interfaces` carries the whole
    ordered list for `sensa-wan-priority-card`, which reorders it through the
    `set_wan_order` service. Unavailable until the router has answered
    `/interfaces/config` ("could not ask" is not "no WANs").
    """

    _attr_has_entity_name = True
    _attr_name = "WAN Priority"
    _attr_icon = "mdi:swap-vertical"

    def __init__(self, coordinator, entry_id: str, device_id: str, mac: str = "") -> None:
        super().__init__(coordinator)
        self._attr_unique_id = teltonika_unique_id(entry_id, device_id, "wan_priority")
        self._device_id = device_id
        self._mac = mac

    def _interfaces(self) -> list[dict[str, Any]] | None:
        wans = (self.coordinator.data or {}).get("wan_interfaces")
        return wans if isinstance(wans, list) else None

    @property
    def available(self) -> bool:
        return super().available and self._interfaces() is not None

    @property
    def native_value(self) -> StateType:
        wans = self._interfaces()
        return wans[0]["name"] if wans else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from ..network_profile import wan_kind

        # Each WAN carries its kind, so the card grades and draws it by the
        # same rule the Networks card uses.
        return {
            "interfaces": [{**w, "kind": wan_kind(w)} for w in self._interfaces() or []],
        }

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


class TeltonikaEsim(CoordinatorEntity, SensorEntity):
    """The router's eSIM, for `sensa-esim-card`.

    Created only for a router that answered the eSIM API (see
    `_fetch_esim_data`), so a router without one gets no entity and the
    Network tab no eSIM card. State is this month's data on the profile in
    use, in MB. Attributes carry every profile, with what is left on it from
    MySensa (`coordinator.esim_balance`) for the eSIMs Sensa sold, and the
    order in progress (`coordinator.esim_order`).

    A poll that could not read the eSIM keeps the last answer: "could not ask"
    is not "no eSIM" and not "no data used".
    """

    _attr_has_entity_name = True
    _attr_name = "eSIM"
    _attr_icon = "mdi:sim"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = "MB"

    def __init__(self, coordinator, entry_id: str, device_id: str, mac: str = "") -> None:
        super().__init__(coordinator)
        self._attr_unique_id = teltonika_unique_id(entry_id, device_id, "esim")
        self._device_id = device_id
        self._mac = mac
        self._last: dict[str, Any] | None = (coordinator.data or {}).get("esim")

    @callback
    def _handle_coordinator_update(self) -> None:
        esim = (self.coordinator.data or {}).get("esim")
        if isinstance(esim, dict):
            self._last = esim
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        return self._last is not None

    @property
    def native_value(self) -> StateType:
        active = next(
            (p for p in (self._last or {}).get("profiles") or [] if p.get("active")),
            None,
        )
        used = (active or {}).get("used_month_bytes")
        return round(used / 1_048_576, 1) if isinstance(used, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        esim = self._last or {}
        balance = getattr(self.coordinator, "esim_balance", None) or {}
        profiles = [
            {**p, **(balance.get(p.get("iccid")) or {})}
            for p in esim.get("profiles") or []
        ]
        return {
            **{k: v for k, v in esim.items() if k != "profiles"},
            "profiles": profiles,
            "order": getattr(self.coordinator, "esim_order", None) or {},
        }

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


def _get_port(data: dict[str, Any], port_index: int) -> dict[str, Any]:
    """Get port data by index from coordinator data."""
    ports = data.get("ports", [])
    if port_index < len(ports):
        return ports[port_index]
    return {}


class TeltonikaPortLink(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor for a Teltonika Ethernet port link state."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry_id: str, device_id: str, port_index: int, port_name: str, mac: str = "") -> None:
        super().__init__(coordinator)
        self._port_index = port_index
        self._attr_name = f"{port_name} Link"
        self._attr_unique_id = teltonika_unique_id(
            entry_id, device_id, f"port_{port_index}_link",
        )
        self._attr_icon = "mdi:ethernet"
        self._device_id = device_id
        self._mac = mac

    @property
    def is_on(self) -> bool | None:
        port = _get_port(self.coordinator.data or {}, self._port_index)
        if not port:
            return None
        return port.get("state", "").lower() == "up"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        port = _get_port(self.coordinator.data or {}, self._port_index)
        if not port:
            return {}
        attrs = {}
        if port.get("speed") is not None:
            attrs["speed"] = port["speed"]
        if port.get("duplex"):
            attrs["duplex"] = port["duplex"]
        if port.get("traffic_load") is not None:
            attrs["traffic_load"] = port["traffic_load"]
        if port.get("max_traffic_load") is not None:
            attrs["max_traffic_load"] = port["max_traffic_load"]
        topology = port.get("topology", [])
        if topology:
            attrs["connected_devices"] = [
                {"mac": t.get("MAC", ""), "vlan": t.get("VID")}
                for t in topology if isinstance(t, dict)
            ]
        return attrs

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


class TeltonikaPortPoe(CoordinatorEntity, BinarySensorEntity):
    """Is this port actually DELIVERING PoE right now.

    Reads the normalised `poe_power` (the device's `power` field), not the
    `poe_enable` config flag -- see `_fetch_port_status`. The config flag is
    the separate `TeltonikaPortPoeSwitch` control.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, coordinator, entry_id: str, device_id: str, port_index: int, port_name: str, mac: str = "") -> None:
        super().__init__(coordinator)
        self._port_index = port_index
        self._attr_name = f"{port_name} PoE"
        self._attr_unique_id = teltonika_unique_id(
            entry_id, device_id, f"port_{port_index}_poe",
        )
        self._attr_icon = "mdi:ethernet"
        self._device_id = device_id
        self._mac = mac

    @property
    def is_on(self) -> bool | None:
        port = _get_port(self.coordinator.data or {}, self._port_index)
        if not port:
            return None
        return port.get("poe_power", "").lower() == "on"

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


class TeltonikaPortPoeSwitch(CoordinatorEntity, SwitchEntity):
    """Turn PoE on or off for one switch port.

    The customer-facing use is power-cycling a stuck PoE device (a camera, an
    access point) without walking to the cupboard. It writes the port's
    `poe_enable` CONFIG flag; whether power is flowing is the read-only
    `TeltonikaPortPoe` sensor.

    Optimistic: the RutOS write applies immediately but the coordinator only
    polls every 30s, so the toggle would sit visibly wrong for up to half a
    minute. The expected state is held until a poll confirms it, mirroring the
    Velit AC pattern. A FAILED write clears the optimistic value and re-raises,
    so a control that did not take effect never looks like it did.
    """

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_icon = "mdi:ethernet"

    def __init__(
        self, coordinator, entry_id: str, device_id: str, port_index: int,
        port_name: str, port_id: str, mac: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._port_index = port_index
        self._port_id = port_id
        self._attr_name = f"{port_name} PoE Power"
        self._attr_unique_id = teltonika_unique_id(
            entry_id, device_id, f"port_{port_index}_poe_power",
        )
        self._device_id = device_id
        self._mac = mac
        self._optimistic: bool | None = None

    def _reported(self) -> bool | None:
        port = _get_port(self.coordinator.data or {}, self._port_index)
        if not port:
            return None
        return port.get("poe_enabled", "").lower() == "on"

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._reported()

    @property
    def available(self) -> bool:
        return bool(_get_port(self.coordinator.data or {}, self._port_index))

    @callback
    def _handle_coordinator_update(self) -> None:
        # Drop the optimistic value once the device agrees with it, so a later
        # change made elsewhere (the RutOS web UI) is not masked for ever.
        if self._optimistic is not None and self._reported() == self._optimistic:
            self._optimistic = None
        super()._handle_coordinator_update()

    async def _async_set(self, enabled: bool) -> None:
        client = getattr(self.coordinator, "client", None)
        if client is None:
            raise HomeAssistantError("Teltonika client unavailable")
        self._optimistic = enabled
        self.async_write_ha_state()
        try:
            await client.async_set_port_poe(self._port_id, enabled)
        except Exception as err:
            self._optimistic = None
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Could not change PoE on {self._port_id}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    @property
    def device_info(self):
        return _device_info(self._device_id, self.coordinator.data or {}, self._mac)


def _port_label(port: dict[str, Any], index: int) -> str:
    """Pick a per-port label that's distinct across all four RutX11
    ports. The previous logic used `port.name` directly — but RutOS
    returns the ROLE ("LAN", "WAN") in `name`, so all three LAN ports
    collapsed to the same "LAN Link" label. Selection order:

      1. `id` — RutOS sets this to the port-specific label like
         "LAN1", "LAN2", "LAN3", "WAN" on most models.
      2. `name + position` — e.g. "LAN 1", "LAN 2" when only the role
         is in `name` and the position field tells us which physical
         port it is.
      3. Plain "Port N" — last-ditch fallback.

    A RUTX11 prefixes the id with an underscore ("_lan2", "_wan5",
    captured from the device on 2026-08-07), which is an internal
    interface name, not something to show a customer. A TSW switch
    reports a clean "port1" / "sfp1". Strip the prefix so both read
    the same way.
    """
    port_id = (port.get("id") or "").strip().lstrip("_")
    if port_id:
        return port_id
    role = (port.get("name") or "").strip()
    position = port.get("position")
    if position is None:
        position = index + 1
    if role:
        return f"{role} {position}"
    return f"Port {position}"


def migrate_unique_ids(hass, entry_id: str, device_id: str) -> int:
    """Re-key this device's entities from the old entry-scoped unique_id onto
    the per-device one. Returns how many rows were rewritten.

    MUST run before the entities are added, or the new ids register as fresh
    entities alongside the old rows and the customer gets a duplicate set with
    the history stranded on the orphans.

    Which device owned an old id is resolved through the HA DEVICE registry,
    never guessed from the entity_id string: every Teltonika entity carries
    `device_info` identified as `(DOMAIN, "teltonika_<device_id>")`, so on a
    box that already has a router and a switch sharing the namespace each row
    is attributed to the unit that actually created it.

    `async_update_entity(new_unique_id=)` keeps the entity_id and its history.
    An id already taken is skipped rather than forced -- a collision here
    means something else legitimately holds it, and stealing it would be the
    same bug in the other direction.
    """
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
    except ImportError:  # pragma: no cover - HA always provides these
        return 0

    try:
        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
    except Exception:  # pragma: no cover - registries unavailable at boot  # noqa: BLE001
        return 0

    ha_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"teltonika_{device_id}")},
    )
    if ha_device is None:
        return 0

    old_prefix = f"{entry_id}_teltonika_"
    new_prefix = teltonika_unique_id_prefix(entry_id, device_id)
    migrated = 0
    for ent in er.async_entries_for_device(
        ent_reg, ha_device.id, include_disabled_entities=True,
    ):
        uid = ent.unique_id or ""
        if not uid.startswith(old_prefix) or uid.startswith(new_prefix):
            continue
        new_uid = new_prefix + uid[len(old_prefix):]
        if ent_reg.async_get_entity_id(ent.domain, DOMAIN, new_uid):
            continue
        ent_reg.async_update_entity(ent.entity_id, new_unique_id=new_uid)
        migrated += 1

    return migrated


def create_port_entities(
    coordinator, entry_id: str, device_id: str, mac: str = "",
) -> list[BinarySensorEntity]:
    """Create port entities from the initial coordinator data."""
    entities: list[BinarySensorEntity] = []
    data = coordinator.data or {}
    ports = data.get("ports", [])
    hw_poe = data.get("hw_poe", False)

    for index, port in enumerate(ports):
        label = _port_label(port, index)
        entities.append(
            TeltonikaPortLink(coordinator, entry_id, device_id, index, label, mac=mac)
        )
        # Only create PoE entity if the hardware supports PoE
        if hw_poe:
            entities.append(
                TeltonikaPortPoe(coordinator, entry_id, device_id, index, label, mac=mac)
            )

    return entities


def create_port_poe_switches(
    coordinator, entry_id: str, device_id: str, mac: str = "",
) -> list[SwitchEntity]:
    """One PoE on/off control per PoE-capable port.

    Gated on the PORT reporting a `poe_enable` key (surfaced as `poe_capable`
    by the normaliser) as well as the device-level `hw_poe`, so a mixed unit
    never gets a control that writes to a port with no PoE hardware behind it.

    Which of these the CUSTOMER is shown is a separate, presentational
    decision made in the dashboard builder (Richard asked for the 8 copper
    ports, not the 2 SFP cages). Deliberately not filtered here: telling copper
    from fibre would mean pattern-matching the device's port names, and a
    naming guess in the entity layer is harder to correct than a list in the
    builder.
    """
    entities: list[SwitchEntity] = []
    data = coordinator.data or {}
    if not data.get("hw_poe", False):
        return entities

    for index, port in enumerate(data.get("ports", [])):
        if not port.get("poe_capable"):
            continue
        port_id = (port.get("id") or "").strip()
        if not port_id:
            # The write is addressed by port id; without one there is nothing
            # to write to, and a control that cannot act is worse than none.
            continue
        entities.append(
            TeltonikaPortPoeSwitch(
                coordinator, entry_id, device_id, index,
                _port_label(port, index), port_id, mac=mac,
            )
        )

    return entities
