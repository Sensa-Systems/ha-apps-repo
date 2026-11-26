"""Teltonika UBUS/JSON-RPC API client.

Communicates with Teltonika RutOS routers via the UBUS JSON-RPC interface
at http://<host>/ubus. Uses gsmctl commands via file.exec for cellular
metrics (compatible with all RutOS firmware versions).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ..const import TELTONIKA_DEFAULT_SSL

_LOGGER = logging.getLogger(__name__)

# UBUS JSON-RPC constants
_NULL_SESSION = "00000000000000000000000000000000"
_UBUS_ACCESS_DENIED = 6


class TeltonikUbusApiError(Exception):
    """Base exception for UBUS API errors."""


class TeltonikUbusAuthError(TeltonikUbusApiError):
    """Authentication failed."""


class TeltonikUbusConnectionError(TeltonikUbusApiError):
    """Connection to the router failed."""


class TeltonikUbusClient:
    """Async client for the Teltonika UBUS/JSON-RPC API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        ssl: bool = TELTONIKA_DEFAULT_SSL,
        session: aiohttp.ClientSession,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._session = session
        self._base_url = f"{'https' if ssl else 'http'}://{host}/ubus"
        self._ubus_session: str = _NULL_SESSION
        self._rpc_id: int = 0

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    # -- Authentication -------------------------------------------------------

    async def async_login(self) -> str:
        """Authenticate and store the UBUS session token."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "call",
            "params": [
                _NULL_SESSION,
                "session",
                "login",
                {"username": self._username, "password": self._password},
            ],
        }

        try:
            async with self._session.post(
                self._base_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 403:
                    raise TeltonikUbusAuthError("Login rejected (HTTP 403)")
                resp.raise_for_status()
                data = await resp.json()
        except TeltonikUbusAuthError:
            raise
        except aiohttp.ClientError as err:
            raise TeltonikUbusConnectionError(f"Connection failed: {err}") from err
        except asyncio.TimeoutError as err:
            raise TeltonikUbusConnectionError("Login timed out") from err

        result = data.get("result")
        if not result or len(result) < 2 or result[0] != 0:
            raise TeltonikUbusAuthError(
                f"Login failed: {result}"
            )

        session_id = result[1].get("ubus_rpc_session")
        if not session_id:
            raise TeltonikUbusAuthError("No session token in login response")

        self._ubus_session = session_id
        _LOGGER.debug("UBUS login successful, session=%s...", session_id[:8])
        return session_id

    # -- Core RPC -------------------------------------------------------------

    async def async_call(
        self,
        obj: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        _retry: bool = True,
    ) -> dict[str, Any]:
        """Call a UBUS method. Auto-retries login on session expiry."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "call",
            "params": [
                self._ubus_session,
                obj,
                method,
                params or {},
            ],
        }

        try:
            async with self._session.post(
                self._base_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 403 and _retry:
                    await self.async_login()
                    return await self.async_call(obj, method, params, _retry=False)
                resp.raise_for_status()
                data = await resp.json()
        except (TeltonikUbusAuthError, TeltonikUbusConnectionError):
            raise
        except aiohttp.ClientError as err:
            raise TeltonikUbusConnectionError(f"RPC call failed: {err}") from err
        except asyncio.TimeoutError as err:
            raise TeltonikUbusConnectionError("RPC call timed out") from err

        result = data.get("result")
        if not result:
            raise TeltonikUbusApiError(f"Empty result from {obj}.{method}")

        # UBUS returns [code, {data}] — code 0 is success, 6 is access denied
        if isinstance(result, list):
            code = result[0] if result else -1
            if code == _UBUS_ACCESS_DENIED and _retry:
                await self.async_login()
                return await self.async_call(obj, method, params, _retry=False)
            if code != 0:
                raise TeltonikUbusApiError(
                    f"UBUS error {code} from {obj}.{method}"
                )
            return result[1] if len(result) > 1 else {}

        return result

    async def async_exec(self, command: str, args: list[str]) -> str:
        """Execute a command on the router via file.exec and return stdout."""
        result = await self.async_call(
            "file", "exec", {"command": command, "params": args}
        )
        return (result.get("stdout") or "").strip()

    # -- Data fetchers --------------------------------------------------------

    async def async_get_router_data(self) -> dict[str, Any]:
        """Fetch all router data in parallel."""
        results = await asyncio.gather(
            self._fetch_cellular_data(),
            self._fetch_wan_status(),
            self._fetch_connected_clients(),
            self._fetch_gps_data(),
            return_exceptions=True,
        )

        data: dict[str, Any] = {}
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.warning("Partial data fetch failed: %s", result)
                continue
            data.update(result)
        return data

    async def _fetch_cellular_data(self) -> dict[str, Any]:
        """Fetch cellular metrics via gsmctl commands."""
        data: dict[str, Any] = {}

        # Run gsmctl commands in parallel
        commands = {
            "signal_rssi": ["/usr/sbin/gsmctl", ["-q"]],
            "signal_rsrp": ["/usr/sbin/gsmctl", ["-W"]],
            "connection_type": ["/usr/sbin/gsmctl", ["-t"]],
            "operator": ["/usr/sbin/gsmctl", ["-o"]],
        }

        tasks = {
            key: self.async_exec(cmd, args)
            for key, (cmd, args) in commands.items()
        }

        results = await asyncio.gather(
            *tasks.values(), return_exceptions=True
        )

        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                _LOGGER.debug("gsmctl %s failed: %s", key, result)
                continue
            if key in ("signal_rssi", "signal_rsrp"):
                data[key] = _parse_dbm(result)
            else:
                data[key] = result if result else None

        # Data usage — gsmctl -D returns TX/RX in bytes
        try:
            usage_raw = await self.async_exec("/usr/sbin/gsmctl", ["-D"])
            tx_mb, rx_mb = _parse_data_usage(usage_raw)
            data["data_tx_mb"] = tx_mb
            data["data_rx_mb"] = rx_mb
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Data usage fetch failed: %s", err)

        return data

    async def _fetch_wan_status(self) -> dict[str, Any]:
        """Fetch WAN interface status."""
        try:
            result = await self.async_call("network.interface.wan", "status")
            up = result.get("up", False)
            # Derive WAN source from the interface protocol
            proto = result.get("proto", "unknown")
            source_map = {
                "wwan": "Mobile",
                "dhcp": "Ethernet",
                "static": "Ethernet",
                "pppoe": "Ethernet",
            }
            wan_source = source_map.get(proto, proto.title())
            return {"wan_up": up, "wan_source": wan_source}
        except TeltonikUbusApiError as err:
            _LOGGER.debug("WAN status fetch failed: %s", err)
            return {}

    async def _fetch_connected_clients(self) -> dict[str, Any]:
        """Fetch connected client count from DHCP leases."""
        try:
            result = await self.async_call("luci-rpc", "getDHCPLeases")
            leases = result.get("dhcp_leases", [])
            return {"connected_clients": len(leases)}
        except TeltonikUbusApiError:
            # Fallback: try iwinfo for Wi-Fi clients
            try:
                output = await self.async_exec("/usr/bin/iwinfo", ["wlan0", "assoclist"])
                # Each associated client has a MAC line
                count = sum(1 for line in output.splitlines() if ":" in line and len(line) > 10)
                return {"connected_clients": count}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Connected clients fetch failed: %s", err)
                return {}

    async def _fetch_gps_data(self) -> dict[str, Any]:
        """Fetch GPS data."""
        try:
            result = await self.async_call("gps", "info")
            lat = result.get("latitude")
            lon = result.get("longitude")
            speed = result.get("speed")
            return {
                "gps_latitude": _parse_float(lat),
                "gps_longitude": _parse_float(lon),
                "gps_speed": _parse_float(speed),
            }
        except TeltonikUbusApiError:
            # Fallback: gpsctl commands
            try:
                lat_raw = await self.async_exec("/usr/sbin/gpsctl", ["-i"])
                lon_raw = await self.async_exec("/usr/sbin/gpsctl", ["-x"])
                speed_raw = await self.async_exec("/usr/sbin/gpsctl", ["-v"])
                return {
                    "gps_latitude": _parse_float(lat_raw),
                    "gps_longitude": _parse_float(lon_raw),
                    "gps_speed": _parse_float(speed_raw),
                }
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("GPS fetch failed: %s", err)
                return {}


# -- Parsing helpers ----------------------------------------------------------


def _parse_dbm(value: str) -> int | None:
    """Parse a dBm string like '-67 dBm' or '-67' to an integer."""
    if not value:
        return None
    cleaned = value.replace("dBm", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    """Parse a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_data_usage(raw: str) -> tuple[float | None, float | None]:
    """Parse gsmctl -D output to TX/RX in MB.

    Output format varies by firmware. Common formats:
      'TX: 123456 bytes, RX: 789012 bytes'
      '123456 789012'
    """
    if not raw:
        return None, None

    raw_lower = raw.lower()

    # Try 'TX: N bytes, RX: N bytes' format
    if "tx:" in raw_lower and "rx:" in raw_lower:
        parts = raw_lower.split(",")
        tx_bytes = rx_bytes = None
        for part in parts:
            part = part.strip()
            if part.startswith("tx:"):
                tx_bytes = _extract_number(part)
            elif part.startswith("rx:"):
                rx_bytes = _extract_number(part)
        return (
            round(tx_bytes / 1_048_576, 2) if tx_bytes is not None else None,
            round(rx_bytes / 1_048_576, 2) if rx_bytes is not None else None,
        )

    # Try space-separated bytes format
    parts = raw.strip().split()
    if len(parts) >= 2:
        try:
            tx = float(parts[0])
            rx = float(parts[1])
            return round(tx / 1_048_576, 2), round(rx / 1_048_576, 2)
        except ValueError:
            pass

    return None, None


def _extract_number(text: str) -> float | None:
    """Extract the first number from a string."""
    import re

    match = re.search(r"[\d.]+", text)
    if match:
        try:
            return float(match.group())
        except ValueError:
            pass
    return None
