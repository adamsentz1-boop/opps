"""Deterministic economics + OPPORTUNITY_SCORE computed from agent scores.

Weighted heavily toward profit, low human effort, high AI completion, low risk, and clear scope.
"""
from __future__ import annotations

from app.models import OpportunityAnalysis
from app.schemas import QualificationOutput

WEIGHTS = {
    "profitability": 0.24,
    "low_human_effort": 0.20,
    "ai_completable": 0.18,
    "low_risk": 0.14,
    "scope_clarity": 0.10,
    "technical_fit": 0.08,
    "buyer_quality": 0.03,
    "low_competition": 0.03,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def compute_economics(q: QualificationOutput, budget_type: str, target_rate: float) -> dict[str, float]:
    if budget_type == "hourly":
        # recommended_price is an hourly rate; total hours = human + agent hours billed
        billable_hours = max(q.estimated_human_hours + q.estimated_agent_hours, 1.0)
        revenue = q.recommended_price * billable_hours
    else:
        revenue = q.recommended_price
    expected_profit = revenue - q.estimated_api_cost - q.estimated_other_cost
    margin = expected_profit / revenue if revenue > 0 else 0.0
    expected_value = q.estimated_probability_of_win * expected_profit
    human_hours = max(q.estimated_human_hours, 0.25)
    return {
        "revenue": round(revenue, 2),
        "expected_profit": round(expected_profit, 2),
        "expected_margin": round(max(min(margin, 1.0), -1.0), 4),
        "expected_value": round(expected_value, 2),
        "profit_per_human_hour": round(expected_profit / human_hours, 2),
    }


def compute_score(q: QualificationOutput, profit_per_human_hour: float, target_rate: float) -> float:
    # Profitability blends the agent's view with the hard economics vs the owner's target rate.
    econ_profitability = max(0.0, min(100.0, 100.0 * profit_per_human_hour / max(target_rate * 2, 1)))
    profitability = 0.5 * q.profitability + 0.5 * econ_profitability
    components = {
        "profitability": profitability,
        "low_human_effort": 100 - q.human_effort,
        "ai_completable": q.ai_completable_percentage,
        "low_risk": 100 - q.risk,
        "scope_clarity": q.scope_clarity,
        "technical_fit": q.technical_fit,
        "buyer_quality": q.buyer_quality,
        "low_competition": 100 - q.competition,
    }
    score = sum(WEIGHTS[k] * v for k, v in components.items())
    # Low confidence pulls the score toward the middle rather than rewarding guesses.
    confidence_factor = 0.6 + 0.4 * (q.confidence / 100)
    score = 50 + (score - 50) * confidence_factor
    if q.injection_detected:
        score -= 15
    if q.reject_recommended:
        score = min(score, 40.0)
    return round(max(0.0, min(100.0, score)), 1)


def apply_to_analysis(analysis: OpportunityAnalysis, q: QualificationOutput, budget_type: str,
                      target_rate: float, model: str) -> OpportunityAnalysis:
    econ = compute_economics(q, budget_type, target_rate)
    for field in ("technical_fit", "ai_completable_percentage", "human_effort", "profitability", "scope_clarity",
                  "buyer_quality", "risk", "competition", "confidence", "estimated_human_hours",
                  "estimated_agent_hours", "estimated_api_cost", "estimated_other_cost", "recommended_price",
                  "estimated_probability_of_win", "category", "summary", "reasoning", "red_flags",
                  "reject_recommended", "reject_reasons"):
        setattr(analysis, field, getattr(q, field))
    analysis.expected_profit = econ["expected_profit"]
    analysis.expected_margin = econ["expected_margin"]
    analysis.expected_value = econ["expected_value"]
    analysis.profit_per_human_hour = econ["profit_per_human_hour"]
    analysis.opportunity_score = compute_score(q, econ["profit_per_human_hour"], target_rate)
    analysis.raw_response = q.model_dump()
    analysis.model = model
    return analysis
