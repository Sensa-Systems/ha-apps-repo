"""The box's own Wi-Fi, through NetworkManager on the host.

Configure Router's Fix renames the van Wi-Fi, and most boxes are on it. A box
does not join a renamed network by itself, so before the rename Fix gives the
box the new name (and password) as a second saved network, keeping the old
one. After the router restarts the box joins whichever exists: the new one if
Fix worked, the old one if the backup was put back.

The host's Wi-Fi is NetworkManager's (Comitup, the box's Wi-Fi setup, is built
on it). Home Assistant reaches the host's system D-Bus through the `/run/dbus`
mount it already has for Bluetooth, and talks to NetworkManager there with
dbus-fast, which Home Assistant ships. Not yet run on a box: whether
NetworkManager is the one running the box's Wi-Fi on every image, and whether
it lets HA's container read the saved password and add a network.
"""

from __future__ import annotations

import uuid
from typing import Any

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
WIFI = "802-11-wireless"
SECURITY = "802-11-wireless-security"


class BoxWifiError(Exception):
    """The box's Wi-Fi could not be read or changed."""


async def _bus() -> Any:
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError as err:
        raise BoxWifiError("no D-Bus library") from err
    try:
        return await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as err:  # noqa: BLE001 - reported to the caller
        raise BoxWifiError(f"cannot reach the box's system bus: {err}") from err


async def _iface(bus: Any, path: str, name: str) -> Any:
    intro = await bus.introspect(NM, path)
    return bus.get_proxy_object(NM, path, intro).get_interface(name)


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


async def _wifi_connection(bus: Any) -> tuple[str, dict[str, Any]] | None:
    """(settings path, settings) of the Wi-Fi connection the box is on, or None."""
    nm = await _iface(bus, NM_PATH, NM)
    for active in await nm.get_active_connections():
        ac = await _iface(bus, active, f"{NM}.Connection.Active")
        if await ac.get_type() != WIFI:
            continue
        path = await ac.get_connection()
        conn = await _iface(bus, path, f"{NM}.Settings.Connection")
        return path, await conn.call_get_settings()
    return None


async def box_wifi_ssid() -> str | None:
    """The Wi-Fi network the box is on, or None when it is not on Wi-Fi.

    Raises BoxWifiError when NetworkManager cannot be asked, which is not the
    same answer as "not on Wi-Fi".
    """
    bus = await _bus()
    try:
        found = await _wifi_connection(bus)
    except Exception as err:  # noqa: BLE001 - could not ask
        raise BoxWifiError(f"NetworkManager did not answer: {err}") from err
    finally:
        bus.disconnect()
    if found is None:
        return None
    ssid = _plain(found[1].get(WIFI, {}).get("ssid"))
    return bytes(ssid).decode(errors="replace") if ssid else None


async def add_box_wifi(new_ssid: str, new_password: str | None) -> None:
    """Save `new_ssid` on the box beside the network it is on now.

    A copy of the current connection with the new name, and the new password
    when it changes (else the current one). The current connection is kept, so
    the box can still join the old name if the router is put back.
    """
    from dbus_fast import Variant

    bus = await _bus()
    try:
        found = await _wifi_connection(bus)
        if found is None:
            raise BoxWifiError("the box is not on Wi-Fi")
        path, settings = found
        conn = await _iface(bus, path, f"{NM}.Settings.Connection")
        if SECURITY in settings:
            secrets = await conn.call_get_secrets(SECURITY)
            settings[SECURITY].update(secrets.get(SECURITY, {}))
        settings["connection"]["id"] = Variant("s", f"{new_ssid} (Sensa)")
        settings["connection"]["uuid"] = Variant("s", str(uuid.uuid4()))
        settings["connection"]["autoconnect"] = Variant("b", True)
        settings[WIFI]["ssid"] = Variant("ay", new_ssid.encode())
        if new_password:
            settings.setdefault(SECURITY, {})["key-mgmt"] = Variant("s", "wpa-psk")
            settings[SECURITY]["psk"] = Variant("s", new_password)
        nm_settings = await _iface(bus, SETTINGS_PATH, f"{NM}.Settings")
        await nm_settings.call_add_connection(settings)
    except BoxWifiError:
        raise
    except Exception as err:  # noqa: BLE001 - reported to the caller
        raise BoxWifiError(f"could not save {new_ssid} on the box: {err}") from err
    finally:
        bus.disconnect()
