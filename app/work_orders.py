"""Work execution planning (WorkOrder lifecycle). Designed now, used mainly in Phase 2.

Guard rails: DELIVERED requires a delivery approval; nothing here sends anything to a client.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

import re
from pathlib import Path

from app.audit import log_event
from app.config import WORKSPACE_DIR
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
    deliverable_names = list(plan.deliverables)
    unlinked = list(deliverable_names)
    for i, t in enumerate(plan.tasks):
        # link each agent task to a deliverable: by shared word first, else the next unlinked deliverable
        output = None
        if t.owner != "human":
            output = next((d for d in unlinked if _shares_word(t.title, d)), None)
            if output is None and unlinked and not any(w in t.title.lower() for w in ("test", "review", "qa", "verify")):
                output = unlinked[0]
            if output in unlinked:
                unlinked.remove(output)
        wo.tasks.append(WorkTask(order=i, title=t.title, description=t.description, owner=t.owner,
                                 depends_on=t.depends_on, estimated_hours=t.estimated_hours,
                                 requires_owner_approval=t.requires_owner_approval, output_deliverable=output))
    existing = {d.name for d in wo.deliverables}
    for name in deliverable_names:
        if name not in existing:
            wo.deliverables.append(Deliverable(name=name))
    wo.workspace_path = str(workspace_for(wo))
    db.flush()
    log_event(db, agent="WorkAgent", action="work_order.planned", object_type="work_order", object_id=wo.id,
              previous_state=previous, new_state=wo.status, details={"tasks": len(plan.tasks)})
    return wo


_STOP = {"the", "a", "an", "and", "of", "for", "to", "with", "draft", "build", "create", "write", "prepare"}


def _shares_word(a: str, b: str) -> bool:
    wa = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if len(w) > 3 and w not in _STOP}
    wb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 3 and w not in _STOP}
    return bool(wa & wb)


def workspace_for(wo: WorkOrder) -> Path:
    path = WORKSPACE_DIR / wo.id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(name: str, fallback: str = "deliverable.md") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._")
    if not name or name.startswith(".") or ".." in name:
        return fallback
    return name[:120]


def execute_task(db: Session, task: WorkTask, inputs: str = "") -> Deliverable:
    """Owner-triggered: the WorkAgent drafts the task's deliverable into the workspace. Draft only."""
    from app.agents import WorkAgent
    wo = task.work_order
    if task.owner == "human":
        raise WorkOrderError("This task is assigned to you, not to an agent")
    if wo.status not in (WorkOrderStatus.PLANNING.value, WorkOrderStatus.IN_PROGRESS.value,
                         WorkOrderStatus.WAITING_FOR_INPUT.value, WorkOrderStatus.QA.value):
        raise WorkOrderError(f"Cannot execute tasks while the work order is {wo.status}")
    unmet = [dep for dep in (task.depends_on or [])
             if any(t.title == dep and t.status != WorkTaskStatus.DONE.value for t in wo.tasks)]
    if unmet:
        raise WorkOrderError("Blocked by unfinished dependencies: " + ", ".join(unmet))
    opp = db.get(Opportunity, wo.opportunity_id) if wo.opportunity_id else None
    deliverable = next((d for d in wo.deliverables if d.name == task.output_deliverable), None)
    if deliverable is None:
        deliverable = Deliverable(name=task.output_deliverable or f"Output of: {task.title}"[:255], task_id=task.id)
        wo.deliverables.append(deliverable)
        task.output_deliverable = deliverable.name
        db.flush()
    task.status = WorkTaskStatus.IN_PROGRESS.value
    db.flush()
    try:
        draft = WorkAgent(get_llm_client()).draft_deliverable(db, wo, task, deliverable, opp, inputs)
    except LLMError as exc:
        task.status = WorkTaskStatus.BLOCKED.value
        task.result_notes = f"Agent failed: {exc}"
        db.flush()
        raise WorkOrderError(f"Agent failed: {exc}") from exc
    deliverable.version += 1
    filename = _safe_filename(draft.filename)
    stem, dot, ext = filename.rpartition(".")
    versioned = f"{stem or ext}-v{deliverable.version}.{ext}" if dot else f"{filename}-v{deliverable.version}"
    path = workspace_for(wo) / versioned
    path.write_text(draft.content, encoding="utf-8")
    deliverable.file_path = str(path)
    deliverable.description = draft.summary or deliverable.description
    deliverable.status = DeliverableStatus.DRAFT.value
    deliverable.qa_passed = None
    deliverable.qa_notes = ""
    deliverable.task_id = task.id
    task.status = WorkTaskStatus.DONE.value
    task.result_notes = "\n".join([draft.summary] + [f"OWNER ACTION: {a}" for a in draft.owner_actions_required]
                                  + [f"Assumption: {a}" for a in draft.assumptions]).strip()
    if wo.status == WorkOrderStatus.PLANNING.value:
        wo.status = WorkOrderStatus.IN_PROGRESS.value
    db.flush()
    log_event(db, agent="WorkAgent", action="deliverable.drafted", object_type="deliverable", object_id=deliverable.id,
              new_state=deliverable.status, details={"work_order_id": wo.id, "task": task.title, "file": versioned,
                                                     "owner_actions": draft.owner_actions_required})
    return deliverable


def read_deliverable(deliverable: Deliverable) -> str:
    if not deliverable.file_path:
        return ""
    path = Path(deliverable.file_path)
    try:
        path.resolve().relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        return ""
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def run_qa(db: Session, deliverable: Deliverable):
    """Owner-triggered QA review of a drafted deliverable. Sets qa_passed / QA_PASSED status; never delivers."""
    from app.agents import QAAgent
    wo = deliverable.work_order
    content = read_deliverable(deliverable)
    if not content:
        raise WorkOrderError("Deliverable has no drafted content yet")
    result = QAAgent(get_llm_client()).review(db, wo, deliverable, content)
    deliverable.qa_passed = result.passed
    deliverable.qa_notes = "\n".join([result.summary] + [f"- {f}" for f in result.findings]
                                     + [f"BLOCKING: {b}" for b in result.blocking_issues]).strip()
    if result.passed and deliverable.status == DeliverableStatus.DRAFT.value:
        deliverable.status = DeliverableStatus.QA_PASSED.value
    db.flush()
    log_event(db, agent="QAAgent", action="deliverable.qa", object_type="deliverable", object_id=deliverable.id,
              new_state=deliverable.status, details={"passed": result.passed, "blocking": result.blocking_issues})
    return result


def write_invoice_draft(db: Session, wo: WorkOrder) -> Path:
    """Data-driven draft invoice (Markdown) in the workspace. The owner sends it; the system never does."""
    opp = db.get(Opportunity, wo.opportunity_id) if wo.opportunity_id else None
    amount = wo.invoiced_amount or wo.contract_value or 0.0
    lines = ["# INVOICE (DRAFT - review before sending)", "", f"Date: {utcnow():%Y-%m-%d}",
             f"Work order: {wo.id}", f"Client: {(opp.buyer_name if opp else '') or '[OWNER: client name]'}",
             f"Project: {wo.title}", "", "| Description | Amount |", "|---|---|",
             f"| {wo.title} - delivered {wo.delivered_at:%Y-%m-%d} | ${amount:,.2f} |" if wo.delivered_at
             else f"| {wo.title} | ${amount:,.2f} |", f"| **Total due** | **${amount:,.2f}** |", "",
             "Deliverables:", *[f"- {d.name} ({d.status})" for d in wo.deliverables], "",
             "Payment terms: [OWNER: terms]", "Remit to: [OWNER: payment details]"]
    path = workspace_for(wo) / "invoice-draft.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    wo.invoice_path = str(path)
    db.flush()
    return path


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
    if new_status == WorkOrderStatus.AWAITING_OWNER_APPROVAL.value:
        blocked = [d.name for d in wo.deliverables if d.qa_passed is False]
        if blocked:
            raise WorkOrderError("Deliverables with failed QA: " + ", ".join(blocked))
    if new_status == WorkOrderStatus.INVOICED.value:
        wo.invoiced_at = utcnow()
        wo.invoiced_amount = wo.invoiced_amount or wo.contract_value
        write_invoice_draft(db, wo)
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
