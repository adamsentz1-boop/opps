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
from app.market import risk as risk_module
from app.market.data import get_provider, provider_name, refresh_quotes
from app.market.indicators import PriceStats, compute_stats
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
def _floor(value: float) -> float:
    factor = 10 ** ledger.QTY_DP
    return int(max(0.0, value) * factor) / factor


def propose_from_decision(db: Session, challenge: TradingChallenge, decision: PortfolioDecisionOutput,
                          research_by_ticker: dict[str, dict[str, Any]], snapshots: dict[str, MarketSnapshot],
                          *, agent: str = "PortfolioAgent",
                          stats: dict[str, PriceStats] | None = None) -> TradeProposal | None:
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
    levels = None
    ticker_stats = (stats or {}).get(ticker)
    if side == TradeSide.BUY.value:
        # Every entry gets an exit plan first, then the size is whatever keeps the loss at that stop inside
        # the per-trade risk budget. Cash is the outer bound, risk is usually the binding one.
        rl = ledger.risk_limits(db)
        levels = risk_module.compute_levels(price, ticker_stats, stop_move_multiple=rl["stop_move_multiple"],
                                            min_stop_pct=rl["min_stop_pct"], max_stop_pct=rl["max_stop_pct"],
                                            reward_risk_target=rl["reward_risk_target"])
        value = challenge.current_portfolio_value or challenge.cash_balance
        cash_cap = ledger.max_buy_quantity(db, challenge, ticker, price)
        risk_cap = _floor(risk_module.max_quantity_for_risk(value, price, levels.stop_price,
                                                            rl["max_risk_per_trade_pct"]))
        cap = min(cash_cap, risk_cap)
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
    at_risk = risk_module.risk_amount(quantity, price, levels.stop_price) if levels else 0.0
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
        stop_price=levels.stop_price if levels else None,
        target_price=levels.target_price if levels else None,
        risk_amount=at_risk, exit_plan=levels.as_dict() if levels else {},
        price_stats=ticker_stats.as_dict() if ticker_stats else {},
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
                       "estimated_total": total, "confidence": proposal.confidence, "adjusted": adjusted,
                       "stop_price": proposal.stop_price, "target_price": proposal.target_price,
                       "risk_amount": at_risk})
    try:
        from app.notifications import notify_system
        risk_line = (f" Risk if stopped out: ${at_risk:,.2f} (stop ${proposal.stop_price:,.2f})."
                     if proposal.stop_price else "")
        notify_system(db, f"Market Challenge: proposed {side} {ticker}",
                      f"{side} {quantity:g} {ticker} @ ~${price:,.2f} (${total:,.2f}).{risk_line} "
                      f"Awaiting your approval at /market.", level="opportunity")
    except Exception:  # noqa: BLE001 - notifications are best-effort
        log.debug("notification failed", exc_info=True)
    return proposal


# --------------------------------------------------------------------------- exit monitor
def propose_exits(db: Session, challenge: TradingChallenge, snapshots: dict[str, MarketSnapshot],
                  *, agent: str = "ExitMonitor") -> list[TradeProposal]:
    """Propose a SELL for any open position whose stop or target has been breached.

    Deliberately deterministic: an exit is a level that was decided when the position was opened, so it does
    not wait on a model call, cannot be talked out of itself, and still cannot sell anything. It creates a
    proposal the owner approves and executes, exactly like an entry.
    """
    if not get_settings().market_exit_monitor_enabled:
        return []
    created: list[TradeProposal] = []
    for pos in ledger.open_positions(challenge):
        if not (pos.stop_price or pos.target_price):
            continue
        snap = snapshots.get(pos.ticker)
        price = float(snap.price) if snap else pos.current_price
        if not price:
            continue
        signal = risk_module.exit_signal(price, pos.stop_price, pos.target_price)
        if signal is None:
            continue
        if has_active_proposal(db, challenge, pos.ticker, TradeSide.SELL.value):
            log_event(db, agent=agent, action="market.trade.duplicate_skipped", object_type="trading_challenge",
                      object_id=challenge.id, details={"ticker": pos.ticker, "side": "SELL", "signal": signal})
            continue
        level = float(pos.stop_price if signal == "stop" else pos.target_price)
        quantity = round(pos.quantity, ledger.QTY_DP)
        reason = risk_module.exit_reason(signal, pos.ticker, price, level, pos.average_cost)
        realised = round((price - pos.average_cost) * quantity, 2)
        before = ledger.portfolio_state(db, challenge)
        ttl = timedelta(hours=max(1, get_settings().market_proposal_ttl_hours))
        proposal = TradeProposal(
            challenge_id=challenge.id, ticker=pos.ticker, side=TradeSide.SELL.value, quantity=quantity,
            estimated_price=price, estimated_total=round(quantity * price, 2),
            thesis=f"Planned exit: {signal} level reached.", reason_for_trade=reason,
            risks=["Price may move further before you can execute; the fill will differ from this estimate.",
                   "This is the exit plan set when the position was opened, not a new forecast."],
            time_horizon="immediate", confidence=90 if signal == "stop" else 80,
            portfolio_before={k: v for k, v in before.items()
                              if k in ("cash", "positions_value", "portfolio_value", "goal_progress_pct")},
            portfolio_after={"cash": round(challenge.cash_balance + quantity * price, 2),
                             "estimated_realised_pnl": realised},
            market_snapshot=({"snapshot_id": snap.id, "price": price, "previous_close": snap.previous_close,
                              "captured_at": snap.captured_at.isoformat(), "provider": snap.provider}
                             if snap else {"price": price, "source": "last known position price"}),
            exit_plan={"signal": signal, "level": level, "average_cost": pos.average_cost,
                       "estimated_realised_pnl": realised},
            status=TS.AWAITING_APPROVAL.value, created_by=agent, expires_at=utcnow() + ttl,
        )
        db.add(proposal)
        db.flush()
        created.append(proposal)
        log_event(db, agent=agent, action="market.exit.proposed", object_type="trade_proposal",
                  object_id=proposal.id, new_state=proposal.status, human_approval_required=True,
                  details={"ticker": pos.ticker, "signal": signal, "level": level, "price": price,
                           "quantity": quantity, "estimated_realised_pnl": realised})
        try:
            from app.notifications import notify_system
            notify_system(db, f"Market Challenge: {signal.upper()} hit on {pos.ticker}",
                          f"{pos.ticker} at ${price:,.2f} reached its {signal} of ${level:,.2f}. "
                          f"A SELL of {quantity:g} share(s) is awaiting your approval at /market.",
                          level="warning" if signal == "stop" else "opportunity")
        except Exception:  # noqa: BLE001 - notifications are best-effort
            log.debug("notification failed", exc_info=True)
    return created


# --------------------------------------------------------------------------- research helpers
def price_history(ticker: str, days: int | None = None) -> list[dict]:
    """Price history for the statistics. Never raises: no history simply means weaker statistics."""
    days = days or get_settings().market_history_days
    try:
        return get_provider().get_history(ticker, days=days)
    except Exception as exc:  # noqa: BLE001
        log.info("history unavailable for %s: %s", ticker, exc)
        return []


def research_ticker(db: Session, challenge: TradingChallenge, snap: MarketSnapshot, *, llm: LLMClient | None = None,
                    owner_notes: str = "", stats: PriceStats | None = None) -> MarketResearchOutput:
    history = price_history(snap.ticker)
    stats = stats or compute_stats(history, current_price=snap.price)
    pos = next((p for p in challenge.positions if p.ticker == snap.ticker and p.quantity > 0), None)
    position = None if pos is None else {"quantity": pos.quantity, "average_cost": pos.average_cost,
                                          "market_value": pos.market_value, "unrealized_pnl": pos.unrealized_pnl,
                                          "stop_price": pos.stop_price, "target_price": pos.target_price}
    out = MarketResearchAgent(llm).run(db, challenge, snap, history=history, owner_notes=owner_notes,
                                       position=position, days_remaining=ledger.days_remaining(challenge),
                                       stats=stats.as_dict())
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
                              "expired": 0, "exits": [], "skipped": None}
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

    # 2. exits before entries: a breached stop matters more than any new idea, and needs no model call
    exits = propose_exits(db, challenge, snapshots)
    result["exits"] = [{"id": p.id, "ticker": p.ticker, "signal": p.exit_plan.get("signal")} for p in exits]

    # 3. measure each candidate's price behaviour in Python before any agent sees it
    stats: dict[str, PriceStats] = {}
    for ticker, snap in snapshots.items():
        stats[ticker] = compute_stats(price_history(ticker), current_price=snap.price)

    # 4. research each candidate (only tickers the owner listed; held tickers are researched for SELL decisions)
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
            out = research_ticker(db, challenge, snap, llm=llm, stats=stats.get(ticker),
                                  owner_notes=(watch[ticker].notes if ticker in watch else ""))
            research_out.append(out.model_dump())
            result["research"] += 1
        except LLMError as exc:
            result["research_errors"].append(f"{ticker}: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.error("research failed for %s: %s\n%s", ticker, exc, traceback.format_exc())
            result["research_errors"].append(f"{ticker}: {type(exc).__name__}: {exc}")

    # 5. portfolio decision
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
            proposal = propose_from_decision(db, challenge, decision, {r["ticker"]: r for r in research_out},
                                             snapshots, stats=stats)
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
        if proposal.side == "BUY":
            rl = ledger.risk_limits(db)
            stats_obj = compute_stats(price_history(proposal.ticker), current_price=price)
            levels = risk_module.compute_levels(price, stats_obj, stop_move_multiple=rl["stop_move_multiple"],
                                                min_stop_pct=rl["min_stop_pct"], max_stop_pct=rl["max_stop_pct"],
                                                reward_risk_target=rl["reward_risk_target"])
            qty = min(qty, _floor(risk_module.max_quantity_for_risk(
                challenge.current_portfolio_value or challenge.cash_balance, price, levels.stop_price,
                rl["max_risk_per_trade_pct"])))
            proposal.stop_price, proposal.target_price = levels.stop_price, levels.target_price
            proposal.exit_plan, proposal.price_stats = levels.as_dict(), stats_obj.as_dict()
            proposal.risk_amount = risk_module.risk_amount(qty, price, levels.stop_price)
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
