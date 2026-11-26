"""Keep HA advertising the van's LAN address, never a Docker bridge.

The Home Assistant phone app finds a box by the `_home-assistant._tcp` mDNS
advert, and connects to the address in it. HA builds that advert from its
network integration's ENABLED adapters, and decides which to enable once, at
start: it looks for the adapter that routes to the mDNS multicast address and
enables that one. If the lookup fails (the LAN was not up yet when HA started,
or the router hands out no gateway), HA falls back to enabling EVERY adapter
with a usable address. On the Container appliance HA runs `network_mode: host`,
so that includes `docker0` (172.17.0.1) and the compose bridges, and a customer
setting up the app was offered `http://172.17.0.1:8124`, which no phone can
reach.

The fix is HA's own setting, Settings > System > Network > Network adapter:
pin the adapters to the LAN ones. HA honours a stored adapter list by NAME, so
the pin holds on every later boot whether or not the lookup works. It is only
written while HA is still on automatic (an empty list), so a choice the customer
made in that screen is never overwritten.

HA registers the advert once at start, so when the advert running NOW carries a
non-LAN address the box is restarted once to re-announce. That cannot loop: after
the restart the stored list enables only the LAN adapters.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Container plumbing and VPN tunnels: never the network a phone is on. Matched
# by adapter NAME for the reason given on `_NON_LAN_ADAPTER_PREFIXES` in
# __init__.py (a real LAN may use 172.16/12). `br-` keeps its dash: Docker's
# compose bridges are `br-<hex>`, a hand-made bridge `br0` is a real LAN.
_NON_LAN_PREFIXES = (
    "docker",
    "br-",
    "veth",
    "hassio",
    "lo",
    "tailscale",
    "wg",
    "tun",
    "zt",
)


def _is_lan_adapter(adapter: dict[str, Any]) -> bool:
    """An adapter a phone on the van's network could reach the box through."""
    name = str(adapter.get("name") or "").lower()
    if not name or name.startswith(_NON_LAN_PREFIXES):
        return False
    for ipv4 in adapter.get("ipv4") or []:
        try:
            address = ipaddress.ip_address(ipv4.get("address"))
        except (TypeError, ValueError):
            continue
        if not (address.is_loopback or address.is_link_local):
            return True
    return False


def plan(
    adapters: list[dict[str, Any]], configured: list[str]
) -> tuple[list[str] | None, bool]:
    """Decide what to store and whether the running advert is wrong.

    Returns `(pin, restart)`: `pin` is the adapter list to store, or None to
    leave HA's setting alone; `restart` is True when an enabled adapter is not
    a LAN one, i.e. the advert on the air right now is offering it.
    """
    if configured:
        return None, False  # the customer chose; theirs stands
    lan = [str(a["name"]) for a in adapters if _is_lan_adapter(a)]
    if not lan:
        return None, False  # LAN not up yet; try again next start
    advertising_non_lan = any(
        a.get("enabled") and not _is_lan_adapter(a) and (a.get("ipv4") or a.get("ipv6"))
        for a in adapters
    )
    return lan, advertising_non_lan


async def async_pin_lan_adapters(hass: HomeAssistant) -> None:
    """Pin HA's network adapters to the LAN, restarting once if needed."""
    try:
        from homeassistant.components import network
        from homeassistant.components.network.network import async_get_network

        net = await async_get_network(hass)
        adapters = await network.async_get_adapters(hass)
        pin, restart = plan(list(adapters), list(net.configured_adapters))
        if pin is None:
            return
        await net.async_reconfig({"configured_adapters": pin})
    except Exception:
        _LOGGER.warning("Could not pin HA's network adapters to the LAN", exc_info=True)
        return

    _LOGGER.info("Pinned HA's network adapters to %s", ", ".join(pin))
    if restart:
        _LOGGER.warning(
            "HA was advertising a Docker address to the phone app; restarting "
            "once so it advertises %s only",
            ", ".join(pin),
        )
        await hass.services.async_call("homeassistant", "restart")
