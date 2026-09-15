from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.sanitize import UNTRUSTED_PREAMBLE, wrap_untrusted
from app.schemas import NormalizedOpportunity, ScoutOutput


class ScoutAgent(BaseAgent):
    """Optional cheap triage for noisy feeds. The rule-based rejection engine already handles
    obvious junk, so the scout is only invoked when a source is flagged `noisy`."""
    role = "ScoutAgent"
    prompt_name = "scout"

    def triage(self, db: Session, item: NormalizedOpportunity) -> ScoutOutput:
        content = "\n".join([UNTRUSTED_PREAMBLE, wrap_untrusted("title", item.title),
                             wrap_untrusted("description", item.description),
                             "\nTriage this item and return ScoutOutput JSON."])
        return self.run_structured(db, user_content=content, output_model=ScoutOutput,
                                   mock_context={"title": item.title, "description": item.description})
