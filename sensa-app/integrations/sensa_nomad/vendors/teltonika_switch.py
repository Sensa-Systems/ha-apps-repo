"""A managed Teltonika switch (TSW202) carrying the van's networks over one cable.

The switch hangs off one router socket. That socket carries the data network
untagged (the switch's own address is there) and every other network the
switch's ports need as a tagged VLAN; each switch port is untagged on one
network. Read and written over the switch's REST API as `admin`, the login the
box already holds for it (Settings, Devices).

What the switch reports (TSW202, TSW2_R_00.01.07.1, .241 on BP's network,
2026-11-01):

* `/port_based_vlan/config`: one row per VLAN, `{id, vlan, name, port1..port8,
  sfp1, sfp2}`, each port `u` (untagged), `t` (tagged) or `off`. VLAN 1 is the
  switch's own management VLAN (`/interfaces/config` `vlan_id: "1"`).
* `/forwarding_table/status`: `{mac, vlan, port}` per learned address, which is
  how the uplink is found (the port the router's MAC is on) and what is plugged
  into each port.

What it never does: change the uplink's VLAN 1 membership (the switch's own
address is reached over it), move the port this box is on, or delete a VLAN
it did not create.
"""

from __future__ import annotations

import re
from typing import Any

VLANS = "/port_based_vlan/config"
FDB = "/forwarding_table/status"
PORTS = "/ports_settings/status"

# A VLAN's name on the switch: what the network is, not "vlan3".
VLAN_NAMES = {"data": "YourDevices", "iot": "SmartDevices", "cctv": "Cameras"}


def _mac(value: Any) -> str:
    return re.sub(r"[^0-9A-F]", "", str(value or "").upper())


def _label(port: str) -> str:
    m = re.fullmatch(r"(port|sfp)(\d+)", port)
    if not m:
        return port
    return f"Port {m.group(2)}" if m.group(1) == "port" else f"SFP {m.group(2)}"


async def read_switch(client: Any) -> dict[str, Any]:
    """{vlans, fdb, ports}: the switch's VLAN rows, learned addresses and port links."""
    out: dict[str, Any] = {}
    for key, path in (("vlans", VLANS), ("fdb", FDB), ("ports", PORTS)):
        data = (await client.async_get(path)).get("data")
        out[key] = data if isinstance(data, list) else []
    return out


def port_names(sw: dict[str, Any]) -> list[str]:
    """The switch's ports in its own order, copper then SFP."""
    names = [str(p.get("id")) for p in sw.get("ports") or [] if p.get("id")]
    if not names:
        names = sorted({k for row in sw.get("vlans") or [] for k in row
                        if re.fullmatch(r"(port|sfp)\d+", k)})
    return sorted(names, key=lambda p: (p.startswith("sfp"), int(re.sub(r"\D", "", p) or 0)))


def uplink(sw: dict[str, Any], router_mac: str) -> str:
    """The port the router is on, or "" when the switch has not seen it."""
    mac = _mac(router_mac)
    return next((str(x.get("port")) for x in sw.get("fdb") or []
                 if mac and _mac(x.get("mac")) == mac), "")


def _vid_net(vids: dict[str, int]) -> dict[int, str]:
    return {v: k for k, v in vids.items()}


def switch_ports(sw: dict[str, Any], vids: dict[str, int], up: str,
                 known: dict[str, dict[str, Any]], box_mac: str = "",
                 placed: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every port: what is on it now and the network it should be on.

    [{port, label, link, uplink, box, network, vlan, target, devices}].
    `network` is the Sensa network the port is untagged on now ("" for a VLAN
    that is not one of the van's); `target` is the customer's choice from
    `placed`, else the network of what is plugged in when it all agrees, else
    where it is now ("" for a port on someone else's VLAN, which is left
    alone). The uplink and the box's port are always the data network.
    """
    by_vid = _vid_net(vids)
    box = _mac(box_mac)
    links = {str(p.get("id")): str(p.get("link", "")) == "1" for p in sw.get("ports") or []}
    out = []
    for port in port_names(sw):
        vid = next((int(r.get("vlan") or 0) for r in sw.get("vlans") or [] if r.get(port) == "u"), 0)
        devices = []
        for x in sw.get("fdb") or []:
            mac = _mac(x.get("mac"))
            if str(x.get("port")) != port or port == up or any(d["mac"] == mac for d in devices):
                continue
            k = known.get(mac) or {}
            devices.append({"mac": mac, "name": k.get("name") or ("This Sensa box" if mac == box else ""),
                            "network": "data" if mac == box else k.get("network") or ""})
        is_box = bool(box) and any(d["mac"] == box for d in devices)
        now = by_vid.get(vid, "")
        nets = {d["network"] for d in devices if d["network"]}
        if port == up or is_box:
            target = "data"
        elif (placed or {}).get(port) in vids:
            target = (placed or {})[port]
        elif len(nets) == 1 and next(iter(nets)) in vids:
            target = next(iter(nets))
        else:
            # On a VLAN that is not the van's with nothing known on it: left alone.
            target = now
        out.append({"port": port, "label": _label(port), "link": links.get(port),
                    "uplink": port == up, "box": is_box, "network": now, "vlan": vid,
                    "target": target, "devices": devices})
    return out


def changes(sw: dict[str, Any], vids: dict[str, int], up: str, target: dict[str, str],
            labels: dict[str, str] | None = None) -> list[tuple[str, str, dict[str, Any], str]]:
    """The writes that put each port on its `target` network ({port: key}).

    [(method, path, data, what)], in order: ports leave the VLANs they are
    untagged on first, then join their new one, because a port untagged in two
    VLANs at once is not a state to pass through. The uplink is tagged on every
    network the ports need beyond the data network.
    """
    labels = labels or {}
    rows = {int(r.get("vlan") or 0): r for r in sw.get("vlans") or []}
    ports = port_names(sw)
    used = {k for p, k in target.items() if k in vids and p != up}
    want: dict[int, dict[str, str]] = {}
    for k, vid in vids.items():
        if k == "data":
            row = {p: v for p, v in rows.get(1, {}).items() if p in ports}
            for p in ports:
                if p == up:
                    row[p] = "u"
                elif p in target:
                    row[p] = "u" if target[p] == "data" else "off"
            want[1] = row
        elif k in used:
            want[vid] = {p: "t" if p == up else ("u" if target.get(p) == k else "off") for p in ports}
    # A VLAN that is not the van's keeps everything except a port Sensa now places.
    for vid, row in rows.items():
        if vid not in want:
            want[vid] = {p: ("off" if p in target and p != up and row.get(p) == "u" else row.get(p, "off"))
                         for p in ports}

    def name(vid: int) -> str:
        k = _vid_net(vids).get(vid, "")
        return labels.get(k, k) if k else f"VLAN {vid}"

    out: list[tuple[str, str, dict[str, Any], str]] = []
    # 1. Off first.
    for vid, row in rows.items():
        off = {p: "off" for p, v in want[vid].items() if v == "off" and row.get(p, "off") != "off"}
        if off:
            out.append(("PUT", f"{VLANS}/{row['id']}", off,
                        f"Took switch {', '.join(_label(p) for p in off)} off {name(vid)}"))
    # 2. Then on.
    for vid, row in sorted(want.items()):
        have = rows.get(vid)
        on = {p: v for p, v in row.items() if v != "off" and (have or {}).get(p, "off") != v}
        k = _vid_net(vids).get(vid, "")
        new_name = VLAN_NAMES.get(k, "")
        if have is None:
            if not on:
                continue
            out.append(("POST", VLANS, {"vlan": str(vid), "name": new_name or f"vlan{vid}", **row},
                        f"Created {name(vid)} (VLAN {vid}) on the switch"))
            continue
        if new_name and k != "data" and have.get("name") != new_name:
            on["name"] = new_name
        if on:
            joined = [_label(p) for p, v in on.items() if v == "u" and p != up]
            what = (f"Put switch {', '.join(joined)} on {name(vid)}" if joined
                    else f"Carried {name(vid)} to the switch")
            out.append(("PUT", f"{VLANS}/{have['id']}", on, what))
    return out


def restore_changes(before: list[dict[str, Any]], now: list[dict[str, Any]]
                    ) -> list[tuple[str, str, dict[str, Any]]]:
    """The writes that put the VLAN rows back as `before` had them.

    VLANs created since are deleted; ports go off before they go back on,
    for the same reason as `changes`.
    """
    was = {str(r.get("id")): r for r in before}
    out: list[tuple[str, str, dict[str, Any]]] = []
    for r in now:
        if str(r.get("id")) not in was:
            out.append(("DELETE", f"{VLANS}/{r['id']}", {}))
    cur = {str(r.get("id")): r for r in now}
    for rid, r in was.items():
        c = cur.get(rid, {})
        off = {p: "off" for p, v in r.items() if re.fullmatch(r"(port|sfp)\d+", p)
               and v == "off" and c.get(p, "off") != "off"}
        if off:
            out.append(("PUT", f"{VLANS}/{rid}", off))
    for rid, r in was.items():
        c = cur.get(rid, {})
        back = {p: v for p, v in r.items() if (re.fullmatch(r"(port|sfp)\d+", p) or p == "name")
                and v != "off" and c.get(p) != v}
        if back:
            out.append(("PUT", f"{VLANS}/{rid}", back))
    return out


def same_rows(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    """True when two reads of the VLAN rows hold the same VLANs and ports."""
    def key(rows: list[dict[str, Any]]) -> list[tuple[str, ...]]:
        return sorted(tuple(f"{k}={v}" for k, v in sorted(r.items()) if not k.startswith("."))
                      for r in rows)
    return key(a) == key(b)
