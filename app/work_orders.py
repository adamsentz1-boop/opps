"""Work execution planning (WorkOrder lifecycle). Designed now, used mainly in Phase 2.

Guard rails: DELIVERED requires a delivery approval; nothing here sends anything to a client.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit import log_event
from app.enums import DeliverableStatus, WorkOrderStatus, WorkTaskStatus
from app.llm import LLMError, get_llm_client
from app.models import Deliverable, Opportunity, WorkOrder, WorkTask, utcnow

_ALLOWED: dict[str, set[str]] = {
    WorkOrderStatus.NEW.value: {WorkOrderStatus.PLANNING.value},
    WorkOrderStatus.PLANNING.value: {WorkOrderStatus.WAITING_FOR_INPUT.value, WorkOrderStatus.IN_PROGRESS.value},
    WorkOrderStatus.WAITING_FOR_INPUT.value: {WorkOrderStatus.IN_PROGRESS.value, WorkOrderStatus.PLANNING.value},
    WorkOrderStatus.IN_PROGRESS.value: {WorkOrderStatus.QA.value, WorkOrderStatus.WAITING_FOR_INPUT.value},
    WorkOrderStatus.QA.value: {WorkOrderStatus.AWAITING_OWNER_APPROVAL.value, WorkOrderStatus.IN_PROGRESS.value},
    WorkOrderStatus.AWAITING_OWNER_APPROVAL.value: {WorkOrderStatus.IN_PROGRESS.value},  # READY_FOR_DELIVERY only via approval
    WorkOrderStatus.READY_FOR_DELIVERY.value: {WorkOrderStatus.DELIVERED.value},
    WorkOrderStatus.DELIVERED.value: {WorkOrderStatus.INVOICED.value},
    WorkOrderStatus.INVOICED.value: {WorkOrderStatus.PAID.value},
    WorkOrderStatus.PAID.value: set(),
}


class WorkOrderError(ValueError):
    pass


def create_work_order(db: Session, opp: Opportunity, plan_with_agent: bool = True) -> WorkOrder:
    value = opp.won_value or (opp.current_proposal.price if opp.current_proposal else 0.0)
    wo = WorkOrder(opportunity_id=opp.id, title=opp.title[:255], contract_value=value or 0.0)
    db.add(wo)
    db.flush()
    log_event(db, agent="System", action="work_order.created", object_type="work_order", object_id=wo.id,
              new_state=wo.status, details={"opportunity_id": opp.id, "value": value})
    if plan_with_agent:
        plan_work_order(db, wo)
    return wo


def plan_work_order(db: Session, wo: WorkOrder) -> WorkOrder:
    from app.agents import WorkAgent
    opp = db.get(Opportunity, wo.opportunity_id) if wo.opportunity_id else None
    previous = wo.status
    wo.status = WorkOrderStatus.PLANNING.value
    try:
        plan = WorkAgent(get_llm_client()).plan(db, wo, opp)
    except LLMError as exc:
        wo.execution_notes = f"Planning failed: {exc}"
        db.flush()
        return wo
    wo.project_plan = plan.project_plan
    wo.required_inputs = plan.required_inputs
    wo.qa_checklist = [{"item": item, "done": False} for item in plan.qa_checklist]
    wo.approval_checkpoints = [{"name": c, "status": "pending", "approval_id": None} for c in plan.approval_checkpoints]
    wo.tasks.clear()
    for i, t in enumerate(plan.tasks):
        wo.tasks.append(WorkTask(order=i, title=t.title, description=t.description, owner=t.owner,
                                 depends_on=t.depends_on, estimated_hours=t.estimated_hours,
                                 requires_owner_approval=t.requires_owner_approval))
    existing = {d.name for d in wo.deliverables}
    for name in plan.deliverables:
        if name not in existing:
            wo.deliverables.append(Deliverable(name=name))
    db.flush()
    log_event(db, agent="WorkAgent", action="work_order.planned", object_type="work_order", object_id=wo.id,
              previous_state=previous, new_state=wo.status, details={"tasks": len(plan.tasks)})
    return wo


def transition(db: Session, wo: WorkOrder, new_status: str, notes: str = "") -> WorkOrder:
    if new_status not in _ALLOWED.get(wo.status, set()):
        raise WorkOrderError(f"Cannot move work order from {wo.status} to {new_status}")
    if new_status == WorkOrderStatus.DELIVERED.value and not wo.delivery_approval_id:
        raise WorkOrderError("Delivery requires owner approval")
    previous = wo.status
    wo.status = new_status
    if new_status == WorkOrderStatus.DELIVERED.value:
        wo.delivered_at = utcnow()
        for d in wo.deliverables:
            if d.status == DeliverableStatus.APPROVED.value:
                d.status = DeliverableStatus.DELIVERED.value
    if new_status == WorkOrderStatus.INVOICED.value:
        wo.invoiced_at = utcnow()
        wo.invoiced_amount = wo.invoiced_amount or wo.contract_value
    if new_status == WorkOrderStatus.PAID.value:
        wo.paid_at = utcnow()
        wo.paid_amount = wo.paid_amount or wo.invoiced_amount or wo.contract_value
    if notes:
        wo.execution_notes = (wo.execution_notes + "\n" if wo.execution_notes else "") + f"[{utcnow():%Y-%m-%d %H:%M}] {notes}"
    db.flush()
    log_event(db, agent="Owner", action="work_order.transition", object_type="work_order", object_id=wo.id,
              previous_state=previous, new_state=new_status, human_approval_required=True,
              approval_id=wo.delivery_approval_id if new_status == WorkOrderStatus.DELIVERED.value else None,
              details={"notes": notes})
    return wo


def set_task_status(db: Session, task: WorkTask, status: str) -> WorkTask:
    if status not in {s.value for s in WorkTaskStatus}:
        raise WorkOrderError(f"Unknown task status {status}")
    previous = task.status
    task.status = status
    db.flush()
    log_event(db, agent="Owner", action="work_task.status", object_type="work_task", object_id=task.id,
              previous_state=previous, new_state=status)
    return task


def toggle_qa_item(db: Session, wo: WorkOrder, index: int, done: bool) -> WorkOrder:
    items = list(wo.qa_checklist or [])
    if 0 <= index < len(items):
        items[index] = {**items[index], "done": done}
        wo.qa_checklist = items
        db.flush()
    return wo
