"""Configure router: read a Teltonika router and check it against each layout.

Kept out of the websocket handler so the whole path, client to sections, runs
in a test and against a live router with nothing but a client.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from .const import (
    DOMAIN,
    ROUTER_LAYOUT_OPTION,
    ROUTER_SETUP_LABELS,
    ROUTER_WIRED_OPTION,
)
from .network_profile import LAYOUT_FILES, load_profile
from .router_fix import FixRefused, RouterFix, box_on_wifi
from .vendors import teltonika_switch
from .vendors.teltonika_apply import wifi_renames
from .vendors.teltonika_setup import (
    WIRED_NETWORKS,
    _mac,
    compare,
    default_identity,
    default_wired,
    network_vids,
    passwords_changeable,
    passwords_needed,
    read_snapshot,
    socket_plan,
    sockets,
    summarise,
)

# Which network a registered device belongs on, by its service class. Anything
# not named here is habitation kit and goes on the smart-device network.
_CCTV_CLASSES = {"ip_camera"}
_DATA_CLASSES = {"router", "network_switch", "switch", "nas", "satellite_internet"}


def _network_for(dev: dict[str, Any]) -> str:
    cls = str(dev.get("service_class") or dev.get("device_type") or "").lower()
    if cls in _CCTV_CLASSES:
        return "cctv"
    if cls in _DATA_CLASSES or not cls:
        return "data"
    return "iot"


def _known_devices(hass: Any) -> dict[str, dict[str, Any]]:
    """{MAC: {name, network}} for every device the box has registered."""
    from .sensor import _registered_macs_by_mac

    out: dict[str, dict[str, Any]] = {}
    for runtime in (hass.data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        for mac, dev in _registered_macs_by_mac(runtime).items():
            out[_mac(mac)] = {"name": dev.get("display_name") or dev.get("name") or "",
                              "network": _network_for(dev), "ip": dev.get("ip_address") or ""}
    return out


def _box_link(host: str) -> tuple[str, str]:
    """(address, MAC) of the box's own adapter towards `host`; blanks when unknown."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 443))
            local = s.getsockname()[0]
        import ifaddr  # HA's own dependency (the network integration)

        for adapter in ifaddr.get_adapters():
            if any(ip.ip == local for ip in adapter.ips):
                path = Path("/sys/class/net") / str(adapter.name) / "address"
                return local, path.read_text(encoding="utf-8").strip() if path.exists() else ""
        return local, ""
    except Exception:  # noqa: BLE001 - unknown is an answer here
        return "", ""


def _switch_clients(hass: Any) -> dict[str, tuple[str, Any]]:
    """{device_id: (MAC, client)} for every managed Teltonika switch the box holds a login for."""
    out: dict[str, tuple[str, Any]] = {}
    for runtime in (hass.data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        for dev_id, coordinator in (runtime.get("vendor_coordinators") or {}).items():
            data = getattr(coordinator, "data", None) or {}
            client = getattr(coordinator, "client", None)
            if data.get("hw_mobile") is not False or client is None or not getattr(client, "_host", ""):
                continue
            mac = _mac(getattr(coordinator, "_mac_address", "")) or _mac(str(dev_id).removeprefix("disc-"))
            out[str(dev_id)] = (mac, client)
    return out


async def _switch_context(hass: Any, snap: dict[str, Any], socks: list[dict[str, Any]] | None,
                          router_id: str) -> dict[str, Any] | None:
    """The managed switch on one of the router's sockets, read: {device_id, socket, sw, client}.

    None when no TSW2xx is seen on a socket. Without a login for it (not
    added in Settings, Devices) `sw` is None and `error` says why.
    """
    hub = next((s for s in socks or [] if s["switch"] == "managed"), None)
    if hub is None:
        return None
    clients = _switch_clients(hass)
    on_socket = {d["mac"] for d in hub["devices"]}
    found = next(((dev_id, c) for dev_id, (mac, c) in clients.items() if mac in on_socket), None)
    ctx: dict[str, Any] = {"socket": hub["port"], "label": hub["label"], "device_id": "", "sw": None,
                           "client": None, "error": ""}
    if found is None:
        ctx["error"] = (f"Add the TSW202 on {hub['label']} in Settings, Devices with its password, "
                        "so Configure can set up its ports.")
        return ctx
    ctx["device_id"], ctx["client"] = found
    try:
        ctx["sw"] = await teltonika_switch.read_switch(found[1])
    except Exception as err:  # noqa: BLE001 - the board says why
        ctx["error"] = f"Could not read the TSW202 on {hub['label']}: {err}"
    # The router as the switch sees it: its own socket MACs, or the MAC its id carries.
    ctx["router_macs"] = ({_mac(p.get("mac")) for p in snap.get("ports") or [] if p.get("mac")}
                          | {_mac(router_id.removeprefix("disc-"))})
    return ctx


def _attach_switch(plan: dict[str, Any], profile: dict[str, Any], ctx: dict[str, Any]) -> None:
    """Add the managed switch to the socket plan: `plan["switch"]`, its ports and their networks."""
    sctx = ctx.get("switch")
    if not sctx:
        return
    out: dict[str, Any] = {"device_id": sctx["device_id"], "model": "TSW202", "socket": sctx["socket"],
                           "label": sctx["label"], "readable": sctx["sw"] is not None,
                           "error": sctx["error"], "uplink": "", "ports": [], "target": {}}
    plan["switch"] = out
    if sctx["sw"] is None:
        return
    sw = sctx["sw"]
    up = next((u for u in (teltonika_switch.uplink(sw, m) for m in sctx["router_macs"] if m) if u), "")
    out["uplink"] = up
    if not up:
        out["error"] = "The TSW202 cannot see the router yet, so its ports cannot be set up."
        return
    placed = (ctx.get("arrangement") or {}).get("switch_ports") or {}
    out["ports"] = teltonika_switch.switch_ports(sw, network_vids(profile), up, ctx["known"],
                                                 ctx["box_mac"], placed)
    socket_net = (plan.get("target") or {}).get(sctx["socket"]) or next(
        (s["network"] for s in plan.get("sockets") or [] if s["port"] == sctx["socket"]), "")
    if socket_net and socket_net != "data":
        out["error"] = (f"{sctx['label']} must be on Your devices for the TSW202 to carry every network. "
                        "Drag Your devices onto it.")
        return
    out["target"] = {p["port"]: p["target"] for p in out["ports"]
                     if p["target"] and not p["uplink"]}


async def _wired_context(hass: Any, client: Any, snap: dict[str, Any], entry: Any,
                         device_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    """What the socket plan needs: known devices, the box's own socket, the counts."""
    host = getattr(client, "_host", "")
    address, mac = await hass.async_add_executor_job(_box_link, host)
    if not mac:
        mac = next((x.get("macaddr") or "" for x in snap.get("leases") or []
                    if address and x.get("ipaddr") == address), "")
    on_wifi = await hass.async_add_executor_job(box_on_wifi, host)
    known = _known_devices(hass)
    options = getattr(entry, "options", None) or {}
    saved = (options.get(ROUTER_WIRED_OPTION) or {}).get(device_id)
    socks = sockets(profile, snap, known, mac)
    wired = saved or default_wired(socks, on_wifi is False)
    return {"known": known, "box_mac": mac, "box_ip": address, "box_wired": on_wifi is False,
            "wired": {k: int(wired.get(k) or 0) for k in WIRED_NETWORKS}, "saved": bool(saved),
            "arrangement": (options.get(ROUTER_LAYOUT_OPTION) or {}).get(device_id) or None,
            "switch": await _switch_context(hass, snap, socks, device_id)}


def save_wired(hass: Any, entry: Any, device_id: str, wired: dict[str, int]) -> None:
    """Keep the customer's wired-device counts for this router."""
    ledger = dict(entry.options.get(ROUTER_WIRED_OPTION) or {})
    ledger[device_id] = {k: max(0, min(48, int(wired.get(k) or 0))) for k in WIRED_NETWORKS}
    hass.config_entries.async_update_entry(entry, options={**entry.options, ROUTER_WIRED_OPTION: ledger})


def save_arrangement(hass: Any, entry: Any, device_id: str, arrangement: dict[str, Any] | None) -> None:
    """Keep what the customer arranged on the board; None goes back to Sensa's plan."""
    ledger = dict(entry.options.get(ROUTER_LAYOUT_OPTION) or {})
    if arrangement:
        ledger[device_id] = {key: {str(k): str(v) for k, v in (arrangement.get(key) or {}).items()}
                             for key in ("sockets", "switches", "devices", "switch_ports")}
    else:
        ledger.pop(device_id, None)
    hass.config_entries.async_update_entry(entry, options={**entry.options, ROUTER_LAYOUT_OPTION: ledger})


def _plan_for(profile: dict[str, Any], snap: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    plan = socket_plan(profile, snap, ctx["wired"], ctx["known"], ctx["box_mac"], ctx["box_wired"],
                       ctx.get("arrangement"))
    _attach_switch(plan, profile, ctx)
    plan["box"] = {"mac": ctx["box_mac"], "ip": ctx.get("box_ip") or ""}
    return plan


def _switch_job(plan: dict[str, Any]) -> dict[str, Any] | None:
    """What Fix needs to set the switch up: {device_id, socket, uplink, ports}; None for nothing."""
    sw = plan.get("switch") or {}
    if not sw.get("target") or not sw.get("device_id"):
        return None
    return {"device_id": sw["device_id"], "socket": sw["socket"], "uplink": sw["uplink"],
            "ports": dict(sw["target"])}


async def async_router_preview(
    hass: Any, client: Any, identity: str | None = None, *,
    device_id: str = "", entry: Any = None,
) -> dict[str, Any]:
    """{host, identity, wired, layouts} for the popup, or {error} saying why not.

    ONE read of the router, checked against every Nomad managed layout, so
    the popup shows each option's check side by side whatever is chosen.
    `layouts` is {single|secure: {label, items, sockets}}, `items` one card
    per part of the van (summarise), each carrying its technical `sections`
    for Nerd Mode's Details; `sockets` is the socket plan (what is plugged
    in, where each socket goes, the switches the van needs).
    """
    host = getattr(client, "_host", "")
    if client is None or not host:
        return {
            "error": "Enter the router's admin password and press Save first.",
        }
    try:
        snap = await read_snapshot(client)
    except Exception as err:  # noqa: BLE001 - the popup shows the reason
        # The owner reads the plain sentence; the technical reason is for Nerd Mode.
        return {"error": f"Sensa cannot reach your router at {host} right now. Check it is switched "
                         "on, then open this screen again.",
                "detail": str(err)}
    ident = identity or default_identity(snap, hass.config.location_name or "")
    layouts = {}
    ctx: dict[str, Any] = {}
    for layout in LAYOUT_FILES:
        profile = await hass.async_add_executor_job(load_profile, layout)
        if not ctx:
            ctx = await _wired_context(hass, client, snap, entry, device_id, profile)
        plan = _plan_for(profile, snap, ctx)
        layouts[layout] = {
            "label": ROUTER_SETUP_LABELS[layout],
            "items": summarise(profile, compare(profile, snap, ident, plan)),
            "sockets": plan,
            # The new Wi-Fi networks Fix needs a password typed for.
            "passwords": passwords_needed(profile, snap, ident),
            # The Wi-Fi Fix keeps, whose password the customer may change.
            "changeable": passwords_changeable(profile, snap, ident),
            # The Wi-Fi Fix renames: whoever presses Fix on it must
            # reconnect to the new name, so the popup says which before.
            "renames": wifi_renames(profile, snap, ident, {}),
        }
    return {"host": host, "identity": ident, "wired": ctx.get("wired") or {},
            "box_wired": ctx.get("box_wired", False), "layouts": layouts}


def _fixes(hass: Any) -> dict[str, dict[str, Any]]:
    """Per router: {running, result} of its last Fix, for the popup."""
    return hass.data.setdefault("sensa_nomad_router_fix", {})


def router_fix_status(hass: Any, device_id: str) -> dict[str, Any]:
    """{running, result} for the popup; {} when no Fix has run since start-up."""
    return dict(_fixes(hass).get(device_id) or {})


def _router_fix(hass: Any, client: Any, device_id: str) -> RouterFix:
    from ._log import sensa_log

    def log(level: str, message: str) -> None:
        sensa_log(hass, level, message, source="system")

    switches = {dev_id: c for dev_id, (_mac_, c) in _switch_clients(hass).items()}
    return RouterFix(hass, client, device_id, log=log, switches=switches)


def _start(hass: Any, device_id: str, job: Any) -> None:
    """Run `job` (the Fix or its resume) away from the popup's connection.

    The router's restart drops a screen on its Wi-Fi, which would otherwise
    take the Fix down with it. The popup reads how it went from the status.
    """
    slot = _fixes(hass).setdefault(device_id, {})
    slot.update(running=True, result=None)

    async def _go() -> None:
        try:
            slot["result"] = await job
        except Exception as err:  # noqa: BLE001 - shown in the popup
            slot["result"] = {"done": [], "error": str(err)}
        finally:
            slot["running"] = False

    hass.async_create_background_task(_go(), f"sensa_nomad router fix {device_id}")


async def async_router_fix(
    hass: Any, client: Any, device_id: str, layout: str, identity: str | None,
    passwords: dict[str, str], entry: Any = None,
) -> dict[str, Any]:
    """Fix: change the router to `layout`, all or nothing (router_fix.py).

    Answers at once: {started}, {done: []} when nothing needs changing, or
    {error} when Fix did not start and nothing was changed. How a started
    Fix went is in `router_fix_status`, and in the System Log.
    """
    host = getattr(client, "_host", "")
    if client is None or not host:
        return {"error": "Enter the router's admin password and press Save first.", "done": []}
    if layout not in LAYOUT_FILES:
        return {"error": f"Unknown layout {layout}.", "done": []}
    if router_fix_status(hass, device_id).get("running"):
        return {"error": "This router is already being configured.", "done": []}
    profile = await hass.async_add_executor_job(load_profile, layout)
    fix = _router_fix(hass, client, device_id)
    try:
        snap = await read_snapshot(client)
        ident = identity or default_identity(snap, hass.config.location_name or "")
        # The socket plan is worked out once, here, and Fix holds the router to it.
        plan = _plan_for(profile, snap, await _wired_context(
            hass, client, snap, entry, device_id, profile))
        target, switch = plan["target"], _switch_job(plan)
        if not await fix.needs_fix(profile, ident, passwords, target, switch, plan["box"]):
            return {"done": []}
    except FixRefused as err:
        return {"error": str(err), "done": []}
    except Exception as err:  # noqa: BLE001 - the popup shows the reason
        return {"error": f"Sensa cannot reach your router at {host} right now, so nothing was changed.",
                "detail": str(err), "done": []}
    _start(hass, device_id, fix.run(layout, ROUTER_SETUP_LABELS[layout], profile, ident, passwords,
                                    target, switch, plan["box"]))
    return {"started": True, "done": []}


async def async_router_fix_resume(hass: Any, client: Any, device_id: str) -> bool:
    """Finish a Fix that stopped part way, if there is one. True when started.

    Called on start-up and whenever the popup opens, so a router is checked
    or put back as soon as the box can reach it again.
    """
    if client is None or router_fix_status(hass, device_id).get("running"):
        return False
    fix = _router_fix(hass, client, device_id)
    if not await fix.journal.read():
        return False

    async def profile_for(layout: str) -> dict[str, Any]:
        return await hass.async_add_executor_job(load_profile, layout)

    _start(hass, device_id, fix.resume(profile_for))
    return True
