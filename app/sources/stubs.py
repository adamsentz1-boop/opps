"""Placeholder adapters for sources that need official API access or credentials.

(SAM.gov has a real adapter in `app/sources/sam_gov.py`.)

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


class CanadaBuysSource(_StubSource):
    source_name = "canadabuys"
    display_name = "CanadaBuys (Government of Canada tenders)"
    _requirements = (
        "CanadaBuys publishes open tender notices as daily CSV/XML downloads and an OCDS feed on open.canada.ca. "
        "Enable by pointing GenericJSONSource at the permitted export, or implement an adapter against the "
        "documented open-data endpoint. Bidding requires a Procurement Business Number (PBN)."
    )


class AusTenderSource(_StubSource):
    source_name = "austender"
    display_name = "AusTender (Australian Government)"
    _requirements = (
        "AusTender publishes ATM (approach to market) notices as public RSS/XML feeds and downloadable datasets. "
        "Enable via RSS_FEED_URLS with the ATM feed, or implement an adapter against the documented dataset. "
        "Bidding requires an ABN and, for many agencies, a panel arrangement."
    )


class UNGMSource(_StubSource):
    source_name = "ungm"
    display_name = "UNGM / UN agencies"
    _requirements = (
        "The UN Global Marketplace publishes tender notices publicly; programmatic access requires a registered "
        "vendor account and, for some agencies, a subscription. Register the entity first, then implement an "
        "adapter against the account's permitted export. Do not scrape."
    )


class WorldBankSource(_StubSource):
    source_name = "world_bank"
    display_name = "World Bank / development bank procurement"
    _requirements = (
        "The World Bank Projects & Operations API and the procurement notices dataset are public. Implement an "
        "adapter against the documented API, or use GenericJSONSource. Bidding follows the borrower country's "
        "process, not the Bank's."
    )


STUB_SOURCES: list[type[_StubSource]] = [UpworkSource, PennsylvaniaProcurementSource, PrivateRFPFeedSource,
                                         CanadaBuysSource, AusTenderSource, UNGMSource, WorldBankSource]
