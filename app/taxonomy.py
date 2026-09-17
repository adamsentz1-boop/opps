"""Legal-technology classification.

Worldwide tender feeds are enormous, so every opportunity is classified with cheap local keyword matching
BEFORE any Claude call. Only plausible legal-technology opportunities reach the agents.

The code lists (CPV / NAICS / UNSPSC) are defaults for querying and hinting only - they are editable in
Settings because official code lists change and each portal tags inconsistently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- categories
# Each category: (key, label, [keywords]). Keywords are matched case-insensitively on word boundaries.
CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("contract_analytics", "Contract analytics & abstraction", [
        "contract analytics", "contract analysis", "contract abstraction", "contract review",
        "clause extraction", "clause library", "contract data extraction", "lease abstraction",
        "document abstraction", "due diligence review", "diligence review", "contract intelligence",
        "obligation extraction", "contract data capture", "metadata extraction"]),
    ("clm", "Contract lifecycle management", [
        "contract lifecycle", "contract life cycle", "clm", "contract management system",
        "contract management software", "contract repository", "contract authoring", "contract drafting",
        "e-signature", "esignature", "electronic signature", "contract workflow"]),
    ("ediscovery", "eDiscovery & litigation support", [
        "ediscovery", "e-discovery", "electronic discovery", "litigation support", "litigation hold",
        "legal hold", "technology assisted review", "predictive coding", "early case assessment",
        "forensic collection", "processing and review", "relativity", "trial presentation"]),
    ("document_review", "Document review & redaction", [
        "document review", "privilege review", "responsiveness review", "redaction", "redacting",
        "anonymisation", "anonymization", "de-identification", "bulk review", "disclosure review"]),
    ("legal_research", "Legal research", [
        "legal research", "case law", "caselaw", "statutory research", "legislation database",
        "jurisprudence", "legal database", "law library system"]),
    ("matter_management", "Matter & legal spend management", [
        "matter management", "legal case management", "enterprise legal management", "legal spend",
        "e-billing", "ebilling", "outside counsel management", "legal operations", "legal workflow",
        "legal service delivery"]),
    ("court_case", "Court & case management systems", [
        "court case management", "judicial case management", "case management system", "e-filing",
        "efiling", "electronic filing", "docketing", "docket management", "tribunal system",
        "prosecution case management", "court records"]),
    ("ip", "IP & patent management", [
        "ip management", "patent docketing", "trademark management", "intellectual property management",
        "patent portfolio", "ip portfolio", "patent analytics"]),
    ("regtech", "Regulatory compliance & obligations", [
        "regulatory compliance software", "compliance management system", "obligations management",
        "regulatory change management", "policy management system", "regtech", "compliance monitoring",
        "third party risk management", "vendor risk management"]),
    ("records", "Records, information governance & FOI", [
        "records management", "information governance", "retention schedule", "records retention",
        "freedom of information", "foia", "public records request", "access to information request",
        "archiving system", "electronic document and records management", "edrms"]),
    ("privacy", "Privacy & data subject requests", [
        "data subject access request", "dsar", "subject access request", "privacy management",
        "gdpr compliance", "consent management", "data mapping", "privacy impact assessment"]),
    ("legal_ai", "Legal AI & document intelligence", [
        "legal ai", "artificial intelligence for legal", "ai contract", "ai-powered contract",
        "machine learning contract", "natural language processing", "large language model",
        "generative ai", "document intelligence", "intelligent document processing", "ocr",
        "optical character recognition", "text analytics", "entity extraction"]),
]

CATEGORY_LABELS = {key: label for key, label, _ in CATEGORIES}

# Buyer-side signals: a legal buyer makes a generic "document management" tender far more relevant.
LEGAL_BUYER_SIGNALS = [
    "ministry of justice", "department of justice", "attorney general", "general counsel", "legal department",
    "law firm", "judiciary", "judicial", "court", "courts service", "tribunal", "prosecutor", "public defender",
    "bar association", "law society", "solicitor", "legal services commission", "ombudsman", "legal aid",
    "compliance department", "company secretary", "corporate legal",
]

# Legal SERVICES (licensed professional work) rather than legal TECHNOLOGY. These are not a fit for a
# software vendor and are rejected unless technology terms also appear.
LEGAL_SERVICES_ONLY = [
    "legal advice", "legal representation", "legal counsel services", "panel of law firms", "outside counsel services",
    "barrister", "solicitors services", "notary", "conveyancing", "litigation services", "legal opinion",
    "advocacy services", "court reporting services", "translation of legal documents", "interpreting services",
    "process serving", "bailiff",
]

# Editable defaults (Settings): used to build portal queries and to boost classification confidence.
DEFAULT_CPV_CODES = [
    "48000000",  # Software package and information systems
    "48100000",  # Industry specific software package
    "48311000",  # Document management software package
    "48329000",  # Imaging and archiving system
    "48613000",  # Electronic data management (EDM)
    "48810000",  # Information systems
    "72000000",  # IT services
    "72212000",  # Programming services of application software
    "72310000",  # Data processing services
    "72316000",  # Data analysis services
    "72322000",  # Data management services
    "79100000",  # Legal services
    "79140000",  # Legal advisory and information services
]
DEFAULT_NAICS_CODES = ["513210", "541511", "541512", "541519", "541110", "541199"]
DEFAULT_KEYWORD_QUERIES = [
    "contract management", "contract analytics", "eDiscovery", "document review", "legal technology",
    "case management system", "records management", "artificial intelligence", "document management",
]


@dataclass
class Classification:
    is_legal_tech: bool
    category: str | None
    category_label: str | None
    relevance: int                      # 0-100, keyword-derived only (no LLM)
    matched_terms: list[str] = field(default_factory=list)
    buyer_signals: list[str] = field(default_factory=list)
    services_only: bool = False
    reasons: list[str] = field(default_factory=list)


def _matches(text: str, terms: list[str]) -> list[str]:
    found = []
    for term in terms:
        pattern = r"\b" + re.escape(term).replace(r"\ ", r"[\s\-]+") + r"\b"
        if re.search(pattern, text, re.I):
            found.append(term)
    return found


def classify(title: str, description: str = "", codes: list[str] | None = None,
             buyer_name: str = "", extra_text: str = "",
             cpv_codes: list[str] | None = None) -> Classification:
    """Classify an opportunity as legal technology using local keyword/code matching only."""
    text = " ".join(x for x in [title, description, extra_text] if x)
    buyer_text = " ".join(x for x in [buyer_name, title] if x)
    cpv_codes = cpv_codes or DEFAULT_CPV_CODES

    scores: dict[str, list[str]] = {}
    for key, _label, terms in CATEGORIES:
        hits = _matches(text, terms)
        if hits:
            scores[key] = hits

    buyer_signals = _matches(buyer_text, LEGAL_BUYER_SIGNALS)
    services_hits = _matches(text, LEGAL_SERVICES_ONLY)
    # A code match is a weak signal on its own; portals tag inconsistently.
    code_hits = [c for c in (codes or []) if any(str(c).startswith(str(cp)[:4]) for cp in cpv_codes)]

    if not scores:
        reasons = ["No legal-technology terms found in the notice"]
        if buyer_signals and code_hits:
            # legal buyer + a software/IT code: worth a look even without explicit terms
            return Classification(True, "legal_ai", CATEGORY_LABELS["legal_ai"], 35, [], buyer_signals, bool(services_hits),
                                  ["Legal buyer with a software/IT classification code, but no explicit legal-tech terms"])
        return Classification(False, None, None, 0, [], buyer_signals, bool(services_hits), reasons)

    # strongest category = most keyword hits
    best = max(scores.items(), key=lambda kv: len(kv[1]))
    matched = sorted({t for hits in scores.values() for t in hits})
    relevance = min(100, 30 + 12 * len(matched) + (15 if buyer_signals else 0) + (10 if code_hits else 0))

    reasons = [f"Matched {len(matched)} legal-tech term(s): {', '.join(matched[:6])}"]
    if buyer_signals:
        reasons.append(f"Legal buyer signal: {', '.join(buyer_signals[:3])}")
    if code_hits:
        reasons.append(f"Classification code match: {', '.join(code_hits[:3])}")

    services_only = bool(services_hits) and len(services_hits) > len(matched)
    if services_only:
        relevance = max(0, relevance - 40)
        reasons.append(f"Looks like legal services rather than technology: {', '.join(services_hits[:3])}")

    return Classification(not services_only, best[0], CATEGORY_LABELS[best[0]], relevance, matched,
                          buyer_signals, services_only, reasons)


def category_choices() -> list[tuple[str, str]]:
    return [(key, label) for key, label, _ in CATEGORIES]
