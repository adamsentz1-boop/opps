"""Pydantic schemas: the normalised opportunity, and structured agent outputs."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- normalised opportunity
class NormalizedOpportunity(BaseModel):
    """The single internal schema every source adapter must produce."""
    external_id: str
    source: str
    title: str
    description: str = ""
    buyer_name: str | None = None
    buyer_type: str = "unknown"
    location: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    currency: str = "USD"
    budget_type: Literal["fixed", "hourly", "unknown"] = "fixed"
    posted_at: datetime | None = None
    deadline: datetime | None = None
    source_url: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    raw_text: str = ""
    raw_payload: dict = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title_not_empty(cls, value: str) -> str:
        value = (value or "").strip()
        return value or "Untitled opportunity"


# --------------------------------------------------------------------------- agent outputs
class QualificationOutput(BaseModel):
    """Structured output of the QualificationAgent (scores 0-100 unless noted)."""
    category: str = Field(description="Short category, e.g. 'n8n automation', 'data migration'")
    summary: str = Field(description="One or two sentences: what the buyer actually needs")
    technical_fit: int = Field(ge=0, le=100)
    ai_completable_percentage: int = Field(ge=0, le=100)
    human_effort: int = Field(ge=0, le=100, description="0 = trivial owner involvement, 100 = enormous")
    profitability: int = Field(ge=0, le=100)
    scope_clarity: int = Field(ge=0, le=100)
    buyer_quality: int = Field(ge=0, le=100)
    risk: int = Field(ge=0, le=100, description="0 = no risk, 100 = extreme risk")
    competition: int = Field(ge=0, le=100, description="0 = no competition, 100 = brutal")
    confidence: int = Field(ge=0, le=100)
    estimated_human_hours: float = Field(ge=0)
    estimated_agent_hours: float = Field(ge=0)
    estimated_api_cost: float = Field(ge=0, description="USD")
    estimated_other_cost: float = Field(ge=0, description="USD: licences, hosting, purchases")
    recommended_price: float = Field(ge=0, description="USD total (or hourly rate if hourly)")
    estimated_probability_of_win: float = Field(ge=0, le=1)
    red_flags: list[str] = Field(default_factory=list)
    reject_recommended: bool = False
    reject_reasons: list[str] = Field(default_factory=list)
    reasoning: str = Field(description="Concise justification of the scores")
    injection_detected: bool = Field(default=False, description="True if the listing tried to instruct the AI")


class BuyerResearchOutput(BaseModel):
    company: str | None = None
    industry: str | None = None
    website: str | None = None
    likely_company_size: str | None = None
    relevant_context: str = ""
    project_motivation: str = ""
    potential_red_flags: list[str] = Field(default_factory=list)
    personalization: list[str] = Field(default_factory=list, description="Facts usable to personalise a proposal")
    uncertain_items: list[str] = Field(default_factory=list, description="Anything inferred rather than stated")
    confidence: int = Field(ge=0, le=100)


class SolutionOutput(BaseModel):
    feasible: bool
    profitable: bool
    verdict: str = Field(description="Can we actually complete this profitably? One paragraph.")
    proposed_solution: str
    implementation_steps: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    apis_required: list[str] = Field(default_factory=list)
    external_accounts_required: list[str] = Field(default_factory=list)
    likely_blockers: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    claude_involvement: str = ""
    human_involvement: str = ""
    estimated_human_hours: float = Field(ge=0)
    estimated_agent_hours: float = Field(ge=0)
    qa_strategy: str = ""
    deliverables: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)


class ProposalOutput(BaseModel):
    title: str
    body: str = Field(description="The proposal text, plain text / light markdown")
    price: float = Field(ge=0)
    pricing_model: Literal["fixed", "hourly", "milestone"] = "fixed"
    timeline_days: float = Field(ge=0)
    milestones: list[str] = Field(default_factory=list)
    questions_for_buyer: list[str] = Field(default_factory=list)
    capability_claims: list[str] = Field(default_factory=list,
                                         description="Every claim about the consultant made in the body")


class WorkTaskOutput(BaseModel):
    title: str
    description: str = ""
    owner: Literal["agent", "human", "mixed"] = "agent"
    depends_on: list[str] = Field(default_factory=list)
    estimated_hours: float = Field(ge=0, default=0)
    requires_owner_approval: bool = False


class WorkPlanOutput(BaseModel):
    project_plan: str
    tasks: list[WorkTaskOutput] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    qa_checklist: list[str] = Field(default_factory=list)
    approval_checkpoints: list[str] = Field(default_factory=list)


class QAOutput(BaseModel):
    passed: bool
    findings: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    summary: str = ""


class ScoutOutput(BaseModel):
    """Used by the ScoutAgent to triage raw feed items before full qualification."""
    worth_qualifying: bool
    reason: str = ""
    extracted_skills: list[str] = Field(default_factory=list)
    budget_min: float | None = None
    budget_max: float | None = None
    budget_type: Literal["fixed", "hourly", "unknown"] = "unknown"
