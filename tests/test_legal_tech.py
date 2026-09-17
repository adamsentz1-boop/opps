import json
import urllib.error
import urllib.request

import pytest

from app.fx import load_rates, to_usd
from app.models import Opportunity
from app.pipeline import process_opportunity
from app.settings_service import set_setting
from app.sources.find_a_tender import FindATenderSource, release_to_payload
from app.sources.ted import TEDSource, flat, notice_to_payload
from app.taxonomy import classify
from tests.conftest import make_opp


# ------------------------------------------------------------------ taxonomy
def test_classifier_separates_legal_tech_from_noise_and_legal_services():
    good = classify("Contract lifecycle management platform", "CLM with clause extraction and e-signature",
                    buyer_name="Ministry of Justice")
    assert good.is_legal_tech and good.category == "clm" and good.relevance > 60
    assert good.buyer_signals

    noise = classify("Supply and delivery of office furniture", "Desks and chairs for the courthouse")
    assert noise.is_legal_tech is False and noise.category is None

    services = classify("Provision of legal advice", "Panel of law firms to provide legal representation, "
                                                     "legal opinion and advocacy services. Contract review of advice.")
    assert services.services_only or not services.is_legal_tech

    ediscovery = classify("eDiscovery and document review platform",
                          "technology assisted review, redaction and disclosure review for litigation")
    assert ediscovery.is_legal_tech and ediscovery.category in ("ediscovery", "document_review")


def test_classifier_uses_cpv_codes_as_a_hint_for_legal_buyers():
    result = classify("Digital transformation programme", "Modernisation of internal systems",
                      codes=["48311000"], buyer_name="Ministry of Justice")
    assert result.is_legal_tech and "classification code" in " ".join(result.reasons).lower()


# ------------------------------------------------------------------ currency
def test_fx_converts_and_reports_unknown_currencies():
    rates = load_rates('{"EUR": 1.10}')
    assert to_usd(100, "EUR", rates) == 110.0
    assert to_usd(100, "GBP", rates) == 127.0          # falls back to the built-in default
    assert to_usd(100, "XYZ", rates) is None           # unknown currency is reported, never guessed
    assert to_usd(None, "EUR", rates) is None


def test_opportunity_value_is_normalised_to_usd(db):
    opp = make_opp(db, external_id="fx-1", currency="EUR", estimated_value=200000, budget_min=None, budget_max=None,
                   budget_type="unknown", opportunity_type="bid", title="Contract analytics platform tender",
                   description="Clause extraction and contract review for the legal department.")
    assert opp.value_usd == pytest.approx(200000 * 1.08, rel=0.01)
    assert opp.is_legal_tech and opp.legal_tech_category == "contract_analytics"


# ------------------------------------------------------------------ TED adapter
NOTICE = {
    "publication-number": "123456-2026",
    "notice-title": {"eng": ["Contract lifecycle management system"], "fra": ["Systeme de gestion"]},
    "buyer-name": {"eng": ["Ministry of Justice"]}, "buyer-country": "IRL",
    "classification-cpv": ["48311000"], "total-value": "450000", "total-value-cur": "EUR",
    "publication-date": "2026-09-10", "deadline": "2026-10-15T12:00:00Z", "notice-type": "cn-standard",
}


def test_ted_flattens_multilingual_values_and_maps_notices():
    assert flat({"fra": ["Systeme"], "eng": ["System"]}) == "System"
    assert flat(["a", "b"]) == "a b"
    assert flat(None) == ""
    payload = notice_to_payload(NOTICE)
    assert payload["external_id"] == "123456-2026" and payload["opportunity_type"] == "bid"
    assert payload["title"] == "Contract lifecycle management system"
    assert payload["country"] == "IRL" and payload["currency"] == "EUR" and payload["estimated_value"] == 450000.0
    assert payload["cpv_codes"] == ["48311000"] and "sam.gov" not in (payload["source_url"] or "")


def test_ted_query_building_combines_cpv_keywords_and_countries():
    src = TEDSource(cpv_codes=["48311000", "72310000"], keywords=["contract management", "eDiscovery"],
                    countries=["IRL", "DEU"], days_back=7)
    queries = src.build_queries()
    assert any("classification-cpv=48311000 OR classification-cpv=72310000" in q for q in queries)
    assert any('FT~"contract management"' in q for q in queries)
    assert all("publication-date>=" in q and "buyer-country=IRL" in q for q in queries)


def test_ted_fetch_dedupes_and_falls_back_when_fields_are_rejected(monkeypatch):
    seen_bodies = []

    class _Resp:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._p).encode()

    def fake_urlopen(req, timeout=0):
        body = json.loads(req.data.decode())
        seen_bodies.append(body)
        if len(body["fields"]) > 5:          # first attempt with the preferred field list
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                         __import__("io").BytesIO(b'{"message":"Validation error on field fields"}'))
        return _Resp({"notices": [NOTICE, NOTICE]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    src = TEDSource(cpv_codes=["48311000"], keywords=[], countries=[], max_results=50)
    src.enabled = True
    items = src.fetch_new_opportunities()
    assert len(items) == 1                                  # de-duplicated by publication-number
    assert len(seen_bodies) == 2 and len(seen_bodies[1]["fields"]) == 5   # retried with the minimal field set
    assert "field list rejected" in (src.status_note() or "")


def test_ted_disabled_without_opt_in():
    src = TEDSource()
    src.enabled = False
    with pytest.raises(NotImplementedError):
        src.fetch_new_opportunities()


# ------------------------------------------------------------------ Find a Tender adapter
RELEASE = {
    "ocid": "ocds-h6vhtk-03d2c1", "id": "2026-000123", "date": "2026-09-12T09:00:00Z",
    "buyer": {"name": "HM Courts & Tribunals Service"},
    "tender": {"title": "eDiscovery and document review platform",
               "description": "Provision of a hosted eDiscovery platform with technology assisted review.",
               "value": {"amount": 750000, "currency": "GBP"},
               "tenderPeriod": {"endDate": "2026-10-20T12:00:00Z"},
               "items": [{"classification": {"scheme": "CPV", "id": "48311000"}}],
               "documents": [{"url": "https://www.find-tender.service.gov.uk/Notice/2026-000123"}],
               "mainProcurementCategory": "services", "procurementMethod": "open"},
}


def test_find_a_tender_parses_ocds_defensively():
    payload = release_to_payload(RELEASE)
    assert payload["title"] == "eDiscovery and document review platform" and payload["country"] == "GBR"
    assert payload["estimated_value"] == 750000.0 and payload["currency"] == "GBP"
    assert payload["cpv_codes"] == ["48311000"] and payload["source_url"].startswith("https://")
    # a sparse release must not raise
    sparse = release_to_payload({"id": "x", "tender": {}})
    assert sparse["title"] == "Untitled tender" and sparse["estimated_value"] is None


def test_find_a_tender_follows_pagination(monkeypatch):
    pages = {"first": {"releases": [RELEASE], "links": {"next": "https://www.find-tender.service.gov.uk/next"}},
             "next": {"releases": [{**RELEASE, "id": "2026-000124"}], "links": {}}}
    calls = []

    class _Resp:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._p).encode()

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        return _Resp(pages["next"] if req.full_url.endswith("/next") else pages["first"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    src = FindATenderSource(days_back=1, max_results=50)
    src.enabled = True
    items = src.fetch_new_opportunities()
    assert len(items) == 2 and len(calls) == 2
    assert all(i.opportunity_type == "bid" for i in items)


# ------------------------------------------------------------------ vendor mode
def _vendor_mode(db, **overrides):
    set_setting(db, "profile_mode", "vendor")
    set_setting(db, "legal_tech_only", "true")
    set_setting(db, "minimum_deal_value", 25000)
    set_setting(db, "maximum_bid_effort_hours", 60)
    for key, value in overrides.items():
        set_setting(db, key, value)
    db.commit()


def test_vendor_mode_rejects_non_legal_tech_before_spending_tokens(db):
    _vendor_mode(db)
    opp = make_opp(db, external_id="v-1", opportunity_type="bid", title="Supply of office furniture",
                   description="Desks, chairs and filing cabinets for three floors.",
                   estimated_value=300000, budget_min=None, budget_max=None, budget_type="unknown")
    assert process_opportunity(db, opp.id) == "rejected"
    opp = db.get(Opportunity, opp.id)
    assert opp.rejection_stage == "rules" and opp.analysis is None
    assert any("Not a legal-technology opportunity" in r for r in opp.rejection_reasons)


def test_vendor_mode_region_gating(db):
    _vendor_mode(db, target_regions="USA,GBR,IRL", excluded_regions="RUS")
    blocked = make_opp(db, external_id="v-2", opportunity_type="bid", country="BRA", currency="USD",
                       title="Contract analytics platform", description="Clause extraction and contract review.",
                       estimated_value=400000, budget_min=None, budget_max=None, budget_type="unknown")
    assert process_opportunity(db, blocked.id) == "rejected"
    assert any("outside target_regions" in r for r in db.get(Opportunity, blocked.id).rejection_reasons)

    excluded = make_opp(db, external_id="v-3", opportunity_type="bid", country="RUS", currency="USD",
                        title="Contract analytics platform", description="Clause extraction and contract review.",
                        estimated_value=400000, budget_min=None, budget_max=None, budget_type="unknown")
    assert process_opportunity(db, excluded.id) == "rejected"
    assert any("excluded_regions" in r for r in db.get(Opportunity, excluded.id).rejection_reasons)


def test_vendor_mode_deal_value_uses_converted_usd(db):
    _vendor_mode(db, minimum_deal_value=100000)
    small = make_opp(db, external_id="v-4", opportunity_type="bid", country="IRL", currency="EUR",
                     title="Contract analytics pilot", description="Clause extraction pilot for the legal department.",
                     estimated_value=50000, budget_min=None, budget_max=None, budget_type="unknown")
    assert process_opportunity(db, small.id) == "rejected"
    reasons = db.get(Opportunity, small.id).rejection_reasons
    assert any("below minimum" in r and "$54,000" in r for r in reasons)


def test_vendor_mode_qualifies_a_legal_tech_tender_with_product_framing(db):
    _vendor_mode(db, organization_name="Acme Legal Tech",
                 product_profile="Organisation: Acme Legal Tech Ltd. Product: contract analytics and AI document "
                                 "review. Deployment: EU and US SaaS regions. Certifications: ISO 27001 (2025). "
                                 "Reference customers: two, with written permission.")
    opp = make_opp(db, external_id="v-5", opportunity_type="bid", country="IRL", currency="EUR",
                   agency="Ministry of Justice", solicitation_number="MOJ-2026-11",
                   title="Contract analytics and clause extraction platform",
                   description=("The Department requires a contract analytics platform providing clause extraction, "
                                "contract review and lease abstraction for its legal department. Detailed requirements "
                                "and sample documents are attached. Vendor must hold ISO 27001 and offer EU data residency."),
                   estimated_value=600000, budget_min=None, budget_max=None, budget_type="unknown")
    result = process_opportunity(db, opp.id)
    opp = db.get(Opportunity, opp.id)
    assert result == "recommended", opp.rejection_reasons
    assert opp.is_legal_tech and opp.legal_tech_category == "contract_analytics"
    assert opp.value_usd == pytest.approx(648000, rel=0.01)
    assert opp.bid_documents and opp.current_proposal

    # the vendor prompt addendum, not the solo one, reaches the model
    from app.agents import QualificationAgent
    prompt = QualificationAgent().system_prompt_for(db)
    assert "Profile: vendor bidding a product" in prompt and "PRODUCT FIT" in prompt
    assert "Profile: independent consultant" not in prompt


def test_solo_mode_is_unchanged_by_default(db):
    from app.agents import QualificationAgent
    assert "Profile: independent consultant" in QualificationAgent().system_prompt_for(db)
    opp = make_opp(db, external_id="v-6")
    assert process_opportunity(db, opp.id) == "recommended"
