"""Deterministic price statistics computed in Python, not by the model.

The research agent used to receive a raw list of closing prices and was expected to infer trend and
volatility from it. Language models are poor arithmetic engines, so the numbers that matter are computed
here and handed over as facts. That keeps the agent doing what it is good at - weighing a situation - and
keeps the measurements reproducible, testable and identical between mock and live mode.

Everything is close-to-close. `get_history` only returns closing prices, so there is no true intraday range;
`avg_daily_move_pct` is the mean absolute daily return, an honest close-only stand-in for average true range.
Names say what the number actually is rather than borrowing a term that implies data we do not have.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

MIN_DAYS_FOR_STATS = 5


def _closes(history: list[dict[str, Any]] | None) -> list[float]:
    out: list[float] = []
    for row in history or []:
        value = row.get("close") if isinstance(row, dict) else row
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0 and math.isfinite(price):
            out.append(price)
    return out


def _volumes(history: list[dict[str, Any]] | None) -> list[float]:
    out: list[float] = []
    for row in history or []:
        if not isinstance(row, dict) or "volume" not in row:
            continue
        try:
            volume = float(row["volume"])
        except (TypeError, ValueError):
            continue
        if volume >= 0 and math.isfinite(volume):
            out.append(volume)
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def daily_returns(closes: list[float]) -> list[float]:
    return [(closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]


@dataclass
class PriceStats:
    """A compact, honest summary of what the supplied price history shows."""
    data_days: int = 0
    sufficient: bool = False
    last_close: float | None = None
    period_high: float | None = None
    period_low: float | None = None
    sma_short: float | None = None
    sma_long: float | None = None
    trend: str = "unknown"                      # rising | falling | sideways | unknown
    return_pct: float | None = None             # over the whole supplied window
    avg_daily_move_pct: float | None = None     # mean absolute daily return (close-only ATR stand-in)
    daily_volatility_pct: float | None = None   # stdev of daily returns
    annualised_volatility_pct: float | None = None
    drawdown_from_high_pct: float | None = None
    range_position_pct: float | None = None     # 0 = at the period low, 100 = at the period high
    volume_trend: str = "unknown"               # rising | falling | steady | unknown
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def compute_stats(history: list[dict[str, Any]] | None, current_price: float | None = None,
                  short_window: int = 10, long_window: int = 30) -> PriceStats:
    """Summarise a price history. Degrades gracefully: too little data yields `sufficient=False`."""
    closes = _closes(history)
    if current_price and current_price > 0:
        closes = closes + [float(current_price)]
    stats = PriceStats(data_days=len(closes))
    if len(closes) < MIN_DAYS_FOR_STATS:
        stats.notes.append(f"only {len(closes)} usable price points; statistics not computed")
        stats.last_close = closes[-1] if closes else None
        return stats

    stats.sufficient = True
    stats.last_close = round(closes[-1], 4)
    stats.period_high = round(max(closes), 4)
    stats.period_low = round(min(closes), 4)
    stats.sma_short = round(_mean(closes[-short_window:]) or 0.0, 4)
    stats.sma_long = round(_mean(closes[-long_window:]) or 0.0, 4)

    returns = daily_returns(closes)
    if returns:
        stats.return_pct = round((closes[-1] / closes[0] - 1.0) * 100.0, 2)
        stats.avg_daily_move_pct = round((_mean([abs(r) for r in returns]) or 0.0) * 100.0, 3)
        daily_sd = _stdev(returns)
        if daily_sd is not None:
            stats.daily_volatility_pct = round(daily_sd * 100.0, 3)
            stats.annualised_volatility_pct = round(daily_sd * math.sqrt(252) * 100.0, 2)

    if stats.period_high:
        stats.drawdown_from_high_pct = round((closes[-1] / stats.period_high - 1.0) * 100.0, 2)
    span = (stats.period_high or 0) - (stats.period_low or 0)
    stats.range_position_pct = round((closes[-1] - stats.period_low) / span * 100.0, 1) if span > 0 else 50.0

    if stats.sma_short and stats.sma_long:
        gap_pct = (stats.sma_short / stats.sma_long - 1.0) * 100.0
        stats.trend = "rising" if gap_pct > 1.0 else "falling" if gap_pct < -1.0 else "sideways"

    volumes = _volumes(history)
    if len(volumes) >= MIN_DAYS_FOR_STATS:
        half = len(volumes) // 2
        earlier, recent = _mean(volumes[:half]), _mean(volumes[half:])
        if earlier and recent:
            change = recent / earlier - 1.0
            stats.volume_trend = "rising" if change > 0.2 else "falling" if change < -0.2 else "steady"

    if len(closes) < long_window:
        stats.notes.append(f"window shorter than {long_window} days; longer-term averages are approximate")
    return stats
