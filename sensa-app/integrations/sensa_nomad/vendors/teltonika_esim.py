"""eSIM data for a Teltonika router: what is left on it, and buying more.

The router knows its profiles and the data it has carried (`_fetch_esim_data`).
What is LEFT on a plan only the seller knows, so for an eSIM Sensa sold that
comes from MySensa, which bought it from eSIM Go. The box never talks to
MySensa itself: every call goes through the App (`/api/esim/*`), which signs
it with the licence the way breakdown cover and the household are fetched.

Buying, start to finish:

  1. the customer picks a plan on the eSIM card; `async_start_order` asks
     MySensa for an order and a checkout link, which the card shows as a QR to
     pay on a phone (nothing is typed on the van);
  2. `_follow` asks for the order every few seconds until MySensa says it is
     paid and the eSIM is ready, then hands the activation code to the router
     (`async_esim_download`), which downloads the profile and switches to it;
  3. it watches the router until the download is done or has failed and tells
     MySensa which, so the order reads Installed there too.

The customer's OWN eSIM, from any provider, takes the same road with no
payment: `async_start_own` gets a one-time phone page from MySensa instead of
a checkout link, the customer scans or pastes the provider's QR there, and
from "ready" on it installs exactly as a bought one does.

A top-up on an eSIM Sensa already sold has no code: the data lands on the same
eSIM, so step 2 ends at "ready". The order in progress is saved, so a restart
part way through picks it up again rather than leaving a paid order on the Hub
that never reaches the router.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .._log import sensa_log
from ..const import SENSA_LOG_SOURCE_SYSTEM

_LOGGER = logging.getLogger(__name__)

_STORE_KEY = "sensa_nomad.esim_orders"
_STORE_VERSION = 1

# What is left on a plan moves slowly and MySensa asks eSIM Go for it, so it is
# asked for on its own clock, not every 30 s poll.
BALANCE_INTERVAL_S = 30 * 60
# How often the order is asked for while the customer is paying, and for how
# long. A checkout link Stripe has not been paid on within the hour is dead.
_ORDER_POLL_S = 5
_ORDER_GIVE_UP_S = 60 * 60
# The router's download: how often to look, and when to call it failed.
_INSTALL_POLL_S = 5
_INSTALL_GIVE_UP_S = 10 * 60

# Order states MySensa reports (the box-facing contract).
READY = "ready"
FINISHED = ("installed", "failed", "expired")
# Waiting on the customer's phone: paying, or entering their own eSIM's code.
WAITING = ("awaiting_payment", "awaiting_code")


def _ok(resp: Any) -> bool:
    """A 2xx answer from the App. A 4xx comes back as a body carrying
    `http_status`, not as an exception (`api/client.py`)."""
    return isinstance(resp, dict) and not resp.get("http_status")


async def async_refresh_balance(coordinator: Any, sensa_client: Any) -> None:
    """Ask MySensa what is left on each eSIM on this router.

    A 2xx replaces the cache (an ICCID it does not list is one Sensa did not
    sell, so it has no balance); anything else keeps it, because "could not
    ask" must never show as an empty plan.
    """
    esim = (coordinator.data or {}).get("esim") or {}
    iccids = [p.get("iccid") for p in esim.get("profiles") or [] if p.get("iccid")]
    if not (iccids and sensa_client):
        return
    try:
        resp = await sensa_client.async_post_json(
            "/api/esim/balance", {"iccids": iccids}, timeout=30,
        )
    except Exception as err:  # noqa: BLE001 (offline is normal; keep the cache)
        _LOGGER.debug("eSIM balance: kept the cached answer (%s)", err)
        return
    if not _ok(resp):
        _LOGGER.debug("eSIM balance: kept the cached answer (%s)", resp)
        return
    coordinator.esim_balance = {
        str(row.get("iccid")): {
            "remaining_mb": row.get("remaining_mb"),
            "total_mb": row.get("total_mb"),
            "expires_at": row.get("expires_at"),
            "plan_name": row.get("plan_name"),
            "sensa": True,
        }
        for row in resp.get("esims") or []
        if isinstance(row, dict) and row.get("iccid")
    }
    coordinator.esim_balance_at = time.monotonic()


async def async_get_plans(sensa_client: Any) -> dict[str, Any]:
    """The plans Sensa sells: `{"plans": [...]}`, or `{"error": ...}` when
    MySensa could not be asked (the card says so rather than "no plans")."""
    if sensa_client is None:
        return {"error": "offline"}
    try:
        resp = await sensa_client.async_post_json("/api/esim/plans", {}, timeout=30)
    except Exception as err:  # noqa: BLE001
        _LOGGER.info("eSIM plans: could not ask MySensa (%s)", err)
        return {"error": "offline"}
    if not _ok(resp):
        return {"error": str(resp.get("error") or resp.get("detail") or "unavailable")}
    return {"plans": [p for p in resp.get("plans") or [] if isinstance(p, dict)]}


class EsimOrders:
    """The one order in progress per router, and the task following it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._saved: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._loaded = False

    async def _load(self) -> None:
        if not self._loaded:
            self._saved = (await self._store.async_load()) or {}
            self._loaded = True

    async def _save(self) -> None:
        await self._store.async_save(self._saved)

    async def async_resume(self, device_id: str, coordinator: Any, sensa_client: Any) -> None:
        """Pick up an order a restart interrupted."""
        await self._load()
        order = self._saved.get(device_id)
        if order and order.get("status") not in FINISHED:
            coordinator.esim_order = order
            self._start(device_id, coordinator, sensa_client)

    async def async_start_order(
        self, device_id: str, coordinator: Any, sensa_client: Any,
        plan_id: str, iccid: str = "",
    ) -> dict[str, Any]:
        """Ask MySensa for an order and its checkout link, and follow it.

        `iccid` set means a top-up on that eSIM. Returns the order as the card
        shows it, or `{"error": ...}`.
        """
        payload: dict[str, Any] = {"plan_id": plan_id}
        if iccid:
            payload["iccid"] = iccid
        resp = await self._ask_to_start(
            device_id, coordinator, sensa_client, "/api/esim/order", payload,
        )
        if "error" in resp:
            return resp
        order = {
            "order_id": str(resp["order_id"]),
            "plan_id": plan_id,
            "topup": bool(iccid),
            "checkout_url": str(resp.get("checkout_url") or ""),
            "status": str(resp.get("status") or "awaiting_payment"),
            "started": time.time(),
            "error": "",
        }
        await self._set(device_id, coordinator, order)
        self._start(device_id, coordinator, sensa_client)
        return order

    async def async_start_own(
        self, device_id: str, coordinator: Any, sensa_client: Any,
    ) -> dict[str, Any]:
        """Ask MySensa for a phone page to enter the customer's own eSIM, and
        follow it. Returns the order as the card shows it, or `{"error": ...}`."""
        resp = await self._ask_to_start(
            device_id, coordinator, sensa_client, "/api/esim/own-start", {},
        )
        if "error" in resp:
            return resp
        order = {
            "order_id": str(resp["order_id"]),
            "own": True,
            "topup": False,
            "phone_url": str(resp.get("phone_url") or ""),
            "status": str(resp.get("status") or "awaiting_code"),
            "started": time.time(),
            "error": "",
        }
        await self._set(device_id, coordinator, order)
        self._start(device_id, coordinator, sensa_client)
        return order

    async def _ask_to_start(
        self, device_id: str, coordinator: Any, sensa_client: Any,
        path: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """MySensa's answer to a new order, or `{"error": ...}`."""
        await self._load()
        current = self._saved.get(device_id) or {}
        if current and current.get("status") not in FINISHED and device_id in self._tasks:
            return {"error": "busy", "order": current}
        if sensa_client is None:
            return {"error": "offline"}
        esim = (coordinator.data or {}).get("esim") or {}
        try:
            resp = await sensa_client.async_post_json(
                path, {**payload, "eid": esim.get("eid") or ""}, timeout=30,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("eSIM order: could not ask MySensa (%s)", err)
            return {"error": "offline"}
        if not _ok(resp) or not resp.get("order_id"):
            return {"error": str(resp.get("error") or resp.get("detail") or "refused")}
        return resp

    async def async_retry(self, device_id: str, coordinator: Any, sensa_client: Any) -> dict[str, Any]:
        """Install a paid (or the customer's own) eSIM again after the router
        failed to.

        MySensa keeps a paid order whose install failed at `ready`, code and
        all, so following it again installs it again (most often the van had
        no other internet for the download the first time).
        """
        await self._load()
        order = dict(self._saved.get(device_id) or {})
        if order.get("status") != "failed" or order.get("topup") or not order.get("order_id"):
            return {"error": "nothing_to_retry"}
        order.update(status="paid", error="")
        await self._set(device_id, coordinator, order)
        self._start(device_id, coordinator, sensa_client)
        return order

    async def async_cancel(self, device_id: str, coordinator: Any) -> None:
        """Stop following an order the customer walked away from. Payment (or
        a code) that arrives later still lands on MySensa; the box just stops
        waiting."""
        task = self._tasks.pop(device_id, None)
        if task is not None:
            task.cancel()
        await self._load()
        order = self._saved.get(device_id)
        if order and order.get("status") in WAITING:
            self._saved.pop(device_id, None)
            await self._save()
            coordinator.esim_order = {}
            coordinator.async_update_listeners()

    async def _set(self, device_id: str, coordinator: Any, order: dict[str, Any]) -> None:
        self._saved[device_id] = order
        await self._save()
        coordinator.esim_order = order
        coordinator.async_update_listeners()

    def _start(self, device_id: str, coordinator: Any, sensa_client: Any) -> None:
        task = self._tasks.get(device_id)
        if task is not None and not task.done():
            return
        self._tasks[device_id] = self._hass.async_create_background_task(
            self._follow(device_id, coordinator, sensa_client),
            f"sensa_nomad esim order {device_id}",
        )

    async def _follow(self, device_id: str, coordinator: Any, sensa_client: Any) -> None:
        order = dict(self._saved.get(device_id) or {})
        try:
            await self._await_ready(device_id, coordinator, sensa_client, order)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 (never leave an order silently stuck)
            _LOGGER.exception("eSIM order %s stopped", order.get("order_id"))
            order.update(status="failed", error=str(err))
            await self._set(device_id, coordinator, order)
        finally:
            self._tasks.pop(device_id, None)

    async def _await_ready(
        self, device_id: str, coordinator: Any, sensa_client: Any, order: dict[str, Any],
    ) -> None:
        order_id = order.get("order_id")
        deadline = (order.get("started") or time.time()) + _ORDER_GIVE_UP_S
        while True:
            try:
                resp = await sensa_client.async_post_json(
                    "/api/esim/order-status", {"order_id": order_id}, timeout=30,
                )
            except Exception as err:  # noqa: BLE001 (could not ask: ask again)
                _LOGGER.debug("eSIM order %s: could not ask (%s)", order_id, err)
                resp = None
            if _ok(resp):
                status = str(resp.get("status") or "")
                if status and status != order.get("status"):
                    order.update(status=status, error=str(resp.get("error") or ""))
                    await self._set(device_id, coordinator, order)
                if status == READY:
                    break
                if status in FINISHED or status == "failed":
                    return
            elif isinstance(resp, dict) and resp.get("http_status") == 404:
                order.update(status="failed", error="Order not found")
                await self._set(device_id, coordinator, order)
                return
            if time.time() > deadline and order.get("status") in WAITING:
                order.update(status="expired")
                await self._set(device_id, coordinator, order)
                return
            await asyncio.sleep(_ORDER_POLL_S)

        code = str(resp.get("activation_code") or "")
        if order.get("topup") or not code:
            # A top-up lands on the eSIM already in the router.
            order.update(status="installed")
            await self._set(device_id, coordinator, order)
            await async_refresh_balance(coordinator, sensa_client)
            sensa_log(
                self._hass, "info", "eSIM data added", source=SENSA_LOG_SOURCE_SYSTEM,
            )
            return

        order.update(status="installing")
        await self._set(device_id, coordinator, order)
        ok, error = await self._install(
            coordinator, code, resp, name="My eSIM" if order.get("own") else "Sensa",
        )
        order.update(status="installed" if ok else "failed", error=error)
        await self._set(device_id, coordinator, order)
        try:
            await sensa_client.async_post_json(
                "/api/esim/installed",
                {"order_id": order_id, "ok": ok, "error": error},
                timeout=30,
            )
        except Exception as err:  # noqa: BLE001 (MySensa catches up on its own)
            _LOGGER.info("eSIM order %s: could not report the install (%s)", order_id, err)
        if ok:
            sensa_log(self._hass, "info", "New eSIM installed", source=SENSA_LOG_SOURCE_SYSTEM)
            await coordinator.async_request_refresh()
            await async_refresh_balance(coordinator, sensa_client)
        else:
            sensa_log(
                self._hass, "warning", f"eSIM install failed: {error}",
                source=SENSA_LOG_SOURCE_SYSTEM,
            )

    async def _install(
        self, coordinator: Any, code: str, ready: dict[str, Any], *, name: str,
    ) -> tuple[bool, str]:
        """Hand the code to the router and wait for its download to end."""
        client = coordinator.client
        esim = (coordinator.data or {}).get("esim") or {}
        modem = esim.get("modem") or ""
        if not modem:
            fresh = (await client.async_get_esim_data()).get("esim") or {}
            modem = fresh.get("modem") or ""
        before = {p.get("iccid") for p in esim.get("profiles") or []}
        try:
            await client.async_esim_download(
                modem, code,
                name=name,
                apn=str(ready.get("apn") or ""),
            )
        except Exception as err:  # noqa: BLE001
            return False, f"The router refused the eSIM: {err}"

        want = str(ready.get("iccid") or "")
        deadline = time.monotonic() + _INSTALL_GIVE_UP_S
        while time.monotonic() < deadline:
            await asyncio.sleep(_INSTALL_POLL_S)
            try:
                now = (await client.async_get_esim_data()).get("esim") or {}
            except Exception as err:  # noqa: BLE001 (the link drops while it switches)
                _LOGGER.debug("eSIM install: router busy (%s)", err)
                continue
            if now.get("errors"):
                return False, "; ".join(now["errors"])
            iccids = {p.get("iccid") for p in now.get("profiles") or []}
            arrived = (want in iccids) if want else bool(iccids - before)
            if arrived and not now.get("busy"):
                return True, ""
        return False, "The router did not finish downloading the eSIM"


def orders(hass: HomeAssistant) -> EsimOrders:
    """The one `EsimOrders` for this HA instance."""
    from ..const import DOMAIN

    bucket = hass.data.setdefault(f"{DOMAIN}_esim_orders", None)
    if bucket is None:
        bucket = EsimOrders(hass)
        hass.data[f"{DOMAIN}_esim_orders"] = bucket
    return bucket
