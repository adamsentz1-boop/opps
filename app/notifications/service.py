from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit import log_event
from app.config import get_settings
from app.models import Opportunity
from app.notifications.adapters import ADAPTERS, DashboardAdapter
from app.notifications.base import NotificationMessage


def _adapters():
    names = get_settings().notification_adapters or ["dashboard"]
    adapters = [ADAPTERS[n]() for n in names if n in ADAPTERS]
    if not any(isinstance(a, DashboardAdapter) for a in adapters):
        adapters.insert(0, DashboardAdapter())
    return adapters


def notify(db: Session, message: NotificationMessage) -> list[str]:
    delivered = []
    for adapter in _adapters():
        if adapter.send(db, message):
            delivered.append(adapter.name)
    log_event(db, agent="System", action="notification.sent", object_type="notification",
              object_id=message.opportunity_id, details={"title": message.title, "channels": delivered})
    return delivered


def notify_new_opportunity(db: Session, opp: Opportunity) -> list[str]:
    a = opp.analysis
    proposal = opp.current_proposal
    body = (f"{opp.title}\n"
            f"Budget: {opp.budget_display}\n"
            f"Recommended bid: ${(proposal.price if proposal else a.recommended_price):,.0f}\n"
            f"AI completion: {a.ai_completable_percentage}%\n"
            f"Estimated human time: {a.estimated_human_hours:.1f} hours\n"
            f"Expected profit: ${a.expected_profit:,.0f}\n"
            f"Opportunity score: {a.opportunity_score:.0f}\n"
            f"Awaiting approval.")
    return notify(db, NotificationMessage(title="NEW MONEY OPPORTUNITY", body=body, level="opportunity",
                                          opportunity_id=opp.id))


def notify_system(db: Session, title: str, body: str, opportunity_id: str | None = None, level: str = "info"):
    return notify(db, NotificationMessage(title=title, body=body, level=level, opportunity_id=opportunity_id))
