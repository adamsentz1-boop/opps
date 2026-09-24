"""Static checks on the container setup.

A real `docker build` needs a daemon, which the test suite must not require. These checks catch the
mistakes that are cheap to make and expensive to debug on someone else's laptop: a host virtualenv leaking
into the image, a secret baked into a layer, a prompt file the agents read at runtime getting excluded, or a
shell script that a Windows clone rewrites to CRLF.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _patterns() -> list[str]:
    text = (ROOT / ".dockerignore").read_text()
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _excluded(path: str) -> bool:
    """Docker matches a pattern against the whole relative path, and `*` does not cross a slash."""
    for pattern in _patterns():
        pattern = pattern.rstrip("/")
        if fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern + "/"):
            return True
    return False


# --------------------------------------------------------------------------- .dockerignore
def test_dockerignore_exists():
    assert (ROOT / ".dockerignore").exists(), (
        "without a .dockerignore, COPY . . drags the host .venv, .git and the live database into the image")


@pytest.mark.parametrize("path", [
    ".venv/lib/python3.11/site-packages/anything.py",
    "venv/bin/python",
    "data/opportunity_engine.db",
    ".git/config",
    "app/__pycache__/main.cpython-311.pyc",
    ".env",
    "workspace/deliverable.txt",
])
def test_harmful_paths_never_reach_the_image(path):
    assert _excluded(path), f"{path} would be copied into the image"


@pytest.mark.parametrize("path", [
    # read from disk on every agent call, so excluding these silently breaks the agents
    "app/prompts/_guardrails.md",
    "app/prompts/_market_guardrails.md",
    "app/prompts/market_research.md",
    "app/prompts/portfolio.md",
    # rendered at runtime
    "app/web/templates/market.html",
    "app/web/static/style.css",
    # needed to run or diagnose the container
    "app/main.py",
    "requirements.txt",
    "scripts/doctor.py",
    "scripts/backtest.py",
    ".env.example",
])
def test_runtime_files_are_kept(path):
    assert (ROOT / path).exists(), f"{path} is missing from the repo"
    assert not _excluded(path), f"{path} is needed at runtime but .dockerignore excludes it"


def test_no_blanket_markdown_rule():
    """A `*.md` rule reads as harmless and quietly removes the agent prompts on some Docker versions."""
    assert "*.md" not in _patterns()


# --------------------------------------------------------------------------- Dockerfile
def test_dockerfile_installs_dependencies_before_copying_code():
    lines = [l.strip() for l in (ROOT / "Dockerfile").read_text().splitlines() if l.strip()]
    pip = next(i for i, l in enumerate(lines) if l.startswith("RUN pip install"))
    copy_all = next(i for i, l in enumerate(lines) if l.startswith("COPY . ."))
    assert pip < copy_all, "editing code would reinstall every dependency on each build"


def test_dockerfile_does_not_run_as_root():
    body = (ROOT / "Dockerfile").read_text()
    assert "USER " in body, "running as root leaves root-owned files in the bind-mounted ./data"
    assert "APP_UID" in body and "APP_GID" in body, "the uid must be overridable to match the host user"


def test_dockerfile_creates_the_runtime_directories():
    body = (ROOT / "Dockerfile").read_text()
    assert "/app/data" in body and "/app/workspace" in body


def test_dockerfile_never_bakes_in_a_secret():
    body = (ROOT / "Dockerfile").read_text().lower()
    for marker in ("anthropic_api_key=", "sam_gov_api_key=", "copy .env "):
        assert marker not in body, f"Dockerfile appears to bake in {marker}"


# --------------------------------------------------------------------------- compose
def test_compose_persists_the_ledger_and_survives_restarts():
    body = (ROOT / "docker-compose.yml").read_text()
    assert "./data:/app/data" in body, "the cash and position ledger must outlive the container"
    assert "restart: unless-stopped" in body
    assert "start_period" in body, "the first boot creates the database; health must not be judged too early"


def test_compose_points_the_database_at_the_mounted_volume():
    body = (ROOT / "docker-compose.yml").read_text()
    assert "DATABASE_URL=sqlite:////app/data/" in body


def test_compose_does_not_require_a_dotenv_to_exist():
    """A fresh clone should be able to `docker compose up` and land in offline mock mode."""
    body = (ROOT / "docker-compose.yml").read_text()
    assert "required: false" in body


def test_compose_caps_its_logs():
    """Left running on a laptop for months, uncapped json logs quietly eat the disk."""
    body = (ROOT / "docker-compose.yml").read_text()
    assert "max-size" in body and "max-file" in body


# --------------------------------------------------------------------------- line endings
def test_gitattributes_forces_lf_on_shell_scripts():
    """A Windows clone with core.autocrlf=true otherwise yields: bad interpreter: /usr/bin/env bash^M"""
    body = (ROOT / ".gitattributes").read_text()
    assert "*.sh text eol=lf" in body
    assert "Dockerfile text eol=lf" in body


def test_shell_scripts_have_unix_line_endings():
    for script in ROOT.glob("*.sh"):
        assert b"\r\n" not in script.read_bytes(), f"{script.name} has CRLF endings and will not run in the image"
