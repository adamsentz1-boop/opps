"""Builds the list of active sources from configuration."""
from __future__ import annotations

from app.config import get_settings
from app.sources.base import OpportunitySource
from app.sources.json_source import GenericJSONSource
from app.sources.manual import ManualSource
from app.sources.rss import GenericRSSSource
from app.sources.stubs import STUB_SOURCES

_manual_source: ManualSource | None = None


def get_manual_source() -> ManualSource:
    global _manual_source
    if _manual_source is None:
        _manual_source = ManualSource()
    return _manual_source


def get_sources(include_stubs: bool = False) -> list[OpportunitySource]:
    settings = get_settings()
    sources: list[OpportunitySource] = [get_manual_source()]
    sources += [GenericRSSSource(url) for url in settings.rss_feeds]
    sources += [GenericJSONSource(url) for url in settings.json_feeds]
    if include_stubs:
        sources += [cls() for cls in STUB_SOURCES]
    return sources


def describe_sources() -> list[dict]:
    rows = []
    for src in get_sources(include_stubs=True):
        rows.append({"name": src.source_name, "display_name": src.display_name, "enabled": src.enabled,
                     "requirements": src.requirements, "note": src.status_note()})
    return rows
