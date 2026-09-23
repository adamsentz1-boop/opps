"""PortfolioAgent: the decision layer. Turns research + portfolio state into ONE proposed action (or HOLD).

Output is a proposal for owner review. The application re-validates every number against the cash-account
rules and position limits before a TradeProposal row is created; the agent never touches cash or positions.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.schemas import PortfolioDecisionOutput


class PortfolioAgent(BaseAgent):
    role = "PortfolioAgent"
    prompt_name = "portfolio"
    guardrails_name = "_market_guardrails"

    def run(self, db: Session, *, state: dict[str, Any], limits: dict[str, float], research: list[dict[str, Any]],
            prices: dict[str, float], active_proposals: list[dict[str, Any]] | None = None,
            track_record: dict[str, Any] | None = None,
            force_hold: bool = False) -> PortfolioDecisionOutput:
        research_with_prices = [dict(r, price=prices.get(r.get("ticker", ""), 0.0)) for r in research]
        content = "\n".join([
            self.trusted_block("Portfolio state", {k: v for k, v in state.items() if k != "positions"}),
            self.trusted_block("Open positions", state.get("positions") or []),
            self.trusted_block("Hard limits (the application enforces these)", dict(limits, rules=[
                "long-only cash account", "no margin, no borrowing, no negative cash", "no short selling",
                "stocks and ordinary ETFs only", "fractional shares allowed"])),
            self.trusted_block("Active proposals already awaiting the owner (do not duplicate)", active_proposals or []),
            self.trusted_block("MarketResearchAgent outputs (one per candidate, with current price)", research_with_prices),
        ] + ([self.trusted_block("Realised track record of this system's past proposals", track_record)]
             if track_record else []) + [
            "\nDecide BUY, SELL or HOLD for the owner to review. Return PortfolioDecisionOutput JSON.",
        ])
        ctx = {"cash": state.get("cash"), "portfolio_value": state.get("portfolio_value"),
               "positions": state.get("positions"), "research": research_with_prices, "force_hold": force_hold,
               **limits}
        return self.run_structured(db, user_content=content, output_model=PortfolioDecisionOutput, mock_context=ctx)
