import pytest

from app import approvals
from app.enums import OpportunityStatus as S
from app.enums import WorkOrderStatus
from app.models import Approval, ComplianceRequirement, Opportunity
from app.pipeline import process_opportunity
from app.work_orders import WorkOrderError, create_work_order, transition
from tests.conftest import make_opp


def _recommended(db):
    opp = make_opp(db)
    assert process_opportunity(db, opp.id) == "recommended"
    db.commit()
    return db.get(Opportunity, opp.id)


def test_approve_moves_to_ready_to_submit_and_records_immutable_approval(db):
    opp = _recommended(db)
    approval = approvals.approve_opportunity(db, opp, "looks good")
    db.commit()
    assert opp.status == S.READY_TO_SUBMIT.value
    assert approval.action == "APPROVE" and approval.snapshot["proposal_body"]
    approval.notes = "tamper"
    with pytest.raises(RuntimeError):
        db.commit()
    db.rollback()


def test_cannot_approve_twice_or_from_wrong_state(db):
    opp = _recommended(db)
    approvals.approve_opportunity(db, opp)
    with pytest.raises(approvals.ApprovalError):
        approvals.approve_opportunity(db, opp)


def test_edit_creates_new_version_and_requires_reapproval(db):
    opp = _recommended(db)
    approvals.approve_opportunity(db, opp)
    approvals.edit_proposal(db, opp, "new body", 1800.0, "raised price")
    db.commit()
    assert opp.status == S.AWAITING_APPROVAL.value
    assert opp.current_proposal.version == 2 and opp.current_proposal.edited_by_owner
    assert opp.analysis.recommended_price == 1800.0
    assert len(opp.proposals) == 2


def test_unverified_mandatory_requirement_blocks_approval(db):
    opp = _recommended(db)
    req = ComplianceRequirement(opportunity_id=opp.id, requirement="SOC 2 Type II report", type="certification")
    db.add(req)
    db.flush()
    assert req.verified is False
    with pytest.raises(approvals.ApprovalError):
        approvals.approve_opportunity(db, opp)
    with pytest.raises(approvals.ApprovalError):
        approvals.verify_requirement(db, req, evidence="")
    approvals.verify_requirement(db, req, evidence="Report on file, dated 2026-01")
    approvals.approve_opportunity(db, opp)
    assert opp.status == S.READY_TO_SUBMIT.value


def test_outcome_flow_creates_work_order_and_guards_delivery(db):
    opp = _recommended(db)
    approvals.approve_opportunity(db, opp)
    approvals.record_outcome(db, opp, "submitted")
    approvals.record_outcome(db, opp, "interviewing")
    approvals.record_outcome(db, opp, "won", won_value=1900)
    wo = create_work_order(db, opp)
    db.commit()
    assert opp.status == S.WON.value and wo.status == WorkOrderStatus.PLANNING.value
    assert wo.tasks and wo.qa_checklist
    transition(db, wo, "IN_PROGRESS")
    transition(db, wo, "QA")
    transition(db, wo, "AWAITING_OWNER_APPROVAL")
    with pytest.raises(WorkOrderError):
        transition(db, wo, "READY_FOR_DELIVERY")  # only via approval
    approvals.approve_delivery(db, wo)
    assert wo.status == WorkOrderStatus.READY_FOR_DELIVERY.value
    transition(db, wo, "DELIVERED")
    transition(db, wo, "INVOICED")
    transition(db, wo, "PAID")
    assert wo.paid_amount == 1900
    assert db.query(Approval).filter(Approval.work_order_id == wo.id).count() == 1
