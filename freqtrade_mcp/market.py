"""ccxt-based market data — fetches OHLCV at any timeframe and computes indicators.

This module exists because Freqtrade's REST API only exposes the strategy's
main timeframe via ``/pair_candles``. The MCP server needs to give the LLM
access to ANY timeframe (1m through 1M) on demand, including pairs not in
the bot's whitelist. So we go straight to the exchange via ccxt.

Indicators computed in pure pandas to avoid the TA-Lib install pain on
Windows. The set matches what MetaStrategy.populate_indicators provides
for the 5m timeframe in Freqtrade, so the LLM sees a consistent column
schema regardless of source.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import ccxt
import numpy as np
import pandas as pd

from .config import Settings, get_settings

logger = logging.getLogger("freqtrade_mcp.market")

# Lock + cache so we don't recreate the ccxt client on every tool call.
_exchange_lock = threading.Lock()
_exchange_cache: dict[str, ccxt.Exchange] = {}


def _get_exchange(settings: Settings | None = None) -> ccxt.Exchange:
    """Return a cached ccxt exchange instance for the configured market."""
    s = settings or get_settings()
    key = f"{s.exchange_id}:{s.exchange_market_type}"
    with _exchange_lock:
        if key not in _exchange_cache:
            klass = getattr(ccxt, s.exchange_id, None)
            if klass is None:
                raise ValueError(f"ccxt has no exchange named '{s.exchange_id}'")
            _exchange_cache[key] = klass(
                {
                    "enableRateLimit": True,
                    "timeout": 30000,  # 30s — first load_markets pulls hundreds of contracts
                    "options": {"defaultType": s.exchange_market_type},
                }
            )
            # Warm up the markets cache eagerly so the FIRST tool call doesn't
            # pay the multi-second load_markets() cost.
            try:
                _exchange_cache[key].load_markets()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ccxt warmup load_markets failed (will retry on first call): %s", exc)
            logger.info(
                "ccxt exchange initialized: %s (%s)",
                s.exchange_id,
                s.exchange_market_type,
            )
        return _exchange_cache[key]


# ---------------------------------------------------------------------
# OHLCV fetch
# ---------------------------------------------------------------------
def fetch_ohlcv(pair: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
    """Fetch raw OHLCV candles from the exchange. Returns a DataFrame with
    columns: date (UTC), open, high, low, close, volume.
    """
    ex = _get_exchange()
    raw = ex.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
    if not raw:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(raw, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------
# Indicator calculations (pure pandas/numpy, TA-Lib free)
# ---------------------------------------------------------------------
def _add_ema(df: pd.DataFrame) -> None:
    df["ema_fast"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=26, adjust=False).mean()
    df["sma_50"] = df["close"].rolling(window=50, min_periods=1).mean()
    df["sma_200"] = df["close"].rolling(window=200, min_periods=1).mean()


def _add_rsi(df: pd.DataFrame, period: int = 14) -> None:
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))


def _add_macd(df: pd.DataFrame) -> None:
    df["macd"] = df["ema_fast"] - df["ema_slow"]
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]


def _add_atr(df: pd.DataFrame, period: int = 14) -> None:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / period, adjust=False).mean()


def _add_bbands(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> None:
    sma = df["close"].rolling(window=period, min_periods=1).mean()
    std = df["close"].rolling(window=period, min_periods=1).std()
    df["bb_middle"] = sma
    df["bb_upper"] = sma + std_mult * std
    df["bb_lower"] = sma - std_mult * std


def _add_volume(df: pd.DataFrame, period: int = 20) -> None:
    df["volume_ma_20"] = df["volume"].rolling(window=period, min_periods=1).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma_20"].replace(0, np.nan)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add the full indicator set in-place-ish. Returns the same DataFrame."""
    if df.empty:
        return df
    _add_ema(df)
    _add_rsi(df)
    _add_macd(df)
    _add_atr(df)
    _add_bbands(df)
    _add_volume(df)
    return df


# ---------------------------------------------------------------------
# Public entrypoint used by mcp_server.get_pair_data
# ---------------------------------------------------------------------
def fetch_with_indicators(
    pair: str,
    timeframe: str,
    limit: int = 200,
) -> dict[str, Any]:
    """Fetch OHLCV + computed indicators, return MCP-friendly dict.

    Always pulls (limit + 200) candles internally so the longer-window
    indicators (sma_200, etc.) have warm-up data, then trims back to limit.
    """
    warmup_extra = 200
    raw_limit = min(limit + warmup_extra, 1500)
    df = fetch_ohlcv(pair, timeframe=timeframe, limit=raw_limit)
    if df.empty:
        return {
            "pair": pair,
            "timeframe": timeframe,
            "candle_count": 0,
            "columns": [],
            "data": [],
            "source": "ccxt",
            "note": "exchange returned no candles",
        }
    df = add_indicators(df)
    df = df.tail(limit).reset_index(drop=True)
    df["date"] = df["date"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Replace NaN with None so JSON serializes cleanly.
    df = df.where(pd.notnull(df), None)
    return {
        "pair": pair,
        "timeframe": timeframe,
        "candle_count": int(len(df)),
        "last_close": float(df["close"].iloc[-1]) if not df.empty else None,
        "last_date": str(df["date"].iloc[-1]) if not df.empty else None,
        "columns": df.columns.tolist(),
        "data": df.values.tolist(),
        "source": "ccxt",
    }


# =====================================================================
# Professional-grade market data — mark/index/funding/OI/liquidations
# =====================================================================

def _ohlcv_to_payload(raw: list, *, source: str, pair: str, timeframe: str) -> dict[str, Any]:
    """Convert raw OHLCV (timestamp, o, h, l, c, v) list into a JSON-friendly dict."""
    if not raw:
        return {
            "pair": pair, "timeframe": timeframe, "candle_count": 0,
            "columns": [], "data": [], "source": source,
        }
    df = pd.DataFrame(raw, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df = df[["date", "open", "high", "low", "close", "volume"]]
    df = df.where(pd.notnull(df), None)
    return {
        "pair": pair, "timeframe": timeframe,
        "candle_count": int(len(df)),
        "last_close": float(df["close"].iloc[-1]),
        "last_date": str(df["date"].iloc[-1]),
        "columns": df.columns.tolist(),
        "data": df.values.tolist(),
        "source": source,
    }


def fetch_mark_ohlcv(pair: str, timeframe: str, limit: int = 200) -> dict[str, Any]:
    """Mark-price OHLCV.

    Mark price is the exchange-calculated reference price used for liquidation
    and unrealized PnL — it filters out spikes from low-liquidity last-trade
    prints. For perp futures, this is what the LLM should care about for
    risk decisions.
    """
    ex = _get_exchange()
    if not ex.has.get("fetchMarkOHLCV"):
        return {"error": f"{ex.id} does not expose fetchMarkOHLCV via ccxt"}
    raw = ex.fetch_mark_ohlcv(pair, timeframe=timeframe, limit=limit)
    return _ohlcv_to_payload(raw, source="ccxt:mark", pair=pair, timeframe=timeframe)


def fetch_index_ohlcv(pair: str, timeframe: str, limit: int = 200) -> dict[str, Any]:
    """Index-price OHLCV.

    Index price is an average across multiple exchanges — useful for spotting
    cross-exchange divergence and confirming whether a move is local to
    this exchange or market-wide.
    """
    ex = _get_exchange()
    if not ex.has.get("fetchIndexOHLCV"):
        return {"error": f"{ex.id} does not expose fetchIndexOHLCV via ccxt"}
    raw = ex.fetch_index_ohlcv(pair, timeframe=timeframe, limit=limit)
    return _ohlcv_to_payload(raw, source="ccxt:index", pair=pair, timeframe=timeframe)


def fetch_funding_rate(pair: str) -> dict[str, Any]:
    """Current funding rate snapshot.

    For perp futures: positive rate → longs pay shorts every 8h.
    Negative rate → shorts pay longs. High |rate| signals crowded
    one-sided positioning (often a contrarian indicator).
    """
    ex = _get_exchange()
    if not ex.has.get("fetchFundingRate"):
        return {"error": f"{ex.id} does not expose fetchFundingRate via ccxt"}
    fr = ex.fetch_funding_rate(pair)
    return {
        "pair": pair,
        "funding_rate": fr.get("fundingRate"),  # current period rate (decimal, e.g. 0.0001 = 0.01%)
        "funding_timestamp": fr.get("fundingTimestamp"),
        "next_funding_rate": fr.get("nextFundingRate"),
        "next_funding_datetime": fr.get("nextFundingDatetime"),
        "mark_price": fr.get("markPrice"),
        "index_price": fr.get("indexPrice"),
        "interest_rate": fr.get("interestRate"),
        "raw": fr.get("info", {}),
    }


def fetch_funding_history(pair: str, limit: int = 30) -> dict[str, Any]:
    """Historical funding rates (past N settlement periods, usually 8h apart)."""
    ex = _get_exchange()
    if not ex.has.get("fetchFundingRateHistory"):
        return {"error": f"{ex.id} does not expose fetchFundingRateHistory via ccxt"}
    history = ex.fetch_funding_rate_history(pair, limit=limit)
    return {
        "pair": pair,
        "count": len(history),
        "history": [
            {
                "datetime": h.get("datetime"),
                "rate": h.get("fundingRate"),
                "timestamp": h.get("timestamp"),
            }
            for h in history
        ],
    }


def fetch_liquidations(pair: str, limit: int = 50) -> dict[str, Any]:
    """Recent liquidation orders on this pair.

    Heavy clusters of long-side liquidations during a dump (or shorts during
    a pump) signal forced unwinds — often where bottoms/tops form.
    """
    ex = _get_exchange()
    if not ex.has.get("fetchLiquidations"):
        return {"error": f"{ex.id} does not expose fetchLiquidations via ccxt"}
    liqs = ex.fetch_liquidations(pair, limit=limit)
    return {
        "pair": pair,
        "count": len(liqs),
        "liquidations": [
            {
                "datetime": l.get("datetime"),
                "side": l.get("side"),
                "price": l.get("price"),
                "amount": l.get("amount"),
                "quote_value": l.get("price", 0) * l.get("amount", 0) if l.get("price") and l.get("amount") else None,
            }
            for l in liqs
        ],
    }


def fetch_orderbook(pair: str, depth: int = 20) -> dict[str, Any]:
    """Order book snapshot — top N bid/ask levels.

    Use to gauge:
      - immediate liquidity (sum of nearby asks / bids)
      - bid/ask imbalance (does the wall lean buy or sell)
      - spread tightness
    """
    ex = _get_exchange()
    ob = ex.fetch_order_book(pair, limit=depth)
    bids = ob.get("bids", [])[:depth]
    asks = ob.get("asks", [])[:depth]
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = (best_ask - best_bid) if (best_bid and best_ask) else None
    return {
        "pair": pair,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": (spread / best_bid * 100) if (best_bid and spread) else None,
        "bids": bids,  # [[price, amount], ...]
        "asks": asks,
        "bid_volume_total": sum(b[1] for b in bids),
        "ask_volume_total": sum(a[1] for a in asks),
        "datetime": ob.get("datetime"),
    }


def fetch_ticker_full(pair: str) -> dict[str, Any]:
    """Comprehensive ticker — last/mark/index plus 24h stats."""
    ex = _get_exchange()
    t = ex.fetch_ticker(pair)
    info = t.get("info") or {}
    return {
        "pair": pair,
        "last": t.get("last"),
        "mark_price": t.get("markPrice") or info.get("mark_price"),
        "index_price": t.get("indexPrice") or info.get("index_price"),
        "bid": t.get("bid"),
        "ask": t.get("ask"),
        "spread": (t.get("ask") - t.get("bid")) if (t.get("ask") and t.get("bid")) else None,
        "high_24h": t.get("high"),
        "low_24h": t.get("low"),
        "open_24h": t.get("open"),
        "change_24h_pct": t.get("percentage"),
        "base_volume_24h": t.get("baseVolume"),
        "quote_volume_24h": t.get("quoteVolume"),
        "datetime": t.get("datetime"),
    }


def fetch_open_interest(pair: str) -> dict[str, Any]:
    """Total open interest — number of contracts outstanding.

    Rising OI + rising price = trend with conviction.
    Rising OI + falling price = bearish conviction.
    Falling OI = positions closing, often trend exhaustion.
    """
    ex = _get_exchange()
    # ccxt has() may say False but the method often still works on Gate.
    try:
        oi = ex.fetch_open_interest(pair)
        return {
            "pair": pair,
            "open_interest_amount": oi.get("openInterestAmount"),  # base units
            "open_interest_value": oi.get("openInterestValue"),    # quote units (USDT)
            "datetime": oi.get("datetime"),
        }
    except Exception as exc:  # noqa: BLE001
        # Fallback: try ticker.info for Gate-specific fields.
        try:
            t = ex.fetch_ticker(pair)
            info = t.get("info") or {}
            return {
                "pair": pair,
                "open_interest_amount": float(info.get("total_size") or 0) or None,
                "raw_ticker_info": {k: info.get(k) for k in ("total_size", "position_size") if k in info},
                "note": "fetched from ticker.info fallback",
            }
        except Exception as exc2:  # noqa: BLE001
            return {"error": f"open interest unavailable: {exc} / {exc2}"}


def fetch_position_detail(pair: str) -> dict[str, Any]:
    """Exchange-side position view — liquidation price, margin ratio, etc.

    Note: Requires authenticated API access. In dry-run without API keys this
    will return an empty/auth-error response; the LLM should rely on
    get_status (Freqtrade DB view) instead.
    """
    ex = _get_exchange()
    if not ex.has.get("fetchPosition"):
        return {"error": f"{ex.id} does not expose fetchPosition via ccxt"}
    try:
        p = ex.fetch_position(pair)
        if not p:
            return {"pair": pair, "note": "no open position on exchange"}
        return {
            "pair": pair,
            "side": p.get("side"),
            "contracts": p.get("contracts"),
            "contract_size": p.get("contractSize"),
            "entry_price": p.get("entryPrice"),
            "mark_price": p.get("markPrice"),
            "liquidation_price": p.get("liquidationPrice"),
            "leverage": p.get("leverage"),
            "margin_mode": p.get("marginMode"),
            "initial_margin": p.get("initialMargin"),
            "maintenance_margin": p.get("maintenanceMargin"),
            "margin_ratio": p.get("marginRatio"),
            "unrealized_pnl": p.get("unrealizedPnl"),
            "percentage": p.get("percentage"),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "error": str(exc),
            "hint": "fetch_position requires API keys; in dry-run use get_status instead",
        }


def set_leverage(pair: str, leverage: float) -> dict[str, Any]:
    """Set leverage on a pair without opening a position.

    Useful when you want to pre-configure leverage before placing entries,
    or to adjust an existing position's risk profile mid-trade.

    Note: Requires authenticated API access. Will fail in dry-run without keys.
    """
    ex = _get_exchange()
    if not ex.has.get("setLeverage"):
        return {"error": f"{ex.id} does not expose setLeverage via ccxt"}
    try:
        result = ex.set_leverage(int(leverage), pair)
        return {"pair": pair, "leverage": leverage, "ok": True, "raw": result}
    except Exception as exc:  # noqa: BLE001
        return {
            "error": str(exc),
            "hint": "set_leverage requires API keys with trading permission; "
                    "in dry-run pass leverage to force_enter instead",
        }
