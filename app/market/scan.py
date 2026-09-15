"""Market scan orchestrator: market data -> MarketResearchAgent -> PortfolioAgent -> TradeProposal (AWAITING_APPROVAL).

Runs on the scheduler or from the RUN MARKET SCAN button. It never executes anything: the most it does is create a
proposal row for the owner to approve, and it refuses to create a duplicate active proposal for the same ticker/side.
"""
from __future__ import annotations

import logging
import traceback
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.agents.market_research import MarketResearchAgent
from app.agents.portfolio import PortfolioAgent
from app.audit import log_event
from app.config import get_settings
from app.enums import ChallengeStatus, TradeProposalStatus as TS, TradeSide
from app.llm import LLMClient, LLMError
from app.market import portfolio as ledger
from app.market.data import get_provider, provider_name, refresh_quotes
from app.market.universe import check_universe, normalise_ticker
from app.models import MarketSnapshot, MarketWatchlist, TradeProposal, TradingChallenge, utcnow
from app.schemas import MarketResearchOutput, PortfolioDecisionOutput
from app.settings_service import get_setting

log = logging.getLogger(__name__)


class MarketScanError(RuntimeError):
    pass


def market_llm_client() -> LLMClient:
    """LLM client for the market agents, honouring the market_model / market_effort settings."""
    from app.db import session_scope
    settings = get_settings()
    model, effort = settings.claude_model, settings.claude_effort
    try:
        with session_scope() as db:
            model = str(get_setting(db, "market_model") or model)
            effort = str(get_setting(db, "market_effort") or effort)
    except Exception:  # noqa: BLE001 - settings table missing during very early startup
        pass
    return LLMClient(model=model, effort=effort)


def enabled_watchlist(db: Session) -> list[MarketWatchlist]:
    return db.query(MarketWatchlist).filter(MarketWatchlist.enabled.is_(True)).order_by(MarketWatchlist.ticker).all()


def active_proposals(db: Session, challenge: TradingChallenge) -> list[TradeProposal]:
    return (db.query(TradeProposal).filter(TradeProposal.challenge_id == challenge.id,
                                           TradeProposal.status.in_(TS.active()))
            .order_by(TradeProposal.created_at.desc()).all())


def has_active_proposal(db: Session, challenge: TradingChallenge, ticker: str, side: str) -> bool:
    return db.query(TradeProposal).filter(TradeProposal.challenge_id == challenge.id, TradeProposal.ticker == ticker,
                                          TradeProposal.side == side, TradeProposal.status.in_(TS.active())).count() > 0


def expire_stale_proposals(db: Session, challenge: TradingChallenge) -> int:
    """AWAITING_APPROVAL proposals past expires_at become EXPIRED. Approved ones wait for the owner."""
    now = utcnow()
    rows = db.query(TradeProposal).filter(TradeProposal.challenge_id == challenge.id,
                                          TradeProposal.status == TS.AWAITING_APPROVAL.value,
                                          TradeProposal.expires_at.isnot(None), TradeProposal.expires_at < now).all()
    for p in rows:
        previous = p.status
        p.status = TS.EXPIRED.value
        log_event(db, agent="System", action="market.trade.expired", object_type="trade_proposal", object_id=p.id,
                  previous_state=previous, new_state=p.status, details={"ticker": p.ticker, "side": p.side})
    return len(rows)


# --------------------------------------------------------------------------- proposal creation (validated)
def propose_from_decision(db: Session, challenge: TradingChallenge, decision: PortfolioDecisionOutput,
                          research_by_ticker: dict[str, dict[str, Any]], snapshots: dict[str, MarketSnapshot],
                          *, agent: str = "PortfolioAgent") -> TradeProposal | None:
    """Turn an agent decision into a TradeProposal, or None (HOLD, duplicate, or fails the hard rules)."""
    if decision.action == "HOLD":
        log_event(db, agent=agent, action="market.portfolio.hold", object_type="trading_challenge",
                  object_id=challenge.id, details={"reason": decision.reason_for_trade[:500]})
        return None
    ticker = normalise_ticker(decision.ticker)
    side = decision.action
    snap = snapshots.get(ticker)
    if snap is None:
        log_event(db, agent=agent, action="market.trade.discarded", object_type="trading_challenge",
                  object_id=challenge.id, details={"ticker": ticker, "side": side, "reason": "no market data for ticker"})
        return None
    ok, why = check_universe(ticker, snap.asset_type, snap.name)
    if not ok:
        log_event(db, agent=agent, action="market.trade.discarded", object_type="trading_challenge",
                  object_id=challenge.id, details={"ticker": ticker, "side": side, "reason": why})
        return None
    if has_active_proposal(db, challenge, ticker, side):
        log_event(db, agent=agent, action="market.trade.duplicate_skipped", object_type="trading_challenge",
                  object_id=challenge.id, details={"ticker": ticker, "side": side})
        return None
    price = float(snap.price)
    quantity = round(float(decision.quantity or 0.0), ledger.QTY_DP)
    adjusted = False
    if side == TradeSide.BUY.value:
        cap = ledger.max_buy_quantity(db, challenge, ticker, price)
        if quantity <= 0 or quantity > cap:
            quantity, adjusted = cap, True
    else:
        owned = next((p.quantity for p in challenge.positions if p.ticker == ticker), 0.0)
        if quantity <= 0 or quantity > owned:
            quantity, adjusted = round(owned, ledger.QTY_DP), True
    problems = ledger.check_proposal_limits(db, challenge, side, ticker, quantity, price) if quantity > 0 else ["nothing to trade"]
    if problems:
        log_event(db, agent=agent, action="market.trade.discarded", object_type="trading_challenge",
                  object_id=challenge.id, details={"ticker": ticker, "side": side, "reason": "; ".join(problems)})
        return None
    total = round(quantity * price, 2)
    before = ledger.portfolio_state(db, challenge)
    cash_after = round(challenge.cash_balance - total, 2) if side == "BUY" else round(challenge.cash_balance + total, 2)
    after = {"cash": cash_after, "estimated_portfolio_value": before["portfolio_value"],
             "ticker_exposure_pct": round(((next((p["market_value"] for p in before["positions"] if p["ticker"] == ticker), 0.0)
                                            + (total if side == "BUY" else -total)) / before["portfolio_value"] * 100.0), 2)
             if before["portfolio_value"] else 0.0}
    research = research_by_ticker.get(ticker, {})
    ttl = timedelta(hours=max(1, get_settings().market_proposal_ttl_hours))
    proposal = TradeProposal(
        challenge_id=challenge.id, ticker=ticker, side=side, quantity=quantity, estimated_price=price,
        estimated_total=total, thesis=decision.thesis or research.get("summary", ""),
        reason_for_trade=decision.reason_for_trade + (" [quantity adjusted to fit account limits]" if adjusted else ""),
        bull_case=research.get("bull_case", ""), bear_case=research.get("bear_case", ""),
        catalysts=list(decision.catalysts or research.get("catalysts") or []),
        risks=list(decision.risks or research.get("risks") or []),
        time_horizon=decision.time_horizon or research.get("time_horizon", ""),
        confidence=int(decision.confidence or research.get("confidence") or 0),
        expected_upside_pct=float(decision.expected_upside_pct or research.get("expected_upside_pct") or 0),
        expected_downside_pct=float(decision.expected_downside_pct or research.get("expected_downside_pct") or 0),
        risk_reward_ratio=float(decision.risk_reward_ratio or research.get("risk_reward_ratio") or 0),
        portfolio_before={k: v for k, v in before.items() if k in ("cash", "positions_value", "portfolio_value",
                                                                     "goal_progress_pct", "days_remaining", "positions")},
        portfolio_after=after,
        market_snapshot={"snapshot_id": snap.id, "price": snap.price, "previous_close": snap.previous_close,
                         "change_pct": snap.change_pct, "volume": snap.volume, "market_cap": snap.market_cap,
                         "asset_type": snap.asset_type, "captured_at": snap.captured_at.isoformat(),
                         "provider": snap.provider},
        research=research, status=TS.AWAITING_APPROVAL.value, created_by=agent, expires_at=utcnow() + ttl,
    )
    db.add(proposal)
    db.flush()
    log_event(db, agent=agent, action="market.trade.proposed", object_type="trade_proposal", object_id=proposal.id,
              previous_state=TS.PROPOSED.value, new_state=proposal.status, human_approval_required=True,
              details={"ticker": ticker, "side": side, "quantity": quantity, "estimated_price": price,
                       "estimated_total": total, "confidence": proposal.confidence, "adjusted": adjusted})
    try:
        from app.notifications import notify_system
        notify_system(db, f"Market Challenge: proposed {side} {ticker}",
                      f"{side} {quantity:g} {ticker} @ ~${price:,.2f} (${total:,.2f}). Awaiting your approval at /market.",
                      level="opportunity")
    except Exception:  # noqa: BLE001 - notifications are best-effort
        log.debug("notification failed", exc_info=True)
    return proposal


# --------------------------------------------------------------------------- research helpers
def research_ticker(db: Session, challenge: TradingChallenge, snap: MarketSnapshot, *, llm: LLMClient | None = None,
                    owner_notes: str = "") -> MarketResearchOutput:
    history: list[dict] = []
    try:
        history = get_provider().get_history(snap.ticker, days=30)
    except Exception as exc:  # noqa: BLE001
        log.info("history unavailable for %s: %s", snap.ticker, exc)
    pos = next((p for p in challenge.positions if p.ticker == snap.ticker and p.quantity > 0), None)
    position = None if pos is None else {"quantity": pos.quantity, "average_cost": pos.average_cost,
                                          "market_value": pos.market_value, "unrealized_pnl": pos.unrealized_pnl}
    out = MarketResearchAgent(llm).run(db, challenge, snap, history=history, owner_notes=owner_notes,
                                       position=position, days_remaining=ledger.days_remaining(challenge))
    log_event(db, agent="MarketResearchAgent", action="market.research.generated", object_type="market_snapshot",
              object_id=snap.id, details={"ticker": snap.ticker, "confidence": out.confidence,
                                          "risk_reward_ratio": out.risk_reward_ratio, "avoid_trade": out.avoid_trade,
                                          "injection_detected": out.injection_detected})
    return out


# --------------------------------------------------------------------------- the scan
def run_market_scan(db: Session, *, trigger: str = "scheduler", tickers: list[str] | None = None,
                    llm: LLMClient | None = None, force_hold: bool = False) -> dict[str, Any]:
    """One full cycle. Returns a summary dict. Never executes a trade."""
    settings = get_settings()
    challenge = ledger.get_or_create_challenge(db)
    result: dict[str, Any] = {"trigger": trigger, "provider": provider_name(), "tickers": [], "data_errors": [],
                              "research": 0, "research_errors": [], "proposal_id": None, "decision": None,
                              "expired": 0, "skipped": None}
    if not settings.market_challenge_enabled:
        result["skipped"] = "MARKET_CHALLENGE_ENABLED=false"
        return result
    if challenge.status != ChallengeStatus.ACTIVE.value:
        result["skipped"] = f"challenge is {challenge.status}"
        return result
    log_event(db, agent="System", action="market.scan.started", object_type="trading_challenge", object_id=challenge.id,
              details={"trigger": trigger, "provider": result["provider"]})
    result["expired"] = expire_stale_proposals(db, challenge)

    watch = {w.ticker: w for w in enabled_watchlist(db)}
    if tickers:
        wanted = [normalise_ticker(t) for t in tickers if normalise_ticker(t)]
    else:
        wanted = list(watch.keys())
    held = [p.ticker for p in ledger.open_positions(challenge)]
    universe = list(dict.fromkeys(wanted + [t for t in held if t not in wanted]))  # always refresh held tickers
    result["tickers"] = universe
    if not universe:
        result["skipped"] = "watchlist is empty - add tickers at /market/settings"
        log_event(db, agent="System", action="market.scan.finished", object_type="trading_challenge",
                  object_id=challenge.id, details=result)
        return result

    # 1. market data (never raises)
    snapshots, errors = refresh_quotes(db, universe, agent="System")
    result["data_errors"] = errors
    prices = {t: float(s.price) for t, s in snapshots.items()}
    ledger.recalculate(db, challenge, prices)
    ledger.take_snapshot(db, challenge, reason="scan")

    # 2. research each candidate (only tickers the owner listed; held tickers are researched for SELL decisions)
    llm = llm or market_llm_client()
    research_out: list[dict[str, Any]] = []
    for ticker in universe:
        snap = snapshots.get(ticker)
        if snap is None:
            continue
        ok, why = check_universe(ticker, snap.asset_type, snap.name)
        if not ok:
            log_event(db, agent="System", action="market.research.skipped", object_type="market_snapshot",
                      object_id=snap.id, details={"ticker": ticker, "reason": why})
            continue
        try:
            out = research_ticker(db, challenge, snap, llm=llm, owner_notes=(watch[ticker].notes if ticker in watch else ""))
            research_out.append(out.model_dump())
            result["research"] += 1
        except LLMError as exc:
            result["research_errors"].append(f"{ticker}: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.error("research failed for %s: %s\n%s", ticker, exc, traceback.format_exc())
            result["research_errors"].append(f"{ticker}: {type(exc).__name__}: {exc}")

    # 3. portfolio decision
    proposal = None
    if research_out:
        state = ledger.portfolio_state(db, challenge)
        lim = ledger.limits(db)
        active = [{"ticker": p.ticker, "side": p.side, "quantity": p.quantity, "status": p.status}
                  for p in active_proposals(db, challenge)]
        try:
            decision = PortfolioAgent(llm).run(db, state=state, limits=lim, research=research_out, prices=prices,
                                               active_proposals=active, force_hold=force_hold)
            result["decision"] = decision.model_dump()
            proposal = propose_from_decision(db, challenge, decision, {r["ticker"]: r for r in research_out}, snapshots)
        except LLMError as exc:
            result["research_errors"].append(f"PortfolioAgent: {exc}")
    result["proposal_id"] = proposal.id if proposal else None
    log_event(db, agent="System", action="market.scan.finished", object_type="trading_challenge", object_id=challenge.id,
              details={k: v for k, v in result.items() if k != "decision"})
    return result


def reanalyze_proposal(db: Session, proposal: TradeProposal, *, llm: LLMClient | None = None) -> dict[str, Any]:
    """Re-run research + decision for one proposal's ticker. Updates the proposal or cancels it on HOLD."""
    challenge = proposal.challenge
    snapshots, errors = refresh_quotes(db, [proposal.ticker], agent="System")
    snap = snapshots.get(proposal.ticker)
    if snap is None:
        return {"result": "no market data", "errors": errors}
    ledger.recalculate(db, challenge, {proposal.ticker: float(snap.price)})
    llm = llm or market_llm_client()
    out = research_ticker(db, challenge, snap, llm=llm)
    state = ledger.portfolio_state(db, challenge)
    others = [{"ticker": p.ticker, "side": p.side, "quantity": p.quantity, "status": p.status}
              for p in active_proposals(db, challenge) if p.id != proposal.id]
    decision = PortfolioAgent(llm).run(db, state=state, limits=ledger.limits(db), research=[out.model_dump()],
                                       prices={proposal.ticker: float(snap.price)}, active_proposals=others)
    previous = proposal.status
    if decision.action == proposal.side and normalise_ticker(decision.ticker) == proposal.ticker:
        price = float(snap.price)
        qty = round(float(decision.quantity or proposal.quantity), ledger.QTY_DP)
        if proposal.side == "BUY":
            qty = min(qty, ledger.max_buy_quantity(db, challenge, proposal.ticker, price))
        else:
            owned = next((p.quantity for p in challenge.positions if p.ticker == proposal.ticker), 0.0)
            qty = min(qty, owned)
        proposal.quantity, proposal.estimated_price, proposal.estimated_total = qty, price, round(qty * price, 2)
        proposal.thesis = decision.thesis or out.summary
        proposal.reason_for_trade = decision.reason_for_trade
        proposal.bull_case, proposal.bear_case = out.bull_case, out.bear_case
        proposal.catalysts, proposal.risks = list(decision.catalysts or out.catalysts), list(decision.risks or out.risks)
        proposal.confidence = int(decision.confidence or out.confidence)
        proposal.expected_upside_pct = float(decision.expected_upside_pct or out.expected_upside_pct)
        proposal.expected_downside_pct = float(decision.expected_downside_pct or out.expected_downside_pct)
        proposal.risk_reward_ratio = float(decision.risk_reward_ratio or out.risk_reward_ratio)
        proposal.research = out.model_dump()
        proposal.market_snapshot = {"snapshot_id": snap.id, "price": snap.price, "previous_close": snap.previous_close,
                                    "change_pct": snap.change_pct, "captured_at": snap.captured_at.isoformat(),
                                    "provider": snap.provider}
        if proposal.status == TS.APPROVED.value:
            proposal.status = TS.AWAITING_APPROVAL.value   # numbers changed -> needs a fresh approval
            proposal.approval_id = None
        proposal.expires_at = utcnow() + timedelta(hours=max(1, get_settings().market_proposal_ttl_hours))
        outcome = "updated"
    else:
        proposal.status = TS.CANCELLED.value
        proposal.reason_for_trade = f"Reanalysis returned {decision.action}: {decision.reason_for_trade}"
        outcome = f"cancelled ({decision.action})"
    log_event(db, agent="PortfolioAgent", action="market.trade.reanalyzed", object_type="trade_proposal",
              object_id=proposal.id, previous_state=previous, new_state=proposal.status,
              details={"outcome": outcome, "decision": decision.action})
    return {"result": outcome, "decision": decision.model_dump(), "errors": errors}
