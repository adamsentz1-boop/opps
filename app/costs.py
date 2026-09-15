"""Approximate Claude API cost tracking.

Prices are USD per million tokens (Anthropic first-party rates). Update when pricing changes.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import AgentRun, utcnow

PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "mock": (0.0, 0.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = PRICING_PER_MTOK.get(model, (5.0, 25.0))
    return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 6)


def cost_summary(db: Session) -> dict:
    """Cost per day/month/agent plus totals."""
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    def _sum(since: datetime | None = None) -> float:
        q = db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0.0))
        if since:
            q = q.filter(AgentRun.created_at >= since)
        return float(q.scalar() or 0.0)

    per_agent = dict(db.query(AgentRun.agent, func.coalesce(func.sum(AgentRun.cost_usd), 0.0))
                     .group_by(AgentRun.agent).all())
    per_day = db.query(func.date(AgentRun.created_at), func.coalesce(func.sum(AgentRun.cost_usd), 0.0),
                       func.count(AgentRun.id)) \
        .filter(AgentRun.created_at >= now - timedelta(days=30)) \
        .group_by(func.date(AgentRun.created_at)).order_by(func.date(AgentRun.created_at).desc()).all()
    tokens = db.query(func.coalesce(func.sum(AgentRun.input_tokens), 0),
                      func.coalesce(func.sum(AgentRun.output_tokens), 0)).one()
    return {
        "total": _sum(),
        "today": _sum(day_start),
        "month": _sum(month_start),
        "per_agent": {k: float(v) for k, v in per_agent.items()},
        "per_day": [(str(d), float(c), int(n)) for d, c, n in per_day],
        "input_tokens": int(tokens[0]),
        "output_tokens": int(tokens[1]),
        "runs": db.query(func.count(AgentRun.id)).scalar() or 0,
    }


def cost_for_opportunity(db: Session, opportunity_id: str) -> float:
    return float(db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0.0))
                 .filter(AgentRun.opportunity_id == opportunity_id).scalar() or 0.0)


def cost_for_work_order(db: Session, work_order_id: str) -> float:
    return float(db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0.0))
                 .filter(AgentRun.work_order_id == work_order_id).scalar() or 0.0)
