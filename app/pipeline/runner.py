"""Pipeline orchestrator: scan sources -> normalise -> reject/qualify -> research -> solution -> proposal -> queue."""
from __future__ import annotations

import logging
import traceback

from sqlalchemy.orm import Session

from app.audit import log_event
from app.enums import OpportunityStatus
from app.llm import LLMError, get_llm_client
from app.models import (BidDocument, Buyer, ComplianceRequirement, Opportunity, OpportunityAnalysis, Proposal,
                        SolutionPlan, SourceRun, utcnow)
from app.normalizer import upsert_opportunity
from app.notifications import notify_new_opportunity, notify_system
from app.pipeline.rejection import post_rules, pre_rules
from app.pipeline.scoring import apply_to_analysis
from app.settings_service import Thresholds, get_setting
from app.sources.base import OpportunitySource, SourceError
from app.sources.registry import get_sources

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- state helpers
def _set_status(db: Session, opp: Opportunity, new_status: OpportunityStatus, agent: str, action: str,
                details: dict | None = None) -> None:
    previous = opp.status
    opp.status = new_status.value
    db.flush()
    log_event(db, agent=agent, action=action, object_type="opportunity", object_id=opp.id,
              previous_state=previous, new_state=opp.status, details=details or {})


def reject(db: Session, opp: Opportunity, reasons: list[str], stage: str, agent: str) -> None:
    opp.rejection_reasons = list(reasons)
    opp.rejection_stage = stage
    _set_status(db, opp, OpportunityStatus.REJECTED, agent, "opportunity.rejected",
                {"stage": stage, "reasons": reasons})


# --------------------------------------------------------------------------- scanning
def run_source(db: Session, source: OpportunitySource) -> SourceRun:
    run = SourceRun(source=source.source_name)
    db.add(run)
    db.flush()
    try:
        items = source.fetch_new_opportunities()
        run.fetched = len(items)
        for item in items:
            _, created = upsert_opportunity(db, item)
            if created:
                run.inserted += 1
            else:
                run.duplicates += 1
        run.status = "ok"
        run.notes = source.status_note()
    except NotImplementedError as exc:
        run.status = "skipped"
        run.notes = str(exc)
    except SourceError as exc:
        run.status = "error"
        run.error = str(exc)[:2000]
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        log.exception("source %s failed", source.source_name)
    run.finished_at = utcnow()
    db.flush()
    log_event(db, agent="ScoutAgent", action="source.scan", object_type="source_run", object_id=run.id,
              new_state=run.status, details={"source": run.source, "fetched": run.fetched, "inserted": run.inserted,
                                             "duplicates": run.duplicates, "error": run.error})
    return run


def run_scan(db: Session, process: bool = True, sources: list[OpportunitySource] | None = None) -> dict:
    """Fetch from every registered source, then process all NEW opportunities."""
    summary = {"runs": [], "processed": 0, "recommended": 0, "rejected": 0, "errors": 0}
    for source in (sources if sources is not None else get_sources()):
        run = run_source(db, source)
        summary["runs"].append({"source": run.source, "status": run.status, "fetched": run.fetched,
                                "inserted": run.inserted, "error": run.error, "notes": run.notes})
        db.commit()
    if process:
        summary.update(process_pending(db))
    return summary


def process_pending(db: Session, limit: int | None = None) -> dict:
    from app.documents import ingest_pending
    try:
        attachments = ingest_pending(db)
    except Exception as exc:  # noqa: BLE001 - ingestion must never stop the pipeline
        log.exception("attachment ingestion failed")
        attachments = {"error": str(exc)[:300]}
    query = db.query(Opportunity).filter(Opportunity.status == OpportunityStatus.NEW.value) \
        .order_by(Opportunity.created_at)
    if limit:
        query = query.limit(limit)
    counts = {"processed": 0, "recommended": 0, "rejected": 0, "errors": 0, "attachments": attachments}
    for opp in query.all():
        result = process_opportunity(db, opp.id)
        counts["processed"] += 1
        counts[result] = counts.get(result, 0) + 1
        db.commit()
    return counts


# --------------------------------------------------------------------------- per-opportunity pipeline
def process_opportunity(db: Session, opportunity_id: str) -> str:
    """Run the full evaluation pipeline for one opportunity. Returns 'recommended' | 'rejected' | 'errors'."""
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise ValueError(f"Unknown opportunity {opportunity_id}")
    thresholds = Thresholds(db)
    llm = get_llm_client()
    _set_status(db, opp, OpportunityStatus.QUALIFYING, "System", "pipeline.start")
    # Injection patterns found inside attachments count against the opportunity as a whole.
    att_flags = sorted({f for a in opp.attachments for f in (a.injection_flags or [])})
    if att_flags:
        opp.injection_flags = sorted(set(opp.injection_flags or []) | {f"attachment:{f}" for f in att_flags})
    try:
        # 1. Rule-based rejection before spending tokens
        pre = pre_rules(opp, thresholds)
        if pre.rejected:
            reject(db, opp, pre.reasons, pre.stage, "System")
            return "rejected"

        # 2. Qualification agent + scoring
        from app.agents import (BidAgent, ProposalAgent, QualificationAgent, RequirementsAgent, ResearchAgent,
                                SolutionArchitectAgent)
        q = QualificationAgent(llm).run(db, opp)
        analysis = opp.analysis or OpportunityAnalysis(opportunity_id=opp.id)
        apply_to_analysis(analysis, q, opp.budget_type, float(thresholds.target_effective_hourly_rate), llm.mode)
        db.add(analysis)
        opp.analysis = analysis
        db.flush()
        log_event(db, agent="QualificationAgent", action="opportunity.qualified_scored", object_type="opportunity",
                  object_id=opp.id, details={"score": analysis.opportunity_score,
                                             "expected_profit": analysis.expected_profit,
                                             "human_hours": analysis.estimated_human_hours})
        post = post_rules(analysis, thresholds)
        if post.rejected:
            reject(db, opp, post.reasons, post.stage, "QualificationAgent")
            return "rejected"
        _set_status(db, opp, OpportunityStatus.QUALIFIED, "QualificationAgent", "opportunity.qualified")

        # 3. Buyer research (non-fatal)
        try:
            r = ResearchAgent(llm).run(db, opp)
            buyer = opp.buyer or Buyer(opportunity_id=opp.id)
            for field in ("company", "industry", "website", "likely_company_size", "relevant_context",
                          "project_motivation", "potential_red_flags", "personalization", "uncertain_items",
                          "confidence"):
                setattr(buyer, field, getattr(r, field))
            buyer.raw_response = r.model_dump()
            db.add(buyer)
            opp.buyer = buyer
            db.flush()
        except LLMError as exc:
            log.warning("research failed for %s: %s", opp.id, exc)

        # 4. Solution architecture - can we actually do this profitably?
        s = SolutionArchitectAgent(llm).run(db, opp)
        plan = opp.solution_plan or SolutionPlan(opportunity_id=opp.id)
        for field in ("feasible", "profitable", "verdict", "proposed_solution", "implementation_steps",
                      "required_tools", "apis_required", "external_accounts_required", "likely_blockers",
                      "assumptions", "claude_involvement", "human_involvement", "estimated_human_hours",
                      "estimated_agent_hours", "qa_strategy", "deliverables", "risk_factors"):
            setattr(plan, field, getattr(s, field))
        plan.raw_response = s.model_dump()
        db.add(plan)
        opp.solution_plan = plan
        db.flush()
        log_event(db, agent="SolutionArchitectAgent", action="opportunity.solution_planned",
                  object_type="opportunity", object_id=opp.id,
                  details={"feasible": s.feasible, "profitable": s.profitable})
        reasons = []
        if not s.feasible:
            reasons.append("Solution architect: not feasible remotely with verified capabilities")
        if not s.profitable:
            reasons.append("Solution architect: not profitable at achievable price")
        if s.estimated_human_hours > thresholds.maximum_human_hours:
            reasons.append(f"Solution architect estimates {s.estimated_human_hours:.1f} owner hours "
                           f"(> {thresholds.maximum_human_hours:.0f})")
        if reasons:
            reject(db, opp, reasons, "solution", "SolutionArchitectAgent")
            return "rejected"
        # Reconcile human hours with the architect's estimate if it is larger (be conservative).
        if s.estimated_human_hours > analysis.estimated_human_hours:
            analysis.estimated_human_hours = s.estimated_human_hours
            analysis.profit_per_human_hour = round(analysis.expected_profit / max(s.estimated_human_hours, 0.25), 2)

        # 5. Formal requirements. Rows are ALWAYS created unverified; only the owner verifies.
        ensure_structural_requirements(db, opp)
        try:
            extract_requirements(db, opp, RequirementsAgent(llm))
        except LLMError as exc:
            log.warning("requirements extraction failed for %s: %s", opp.id, exc)

        # 5b. Bids: assemble the bid package (each document needs owner sign-off before approval)
        if opp.is_bid:
            build_bid_package(db, opp, BidAgent(llm))

        # 6. Proposal draft (for bids this is the executive summary / cover narrative)
        p = ProposalAgent(llm).run(db, opp)
        version = (opp.proposals[-1].version + 1) if opp.proposals else 1
        proposal = Proposal(opportunity_id=opp.id, version=version, title=p.title, body=p.body, price=p.price,
                            pricing_model=p.pricing_model, timeline_days=p.timeline_days, milestones=p.milestones,
                            questions_for_buyer=p.questions_for_buyer, capability_claims=p.capability_claims,
                            raw_response=p.model_dump())
        db.add(proposal)
        opp.proposals.append(proposal)
        db.flush()
        log_event(db, agent="ProposalAgent", action="proposal.drafted", object_type="proposal", object_id=proposal.id,
                  details={"opportunity_id": opp.id, "price": p.price, "version": version})

        # 7. Into the approval queue - nothing is submitted without the owner
        _set_status(db, opp, OpportunityStatus.AWAITING_APPROVAL, "System", "opportunity.recommended",
                    {"score": analysis.opportunity_score, "expected_value": analysis.expected_value})
        if analysis.opportunity_score >= float(get_setting(db, "notify_min_score")):
            notify_new_opportunity(db, opp)
        return "recommended"

    except LLMError as exc:
        opp.last_error = str(exc)[:2000]
        _set_status(db, opp, OpportunityStatus.ERROR, "System", "pipeline.error", {"error": str(exc)[:500]})
        notify_system(db, "Pipeline error", f"{opp.title[:80]}: {str(exc)[:200]}", opp.id, level="error")
        return "errors"
    except Exception as exc:  # noqa: BLE001
        opp.last_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
        _set_status(db, opp, OpportunityStatus.ERROR, "System", "pipeline.error", {"error": str(exc)[:500]})
        log.exception("pipeline failed for %s", opp.id)
        return "errors"


_SET_ASIDE_HINTS = ("set-aside", "set aside", "8(a)", "hubzone", "sdvosb", "wosb", "edwosb", "small business")


def ensure_structural_requirements(db: Session, opp: Opportunity) -> int:
    """Deterministic requirements for bids: registration, set-aside eligibility, deadline. Always unverified."""
    if not opp.is_bid:
        return 0
    wanted: list[tuple[str, str, bool]] = [
        ("Active SAM.gov entity registration (UEI/CAGE) or equivalent vendor registration", "form", True)]
    if opp.set_aside and opp.set_aside.lower() not in ("none", "no set-aside", "n/a"):
        wanted.append((f"Eligible for set-aside: {opp.set_aside}", "representation", True))
    if opp.deadline:
        wanted.append((f"Submit before response deadline {opp.deadline:%Y-%m-%d %H:%M} UTC", "deadline", True))
    existing = {r.requirement.strip().lower() for r in db.query(ComplianceRequirement).filter_by(opportunity_id=opp.id)}
    added = 0
    for text, rtype, mandatory in wanted:
        if text.lower() in existing:
            continue
        req = ComplianceRequirement(opportunity_id=opp.id, requirement=text, type=rtype, mandatory=mandatory,
                                    verified=False, source_agent="System")
        db.add(req)
        opp.requirements.append(req)
        added += 1
    db.flush()
    return added


def build_bid_package(db: Session, opp: Opportunity, agent, owner_notes: str = "") -> int:
    """Draft (or re-draft) the bid documents. Existing owner-approved documents are kept; drafts are replaced."""
    result = agent.run(db, opp, owner_notes)
    approved = {d.name.strip().lower(): d for d in db.query(BidDocument).filter_by(opportunity_id=opp.id, status="OWNER_APPROVED")}
    for draft in db.query(BidDocument).filter_by(opportunity_id=opp.id, status="DRAFT").all():
        db.delete(draft)
    db.flush()
    created = 0
    for i, doc in enumerate(result.documents):
        key = doc.name.strip().lower()
        if key in approved:
            continue
        row = BidDocument(opportunity_id=opp.id, order=i, name=doc.name, doc_type=doc.doc_type, content=doc.content,
                          requires_owner_input=doc.requires_owner_input, owner_input_notes=doc.owner_input_notes,
                          author=agent.role)
        db.add(row)
        created += 1
    db.flush()
    db.expire(opp, ["bid_documents"])
    if opp.analysis and result.price_total > 0:
        a = opp.analysis
        delta = result.price_total - a.recommended_price
        if abs(delta) > 0.5:
            a.recommended_price = result.price_total
            a.expected_profit = round(a.expected_profit + delta, 2)
            a.expected_value = round(a.estimated_probability_of_win * a.expected_profit, 2)
            a.profit_per_human_hour = round(a.expected_profit / max(a.estimated_human_hours, 0.25), 2)
    log_event(db, agent=agent.role, action="bid_package.drafted", object_type="opportunity", object_id=opp.id,
              details={"documents": created, "kept_approved": len(approved), "price_total": result.price_total,
                       "submission_checklist": result.submission_checklist, "open_questions": result.open_questions})
    return created


def extract_requirements(db: Session, opp: Opportunity, agent) -> int:
    """Create ComplianceRequirement rows from the listing. Never touches `verified`; keeps owner-verified rows."""
    result = agent.run(db, opp)
    existing = {r.requirement.strip().lower(): r
                for r in db.query(ComplianceRequirement).filter_by(opportunity_id=opp.id).all()}
    added = 0
    for item in result.requirements:
        key = item.requirement.strip().lower()
        if not key or key in existing:
            continue
        req = ComplianceRequirement(opportunity_id=opp.id, requirement=item.requirement, type=item.type,
                                    mandatory=item.mandatory, evidence="", verified=False,
                                    source_agent=agent.role)
        db.add(req)
        opp.requirements.append(req)
        existing[key] = req
        added += 1
    db.flush()
    if added:
        log_event(db, agent=agent.role, action="requirements.extracted", object_type="opportunity", object_id=opp.id,
                  details={"added": added, "summary": result.summary[:300]})
    return added


def reanalyze_opportunity(db: Session, opportunity_id: str, approval_id: str | None = None) -> str:
    """Owner-triggered re-run of the pipeline (keeps history: proposal versions increment)."""
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise ValueError(opportunity_id)
    previous = opp.status
    opp.rejection_reasons = []
    opp.rejection_stage = None
    opp.last_error = None
    opp.status = OpportunityStatus.NEW.value
    db.flush()
    log_event(db, agent="Owner", action="opportunity.reanalyze_requested", object_type="opportunity",
              object_id=opp.id, previous_state=previous, new_state=opp.status, human_approval_required=True,
              approval_id=approval_id)
    return process_opportunity(db, opp.id)
