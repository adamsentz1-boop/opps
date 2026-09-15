"""Server-rendered dashboard routes."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import approvals as approval_service
from app import work_orders as wo_service
from app.audit import log_event
from app.config import get_settings
from app.costs import cost_for_opportunity, cost_summary
from app.db import get_db
from app.enums import OpportunityStatus as S
from app.enums import WorkOrderStatus, WorkTaskStatus
from app.metrics import dashboard_metrics
from app.models import (AgentRun, AuditLog, BidDocument, ComplianceRequirement, Deliverable, Notification, Opportunity,
                        OpportunityAnalysis, SourceRun, WorkOrder, WorkTask)
from app.normalizer import upsert_opportunity
from app.pipeline import process_opportunity, reanalyze_opportunity
from app.scheduler import scan_in_background, scheduler_status
from app.settings_service import SETTING_SPECS, get_all_settings, set_setting
from app.sources.manual import normalize_manual
from app.sources.registry import describe_sources
from app.web.templating import templates

router = APIRouter()


def _ctx(request: Request, db: Session, **extra) -> dict:
    unread = db.query(func.count(Notification.id)).filter(Notification.read.is_(False)).scalar() or 0
    awaiting = db.query(func.count(Opportunity.id)).filter(Opportunity.status == S.AWAITING_APPROVAL.value).scalar() or 0
    settings = get_settings()
    base = {"request": request, "unread": unread, "awaiting_count": awaiting,
            "llm_mode": settings.claude_model if settings.llm_enabled else "MOCK",
            "flash": request.query_params.get("msg"), "error": request.query_params.get("err")}
    base.update(extra)
    return base


def _redirect(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    from urllib.parse import quote
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(url, status_code=303)


def _get_opp(db: Session, opportunity_id: str) -> Opportunity:
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(404, "Opportunity not found")
    return opp


# --------------------------------------------------------------------------- dashboard
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    m = dashboard_metrics(db)
    queue = db.query(Opportunity, OpportunityAnalysis).join(OpportunityAnalysis) \
        .filter(Opportunity.status == S.AWAITING_APPROVAL.value) \
        .order_by(OpportunityAnalysis.profit_per_human_hour.desc()).limit(5).all()
    notifications = db.query(Notification).order_by(Notification.created_at.desc()).limit(5).all()
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request, db, m=m, queue=queue,
                                                             notifications=notifications,
                                                             sched=scheduler_status()))


# --------------------------------------------------------------------------- approval queue
@router.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request, db: Session = Depends(get_db)):
    rows = db.query(Opportunity, OpportunityAnalysis).join(OpportunityAnalysis) \
        .filter(Opportunity.status == S.AWAITING_APPROVAL.value) \
        .order_by(OpportunityAnalysis.profit_per_human_hour.desc()).all()
    ready = db.query(Opportunity).filter(Opportunity.status == S.READY_TO_SUBMIT.value) \
        .order_by(Opportunity.updated_at.desc()).all()
    return templates.TemplateResponse(request, "approvals.html", _ctx(request, db, rows=rows, ready=ready))


@router.post("/opportunities/{opportunity_id}/approve")
def approve(opportunity_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    opp = _get_opp(db, opportunity_id)
    try:
        approval_service.approve_opportunity(db, opp, notes)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        return _redirect(f"/opportunities/{opportunity_id}", err=str(exc))
    return _redirect(f"/opportunities/{opportunity_id}", msg="Approved. Status is READY_TO_SUBMIT - nothing has been sent.")


@router.post("/opportunities/{opportunity_id}/reject")
def reject(opportunity_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    opp = _get_opp(db, opportunity_id)
    try:
        approval_service.reject_opportunity(db, opp, notes)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        return _redirect(f"/opportunities/{opportunity_id}", err=str(exc))
    return _redirect("/approvals", msg="Declined.")


@router.post("/opportunities/{opportunity_id}/edit")
def edit(opportunity_id: str, body: str = Form(...), price: float = Form(...), notes: str = Form(""),
         db: Session = Depends(get_db)):
    opp = _get_opp(db, opportunity_id)
    try:
        approval_service.edit_proposal(db, opp, body, price, notes)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        return _redirect(f"/opportunities/{opportunity_id}", err=str(exc))
    return _redirect(f"/opportunities/{opportunity_id}", msg="Proposal saved as a new version.")


@router.post("/opportunities/{opportunity_id}/reanalyze")
def reanalyze(opportunity_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    opp = _get_opp(db, opportunity_id)
    approval = approval_service.request_reanalysis(db, opp, notes)
    db.commit()
    result = reanalyze_opportunity(db, opportunity_id, approval.id)
    db.commit()
    return _redirect(f"/opportunities/{opportunity_id}", msg=f"Reanalysis finished: {result}.")


@router.post("/opportunities/{opportunity_id}/outcome")
def outcome(opportunity_id: str, outcome: str = Form(...), notes: str = Form(""),
            won_value: Optional[float] = Form(None), db: Session = Depends(get_db)):
    opp = _get_opp(db, opportunity_id)
    try:
        approval_service.record_outcome(db, opp, outcome, notes, won_value)
        if outcome == "won":
            wo_service.create_work_order(db, opp)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        return _redirect(f"/opportunities/{opportunity_id}", err=str(exc))
    return _redirect(f"/opportunities/{opportunity_id}", msg=f"Recorded outcome: {outcome}.")


@router.post("/opportunities/{opportunity_id}/process")
def process_now(opportunity_id: str, db: Session = Depends(get_db)):
    result = process_opportunity(db, opportunity_id)
    db.commit()
    return _redirect(f"/opportunities/{opportunity_id}", msg=f"Pipeline result: {result}.")


@router.post("/opportunities/{opportunity_id}/requirements")
def add_requirement(opportunity_id: str, requirement: str = Form(...), type: str = Form("other"),
                    mandatory: bool = Form(True), db: Session = Depends(get_db)):
    _get_opp(db, opportunity_id)
    req = ComplianceRequirement(opportunity_id=opportunity_id, requirement=requirement, type=type, mandatory=mandatory,
                                source_agent="Owner")
    db.add(req)
    db.flush()
    log_event(db, agent="Owner", action="requirement.added", object_type="compliance_requirement", object_id=req.id,
              details={"requirement": requirement, "type": type})
    db.commit()
    return _redirect(f"/opportunities/{opportunity_id}", msg="Requirement added (unverified).")


@router.post("/requirements/{requirement_id}/verify")
def verify_requirement(requirement_id: str, evidence: str = Form(...), notes: str = Form(""),
                       db: Session = Depends(get_db)):
    req = db.get(ComplianceRequirement, requirement_id)
    if req is None:
        raise HTTPException(404)
    try:
        approval_service.verify_requirement(db, req, evidence, notes)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        return _redirect(f"/opportunities/{req.opportunity_id}", err=str(exc))
    return _redirect(f"/opportunities/{req.opportunity_id}", msg="Requirement verified.")


# --------------------------------------------------------------------------- opportunities
@router.get("/opportunities", response_class=HTMLResponse)
def opportunities(request: Request, status: str = "", q: str = "", kind: str = "", db: Session = Depends(get_db)):
    query = db.query(Opportunity).outerjoin(OpportunityAnalysis)
    if kind in ("bid", "freelance"):
        query = query.filter(Opportunity.opportunity_type == kind)
    if status == "rejected":
        query = query.filter(Opportunity.status.in_([S.REJECTED.value, S.DECLINED.value]))
    elif status == "active":
        query = query.filter(Opportunity.status.in_([S.AWAITING_APPROVAL.value, S.READY_TO_SUBMIT.value,
                                                     S.SUBMITTED.value, S.INTERVIEWING.value, S.QUALIFIED.value]))
    elif status:
        query = query.filter(Opportunity.status == status.upper())
    else:
        query = query.filter(Opportunity.status.notin_([S.REJECTED.value, S.DECLINED.value]))
    if q:
        like = f"%{q}%"
        query = query.filter((Opportunity.title.ilike(like)) | (Opportunity.description.ilike(like)))
    rows = query.order_by(Opportunity.updated_at.desc()).limit(300).all()
    counts = dict(db.query(Opportunity.status, func.count(Opportunity.id)).group_by(Opportunity.status).all())
    return templates.TemplateResponse(request, "opportunities.html", _ctx(request, db, rows=rows, status=status, q=q, kind=kind,
                                                                 counts=counts, statuses=[s.value for s in S]))


@router.get("/opportunities/{opportunity_id}", response_class=HTMLResponse)
def opportunity_detail(request: Request, opportunity_id: str, db: Session = Depends(get_db)):
    opp = _get_opp(db, opportunity_id)
    audit = db.query(AuditLog).filter(AuditLog.object_id == opportunity_id).order_by(AuditLog.timestamp.desc()).limit(50).all()
    runs = db.query(AgentRun).filter(AgentRun.opportunity_id == opportunity_id).order_by(AgentRun.created_at.desc()).all()
    return templates.TemplateResponse(request, "opportunity_detail.html",
                                      _ctx(request, db, opp=opp, audit=audit, runs=runs,
                                           ai_cost=cost_for_opportunity(db, opportunity_id)))


# --------------------------------------------------------------------------- bid documents (Phase 2)
@router.post("/bid-documents/{doc_id}/approve")
def bid_document_approve(doc_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    doc = db.get(BidDocument, doc_id)
    if doc is None:
        raise HTTPException(404)
    try:
        approval_service.approve_bid_document(db, doc, notes)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        return _redirect(f"/opportunities/{doc.opportunity_id}#bid", err=str(exc))
    return _redirect(f"/opportunities/{doc.opportunity_id}#bid", msg=f"Approved document: {doc.name}.")


@router.post("/bid-documents/{doc_id}/edit")
def bid_document_edit(doc_id: str, content: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db)):
    doc = db.get(BidDocument, doc_id)
    if doc is None:
        raise HTTPException(404)
    approval_service.edit_bid_document(db, doc, content, notes)
    db.commit()
    return _redirect(f"/opportunities/{doc.opportunity_id}#bid", msg=f"Saved {doc.name} v{doc.version} (needs approval).")


@router.get("/bid-documents/{doc_id}.md", response_class=PlainTextResponse)
def bid_document_markdown(doc_id: str, db: Session = Depends(get_db)):
    doc = db.get(BidDocument, doc_id)
    if doc is None:
        raise HTTPException(404)
    return doc.content + f"\n\n<!-- {doc.name} v{doc.version} · {doc.status} · not submitted by the system -->"


@router.post("/opportunities/{opportunity_id}/bid-package/redraft")
def bid_package_redraft(opportunity_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    from app.agents import BidAgent
    from app.pipeline.runner import build_bid_package
    opp = _get_opp(db, opportunity_id)
    if not opp.is_bid:
        return _redirect(f"/opportunities/{opportunity_id}", err="Not a bid-type opportunity.")
    try:
        n = build_bid_package(db, opp, BidAgent(), notes)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return _redirect(f"/opportunities/{opportunity_id}#bid", err=f"Redraft failed: {exc}")
    return _redirect(f"/opportunities/{opportunity_id}#bid", msg=f"Redrafted {n} document(s); approved ones were kept.")


@router.get("/opportunities/{opportunity_id}/proposal.md", response_class=PlainTextResponse)
def proposal_markdown(opportunity_id: str, db: Session = Depends(get_db)):
    """Export the current proposal as Markdown so the owner can paste it wherever they submit."""
    opp = _get_opp(db, opportunity_id)
    p = opp.current_proposal
    if p is None:
        raise HTTPException(404, "No proposal")
    lines = [f"# {p.title}", "", p.body.strip(), ""]
    if p.milestones:
        lines += ["## Milestones", *[f"- {m}" for m in p.milestones], ""]
    if p.questions_for_buyer:
        lines += ["## Questions", *[f"- {q}" for q in p.questions_for_buyer], ""]
    lines += [f"**Price:** ${p.price:,.0f} ({p.pricing_model})" + (f" · **Timeline:** {p.timeline_days:g} days" if p.timeline_days else ""),
              "", f"<!-- proposal v{p.version} · opportunity {opp.id} · status {opp.status} · not submitted by the system -->"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- sources / manual intake
@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, db: Session = Depends(get_db)):
    runs = db.query(SourceRun).order_by(SourceRun.started_at.desc()).limit(30).all()
    return templates.TemplateResponse(request, "sources.html", _ctx(request, db, sources=describe_sources(), runs=runs,
                                                           sched=scheduler_status()))


@router.post("/sources/scan")
def trigger_scan():
    if not scan_in_background():
        return _redirect("/sources", err="A scan is already running.")
    return _redirect("/sources", msg="Scan started in the background. Refresh to see results.")


@router.post("/sources/manual")
def add_manual(title: str = Form(...), description: str = Form(""), buyer_name: str = Form(""),
               budget_min: str = Form(""), budget_max: str = Form(""), budget_type: str = Form("fixed"),
               source_url: str = Form(""), location: str = Form(""), required_skills: str = Form(""),
               deadline: str = Form(""), run_now: bool = Form(True), opportunity_type: str = Form("freelance"),
               agency: str = Form(""), solicitation_number: str = Form(""), set_aside: str = Form(""),
               naics_code: str = Form(""), estimated_value: str = Form(""), db: Session = Depends(get_db)):
    payload = {"title": title, "description": description, "buyer_name": buyer_name or None,
               "budget_min": budget_min or None, "budget_max": budget_max or None, "budget_type": budget_type,
               "source_url": source_url or None, "location": location or None, "required_skills": required_skills,
               "deadline": deadline or None, "opportunity_type": opportunity_type, "agency": agency or None,
               "solicitation_number": solicitation_number or None, "set_aside": set_aside or None,
               "naics_code": naics_code or None, "estimated_value": estimated_value or None,
               "buyer_type": "government" if opportunity_type == "bid" else "unknown"}
    item = normalize_manual(payload)
    opp, created = upsert_opportunity(db, item)
    db.commit()
    if not created:
        return _redirect(f"/opportunities/{opp.id}", err="Duplicate of an existing opportunity.")
    if run_now:
        result = process_opportunity(db, opp.id)
        db.commit()
        return _redirect(f"/opportunities/{opp.id}", msg=f"Added and processed: {result}.")
    return _redirect(f"/opportunities/{opp.id}", msg="Added. It will be processed on the next scan.")


# --------------------------------------------------------------------------- work orders
@router.get("/work-orders", response_class=HTMLResponse)
def work_orders_page(request: Request, db: Session = Depends(get_db)):
    rows = db.query(WorkOrder).order_by(WorkOrder.updated_at.desc()).all()
    return templates.TemplateResponse(request, "work_orders.html", _ctx(request, db, rows=rows))


@router.get("/work-orders/{work_order_id}", response_class=HTMLResponse)
def work_order_detail(request: Request, work_order_id: str, db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, work_order_id)
    if wo is None:
        raise HTTPException(404)
    audit = db.query(AuditLog).filter(AuditLog.object_id == work_order_id).order_by(AuditLog.timestamp.desc()).limit(30).all()
    next_statuses = sorted(wo_service._ALLOWED.get(wo.status, set()))
    contents = {d.id: wo_service.read_deliverable(d) for d in wo.deliverables}
    return templates.TemplateResponse(request, "work_order_detail.html",
                                      _ctx(request, db, wo=wo, audit=audit, next_statuses=next_statuses, contents=contents,
                                           task_statuses=[s.value for s in WorkTaskStatus],
                                           all_statuses=[s.value for s in WorkOrderStatus]))


@router.post("/work-orders/{work_order_id}/transition")
def work_order_transition(work_order_id: str, status: str = Form(...), notes: str = Form(""),
                          db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, work_order_id)
    if wo is None:
        raise HTTPException(404)
    try:
        wo_service.transition(db, wo, status, notes)
        db.commit()
    except wo_service.WorkOrderError as exc:
        db.rollback()
        return _redirect(f"/work-orders/{work_order_id}", err=str(exc))
    return _redirect(f"/work-orders/{work_order_id}", msg=f"Work order moved to {status}.")


@router.post("/work-orders/{work_order_id}/approve-delivery")
def work_order_approve_delivery(work_order_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, work_order_id)
    if wo is None:
        raise HTTPException(404)
    try:
        approval_service.approve_delivery(db, wo, notes)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        return _redirect(f"/work-orders/{work_order_id}", err=str(exc))
    return _redirect(f"/work-orders/{work_order_id}", msg="Delivery approved. You still deliver it yourself.")


@router.post("/work-orders/{work_order_id}/replan")
def work_order_replan(work_order_id: str, db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, work_order_id)
    if wo is None:
        raise HTTPException(404)
    wo_service.plan_work_order(db, wo)
    db.commit()
    return _redirect(f"/work-orders/{work_order_id}", msg="Plan regenerated.")


@router.post("/work-orders/{work_order_id}/hours")
def work_order_hours(work_order_id: str, hours: float = Form(...), db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, work_order_id)
    if wo is None:
        raise HTTPException(404)
    wo.human_hours_logged = (wo.human_hours_logged or 0) + hours
    log_event(db, agent="Owner", action="work_order.hours_logged", object_type="work_order", object_id=wo.id,
              details={"hours": hours, "total": wo.human_hours_logged})
    db.commit()
    return _redirect(f"/work-orders/{work_order_id}", msg=f"Logged {hours:.1f}h.")


@router.post("/work-tasks/{task_id}/execute")
def work_task_execute(task_id: str, inputs: str = Form(""), db: Session = Depends(get_db)):
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404)
    try:
        d = wo_service.execute_task(db, task, inputs)
        db.commit()
    except wo_service.WorkOrderError as exc:
        db.rollback()
        return _redirect(f"/work-orders/{task.work_order_id}", err=str(exc))
    return _redirect(f"/work-orders/{task.work_order_id}", msg=f"Drafted '{d.name}' v{d.version}. Run QA, then review.")


@router.post("/deliverables/{deliverable_id}/qa")
def deliverable_qa(deliverable_id: str, db: Session = Depends(get_db)):
    d = db.get(Deliverable, deliverable_id)
    if d is None:
        raise HTTPException(404)
    try:
        result = wo_service.run_qa(db, d)
        db.commit()
    except wo_service.WorkOrderError as exc:
        db.rollback()
        return _redirect(f"/work-orders/{d.work_order_id}", err=str(exc))
    return _redirect(f"/work-orders/{d.work_order_id}", msg=f"QA {'passed' if result.passed else 'found issues'} for {d.name}.")


@router.post("/deliverables/{deliverable_id}/approve")
def deliverable_approve(deliverable_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    d = db.get(Deliverable, deliverable_id)
    if d is None:
        raise HTTPException(404)
    try:
        approval_service.approve_deliverable(db, d, notes)
        db.commit()
    except approval_service.ApprovalError as exc:
        db.rollback()
        return _redirect(f"/work-orders/{d.work_order_id}", err=str(exc))
    return _redirect(f"/work-orders/{d.work_order_id}", msg=f"Approved {d.name}. You deliver it yourself.")


@router.get("/deliverables/{deliverable_id}/content", response_class=PlainTextResponse)
def deliverable_content(deliverable_id: str, db: Session = Depends(get_db)):
    d = db.get(Deliverable, deliverable_id)
    if d is None:
        raise HTTPException(404)
    return wo_service.read_deliverable(d) or "(no content yet)"


@router.get("/work-orders/{work_order_id}/invoice.md", response_class=PlainTextResponse)
def work_order_invoice(work_order_id: str, db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, work_order_id)
    if wo is None or not wo.invoice_path:
        raise HTTPException(404, "No invoice draft yet (generated when the work order is INVOICED)")
    from pathlib import Path
    return Path(wo.invoice_path).read_text(encoding="utf-8")


@router.post("/work-tasks/{task_id}/status")
def work_task_status(task_id: str, status: str = Form(...), db: Session = Depends(get_db)):
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404)
    wo_service.set_task_status(db, task, status)
    db.commit()
    return _redirect(f"/work-orders/{task.work_order_id}")


@router.post("/work-orders/{work_order_id}/qa/{index}")
def work_order_qa_toggle(work_order_id: str, index: int, done: bool = Form(False), db: Session = Depends(get_db)):
    wo = db.get(WorkOrder, work_order_id)
    if wo is None:
        raise HTTPException(404)
    wo_service.toggle_qa_item(db, wo, index, done)
    db.commit()
    return _redirect(f"/work-orders/{work_order_id}")


# --------------------------------------------------------------------------- audit / runs / notifications / settings
@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, agent: str = "", action: str = "", db: Session = Depends(get_db)):
    query = db.query(AuditLog)
    if agent:
        query = query.filter(AuditLog.agent == agent)
    if action:
        query = query.filter(AuditLog.action.like(f"{action}%"))
    rows = query.order_by(AuditLog.timestamp.desc()).limit(300).all()
    agents = [a for (a,) in db.query(AuditLog.agent).distinct().all()]
    return templates.TemplateResponse(request, "audit.html", _ctx(request, db, rows=rows, agents=agents, agent=agent, action=action))


@router.get("/agent-runs", response_class=HTMLResponse)
def agent_runs_page(request: Request, db: Session = Depends(get_db)):
    rows = db.query(AgentRun).order_by(AgentRun.created_at.desc()).limit(200).all()
    return templates.TemplateResponse(request, "agent_runs.html", _ctx(request, db, rows=rows, costs=cost_summary(db)))


@router.get("/agent-runs/{run_id}", response_class=HTMLResponse)
def agent_run_detail(request: Request, run_id: str, db: Session = Depends(get_db)):
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "agent_run_detail.html", _ctx(request, db, run=run))


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db)):
    rows = db.query(Notification).order_by(Notification.created_at.desc()).limit(100).all()
    return templates.TemplateResponse(request, "notifications.html", _ctx(request, db, rows=rows))


@router.post("/notifications/read")
def notifications_read(db: Session = Depends(get_db)):
    db.query(Notification).filter(Notification.read.is_(False)).update({"read": True})
    db.commit()
    return _redirect("/notifications", msg="All marked read.")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    values = get_all_settings(db)
    return templates.TemplateResponse(request, "settings.html", _ctx(request, db, specs=SETTING_SPECS, values=values,
                                                            env=_safe_env()))


@router.post("/settings")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    changed = []
    for spec in SETTING_SPECS:
        if spec.key in form:
            new_value = form[spec.key]
            try:
                old = get_all_settings(db).get(spec.key)
                if str(old) != str(new_value):
                    set_setting(db, spec.key, new_value)
                    changed.append(spec.key)
                    log_event(db, agent="Owner", action="setting.changed", object_type="setting", object_id=spec.key,
                              previous_state=str(old)[:60], new_state=str(new_value)[:60])
            except ValueError:
                db.rollback()
                return _redirect("/settings", err=f"Invalid value for {spec.key}")
    db.commit()
    return _redirect("/settings", msg=f"Saved {len(changed)} change(s).")


def _safe_env() -> dict:
    """Environment summary for the settings page - never includes secrets."""
    s = get_settings()
    return {"claude_model": s.claude_model, "claude_effort": s.claude_effort, "llm_enabled": s.llm_enabled,
            "api_key_configured": bool(s.anthropic_api_key), "scheduler_enabled": s.scheduler_enabled,
            "scan_interval_minutes": s.scan_interval_minutes, "rss_feeds": len(s.rss_feeds),
            "json_feeds": len(s.json_feeds), "notify_adapters": s.notification_adapters}
