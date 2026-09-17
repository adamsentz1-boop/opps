from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.config import get_settings
from app.models import Opportunity
from app.schemas import ProposalOutput
from app.settings_service import get_setting


class ProposalAgent(BaseAgent):
    role = "ProposalAgent"
    prompt_name = "proposal"

    def run(self, db: Session, opp: Opportunity, owner_notes: str = "") -> ProposalOutput:
        settings = get_settings()
        analysis, plan, buyer = opp.analysis, opp.solution_plan, opp.buyer
        mode = self.mode(db)
        signer = {"mode": mode.key,
                  "name": settings.owner_name or get_setting(db, "organization_name")
                  or "(not provided - sign off without a name)",
                  "title": settings.owner_title if mode.key == "solo" else "(vendor bid)"}
        pricing = {}
        if analysis:
            pricing = {"recommended_price": analysis.recommended_price, "budget_type": opp.budget_type,
                       "estimated_human_hours": analysis.estimated_human_hours,
                       "estimated_agent_hours": analysis.estimated_agent_hours}
        plan_dict = {}
        if plan:
            plan_dict = {"proposed_solution": plan.proposed_solution, "implementation_steps": plan.implementation_steps,
                         "deliverables": plan.deliverables, "assumptions": plan.assumptions,
                         "required_tools": plan.required_tools, "external_accounts_required": plan.external_accounts_required}
        research = {}
        if buyer:
            research = {"company": buyer.company, "industry": buyer.industry, "personalization": buyer.personalization,
                        "uncertain_items": buyer.uncertain_items, "project_motivation": buyer.project_motivation}
        parts = [
            self.profile_block(db),
            self.trusted_block("Signature / sender", signer),
            self.trusted_block("Pricing & effort", pricing),
            self.trusted_block("Solution plan", plan_dict),
            self.trusted_block("Buyer research (inferences are marked uncertain)", research),
        ]
        if owner_notes:
            parts.append(self.trusted_block("Owner notes for this revision", owner_notes))
        parts += [self.opportunity_block(opp), "\nWrite the proposal and return ProposalOutput JSON."]
        ctx = self.mock_context_for(opp, **pricing, required_tools=plan_dict.get("required_tools"))
        return self.run_structured(db, user_content="\n".join(parts), output_model=ProposalOutput,
                                   opportunity_id=opp.id, mock_context=ctx)
