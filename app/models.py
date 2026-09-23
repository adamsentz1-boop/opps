"""SQLAlchemy models for the Opportunity Engine.

Tables: opportunities, opportunity_analysis, solution_plans, buyers, proposals,
approvals, source_runs, agent_runs, work_orders, work_tasks, deliverables,
compliance_requirements, audit_log, settings, notifications.

Market Challenge tables: trading_challenges, market_positions, trade_proposals, trade_executions,
market_snapshots, portfolio_snapshots, market_watchlist. The market module never talks to a broker:
positions and cash only change through the owner's Record Fill action (app/market/portfolio.py).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, event, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (ChallengeStatus, DeliverableStatus, OpportunityStatus, TradeProposalStatus,
                       WorkOrderStatus, WorkTaskStatus)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --------------------------------------------------------------------------- opportunities
class Opportunity(TimestampMixin, Base):
    __tablename__ = "opportunities"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_external"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    buyer_name: Mapped[str | None] = mapped_column(String(255))
    buyer_type: Mapped[str] = mapped_column(String(32), default="unknown")
    location: Mapped[str | None] = mapped_column(String(255))
    budget_min: Mapped[float | None] = mapped_column(Float)
    budget_max: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    budget_type: Mapped[str] = mapped_column(String(16), default="fixed")  # fixed | hourly | unknown
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline: Mapped[datetime | None] = mapped_column(DateTime)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    deliverables: Mapped[list] = mapped_column(JSON, default=list)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=OpportunityStatus.NEW.value, index=True)
    rejection_reasons: Mapped[list] = mapped_column(JSON, default=list)
    rejection_stage: Mapped[str | None] = mapped_column(String(32))  # rules | thresholds | solution | owner
    injection_flags: Mapped[list] = mapped_column(JSON, default=list)
    last_error: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    won_at: Mapped[datetime | None] = mapped_column(DateTime)
    won_value: Mapped[float | None] = mapped_column(Float)

    analysis: Mapped["OpportunityAnalysis | None"] = relationship(back_populates="opportunity", uselist=False,
                                                                  cascade="all, delete-orphan")
    solution_plan: Mapped["SolutionPlan | None"] = relationship(back_populates="opportunity", uselist=False,
                                                                cascade="all, delete-orphan")
    buyer: Mapped["Buyer | None"] = relationship(back_populates="opportunity", uselist=False,
                                                cascade="all, delete-orphan")
    proposals: Mapped[list["Proposal"]] = relationship(back_populates="opportunity", cascade="all, delete-orphan",
                                                       order_by="Proposal.version")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="opportunity", order_by="Approval.created_at")
    agent_runs: Mapped[list["AgentRun"]] = relationship(back_populates="opportunity")
    work_orders: Mapped[list["WorkOrder"]] = relationship(back_populates="opportunity")
    requirements: Mapped[list["ComplianceRequirement"]] = relationship(back_populates="opportunity",
                                                                       cascade="all, delete-orphan")

    @property
    def current_proposal(self) -> "Proposal | None":
        return self.proposals[-1] if self.proposals else None

    @property
    def budget_display(self) -> str:
        if self.budget_min is None and self.budget_max is None:
            return "Not stated"
        suffix = "/hr" if self.budget_type == "hourly" else ""
        if self.budget_min is not None and self.budget_max is not None and self.budget_min != self.budget_max:
            return f"${self.budget_min:,.0f} - ${self.budget_max:,.0f}{suffix}"
        value = self.budget_max if self.budget_max is not None else self.budget_min
        return f"${value:,.0f}{suffix}"


class OpportunityAnalysis(TimestampMixin, Base):
    """Qualification agent output + computed scores. One row per opportunity (overwritten on reanalyze)."""
    __tablename__ = "opportunity_analysis"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), unique=True)

    technical_fit: Mapped[int] = mapped_column(Integer, default=0)
    ai_completable_percentage: Mapped[int] = mapped_column(Integer, default=0)
    human_effort: Mapped[int] = mapped_column(Integer, default=0)      # 0 = trivial, 100 = enormous
    profitability: Mapped[int] = mapped_column(Integer, default=0)
    scope_clarity: Mapped[int] = mapped_column(Integer, default=0)
    buyer_quality: Mapped[int] = mapped_column(Integer, default=0)
    risk: Mapped[int] = mapped_column(Integer, default=0)              # 0 = none, 100 = extreme
    competition: Mapped[int] = mapped_column(Integer, default=0)       # 0 = none, 100 = brutal
    confidence: Mapped[int] = mapped_column(Integer, default=0)

    estimated_human_hours: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_agent_hours: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_api_cost: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_other_cost: Mapped[float] = mapped_column(Float, default=0.0)
    recommended_price: Mapped[float] = mapped_column(Float, default=0.0)
    expected_margin: Mapped[float] = mapped_column(Float, default=0.0)      # 0..1
    expected_profit: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_probability_of_win: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    expected_value: Mapped[float] = mapped_column(Float, default=0.0)
    profit_per_human_hour: Mapped[float] = mapped_column(Float, default=0.0)
    opportunity_score: Mapped[float] = mapped_column(Float, default=0.0)

    category: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text, default="")
    reasoning: Mapped[str] = mapped_column(Text, default="")
    red_flags: Mapped[list] = mapped_column(JSON, default=list)
    reject_recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reasons: Mapped[list] = mapped_column(JSON, default=list)
    raw_response: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(64))

    opportunity: Mapped[Opportunity] = relationship(back_populates="analysis")


class SolutionPlan(TimestampMixin, Base):
    __tablename__ = "solution_plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), unique=True)
    feasible: Mapped[bool] = mapped_column(Boolean, default=True)
    profitable: Mapped[bool] = mapped_column(Boolean, default=True)
    verdict: Mapped[str] = mapped_column(Text, default="")
    proposed_solution: Mapped[str] = mapped_column(Text, default="")
    implementation_steps: Mapped[list] = mapped_column(JSON, default=list)
    required_tools: Mapped[list] = mapped_column(JSON, default=list)
    apis_required: Mapped[list] = mapped_column(JSON, default=list)
    external_accounts_required: Mapped[list] = mapped_column(JSON, default=list)
    likely_blockers: Mapped[list] = mapped_column(JSON, default=list)
    assumptions: Mapped[list] = mapped_column(JSON, default=list)
    claude_involvement: Mapped[str] = mapped_column(Text, default="")
    human_involvement: Mapped[str] = mapped_column(Text, default="")
    estimated_human_hours: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_agent_hours: Mapped[float] = mapped_column(Float, default=0.0)
    qa_strategy: Mapped[str] = mapped_column(Text, default="")
    deliverables: Mapped[list] = mapped_column(JSON, default=list)
    risk_factors: Mapped[list] = mapped_column(JSON, default=list)
    raw_response: Mapped[dict] = mapped_column(JSON, default=dict)

    opportunity: Mapped[Opportunity] = relationship(back_populates="solution_plan")


class Buyer(TimestampMixin, Base):
    __tablename__ = "buyers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), unique=True)
    company: Mapped[str | None] = mapped_column(String(255))
    industry: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(String(500))
    likely_company_size: Mapped[str | None] = mapped_column(String(64))
    relevant_context: Mapped[str] = mapped_column(Text, default="")
    project_motivation: Mapped[str] = mapped_column(Text, default="")
    potential_red_flags: Mapped[list] = mapped_column(JSON, default=list)
    personalization: Mapped[list] = mapped_column(JSON, default=list)
    uncertain_items: Mapped[list] = mapped_column(JSON, default=list)   # explicitly-labelled guesses
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    raw_response: Mapped[dict] = mapped_column(JSON, default=dict)

    opportunity: Mapped[Opportunity] = relationship(back_populates="buyer")


class Proposal(TimestampMixin, Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    pricing_model: Mapped[str] = mapped_column(String(32), default="fixed")
    timeline_days: Mapped[float | None] = mapped_column(Float)
    milestones: Mapped[list] = mapped_column(JSON, default=list)
    questions_for_buyer: Mapped[list] = mapped_column(JSON, default=list)
    capability_claims: Mapped[list] = mapped_column(JSON, default=list)  # claims made, for audit
    edited_by_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    author: Mapped[str] = mapped_column(String(64), default="ProposalAgent")
    raw_response: Mapped[dict] = mapped_column(JSON, default=dict)

    opportunity: Mapped[Opportunity] = relationship(back_populates="proposals")


# --------------------------------------------------------------------------- approvals (immutable)
class Approval(Base):
    """Immutable record of a human decision. Rows are never updated or deleted."""
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    opportunity_id: Mapped[str | None] = mapped_column(ForeignKey("opportunities.id"), index=True)
    work_order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.id"), index=True)
    object_type: Mapped[str] = mapped_column(String(64), default="opportunity")
    object_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(32))
    decided_by: Mapped[str] = mapped_column(String(128), default="owner")
    notes: Mapped[str] = mapped_column(Text, default="")
    previous_state: Mapped[str | None] = mapped_column(String(32))
    new_state: Mapped[str | None] = mapped_column(String(32))
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)  # what exactly was approved

    opportunity: Mapped[Opportunity | None] = relationship(back_populates="approvals")


@event.listens_for(Approval, "before_update")
def _approval_immutable(_mapper, _connection, _target):  # pragma: no cover - guard
    raise RuntimeError("Approval records are immutable")


@event.listens_for(Approval, "before_delete")
def _approval_no_delete(_mapper, _connection, _target):  # pragma: no cover - guard
    raise RuntimeError("Approval records cannot be deleted")


# --------------------------------------------------------------------------- runs
class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="running")  # running|ok|error|skipped
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    opportunity_id: Mapped[str | None] = mapped_column(ForeignKey("opportunities.id"), index=True)
    work_order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.id"), index=True)
    model: Mapped[str] = mapped_column(String(64), default="mock")
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|error|refused
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    system_prompt_hash: Mapped[str | None] = mapped_column(String(64))
    request_summary: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    opportunity: Mapped[Opportunity | None] = relationship(back_populates="agent_runs")


# --------------------------------------------------------------------------- work execution
class WorkOrder(TimestampMixin, Base):
    __tablename__ = "work_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    opportunity_id: Mapped[str | None] = mapped_column(ForeignKey("opportunities.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=WorkOrderStatus.NEW.value, index=True)
    contract_value: Mapped[float] = mapped_column(Float, default=0.0)
    project_plan: Mapped[str] = mapped_column(Text, default="")
    required_inputs: Mapped[list] = mapped_column(JSON, default=list)
    qa_checklist: Mapped[list] = mapped_column(JSON, default=list)      # [{item, done}]
    approval_checkpoints: Mapped[list] = mapped_column(JSON, default=list)  # [{name, status, approval_id}]
    execution_notes: Mapped[str] = mapped_column(Text, default="")
    human_hours_logged: Mapped[float] = mapped_column(Float, default=0.0)
    invoiced_amount: Mapped[float | None] = mapped_column(Float)
    paid_amount: Mapped[float | None] = mapped_column(Float)
    invoiced_at: Mapped[datetime | None] = mapped_column(DateTime)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivery_approval_id: Mapped[str | None] = mapped_column(String(32))

    opportunity: Mapped[Opportunity | None] = relationship(back_populates="work_orders")
    tasks: Mapped[list["WorkTask"]] = relationship(back_populates="work_order", cascade="all, delete-orphan",
                                                   order_by="WorkTask.order")
    deliverables: Mapped[list["Deliverable"]] = relationship(back_populates="work_order",
                                                             cascade="all, delete-orphan")


class WorkTask(TimestampMixin, Base):
    __tablename__ = "work_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id", ondelete="CASCADE"), index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(32), default="agent")  # agent | human | mixed
    status: Mapped[str] = mapped_column(String(32), default=WorkTaskStatus.TODO.value)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)   # list of task titles/ids
    estimated_hours: Mapped[float] = mapped_column(Float, default=0.0)
    requires_owner_approval: Mapped[bool] = mapped_column(Boolean, default=False)

    work_order: Mapped[WorkOrder] = relationship(back_populates="tasks")


class Deliverable(TimestampMixin, Base):
    __tablename__ = "deliverables"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(32), default=DeliverableStatus.PLANNED.value)
    qa_notes: Mapped[str] = mapped_column(Text, default="")
    approval_id: Mapped[str | None] = mapped_column(String(32))

    work_order: Mapped[WorkOrder] = relationship(back_populates="deliverables")


class ComplianceRequirement(TimestampMixin, Base):
    """Phase-2 concept: formal requirements attached to an opportunity.

    `verified` defaults to False and can ONLY be set through the owner verification
    route. Agents may create requirement rows but never mark them verified.
    """
    __tablename__ = "compliance_requirements"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), index=True)
    requirement: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(32), default="other")
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[str] = mapped_column(Text, default="")
    owner_approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    verified_by: Mapped[str | None] = mapped_column(String(128))
    verification_approval_id: Mapped[str | None] = mapped_column(String(32))
    source_agent: Mapped[str | None] = mapped_column(String(64))

    opportunity: Mapped[Opportunity] = relationship(back_populates="requirements")


# --------------------------------------------------------------------------- audit / settings / notifications
class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    previous_state: Mapped[str | None] = mapped_column(String(64))
    new_state: Mapped[str | None] = mapped_column(String(64))
    human_approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_id: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


@event.listens_for(AuditLog, "before_update")
def _audit_immutable(_mapper, _connection, _target):  # pragma: no cover - guard
    raise RuntimeError("Audit log entries are immutable")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    value_type: Mapped[str] = mapped_column(String(16), default="str")  # str|int|float|bool|text
    description: Mapped[str] = mapped_column(Text, default="")
    group: Mapped[str] = mapped_column(String(32), default="general")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    opportunity_id: Mapped[str | None] = mapped_column(String(32), index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    channel: Mapped[str] = mapped_column(String(32), default="dashboard")


# --------------------------------------------------------------------------- market challenge
class TradingChallenge(Base):
    """A personal, hypothetical/recorded portfolio challenge. Cash and P&L are a ledger of owner-recorded fills."""
    __tablename__ = "trading_challenges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), default="$200 to $1,000 Market Challenge")
    starting_cash: Mapped[float] = mapped_column(Float, default=200.0)
    target_value: Mapped[float] = mapped_column(Float, default=1000.0)
    target_date: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime(2027, 1, 1))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    cash_balance: Mapped[float] = mapped_column(Float, default=200.0)
    current_portfolio_value: Mapped[float] = mapped_column(Float, default=200.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default=ChallengeStatus.ACTIVE.value, index=True)

    positions: Mapped[list["MarketPosition"]] = relationship(back_populates="challenge",
                                                             order_by="MarketPosition.ticker")
    proposals: Mapped[list["TradeProposal"]] = relationship(back_populates="challenge",
                                                            order_by="TradeProposal.created_at")
    executions: Mapped[list["TradeExecution"]] = relationship(back_populates="challenge",
                                                              order_by="TradeExecution.executed_at")

    @property
    def positions_value(self) -> float:
        return round(sum(p.market_value or 0.0 for p in self.positions if (p.quantity or 0) > 0), 2)


class MarketPosition(Base):
    """A long-only position. Quantity is a float so fractional shares are supported."""
    __tablename__ = "market_positions"
    __table_args__ = (UniqueConstraint("challenge_id", "ticker", name="uq_position_challenge_ticker"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    challenge_id: Mapped[str] = mapped_column(ForeignKey("trading_challenges.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_cost: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    # Exit plan carried over from the proposal that opened the position. Advisory: a stop is only real
    # once the owner enters it with their broker. The scan proposes an exit when one is breached.
    stop_price: Mapped[float | None] = mapped_column(Float)
    target_price: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    challenge: Mapped[TradingChallenge] = relationship(back_populates="positions")

    @property
    def cost_basis(self) -> float:
        return round((self.quantity or 0.0) * (self.average_cost or 0.0), 2)

    @property
    def unrealized_pnl_pct(self) -> float:
        basis = self.cost_basis
        return round(self.unrealized_pnl / basis * 100.0, 2) if basis else 0.0


class TradeProposal(Base):
    """A proposed trade for OWNER review. Approval never executes it; the owner trades manually and records the fill."""
    __tablename__ = "trade_proposals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    challenge_id: Mapped[str] = mapped_column(ForeignKey("trading_challenges.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))                     # BUY | SELL
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_price: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_total: Mapped[float] = mapped_column(Float, default=0.0)
    thesis: Mapped[str] = mapped_column(Text, default="")
    reason_for_trade: Mapped[str] = mapped_column(Text, default="")
    bull_case: Mapped[str] = mapped_column(Text, default="")
    bear_case: Mapped[str] = mapped_column(Text, default="")
    catalysts: Mapped[list] = mapped_column(JSON, default=list)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    time_horizon: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[int] = mapped_column(Integer, default=0)      # 0-100
    expected_upside_pct: Mapped[float] = mapped_column(Float, default=0.0)
    expected_downside_pct: Mapped[float] = mapped_column(Float, default=0.0)
    risk_reward_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    # Exit plan and the measured statistics behind it (app/market/risk.py, app/market/indicators.py).
    stop_price: Mapped[float | None] = mapped_column(Float)
    target_price: Mapped[float | None] = mapped_column(Float)
    # server_default lets these be added to a database that already has this table (see db.ensure_columns)
    risk_amount: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    exit_plan: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    price_stats: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    portfolio_before: Mapped[dict] = mapped_column(JSON, default=dict)
    portfolio_after: Mapped[dict] = mapped_column(JSON, default=dict)
    market_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    research: Mapped[dict] = mapped_column(JSON, default=dict)      # MarketResearchAgent output used
    status: Mapped[str] = mapped_column(String(32), default=TradeProposalStatus.PROPOSED.value, index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="PortfolioAgent")
    edited_by_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    approval_id: Mapped[str | None] = mapped_column(String(32))     # approval row for APPROVE/REJECT

    challenge: Mapped[TradingChallenge] = relationship(back_populates="proposals")
    executions: Mapped[list["TradeExecution"]] = relationship(back_populates="proposal",
                                                              order_by="TradeExecution.executed_at")

    @property
    def is_active(self) -> bool:
        return self.status in TradeProposalStatus.active()


class TradeExecution(Base):
    """A fill the OWNER executed outside Opportunity Engine and then recorded here. Immutable."""
    __tablename__ = "trade_executions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    trade_proposal_id: Mapped[str] = mapped_column(ForeignKey("trade_proposals.id"), index=True)
    challenge_id: Mapped[str] = mapped_column(ForeignKey("trading_challenges.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    total_value: Mapped[float] = mapped_column(Float)               # cash out (BUY, incl. fees) / cash in (SELL, net)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # SELL only
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    entered_by: Mapped[str] = mapped_column(String(128), default="owner")
    notes: Mapped[str] = mapped_column(Text, default="")
    approval_id: Mapped[str | None] = mapped_column(String(32))

    proposal: Mapped[TradeProposal] = relationship(back_populates="executions")
    challenge: Mapped[TradingChallenge] = relationship(back_populates="executions")


@event.listens_for(TradeExecution, "before_update")
def _execution_immutable(_mapper, _connection, _target):  # pragma: no cover - guard
    raise RuntimeError("Trade execution records are immutable")


@event.listens_for(TradeExecution, "before_delete")
def _execution_no_delete(_mapper, _connection, _target):  # pragma: no cover - guard
    raise RuntimeError("Trade execution records cannot be deleted")


class MarketSnapshot(Base):
    """A quote fetched from the market data provider (read-only). Never treated as instructions."""
    __tablename__ = "market_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    price: Mapped[float] = mapped_column(Float)
    open_price: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    previous_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    asset_type: Mapped[str | None] = mapped_column(String(32))       # stock | etf | unknown | ...
    name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str | None] = mapped_column(String(8))
    provider: Mapped[str] = mapped_column(String(32), default="mock")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def change_pct(self) -> float | None:
        if self.previous_close:
            return round((self.price - self.previous_close) / self.previous_close * 100.0, 2)
        return None


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    challenge_id: Mapped[str] = mapped_column(ForeignKey("trading_challenges.id", ondelete="CASCADE"), index=True)
    cash: Mapped[float] = mapped_column(Float)
    positions_value: Mapped[float] = mapped_column(Float)
    portfolio_value: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    goal_progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(64), default="")     # fill | scan | refresh | manual
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MarketWatchlist(Base):
    """Tickers the owner allows the agents to research. Seeded EMPTY on purpose - the owner populates it."""
    __tablename__ = "market_watchlist"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
