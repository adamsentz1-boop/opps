"""Automatic Rejection Engine.

Two stages:
1. `pre_rules(opp, thresholds)` - cheap deterministic rules that run BEFORE any Claude call
   (saves API spend on obvious junk).
2. `post_rules(analysis, thresholds)` - threshold checks on the qualification scores.

Every reason is recorded on the opportunity so the owner can audit why something was dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import Opportunity, OpportunityAnalysis
from app.settings_service import Thresholds


@dataclass
class RejectionResult:
    rejected: bool
    reasons: list[str]
    stage: str


_PHYSICAL = re.compile(r"\b(on[- ]?site|in[- ]person|physical(ly)? (install|present|visit)|warehouse|forklift|"
                       r"hardware install|cabling|drive to|travel to (our|the) (office|site)|hands[- ]on site)\b", re.I)
_LICENSED = re.compile(r"\b(licensed (attorney|lawyer|cpa|engineer|electrician|plumber|contractor|physician|nurse)|"
                       r"bar[- ]admitted|legal advice|medical advice|legal opinion|notariz|pe stamp|stamped drawings|"
                       r"must be a (licensed|registered|certified) (professional|attorney|cpa|physician))\b", re.I)
_TOS = re.compile(r"\b(fake reviews?|bot (farm|network)|mass (dm|messag|email)|spam(ming)?|bypass (captcha|2fa|"
                  r"verification|rate limit)|scrape (linkedin|instagram|facebook|tiktok) (profiles|users|dms)|"
                  r"buy (followers|likes)|account (farming|creation bot)|cookie stuffing|click fraud|"
                  r"ad fraud|multiple accounts to)\b", re.I)
_FULLTIME = re.compile(r"\b(full[- ]time (employee|position|role|hire)|w-?2 (only|employee)|"
                       r"40 hours? (a|per) week|9[- ]?to[- ]?5|relocat(e|ion) required)\b", re.I)
_ADULT = re.compile(r"\b(adult content|onlyfans|escort|gambling site|casino affiliate)\b", re.I)


def pre_rules(opp: Opportunity, t: Thresholds) -> RejectionResult:
    reasons: list[str] = []
    text = f"{opp.title}\n{opp.description}\n{opp.raw_text}"

    if _PHYSICAL.search(text):
        reasons.append("Requires onsite / physical work")
    if _LICENSED.search(text):
        reasons.append("Requires a licensed professional or legal/medical advice")
    if _TOS.search(text):
        reasons.append("Likely violates platform terms or is deceptive")
    if _FULLTIME.search(text):
        reasons.append("Full-time employment, not a project")
    if _ADULT.search(text):
        reasons.append("Out-of-scope content category")

    from app.models import utcnow
    if opp.deadline and opp.deadline < utcnow():
        reasons.append(f"Response deadline passed ({opp.deadline:%Y-%m-%d})")

    best_case = opp.budget_max if opp.budget_max is not None else opp.budget_min
    if best_case is None and opp.estimated_value is not None:
        best_case = opp.estimated_value
    if best_case is not None:
        if opp.budget_type == "hourly":
            if best_case < t.minimum_hourly_budget:
                reasons.append(f"Hourly rate ${best_case:,.0f} below minimum ${t.minimum_hourly_budget:,.0f}")
        elif best_case < t.minimum_project_value:
            reasons.append(f"Budget ${best_case:,.0f} below minimum project value ${t.minimum_project_value:,.0f}")

    if len(opp.injection_flags or []) >= 3:
        reasons.append("Listing contains multiple prompt-injection patterns")

    if opp.deadline and opp.posted_at and (opp.deadline - opp.posted_at).total_seconds() < 6 * 3600:
        reasons.append("Unrealistic deadline (< 6 hours after posting)")

    return RejectionResult(bool(reasons), reasons, "rules")


def post_rules(analysis: OpportunityAnalysis, t: Thresholds) -> RejectionResult:
    reasons: list[str] = []
    if analysis.reject_recommended:
        reasons.extend(f"Agent: {r}" for r in (analysis.reject_reasons or ["recommended rejection"]))
    if analysis.opportunity_score < t.minimum_opportunity_score:
        reasons.append(f"Score {analysis.opportunity_score:.0f} below minimum {t.minimum_opportunity_score:.0f}")
    if analysis.ai_completable_percentage < t.minimum_ai_completable_percentage:
        reasons.append(f"AI completable {analysis.ai_completable_percentage}% below minimum "
                       f"{t.minimum_ai_completable_percentage:.0f}%")
    if analysis.estimated_human_hours > t.maximum_human_hours:
        reasons.append(f"Human hours {analysis.estimated_human_hours:.1f} exceed maximum {t.maximum_human_hours:.0f}")
    if analysis.expected_profit < t.minimum_expected_profit:
        reasons.append(f"Expected profit ${analysis.expected_profit:,.0f} below minimum ${t.minimum_expected_profit:,.0f}")
    if analysis.risk > t.maximum_risk_score:
        reasons.append(f"Risk {analysis.risk} exceeds maximum {t.maximum_risk_score:.0f}")
    if analysis.scope_clarity < t.minimum_scope_clarity:
        reasons.append(f"Scope clarity {analysis.scope_clarity} below minimum {t.minimum_scope_clarity:.0f}")
    if analysis.technical_fit < t.minimum_technical_fit:
        reasons.append(f"Technical fit {analysis.technical_fit} below minimum {t.minimum_technical_fit:.0f}")
    if analysis.confidence < t.minimum_confidence:
        reasons.append(f"Confidence {analysis.confidence} below minimum {t.minimum_confidence:.0f}")
    return RejectionResult(bool(reasons), reasons, "thresholds")
