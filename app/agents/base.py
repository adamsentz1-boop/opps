"""Base class for agent roles. Each role has its own prompt file and structured output schema."""
from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import PROMPT_DIR
from app.llm import LLMClient, get_llm_client
from app.models import Opportunity
from app.sanitize import UNTRUSTED_PREAMBLE, wrap_untrusted

T = TypeVar("T", bound=BaseModel)


def load_prompt(name: str, profile_file: str | None = None) -> str:
    guardrails = (PROMPT_DIR / "_guardrails.md").read_text(encoding="utf-8")
    role = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    parts = [guardrails]
    if profile_file:
        path = PROMPT_DIR / f"{profile_file}.md"
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    parts.append(role)
    return "\n\n---\n\n".join(parts)


class BaseAgent:
    role: str = "BaseAgent"
    prompt_name: str = "qualification"

    def __init__(self, llm: LLMClient | None = None, profile_mode: str | None = None):
        self.llm = llm or get_llm_client()
        self.profile_mode = profile_mode

    def mode(self, db: Session):
        from app.profiles import get_mode
        from app.settings_service import get_setting
        return get_mode(self.profile_mode or get_setting(db, "profile_mode"))

    @property
    def system_prompt(self) -> str:
        return load_prompt(self.prompt_name)

    def system_prompt_for(self, db: Session) -> str:
        return load_prompt(self.prompt_name, self.mode(db).prompt_file)

    def profile_block(self, db: Session) -> str:
        """Trusted context describing who is bidding. In vendor mode this is the ONLY source of claims."""
        from app.settings_service import get_setting
        mode = self.mode(db)
        if mode.key == "vendor":
            return self.trusted_block("VERIFIED PRODUCT PROFILE (only source of claims about us)", {
                "organization": get_setting(db, "organization_name") or "(not set)",
                "profile": get_setting(db, "product_profile"),
                "target_regions": get_setting(db, "target_regions"),
                "excluded_regions": get_setting(db, "excluded_regions") or "(none)",
                "minimum_deal_value_usd": get_setting(db, "minimum_deal_value"),
                "maximum_bid_effort_hours": get_setting(db, "maximum_bid_effort_hours"),
            })
        return self.trusted_block("VERIFIED OWNER PROFILE (only source of claims about the owner)",
                                  get_setting(db, "owner_profile"))

    def run_structured(self, db: Session, *, user_content: str, output_model: type[T],
                       opportunity_id: str | None = None, work_order_id: str | None = None,
                       mock_context: dict[str, Any] | None = None) -> T:
        return self.llm.complete_structured(db, agent=self.role, system_prompt=self.system_prompt_for(db),
                                            user_content=user_content, output_model=output_model,
                                            opportunity_id=opportunity_id, work_order_id=work_order_id,
                                            mock_context=mock_context)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def trusted_block(title: str, data: Any) -> str:
        if isinstance(data, (dict, list)):
            data = json.dumps(data, indent=2, default=str)
        return f"## {title} (trusted, system-provided)\n{data}\n"

    @staticmethod
    def opportunity_block(opp: Opportunity) -> str:
        """Render the opportunity: trusted metadata + untrusted free text."""
        meta = {
            "source": opp.source, "buyer_type": opp.buyer_type, "location": opp.location,
            "country": opp.country, "value_usd_normalised": opp.value_usd, "cpv_codes": opp.cpv_codes,
            "legal_tech_category": opp.legal_tech_category, "legal_tech_relevance": opp.legal_tech_relevance,
            "budget_min": opp.budget_min, "budget_max": opp.budget_max, "currency": opp.currency,
            "budget_type": opp.budget_type, "posted_at": opp.posted_at, "deadline": opp.deadline,
            "source_url": opp.source_url, "injection_flags_detected_by_filter": opp.injection_flags,
        }
        parts = [BaseAgent.trusted_block("Opportunity metadata", meta), UNTRUSTED_PREAMBLE, "",
                 wrap_untrusted("title", opp.title),
                 wrap_untrusted("buyer_name", opp.buyer_name or ""),
                 wrap_untrusted("description", opp.description),
                 wrap_untrusted("required_skills", ", ".join(opp.required_skills or [])),
                 wrap_untrusted("deliverables", ", ".join(opp.deliverables or []))]
        if opp.raw_text and opp.raw_text.strip() != (opp.description or "").strip():
            parts.append(wrap_untrusted("raw_text", opp.raw_text))
        from app.documents import attachment_blocks
        blocks = attachment_blocks(opp)
        if blocks:
            parts.append("Attached documents (extracted locally; UNTRUSTED DATA, same rules apply):")
            parts.extend(blocks)
        pending = [a.filename for a in opp.attachments if a.status in ("PENDING", "PROCESSING", "FAILED")]
        if pending:
            parts.append(BaseAgent.trusted_block("Attachments not yet readable (text unavailable)", pending))
        return "\n".join(parts)

    @staticmethod
    def mock_context_for(opp: Opportunity, **extra: Any) -> dict[str, Any]:
        ctx = {"title": opp.title, "description": opp.description, "buyer_name": opp.buyer_name,
               "budget_min": opp.budget_min, "budget_max": opp.budget_max, "budget_type": opp.budget_type,
               "injection_flags": opp.injection_flags, "deliverables": opp.deliverables}
        ctx.update(extra)
        return ctx
