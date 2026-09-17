import io
import urllib.request

from app.enums import OpportunityStatus as S
from app.models import ComplianceRequirement, Opportunity
from app.notifications.adapters import NtfyAdapter
from app.notifications.base import NotificationMessage
from app.pipeline import process_opportunity
from tests.conftest import make_opp


def test_requirements_are_extracted_unverified(db):
    opp = make_opp(db, external_id="r-1", description=(
        "Build an n8n and python automation to sync HubSpot to Google Sheets. Vendor must have a SOC 2 report and "
        "provide a W-9 and proof of liability insurance. Budget $2,500. Sample data available, documentation required."))
    assert process_opportunity(db, opp.id) == "recommended"
    opp = db.get(Opportunity, opp.id)
    reqs = db.query(ComplianceRequirement).filter_by(opportunity_id=opp.id).all()
    assert {r.type for r in reqs} >= {"certification", "form", "insurance"}
    assert all(r.verified is False and r.source_agent == "RequirementsAgent" for r in reqs)
    # mandatory unverified requirements block approval until the owner verifies them
    from app import approvals
    import pytest
    with pytest.raises(approvals.ApprovalError):
        approvals.approve_opportunity(db, opp)
    for r in reqs:
        approvals.verify_requirement(db, r, evidence="on file")
    approvals.approve_opportunity(db, opp)
    assert opp.status == S.READY_TO_SUBMIT.value


def test_reanalysis_keeps_verified_requirements(db):
    from app import approvals
    from app.pipeline import reanalyze_opportunity
    opp = make_opp(db, external_id="r-2", description="n8n python automation, must have SOC 2 report. Budget $2,000. Sample data available, documentation required.")
    process_opportunity(db, opp.id)
    req = db.query(ComplianceRequirement).filter_by(opportunity_id=opp.id).one()
    approvals.verify_requirement(db, req, evidence="report on file")
    db.commit()
    reanalyze_opportunity(db, opp.id)
    reqs = db.query(ComplianceRequirement).filter_by(opportunity_id=opp.id).all()
    assert len(reqs) == 1 and reqs[0].verified is True


def test_ntfy_is_silent_unless_configured(db, monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("should not send")))
    assert NtfyAdapter().send(db, NotificationMessage(title="t", body="b")) is False
    assert calls == []


def test_ntfy_sends_when_configured(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com")
    monkeypatch.setenv("NTFY_TOPIC", "opps")
    get_settings.cache_clear()
    sent = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["title"] = req.get_header("Title")
        sent["body"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    try:
        ok = NtfyAdapter().send(db, NotificationMessage(title="NEW MONEY OPPORTUNITY", body="Budget: $2,500", level="opportunity"))
    finally:
        get_settings.cache_clear()
    assert ok and sent["url"] == "https://ntfy.example.com/opps" and sent["title"] == "NEW MONEY OPPORTUNITY"
    assert "Budget" in sent["body"]


def test_proposal_markdown_export(client):
    r = client.post("/api/opportunities", json={
        "title": "n8n automation for HubSpot to Sheets", "budget_min": 1500, "budget_max": 2000,
        "description": "n8n workflow syncing HubSpot contacts to Google Sheets with Slack alerts. API access ready. "
                       "Documentation required. Sample data available. Budget $1,500-2,000."})
    assert r.json()["pipeline_result"] == "recommended"
    opp_id = r.json()["opportunity"]["id"]
    md = client.get(f"/opportunities/{opp_id}/proposal.md")
    assert md.status_code == 200 and md.text.startswith("# ") and "**Price:**" in md.text
    assert client.post("/sources/scan", follow_redirects=False).status_code == 303


def test_webhook_adapter_is_silent_unless_configured(db, monkeypatch):
    from app.notifications.adapters import WebhookAdapter
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("should not send")))
    assert WebhookAdapter().send(db, NotificationMessage(title="t", body="b")) is False
    assert calls == []


def test_webhook_adapter_posts_json_for_n8n(db, monkeypatch):
    import json as _json
    from app.config import get_settings
    from app.notifications.adapters import WebhookAdapter
    monkeypatch.setenv("WEBHOOK_URL", "http://host.docker.internal:5678/webhook/opps")
    monkeypatch.setenv("WEBHOOK_TOKEN", "s3cret")
    get_settings.cache_clear()
    sent = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["token"] = req.get_header("X-engine-token")
        sent["payload"] = _json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    try:
        ok = WebhookAdapter().send(db, NotificationMessage(title="NEW MONEY OPPORTUNITY", body="Budget: $2,500",
                                                           level="opportunity", opportunity_id="abc",
                                                           meta={"score": 93, "country": "IRL"}))
    finally:
        get_settings.cache_clear()
    assert ok and sent["url"].endswith("/webhook/opps") and sent["token"] == "s3cret"
    assert sent["payload"]["score"] == 93 and sent["payload"]["country"] == "IRL"
    assert sent["payload"]["opportunity_id"] == "abc" and sent["payload"]["source"] == "opportunity-engine"


def test_new_opportunity_alert_carries_routable_numbers(db, monkeypatch):
    from app.models import Notification
    from app.notifications import notify_new_opportunity
    from app.pipeline import process_opportunity
    from tests.conftest import make_opp
    captured = {}
    import app.notifications.service as svc
    real_notify = svc.notify

    def spy(db_, message):
        captured.update(message.meta)
        return real_notify(db_, message)

    monkeypatch.setattr(svc, "notify", spy)
    opp = make_opp(db, external_id="n8n-1")
    process_opportunity(db, opp.id)
    assert captured["score"] > 0 and captured["expected_profit"] > 0
    assert "recommended_bid" in captured and "human_hours" in captured
