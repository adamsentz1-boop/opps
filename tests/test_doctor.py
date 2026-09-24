"""Preflight diagnostics. The doctor is the first thing a new user runs, so it must never lie or leak a key."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.doctor import (FAIL, OK, WARN, Report, check_challenge, check_env_file, check_llm,
                            check_market_data, check_packages, check_python, check_scheduler, main)


def _row(report: Report, name: str) -> tuple[str, str, str, str]:
    return next(r for r in report.rows if r[0] == name)


def test_report_tracks_failure_and_renders():
    report = Report()
    report.add("alpha", OK, "fine")
    report.add("beta", WARN, "iffy", "do this")
    assert report.failed is False
    report.add("gamma", FAIL, "broken", "fix it")
    assert report.failed is True
    rendered = report.render()
    assert "alpha" in rendered and "-> do this" in rendered and "FAIL" in rendered


def test_python_and_packages_pass_in_this_environment():
    report = Report()
    check_python(report)
    check_packages(report)
    assert _row(report, "python")[1] == OK
    assert _row(report, "dependencies")[1] == OK


def test_env_file_check(tmp_path):
    report = Report()
    check_env_file(report, tmp_path)
    assert _row(report, ".env")[1] == WARN          # neither .env nor an example
    (tmp_path / ".env.example").write_text("X=1")
    report = Report()
    check_env_file(report, tmp_path)
    assert "cp .env.example" in _row(report, ".env")[3]
    (tmp_path / ".env").write_text("X=1")
    report = Report()
    check_env_file(report, tmp_path)
    assert _row(report, ".env")[1] == OK


def test_mock_market_data_warns_rather_than_failing(db):
    """Mock prices are a legitimate way to run, so they must not look like a broken install."""
    report = Report()
    check_market_data(report, offline=False)
    name, status, detail, fix = _row(report, "market data")
    assert status == WARN and "mock" in detail
    assert "MARKET_MOCK=false" in fix


def test_offline_skips_the_network_probe(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("MARKET_MOCK", "false")
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
    get_settings.cache_clear()
    try:
        report = Report()
        check_market_data(report, offline=True)
        assert _row(report, "market data")[1] == WARN
        assert "not tested" in _row(report, "market data")[2]
    finally:
        get_settings.cache_clear()


def test_an_unreachable_provider_is_a_hard_failure(db, monkeypatch):
    """This is the check that matters: a dead data feed makes the whole market module inert."""
    from app.config import get_settings
    from app.market.data import get_provider

    monkeypatch.setenv("MARKET_MOCK", "false")
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
    get_settings.cache_clear()
    provider = get_provider()

    def boom(ticker):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(provider, "get_quote", boom)
    try:
        report = Report()
        check_market_data(report, offline=False)
        name, status, detail, fix = _row(report, "market data")
        assert status == FAIL and "could not fetch SPY" in detail
        assert "outbound network" in fix and report.failed
    finally:
        get_settings.cache_clear()


def test_challenge_check_flags_an_empty_watchlist(db):
    report = Report()
    check_challenge(report)
    assert _row(report, "challenge")[1] == OK
    assert "$200.00" in _row(report, "challenge")[2]
    assert _row(report, "watchlist")[1] == WARN
    assert "/market/settings" in _row(report, "watchlist")[3]


def test_challenge_check_flags_a_position_with_no_stop(db):
    from app.market.portfolio import get_or_create_challenge
    from app.models import MarketPosition, MarketWatchlist

    challenge = get_or_create_challenge(db)
    db.add(MarketWatchlist(ticker="NVDA"))
    db.add(MarketPosition(challenge_id=challenge.id, ticker="NVDA", quantity=1.0, average_cost=10.0,
                          current_price=10.0, market_value=10.0))
    db.commit()
    report = Report()
    check_challenge(report)
    assert _row(report, "watchlist")[1] == OK
    assert _row(report, "open positions")[1] == WARN
    assert "NVDA" in _row(report, "open positions")[2]


def test_llm_and_scheduler_are_reported(db):
    report = Report()
    check_llm(report)
    check_scheduler(report)
    assert _row(report, "claude")[1] == WARN            # tests force mock mode
    assert "scan" in _row(report, "scheduler")[2]


def test_doctor_never_prints_a_secret(db, monkeypatch, capsys):
    secret = "sk-ant-do-not-leak-me"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("LLM_MOCK", "false")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        monkeypatch.setattr("sys.argv", ["doctor", "--offline"])
        main()
        captured = capsys.readouterr()
        assert secret not in captured.out and secret not in captured.err
        assert "claude" in captured.out
    finally:
        get_settings.cache_clear()


def test_doctor_exits_zero_when_nothing_is_broken(db, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["doctor", "--offline"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "Opportunity Engine preflight" in out
    assert "Ready to start" in out or "All good" in out


def test_start_script_is_executable_and_valid():
    """start.sh is the front door; a syntax error there is the worst possible first impression."""
    import subprocess

    script = Path(__file__).resolve().parent.parent / "start.sh"
    assert script.exists(), "start.sh is missing"
    assert script.stat().st_mode & 0o111, "start.sh is not executable"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, f"start.sh has a syntax error: {result.stderr}"
    body = script.read_text()
    assert "set -euo pipefail" in body        # fail loudly rather than half-starting
    for flag in ("--check", "--docker", "--seed", "--test", "--offline", "--port"):
        assert flag in body, f"{flag} is documented but not handled"


def test_help_text_shows_only_documentation():
    import subprocess

    script = Path(__file__).resolve().parent.parent / "start.sh"
    result = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "./start.sh --check" in result.stdout
    assert "set -euo pipefail" not in result.stdout       # no code leaking into the help
