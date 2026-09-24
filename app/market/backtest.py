"""Backtest the deterministic half of the Market Challenge against historical prices.

The point is to find out whether the exit and sizing discipline has an edge *before* risking real money,
instead of discovering it over 99 days and $200.

**What is tested.** The rules that are computed in Python: the volatility-derived stop (`risk.compute_levels`),
the risk-bounded position size (`risk.max_quantity_for_risk`) and the stop/target exit (`risk.exit_signal`).
Those are the parts that govern how much money is exposed, and they run here through the *same functions* the
live scan uses, so this measures the real thing rather than a re-implementation of it.

**What is NOT tested.** The agents' judgement about which security to buy and when. Replaying a language model
over history would be slow, expensive, and contaminated by hindsight: the model has read the news for these
dates. Instead a handful of mechanical entry rules stand in for the entry signal, and a buy-and-hold benchmark
is always reported alongside. If the machinery cannot beat simply holding, that is the finding.

**How backtests lie**, and what is done about each here:

* *Look-ahead bias.* Every decision uses only bars up to and including the decision bar. `_window` slices
  `closes[:i + 1]`, and there is a test that feeds a series whose future is wildly different to prove no
  future bar leaks in.
* *Close-only exits understate losses.* `get_history` returns closing prices, so a stop can only be detected
  at a close. A real stop triggers intraday, usually at a worse price, and a gap can blow through it entirely.
  Losses here are therefore optimistic. Reported, not hidden.
* *Costs.* Commission and slippage default to zero because a fractional-share broker usually charges nothing,
  but both are configurable and slippage is applied against you on entry and exit.
* *Survivorship bias.* A ticker you can fetch history for today is one that still exists. Companies that went
  to zero are not in the sample, so any result is flattered.
* *Overfitting.* Tuning the settings until the backtest looks good produces a curve fit, not an edge. The
  result carries its settings so a suspiciously good run can be recognised for what it is.

Every result carries these caveats in `assumptions`, so a report can never be read without them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.market.indicators import MIN_DAYS_FOR_STATS, PriceStats, compute_stats
from app.market.risk import (compute_levels, exit_signal, max_quantity_for_risk, risk_amount, risk_per_share)

#: Bars of history consumed before the first trade may be taken, so early decisions are not made blind.
DEFAULT_WARMUP = 30


@dataclass
class Bar:
    index: int
    close: float
    label: str = ""          # a date when the provider supplied one, else an offset


@dataclass
class BacktestTrade:
    ticker: str
    entry_index: int
    entry_label: str
    entry_price: float
    exit_index: int
    exit_label: str
    exit_price: float
    quantity: float
    stop_price: float
    target_price: float
    pnl: float
    exit_reason: str          # stop | target | end_of_data

    @property
    def held_bars(self) -> int:
        return self.exit_index - self.entry_index

    @property
    def won(self) -> bool:
        return self.pnl > 0

    @property
    def return_pct(self) -> float:
        basis = self.quantity * self.entry_price
        return round(self.pnl / basis * 100.0, 2) if basis else 0.0

    @property
    def r_multiple(self) -> float | None:
        risked = risk_amount(self.quantity, self.entry_price, self.stop_price)
        return round(self.pnl / risked, 2) if risked else None

    def as_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "entry": self.entry_label, "exit": self.exit_label,
                "entry_price": self.entry_price, "exit_price": self.exit_price, "quantity": self.quantity,
                "stop_price": self.stop_price, "target_price": self.target_price, "pnl": self.pnl,
                "return_pct": self.return_pct, "r_multiple": self.r_multiple, "held_bars": self.held_bars,
                "exit_reason": self.exit_reason, "won": self.won}


# --------------------------------------------------------------------------- entry rules
#: An entry rule sees only the closes up to and including the current bar, plus the measured statistics.
EntryRule = Callable[[list[float], PriceStats], bool]


def entry_always(closes: list[float], stats: PriceStats) -> bool:
    """Enter whenever flat. The control: it has no entry edge, so it isolates the risk machinery."""
    return True


def entry_trend(closes: list[float], stats: PriceStats) -> bool:
    """Buy strength: the short average is above the long one."""
    return stats.trend == "rising"


def entry_dip(closes: list[float], stats: PriceStats) -> bool:
    """Buy weakness inside strength: trend still rising, but price has pulled back off the high."""
    return stats.trend == "rising" and (stats.drawdown_from_high_pct or 0.0) <= -5.0


def entry_breakout(closes: list[float], stats: PriceStats) -> bool:
    """Buy a new high for the window."""
    return (stats.range_position_pct or 0.0) >= 99.0


ENTRY_RULES: dict[str, EntryRule] = {
    "always": entry_always, "trend": entry_trend, "dip": entry_dip, "breakout": entry_breakout,
}


# --------------------------------------------------------------------------- configuration + result
@dataclass
class BacktestConfig:
    starting_cash: float = 200.0
    entry_rule: str = "trend"
    warmup: int = DEFAULT_WARMUP
    stat_window: int = 60                 # trailing bars fed to the statistics
    max_risk_per_trade_pct: float = 10.0
    stop_move_multiple: float = 2.5
    min_stop_pct: float = 5.0
    max_stop_pct: float = 25.0
    reward_risk_target: float = 2.0
    max_position_pct: float = 60.0        # of equity, per ticker
    max_open_positions: int = 3
    fee_per_trade: float = 0.0
    slippage_pct: float = 0.0             # applied against you on both entry and exit

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class BacktestResult:
    config: dict[str, Any]
    tickers: list[str]
    bars: int
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    final_equity: float = 0.0
    benchmark: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- metrics
    @property
    def total_return_pct(self) -> float:
        start = self.config.get("starting_cash") or 0.0
        return round((self.final_equity / start - 1.0) * 100.0, 2) if start else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        peak, worst = 0.0, 0.0
        for value in self.equity_curve:
            peak = max(peak, value)
            if peak > 0:
                worst = min(worst, value / peak - 1.0)
        return round(worst * 100.0, 2)

    def summary(self) -> dict[str, Any]:
        wins = [t for t in self.trades if t.won]
        losses = [t for t in self.trades if t.pnl < 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        rs = [t.r_multiple for t in self.trades if t.r_multiple is not None]
        by_reason: dict[str, int] = {}
        for trade in self.trades:
            by_reason[trade.exit_reason] = by_reason.get(trade.exit_reason, 0) + 1
        return {
            "trades": len(self.trades), "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(self.trades) * 100.0, 1) if self.trades else None,
            "final_equity": round(self.final_equity, 2), "total_return_pct": self.total_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "expectancy": round(sum(t.pnl for t in self.trades) / len(self.trades), 2) if self.trades else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "avg_held_bars": round(sum(t.held_bars for t in self.trades) / len(self.trades), 1)
            if self.trades else None,
            "exits": by_reason,
            "benchmark": self.benchmark,
            # Sitting in cash and "beating" a falling benchmark is not an edge, it is abstention. The
            # comparison only means something once the rule has actually taken trades.
            "beat_benchmark": (self.total_return_pct > self.benchmark.get("total_return_pct", 0.0)
                               if (self.benchmark and self.trades) else None),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"config": self.config, "tickers": self.tickers, "bars": self.bars,
                "summary": self.summary(), "trades": [t.as_dict() for t in self.trades],
                "equity_curve": [round(v, 2) for v in self.equity_curve], "assumptions": self.assumptions}


# --------------------------------------------------------------------------- history handling
def to_bars(history: list[dict[str, Any]]) -> list[Bar]:
    """Normalise provider history into bars. Accepts the yfinance shape and the mock shape alike."""
    bars: list[Bar] = []
    for row in history or []:
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        label = str(row.get("date") or row.get("day") or len(bars))
        bars.append(Bar(index=len(bars), close=close, label=label))
    return bars


def align(history_by_ticker: dict[str, list[dict[str, Any]]]) -> tuple[list[str], dict[str, list[Bar]], int]:
    """Trim every series to the shortest so one bar index means the same moment for every ticker.

    Both providers return a contiguous recent series on a shared market calendar, so trimming from the front
    keeps the most recent bars aligned. Tickers with too little history are dropped rather than padded.
    """
    series = {t: to_bars(h) for t, h in history_by_ticker.items()}
    series = {t: b for t, b in series.items() if len(b) > MIN_DAYS_FOR_STATS}
    if not series:
        return [], {}, 0
    length = min(len(b) for b in series.values())
    trimmed = {t: [Bar(index=i, close=b.close, label=b.label) for i, b in enumerate(bars[-length:])]
               for t, bars in series.items()}
    return sorted(trimmed), trimmed, length


# --------------------------------------------------------------------------- the engine
@dataclass
class _Open:
    ticker: str
    entry_index: int
    entry_label: str
    entry_price: float
    quantity: float
    stop_price: float
    target_price: float


def _window(closes: list[float], upto: int, size: int) -> list[float]:
    """Closes up to AND INCLUDING bar `upto`. Never reaches into the future."""
    start = max(0, upto + 1 - size)
    return closes[start:upto + 1]


def run_backtest(history_by_ticker: dict[str, list[dict[str, Any]]],
                 config: BacktestConfig | None = None) -> BacktestResult:
    """Walk the bars forward, trading one shared cash pool, using the live risk functions."""
    config = config or BacktestConfig()
    rule = ENTRY_RULES.get(config.entry_rule)
    if rule is None:
        raise ValueError(f"unknown entry rule {config.entry_rule!r}; choose from {sorted(ENTRY_RULES)}")

    tickers, bars_by_ticker, length = align(history_by_ticker)
    result = BacktestResult(config=config.as_dict(), tickers=tickers, bars=length,
                            assumptions=_assumptions(config))
    if not tickers:
        result.final_equity = config.starting_cash
        result.equity_curve = [config.starting_cash]
        return result

    closes = {t: [b.close for b in bars_by_ticker[t]] for t in tickers}
    cash = float(config.starting_cash)
    open_positions: dict[str, _Open] = {}
    slip = config.slippage_pct / 100.0

    for i in range(length):
        # ---- 1. exits first: a breached level matters before any new idea
        for ticker in sorted(list(open_positions)):
            position = open_positions[ticker]
            price = closes[ticker][i]
            signal = exit_signal(price, position.stop_price, position.target_price)
            if signal is None:
                continue
            fill = price * (1 - slip)                      # slippage works against you on the way out
            proceeds = position.quantity * fill - config.fee_per_trade
            cash += proceeds
            entry_cost = position.quantity * position.entry_price + config.fee_per_trade
            result.trades.append(BacktestTrade(
                ticker=ticker, entry_index=position.entry_index, entry_label=position.entry_label,
                entry_price=round(position.entry_price, 6), exit_index=i,
                exit_label=bars_by_ticker[ticker][i].label, exit_price=round(fill, 6),
                quantity=position.quantity, stop_price=position.stop_price, target_price=position.target_price,
                pnl=round(proceeds - entry_cost, 4), exit_reason=signal))
            del open_positions[ticker]

        # ---- 2. entries, only after the warmup and only with cash to spare
        equity_now = cash + sum(p.quantity * closes[p.ticker][i] for p in open_positions.values())
        if i >= max(config.warmup, MIN_DAYS_FOR_STATS):
            for ticker in tickers:
                if ticker in open_positions or len(open_positions) >= config.max_open_positions:
                    continue
                window = _window(closes[ticker], i, config.stat_window)
                stats = compute_stats([{"close": c} for c in window])
                if not stats.sufficient or not rule(window, stats):
                    continue
                price = closes[ticker][i] * (1 + slip)     # and against you on the way in
                levels = compute_levels(price, stats, stop_move_multiple=config.stop_move_multiple,
                                        min_stop_pct=config.min_stop_pct, max_stop_pct=config.max_stop_pct,
                                        reward_risk_target=config.reward_risk_target)
                if risk_per_share(price, levels.stop_price) <= 0:
                    continue
                by_risk = max_quantity_for_risk(equity_now, price, levels.stop_price,
                                                config.max_risk_per_trade_pct)
                by_cash = max(0.0, cash - config.fee_per_trade) / price
                by_concentration = equity_now * config.max_position_pct / 100.0 / price
                quantity = min(by_risk, by_cash, by_concentration)
                if quantity <= 0 or quantity * price < 1.0:
                    continue
                cash -= quantity * price + config.fee_per_trade
                open_positions[ticker] = _Open(ticker=ticker, entry_index=i,
                                               entry_label=bars_by_ticker[ticker][i].label,
                                               entry_price=price, quantity=quantity,
                                               stop_price=levels.stop_price, target_price=levels.target_price)

        result.equity_curve.append(cash + sum(p.quantity * closes[p.ticker][i] for p in open_positions.values()))

    # ---- close anything still open at the last bar, so the result is not flattered by an unrealised winner
    last = length - 1
    for ticker, position in sorted(open_positions.items()):
        fill = closes[ticker][last] * (1 - slip)
        proceeds = position.quantity * fill - config.fee_per_trade
        cash += proceeds
        entry_cost = position.quantity * position.entry_price + config.fee_per_trade
        result.trades.append(BacktestTrade(
            ticker=ticker, entry_index=position.entry_index, entry_label=position.entry_label,
            entry_price=round(position.entry_price, 6), exit_index=last,
            exit_label=bars_by_ticker[ticker][last].label, exit_price=round(fill, 6),
            quantity=position.quantity, stop_price=position.stop_price, target_price=position.target_price,
            pnl=round(proceeds - entry_cost, 4), exit_reason="end_of_data"))
    result.final_equity = cash
    if result.equity_curve:
        result.equity_curve[-1] = cash
    result.benchmark = buy_and_hold(bars_by_ticker, config.starting_cash, config.warmup)
    return result


def buy_and_hold(bars_by_ticker: dict[str, list[Bar]], starting_cash: float, warmup: int) -> dict[str, Any]:
    """The benchmark that matters: split the cash evenly at the first tradable bar and do nothing.

    Without this a positive return is meaningless, because the market may simply have gone up.
    """
    tickers = sorted(bars_by_ticker)
    if not tickers:
        return {}
    length = len(bars_by_ticker[tickers[0]])
    start = min(max(warmup, MIN_DAYS_FOR_STATS), length - 1)
    per_ticker = starting_cash / len(tickers)
    final = 0.0
    for ticker in tickers:
        bars = bars_by_ticker[ticker]
        entry, exit_price = bars[start].close, bars[-1].close
        final += per_ticker * (exit_price / entry) if entry else per_ticker
    return {"final_equity": round(final, 2),
            "total_return_pct": round((final / starting_cash - 1.0) * 100.0, 2) if starting_cash else 0.0,
            "method": f"equal weight across {len(tickers)} ticker(s) from bar {start}, held to the end"}


def _assumptions(config: BacktestConfig) -> list[str]:
    """Stated on every result. A backtest read without these is worse than no backtest."""
    return [
        "Exits are detected on CLOSING prices only, because the provider supplies closes. A real stop triggers "
        "intraday and often fills worse, and a gap can skip it entirely, so losses here are optimistic.",
        "Only securities with fetchable history are included. Companies that went to zero are absent, which "
        "flatters any result (survivorship bias).",
        f"Commission {config.fee_per_trade:.2f} per trade and slippage {config.slippage_pct:.2f}% per side. "
        "Zero is realistic for a fractional-share broker but optimistic for a thin security.",
        "The agents' judgement is NOT tested. A mechanical entry rule stands in for the entry signal, so this "
        "measures the stop, sizing and exit discipline, not stock selection.",
        "Tuning these settings until the result looks good produces a curve fit, not an edge. Compare against "
        "the buy-and-hold benchmark and treat a small number of trades as noise.",
        "Past price behaviour does not predict future returns.",
    ]
