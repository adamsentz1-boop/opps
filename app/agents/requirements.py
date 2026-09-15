from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.models import Opportunity
from app.schemas import RequirementsOutput


class RequirementsAgent(BaseAgent):
    """Phase-2 preparation: extracts formal requirements into ComplianceRequirement rows (always unverified)."""
    role = "RequirementsAgent"
    prompt_name = "requirements"

    def run(self, db: Session, opp: Opportunity) -> RequirementsOutput:
        content = "\n".join([self.opportunity_block(opp),
                             "\nExtract formal vendor requirements and return RequirementsOutput JSON."])
        return self.run_structured(db, user_content=content, output_model=RequirementsOutput,
                                   opportunity_id=opp.id, mock_context=self.mock_context_for(opp))
