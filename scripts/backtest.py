"""Backtest the Market Challenge risk rules against historical prices.

    python -m scripts.backtest                          # watchlist, every entry rule, 2 years
    python -m scripts.backtest --tickers SPY,QQQ        # explicit tickers
    python -m scripts.backtest --rule trend --days 500  # one rule, shorter history
    python -m scripts.backtest --json out.json          # also save the full result

Reads market data through the configured provider, so MARKET_MOCK=true gives a deterministic offline run and
yfinance gives real history. Nothing is written to the database and no trade is ever placed.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import get_settings
from app.db import init_db, session_scope
from app.market.backtest import ENTRY_RULES, BacktestConfig, BacktestResult, run_backtest
from app.market.data import get_provider, provider_name
from app.models import MarketWatchlist


def _watchlist_tickers() -> list[str]:
    with session_scope() as db:
        return [w.ticker for w in db.query(MarketWatchlist)
                .filter(MarketWatchlist.enabled.is_(True)).order_by(MarketWatchlist.ticker).all()]


def fetch_history(tickers: list[str], days: int) -> dict[str, list[dict]]:
    provider = get_provider()
    history: dict[str, list[dict]] = {}
    for ticker in tickers:
        try:
            bars = provider.get_history(ticker, days=days)
        except Exception as exc:  # noqa: BLE001 - a dead ticker must not kill the run
            print(f"  {ticker}: history unavailable ({type(exc).__name__}: {str(exc)[:80]})", file=sys.stderr)
            continue
        if bars:
            history[ticker] = bars
            print(f"  {ticker}: {len(bars)} bars", file=sys.stderr)
        else:
            print(f"  {ticker}: no bars returned", file=sys.stderr)
    return history


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def print_report(results: dict[str, BacktestResult]) -> None:
    first = next(iter(results.values()))
    benchmark = first.benchmark
    print(f"\ntickers   {', '.join(first.tickers) or 'none'}")
    print(f"bars      {first.bars} per ticker")
    print(f"capital   {_money(first.config['starting_cash'])}")
    if benchmark:
        print(f"benchmark {_money(benchmark['final_equity'])}  {_pct(benchmark['total_return_pct'])}"
              f"   ({benchmark['method']})")

    print(f"\n{'entry rule':<12}{'final':>11}{'return':>10}{'max DD':>9}{'trades':>8}"
          f"{'win %':>8}{'avg R':>8}{'P/F':>7}  vs benchmark")
    print("-" * 87)
    for name, result in results.items():
        s = result.summary()
        win = f"{s['win_rate_pct']:.0f}%" if s["win_rate_pct"] is not None else "-"
        avg_r = f"{s['avg_r']:+.2f}" if s["avg_r"] is not None else "-"
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "-"
        if not s["trades"]:
            verdict = "no trades taken"
        elif s["beat_benchmark"] is None:
            verdict = "-"
        else:
            verdict = "beat it" if s["beat_benchmark"] else "lost to it"
        print(f"{name:<12}{_money(s['final_equity']):>11}{_pct(s['total_return_pct']):>10}"
              f"{s['max_drawdown_pct']:>8.1f}%{s['trades']:>8}{win:>8}{avg_r:>8}{pf:>7}  {verdict}")

    for name, result in results.items():
        s = result.summary()
        if not s["trades"]:
            continue
        print(f"\n--- {name}: how {s['trades']} trades ended ---")
        for reason, count in sorted(s["exits"].items()):
            print(f"  {reason:<14}{count:>4}")
        worst = min(result.trades, key=lambda t: t.pnl)
        best = max(result.trades, key=lambda t: t.pnl)
        print(f"  best  {best.ticker:<6} {_money(best.pnl):>9} ({best.return_pct:+.1f}%, {best.held_bars} bars)")
        print(f"  worst {worst.ticker:<6} {_money(worst.pnl):>9} ({worst.return_pct:+.1f}%, {worst.held_bars} bars)")

    print("\n--- read this before believing any number above ---")
    for line in first.assumptions:
        print(f"  * {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickers", help="comma-separated; defaults to the enabled watchlist")
    parser.add_argument("--days", type=int, default=500, help="bars of history to request (default 500)")
    parser.add_argument("--rule", help=f"one of {sorted(ENTRY_RULES)}; default runs all of them")
    parser.add_argument("--cash", type=float, help="starting capital (default: MARKET_STARTING_CAPITAL)")
    parser.add_argument("--risk-pct", type=float, help="max %% of equity risked per trade")
    parser.add_argument("--max-positions", type=int, help="how many positions may be open at once")
    parser.add_argument("--fee", type=float, default=0.0, help="commission per trade")
    parser.add_argument("--slippage-pct", type=float, default=0.0, help="slippage per side, in %%")
    parser.add_argument("--json", dest="json_path", help="write the full result to this file")
    args = parser.parse_args()

    init_db()
    settings = get_settings()
    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else _watchlist_tickers())
    if not tickers:
        print("No tickers. Pass --tickers, or add some at /market/settings.", file=sys.stderr)
        return 1

    print(f"market data provider: {provider_name()}", file=sys.stderr)
    print(f"fetching {args.days} bars for {len(tickers)} ticker(s)...", file=sys.stderr)
    history = fetch_history(tickers, args.days)
    if not history:
        print("No history returned. With MARKET_MOCK=false this usually means the provider is unreachable.",
              file=sys.stderr)
        return 1

    def build(rule: str) -> BacktestConfig:
        config = BacktestConfig(
            starting_cash=args.cash if args.cash is not None else settings.market_starting_capital,
            entry_rule=rule,
            max_risk_per_trade_pct=(args.risk_pct if args.risk_pct is not None
                                    else settings.market_max_risk_per_trade_pct),
            stop_move_multiple=settings.market_stop_move_multiple,
            min_stop_pct=settings.market_min_stop_pct, max_stop_pct=settings.market_max_stop_pct,
            reward_risk_target=settings.market_reward_risk_target,
            max_position_pct=settings.market_max_position_pct,
            fee_per_trade=args.fee, slippage_pct=args.slippage_pct)
        if args.max_positions is not None:
            config.max_open_positions = args.max_positions
        return config

    rules = [args.rule] if args.rule else sorted(ENTRY_RULES)
    results = {rule: run_backtest(history, build(rule)) for rule in rules}
    print_report(results)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump({rule: result.as_dict() for rule, result in results.items()}, handle, indent=2, default=str)
        print(f"\nfull result written to {args.json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
