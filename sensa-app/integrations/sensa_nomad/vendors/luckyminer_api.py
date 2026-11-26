"""Lucky Miner LV08 Pro client, the AxeOS HTTP API.

The LV08 boards are Bitaxe derivatives and their stock firmware is a fork of
AxeOS, so this speaks the API documented in ESP-Miner-LVXX's
`main/http_server/openapi.yaml`. **Transcribed from that document, not observed
on hardware**, two units are on order. Every field mapping here is due a
re-check the day a real one answers.

**Every writable field the API documents is exposed as a Nomad control.**
There is no separate Lucky Miner app: the LV06-LV08 series ships with no
downloadable app at all. Setup is joining the miner's own Wi-Fi hotspot, which
pops a captive-portal page in the phone's browser: that page IS the app, and
it's served by the same AxeOS firmware answering this same HTTP API. So "every
control the app can set" is exactly the `PATCH /api/system` request schema,
and `WRITABLE_FIELDS` below is a straight walk through it, not a curated
subset.

Two things the document says that are easy to get wrong, and expensive:

* **The units are not what the field names suggest.** `current` is
  **milliamps**, `coreVoltage` and `coreVoltageActual` are **millivolts**,
  `hashRate` is **GH/s**. A client reading amps and TH/s is a thousand times
  out and nothing raises, because 12400 is a perfectly good number. Conversion
  happens once, here, at the edge, never in a card and never twice.

* **The API authorises by network range** and answers **401 "Client not in
  allowed network range"** to a caller outside it. That is a completely
  different fault from unreachable: on a van with the miners on an IoT VLAN and
  HA elsewhere it is the likeliest thing to go wrong, and reporting it as
  "offline" sends someone hunting the wrong problem. It gets its own exception.

**`rotation` is where the vendor's own document contradicts itself.** The
`GET /api/system/info` response schema describes it as "0, 90, 180, 270" (a
degree value); the `PATCH /api/system` request schema's own `enum` for the
same field is literally `[0, 1]`. Fetched twice, both times returned the same
contradiction, so this isn't a transcription slip on our end. We implement the
degree-value reading (0/90/180/270) because that's what every AxeOS-family web
UI actually offers and what the description text says; the `enum: [0, 1]`
half of the spec is treated as the document's own bug until a real unit proves
otherwise.

**`ssid` / `wifiPass` / `hostname` and the whole `stratum*` family are now
writable, on purpose, per an explicit instruction to expose everything the
device's own settings page can set.** They were refused in an earlier revision
of this file over exactly the risk stated below; that risk is real and worth
knowing even though the fields are now allowed:

* The LV08 Pro is **Wi-Fi only, with no Ethernet port**. A miner handed a bad
  SSID or password is off the network until somebody physically drives to it
  and redoes the AP-mode onboarding by hand, in a van, possibly a long way
  from anyone.
* The `stratum*` fields point the miner at a real mining pool. Setting them
  switches the device from solo to pool mining, and the CryptoGenerator
  earnings math in `luckyminer_coordinator.py` (block-odds, "expected time to
  a block") is written for solo only, it does not know or care what these
  fields say, so it will keep showing a solo-shaped answer even after a pool
  is configured.

Validation (length, pattern, range) still runs before every write, same as
every other field, that catches a malformed value, not a value that is simply
inadvisable. Nothing here refuses a syntactically valid SSID or pool URL.

The one legal write to `overheat_mode` is 0, which clears the latch. The
firmware sets it and will not clear it itself, which is correct: the producer
of a latched fault owns clearing it, or the condition that tripped it is
hidden.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

# The firmware refreshes /api/system/info every 5 s and the miners are on
# 2.4GHz Wi-Fi, often two of them sharing one AP. Asking faster than this buys
# nothing and costs airtime.
DEFAULT_TIMEOUT = 6.0

# Fields the API never echoes back in GET /api/system/info (the OpenAPI
# document marks them writeOnly). A UI must not treat "blank" as "cleared" for
# these, it means "we were never told", the same could-not-ask distinction the
# rest of this codebase draws for licence verdicts and sensor readings.
WRITE_ONLY_FIELDS: frozenset[str] = frozenset({
    "wifiPass", "stratumPassword", "fallbackStratumPassword",
})

_HOSTNAME_PATTERN = re.compile(r"^[a-zA-Z0-9-]+$")


@dataclass(frozen=True)
class _FieldSpec:
    """How one writable field is validated. Either an int in [low, high]
    (optionally further restricted to a discrete `allowed` set), or a string
    of length [min_len, max_len] optionally matching `pattern`."""

    kind: str  # "int" or "str"
    low: int = 0
    high: int = 0
    allowed: frozenset[int] | None = None
    min_len: int = 0
    max_len: int = 1_000_000
    pattern: re.Pattern[str] | None = None


def _int_field(low: int, high: int) -> _FieldSpec:
    return _FieldSpec(kind="int", low=low, high=high)


def _enum_field(*allowed: int) -> _FieldSpec:
    return _FieldSpec(
        kind="int", low=min(allowed), high=max(allowed), allowed=frozenset(allowed),
    )


def _str_field(
    min_len: int = 0, max_len: int = 1_000_000,
    pattern: re.Pattern[str] | None = None,
) -> _FieldSpec:
    return _FieldSpec(kind="str", min_len=min_len, max_len=max_len, pattern=pattern)


# Every writable field in the `PATCH /api/system` request schema. An
# ALLOW-LIST: anything absent here cannot be sent, however it is called, a
# field never intended to be writable (e.g. a future firmware addition we
# haven't reviewed) fails loudly here rather than going out on the wire
# unexamined.
#
# No documented maximum exists for `frequency` or `coreVoltage` (the schema
# says only "minimum: 1"). The ceilings below are OUR safety limits, not the
# vendor's: the API accepting a value up to 2^31 doesn't mean sending it is
# safe for the ASIC. Revisit once a real board's `/api/system/asic`
# `frequencyOptions` / `voltageOptions` are known.
WRITABLE_FIELDS: dict[str, _FieldSpec] = {
    # --- tuning -------------------------------------------------------
    "frequency": _int_field(1, 650),            # MHz; self-imposed ceiling
    "coreVoltage": _int_field(1, 1350),          # mV; self-imposed ceiling
    "overclockEnabled": _int_field(0, 1),
    # --- cooling --------------------------------------------------------
    "fanspeed": _int_field(0, 100),              # %
    "autofanspeed": _int_field(0, 1),
    "temptarget": _int_field(0, 100),            # °C
    # --- display ----------------------------------------------------------
    # Degree value (0/90/180/270), NOT the request schema's literal
    # `enum: [0, 1]`, see the module docstring for the spec contradiction.
    "rotation": _enum_field(0, 90, 180, 270),
    "invertscreen": _int_field(0, 1),
    "displayTimeout": _int_field(-1, 71_582),    # minutes; -1 = never
    # --- diagnostics --------------------------------------------------
    "statsFrequency": _int_field(0, 3600),       # seconds; self-imposed ceiling
    # --- network ------------------------------------------------------
    "ssid": _str_field(min_len=1, max_len=32),
    "wifiPass": _str_field(min_len=8, max_len=63),
    "hostname": _str_field(min_len=1, max_len=63, pattern=_HOSTNAME_PATTERN),
    # --- pool (stratum) -------------------------------------------------
    "stratumURL": _str_field(max_len=255),
    "stratumPort": _int_field(1, 65535),
    "stratumUser": _str_field(max_len=255),
    "stratumPassword": _str_field(max_len=255),
    "fallbackStratumURL": _str_field(max_len=255),
    "fallbackStratumPort": _int_field(1, 65535),
    "fallbackStratumUser": _str_field(max_len=255),
    "fallbackStratumPassword": _str_field(max_len=255),
    # --- fault ------------------------------------------------------------
    "overheat_mode": _int_field(0, 0),  # 0 is the only legal value: clear the latch
}


class LuckyMinerError(Exception):
    """Base error for the Lucky Miner client."""


class LuckyMinerConnectionError(LuckyMinerError):
    """Could not reach the miner (offline, wrong host, timeout)."""


class LuckyMinerAuthError(LuckyMinerError):
    """The miner answered 401, we are outside its allowed network range.

    Deliberately NOT a subclass of the connection error. "It refused us" and
    "it isn't there" need different fixes, and collapsing them is how an
    afternoon gets spent power-cycling a miner that was answering all along.
    """


class LuckyMinerWriteRejected(LuckyMinerError):
    """A write was refused before it left this process (not on the wire)."""


def to_amps(milliamps: Any) -> float | None:
    """mA -> A. The API's `current` field is milliamps."""
    try:
        return round(float(milliamps) / 1000.0, 2)
    except (TypeError, ValueError):
        return None


def to_volts(millivolts: Any) -> float | None:
    """mV -> V. The API's `coreVoltage` fields are millivolts."""
    try:
        return round(float(millivolts) / 1000.0, 3)
    except (TypeError, ValueError):
        return None


def to_terahash(gigahash: Any) -> float | None:
    """GH/s -> TH/s. AxeOS reports hash rate in GH/s, so 7 TH/s reads ~7000."""
    try:
        return round(float(gigahash) / 1000.0, 2)
    except (TypeError, ValueError):
        return None


class LuckyMinerClient:
    """Talks to one Lucky Miner over HTTP."""

    def __init__(self, hass: Any, host: str, port: int = 80) -> None:
        self._hass = hass
        self._host = host
        self._port = port

    @property
    def host(self) -> str:
        return self._host

    def rebind(self, host: str, port: int | None = None) -> None:
        """Point at a new address without rebuilding the coordinator.

        A miner that moves (new DHCP lease, moved to another AP) keeps its
        identity, the MAC, so the sane response is to re-aim, not to tear
        down and re-add the device.
        """
        self._host = host
        if port:
            self._port = port

    def _url(self, path: str) -> str:
        return f"http://{self._host}:{self._port}{path}"

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> Any:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        url = self._url(path)
        try:
            async with session.request(
                method, url, json=payload,
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    raise LuckyMinerAuthError(
                        f"{url}: 401, this client is not in the miner's allowed "
                        "network range (check the miner and HA are on the same subnet)"
                    )
                if resp.status >= 400:
                    body = await resp.text()
                    raise LuckyMinerError(f"{url}: HTTP {resp.status}: {body[:200]}")
                if resp.status == 204 or method == "POST":
                    return {}
                # A fork that serves text/plain is still perfectly parseable.
                return await resp.json(content_type=None)
        except (LuckyMinerAuthError, LuckyMinerError):
            raise
        except Exception as err:
            raise LuckyMinerConnectionError(f"{url}: {err}") from err

    async def async_get_info(self) -> dict[str, Any]:
        """`GET /api/system/info`, the whole telemetry document."""
        body = await self._request("GET", "/api/system/info")
        if not isinstance(body, dict):
            raise LuckyMinerError(
                f"/api/system/info returned {type(body).__name__}, not an object"
            )
        return body

    async def async_get_asic(self) -> dict[str, Any]:
        """`GET /api/system/asic`, the tuning ranges the hardware allows.

        Used to bound the frequency and core-voltage controls to what this
        board will actually accept, rather than to a constant we guessed.
        """
        body = await self._request("GET", "/api/system/asic")
        return body if isinstance(body, dict) else {}

    async def async_restart(self) -> None:
        """`POST /api/system/restart`. Settings persist; counters reset."""
        await self._request("POST", "/api/system/restart")

    async def async_write(self, **fields: Any) -> None:
        """`PATCH /api/system`, through the allow-list.

        Validates everything before sending anything, so a rejected write
        cannot leave the miner half-configured with nobody knowing which half.
        """
        if not fields:
            return
        payload: dict[str, int | str] = {}
        for key, raw in fields.items():
            spec = WRITABLE_FIELDS.get(key)
            if spec is None:
                raise LuckyMinerWriteRejected(
                    f"{key!r} is not a field this client knows how to write. "
                    "If the firmware genuinely documents it, add it to "
                    "WRITABLE_FIELDS deliberately, don't pass it through blind."
                )
            if spec.kind == "int":
                try:
                    value = int(raw)
                except (TypeError, ValueError) as err:
                    raise LuckyMinerWriteRejected(
                        f"{key}: expected an integer, got {raw!r}"
                    ) from err
                if not spec.low <= value <= spec.high:
                    raise LuckyMinerWriteRejected(
                        f"{key}: {value} is outside the allowed range "
                        f"{spec.low}-{spec.high}"
                    )
                if spec.allowed is not None and value not in spec.allowed:
                    raise LuckyMinerWriteRejected(
                        f"{key}: {value} must be one of {sorted(spec.allowed)}"
                    )
                payload[key] = value
            else:
                text = str(raw)
                if not spec.min_len <= len(text) <= spec.max_len:
                    raise LuckyMinerWriteRejected(
                        f"{key}: length {len(text)} is outside the allowed range "
                        f"{spec.min_len}-{spec.max_len}"
                    )
                if spec.pattern is not None and not spec.pattern.match(text):
                    raise LuckyMinerWriteRejected(
                        f"{key}: {text!r} does not match the required pattern "
                        f"{spec.pattern.pattern!r}"
                    )
                payload[key] = text

        # Never log a secret. A write-only field's VALUE never appears in the
        # log line, only that it was included in this write.
        loggable = {
            k: ("<redacted>" if k in WRITE_ONLY_FIELDS else v)
            for k, v in payload.items()
        }
        _LOGGER.debug("Lucky Miner %s: PATCH %s", self._host, loggable)
        await self._request("PATCH", "/api/system", payload)

    async def async_clear_overheat(self) -> None:
        """Clear the overheat latch, the only legal `overheat_mode` write."""
        await self.async_write(overheat_mode=0)
