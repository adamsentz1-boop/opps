"""Performance feedback: round trips, calibration and the counterfactual scorecard.

The point of these tests is that the quality numbers come from the ledger and reconcile with it, so the
dashboard cannot flatter the agents.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.market import approvals as trade_approvals
from app.market import portfolio as ledger
from app.market.data import MockMarketDataProvider, get_provider, refresh_quotes
from app.market.indicators import compute_stats
from app.market.performance import (MIN_TRADES_FOR_SIGNAL, agent_track_record, confidence_calibration,
                                    performance_summary, proposal_scorecard, round_trips)
from app.market.scan import propose_exits, propose_from_decision
from app.models import TradeProposal, utcnow
from app.schemas import PortfolioDecisionOutput


def _history(base: float, daily_move_pct: float, days: int = 60) -> list[dict]:
    out, price = [], base
    for i in range(days):
        price *= 1 + (daily_move_pct / 100.0) * (1 if i % 2 else -1)
        out.append({"day": -days + i, "close": round(price, 4)})
    return out


def _price(db, ticker: str, price: float):
    provider = get_provider()
    assert isinstance(provider, MockMarketDataProvider)
    provider.set_price(ticker, price)
    snaps, errors = refresh_quotes(db, [ticker])
    assert not errors
    return snaps[ticker]


def _buy(db, ticker="ABC", price=50.0, confidence=70, quantity=None, when=None):
    """Propose, approve and fill a BUY. Returns the proposal."""
    challenge = ledger.get_or_create_challenge(db)
    snap = _price(db, ticker, price)
    decision = PortfolioDecisionOutput(action="BUY", ticker=ticker, quantity=quantity or 9999.0,
                                       estimated_price=price, estimated_total=price, thesis="t",
                                       reason_for_trade="r", confidence=confidence, expected_upside_pct=20,
                                       expected_downside_pct=10, risk_reward_ratio=2.0)
    proposal = propose_from_decision(db, challenge, decision, {}, {ticker: snap},
                                     stats={ticker: compute_stats(_history(price, 3.0), current_price=price)})
    assert proposal is not None
    trade_approvals.approve_trade(db, proposal)
    ledger.record_fill(db, proposal, quantity=quantity or proposal.quantity, fill_price=price,
                       executed_at=when or utcnow())
    return proposal


def _sell(db, ticker="ABC", price=60.0, quantity=None, when=None):
    """Owner-initiated SELL: propose, approve, fill. Returns the proposal."""
    challenge = ledger.get_or_create_challenge(db)
    snap = _price(db, ticker, price)
    position = next(p for p in ledger.open_positions(challenge) if p.ticker == ticker)
    quantity = quantity or position.quantity
    proposal = TradeProposal(challenge_id=challenge.id, ticker=ticker, side="SELL", quantity=quantity,
                             estimated_price=price, estimated_total=round(quantity * price, 2),
                             status="AWAITING_APPROVAL", market_snapshot={"snapshot_id": snap.id},
                             expires_at=utcnow() + timedelta(days=1))
    db.add(proposal)
    db.flush()
    challenge.proposals.append(proposal)
    trade_approvals.approve_trade(db, proposal)
    ledger.record_fill(db, proposal, quantity=quantity, fill_price=price, executed_at=when or utcnow())
    return proposal


# --------------------------------------------------------------------------- round trips
def test_no_trades_yields_an_empty_honest_summary(db):
    challenge = ledger.get_or_create_challenge(db)
    summary = performance_summary(db, challenge)
    assert summary["trades"] == 0 and summary["enough_data"] is False
    assert summary["win_rate_pct"] is None and summary["expectancy"] is None
    assert round_trips(db, challenge) == []


def test_a_winning_round_trip_is_reconstructed(db):
    challenge = ledger.get_or_create_challenge(db)
    entry = _buy(db, price=50.0, confidence=80)
    _sell(db, price=60.0)
    trips = round_trips(db, challenge)
    assert len(trips) == 1
    trip = trips[0]
    assert trip.ticker == "ABC" and trip.won is True
    assert trip.entry_price == pytest.approx(50.0) and trip.exit_price == 60.0
    assert trip.confidence == 80 and trip.entry_proposal_id == entry.id
    assert trip.return_pct == pytest.approx(20.0, abs=0.5)


def test_round_trip_pnl_reconciles_with_the_ledger(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, price=50.0)
    _sell(db, price=62.0)
    total = sum(t.realized_pnl for t in round_trips(db, challenge))
    assert total == pytest.approx(challenge.realized_pnl, abs=0.01)
    assert performance_summary(db, challenge)["realized_pnl"] == pytest.approx(challenge.realized_pnl, abs=0.01)


def test_fifo_matches_the_oldest_lot_first(db):
    challenge = ledger.get_or_create_challenge(db)
    first = _buy(db, price=10.0, quantity=2.0, confidence=60, when=utcnow() - timedelta(days=10))
    second = _buy(db, price=20.0, quantity=2.0, confidence=90, when=utcnow() - timedelta(days=5))
    _sell(db, price=30.0, quantity=3.0)
    trips = round_trips(db, challenge)
    assert len(trips) == 2
    # the older, cheaper lot is closed first and carries its own proposal's confidence
    assert trips[0].quantity == pytest.approx(2.0) and trips[0].entry_proposal_id == first.id
    assert trips[0].confidence == 60
    assert trips[1].quantity == pytest.approx(1.0) and trips[1].entry_proposal_id == second.id
    assert trips[1].confidence == 90
    assert sum(t.realized_pnl for t in trips) == pytest.approx(challenge.realized_pnl, abs=0.01)


def test_partial_sale_leaves_the_rest_open(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, price=10.0, quantity=4.0)
    _sell(db, price=15.0, quantity=1.0)
    trips = round_trips(db, challenge)
    assert len(trips) == 1 and trips[0].quantity == pytest.approx(1.0)
    assert ledger.open_positions(challenge)[0].quantity == pytest.approx(3.0)


def test_holding_period_is_measured(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, price=50.0, when=utcnow() - timedelta(days=7))
    _sell(db, price=55.0)
    assert round_trips(db, challenge)[0].holding_days == pytest.approx(7.0, abs=0.1)


def test_r_multiple_uses_the_risk_taken_at_entry(db):
    challenge = ledger.get_or_create_challenge(db)
    entry = _buy(db, price=50.0)
    risk_per_share = entry.estimated_price - entry.stop_price
    _sell(db, price=50.0 + risk_per_share * 2)          # a clean +2R winner
    trip = round_trips(db, challenge)[0]
    assert trip.r_multiple == pytest.approx(2.0, abs=0.1)


def test_r_multiple_is_none_without_a_stop(db):
    challenge = ledger.get_or_create_challenge(db)
    entry = _buy(db, price=50.0)
    entry.stop_price = None
    db.flush()
    _sell(db, price=60.0)
    trip = round_trips(db, challenge)[0]
    assert trip.r_multiple is None and trip.risk_amount is None


def test_exit_reason_records_how_the_trade_ended(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, price=50.0)
    position = ledger.open_positions(challenge)[0]
    stop_hit = round(position.stop_price * 0.99, 2)
    snap = _price(db, "ABC", stop_hit)
    exit_proposal = propose_exits(db, challenge, {"ABC": snap})[0]
    trade_approvals.approve_trade(db, exit_proposal)
    ledger.record_fill(db, exit_proposal, quantity=exit_proposal.quantity, fill_price=stop_hit)
    trip = round_trips(db, challenge)[0]
    assert trip.exit_reason == "stop" and trip.won is False
    assert performance_summary(db, challenge)["by_exit_reason"]["stop"]["trades"] == 1


# --------------------------------------------------------------------------- summary metrics
def test_summary_counts_wins_losses_and_expectancy(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, ticker="WIN", price=10.0, quantity=2.0)
    _sell(db, ticker="WIN", price=15.0)                 # +$10
    _buy(db, ticker="LOSE", price=10.0, quantity=2.0)
    _sell(db, ticker="LOSE", price=8.0)                 # -$4
    summary = performance_summary(db, challenge)
    assert summary["trades"] == 2 and summary["wins"] == 1 and summary["losses"] == 1
    assert summary["win_rate_pct"] == 50.0
    assert summary["gross_win"] == pytest.approx(10.0, abs=0.01)
    assert summary["gross_loss"] == pytest.approx(4.0, abs=0.01)
    assert summary["profit_factor"] == pytest.approx(2.5, abs=0.01)
    assert summary["expectancy"] == pytest.approx(3.0, abs=0.01)
    assert summary["best"]["ticker"] == "WIN" and summary["worst"]["ticker"] == "LOSE"


def test_profit_factor_is_none_without_losses(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, price=10.0, quantity=2.0)
    _sell(db, price=12.0)
    assert performance_summary(db, challenge)["profit_factor"] is None


def test_small_samples_are_flagged(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, price=10.0, quantity=1.0)
    _sell(db, price=11.0)
    summary = performance_summary(db, challenge)
    assert summary["trades"] == 1 and summary["enough_data"] is False
    assert summary["min_trades_for_signal"] == MIN_TRADES_FOR_SIGNAL


# --------------------------------------------------------------------------- calibration
def test_confidence_calibration_separates_the_bands(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, ticker="HI", price=10.0, quantity=1.0, confidence=95)
    _sell(db, ticker="HI", price=15.0)
    _buy(db, ticker="LO", price=10.0, quantity=1.0, confidence=50)
    _sell(db, ticker="LO", price=8.0)
    rows = {r["band"]: r for r in confidence_calibration(db, challenge)}
    assert rows["90+"]["trades"] == 1 and rows["90+"]["win_rate_pct"] == 100.0
    assert rows["under 60"]["trades"] == 1 and rows["under 60"]["win_rate_pct"] == 0.0
    assert rows["60-74"]["trades"] == 0 and rows["60-74"]["win_rate_pct"] is None


# --------------------------------------------------------------------------- counterfactual scorecard
def test_scorecard_prices_ideas_that_were_never_taken(db):
    challenge = ledger.get_or_create_challenge(db)
    snap = _price(db, "SKIP", 100.0)
    decision = PortfolioDecisionOutput(action="BUY", ticker="SKIP", quantity=1.0, estimated_price=100.0,
                                       estimated_total=100.0, thesis="t", reason_for_trade="r", confidence=70,
                                       expected_upside_pct=20, expected_downside_pct=10, risk_reward_ratio=2.0)
    proposal = propose_from_decision(db, challenge, decision, {}, {"SKIP": snap})
    trade_approvals.reject_trade(db, proposal, "not for me")
    _price(db, "SKIP", 130.0)                       # it ran without us
    scorecard = proposal_scorecard(db, challenge)
    assert scorecard["stats"]["rejected"]["count"] == 1
    assert scorecard["stats"]["rejected"]["avg_change_pct"] == pytest.approx(30.0, abs=0.5)
    assert scorecard["groups"]["rejected"][0]["ticker"] == "SKIP"
    assert scorecard["all_ideas"]["count"] == 1 and scorecard["all_ideas"]["up"] == 1


def test_scorecard_separates_taken_from_untaken(db):
    challenge = ledger.get_or_create_challenge(db)
    _buy(db, ticker="TAKEN", price=50.0)
    _sell(db, ticker="TAKEN", price=55.0)
    snap = _price(db, "LEFT", 20.0)
    decision = PortfolioDecisionOutput(action="BUY", ticker="LEFT", quantity=1.0, estimated_price=20.0,
                                       estimated_total=20.0, thesis="t", reason_for_trade="r", confidence=70,
                                       expected_upside_pct=20, expected_downside_pct=10, risk_reward_ratio=2.0)
    trade_approvals.reject_trade(db, propose_from_decision(db, challenge, decision, {}, {"LEFT": snap}), "no")
    _price(db, "LEFT", 16.0)                        # good call, it fell
    scorecard = proposal_scorecard(db, challenge)
    assert scorecard["stats"]["taken"]["count"] == 1
    assert scorecard["stats"]["rejected"]["avg_change_pct"] == pytest.approx(-20.0, abs=0.5)
    assert scorecard["stats"]["rejected"]["down"] == 1


# --------------------------------------------------------------------------- agent feedback
def test_agent_sees_nothing_until_the_sample_is_meaningful(db):
    challenge = ledger.get_or_create_challenge(db)
    assert agent_track_record(db, challenge) is None
    _buy(db, price=10.0, quantity=1.0)
    _sell(db, price=11.0)
    assert agent_track_record(db, challenge) is None      # one trade is not a record


def test_agent_track_record_appears_once_there_is_enough(db):
    challenge = ledger.get_or_create_challenge(db)
    for i in range(MIN_TRADES_FOR_SIGNAL):
        ticker = f"T{i}"
        _buy(db, ticker=ticker, price=10.0, quantity=1.0)
        _sell(db, ticker=ticker, price=12.0)
    record = agent_track_record(db, challenge)
    assert record is not None
    assert record["closed_trades"] == MIN_TRADES_FOR_SIGNAL and record["win_rate_pct"] == 100.0
    assert "small sample" in record["note"].lower()
    assert "more risk" in record["note"]        # the agent is told not to size up on a good run


# --------------------------------------------------------------------------- web + api
def test_performance_page_and_api(client):
    assert client.get("/market/performance").status_code == 200
    assert "No closed trades yet" in client.get("/market/performance").text

    client.post("/api/market/watchlist", json={"ticker": "NVDA"})
    result = client.post("/api/market/scan").json()
    proposal = client.get(f"/api/market/proposals/{result['proposal_id']}").json()
    client.post(f"/market/proposals/{proposal['id']}/approve", data={"notes": "ok"}, follow_redirects=False)
    client.post(f"/market/proposals/{proposal['id']}/fill",
                data={"quantity": proposal["quantity"], "fill_price": proposal["estimated_price"], "fees": "0"},
                follow_redirects=False)

    payload = client.get("/api/market/performance").json()
    assert payload["summary"]["trades"] == 0          # still open, nothing closed
    assert payload["proposal_scorecard"]["stats"]["taken"]["count"] == 1
    page = client.get("/market/performance").text
    assert "Every idea the agents had" in page and "NVDA" in page
    assert "Performance" in client.get("/market").text       # reachable from the nav
