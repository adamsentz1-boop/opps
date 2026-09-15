from __future__ import annotations

import os

import pytest

os.environ["LLM_MOCK"] = "true"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ANTHROPIC_API_KEY"] = ""


@pytest.fixture()
def db(tmp_path):
    from app import db as dbmod
    from app.llm.client import reset_llm_client
    dbmod.reset_engine_for_tests(f"sqlite:///{tmp_path / 'test.db'}")
    reset_llm_client()
    dbmod.init_db()
    session = dbmod.get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_opp(db, **overrides):
    from app.normalizer import upsert_opportunity
    from app.sources.manual import normalize_manual
    payload = {
        "external_id": overrides.pop("external_id", "t-1"),
        "title": "n8n workflow: sync HubSpot contacts to Google Sheets and Slack alerts",
        "buyer_name": "Acme Widgets", "budget_min": 1500, "budget_max": 2000, "budget_type": "fixed",
        "description": ("We need an n8n automation that syncs HubSpot contacts to a Google Sheet, dedupes by email, and posts "
                        "a Slack alert for new enterprise leads. We have n8n cloud and API access. Documentation required. "
                        "Clear inputs and outputs, sample data available. Budget $1,500-2,000."),
    }
    payload.update(overrides)
    opp, _ = upsert_opportunity(db, normalize_manual(payload))
    db.commit()
    return opp
