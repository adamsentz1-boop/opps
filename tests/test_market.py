"""Market Challenge: ledger rules, approvals, agents and the mock scan. Everything runs offline (MARKET_MOCK)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.approvals import ApprovalError
from app.enums import TradeProposalStatus as TS
from app.market import approvals as trade_approvals
from app.market import portfolio as ledger
from app.market.data import MockMarketDataProvider, get_provider, latest_snapshot, refresh_quotes
from app.market.scan import propose_from_decision, run_market_scan
from app.market.universe import check_universe, is_valid_ticker
from app.models import (Approval, AuditLog, MarketPosition, MarketWatchlist, PortfolioSnapshot, TradeExecution,
                        TradeProposal)
from app.schemas import PortfolioDecisionOutput


# --------------------------------------------------------------------------- helpers
def _challenge(db):
    return ledger.get_or_create_challenge(db)


def _price(db, ticker: str, price: float):
    """Pin a deterministic mock price and store a snapshot for it."""
    provider = get_provider()
    assert isinstance(provider, MockMarketDataProvider)
    provider.set_price(ticker, price)
    snaps, errors = refresh_quotes(db, [ticker])
    assert not errors
    return snaps[ticker]


def _proposal(db, ticker="ABC", side="BUY", quantity=1.0, price=50.0, status=TS.AWAITING_APPROVAL.value):
    ch = _challenge(db)
    snap = _price(db, ticker, price)
    p = TradeProposal(challenge_id=ch.id, ticker=ticker, side=side, quantity=quantity, estimated_price=price,
                      estimated_total=round(quantity * price, 2), thesis="test", status=status,
                      market_snapshot={"snapshot_id": snap.id, "price": price},
                      expires_at=datetime.utcnow() + timedelta(days=1))
    db.add(p)
    db.flush()
    ch.proposals.append(p)
    return p


def _approved(db, **kw):
    p = _proposal(db, **kw)
    trade_approvals.approve_trade(db, p)
    return p


def _buy(db, ticker="ABC", quantity=1.0, price=50.0, fees=0.0):
    p = _approved(db, ticker=ticker, quantity=quantity, price=price)
    return ledger.record_fill(db, p, quantity=quantity, fill_price=price, fees=fees)


def _sell(db, ticker="ABC", quantity=1.0, price=50.0, fees=0.0):
    p = _approved(db, ticker=ticker, side="SELL", quantity=quantity, price=price)
    return ledger.record_fill(db, p, quantity=quantity, fill_price=price, fees=fees)


def _position(db, ticker="ABC") -> MarketPosition | None:
    return db.query(MarketPosition).filter_by(ticker=ticker).first()


# --------------------------------------------------------------------------- balances and rules
def test_default_challenge_starts_with_200_cash(db):
    ch = _challenge(db)
    assert ch.cash_balance == 200.0 and ch.starting_cash == 200.0 and ch.current_portfolio_value == 200.0
    assert ch.target_value == 1000.0 and ch.target_date.date().isoformat() == "2027-01-01"
    assert ch.status == "ACTIVE" and ch.name == "$200 to $1,000 Market Challenge"
    assert db.query(MarketWatchlist).count() == 0  # watchlist is seeded empty on purpose


def test_buy_cannot_exceed_cash(db):
    # a proposal that does not fit in cash cannot even be approved
    with pytest.raises(ApprovalError):
        trade_approvals.approve_trade(db, _proposal(db, quantity=5.0, price=50.0))  # $250 > $200
    # an approved proposal cannot be filled for more than cash either (e.g. price moved up)
    p = _approved(db, ticker="DEF", quantity=4.0, price=50.0)  # $200
    with pytest.raises(ledger.LedgerError):
        ledger.record_fill(db, p, quantity=4.0, fill_price=51.0)
    with pytest.raises(ledger.LedgerError):
        ledger.record_fill(db, p, quantity=5.0, fill_price=50.0)
    assert _challenge(db).cash_balance == 200.0 and db.query(TradeExecution).count() == 0


def test_no_negative_cash_including_fees(db):
    p = _approved(db, quantity=4.0, price=50.0)  # exactly $200, fees push it over
    with pytest.raises(ledger.LedgerError):
        ledger.record_fill(db, p, quantity=4.0, fill_price=50.0, fees=0.01)
    ledger.record_fill(db, p, quantity=4.0, fill_price=50.0, fees=0.0)
    assert _challenge(db).cash_balance == 0.0
    assert ledger.check_trade(db, _challenge(db), "BUY", "ABC", 0.01, 50.0) == \
        ["buy total $0.50 exceeds cash $0.00 (no margin, no borrowing)"]


def test_sell_cannot_exceed_owned_quantity(db):
    _buy(db, quantity=2.0, price=50.0)
    with pytest.raises(ApprovalError):
        trade_approvals.approve_trade(db, _proposal(db, side="SELL", quantity=3.0, price=60.0))
    p = _approved(db, side="SELL", quantity=2.0, price=60.0)
    with pytest.raises(ledger.LedgerError):
        ledger.record_fill(db, p, quantity=3.0, fill_price=60.0)
    assert _position(db).quantity == 2.0
    # short selling of an unowned ticker is refused at proposal validation too
    assert ledger.check_trade(db, _challenge(db), "SELL", "XYZ", 1.0, 10.0)


def test_trade_cannot_execute_without_approval(db):
    p = _proposal(db)  # AWAITING_APPROVAL
    with pytest.raises(ledger.LedgerError):
        ledger.record_fill(db, p, quantity=1.0, fill_price=50.0)
    trade_approvals.reject_trade(db, p, "no")
    with pytest.raises(ledger.LedgerError):
        ledger.record_fill(db, p, quantity=1.0, fill_price=50.0)
    assert db.query(TradeExecution).count() == 0 and _position(db) is None


def test_approval_does_not_execute_trade(db):
    p = _proposal(db, quantity=2.0, price=50.0)
    approval = trade_approvals.approve_trade(db, p, "go")
    db.commit()
    ch = _challenge(db)
    assert p.status == TS.APPROVED.value and p.approval_id == approval.id
    assert approval.object_type == "market_trade" and approval.object_id == p.id and approval.action == "APPROVE"
    assert ch.cash_balance == 200.0 and _position(db) is None and db.query(TradeExecution).count() == 0
    assert db.query(AuditLog).filter_by(action="market.trade.approved").count() == 1


def test_fill_updates_cash_correctly(db):
    execution = _buy(db, quantity=2.0, price=40.0, fees=1.0)
    ch = _challenge(db)
    assert execution.total_value == 81.0 and ch.cash_balance == 119.0
    assert execution.approval_id and db.get(Approval, execution.approval_id).action == "RECORD_FILL"
    assert db.query(AuditLog).filter_by(action="market.trade.executed").count() == 1
    assert db.query(AuditLog).filter_by(action="market.position.changed").count() == 1
    assert db.query(PortfolioSnapshot).filter_by(reason="fill").count() == 1


def test_fill_updates_cost_basis_weighted_average(db):
    _buy(db, quantity=1.0, price=40.0)
    _buy(db, quantity=1.0, price=60.0)
    pos = _position(db)
    assert pos.quantity == 2.0 and pos.average_cost == 50.0
    _buy(db, quantity=2.0, price=20.0, fees=2.0)   # (100 + 40 + 2) / 4
    assert pos.quantity == 4.0 and pos.average_cost == 35.5


def test_sell_updates_position_and_removes_it_when_flat(db):
    _buy(db, quantity=3.0, price=20.0)
    _sell(db, quantity=1.0, price=25.0)
    pos = _position(db)
    assert pos.quantity == 2.0 and pos.average_cost == 20.0
    _sell(db, quantity=2.0, price=25.0)
    assert pos.quantity == 0.0 and ledger.open_positions(_challenge(db)) == []
    assert _challenge(db).cash_balance == 200.0 - 60.0 + 25.0 + 50.0


def test_realized_pnl(db):
    _buy(db, quantity=2.0, price=50.0)
    execution = _sell(db, quantity=2.0, price=70.0, fees=1.0)
    ch = _challenge(db)
    assert execution.realized_pnl == 39.0 and ch.realized_pnl == 39.0
    assert ch.cash_balance == 239.0 and ch.current_portfolio_value == 239.0


def test_unrealized_pnl_marks_to_latest_price(db):
    _buy(db, quantity=2.0, price=50.0)
    _price(db, "ABC", 65.0)
    ch = ledger.recalculate(db, _challenge(db))
    pos = _position(db)
    assert pos.current_price == 65.0 and pos.market_value == 130.0 and pos.unrealized_pnl == 30.0
    assert ch.unrealized_pnl == 30.0 and ch.current_portfolio_value == 230.0
    _price(db, "ABC", 45.0)
    ledger.recalculate(db, ch)
    assert ch.unrealized_pnl == -10.0 and ch.current_portfolio_value == 190.0


def test_fractional_shares(db):
    execution = _buy(db, quantity=0.3333, price=150.0)
    pos = _position(db)
    assert execution.quantity == 0.3333 and pos.quantity == 0.3333
    assert _challenge(db).cash_balance == pytest.approx(200 - 0.3333 * 150, abs=0.01)
    _sell(db, quantity=0.1111, price=150.0)
    assert pos.quantity == pytest.approx(0.2222)
    _sell(db, quantity=0.2222, price=150.0)
    assert pos.quantity == 0.0 and _challenge(db).cash_balance == pytest.approx(200.0, abs=0.02)


def test_portfolio_value_is_cash_plus_positions(db):
    _buy(db, ticker="ABC", quantity=1.0, price=50.0)
    _buy(db, ticker="DEF", quantity=2.0, price=25.0)
    _price(db, "ABC", 80.0)
    _price(db, "DEF", 20.0)
    ch = ledger.recalculate(db, _challenge(db))
    assert ch.cash_balance == 100.0 and ch.positions_value == 120.0 and ch.current_portfolio_value == 220.0
    state = ledger.portfolio_state(db, ch)
    assert state["portfolio_value"] == 220.0 and state["total_return_pct"] == 10.0


def test_goal_progress(db):
    ch = _challenge(db)
    assert ledger.goal_progress_pct(ch) == 0.0
    assert ledger.goal_progress_pct(ch, 600.0) == 50.0
    assert ledger.goal_progress_pct(ch, 1000.0) == 100.0
    assert ledger.goal_progress_pct(ch, 150.0) == -6.25
    assert ledger.days_remaining(ch, now=datetime(2026, 12, 31)) == 1
    assert ledger.days_remaining(ch, now=datetime(2027, 3, 1)) == 0


# --------------------------------------------------------------------------- proposals and approvals
def _decision(ticker="ABC", action="BUY", quantity=1.0, price=50.0):
    return PortfolioDecisionOutput(action=action, ticker=ticker, quantity=quantity, estimated_price=price,
                                   estimated_total=quantity * price, thesis="t", reason_for_trade="r",
                                   confidence=70, expected_upside_pct=20, expected_downside_pct=10, risk_reward_ratio=2.0)


def test_duplicate_proposal_prevention(db):
    ch = _challenge(db)
    snap = _price(db, "ABC", 50.0)
    first = propose_from_decision(db, ch, _decision(), {}, {"ABC": snap})
    assert first is not None and first.status == TS.AWAITING_APPROVAL.value
    assert propose_from_decision(db, ch, _decision(), {}, {"ABC": snap}) is None
    assert db.query(AuditLog).filter_by(action="market.trade.duplicate_skipped").count() == 1
    trade_approvals.approve_trade(db, first)   # still active -> still a duplicate
    assert propose_from_decision(db, ch, _decision(), {}, {"ABC": snap}) is None
    trade_approvals.cancel_trade(db, first)
    assert propose_from_decision(db, ch, _decision(), {}, {"ABC": snap}) is not None


def test_proposal_is_clamped_to_cash_and_position_limits(db):
    ch = _challenge(db)
    snap = _price(db, "ABC", 10.0)
    p = propose_from_decision(db, ch, _decision(quantity=50.0, price=10.0), {}, {"ABC": snap})  # $500 > cash
    assert p is not None and p.estimated_total == 120.0 and p.quantity == 12.0   # 60% single-trade cap of $200
    assert "adjusted" in p.reason_for_trade


def test_market_approval_is_immutable(db):
    p = _proposal(db)
    approval = trade_approvals.approve_trade(db, p, "ok")
    db.commit()
    approval.notes = "tamper"
    with pytest.raises(RuntimeError):
        db.commit()
    db.rollback()
    with pytest.raises(RuntimeError):
        db.delete(db.get(Approval, approval.id))
        db.commit()
    db.rollback()


def test_execution_record_is_immutable(db):
    execution = _buy(db)
    db.commit()
    execution.fill_price = 1.0
    with pytest.raises(RuntimeError):
        db.commit()
    db.rollback()


def test_reject_and_edit_flow(db):
    p = _proposal(db, quantity=2.0, price=50.0)
    trade_approvals.edit_trade(db, p, quantity=1.5, estimated_price=52.0, notes="smaller")
    assert p.quantity == 1.5 and p.estimated_total == 78.0 and p.edited_by_owner
    trade_approvals.approve_trade(db, p)
    trade_approvals.edit_trade(db, p, quantity=1.0)   # edit after approval -> needs re-approval
    assert p.status == TS.AWAITING_APPROVAL.value and p.approval_id is None
    with pytest.raises(ApprovalError):
        trade_approvals.edit_trade(db, p, quantity=100.0)   # exceeds cash
    trade_approvals.reject_trade(db, p, "changed my mind")
    assert p.status == TS.REJECTED.value
    with pytest.raises(ApprovalError):
        trade_approvals.approve_trade(db, p)
    assert db.query(Approval).filter_by(object_type="market_trade", object_id=p.id).count() == 4


def test_agent_hold_creates_no_proposal(db):
    ch = _challenge(db)
    snap = _price(db, "ABC", 50.0)
    hold = PortfolioDecisionOutput(action="HOLD", reason_for_trade="nothing compelling")
    assert propose_from_decision(db, ch, hold, {}, {"ABC": snap}) is None
    assert db.query(TradeProposal).count() == 0
    assert db.query(AuditLog).filter_by(action="market.portfolio.hold").count() == 1
    # full pipeline with the agent forced to HOLD
    db.add(MarketWatchlist(ticker="ABC"))
    db.flush()
    result = run_market_scan(db, trigger="test", force_hold=True)
    assert result["decision"]["action"] == "HOLD" and result["proposal_id"] is None
    assert db.query(TradeProposal).count() == 0


def test_mock_scan_works_without_network(db):
    assert isinstance(get_provider(), MockMarketDataProvider)
    result = run_market_scan(db, trigger="test")
    assert result["skipped"].startswith("watchlist is empty")
    db.add_all([MarketWatchlist(ticker="AAA", notes="owner note"), MarketWatchlist(ticker="BBB"),
                MarketWatchlist(ticker="CCC", enabled=False)])
    db.flush()
    result = run_market_scan(db, trigger="test")
    assert result["tickers"] == ["AAA", "BBB"] and result["data_errors"] == [] and result["research"] == 2
    assert latest_snapshot(db, "AAA") is not None and latest_snapshot(db, "CCC") is None
    assert db.query(AuditLog).filter_by(action="market.scan.started").count() == 2
    assert db.query(AuditLog).filter_by(action="market.data.retrieved").count() == 2
    assert db.query(AuditLog).filter_by(action="market.research.generated").count() == 2
    from app.models import AgentRun
    runs = db.query(AgentRun).filter(AgentRun.agent.in_(["MarketResearchAgent", "PortfolioAgent"])).all()
    assert len(runs) == 3 and all(r.model == "mock" and r.status == "ok" for r in runs)
    # the mock decision layer buys the best candidate inside the limits, never executes anything
    ch = _challenge(db)
    if result["proposal_id"]:
        p = db.get(TradeProposal, result["proposal_id"])
        assert p.status == TS.AWAITING_APPROVAL.value and p.estimated_total <= 200 * 0.6 + 1e-6
    assert ch.cash_balance == 200.0 and db.query(TradeExecution).count() == 0 and _position(db, "AAA") is None
    # re-running does not duplicate the active proposal
    again = run_market_scan(db, trigger="test")
    assert db.query(TradeProposal).filter(TradeProposal.status.in_(TS.active())).count() <= 2
    assert again["proposal_id"] is None or again["proposal_id"] != result["proposal_id"]


def test_market_data_failure_does_not_crash(db, monkeypatch):
    provider = get_provider()

    def boom(ticker):
        raise RuntimeError("provider down")
    monkeypatch.setattr(provider, "get_quote", boom)
    snaps, errors = refresh_quotes(db, ["AAA"])
    assert snaps == {} and len(errors) == 1
    assert db.query(AuditLog).filter_by(action="market.data.error").count() == 1
    db.add(MarketWatchlist(ticker="AAA"))
    db.flush()
    result = run_market_scan(db, trigger="test")
    assert result["research"] == 0 and result["proposal_id"] is None and result["data_errors"]


def test_universe_rules():
    assert is_valid_ticker("NVDA") and is_valid_ticker("BRK.B") and is_valid_ticker("SPY")
    assert not is_valid_ticker("BTC-USD") and not is_valid_ticker("ES=F") and not is_valid_ticker("EURUSD=X")
    assert not is_valid_ticker("NVDA260117C00100000") and not is_valid_ticker("")
    assert check_universe("SPY", "ETF")[0] and check_universe("NVDA", "EQUITY")[0]
    assert not check_universe("TQQQ", "ETF", "ProShares UltraPro QQQ 3x")[0]
    assert not check_universe("SPXW", "OPTION")[0] and not check_universe("BTC", "CRYPTOCURRENCY")[0]


# --------------------------------------------------------------------------- web + api
def test_market_web_flow(client):
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    home = client.get("/").text
    assert "Opportunity Engine" in home and "Market Challenge" in home
    assert client.get("/approvals").status_code == 200 and client.get("/settings").status_code == 200
    page = client.get("/market")
    assert page.status_code == 200 and "$200.00" in page.text and "RUN MARKET SCAN" in page.text
    assert client.get("/market/settings").status_code == 200
    # watchlist
    r = client.post("/market/settings/watchlist", data={"ticker": "btc-usd"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post("/market/settings/watchlist", data={"ticker": "nvda", "notes": "chips"}, follow_redirects=False)
    assert r.status_code == 303 and client.get("/api/market/watchlist").json()[0]["ticker"] == "NVDA"
    # scan (sync via API) -> proposal awaiting approval
    result = client.post("/api/market/scan").json()
    assert result["research"] == 1 and result["proposal_id"]
    pid = result["proposal_id"]
    assert "APPROVE" in client.get("/market").text
    assert client.get(f"/market/proposals/{pid}").status_code == 200
    # fill before approval is refused
    r = client.post(f"/api/market/proposals/{pid}/fill", json={"quantity": 1, "fill_price": 10})
    assert r.status_code == 409
    # approve: no execution, cash unchanged, explicit "execute manually" message
    r = client.post(f"/market/proposals/{pid}/approve", data={"notes": "ok"}, follow_redirects=False)
    assert "execute%20this%20trade%20manually" in r.headers["location"]
    summary = client.get("/api/market/summary").json()
    assert summary["cash"] == 200.0 and summary["brokerage_connection"] is None
    assert "RECORD FILL" in client.get("/market").text
    assert client.get(f"/market/proposals/{pid}/fill").status_code == 200
    # record the fill
    p = client.get(f"/api/market/proposals/{pid}").json()
    r = client.post(f"/market/proposals/{pid}/fill", data={"quantity": p["quantity"], "fill_price": p["estimated_price"],
                                                           "fees": "0", "executed_at": "2026-09-15T14:00"},
                    follow_redirects=False)
    assert "Fill%20recorded" in r.headers["location"], r.headers["location"]
    summary = client.get("/api/market/summary").json()
    assert summary["positions"][0]["ticker"] == "NVDA"
    assert summary["cash"] == round(200 - p["estimated_total"], 2)
    assert client.get("/api/market/proposals/" + pid).json()["status"] == "EXECUTED"
    assert len(client.get("/api/market/executions").json()) == 1
    assert "Trade history" in client.get("/market").text
    assert client.get("/audit?action=market").status_code == 200
    assert len(client.get("/api/market/agent-runs").json()) == 2
    # settings: starting capital change after trades needs explicit confirmation + audit entry
    r = client.post("/market/settings", data={"starting_capital": "250", "target_value": "1000",
                                              "target_date": "2027-01-01"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post("/market/settings", data={"starting_capital": "250", "confirm_capital_change": "yes",
                                              "target_value": "1200", "target_date": "2027-06-30",
                                              "market_scan_interval_minutes": "45"}, follow_redirects=False)
    assert "Saved%204" in r.headers["location"]   # capital, target, date, interval
    summary = client.get("/api/market/summary").json()
    assert summary["starting_cash"] == 250.0 and summary["target_value"] == 1200.0 and summary["target_date"] == "2027-06-30"
    assert summary["cash"] == round(250 - p["estimated_total"], 2)


def test_existing_opportunity_engine_still_works(client):
    r = client.post("/api/opportunities", json={
        "title": "n8n workflow: sync HubSpot contacts to Google Sheets", "buyer_name": "Acme",
        "budget_min": 1500, "budget_max": 2000,
        "description": "We need an n8n automation that syncs HubSpot contacts to a Google Sheet, dedupes by email. "
                       "API access available. Documentation required. Sample data available. Budget $1,500-2,000."})
    assert r.status_code == 201 and r.json()["pipeline_result"] == "recommended"
    opp_id = r.json()["opportunity"]["id"]
    assert client.post(f"/api/opportunities/{opp_id}/approve", json={"notes": "go"}).json()["status"] == "READY_TO_SUBMIT"
    assert client.get("/opportunities").status_code == 200 and client.get("/work-orders").status_code == 200
