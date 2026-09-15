from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.models import Opportunity
from app.schemas import BuyerResearchOutput


class ResearchAgent(BaseAgent):
    role = "ResearchAgent"
    prompt_name = "research"

    def run(self, db: Session, opp: Opportunity) -> BuyerResearchOutput:
        content = "\n".join([
            self.opportunity_block(opp),
            "\nDescribe the buyer using only the information above. Return BuyerResearchOutput JSON.",
        ])
        return self.run_structured(db, user_content=content, output_model=BuyerResearchOutput,
                                   opportunity_id=opp.id, mock_context=self.mock_context_for(opp))
