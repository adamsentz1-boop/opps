"""Permitted stock universe for the Market Challenge (V1).

Long-only cash account: ordinary stocks and ETFs, fractional shares allowed.
No options, futures, forex, crypto, leveraged/inverse products, short selling, margin or borrowing.
"""
from __future__ import annotations

import re

from app.config import get_settings

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z]{1,2})?$")

# Asset types we can recognise from providers, normalised to our vocabulary.
_ASSET_TYPE_MAP = {
    "equity": "stock", "stock": "stock", "stocks": "stock", "common stock": "stock",
    "etf": "etf", "etfs": "etf", "exchange traded fund": "etf",
    "mutualfund": "mutual_fund", "mutual fund": "mutual_fund",
    "option": "option", "future": "future", "futures": "future", "currency": "forex", "forex": "forex",
    "cryptocurrency": "crypto", "crypto": "crypto", "index": "index", "warrant": "warrant",
}
_ALLOWED_NORMALISED = {"stocks": "stock", "etfs": "etf", "stock": "stock", "etf": "etf"}

_LEVERAGED_NAME_MARKERS = ("2x", "3x", "-1x", "-2x", "-3x", "ultra", "ultrapro", "inverse", "leveraged", "bull 2",
                           "bull 3", "bear 2", "bear 3", "daily 2", "daily 3", "short s&p", "short qqq")


def normalise_ticker(raw: str) -> str:
    return (raw or "").strip().upper().replace("$", "")


def is_valid_ticker(ticker: str) -> bool:
    """Reject anything that is not a plain stock/ETF symbol (option chains, crypto pairs, futures, forex)."""
    if not ticker or not TICKER_RE.match(ticker):
        return False
    if ticker.endswith("=F") or ticker.endswith("=X") or "-USD" in ticker:
        return False
    return True


def normalise_asset_type(raw: str | None) -> str:
    if not raw:
        return "unknown"
    return _ASSET_TYPE_MAP.get(str(raw).strip().lower(), str(raw).strip().lower())


def allowed_asset_types() -> set[str]:
    return {_ALLOWED_NORMALISED[a] for a in get_settings().market_asset_types if a in _ALLOWED_NORMALISED}


def looks_leveraged(name: str | None) -> bool:
    lower = (name or "").lower()
    return any(marker in lower for marker in _LEVERAGED_NAME_MARKERS)


def check_universe(ticker: str, asset_type: str | None, name: str | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). 'unknown' asset types are allowed only for plain valid symbols."""
    if not is_valid_ticker(ticker):
        return False, f"{ticker!r} is not a plain stock/ETF symbol"
    kind = normalise_asset_type(asset_type)
    allowed = allowed_asset_types()
    if kind not in allowed and kind != "unknown":
        return False, f"asset type {kind} is not permitted (allowed: {', '.join(sorted(allowed))})"
    if looks_leveraged(name):
        return False, "leveraged/inverse products are not permitted"
    return True, ""


UNIVERSE_RULES = [
    "Long-only cash account", "Ordinary stocks and ETFs only", "Fractional shares allowed",
    "No options, futures, forex or crypto", "No leveraged or inverse products", "No short selling",
    "No margin, no borrowing, no negative cash",
]
