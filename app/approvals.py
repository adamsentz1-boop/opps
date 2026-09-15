"""Approval system - first-class component.

Every consequential decision is recorded as an immutable Approval row and an audit event.
Agents cannot call these functions; only the owner-facing routes do. Nothing here submits anything.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit import log_event
from app.enums import ApprovalAction, ApprovalDecision, OpportunityStatus
from app.models import Approval, ComplianceRequirement, Opportunity, Proposal, WorkOrder, utcnow


class ApprovalError(ValueError):
    pass


def _record(db: Session, *, action: ApprovalAction, decision: ApprovalDecision, object_type: str, object_id: str,
            previous_state: str | None, new_state: str | None, notes: str, snapshot: dict,
            opportunity_id: str | None = None, work_order_id: str | None = None, decided_by: str = "owner") -> Approval:
    approval = Approval(opportunity_id=opportunity_id, work_order_id=work_order_id, object_type=object_type,
                        object_id=object_id, action=action.value, decision=decision.value, decided_by=decided_by,
                        notes=notes or "", previous_state=previous_state, new_state=new_state, snapshot=snapshot)
    db.add(approval)
    db.flush()
    log_event(db, agent="Owner", action=f"approval.{action.value.lower()}", object_type=object_type,
              object_id=object_id, previous_state=previous_state, new_state=new_state,
              human_approval_required=True, approval_id=approval.id, details={"notes": notes})
    return approval


def _proposal_snapshot(opp: Opportunity) -> dict:
    p = opp.current_proposal
    a = opp.analysis
    return {
        "title": opp.title, "source": opp.source, "source_url": opp.source_url,
        "proposal_id": p.id if p else None, "proposal_version": p.version if p else None,
        "proposal_body": p.body if p else None, "price": p.price if p else None,
        "pricing_model": p.pricing_model if p else None,
        "opportunity_score": a.opportunity_score if a else None,
        "expected_profit": a.expected_profit if a else None,
        "estimated_human_hours": a.estimated_human_hours if a else None,
    }


# --------------------------------------------------------------------------- opportunity decisions
def approve_opportunity(db: Session, opp: Opportunity, notes: str = "") -> Approval:
    if opp.status != OpportunityStatus.AWAITING_APPROVAL.value:
        raise ApprovalError(f"Cannot approve an opportunity in status {opp.status}")
    if not opp.current_proposal:
        raise ApprovalError("No proposal to approve")
    unverified = [r for r in opp.requirements if r.mandatory and not r.verified]
    if unverified:
        raise ApprovalError("Mandatory requirements are unverified: " + "; ".join(r.requirement for r in unverified))
    previous = opp.status
    opp.status = OpportunityStatus.READY_TO_SUBMIT.value
    approval = _record(db, action=ApprovalAction.APPROVE, decision=ApprovalDecision.APPROVED,
                       object_type="opportunity", object_id=opp.id, previous_state=previous, new_state=opp.status,
                       notes=notes, snapshot=_proposal_snapshot(opp), opportunity_id=opp.id)
    return approval


def reject_opportunity(db: Session, opp: Opportunity, notes: str = "") -> Approval:
    if opp.status in (OpportunityStatus.WON.value, OpportunityStatus.LOST.value):
        raise ApprovalError(f"Cannot reject an opportunity in status {opp.status}")
    previous = opp.status
    opp.status = OpportunityStatus.DECLINED.value
    opp.rejection_stage = "owner"
    opp.rejection_reasons = [f"Owner declined: {notes}" if notes else "Owner declined"]
    return _record(db, action=ApprovalAction.REJECT, decision=ApprovalDecision.REJECTED, object_type="opportunity",
                   object_id=opp.id, previous_state=previous, new_state=opp.status, notes=notes,
                   snapshot=_proposal_snapshot(opp), opportunity_id=opp.id)


def edit_proposal(db: Session, opp: Opportunity, body: str, price: float, notes: str = "") -> Approval:
    """Owner edits create a new proposal version (history preserved)."""
    if opp.status not in (OpportunityStatus.AWAITING_APPROVAL.value, OpportunityStatus.READY_TO_SUBMIT.value):
        raise ApprovalError(f"Cannot edit a proposal in status {opp.status}")
    current = opp.current_proposal
    if current is None:
        raise ApprovalError("No proposal to edit")
    new = Proposal(opportunity_id=opp.id, version=current.version + 1, title=current.title, body=body, price=price,
                   pricing_model=current.pricing_model, timeline_days=current.timeline_days,
                   milestones=current.milestones, questions_for_buyer=current.questions_for_buyer,
                   capability_claims=current.capability_claims, edited_by_owner=True, author="Owner")
    db.add(new)
    db.flush()
    opp.proposals.append(new)
    if opp.analysis and price != current.price:
        # keep economics honest after an owner price change
        a = opp.analysis
        delta = price - a.recommended_price
        a.recommended_price = price
        a.expected_profit = round(a.expected_profit + delta, 2)
        a.expected_value = round(a.estimated_probability_of_win * a.expected_profit, 2)
        a.profit_per_human_hour = round(a.expected_profit / max(a.estimated_human_hours, 0.25), 2)
    previous = opp.status
    if opp.status == OpportunityStatus.READY_TO_SUBMIT.value:
        # an edit after approval requires re-approval
        opp.status = OpportunityStatus.AWAITING_APPROVAL.value
    return _record(db, action=ApprovalAction.EDIT, decision=ApprovalDecision.EDITED, object_type="proposal",
                   object_id=new.id, previous_state=previous, new_state=opp.status, notes=notes,
                   snapshot={"proposal_version": new.version, "price": price, "body": body}, opportunity_id=opp.id)


def request_reanalysis(db: Session, opp: Opportunity, notes: str = "") -> Approval:
    return _record(db, action=ApprovalAction.REANALYZE, decision=ApprovalDecision.REANALYZE,
                   object_type="opportunity", object_id=opp.id, previous_state=opp.status, new_state="NEW",
                   notes=notes, snapshot={"previous_score": opp.analysis.opportunity_score if opp.analysis else None},
                   opportunity_id=opp.id)


_MANUAL_TRANSITIONS: dict[str, tuple[set[str], OpportunityStatus, ApprovalAction]] = {
    "submitted": ({OpportunityStatus.READY_TO_SUBMIT.value}, OpportunityStatus.SUBMITTED, ApprovalAction.MARK_SUBMITTED),
    "interviewing": ({OpportunityStatus.SUBMITTED.value}, OpportunityStatus.INTERVIEWING, ApprovalAction.MARK_INTERVIEWING),
    "won": ({OpportunityStatus.SUBMITTED.value, OpportunityStatus.INTERVIEWING.value}, OpportunityStatus.WON,
            ApprovalAction.MARK_WON),
    "lost": ({OpportunityStatus.SUBMITTED.value, OpportunityStatus.INTERVIEWING.value}, OpportunityStatus.LOST,
             ApprovalAction.MARK_LOST),
}


def record_outcome(db: Session, opp: Opportunity, outcome: str, notes: str = "", won_value: float | None = None) -> Approval:
    """Owner records what happened outside the system (they submitted, got an interview, won, lost)."""
    if outcome not in _MANUAL_TRANSITIONS:
        raise ApprovalError(f"Unknown outcome {outcome}")
    allowed_from, new_status, action = _MANUAL_TRANSITIONS[outcome]
    if opp.status not in allowed_from:
        raise ApprovalError(f"Cannot mark {outcome} from status {opp.status}")
    previous = opp.status
    opp.status = new_status.value
    if outcome == "submitted":
        opp.submitted_at = utcnow()
    if outcome == "won":
        opp.won_at = utcnow()
        opp.won_value = won_value if won_value is not None else (opp.current_proposal.price if opp.current_proposal else None)
    return _record(db, action=action, decision=ApprovalDecision.RECORDED, object_type="opportunity",
                   object_id=opp.id, previous_state=previous, new_state=opp.status, notes=notes,
                   snapshot={"won_value": opp.won_value}, opportunity_id=opp.id)


# --------------------------------------------------------------------------- compliance requirements
def verify_requirement(db: Session, req: ComplianceRequirement, evidence: str, notes: str = "") -> Approval:
    """The ONLY path that sets verified=True. Agents never call this."""
    if not evidence.strip():
        raise ApprovalError("Evidence is required to verify a requirement")
    req.verified = True
    req.evidence = evidence
    req.verified_at = utcnow()
    req.verified_by = "owner"
    approval = _record(db, action=ApprovalAction.VERIFY_REQUIREMENT, decision=ApprovalDecision.APPROVED,
                       object_type="compliance_requirement", object_id=req.id, previous_state="unverified",
                       new_state="verified", notes=notes, snapshot={"requirement": req.requirement, "evidence": evidence},
                       opportunity_id=req.opportunity_id)
    req.verification_approval_id = approval.id
    return approval


# --------------------------------------------------------------------------- work orders
def approve_delivery(db: Session, work_order: WorkOrder, notes: str = "") -> Approval:
    from app.enums import WorkOrderStatus
    if work_order.status != WorkOrderStatus.AWAITING_OWNER_APPROVAL.value:
        raise ApprovalError(f"Work order must be AWAITING_OWNER_APPROVAL (is {work_order.status})")
    previous = work_order.status
    work_order.status = WorkOrderStatus.READY_FOR_DELIVERY.value
    approval = _record(db, action=ApprovalAction.APPROVE_DELIVERY, decision=ApprovalDecision.APPROVED,
                       object_type="work_order", object_id=work_order.id, previous_state=previous,
                       new_state=work_order.status, notes=notes,
                       snapshot={"deliverables": [d.name for d in work_order.deliverables]},
                       opportunity_id=work_order.opportunity_id, work_order_id=work_order.id)
    work_order.delivery_approval_id = approval.id
    for d in work_order.deliverables:
        if d.status in ("QA_PASSED", "DRAFT"):
            d.status = "APPROVED"
            d.approval_id = approval.id
    return approval
