"""The van network layouts (`network_profile*.json`), each read once.

A layout is data: segments, subnets, VLAN IDs, which zone may reach the
internet. The provisioner writes it onto the router; the Networks card on the
Habitation Network tab reads the SAME file to name and describe what it shows,
so the two can never disagree about what "IoT" or "Guest" is.

Two layouts, chosen per router by its Network Setup select
(`layout_for_device`): `secure` (VLANs) and `single` (one network). A router
that is Self managed has no layout; its Networks card lists the router's own
LANs.

Read lazily and cached. The first read is a file open, which HA flags inside
its event loop, so setup calls `async_preload` (an executor job) before any
entity reads it.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any

LAYOUT_FILES = {
    "secure": "network_profile.json",
    "single": "network_profile_single.json",
}

_PROFILES: dict[str, dict[str, Any]] = {}


def load_profile(layout: str = "secure") -> dict[str, Any]:
    """A layout, read from disk once. Blocking on the first call.

    An unknown layout (Self managed included) reads the secure one: callers
    that must not use a layout for a Self managed router check
    `layout_for_device` first.
    """
    name = LAYOUT_FILES.get(layout, LAYOUT_FILES["secure"])
    if name not in _PROFILES:
        _PROFILES[name] = json.loads(
            (Path(__file__).parent / name).read_text(encoding="utf-8")
        )
    return _PROFILES[name]


async def async_preload(hass: Any) -> None:
    """Warm the cache off the event loop."""
    for layout in LAYOUT_FILES:
        await hass.async_add_executor_job(load_profile, layout)


def layout_for_device(entry: Any, device_id: str) -> str:
    """`not_set`, `self`, `single` or `secure`: the router's Network Setup.

    THE one reading of the option. Unset or unknown is `not_set`, which every
    caller treats as Self managed (it is not in LAYOUT_FILES).
    """
    from .const import ROUTER_SETUP_LABELS, ROUTER_SETUP_OPTION, ROUTER_SETUP_UNSET

    chosen = ((getattr(entry, "options", None) or {}).get(ROUTER_SETUP_OPTION) or {}).get(
        device_id,
    )
    return chosen if chosen in ROUTER_SETUP_LABELS else ROUTER_SETUP_UNSET


def _in(ip: str | None, subnet: str | None) -> bool:
    if not ip or not subnet:
        return False
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return False


def wan_kind(w: dict[str, Any]) -> str:
    """What a WAN is: `starlink`, `mobile`, `wifi` or `wired`.

    ONE rule for every card that names a WAN's art and grade (the Networks
    card's rows and the WAN Priority sensor's list). Starlink is recognised
    by the router's address sitting in the dish's LAN subnet, or by its name;
    a SIM by its mobile protocol.
    """
    wan_subnet = load_profile().get("starlink", {}).get("expected_wan_subnet")
    proto = (w.get("proto") or "").lower()
    if _in(w.get("ip"), wan_subnet) or "starlink" in (w.get("name") or "").lower():
        return "starlink"
    if proto in _MOBILE_PROTOS:
        return "mobile"
    if (w.get("type") or "") == "wifi":
        return "wifi"
    return "wired"


def _own_segments(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The router's own LANs as segments, for a Self managed router.

    Named as the router names them; a LAN with no usable netmask is skipped
    rather than guessed. Internet access is not known, so it is left None.
    """
    segs = []
    for n in networks:
        if not n.get("enabled", True):
            continue
        try:
            net = ipaddress.ip_network(f"{n.get('ip')}/{n.get('netmask')}", strict=False)
        except ValueError:
            continue
        segs.append({
            "key": n.get("id") or str(net),
            "label": n.get("name") or n.get("id") or str(net),
            "icon": "mdi:lan",
            "subnet": str(net),
            "router_ip": n.get("ip"),
            "vlan_id": None,
            "purpose": None,
            "own": True,
        })
    return segs


def network_rows(
    data: dict[str, Any], layout: str = "secure",
) -> list[dict[str, Any]] | None:
    """One row per network for the Networks card, from a router poll.

    `layout` is the router's Network Setup (`layout_for_device`). Self managed
    (`self`) lists the router's own LANs. Otherwise every profile
    segment is listed, set up on this router or not, so a van
    that has not been provisioned yet shows what it will have rather than an
    unexplained gap. The WANs lead, one row each in failover order (Starlink,
    SIM 1, SIM 2, an eSIM, a wired uplink; `wan: True`), then the LAN
    segments in subnet order. Clients are the DHCP
    leases whose address sits in the segment's subnet, split wired / wireless
    by whether the lease's address or MAC is associated to a Wi-Fi AP; a
    Wi-Fi client with a fixed address (no lease) is counted as wireless, a
    wired one with a fixed address cannot be seen and is not counted.
    `wired` / `wireless` are None when the Wi-Fi read failed (no split).

    None when the router has not answered the lease read ("could not ask"),
    never an empty list, which would read as "no networks".
    """
    leases = data.get("lan_leases")
    networks = data.get("lan_networks")
    if not isinstance(leases, list) or not isinstance(networks, list):
        return None
    if layout not in LAYOUT_FILES:
        segments = _own_segments(networks)
        forwarding: dict[str, Any] = {}
    else:
        profile = load_profile(layout)
        segments = profile.get("segments", [])
        forwarding = profile.get("forwarding", {})
    wifi = data.get("lan_wifi_clients")
    rows: list[dict[str, Any]] = []

    # WANs, in failover order (`wan_interfaces` is sorted by metric).
    for priority, w in enumerate(data.get("wan_interfaces") or [], start=1):
        rows.append({
            "key": w.get("id"),
            "label": w.get("name") or w.get("id"),
            "wan": True,
            "kind": wan_kind(w),
            "priority": priority,
            "up": bool(w.get("up")),
            "address": w.get("ip"),
        })

    for seg in segments:
        subnet = seg.get("subnet")
        present = seg.get("own") or any(
            n.get("ip") == seg.get("router_ip") and n.get("enabled", True)
            for n in networks
        )
        in_seg = [lease for lease in leases if _in(lease.get("ip"), subnet)]
        devices = [
            lease.get("hostname") or lease.get("mac") or lease.get("ip")
            for lease in in_seg
        ]
        wired_devices: list[str] | None = None
        wireless_devices: list[str] | None = None
        if isinstance(wifi, list):
            wifi_ips = {c.get("ip") for c in wifi if c.get("ip")}
            wifi_macs = {(c.get("mac") or "").upper() for c in wifi if c.get("mac")}
            wired_devices, wireless_devices = [], []
            for lease, name in zip(in_seg, devices):
                on_wifi = lease.get("ip") in wifi_ips or (
                    (lease.get("mac") or "").upper() in wifi_macs
                )
                (wireless_devices if on_wifi else wired_devices).append(name)
            leased_ips = {lease.get("ip") for lease in in_seg}
            for c in wifi:
                if _in(c.get("ip"), subnet) and c.get("ip") not in leased_ips:
                    name = c.get("hostname") or c.get("mac") or c.get("ip")
                    wireless_devices.append(name)
                    devices.append(name)
        rows.append({
            "key": seg["key"],
            "label": seg.get("label", seg["key"]),
            "icon": seg.get("icon", "mdi:lan"),
            "subnet": subnet,
            "vlan_id": seg.get("vlan_id"),
            "purpose": seg.get("purpose"),
            "wan": False,
            "present": present,
            "address": seg.get("router_ip"),
            "internet": None if seg.get("own") else "wan" in forwarding.get(seg.get("zone"), []),
            "clients": len(devices) if present else None,
            "devices": devices if present else [],
            "wired": len(wired_devices) if present and wired_devices is not None else None,
            "wireless": (
                len(wireless_devices) if present and wireless_devices is not None else None
            ),
            "wired_devices": (wired_devices or []) if present else [],
            "wireless_devices": (wireless_devices or []) if present else [],
        })
    # LANs in subnet order (.1 Secure, .2 IoT, .4 CCTV, .5 Guest); the WANs
    # keep their failover order ahead of them.
    wans = [r for r in rows if r.get("wan")]
    lans = sorted((r for r in rows if not r.get("wan")), key=lambda r: _network_key(r.get("subnet")))
    return wans + lans


# RutOS protocols of a cellular WAN (a SIM or an eSIM profile).
_MOBILE_PROTOS = frozenset({"wwan", "qmi", "qmux", "ncm", "mbim", "3g"})


def _network_key(subnet: str | None) -> tuple[int, int]:
    """Sort key: a subnet's network address, unparseable last."""
    try:
        return (0, int(ipaddress.ip_network(subnet or "", strict=False).network_address))
    except ValueError:
        return (1, 0)
