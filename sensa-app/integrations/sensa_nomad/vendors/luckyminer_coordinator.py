"""Coordinator for one Lucky Miner LV08 Pro.

Polls the AxeOS HTTP API, converts every reading out of the API's units into
ours ONCE (see `luckyminer_api`), and derives the numbers no miner reports
about itself: what it earns, and what it heats.

Two rules from the project CLAUDE.md do the shaping here.

**"Told no" is not "could not ask."** A failed poll must never publish a
confident zero. A miner that answers "0 TH/s" has stopped; a miner we could not
reach has not told us anything, and rendering that as zero would put a flat line
on the customer's chart and a false story on the tab. So a failed poll carries
the previous sample forward, marks it stale, and logs on the EDGE, once when
the run of failures starts, not once every fifteen seconds for an hour.

**The producer of a latched fault owns clearing it.** `overheat_mode` latches in
the firmware and stays latched after the miner cools, which is correct: it
records that something went wrong. Nothing here clears it implicitly.

Earnings are an EXPECTATION, and the distinction is load-bearing because this
goes in front of customers. Solo mining pays nothing at all until it finds a
block, and then pays the whole subsidy at once. Two LV08 Pros are ~14 TH/s
against a network measured in ZH/s, so the honest figure is "about one block in
N centuries" sitting beside the pounds-per-day, and the derived sensors carry
the odds so no card can show one without the other.

The market data (price, difficulty, block height) comes from the **Sensa App**,
never from here: the add-on owns internet access and the integration stays
LAN-only. When the App cannot refresh, the last good sample is used and flagged
stale, a zero price would read as "your miner earns nothing", which is a
confident wrong answer to a question we could not ask.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .luckyminer_api import (
    LuckyMinerAuthError,
    LuckyMinerClient,
    LuckyMinerConnectionError,
    to_amps,
    to_terahash,
    to_volts,
)

_LOGGER = logging.getLogger(__name__)

# Two miners on one 2.4GHz AP, and the firmware only refreshes its own document
# every 5 s. Polling harder buys nothing and costs airtime.
POLL_INTERVAL = timedelta(seconds=15)

# Market data changes slowly and lives behind the App's own 15-minute cache;
# asking more often would just re-read the same cached document.
MARKET_POLL_INTERVAL = timedelta(minutes=5)

# Bitcoin's difficulty-to-hashrate constant: difficulty x 2^32 / 600 s.
_HASHES_PER_DIFFICULTY = 2 ** 32
_BLOCK_TARGET_SECONDS = 600
_BLOCKS_PER_DAY = 86_400 / _BLOCK_TARGET_SECONDS      # 144
_SATS_PER_BTC = 100_000_000

# A miner is a resistive load: essentially all the power it draws leaves as
# heat. 1 W = 3.412 BTU/h.
_BTU_PER_WATT = 3.412


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class LuckyMinerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls one miner and derives its earnings and heat."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: LuckyMinerClient,
        device_id: str,
        *,
        display_name: str = "Bitcoin Miner",
        sensa_client: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"luckyminer_{device_id}",
            update_interval=POLL_INTERVAL,
        )
        self._client = client
        self._sensa_client = sensa_client
        self.device_id = device_id
        self.display_name = display_name

        # Last good telemetry, carried forward across a failed poll.
        self._last_good: dict[str, Any] | None = None
        self._fail_streak = 0
        # Session accrual: expected BTC integrated over time. Reset when the
        # miner's own uptime goes backwards, which is how a reboot announces
        # itself, it is the miner's session, not ours, so it is the miner's
        # clock that defines it.
        self._session_btc = 0.0
        self._last_uptime = 0
        self._last_accrual_at = 0.0
        # Market snapshot from the App, and when it was last refreshed.
        self._market: dict[str, Any] = {}
        self._market_fetched_at = 0.0
        # Tuning bounds read once from /api/system/asic so the number entities
        # are bounded by what the board accepts, not by a constant we invented.
        self.asic_limits: dict[str, Any] = {}

    @property
    def client(self) -> LuckyMinerClient:
        return self._client

    # --- market data ------------------------------------------------------

    async def _async_refresh_market(self) -> None:
        """Pull price/difficulty/height from the App, keeping the last good one.

        Never raises: no market data means the earnings sensors go
        UNAVAILABLE, which is honest, while the telemetry sensors carry on.
        Losing the Bitcoin price must not take the hash rate off the screen.
        """
        if self._sensa_client is None:
            return
        now = time.time()
        if now - self._market_fetched_at < MARKET_POLL_INTERVAL.total_seconds():
            return
        try:
            # Priced in HA's own currency (Settings, System), so the tab's
            # pounds are pounds; the App falls back to USD for one it lacks.
            body = await self._sensa_client.async_get_crypto_market(
                getattr(self.hass.config, "currency", None),
            )
        except Exception as err:  # noqa: BLE001, the App being down is expected
            _LOGGER.debug(
                "Lucky Miner %s: market refresh failed (%s), keeping the last "
                "known price and marking it stale", self.device_id, err,
            )
            return
        if isinstance(body, dict) and body.get("price"):
            self._market = body
            self._market_fetched_at = now

    # --- derived numbers --------------------------------------------------

    def _derive(self, info: dict[str, Any]) -> dict[str, Any]:
        """Everything the miner does not report about itself."""
        out: dict[str, Any] = {}

        watts = _f(info.get("power"))
        if watts is not None:
            # A miner is ~100% resistive: what goes in comes out as heat.
            out["heat_w"] = round(watts, 1)
            out["heat_btu_h"] = round(watts * _BTU_PER_WATT, 0)

        ths = to_terahash(info.get("hashRate"))
        if ths and watts:
            # Joules per terahash, the number that says whether a tuning
            # change was actually worth making.
            out["efficiency_j_th"] = round(watts / ths, 1)

        market = self._market or {}
        price = _f(market.get("price"))
        difficulty = _f(market.get("difficulty"))
        subsidy = _f(market.get("block_subsidy_btc"))
        if not (ths and price and difficulty and subsidy):
            # No market sample, or the miner is not hashing. Everything below
            # is UNAVAILABLE rather than zero, see the module docstring.
            return out

        network_hs = difficulty * _HASHES_PER_DIFFICULTY / _BLOCK_TARGET_SECONDS
        our_hs = ths * 1e12
        share = our_hs / network_hs if network_hs else 0.0

        daily_btc = share * subsidy * _BLOCKS_PER_DAY
        out["network_hashrate_eh"] = round(network_hs / 1e18, 1)
        out["expected_daily_btc"] = daily_btc
        out["expected_daily_sats"] = round(daily_btc * _SATS_PER_BTC)
        out["expected_daily_value"] = round(daily_btc * price, 2)
        out["expected_monthly_sats"] = round(daily_btc * 30 * _SATS_PER_BTC)
        out["expected_monthly_value"] = round(daily_btc * 30 * price, 2)
        out["btc_price"] = round(price, 2)
        out["currency"] = market.get("currency", "USD")
        out["price_fetched_at"] = market.get("fetched_at")
        out["price_stale"] = bool(market.get("stale"))

        # The honesty line. A daily probability of finding a block, expressed
        # as the years you would expect to wait, which for a solo miner is the
        # only context that makes a pounds-per-day figure truthful.
        daily_p = share * _BLOCKS_PER_DAY
        if daily_p > 0:
            out["block_odds_years"] = round(1.0 / (daily_p * 365.0), 1)

        # Session accrual, integrated on the miner's own clock.
        uptime = int(_f(info.get("uptimeSeconds")) or 0)
        now = time.time()
        if uptime < self._last_uptime:
            _LOGGER.debug(
                "Lucky Miner %s: uptime went backwards (%s -> %s), the miner "
                "restarted, so the session total resets with it",
                self.device_id, self._last_uptime, uptime,
            )
            self._session_btc = 0.0
            self._last_accrual_at = now
        elif self._last_accrual_at:
            elapsed = max(0.0, now - self._last_accrual_at)
            self._session_btc += daily_btc * (elapsed / 86_400.0)
            self._last_accrual_at = now
        else:
            self._last_accrual_at = now
        self._last_uptime = uptime

        out["session_btc"] = self._session_btc
        out["session_sats"] = round(self._session_btc * _SATS_PER_BTC)
        out["session_value"] = round(self._session_btc * price, 2)
        return out

    @staticmethod
    def _normalise(info: dict[str, Any]) -> dict[str, Any]:
        """Convert out of the API's units, once, at the edge.

        `current` is mA, `coreVoltage` is mV and `hashRate` is GH/s. Doing this
        here means no card, template or automation downstream can disagree
        about the scale, which is exactly how one predicate written twice
        becomes a shipped bug.
        """
        out = dict(info)
        out["hashrate_th"] = to_terahash(info.get("hashRate"))
        out["expected_hashrate_th"] = to_terahash(info.get("expectedHashrate"))
        out["current_a"] = to_amps(info.get("current"))
        out["core_voltage_v"] = to_volts(info.get("coreVoltage"))
        out["core_voltage_actual_v"] = to_volts(info.get("coreVoltageActual"))
        return out

    # --- the poll ---------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        await self._async_refresh_market()
        try:
            info = await self._client.async_get_info()
        except LuckyMinerAuthError as err:
            # Distinct from unreachable ON PURPOSE, see luckyminer_api.
            if self._fail_streak == 0:
                _LOGGER.warning(
                    "Lucky Miner %s refused us: %s. The miner is reachable but "
                    "will not answer this client; check it and Home Assistant "
                    "are on the same subnet.", self.device_id, err,
                )
            self._fail_streak += 1
            return self._stale()
        except (LuckyMinerConnectionError, Exception) as err:  # noqa: BLE001
            # Log on the EDGE. A miner unplugged for a week must not write 5760
            # identical warnings a day into the log.
            if self._fail_streak == 0:
                _LOGGER.warning(
                    "Lucky Miner %s unreachable (%s), holding the last reading; "
                    "further failures will be quiet until it returns",
                    self.device_id, err,
                )
            self._fail_streak += 1
            return self._stale()

        if self._fail_streak:
            _LOGGER.info(
                "Lucky Miner %s is answering again after %d failed poll(s)",
                self.device_id, self._fail_streak,
            )
            self._fail_streak = 0

        data = self._normalise(info)
        data.update(self._derive(data))
        data["_available"] = True
        data["_stale"] = False
        self._last_good = data
        return data

    def _stale(self) -> dict[str, Any]:
        """Carry the last good sample forward, flagged.

        Not zeros. "We could not ask" and "it reported nothing" are different
        answers and only one of them is true here.
        """
        if self._last_good is None:
            return {"_available": False, "_stale": True}
        return {**self._last_good, "_available": False, "_stale": True}

    # --- writes -----------------------------------------------------------

    async def async_write(self, **fields: Any) -> None:
        """Apply a tuning change and refresh so the UI reflects it at once."""
        await self._client.async_write(**fields)
        await self.async_request_refresh()

    async def async_restart(self) -> None:
        await self._client.async_restart()
        await self.async_request_refresh()

    async def async_load_asic_limits(self) -> None:
        """Read the board's tuning ranges once, best effort."""
        try:
            self.asic_limits = await self._client.async_get_asic()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Lucky Miner %s: /api/system/asic unavailable (%s), the tuning "
                "controls fall back to the documented defaults",
                self.device_id, err,
            )
            self.asic_limits = {}
