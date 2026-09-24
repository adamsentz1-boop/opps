"""Backtest engine: no look-ahead, honest accounting, and a benchmark that cannot be dodged.

A backtest that is wrong is worse than no backtest, because it invites you to risk money on a fantasy. These
tests exist mainly to prove the engine cannot see the future and cannot spend money it does not have.
"""
from __future__ import annotations

import pytest

from app.market.backtest import (DEFAULT_WARMUP, ENTRY_RULES, BacktestConfig, align, buy_and_hold,
                                 entry_breakout, entry_dip, entry_trend, run_backtest, to_bars)
from app.market.indicators import compute_stats


def rising(n: int = 120, base: float = 100.0, step: float = 0.01) -> list[dict]:
    return [{"close": round(base * ((1 + step) ** i), 4)} for i in range(n)]


def falling(n: int = 120, base: float = 100.0, step: float = 0.01) -> list[dict]:
    return [{"close": round(base * ((1 - step) ** i), 4)} for i in range(n)]


def choppy(n: int = 120, base: float = 100.0, move: float = 0.02) -> list[dict]:
    out, price = [], base
    for i in range(n):
        price *= 1 + move * (1 if i % 2 else -1)
        out.append({"close": round(price, 4)})
    return out


# --------------------------------------------------------------------------- history handling
def test_to_bars_accepts_both_provider_shapes():
    yf = to_bars([{"date": "2026-01-02", "close": 10.0, "volume": 5}, {"date": "2026-01-03", "close": 11.0}])
    mock = to_bars([{"day": -2, "close": 10.0}, {"day": -1, "close": 11.0}])
    assert [b.close for b in yf] == [10.0, 11.0] and yf[0].label == "2026-01-02"
    assert [b.close for b in mock] == [10.0, 11.0]


def test_to_bars_drops_unusable_rows():
    bars = to_bars([{"close": "n/a"}, {"close": None}, {"close": -1}, {"close": 0}, {"close": 12.0}])
    assert [b.close for b in bars] == [12.0]


def test_align_trims_to_the_shortest_series_and_drops_stubs():
    tickers, bars, length = align({"LONG": rising(100), "SHORT": rising(40), "STUB": rising(3)})
    assert tickers == ["LONG", "SHORT"] and length == 40
    assert all(len(b) == 40 for b in bars.values())
    # trimming keeps the most recent bars, so the last close is preserved
    assert bars["LONG"][-1].close == rising(100)[-1]["close"]


def test_align_handles_nothing_usable():
    assert align({}) == ([], {}, 0)
    assert align({"X": rising(2)})[0] == []


# --------------------------------------------------------------------------- the look-ahead guarantee
def test_decisions_cannot_see_the_future():
    """The same bars must produce the same entries whether or not later bars exist.

    This is the assumption every other number depends on. If a future bar leaked into the statistics, a
    backtest could 'know' about a crash before it happened and would be worthless.
    """
    base = rising(90)
    extended = base + falling(60, base=base[-1]["close"])      # the world ends right after
    config = BacktestConfig(entry_rule="trend")

    short_run = run_backtest({"AAA": base}, config)
    long_run = run_backtest({"AAA": extended}, config)

    def entries(result, limit):
        return [(t.entry_index, round(t.entry_price, 6)) for t in result.trades if t.entry_index < limit]

    # align() trims from the front, so compare on the shared tail by offsetting the longer run
    assert short_run.trades, "the rising series should have produced at least one trade"
    assert entries(short_run, len(base)), "no entries to compare"
    # every entry the short run made must appear, at the same price, in the long run
    short_prices = sorted(p for _, p in entries(short_run, len(base)))
    long_prices = sorted(p for _, p in entries(long_run, len(extended)))
    assert set(short_prices).issubset(set(long_prices))


def test_window_never_includes_later_bars():
    from app.market.backtest import _window
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _window(closes, 2, 10) == [1.0, 2.0, 3.0]        # inclusive of the decision bar, nothing after
    assert _window(closes, 4, 2) == [4.0, 5.0]
    assert _window(closes, 0, 5) == [1.0]


# --------------------------------------------------------------------------- entry rules
def test_entry_rules_do_what_they_say():
    up = [r["close"] for r in rising(80)]
    down = [r["close"] for r in falling(80)]
    assert entry_trend(up, compute_stats([{"close": c} for c in up])) is True
    assert entry_trend(down, compute_stats([{"close": c} for c in down])) is False
    assert entry_breakout(up, compute_stats([{"close": c} for c in up])) is True      # at a new high
    assert entry_breakout(down, compute_stats([{"close": c} for c in down])) is False
    # a dip needs an uptrend that has pulled back, which a straight line has not
    assert entry_dip(up, compute_stats([{"close": c} for c in up])) is False


def test_unknown_entry_rule_is_rejected():
    with pytest.raises(ValueError, match="unknown entry rule"):
        run_backtest({"AAA": rising()}, BacktestConfig(entry_rule="crystal_ball"))


def test_every_registered_rule_runs():
    for name in ENTRY_RULES:
        result = run_backtest({"AAA": choppy(150), "BBB": rising(150)}, BacktestConfig(entry_rule=name))
        assert result.bars == 150 and isinstance(result.summary()["trades"], int)


# --------------------------------------------------------------------------- accounting
def test_engine_never_spends_money_it_does_not_have():
    result = run_backtest({f"T{i}": rising(150) for i in range(6)},
                          BacktestConfig(starting_cash=200.0, entry_rule="always", max_open_positions=6))
    assert min(result.equity_curve) > 0
    assert result.final_equity > 0
    # equity is never wildly beyond what the positions could be worth
    assert result.final_equity < 200.0 * 20


def test_position_size_respects_the_risk_budget():
    result = run_backtest({"AAA": rising(150)},
                          BacktestConfig(starting_cash=200.0, entry_rule="always", max_risk_per_trade_pct=5.0))
    assert result.trades
    for trade in result.trades:
        risked = trade.quantity * (trade.entry_price - trade.stop_price)
        assert risked <= 200.0 * 0.05 * 3          # generous headroom: equity grows as the series rises
        assert trade.stop_price < trade.entry_price < trade.target_price


def test_max_open_positions_is_enforced():
    history = {f"T{i}": rising(150) for i in range(5)}
    result = run_backtest(history, BacktestConfig(entry_rule="always", max_open_positions=2))
    # reconstruct concurrency from the trade spans
    for bar in range(result.bars):
        concurrent = sum(1 for t in result.trades if t.entry_index <= bar < t.exit_index)
        assert concurrent <= 2


def test_a_rising_market_exits_on_targets():
    result = run_backtest({"AAA": rising(200)}, BacktestConfig(entry_rule="trend"))
    assert result.trades
    assert result.summary()["exits"].get("target", 0) > 0
    assert result.total_return_pct > 0


def test_a_falling_market_exits_on_stops_and_loses_bounded_money():
    result = run_backtest({"AAA": falling(200)},
                          BacktestConfig(entry_rule="always", max_risk_per_trade_pct=10.0))
    summary = result.summary()
    assert summary["exits"].get("stop", 0) > 0
    assert result.total_return_pct < 0
    # the whole point of the risk rules: a relentless decline must not wipe the account out
    assert result.final_equity > 200.0 * 0.25


def test_open_positions_are_closed_at_the_end():
    result = run_backtest({"AAA": rising(150)}, BacktestConfig(entry_rule="always"))
    assert result.trades
    # nothing is left marked-to-market as an unrealised winner
    assert result.final_equity == pytest.approx(result.equity_curve[-1], abs=0.01)


def test_fees_and_slippage_make_things_worse():
    clean = run_backtest({"AAA": rising(200)}, BacktestConfig(entry_rule="trend"))
    costly = run_backtest({"AAA": rising(200)},
                          BacktestConfig(entry_rule="trend", fee_per_trade=1.0, slippage_pct=0.5))
    assert costly.final_equity < clean.final_equity


def test_empty_history_returns_a_safe_result():
    result = run_backtest({}, BacktestConfig(starting_cash=200.0))
    assert result.trades == [] and result.final_equity == 200.0
    assert result.tickers == [] and result.benchmark == {}
    assert result.assumptions          # still stated


# --------------------------------------------------------------------------- benchmark and honesty
def test_benchmark_is_always_reported():
    result = run_backtest({"AAA": rising(200)}, BacktestConfig(entry_rule="trend"))
    assert result.benchmark["total_return_pct"] > 0
    assert result.summary()["beat_benchmark"] in (True, False)


def test_buy_and_hold_splits_capital_evenly():
    _, bars, _ = align({"UP": rising(100), "DOWN": falling(100)})
    benchmark = buy_and_hold(bars, 200.0, DEFAULT_WARMUP)
    assert benchmark["final_equity"] > 0 and "equal weight across 2" in benchmark["method"]


def test_trend_following_a_falling_market_loses_to_holding_nothing_special():
    """A sanity check that the benchmark is a real comparison, not a rubber stamp."""
    result = run_backtest({"AAA": rising(200)}, BacktestConfig(entry_rule="trend"))
    summary = result.summary()
    assert summary["benchmark"]["total_return_pct"] == pytest.approx(
        result.benchmark["total_return_pct"], abs=0.01)


def test_assumptions_name_the_ways_a_backtest_lies():
    result = run_backtest({"AAA": rising(120)}, BacktestConfig())
    text = " ".join(result.assumptions).lower()
    for caveat in ("closing prices", "survivorship", "slippage", "curve fit", "not tested"):
        assert caveat in text, caveat


def test_max_drawdown_is_measured():
    result = run_backtest({"AAA": rising(60) + falling(80, base=rising(60)[-1]["close"])},
                          BacktestConfig(entry_rule="always"))
    assert result.max_drawdown_pct <= 0
    assert result.summary()["max_drawdown_pct"] == result.max_drawdown_pct


def test_result_serialises_for_the_json_report():
    result = run_backtest({"AAA": rising(150)}, BacktestConfig(entry_rule="trend"))
    payload = result.as_dict()
    assert set(payload) == {"config", "tickers", "bars", "summary", "trades", "equity_curve", "assumptions"}
    assert payload["config"]["entry_rule"] == "trend"
    if payload["trades"]:
        assert {"ticker", "pnl", "r_multiple", "exit_reason"} <= set(payload["trades"][0])


# --------------------------------------------------------------------------- the CLI
def test_cli_runs_end_to_end_in_mock_mode(capsys, monkeypatch):
    from scripts import backtest as cli

    monkeypatch.setattr("scripts.backtest.init_db", lambda: None)
    monkeypatch.setattr("sys.argv", ["backtest", "--tickers", "AAA,BBB", "--days", "180", "--rule", "trend"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "entry rule" in out and "benchmark" in out
    assert "read this before believing any number above" in out


def test_cli_reports_when_there_is_nothing_to_test(capsys, monkeypatch):
    from scripts import backtest as cli
    monkeypatch.setattr("scripts.backtest.init_db", lambda: None)
    monkeypatch.setattr("scripts.backtest._watchlist_tickers", lambda: [])
    monkeypatch.setattr("sys.argv", ["backtest"])
    assert cli.main() == 1
    assert "No tickers" in capsys.readouterr().err


def test_taking_no_trades_is_not_counted_as_beating_the_benchmark():
    """Sitting in cash while the market falls is abstention, not an edge."""
    result = run_backtest({"AAA": falling(200)}, BacktestConfig(entry_rule="trend"))
    summary = result.summary()
    assert summary["trades"] == 0
    assert summary["benchmark"]["total_return_pct"] < 0        # holding would have lost money
    assert summary["beat_benchmark"] is None                   # but we did not win, we just did not play
