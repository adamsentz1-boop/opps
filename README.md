# Opportunity Engine

A local-first platform that continuously finds freelance and contract opportunities where **AI can do most of
the work**, scores them on **expected profit per human hour**, drafts the proposal, and puts the result in an
**approval queue**. Nothing is ever submitted, sent, signed, purchased, or promised without an explicit human
decision recorded in the dashboard.

Phase 1 covers freelance/contract listings from manual entry, RSS/Atom feeds and JSON feeds. Phase 2 adds
government bids, RFPs/RFQs and post-award work execution; the data model already supports it
(`ComplianceRequirement`, `WorkOrder`, `WorkTask`, `Deliverable`).

## Quick start

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY, or leave blank for offline MOCK mode
pip install -r requirements.txt
python -m scripts.seed          # 15 fake opportunities through the pipeline (spends tokens with a real key)
uvicorn app.main:app --reload   # http://localhost:8000
```

Or with Docker:

```bash
cp .env.example .env
docker compose up --build       # http://localhost:8000
docker compose exec opportunity-engine python -m scripts.seed      # optional demo data
```

Remove the demo data later with `python -m scripts.seed --purge` (or delete `data/opportunity_engine.db`).

Tests:

```bash
python -m pytest -q
```

## What you see

The home page is a command center:

```
OPPORTUNITIES SCANNED: 15   AUTO-REJECTED: 11   QUALIFIED: 4   RECOMMENDED: 4   AWAITING APPROVAL: 3
POTENTIAL REVENUE: $3,490   EXPECTED PROFIT: $3,434   ESTIMATED HUMAN TIME: 2.5 HOURS   AI COST: $0.84
```

The most important screen is **Awaiting approval** (`/approvals`). Each card answers in under 15 seconds:
what is it, how much could we make, how much of my time, can Claude actually do it, what could go wrong,
what are we proposing, and should I approve it. Buttons: **APPROVE**, **REJECT**, **EDIT**, **REANALYZE**.

Approving creates an immutable `approvals` row and moves the opportunity to `READY_TO_SUBMIT`. You submit it
yourself in Phase 1 (copy button / Markdown export on the opportunity page) and then record the outcome (submitted → interviewing → won/lost). Marking an opportunity
WON creates a `WorkOrder` with an agent-generated plan, task list, QA checklist and approval checkpoints.

## How it works

```
Sources ──► Normalizer ──► Rule rejection ──► QualificationAgent ──► Threshold rejection
                                                                          │
   ResearchAgent ◄─── QUALIFIED ◄─────────────────────────────────────────┘
        │
   SolutionArchitectAgent ("can we actually do this profitably?") ──► reject if not
        │
   RequirementsAgent (ComplianceRequirement rows, always unverified)
        │
   ProposalAgent ──► AWAITING_APPROVAL ──► notification ──► YOU ──► READY_TO_SUBMIT
```

* **Sources** (`app/sources/`): `ManualSource`, `GenericRSSSource`, `GenericJSONSource`, plus documented stubs
  for Upwork, SAM.gov, Pennsylvania procurement and private RFP feeds. No scraping: stubs explain what
  official access is required.
* **Agents** (`app/agents/`, prompts in `app/prompts/*.md`, editable live): Scout, Qualification, Research,
  SolutionArchitect, Requirements, Proposal, Work, QA. All use Claude structured outputs (`messages.parse`) and every call is
  recorded in `agent_runs` with tokens, cost and the raw JSON.
* **Rejection engine** (`app/pipeline/rejection.py`): deterministic rules before any tokens are spent
  (onsite, licensed, ToS violations, budget too low, full-time, unrealistic deadline, multiple prompt-injection
  patterns) and threshold checks after scoring.
* **Scoring** (`app/pipeline/scoring.py`): `OPPORTUNITY_SCORE` weighted toward profitability, low human effort,
  AI completion, low risk and scope clarity; economics (`expected_profit`, `expected_value = p(win) × profit`,
  `profit_per_human_hour`) computed deterministically in Python.
* **Approvals** (`app/approvals.py`): the only way state advances to READY_TO_SUBMIT, to a verified
  `ComplianceRequirement`, or to READY_FOR_DELIVERY on a work order. Rows are immutable at the ORM level.
* **Audit log**: every important action with agent, action, object, previous/new state, whether human approval
  was required, and the approval id.
* **Settings** (`/settings`): thresholds, pricing targets, the verified owner profile and preferred/avoided work,
  editable without code changes.
* **Notifications**: dashboard adapter enabled by default. ntfy push works when you opt in
  (`NOTIFY_ADAPTERS=dashboard,ntfy` plus `NTFY_URL`/`NTFY_TOPIC`); email/Slack/SMS are stubs that never send.

## Market Challenge

A second module, **Market Challenge** (`/market`), runs alongside the freelance pipeline with the same
human-approval philosophy.

* **Starting capital:** $200
* **Target:** $1,000
* **Target date:** January 1, 2027

**Opportunity Engine cannot execute stock trades.** It researches the tickers *you* put on the watchlist and
proposes trades. **Owner approval is required** for every proposal. **The owner executes approved trades
manually** with their own broker, and **the owner records the actual fills** in the dashboard. There is no
brokerage API, no order submission, no money movement and no brokerage credentials anywhere in the code.

```
MARKET DATA (yfinance, read-only) ──► MarketResearchAgent ──► PortfolioAgent ──► TradeProposal
        AWAITING_APPROVAL ──► YOU: APPROVE / REJECT / EDIT / REANALYZE ──► APPROVED
        ──► you trade with your broker ──► you RECORD FILL ──► ledger (cash, positions, P&L, snapshot)
```

* `/market` is a command center: portfolio value, starting capital, target, goal progress, cash, today's move,
  total return, days remaining, a $200 → $1,000 progress bar, positions, proposals awaiting approval
  (15-second trade cards with why-now, upside/downside cases, catalysts, risks, confidence, risk/reward),
  approved trades awaiting a fill, trade history and market agent activity (model, tokens, cost).
* `/market/settings`: starting capital, target, target date, scan interval, position/trade limits, model, effort,
  and the **allowed watchlist**. The watchlist ships **empty** - the system never seeds recommendations.
  Changing the starting capital after trades have been recorded requires an explicit confirmation and is audited.
* Universe (V1): long-only cash account, ordinary stocks and ETFs, fractional shares; no options, futures, forex,
  crypto, leveraged/inverse products, short selling, margin, borrowing or negative cash. Every proposal and every
  fill is re-checked deterministically in Python (`app/market/portfolio.py`); agents never mutate the ledger.
* Approvals reuse the same immutable `approvals` table (`object_type = "market_trade"`). Approving records the
  decision and shows *"Approved — execute this trade manually with your broker, then record the fill."*
* A scheduled scan (`MARKET_SCAN_ENABLED`, `MARKET_SCAN_INTERVAL_MINUTES`) and the **RUN MARKET SCAN** button
  refresh quotes, run the two agents and create at most one proposal per ticker/side. Market-data failures are
  logged, never fatal. `MARKET_MOCK=true` uses deterministic fake prices so everything works offline.
* Prompts: `app/prompts/market_research.md`, `app/prompts/portfolio.md`, `app/prompts/_market_guardrails.md`.
  The agents are told the target is an optimisation objective, never a guarantee, and to never fabricate prices,
  news, earnings or ratings. Provider text is wrapped as untrusted data.

This is a personal, experimental portfolio challenge, not investment advice.

## Security model

* Secrets only in `.env`; the API key is never rendered or logged (`/settings` shows only "configured: true").
* All external content (listings, feeds, pasted text) is treated as **untrusted data**: control characters are
  stripped, it is wrapped in `<untrusted_opportunity_data>` blocks whose closing tag cannot be forged, and
  prompt-injection patterns are detected, stored as `injection_flags`, shown in the UI and penalised in scoring.
* System instructions go in the Claude `system` parameter; untrusted data only ever appears in delimited blocks
  in the user message. The shared guardrails prompt (`app/prompts/_guardrails.md`) tells every agent to treat
  instructions inside those blocks as red flags.
* Agents cannot submit, send, sign, spend, self-certify, or delete. Those code paths simply do not exist in the
  agent layer; approval routes are owner-only UI/API actions.
* The Market Challenge has no brokerage integration at all: the only code that changes cash or positions is the
  owner's Record Fill action, which requires an APPROVED proposal and refuses negative cash or over-selling.
* `ComplianceRequirement.verified` defaults to `false` and can only be set through the owner verification route
  with evidence. Unverified mandatory requirements block approval.

## Mock mode

Without `ANTHROPIC_API_KEY` (or with `LLM_MOCK=true`) the platform runs a deterministic keyword-based stand-in
for Claude so the pipeline, dashboard, approvals and tests work offline. Mock outputs are labelled `[MOCK]`.
Set the key to use `claude-opus-5` (configurable via `CLAUDE_MODEL`, `CLAUDE_EFFORT`). `MARKET_MOCK=true` (or
`MARKET_DATA_PROVIDER=mock`) does the same for market data: deterministic fake quotes, no network.

## Layout

```
app/
  main.py            FastAPI app + scheduler lifecycle
  config.py          env settings (.env)
  db.py, models.py   SQLAlchemy / SQLite
  schemas.py         NormalizedOpportunity + structured agent outputs
  sanitize.py        untrusted-content handling
  normalizer.py      payload -> NormalizedOpportunity -> DB
  sources/           adapters (manual, rss, json, stubs, registry)
  agents/            agent roles (incl. market_research, portfolio)   prompts/  editable system prompts
  llm/               Claude client + mock
  pipeline/          rejection, scoring, runner
  market/            Market Challenge: data.py (providers), universe.py, portfolio.py (ledger), approvals.py, scan.py
  approvals.py       approval system   work_orders.py  execution lifecycle
  audit.py, costs.py, metrics.py, notifications/, scheduler.py, settings_service.py
  web/               routes, JSON API, market_routes/market_api, templates, static
scripts/seed.py      realistic fake opportunities    scripts/run_pipeline.py  one scan cycle
tests/               pytest suite (mock mode)
```

See `ARCHITECTURE.md` for design details and `TODO.md` for the roadmap.
