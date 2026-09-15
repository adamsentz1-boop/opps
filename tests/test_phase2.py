import json
import urllib.request
from datetime import timedelta

import pytest

from app import approvals
from app.enums import OpportunityStatus as S
from app.enums import WorkOrderStatus
from app.models import BidDocument, ComplianceRequirement, Opportunity, utcnow
from app.pipeline import process_opportunity
from app.sources.sam_gov import SamGovSource, notice_to_payload
from app.work_orders import WorkOrderError, create_work_order, execute_task, read_deliverable, run_qa, transition
from tests.conftest import make_opp

NOTICE = {
    "noticeId": "abc123", "title": "Data Migration and Workflow Automation Services", "solicitationNumber": "W912DY-26-R-0042",
    "fullParentPathName": "DEPT OF DEFENSE.DEPT OF THE ARMY.USACE", "postedDate": "2026-09-10", "type": "Combined Synopsis/Solicitation",
    "typeOfSetAside": "SBA", "typeOfSetAsideDescription": "Total Small Business Set-Aside (FAR 19.5)",
    "responseDeadLine": "2026-10-01T17:00:00-04:00", "naicsCode": "541511", "classificationCode": "DA01",
    "pointOfContact": [{"fullName": "Jane Doe", "email": "jane@example.mil"}],
    "description": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc123",
    "placeOfPerformance": {"city": {"name": "Huntsville"}, "state": {"code": "AL"}},
    "uiLink": "https://sam.gov/opp/abc123/view",
}


def test_sam_gov_notice_mapping():
    item = __import__("app.normalizer", fromlist=["normalize"]).normalize("sam_gov", notice_to_payload(NOTICE, "Migrate legacy Access DB to cloud with python automation."))
    assert item.opportunity_type == "bid" and item.external_id == "abc123" and item.set_aside.startswith("Total Small Business")
    assert item.naics_code == "541511" and item.deadline is not None and item.buyer_type == "government"
    assert "Migrate legacy Access DB" in item.description and item.solicitation_number == "W912DY-26-R-0042"


def test_sam_gov_adapter_disabled_without_key():
    src = SamGovSource(api_key="", naics=[], keywords=[])
    assert src.enabled is False
    with pytest.raises(NotImplementedError):
        src.fetch_new_opportunities()


def test_sam_gov_adapter_fetches_and_dedupes(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._p).encode()

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        calls.append(url)
        assert "api_key=secret" in url
        if "noticedesc" in url:
            return _Resp({"description": "<p>Automation &amp; data migration</p>"})
        return _Resp({"totalRecords": 1, "opportunitiesData": [NOTICE]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    src = SamGovSource(api_key="secret", naics=["541511"], keywords=["automation"], days_back=7, max_results=10)
    items = src.fetch_new_opportunities()
    assert len(items) == 1  # same notice from both queries, de-duplicated
    assert "Automation & data migration" in items[0].description
    assert any("ncode=541511" in c for c in calls) and any("title=automation" in c for c in calls)


def _bid(db, **over):
    payload = dict(external_id="bid-1", opportunity_type="bid", agency="City of Harrisburg IT",
                   solicitation_number="RFP-26-114", set_aside="Total Small Business", naics_code="541511",
                   budget_min=None, budget_max=None, estimated_value=18000, budget_type="unknown",
                   deadline=(utcnow() + timedelta(days=12)).isoformat(),
                   title="RFP: automate permit application intake and document classification",
                   description=("The City seeks a vendor to build an automated intake workflow: OCR permit PDFs, classify "
                                "document types with an AI model, extract fields to a database, and produce daily reports. "
                                "Vendor must be registered in SAM.gov, provide a W-9, and carry general liability insurance. "
                                "Python or n8n acceptable. Detailed requirements and sample documents attached. Estimated value $18,000."))
    payload.update(over)
    return make_opp(db, **payload)


def test_bid_pipeline_builds_package_and_requirements(db):
    opp = _bid(db)
    assert process_opportunity(db, opp.id) == "recommended", db.get(Opportunity, opp.id).rejection_reasons
    opp = db.get(Opportunity, opp.id)
    docs = db.query(BidDocument).filter_by(opportunity_id=opp.id).all()
    assert {d.doc_type for d in docs} >= {"cover_letter", "technical", "price", "forms_checklist"}
    assert all(d.status == "DRAFT" for d in docs)
    reqs = db.query(ComplianceRequirement).filter_by(opportunity_id=opp.id).all()
    texts = " | ".join(r.requirement for r in reqs)
    assert "SAM.gov" in texts and "set-aside" in texts.lower() and "deadline" in texts.lower()
    assert all(not r.verified for r in reqs)
    # approval blocked: docs unapproved AND requirements unverified
    with pytest.raises(approvals.ApprovalError):
        approvals.approve_opportunity(db, opp)
    for r in reqs:
        approvals.verify_requirement(db, r, evidence="verified by owner")
    with pytest.raises(approvals.ApprovalError, match="Bid documents not yet approved"):
        approvals.approve_opportunity(db, opp)
    for d in docs:
        if "[OWNER:" in d.content:
            with pytest.raises(approvals.ApprovalError, match="placeholders"):
                approvals.approve_bid_document(db, d)
            approvals.edit_bid_document(db, d, d.content.replace("[OWNER: signature block]", "Jane Owner, Owner LLC")
                                        .replace("[OWNER: solicitation number]", "RFP-26-114")
                                        .replace("[OWNER: add up to three verifiable references or state 'none']", "None."))
        approvals.approve_bid_document(db, d)
    approvals.approve_opportunity(db, opp)
    assert opp.status == S.READY_TO_SUBMIT.value


def test_past_deadline_bid_rejected_by_rules(db):
    opp = _bid(db, external_id="bid-2", deadline=(utcnow() - timedelta(days=1)).isoformat())
    assert process_opportunity(db, opp.id) == "rejected"
    assert any("deadline passed" in r.lower() for r in db.get(Opportunity, opp.id).rejection_reasons)


def test_redraft_keeps_approved_documents(db):
    from app.agents import BidAgent
    from app.pipeline.runner import build_bid_package
    opp = _bid(db, external_id="bid-3")
    process_opportunity(db, opp.id)
    opp = db.get(Opportunity, opp.id)
    tech = next(d for d in opp.bid_documents if d.doc_type == "technical")
    approvals.approve_bid_document(db, tech, "fine")
    db.commit()
    build_bid_package(db, opp, BidAgent(), "tighten the price")
    db.commit()
    docs = db.query(BidDocument).filter_by(opportunity_id=opp.id).all()
    assert sum(1 for d in docs if d.status == "OWNER_APPROVED") == 1
    assert next(d for d in docs if d.doc_type == "technical").id == tech.id


def test_work_execution_drafts_qa_and_guards_delivery(db, tmp_path, monkeypatch):
    import app.work_orders as wom
    monkeypatch.setattr(wom, "WORKSPACE_DIR", tmp_path)
    opp = make_opp(db, external_id="w-1")
    process_opportunity(db, opp.id)
    opp = db.get(Opportunity, opp.id)
    approvals.approve_opportunity(db, opp)
    approvals.record_outcome(db, opp, "submitted")
    approvals.record_outcome(db, opp, "won", won_value=1800)
    wo = create_work_order(db, opp)
    db.commit()
    human = [t for t in wo.tasks if t.owner == "human"]
    agent_tasks = [t for t in wo.tasks if t.owner != "human"]
    assert human and agent_tasks
    with pytest.raises(WorkOrderError, match="assigned to you"):
        execute_task(db, human[0])
    with pytest.raises(WorkOrderError, match="dependencies"):
        execute_task(db, agent_tasks[0])  # depends on the human 'collect inputs' task
    from app.work_orders import set_task_status
    set_task_status(db, human[0], "DONE")
    d = execute_task(db, agent_tasks[0], inputs="sample rows: a,b,c")
    assert d.file_path and d.file_path.startswith(str(tmp_path)) and d.version == 1 and d.status == "DRAFT"
    assert read_deliverable(d) and wo.status == WorkOrderStatus.IN_PROGRESS.value
    result = run_qa(db, d)
    assert result.passed and d.status == "QA_PASSED"
    approvals.approve_deliverable(db, d, "ok")
    assert d.status == "APPROVED"
    transition(db, wo, "QA")
    transition(db, wo, "AWAITING_OWNER_APPROVAL")
    with pytest.raises(WorkOrderError):
        transition(db, wo, "DELIVERED")
    approvals.approve_delivery(db, wo)
    transition(db, wo, "DELIVERED")
    transition(db, wo, "INVOICED")
    assert wo.invoice_path and "INVOICE" in open(wo.invoice_path).read()
    transition(db, wo, "PAID")
    assert wo.paid_amount == 1800


def test_bid_web_pages(client):
    r = client.post("/api/opportunities", json={
        "title": "RFP: automate records OCR and classification with python", "opportunity_type": "bid",
        "agency": "County Clerk", "solicitation_number": "RFP-1", "set_aside": "Total Small Business",
        "estimated_value": 12000, "budget_type": "unknown",
        "deadline": (utcnow() + timedelta(days=10)).isoformat(),
        "description": "OCR scanned records, classify, extract fields, daily reports. Python or n8n. SAM.gov registration required. "
                       "Sample documents available. Detailed requirements attached."})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pipeline_result"] == "recommended" and body["opportunity"]["opportunity_type"] == "bid"
    assert len(body["opportunity"]["bid_documents"]) >= 4
    opp_id = body["opportunity"]["id"]
    page = client.get(f"/opportunities/{opp_id}")
    assert page.status_code == 200 and "Bid package" in page.text and "Approve document" in page.text
    doc_id = body["opportunity"]["bid_documents"][0]["id"]
    assert client.get(f"/bid-documents/{doc_id}.md").status_code == 200
    assert client.post(f"/api/opportunities/{opp_id}/approve").status_code == 409
    assert "Deadlines in the next 14 days" in client.get("/").text
    assert client.get("/opportunities?kind=bid").status_code == 200
