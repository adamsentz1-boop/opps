"""Dashboard metrics: today, pipeline, financial, and top opportunities."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.costs import cost_summary
from app.enums import OpportunityStatus as S
from app.models import AuditLog, Opportunity, OpportunityAnalysis, WorkOrder, utcnow

ACTIVE_STATUSES = {S.AWAITING_APPROVAL.value, S.READY_TO_SUBMIT.value, S.SUBMITTED.value, S.INTERVIEWING.value}


def _count(db: Session, *statuses: str, since=None) -> int:
    q = db.query(func.count(Opportunity.id)).filter(Opportunity.status.in_(statuses))
    if since is not None:
        q = q.filter(Opportunity.updated_at >= since)
    return int(q.scalar() or 0)


def dashboard_metrics(db: Session) -> dict:
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    discovered_today = int(db.query(func.count(Opportunity.id)).filter(Opportunity.created_at >= day_start).scalar() or 0)
    rejected_today = int(db.query(func.count(AuditLog.id)).filter(AuditLog.action == "opportunity.rejected",
                                                                  AuditLog.timestamp >= day_start).scalar() or 0)
    qualified_today = int(db.query(func.count(AuditLog.id)).filter(AuditLog.action == "opportunity.qualified",
                                                                   AuditLog.timestamp >= day_start).scalar() or 0)
    total = int(db.query(func.count(Opportunity.id)).scalar() or 0)
    rejected = _count(db, S.REJECTED.value)
    declined = _count(db, S.DECLINED.value)
    qualified_ever = int(db.query(func.count(func.distinct(AuditLog.object_id)))
                         .filter(AuditLog.action == "opportunity.qualified").scalar() or 0)
    recommended_ever = int(db.query(func.count(func.distinct(AuditLog.object_id)))
                           .filter(AuditLog.action == "opportunity.recommended").scalar() or 0)
    awaiting = _count(db, S.AWAITING_APPROVAL.value)

    pipeline = {
        "discovered": total,
        "new": _count(db, S.NEW.value, S.QUALIFYING.value),
        "rejected": rejected,
        "qualified": qualified_ever,
        "awaiting_approval": awaiting,
        "approved": _count(db, S.READY_TO_SUBMIT.value, S.SUBMITTED.value, S.INTERVIEWING.value, S.WON.value, S.LOST.value),
        "ready_to_submit": _count(db, S.READY_TO_SUBMIT.value),
        "submitted": _count(db, S.SUBMITTED.value),
        "interviewing": _count(db, S.INTERVIEWING.value),
        "won": _count(db, S.WON.value),
        "lost": _count(db, S.LOST.value),
        "declined": declined,
        "errors": _count(db, S.ERROR.value),
    }

    active = db.query(Opportunity, OpportunityAnalysis).join(OpportunityAnalysis) \
        .filter(Opportunity.status.in_(ACTIVE_STATUSES)).all()
    awaiting_rows = [(o, a) for o, a in active if o.status == S.AWAITING_APPROVAL.value]
    potential_revenue = sum((o.current_proposal.price if o.current_proposal else a.recommended_price) for o, a in active)
    expected_profit = sum(a.expected_profit for o, a in active)
    expected_value = sum(a.expected_value for o, a in active)
    human_hours = sum(a.estimated_human_hours for o, a in active)
    awaiting_revenue = sum((o.current_proposal.price if o.current_proposal else a.recommended_price) for o, a in awaiting_rows)
    awaiting_profit = sum(a.expected_profit for o, a in awaiting_rows)
    awaiting_hours = sum(a.estimated_human_hours for o, a in awaiting_rows)

    won_revenue = float(db.query(func.coalesce(func.sum(Opportunity.won_value), 0.0))
                        .filter(Opportunity.status == S.WON.value).scalar() or 0.0)
    paid_revenue = float(db.query(func.coalesce(func.sum(WorkOrder.paid_amount), 0.0)).scalar() or 0.0)
    logged_hours = float(db.query(func.coalesce(func.sum(WorkOrder.human_hours_logged), 0.0)).scalar() or 0.0)
    costs = cost_summary(db)
    realised_profit = paid_revenue - costs["total"]
    effective_rate = (realised_profit / logged_hours) if logged_hours > 0 else (
        (awaiting_profit / awaiting_hours) if awaiting_hours > 0 else 0.0)

    top = db.query(Opportunity, OpportunityAnalysis).join(OpportunityAnalysis) \
        .filter(Opportunity.status.in_({S.AWAITING_APPROVAL.value, S.READY_TO_SUBMIT.value, S.QUALIFIED.value})) \
        .order_by(OpportunityAnalysis.profit_per_human_hour.desc()).limit(8).all()

    soon = now + timedelta(days=14)
    deadlines = db.query(Opportunity).filter(Opportunity.deadline.isnot(None), Opportunity.deadline >= now,
                                             Opportunity.deadline <= soon,
                                             Opportunity.status.in_({S.AWAITING_APPROVAL.value, S.READY_TO_SUBMIT.value,
                                                                     S.QUALIFIED.value, S.NEW.value})) \
        .order_by(Opportunity.deadline).limit(8).all()
    bids = int(db.query(func.count(Opportunity.id)).filter(Opportunity.opportunity_type == "bid").scalar() or 0)

    return {
        "deadlines": deadlines,
        "bids": bids,
        "today": {"discovered": discovered_today, "rejected": rejected_today, "qualified": qualified_today,
                  "awaiting": awaiting},
        "headline": {"scanned": total, "auto_rejected": rejected, "qualified": qualified_ever,
                     "recommended": recommended_ever, "awaiting": awaiting,
                     "potential_revenue": awaiting_revenue, "expected_profit": awaiting_profit,
                     "human_hours": awaiting_hours, "ai_cost": costs["total"]},
        "pipeline": pipeline,
        "financial": {"potential_revenue": potential_revenue, "expected_profit": expected_profit,
                      "expected_value": expected_value, "won_revenue": won_revenue, "paid_revenue": paid_revenue,
                      "ai_cost_total": costs["total"], "ai_cost_today": costs["today"], "ai_cost_month": costs["month"],
                      "human_hours": human_hours, "logged_hours": logged_hours, "effective_hourly_rate": effective_rate,
                      "gross_profit": realised_profit},
        "top": top,
        "costs": costs,
        "last_scan": db.query(func.max(AuditLog.timestamp)).filter(AuditLog.action == "source.scan").scalar(),
        "recent_errors": db.query(Opportunity).filter(Opportunity.status == S.ERROR.value)
                           .order_by(Opportunity.updated_at.desc()).limit(5).all(),
    }
