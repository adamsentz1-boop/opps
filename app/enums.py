"""Enumerations shared across the platform."""
from __future__ import annotations

import enum


class OpportunityStatus(str, enum.Enum):
    NEW = "NEW"                          # discovered, not yet evaluated
    QUALIFYING = "QUALIFYING"            # pipeline in progress
    REJECTED = "REJECTED"                # auto-rejected by rules/thresholds
    QUALIFIED = "QUALIFIED"              # passed qualification, analysis in progress
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # recommended; proposal drafted; owner must decide
    DECLINED = "DECLINED"                # owner rejected
    READY_TO_SUBMIT = "READY_TO_SUBMIT"  # owner approved; Phase 1 terminal state before submission
    SUBMITTED = "SUBMITTED"              # owner confirms submission happened (manual in Phase 1)
    INTERVIEWING = "INTERVIEWING"
    WON = "WON"
    LOST = "LOST"
    ERROR = "ERROR"

    @classmethod
    def pipeline_order(cls) -> list["OpportunityStatus"]:
        return [cls.NEW, cls.QUALIFIED, cls.AWAITING_APPROVAL, cls.READY_TO_SUBMIT,
                cls.SUBMITTED, cls.INTERVIEWING, cls.WON, cls.LOST]


class ApprovalAction(str, enum.Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    EDIT = "EDIT"
    REANALYZE = "REANALYZE"
    VERIFY_REQUIREMENT = "VERIFY_REQUIREMENT"
    APPROVE_DELIVERY = "APPROVE_DELIVERY"
    APPROVE_BID_DOCUMENT = "APPROVE_BID_DOCUMENT"
    MARK_SUBMITTED = "MARK_SUBMITTED"
    MARK_WON = "MARK_WON"
    MARK_LOST = "MARK_LOST"
    MARK_INTERVIEWING = "MARK_INTERVIEWING"


class ApprovalDecision(str, enum.Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EDITED = "EDITED"
    REANALYZE = "REANALYZE"
    RECORDED = "RECORDED"


class AgentRole(str, enum.Enum):
    SCOUT = "ScoutAgent"
    QUALIFICATION = "QualificationAgent"
    RESEARCH = "ResearchAgent"
    SOLUTION_ARCHITECT = "SolutionArchitectAgent"
    PROPOSAL = "ProposalAgent"
    WORK = "WorkAgent"
    QA = "QAAgent"
    SYSTEM = "System"
    OWNER = "Owner"


class WorkOrderStatus(str, enum.Enum):
    NEW = "NEW"
    PLANNING = "PLANNING"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    IN_PROGRESS = "IN_PROGRESS"
    QA = "QA"
    AWAITING_OWNER_APPROVAL = "AWAITING_OWNER_APPROVAL"
    READY_FOR_DELIVERY = "READY_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    INVOICED = "INVOICED"
    PAID = "PAID"


class WorkTaskStatus(str, enum.Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    DONE = "DONE"


class DeliverableStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    DRAFT = "DRAFT"
    QA_PASSED = "QA_PASSED"
    APPROVED = "APPROVED"      # owner approved for delivery
    DELIVERED = "DELIVERED"    # owner confirmed delivery happened


class BuyerType(str, enum.Enum):
    INDIVIDUAL = "individual"
    SMALL_BUSINESS = "small_business"
    ENTERPRISE = "enterprise"
    AGENCY = "agency"
    GOVERNMENT = "government"
    NONPROFIT = "nonprofit"
    EDUCATION = "education"
    UNKNOWN = "unknown"


class RequirementType(str, enum.Enum):
    CERTIFICATION = "certification"
    LICENSE = "license"
    INSURANCE = "insurance"
    REPRESENTATION = "representation"
    CONTRACT_CLAUSE = "contract_clause"
    FORM = "form"
    DEADLINE = "deadline"
    CAPABILITY = "capability"
    OTHER = "other"


class NotificationLevel(str, enum.Enum):
    INFO = "info"
    OPPORTUNITY = "opportunity"
    WARNING = "warning"
    ERROR = "error"
