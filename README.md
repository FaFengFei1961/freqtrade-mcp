# freqtrade-mcp

> **Bridge any MCP-aware LLM client (Claude Code / Codex CLI / Gemini CLI / Cursor / Cline / Claude Desktop / …) to a [Freqtrade](https://www.freqtrade.io/) trading bot.**
>
> Lets a language model trade perpetual futures (or any market Freqtrade supports) through 33 typed tools, with system-level take-profit / stop-loss enforcement, mark/last/index trigger price selection, and full ccxt-backed multi-timeframe market data — all over the Model Context Protocol.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

## Why

Freqtrade is a battle-tested algorithmic trading framework, but its strategy code is *deterministic*. If you want an LLM to **decide** what to trade — pick pairs, sizing, leverage, stops — you need a way to expose Freqtrade's full capability surface to the model.

`freqtrade-mcp` is that bridge. The LLM gets:

- **Real-time market data** for any pair on any timeframe (via ccxt direct fetch — bypasses Freqtrade's whitelist)
- **Last / Mark / Index** triple-price visibility, so risk decisions match how the exchange actually computes liquidations
- **System-enforced stop-loss / take-profit** that fire even when the LLM is asleep between heartbeats
- **Full lifecycle control** — open, close, partial-close, reverse, leverage adjust, blacklist mgmt, bot start/stop

The LLM provides judgment, Freqtrade provides execution, and ccxt provides exchange connectivity. **It works with Gate.io out of the box and any of ccxt's ~100 supported exchanges** with a single `.env` change.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  MCP Client                                                  │
│  Claude Code / Codex CLI / Gemini CLI / Cursor / Cline / …  │
└─────────────────────────┬────────────────────────────────────┘
                          │ stdio (MCP protocol)
                          ▼
┌──────────────────────────────────────────────────────────────┐
│  freqtrade-mcp (this project)                                │
│   • 33 tools (read state / trade / manage)                   │
│   • Pure-pandas indicator computation                        │
│   • Mark/Last/Index trigger evaluation w/ 10s cache          │
│   • stop_levels.json shared with strategy                    │
└──────────┬─────────────────────────────────┬─────────────────┘
           │ HTTP REST                       │ ccxt direct
           ▼                                 ▼
┌─────────────────────────────┐   ┌──────────────────────────┐
│  Freqtrade (Docker)         │   │  Exchange (Gate / Binance │
│   • State / wallet / DB     │   │   / Bybit / OKX / …)     │
│   • MetaStrategy.py         │   │   Public market data     │
│     reads stop_levels.json  │   │   for any timeframe       │
└─────────────────────────────┘   └──────────────────────────┘
```

---

## Quick Start

### 0. Prerequisites

- **Python 3.11+** (3.12 recommended; the project supports up to 3.14)
- **Docker** + **Docker Compose** (Freqtrade runs in a container)
- An MCP-aware client: [Claude Code](https://www.anthropic.com/claude-code), [Codex CLI](https://developers.openai.com/codex/cli/), [Gemini CLI](https://github.com/google-gemini/gemini-cli), Cursor, Cline, or Claude Desktop.

### 1. Run Freqtrade in Docker (dry-run mode)

```bash
mkdir -p freqtrade-data/user_data/strategies freqtrade-data/user_data/logs
cp examples/freqtrade/config.json.example  freqtrade-data/user_data/config.json
cp examples/freqtrade/MetaStrategy.py       freqtrade-data/user_data/strategies/
cp examples/freqtrade/docker-compose.yml.example  freqtrade-data/docker-compose.yml

# Generate API server secrets and edit them into config.json
python -c "import secrets; print('jwt_secret_key:', secrets.token_hex(32))"
python -c "import secrets; print('ws_token:', secrets.token_hex(32))"
python -c "import secrets; print('password:', secrets.token_urlsafe(24))"
# (Replace the three REPLACE_ME values in user_data/config.json)

cd freqtrade-data
docker compose up -d
```

FreqUI is now at `http://127.0.0.1:8080`. Log in with `freqtrader` / your generated password.

### 2. Install freqtrade-mcp

```bash
git clone https://github.com/FaFengFei1961/freqtrade-mcp.git
cd freqtrade-mcp

python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\Activate.ps1
pip install -e .

cp .env.example .env
# Edit .env — at minimum set FREQTRADE_PASSWORD to match config.json.
```

### 3. Verify

```bash
freqtrade-mcp doctor
```

Should print "All systems go." Confirms Freqtrade is reachable and ccxt can pull data.

### 4. Wire up your MCP client

Pick the snippet that matches your client and adjust the absolute path:

| Client       | File                                             | Snippet                                              |
|--------------|--------------------------------------------------|------------------------------------------------------|
| Claude Code  | `<project>/.mcp.json`                            | [claude-code.mcp.json](examples/clients/claude-code.mcp.json) |
| Codex CLI    | `~/.codex/config.toml`                           | [codex-config.toml.snippet](examples/clients/codex-config.toml.snippet) |
| Gemini CLI   | `~/.gemini/settings.json`                        | [gemini-settings.json.snippet](examples/clients/gemini-settings.json.snippet) |

Restart your client. The `freqtrade` MCP server will appear with **33 tools** registered.

### 5. Talk to the bot

In your MCP client, ask:

> *"Use freqtrade tools — call get_config_summary, get_balance, then scan BTC/USDT:USDT and SOL/USDT:USDT on 1h and 5m. Don't open anything yet, just summarize."*

The LLM will discover the tools, call them, and produce a market summary. From there, you can let it open positions with `force_enter`, set stops, etc. — see the tool reference below.

---

## The 33 Tools

### Read (information)
| Tool | Returns |
|---|---|
| `get_config_summary` | Bot configuration snapshot |
| `get_status` | Open trades |
| `get_balance` | Wallet balance + PnL |
| `get_whitelist` / `get_blacklist` | Currently monitored / excluded pairs |
| `get_trades` | Recent trade history |
| `get_profit` | Aggregate P&L statistics |
| `get_stop_levels` | Currently registered SL/TP levels |
| `list_supported_timeframes` | 1m through 1M |

### Market data (any pair, any timeframe — bypasses whitelist)
| Tool | Returns |
|---|---|
| `get_pair_data(pair, timeframe, limit)` | Last-price OHLCV + 21 indicators (RSI, MACD, EMA, BB, ATR, volume) |
| `get_mark_ohlcv` | Mark-price candles (what the exchange uses for liquidation) |
| `get_index_ohlcv` | Index-price candles (multi-exchange average) |
| `get_ticker_full` | Last + Mark + Index + 24h stats + bid/ask |
| `get_orderbook(pair, depth)` | Top-N bids/asks + spread |
| `get_funding_rate` | Current rate + next-settlement countdown |
| `get_funding_rate_history` | Past rates |
| `get_open_interest` | Total contracts outstanding |
| `get_recent_liquidations` | Recent liquidation events |
| `get_position_detail` | Exchange-side liquidation price, margin ratio (live mode) |

### Trade lifecycle
| Tool | What it does |
|---|---|
| `force_enter(pair, side, stake_usdt, leverage, ..., stop_loss, stop_loss_trigger, take_profit, take_profit_trigger)` | Open position; optionally register SL/TP atomically |
| `force_exit(trade_id, amount?, order_type?)` | Close fully or partially; market or limit |
| `reverse_position(trade_id)` | Flatten + open same-size opposite direction |
| `cancel_open_order(trade_id)` | Cancel a pending entry/exit limit order |

### Risk management
| Tool | What it does |
|---|---|
| `set_stop_loss(trade_id, price, trigger='last'|'mark'|'index')` | System-level SL — fires even while LLM is offline |
| `set_take_profit(trade_id, price, trigger='last'|'mark'|'index')` | System-level TP |
| `clear_stop_level(trade_id, which='sl'|'tp'|'both')` | Remove a registered level |

### Bot control
| Tool | What it does |
|---|---|
| `start_bot` / `stop_bot` | Resume / halt the trading loop |
| `pause_entry` | Stop accepting new entries (existing positions still managed) |
| `set_leverage(pair, leverage)` | Adjust leverage without opening a position (live mode) |
| `add_to_blacklist(pairs)` / `remove_from_blacklist(pairs)` | Curate trading universe |
| `reload_config` | Hot-reload Freqtrade's config.json |

---

## Trigger price types

When you set stop-loss or take-profit, you choose **which price stream the trigger evaluates against**:

| Trigger | What it is | When to use |
|---|---|---|
| `last` | Most recent traded price | Real-time exits — but vulnerable to single-trade wicks |
| `mark` | Exchange-computed reference (index + funding adjustment) | **Recommended for stop-loss** — same source as the liquidation engine, resists manipulation |
| `index` | Multi-exchange average | Cross-platform consensus, useful when one venue is glitchy |

A common pattern:

```python
force_enter(
    pair="SOL/USDT:USDT", side="long", stake_usdt=50, leverage=3,
    stop_loss=140.0,  stop_loss_trigger="mark",   # stop on the price that matters for liquidation
    take_profit=155.0, take_profit_trigger="last" # exit profit on real fills
)
```

---

## Multi-Exchange Support

The data and order layers are pure ccxt — change one line in `.env`:

```bash
EXCHANGE_ID=gate         # default: Gate.io
EXCHANGE_ID=binance      # Binance USDT-M perpetuals
EXCHANGE_ID=bybit        # Bybit
EXCHANGE_ID=okx          # OKX
EXCHANGE_ID=hyperliquid  # Hyperliquid (DEX)
EXCHANGE_ID=bitget
EXCHANGE_ID=kucoinfutures
# … any of the ~100 ccxt exchanges
```

Most tools work unchanged. Exchange-specific high-end features (e.g. iceberg orders, partial-fill semantics) may require small adapters in `market.py`.

---

## Limits and known caveats

- **Currently dry-run by default.** All orders are simulated by Freqtrade's internal engine using last-price ticks — no real funds are at risk and no orders hit the exchange. Set `dry_run: false` and provide exchange API keys to go live.
- **Mark/index triggers are evaluated by the strategy in dry-run** (custom_exit hook fetches mark price every ~10s). In live mode, the proper approach is to forward these triggers to native exchange conditional orders via ccxt — see `examples/` for the planned migration path.
- **No project-level caps.** Leverage / stake / drawdown limits were intentionally removed — the LLM is fully responsible for its own discipline. Reintroduce caps in `freqtrade_mcp/risk_guardian.py` if you want training wheels.
- **The bundled `MetaStrategy` is intentionally passive.** It does not generate entry signals on its own — every position is opened/closed via the LLM through the MCP. If you want autonomous strategy logic alongside, write your own strategy class.
- **Token cost vs. PnL.** A 5-minute heartbeat with a frontier model can run $5–$20/day in API costs. For low-stake testing this dwarfs the PnL — make sure your sizing is realistic before drawing conclusions.

---

## Project layout

```
freqtrade_mcp/
├── mcp_server.py        # The 33 MCP tools
├── market.py            # ccxt-backed market data + indicator computation
├── freqtrade_client.py  # Typed wrapper over Freqtrade's REST API
├── config.py            # pydantic-settings, .env loader
├── risk_guardian.py     # Optional pre-trade gate (defaults to permissive)
└── cli.py               # `freqtrade-mcp` entry point + debug subcommands

examples/
├── freqtrade/           # config.json + MetaStrategy.py + docker-compose.yml
└── clients/             # MCP client config snippets

tests/
└── test_mcp_e2e.py      # End-to-end smoke test (spawns server, calls tools)
```

---

## License & Upstream Compatibility

**This project: [GNU AGPL-3.0-or-later](LICENSE)** — strong copyleft. You can use, modify, and run it commercially, **but any derivative work — including network-accessible services (SaaS) — must also be released under AGPL-3.0 with full source available to its users.**

This is a deliberate choice: trading-bot tooling tends to drift behind closed doors. AGPL keeps improvements in the commons.

### Compatibility with Freqtrade (GPL-3.0)

[Freqtrade](https://github.com/freqtrade/freqtrade) — the trading framework this project sits on top of — is licensed under **GPL-3.0**.

| Component | License obligation | Why |
|---|---|---|
| `freqtrade_mcp/*.py` (this project's main code) | AGPL-3.0 | Communicates with Freqtrade only over HTTP REST and ccxt-direct fetches — "arm's length" interop, not a derivative work under GPL. We choose AGPL freely. |
| `examples/freqtrade/MetaStrategy.py` | AGPL-3.0 (compatible with the GPL-3.0 it inherits) | Subclasses `freqtrade.strategy.IStrategy` and imports `freqtrade.persistence.Trade`, so it IS a derivative work of Freqtrade and inherits GPL-3.0 obligations. AGPL-3.0 is explicitly upward-compatible with GPL-3.0 (GPL §13 + AGPL §13), so distributing this file under AGPL-3.0 satisfies both. |

In plain English: **GPL-3.0 code can be combined with AGPL-3.0 code**, the combined work goes out under AGPL-3.0, and Freqtrade's authors are credited. The GNU project itself sanctions this combination — see GPL-3.0 §13:

> Notwithstanding any other provision of this License, you have permission to link or combine any covered work with a work licensed under version 3 of the GNU Affero General Public License into a single combined work, and to convey the resulting work.

See **[NOTICE](NOTICE)** for the full attribution list (Freqtrade, ccxt, MCP SDK, pandas, NumPy, httpx, pydantic, Typer, Rich, python-dotenv).

---

## Acknowledgements

- [Freqtrade](https://www.freqtrade.io/) — the trading framework that does the actual heavy lifting
- [ccxt](https://github.com/ccxt/ccxt) — the unified exchange API library
- [Anthropic's Model Context Protocol](https://modelcontextprotocol.io/) — the standard that makes this bridgeable to any compliant client

---

## Disclaimer

This project is for educational and research use. Trading derivatives involves substantial risk of loss; you can lose more than you deposit. The authors are not responsible for any trading losses incurred while using this software, dry-run or live. **Always test thoroughly on dry-run / testnet before risking real capital.**
