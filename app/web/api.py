"""JSON API (internal REST). Same guard rails as the UI: nothing is ever submitted externally."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import __version__
from app import approvals as approval_service
from app.config import get_settings
from app.costs import cost_summary
from app.db import get_db
from app.enums import OpportunityStatus as S
from app.metrics import dashboard_metrics
from app.models import Opportunity, OpportunityAnalysis
from app.normalizer import upsert_opportunity
from app.pipeline import process_opportunity
from app.scheduler import scan_job, scheduler_status
from app.settings_service import get_all_settings, set_setting
from app.sources.manual import normalize_manual

router = APIRouter(prefix="/api", tags=["api"])


class ManualOpportunityIn(BaseModel):
    title: str
    description: str = ""
    buyer_name: str | None = None
    buyer_type: str = "unknown"
    location: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    budget_type: str = "fixed"
    currency: str = "USD"
    source_url: str | None = None
    required_skills: list[str] = []
    deliverables: list[str] = []
    deadline: str | None = None
    opportunity_type: str = "freelance"
    agency: str | None = None
    solicitation_number: str | None = None
    set_aside: str | None = None
    naics_code: str | None = None
    estimated_value: float | None = None
    process: bool = True


class DecisionIn(BaseModel):
    notes: str = ""


class EditIn(BaseModel):
    body: str
    price: float
    notes: str = ""


def _opp_json(opp: Opportunity) -> dict[str, Any]:
    a = opp.analysis
    p = opp.current_proposal
    return {
        "id": opp.id, "source": opp.source, "external_id": opp.external_id, "title": opp.title,
        "opportunity_type": opp.opportunity_type, "agency": opp.agency, "set_aside": opp.set_aside,
        "deadline": opp.deadline, "bid_documents": [{"id": d.id, "name": d.name, "status": d.status} for d in opp.bid_documents],
        "buyer_name": opp.buyer_name, "status": opp.status, "budget_min": opp.budget_min, "budget_max": opp.budget_max,
        "budget_type": opp.budget_type, "source_url": opp.source_url, "created_at": opp.created_at,
        "rejection_reasons": opp.rejection_reasons, "injection_flags": opp.injection_flags,
        "analysis": None if a is None else {
            "opportunity_score": a.opportunity_score, "technical_fit": a.technical_fit,
            "ai_completable_percentage": a.ai_completable_percentage, "human_effort": a.human_effort,
            "profitability": a.profitability, "scope_clarity": a.scope_clarity, "buyer_quality": a.buyer_quality,
            "risk": a.risk, "competition": a.competition, "confidence": a.confidence,
            "estimated_human_hours": a.estimated_human_hours, "estimated_agent_hours": a.estimated_agent_hours,
            "estimated_api_cost": a.estimated_api_cost, "estimated_other_cost": a.estimated_other_cost,
            "recommended_price": a.recommended_price, "expected_margin": a.expected_margin,
            "expected_profit": a.expected_profit, "estimated_probability_of_win": a.estimated_probability_of_win,
            "expected_value": a.expected_value, "profit_per_human_hour": a.profit_per_human_hour,
            "summary": a.summary, "red_flags": a.red_flags},
        "proposal": None if p is None else {"id": p.id, "version": p.version, "price": p.price, "body": p.body,
                                            "timeline_days": p.timeline_days},
        "solution_plan": None if opp.solution_plan is None else {
            "feasible": opp.solution_plan.feasible, "profitable": opp.solution_plan.profitable,
            "verdict": opp.solution_plan.verdict, "proposed_solution": opp.solution_plan.proposed_solution,
            "implementation_steps": opp.solution_plan.implementation_steps},
    }


@router.get("/health")
def health():
    s = get_settings()
    return {"status": "ok", "version": __version__, "llm": s.claude_model if s.llm_enabled else "mock",
            "scheduler": scheduler_status()}


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    m = dashboard_metrics(db)
    m["top"] = [{"id": o.id, "title": o.title, "score": a.opportunity_score, "expected_profit": a.expected_profit,
                 "human_hours": a.estimated_human_hours, "profit_per_human_hour": a.profit_per_human_hour}
                for o, a in m["top"]]
    m.pop("recent_errors", None)
    return m


@router.get("/costs")
def costs(db: Session = Depends(get_db)):
    return cost_summary(db)


@router.get("/opportunities")
def list_opportunities(status: Optional[str] = None, limit: int = 100, db: Session = Depends(get_db)):
    q = db.query(Opportunity).outerjoin(OpportunityAnalysis)
    if status:
        q = q.filter(Opportunity.status == status.upper())
    return [_opp_json(o) for o in q.order_by(Opportunity.updated_at.desc()).limit(min(limit, 500)).all()]


@router.get("/approvals/queue")
def approval_queue(db: Session = Depends(get_db)):
    rows = db.query(Opportunity).join(OpportunityAnalysis).filter(Opportunity.status == S.AWAITING_APPROVAL.value) \
        .order_by(OpportunityAnalysis.profit_per_human_hour.desc()).all()
    return [_opp_json(o) for o in rows]


@router.get("/opportunities/{opportunity_id}")
def get_opportunity(opportunity_id: str, db: Session = Depends(get_db)):
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(404)
    return _opp_json(opp)


@router.post("/opportunities", status_code=201)
def create_opportunity(item: ManualOpportunityIn, db: Session = Depends(get_db)):
    payload = item.model_dump(exclude={"process"})
    opp, created = upsert_opportunity(db, normalize_manual(payload))
    db.commit()
    result = None
    if created and item.process:
        result = process_opportunity(db, opp.id)
        db.commit()
    return {"created": created, "pipeline_result": result, "opportunity": _opp_json(opp)}


@router.post("/opportunities/{opportunity_id}/process")
def api_process(opportunity_id: str, db: Session = Depends(get_db)):
    if db.get(Opportunity, opportunity_id) is None:
        raise HTTPException(404)
    result = process_opportunity(db, opportunity_id)
    db.commit()
    return {"result": result, "opportunity": _opp_json(db.get(Opportunity, opportunity_id))}


def _decide(db: Session, opportunity_id: str, fn, *args):
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(404)
    try:
        approval = fn(db, opp, *args)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return {"approval_id": approval.id, "action": approval.action, "decision": approval.decision,
            "status": opp.status, "opportunity": _opp_json(opp)}


@router.post("/opportunities/{opportunity_id}/approve")
def api_approve(opportunity_id: str, body: DecisionIn = DecisionIn(), db: Session = Depends(get_db)):
    return _decide(db, opportunity_id, approval_service.approve_opportunity, body.notes)


@router.post("/opportunities/{opportunity_id}/reject")
def api_reject(opportunity_id: str, body: DecisionIn = DecisionIn(), db: Session = Depends(get_db)):
    return _decide(db, opportunity_id, approval_service.reject_opportunity, body.notes)


@router.post("/opportunities/{opportunity_id}/edit")
def api_edit(opportunity_id: str, body: EditIn, db: Session = Depends(get_db)):
    return _decide(db, opportunity_id, approval_service.edit_proposal, body.body, body.price, body.notes)


@router.post("/opportunities/{opportunity_id}/reanalyze")
def api_reanalyze(opportunity_id: str, body: DecisionIn = DecisionIn(), db: Session = Depends(get_db)):
    from app.pipeline import reanalyze_opportunity
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(404)
    approval = approval_service.request_reanalysis(db, opp, body.notes)
    db.commit()
    result = reanalyze_opportunity(db, opportunity_id, approval.id)
    db.commit()
    return {"approval_id": approval.id, "result": result, "opportunity": _opp_json(db.get(Opportunity, opportunity_id))}


@router.post("/scan")
def api_scan(background: BackgroundTasks, wait: bool = True):
    if wait:
        result = scan_job()
        if result is None:
            raise HTTPException(409, "scan already running")
        return result
    background.add_task(scan_job)
    return {"queued": True}


@router.get("/settings")
def api_settings(db: Session = Depends(get_db)):
    return get_all_settings(db)


@router.put("/settings/{key}")
def api_set_setting(key: str, value: dict, db: Session = Depends(get_db)):
    if "value" not in value:
        raise HTTPException(422, "body must be {'value': ...}")
    try:
        row = set_setting(db, key, value["value"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return {"key": row.key, "value": row.value}
