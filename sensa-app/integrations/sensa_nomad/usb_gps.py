"""A USB GPS puck plugged into the box, as a Vehicle GPS source.

For a van whose router has no GPS (a Teltonika RUT200, say). The customer plugs
a u-blox USB receiver (VK-162, VK-172 and similar) into the box, and within one
scan it becomes "USB GPS" on the vehicle tracker, behind router GPS in
`device_tracker._GPS_SOURCES`.

Only u-blox (USB vendor 1546) is supported. Those enumerate as a CDC ACM port
(``ttyACM``) and speak NMEA at 1 Hz with no configuration. Cheaper pucks built
on a CH340 or PL2303 bridge are deliberately NOT matched: those vendor ids are
generic serial adapters, and opening one could be somebody's Zigbee stick or
heater cable.

The sensors are added the first time a puck is seen, not at setup, because
`van_gps_available` asks the entity registry whether a GPS EXISTS: creating
them on every box would tell a van with no GPS that it has one. Once
registered they stay, like a router's, and read unavailable while unplugged.

Everything that is not I/O is a pure function: :func:`parse_rmc`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, SIGNAL_DASHBOARD_REGEN_REQUESTED
from .usb_serial import ensure_dev_node, matching_ttys, usb_product

_LOGGER = logging.getLogger(__name__)

GPS_USB_VENDORS = frozenset({"1546"})  # u-blox
SCAN_INTERVAL = timedelta(seconds=30)
# u-blox sends a sentence every second; this many silent one-second reads with
# the port still open means it has stopped answering, so the fix is withdrawn.
_SILENT_READS_BEFORE_NO_FIX = 5
_KNOTS_TO_KMH = 1.852

# The vendor segment `device_tracker._GPS_SOURCES` matches on, and the one
# device this supports (a van has one puck).
VENDOR = "usb_gps"
DEVICE_ID = "puck"
SIGNAL_USB_GPS_UPDATED = "sensa_nomad_usb_gps_updated"

# key -> (name, km/h unit or None). The keys are the ones the tracker pairs.
_SENSORS: dict[str, tuple[str, str | None]] = {
    "gps_latitude": ("GPS Latitude", None),
    "gps_longitude": ("GPS Longitude", None),
    "gps_speed": ("GPS Speed", "km/h"),
}


def usb_gps_unique_id(entry_id: str, key: str) -> str:
    """Shaped like `teltonika_unique_id`, which `_find_gps_pair` relies on."""
    return f"{entry_id}_{VENDOR}_{DEVICE_ID}_{key}"


Fix = dict[str, float]


def _checksum_ok(sentence: str) -> bool:
    body, sep, given = sentence.partition("*")
    if not sep or len(given) < 2:
        return False
    total = 0
    for ch in body:
        total ^= ord(ch)
    try:
        return total == int(given[:2], 16)
    except ValueError:
        return False


def _coord(value: str, hemi: str, degree_digits: int) -> float | None:
    """NMEA ``ddmm.mmmm`` (or ``dddmm.mmmm``) plus hemisphere, to degrees."""
    if len(value) <= degree_digits or hemi not in ("N", "S", "E", "W"):
        return None
    try:
        degrees = int(value[:degree_digits]) + float(value[degree_digits:]) / 60
    except ValueError:
        return None
    return -degrees if hemi in ("S", "W") else degrees


def parse_rmc(line: str) -> Fix | None:
    """A fix from one RMC sentence, or None when it is not a usable fix.

    Any talker (``$GPRMC`` from a u-blox 7, ``$GNRMC`` from an 8 tracking
    several constellations). Status ``V`` is the receiver saying it has no fix
    yet, and the coordinates it sends alongside are stale or empty.
    """
    line = line.strip()
    if not line.startswith("$") or len(line) < 7 or line[3:6] != "RMC":
        return None
    if not _checksum_ok(line[1:]):
        return None
    fields = line[1:].split("*", 1)[0].split(",")
    if len(fields) < 8 or fields[2] != "A":
        return None
    lat = _coord(fields[3], fields[4], 2)
    lon = _coord(fields[5], fields[6], 3)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    try:
        speed_kmh = round(float(fields[7]) * _KNOTS_TO_KMH, 1) if fields[7] else 0.0
    except ValueError:
        speed_kmh = 0.0
    return {
        "gps_latitude": round(lat, 7),
        "gps_longitude": round(lon, 7),
        "gps_speed": speed_kmh,
    }


def find_gps_port() -> tuple[str | None, str | None, str | None]:
    """(device path, product, problem). Blocking: call from an executor."""
    try:
        names = matching_ttys(GPS_USB_VENDORS)
    except OSError:
        return None, None, None
    for name in names:
        path, err = ensure_dev_node(name)
        if err is not None:
            return None, None, f"{name}: {err}"
        return path, usb_product(name), None
    return None, None, None


class _Reader:
    """Reads one open port on its own thread until stopped or unplugged.

    ``on_fix(fix_or_None)`` and ``on_closed()`` are called from that thread.
    """

    def __init__(
        self,
        port: str,
        on_fix: Callable[[Fix | None], None],
        on_closed: Callable[[], None],
    ) -> None:
        self._port = port
        self._on_fix = on_fix
        self._on_closed = on_closed
        self._serial: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def open(self) -> None:
        """Blocking. Baud is ignored by a CDC ACM receiver; 9600 is NMEA's."""
        import serial  # pyserial ships with Home Assistant (the usb integration)

        self._serial = serial.Serial(self._port, baudrate=9600, timeout=1, exclusive=True)
        self._thread = threading.Thread(target=self._run, name="sensa-usb-gps", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Blocking: call from an executor."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._close()

    def _close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                pass
            self._serial = None

    def _run(self) -> None:
        silent = 0
        had_fix = False
        try:
            while not self._stop.is_set():
                raw = self._serial.readline()
                if not raw:
                    silent += 1
                    if silent >= _SILENT_READS_BEFORE_NO_FIX and had_fix:
                        had_fix = False
                        self._on_fix(None)
                    continue
                silent = 0
                line = raw.decode("ascii", errors="ignore")
                if line[3:6] != "RMC":
                    continue
                fix = parse_rmc(line)
                # Every RMC is news: a V after an A is the fix being lost.
                if fix is not None or had_fix:
                    self._on_fix(fix)
                had_fix = fix is not None
        except Exception as err:  # noqa: BLE001 - the puck was pulled, mostly
            if not self._stop.is_set():
                _LOGGER.info("USB GPS stopped: %s", err)
        finally:
            self._close()
            if not self._stop.is_set():
                self._on_closed()


class UsbGps:
    """Finds the puck, keeps it open, and holds the latest fix for the sensors."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._add_entities = add_entities
        self._reader: _Reader | None = None
        self._opening = False
        self._entities_added = False
        self._last_problem: str | None = None
        self.connected = False
        self.product: str | None = None
        self.fix: Fix | None = None

    def value(self, key: str) -> float | None:
        return (self.fix or {}).get(key)

    @callback
    def async_start(self) -> None:
        # A puck seen in an earlier session already has registry rows; add the
        # entities now so they come back (unavailable) rather than orphaned.
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(self.hass)
        lat_uid = usb_gps_unique_id(self._entry.entry_id, "gps_latitude")
        if ent_reg.async_get_entity_id("sensor", DOMAIN, lat_uid):
            self._add_sensors()

        self._entry.async_on_unload(
            async_track_time_interval(self.hass, self._async_scan, SCAN_INTERVAL)
        )
        self._entry.async_on_unload(self._async_stop)
        self.hass.async_create_task(self._async_scan())

    @callback
    def _add_sensors(self) -> None:
        if self._entities_added:
            return
        self._entities_added = True
        self._add_entities(UsbGpsSensor(self, self._entry, key) for key in _SENSORS)

    async def _async_scan(self, *_args: Any) -> None:
        if self._reader is not None or self._opening:
            return
        self._opening = True
        try:
            await self._async_try_open()
        finally:
            self._opening = False

    async def _async_try_open(self) -> None:
        port, product, problem = await self.hass.async_add_executor_job(find_gps_port)
        if port is None:
            self._report_problem(problem)
            return
        reader = _Reader(port, self._fix_from_thread, self._closed_from_thread)
        try:
            await self.hass.async_add_executor_job(reader.open)
        except Exception as err:  # noqa: BLE001 - reported once, retried each scan
            self._report_problem(f"{port}: {err}")
            return
        self._report_problem(None)
        self._reader = reader
        self.product = product
        self.connected = True
        _LOGGER.info("USB GPS connected on %s (%s)", port, product or "unknown model")
        first_ever = not self._entities_added
        self._add_sensors()
        if first_ever:
            # The geofence card and the Cab speed dial are built only when the
            # van has a GPS, so the dashboard needs to see this one.
            async_dispatcher_send(self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED)
        async_dispatcher_send(self.hass, SIGNAL_USB_GPS_UPDATED)

    def _report_problem(self, problem: str | None) -> None:
        """Log a puck that is there but cannot be opened, on the edge only."""
        if problem and problem != self._last_problem:
            _LOGGER.warning(
                "A USB GPS is plugged in but Home Assistant cannot open it (%s). "
                "The box may need the current stack update.",
                problem,
            )
        self._last_problem = problem

    def _fix_from_thread(self, fix: Fix | None) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_fix, fix)

    def _closed_from_thread(self) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_closed)

    @callback
    def _apply_fix(self, fix: Fix | None) -> None:
        if self._reader is None:
            return
        self.fix = fix
        async_dispatcher_send(self.hass, SIGNAL_USB_GPS_UPDATED)

    @callback
    def _apply_closed(self) -> None:
        self._reader = None
        self.connected = False
        self.fix = None
        _LOGGER.info("USB GPS disconnected")
        async_dispatcher_send(self.hass, SIGNAL_USB_GPS_UPDATED)

    @callback
    def _async_stop(self) -> None:
        reader, self._reader = self._reader, None
        self.connected = False
        if reader is not None:
            self.hass.async_add_executor_job(reader.stop)


class UsbGpsSensor(SensorEntity):
    """One of the puck's three readings, shaped like the router's GPS sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:crosshairs-gps"

    def __init__(self, gps: UsbGps, entry: ConfigEntry, key: str) -> None:
        name, unit = _SENSORS[key]
        self._gps = gps
        self._key = key
        self._attr_name = name
        self._attr_unique_id = usb_gps_unique_id(entry.entry_id, key)
        if unit is not None:
            self._attr_native_unit_of_measurement = unit
            self._attr_device_class = SensorDeviceClass.SPEED
            self._attr_icon = "mdi:speedometer"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{VENDOR}_{DEVICE_ID}")},
            "name": "USB GPS",
            "manufacturer": "u-blox",
            "model": self._gps.product or "USB GPS receiver",
        }

    @property
    def available(self) -> bool:
        return self._gps.connected

    @property
    def native_value(self) -> float | None:
        return self._gps.value(self._key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _refresh() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_USB_GPS_UPDATED, _refresh)
        )


@callback
def async_setup_usb_gps(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> UsbGps:
    """Start watching for a puck. Called from the sensor platform's setup."""
    gps = UsbGps(hass, entry, add_entities)
    gps.async_start()
    return gps
