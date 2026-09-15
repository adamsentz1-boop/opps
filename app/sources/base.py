"""Common interface every opportunity source adapter implements."""
from __future__ import annotations

import abc

from app.schemas import NormalizedOpportunity


class SourceError(RuntimeError):
    pass


class OpportunitySource(abc.ABC):
    """A source of opportunities (marketplace, feed, procurement portal, manual entry)."""

    #: machine name stored on every opportunity from this source
    source_name: str = "base"
    #: human description shown in the dashboard
    display_name: str = "Base source"
    #: whether the adapter can actually fetch anything right now
    enabled: bool = True

    @abc.abstractmethod
    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        """Return newly discovered opportunities in the normalised schema (may include duplicates;
        the normaliser de-duplicates on (source, external_id))."""

    def fetch_opportunity_details(self, external_id: str) -> NormalizedOpportunity | None:
        """Fetch full details for one opportunity. Default: not supported."""
        return None

    def source_url(self, external_id: str) -> str | None:
        """Canonical URL for an opportunity on this source."""
        return None

    def status_note(self) -> str | None:
        """Free-text status shown in the source run log."""
        return None

    @property
    def requirements(self) -> str:
        """What would be needed to enable this source (used for stubs)."""
        return ""
