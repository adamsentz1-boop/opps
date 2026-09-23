"""Contract-AI RFP relevance scoring and source filtering.

Procurement feeds are a firehose. A SAM.gov NAICS search or a paid aggregator subscription returns
thousands of notices, almost none of which are contract-analytics work. This module scores a notice
against a taxonomy of contract-AI / legal-document-automation terms so off-domain notices are dropped
*before* they reach the database and before a single Claude token is spent on them.

Scoring is deterministic keyword matching, exactly like `app/pipeline/rejection.py`: cheap, auditable, and
explainable in the source run log. It is a coarse pre-filter, not a judgement - anything that survives still
goes through the normal rejection rules, the QualificationAgent and the owner's approval queue.

The taxonomy is the kind of work eBrevia, Kira, Luminance and similar contract-analytics tools are bought
for: contract abstraction, clause and obligation extraction, lease abstraction, due-diligence document
review, repapering/remediation projects, and the document-AI plumbing around them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource

# Weights: 3 = unambiguous contract-AI work, 2 = strong document-AI signal, 1 = context only.
CORE_TERMS: tuple[str, ...] = (
    "contract analytics", "contract analysis", "contract abstraction", "contract review",
    "contract data extraction", "contract intelligence", "contract digitization", "contract digitisation",
    "contract lifecycle management", "contract repository", "contract migration", "contract remediation",
    "contract repapering", "repapering",
    "lease abstraction", "lease administration", "lease data extraction",
    "clause extraction", "clause library", "obligation extraction", "obligation management",
    "due diligence review", "due diligence document", "diligence document review",
    "document review", "e-discovery", "ediscovery", "electronic discovery",
    "legal document automation", "legal document review", "legal artificial intelligence",
    "agreement abstraction", "agreement review", "metadata extraction",
)
SUPPORTING_TERMS: tuple[str, ...] = (
    "natural language processing", "machine learning", "artificial intelligence",
    "optical character recognition", "document classification", "document categorization",
    "document categorisation", "document processing", "document extraction", "data extraction",
    "unstructured data", "text analytics", "text mining", "entity extraction", "information extraction",
    "redaction", "personally identifiable information", "records management", "information governance",
    "taxonomy development", "data migration", "digitization", "digitisation",
)
CONTEXT_TERMS: tuple[str, ...] = (
    "general counsel", "in-house counsel", "law firm", "legal department", "legal operations",
    "master service agreement", "master services agreement", "statement of work", "non-disclosure agreement",
    "supplier agreement", "vendor agreement", "procurement contract", "commercial agreement",
    "regulatory review", "compliance review", "contract compliance",
)
# Matched case-sensitively so they do not fire inside ordinary words.
ACRONYMS: dict[str, int] = {"CLM": 3, "NLP": 2, "OCR": 2, "PII": 2, "NDA": 1, "MSA": 1, "SOW": 1}

# Notices that use domain words but are plainly a different kind of contract (staffing, physical services).
EXCLUSION_TERMS: tuple[str, ...] = (
    "janitorial", "custodial", "landscaping", "food service", "security guard", "armed guard",
    "construction contract", "roofing", "hvac", "snow removal", "courier service", "temporary staffing",
    "staffing agency", "nurse staffing", "medical transcription", "pest control", "uniform rental",
)

WEIGHTS = {"core": 3, "supporting": 2, "context": 1}


def _pattern(term: str) -> re.Pattern[str]:
    """Word-boundary matcher tolerant of hyphen/space/no-space variants (e-discovery, ediscovery)."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term) if p]
    return re.compile(r"\b" + r"[\s\-]?".join(parts) + r"\b", re.I)


_CORE_RES = [(t, _pattern(t)) for t in CORE_TERMS]
_SUPPORTING_RES = [(t, _pattern(t)) for t in SUPPORTING_TERMS]
_CONTEXT_RES = [(t, _pattern(t)) for t in CONTEXT_TERMS]
_ACRONYM_RES = [(a, re.compile(r"\b" + a + r"\b"), w) for a, w in ACRONYMS.items()]
_EXCLUSION_RES = [(t, _pattern(t)) for t in EXCLUSION_TERMS]


@dataclass
class RelevanceResult:
    """Why a notice was kept or dropped. Stored on the opportunity so the decision is auditable."""
    score: int
    matched: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)

    @property
    def relevant(self) -> bool:
        return self.score > 0

    def as_dict(self) -> dict:
        return {"score": self.score, "matched_terms": self.matched, "exclusions": self.excluded,
                "taxonomy": "contract_ai"}


def score_relevance(text: str, extra_core_terms: list[str] | None = None) -> RelevanceResult:
    """Score free text against the contract-AI taxonomy.

    Each distinct term counts once at its weight. Exclusion terms zero the score outright: a janitorial
    services contract that happens to say "document management" is not contract-analytics work.
    """
    text = text or ""
    matched: list[str] = []
    excluded = [term for term, pattern in _EXCLUSION_RES if pattern.search(text)]
    score = 0
    for term, pattern in _CORE_RES:
        if pattern.search(text):
            matched.append(term)
            score += WEIGHTS["core"]
    for term in extra_core_terms or []:
        if _pattern(term).search(text) and term not in matched:
            matched.append(term)
            score += WEIGHTS["core"]
    for term, pattern in _SUPPORTING_RES:
        if pattern.search(text):
            matched.append(term)
            score += WEIGHTS["supporting"]
    for term, pattern in _CONTEXT_RES:
        if pattern.search(text):
            matched.append(term)
            score += WEIGHTS["context"]
    for acronym, pattern, weight in _ACRONYM_RES:
        if pattern.search(text):
            matched.append(acronym)
            score += weight
    if excluded:
        score = 0
    return RelevanceResult(score=score, matched=sorted(matched), excluded=sorted(excluded))


def score_opportunity(item: NormalizedOpportunity, extra_core_terms: list[str] | None = None) -> RelevanceResult:
    text = "\n".join(filter(None, [item.title, item.description, item.raw_text,
                                   " ".join(item.required_skills or [])]))
    return score_relevance(text, extra_core_terms)


class ContractAIRelevanceFilter(OpportunitySource):
    """Wraps any source and keeps only notices that score at or above `min_score`.

    The wrapper is transparent: it forwards the inner source's name, display name and URLs so source runs,
    the dashboard and de-duplication behave exactly as if the inner source were registered directly. The
    number of notices dropped is reported through `status_note()` and lands in the source run log.
    """

    def __init__(self, inner: OpportunitySource, min_score: int = 3, extra_core_terms: list[str] | None = None):
        self.inner = inner
        self.min_score = max(1, int(min_score))
        self.extra_core_terms = extra_core_terms or []
        self.source_name = inner.source_name
        self.display_name = f"{inner.display_name} (contract-AI filtered)"
        self._kept = 0
        self._dropped = 0

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return self.inner.enabled

    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        items = self.inner.fetch_new_opportunities()
        kept: list[NormalizedOpportunity] = []
        dropped = 0
        for item in items:
            result = score_opportunity(item, self.extra_core_terms)
            if result.score < self.min_score:
                dropped += 1
                continue
            item.raw_payload = {**(item.raw_payload or {}), "rfp_relevance": result.as_dict()}
            kept.append(item)
        self._kept, self._dropped = len(kept), dropped
        return kept

    def fetch_opportunity_details(self, external_id: str) -> NormalizedOpportunity | None:
        return self.inner.fetch_opportunity_details(external_id)

    def source_url(self, external_id: str) -> str | None:
        return self.inner.source_url(external_id)

    def status_note(self) -> str | None:
        inner_note = self.inner.status_note() or ""
        summary = (f"contract-AI filter: kept {self._kept}, dropped {self._dropped} "
                   f"below relevance {self.min_score}")
        return f"{inner_note} · {summary}".strip(" ·")

    @property
    def requirements(self) -> str:
        return self.inner.requirements
