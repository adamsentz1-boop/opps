"""Exit levels and risk-based position sizing - computed in Python, never by an agent.

Two rules decide how much of the account a single idea may cost:

1. **Every entry carries an exit plan before it is proposed.** A stop and a target are derived from how much
   the security actually moves day to day, not from a round number and not from the model's opinion. A name
   that swings 6% a day gets a wider stop than one that swings 1%, because an identical stop would be noise
   on one and a real signal on the other.

2. **Position size is bounded by the loss at the stop, not by available cash.** Risking a fixed small slice
   of the portfolio per idea is what keeps a run of losses survivable. Without this a single 60% position can
   halve the account on one bad week.

These are proposals for the owner to act on manually. Nothing here places an order or moves money, and a
stop is only real once the owner enters it with their broker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.market.indicators import PriceStats

# Fallback when there is too little history to measure how much the security moves.
DEFAULT_STOP_PCT = 12.0


@dataclass
class TradeLevels:
    """The exit plan attached to a proposed entry."""
    entry_price: float
    stop_price: float
    target_price: float
    stop_distance_pct: float
    target_distance_pct: float
    reward_risk: float
    basis: str                      # how the stop distance was derived, for the audit trail

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def compute_levels(entry_price: float, stats: PriceStats | None, *, stop_move_multiple: float = 2.5,
                   min_stop_pct: float = 5.0, max_stop_pct: float = 25.0,
                   reward_risk_target: float = 2.0) -> TradeLevels:
    """Derive a stop and a target for a long entry.

    The stop sits `stop_move_multiple` average daily moves below the entry, clamped into a sane band so a
    quiet name does not get a 1% stop and a wild one does not get a 60% stop. The target is placed at
    `reward_risk_target` times the stop distance, which is what makes the reward-to-risk ratio a property of
    the plan rather than a number the model asserts.
    """
    entry_price = float(entry_price)
    if entry_price <= 0:
        raise ValueError("entry price must be positive")
    min_stop_pct = max(0.1, float(min_stop_pct))
    max_stop_pct = max(min_stop_pct, float(max_stop_pct))

    move = stats.avg_daily_move_pct if (stats and stats.sufficient and stats.avg_daily_move_pct) else None
    if move:
        raw_pct = move * float(stop_move_multiple)
        basis = f"{stop_move_multiple:g} x average daily move of {move:.2f}%"
    else:
        raw_pct = DEFAULT_STOP_PCT
        basis = f"default {DEFAULT_STOP_PCT:g}% (insufficient price history)"

    stop_pct = min(max(raw_pct, min_stop_pct), max_stop_pct)
    if stop_pct != raw_pct:
        basis += f", clamped to {stop_pct:.2f}%"
    target_pct = stop_pct * float(reward_risk_target)

    stop_price = round(entry_price * (1 - stop_pct / 100.0), 4)
    target_price = round(entry_price * (1 + target_pct / 100.0), 4)
    return TradeLevels(entry_price=round(entry_price, 4), stop_price=stop_price, target_price=target_price,
                       stop_distance_pct=round(stop_pct, 2), target_distance_pct=round(target_pct, 2),
                       reward_risk=round(float(reward_risk_target), 2), basis=basis)


def risk_per_share(entry_price: float, stop_price: float) -> float:
    """What one share loses if the stop is hit."""
    return max(0.0, round(float(entry_price) - float(stop_price), 6))


def max_quantity_for_risk(portfolio_value: float, entry_price: float, stop_price: float,
                          max_risk_pct: float) -> float:
    """Largest quantity whose loss at the stop stays inside the per-trade risk budget.

    Returns 0.0 when the stop is at or above the entry, which would make the risk undefined.
    """
    per_share = risk_per_share(entry_price, stop_price)
    if per_share <= 0 or portfolio_value <= 0 or max_risk_pct <= 0:
        return 0.0
    budget = float(portfolio_value) * float(max_risk_pct) / 100.0
    return budget / per_share


def risk_amount(quantity: float, entry_price: float, stop_price: float) -> float:
    """Dollars at risk for a proposed quantity if the stop is hit."""
    return round(float(quantity) * risk_per_share(entry_price, stop_price), 2)


# --------------------------------------------------------------------------- exits
def exit_signal(current_price: float, stop_price: float | None, target_price: float | None) -> str | None:
    """Return 'stop' or 'target' when a level has been breached, else None."""
    if current_price is None or current_price <= 0:
        return None
    if stop_price and current_price <= float(stop_price):
        return "stop"
    if target_price and current_price >= float(target_price):
        return "target"
    return None


def exit_reason(signal: str, ticker: str, current_price: float, level: float, average_cost: float) -> str:
    """Plain-language why-now for an exit proposal, in the owner's terms."""
    move_pct = (current_price / average_cost - 1.0) * 100.0 if average_cost else 0.0
    if signal == "stop":
        return (f"{ticker} traded at ${current_price:,.2f}, at or below the ${level:,.2f} stop set when the "
                f"position was opened. That is {move_pct:+.1f}% against an average cost of ${average_cost:,.2f}. "
                f"The plan was to exit here rather than widen the stop.")
    return (f"{ticker} reached ${current_price:,.2f}, at or above the ${level:,.2f} target set when the position "
            f"was opened. That is {move_pct:+.1f}% versus an average cost of ${average_cost:,.2f}. Taking the "
            f"planned profit rather than letting a winner round-trip.")
