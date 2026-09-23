"""Portfolio ledger for the Market Challenge.

The ONLY function that changes cash or positions is `record_fill`, which the owner calls after executing a
trade themselves with their broker. Agents never call anything in this module that mutates state; they only
read `portfolio_state`.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.audit import log_event
from app.config import get_settings
from app.enums import ApprovalAction, ApprovalDecision, ChallengeStatus, TradeProposalStatus, TradeSide
from app.market.data import latest_snapshot
from app.models import (MarketPosition, PortfolioSnapshot, TradeExecution, TradeProposal, TradingChallenge,
                        utcnow)

log = logging.getLogger(__name__)

QTY_DP = 6      # fractional share precision
MONEY_DP = 2


class LedgerError(ValueError):
    """Raised when a fill would violate the cash-account rules (never negative cash, never oversell)."""


# --------------------------------------------------------------------------- challenge
def parse_target_date(raw: str | date | datetime | None) -> datetime:
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    text = (raw or "").strip() or get_settings().market_target_date
    return datetime.strptime(text[:10], "%Y-%m-%d")


def get_or_create_challenge(db: Session) -> TradingChallenge:
    """Return the active challenge, creating the default one from configuration if none exists."""
    challenge = (db.query(TradingChallenge).filter(TradingChallenge.status == ChallengeStatus.ACTIVE.value)
                 .order_by(TradingChallenge.created_at).first())
    if challenge is None:
        challenge = db.query(TradingChallenge).order_by(TradingChallenge.created_at).first()
    if challenge is None:
        s = get_settings()
        challenge = TradingChallenge(
            name=f"${s.market_starting_capital:,.0f} to ${s.market_target_value:,.0f} Market Challenge",
            starting_cash=round(float(s.market_starting_capital), MONEY_DP),
            target_value=round(float(s.market_target_value), MONEY_DP),
            target_date=parse_target_date(s.market_target_date),
            cash_balance=round(float(s.market_starting_capital), MONEY_DP),
            current_portfolio_value=round(float(s.market_starting_capital), MONEY_DP),
            status=ChallengeStatus.ACTIVE.value,
        )
        db.add(challenge)
        db.flush()
        log_event(db, agent="System", action="market.challenge.created", object_type="trading_challenge",
                  object_id=challenge.id, new_state=challenge.status,
                  details={"starting_cash": challenge.starting_cash, "target_value": challenge.target_value,
                           "target_date": challenge.target_date.date().isoformat()})
    return challenge


def days_remaining(challenge: TradingChallenge, now: datetime | None = None) -> int:
    now = now or utcnow()
    return max(0, (challenge.target_date.date() - now.date()).days)


def goal_progress_pct(challenge: TradingChallenge, value: float | None = None) -> float:
    """Progress from starting cash to target, in percent. Can be negative (below start) or exceed 100."""
    value = challenge.current_portfolio_value if value is None else value
    span = challenge.target_value - challenge.starting_cash
    if span <= 0:
        return 100.0 if value >= challenge.target_value else 0.0
    return round((value - challenge.starting_cash) / span * 100.0, 2)


def total_return_pct(challenge: TradingChallenge) -> float:
    if not challenge.starting_cash:
        return 0.0
    return round((challenge.current_portfolio_value - challenge.starting_cash) / challenge.starting_cash * 100.0, 2)


# --------------------------------------------------------------------------- valuation
def open_positions(challenge: TradingChallenge) -> list[MarketPosition]:
    return [p for p in challenge.positions if (p.quantity or 0) > 0]


def _price_for(db: Session, ticker: str, prices: dict[str, float] | None) -> float | None:
    if prices and ticker in prices and prices[ticker]:
        return float(prices[ticker])
    snap = latest_snapshot(db, ticker)
    return float(snap.price) if snap else None


def recalculate(db: Session, challenge: TradingChallenge, prices: dict[str, float] | None = None) -> TradingChallenge:
    """Mark positions to the latest known prices and refresh portfolio totals. Does not move cash."""
    unrealized = 0.0
    positions_value = 0.0
    for pos in challenge.positions:
        if (pos.quantity or 0) <= 0:
            pos.market_value = 0.0
            pos.unrealized_pnl = 0.0
            continue
        price = _price_for(db, pos.ticker, prices)
        if price is None:
            price = pos.current_price if pos.current_price is not None else pos.average_cost
        pos.current_price = round(price, 4)
        pos.market_value = round(pos.quantity * price, MONEY_DP)
        pos.unrealized_pnl = round((price - pos.average_cost) * pos.quantity, MONEY_DP)
        positions_value += pos.market_value
        unrealized += pos.unrealized_pnl
    challenge.unrealized_pnl = round(unrealized, MONEY_DP)
    challenge.current_portfolio_value = round(challenge.cash_balance + positions_value, MONEY_DP)
    db.flush()
    return challenge


def take_snapshot(db: Session, challenge: TradingChallenge, reason: str = "manual") -> PortfolioSnapshot:
    snap = PortfolioSnapshot(challenge_id=challenge.id, cash=challenge.cash_balance,
                             positions_value=challenge.positions_value,
                             portfolio_value=challenge.current_portfolio_value,
                             realized_pnl=challenge.realized_pnl, unrealized_pnl=challenge.unrealized_pnl,
                             goal_progress_pct=goal_progress_pct(challenge), reason=reason)
    db.add(snap)
    db.flush()
    log_event(db, agent="System", action="market.portfolio.snapshot", object_type="portfolio_snapshot",
              object_id=snap.id, details={"portfolio_value": snap.portfolio_value, "cash": snap.cash,
                                          "goal_progress_pct": snap.goal_progress_pct, "reason": reason})
    return snap


def today_change(db: Session, challenge: TradingChallenge) -> float:
    """Sum of qty * (price - previous_close) over open positions, from the latest snapshots."""
    total = 0.0
    for pos in open_positions(challenge):
        snap = latest_snapshot(db, pos.ticker)
        if snap and snap.previous_close and snap.price:
            total += pos.quantity * (snap.price - snap.previous_close)
    return round(total, MONEY_DP)


def portfolio_state(db: Session, challenge: TradingChallenge) -> dict[str, Any]:
    """Read-only summary shared by the dashboard, the API and the agents."""
    positions = [{"ticker": p.ticker, "quantity": p.quantity, "average_cost": p.average_cost,
                  "current_price": p.current_price, "market_value": p.market_value,
                  "unrealized_pnl": p.unrealized_pnl,
                  "weight_pct": round(p.market_value / challenge.current_portfolio_value * 100.0, 2)
                  if challenge.current_portfolio_value else 0.0}
                 for p in open_positions(challenge)]
    return {
        "challenge_id": challenge.id, "name": challenge.name, "status": challenge.status,
        "starting_cash": challenge.starting_cash, "target_value": challenge.target_value,
        "target_date": challenge.target_date.date().isoformat(), "days_remaining": days_remaining(challenge),
        "cash": challenge.cash_balance, "positions_value": challenge.positions_value,
        "portfolio_value": challenge.current_portfolio_value, "realized_pnl": challenge.realized_pnl,
        "unrealized_pnl": challenge.unrealized_pnl, "goal_progress_pct": goal_progress_pct(challenge),
        "total_return_pct": total_return_pct(challenge), "today_change": today_change(db, challenge),
        "positions": positions,
    }


# --------------------------------------------------------------------------- risk limits (deterministic)
def limits(db: Session) -> dict[str, float]:
    from app.settings_service import get_setting
    return {"max_position_pct": float(get_setting(db, "market_max_position_pct")),
            "max_single_trade_pct": float(get_setting(db, "market_max_single_trade_pct")),
            "min_cash_reserve_pct": float(get_setting(db, "market_min_cash_reserve_pct"))}


def risk_limits(db: Session) -> dict[str, float]:
    """Owner-tunable risk knobs used by the deterministic sizing and exit-level maths."""
    from app.settings_service import get_setting
    return {"max_risk_per_trade_pct": float(get_setting(db, "market_max_risk_per_trade_pct")),
            "stop_move_multiple": float(get_setting(db, "market_stop_move_multiple")),
            "min_stop_pct": float(get_setting(db, "market_min_stop_pct")),
            "max_stop_pct": float(get_setting(db, "market_max_stop_pct")),
            "reward_risk_target": float(get_setting(db, "market_reward_risk_target"))}


def check_trade(db: Session, challenge: TradingChallenge, side: str, ticker: str, quantity: float,
                price: float, fees: float = 0.0) -> list[str]:
    """Deterministic rule check. Returns a list of violations (empty = OK). Used for proposals AND fills."""
    problems: list[str] = []
    if quantity is None or quantity <= 0:
        problems.append("quantity must be positive")
    if price is None or price <= 0:
        problems.append("price must be positive")
    if fees is None or fees < 0:
        problems.append("fees cannot be negative")
    if problems:
        return problems
    if side == TradeSide.BUY.value:
        total = quantity * price + fees
        if total > challenge.cash_balance + 1e-9:
            problems.append(f"buy total ${total:,.2f} exceeds cash ${challenge.cash_balance:,.2f} (no margin, no borrowing)")
    elif side == TradeSide.SELL.value:
        pos = next((p for p in challenge.positions if p.ticker == ticker), None)
        owned = pos.quantity if pos else 0.0
        if quantity > owned + 1e-9:
            problems.append(f"cannot sell {quantity:g} {ticker}: only {owned:g} owned (no short selling)")
        if fees > quantity * price:
            problems.append("fees exceed sale proceeds")
    else:
        problems.append(f"unknown side {side!r}")
    return problems


def check_proposal_limits(db: Session, challenge: TradingChallenge, side: str, ticker: str, quantity: float,
                          price: float) -> list[str]:
    """Portfolio-construction limits for NEW proposals (position size, single trade size, cash reserve)."""
    problems = check_trade(db, challenge, side, ticker, quantity, price, 0.0)
    if problems or side != TradeSide.BUY.value:
        return problems
    lim = limits(db)
    value = challenge.current_portfolio_value or challenge.cash_balance
    total = quantity * price
    if total > value * lim["max_single_trade_pct"] / 100.0 + 1e-6:
        problems.append(f"trade ${total:,.2f} exceeds max single trade {lim['max_single_trade_pct']:g}% of ${value:,.2f}")
    held = next((p.market_value for p in challenge.positions if p.ticker == ticker and p.quantity > 0), 0.0)
    if held + total > value * lim["max_position_pct"] / 100.0 + 1e-6:
        problems.append(f"resulting {ticker} position ${held + total:,.2f} exceeds max position {lim['max_position_pct']:g}%")
    if challenge.cash_balance - total < value * lim["min_cash_reserve_pct"] / 100.0 - 1e-6:
        problems.append(f"would breach the minimum cash reserve of {lim['min_cash_reserve_pct']:g}%")
    return problems


def max_buy_quantity(db: Session, challenge: TradingChallenge, ticker: str, price: float) -> float:
    """Largest BUY quantity that satisfies every limit (used to clamp agent suggestions)."""
    if price <= 0:
        return 0.0
    lim = limits(db)
    value = challenge.current_portfolio_value or challenge.cash_balance
    held = next((p.market_value for p in challenge.positions if p.ticker == ticker and p.quantity > 0), 0.0)
    budget = min(challenge.cash_balance,
                 value * lim["max_single_trade_pct"] / 100.0,
                 value * lim["max_position_pct"] / 100.0 - held,
                 challenge.cash_balance - value * lim["min_cash_reserve_pct"] / 100.0)
    if budget <= 0:
        return 0.0
    qty = budget / price
    # round DOWN so the total never exceeds the budget by a rounding artefact
    factor = 10 ** QTY_DP
    return int(qty * factor) / factor


# --------------------------------------------------------------------------- the one mutating path
def record_fill(db: Session, proposal: TradeProposal, *, quantity: float, fill_price: float, fees: float = 0.0,
                executed_at: datetime | None = None, notes: str = "", entered_by: str = "owner") -> TradeExecution:
    """Owner records a fill they executed with their broker. Requires an APPROVED proposal.

    Creates the immutable TradeExecution, moves cash, updates the position (weighted average cost on buys,
    realised P&L on sells), recalculates the portfolio, takes a PortfolioSnapshot and writes audit entries.
    """
    from app.approvals import _record  # reuse the immutable Approval model (same system as opportunities)

    challenge = proposal.challenge
    if proposal.status != TradeProposalStatus.APPROVED.value:
        raise LedgerError(f"Fill refused: proposal is {proposal.status}, not APPROVED. Approve it first.")
    if challenge.status != ChallengeStatus.ACTIVE.value:
        raise LedgerError(f"Challenge is {challenge.status}; fills are only accepted on an ACTIVE challenge")
    quantity = round(float(quantity), QTY_DP)
    fill_price = round(float(fill_price), 4)
    fees = round(float(fees or 0.0), MONEY_DP)
    problems = check_trade(db, challenge, proposal.side, proposal.ticker, quantity, fill_price, fees)
    if problems:
        raise LedgerError("; ".join(problems))
    executed_at = executed_at or utcnow()

    pos = next((p for p in challenge.positions if p.ticker == proposal.ticker), None)
    previous_cash = challenge.cash_balance
    previous_qty = pos.quantity if pos else 0.0
    realized = 0.0

    if proposal.side == TradeSide.BUY.value:
        total = round(quantity * fill_price + fees, MONEY_DP)
        challenge.cash_balance = round(challenge.cash_balance - total, MONEY_DP)
        if challenge.cash_balance < 0:  # belt and braces: never negative cash
            raise LedgerError("fill would make cash negative")
        if pos is None:
            pos = MarketPosition(challenge_id=challenge.id, ticker=proposal.ticker, quantity=0.0, average_cost=0.0,
                                 opened_at=executed_at)
            db.add(pos)
            challenge.positions.append(pos)
        if pos.quantity <= 0:
            pos.opened_at = executed_at
            pos.quantity, pos.average_cost = 0.0, 0.0
        if proposal.stop_price or proposal.target_price:
            # the exit plan follows the money: the position now carries the levels the owner approved
            pos.stop_price = proposal.stop_price
            pos.target_price = proposal.target_price
        new_qty = pos.quantity + quantity
        pos.average_cost = round((pos.quantity * pos.average_cost + quantity * fill_price + fees) / new_qty, 6)
        pos.quantity = round(new_qty, QTY_DP)
    else:
        assert pos is not None
        gross = quantity * fill_price
        total = round(gross - fees, MONEY_DP)
        realized = round((fill_price - pos.average_cost) * quantity - fees, MONEY_DP)
        challenge.cash_balance = round(challenge.cash_balance + total, MONEY_DP)
        challenge.realized_pnl = round(challenge.realized_pnl + realized, MONEY_DP)
        pos.quantity = round(pos.quantity - quantity, QTY_DP)
        if pos.quantity <= 10 ** -QTY_DP:
            pos.quantity = 0.0
            pos.average_cost = 0.0
            pos.stop_price = pos.target_price = None      # flat: the exit plan no longer applies
    pos.current_price = fill_price
    db.flush()

    previous_status = proposal.status
    proposal.status = TradeProposalStatus.EXECUTED.value
    approval = _record(db, action=ApprovalAction.RECORD_FILL, decision=ApprovalDecision.RECORDED,
                       object_type="market_trade", object_id=proposal.id, previous_state=previous_status,
                       new_state=proposal.status, notes=notes,
                       snapshot={"ticker": proposal.ticker, "side": proposal.side, "quantity": quantity,
                                 "fill_price": fill_price, "fees": fees, "total_value": total,
                                 "realized_pnl": realized, "executed_at": executed_at.isoformat()},
                       decided_by=entered_by)
    # The execution row is immutable once flushed, so every field (incl. the approval id) is set up front.
    execution = TradeExecution(trade_proposal_id=proposal.id, challenge_id=challenge.id, ticker=proposal.ticker,
                               side=proposal.side, quantity=quantity, fill_price=fill_price, fees=fees,
                               total_value=total, realized_pnl=realized, executed_at=executed_at,
                               entered_by=entered_by, notes=notes or "", approval_id=approval.id)
    db.add(execution)
    db.flush()

    log_event(db, agent="Owner", action="market.trade.executed", object_type="trade_execution", object_id=execution.id,
              previous_state=previous_status, new_state=proposal.status, human_approval_required=True,
              approval_id=approval.id,
              details={"proposal_id": proposal.id, "ticker": proposal.ticker, "side": proposal.side,
                       "quantity": quantity, "fill_price": fill_price, "fees": fees, "total_value": total,
                       "realized_pnl": realized, "cash_before": previous_cash, "cash_after": challenge.cash_balance})
    log_event(db, agent="Owner", action="market.position.changed", object_type="market_position", object_id=pos.id,
              previous_state=f"{previous_qty:g}", new_state=f"{pos.quantity:g}", approval_id=approval.id,
              details={"ticker": pos.ticker, "average_cost": pos.average_cost, "execution_id": execution.id})

    recalculate(db, challenge, {proposal.ticker: fill_price} if latest_snapshot(db, proposal.ticker) is None else None)
    take_snapshot(db, challenge, reason="fill")
    return execution
