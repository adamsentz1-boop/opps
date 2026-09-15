import json

from app.normalizer import normalize, parse_budget
from app.sources.json_source import GenericJSONSource
from app.sources.rss import parse_feed
from app.sources.stubs import STUB_SOURCES

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Jobs</title>
<item><title>Python ETL script - $900</title><link>https://example.com/j/1</link><guid>j-1</guid>
<description><![CDATA[<p>Need a <b>Python</b> script to transform CSVs. Budget $900.</p>]]></description>
<pubDate>Mon, 14 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"""


def test_parse_budget_variants():
    assert parse_budget("Budget $1,500-2,000") == (1500.0, 2000.0, "fixed")
    assert parse_budget("pay $45/hr") == (45.0, 45.0, "hourly")
    assert parse_budget("no money mentioned") == (None, None, "unknown")


def test_rss_feed_parses_to_normalised_schema():
    items = parse_feed(RSS, "rss:test")
    assert len(items) == 1
    item = items[0]
    assert item.external_id == "j-1" and item.budget_max == 900 and "<b>" not in item.description
    assert item.posted_at is not None


def test_json_source_with_field_map(tmp_path):
    path = tmp_path / "feed.json"
    path.write_text(json.dumps({"jobs": [{"jobTitle": "Excel automation", "details": "Consolidate workbooks $700",
                                          "jobId": 42, "client": "ACME"}]}))
    src = GenericJSONSource(str(path), field_map={"title": "jobTitle", "description": "details", "external_id": "jobId"})
    items = src.fetch_new_opportunities()
    assert items[0].title == "Excel automation" and items[0].external_id == "42" and items[0].buyer_name == "ACME"


def test_stub_sources_do_not_scrape():
    import pytest
    for cls in STUB_SOURCES:
        with pytest.raises(NotImplementedError):
            cls().fetch_new_opportunities()
        assert cls().requirements


def test_web_end_to_end(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["llm"] == "mock"
    r = client.post("/api/opportunities", json={
        "title": "Workato API integration HubSpot to NetSuite", "buyer_name": "Brightline",
        "budget_min": 2000, "budget_max": 2500,
        "description": "Workato recipe to sync HubSpot deals to NetSuite sales orders via REST API. Connectors authorised. "
                       "Documentation required. Sample data available. Slack error alerts. Budget $2,000-2,500."})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pipeline_result"] == "recommended"
    opp_id = body["opportunity"]["id"]
    assert client.get("/").status_code == 200
    assert "AWAITING_APPROVAL" in client.get("/approvals").text or "APPROVE" in client.get("/approvals").text
    assert client.get(f"/opportunities/{opp_id}").status_code == 200
    r = client.post(f"/api/opportunities/{opp_id}/approve", json={"notes": "go"})
    assert r.status_code == 200 and r.json()["status"] == "READY_TO_SUBMIT"
    assert client.post(f"/api/opportunities/{opp_id}/approve").status_code == 409
    for path in ["/opportunities", "/opportunities?status=rejected", "/work-orders", "/sources", "/agent-runs",
                 "/audit", "/notifications", "/settings", "/api/metrics", "/api/costs", "/api/settings"]:
        assert client.get(path).status_code == 200, path
    r = client.put("/api/settings/minimum_project_value", json={"value": 750})
    assert r.status_code == 200 and client.get("/api/settings").json()["minimum_project_value"] == 750.0
    # settings page never leaks the API key
    assert "sk-ant" not in client.get("/settings").text
