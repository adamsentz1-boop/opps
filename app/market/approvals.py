"""Owner decisions on trade proposals, recorded through the EXISTING immutable Approval model.

object_type = "market_trade", object_id = TradeProposal.id. Approving NEVER executes anything: the owner trades
manually with their broker and then records the fill (app/market/portfolio.record_fill).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.approvals import ApprovalError, _record
from app.audit import log_event
from app.enums import ApprovalAction, ApprovalDecision, TradeProposalStatus as TS, TradeSide
from app.market.portfolio import check_trade
from app.models import Approval, TradeProposal, utcnow


def _snapshot(p: TradeProposal) -> dict:
    return {"ticker": p.ticker, "side": p.side, "quantity": p.quantity, "estimated_price": p.estimated_price,
            "estimated_total": p.estimated_total, "thesis": p.thesis, "confidence": p.confidence,
            "risk_reward_ratio": p.risk_reward_ratio, "expected_upside_pct": p.expected_upside_pct,
            "expected_downside_pct": p.expected_downside_pct, "portfolio_before": p.portfolio_before,
            "portfolio_after": p.portfolio_after, "market_snapshot": p.market_snapshot}


def approve_trade(db: Session, proposal: TradeProposal, notes: str = "") -> Approval:
    """APPROVED means "the owner may execute this manually". It does not buy or sell anything."""
    if proposal.status != TS.AWAITING_APPROVAL.value:
        raise ApprovalError(f"Cannot approve a trade proposal in status {proposal.status}")
    if proposal.expires_at and proposal.expires_at < utcnow():
        raise ApprovalError("This proposal has expired; run a new scan or reanalyze it")
    problems = check_trade(db, proposal.challenge, proposal.side, proposal.ticker, proposal.quantity,
                           proposal.estimated_price)
    if problems:
        raise ApprovalError("Proposal no longer fits the account rules: " + "; ".join(problems))
    previous = proposal.status
    proposal.status = TS.APPROVED.value
    proposal.decided_at = utcnow()
    approval = _record(db, action=ApprovalAction.APPROVE, decision=ApprovalDecision.APPROVED,
                       object_type="market_trade", object_id=proposal.id, previous_state=previous,
                       new_state=proposal.status, notes=notes, snapshot=_snapshot(proposal))
    proposal.approval_id = approval.id
    log_event(db, agent="Owner", action="market.trade.approved", object_type="trade_proposal", object_id=proposal.id,
              previous_state=previous, new_state=proposal.status, human_approval_required=True,
              approval_id=approval.id, details={"ticker": proposal.ticker, "side": proposal.side,
                                                 "quantity": proposal.quantity, "note": "not executed by the system"})
    return approval


def reject_trade(db: Session, proposal: TradeProposal, notes: str = "") -> Approval:
    if proposal.status not in (TS.AWAITING_APPROVAL.value, TS.PROPOSED.value, TS.APPROVED.value):
        raise ApprovalError(f"Cannot reject a trade proposal in status {proposal.status}")
    previous = proposal.status
    proposal.status = TS.REJECTED.value
    proposal.decided_at = utcnow()
    approval = _record(db, action=ApprovalAction.REJECT, decision=ApprovalDecision.REJECTED,
                       object_type="market_trade", object_id=proposal.id, previous_state=previous,
                       new_state=proposal.status, notes=notes, snapshot=_snapshot(proposal))
    proposal.approval_id = approval.id
    log_event(db, agent="Owner", action="market.trade.rejected", object_type="trade_proposal", object_id=proposal.id,
              previous_state=previous, new_state=proposal.status, human_approval_required=True,
              approval_id=approval.id, details={"ticker": proposal.ticker, "side": proposal.side, "notes": notes})
    return approval


def edit_trade(db: Session, proposal: TradeProposal, *, quantity: float, estimated_price: float | None = None,
               thesis: str | None = None, notes: str = "") -> Approval:
    """Owner edits quantity/price/thesis. An edit of an APPROVED proposal sends it back for re-approval."""
    if proposal.status not in (TS.AWAITING_APPROVAL.value, TS.APPROVED.value):
        raise ApprovalError(f"Cannot edit a trade proposal in status {proposal.status}")
    quantity = round(float(quantity), 6)
    price = round(float(estimated_price), 4) if estimated_price else proposal.estimated_price
    problems = check_trade(db, proposal.challenge, proposal.side, proposal.ticker, quantity, price)
    if problems:
        raise ApprovalError("; ".join(problems))
    before = _snapshot(proposal)
    proposal.quantity = quantity
    proposal.estimated_price = price
    proposal.estimated_total = round(quantity * price, 2)
    if thesis is not None and thesis.strip():
        proposal.thesis = thesis.strip()
    proposal.edited_by_owner = True
    previous = proposal.status
    if proposal.status == TS.APPROVED.value:
        proposal.status = TS.AWAITING_APPROVAL.value
        proposal.approval_id = None
    approval = _record(db, action=ApprovalAction.EDIT, decision=ApprovalDecision.EDITED, object_type="market_trade",
                       object_id=proposal.id, previous_state=previous, new_state=proposal.status, notes=notes,
                       snapshot={"before": before, "after": _snapshot(proposal)})
    log_event(db, agent="Owner", action="market.trade.edited", object_type="trade_proposal", object_id=proposal.id,
              previous_state=previous, new_state=proposal.status, human_approval_required=True,
              approval_id=approval.id, details={"quantity": quantity, "estimated_price": price})
    return approval


def cancel_trade(db: Session, proposal: TradeProposal, notes: str = "") -> Approval:
    """Owner withdraws an approved-but-not-filled proposal (e.g. they decided not to trade)."""
    if proposal.status not in TS.active():
        raise ApprovalError(f"Cannot cancel a trade proposal in status {proposal.status}")
    previous = proposal.status
    proposal.status = TS.CANCELLED.value
    proposal.decided_at = utcnow()
    approval = _record(db, action=ApprovalAction.CANCEL, decision=ApprovalDecision.RECORDED, object_type="market_trade",
                       object_id=proposal.id, previous_state=previous, new_state=proposal.status, notes=notes,
                       snapshot=_snapshot(proposal))
    log_event(db, agent="Owner", action="market.trade.cancelled", object_type="trade_proposal", object_id=proposal.id,
              previous_state=previous, new_state=proposal.status, approval_id=approval.id, details={"notes": notes})
    return approval


def request_reanalysis(db: Session, proposal: TradeProposal, notes: str = "") -> Approval:
    return _record(db, action=ApprovalAction.REANALYZE, decision=ApprovalDecision.REANALYZE, object_type="market_trade",
                   object_id=proposal.id, previous_state=proposal.status, new_state=proposal.status, notes=notes,
                   snapshot={"ticker": proposal.ticker, "side": proposal.side, "confidence": proposal.confidence})


__all__ = ["approve_trade", "reject_trade", "edit_trade", "cancel_trade", "request_reanalysis", "ApprovalError",
           "TradeSide"]
