from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.models import Opportunity
from app.schemas import QualificationOutput
from app.settings_service import get_setting


class QualificationAgent(BaseAgent):
    role = "QualificationAgent"
    prompt_name = "qualification"

    def run(self, db: Session, opp: Opportunity) -> QualificationOutput:
        content = "\n".join([
            self.trusted_block("Owner preferred work", get_setting(db, "preferred_work")),
            self.trusted_block("Owner avoids", get_setting(db, "avoid_work")),
            self.trusted_block("Thresholds", {
                "minimum_project_value": get_setting(db, "minimum_project_value"),
                "maximum_human_hours": get_setting(db, "maximum_human_hours"),
                "target_effective_hourly_rate": get_setting(db, "target_effective_hourly_rate"),
            }),
            self.opportunity_block(opp),
            "\nEvaluate this opportunity and return the QualificationOutput JSON.",
        ])
        return self.run_structured(db, user_content=content, output_model=QualificationOutput,
                                   opportunity_id=opp.id, mock_context=self.mock_context_for(opp))
