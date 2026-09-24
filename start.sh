#!/usr/bin/env bash
#
# Opportunity Engine - one command from a fresh clone to a running app.
#
#   ./start.sh                  set up if needed, run preflight, start on http://localhost:8000
#   ./start.sh --check          preflight only, do not start the server
#   ./start.sh --offline        skip the live market-data probe during preflight
#   ./start.sh --seed           load demo freelance opportunities first
#   ./start.sh --port 9000      serve on a different port
#   ./start.sh --docker         build and run with docker compose instead
#   ./start.sh --test           run the test suite and exit
#
# Safe to re-run: the virtualenv, the .env and the database are created only when missing.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VENV=".venv"
PORT=8000
DO_SEED=false
CHECK_ONLY=false
OFFLINE=""
USE_DOCKER=false
RUN_TESTS=false
RELOAD="--reload"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$1" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)      CHECK_ONLY=true; shift ;;
    --offline)    OFFLINE="--offline"; shift ;;
    --seed)       DO_SEED=true; shift ;;
    --docker)     USE_DOCKER=true; shift ;;
    --test)       RUN_TESTS=true; shift ;;
    --no-reload)  RELOAD=""; shift ;;
    --port)       PORT="${2:?--port needs a number}"; shift 2 ;;
    -h|--help)    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            die "unknown option: $1 (try --help)" ;;
  esac
done

# --------------------------------------------------------------------------- .env
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    bold "Created .env from .env.example"
    info "It runs in offline MOCK mode until you add ANTHROPIC_API_KEY."
  else
    warn "No .env and no .env.example; built-in defaults will be used."
  fi
fi

# --------------------------------------------------------------------------- docker path
if [[ "$USE_DOCKER" == true ]]; then
  command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
  docker info >/dev/null 2>&1 || die "the docker daemon is not reachable; start Docker Desktop and retry"
  bold "Building and starting with docker compose"
  docker compose up --build -d
  info "Waiting for the health endpoint..."
  for _ in $(seq 1 60); do
    if curl -sf "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
      bold "Running at http://localhost:${PORT}"
      info "Preflight:  docker compose exec opportunity-engine python -m scripts.doctor"
      info "Logs:       docker compose logs -f"
      info "Stop:       docker compose down"
      exit 0
    fi
    sleep 1
  done
  warn "Health check did not pass in time. Inspect with: docker compose logs"
  exit 1
fi

# --------------------------------------------------------------------------- python
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PYTHON="$candidate"; break
    fi
  fi
done
[[ -n "$PYTHON" ]] || die "no Python 3.10+ found on PATH. Install Python 3.11 and retry."

# --------------------------------------------------------------------------- virtualenv
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  info "Using the already-active virtualenv at $VIRTUAL_ENV"
  PY="python"
else
  if [[ ! -d "$VENV" ]]; then
    bold "Creating a virtualenv in $VENV"
    "$PYTHON" -m venv "$VENV" || die "could not create a virtualenv (is the venv module installed?)"
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  PY="python"
fi

# --------------------------------------------------------------------------- dependencies
NEED_INSTALL=false
if [[ ! -f "$VENV/.installed" ]]; then
  NEED_INSTALL=true
elif [[ requirements.txt -nt "$VENV/.installed" ]]; then
  NEED_INSTALL=true
  info "requirements.txt changed since the last install"
fi
if [[ "$NEED_INSTALL" == true ]]; then
  bold "Installing dependencies"
  $PY -m pip install --quiet --upgrade pip
  $PY -m pip install --quiet -r requirements.txt || die "dependency install failed"
  touch "$VENV/.installed"
fi

mkdir -p data workspace

# --------------------------------------------------------------------------- tests
if [[ "$RUN_TESTS" == true ]]; then
  bold "Running the test suite"
  exec $PY -m pytest -q
fi

# --------------------------------------------------------------------------- preflight
bold "Preflight"
set +e
$PY -m scripts.doctor $OFFLINE
DOCTOR_STATUS=$?
set -e
if [[ "$CHECK_ONLY" == true ]]; then
  exit $DOCTOR_STATUS
fi
if [[ $DOCTOR_STATUS -ne 0 ]]; then
  warn "Preflight reported a problem. The app will still start, but the parts above will not work."
fi

# --------------------------------------------------------------------------- demo data
if [[ "$DO_SEED" == true ]]; then
  bold "Seeding demo opportunities"
  warn "With a real ANTHROPIC_API_KEY this spends tokens. Remove later with: python -m scripts.seed --purge"
  $PY -m scripts.seed
fi

# --------------------------------------------------------------------------- go
echo
bold "Starting on http://localhost:${PORT}"
info "Opportunity Engine  http://localhost:${PORT}/"
info "Market Challenge    http://localhost:${PORT}/market"
info "Watchlist/settings  http://localhost:${PORT}/market/settings"
info "Performance         http://localhost:${PORT}/market/performance"
info "Stop with Ctrl-C"
echo
exec $PY -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" $RELOAD
