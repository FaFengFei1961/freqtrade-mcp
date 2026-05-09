"""Configuration loaded from environment / .env file.

Single source of truth for all runtime settings.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PermissionMode(str, Enum):
    """Permission tiers — mirror Claude Code's mode selector."""

    PLAN = "plan"
    ASK = "ask"
    AUTO_SMALL = "auto-small"
    AUTO = "auto"
    BYPASS = "bypass"


class Settings(BaseSettings):
    """freqtrade-mcp runtime configuration.

    Resolution order:
      1. Environment variables
      2. `.env` file in the project root
      3. Defaults declared on the model
    """

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Freqtrade REST API ---
    freqtrade_url: HttpUrl = Field(default=HttpUrl("http://127.0.0.1:8080"))
    freqtrade_username: str = Field(default="freqtrader")
    freqtrade_password: SecretStr = Field(default=SecretStr(""))

    # --- Exchange for ccxt direct fetch (multi-timeframe market data) ---
    # Defaults match Freqtrade's exchange when in futures mode.
    exchange_id: str = Field(default="gate")
    exchange_market_type: str = Field(default="swap")  # "swap" (perp) or "spot"

    # --- Risk Guardian: limits (set to permissive defaults; .env can tighten) ---
    # Default values here are deliberately permissive ("no guardrails" mode).
    # Tighten in `.env` if/when you want to add training wheels back.
    risk_max_leverage: float = Field(default=125.0, gt=0, le=200)
    risk_max_stake_fraction: float = Field(default=1.0, gt=0, le=1.0)
    risk_daily_loss_circuit: float = Field(default=-1.0, ge=-1.0, le=0.0)
    risk_total_drawdown_circuit: float = Field(default=-1.0, ge=-1.0, le=0.0)
    risk_min_24h_volume_usdt: float = Field(default=0, ge=0)

    # --- Permission mode (informational; client controls actual approval flow) ---
    permission_mode: PermissionMode = Field(default=PermissionMode.ASK)

    # --- Path to Freqtrade's user_data directory (shared with Docker volume) ---
    # The MCP server writes stop_levels.json here; the strategy reads it from
    # the corresponding container path /freqtrade/user_data/stop_levels.json.
    # Set USER_DATA_PATH in .env to your absolute path, e.g.
    #   USER_DATA_PATH=/home/me/freqtrade-data/user_data
    #   USER_DATA_PATH=D:\\project\\freqtrade-data\\user_data
    user_data_path: Path = Field(default=Path("user_data"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Cached so the .env file is read only once per process. Tests that need a
    fresh config should call ``get_settings.cache_clear()``.
    """
    return Settings()
