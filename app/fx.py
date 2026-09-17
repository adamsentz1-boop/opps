"""Currency normalisation to USD.

Worldwide tenders arrive in many currencies, and every threshold and comparison in the engine is in USD.
Rates are a LOCAL, APPROXIMATE, owner-editable table (Settings -> fx_rates) rather than a live FX API: the
system stays local-first and never depends on a third-party call to score an opportunity. Rates only need to
be good enough for "is this deal big enough to bid on".
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

# USD per 1 unit of currency. Approximate; edit in Settings.
DEFAULT_RATES: dict[str, float] = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66, "NZD": 0.61, "CHF": 1.12,
    "SEK": 0.094, "NOK": 0.092, "DKK": 0.145, "PLN": 0.25, "CZK": 0.043, "HUF": 0.0028, "RON": 0.22,
    "BGN": 0.55, "HRK": 0.14, "ISK": 0.0072, "TRY": 0.029, "ILS": 0.27, "AED": 0.27, "SAR": 0.27,
    "QAR": 0.27, "ZAR": 0.055, "INR": 0.012, "SGD": 0.74, "HKD": 0.128, "JPY": 0.0064, "KRW": 0.00073,
    "MYR": 0.22, "PHP": 0.017, "THB": 0.028, "IDR": 0.000062, "CNY": 0.14, "BRL": 0.18, "MXN": 0.050,
    "CLP": 0.0010, "COP": 0.00024, "ARS": 0.0010,
}


def load_rates(raw: str | dict | None) -> dict[str, float]:
    if isinstance(raw, dict):
        table = {**DEFAULT_RATES, **{k.upper(): float(v) for k, v in raw.items()}}
        return table
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return {**DEFAULT_RATES, **{str(k).upper(): float(v) for k, v in data.items()}}
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("fx_rates setting is not valid JSON; using defaults")
    return dict(DEFAULT_RATES)


def to_usd(amount: float | None, currency: str | None, rates: dict[str, float] | None = None) -> float | None:
    """Convert to USD. Returns None when the amount is missing or the currency is unknown."""
    if amount is None:
        return None
    table = rates or DEFAULT_RATES
    code = (currency or "USD").strip().upper()
    rate = table.get(code)
    if rate is None:
        log.info("no FX rate for %s; leaving value unconverted", code)
        return None
    return round(float(amount) * rate, 2)


def known_currency(currency: str | None, rates: dict[str, float] | None = None) -> bool:
    return (currency or "USD").strip().upper() in (rates or DEFAULT_RATES)
