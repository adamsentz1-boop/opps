"""Profile modes: who is bidding.

`solo`   - an independent consultant selling their own delivery time (the original Phase 1 model).
`vendor` - an organisation selling a product/platform (e.g. legal-technology software) into RFPs and tenders.

The same Opportunity/Analysis tables serve both; only the framing, labels, thresholds and prompt addendum
change. This keeps one pipeline, one dashboard and one audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProfileMode:
    key: str
    label: str
    #: what estimated_human_hours means in this mode
    effort_label: str
    effort_unit: str
    #: what technical_fit / ai_completable_percentage mean
    fit_label: str
    automation_label: str
    price_label: str
    prompt_file: str
    headline: str


SOLO = ProfileMode(
    key="solo", label="Solo consultant (services)",
    effort_label="Your hours", effort_unit="h",
    fit_label="Technical fit", automation_label="AI completable",
    price_label="Recommended bid", prompt_file="_profile_solo",
    headline="Find work AI can do with minimal personal time.",
)

VENDOR = ProfileMode(
    key="vendor", label="Vendor / product company",
    effort_label="Internal effort", effort_unit="h",
    fit_label="Product fit", automation_label="Met by product",
    price_label="Proposed contract value", prompt_file="_profile_vendor",
    headline="Find RFPs our product can win with a proportionate bid effort.",
)

MODES = {m.key: m for m in (SOLO, VENDOR)}


def get_mode(key: str | None) -> ProfileMode:
    return MODES.get((key or "solo").strip().lower(), SOLO)
