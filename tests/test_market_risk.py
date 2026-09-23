"""Risk management for the Market Challenge: measured statistics, exit levels, position sizing, exit monitor.

All offline. The point of these tests is that the numbers that decide how much money is exposed are computed
in Python and are reproducible, never asserted by a model.
"""
from __future__ import annotations

import pytest

from app.market import approvals as trade_approvals
from app.market import portfolio as ledger
from app.market import risk
from app.market.data import MockMarketDataProvider, get_provider, latest_snapshot, refresh_quotes
from app.market.indicators import MIN_DAYS_FOR_STATS, compute_stats, daily_returns
from app.market.scan import propose_exits, propose_from_decision
from app.models import TradeExecution, TradeProposal
from app.schemas import PortfolioDecisionOutput


def _history(base: float, daily_move_pct: float, days: int = 60, volumes: bool = False) -> list[dict]:
    """A zig-zag series with a known average absolute daily move."""
    out, price = [], base
    for i in range(days):
        price *= 1 + (daily_move_pct / 100.0) * (1 if i % 2 else -1)
        row = {"day": -days + i, "close": round(price, 4)}
        if volumes:
            row["volume"] = 1_000_000 * (2 if i > days // 2 else 1)
        out.append(row)
    return out


def _rising(base: float, days: int = 60) -> list[dict]:
    return [{"day": -days + i, "close": round(base * (1.01 ** i), 4)} for i in range(days)]


# --------------------------------------------------------------------------- indicators
def test_too_little_history_is_reported_not_guessed():
    stats = compute_stats([{"close": 10.0}, {"close": 11.0}])
    assert stats.sufficient is False and stats.data_days == 2
    assert stats.annualised_volatility_pct is None and stats.notes


def test_stats_measure_the_average_daily_move():
    stats = compute_stats(_history(50.0, 3.0))
    assert stats.sufficient is True
    assert stats.avg_daily_move_pct == pytest.approx(3.0, abs=0.1)
    assert stats.daily_volatility_pct is not None and stats.annualised_volatility_pct is not None


def test_trend_detection():
    assert compute_stats(_rising(50.0)).trend == "rising"
    falling = [{"close": round(50.0 * (0.99 ** i), 4)} for i in range(60)]
    assert compute_stats(falling).trend == "falling"
    assert compute_stats([{"close": 50.0} for _ in range(60)]).trend == "sideways"


def test_drawdown_and_range_position():
    rising = _rising(50.0)
    stats = compute_stats(rising)
    assert stats.drawdown_from_high_pct == pytest.approx(0.0, abs=0.01)   # at the high
    assert stats.range_position_pct == pytest.approx(100.0, abs=0.1)
    falling = [{"close": round(50.0 * (0.99 ** i), 4)} for i in range(60)]
    assert compute_stats(falling).drawdown_from_high_pct < -20
    assert compute_stats(falling).range_position_pct == pytest.approx(0.0, abs=0.1)


def test_volume_trend_when_volume_is_supplied():
    assert compute_stats(_history(50.0, 2.0, volumes=True)).volume_trend == "rising"
    assert compute_stats(_history(50.0, 2.0, volumes=False)).volume_trend == "unknown"


def test_garbage_rows_are_ignored():
    stats = compute_stats([{"close": "n/a"}, {"close": None}, {"close": -5}, {"close": 10.0}, {"close": 11.0},
                           {"close": 10.5}, {"close": 10.8}, {"close": 11.2}])
    assert stats.sufficient is True and stats.data_days == 5


def test_current_price_is_appended_to_the_series():
    assert compute_stats(_history(50.0, 1.0), current_price=999.0).last_close == 999.0


def test_daily_returns_skips_bad_denominators():
    assert daily_returns([10.0, 11.0]) == [pytest.approx(0.1)]
    assert daily_returns([]) == [] and daily_returns([10.0]) == []


def test_min_days_constant_is_respected():
    assert compute_stats([{"close": 10.0}] * (MIN_DAYS_FOR_STATS - 1)).sufficient is False
    assert compute_stats([{"close": 10.0}] * MIN_DAYS_FOR_STATS).sufficient is True


# --------------------------------------------------------------------------- exit levels
def test_stop_widens_with_volatility():
    calm = risk.compute_levels(100.0, compute_stats(_history(100.0, 1.0)))
    wild = risk.compute_levels(100.0, compute_stats(_history(100.0, 8.0)))
    assert wild.stop_distance_pct > calm.stop_distance_pct
    assert wild.stop_price < calm.stop_price < 100.0


def test_stop_is_clamped_into_a_sane_band():
    tiny = risk.compute_levels(100.0, compute_stats(_history(100.0, 0.1)), min_stop_pct=5.0, max_stop_pct=25.0)
    huge = risk.compute_levels(100.0, compute_stats(_history(100.0, 30.0)), min_stop_pct=5.0, max_stop_pct=25.0)
    assert tiny.stop_distance_pct == 5.0 and "clamped" in tiny.basis
    assert huge.stop_distance_pct == 25.0 and "clamped" in huge.basis


def test_target_follows_the_reward_risk_setting():
    levels = risk.compute_levels(100.0, compute_stats(_history(100.0, 2.0)), reward_risk_target=3.0)
    assert levels.target_distance_pct == pytest.approx(levels.stop_distance_pct * 3.0, abs=0.01)
    assert levels.reward_risk == 3.0 and levels.target_price > 100.0


def test_levels_fall_back_without_history():
    levels = risk.compute_levels(100.0, None)
    assert levels.stop_distance_pct == risk.DEFAULT_STOP_PCT and "insufficient" in levels.basis
    assert risk.compute_levels(100.0, compute_stats([{"close": 1.0}])).stop_distance_pct == risk.DEFAULT_STOP_PCT


def test_levels_reject_a_nonsense_entry():
    with pytest.raises(ValueError):
        risk.compute_levels(0.0, None)


# --------------------------------------------------------------------------- sizing
def test_quantity_is_bounded_by_the_loss_at_the_stop():
    # $200 portfolio, 10% risk budget = $20. Entry 50, stop 40 => $10 per share => 2 shares.
    assert risk.max_quantity_for_risk(200.0, 50.0, 40.0, 10.0) == pytest.approx(2.0)
    # A tighter stop allows a bigger position for the same risk.
    assert risk.max_quantity_for_risk(200.0, 50.0, 45.0, 10.0) == pytest.approx(4.0)


def test_sizing_refuses_an_undefined_risk():
    assert risk.max_quantity_for_risk(200.0, 50.0, 50.0, 10.0) == 0.0     # stop at the entry
    assert risk.max_quantity_for_risk(200.0, 50.0, 60.0, 10.0) == 0.0     # stop above the entry
    assert risk.max_quantity_for_risk(0.0, 50.0, 40.0, 10.0) == 0.0
    assert risk.max_quantity_for_risk(200.0, 50.0, 40.0, 0.0) == 0.0


def test_risk_amount_and_per_share():
    assert risk.risk_per_share(50.0, 40.0) == 10.0
    assert risk.risk_per_share(50.0, 60.0) == 0.0
    assert risk.risk_amount(2.5, 50.0, 40.0) == 25.0


# --------------------------------------------------------------------------- exit signals
def test_exit_signal_fires_only_on_a_breach():
    assert risk.exit_signal(45.0, 46.0, 60.0) == "stop"
    assert risk.exit_signal(46.0, 46.0, 60.0) == "stop"        # at the level counts
    assert risk.exit_signal(61.0, 46.0, 60.0) == "target"
    assert risk.exit_signal(50.0, 46.0, 60.0) is None
    assert risk.exit_signal(50.0, None, None) is None
    assert risk.exit_signal(0.0, 46.0, 60.0) is None


def test_exit_reason_is_written_for_a_human():
    stop = risk.exit_reason("stop", "NVDA", 45.0, 46.0, 50.0)
    assert "NVDA" in stop and "$45.00" in stop and "$46.00" in stop and "-10.0%" in stop
    assert "target" in risk.exit_reason("target", "NVDA", 70.0, 68.0, 50.0).lower()


# --------------------------------------------------------------------------- integration
def _price(db, ticker: str, price: float):
    provider = get_provider()
    assert isinstance(provider, MockMarketDataProvider)
    provider.set_price(ticker, price)
    snaps, errors = refresh_quotes(db, [ticker])
    assert not errors
    return snaps[ticker]


def _decision(ticker: str, price: float, quantity: float = 9999.0) -> PortfolioDecisionOutput:
    return PortfolioDecisionOutput(action="BUY", ticker=ticker, quantity=quantity, estimated_price=price,
                                   estimated_total=quantity * price, thesis="t", reason_for_trade="r",
                                   confidence=70, expected_upside_pct=20, expected_downside_pct=10,
                                   risk_reward_ratio=2.0)


def test_proposal_carries_an_exit_plan_and_bounded_risk(db):
    challenge = ledger.get_or_create_challenge(db)
    snap = _price(db, "ABC", 50.0)
    stats = compute_stats(_history(50.0, 3.0), current_price=50.0)
    proposal = propose_from_decision(db, challenge, _decision("ABC", 50.0), {}, {"ABC": snap}, stats={"ABC": stats})
    assert proposal is not None
    assert 0 < proposal.stop_price < 50.0 < proposal.target_price
    assert proposal.risk_amount <= 200.0 * 0.10 + 0.01          # the 10% per-trade risk budget
    assert proposal.exit_plan["basis"] and proposal.price_stats["sufficient"] is True
    assert proposal.risk_amount == pytest.approx(
        risk.risk_amount(proposal.quantity, proposal.estimated_price, proposal.stop_price))


def test_a_volatile_name_gets_a_smaller_position_than_a_calm_one(db):
    challenge = ledger.get_or_create_challenge(db)
    calm_snap, wild_snap = _price(db, "CALM", 50.0), _price(db, "WILD", 50.0)
    calm = propose_from_decision(db, challenge, _decision("CALM", 50.0), {}, {"CALM": calm_snap},
                                 stats={"CALM": compute_stats(_history(50.0, 1.0), current_price=50.0)})
    wild = propose_from_decision(db, challenge, _decision("WILD", 50.0), {}, {"WILD": wild_snap},
                                 stats={"WILD": compute_stats(_history(50.0, 9.0), current_price=50.0)})
    assert wild.quantity < calm.quantity and wild.estimated_total < calm.estimated_total
    assert wild.stop_price < calm.stop_price
    for proposal in (calm, wild):
        assert proposal.risk_amount <= 200.0 * 0.10 + 0.01


def test_proposal_without_statistics_still_gets_a_stop(db):
    challenge = ledger.get_or_create_challenge(db)
    snap = _price(db, "NEW", 20.0)
    proposal = propose_from_decision(db, challenge, _decision("NEW", 20.0), {}, {"NEW": snap})
    assert proposal.stop_price == pytest.approx(20.0 * (1 - risk.DEFAULT_STOP_PCT / 100), abs=0.01)
    assert "insufficient" in proposal.exit_plan["basis"]


def test_fill_carries_the_exit_plan_onto_the_position(db):
    challenge = ledger.get_or_create_challenge(db)
    snap = _price(db, "ABC", 50.0)
    proposal = propose_from_decision(db, challenge, _decision("ABC", 50.0), {}, {"ABC": snap},
                                     stats={"ABC": compute_stats(_history(50.0, 3.0), current_price=50.0)})
    trade_approvals.approve_trade(db, proposal)
    ledger.record_fill(db, proposal, quantity=proposal.quantity, fill_price=50.0)
    position = ledger.open_positions(challenge)[0]
    assert position.stop_price == proposal.stop_price and position.target_price == proposal.target_price


def _opened(db, ticker="ABC", price=50.0, move=3.0):
    challenge = ledger.get_or_create_challenge(db)
    snap = _price(db, ticker, price)
    proposal = propose_from_decision(db, challenge, _decision(ticker, price), {}, {ticker: snap},
                                     stats={ticker: compute_stats(_history(price, move), current_price=price)})
    trade_approvals.approve_trade(db, proposal)
    ledger.record_fill(db, proposal, quantity=proposal.quantity, fill_price=price)
    return challenge, ledger.open_positions(challenge)[0]


def test_exit_monitor_stays_quiet_inside_the_levels(db):
    challenge, position = _opened(db)
    snaps, _ = refresh_quotes(db, [position.ticker])          # unchanged price
    assert propose_exits(db, challenge, snaps) == []


def test_exit_monitor_proposes_a_sell_when_the_stop_breaks(db):
    challenge, position = _opened(db)
    cash_before = challenge.cash_balance
    snap = _price(db, position.ticker, round(position.stop_price * 0.99, 2))
    created = propose_exits(db, challenge, {position.ticker: snap})
    assert len(created) == 1
    exit_proposal = created[0]
    assert exit_proposal.side == "SELL" and exit_proposal.quantity == position.quantity
    assert exit_proposal.status == "AWAITING_APPROVAL" and exit_proposal.created_by == "ExitMonitor"
    assert exit_proposal.exit_plan["signal"] == "stop"
    assert position.ticker in exit_proposal.reason_for_trade
    # proposing an exit sells nothing
    assert challenge.cash_balance == cash_before
    assert db.query(TradeExecution).filter_by(side="SELL").count() == 0


def test_exit_monitor_proposes_on_the_target_too(db):
    challenge, position = _opened(db)
    snap = _price(db, position.ticker, round(position.target_price * 1.01, 2))
    created = propose_exits(db, challenge, {position.ticker: snap})
    assert len(created) == 1 and created[0].exit_plan["signal"] == "target"
    assert created[0].exit_plan["estimated_realised_pnl"] > 0


def test_exit_monitor_does_not_duplicate(db):
    challenge, position = _opened(db)
    snap = _price(db, position.ticker, round(position.stop_price * 0.99, 2))
    assert len(propose_exits(db, challenge, {position.ticker: snap})) == 1
    assert propose_exits(db, challenge, {position.ticker: snap}) == []


def test_exit_monitor_can_be_disabled(db, monkeypatch):
    challenge, position = _opened(db)
    from app.config import get_settings
    monkeypatch.setenv("MARKET_EXIT_MONITOR_ENABLED", "false")
    get_settings.cache_clear()
    try:
        snap = _price(db, position.ticker, round(position.stop_price * 0.99, 2))
        assert propose_exits(db, challenge, {position.ticker: snap}) == []
    finally:
        get_settings.cache_clear()


def test_exit_monitor_ignores_positions_without_a_plan(db):
    challenge, position = _opened(db)
    position.stop_price = position.target_price = None
    db.flush()
    snap = _price(db, position.ticker, 1.0)
    assert propose_exits(db, challenge, {position.ticker: snap}) == []


def test_approved_exit_realises_the_loss_and_clears_the_plan(db):
    challenge, position = _opened(db)
    stop_hit = round(position.stop_price * 0.99, 2)
    snap = _price(db, position.ticker, stop_hit)
    exit_proposal = propose_exits(db, challenge, {position.ticker: snap})[0]
    trade_approvals.approve_trade(db, exit_proposal)
    execution = ledger.record_fill(db, exit_proposal, quantity=exit_proposal.quantity, fill_price=stop_hit)
    assert execution.side == "SELL" and execution.realized_pnl < 0
    assert challenge.realized_pnl == execution.realized_pnl
    assert position.quantity == 0.0
    assert position.stop_price is None and position.target_price is None      # flat: plan no longer applies
    assert ledger.open_positions(challenge) == []


def test_a_full_scan_reports_exits(db):
    from app.models import MarketWatchlist
    from app.market.scan import run_market_scan

    challenge, position = _opened(db)
    db.add(MarketWatchlist(ticker=position.ticker))
    db.flush()
    _price(db, position.ticker, round(position.stop_price * 0.98, 2))
    result = run_market_scan(db, trigger="test")
    assert len(result["exits"]) == 1 and result["exits"][0]["signal"] == "stop"
    assert db.query(TradeProposal).filter_by(side="SELL", created_by="ExitMonitor").count() == 1


# --------------------------------------------------------------------------- schema upgrade
def test_ensure_columns_is_additive_and_idempotent(db, tmp_path):
    """A database created before these columns existed must keep working after an upgrade."""
    import sqlite3
    from app import db as dbmod

    path = tmp_path / "legacy.db"
    dbmod.reset_engine_for_tests(f"sqlite:///{path}")
    dbmod.init_db()
    new_columns = ("stop_price", "target_price", "risk_amount", "exit_plan", "price_stats")

    con = sqlite3.connect(path)
    kept = [c[1] for c in con.execute("PRAGMA table_info(trade_proposals)") if c[1] not in new_columns]
    con.execute("ALTER TABLE trade_proposals RENAME TO legacy_tp")
    con.execute(f"CREATE TABLE trade_proposals AS SELECT {', '.join(kept)} FROM legacy_tp")
    con.execute("INSERT INTO trade_proposals (id, challenge_id, ticker, side, status) "
                "VALUES ('old', 'c', 'NVDA', 'BUY', 'EXECUTED')")
    con.execute("DROP TABLE legacy_tp")
    con.commit()
    con.close()

    dbmod._engine = None
    dbmod._SessionLocal = None
    applied = dbmod.ensure_columns()
    assert all(f"trade_proposals.{c}" in applied for c in new_columns)

    con = sqlite3.connect(path)
    row = con.execute("SELECT risk_amount, exit_plan, price_stats, stop_price FROM trade_proposals "
                      "WHERE id = 'old'").fetchone()
    con.close()
    assert row == (0.0, "{}", "{}", None)         # existing rows are backfilled, not broken
    assert dbmod.ensure_columns() == []            # running it again changes nothing


# --------------------------------------------------------------------------- API surface
def test_api_exposes_the_risk_numbers(client):
    """The owner decides from these numbers, so the JSON API must carry them too."""
    client.post("/api/market/watchlist", json={"ticker": "NVDA"})
    result = client.post("/api/market/scan").json()
    proposal = client.get(f"/api/market/proposals/{result['proposal_id']}").json()
    assert proposal["stop_price"] < proposal["estimated_price"] < proposal["target_price"]
    assert proposal["risk_amount"] > 0 and proposal["exit_plan"]["basis"]
    assert proposal["price_stats"]["data_days"] > 0

    client.post(f"/market/proposals/{proposal['id']}/approve", data={"notes": "ok"}, follow_redirects=False)
    client.post(f"/market/proposals/{proposal['id']}/fill",
                data={"quantity": proposal["quantity"], "fill_price": proposal["estimated_price"], "fees": "0"},
                follow_redirects=False)
    summary = client.get("/api/market/summary").json()
    assert summary["open_risk"] > 0 and summary["positions_without_stop"] == []
    assert summary["positions"][0]["stop_price"] == proposal["stop_price"]
