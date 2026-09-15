"""Placeholder adapters for sources that need official API access or credentials.

None of these scrape. Each documents exactly what is required to enable it. When enabled in a later phase,
implement `fetch_new_opportunities` using the official, permitted integration and register it in the registry.
"""
from __future__ import annotations

from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource


class _StubSource(OpportunitySource):
    enabled = False
    _requirements = ""

    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        raise NotImplementedError(f"{self.display_name} is not configured. Requirements: {self.requirements}")

    @property
    def requirements(self) -> str:
        return self._requirements


class UpworkSource(_StubSource):
    source_name = "upwork"
    display_name = "Upwork"
    _requirements = (
        "Upwork forbids scraping. Enable via the official Upwork API (GraphQL) which requires an approved API "
        "key/OAuth2 app (https://www.upwork.com/developer). Job search access is limited to approved partner "
        "apps; alternatively, the owner can forward saved-search RSS feeds Upwork exposes for logged-in users "
        "to GenericRSSSource (RSS_FEED_URLS) where permitted by their terms."
    )

    def source_url(self, external_id: str) -> str | None:
        return f"https://www.upwork.com/jobs/{external_id}"


class SamGovSource(_StubSource):
    source_name = "sam_gov"
    display_name = "SAM.gov (federal contract opportunities)"
    _requirements = (
        "Official public API: https://open.gsa.gov/api/get-opportunities-public-api/ . Requires a free api.data.gov "
        "key (SAM_GOV_API_KEY) and an entity registration (UEI) before bidding. Phase 2: map notices to "
        "ComplianceRequirement rows (set-asides, NAICS, representations, insurance)."
    )

    def source_url(self, external_id: str) -> str | None:
        return f"https://sam.gov/opp/{external_id}/view"


class PennsylvaniaProcurementSource(_StubSource):
    source_name = "pa_procurement"
    display_name = "Pennsylvania eMarketplace / PA Supplier Portal"
    _requirements = (
        "PA eMarketplace (https://www.emarketplace.state.pa.us/) publishes solicitations publicly; there is no "
        "official API. Enable only via a permitted feed/export (e.g. official email alerts forwarded to a JSON "
        "inbox) or manual entry. Bidding requires PA Supplier Portal registration (vendor number)."
    )


class PrivateRFPFeedSource(_StubSource):
    source_name = "private_rfp"
    display_name = "Private RFP feeds (RFPMart, BidNet, FindRFP, etc.)"
    _requirements = (
        "Paid RFP aggregators provide email/RSS/API access to subscribers. Enable with subscriber RSS URL "
        "(RSS_FEED_URLS) or JSON export (JSON_FEED_URLS) under the aggregator's terms; do not scrape."
    )


STUB_SOURCES: list[type[_StubSource]] = [UpworkSource, SamGovSource, PennsylvaniaProcurementSource, PrivateRFPFeedSource]
