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
./start.sh
```

That is the whole thing. From a fresh clone it creates `.env`, builds a virtualenv, installs the
dependencies, creates and upgrades the database, runs a preflight check and serves on
http://localhost:8000. It is safe to re-run: anything that already exists is left alone.

```bash
./start.sh --check      # preflight only, do not start the server
./start.sh --offline    # skip the live market-data probe (faster, no network)
./start.sh --seed       # load demo freelance opportunities first
./start.sh --port 9000  # serve somewhere else
./start.sh --docker     # build and run with docker compose instead
./start.sh --test       # run the test suite and exit
```

It runs in **offline MOCK mode** until you edit `.env`. Two keys decide how much is real:

| key | blank / mock | set |
|---|---|---|
| `ANTHROPIC_API_KEY` | deterministic keyword agents, no network, free | real Claude analysis |
| `MARKET_MOCK` | `true` gives deterministic fake prices | `false` fetches real quotes via yfinance |

**Run the preflight before trusting anything.** `./start.sh --check` (or `python -m scripts.doctor`) reports
your Python and dependencies, the database and whether its schema is current, whether Claude is live or
mocked, **whether a real stock quote can actually be fetched**, and whether the watchlist has anything in it.
Every problem comes with the command that fixes it. Secrets are never printed.

```
[  ok  ] database      sqlite:///./data/opportunity_engine.db
[  ok  ] schema        current
[ warn ] claude        MOCK mode (no ANTHROPIC_API_KEY)
[ FAIL ] market data   yfinance could not fetch SPY: MarketDataError: No price available
                       -> check outbound network access, or set MARKET_MOCK=true to work offline
[ warn ] watchlist     empty, so scans have nothing to research
```

### First five minutes

1. `./start.sh` and open http://localhost:8000
2. Click **Market Challenge**, then **Watchlist & settings**, and add a few tickers. The watchlist ships
   empty on purpose: the system never seeds investment recommendations.
3. Back on `/market`, press **RUN MARKET SCAN**. Refresh after a moment for a proposal.
4. Read the trade card, then **APPROVE**, **REJECT** or **EDIT**. Approving places no order.
5. If you approve, trade it yourself at your broker, **enter the stop there too**, then press
   **RECORD FILL** so the ledger matches reality.

Before risking money, replay the rules over history and compare them against simply holding:

```bash
python -m scripts.backtest --tickers SPY,QQQ,NVDA --days 500
```

Demo freelance data: `python -m scripts.seed`, removed later with `python -m scripts.seed --purge`
(or delete `data/opportunity_engine.db`).

Tests:

```bash
python -m pytest -q
```

### Doing it by hand

```bash
cp .env.example .env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.doctor            # preflight
uvicorn app.main:app --reload       # http://localhost:8000
```

## Running it in Docker

```bash
./start.sh --docker
```

That builds the image, starts it detached, waits for the health endpoint and prints where to go. It builds
for your own user id so the database written to `./data` belongs to you rather than to root. Or do it by
hand:

```bash
cp .env.example .env        # optional; without it the app runs in offline MOCK mode
docker compose up --build -d
```

Everyday commands:

```bash
docker compose exec opportunity-engine python -m scripts.doctor          # preflight inside the container
docker compose exec opportunity-engine python -m scripts.backtest --tickers SPY,QQQ --days 500
docker compose exec opportunity-engine python -m pytest -q               # prove the container is sane
docker compose logs -f                                                   # follow the scheduler
docker compose down                                                      # stop; your ledger is kept
docker compose up -d --build                                             # after pulling new code
```

**Your data lives on the host, not in the container.** `./data/opportunity_engine.db` holds the cash,
positions and recorded fills, bind-mounted in. It survives `docker compose down`, rebuilds and image
deletion. Back it up by copying that one file. `./app/prompts` is mounted too, so you can edit an agent
prompt and the next run picks it up without a rebuild.

The container restarts unless you stop it, so on a machine you leave running the scheduler keeps scanning.
Logs are capped at three 10MB files so months of uptime cannot fill the disk.

### When Docker misbehaves

| symptom | cause and fix |
|---|---|
| `permission denied` on the database | `./data` belongs to another user. `sudo chown -R $(id -u):$(id -g) data workspace` then rebuild. |
| `bad interpreter: /usr/bin/env bash^M` | Cloned on Windows with `core.autocrlf=true`. `git config core.autocrlf input` and re-clone; `.gitattributes` prevents it going forward. |
| port 8000 already taken | `PORT=9000 docker compose up -d`, or `./start.sh --docker --port 9000` |
| container is up but `/market` is empty | The watchlist ships empty. Add tickers at `/market/settings`. |
| market data row FAILs in the doctor | The container cannot reach the internet, or yfinance is blocked. Until it is fixed, set `MARKET_MOCK=true` in `.env` to work offline. |
| changes to code do nothing | Only `./data`, `./workspace` and `./app/prompts` are mounted. Everything else needs `docker compose up -d --build`. |

Timestamps are UTC everywhere, in the database and in the logs, so a fill recorded at 9am Eastern reads as
13:00 or 14:00 depending on the season. That is deliberate: one clock, no daylight-saving ambiguity.

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

* **Sources** (`app/sources/`): `ManualSource`, `GenericRSSSource`, `GenericJSONSource`, a real `SamGovSource`
  against the official public API, plus documented stubs for Upwork, Pennsylvania procurement and private RFP
  feeds. No scraping: stubs explain what official access is required.
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

## Contract-AI RFP intake

Procurement feeds are a firehose, so RFP sources are scored against a contract-analytics taxonomy and the
off-domain notices are dropped **before** they reach the database and before a single token is spent.

* **What counts as relevant** (`app/sources/rfp.py`): contract abstraction, clause and obligation extraction,
  lease abstraction, due-diligence document review, e-discovery, repapering and remediation score highest;
  document-AI plumbing (NLP, OCR, classification, extraction, redaction) scores next; legal-department context
  scores lowest. Bare "contract" is deliberately not a term, because every procurement notice contains it.
  Exclusion terms (janitorial, staffing, construction, food service) zero the score outright.
* **Tuning**: `RFP_MIN_RELEVANCE` sets the bar, `RFP_EXTRA_TERMS` adds your own vocabulary as core terms, and
  `RFP_FILTER_ENABLED=false` turns it off. Every kept opportunity stores its score and matched terms, and each
  source run logs how many notices were dropped, so the filter is auditable rather than a black box.
* **SAM.gov** (`app/sources/sam_gov.py`) is a working adapter against
  [the official public API](https://open.gsa.gov/api/get-opportunities-public-api/). Set `SAM_GOV_API_KEY` to a
  free api.data.gov key; without one the source is skipped and the sources page explains what is needed. One
  query is issued per configured NAICS code, and results pass through the relevance filter because a NAICS
  search alone returns far too much unrelated work. Submitting a bid still requires a SAM.gov entity
  registration (UEI), which surfaces as a `ComplianceRequirement` and blocks approval until you verify it.
* **Paid aggregators** (RFPMart, BidNet, FindRFP and similar) plug in through `RFP_RSS_FEED_URLS` or
  `RFP_JSON_FEED_URLS` using your subscriber feed, under the aggregator's terms. These are kept separate from
  `RSS_FEED_URLS`/`JSON_FEED_URLS` so the filter only applies to RFP intake.

One thing to tune before you trust the results: the default thresholds (`minimum_opportunity_score` 75,
`maximum_human_hours` 10, `minimum_expected_profit` 400) were set for small freelance gigs. A six-figure RFP
will routinely exceed the hours ceiling and get auto-rejected at the threshold stage. Raise
`maximum_human_hours` and revisit `minimum_opportunity_score` in `/settings` when you start ingesting RFPs.

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
* **Every entry carries an exit plan, and size is bounded by risk.** A stop is derived from how much the
  security actually moves day to day (`app/market/indicators.py`), then the position is sized so that being
  stopped out costs at most `MARKET_MAX_RISK_PER_TRADE_PCT` of portfolio value (`app/market/risk.py`). A
  volatile name therefore gets a wider stop *and* a smaller position. The model proposes the idea; the
  arithmetic that decides how much money is exposed is done in Python and is fully tested.
* **Open positions are watched.** When a stop or target is breached, the scan raises a SELL proposal with no
  model call at all, because an exit is a level decided when the position was opened. Like every other
  proposal it needs your approval and your manual execution. The dashboard shows a portfolio-level
  "at risk to stops" figure and flags any position with no stop.
* Approvals reuse the same immutable `approvals` table (`object_type = "market_trade"`). Approving records the
  decision and shows *"Approved — execute this trade manually with your broker, then record the fill."*
* A scheduled scan (`MARKET_SCAN_ENABLED`, `MARKET_SCAN_INTERVAL_MINUTES`) and the **RUN MARKET SCAN** button
  refresh quotes, run the two agents and create at most one proposal per ticker/side. Market-data failures are
  logged, never fatal. `MARKET_MOCK=true` uses deterministic fake prices so everything works offline.
* Prompts: `app/prompts/market_research.md`, `app/prompts/portfolio.md`, `app/prompts/_market_guardrails.md`.
  The agents are told the target is an optimisation objective, never a guarantee, and to never fabricate prices,
  news, earnings or ratings. Provider text is wrapped as untrusted data.

* **Did any of it work?** `/market/performance` scores the record from the ledger rather than from the agents'
  own confidence: win rate, profit factor, expectancy, and average R, where 1R is what was at stake when the
  position was opened. Average R matters more than win rate, because losing six times in ten still makes money
  if the winners are worth twice the losers. A **calibration table** buckets closed trades by the confidence
  stated on the entry proposal, which answers the only question that makes confidence worth reading: are the
  high-confidence calls actually better? A **counterfactual scorecard** prices every idea the agents had,
  including the ones you turned down, so idea quality and decision quality can be told apart. Below five
  closed trades the page says outright that the numbers are noise. Once there are enough, a compact, neutral
  summary is shown to the decision agent, which is told never to read a good run as licence to size up or a
  bad one as a reason to chase.

* **Does any of it have an edge?** `python -m scripts.backtest` replays the deterministic rules over
  historical prices, through the same functions the live scan uses, and always reports a buy-and-hold
  benchmark beside the result. It tests the stop, the sizing and the exit discipline, not the agents'
  stock picking, because replaying a model over history is slow, costly and contaminated by hindsight.
  Every result states how backtests lie: close-only exits understate stop losses, survivorship flatters the
  sample, and tuning the settings until the numbers look good is a curve fit rather than an edge.

  The headline finding from the built-in regimes is worth knowing before you trade. In a steady bull market
  the stops cut winners short and the rules badly underperform simply holding. In a decline they preserve a
  meaningful fraction of the account that holding would have lost, and a trend filter that declines to trade
  at all preserves more still. Stop discipline is insurance: it costs money in the good case and saves it in
  the bad one.

A stop recorded here only tells the dashboard when to propose an exit. **It does not protect the position.**
Place the stop with your broker when you place the trade, and remember a gap can skip straight through it.

This is a personal, experimental portfolio challenge, not investment advice. The target is an optimisation
objective, never a forecast: going from $200 to $1,000 by January 2027 implies a compounding return far beyond
what any systematic approach reliably delivers, and the software is built to say so rather than chase it.

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
  market/            Market Challenge: data.py (providers), universe.py, indicators.py (price statistics),
                     risk.py (exit levels + position sizing), performance.py (round trips + calibration),
                     backtest.py (historical replay + benchmark), portfolio.py (ledger), approvals.py, scan.py
  approvals.py       approval system   work_orders.py  execution lifecycle
  audit.py, costs.py, metrics.py, notifications/, scheduler.py, settings_service.py
  web/               routes, JSON API, market_routes/market_api, templates, static
start.sh             one command: set up, preflight, run
scripts/doctor.py    preflight check, including whether real market quotes actually arrive
scripts/seed.py      realistic fake opportunities    scripts/run_pipeline.py  one scan cycle
scripts/backtest.py  replay the market risk rules over history against a buy-and-hold benchmark
tests/               pytest suite (mock mode)
```

See `ARCHITECTURE.md` for design details and `TODO.md` for the roadmap.
