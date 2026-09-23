"""JSON API for the Market Challenge (/api/market). Same rules as the UI: research and proposals only."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.approvals import ApprovalError
from app.audit import log_event
from app.config import get_settings
from app.db import get_db
from app.enums import TradeProposalStatus as TS
from app.market import approvals as trade_approvals
from app.market import portfolio as ledger
from app.market.data import provider_name
from app.market.universe import is_valid_ticker, normalise_ticker
from app.models import AgentRun, MarketWatchlist, TradeExecution, TradeProposal
from app.scheduler import market_scan_job, market_scheduler_status

router = APIRouter(prefix="/api/market", tags=["market-api"])


def _enabled() -> None:
    if not get_settings().market_challenge_enabled:
        raise HTTPException(404, "Market Challenge module is disabled")


class DecisionIn(BaseModel):
    notes: str = ""


class EditIn(BaseModel):
    quantity: float
    estimated_price: float | None = None
    thesis: str | None = None
    notes: str = ""


class FillIn(BaseModel):
    quantity: float
    fill_price: float
    fees: float = 0.0
    executed_at: datetime | None = None
    notes: str = ""


class WatchIn(BaseModel):
    ticker: str
    notes: str = ""


def _proposal_json(p: TradeProposal) -> dict[str, Any]:
    return {"id": p.id, "ticker": p.ticker, "side": p.side, "quantity": p.quantity, "estimated_price": p.estimated_price,
            "estimated_total": p.estimated_total, "status": p.status, "thesis": p.thesis,
            "reason_for_trade": p.reason_for_trade, "bull_case": p.bull_case, "bear_case": p.bear_case,
            "catalysts": p.catalysts, "risks": p.risks, "time_horizon": p.time_horizon, "confidence": p.confidence,
            "expected_upside_pct": p.expected_upside_pct, "expected_downside_pct": p.expected_downside_pct,
            "risk_reward_ratio": p.risk_reward_ratio,
            "stop_price": p.stop_price, "target_price": p.target_price, "risk_amount": p.risk_amount,
            "exit_plan": p.exit_plan or {}, "price_stats": p.price_stats or {},
            "portfolio_before": p.portfolio_before,
            "portfolio_after": p.portfolio_after, "market_snapshot": p.market_snapshot, "created_at": p.created_at,
            "expires_at": p.expires_at, "approval_id": p.approval_id, "edited_by_owner": p.edited_by_owner}


def _execution_json(e: TradeExecution) -> dict[str, Any]:
    return {"id": e.id, "trade_proposal_id": e.trade_proposal_id, "ticker": e.ticker, "side": e.side,
            "quantity": e.quantity, "fill_price": e.fill_price, "fees": e.fees, "total_value": e.total_value,
            "realized_pnl": e.realized_pnl, "executed_at": e.executed_at, "entered_by": e.entered_by}


@router.get("/summary", dependencies=[Depends(_enabled)])
def summary(db: Session = Depends(get_db)):
    challenge = ledger.get_or_create_challenge(db)
    ledger.recalculate(db, challenge)
    db.commit()
    state = ledger.portfolio_state(db, challenge)
    state["provider"] = provider_name()
    state["scheduler"] = market_scheduler_status()
    state["awaiting_approval"] = db.query(TradeProposal).filter(TradeProposal.challenge_id == challenge.id,
                                                                TradeProposal.status == TS.AWAITING_APPROVAL.value).count()
    state["brokerage_connection"] = None   # by design: the system never executes trades
    positions = ledger.open_positions(challenge)
    state["open_risk"] = round(sum(((p.current_price or p.average_cost) - p.stop_price) * p.quantity
                                   for p in positions
                                   if p.stop_price and (p.current_price or p.average_cost) > p.stop_price), 2)
    state["positions_without_stop"] = [p.ticker for p in positions if not p.stop_price]
    for row, position in zip(state["positions"], positions):
        row["stop_price"], row["target_price"] = position.stop_price, position.target_price
    return state


@router.get("/proposals", dependencies=[Depends(_enabled)])
def list_proposals(status: Optional[str] = None, limit: int = 100, db: Session = Depends(get_db)):
    q = db.query(TradeProposal)
    if status:
        q = q.filter(TradeProposal.status == status.upper())
    return [_proposal_json(p) for p in q.order_by(TradeProposal.created_at.desc()).limit(min(limit, 500)).all()]


@router.get("/proposals/{proposal_id}", dependencies=[Depends(_enabled)])
def get_proposal(proposal_id: str, db: Session = Depends(get_db)):
    p = db.get(TradeProposal, proposal_id)
    if p is None:
        raise HTTPException(404)
    return _proposal_json(p)


def _decide(db: Session, proposal_id: str, fn, **kwargs):
    p = db.get(TradeProposal, proposal_id)
    if p is None:
        raise HTTPException(404)
    try:
        approval = fn(db, p, **kwargs)
        db.commit()
    except (ApprovalError, ledger.LedgerError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return {"approval_id": approval.id, "action": approval.action, "decision": approval.decision,
            "status": p.status, "executed": False, "proposal": _proposal_json(p)}


@router.post("/proposals/{proposal_id}/approve", dependencies=[Depends(_enabled)])
def api_approve(proposal_id: str, body: DecisionIn = DecisionIn(), db: Session = Depends(get_db)):
    out = _decide(db, proposal_id, trade_approvals.approve_trade, notes=body.notes)
    out["message"] = "Approved — execute this trade manually with your broker, then record the fill."
    return out


@router.post("/proposals/{proposal_id}/reject", dependencies=[Depends(_enabled)])
def api_reject(proposal_id: str, body: DecisionIn = DecisionIn(), db: Session = Depends(get_db)):
    return _decide(db, proposal_id, trade_approvals.reject_trade, notes=body.notes)


@router.post("/proposals/{proposal_id}/edit", dependencies=[Depends(_enabled)])
def api_edit(proposal_id: str, body: EditIn, db: Session = Depends(get_db)):
    return _decide(db, proposal_id, trade_approvals.edit_trade, quantity=body.quantity,
                   estimated_price=body.estimated_price, thesis=body.thesis, notes=body.notes)


@router.post("/proposals/{proposal_id}/cancel", dependencies=[Depends(_enabled)])
def api_cancel(proposal_id: str, body: DecisionIn = DecisionIn(), db: Session = Depends(get_db)):
    return _decide(db, proposal_id, trade_approvals.cancel_trade, notes=body.notes)


@router.post("/proposals/{proposal_id}/fill", dependencies=[Depends(_enabled)])
def api_fill(proposal_id: str, body: FillIn, db: Session = Depends(get_db)):
    p = db.get(TradeProposal, proposal_id)
    if p is None:
        raise HTTPException(404)
    try:
        execution = ledger.record_fill(db, p, quantity=body.quantity, fill_price=body.fill_price, fees=body.fees,
                                       executed_at=body.executed_at, notes=body.notes)
        db.commit()
    except ledger.LedgerError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return {"execution": _execution_json(execution), "portfolio": ledger.portfolio_state(db, p.challenge)}


@router.get("/executions", dependencies=[Depends(_enabled)])
def list_executions(db: Session = Depends(get_db)):
    return [_execution_json(e) for e in db.query(TradeExecution).order_by(TradeExecution.executed_at.desc()).all()]


@router.post("/scan", dependencies=[Depends(_enabled)])
def api_scan(background: BackgroundTasks, wait: bool = True):
    if wait:
        result = market_scan_job(trigger="api")
        if result is None:
            raise HTTPException(409, "market scan already running")
        return result
    background.add_task(market_scan_job, "api")
    return {"queued": True}


@router.get("/watchlist", dependencies=[Depends(_enabled)])
def watchlist(db: Session = Depends(get_db)):
    return [{"ticker": w.ticker, "enabled": w.enabled, "notes": w.notes, "created_at": w.created_at}
            for w in db.query(MarketWatchlist).order_by(MarketWatchlist.ticker).all()]


@router.post("/watchlist", status_code=201, dependencies=[Depends(_enabled)])
def watchlist_add(body: WatchIn, db: Session = Depends(get_db)):
    symbol = normalise_ticker(body.ticker)
    if not is_valid_ticker(symbol):
        raise HTTPException(422, f"{body.ticker!r} is not a plain stock/ETF symbol")
    row = db.get(MarketWatchlist, symbol)
    if row is None:
        row = MarketWatchlist(ticker=symbol, enabled=True, notes=body.notes[:2000])
        db.add(row)
    else:
        row.enabled = True
    log_event(db, agent="Owner", action="market.watchlist.added", object_type="market_watchlist", object_id=symbol)
    db.commit()
    return {"ticker": row.ticker, "enabled": row.enabled}


@router.delete("/watchlist/{ticker}", dependencies=[Depends(_enabled)])
def watchlist_remove(ticker: str, db: Session = Depends(get_db)):
    row = db.get(MarketWatchlist, normalise_ticker(ticker))
    if row is None:
        raise HTTPException(404)
    db.delete(row)
    log_event(db, agent="Owner", action="market.watchlist.removed", object_type="market_watchlist", object_id=row.ticker)
    db.commit()
    return {"removed": row.ticker}


@router.get("/agent-runs", dependencies=[Depends(_enabled)])
def agent_runs(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.query(AgentRun).filter(AgentRun.agent.in_(("MarketResearchAgent", "PortfolioAgent"))) \
        .order_by(AgentRun.created_at.desc()).limit(min(limit, 500)).all()
    return [{"id": r.id, "agent": r.agent, "model": r.model, "status": r.status, "input_tokens": r.input_tokens,
             "output_tokens": r.output_tokens, "cost_usd": r.cost_usd, "created_at": r.created_at} for r in rows]
