"""Preflight check: will this actually work here?

    python -m scripts.doctor              # full check, including a live market-data fetch
    python -m scripts.doctor --offline    # skip anything that touches the network

Answers the questions that otherwise turn into a confusing empty dashboard: are the dependencies installed,
is there a database and is its schema current, is Claude live or mocked, **can the app actually fetch a stock
quote**, and is there anything on the watchlist to scan.

Secrets are never printed. Keys are reported as configured or not, never shown.
Exit code is 0 when nothing is broken, 1 when something needs fixing.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}

REQUIRED_PACKAGES = ("fastapi", "uvicorn", "sqlalchemy", "jinja2", "pydantic", "apscheduler", "httpx")


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, name: str, status: str, detail: str, fix: str = "") -> None:
        self.rows.append((name, status, detail, fix))

    @property
    def failed(self) -> bool:
        return any(status == FAIL for _, status, _, _ in self.rows)

    def render(self) -> str:
        width = max((len(name) for name, *_ in self.rows), default=10)
        lines = []
        for name, status, detail, fix in self.rows:
            lines.append(f"[{_MARK[status]}] {name.ljust(width)}  {detail}")
            if fix:
                lines.append(f"{' ' * (width + 11)}-> {fix}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- checks
def check_python(report: Report) -> None:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version < (3, 10):
        report.add("python", FAIL, f"{text} is too old", "install Python 3.11 or newer")
    else:
        report.add("python", OK, text)


def check_packages(report: Report) -> None:
    missing = [name for name in REQUIRED_PACKAGES if not _importable(name)]
    if missing:
        report.add("dependencies", FAIL, f"missing: {', '.join(missing)}",
                   "pip install -r requirements.txt")
    else:
        report.add("dependencies", OK, f"{len(REQUIRED_PACKAGES)} core packages present")
    if _importable("yfinance"):
        report.add("yfinance", OK, "installed")
    else:
        report.add("yfinance", WARN, "not installed",
                   "pip install -r requirements.txt, or set MARKET_MOCK=true to run offline")


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001 - a broken install counts as missing
        return False


def check_env_file(report: Report, root: Path) -> None:
    if (root / ".env").exists():
        report.add(".env", OK, "present")
    elif (root / ".env.example").exists():
        report.add(".env", WARN, "not found; defaults will be used",
                   "cp .env.example .env, then edit it")
    else:
        report.add(".env", WARN, "not found and no example to copy")


def check_database(report: Report) -> None:
    from app.db import ensure_columns, get_engine, init_db

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        report.add("database", FAIL, f"could not open: {type(exc).__name__}: {str(exc)[:80]}",
                   "check DATABASE_URL and that the data/ directory is writable")
        return
    url = str(get_engine().url)
    report.add("database", OK, url)
    pending = ensure_columns()
    if pending:
        report.add("schema", OK, f"upgraded {len(pending)} column(s): {', '.join(pending[:4])}")
    else:
        report.add("schema", OK, "current")


def check_llm(report: Report) -> None:
    from app.config import get_settings

    settings = get_settings()
    if settings.llm_enabled:
        report.add("claude", OK, f"live, model {settings.claude_model}, effort {settings.claude_effort}")
    elif settings.llm_mock:
        report.add("claude", WARN, "MOCK mode forced by LLM_MOCK=true",
                   "set LLM_MOCK=false and add ANTHROPIC_API_KEY for real analysis")
    else:
        report.add("claude", WARN, "MOCK mode (no ANTHROPIC_API_KEY)",
                   "add ANTHROPIC_API_KEY to .env for real agent analysis")


def check_market_data(report: Report, offline: bool) -> None:
    """The one that matters. Everything in the Market Challenge is inert without a working quote."""
    from app.config import get_settings
    from app.market.data import get_provider, provider_name

    settings = get_settings()
    if not settings.market_challenge_enabled:
        report.add("market data", WARN, "Market Challenge disabled",
                   "set MARKET_CHALLENGE_ENABLED=true to use it")
        return
    name = provider_name()
    if settings.market_mock_enabled:
        report.add("market data", WARN, f"provider {name} (deterministic fake prices)",
                   "set MARKET_MOCK=false and MARKET_DATA_PROVIDER=yfinance for real quotes")
        return
    if offline:
        report.add("market data", WARN, f"provider {name}, not tested (--offline)")
        return
    probe = "SPY"
    try:
        quote = get_provider().get_quote(probe)
    except Exception as exc:  # noqa: BLE001
        report.add("market data", FAIL, f"{name} could not fetch {probe}: {type(exc).__name__}: {str(exc)[:70]}",
                   "check outbound network access, or set MARKET_MOCK=true to work offline")
        return
    if not quote.price or quote.price <= 0:
        report.add("market data", FAIL, f"{name} returned no price for {probe}",
                   "the provider is reachable but not returning data; try again or use MARKET_MOCK=true")
        return
    report.add("market data", OK, f"{name} live: {probe} at ${quote.price:,.2f}")
    try:
        bars = get_provider().get_history(probe, days=60)
        if len(bars) >= 20:
            report.add("price history", OK, f"{len(bars)} bars for {probe} (backtests will work)")
        else:
            report.add("price history", WARN, f"only {len(bars)} bars returned",
                       "backtests need more history to be meaningful")
    except Exception as exc:  # noqa: BLE001
        report.add("price history", WARN, f"unavailable: {type(exc).__name__}",
                   "quotes work but backtests need history")


def check_challenge(report: Report) -> None:
    from app.db import session_scope
    from app.market.portfolio import days_remaining, get_or_create_challenge
    from app.models import MarketWatchlist

    with session_scope() as db:
        challenge = get_or_create_challenge(db)
        left = days_remaining(challenge)
        report.add("challenge", OK,
                   f"{challenge.name}: ${challenge.current_portfolio_value:,.2f} of "
                   f"${challenge.target_value:,.0f}, {left} days left")
        watched = db.query(MarketWatchlist).filter(MarketWatchlist.enabled.is_(True)).count()
        if watched:
            report.add("watchlist", OK, f"{watched} ticker(s) enabled")
        else:
            report.add("watchlist", WARN, "empty, so scans have nothing to research",
                       "add tickers at /market/settings (the system never seeds recommendations)")
        positions = [p for p in challenge.positions if p.quantity > 0]
        unprotected = [p.ticker for p in positions if not p.stop_price]
        if unprotected:
            report.add("open positions", WARN, f"no stop set on {', '.join(unprotected)}",
                       "the exit monitor cannot watch a position with no levels")
        elif positions:
            report.add("open positions", OK, f"{len(positions)} position(s), all with stops")


def check_scheduler(report: Report) -> None:
    from app.config import get_settings

    settings = get_settings()
    parts = []
    parts.append(f"opportunity scan every {settings.scan_interval_minutes} min"
                 if settings.scheduler_enabled else "opportunity scan off")
    if settings.market_challenge_enabled and settings.market_scan_enabled and settings.scheduler_enabled:
        parts.append(f"market scan every {settings.market_scan_interval_minutes} min")
    else:
        parts.append("market scan off")
    report.add("scheduler", OK, "; ".join(parts))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="skip checks that need the network")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    report = Report()
    print("Opportunity Engine preflight\n")

    check_python(report)
    check_packages(report)
    check_env_file(report, root)
    if report.failed:
        print(report.render())
        print("\nFix the failures above before starting the app.")
        return 1

    check_database(report)
    check_llm(report)
    check_market_data(report, args.offline)
    check_challenge(report)
    check_scheduler(report)

    print(report.render())
    warnings = sum(1 for _, status, _, _ in report.rows if status == WARN)
    if report.failed:
        print("\nSomething is broken. Fix the FAIL rows above, then run this again.")
        return 1
    if warnings:
        print(f"\nReady to start, with {warnings} thing(s) worth knowing about above.")
    else:
        print("\nAll good. Start the app with ./start.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
