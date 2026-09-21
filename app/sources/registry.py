"""Builds the list of active sources from configuration."""
from __future__ import annotations

from app.config import Settings, get_settings
from app.sources.base import OpportunitySource
from app.sources.json_source import GenericJSONSource
from app.sources.manual import ManualSource
from app.sources.rfp import ContractAIRelevanceFilter
from app.sources.rss import GenericRSSSource, _slug
from app.sources.sam_gov import SamGovSource
from app.sources.stubs import STUB_SOURCES

_manual_source: ManualSource | None = None


def get_manual_source() -> ManualSource:
    global _manual_source
    if _manual_source is None:
        _manual_source = ManualSource()
    return _manual_source


def _maybe_filter(source: OpportunitySource, settings: Settings) -> OpportunitySource:
    """Wrap an RFP source in the contract-AI relevance filter unless the owner turned it off."""
    if not settings.rfp_filter_enabled:
        return source
    return ContractAIRelevanceFilter(source, min_score=settings.rfp_min_relevance,
                                     extra_core_terms=settings.rfp_extra_term_list)


def get_rfp_sources(settings: Settings | None = None) -> list[OpportunitySource]:
    """Contract-AI RFP intake: subscriber feeds plus SAM.gov, each relevance-filtered.

    These are kept separate from the ordinary freelance feeds so the filter only applies where it should.
    """
    settings = settings or get_settings()
    sources: list[OpportunitySource] = []
    sources += [_maybe_filter(GenericRSSSource(url, source_name=f"rfp-rss:{_slug(url)}"), settings)
                for url in settings.rfp_rss_feeds]
    sources += [_maybe_filter(GenericJSONSource(url, source_name=f"rfp-json:{_slug(url)}"), settings)
                for url in settings.rfp_json_feeds]
    if settings.sam_gov_enabled:
        sources.append(_maybe_filter(SamGovSource(), settings))
    return sources


def get_sources(include_stubs: bool = False) -> list[OpportunitySource]:
    settings = get_settings()
    sources: list[OpportunitySource] = [get_manual_source()]
    sources += [GenericRSSSource(url) for url in settings.rss_feeds]
    sources += [GenericJSONSource(url) for url in settings.json_feeds]
    sources += get_rfp_sources(settings)
    if include_stubs:
        sources += [cls() for cls in STUB_SOURCES]
        if not settings.sam_gov_enabled:
            # not scanned, but the sources page should say what enabling it needs
            sources.append(SamGovSource())
    return sources


def describe_sources() -> list[dict]:
    rows = []
    for src in get_sources(include_stubs=True):
        rows.append({"name": src.source_name, "display_name": src.display_name, "enabled": src.enabled,
                     "requirements": src.requirements, "note": src.status_note()})
    return rows
