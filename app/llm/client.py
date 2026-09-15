"""Claude API wrapper.

* Uses the official `anthropic` SDK with structured outputs (`messages.parse`).
* Separates SYSTEM INSTRUCTIONS (the `system` parameter) from UNTRUSTED OPPORTUNITY DATA
  (which only ever appears inside delimited blocks in the user message).
* Records every call as an AgentRun (tokens, cost, raw JSON) for auditing.
* Falls back to a deterministic MockLLM when no API key is configured so the whole
  platform can run and be tested offline.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.costs import estimate_cost
from app.models import AgentRun

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMClient:
    """Thin wrapper around the Anthropic client with a mock fallback."""

    def __init__(self, mock: bool | None = None, model: str | None = None, effort: str | None = None):
        settings = get_settings()
        self.model = model or settings.claude_model
        self.effort = effort if effort is not None else settings.claude_effort
        self.max_tokens = settings.claude_max_tokens
        self.mock = (not settings.llm_enabled) if mock is None else mock
        self._client = None
        if not self.mock:
            import anthropic  # imported lazily so mock mode has no hard dependency at runtime
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # ------------------------------------------------------------------ public
    @property
    def mode(self) -> str:
        return "mock" if self.mock else self.model

    def complete_structured(self, db: Session, *, agent: str, system_prompt: str, user_content: str,
                            output_model: type[T], opportunity_id: str | None = None,
                            work_order_id: str | None = None, mock_context: dict[str, Any] | None = None) -> T:
        """Call Claude and parse a validated `output_model`. Records an AgentRun either way."""
        started = time.perf_counter()
        run = AgentRun(agent=agent, opportunity_id=opportunity_id, work_order_id=work_order_id,
                       model=self.mode, system_prompt_hash=hashlib.sha256(system_prompt.encode()).hexdigest()[:16],
                       request_summary=user_content[:2000])
        try:
            if self.mock:
                from app.llm.mock import mock_response
                result = mock_response(agent, output_model, mock_context or {}, user_content)
                run.input_tokens = len(system_prompt + user_content) // 4
                run.output_tokens = len(result.model_dump_json()) // 4
                run.cost_usd = 0.0
            else:
                result, usage = self._call_anthropic(system_prompt, user_content, output_model)
                run.input_tokens = usage.get("input_tokens", 0)
                run.output_tokens = usage.get("output_tokens", 0)
                run.cost_usd = estimate_cost(self.model, run.input_tokens, run.output_tokens)
            run.status = "ok"
            run.response_json = json.loads(result.model_dump_json())
            return result
        except LLMError as exc:
            run.status = "refused" if "refus" in str(exc).lower() else "error"
            run.error = str(exc)[:2000]
            raise
        except Exception as exc:  # noqa: BLE001 - we want to record every failure
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"[:2000]
            raise LLMError(run.error) from exc
        finally:
            run.duration_ms = int((time.perf_counter() - started) * 1000)
            db.add(run)
            db.flush()

    # ------------------------------------------------------------------ internals
    def _call_anthropic(self, system_prompt: str, user_content: str, output_model: type[T]) -> tuple[T, dict]:
        assert self._client is not None
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            output_format=output_model,
        )
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        try:
            response = self._client.messages.parse(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # Never include the key in error text; SDK errors don't, but be defensive.
            raise LLMError(f"Anthropic API call failed: {type(exc).__name__}: {str(exc)[:500]}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"Model refused the request (category={category})")
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise LLMError("Model output truncated (max_tokens); increase CLAUDE_MAX_TOKENS")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMError("Model returned no parsable structured output")
        usage = getattr(response, "usage", None)
        usage_dict = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0)
            + int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            + int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }
        return parsed, usage_dict


_client_singleton: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = LLMClient()
    return _client_singleton


def reset_llm_client() -> None:
    global _client_singleton
    _client_singleton = None
