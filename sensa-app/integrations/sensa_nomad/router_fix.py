"""Fix, all or nothing: the router ends up fully changed and checked, or as it was.

RutOS has no way to make several changes at once: every REST write takes
effect as it is sent. So Fix takes the router's own full backup first and puts
it back if anything goes wrong:

1. Check first, change nothing: passwords typed, and something to change. A
   box on a Wi-Fi network Fix renames (most boxes) is given the new name and
   password as a second saved network first (box_wifi.py), so it can come
   back after the restart to check the router or put it back. If it cannot
   be, nothing is changed.
2. Take the router's backup and keep it on the box, with a journal saying a
   Fix is running.
3. Make every change except renaming existing Wi-Fi or changing its password.
4. Read the router back: a dry run of the same changes must write nothing.
5. Rename the Wi-Fi and change its passwords, then restart the router.
6. Once it answers again, check everything. Anything wrong puts the backup back.
7. Remove the journal and the backup.

A managed switch Fix sets up (vendors/teltonika_switch.py) has no backup of
its own to take, so its VLAN rows are read before anything changes and kept
in the journal; putting the router back puts those rows back too.

The journal is what makes a half-way stop safe. If the box restarts or loses
the router part way, the journal is still there, and the next time the box
can reach the router (on start-up, opening the popup, or pressing Fix) it
finishes the check or puts the backup back. A router is never left half
changed by a Fix the box knows about. Design: docs/devices/teltonika-router.md,
"Fix, all or nothing".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .box_wifi import BoxWifiError, add_box_wifi, box_wifi_ssid
from .vendors import teltonika_switch
from .vendors.teltonika_apply import (
    ApplyError,
    apply,
    bad_passwords,
    networks_down,
    pending,
    wifi_renames,
)
from .vendors.teltonika_setup import read_snapshot, wan6_of

_LOGGER = logging.getLogger(__name__)

# Where a running Fix keeps its journal and the router's backup, one per router.
FIX_DIR = ("nomad", ".sensa_router_fix")

# After a restart RutOS takes a minute or two to answer. One login every
# RETRY seconds keeps well clear of its ban on login bursts.
BOOT_WAIT = 45
RETRY = 20
BACK_TIMEOUT = 15 * 60
# How long the networks may take to come up once the router answers again.
NETWORK_WAIT = 2 * 60
# A backup that could not be put back is tried again at most this often.
FAILED_RETRY = 10 * 60

# Journal states. Every state but DONE means the router may differ from the
# backup and must be checked or put back before anything else is changed.
CHANGING = "changing"     # changes being made; a stop here puts the backup back
RESTARTING = "restarting"  # all changed, router restarting, not yet checked
RESTORING = "restoring"   # putting the backup back
FAILED = "failed"         # the backup could not be put back; a person must look

Sleep = Callable[[float], Awaitable[None]]


class FixRefused(Exception):
    """Fix did not start, and nothing on the router was changed."""


def _paths(hass: Any, device_id: str) -> tuple[Path, Path]:
    base = Path(hass.config.path(*FIX_DIR))
    safe = "".join(c for c in device_id if c.isalnum() or c in "-_")
    return base / f"{safe}.json", base / f"{safe}.backup"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _age(state: dict[str, Any]) -> float:
    """Seconds since the journal was last written; a journal with no time is old."""
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(state["updated"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return float("inf")


class Journal:
    """The running Fix for one router, on disk so it outlives a restart."""

    def __init__(self, hass: Any, device_id: str) -> None:
        self.hass = hass
        self.path, self.backup_path = _paths(hass, device_id)

    def _read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _save_backup(self, data: bytes) -> None:
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.backup_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.backup_path)

    def _clear(self) -> None:
        for p in (self.path, self.backup_path):
            p.unlink(missing_ok=True)

    async def read(self) -> dict[str, Any] | None:
        return await self.hass.async_add_executor_job(self._read)

    async def write(self, data: dict[str, Any]) -> None:
        data["updated"] = _now()
        await self.hass.async_add_executor_job(self._write, data)

    async def save_backup(self, data: bytes) -> None:
        await self.hass.async_add_executor_job(self._save_backup, data)

    async def backup(self) -> bytes:
        return await self.hass.async_add_executor_job(self.backup_path.read_bytes)

    async def clear(self) -> None:
        await self.hass.async_add_executor_job(self._clear)


async def pending_fix_devices(hass: Any) -> list[str]:
    """The routers with a Fix journal on disk, by the device id it was saved under."""
    base = Path(hass.config.path(*FIX_DIR))

    def _list() -> list[str]:
        return sorted(p.stem for p in base.glob("*.json")) if base.is_dir() else []

    return await hass.async_add_executor_job(_list)


def box_on_wifi(host: str) -> bool | None:
    """Whether the box reaches `host` over Wi-Fi. None when it cannot tell.

    The address the box uses towards the router, matched to its network
    adapter's name (`wlan0`, `wlp2s0`). HA Container runs on the host's
    network, so these are the box's own adapters.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 443))
            local = s.getsockname()[0]
        import ifaddr  # HA's own dependency (the network integration)

        for adapter in ifaddr.get_adapters():
            if any(ip.ip == local for ip in adapter.ips):
                return str(adapter.nice_name or adapter.name).lower().startswith("wl")
    except Exception:  # noqa: BLE001 - unknown is an answer here
        return None
    return None


def _ids(snap: dict[str, Any]) -> dict[str, Any]:
    """What a restored router must have exactly as the backup had it."""
    out: dict[str, Any] = {
        key: sorted(str(x.get("id")) for x in snap.get(key) or [])
        for key in ("interfaces", "dhcp", "zones", "rules", "wifi", "static_leases")
    }
    out["ssids"] = sorted(f"{w.get('id')}={w.get('ssid')}" for w in snap.get("wifi") or [])
    out["gps"] = str((snap.get("gps") or {}).get("enabled", ""))
    out["wan6"] = str((wan6_of(snap) or {}).get("enabled", ""))
    out["sockets"] = sorted(f"{b.get('id')}={','.join(sorted(map(str, b.get('ports') or [])))}"
                            for b in snap.get("bridges") or [])
    out["vlan_devices"] = sorted(str(d.get("name") or d.get("id")) for d in snap.get("vlan_devices") or [])
    return out


class RouterFix:
    """One Fix on one router. `run` is the press; `resume` picks up a stopped one."""

    def __init__(self, hass: Any, client: Any, device_id: str, *,
                 log: Callable[[str, str], None], sleep: Sleep = asyncio.sleep,
                 on_wifi: Callable[[str], bool | None] = box_on_wifi,
                 box_ssid: Callable[[], Awaitable[str | None]] = box_wifi_ssid,
                 add_wifi: Callable[[str, str | None], Awaitable[None]] = add_box_wifi,
                 switches: dict[str, Any] | None = None) -> None:
        self.hass, self.client, self.device_id = hass, client, device_id
        # {device_id: client} for the managed switches the box holds a login for.
        self.switches = switches or {}
        self.journal = Journal(hass, device_id)
        self.log, self.sleep, self.on_wifi = log, sleep, on_wifi
        self.box_ssid, self.add_wifi = box_ssid, add_wifi
        self.box_rename: dict[str, Any] | None = None
        self.host = getattr(client, "_host", "")

    def _switch(self, plan: dict[str, Any] | None) -> dict[str, Any] | None:
        """The switch plan with its client, as `apply` takes it; None without one."""
        if not plan or not plan.get("ports"):
            return None
        client = self.switches.get(plan.get("device_id") or "")
        if client is None:
            raise FixRefused("The managed switch could not be reached, so nothing was changed.")
        return {**plan, "client": client}

    # -- the press ------------------------------------------------------------

    async def needs_fix(self, profile: dict[str, Any], identity: str,
                        passwords: dict[str, str], sockets: dict[str, str] | None = None,
                        switch: dict[str, Any] | None = None,
                        box: dict[str, str] | None = None) -> bool:
        """Step 1, writing nothing. Raises FixRefused with the reason a Fix must not start.

        `sockets` is the socket plan's target ({port: network}), worked out
        once before the press and kept in the journal, so the check after the
        restart holds the router to the same plan.
        """
        if await self.journal.read():
            raise FixRefused("This router is still being configured. Open this screen again in a minute.")
        snap = await read_snapshot(self.client)
        missing = bad_passwords(profile, snap, identity, passwords)
        if missing:
            raise FixRefused("Enter a Wi-Fi password of 8 to 63 characters for " + ", ".join(missing) + ".")
        try:
            if not await pending(self.client, profile, identity, passwords, sockets=sockets,
                                 switch=self._switch(switch), box=box):
                return False
        except ApplyError:
            pass  # a network has to be created first: there is work to do
        # Most boxes are on the router's Wi-Fi. One whose Wi-Fi Fix renames
        # must be given the new name first, or it cannot come back after the
        # restart to check the router or put it back.
        renames = wifi_renames(profile, snap, identity, passwords)
        self.box_rename = None
        try:
            ssid = await self.box_ssid()
        except BoxWifiError as err:
            if renames and await self.hass.async_add_executor_job(self.on_wifi, self.host):
                raise FixRefused(
                    "This Sensa box is on the router's Wi-Fi, which Configure renames, and it could not "
                    f"be given the new Wi-Fi name ({err}). Nothing was changed."
                ) from err
            return True
        if ssid:
            self.box_rename = next(
                (r for r in renames if r["from"].lower() == ssid.lower()), None,
            )
        return True

    def _full_timeout(self) -> Any:
        """The router client's healthy timeout for the whole Fix (`full_timeout`)."""
        hold = getattr(self.client, "full_timeout", None)
        return hold() if hold else contextlib.nullcontext()

    async def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """The whole Fix (`_run`), never on the 1.5 s timeout of a missed poll."""
        with self._full_timeout():
            return await self._run(*args, **kwargs)

    async def resume(self, profile_for: Callable[[str], Awaitable[dict[str, Any]]]) -> dict[str, Any] | None:
        """Finish a Fix that stopped part way (`_resume`), on the healthy timeout."""
        with self._full_timeout():
            return await self._resume(profile_for)

    async def _run(self, layout: str, layout_label: str, profile: dict[str, Any],
                   identity: str, passwords: dict[str, str],
                   sockets: dict[str, str] | None = None,
                   switch: dict[str, Any] | None = None,
                   box: dict[str, str] | None = None) -> dict[str, Any]:
        """The whole Fix. {done, restored?, error?}. Nothing is left half changed.

        `switch` is the managed switch plan ({device_id, socket, uplink,
        ports}), worked out with the socket plan and kept in the journal, as
        is `box`, the box's own {mac, ip} to reserve.
        """
        sockets = sockets or {}
        if not await self.needs_fix(profile, identity, passwords, sockets, switch, box):
            self.log("info", f"Router already set to {layout_label} ({self.host}); nothing changed")
            return {"done": []}

        # The box's own Wi-Fi, before anything on the router. Kept beside the
        # old one, so the box rejoins whichever the router ends up with.
        if self.box_rename:
            new = self.box_rename
            try:
                await self.add_wifi(new["to"], new["password"])
            except BoxWifiError as err:
                raise FixRefused(
                    f"This Sensa box could not save the new Wi-Fi {new['to']}, so nothing was changed: {err}"
                ) from err
            self.log("info", f"Configure router ({self.host}): saved the Wi-Fi {new['to']} on this box, "
                     f"beside {new['from']}")

        # 2. The backup. Without one, nothing is changed.
        try:
            before = await read_snapshot(self.client)
            backup = await self.client.async_backup_create()
        except Exception as err:  # noqa: BLE001 - the popup shows the reason
            # Logged too: the popup's copy is held in memory and a restart loses it.
            why = f"Could not take a backup of the router, so nothing was changed: {err}"
            self.log("warning", f"Configure router ({self.host}): {why}")
            raise FixRefused(why) from err
        sw = self._switch(switch)
        if sw:
            try:
                switch = {**switch, "before": (await teltonika_switch.read_switch(sw["client"]))["vlans"]}
            except Exception as err:
                raise FixRefused(f"Could not read the managed switch, so nothing was changed: {err}") from err
        await self.journal.save_backup(backup)
        state: dict[str, Any] = {
            "state": CHANGING, "host": self.host, "layout": layout, "label": layout_label,
            "identity": identity, "started": _now(), "done": [], "before": _ids(before),
            "sockets": sockets, "switch": switch if sw else None, "box": box,
        }
        await self.journal.write(state)

        # 3 to 5. The changes, the check, then the Wi-Fi names.
        try:
            done = await apply(self.client, profile, identity, passwords, wifi_names=False,
                               sockets=sockets, switch=sw, box=box)
            state["done"] = done
            await self.journal.write(state)
            left = await pending(self.client, profile, identity, passwords, wifi_names=False,
                                 sockets=sockets, switch=sw, box=box)
            if left:
                raise ApplyError("the router did not keep: " + "; ".join(left), done)
            renamed = await apply(self.client, profile, identity, passwords, sockets=sockets,
                                  switch=sw, box=box)
            done = done + renamed
        except Exception as err:  # noqa: BLE001 - any stop puts the backup back
            reason = str(err)
            changed = getattr(err, "done", None) or state["done"]
            self.log("warning", f"Configure router stopped ({self.host}): {reason}. Changed first: "
                     + ("; ".join(changed) or "nothing") + ". Putting the router back as it was.")
            return await self._restore(state, f"Configure stopped: {reason}")

        state.update(done=done, state=RESTARTING)
        await self.journal.write(state)
        self.log("info", f"Router set to {layout_label} ({self.host}): " + "; ".join(done)
                 + "; restarting the router")
        try:
            await self.client.async_reboot()
        except Exception as err:  # noqa: BLE001 - the check after decides
            _LOGGER.debug("Router fix: restart call failed (%s); checking anyway", err)
        return await self._after_restart(state, profile)

    # -- after the restart, and putting it back ------------------------------

    async def _wait_back(self) -> bool:
        """True once the router answers a fresh login and a full config read.

        A login alone is not back: a RUTM11 just up answers it, then times out
        /interfaces/config for a while, and the check after read that timeout
        as a failed restore (.177, 2026-10-31), costing a second restore.
        """
        await self.sleep(BOOT_WAIT)
        waited = BOOT_WAIT
        while waited < BACK_TIMEOUT:
            try:
                self.client._token = None
                await self.client.async_get("/system/device/status")
                await read_snapshot(self.client)
                return True
            except Exception as err:  # noqa: BLE001 - still restarting
                _LOGGER.debug("Router fix: router not back yet (%s)", err)
            await self.sleep(RETRY)
            waited += RETRY
        return False

    async def _networks_down(self, profile: dict[str, Any]) -> list[str]:
        """The networks still not running after NETWORK_WAIT, [] once all are.

        A status the router will not give is "could not ask" and passes, as it
        did before this check existed.
        """
        waited = 0
        while True:
            try:
                down = await networks_down(self.client, profile)
            except Exception as err:  # noqa: BLE001 - could not ask is not down
                _LOGGER.debug("Router fix: could not read the networks' status (%s)", err)
                return []
            if not down or waited >= NETWORK_WAIT:
                return down
            await self.sleep(RETRY)
            waited += RETRY

    async def _after_restart(self, state: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        # No passwords: they are only asked for networks that do not exist
        # yet, and after a Fix every network does. A missing one fails the check.
        passwords: dict[str, str] = {}
        left: list[str] | None = None
        if await self._wait_back():
            # A check that could not run is "could not ask", not "wrong": a
            # RUTM11 just back still times out a read now and then (.178,
            # 2026-11-08, /dhcp/servers/ipv4/config), and that alone put back
            # a Fix that had worked. Ask again; only a real answer decides.
            waited = 0
            while left is None:
                try:
                    left = await pending(self.client, profile, state["identity"], passwords,
                                         sockets=state.get("sockets") or {},
                                         switch=self._switch(state.get("switch")), box=state.get("box"))
                except Exception as err:  # noqa: BLE001 - asked again below
                    if waited >= BACK_TIMEOUT:
                        break
                    _LOGGER.debug("Router fix: could not check yet (%s)", err)
                    await self.sleep(RETRY)
                    waited += RETRY
        if left is None:
            # The journal stays: the next time the box reaches the router it checks again.
            self.log("warning", f"Configure router ({self.host}): the router has not come back after "
                     "restarting. The box will check it as soon as it can reach it.")
            return {"done": state["done"], "error": "The router is still restarting. "
                    "Open this screen again in a few minutes."}
        if left:
            self.log("warning", f"Configure router ({self.host}): after restarting the router still needs: "
                     + "; ".join(left) + ". Putting the router back as it was.")
            return await self._restore(state, "After restarting, the router was not as Configure left it")
        # Saved is not running: every network must be up too. They come up a
        # little after the router answers, so this waits before it decides.
        down = await self._networks_down(profile)
        if down:
            self.log("warning", f"Configure router ({self.host}): after restarting, these networks are not "
                     "running: " + ", ".join(down) + ". Putting the router back as it was.")
            return await self._restore(state, "After restarting, the router did not start the "
                                       + ", ".join(down) + " network" + ("s" if len(down) > 1 else ""))
        await self.journal.clear()
        self.log("info", f"Configure router ({self.host}): restarted and checked, all {len(state['done'])} changes in place")
        return {"done": state["done"]}

    async def _restore(self, state: dict[str, Any], why: str) -> dict[str, Any]:
        state["state"] = RESTORING
        state["why"] = why
        await self.journal.write(state)
        try:
            await self.client.async_backup_restore(await self.journal.backup())
            back = await self._wait_back()
            now = _ids(await read_snapshot(self.client)) if back else None
        except Exception as err:  # noqa: BLE001 - reported, journal kept
            now, back = None, False
            _LOGGER.warning("Router fix: putting the backup back failed: %s", err)
        switch_back = await self._restore_switch(state.get("switch"))
        # A journal from before a check was added holds fewer keys; compare those.
        if (now is not None and {k: now.get(k) for k in state["before"]} == state["before"]
                and switch_back):
            await self.journal.clear()
            self.log("warning", f"Configure router ({self.host}): {why}. The router has been put back "
                     "exactly as it was before Configure.")
            return {"done": [], "restored": True, "error": f"{why}. Your router has been put back as it was."}
        state["state"] = FAILED
        await self.journal.write(state)
        self.log("error", f"Configure router ({self.host}): {why}, and the router could not be put back "
                 + ("(it has not come back)" if not back else "(it does not match the backup)")
                 + f". The backup is kept on the box at {self.journal.backup_path}.")
        return {"done": [], "error": f"{why}, and the router could not be put back yet. "
                "The box will try again. Contact support if this stays."}

    async def _restore_switch(self, plan: dict[str, Any] | None) -> bool:
        """Put the switch's VLAN rows back as the journal has them. True when they are."""
        if not plan or plan.get("before") is None:
            return True
        client = self.switches.get(plan.get("device_id") or "")
        if client is None:
            _LOGGER.warning("Router fix: the managed switch is not reachable to put back")
            return False
        try:
            now = (await teltonika_switch.read_switch(client))["vlans"]
            for method, path, data in teltonika_switch.restore_changes(plan["before"], now):
                if method == "DELETE":
                    await client.async_delete(path)
                else:
                    await client.async_put(path, {"data": data})
            return teltonika_switch.same_rows(plan["before"],
                                              (await teltonika_switch.read_switch(client))["vlans"])
        except Exception as err:  # noqa: BLE001 - reported, journal kept
            _LOGGER.warning("Router fix: putting the switch back failed: %s", err)
            return False

    async def _resume(self, profile_for: Callable[[str], Awaitable[dict[str, Any]]]) -> dict[str, Any] | None:
        """Finish a Fix that stopped part way. None when there is none."""
        state = await self.journal.read()
        if not state:
            return None
        if state["state"] == RESTARTING:
            return await self._after_restart(state, await profile_for(state["layout"]))
        if state["state"] == FAILED and _age(state) < FAILED_RETRY:
            # Each attempt restarts the router; opening the popup again must not.
            return {"done": [], "error": "Your router could not be put back yet. The box will try "
                    "again in a few minutes. Contact support if this stays."}
        # CHANGING (the box stopped mid-change), RESTORING or FAILED: put it back.
        return await self._restore(state, state.get("why") or "Configure was stopped part way")
