"""Risk Guardian — hard limits enforced regardless of permission mode.

This module is intentionally simple, pure, and synchronous. It is the LAST
line of defense before any state-changing action is sent to Freqtrade. The
LLM must never write to this file; humans curate the rules.

Decision contract:
    Every check returns a ``Decision``. ``approved`` is True only if every
    individual rule passes. ``reasons`` always contains a human-readable
    explanation, populated for both rejections and approvals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .config import Settings, get_settings


@dataclass(frozen=True)
class Decision:
    """Outcome of a Risk Guardian check.

    Attributes:
        approved: True if the action is allowed, False if blocked.
        reasons: List of rule evaluations (each line: ``[OK|BLOCK] message``).
    """

    approved: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        verdict = "APPROVED" if self.approved else "BLOCKED"
        return f"{verdict}\n  " + "\n  ".join(self.reasons)


@dataclass(frozen=True)
class EntryIntent:
    """A proposed force-entry that must pass the Guardian first."""

    pair: str
    side: Literal["long", "short"]
    leverage: float
    stake_amount: float
    bot_owned_balance: float  # Total USDT the bot is allowed to deploy.
    starting_capital: float  # Snapshot at session start, for drawdown calc.
    current_total_value: float  # Live equity, for drawdown calc.


class RiskGuardian:
    """Stateless evaluator. One instance can serve many decisions."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()

    # ------------------------------------------------------------------
    def check_entry(self, intent: EntryIntent) -> Decision:
        """Evaluate every hard rule against a proposed entry."""
        reasons: list[str] = []
        approved = True

        # --- Rule 1: leverage cap ---
        if intent.leverage > self._s.risk_max_leverage:
            approved = False
            reasons.append(
                f"[BLOCK] leverage {intent.leverage}x exceeds hard cap "
                f"{self._s.risk_max_leverage}x"
            )
        else:
            reasons.append(
                f"[OK] leverage {intent.leverage}x within cap {self._s.risk_max_leverage}x"
            )

        # --- Rule 2: stake size cap ---
        if intent.bot_owned_balance <= 0:
            approved = False
            reasons.append("[BLOCK] bot has zero deployable balance")
        else:
            stake_fraction = intent.stake_amount / intent.bot_owned_balance
            if stake_fraction > self._s.risk_max_stake_fraction:
                approved = False
                reasons.append(
                    f"[BLOCK] stake {intent.stake_amount:.2f} USDT is "
                    f"{stake_fraction:.1%} of bot balance "
                    f"(cap {self._s.risk_max_stake_fraction:.1%})"
                )
            else:
                reasons.append(
                    f"[OK] stake {intent.stake_amount:.2f} USDT = "
                    f"{stake_fraction:.1%} of bot balance"
                )

        # --- Rule 3: total drawdown circuit-breaker ---
        if intent.starting_capital <= 0:
            reasons.append("[OK] starting capital unset, drawdown check skipped")
        else:
            drawdown = (intent.current_total_value - intent.starting_capital) / intent.starting_capital
            if drawdown <= self._s.risk_total_drawdown_circuit:
                approved = False
                reasons.append(
                    f"[BLOCK] total drawdown {drawdown:.1%} reached "
                    f"circuit-breaker {self._s.risk_total_drawdown_circuit:.1%}"
                )
            else:
                reasons.append(
                    f"[OK] total drawdown {drawdown:+.1%} within "
                    f"limit {self._s.risk_total_drawdown_circuit:.1%}"
                )

        # --- Rule 4: positive stake amount ---
        if intent.stake_amount <= 0:
            approved = False
            reasons.append("[BLOCK] stake amount must be positive")

        return Decision(approved=approved, reasons=reasons)
