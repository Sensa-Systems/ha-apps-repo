"""Service-class abstraction for Sensa Nomad.

A **service class** is the canonical, vendor-neutral answer to "what *kind*
of thing is this device?" — `battery_monitor`, `solar_charger`,
`inverter_charger`, `router`, … . It is a flat vocabulary (no sub-classes:
we do not care lithium vs AGM here) and its single job is to drive the
dashboard:

  1. **View routing** — every service class rolls up into a `category`
     (`electrical` / `switching` / `network` / `environment` / `water`).
     A view renders the device rows whose service class sits in its
     category. `category` is *derived from* the service class, never
     hand-maintained per device.
  2. **Role resolution** — `dashboard.py` asks "which device supplies the
     battery SoC?" and resolves it by walking a preference list of
     service classes (see `ROLE_SOURCES` there).

The hard part this module solves is **normalisation**: every vendor speaks
its own vocabulary. The Victron Cerbo fans out Venus OS service names
(`battery`, `solarcharger`, `vebus`, …); the device catalog uses its own
`type` field (`battery_monitor`, `dc_dc_converter`, `gx_gateway`, …); a
BLE-native device carries whatever its catalog entry says. `classify()` is
the one entry point that maps any of those onto a canonical service-class
key, so the rest of the integration only ever deals in canonical keys.

Adding a new vendor is purely additive: extend the mapping tables below
and every existing view picks the new devices up for free. See
`docs/system-design/service-classes.md` for the full framework writeup.

End-state note: `service_class` will be **persisted on the backend device
record** (written by `classify()` at the discovery / manual add paths).
Until that lands, `classify()` is called at runtime on every dashboard
regen — the same function, so nothing here is throwaway.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, kw_only=True)
class ServiceClass:
    """One canonical device kind.

    `key`      — canonical, vendor-neutral slug. The whole integration
                 keys off this.
    `label`    — customer-facing name (card titles, group headers).
    `icon`     — mdi icon for cards / view headers.
    `category` — coarse view-routing bucket the class rolls up into.
    """

    key: str
    label: str
    icon: str
    category: str


# ---------------------------------------------------------------------------
# The canonical vocabulary — flat, controlled. This is the single source of
# truth; `category` lives here, not on individual catalog entries.
# ---------------------------------------------------------------------------
_SERVICE_CLASS_LIST: tuple[ServiceClass, ...] = (
    # --- electrical -------------------------------------------------------
    ServiceClass(key="battery", label="Battery",
                 icon="mdi:battery", category="electrical"),
    ServiceClass(key="battery_monitor", label="Battery Monitor",
                 icon="mdi:battery-heart-variant", category="electrical"),
    ServiceClass(key="solar_charger", label="Solar Charger",
                 icon="mdi:solar-power", category="electrical"),
    ServiceClass(key="inverter_charger", label="Inverter / Charger",
                 icon="mdi:power-plug", category="electrical"),
    ServiceClass(key="dc_dc_charger", label="DC-DC Charger",
                 icon="mdi:current-dc", category="electrical"),
    # A low-voltage load disconnect (e.g. the Victron Smart BatteryProtect
    # 12/24V-220A). Protects the battery by cutting downstream loads when the
    # voltage drops. Adopted read-only via HA core's `victron_ble` (Instant
    # Readout is broadcast-only): output state, off-reason, input/output
    # voltage. Switching its output would need a GATT-write vendor module
    # (a follow-on); no bespoke card yet, so it surfaces in Discovery + the
    # Support device registry + Devices & Services until a card lands.
    ServiceClass(key="battery_protect", label="Battery Protect",
                 icon="mdi:battery-lock", category="electrical"),
    ServiceClass(key="dc_system", label="DC System",
                 icon="mdi:home-lightning-bolt", category="electrical"),
    # Sensa DC Meter 6: an ESP32 with up to six INA226 shunt monitors, one cut
    # into the positive feed of each 12V circuit. Reports watts per circuit, which
    # the Electric tab shows as one tile per circuit.
    ServiceClass(key="dc_meter", label="DC Meter",
                 icon="mdi:meter-electric-outline", category="electrical"),
    ServiceClass(key="grid_meter", label="Grid Meter",
                 icon="mdi:transmission-tower", category="electrical"),
    # AC-output-side meter — typically a Carlo Gavazzi EM24 / ET112 wired
    # on a Multiplus/Quattro AC-Out-2 line so the customer can see the
    # shore-conditional load separately from always-live AC-Out-1. Venus
    # OS publishes both as `com.victronenergy.grid` services; the
    # `Position` field (0=AC input 1, 1=AC output, 2=AC input 2)
    # distinguishes them. Picked by `_attach_service_class_rows` via
    # the synthetic-row attach path, not by `classify()` — Venus gives us
    # the same `victron_service` ("grid") for both kinds, so the
    # disambiguation has to read Position at attach time.
    ServiceClass(key="ac_output_meter", label="AC Output Meter",
                 icon="mdi:meter-electric", category="electrical"),
    ServiceClass(key="pv_inverter", label="PV Inverter",
                 icon="mdi:solar-panel", category="electrical"),
    ServiceClass(key="generator", label="Generator",
                 icon="mdi:engine", category="electrical"),
    ServiceClass(key="gx_gateway", label="System Gateway",
                 icon="mdi:hub", category="electrical"),
    # A Bitcoin ASIC miner (e.g. the Lucky Miner LV08 Pro) run as a *dumb DC
    # load with telemetry*: it draws off the 12V leisure bus like any other
    # load, which is why it lives in `electrical` and not in a category of its
    # own. Sensa polls its AxeOS HTTP API for hash rate, power, temperatures
    # and share counts, and can throttle or stop it. Being a discretionary
    # load is the whole point: it is the first device the van can shed when
    # the battery is low and soak up surplus solar when it is not.
    ServiceClass(key="bitcoin_miner", label="Crypto Miner",
                 icon="mdi:pickaxe", category="electrical"),
    # A portable power station (e.g. the DJI Power 1000 V2): battery, inverter
    # and charger in one box that a customer carries in and out of the van. It
    # is deliberately NOT `battery` (that role is the fitted leisure bank and
    # is summed into the Power flow) and NOT `inverter_charger` (it never sits
    # on the VE.Bus). Its SoC and AC load are its own, shown on its own card.
    ServiceClass(key="power_station", label="Power Station",
                 icon="mdi:battery-charging", category="electrical"),
    # --- switching --------------------------------------------------------
    ServiceClass(key="relay", label="Relay Controller",
                 icon="mdi:electric-switch", category="switching"),
    # A wireless scene button (e.g. the SONOFF SNZB-01M, four buttons x
    # single / double / triple / long). It switches nothing itself: every
    # press is an INPUT the customer binds to a Routine, which is why it sits
    # beside the relay in `switching` rather than in `security` with the
    # read-only Zigbee sensors. Z2M publishes the presses as an HA `event`
    # entity, not a binary_sensor, so `classify_zigbee2mqtt_device` reaches it
    # via the `event` domain and the catalog class is authoritative for the
    # known model (the DJT11LM pattern).
    ServiceClass(key="scene_button", label="Scene Button",
                 icon="mdi:gesture-tap-button", category="switching"),
    # Sensa Dial: a round touch screen in a rotary knob, fed and driven by
    # `dial.py` (climate, lights, tanks, electrical, bedside).
    ServiceClass(key="dial", label="Dial",
                 icon="mdi:knob", category="display"),
    # --- network ----------------------------------------------------------
    ServiceClass(key="router", label="Router",
                 icon="mdi:router-wireless", category="network"),
    ServiceClass(key="network_switch", label="Network Switch",
                 icon="mdi:switch", category="network"),
    ServiceClass(key="satellite_internet", label="Satellite Internet",
                 icon="mdi:satellite-uplink", category="network"),
    ServiceClass(key="zigbee_coordinator", label="Zigbee Coordinator",
                 icon="mdi:zigbee", category="network"),
    ServiceClass(key="bluetooth_proxy", label="Bluetooth Proxy",
                 icon="mdi:bluetooth", category="network"),
    # Sensa Infra red C008: learns and sends IR codes for the IR-controlled
    # devices nested under it (`parent_kind: ir_transceiver`).
    ServiceClass(key="ir_transceiver", label="Infra Red",
                 icon="mdi:remote", category="network"),
    # A network-attached storage appliance (e.g. the Asustor Flashstor 6
    # FS6706T). Sensa drives only its power: a clean shutdown over SSH +
    # Wake-on-LAN power-on, surfaced as a single switch on Settings → Support.
    # `category="network"` is purely for discovery-picker grouping + giving the
    # Support device-registry table a category — no Habitation/Settings network
    # tab renders NAS rows (those gate on the Teltonika/Starlink card lists, not
    # on the network category).
    ServiceClass(key="nas", label="Network Storage",
                 icon="mdi:nas", category="network"),
    # --- environment ------------------------------------------------------
    ServiceClass(key="temperature_sensor", label="Temperature Sensor",
                 icon="mdi:thermometer", category="environment"),
    ServiceClass(key="environment_sensor", label="Environment Sensor",
                 icon="mdi:gauge", category="environment"),
    # --- security ---------------------------------------------------------
    # Zigbee binary sensors surfaced via Zigbee2MQTT (motion / door-window /
    # water-leak). Read-only — they drive a conditional Habitation Security tab
    # of state tiles, not a control. Like the relay-assignable classes below,
    # classify() reaches them via the `role` the Z2M add path persists
    # (CATALOG_TYPE_TO_SERVICE_CLASS), not from vendor metadata.
    ServiceClass(key="motion_sensor", label="Motion Sensor",
                 icon="mdi:motion-sensor", category="security"),
    ServiceClass(key="contact_sensor", label="Contact Sensor",
                 icon="mdi:door", category="security"),
    ServiceClass(key="leak_sensor", label="Leak Detector",
                 icon="mdi:water-alert", category="security"),
    ServiceClass(key="vibration_sensor", label="Vibration Sensor",
                 icon="mdi:vibrate", category="security"),
    # A Shelly Plus Smoke, adopted into HA's own `shelly` integration. Read-only
    # like the Zigbee sensors above, but classified from its catalog `type`.
    ServiceClass(key="smoke_sensor", label="Smoke Detector",
                 icon="mdi:smoke-detector", category="security"),
    # An IP camera on the LAN (RTSP). At Pro it is the camera a customer
    # nominates for grey-waste dumping, shown live through Frigate; the Vision
    # tier's recording and face recognition are the same class with more
    # behind it (docs/system-design/vision.md).
    ServiceClass(key="ip_camera", label="Camera",
                 icon="mdi:cctv", category="security"),
    # --- water (no view yet — service class defined so the future Water
    #     view is a pure filter, no discovery code) -------------------------
    ServiceClass(key="tank", label="Tank",
                 icon="mdi:propane-tank", category="water"),
    # --- vehicle (no view yet — service class defined so a future Cab /
    #     levelling surface is a pure filter, no discovery code) ------------
    ServiceClass(key="levelling", label="Levelling",
                 icon="mdi:spirit-level", category="vehicle"),
    # An OBD2 adapter (e.g. the MeatPi WiCAN Pro) bridges the vehicle's CAN
    # bus and exposes engine/vehicle telemetry (RPM, speed, coolant, fuel…)
    # as HA `mqtt` sensors. It backs the conditional Cab Engine tab (see
    # `dashboard._is_obd2_device`).
    ServiceClass(key="obd2", label="OBD2 Adapter",
                 icon="mdi:car-info", category="vehicle"),
    # BLE tyre-pressure sensor (one per wheel, passive adverts). Laid out by
    # wheel position on the Cab "Tyres" tab.
    ServiceClass(key="tyre_pressure", label="Tyre Pressure Sensor",
                 icon="mdi:car-tire-alert", category="vehicle"),
    # --- media (cooperate-flow) -------------------------------------------
    # A network speaker (e.g. the Sonos Roam 2) is owned by HA's own native
    # integration (`sonos`), which creates the `media_player.*` entity. Sensa
    # does not run a vendor coordinator for it — it discovers/adopts the
    # device and surfaces the native entity on a Habitation Media tab via the
    # `custom:sensa-media-player-card`. The `media` category backs that tab.
    ServiceClass(key="speaker", label="Speaker",
                 icon="mdi:speaker", category="media"),
    # A voice satellite (the Home Assistant Voice PE) is an ESPHome device HA's
    # own `esphome` integration owns. Sensa adopts it and runs the local speech
    # pipeline behind it; the licence gates it by this class (Pro and up).
    ServiceClass(key="voice_satellite", label="Voice",
                 icon="mdi:microphone-message", category="media"),
    # --- climate (rich, vendor-driven) -----------------------------------
    # A forced-air heater (e.g. the Autoterm Air D family) is NOT a
    # relay-switched dumb load like `space_heater` below — it runs its own vendor
    # module exposing temperature / setpoint / power-level / mode entities, so it
    # classifies normally from the catalog `type` and resolves to its own
    # rich card (`custom:sensa-autoterm-heater-card`) on the Habitation Climate tab.
    # Named `air_heater` (2026-07-08, renamed from `diesel_heater`) to match the
    # firmware's `Sensa Device Type` and to pair symmetrically with
    # `hydronic_heater` — air vs wet. See `_LEGACY_SERVICE_CLASS_ALIASES`.
    ServiceClass(key="air_heater", label="Air Heater",
                 icon="mdi:fire", category="climate"),
    # A hydronic (wet / glycol) central-heating system — e.g. the Alde
    # Compact 4000 D Plus — is, like the diesel air heater, a RICH
    # vendor-driven device (its ESPHome bridge exposes its own
    # temperature / setpoint / mode / circulation entities), not a
    # relay-switched dumb load. It classifies from the catalog `type`
    # (`hydronic_heater`). Distinct from `air_heater` because the
    # surfaces differ (wet heating + a hot-water circuit vs forced air);
    # its own card is a follow-on.
    ServiceClass(key="hydronic_heater", label="Hydronic Heater",
                 icon="mdi:radiator", category="climate"),
    # A rooftop air conditioner — e.g. the Velit — is a RICH vendor-driven
    # device exposing its own HA `climate` entity (the Velit BLE GATT
    # integration), classified from the catalog `type` (`air_conditioner`).
    ServiceClass(key="air_conditioner", label="Air Conditioner",
                 icon="mdi:air-conditioner", category="climate"),
    # --- relay-assignable "Switched Device" classes -----------------------
    # These never resolve from vendor metadata — `classify()` cannot reach
    # them. They exist only as the choices a customer can assign to a relay
    # channel via the "Works with" dropdown (a channel that switches a load
    # Sensa has no driver for). Their categories (`lighting` / `climate` /
    # `water`) back the conditional Habitation tabs added in Phase 2.
    ServiceClass(key="lighting", label="Lighting",
                 icon="mdi:lightbulb", category="lighting"),
    ServiceClass(key="water_pump", label="Water Pump",
                 icon="mdi:water-pump", category="water"),
    # A wet heating system's circulation pump (the Baltica's radiators or
    # underfloor): a plain relay type. Climate Control no longer drives it
    # (a Baltica is never its heater, 2026-10-17); kept so a relay already
    # set to it keeps its label and icon.
    ServiceClass(key="circulation_pump", label="Circulation Pump",
                 icon="mdi:pump", category="climate"),
    ServiceClass(key="water_heater", label="Water Heater",
                 icon="mdi:water-boiler", category="water"),
    ServiceClass(key="dump_valve", label="Dump Valve",
                 icon="mdi:valve", category="water"),
    ServiceClass(key="ventilation", label="Ventilation / Fan",
                 icon="mdi:fan", category="climate"),
    # Sensa Fan Temperature Control: 4-pin PWM fans with a speed, driven by the fan
    # autopilot (`fan_autopilot.py`). Not relay-assignable: a relay channel
    # only switches, which is `ventilation`.
    ServiceClass(key="fan_controller", label="Fan Temperature Control",
                 icon="mdi:fan-auto", category="climate"),
    ServiceClass(key="space_heater", label="Heater",
                 icon="mdi:radiator", category="climate"),
    ServiceClass(key="refrigeration", label="Fridge / Cooling",
                 icon="mdi:fridge", category="climate"),
    # A relay-switched battery maintenance / trickle charger. `electrical`
    # category, which has no routed Habitation switched-tab (only lighting /
    # water / climate do), so a channel assigned this is controlled from the
    # Settings → Switching tab — no separate Habitation tile.
    ServiceClass(key="trickle_charge", label="Trickle Charge",
                 icon="mdi:battery-charging", category="electrical"),
)

SERVICE_CLASSES: dict[str, ServiceClass] = {
    sc.key: sc for sc in _SERVICE_CLASS_LIST
}


# Service-class keys that were renamed. A device row persisted by the backend
# BEFORE the rename still carries the old key (`classify()` treats an existing
# `service_class` as most authoritative), so it must be mapped forward or the
# device silently stops matching its card's gate — e.g. a heater registered on a
# dev rig would disappear from the Habitation Climate tab until re-added.
# Keep entries here forever: they cost one dict lookup and there is no migration.
_LEGACY_SERVICE_CLASS_ALIASES: dict[str, str] = {
    "diesel_heater": "air_heater",  # renamed 2026-07-08
}


# Sentinel value for an unassigned relay channel (the dropdown default).
RELAY_UNASSIGNED = "unassigned"

# The "Switched Device" service classes a relay channel may be assigned to.
# A relay channel only ever carries on/off control, so these are the load
# *kinds* Sensa can route to a category view (Phase 2) without owning a full
# vendor integration. Order here is NOT dropdown order: the relay card's
# Switched-Device group is sorted A-Z by label when it is built.
RELAY_ASSIGNABLE_SERVICE_CLASSES: tuple[str, ...] = (
    "lighting",
    "water_pump",
    "circulation_pump",
    "water_heater",
    "dump_valve",
    "ventilation",
    "space_heater",
    "refrigeration",
    "trickle_charge",
)


# ---------------------------------------------------------------------------
# Normalisation layer — every vendor's native vocabulary -> canonical key.
# Extend these tables to onboard a new vendor; nothing else needs to change.
# ---------------------------------------------------------------------------

# Device-catalog `type` (device_catalog.json) -> service_class.
CATALOG_TYPE_TO_SERVICE_CLASS: dict[str, str] = {
    "relay": "relay",
    "bluetooth_proxy": "bluetooth_proxy",
    "ir_transceiver": "ir_transceiver",
    "router": "router",
    "switch": "network_switch",          # catalog `switch` == Teltonika net switch
    "satellite_internet": "satellite_internet",
    "nas": "nas",                        # Asustor Flashstor 6 (clean-shutdown NAS)
    "zigbee_coordinator": "zigbee_coordinator",
    "environment_sensor": "environment_sensor",
    "battery_monitor": "battery_monitor",
    "solar_charger": "solar_charger",
    "dc_dc_converter": "dc_dc_charger",
    "battery_protect": "battery_protect",  # Victron Smart BatteryProtect (BLE)
    "gx_gateway": "gx_gateway",
    "dc_meter": "dc_meter",              # Sensa DC Meter 6 (INA226 per circuit)
    "bitcoin_miner": "bitcoin_miner",  # Lucky Miner LV08 Pro (AxeOS HTTP API)
    "power_station": "power_station",  # DJI Power 1000 V2 (portable power station)
    "leisure_battery": "battery",
    "air_heater": "air_heater",
    # Legacy: rows persisted before the 2026-07-08 rename carry the old kind.
    "diesel_heater": "air_heater",
    "hydronic_heater": "hydronic_heater",  # Alde Compact 4000 (wet/glycol heating)
    "van_level": "levelling",            # Sensa Van Level Pro 6 (IMU tilt sensor)
    "obd2": "obd2",                      # MeatPi WiCAN Pro (OBD2 engine telemetry)
    "air_conditioner": "air_conditioner",  # Velit Rooftop AC (BLE GATT climate)
    "speaker": "speaker",                # Sonos Roam 2 (cooperate-flow media_player)
    "voice_satellite": "voice_satellite",  # Home Assistant Voice PE (adopted ESPHome)
    "fan_controller": "fan_controller",  # Sensa Fan Temperature Control (ESPHome PWM fans)
    "dial": "dial",                      # Sensa Dial (M5Stack Dial knob, `dial.py`)
    # Zigbee2MQTT devices persist their service class as `role`; map each back
    # so a re-read of the registered row re-classifies (classify step 3). The
    # per-device class can't come from the integration slug — one integration
    # (zigbee2mqtt) spans lighting / environment / security.
    "lighting": "lighting",
    "motion_sensor": "motion_sensor",
    "contact_sensor": "contact_sensor",
    "leak_sensor": "leak_sensor",
    "vibration_sensor": "vibration_sensor",
    "smoke_sensor": "smoke_sensor",      # Shelly Plus Smoke (adopted `shelly` entry)
    "scene_button": "scene_button",
}

# Venus OS service name (Victron Cerbo MQTT fanout) -> service_class.
# Mirrors the service keys in `victron_mqtt_services.VENUS_SERVICE_TEMPLATES`;
# a test pins the two in sync.
VENUS_SERVICE_TO_SERVICE_CLASS: dict[str, str] = {
    "battery": "battery_monitor",
    "solarcharger": "solar_charger",
    "vebus": "inverter_charger",
    "alternator": "dc_dc_charger",
    "dcdc": "dc_dc_charger",
    "dcsystem": "dc_system",
    "grid": "grid_meter",
    "pvinverter": "pv_inverter",
    "genset": "generator",
    "tank": "tank",
    "temperature": "temperature_sensor",
}

# Integration slug -> service_class, for rows that carry neither a catalog
# `type` nor a Venus service (last-resort fallback).
#
# This fallback is load-bearing, not cosmetic: the Discovery picker's *rendered*
# rows (`SensaDiscoverySensor`) drop `device_type`/`vendor_device_type`, so when
# a device is added `_async_register_single_device` stores `role="unknown"` in
# the backend (and the backend sets `device_type=role`). Every device therefore
# reaches `classify()` with no `type`/`role` signal and relies on this slug map
# to be categorised at all. The Sensa-owned vendors below were covered; the
# cooperate-flow BLE environment sensors (Govee / Ruuvi) were NOT — so they
# classified as None, were skipped by the environment-category filters in
# `dashboard._env_chip_name_overrides` / `_environment_sensor_entities`, and a
# Discovery rename never reached the temperature header chip (the chip itself
# still showed, because it's resolved by integration *domain*, not class). Keep
# this in sync with the catalog's environment `accept_native` domains — a test
# pins the two together.
_INTEGRATION_TO_SERVICE_CLASS: dict[str, str] = {
    "sensa_bms_ble": "battery",
    "sensa_teltonika_api": "router",
    "sensa_starlink_grpc": "satellite_internet",
    "sensa_victron_mqtt": "gx_gateway",
    "sensa_victron_orion_xs": "dc_dc_charger",
    "sensa_velit_rooftop_ac": "air_conditioner",
    "sensa_luckyminer_api": "bitcoin_miner",
    "sensa_dji_ble": "power_station",
    "sensa_tpms_ble": "tyre_pressure",
    "sensa_rtsp_camera": "ip_camera",
    # Asustor NAS — backend rows carry integration_type `sensa_asustor_ssh`;
    # the rendered Discovery row drops the catalog `type`, so this slug
    # fallback is what classifies it.
    "sensa_asustor_ssh": "nas",
    # WiCAN OBD2 adapter — HA's mqtt integration owns the device; the backend
    # row carries integration_type `wican`, so the slug fallback classifies it.
    "wican": "obd2",
    # Cooperate-flow BLE environment sensors owned by HA's native integration.
    "govee_ble": "environment_sensor",
    "ruuvitag_ble": "environment_sensor",
    # Mopeka bottle sensor owned by HA's native `mopeka` integration
    # (`mopeka.py` adds the Bottle Size select and the % Tank Level).
    "mopeka": "tank",
    # Cooperate-flow network speaker owned by HA's native `sonos` integration.
    "sonos": "speaker",
    # Zigbee coordinators. The rendered Discovery row drops the catalog `type`,
    # so an ADDED coordinator lands in the backend with `role: "unknown"` and
    # classified as nothing at all — verified on the dev box 2026-09-18, where a
    # freshly-added Dongle-M read
    # `{"device_type": "unknown", "role": "unknown", "model": null}`.
    # `zigbee_coordinator_present`'s backend clause was written for exactly this
    # device and could never match it; it only ever answered True through its
    # liveness clause, i.e. only while Z2M was already running.
    "sonoff_dongle_m": "zigbee_coordinator",
    "smlight": "zigbee_coordinator",
}


def category_of(service_class: str) -> str | None:
    """Return the view-routing category for a service-class key, or None
    when the key is unknown."""
    sc = SERVICE_CLASSES.get(service_class)
    return sc.category if sc else None


def service_classes_in_category(category: str) -> list[str]:
    """All service-class keys that roll up into `category`, in registry
    order. Views use this to know which devices they render."""
    return [sc.key for sc in _SERVICE_CLASS_LIST if sc.category == category]


def device_kind(device: dict[str, Any]) -> str:
    """A device row's catalog kind: the first of `vendor_device_type` /
    `device_type` / `type` / `role` that is set, else "".

    Which field carries it depends on how the row was registered: an ESPHome
    confirm leaves `vendor_device_type`, a network-scan add (`static_probe`:
    the Shelly, the Lucky Miner) sends only `role`. Reading three of the four
    in a dozen places is how a Shelly Pro 4PM registered with `role: relay`
    got no Switches tab. One reader, used everywhere.

    `"unknown"` is skipped, not returned: the backend fills the fields it has
    no answer for with it, so a row can read
    `{"device_type": "unknown", "role": "relay"}`. Taking the first field with
    ANY value in it stopped at "unknown" and never reached the answer, which
    is how the same Shelly still had no tab after the first fix.
    """
    for key in ("vendor_device_type", "device_type", "type", "role"):
        value = str(device.get(key) or "").strip()
        if value and value != "unknown":
            return value
    return ""


def is_relay_device(device: dict[str, Any]) -> bool:
    """Whether a device row is a relay controller (a Sensa relay board or an
    adopted Shelly), however it was registered.

    THREE ways to answer, because which one a row carries depends entirely on
    how it was added, and a relay that answers none of them gets no Switches
    tab on either dashboard:

      1. the stamped `service_class`,
      2. ANY of the four kind fields (the others are routinely "unknown":
         an ESPHome confirm fills `vendor_device_type`, a network-scan add
         sends `role`),
      3. its catalog entry's service class, by `catalog_id` -- the last
         resort, and the one field a registered row always has, since
         registration sends it for the entitlement rules.

    A Shelly Pro 4PM added by the `/shelly` sweep is the device this exists
    for: it was registered, listed, and still had no Switches tab.
    """
    if str(device.get("service_class") or "").strip() == "relay":
        return True
    if any(
        str(device.get(key) or "").strip() == "relay"
        for key in ("vendor_device_type", "device_type", "type", "role")
    ):
        return True
    catalog_id = str(device.get("catalog_id") or "").strip()
    if not catalog_id:
        return False
    from ..device_catalog import PHYSICAL_DEVICE_CATALOG

    return any(
        entry.get("id") == catalog_id and entry.get("service_class") == "relay"
        for entry in PHYSICAL_DEVICE_CATALOG
    )


def classify(device: dict[str, Any]) -> str | None:
    """Normalise a device row to its canonical service-class key.

    Resolution order, most authoritative first:
      1. `service_class` already on the row — backend-persisted (end state)
         or attached by `_attach_service_class_rows` for Cerbo sub-devices.
      2. `victron_service` — a Venus OS service name on a Cerbo fanout row.
      3. Device-catalog kind — `vendor_device_type` / `device_type` / `type`
         / `role` (the same fields the backend and the rest of the
         integration use to describe a device's kind).
      4. Integration slug — last-resort fallback.

    Returns None when nothing matches; callers treat that as "uncategorised,
    do not place on a view" rather than crashing.
    """
    # 1. Already classified. A backend-persisted row written before a service
    #    class was renamed still carries the old key, so map it forward first —
    #    otherwise the device silently stops matching its card's gate (the
    #    heater would vanish off the Climate tab until re-added).
    existing = device.get("service_class")
    existing = _LEGACY_SERVICE_CLASS_ALIASES.get(existing, existing)
    if existing and existing in SERVICE_CLASSES:
        return existing

    # 2. Venus OS service name (Cerbo sub-device fanout row).
    venus = device.get("victron_service")
    if venus:
        mapped = VENUS_SERVICE_TO_SERVICE_CLASS.get(str(venus).lower())
        if mapped:
            return mapped

    # 3. Device-catalog kind vocabulary. `role` is included because the
    #    backend stamps it on rows (e.g. a Teltonika `role: switch`) and
    #    it is part of the same kind vocabulary as `type`.
    for field in ("vendor_device_type", "device_type", "type", "role"):
        raw = device.get(field)
        if raw:
            mapped = CATALOG_TYPE_TO_SERVICE_CLASS.get(str(raw).lower())
            if mapped:
                return mapped

    # 4. Integration slug fallback.
    integration = (
        device.get("integration_type") or device.get("integration") or ""
    )
    return _INTEGRATION_TO_SERVICE_CLASS.get(str(integration).lower())
