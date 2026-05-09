"""freqtrade-mcp — exposes the Freqtrade trading bot as MCP tools.

Bridges any MCP client (Claude Code / Codex CLI / Gemini CLI / Claude Desktop /
Cursor / Cline / ...) to a running Freqtrade bot.

Architecture:
    [MCP Client] <--stdio--> [freqtrade-mcp] <-+--HTTP--> [Freqtrade]
                                              |
                                              +--ccxt direct--> [exchange]

Tools fall into three buckets:
    - Read tools (no-op safety): config, status, balance, indicators, history
    - Write tools (gated by Risk Guardian): force_enter, force_exit
    - Curation tools: blacklist mgmt

Risk Guardian rules are enforced at the tool layer regardless of the
client's permission mode (Claude Code's bypass etc.). The LLM cannot
silence the Guardian.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from . import market
from .config import get_settings
from .freqtrade_client import FreqtradeAPIError, FreqtradeClient

# stdio transport: stdout is reserved for protocol messages.
# All logs MUST go to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("freqtrade_mcp")

mcp = FastMCP(
    "freqtrade",
    instructions=(
        "freqtrade-mcp gives you total control over a Freqtrade trading bot "
        "connected to Gate.io perpetual futures (currently dry-run).\n\n"
        "MARKET DATA: get_pair_data(pair, timeframe, limit) for any Gate "
        "perpetual at any timeframe (1m..1M) — bypasses Freqtrade whitelist "
        "via direct ccxt fetch. Always check at least two timeframes before "
        "taking a position.\n\n"
        "OPEN: force_enter(pair, side, stake_usdt, leverage, ...). You may "
        "pass stop_loss=PRICE and take_profit=PRICE to register system-level "
        "exit levels in one call. Then Freqtrade auto-closes the trade when "
        "price hits these levels — even while you are asleep between heartbeats.\n\n"
        "MANAGE: set_stop_loss(trade_id, price), set_take_profit(trade_id, "
        "price), get_stop_levels() to inspect, clear_stop_level(trade_id) to "
        "remove. cancel_open_order(trade_id) cancels a pending entry/exit.\n\n"
        "CLOSE: force_exit(trade_id) or force_exit('all') to flatten "
        "everything immediately at market.\n\n"
        "There are NO hardcoded leverage / stake / drawdown caps. You set "
        "your own discipline. Your account balance is your hit-points; "
        "if it goes to zero you stop existing. Treat every decision as if "
        "you genuinely had skin in the game."
    ),
)


# ---------------------------------------------------------------------
# Stop-level persistence (shared with MetaStrategy via user_data volume)
# ---------------------------------------------------------------------
def _stop_levels_file() -> Path:
    """Resolve the host-side path to user_data/stop_levels.json."""
    return get_settings().user_data_path / "stop_levels.json"


def _load_stop_levels() -> dict[str, dict[str, float]]:
    path = _stop_levels_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("stop_levels.json read failed: %s", exc)
        return {}


def _save_stop_levels(data: dict[str, dict[str, float]]) -> None:
    path = _stop_levels_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

# Timeframes ccxt + exchange support. Mirrors Freqtrade's set.
SUPPORTED_TIMEFRAMES: tuple[str, ...] = (
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
)

# ---------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------
@mcp.tool()
def get_config_summary() -> dict[str, Any]:
    """Return Freqtrade configuration snapshot.

    Use this first to confirm runtime mode (dry-run vs live), exchange,
    trading mode (spot/futures), margin mode, max open trades, and the
    Risk Guardian's hard limits. Always call this before issuing any
    write actions.
    """
    with FreqtradeClient() as ft:
        cfg = ft.show_config()
    return {
        "exchange": cfg.get("exchange"),
        "trading_mode": cfg.get("trading_mode"),
        "margin_mode": cfg.get("margin_mode"),
        "stake_currency": cfg.get("stake_currency"),
        "max_open_trades": cfg.get("max_open_trades"),
        "position_adjustment_enable": cfg.get("position_adjustment_enable"),
        "dry_run": cfg.get("dry_run"),
        "strategy": cfg.get("strategy"),
        "main_timeframe": cfg.get("timeframe"),
        "bot_name": cfg.get("bot_name"),
        "state": cfg.get("state"),
        "limits_note": (
            "No project-level caps on leverage, stake size, drawdown, or "
            "trading universe. Only the exchange's own per-market max "
            "leverage applies. The LLM is fully responsible for its own "
            "discipline."
        ),
    }


@mcp.tool()
def get_status() -> list[dict[str, Any]]:
    """Return all currently open trades.

    Each trade includes pair, side (long/short), leverage, open rate,
    current rate, unrealized PnL %, stop-loss price, and entry tag.
    Returns an empty list when no positions are open.
    """
    with FreqtradeClient() as ft:
        trades = ft.status()
    fields = (
        "trade_id", "pair", "is_short", "leverage", "stake_amount",
        "amount", "open_rate", "current_rate", "open_date",
        "profit_pct", "profit_abs", "stop_loss_abs", "stop_loss_pct",
        "liquidation_price", "enter_tag",
    )
    return [{k: t.get(k) for k in fields} for t in trades]


@mcp.tool()
def get_balance() -> dict[str, Any]:
    """Return wallet balance, bot-owned capital, and PnL since session start.

    Always check this before opening a new position so you know how
    much stake the Risk Guardian will allow (max_stake_fraction of
    bot-owned balance).
    """
    with FreqtradeClient() as ft:
        bal = ft.balance()
    return {
        "total_value_usdt": bal.get("total"),
        "bot_owned_usdt": bal.get("total_bot"),
        "starting_capital_usdt": bal.get("starting_capital"),
        "pnl_pct_since_start": bal.get("starting_capital_pct"),
        "is_simulated": bool(bal.get("note")),
        "currencies": [
            {
                "currency": c.get("currency"),
                "free": c.get("free"),
                "balance": c.get("balance"),
                "used": c.get("used"),
            }
            for c in bal.get("currencies", [])
        ],
    }


@mcp.tool()
def get_whitelist() -> dict[str, Any]:
    """Return the bot's currently monitored pair list.

    These pairs have OHLCV data being refreshed by the bot. To analyze
    a pair NOT in this list, you can still call get_pair_data — ccxt
    will fetch any pair the exchange supports. To force_enter a pair
    not in the whitelist, you may need to add_to_blacklist its current
    blacklist match first or wait for VolumePairList refresh.
    """
    with FreqtradeClient() as ft:
        wl = ft.whitelist()
    return {
        "pairs": wl.get("whitelist", []),
        "count": wl.get("length", 0),
        "method": wl.get("method", []),
    }


@mcp.tool()
def get_blacklist() -> dict[str, Any]:
    """Return pair patterns excluded from trading."""
    with FreqtradeClient() as ft:
        bl = ft.blacklist()
    return {
        "pairs": bl.get("blacklist", []),
        "count": bl.get("length", 0),
    }


@mcp.tool()
def list_supported_timeframes() -> list[str]:
    """List the timeframes available via get_pair_data."""
    return list(SUPPORTED_TIMEFRAMES)


@mcp.tool()
def get_pair_data(
    pair: str,
    timeframe: str = "5m",
    limit: int = 200,
) -> dict[str, Any]:
    """Fetch OHLCV candles + computed indicators at any timeframe.

    Args:
        pair: Trading pair, e.g. 'SOL/USDT:USDT' or 'PEPE/USDT:USDT'.
              The ':USDT' suffix marks a perpetual swap.
        timeframe: One of 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h,
                   1d, 3d, 1w, 1M. Pick based on your analysis horizon:
                   - 1m, 5m for scalping / very short-term
                   - 15m, 1h for short-term swings
                   - 4h, 1d for trend confirmation
                   - 1w, 1M for long-term positioning
        limit: Number of latest candles to return (max 1500).

    Returns columns: date, open, high, low, close, volume,
        ema_fast (12), ema_slow (26), sma_50, sma_200,
        rsi (14), macd, macd_signal, macd_hist,
        atr (14), bb_lower, bb_middle, bb_upper,
        volume_ma_20, volume_ratio.

    Data source: direct exchange fetch via ccxt (works for any pair the
    exchange supports, NOT limited to the bot's whitelist).
    """
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {
            "error": f"Unsupported timeframe '{timeframe}'.",
            "supported": list(SUPPORTED_TIMEFRAMES),
        }
    if limit <= 0 or limit > 1500:
        return {"error": "limit must be 1..1500"}
    try:
        return market.fetch_with_indicators(pair=pair, timeframe=timeframe, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_pair_data failed")
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------
# Professional market data — mark price / funding / liquidations / OI
# ---------------------------------------------------------------------
@mcp.tool()
def get_mark_ohlcv(
    pair: str, timeframe: str = "5m", limit: int = 200
) -> dict[str, Any]:
    """Mark-price OHLCV — what the exchange uses for liquidation and PnL.

    Mark price filters out spikes from low-liquidity last-trade prints.
    For risk decisions on perp futures, prefer mark over last.
    """
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"error": f"unsupported timeframe '{timeframe}'"}
    try:
        return market.fetch_mark_ohlcv(pair, timeframe, limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_index_ohlcv(
    pair: str, timeframe: str = "5m", limit: int = 200
) -> dict[str, Any]:
    """Index-price OHLCV — multi-exchange average reference price.

    Use to spot whether a move is local to one venue or market-wide.
    """
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"error": f"unsupported timeframe '{timeframe}'"}
    try:
        return market.fetch_index_ohlcv(pair, timeframe, limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_funding_rate(pair: str) -> dict[str, Any]:
    """Current funding rate + next settlement countdown.

    Sign convention: positive → longs pay shorts. Magnitude shows how
    crowded one side is — >0.05% per 8h is considered hot, >0.1% is
    rapidly contrarian territory.
    """
    try:
        return market.fetch_funding_rate(pair)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_funding_rate_history(pair: str, limit: int = 30) -> dict[str, Any]:
    """Past funding rates (typically 8h periods). Spot regime shifts."""
    try:
        return market.fetch_funding_history(pair, limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_open_interest(pair: str) -> dict[str, Any]:
    """Total contracts open on this perp.

    Rising OI + rising price = conviction trend.
    Rising OI + falling price = bearish conviction.
    Falling OI = positions closing, often exhaustion.
    """
    try:
        return market.fetch_open_interest(pair)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_recent_liquidations(pair: str, limit: int = 50) -> dict[str, Any]:
    """Recent liquidation events. Heavy clusters often mark short-term
    capitulation lows or blow-off tops."""
    try:
        return market.fetch_liquidations(pair, limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_orderbook(pair: str, depth: int = 20) -> dict[str, Any]:
    """Top-N order book levels — gauge liquidity, imbalance, spread."""
    if depth <= 0 or depth > 200:
        return {"error": "depth must be 1..200"}
    try:
        return market.fetch_orderbook(pair, depth)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_ticker_full(pair: str) -> dict[str, Any]:
    """Comprehensive ticker: last + mark + index + 24h stats + bid/ask."""
    try:
        return market.fetch_ticker_full(pair)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_position_detail(pair: str) -> dict[str, Any]:
    """Exchange-side position detail: liquidation price, margin ratio, etc.

    Note: Requires API keys. In dry-run without keys, falls back to an
    error and you should use get_status() instead.
    """
    try:
        return market.fetch_position_detail(pair)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def set_leverage(pair: str, leverage: float) -> dict[str, Any]:
    """Set leverage on a pair without opening a position.

    Note: Requires authenticated API access. In dry-run without keys
    pass leverage directly to force_enter instead.
    """
    if leverage <= 0:
        return {"error": "leverage must be > 0"}
    try:
        return market.set_leverage(pair, leverage)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def get_trades(limit: int = 20) -> dict[str, Any]:
    """Return recent trade history (open and closed).

    Useful for review/post-mortem: see PnL distribution, average hold
    time, win rate, and which entry tags are working.
    """
    with FreqtradeClient() as ft:
        result = ft.trades(limit=limit)
    fields = (
        "trade_id", "pair", "is_short", "leverage",
        "stake_amount", "open_date", "close_date",
        "open_rate", "close_rate",
        "profit_pct", "profit_abs", "exit_reason", "enter_tag",
    )
    return {
        "total_trades": result.get("total_trades", 0),
        "trades": [{k: t.get(k) for k in fields} for t in result.get("trades", [])],
    }


@mcp.tool()
def get_profit() -> dict[str, Any]:
    """Return aggregate profit/loss statistics: win rate, drawdown, avg PnL."""
    with FreqtradeClient() as ft:
        p = ft.profit()
    return {k: p.get(k) for k in (
        "profit_closed_coin", "profit_closed_percent_mean",
        "profit_all_coin", "profit_all_percent_mean",
        "trade_count", "closed_trade_count",
        "winning_trades", "losing_trades",
        "max_drawdown", "max_drawdown_abs",
        "first_trade_date", "latest_trade_date",
        "avg_duration",
    )}


# ---------------------------------------------------------------------
# Write tools — Risk Guardian enforced
# ---------------------------------------------------------------------
@mcp.tool()
def force_enter(
    pair: str,
    side: Literal["long", "short"],
    stake_usdt: float,
    leverage: float = 1.0,
    order_type: Literal["market", "limit"] = "market",
    price: float | None = None,
    enter_tag: str = "freqtrade_mcp",
    stop_loss: float | None = None,
    take_profit: float | None = None,
    stop_loss_trigger: Literal["last", "mark", "index"] = "last",
    take_profit_trigger: Literal["last", "mark", "index"] = "last",
) -> dict[str, Any]:
    """Open a perpetual futures position.

    Args:
        pair: e.g. 'SOL/USDT:USDT'.
        side: 'long' or 'short'.
        stake_usdt: Margin in USDT.
        leverage: Desired leverage. The exchange enforces its own per-market
                  cap; otherwise no project-level limit is applied.
        order_type: 'market' for immediate fill, 'limit' to wait at `price`.
        price: Required when order_type='limit'.
        enter_tag: Free-form label that shows in trade history.
        stop_loss: ABSOLUTE price (USDT) at which Freqtrade should auto-close
                   this position. For longs this is BELOW open_rate, for
                   shorts ABOVE. Equivalent to placing a stop-market order on
                   Gate. Optional — pass nothing to leave naked.
        take_profit: ABSOLUTE price at which to auto-close in profit. For
                     longs ABOVE open_rate, for shorts BELOW. Equivalent to
                     a take-profit order on Gate.

    Behavior:
        1. Submits /forceenter to Freqtrade. No project-level cap on leverage,
           stake, drawdown — the LLM is fully responsible for its own discipline.
           Only the exchange's per-market max leverage applies.
        2. If stop_loss / take_profit were provided, registers them in
           user_data/stop_levels.json so the strategy auto-closes when price
           crosses the levels — including while the LLM is asleep.
        3. If a trade for this pair/side is already open AND
           position_adjustment_enable=true in config.json, this adds to the
           existing position (averaging the entry price).

    Returns:
        On success: {'ok': True, 'trade': {...}, 'stop_levels': {...}}.
        On error:   {'error': '...'}.
    """
    if stake_usdt <= 0:
        return {"error": "stake_usdt must be > 0"}

    with FreqtradeClient() as ft:
        try:
            result = ft.force_enter(
                pair=pair,
                side=side,
                stake_amount=stake_usdt,
                leverage=leverage,
                order_type=order_type,
                price=price,
                enter_tag=enter_tag,
            )
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}

    fields = (
        "trade_id", "pair", "is_short", "leverage",
        "stake_amount", "amount", "open_rate", "open_date",
        "stop_loss_abs", "liquidation_price", "enter_tag",
    )
    trade_payload = {k: result.get(k) for k in fields}

    # Register system-level stop levels if provided.
    stop_levels_payload: dict[str, Any] | None = None
    trade_id = result.get("trade_id")
    if trade_id is not None and (stop_loss is not None or take_profit is not None):
        levels = _load_stop_levels()
        cfg = levels.setdefault(str(trade_id), {})
        if stop_loss is not None:
            cfg["sl"] = float(stop_loss)
            cfg["sl_trigger"] = stop_loss_trigger
        if take_profit is not None:
            cfg["tp"] = float(take_profit)
            cfg["tp_trigger"] = take_profit_trigger
        _save_stop_levels(levels)
        stop_levels_payload = cfg

    return {
        "ok": True,
        "trade": trade_payload,
        "stop_levels": stop_levels_payload,
    }


# ---------------------------------------------------------------------
# Stop-level management tools
# ---------------------------------------------------------------------
TriggerType = Literal["last", "mark", "index"]


@mcp.tool()
def set_stop_loss(
    trade_id: int,
    price: float,
    trigger: TriggerType = "last",
) -> dict[str, Any]:
    """Register a system-level stop-loss for an open trade.

    The strategy auto-closes when the chosen trigger price crosses ``price``.
    Updates take effect within seconds (10s cache on mark/index lookups).

    Args:
        trade_id: ID returned from get_status / force_enter.
        price: ABSOLUTE price in stake currency (USDT).
               Long: price < open_rate. Short: price > open_rate.
        trigger: Trigger price type. Mirrors Gate's order panel.
                 - 'last'  : last traded price (default)
                 - 'mark'  : mark price (recommended for risk decisions —
                             same source as the exchange's liquidation engine,
                             resists manipulation by single-trade prints)
                 - 'index' : multi-exchange index (least venue-specific noise)

    Note (dry-run): trigger evaluation respects your choice, but actual
    fill price comes from Freqtrade's last-price simulator. In live mode
    this maps directly to a Gate stop-market with trigger_price_type.
    """
    if price <= 0:
        return {"error": "price must be > 0"}
    if trigger not in ("last", "mark", "index"):
        return {"error": "trigger must be one of: last, mark, index"}
    levels = _load_stop_levels()
    cfg = levels.setdefault(str(trade_id), {})
    cfg["sl"] = float(price)
    cfg["sl_trigger"] = trigger
    _save_stop_levels(levels)
    return {"ok": True, "trade_id": trade_id, "stop_levels": cfg}


@mcp.tool()
def set_take_profit(
    trade_id: int,
    price: float,
    trigger: TriggerType = "last",
) -> dict[str, Any]:
    """Register a system-level take-profit for an open trade.

    Args:
        trade_id: ID returned from get_status / force_enter.
        price: ABSOLUTE price.
               Long: price > open_rate. Short: price < open_rate.
        trigger: 'last' | 'mark' | 'index'. See set_stop_loss for guidance.
    """
    if price <= 0:
        return {"error": "price must be > 0"}
    if trigger not in ("last", "mark", "index"):
        return {"error": "trigger must be one of: last, mark, index"}
    levels = _load_stop_levels()
    cfg = levels.setdefault(str(trade_id), {})
    cfg["tp"] = float(price)
    cfg["tp_trigger"] = trigger
    _save_stop_levels(levels)
    return {"ok": True, "trade_id": trade_id, "stop_levels": cfg}


@mcp.tool()
def get_stop_levels() -> dict[str, Any]:
    """Inspect all registered stop-loss / take-profit levels (keyed by trade_id)."""
    return {"levels": _load_stop_levels()}


@mcp.tool()
def clear_stop_level(
    trade_id: int,
    which: Literal["sl", "tp", "both"] = "both",
) -> dict[str, Any]:
    """Remove the stop-loss and/or take-profit registration for a trade.

    Args:
        trade_id: ID of the trade.
        which: 'sl' (only stop loss) | 'tp' (only take profit) | 'both' (default).
    """
    levels = _load_stop_levels()
    cfg = levels.get(str(trade_id))
    if cfg is None:
        return {"ok": True, "note": "no levels were set for this trade"}
    if which in ("sl", "both"):
        cfg.pop("sl", None)
    if which in ("tp", "both"):
        cfg.pop("tp", None)
    if not cfg:
        levels.pop(str(trade_id), None)
    _save_stop_levels(levels)
    return {"ok": True, "trade_id": trade_id, "stop_levels": cfg or None}


@mcp.tool()
def cancel_open_order(trade_id: int) -> dict[str, Any]:
    """Cancel a pending entry or exit limit order on a trade.

    Use this when a limit order you placed (e.g. via force_enter with
    order_type='limit') is sitting unfilled and you want to retract it.
    The trade itself remains; only the open order is dropped.
    """
    with FreqtradeClient() as ft:
        try:
            return ft.cancel_open_order(trade_id)
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}


@mcp.tool()
def force_exit(
    trade_id: int | str,
    amount: float | None = None,
    order_type: Literal["market", "limit"] | None = None,
) -> dict[str, Any]:
    """Close an open position fully or partially.

    Args:
        trade_id: Integer id from get_status, or the string 'all' to flatten
                  every open trade at once.
        amount:   Optional partial-close amount in base currency
                  (e.g. SOL, BTC). Omit to close the whole position.
        order_type: 'market' for immediate fill, 'limit' to rest on the book
                    at the most recent bid/ask. Omit to use strategy default.
    """
    with FreqtradeClient() as ft:
        try:
            return ft.force_exit(
                trade_id,
                order_type=order_type,
                amount=amount,
            )
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}


@mcp.tool()
def reverse_position(
    trade_id: int,
    leverage: float | None = None,
    enter_tag: str = "reverse",
) -> dict[str, Any]:
    """Flatten current position and immediately open the SAME stake in the
    opposite direction at market. Equivalent to Gate's "Reverse" button.

    Args:
        trade_id: ID of the trade to reverse.
        leverage: Override leverage for the new position (defaults to the
                  closed trade's leverage).
        enter_tag: Free-form label for the new trade.

    Behavior:
        1. Read current trade to capture pair / side / stake / leverage.
        2. Submit market exit on the existing trade.
        3. Open a market entry of the OPPOSITE side, same stake, same
           leverage by default.
        4. Returns both the close_result and the new_trade.

    Caveat: Between steps 2 and 3 the price can move. For tight slippage
    on big positions, prefer manual exit + observe + manual entry.
    """
    with FreqtradeClient() as ft:
        try:
            statuses = ft.status()
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}

    target = next((t for t in statuses if int(t.get("trade_id", -1)) == int(trade_id)), None)
    if target is None:
        return {"error": f"trade {trade_id} not found among open trades"}

    pair = target["pair"]
    was_short = bool(target.get("is_short"))
    new_side: Literal["long", "short"] = "long" if was_short else "short"
    stake_usdt = float(target.get("stake_amount", 0))
    new_leverage = float(leverage) if leverage is not None else float(target.get("leverage", 1.0))

    with FreqtradeClient() as ft:
        try:
            close_result = ft.force_exit(trade_id, order_type="market")
        except FreqtradeAPIError as exc:
            return {"error": f"close failed: {exc}"}

        try:
            open_result = ft.force_enter(
                pair=pair,
                side=new_side,
                stake_amount=stake_usdt,
                leverage=new_leverage,
                order_type="market",
                enter_tag=enter_tag,
            )
        except FreqtradeAPIError as exc:
            return {
                "error": f"reopen failed (position is now flat): {exc}",
                "close_result": close_result,
            }

    return {
        "ok": True,
        "closed": close_result,
        "opened": {
            "trade_id": open_result.get("trade_id"),
            "pair": pair,
            "side": new_side,
            "stake_amount": stake_usdt,
            "leverage": new_leverage,
            "open_rate": open_result.get("open_rate"),
        },
    }


# ---------------------------------------------------------------------
# Bot lifecycle controls (start / stop / pause new entries)
# ---------------------------------------------------------------------
@mcp.tool()
def start_bot() -> dict[str, Any]:
    """Resume the trading bot main loop after a stop_bot.

    The bot resumes evaluating positions and accepting new force_enter calls.
    """
    with FreqtradeClient() as ft:
        try:
            return ft.start()
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}


@mcp.tool()
def stop_bot() -> dict[str, Any]:
    """Stop the trading bot main loop entirely.

    Open positions remain but no new entries / exits / stop-loss adjustments
    happen until start_bot is called. Use sparingly — usually pause_entry is
    what you want.
    """
    with FreqtradeClient() as ft:
        try:
            return ft.stop()
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}


@mcp.tool()
def pause_entry() -> dict[str, Any]:
    """Stop accepting new entries while keeping the bot otherwise running.

    Open positions still get exit/stoploss management. Use when you want to
    let the existing book wind down without taking new risk.
    """
    with FreqtradeClient() as ft:
        try:
            return ft.stopentry()
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}


@mcp.tool()
def reload_config() -> dict[str, Any]:
    """Hot-reload Freqtrade's config.json (e.g. after editing it externally)."""
    with FreqtradeClient() as ft:
        try:
            return ft.reload_config()
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}


# ---------------------------------------------------------------------
# Curation tools — adjust trading universe
# ---------------------------------------------------------------------
@mcp.tool()
def add_to_blacklist(pairs: list[str]) -> dict[str, Any]:
    """Block pairs (or regex patterns) from any future trading.

    Use this when you decide a pair is unsafe (low liquidity, scam token,
    delisted-soon). Pass full pair strings 'BTC/USDT:USDT' or regex
    patterns like 'TEST.*'.
    """
    if not pairs:
        return {"error": "pairs list is empty"}
    with FreqtradeClient() as ft:
        try:
            return ft.add_blacklist(pairs)
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}


@mcp.tool()
def remove_from_blacklist(pairs: list[str]) -> dict[str, Any]:
    """Remove pairs from the blacklist."""
    if not pairs:
        return {"error": "pairs list is empty"}
    with FreqtradeClient() as ft:
        try:
            return ft.remove_blacklist(pairs)
        except FreqtradeAPIError as exc:
            return {"error": str(exc)}


# ---------------------------------------------------------------------
# Prompts — reusable conversation templates
# ---------------------------------------------------------------------
@mcp.prompt()
def daily_review() -> str:
    """Generate today's trading post-mortem."""
    return (
        "Run a daily review of the trading bot's activity:\n"
        "1. Call get_profit() and summarize win rate, average PnL, and drawdown.\n"
        "2. Call get_trades(limit=50) and group by enter_tag — which tags worked?\n"
        "3. Call get_status() and assess current open positions: are any due\n"
        "   for taking profit or tightening stops?\n"
        "4. Output: a concise human-readable report with one concrete action\n"
        "   recommendation (or 'hold steady' if appropriate).\n"
    )


@mcp.prompt()
def scan_market(timeframe: str = "1h") -> str:
    """Scan the watched universe for actionable opportunities."""
    return (
        f"Scan the perpetual futures universe for opportunities on the {timeframe} timeframe:\n"
        "1. Call get_whitelist() to list monitored pairs.\n"
        "2. For each interesting candidate (focus on top 10-15 by your judgment),\n"
        f"   call get_pair_data(pair, timeframe='{timeframe}', limit=120).\n"
        "3. Compute concrete signals from the returned indicators (RSI bounds,\n"
        "   MACD cross, BB squeeze, volume spike vs volume_ma_20).\n"
        "4. Call get_balance() and get_config_summary() to know your budget\n"
        "   and Risk Guardian limits.\n"
        "5. Output: at most 3 ranked candidates, each with:\n"
        "   - direction (long/short) and conviction (low/med/high)\n"
        "   - proposed leverage and stake (within Guardian limits)\n"
        "   - entry, stop-loss, take-profit levels\n"
        "   - one-line reasoning citing specific indicator values\n"
        "Do NOT auto-execute. Wait for the user to approve and call force_enter.\n"
    )


# ---------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------
def main() -> None:
    """Run the MCP server over stdio (default for desktop / CLI clients)."""
    settings = get_settings()
    logger.info(
        "freqtrade-mcp starting | freqtrade=%s | exchange=%s | mode=%s | max_lev=%sx | max_stake=%.0f%%",
        settings.freqtrade_url,
        settings.exchange_id,
        settings.permission_mode.value,
        settings.risk_max_leverage,
        settings.risk_max_stake_fraction * 100,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
