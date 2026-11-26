"""Wi-Fi as an internet source: the router joins a nearby Wi-Fi as a client.

Starlink's own Wi-Fi, a campsite's or a hotel's: the router scans, the
customer picks one and types its password, and the router joins it as a WAN.
It then shows in WAN Priority like any other source and can be dragged into
the failover order.

Every path and field here was measured on BP's RUTM11 (RUTM_R_00.07.25.3,
2026-11-07) and read off the router's own web UI bundle
(`app.wireless.app-*.js`, its Scan and Join pages):

- Scan: `POST /wireless/actions/scan` {"data": {"device": "radio0"}}, one
  radio per call. Each row has ssid (absent for a hidden network), bssid,
  channel, signal (dBm), quality / quality_max, mode ("Access Point"), and
  `encryption` {enabled, authentication: ["psk"|"sae"|"802.1x"], wpa, wep}.
- Join: `POST /wireless/interfaces/config` with mode "sta", device [radio],
  ssid, encryption, key and `network: "wifiN"` (a name no Wi-Fi or interface
  uses yet, the UI's `getAutoNetworkName`). The router itself then creates
  the DHCP interface with area_type "wan", puts it in the wan firewall zone,
  and adds it to both failover policies, last. Created with enabled "0"
  unless told otherwise.
- Forget: `DELETE /wireless/interfaces/config/<id>` takes the interface, the
  zone entry and the failover members away with it (measured: the config
  read back identical to before).

Only one Wi-Fi client is kept: joining another network rewrites the one that
is there. A radio that is a client follows the joined network's channel, so
the van's own Wi-Fi on that band moves channel with it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

SCAN = "/wireless/actions/scan"
WIFI = "/wireless/interfaces/config"
WIFI_STATUS = "/wireless/interfaces/status"
RADIOS = "/wireless/devices/config"
IFACES = "/interfaces/config"
IFACE_STATUS = "/interfaces/status"

# How long a join waits for the router to associate and get an address. The
# web UI allows 40 s on a multi-core router and 65 s on a single core.
JOIN_WAIT = 60
_POLL = 3


def _rows(resp: Any) -> list[dict[str, Any]]:
    data = resp.get("data") if isinstance(resp, dict) else None
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def encryption_of(row: dict[str, Any]) -> str | None:
    """The `encryption` value to join `row` with, or None when we cannot.

    Mirrors the web UI: WEP and enterprise (802.1x) networks cannot be joined
    with a password alone. WPA3 joins as sae, a WPA2/WPA3 network as psk2,
    which every RutOS client supports.
    """
    enc = row.get("encryption") or {}
    if not enc.get("enabled"):
        return "none"
    if enc.get("wep"):
        return None
    auth = enc.get("authentication") or []
    if "802.1x" in auth:
        return None
    if "psk" in auth:
        return "psk2"
    if "sae" in auth:
        return "sae"
    return None


def band_of(radio_id: str, rows: list[dict[str, Any]]) -> str:
    """"2.4GHz" or "5GHz" for a radio, from a scan row's band (2 or 5)."""
    band = next((r.get("band") for r in rows if r.get("band")), None)
    if band in (5, "5"):
        return "5GHz"
    if band in (2, "2"):
        return "2.4GHz"
    return "5GHz" if radio_id.endswith("1") else "2.4GHz"


def networks(scans: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """One entry per network name, strongest first, from {radio: scan rows}.

    A network heard on both bands is listed once, on the band it is heard
    best on, because that is the radio the router will join it with.
    """
    best: dict[str, dict[str, Any]] = {}
    for radio, rows in scans.items():
        band = band_of(radio, rows)
        for row in rows:
            ssid = row.get("ssid")
            if not ssid or row.get("mode", "Access Point") != "Access Point":
                continue
            signal = row.get("signal")
            signal = int(signal) if isinstance(signal, (int, float)) else -100
            quality, qmax = row.get("quality") or 0, row.get("quality_max") or 70
            entry = {
                "ssid": ssid,
                "radio": radio,
                "band": band,
                "channel": row.get("channel"),
                "signal": signal,
                "percent": max(0, min(100, round(100 * quality / qmax))) if qmax else 0,
                "encryption": encryption_of(row),
                "secure": bool((row.get("encryption") or {}).get("enabled")),
            }
            if ssid not in best or signal > best[ssid]["signal"]:
                best[ssid] = entry
    return sorted(best.values(), key=lambda n: n["signal"], reverse=True)


async def async_scan(client: Any) -> dict[str, Any]:
    """{networks, current} for the Scan list. A radio that fails is skipped."""
    radios = [r.get("id") for r in _rows(await client.async_get(RADIOS))
              if r.get("id") and str(r.get("enabled", "1")) == "1"]
    scans: dict[str, list[dict[str, Any]]] = {}
    errors = []
    for radio in radios:
        try:
            scans[radio] = _rows(await client.async_post(SCAN, {"data": {"device": radio}},
                                                         timeout=30))
        except Exception as err:  # noqa: BLE001 (one radio failing still lists the other)
            _LOGGER.debug("Wi-Fi scan on %s failed: %s", radio, err)
            errors.append(f"{radio}: {err}")
    if not scans and errors:
        return {"error": "The router could not scan: " + "; ".join(errors)}
    return {"networks": networks(scans), "current": await async_current(client)}


def _client_iface(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((r for r in rows if r.get("mode") == "sta"), None)


async def async_current(client: Any) -> dict[str, Any] | None:
    """The Wi-Fi the router is a client of: {id, ssid, network, up, signal}."""
    sta = _client_iface(_rows(await client.async_get(WIFI)))
    if sta is None:
        return None
    status = next((s for s in _rows(await client.async_get(WIFI_STATUS))
                   if str(s.get("id")) == str(sta.get("id"))), {})
    return {
        "id": str(sta.get("id")),
        "ssid": sta.get("ssid") or "",
        "network": sta.get("network") or "",
        "enabled": str(sta.get("enabled", "1")) == "1",
        "up": status.get("up") is True,
        "signal": status.get("signal"),
    }


def _free_network(wifi: list[dict[str, Any]], ifaces: list[dict[str, Any]]) -> str:
    """The router web UI's name for a new client network: wifi0, wifi1, ..."""
    used = {w.get("network") for w in wifi} | {i.get("name") for i in ifaces}
    n = 0
    while f"wifi{n}" in used:
        n += 1
    return f"wifi{n}"


async def _wait_connected(client: Any, sta_id: str, network: str,
                          wait: float = JOIN_WAIT) -> dict[str, Any]:
    """Poll until the client is associated and its network has an address."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    associated = False
    while True:
        wifi = next((s for s in _rows(await client.async_get(WIFI_STATUS))
                     if str(s.get("id")) == sta_id), {})
        associated = associated or wifi.get("up") is True
        iface = next((i for i in _rows(await client.async_get(IFACE_STATUS))
                      if network in (i.get("name"), i.get("interface"))), {})
        addrs = iface.get("ipv4-address") or []
        ip = addrs[0].get("address") if addrs and isinstance(addrs[0], dict) else None
        if associated and ip:
            return {"connected": True, "ip": ip}
        if loop.time() >= deadline:
            return {"connected": False, "associated": associated}
        await asyncio.sleep(_POLL)


async def async_join(client: Any, ssid: str, radio: str, encryption: str,
                     password: str = "", *, wait: float = JOIN_WAIT) -> dict[str, Any]:
    """Join `ssid` on `radio`. {connected, ip} or {error}."""
    if encryption not in ("none", "psk2", "sae"):
        return {"error": "This Wi-Fi needs a kind of sign-in the router cannot use."}
    if encryption != "none" and not 8 <= len(password) <= 63:
        return {"error": "A Wi-Fi password is 8 to 63 characters."}
    wifi = _rows(await client.async_get(WIFI))
    want = {"device": [radio], "mode": "sta", "ssid": ssid, "encryption": encryption,
            "enabled": "1"}
    if encryption != "none":
        want["key"] = password
    sta = _client_iface(wifi)
    try:
        if sta is None:
            network = _free_network(wifi, _rows(await client.async_get(IFACES)))
            made = await client.async_post(WIFI, {"data": {**want, "network": network}})
            sta = (made or {}).get("data") or {}
        else:
            network = sta.get("network") or ""
            made = await client.async_put(f"{WIFI}/{sta['id']}", {"data": want})
            sta = (made or {}).get("data") or sta
        # Read back what the router kept, never assume the write landed.
        sta = next((w for w in _rows(await client.async_get(WIFI))
                    if str(w.get("id")) == str(sta.get("id"))), None)
        if sta is None:
            return {"error": "The router did not keep the Wi-Fi client."}
        if str(sta.get("enabled")) != "1":
            await client.async_put(f"{WIFI}/{sta['id']}", {"data": {"enabled": "1"}})
    except Exception as err:  # noqa: BLE001 (the router's own reason is shown)
        return {"error": f"The router refused: {err}"}
    result = await _wait_connected(client, str(sta["id"]), sta.get("network") or network, wait)
    if result["connected"]:
        return result
    if encryption != "none" and not result.get("associated"):
        return {"error": f"Could not join {ssid}. Check the password and try again."}
    return {"error": f"Joined {ssid} but it gave the router no address. Try again, "
                     "or move closer."}


async def async_forget(client: Any) -> dict[str, Any]:
    """Remove the Wi-Fi client, and with it its WAN and failover entries."""
    sta = _client_iface(_rows(await client.async_get(WIFI)))
    if sta is None:
        return {}
    try:
        await client.async_delete(f"{WIFI}/{sta['id']}")
    except Exception as err:  # noqa: BLE001
        return {"error": f"The router refused: {err}"}
    return {}
