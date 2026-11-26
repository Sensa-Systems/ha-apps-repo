"""Compare a RutOS router's live config with the Sensa van network layout.

Read over the router's REST API as `admin`, the account Sensa connects
with. SSH is no use for this: `admin` is an ordinary user (uid 2) who
cannot read /etc/config, and root's password is not the admin password on
a router whose owner has changed it (both measured on BP-RUTX11, RutOS
00.07.24, 2026-10-01). The REST config endpoints below all answered 200 as
`admin` on that router.

The REST sections are uci sections with REST field names, and a network is
referred to by its NAME (`IoT_Zone`), not its section id. A segment is
matched to the router by what it IS, its router address, not by a name,
so a router somebody configured by hand is compared on substance.

Wired sockets are planned from the router's own sockets and the customer's
count of wired devices per network (`socket_plan`). Switch VLANs are not
readable over REST (every candidate endpoint on a RUTX11 answered 404), so
there the sockets report "unknown" rather than guessing.

Standard library only, like `teltonika_provision`, so the CLI can use it.
"""

from __future__ import annotations

import re
from typing import Any

# Snapshot key -> REST path, as `read_snapshot` fetches them.
ENDPOINTS = {
    "interfaces": "/interfaces/config",
    "dhcp": "/dhcp/servers/ipv4/config",
    "zones": "/firewall/zones/config",
    "rules": "/firewall/traffic_rules/config",
    "wifi": "/wireless/interfaces/config",
    "radios": "/wireless/devices/config",
    # GPS on/off. No `/config` suffix: `/gps/global` is the one path that
    # answered (BP-RUTX11, 2026-10-01); every `/gps/*/config` guess was 501.
    # A router with no GPS answers 404, which reads as {} and skips the card.
    "gps": "/gps/global",
}
# The LAN sockets, what is plugged into them and the DHCP leases naming it,
# read apart from ENDPOINTS because not every router has them (BP's RUTM11
# answers all three, 2026-10-31; a switch router has no bridge path).
BRIDGES = "/network/devices/bridge/config"
# 802.1q devices (`lan2.3`): a socket carrying a network tagged to a managed switch.
VLAN_DEVICES = "/interface_based_vlan/config"
PORTS = "/ports_settings/status"
LEASES = "/dhcp/leases/ipv4/status"
# DHCP reservations, {id, name, mac, ip}. Read on BP's RUTM11 (2026-11-02, none
# set); the write's fields are from Teltonika's API reference for 7.24.3.
STATIC_LEASES = "/dhcp/static_leases/ipv4/config"

_BAND = {"2g": "2.4 GHz", "5g": "5 GHz"}


def _list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _prefix(ip: str) -> str:
    return ip.rsplit(".", 1)[0]


def _on_off(value: Any) -> str:
    return {"1": "On", "0": "Off"}.get(str(value), "" if value is None else str(value))


def _row(setting: str, current: Any, target: Any, same: bool | None = None) -> dict[str, Any]:
    cur = "" if current is None else str(current)
    tgt = "" if target is None else str(target)
    ok = (cur == tgt) if same is None else same
    return {
        "setting": setting,
        "current": cur,
        "target": tgt,
        "status": "same" if ok else ("add" if cur == "" else "change"),
    }


def _section(area: str, title: str, rows: list[dict[str, Any]], *, nerd: bool,
             found: bool = True, segment: str = "") -> dict[str, Any]:
    if not found:
        status = "add"
    elif all(r["status"] == "same" for r in rows):
        status = "same"
    else:
        status = "change"
    return {"nerd": nerd, "area": area, "title": title, "status": status,
            "segment": segment, "rows": rows}


def _band_of(radio: dict[str, Any]) -> str:
    """`2g` or `5g`: the channel when it is a number, else the radio's mode.

    A new router ships both radios on channel `auto`, so the mode decides:
    `a` / `ac` (and `11a`) and any VHT width are 5 GHz only. Reading `ac` as
    2.4 GHz once checked a 5 GHz Wi-Fi against the 2.4 GHz name (BP router,
    2026-10-27).
    """
    if str(radio.get("band") or "").lower() in ("2g", "5g"):
        return str(radio["band"]).lower()
    try:
        return "2g" if int(str(radio.get("channel") or "0")) <= 14 else "5g"
    except ValueError:
        pass
    hwmode = str(radio.get("hwmode") or "").lower().removeprefix("11")
    if hwmode in ("a", "ac") or str(radio.get("htmode") or "").upper().startswith("VHT"):
        return "5g"
    return "2g"


def _interface_for(snap: dict[str, Any], router_ip: str) -> dict[str, Any] | None:
    """The interface holding a segment's router address.

    Two interfaces can claim one address (a hand-made bridge next to the
    WebUI's own, as on BP-RUTX11); the one a firewall zone uses wins,
    because that is the one traffic is actually policed on.
    """
    hits = [i for i in snap["interfaces"] if i.get("ipaddr") == router_ip]
    if len(hits) <= 1:
        return hits[0] if hits else None
    zoned = {n for z in snap["zones"] for n in _list(z.get("network"))}
    return next((i for i in hits if i.get("name") in zoned), hits[0])


class Resolved:
    """Every segment of a layout matched to the router's interface and zone.

    The check and Fix both read the router through this, so what the check
    says is wrong is exactly what Fix changes.
    """

    def __init__(self, profile: dict[str, Any], snap: dict[str, Any]) -> None:
        self.segments = profile.get("segments", [])
        self.iface_of: dict[str, dict[str, Any] | None] = {}
        self.zone_of: dict[str, dict[str, Any] | None] = {}
        for seg in self.segments:
            iface = _interface_for(snap, seg["router_ip"])
            self.iface_of[seg["key"]] = iface
            name = (iface or {}).get("name")
            self.zone_of[seg["key"]] = next(
                (z for z in snap["zones"] if name and name in _list(z.get("network"))), None
            )

    def net_name(self, seg: dict[str, Any]) -> str:
        return (self.iface_of[seg["key"]] or {}).get("name") or seg["key"]

    def zone_name(self, zone_key: str) -> str:
        if zone_key == "wan":
            return "wan"
        seg = next((s for s in self.segments if s["zone"] == zone_key), None)
        if seg is None:
            return zone_key
        return (self.zone_of[seg["key"]] or {}).get("name") or zone_key


def dhcp_for(snap: dict[str, Any], iface: dict[str, Any] | None) -> dict[str, Any] | None:
    """The DHCP server on an interface, found by the interface's NAME."""
    if iface is None:
        return None
    return next((d for d in snap["dhcp"] if d.get("interface") == iface.get("name")), None)


def pool(seg: dict[str, Any]) -> tuple[str, str, str]:
    """A segment's DHCP pool as the router stores it: start, end, lease."""
    dhcp = seg.get("dhcp") or {}
    prefix = _prefix(seg["router_ip"])
    start = int(dhcp.get("start", 50))
    end = start + int(dhcp.get("limit", 200)) - 1
    return f"{prefix}.{start}", f"{prefix}.{end}", str(dhcp.get("lease", "12h"))


def rule_ports(ports: list[Any]) -> set[str]:
    return {str(p) for p in ports}


def has_rule(rules: list[dict[str, Any]], src: str, ports: set[str], *,
             dest: str | None = None, all_ports: bool = False) -> bool:
    """An enabled ACCEPT rule from `src` (to `dest`) opening `ports`.

    Input exceptions need any of the ports, a zone exception all of them.
    """
    for r in rules:
        if r.get("src") != src or str(r.get("target")).upper() != "ACCEPT":
            continue
        if str(r.get("enabled", "1")) != "1" or (dest is not None and r.get("dest") != dest):
            continue
        opened = {p for v in _list(r.get("dest_port")) for p in re.split(r"[-: ]", v)}
        if (ports <= opened) if all_ports else bool(ports & opened):
            return True
    return False


def wifi_plan(profile: dict[str, Any], snap: dict[str, Any], identity: str,
              res: Resolved) -> list[dict[str, Any]]:
    """One entry per Wi-Fi network per band the router has a radio for.

    {ssid (the profile entry), seg, band, radio, name, network, have}; `have`
    is the router's matching Wi-Fi or None.
    """
    radios = {_band_of(r): r for r in snap["radios"]}
    template = profile.get("ssid_template", "{identity}_{suffix}")
    out = []
    for ssid in profile.get("ssids", []):
        seg = next((s for s in res.segments if s["key"] == ssid["segment"]), None)
        if seg is None:
            continue
        # The network type is always upper case: SENSADEV_IOT, never SENSADEV_iot (Richard, 2026-11-01).
        name = template.format(identity=identity, suffix=str(ssid["suffix"]).upper())
        network = res.net_name(seg)
        for band in ssid.get("bands", []):
            radio = radios.get(band)
            if radio is None:
                continue
            rid = radio.get("id")
            # A one-radio router has no `device` on its Wi-Fi at all (RUT241,
            # RUT2M_R_00.07.24.8, 2026-11-12): every Wi-Fi is on its only
            # radio. Missing that, Fix never saw the factory RUT241_xxxx as the
            # data network's Wi-Fi, made a second one, and ran out of room.
            on_radio = [
                w for w in snap["wifi"]
                if (rid in _list(w.get("device"))
                    or (len(snap["radios"]) == 1 and not w.get("device")))
                and w.get("mode") == "ap"
            ]
            have = next(
                (w for w in on_radio if str(w.get("ssid", "")).lower() == name.lower()),
                next((w for w in on_radio if w.get("network") == network), None),
            )
            # SSIDs are matched ignoring case, so a Wi-Fi in the wrong case is
            # renamed to its upper-case type rather than made a second time.
            out.append({
                "ssid": ssid, "seg": seg, "band": band, "radio": radio,
                "name": name, "network": network, "have": have,
            })
    return out


def passwords_needed(profile: dict[str, Any], snap: dict[str, Any],
                     identity: str) -> list[dict[str, str]]:
    """The Wi-Fi passwords Fix needs typed in: one per new or unsecured network.

    A Wi-Fi that already exists with WPA2 keeps its own password, so it never
    asks. [{param, name}], one per password the profile names.
    """
    out: dict[str, dict[str, str]] = {}
    for w in wifi_plan(profile, snap, identity, Resolved(profile, snap)):
        have = w["have"]
        if have is None or have.get("encryption") != "psk2":
            param = w["ssid"].get("password_param") or w["name"]
            out.setdefault(param, {"param": param, "name": w["name"]})
    return list(out.values())


def passwords_changeable(profile: dict[str, Any], snap: dict[str, Any],
                         identity: str) -> list[dict[str, str]]:
    """The Wi-Fi Fix keeps whose password the customer MAY change:
    [{param, name, ssids}].

    Every Wi-Fi already on the router with WPA2, so not one `passwords_needed`
    asks for. Blank keeps its password; one typed is set by Fix, last, as a
    rename is, since it drops everything on that Wi-Fi. Wi-Fi sharing a
    password (the 2G and 5G of one network) are one entry naming them all,
    since one password typed changes every one of them.
    """
    needed = {p["param"] for p in passwords_needed(profile, snap, identity)}
    out: dict[str, dict[str, Any]] = {}
    for w in wifi_plan(profile, snap, identity, Resolved(profile, snap)):
        param = w["ssid"].get("password_param") or w["name"]
        if w["have"] is None or param in needed:
            continue
        entry = out.setdefault(param, {"param": param, "ssids": []})
        if w["name"] not in entry["ssids"]:
            entry["ssids"].append(w["name"])
    for entry in out.values():
        entry["name"] = " and ".join(entry["ssids"])
    return list(out.values())


# -- wired sockets -------------------------------------------------------------
#
# A RUTM11's LAN sockets are ports in bridges (`snap["bridges"]`), one bridge
# per network, so which network a socket is on is readable and writable. What
# is plugged into each comes from `/ports_settings/status` (`topology`: every
# MAC seen on the socket, so a switch shows as several) named from the
# router's DHCP leases and the box's own device list. A switch router
# (RUTX11) cannot report its sockets at all: `bridges` is None there.

# The networks a customer can have wired devices on. Guest is wireless only.
WIRED_NETWORKS = ("data", "iot", "cctv")


def network_vids(profile: dict[str, Any]) -> dict[str, int]:
    """{network: VLAN} for the wired networks; the data network is untagged, VLAN 1."""
    return {s["key"]: int(s.get("vlan_id") or 1) for s in profile.get("segments", [])
            if s["key"] in WIRED_NETWORKS}


def _mac(value: Any) -> str:
    return re.sub(r"[^0-9A-F]", "", str(value or "").upper())


def _net_name(key: str) -> str:
    return _CUSTOMER.get(key, (key, ""))[0]


def _bridge_owner(res: Resolved, bridge: dict[str, Any]) -> str:
    """The layout network whose interface uses this bridge, or ""."""
    names = {bridge.get("id"), bridge.get("name")} - {None, ""}
    return next((k for k, i in res.iface_of.items() if i and i.get("device") in names), "")


def sockets(profile: dict[str, Any], snap: dict[str, Any], known: dict[str, dict[str, Any]] | None = None,
            box_mac: str = "") -> list[dict[str, Any]] | None:
    """Every LAN socket the router has, and what is on it. None when it cannot tell.

    [{port, label, number, bridge, network, link, devices: [{mac, name,
    network}], switch, box}]. `known` is {MAC: {name, network}} from the box's
    device list. `switch` is "managed" when a TSW2xx is seen, "unmanaged" when
    several devices share the socket with no managed switch among them.
    """
    if snap.get("bridges") is None:
        return None
    known = known or {}
    res = Resolved(profile, snap)
    leases = {_mac(x.get("macaddr")): x.get("hostname") for x in snap.get("leases") or []
              if x.get("hostname")}
    status = {str(p.get("id", "")).lstrip("_"): p for p in snap.get("ports") or []}
    own = {_mac(p.get("mac")) for p in snap.get("ports") or []}
    box = _mac(box_mac)
    out = []
    for bridge in snap["bridges"]:
        for port in _list(bridge.get("ports")):
            if not re.fullmatch(r"lan\d+", port):
                continue
            st = status.get(port)
            devices = []
            for seen in (st or {}).get("topology") or []:
                mac = _mac(seen.get("MAC"))
                if not mac or mac in own or any(d["mac"] == mac for d in devices):
                    continue
                k = known.get(mac) or {}
                name = k.get("name") or leases.get(mac) or ("This Sensa box" if mac == box else "")
                devices.append({"mac": mac, "name": name,
                                "network": "data" if mac == box else k.get("network") or ""})
            # A registered switch is named "Teltonika TSW2XX Switch (TSW202)", not "TSW202".
            managed = any(re.search(r"TSW2\d\d", d["name"] or "", re.IGNORECASE) for d in devices)
            out.append({
                "port": port, "number": int(port[3:]), "label": f"LAN {port[3:]}",
                "bridge": bridge.get("id"), "network": _bridge_owner(res, bridge),
                "link": None if st is None else st.get("state") == "up",
                "devices": devices,
                "switch": "managed" if managed else ("unmanaged" if len(devices) > 1 else ""),
                "box": bool(box) and any(d["mac"] == box for d in devices),
            })
    return sorted(out, key=lambda s: s["number"])


def default_wired(socks: list[dict[str, Any]] | None, box_wired: bool) -> dict[str, int]:
    """Wired devices per network as the box can see them, before the customer says."""
    out = dict.fromkeys(WIRED_NETWORKS, 0)
    for s in socks or []:
        for d in s["devices"]:
            if d["network"] in out and not re.search(r"\bTSW\d", d["name"] or "", re.IGNORECASE):
                out[d["network"]] += 1
    if box_wired:
        out["data"] = max(out["data"], 1)
    return out


def _tsw202_socket(plan: dict[str, Any], socks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The socket a TSW202 is already on, seen or placed."""
    return next((s for s in socks if (plan["switches"].get(s["port"]) or {}).get("model") == "TSW202"), None)


def _tsw202_text(plan: dict[str, Any], socks: list[dict[str, Any]], on: dict[str, str], k: str,
                 count: int, place: str) -> tuple[str, dict[str, Any] | None]:
    """The advice for a network that outnumbers its sockets, and the socket it names.

    A TSW202 gives each device a gigabit port with its power and VLAN set per
    port. It only carries every network from a socket on Your devices, so a new
    one goes there, never on the network's own socket.
    """
    cameras = f"{_net_name(k)} has {count} wired devices."
    why = "each one gets a gigabit port, with its power and network set per port."
    hub = _tsw202_socket(plan, socks)
    if hub:
        text = (f"{cameras} Your managed switch on {hub['label']} has the room: {why} "
                f"Drag {_net_name(k)} onto its ports.")
        return text, hub
    spot = min((s for s in socks if on[s["port"]] == "data"),
               key=lambda s: (s["box"], bool(s["devices"]), s["number"]), default=None)
    add = "Add it in Settings, Devices and Fix sets up its ports."
    if spot is None:
        return f"{cameras} Drag {_net_name('data')} onto a socket, then add a TSW202 switch there: {why} {add}", None
    return f"{cameras} {place} {spot['label']}: {why} {add}", spot


def socket_plan(profile: dict[str, Any], snap: dict[str, Any], wired: dict[str, int],
                known: dict[str, dict[str, Any]] | None = None, box_mac: str = "",
                box_wired: bool = False, arrangement: dict[str, Any] | None = None) -> dict[str, Any]:
    """Which network each socket should be on, and the switches the van needs.

    {readable, sockets, target: {port: network}, advice: [{text, product,
    socket, network}]}. Richard's rules (2026-10-31): wireless only needs no
    socket; a socket per network that has wired devices; a dumb TSW040 on a
    network's socket when it has more devices than sockets; one socket for
    several networks needs a managed TSW202 carrying the VLANs. A TSW040 is only
    100 Mb/s, so a TSW202 already there comes first for any network, and cameras
    always get a TSW202 (2026-11-03). Guest is never wired.

    Never moves the socket the box is on, nor one with something on it that
    is not known to belong to the network it would join, so a socket the box
    might be behind (wired, not seen) stays where it is.
    """
    segs = {s["key"] for s in profile.get("segments", [])}
    nets = [k for k in WIRED_NETWORKS if k in segs]
    socks = sockets(profile, snap, known, box_mac)
    need = {k: max(0, int(wired.get(k) or 0)) for k in nets}
    if box_wired and "data" in need:
        need["data"] = max(need["data"], 1)
    wanted = [k for k in nets if need[k]]
    for s in socks or []:
        s["locked"] = s["box"]
    plan: dict[str, Any] = {
        "readable": socks is not None, "sockets": socks or [], "target": {}, "advice": [],
        "need": need, "networks": nets,
        # The data network is untagged, VLAN 1 on the wire.
        "vlans": network_vids(profile),
        # {port: {model, placed}}: a switch seen or placed on a socket, or one advised.
        "switches": {s["port"]: {"model": "TSW202" if s["switch"] == "managed" else "", "placed": True}
                     for s in socks or [] if s["switch"]},
        "devices": _board_devices(socks or [], known or {}, None),
        "arranged": False,
    }
    if socks is None:
        if wanted:
            plan["advice"].append({
                "text": "This router cannot say which network each socket is on. Check by hand that "
                        "wired devices are plugged into the right sockets.",
                "product": "", "socket": "", "network": "", "port": ""})
        return plan
    if not socks:
        return plan
    if arrangement:
        return _arranged(plan, socks, known or {}, arrangement)
    if not wanted:
        return plan

    def advise(text: str, product: str = "", s: dict[str, Any] | None = None, network: str = "") -> None:
        plan["advice"].append({"text": text, "product": product, "socket": (s or {}).get("label", ""),
                               "port": (s or {}).get("port", ""), "network": network})
        if product and s and s["port"] not in plan["switches"]:
            plan["switches"][s["port"]] = {"model": product, "placed": False}

    if len(wanted) > len(socks):
        hub = next((s for s in socks if s["switch"] == "managed"), None)
        names = ", ".join(_net_name(k) for k in wanted)
        if hub:
            advise(f"Your managed switch on {hub['label']} carries {names} over one cable. "
                   "Fix sets up its ports.", "TSW202", hub)
        else:
            advise(f"Your router has {len(socks)} socket{'s' if len(socks) > 1 else ''} for "
                   f"{len(wanted)} wired networks ({names}). Plug a managed TSW202 switch into "
                   f"{socks[0]['label']}: it carries every network over one cable. Add it in "
                   "Settings, Devices and Fix sets up its ports.", "TSW202", socks[0])
        return plan

    on = {s["port"]: s["network"] for s in socks}

    def held(k: str) -> list[dict[str, Any]]:
        return [s for s in socks if on[s["port"]] == k]

    def movable(s: dict[str, Any], to: str) -> bool:
        if s["box"] or on[s["port"]] == to:
            return False
        frm = on[s["port"]]
        # The network it leaves must keep a socket if it needs one.
        if frm in need and need[frm] and len(held(frm)) <= 1:
            return False
        if not s["link"] and not s["devices"]:
            return True
        return bool(s["devices"]) and all(d["network"] == to for d in s["devices"])

    def best(to: str) -> dict[str, Any] | None:
        options = [s for s in socks if movable(s, to)]
        # Prefer a socket that already has this network's kit on it, then an empty one.
        options.sort(key=lambda s: (not s["devices"], s["number"]))
        return options[0] if options else None

    # One socket for each network that needs one.
    for k in wanted:
        if held(k):
            continue
        s = best(k)
        if s is None:
            advise(f"No free socket for {_net_name(k)}. Unplug something from a socket on another "
                   "network, or drag the network onto a socket yourself.", "", None, k)
            continue
        on[s["port"]] = k
    # Spare empty sockets go to networks with more devices than sockets.
    for k in wanted:
        while len(held(k)) < need[k]:
            s = best(k)
            if s is None or s["devices"]:
                break
            on[s["port"]] = k
    for k in wanted:
        mine = held(k)
        if mine and len(mine) < need[k] and not any(s["switch"] for s in mine):
            if k == "cctv" or _tsw202_socket(plan, socks):
                # They go on the switch, so the empty sockets moved for them stay put.
                freed = [s for s in mine if s["network"] != k and not s["devices"]]
                for s in freed:
                    on[s["port"]] = s["network"]
                if freed and not held("data"):
                    on[freed[0]["port"]] = "data"
                text, s = _tsw202_text(plan, socks, on, k, need[k], "Plug a TSW202 switch into")
                advise(text, "TSW202", s, k)
                continue
            advise(f"{_net_name(k)} has {need[k]} wired devices and {len(mine)} socket"
                   f"{'s' if len(mine) > 1 else ''}. Plug a TSW040 switch into {mine[0]['label']}: "
                   f"everything plugged into it joins {_net_name(k)}.", "TSW040", mine[0], k)
    plan["target"] = {s["port"]: on[s["port"]] for s in socks if on[s["port"]] != s["network"]}
    return plan


def _board_devices(socks: list[dict[str, Any]], known: dict[str, dict[str, Any]],
                   placed: dict[str, str] | None) -> list[dict[str, Any]]:
    """Every wired device the board shows: {mac, name, network, port}.

    Seen on a socket, or registered with an address and not seen (port "",
    waiting in the tray). `placed` ({MAC: port}) is where the customer put them.
    """
    out: dict[str, dict[str, Any]] = {}
    for s in socks:
        for d in s["devices"]:
            if not re.search(r"\bTSW\d", d["name"] or "", re.IGNORECASE):
                out[d["mac"]] = {**d, "port": s["port"]}
    for mac, k in known.items():
        if mac not in out and k.get("ip"):
            out[mac] = {"mac": mac, "name": k.get("name") or "", "network": k.get("network") or "",
                        "port": ""}
    ports = {s["port"] for s in socks}
    for mac, port in (placed or {}).items():
        if mac in out and (port in ports or port == ""):
            out[mac]["port"] = port
    return list(out.values())


def _arranged(plan: dict[str, Any], socks: list[dict[str, Any]], known: dict[str, dict[str, Any]],
              arrangement: dict[str, Any]) -> dict[str, Any]:
    """The plan as the customer arranged it on the board.

    `arrangement` is {sockets: {port: network}, switches: {port: model},
    devices: {MAC: port}}. The box's socket and a network this layout does
    not have are never taken from it; advice follows what was placed.
    """
    nets, need = plan["networks"], dict(plan["need"])
    on = {s["port"]: s["network"] for s in socks}
    by_port = {s["port"]: s for s in socks}
    for port, k in (arrangement.get("sockets") or {}).items():
        s = by_port.get(port)
        if s is None or k not in nets:
            continue
        if s["locked"] and k != s["network"]:
            plan["advice"].append({
                "text": f"{s['label']} has this Sensa box on it, so it stays on {_net_name(s['network'])}.",
                "product": "", "socket": s["label"], "port": port, "network": s["network"]})
            continue
        on[port] = k
    switches = {p: m for p, m in (arrangement.get("switches") or {}).items()
                if p in by_port and m in ("TSW040", "TSW202")}
    plan["switches"] = {**{p: v for p, v in plan["switches"].items() if v["placed"]},
                        **{p: {"model": m, "placed": True} for p, m in switches.items()}}
    plan["devices"] = _board_devices(socks, known, arrangement.get("devices") or {})
    # A device placed on a socket is a wired device on that socket's network.
    for d in plan["devices"]:
        if d["port"]:
            k = on.get(d["port"])
            if k in need:
                need[k] = max(need[k], sum(1 for x in plan["devices"] if on.get(x["port"]) == k))
    plan["need"] = need

    def advise(text: str, product: str, s: dict[str, Any] | None, network: str) -> None:
        plan["advice"].append({"text": text, "product": product, "socket": (s or {}).get("label", ""),
                               "port": (s or {}).get("port", ""), "network": network})

    for k in nets:
        mine = [s for s in socks if on[s["port"]] == k]
        if mine and len(mine) < need[k] and not any(s["port"] in plan["switches"] for s in mine):
            if k == "cctv" or _tsw202_socket(plan, socks):
                text, s = _tsw202_text(plan, socks, on, k, need[k], "Drag a TSW202 switch onto")
                advise(text, "TSW202", s, k)
                continue
            advise(f"{_net_name(k)} has {need[k]} wired devices and {len(mine)} socket"
                   f"{'s' if len(mine) > 1 else ''}. Drag a TSW040 switch onto {mine[0]['label']}: "
                   f"everything plugged into it joins {_net_name(k)}.", "TSW040", mine[0], k)
    # A managed switch the customer placed but the router does not see yet.
    seen = {s["port"] for s in socks if s["switch"] == "managed"}
    for port, model in switches.items():
        if model == "TSW202" and port not in seen:
            advise(f"Plug the TSW202 into {by_port[port]['label']} and add it in Settings, Devices: "
                   "Fix then sets up its ports.", "TSW202", by_port[port], "")
    plan["target"] = {s["port"]: on[s["port"]] for s in socks if on[s["port"]] != s["network"]}
    plan["arranged"] = True
    return plan


def _socket_sections(profile: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The check's rows for the sockets, one section per network that has one."""
    out = []
    if not plan.get("readable"):
        for k in [k for k, n in (plan.get("need") or {}).items() if n and k != "data"]:
            rows = [{"setting": "Sockets", "current": "Not readable on this router",
                     "target": f"Wired {_net_name(k)}", "status": "unknown"}]
            out.append({"nerd": True, "area": "Networks", "title": f"{_net_name(k)} sockets",
                        "status": "unknown", "segment": k, "rows": rows})
        return out
    target = plan.get("target") or {}
    for k in (plan.get("need") or {}):
        rows = []
        for s in plan["sockets"]:
            to = target.get(s["port"], s["network"])
            if to == k:
                rows.append(_row(s["label"], _net_name(s["network"]) if s["network"] else "Not used",
                                 _net_name(k)))
        if rows and (plan["need"].get(k) or any(r["status"] != "same" for r in rows)):
            out.append(_section("Networks", f"{_net_name(k)} sockets", rows, nerd=True, segment=k))
    sw = plan.get("switch") or {}
    if sw.get("target"):
        rows = []
        for p in sw.get("ports") or []:
            to = sw["target"].get(p["port"])
            if to:
                now = _net_name(p["network"]) if p["network"] else (
                    f"VLAN {p['vlan']}" if p["vlan"] else "Not used")
                rows.append(_row(f"Switch {p['label']}", now, _net_name(to),
                                 same=bool(p["network"]) and p["network"] == to))
        if rows:
            out.append(_section("Networks", f"TSW202 on {sw['label']}", rows, nerd=True))
    return out


def wan6_of(snap: dict[str, Any]) -> dict[str, Any] | None:
    """The router's IPv6 WAN, which the check and Fix both turn off, or None
    on a router without one."""
    return next((i for i in snap.get("interfaces") or [] if i.get("id") == "wan6"), None)


def box_reservation(snap: dict[str, Any], box: dict[str, str] | None) -> dict[str, Any] | None:
    """What the router can say about the box's reservation, or None when it cannot.

    `box` is {mac, ip}. None covers a router or box that cannot tell and a box
    not on one of this router's networks: "could not ask", never "not reserved".
    Otherwise {lease, taken}: `lease` is the reservation for the box's MAC, and
    `taken` is true when the box's address is reserved for something else.
    The check and Fix both read this, so they cannot disagree.
    """
    leases = snap.get("static_leases")
    mac, ip = _mac((box or {}).get("mac")), (box or {}).get("ip") or ""
    if leases is None or not mac or not ip:
        return None
    if not any(i.get("ipaddr") and _prefix(str(i["ipaddr"])) == _prefix(ip) for i in snap["interfaces"]):
        return None
    lease = next((x for x in leases if mac in (_mac(m) for m in _list(x.get("mac")))), None)
    return {"lease": lease, "taken": lease is None and any(x.get("ip") == ip for x in leases)}


def _box_section(snap: dict[str, Any], box: dict[str, str] | None) -> list[dict[str, Any]]:
    """The check's section for the box's DHCP reservation, on the data network."""
    found = box_reservation(snap, box)
    if found is None:
        return []
    ip = box["ip"]  # type: ignore[index]
    if found["taken"]:
        row = {"setting": "Sensa box address", "current": "Reserved for another device",
               "target": ip, "status": "unknown"}
        return [{"nerd": False, "area": "Addresses (DHCP)", "title": "Sensa box address",
                 "status": "unknown", "segment": "data", "rows": [row]}]
    have = (found["lease"] or {}).get("ip")
    return [_section("Addresses (DHCP)", "Sensa box address", [
        _row("Sensa box address", have, have or ip),
    ], nerd=False, segment="data")]


def compare(profile: dict[str, Any], snap: dict[str, Any], identity: str,
            plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every part of the layout, current vs target, from a REST snapshot.

    Returns [{nerd, area, title, status, rows: [{setting, current, target,
    status}]}]; section status is add / change / same, and a row can also be
    "unknown" for what the router cannot report. Wi-Fi passwords are never
    read back or shown. `plan` is `socket_plan`'s answer; without one the
    sockets are not checked.
    """
    segments = profile.get("segments", [])
    forwarding = profile.get("forwarding", {})
    policy = profile.get("input_policy", {})
    out: list[dict[str, Any]] = []

    # Resolve every segment to the router's interface and zone first, so the
    # forwarding rows can name zones that already exist on this router.
    res = Resolved(profile, snap)
    iface_of, zone_of, zone_name = res.iface_of, res.zone_of, res.zone_name

    for seg in segments:
        if not seg.get("managed"):
            continue
        key, label = seg["key"], seg.get("label", seg["key"])
        iface = iface_of[key]
        out.append(_section("Networks", f"{label} network", [
            _row("Router address", (iface or {}).get("ipaddr"), seg["router_ip"]),
            _row("Netmask", (iface or {}).get("netmask"), "255.255.255.0"),
            _row("Addressing", (iface or {}).get("proto"), "static"),
            _row("Enabled", _on_off((iface or {}).get("enabled")) if iface else "", "On"),
        ], nerd=False, found=iface is not None, segment=key))

        server = dhcp_for(snap, iface)
        start_ip, end_ip, lease = pool(seg)
        out.append(_section("Addresses (DHCP)", f"{label} addresses", [
            _row("Pool start", (server or {}).get("start_ip"), start_ip),
            _row("Pool end", (server or {}).get("end_ip"), end_ip),
            _row("Lease time", (server or {}).get("leasetime"), lease),
            _row("Enabled", _on_off((server or {}).get("enable_dhcpv4")) if server else "", "On"),
        ], nerd=False, found=server is not None, segment=key))

        zone = zone_of[key]
        wan_out = "wan" in forwarding.get(seg["zone"], [])
        want_out = sorted(zone_name(z) for z in forwarding.get(seg["zone"], []))
        have_out = sorted(_list((zone or {}).get("out")))
        rows = [
            _row("Access to the router", (zone or {}).get("input"),
                 str(policy.get(seg["zone"], "reject")).upper()),
            _row("Forwarding", (zone or {}).get("forward"), "REJECT"),
            _row("Internet sharing (NAT)", _on_off((zone or {}).get("masq")) if zone else "",
                 "On" if wan_out else "Off"),
            _row("Can reach", (", ".join(have_out) or "Nothing") if zone else "",
                 ", ".join(want_out) or "Nothing",
                 same=zone is not None and have_out == want_out),
        ]
        if str(policy.get(seg["zone"], "reject")).lower() == "reject":
            zname = (zone or {}).get("name") or seg["zone"]
            for exc in profile.get("input_exceptions", []):
                if seg["zone"] not in exc.get("zones", []):
                    continue
                has = has_rule(snap["rules"], zname, rule_ports(exc.get("dest_ports", [])))
                now = ("Yes" if has else "No") if zone else ""
                rows.append(_row(f"Allows {exc['name'].upper()}", now, "Yes"))
        # Single ports this zone may open into another (zone_exceptions).
        zname = (zone or {}).get("name") or seg["zone"]
        for exc in profile.get("zone_exceptions", []):
            if exc.get("from") != seg["zone"]:
                continue
            ports = rule_ports(exc.get("dest_ports", []))
            has = has_rule(snap["rules"], zname, ports,
                           dest=zone_name(exc.get("to", "")), all_ports=True)
            now = ("Yes" if has else "No") if zone else ""
            what = exc.get("label") or exc["name"]
            rows.append(_row(f"Allows {what} (port {', '.join(sorted(ports))})", now, "Yes"))
        out.append(_section("Firewall", f"{label} zone", rows, nerd=True,
                            found=zone is not None, segment=key))
        # Internet access in plain words, outside Nerd Mode: cameras get none.
        has_net = zone is not None and "wan" in have_out
        out.append(_section("Internet", f"{label} internet", [
            _row("Internet access", ("Yes" if has_net else "No") if zone else "",
                 "Yes" if wan_out else "No"),
        ], nerd=False, found=zone is not None, segment=key))

    # The data network's zone: what Sensa needs it to reach.
    data = next((s for s in segments if not s.get("managed")), None)
    if data is not None:
        zone = zone_of[data["key"]]
        want_out = sorted(zone_name(z) for z in forwarding.get(data["zone"], []))
        have_out = sorted(_list((zone or {}).get("out")))
        out.append(_section("Firewall", f"{data.get('label', data['key'])} zone", [
            _row("Can reach", ", ".join(have_out) or "Nothing", ", ".join(want_out),
                 same=set(want_out) <= set(have_out)),
        ], nerd=True, found=zone is not None, segment=data["key"]))
        out.append(_section("Internet", f"{data.get('label', data['key'])} internet", [
            _row("Internet access", ("Yes" if "wan" in have_out else "No") if zone else "",
                 "Yes" if "wan" in want_out else "No"),
        ], nerd=False, found=zone is not None, segment=data["key"]))

    # Wi-Fi: one network per SSID per band this router has a radio for.
    radios = {_band_of(r): r for r in snap["radios"]}
    for w in wifi_plan(profile, snap, identity, res):
        have, name = w["have"], w["name"]
        out.append(_section("Wi-Fi", f"{name} ({_BAND[w['band']]})", [
            _row("Wi-Fi name", (have or {}).get("ssid"), name),
            _row("Network", (have or {}).get("network"), w["network"]),
            _row("Security", (have or {}).get("encryption"), "psk2"),
            _row("Client isolation", _on_off((have or {}).get("isolate")) if have else "",
                 "On" if w["ssid"].get("client_isolation") else "Off"),
            _row("Enabled", _on_off((have or {}).get("enabled")) if have else "", "On"),
        ], nerd=False, found=have is not None, segment=w["seg"]["key"]))

    for band, radio in sorted(radios.items()):
        out.append(_section("Wi-Fi", f"Radio {_BAND[band]}", [
            _row("Radio", _on_off(radio.get("enabled")), "On"),
        ], nerd=True, segment="radio"))

    # GPS ships OFF on a RutOS router (wiki default), and the van's position
    # for the map, tracking and journeys comes from it.
    gps = snap.get("gps")
    if isinstance(gps, dict) and gps:
        want = gps_wanted(profile, gps)
        out.append(_section("GPS", "GPS", [
            _row(label, _on_off(gps.get(key)), _on_off(want[key]))
            for key, label in GPS_SETTINGS if key in want
        ], nerd=False, segment="gps"))
    # IPv6 is not used in the van: wan6 ships on and takes a place among the WANs.
    wan6 = wan6_of(snap)
    if wan6 is not None:
        out.append(_section("IPv6", "IPv6", [
            _row("IPv6 (wan6)", _on_off(wan6.get("enabled")), "Off"),
        ], nerd=False, segment="ipv6"))
    if plan is not None:
        out += _box_section(snap, plan.get("box"))
        out += _socket_sections(profile, plan)
    return out


# The GPS settings Fix looks after, in order: the receiver, then the other
# satellite systems beside the US one. Read on BP-RUTX11 (2026-10-01, all
# on) and BP's RUTM11 (2026-10-30, the three systems off). More satellites
# give a faster, steadier fix, which the speedo and the map wait on.
GPS_SETTINGS = (
    ("enabled", "GPS"),
    ("galileo_sup", "Galileo satellites"),
    ("glonass_sup", "GLONASS satellites"),
    ("beidou_sup", "BeiDou satellites"),
)


def gps_wanted(profile: dict[str, Any], gps: dict[str, Any]) -> dict[str, str]:
    """{setting: "1"/"0"} for the GPS settings this router has.

    The check and Fix both read this, so they cannot disagree. A satellite
    system the router does not report is left alone; with GPS off they are
    left alone too, since nothing uses them.
    """
    on = (profile.get("gps") or {"enabled": True}).get("enabled", True)
    want = {"enabled": "1" if on else "0"}
    if on:
        want.update({key: "1" for key, _label in GPS_SETTINGS[1:] if key in gps})
    return want


# What each part of the layout is FOR, in the van owner's words. The
# profile's labels (IoT, CCTV) are the installer's; a customer reads these.
_CUSTOMER = {
    "data": ("Your devices", "The Sensa box, your screens and phones"),
    "iot": ("Smart devices", "Heating, power, lights and sensors"),
    "cctv": ("Cameras", "Kept off the internet, watched from inside the van"),
    "guest": ("Guest Wi-Fi", "Internet for visitors, kept away from the van's systems"),
    "radio": ("Wi-Fi radios", "The router's 2.4 and 5 GHz radios"),
    "gps": ("GPS", "The van's position, for the map, tracking and journeys"),
    "ipv6": ("IPv6", "Not used in the van, so it is kept off"),
}

# Icons for the parts that are not network segments.
_ICON = {"radio": "mdi:wifi", "gps": "mdi:crosshairs-gps", "ipv6": "mdi:ip-network"}


def _duration(value: str) -> str:
    """A uci lease time (12h, 168h, 30m) as words: "12 hours", "7 days"."""
    m = re.fullmatch(r"(\d+)([hmd])", value or "")
    if not m:
        return value
    n, unit = int(m.group(1)), m.group(2)
    if unit == "h" and n % 24 == 0:
        n, unit = n // 24, "d"
    word = {"h": "hour", "m": "minute", "d": "day"}[unit]
    return f"{n} {word}{'' if n == 1 else 's'}"


def _band_label(title: str) -> str:
    return title.rsplit("(", 1)[-1].rstrip(")") if "(" in title else ""


def _says(section: dict[str, Any], row: dict[str, Any], seg: dict[str, Any] | None) -> str:
    """One plain sentence for a row that is not already right, or ""."""
    setting, target, current = row["setting"], row["target"], row["current"]
    title = section["title"]
    if setting == "Sensa box address":
        if row["status"] == "unknown":
            return f"This Sensa box's address, {target}, is reserved for another device. Change that on the router"
        return f"This Sensa box always gets the address {target}"
    if row["status"] == "unknown":
        return "Check by hand which sockets your wired devices are in: this router cannot report it"
    if setting.startswith("LAN "):
        return f"Socket {setting} on the router joins this network"
    if section["area"] == "GPS":
        if setting != "GPS":
            return f"{setting} are used too, for a faster fix" if target == "On" else f"{setting} are not used"
        return "GPS is switched on" if target == "On" else "GPS is switched off"
    if section["area"] == "IPv6":
        return "IPv6 is switched off"
    if section["area"] == "Wi-Fi":
        if setting == "Wi-Fi name":
            if current:
                return f'Wi-Fi "{current}" is renamed "{target}"'
            return f'New Wi-Fi "{target}"'
        if setting == "Client isolation":
            return ("Guests cannot see each other's phones" if target == "On"
                    else "Devices on this Wi-Fi can see each other")
        if setting == "Radio":
            return f"The {_band_label(title) or title} radio is switched on"
        return ""
    if setting == "Router address":
        return f"Gets its own network, {target.rsplit('.', 1)[0]}.x"
    if setting == "Pool start":
        return "Hands out addresses automatically"
    if setting == "Lease time" and row["status"] == "change":
        return f"Devices keep their address for {_duration(target)} instead of {_duration(current)}"
    if setting == "Access to the router":
        if target != "REJECT":
            return "Can open the router's settings page"
        return ("Can no longer open the router's settings page" if current
                else "Cannot open the router's settings page")
    if setting.startswith("Allows "):
        return ""  # plumbing behind "Hands out addresses"; the details show it
    if setting == "Internet sharing (NAT)":
        return "Gets internet access" if target == "On" else "Has no internet access"
    if setting == "Can reach":
        if seg is not None and not seg.get("managed") and target != "wan":
            return "Your screens and phones can reach the smart devices and cameras"
        if target == "wan":
            return "Reaches the internet and nothing else"
        if target == "Nothing":
            return "Reaches nothing else"
        return f"Reaches {target}"
    return ""


def summarise(profile: dict[str, Any], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The comparison as the van owner reads it: one item per part of the van.

    [{key, icon, name, purpose, status, says: [sentence], sections}]. Status
    is "same" when every section is, "add" when the part does not exist yet,
    "unknown" when the only gap is something the router cannot report, else
    "change".
    """
    segs = {s["key"]: s for s in profile.get("segments", [])}
    order = [s["key"] for s in profile.get("segments", [])] + ["gps", "ipv6", "radio"]
    items = []
    for key in order:
        mine = [s for s in sections if s.get("segment") == key]
        if not mine:
            continue
        seg = segs.get(key)
        says: list[str] = []
        for sec in mine:
            for row in sec["rows"]:
                if row["status"] == "same":
                    continue
                line = _says(sec, row, seg)
                if line and line not in says:
                    says.append(line)
        statuses = {s["status"] for s in mine}
        if statuses == {"same"}:
            status = "same"
        elif statuses <= {"same", "unknown"}:
            status = "unknown"
        elif all(s["status"] in ("add", "unknown") for s in mine if s["area"] != "Wi-Fi") \
                and any(s["status"] == "add" and s["area"] == "Networks" for s in mine):
            status = "add"
        else:
            status = "change"
        # A layout may name a part itself (the single network's "Everything").
        name, purpose = (seg or {}).get("customer") or _CUSTOMER.get(
            key, ((seg or {}).get("label", key), (seg or {}).get("purpose", ""))
        )
        items.append({
            "key": key,
            "icon": (seg or {}).get("icon") or _ICON.get(key, "mdi:router-wireless"),
            "name": name,
            "purpose": purpose,
            "status": status,
            "says": says,
            "sections": mine,
        })
    return items


def van_identity(name: str) -> str:
    """The van's name (Settings, System, Name) as a Wi-Fi name prefix: its
    first word, letters and digits only (no hyphen or underscore), at most 10
    characters, in capitals, so "Bertha the Van" gives BERTHA_2G and
    "sensa-dev" gives SENSADEV. "" when there is no usable name."""
    words = (name or "").split()
    first = words[0] if words else ""
    return re.sub(r"[^A-Za-z0-9]+", "", first)[:10].upper()


def default_identity(snap: dict[str, Any], fallback: str) -> str:
    """The van name already on the router (`<name>_2g`), else the fallback.

    Cleaned to what `ProvisionParams.validate` accepts: letters, digits,
    hyphen and underscore, at most 20 characters.
    """
    for w in snap.get("wifi", []):
        ssid = str(w.get("ssid") or "")
        if ssid.lower().endswith("_2g") and len(ssid) > 3:
            return ssid[:-3][:20]
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", fallback.strip()).strip("_")
    return (cleaned or "sensa")[:20].lower()


async def _read_if_present(client: Any, path: str) -> list[dict[str, Any]] | None:
    """A list a router may not have: None when it says so ("cannot tell").

    A timeout or a dropped connection is "could not ask", which says nothing
    about the router: one more try, then it raises. A status below 500, or
    RutOS's 501 for "not implemented", is the router's own answer.
    """
    for last_try in (False, True):
        try:
            data = (await client.async_get(path)).get("data")
            return data if isinstance(data, list) else None
        except Exception as err:
            status = getattr(err, "status", None)
            if isinstance(status, int) and (status < 500 or status == 501):
                return None
            if last_try:
                raise
    return None


async def read_snapshot(client: Any) -> dict[str, Any]:
    """Every config list `compare` reads, via the client's REST `async_get`."""
    snap: dict[str, Any] = {}
    for key, path in ENDPOINTS.items():
        data = (await client.async_get(path)).get("data")
        # Config lists are lists; /gps/global is one dict.
        snap[key] = data if data else ({} if key == "gps" else [])
    # The wired sockets (ports in bridges on a RUTM11), what is plugged into
    # each, and who holds which address. A router without one of these paths
    # errors, which is "cannot tell", never "nothing there".
    for key, path in (("bridges", BRIDGES), ("ports", PORTS), ("leases", LEASES),
                      ("vlan_devices", VLAN_DEVICES), ("static_leases", STATIC_LEASES)):
        snap[key] = await _read_if_present(client, path)
    return snap
