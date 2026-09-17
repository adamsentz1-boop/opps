from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.models import Opportunity
from app.schemas import SolutionOutput
from app.settings_service import get_setting


class SolutionArchitectAgent(BaseAgent):
    role = "SolutionArchitectAgent"
    prompt_name = "solution_architect"

    def run(self, db: Session, opp: Opportunity) -> SolutionOutput:
        analysis = opp.analysis
        qual = {}
        if analysis:
            qual = {"category": analysis.category, "summary": analysis.summary,
                    "estimated_human_hours": analysis.estimated_human_hours,
                    "estimated_agent_hours": analysis.estimated_agent_hours,
                    "recommended_price": analysis.recommended_price,
                    "expected_profit": analysis.expected_profit, "red_flags": analysis.red_flags}
        content = "\n".join([
            self.profile_block(db),
            self.trusted_block("Qualification summary", qual),
            self.opportunity_block(opp),
            "\nDesign the solution and return SolutionOutput JSON.",
        ])
        ctx = self.mock_context_for(opp, **qual)
        return self.run_structured(db, user_content=content, output_model=SolutionOutput,
                                   opportunity_id=opp.id, mock_context=ctx)
