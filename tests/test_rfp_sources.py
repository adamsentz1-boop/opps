"""Contract-AI RFP intake: relevance scoring, the source filter, and the SAM.gov adapter.

Everything runs offline. The SAM.gov adapter is exercised against a synthetic payload shaped like the
documented v2 response; no test makes a network call.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.normalizer import normalize
from app.pipeline.rejection import _licensed_reason
from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource, SourceError
from app.sources.rfp import ContractAIRelevanceFilter, score_opportunity, score_relevance
from app.sources.sam_gov import SEARCH_URL, SamGovSource


@pytest.fixture()
def env(monkeypatch):
    """Set environment variables and rebuild the cached Settings object."""
    def _set(**values):
        for key, value in values.items():
            monkeypatch.setenv(key.upper(), str(value))
        get_settings.cache_clear()
        return get_settings()
    yield _set
    get_settings.cache_clear()


# --------------------------------------------------------------------------- relevance scoring
def test_core_terms_score_highest():
    result = score_relevance("Seeking a vendor for contract abstraction of 40,000 commercial leases.")
    assert result.score >= 3 and "contract abstraction" in result.matched and result.relevant


def test_hyphen_and_spacing_variants_match():
    for text in ("e-discovery support services", "ediscovery support services", "e discovery support services"):
        assert score_relevance(text).score >= 3, text


def test_acronyms_are_case_sensitive():
    assert "CLM" in score_relevance("CLM platform implementation").matched
    assert "CLM" not in score_relevance("the clm of the river was calm").matched


def test_bare_contract_does_not_match():
    """Every procurement notice says 'contract'. On its own it must not be a signal."""
    assert score_relevance("This contract is for snow plowing services.").score == 0


def test_exclusion_terms_zero_the_score():
    text = "Janitorial contract including document management and records management for the facility."
    result = score_relevance(text)
    assert result.score == 0 and "janitorial" in result.excluded and not result.relevant


def test_supporting_and_context_terms_accumulate():
    text = ("The law firm requires natural language processing and optical character recognition "
            "to process supplier agreements.")
    result = score_relevance(text)
    assert result.score >= 3 and len(result.matched) >= 3


def test_extra_owner_terms_score_as_core():
    text = "Vendor will perform Kira Systems configuration for the diligence workstream."
    assert score_relevance(text).score < 3
    assert score_relevance(text, extra_core_terms=["Kira Systems"]).score >= 3


def test_result_is_auditable():
    result = score_relevance("clause extraction and obligation management")
    payload = result.as_dict()
    assert payload["taxonomy"] == "contract_ai" and payload["score"] == result.score
    assert "clause extraction" in payload["matched_terms"]


# --------------------------------------------------------------------------- the filter wrapper
class _FakeSource(OpportunitySource):
    source_name = "fake"
    display_name = "Fake feed"

    def __init__(self, payloads):
        self.payloads = payloads

    def fetch_new_opportunities(self):
        return [normalize(self.source_name, p) for p in self.payloads]

    def status_note(self):
        return "fake-feed"


RELEVANT = {"title": "Contract abstraction services", "description": "Clause extraction across 10,000 MSAs.",
            "external_id": "r1"}
IRRELEVANT = {"title": "Lawn mowing services", "description": "Mow the north campus weekly.", "external_id": "i1"}
EXCLUDED = {"title": "Janitorial services", "description": "Includes records management of cleaning logs.",
            "external_id": "x1"}


def test_filter_keeps_relevant_and_drops_the_rest():
    src = ContractAIRelevanceFilter(_FakeSource([RELEVANT, IRRELEVANT, EXCLUDED]), min_score=3)
    items = src.fetch_new_opportunities()
    assert [i.external_id for i in items] == ["r1"]
    assert "kept 1" in src.status_note() and "dropped 2" in src.status_note()
    assert "fake-feed" in src.status_note()


def test_filter_stamps_relevance_onto_the_opportunity():
    src = ContractAIRelevanceFilter(_FakeSource([RELEVANT]), min_score=3)
    item = src.fetch_new_opportunities()[0]
    relevance = item.raw_payload["rfp_relevance"]
    assert relevance["score"] >= 3 and "contract abstraction" in relevance["matched_terms"]


def test_filter_is_transparent_to_the_pipeline():
    inner = _FakeSource([RELEVANT])
    src = ContractAIRelevanceFilter(inner, min_score=3)
    assert src.source_name == inner.source_name        # de-duplication keys stay stable
    assert src.enabled is inner.enabled
    assert "Fake feed" in src.display_name


def test_threshold_is_configurable():
    loose = ContractAIRelevanceFilter(_FakeSource([{"title": "Document processing services", "external_id": "d1"}]),
                                      min_score=2)
    strict = ContractAIRelevanceFilter(_FakeSource([{"title": "Document processing services", "external_id": "d1"}]),
                                       min_score=6)
    assert len(loose.fetch_new_opportunities()) == 1
    assert len(strict.fetch_new_opportunities()) == 0


def test_filtered_source_feeds_the_normal_pipeline(db):
    from app.pipeline.runner import run_source
    from app.models import Opportunity

    src = ContractAIRelevanceFilter(_FakeSource([RELEVANT, IRRELEVANT]), min_score=3)
    run = run_source(db, src)
    db.commit()
    assert run.status == "ok" and run.fetched == 1 and run.inserted == 1
    assert "dropped 1" in (run.notes or "")
    stored = db.query(Opportunity).filter_by(source="fake").one()
    assert stored.raw_payload["rfp_relevance"]["score"] >= 3


# --------------------------------------------------------------------------- SAM.gov adapter
NOTICE = {
    "noticeId": "abc123", "title": "Contract Abstraction and Clause Extraction Services",
    "solicitationNumber": "W912-26-R-0001", "fullParentPathName": "DEPT OF DEFENSE.DEPT OF THE ARMY",
    "postedDate": "2026-09-15", "type": "Solicitation", "responseDeadLine": "2026-10-15T17:00:00-04:00",
    "naicsCode": "541519", "classificationCode": "R499",
    "typeOfSetAsideDescription": "Total Small Business Set-Aside", "typeOfSetAside": "SBA",
    "description": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc123",
    "uiLink": "https://sam.gov/opp/abc123/view",
    "placeOfPerformance": {"city": {"name": "Arlington"}, "state": {"name": "Virginia"}},
    "pointOfContact": [{"email": "ko@example.mil"}], "organizationType": "OFFICE", "archiveDate": "2026-11-15",
}


def _stub_api(source: SamGovSource, notices=(NOTICE,), body="<p>Seeking contract abstraction support.</p>"):
    calls = []

    def fake_get_json(url, params):
        calls.append((url, params))
        if url == SEARCH_URL:
            return {"totalRecords": len(notices), "opportunitiesData": list(notices)}
        return {"description": body}
    source._get_json = fake_get_json  # type: ignore[method-assign]
    return calls


def test_sam_gov_without_a_key_reports_requirements():
    source = SamGovSource(api_key="")
    assert source.enabled is False
    with pytest.raises(NotImplementedError) as exc:
        source.fetch_new_opportunities()
    assert "open.gsa.gov" in str(exc.value) and "SAM_GOV_API_KEY" in str(exc.value)


def test_sam_gov_maps_a_notice_to_the_normalised_schema():
    source = SamGovSource(api_key="k", naics=["541519"], fetch_descriptions=True)
    _stub_api(source)
    items = source.fetch_new_opportunities()
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, NormalizedOpportunity)
    assert item.external_id == "abc123" and item.source == "sam_gov"
    assert item.buyer_type == "government" and "ARMY" in (item.buyer_name or "")
    assert item.location == "Arlington, Virginia"
    assert item.source_url == "https://sam.gov/opp/abc123/view"
    assert item.posted_at is not None and item.deadline is not None
    assert "W912-26-R-0001" in item.description and "Total Small Business Set-Aside" in item.description
    assert "Seeking contract abstraction support." in item.description and "<p>" not in item.description
    assert item.raw_payload["set_aside_code"] == "SBA" and item.raw_payload["naics_code"] == "541519"


def test_sam_gov_queries_each_naics_code_and_dedupes():
    source = SamGovSource(api_key="k", naics=["541511", "541519"], fetch_descriptions=False)
    calls = _stub_api(source)
    items = source.fetch_new_opportunities()
    searches = [p for url, p in calls if url == SEARCH_URL]
    assert [s["ncode"] for s in searches] == ["541511", "541519"]
    assert len(items) == 1          # same noticeId returned twice, stored once
    assert "2 NAICS code(s)" in (source.status_note() or "")


def test_sam_gov_can_skip_description_fetches():
    source = SamGovSource(api_key="k", naics=["541519"], fetch_descriptions=False)
    calls = _stub_api(source)
    source.fetch_new_opportunities()
    assert all(url == SEARCH_URL for url, _ in calls)


def test_sam_gov_survives_a_missing_description():
    source = SamGovSource(api_key="k", naics=["541519"], fetch_descriptions=True)

    def fake_get_json(url, params):
        if url == SEARCH_URL:
            return {"opportunitiesData": [NOTICE]}
        raise SourceError("description endpoint 500")
    source._get_json = fake_get_json  # type: ignore[method-assign]
    items = source.fetch_new_opportunities()
    assert len(items) == 1 and "W912-26-R-0001" in items[0].description


def test_sam_gov_tolerates_sparse_notices():
    sparse = {"noticeId": "n2", "title": "Document review support"}
    source = SamGovSource(api_key="k", naics=["541519"], fetch_descriptions=False)
    _stub_api(source, notices=(sparse,))
    item = source.fetch_new_opportunities()[0]
    assert item.external_id == "n2" and item.location is None and item.deadline is None


def test_sam_gov_rejects_an_unexpected_payload():
    source = SamGovSource(api_key="k", naics=["541519"])
    source._get_json = lambda url, params: {"totalRecords": 0, "opportunitiesData": "not-a-list"}  # type: ignore
    with pytest.raises(SourceError):
        source.fetch_new_opportunities()


def test_sam_gov_never_leaks_the_api_key(monkeypatch):
    secret = "super-secret-key"
    source = SamGovSource(api_key=secret, naics=["541519"])

    def boom(req, timeout=None):
        raise OSError(f"connection refused for https://api.sam.gov/...api_key={secret}")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(SourceError) as exc:
        source.fetch_new_opportunities()
    assert secret not in str(exc.value) and "[redacted]" in str(exc.value)


# --------------------------------------------------------------------------- registry wiring
def test_rfp_sources_are_absent_until_configured(env):
    from app.sources.registry import get_rfp_sources
    settings = env(rfp_rss_feed_urls="", rfp_json_feed_urls="", sam_gov_api_key="")
    assert get_rfp_sources(settings) == []


def test_configured_rfp_feeds_are_filtered(env):
    from app.sources.registry import get_rfp_sources
    settings = env(rfp_rss_feed_urls="https://example.com/rfp.xml", sam_gov_api_key="k", rfp_filter_enabled="true")
    sources = get_rfp_sources(settings)
    assert len(sources) == 2
    assert all(isinstance(s, ContractAIRelevanceFilter) for s in sources)
    assert {s.source_name for s in sources} == {"rfp-rss:example-com-rfp-xml", "sam_gov"}


def test_filter_can_be_turned_off(env):
    from app.sources.registry import get_rfp_sources
    settings = env(rfp_rss_feed_urls="https://example.com/rfp.xml", sam_gov_api_key="", rfp_filter_enabled="false")
    sources = get_rfp_sources(settings)
    assert len(sources) == 1 and not isinstance(sources[0], ContractAIRelevanceFilter)


def test_unconfigured_sam_gov_still_documents_itself(env):
    from app.sources.registry import describe_sources
    env(sam_gov_api_key="", rfp_rss_feed_urls="", rfp_json_feed_urls="")
    rows = {r["name"]: r for r in describe_sources()}
    assert rows["sam_gov"]["enabled"] is False and "api.data.gov" in rows["sam_gov"]["requirements"]
    assert "RFP_RSS_FEED_URLS" in rows["private_rfp"]["requirements"]


# --------------------------------------------------------------------------- rejection-rule interaction
def test_rfp_disclaimer_does_not_auto_reject():
    """Contract-analytics RFPs routinely disclaim legal advice; that must not look like a licensing requirement."""
    assert _licensed_reason("The contractor shall not provide legal advice or legal opinions.") is None
    assert _licensed_reason("Services exclude legal advice.") is None
    assert _licensed_reason("Bidder must provide legal advice on contract terms.") is not None
    assert _licensed_reason("Work must be performed by a licensed attorney.") is not None
    assert _licensed_reason("No legal advice is given, but a licensed attorney must sign off.") is not None


def test_a_real_rfp_survives_the_pre_rules(db):
    """End to end: a plausible contract-AI notice is scored, stored and not auto-rejected before scoring."""
    from app.models import Opportunity
    from app.pipeline.rejection import pre_rules
    from app.settings_service import Thresholds
    from app.normalizer import upsert_opportunity

    item = normalize("sam_gov", {
        "external_id": "real-1", "title": "Contract Abstraction and Obligation Extraction Services",
        "buyer_type": "government",
        "description": ("The Government seeks a contractor to perform contract abstraction and obligation "
                        "extraction across approximately 25,000 agreements using natural language processing. "
                        "The contractor shall not provide legal advice. Estimated value $250,000."),
    })
    assert score_opportunity(item).score >= 3
    opp, created = upsert_opportunity(db, item)
    db.commit()
    assert created
    result = pre_rules(db.get(Opportunity, opp.id), Thresholds(db))
    assert not result.rejected, result.reasons
