"""Market data providers (READ-ONLY quotes). No brokerage, no orders, no credentials.

`MarketDataProvider` is the abstraction; `YFinanceProvider` uses the public yfinance library for prices and
`MockMarketDataProvider` returns deterministic fake prices for tests and offline use (MARKET_MOCK=true).
Fetched values are stored as `MarketSnapshot` rows. Provider failures never raise out of `refresh_quotes`;
they are recorded in the audit log and the scan continues with the last known snapshot.

Everything returned by a provider is treated as untrusted external DATA (see app/sanitize.py) - it is never
interpreted as an instruction by the agents.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.audit import log_event
from app.config import get_settings
from app.market.universe import normalise_asset_type, normalise_ticker
from app.models import MarketSnapshot, utcnow
from app.sanitize import clean_text

log = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    pass


@dataclass
class Quote:
    ticker: str
    price: float
    open_price: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: float | None = None
    market_cap: float | None = None
    asset_type: str = "unknown"
    name: str | None = None
    currency: str | None = "USD"
    captured_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def change_pct(self) -> float | None:
        if self.previous_close:
            return round((self.price - self.previous_close) / self.previous_close * 100.0, 2)
        return None


class MarketDataProvider(Protocol):
    name: str

    def get_quote(self, ticker: str) -> Quote: ...

    def get_history(self, ticker: str, days: int = 30) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- mock provider
class MockMarketDataProvider:
    """Deterministic fake prices derived from the ticker string. Supports explicit overrides for tests."""
    name = "mock"

    def __init__(self) -> None:
        self._overrides: dict[str, float] = {}

    def set_price(self, ticker: str, price: float) -> None:
        self._overrides[normalise_ticker(ticker)] = float(price)

    def clear(self) -> None:
        self._overrides.clear()

    @staticmethod
    def _seed(text: str) -> int:
        return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)

    def base_price(self, ticker: str) -> float:
        seed = self._seed(normalise_ticker(ticker))
        return round(10.0 + (seed % 39000) / 100.0, 2)          # 10.00 .. 400.00

    def get_quote(self, ticker: str) -> Quote:
        ticker = normalise_ticker(ticker)
        seed = self._seed(ticker)
        price = self._overrides.get(ticker, self.base_price(ticker))
        drift = ((seed >> 8) % 600 - 300) / 10000.0                # -3% .. +3%
        prev = round(price / (1.0 + drift), 2) if price else None
        asset_type = "etf" if ticker in ("SPY", "QQQ", "VTI", "VOO", "IWM", "DIA") or ticker.startswith("X") else "stock"
        return Quote(ticker=ticker, price=price, open_price=prev, high=round(max(price, prev or price) * 1.01, 2),
                     low=round(min(price, prev or price) * 0.99, 2), previous_close=prev,
                     volume=float(1_000_000 + seed % 9_000_000), market_cap=float(price * (seed % 900 + 100) * 1e6),
                     asset_type=asset_type, name=f"Mock {ticker} {'ETF' if asset_type == 'etf' else 'Corp'}",
                     currency="USD", raw={"provider": "mock", "deterministic": True})

    def get_history(self, ticker: str, days: int = 30) -> list[dict[str, Any]]:
        q = self.get_quote(ticker)
        seed = self._seed(normalise_ticker(ticker))
        out = []
        for i in range(days, 0, -1):
            wiggle = math.sin((seed % 97) + i / 3.0) * 0.02
            out.append({"day": -i, "close": round(q.price * (1.0 + wiggle), 2)})
        return out


# --------------------------------------------------------------------------- yfinance provider
class YFinanceProvider:
    """Read-only quotes via the yfinance library (public Yahoo Finance data). No account, no orders."""
    name = "yfinance"

    def __init__(self) -> None:
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise MarketDataError("yfinance is not installed; pip install yfinance or set MARKET_MOCK=true") from exc

    @staticmethod
    def _num(value: Any) -> float | None:
        try:
            if value is None:
                return None
            value = float(value)
            return None if math.isnan(value) else value
        except (TypeError, ValueError):
            return None

    def get_quote(self, ticker: str) -> Quote:
        import yfinance as yf
        ticker = normalise_ticker(ticker)
        t = yf.Ticker(ticker)
        fast: dict[str, Any] = {}
        try:
            fi = t.fast_info
            for key in ("last_price", "lastPrice", "open", "day_high", "dayHigh", "day_low", "dayLow",
                        "previous_close", "previousClose", "last_volume", "lastVolume", "market_cap", "marketCap",
                        "currency", "quote_type", "quoteType"):
                try:
                    fast[key] = fi[key]
                except (KeyError, AttributeError, TypeError):
                    continue
        except Exception as exc:  # noqa: BLE001
            log.warning("yfinance fast_info failed for %s: %s", ticker, exc)
        price = self._num(fast.get("last_price", fast.get("lastPrice")))
        info: dict[str, Any] = {}
        if price is None or not fast.get("quote_type", fast.get("quoteType")):
            try:
                info = dict(t.info or {})
            except Exception as exc:  # noqa: BLE001
                log.warning("yfinance info failed for %s: %s", ticker, exc)
        if price is None:
            price = self._num(info.get("currentPrice") or info.get("regularMarketPrice"))
        if price is None:
            raise MarketDataError(f"No price available for {ticker}")
        quote_type = fast.get("quote_type") or fast.get("quoteType") or info.get("quoteType")
        name = info.get("longName") or info.get("shortName")
        raw = {"fast_info": {k: (v if isinstance(v, (int, float, str)) else str(v)) for k, v in fast.items()},
               "info_subset": {k: info.get(k) for k in ("longName", "shortName", "quoteType", "sector", "industry",
                                                         "marketCap", "regularMarketPrice") if k in info}}
        return Quote(
            ticker=ticker, price=round(price, 4),
            open_price=self._num(fast.get("open") or info.get("open") or info.get("regularMarketOpen")),
            high=self._num(fast.get("day_high", fast.get("dayHigh")) or info.get("dayHigh")),
            low=self._num(fast.get("day_low", fast.get("dayLow")) or info.get("dayLow")),
            previous_close=self._num(fast.get("previous_close", fast.get("previousClose")) or info.get("previousClose")),
            volume=self._num(fast.get("last_volume", fast.get("lastVolume")) or info.get("volume")),
            market_cap=self._num(fast.get("market_cap", fast.get("marketCap")) or info.get("marketCap")),
            asset_type=normalise_asset_type(quote_type), name=clean_text(name, 255) if name else None,
            currency=str(fast.get("currency") or info.get("currency") or "USD")[:8], raw=raw,
        )

    def get_history(self, ticker: str, days: int = 30) -> list[dict[str, Any]]:
        import yfinance as yf
        hist = yf.Ticker(normalise_ticker(ticker)).history(period=f"{max(5, days)}d")
        out = []
        for idx, row in hist.iterrows():
            out.append({"date": str(getattr(idx, "date", lambda: idx)()), "close": round(float(row["Close"]), 4),
                        "volume": float(row.get("Volume", 0) or 0)})
        return out[-days:]


# --------------------------------------------------------------------------- registry
_provider: MarketDataProvider | None = None


def get_provider() -> MarketDataProvider:
    """Provider singleton chosen by MARKET_DATA_PROVIDER / MARKET_MOCK. Unknown or broken providers fall back to mock."""
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.market_mock_enabled:
            _provider = MockMarketDataProvider()
        elif settings.market_data_provider.strip().lower() == "yfinance":
            try:
                _provider = YFinanceProvider()
            except MarketDataError as exc:
                log.error("%s - falling back to MOCK market data", exc)
                _provider = MockMarketDataProvider()
        else:
            log.error("Unknown MARKET_DATA_PROVIDER=%s - using MOCK market data", settings.market_data_provider)
            _provider = MockMarketDataProvider()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None


def provider_name() -> str:
    return get_provider().name


# --------------------------------------------------------------------------- snapshots
def store_quote(db: Session, quote: Quote) -> MarketSnapshot:
    snap = MarketSnapshot(ticker=quote.ticker, price=quote.price, open_price=quote.open_price, high=quote.high,
                          low=quote.low, previous_close=quote.previous_close, volume=quote.volume,
                          market_cap=quote.market_cap, asset_type=quote.asset_type, name=quote.name,
                          currency=quote.currency, provider=get_provider().name, captured_at=quote.captured_at,
                          raw_payload=quote.raw)
    db.add(snap)
    db.flush()
    return snap


def latest_snapshot(db: Session, ticker: str) -> MarketSnapshot | None:
    return (db.query(MarketSnapshot).filter(MarketSnapshot.ticker == normalise_ticker(ticker))
            .order_by(MarketSnapshot.captured_at.desc()).first())


def latest_snapshots(db: Session, tickers: list[str]) -> dict[str, MarketSnapshot]:
    return {t: s for t in tickers if (s := latest_snapshot(db, t)) is not None}


def refresh_quotes(db: Session, tickers: list[str], *, agent: str = "System") -> tuple[dict[str, MarketSnapshot], list[str]]:
    """Fetch and store a quote per ticker. Returns (snapshots, errors). Never raises for provider problems."""
    provider = get_provider()
    snapshots: dict[str, MarketSnapshot] = {}
    errors: list[str] = []
    for raw in tickers:
        ticker = normalise_ticker(raw)
        if not ticker:
            continue
        try:
            quote = provider.get_quote(ticker)
            snap = store_quote(db, quote)
            snapshots[ticker] = snap
            log_event(db, agent=agent, action="market.data.retrieved", object_type="market_snapshot", object_id=snap.id,
                      details={"ticker": ticker, "price": quote.price, "provider": provider.name,
                               "asset_type": quote.asset_type})
        except Exception as exc:  # noqa: BLE001 - market data failures must never crash the app
            msg = f"{ticker}: {type(exc).__name__}: {str(exc)[:200]}"
            errors.append(msg)
            log.warning("market data failure %s", msg)
            log_event(db, agent=agent, action="market.data.error", object_type="market_snapshot", object_id=None,
                      details={"ticker": ticker, "error": msg, "provider": provider.name})
            cached = latest_snapshot(db, ticker)
            if cached is not None:
                snapshots[ticker] = cached
    return snapshots, errors
