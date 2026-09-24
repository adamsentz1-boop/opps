"""Market Challenge dashboard routes (/market). Owner-only UI actions; nothing here talks to a broker."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.approvals import ApprovalError
from app.audit import log_event
from app.config import get_settings
from app.db import get_db
from app.enums import TradeProposalStatus as TS
from app.market import approvals as trade_approvals
from app.market import portfolio as ledger
from app.market.data import latest_snapshots, provider_name, refresh_quotes
from app.market.performance import (confidence_calibration, performance_summary, proposal_scorecard,
                                     round_trips)
from app.market.scan import active_proposals, reanalyze_proposal
from app.market.universe import UNIVERSE_RULES, is_valid_ticker, normalise_ticker
from app.models import AgentRun, AuditLog, MarketWatchlist, PortfolioSnapshot, TradeExecution, TradeProposal
from app.scheduler import market_scan_in_background, market_scheduler_status, reschedule_market_scan
from app.settings_service import MARKET_SETTING_KEYS, SPEC_BY_KEY, get_all_settings, set_setting
from app.web.routes import _ctx, _redirect
from app.web.templating import templates

router = APIRouter(prefix="/market", tags=["market"])
MARKET_AGENTS = ("MarketResearchAgent", "PortfolioAgent")


def require_market_enabled() -> None:
    if not get_settings().market_challenge_enabled:
        raise HTTPException(404, "Market Challenge module is disabled (MARKET_CHALLENGE_ENABLED=false)")


def _get_proposal(db: Session, proposal_id: str) -> TradeProposal:
    p = db.get(TradeProposal, proposal_id)
    if p is None:
        raise HTTPException(404, "Trade proposal not found")
    return p


def _market_ctx(request: Request, db: Session, **extra) -> dict:
    challenge = ledger.get_or_create_challenge(db)
    awaiting = db.query(TradeProposal).filter(TradeProposal.challenge_id == challenge.id,
                                              TradeProposal.status == TS.AWAITING_APPROVAL.value).count()
    base = _ctx(request, db, challenge=challenge, market_awaiting=awaiting, market_provider=provider_name(),
                market_mock=get_settings().market_mock_enabled)
    base.update(extra)
    return base


# --------------------------------------------------------------------------- dashboard
@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_market_enabled)])
@router.get("/", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_market_enabled)])
def market_dashboard(request: Request, db: Session = Depends(get_db)):
    challenge = ledger.get_or_create_challenge(db)
    ledger.recalculate(db, challenge)
    db.commit()
    state = ledger.portfolio_state(db, challenge)
    positions = ledger.open_positions(challenge)
    snaps = latest_snapshots(db, [p.ticker for p in positions])
    awaiting = [p for p in active_proposals(db, challenge) if p.status == TS.AWAITING_APPROVAL.value]
    approved = [p for p in active_proposals(db, challenge) if p.status == TS.APPROVED.value]
    fills = db.query(TradeExecution).filter(TradeExecution.challenge_id == challenge.id) \
        .order_by(TradeExecution.executed_at.desc()).limit(100).all()
    runs = db.query(AgentRun).filter(AgentRun.agent.in_(MARKET_AGENTS)).order_by(AgentRun.created_at.desc()).limit(30).all()
    history = db.query(PortfolioSnapshot).filter(PortfolioSnapshot.challenge_id == challenge.id) \
        .order_by(PortfolioSnapshot.captured_at.desc()).limit(20).all()
    recent = db.query(TradeProposal).filter(TradeProposal.challenge_id == challenge.id,
                                            TradeProposal.status.notin_(list(TS.active()))) \
        .order_by(TradeProposal.updated_at.desc()).limit(10).all()
    watch_count = db.query(MarketWatchlist).filter(MarketWatchlist.enabled.is_(True)).count()
    bar = max(0.0, min(100.0, state["goal_progress_pct"]))
    # What the portfolio loses from here if every stop is hit. The number that matters most on a small account.
    open_risk = round(sum(((p.current_price or p.average_cost) - p.stop_price) * p.quantity
                          for p in positions
                          if p.stop_price and (p.current_price or p.average_cost) > p.stop_price), 2)
    unprotected = [p.ticker for p in positions if not p.stop_price]
    return templates.TemplateResponse(request, "market.html", _market_ctx(
        request, db, state=state, positions=positions, snaps=snaps, awaiting=awaiting, approved=approved, fills=fills,
        runs=runs, history=history, recent=recent, watch_count=watch_count, bar=bar, sched=market_scheduler_status(),
        rules=UNIVERSE_RULES, open_risk=open_risk, unprotected=unprotected))


@router.post("/scan", dependencies=[Depends(require_market_enabled)])
def market_scan(db: Session = Depends(get_db)):
    if not db.query(MarketWatchlist).filter(MarketWatchlist.enabled.is_(True)).count():
        return _redirect("/market/settings", err="The watchlist is empty. Add at least one ticker before scanning.")
    if not market_scan_in_background():
        return _redirect("/market", err="A market scan is already running.")
    return _redirect("/market", msg="Market scan started in the background (research + proposal only; nothing is traded). Refresh in a moment.")


@router.post("/refresh", dependencies=[Depends(require_market_enabled)])
def market_refresh(db: Session = Depends(get_db)):
    challenge = ledger.get_or_create_challenge(db)
    tickers = sorted({p.ticker for p in ledger.open_positions(challenge)} |
                     {w.ticker for w in db.query(MarketWatchlist).filter(MarketWatchlist.enabled.is_(True)).all()})
    snaps, errors = refresh_quotes(db, tickers, agent="Owner")
    ledger.recalculate(db, challenge, {t: float(s.price) for t, s in snaps.items()})
    ledger.take_snapshot(db, challenge, reason="refresh")
    db.commit()
    if errors:
        return _redirect("/market", err="Some quotes failed: " + "; ".join(errors)[:300])
    return _redirect("/market", msg=f"Refreshed {len(snaps)} quote(s) from {provider_name()}.")


@router.get("/performance", response_class=HTMLResponse, dependencies=[Depends(require_market_enabled)])
def market_performance(request: Request, db: Session = Depends(get_db)):
    """Has any of this worked? Measured from the ledger, not from the agents' own confidence."""
    challenge = ledger.get_or_create_challenge(db)
    trips = round_trips(db, challenge)
    return templates.TemplateResponse(request, "market_performance.html", _market_ctx(
        request, db, summary=performance_summary(db, challenge), calibration=confidence_calibration(db, challenge),
        scorecard=proposal_scorecard(db, challenge),
        trips=sorted(trips, key=lambda t: t.closed_at, reverse=True)))


# --------------------------------------------------------------------------- proposals
@router.get("/proposals/{proposal_id}", response_class=HTMLResponse, dependencies=[Depends(require_market_enabled)])
def proposal_detail(request: Request, proposal_id: str, db: Session = Depends(get_db)):
    p = _get_proposal(db, proposal_id)
    audit = db.query(AuditLog).filter(AuditLog.object_id == proposal_id).order_by(AuditLog.timestamp.desc()).limit(50).all()
    return templates.TemplateResponse(request, "market_proposal.html", _market_ctx(request, db, p=p, audit=audit))


def _decide(db: Session, proposal_id: str, fn, ok_msg: str, back: str = "/market", **kwargs):
    p = _get_proposal(db, proposal_id)
    try:
        fn(db, p, **kwargs)
        db.commit()
    except (ApprovalError, ledger.LedgerError, ValueError) as exc:
        db.rollback()
        return _redirect(back, err=str(exc))
    return _redirect(back, msg=ok_msg)


@router.post("/proposals/{proposal_id}/approve", dependencies=[Depends(require_market_enabled)])
def approve(proposal_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    return _decide(db, proposal_id, trade_approvals.approve_trade,
                   "Approved — execute this trade manually with your broker, then record the fill.", notes=notes)


@router.post("/proposals/{proposal_id}/reject", dependencies=[Depends(require_market_enabled)])
def reject(proposal_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    return _decide(db, proposal_id, trade_approvals.reject_trade, "Rejected.", notes=notes)


@router.post("/proposals/{proposal_id}/edit", dependencies=[Depends(require_market_enabled)])
def edit(proposal_id: str, quantity: float = Form(...), estimated_price: Optional[float] = Form(None),
         thesis: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    return _decide(db, proposal_id, trade_approvals.edit_trade, "Proposal edited (needs approval).",
                   quantity=quantity, estimated_price=estimated_price, thesis=thesis, notes=notes)


@router.post("/proposals/{proposal_id}/cancel", dependencies=[Depends(require_market_enabled)])
def cancel(proposal_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    return _decide(db, proposal_id, trade_approvals.cancel_trade, "Proposal cancelled.", notes=notes)


@router.post("/proposals/{proposal_id}/reanalyze", dependencies=[Depends(require_market_enabled)])
def reanalyze(proposal_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    p = _get_proposal(db, proposal_id)
    if p.status not in TS.active():
        return _redirect("/market", err=f"Cannot reanalyze a proposal in status {p.status}")
    trade_approvals.request_reanalysis(db, p, notes)
    db.commit()
    try:
        result = reanalyze_proposal(db, p)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return _redirect("/market", err=f"Reanalysis failed: {exc}")
    return _redirect("/market", msg=f"Reanalysis: {result['result']}.")


# --------------------------------------------------------------------------- record fill (the only ledger mutation)
@router.get("/proposals/{proposal_id}/fill", response_class=HTMLResponse, dependencies=[Depends(require_market_enabled)])
def fill_form(request: Request, proposal_id: str, db: Session = Depends(get_db)):
    p = _get_proposal(db, proposal_id)
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
    return templates.TemplateResponse(request, "market_fill.html", _market_ctx(request, db, p=p, now=now))


@router.post("/proposals/{proposal_id}/fill", dependencies=[Depends(require_market_enabled)])
def record_fill(proposal_id: str, quantity: float = Form(...), fill_price: float = Form(...), fees: float = Form(0.0),
                executed_at: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    p = _get_proposal(db, proposal_id)
    when = None
    if executed_at.strip():
        try:
            when = datetime.fromisoformat(executed_at.strip())
        except ValueError:
            return _redirect(f"/market/proposals/{proposal_id}/fill", err="Invalid execution timestamp")
    try:
        execution = ledger.record_fill(db, p, quantity=quantity, fill_price=fill_price, fees=fees, executed_at=when,
                                       notes=notes)
        db.commit()
    except ledger.LedgerError as exc:
        db.rollback()
        return _redirect(f"/market/proposals/{proposal_id}/fill", err=str(exc))
    return _redirect("/market", msg=f"Fill recorded: {execution.side} {execution.quantity:g} {execution.ticker} @ "
                                    f"${execution.fill_price:,.2f}. Ledger updated.")


# --------------------------------------------------------------------------- settings + watchlist
@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_market_enabled)])
def market_settings(request: Request, db: Session = Depends(get_db)):
    challenge = ledger.get_or_create_challenge(db)
    values = get_all_settings(db)
    watch = db.query(MarketWatchlist).order_by(MarketWatchlist.ticker).all()
    fills = db.query(TradeExecution).filter(TradeExecution.challenge_id == challenge.id).count()
    s = get_settings()
    env = {"MARKET_DATA_PROVIDER": s.market_data_provider, "MARKET_MOCK": s.market_mock_enabled,
           "MARKET_ALLOWED_ASSET_TYPES": s.market_allowed_asset_types, "MARKET_SCAN_ENABLED": s.market_scan_enabled,
           "MARKET_PROPOSAL_TTL_HOURS": s.market_proposal_ttl_hours, "brokerage_connection": "none (by design)"}
    return templates.TemplateResponse(request, "market_settings.html", _market_ctx(
        request, db, specs=[SPEC_BY_KEY[k] for k in MARKET_SETTING_KEYS], values=values, watch=watch, fills=fills,
        env=env, rules=UNIVERSE_RULES, sched=market_scheduler_status()))


@router.post("/settings", dependencies=[Depends(require_market_enabled)])
async def market_settings_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    challenge = ledger.get_or_create_challenge(db)
    fills = db.query(TradeExecution).filter(TradeExecution.challenge_id == challenge.id).count()
    changed: list[str] = []
    try:
        # --- challenge fields
        if "starting_capital" in form:
            new_start = round(float(form["starting_capital"]), 2)
            if new_start != challenge.starting_cash:
                if new_start <= 0:
                    raise ValueError("starting capital must be positive")
                if fills and form.get("confirm_capital_change") != "yes":
                    return _redirect("/market/settings",
                                     err="Trades have been recorded. Tick 'I understand' to change the starting capital.")
                old = challenge.starting_cash
                delta = new_start - old
                challenge.starting_cash = new_start
                challenge.cash_balance = round(challenge.cash_balance + delta, 2)
                if challenge.cash_balance < 0:
                    raise ValueError("that starting capital would make the cash balance negative")
                ledger.recalculate(db, challenge)
                log_event(db, agent="Owner", action="market.settings.changed", object_type="trading_challenge",
                          object_id=challenge.id, previous_state=f"{old:.2f}", new_state=f"{new_start:.2f}",
                          human_approval_required=bool(fills),
                          details={"field": "starting_capital", "confirmed_after_trades": bool(fills),
                                   "cash_adjusted_by": delta, "fills_recorded": fills})
                changed.append("starting_capital")
        if "target_value" in form:
            new_target = round(float(form["target_value"]), 2)
            if new_target != challenge.target_value:
                if new_target <= 0:
                    raise ValueError("target value must be positive")
                old = challenge.target_value
                challenge.target_value = new_target
                log_event(db, agent="Owner", action="market.settings.changed", object_type="trading_challenge",
                          object_id=challenge.id, previous_state=f"{old:.2f}", new_state=f"{new_target:.2f}",
                          details={"field": "target_value"})
                changed.append("target_value")
        if "target_date" in form and str(form["target_date"]).strip():
            new_date = ledger.parse_target_date(str(form["target_date"]))
            if new_date != challenge.target_date:
                old = challenge.target_date.date().isoformat()
                challenge.target_date = new_date
                log_event(db, agent="Owner", action="market.settings.changed", object_type="trading_challenge",
                          object_id=challenge.id, previous_state=old, new_state=new_date.date().isoformat(),
                          details={"field": "target_date"})
                changed.append("target_date")
        if "name" in form and str(form["name"]).strip() and str(form["name"]).strip() != challenge.name:
            challenge.name = str(form["name"]).strip()[:255]
            changed.append("name")
        # --- market settings rows
        current = get_all_settings(db)
        for key in MARKET_SETTING_KEYS:
            if key in form and str(current.get(key)) != str(form[key]):
                if key.endswith("_pct") and not (0 <= float(form[key]) <= 100):
                    raise ValueError(f"{key} must be between 0 and 100")
                if key == "market_scan_interval_minutes" and int(float(form[key])) < 1:
                    raise ValueError("scan interval must be at least 1 minute")
                set_setting(db, key, form[key])
                log_event(db, agent="Owner", action="market.settings.changed", object_type="setting", object_id=key,
                          previous_state=str(current.get(key))[:60], new_state=str(form[key])[:60])
                changed.append(key)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _redirect("/market/settings", err=f"Invalid value: {exc}")
    if "market_scan_interval_minutes" in changed:
        reschedule_market_scan()
    return _redirect("/market/settings", msg=f"Saved {len(changed)} change(s).")


@router.post("/settings/watchlist", dependencies=[Depends(require_market_enabled)])
def watchlist_add(ticker: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db)):
    symbol = normalise_ticker(ticker)
    if not is_valid_ticker(symbol):
        return _redirect("/market/settings", err=f"{ticker!r} is not a plain stock/ETF symbol (no options, futures, forex, crypto).")
    row = db.get(MarketWatchlist, symbol)
    if row is None:
        row = MarketWatchlist(ticker=symbol, enabled=True, notes=notes.strip()[:2000])
        db.add(row)
        action = "market.watchlist.added"
    else:
        row.enabled = True
        if notes.strip():
            row.notes = notes.strip()[:2000]
        action = "market.watchlist.enabled"
    log_event(db, agent="Owner", action=action, object_type="market_watchlist", object_id=symbol,
              details={"notes": notes.strip()[:200]})
    db.commit()
    return _redirect("/market/settings", msg=f"{symbol} added to the watchlist.")


@router.post("/settings/watchlist/{ticker}/toggle", dependencies=[Depends(require_market_enabled)])
def watchlist_toggle(ticker: str, db: Session = Depends(get_db)):
    row = db.get(MarketWatchlist, normalise_ticker(ticker))
    if row is None:
        raise HTTPException(404)
    row.enabled = not row.enabled
    log_event(db, agent="Owner", action="market.watchlist.enabled" if row.enabled else "market.watchlist.disabled",
              object_type="market_watchlist", object_id=row.ticker)
    db.commit()
    return _redirect("/market/settings", msg=f"{row.ticker} {'enabled' if row.enabled else 'disabled'}.")


@router.post("/settings/watchlist/{ticker}/remove", dependencies=[Depends(require_market_enabled)])
def watchlist_remove(ticker: str, db: Session = Depends(get_db)):
    row = db.get(MarketWatchlist, normalise_ticker(ticker))
    if row is None:
        raise HTTPException(404)
    db.delete(row)
    log_event(db, agent="Owner", action="market.watchlist.removed", object_type="market_watchlist", object_id=row.ticker)
    db.commit()
    return _redirect("/market/settings", msg=f"{row.ticker} removed from the watchlist.")
