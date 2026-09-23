"""MarketResearchAgent: analyses ONE candidate security from application-supplied market data.

It cannot fetch anything itself and cannot trade. Provider data (names, sectors, any text) is wrapped as
untrusted content; prices are passed as a trusted numeric block that the application produced.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.models import MarketSnapshot, TradingChallenge
from app.sanitize import UNTRUSTED_PREAMBLE, detect_injection, wrap_untrusted
from app.schemas import MarketResearchOutput


class MarketResearchAgent(BaseAgent):
    role = "MarketResearchAgent"
    prompt_name = "market_research"
    guardrails_name = "_market_guardrails"

    def run(self, db: Session, challenge: TradingChallenge, snapshot: MarketSnapshot, *, history: list[dict] | None = None,
            owner_notes: str = "", position: dict[str, Any] | None = None, days_remaining: int = 0,
            stats: dict[str, Any] | None = None) -> MarketResearchOutput:
        quote = {"ticker": snapshot.ticker, "price": snapshot.price, "open": snapshot.open_price, "high": snapshot.high,
                 "low": snapshot.low, "previous_close": snapshot.previous_close, "change_pct": snapshot.change_pct,
                 "volume": snapshot.volume, "market_cap": snapshot.market_cap, "asset_type": snapshot.asset_type,
                 "currency": snapshot.currency, "captured_at": snapshot.captured_at, "provider": snapshot.provider}
        external_text = " ".join(str(v) for v in [snapshot.name or "",
                                                   (snapshot.raw_payload or {}).get("info_subset", {}).get("sector", ""),
                                                   (snapshot.raw_payload or {}).get("info_subset", {}).get("industry", "")])
        flags = detect_injection(external_text)
        parts = [
            self.trusted_block("Challenge", {"starting_cash": challenge.starting_cash, "target_value": challenge.target_value,
                                             "target_date": challenge.target_date.date().isoformat(),
                                             "days_remaining": days_remaining, "cash": challenge.cash_balance,
                                             "portfolio_value": challenge.current_portfolio_value}),
            self.trusted_block("Quote (numeric market data fetched by the application)", quote),
        ]
        if stats:
            # Measured in Python (app/market/indicators.py), not inferred by the model from raw numbers.
            parts.append(self.trusted_block("Measured price statistics", stats))
        if history:
            parts.append(self.trusted_block("Recent closes (oldest first)", history[-30:]))
        if position:
            parts.append(self.trusted_block("Existing position in this ticker", position))
        if owner_notes:
            parts.append(self.trusted_block("Owner watchlist notes", owner_notes))
        parts += [UNTRUSTED_PREAMBLE, "", wrap_untrusted("security_name", snapshot.name or ""),
                  wrap_untrusted("provider_text", external_text if external_text.strip() else "(none)"),
                  "\nNo news, earnings, fundamentals or analyst data were supplied unless they appear above. "
                  "Analyse only what is supplied and return MarketResearchOutput JSON."]
        ctx = {"ticker": snapshot.ticker, "price": snapshot.price, "previous_close": snapshot.previous_close,
               "asset_type": snapshot.asset_type, "injection_flags": flags}
        return self.run_structured(db, user_content="\n".join(parts), output_model=MarketResearchOutput, mock_context=ctx)
