"""Did any of this actually work? Measured from the ledger, not from the agent's own opinion of itself.

Without this the only quality signal in the whole module is the number the model puts in `confidence`, which
is worth nothing until it has been checked against outcomes. Three questions get answered here:

1. **How have closed trades done?** Win rate, profit factor and expectancy, both in dollars and in R, where
   1R is the amount that was at risk when the position was opened. R matters more than win rate: losing 6
   times out of 10 is fine if the winners are worth twice the losers.
2. **Does the agent's confidence predict anything?** Round trips are bucketed by the confidence stated on the
   entry proposal. If the high-confidence bucket is not better than the low one, confidence is noise and
   should be ignored when deciding.
3. **What happened to the ideas that were not taken?** Every proposal records the price at the time, so the
   rejected and expired ones can be priced today. If rejections keep going up, the owner is the bottleneck;
   if they keep going down, the owner is adding value.

Everything is derived from `trade_executions`, `trade_proposals` and the stored price snapshots, so there is
nothing to keep in sync and no new table to migrate.

**Accounting note.** The ledger realises P&L on a weighted-average-cost basis and that number is
authoritative. Lots are matched first-in-first-out here purely to attribute each sale back to the buys it
closed, which is what gives a holding period and a link to the entry proposal. Each round trip therefore
carries a slice of the sale's own recorded `realized_pnl`, so these figures always add up to the ledger.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.enums import TradeProposalStatus as TS
from app.enums import TradeSide
from app.market.data import latest_snapshot
from app.models import TradeExecution, TradeProposal, TradingChallenge, utcnow

#: Confidence bands used for the calibration table.
CONFIDENCE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("under 60", 0, 59), ("60-74", 60, 74), ("75-89", 75, 89), ("90+", 90, 100),
)
#: Below this many closed trades the statistics are noise, and the UI says so rather than implying a finding.
MIN_TRADES_FOR_SIGNAL = 5


@dataclass
class _Lot:
    quantity: float
    cost_per_share: float
    executed_at: datetime
    proposal_id: str


@dataclass
class RoundTrip:
    """One closed slice of a position: some quantity bought, later sold."""
    ticker: str
    quantity: float
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    realized_pnl: float
    entry_proposal_id: str
    exit_proposal_id: str
    confidence: int | None = None
    risk_amount: float | None = None      # 1R: what was at stake when the trade was opened
    stop_price: float | None = None
    exit_reason: str = "discretionary"    # stop | target | discretionary

    @property
    def holding_days(self) -> float:
        return round((self.closed_at - self.opened_at).total_seconds() / 86400.0, 2)

    @property
    def won(self) -> bool:
        return self.realized_pnl > 0

    @property
    def return_pct(self) -> float:
        basis = self.quantity * self.entry_price
        return round(self.realized_pnl / basis * 100.0, 2) if basis else 0.0

    @property
    def r_multiple(self) -> float | None:
        """P&L as a multiple of what was risked. None when the entry carried no stop."""
        if not self.risk_amount:
            return None
        return round(self.realized_pnl / self.risk_amount, 2)

    def as_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "quantity": self.quantity, "entry_price": self.entry_price,
                "exit_price": self.exit_price, "opened_at": self.opened_at, "closed_at": self.closed_at,
                "realized_pnl": self.realized_pnl, "return_pct": self.return_pct,
                "holding_days": self.holding_days, "won": self.won, "r_multiple": self.r_multiple,
                "confidence": self.confidence, "risk_amount": self.risk_amount,
                "exit_reason": self.exit_reason, "entry_proposal_id": self.entry_proposal_id}


def _proposals_by_id(db: Session, challenge: TradingChallenge) -> dict[str, TradeProposal]:
    rows = db.query(TradeProposal).filter(TradeProposal.challenge_id == challenge.id).all()
    return {p.id: p for p in rows}


def round_trips(db: Session, challenge: TradingChallenge) -> list[RoundTrip]:
    """Reconstruct closed trades by matching sales against earlier buys, first in first out."""
    executions = (db.query(TradeExecution)
                  .filter(TradeExecution.challenge_id == challenge.id)
                  .order_by(TradeExecution.executed_at, TradeExecution.recorded_at).all())
    proposals = _proposals_by_id(db, challenge)
    open_lots: dict[str, deque[_Lot]] = {}
    trips: list[RoundTrip] = []

    for execution in executions:
        lots = open_lots.setdefault(execution.ticker, deque())
        if execution.side == TradeSide.BUY.value:
            if execution.quantity <= 0:
                continue
            # fees are part of what the shares cost, exactly as the ledger treats them
            cost_per_share = (execution.quantity * execution.fill_price + execution.fees) / execution.quantity
            lots.append(_Lot(quantity=execution.quantity, cost_per_share=cost_per_share,
                             executed_at=execution.executed_at, proposal_id=execution.trade_proposal_id))
            continue

        remaining = execution.quantity
        if remaining <= 0:
            continue
        # the sale's own recorded P&L is shared across the lots it closes, so totals match the ledger
        total_sold = execution.quantity
        while remaining > 1e-9 and lots:
            lot = lots[0]
            matched = min(remaining, lot.quantity)
            share = matched / total_sold if total_sold else 0.0
            entry_proposal = proposals.get(lot.proposal_id)
            exit_proposal = proposals.get(execution.trade_proposal_id)
            risk = None
            if entry_proposal and entry_proposal.stop_price and entry_proposal.estimated_price:
                per_share = entry_proposal.estimated_price - entry_proposal.stop_price
                risk = round(per_share * matched, 4) if per_share > 0 else None
            trips.append(RoundTrip(
                ticker=execution.ticker, quantity=round(matched, 6),
                entry_price=round(lot.cost_per_share, 6), exit_price=execution.fill_price,
                opened_at=lot.executed_at, closed_at=execution.executed_at,
                realized_pnl=round(execution.realized_pnl * share, 4),
                entry_proposal_id=lot.proposal_id, exit_proposal_id=execution.trade_proposal_id,
                confidence=entry_proposal.confidence if entry_proposal else None,
                risk_amount=risk, stop_price=entry_proposal.stop_price if entry_proposal else None,
                exit_reason=(exit_proposal.exit_plan or {}).get("signal", "discretionary")
                if exit_proposal else "discretionary"))
            lot.quantity = round(lot.quantity - matched, 9)
            remaining = round(remaining - matched, 9)
            if lot.quantity <= 1e-9:
                lots.popleft()
        # a sale with no matching buy means the ledger and this reconstruction disagree; ignore the remainder
        # rather than inventing a lot, since the ledger's own realised P&L stays authoritative either way
    return trips


def performance_summary(db: Session, challenge: TradingChallenge) -> dict[str, Any]:
    """Headline quality metrics over closed trades. Honest about small samples."""
    trips = round_trips(db, challenge)
    wins = [t for t in trips if t.won]
    losses = [t for t in trips if not t.won and t.realized_pnl < 0]
    gross_win = sum(t.realized_pnl for t in wins)
    gross_loss = abs(sum(t.realized_pnl for t in losses))
    rs = [t.r_multiple for t in trips if t.r_multiple is not None]
    summary: dict[str, Any] = {
        "trades": len(trips),
        "enough_data": len(trips) >= MIN_TRADES_FOR_SIGNAL,
        "min_trades_for_signal": MIN_TRADES_FOR_SIGNAL,
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trips) * 100.0, 1) if trips else None,
        "realized_pnl": round(sum(t.realized_pnl for t in trips), 2),
        "gross_win": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "expectancy": round(sum(t.realized_pnl for t in trips) / len(trips), 2) if trips else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "trades_with_r": len(rs),
        "avg_holding_days": round(sum(t.holding_days for t in trips) / len(trips), 1) if trips else None,
        "best": max(trips, key=lambda t: t.realized_pnl).as_dict() if trips else None,
        "worst": min(trips, key=lambda t: t.realized_pnl).as_dict() if trips else None,
        "ledger_realized_pnl": round(challenge.realized_pnl, 2),
    }
    by_reason: dict[str, dict[str, Any]] = {}
    for trip in trips:
        bucket = by_reason.setdefault(trip.exit_reason, {"trades": 0, "realized_pnl": 0.0})
        bucket["trades"] += 1
        bucket["realized_pnl"] = round(bucket["realized_pnl"] + trip.realized_pnl, 2)
    summary["by_exit_reason"] = by_reason
    return summary


def confidence_calibration(db: Session, challenge: TradingChallenge) -> list[dict[str, Any]]:
    """Does a higher stated confidence actually produce better trades? Bucketed so it can be read at a glance."""
    trips = [t for t in round_trips(db, challenge) if t.confidence is not None]
    rows = []
    for label, low, high in CONFIDENCE_BUCKETS:
        bucket = [t for t in trips if low <= t.confidence <= high]
        rs = [t.r_multiple for t in bucket if t.r_multiple is not None]
        rows.append({
            "band": label, "trades": len(bucket),
            "win_rate_pct": round(sum(1 for t in bucket if t.won) / len(bucket) * 100.0, 1) if bucket else None,
            "realized_pnl": round(sum(t.realized_pnl for t in bucket), 2) if bucket else 0.0,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        })
    return rows


def proposal_scorecard(db: Session, challenge: TradingChallenge) -> dict[str, Any]:
    """Price every BUY proposal at today's price, including the ones that were never taken.

    This separates the agent's idea quality from the owner's decision quality. If rejected ideas keep rising,
    the owner is vetoing good trades; if they keep falling, the owner is adding value.
    """
    proposals = (db.query(TradeProposal)
                 .filter(TradeProposal.challenge_id == challenge.id,
                         TradeProposal.side == TradeSide.BUY.value).all())
    groups: dict[str, list[dict[str, Any]]] = {}
    for proposal in proposals:
        if not proposal.estimated_price:
            continue
        snapshot = latest_snapshot(db, proposal.ticker)
        if snapshot is None or not snapshot.price:
            continue
        change_pct = round((snapshot.price / proposal.estimated_price - 1.0) * 100.0, 2)
        outcome = {"id": proposal.id, "ticker": proposal.ticker, "status": proposal.status,
                   "proposed_at": proposal.created_at, "proposed_price": proposal.estimated_price,
                   "price_now": snapshot.price, "change_pct": change_pct,
                   "confidence": proposal.confidence,
                   "days_since": round((utcnow() - proposal.created_at).total_seconds() / 86400.0, 1)}
        key = {TS.EXECUTED.value: "taken", TS.REJECTED.value: "rejected",
               TS.EXPIRED.value: "expired", TS.CANCELLED.value: "cancelled"}.get(proposal.status, "open")
        groups.setdefault(key, []).append(outcome)

    def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"count": 0, "avg_change_pct": None, "up": 0, "down": 0}
        changes = [r["change_pct"] for r in rows]
        return {"count": len(rows), "avg_change_pct": round(sum(changes) / len(changes), 2),
                "up": sum(1 for c in changes if c > 0), "down": sum(1 for c in changes if c < 0)}

    # every BUY idea the agent had, regardless of what the owner did with it
    everything = [row for rows in groups.values() for row in rows]
    return {"groups": {k: sorted(v, key=lambda r: r["proposed_at"], reverse=True) for k, v in groups.items()},
            "stats": {k: _stats(v) for k, v in groups.items()},
            "all_ideas": _stats(everything),
            "note": "Prices are marked to the latest stored snapshot, so these move until you refresh quotes."}


def agent_track_record(db: Session, challenge: TradingChallenge) -> dict[str, Any] | None:
    """A compact, neutral record to show the decision agent. None until there is enough to be meaningful."""
    summary = performance_summary(db, challenge)
    if not summary["enough_data"]:
        return None
    return {"closed_trades": summary["trades"], "win_rate_pct": summary["win_rate_pct"],
            "average_r_multiple": summary["avg_r"], "profit_factor": summary["profit_factor"],
            "expectancy_per_trade": summary["expectancy"],
            "note": ("This is the realised record of proposals from this system so far. It is a small sample "
                     "and describes the past only. Do not treat a good record as licence to take more risk, "
                     "or a poor one as a reason to chase losses.")}
