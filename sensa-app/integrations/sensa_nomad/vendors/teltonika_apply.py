"""Fix: bring a RutOS router to a Sensa van network layout over REST.

Writes as `admin`, the login Sensa already holds. Every write here was
measured on BP's RUTM11 (RutOS 00.07.24.3, 2026-10-28) with the formats in
docs/devices/teltonika-router.md: a firewall zone is created before the
network that sits in it, a DHCP pool takes its network's id, Wi-Fi takes its
radio as a list and its network by name, and rule ports are lists.

What it never does:

* touch the data network's interface or DHCP pool. The box lives there and the
  router is reached over it, so changing it cuts Fix off mid-run. It only adds
  the box's own reservation, at the address the box already has;
* delete anything. A layout that drops a network leaves it on the router;
* move a socket the plan did not ask for. Sockets move only as
  `teltonika_setup.socket_plan` says, which never moves the box's own socket.

Wi-Fi goes last: renaming a network drops everything on it, which may
include the screen the customer pressed Fix on. It also drops the box itself
when the box is on that Wi-Fi: .236 fell off the network after renaming
RUT_E474_2G on BP's router (2026-10-28). So the renames and password
changes of existing Wi-Fi go after everything else, new Wi-Fi included,
and every other change is reported before anything drops.

Each phase reads the router again, so ids the router picked for what was
just created (it ignores the id it is sent) are found by name or address.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import teltonika_switch
from .teltonika_setup import (
    _BAND,
    ENDPOINTS,
    GPS_SETTINGS,
    STATIC_LEASES,
    VLAN_DEVICES,
    Resolved,
    _interface_for,
    _list,
    _net_name,
    box_reservation,
    dhcp_for,
    gps_wanted,
    has_rule,
    network_vids,
    passwords_changeable,
    passwords_needed,
    pool,
    read_snapshot,
    rule_ports,
    wan6_of,
    wifi_plan,
)


class ApplyError(Exception):
    """Fix stopped. `done` is what had been changed before it did."""

    def __init__(self, message: str, done: list[str]) -> None:
        super().__init__(message)
        self.done = done


def _diff(have: dict[str, Any], want: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in want.items() if str(have.get(k, "")) != v}


# A network with no socket in it: not a bridge, and no interface named, so its
# Wi-Fi joins it directly. Both fields go together: a PUT of `bridge: "0"`
# alone makes RutOS name the old bridge as the interface, a device that does
# not exist (NO_DEVICE). RUT241, RUT2M_R_00.07.24.8, 2026-11-12.
#
# Why not a bridge: netifd keeps a bridge only while a member is present
# (`bridge_set_up` answers -ENOENT otherwise; `bridge_empty` would keep it,
# and the RutOS API cannot set it). A Wi-Fi-only bridge loses its one member
# while the radio starts, so after a restart the network stays down with
# DEVICE_CLAIM_FAILED and nothing on its Wi-Fi gets an address. Teltonika's
# own advice for a Wi-Fi-only network is the same: no bridge, interface blank.
_UNBRIDGED: dict[str, Any] = {"bridge": "0", "ifname": []}


INTERFACE_STATUS = "/interfaces/status"


async def networks_down(client: Any, profile: dict[str, Any]) -> list[str]:
    """The managed networks the router has but is not running, by label.

    Saved settings are not a running network: IoT and Guest were saved right
    and still down after the restart (DEVICE_CLAIM_FAILED, 2026-11-12), and
    the check passed. NO_DEVICE is left out: a network with no socket and no
    Wi-Fi yet (CCTV before a camera socket) has nothing to run on. [] when the
    router does not answer the status, since "could not ask" is not "down".
    """
    rows = (await client.async_get(INTERFACE_STATUS) or {}).get("data")
    if not isinstance(rows, list):
        return []
    snap = await read_snapshot(client)
    res = Resolved(profile, snap)
    by_name = {r.get("name"): r for r in rows if isinstance(r, dict)}
    down = []
    for seg in profile.get("segments", []):
        if not seg.get("managed"):
            continue
        status = by_name.get((res.iface_of.get(seg["key"]) or {}).get("name"))
        if status is None or status.get("up") is not False:
            continue
        codes = {e.get("code") for e in status.get("errors") or [] if isinstance(e, dict)}
        if "NO_DEVICE" not in codes:
            down.append(seg.get("label", seg["key"]))
    return down


def _wired(iface: dict[str, Any]) -> list[str]:
    """What is in a network's interface: sockets, VLANs, or a bridge it names."""
    return _list(iface.get("ifname"))


def _empty_bridge(iface: dict[str, Any], wifi: list[dict[str, Any]] | None = None) -> bool:
    """A network that would go down after a restart: a bridge with nothing in
    it. An unbridged network naming a bridge is left alone: BP's RUTX11 runs
    its IoT on a hand-made `br-iot` that way.

    A bridge holding one of the router's Wi-Fi is not empty. A RUTM11 makes a
    network a bridge itself when a Wi-Fi joins it, so unbridging it was undone
    straight away and the Fix's check failed for ever (customer RUTM11,
    RUTM_R_00.07.24.3, 2026-11-21). Those bridges run: the office RUTM11's
    IoT and Guest were up 7 days on RUTM_R_00.07.26.2."""
    if str(iface.get("bridge", "0")) != "1" or _wired(iface):
        return False
    names = {iface.get("id"), iface.get("name")} - {None, ""}
    return not any(names & set(_list(w.get("network"))) for w in wifi or [])


def bad_passwords(profile: dict[str, Any], snap: dict[str, Any], identity: str,
                  passwords: dict[str, str]) -> list[str]:
    """The Wi-Fi networks whose password is wrong: a new one missing or not 8
    to 63 long, or a kept one given a new password that is not 8 to 63 long
    (blank keeps its password, so blank is fine there)."""
    bad = [
        p["name"] for p in passwords_needed(profile, snap, identity)
        if not 8 <= len(passwords.get(p["param"]) or "") <= 63
    ]
    for p in passwords_changeable(profile, snap, identity):
        typed = passwords.get(p["param"]) or ""
        if typed and not 8 <= len(typed) <= 63:
            bad.append(p["name"])
    return bad


def _wifi_change(w: dict[str, Any], passwords: dict[str, str]) -> tuple[
        dict[str, str], str, dict[str, str], dict[str, str]]:
    """For one planned Wi-Fi: (wanted, its key, quiet changes, changes that drop devices).

    A change that drops everything on the network is a new name or a new
    password; those go last. The one place this is decided, for Fix and for
    `wifi_renames`.
    """
    have, name = w["have"], w["name"]
    key = passwords.get(w["ssid"].get("password_param") or name) or ""
    want = {
        "ssid": name, "network": w["network"],
        "isolate": "1" if w["ssid"].get("client_isolation") else "0", "enabled": "1",
    }
    if have is None:
        return want, key, {}, {}
    change = _diff(have, want)
    drops = {k: change.pop(k) for k in ("ssid",) if k in change}
    if have.get("encryption") != "psk2":
        drops.update({"encryption": "psk2", "key": key})
    elif key and key != str(have.get("key", "")):
        # A new password the customer chose for a Wi-Fi Fix keeps. RutOS reads
        # the key back as set (RUT241, 2026-11-12), so the check after the
        # restart sees it in place and a second Fix changes nothing.
        drops["key"] = key
    return want, key, change, drops


def wifi_renames(profile: dict[str, Any], snap: dict[str, Any], identity: str,
                 passwords: dict[str, str]) -> list[dict[str, Any]]:
    """The existing Wi-Fi networks Fix renames or re-keys: {from, to, password}.

    `password` is the new one, or None when it stays as it is. Everything on
    a network listed here drops off and must be connected to `to` again.
    """
    out = []
    for w in wifi_plan(profile, snap, identity, Resolved(profile, snap)):
        if w["have"] is None:
            continue
        _want, _key, _change, drops = _wifi_change(w, passwords)
        if drops:
            out.append({"from": w["have"].get("ssid") or "", "to": w["name"],
                        "password": drops.get("key")})
    return out


class _DryRun:
    """Reads the router through `client`, records writes instead of sending them."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def async_get(self, path: str) -> dict[str, Any]:
        return await self._client.async_get(path)

    async def async_post(self, path: str, payload: dict[str, Any], *,
                         timeout: float | None = None) -> dict[str, Any]:
        return {}

    async def async_put(self, path: str, payload: dict[str, Any], *,
                        timeout: float | None = None) -> dict[str, Any]:
        return {}


# Never written into a log: a dry-run report names these fields, not their values.
_SECRET_FIELDS = {"key", "password"}


async def _router_holds(client: Any, path: str, data: dict[str, Any]) -> str:
    """For the check's report, what the router holds for each field a PUT would change.

    "the router did not keep: Updated the IoT network" could not say which
    setting the router dropped (customer RUT2M, 2026-11-26), so the dry run
    names each field with the router's value and the one Fix wanted.
    """
    base, _, row_id = path.rpartition("/")
    try:
        rows = (await client.async_get(base) or {}).get("data")
    except Exception:  # noqa: BLE001 - the report goes out without the values
        rows = None
    row = next((r for r in rows if isinstance(r, dict) and str(r.get("id")) == row_id),
               {}) if isinstance(rows, list) else {}
    parts = [k if k in _SECRET_FIELDS else f"{k} {row.get(k, 'unset')}, wanted {v}"
             for k, v in data.items()]
    return f" ({'; '.join(parts)})" if parts else ""


async def pending(client: Any, profile: dict[str, Any], identity: str,
                  passwords: dict[str, str], *, wifi_names: bool = True,
                  sockets: dict[str, str] | None = None,
                  switch: dict[str, Any] | None = None,
                  box: dict[str, str] | None = None) -> list[str]:
    """What a Fix would still change, writing nothing. [] means the router is right.

    Raises ApplyError when the router is so far out that a Fix would need to
    create something first (a dry run cannot see what it did not create).
    """
    if switch:
        switch = {**switch, "client": _DryRun(switch["client"])}
    return await apply(_DryRun(client), profile, identity, passwords, wifi_names=wifi_names,
                       sockets=sockets, switch=switch, box=box)


async def _move_sockets(client: Any, profile: dict[str, Any], sockets: dict[str, str],
                        write: Any) -> None:
    """Put each socket in `sockets` ({lan3: "iot"}) on its network.

    A network's sockets are its interface's `ifname`, with `bridge: "1"`; the
    router keeps a bridge (`br_<id>`) to match. Measured on BP's RUTM11
    (2026-10-31): that PUT made `br_lan2` holding LAN 3 for cctv, which had no
    bridge, while a bridge POSTed on its own could not be tied to the network
    (the interface's `device` is read only, 422).
    """
    labels = {s["key"]: s.get("label", s["key"]) for s in profile.get("segments", [])}
    ifaces = ENDPOINTS["interfaces"]
    for port, key in sorted(sockets.items()):
        snap = await _read_settled(client)
        if snap.get("bridges") is None:
            raise ApplyError("This router cannot set its sockets.", [])
        iface = Resolved(profile, snap).iface_of.get(key)
        if iface is None:
            raise ApplyError(f"The {labels.get(key, key)} network is not on the router.", [])
        if port in _list(iface.get("ifname")):
            continue
        label = f"LAN {port.removeprefix('lan')}"
        old = next((i for i in snap["interfaces"] if port in _list(i.get("ifname"))), None)
        if old is not None:
            rest = [p for p in _list(old.get("ifname")) if p != port]
            # The last socket leaving: unbridge it, or its Wi-Fi alone would
            # leave an empty bridge that fails after a restart (`_UNBRIDGED`).
            body = {"ifname": rest} if _wired({"ifname": rest}) else dict(_UNBRIDGED)
            await write("PUT", f"{ifaces}/{old['id']}", body,
                        f"Took {label} off its old network")
        await write("PUT", f"{ifaces}/{iface['id']}",
                    {"bridge": "1", "ifname": [*_list(iface.get("ifname")), port]},
                    f"Put {label} on the {labels.get(key, key)} network")
    if sockets:
        await _read_settled(client)


async def _trunk(client: Any, profile: dict[str, Any], switch: dict[str, Any], write: Any) -> None:
    """Carry each network the switch's ports need over its router socket, tagged.

    An 802.1q device on the socket (`lan2.3`) joins that network's interface;
    the socket itself stays on the data network, untagged, which is where the
    switch's own address is.
    """
    vids = network_vids(profile)
    labels = {k: _net_name(k) for k in vids}
    port = switch["socket"]
    sock = f"LAN {port.removeprefix('lan')}"
    nets = sorted({k for p, k in switch["ports"].items()
                   if k in vids and k != "data" and p != switch.get("uplink")})
    ifaces = ENDPOINTS["interfaces"]
    for k in nets:
        dev = f"{port}.{vids[k]}"
        snap = await read_snapshot(client)
        if snap.get("vlan_devices") is None:
            raise ApplyError("This router cannot carry networks to a switch.", [])
        if not any(d.get("name") == dev or (d.get("ifname") == port and str(d.get("vid")) == str(vids[k]))
                   for d in snap["vlan_devices"]):
            await write("POST", VLAN_DEVICES, {"name": dev, "type": "8021q", "ifname": port,
                                               "vid": str(vids[k])},
                        f"Carried {labels.get(k, k)} on {sock} to the switch (VLAN {vids[k]})")
            snap = await read_snapshot(client)
        iface = Resolved(profile, snap).iface_of.get(k)
        if iface is None:
            raise ApplyError(f"The {labels.get(k, k)} network is not on the router.", [])
        if dev not in _list(iface.get("ifname")):
            await write("PUT", f"{ifaces}/{iface['id']}",
                        {"bridge": "1", "ifname": [*_list(iface.get("ifname")), dev]},
                        f"Joined VLAN {vids[k]} on {sock} to the {labels.get(k, k)} network")
    if nets:
        await _read_settled(client)


async def _switch_vlans(profile: dict[str, Any], switch: dict[str, Any], write: Any) -> None:
    """Put each switch port on its network (teltonika_switch.changes)."""
    labels = {k: _net_name(k) for k in network_vids(profile)}
    sw = await teltonika_switch.read_switch(switch["client"])
    for method, path, data, what in teltonika_switch.changes(
            sw, network_vids(profile), switch.get("uplink") or "", switch["ports"], labels):
        await write(method, path, data, what)


# A socket move reloads the router's network, and for a few seconds it does not
# answer (BP's RUTM11, 2026-10-31: the read straight after timed out). Waiting
# here keeps a Fix from taking that pause for a failure and putting the backup back.
SETTLE_TRIES = 10
SETTLE_WAIT = 3

# How long one Fix write may take to answer. Creating a network makes RutOS
# reload, and a RUT241 took more than the usual 10 s to answer creating the
# third one (RUT2M_R_00.07.24.8, 2026-11-12), so Fix took the slow reply for a
# failure and put the backup back. Fix only, never the everyday poll.
WRITE_TIMEOUT = 60


async def _read_settled(client: Any) -> dict[str, Any]:
    """The router's settings, once it answers again after a network reload."""
    for _ in range(SETTLE_TRIES - 1):
        try:
            return await read_snapshot(client)
        except Exception:  # noqa: BLE001 - still reloading; the last try raises
            await asyncio.sleep(SETTLE_WAIT)
    return await read_snapshot(client)


async def apply(client: Any, profile: dict[str, Any], identity: str,
                passwords: dict[str, str], *, wifi_names: bool = True,
                sockets: dict[str, str] | None = None,
                switch: dict[str, Any] | None = None,
                box: dict[str, str] | None = None) -> list[str]:
    """Make the router match `profile`. Returns what was changed, in order.

    Raises ApplyError on the first write the router refuses, carrying what
    had already been changed. `passwords` maps a Wi-Fi's `password_param` to
    the password typed for it; only new or unsecured networks need one.
    `wifi_names=False` stops short of renaming existing Wi-Fi or changing its
    password, so the router can be checked before anything is cut off.
    `sockets` is the socket plan's target ({port: network}). `switch` is a
    managed switch to set up: {client, socket, uplink, ports: {port: network}}.
    `box` is the box's own {mac, ip}, reserved so its address never moves.
    """
    done: list[str] = []
    zones, ifaces, dhcp = ENDPOINTS["zones"], ENDPOINTS["interfaces"], ENDPOINTS["dhcp"]

    async def write(method: str, path: str, data: dict[str, Any], what: str) -> None:
        try:
            if method == "POST":
                await client.async_post(path, {"data": data}, timeout=WRITE_TIMEOUT)
            else:
                await client.async_put(path, {"data": data}, timeout=WRITE_TIMEOUT)
        except Exception as err:
            # The router's own reason is shown, with what was done first.
            raise ApplyError(f"{what}: {err}", done) from err
        if method == "PUT" and isinstance(client, _DryRun):
            what += await _router_holds(client, path, data)
        done.append(what)

    async def switch_write(method: str, path: str, data: dict[str, Any], what: str) -> None:
        try:
            if method == "POST":
                await switch["client"].async_post(path, {"data": data})
            else:
                await switch["client"].async_put(path, {"data": data})
        except Exception as err:
            raise ApplyError(f"{what}: {err}", done) from err
        done.append(what)

    snap = await read_snapshot(client)
    missing = bad_passwords(profile, snap, identity, passwords)
    if missing:
        raise ApplyError(
            "Enter a Wi-Fi password of 8 to 63 characters for " + ", ".join(missing) + ".",
            done,
        )

    policy = profile.get("input_policy", {})
    forwarding = profile.get("forwarding", {})
    managed = [s for s in profile.get("segments", []) if s.get("managed")]

    # 1. Each managed network: its zone, then the network in it, then DHCP.
    for seg in managed:
        label = seg.get("label", seg["key"])
        res = Resolved(profile, snap)
        zone = res.zone_of[seg["key"]] or next(
            (z for z in snap["zones"] if z.get("name") == seg["zone"]), None,
        )
        want_zone = {
            "input": str(policy.get(seg["zone"], "reject")).upper(),
            "forward": "REJECT",
            "masq": "1" if "wan" in forwarding.get(seg["zone"], []) else "0",
        }
        if zone is None:
            await write("POST", zones, {"name": seg["zone"], "output": "ACCEPT", **want_zone},
                        f"Created the {label} firewall zone")
            zname = seg["zone"]
        else:
            zname = zone.get("name") or seg["zone"]
            change = _diff(zone, want_zone)
            if change:
                await write("PUT", f"{zones}/{zone['id']}", change,
                            f"Updated the {label} firewall zone")

        iface = res.iface_of[seg["key"]]
        want_iface = {"proto": "static", "netmask": "255.255.255.0", "enabled": "1"}
        if iface is None:
            # Unbridged, nothing in it: its Wi-Fi joins it directly. A socket
            # moved onto it later turns the bridge on (`_move_sockets`).
            await write("POST", ifaces, {
                "name": seg["key"], "ipaddr": seg["router_ip"], "area_type": "lan",
                "fwzone": zname, **want_iface, **_UNBRIDGED,
            }, f"Created the {label} network ({seg['router_ip']})")
        else:
            change: dict[str, Any] = _diff(iface, want_iface)
            if iface.get("name") not in _list((zone or {}).get("network")):
                change["fwzone"] = zname
            # Not when a socket is about to join it: it stays a bridge then.
            if (_empty_bridge(iface, snap.get("wifi"))
                    and seg["key"] not in (sockets or {}).values()):
                change.update(_UNBRIDGED)
            if change:
                await write("PUT", f"{ifaces}/{iface['id']}", change,
                            f"Updated the {label} network")

        # A network change reloads the router's network, and the read straight
        # after can time out (a socket move did on BP's RUTM11), so it waits.
        snap = await _read_settled(client)
        iface = _interface_for(snap, seg["router_ip"])
        if iface is None:
            raise ApplyError(f"The {label} network did not appear on the router.", done)
        start_ip, end_ip, lease = pool(seg)
        want_dhcp = {"start_ip": start_ip, "end_ip": end_ip, "leasetime": lease,
                     "enable_dhcpv4": "1"}
        server = dhcp_for(snap, iface)
        if server is None:
            # The pool's id must be its network's id, not its name.
            await write("POST", dhcp, {"id": iface["id"], "mode": "server", **want_dhcp},
                        f"Created the {label} addresses ({start_ip} to {end_ip})")
        else:
            change = _diff(server, want_dhcp)
            if change:
                await write("PUT", f"{dhcp}/{server['id']}", change,
                            f"Updated the {label} addresses")

    # 2. What each zone can reach. A managed zone gets exactly the layout's
    # list; the data zone only gains, because the customer's own links from
    # it are none of Sensa's business.
    snap = await read_snapshot(client)
    res = Resolved(profile, snap)
    for seg in profile.get("segments", []):
        zone = res.zone_of[seg["key"]]
        if zone is None:
            continue
        want = {res.zone_name(z) for z in forwarding.get(seg["zone"], [])}
        have = set(_list(zone.get("out")))
        new = want if seg.get("managed") else have | want
        if new != have:
            await write("PUT", f"{zones}/{zone['id']}", {"out": sorted(new)},
                        f"Set what {seg.get('label', seg['key'])} can reach: "
                        + (", ".join(sorted(new)) or "nothing"))

    # 3. The single ports a closed zone must still open.
    rules = ENDPOINTS["rules"]
    for seg in managed:
        zone = res.zone_of[seg["key"]]
        if zone is None or str(policy.get(seg["zone"], "reject")).lower() != "reject":
            continue
        zname = zone.get("name") or seg["zone"]
        for exc in profile.get("input_exceptions", []):
            ports = rule_ports(exc.get("dest_ports", []))
            if seg["zone"] in exc.get("zones", []) and not has_rule(snap["rules"], zname, ports):
                await write("POST", rules, {
                    "name": f"Allow-{exc['name'].upper()}-{zname}", "src": zname,
                    "dest_port": sorted(ports), "proto": _list(exc.get("proto")),
                    "target": "ACCEPT", "enabled": "1",
                }, f"Allowed {exc['name'].upper()} on {seg.get('label', seg['key'])}")
    for exc in profile.get("zone_exceptions", []):
        seg = next((s for s in managed if s["zone"] == exc.get("from")), None)
        zone = res.zone_of[seg["key"]] if seg else None
        if zone is None:
            continue
        zname = zone.get("name") or seg["zone"]
        dest = res.zone_name(exc.get("to", ""))
        ports = rule_ports(exc.get("dest_ports", []))
        if not has_rule(snap["rules"], zname, ports, dest=dest, all_ports=True):
            await write("POST", rules, {
                "name": f"Allow-{exc['name']}", "src": zname, "dest": dest,
                "dest_port": sorted(ports), "proto": _list(exc.get("proto")),
                "target": "ACCEPT", "enabled": "1",
            }, f"Allowed {exc.get('label') or exc['name']}")

    # 4. Radios on, GPS, and IPv6 off.
    for radio in snap["radios"]:
        if str(radio.get("enabled")) != "1":
            await write("PUT", f"{ENDPOINTS['radios']}/{radio['id']}", {"enabled": "1"},
                        f"Turned on Wi-Fi radio {radio['id']}")
    gps = snap.get("gps")
    if isinstance(gps, dict) and gps:
        change = _diff(gps, gps_wanted(profile, gps))
        if change:
            labels = dict(GPS_SETTINGS)
            on = [labels[k] for k, v in change.items() if v == "1" and k != "enabled"]
            what = []
            if "enabled" in change:
                what.append("Turned on GPS" if change["enabled"] == "1" else "Turned off GPS")
            if on:
                what.append("Turned on " + ", ".join(on))
            await write("PUT", ENDPOINTS["gps"], change, "; ".join(what))
    wan6 = wan6_of(snap)
    if wan6 is not None and str(wan6.get("enabled")) != "0":
        await write("PUT", f"{ifaces}/{wan6['id']}", {"enabled": "0"}, "Turned off IPv6 (wan6)")
    reserved = box_reservation(snap, box)
    if reserved and not reserved["lease"] and not reserved["taken"]:
        await write("POST", STATIC_LEASES,
                    {"name": "sensa-control", "mac": box["mac"].upper(), "ip": box["ip"]},
                    f"Reserved {box['ip']} for this Sensa box")

    # 5. Wired sockets, as `sockets` ({port: network}) says. A socket leaves
    # its old network before joining the new one. The plan never moves the
    # socket the box is on (`socket_plan`).
    if sockets:
        await _move_sockets(client, profile, sockets, write)

    # 6. A managed switch: its socket carries the networks its ports need,
    # tagged, then each of its ports joins its network. The data network and
    # the switch's uplink are never changed, so neither is ever cut off.
    if switch and switch.get("ports"):
        await _trunk(client, profile, switch, write)
        await _switch_vlans(profile, switch, switch_write)

    # 7. Wi-Fi last, and within it a name or password change on an existing
    # network goes at the very end: it drops everything on that network, so
    # every other change has gone through and been reported first.
    snap = await read_snapshot(client)
    wifi = ENDPOINTS["wifi"]
    renames: list[tuple[dict[str, Any], dict[str, str], str, str]] = []
    for w in wifi_plan(profile, snap, identity, Resolved(profile, snap)):
        have, name = w["have"], w["name"]
        # A Wi-Fi on both radios is two writes: the band tells the two lines apart.
        band = f" ({_BAND.get(w['band'], w['band'])})"
        want, key, change, drops = _wifi_change(w, passwords)
        if have is None:
            body = {"mode": "ap", "encryption": "psk2", "key": key, **want}
            # `device` only where there is a radio to choose. RutOS shows the
            # Radios field only on a router with more than one, and a RUT241
            # (one radio, RUT2M_R_00.07.24.8) answers 422 "Invalid option
            # (device)" to it, 2026-11-12.
            if len(snap["radios"]) > 1:
                body["device"] = [w["radio"]["id"]]
            await write("POST", wifi, body, f"Created the Wi-Fi {name}{band}")
            continue
        if change:
            await write("PUT", f"{wifi}/{have['id']}", change,
                        f"Updated the Wi-Fi {have.get('ssid')}{band}")
        if drops:
            renames.append((have, drops, name, band))
    if not wifi_names:
        return done
    for have, drops, name, band in renames:
        old = have.get("ssid")
        await write("PUT", f"{wifi}/{have['id']}", drops,
                    f"Renamed the Wi-Fi {old} to {name}{band}" if old != name
                    else f"Updated the Wi-Fi {name}{band}")
    return done
