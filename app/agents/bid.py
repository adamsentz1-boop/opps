from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.config import get_settings
from app.models import Opportunity
from app.schemas import BidPackageOutput
from app.settings_service import get_setting


class BidAgent(BaseAgent):
    """Drafts a formal bid package (multiple documents) for bid-type opportunities."""
    role = "BidAgent"
    prompt_name = "bid"

    def run(self, db: Session, opp: Opportunity, owner_notes: str = "") -> BidPackageOutput:
        settings = get_settings()
        a, plan = opp.analysis, opp.solution_plan
        signer = {"name": settings.owner_name or get_setting(db, "organization_name") or "(not provided)",
                  "mode": self.mode(db).key}
        pricing = {} if a is None else {
            "recommended_price": a.recommended_price, "estimated_human_hours": a.estimated_human_hours,
            "estimated_agent_hours": a.estimated_agent_hours, "estimated_api_cost": a.estimated_api_cost,
            "estimated_other_cost": a.estimated_other_cost, "target_rate": get_setting(db, "target_effective_hourly_rate")}
        plan_dict = {} if plan is None else {
            "proposed_solution": plan.proposed_solution, "implementation_steps": plan.implementation_steps,
            "deliverables": plan.deliverables, "qa_strategy": plan.qa_strategy, "assumptions": plan.assumptions,
            "risk_factors": plan.risk_factors, "required_tools": plan.required_tools}
        reqs = [{"requirement": r.requirement, "type": r.type, "mandatory": r.mandatory, "verified": r.verified}
                for r in opp.requirements]
        bid_meta = {"agency": opp.agency, "solicitation_number": opp.solicitation_number, "notice_type": opp.notice_type,
                    "set_aside": opp.set_aside, "naics_code": opp.naics_code, "deadline": opp.deadline,
                    "place_of_performance": opp.place_of_performance}
        parts = [self.profile_block(db),
                 self.trusted_block("Signature / sender", signer),
                 self.trusted_block("Bid metadata", bid_meta),
                 self.trusted_block("Pricing inputs", pricing),
                 self.trusted_block("Solution plan", plan_dict),
                 self.trusted_block("Known requirements (verified = owner has confirmed evidence)", reqs)]
        if owner_notes:
            parts.append(self.trusted_block("Owner notes for this revision", owner_notes))
        parts += [self.opportunity_block(opp), "\nAssemble the bid package and return BidPackageOutput JSON."]
        ctx = self.mock_context_for(opp, **pricing, required_tools=plan_dict.get("required_tools"),
                                    set_aside=opp.set_aside, solicitation_number=opp.solicitation_number)
        return self.run_structured(db, user_content="\n".join(parts), output_model=BidPackageOutput,
                                   opportunity_id=opp.id, mock_context=ctx)
