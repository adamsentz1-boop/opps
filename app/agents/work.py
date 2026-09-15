from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.models import Opportunity, WorkOrder
from app.schemas import WorkPlanOutput


class WorkAgent(BaseAgent):
    role = "WorkAgent"
    prompt_name = "work"

    def plan(self, db: Session, work_order: WorkOrder, opp: Opportunity | None) -> WorkPlanOutput:
        parts = [self.trusted_block("Work order", {"title": work_order.title,
                                                   "contract_value": work_order.contract_value})]
        ctx: dict = {"deliverables": []}
        if opp:
            plan = opp.solution_plan
            if plan:
                parts.append(self.trusted_block("Approved solution plan", {
                    "proposed_solution": plan.proposed_solution, "implementation_steps": plan.implementation_steps,
                    "deliverables": plan.deliverables, "qa_strategy": plan.qa_strategy,
                    "external_accounts_required": plan.external_accounts_required}))
                ctx["deliverables"] = plan.deliverables
            proposal = opp.current_proposal
            if proposal:
                parts.append(self.trusted_block("Approved proposal", {"price": proposal.price,
                                                                     "milestones": proposal.milestones,
                                                                     "body": proposal.body}))
            parts.append(self.opportunity_block(opp))
            ctx.update(self.mock_context_for(opp))
        parts.append("\nCreate the execution plan and return WorkPlanOutput JSON.")
        return self.run_structured(db, user_content="\n".join(parts), output_model=WorkPlanOutput,
                                   opportunity_id=opp.id if opp else None, work_order_id=work_order.id,
                                   mock_context=ctx)
