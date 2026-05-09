"""freqtrade-mcp CLI — dual purpose.

Default behavior (no subcommand): start the MCP server over stdio. This is
how MCP-aware clients (Claude Code / Codex CLI / Gemini CLI / Cursor / Cline /
Claude Desktop) launch the server.

Subcommands are debugging utilities for human use.
"""

from __future__ import annotations

import typer
from rich.console import Console

from . import __version__
from .config import get_settings
from .freqtrade_client import FreqtradeAPIError, FreqtradeClient

app = typer.Typer(
    name="freqtrade-mcp",
    help="MCP server bridging Freqtrade to MCP-aware LLM clients.",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True, style="bold red")


def _abort(message: str) -> None:
    err_console.print(f"[ERROR] {message}")
    raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context) -> None:
    """Start the MCP server over stdio when invoked without a subcommand."""
    if ctx.invoked_subcommand is None:
        # Imported lazily so debugging subcommands don't pay MCP startup cost.
        from . import mcp_server
        mcp_server.main()


@app.command()
def version() -> None:
    """Print the package version."""
    console.print(f"freqtrade-mcp {__version__}")


@app.command()
def doctor() -> None:
    """Verify configuration and end-to-end connectivity to Freqtrade."""
    s = get_settings()
    console.print("[bold]Configuration[/bold]")
    console.print(f"  freqtrade_url        : {s.freqtrade_url}")
    console.print(f"  freqtrade_username   : {s.freqtrade_username}")
    console.print(f"  password set         : {bool(s.freqtrade_password.get_secret_value())}")
    console.print(f"  exchange (ccxt)      : {s.exchange_id} / {s.exchange_market_type}")
    console.print(f"  permission mode      : {s.permission_mode.value}")
    console.print(f"  max leverage         : {s.risk_max_leverage}x")
    console.print(f"  max stake fraction   : {s.risk_max_stake_fraction:.0%}")
    console.print(f"  daily loss circuit   : {s.risk_daily_loss_circuit:.0%}")
    console.print(f"  drawdown circuit     : {s.risk_total_drawdown_circuit:.0%}")

    console.print("\n[bold]Freqtrade connection[/bold]")
    try:
        with FreqtradeClient() as ft:
            console.print(f"  ping       : {ft.ping()}")
            v = ft.version()
            console.print(f"  version    : {v.get('version')}")
            cfg = ft.show_config()
            console.print(f"  exchange   : {cfg.get('exchange')}")
            console.print(f"  trading    : {cfg.get('trading_mode')} / {cfg.get('margin_mode')}")
            console.print(f"  dry-run    : {cfg.get('dry_run')}")
            console.print(f"  strategy   : {cfg.get('strategy')}")
    except FreqtradeAPIError as exc:
        _abort(f"Freqtrade unreachable: {exc}")
    except Exception as exc:  # noqa: BLE001
        _abort(f"connection failed: {exc}")

    console.print("\n[bold]ccxt market data[/bold]")
    try:
        from . import market
        df = market.fetch_ohlcv("BTC/USDT:USDT", timeframe="1h", limit=2)
        if df.empty:
            console.print("  [yellow]no data returned (exchange empty?)[/yellow]")
        else:
            console.print(f"  fetched {len(df)} candles for BTC/USDT:USDT 1h")
            console.print(f"  latest close: {df['close'].iloc[-1]}")
    except Exception as exc:  # noqa: BLE001
        _abort(f"ccxt fetch failed: {exc}")

    console.print("\n[green]All systems go.[/green]")


@app.command()
def status() -> None:
    """Show open trades (debug helper)."""
    with FreqtradeClient() as ft:
        trades = ft.status()
    if not trades:
        console.print("[dim]No open trades.[/dim]")
        return
    for t in trades:
        side = "SHORT" if t.get("is_short") else "LONG"
        console.print(
            f"  #{t.get('trade_id')} {side} {t.get('pair')} "
            f"@ {t.get('open_rate')} lev={t.get('leverage')}x "
            f"PnL={t.get('profit_pct'):+.2f}%"
        )


@app.command()
def balance() -> None:
    """Show wallet balance (debug helper)."""
    with FreqtradeClient() as ft:
        bal = ft.balance()
    note = bal.get("note") or "live"
    console.print(f"[bold]Wallet[/bold] ([yellow]{note}[/yellow])")
    console.print(f"  total      : {bal.get('total', 0):,.2f} USDT")
    console.print(f"  bot-owned  : {bal.get('total_bot', 0):,.2f} USDT")
    console.print(f"  starting   : {bal.get('starting_capital', 0):,.2f} USDT")
    console.print(f"  PnL pct    : {bal.get('starting_capital_pct', 0):+.2f}%")


if __name__ == "__main__":
    app()
