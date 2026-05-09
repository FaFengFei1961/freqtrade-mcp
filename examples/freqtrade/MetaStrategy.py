"""
MetaStrategy — Orchestrator-driven container strategy for freqtrade-mcp.

This strategy is intentionally PASSIVE. It does not generate entry signals on
its own. Trades are opened/closed exclusively via Freqtrade's REST API
(`/forceenter`, `/forceexit`) by an external MCP-driven LLM.

Responsibilities:
1. Compute commonly-useful indicators on every new candle (consumed by the
   LLM via the MCP `get_pair_data` tool when it queries the strategy
   timeframe).
2. Subscribe to multiple higher timeframes via `informative_pairs` so the LLM
   can request 15m/1h/4h/1d candles through the same Freqtrade endpoint
   (the MCP also has its own ccxt direct-fetch fallback).
3. Enforce per-trade stop loss / take profit levels written by the LLM into
   `user_data/stop_levels.json`. This is how the LLM's "psychological" stop
   becomes a system-enforced stop — Freqtrade auto-closes the trade when the
   level is hit, even if the LLM is not awake.

NOTE: This file is part of the trust boundary. The LLM never edits this file
directly. Only humans (you) and curated tooling change it.
"""

# pragma pylint: disable=missing-docstring, invalid-name
# flake8: noqa: F401

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

logger = logging.getLogger(__name__)

# JSON file shared with freqtrade-mcp (host writes, container reads).
# Container mount: ./user_data -> /freqtrade/user_data
STOP_LEVELS_FILE = Path("/freqtrade/user_data/stop_levels.json")

# How long mark/index price is cached before re-fetching (seconds).
_TRIGGER_PRICE_CACHE_TTL_S = 10
# Module-level cache: {(trigger_type, pair): (price, fetched_at)}
_trigger_price_cache: dict[tuple[str, str], tuple[float, datetime]] = {}


def _load_stop_levels() -> dict:
    """Read stop / take profit levels keyed by trade_id (string).

    Schema (each trade may include any subset):
      {
        "1": {
          "sl": 1.018,            # absolute stop-loss price
          "sl_trigger": "mark",   # 'last' | 'mark' | 'index'  (default 'last')
          "tp": 1.036,
          "tp_trigger": "last",
          "tp2": 1.044            # optional second-leg take profit (advisory)
        }
      }
    """
    if not STOP_LEVELS_FILE.exists():
        return {}
    try:
        return json.loads(STOP_LEVELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("stop_levels.json read failed: %s", exc)
        return {}


class MetaStrategy(IStrategy):
    """Container strategy. Signals come from the LLM via REST API + MCP."""

    INTERFACE_VERSION: int = 3

    # Permit short positions (required for futures both-sides trading).
    can_short: bool = True

    # Default timeframe. Can be overridden in config.json.
    timeframe: str = "5m"

    # Disable automatic ROI exits — exits are decided by the LLM.
    minimal_roi: dict[str, float] = {"0": 100.0}

    # NO HARDCODED SAFETY NET. -0.99 means "trigger only at -99%" which in
    # practice never fires. The LLM is fully responsible for setting
    # per-trade stop levels through the MCP set_stop_loss tool.
    stoploss: float = -0.99

    # Required so custom_stoploss is actually consulted on every candle.
    use_custom_stoploss: bool = True

    # Trailing stop is managed by the LLM via set_stop_loss updates.
    trailing_stop: bool = False

    # Only run logic when a new candle closes — saves CPU and avoids
    # double-acting on partial bars.
    process_only_new_candles: bool = True

    # Make sure exit signals from custom_exit fire.
    use_exit_signal: bool = True
    exit_profit_only: bool = False
    ignore_roi_if_entry_signal: bool = False

    # Number of candles the strategy needs before producing meaningful output.
    startup_candle_count: int = 100

    # Higher timeframes the LLM may query via MCP get_pair_data.
    HIGHER_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")

    order_types: dict[str, str | bool | int] = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    order_time_in_force: dict[str, str] = {
        "entry": "GTC",
        "exit": "GTC",
    }

    # ------------------------------------------------------------------
    # Multi-timeframe data subscription
    # ------------------------------------------------------------------
    def informative_pairs(self) -> list[tuple[str, str]]:
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, tf) for p in pairs for tf in self.HIGHER_TIMEFRAMES]

    # ------------------------------------------------------------------
    # Indicator population
    # ------------------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Trend
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=26)
        dataframe["sma_50"] = ta.SMA(dataframe, timeperiod=50)
        dataframe["sma_200"] = ta.SMA(dataframe, timeperiod=200)

        # Momentum
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macd_signal"] = macd["macdsignal"]
        dataframe["macd_hist"] = macd["macdhist"]

        # Volatility
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_lower"] = bb["lowerband"]
        dataframe["bb_middle"] = bb["middleband"]
        dataframe["bb_upper"] = bb["upperband"]

        # Volume
        dataframe["volume_ma_20"] = dataframe["volume"].rolling(window=20).mean()
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_ma_20"]

        return dataframe

    # ------------------------------------------------------------------
    # Entry / exit signals — INTENTIONALLY DISABLED (LLM drives everything)
    # ------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    # ------------------------------------------------------------------
    # Leverage hook — accept whatever the LLM requests, no clamping
    # ------------------------------------------------------------------
    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """Honor the LLM's leverage choice up to whatever the exchange permits."""
        requested = proposed_leverage if proposed_leverage > 0 else 1.0
        # Only the exchange's own per-market max is respected (it's a hard error
        # to exceed). No project-level cap.
        return float(min(requested, max_leverage))

    # ------------------------------------------------------------------
    # Trigger-price lookup helper (mark / index, with short cache)
    # ------------------------------------------------------------------
    def _fetch_trigger_price(self, pair: str, trigger_type: str) -> Optional[float]:
        """Return mark or index price for a pair (cached for 10s).

        Used by custom_stoploss and custom_exit when the LLM has chosen
        'mark' or 'index' as the trigger type. We pull a fresh ticker via
        the strategy's ccxt instance (same one Freqtrade is already using).
        """
        if trigger_type not in ("mark", "index"):
            return None

        now = datetime.now(timezone.utc)
        key = (trigger_type, pair)
        cached = _trigger_price_cache.get(key)
        if cached and (now - cached[1]).total_seconds() < _TRIGGER_PRICE_CACHE_TTL_S:
            return cached[0]

        if not (self.dp and getattr(self.dp, "_exchange", None)):
            return None

        try:
            ticker = self.dp._exchange.fetch_ticker(pair)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trigger price fetch failed pair=%s type=%s: %s", pair, trigger_type, exc)
            return None

        price = None
        if trigger_type == "mark":
            price = ticker.get("markPrice")
            if price is None:
                raw = (ticker.get("info") or {}).get("mark_price")
                price = float(raw) if raw else None
        elif trigger_type == "index":
            price = ticker.get("indexPrice")
            if price is None:
                raw = (ticker.get("info") or {}).get("index_price")
                price = float(raw) if raw else None

        if price is not None:
            _trigger_price_cache[key] = (float(price), now)
            logger.info(
                "fetched %s price for %s: %.6f", trigger_type, pair, float(price),
            )
            return float(price)
        return None

    # ------------------------------------------------------------------
    # Custom stoploss — only fires when sl_trigger == 'last'
    # ------------------------------------------------------------------
    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> Optional[float]:
        """Wire LLM-set stop-loss price into Freqtrade's native stop-loss machinery.

        ONLY applies when the LLM chose ``sl_trigger == 'last'`` (the default).
        For ``mark`` or ``index`` triggers we let custom_exit handle it
        (because Freqtrade's native stop-loss tracker is hard-wired to last
        price and cannot evaluate against mark/index).
        """
        levels = _load_stop_levels()
        cfg = levels.get(str(trade.id), {})
        sl_price = cfg.get("sl")
        sl_trigger = (cfg.get("sl_trigger") or "last").lower()
        if sl_price is None or sl_trigger != "last":
            return None
        try:
            sl_price = float(sl_price)
        except (TypeError, ValueError):
            return None

        leverage = float(trade.leverage or 1.0)
        if trade.is_short:
            ratio = (trade.open_rate - sl_price) / trade.open_rate * leverage
        else:
            ratio = (sl_price - trade.open_rate) / trade.open_rate * leverage

        return ratio

    # ------------------------------------------------------------------
    # Custom exit — handles BOTH:
    #   * stop-loss with mark/index trigger (last-trigger goes via custom_stoploss)
    #   * take-profit with any trigger
    # ------------------------------------------------------------------
    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        """Evaluate the LLM-set SL/TP using the configured trigger price."""
        levels = _load_stop_levels()
        cfg = levels.get(str(trade.id), {})

        # ---- Stop-loss path (only fires here for mark/index triggers) ----
        sl_price = cfg.get("sl")
        sl_trigger = (cfg.get("sl_trigger") or "last").lower()
        if sl_price is not None and sl_trigger in ("mark", "index"):
            try:
                sl_price_f = float(sl_price)
            except (TypeError, ValueError):
                sl_price_f = None
            if sl_price_f is not None:
                trigger_p = self._fetch_trigger_price(pair, sl_trigger)
                if trigger_p is not None:
                    if trade.is_short and trigger_p >= sl_price_f:
                        logger.info(
                            "custom_exit SL hit: trade=%s pair=%s trigger=%s "
                            "trigger_price=%.6f sl=%.6f",
                            trade.id, pair, sl_trigger, trigger_p, sl_price_f,
                        )
                        return f"sl_{sl_trigger}"
                    if (not trade.is_short) and trigger_p <= sl_price_f:
                        logger.info(
                            "custom_exit SL hit: trade=%s pair=%s trigger=%s "
                            "trigger_price=%.6f sl=%.6f",
                            trade.id, pair, sl_trigger, trigger_p, sl_price_f,
                        )
                        return f"sl_{sl_trigger}"

        # ---- Take-profit path (any trigger) ----
        tp_price = cfg.get("tp")
        if tp_price is None:
            return None

        try:
            tp_price_f = float(tp_price)
        except (TypeError, ValueError):
            return None

        tp_trigger = (cfg.get("tp_trigger") or "last").lower()
        if tp_trigger == "last":
            trigger_p = current_rate
        else:
            trigger_p = self._fetch_trigger_price(pair, tp_trigger)
            if trigger_p is None:
                return None

        if trade.is_short and trigger_p <= tp_price_f:
            logger.info(
                "custom_exit TP hit: trade=%s pair=%s trigger=%s "
                "trigger_price=%.6f tp=%.6f",
                trade.id, pair, tp_trigger, trigger_p, tp_price_f,
            )
            return f"tp_{tp_trigger}"
        if not trade.is_short and trigger_p >= tp_price_f:
            logger.info(
                "custom_exit TP hit: trade=%s pair=%s trigger=%s "
                "trigger_price=%.6f tp=%.6f",
                trade.id, pair, tp_trigger, trigger_p, tp_price_f,
            )
            return f"tp_{tp_trigger}"
        return None
