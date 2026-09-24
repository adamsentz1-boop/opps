# TODO / Roadmap

## Done (Phase 1)
- [x] Project structure, README, ARCHITECTURE, docker-compose, `.env.example`
- [x] SQLite schema: opportunities, opportunity_analysis, solution_plans, buyers, proposals, approvals,
      source_runs, agent_runs, work_orders, work_tasks, deliverables, compliance_requirements, audit_log,
      settings, notifications
- [x] Normalised opportunity schema + budget/date parsing + dedupe
- [x] Sources: ManualSource, GenericRSSSource, GenericJSONSource; documented stubs for Upwork, SAM.gov,
      PA procurement, private RFP feeds (no scraping)
- [x] Rule-based rejection engine (pre-LLM) + threshold rejection (post-LLM) with stored reasons
- [x] Qualification / Research / SolutionArchitect / Proposal / Work / QA / Scout agents with separate prompt
      files and structured outputs; mock mode for offline use
- [x] Deterministic scoring + economics (expected profit, EV, profit per human hour)
- [x] Approval queue with APPROVE / REJECT / EDIT / REANALYZE, immutable approval records
- [x] Outcome tracking (submitted / interviewing / won / lost) and WorkOrder lifecycle with delivery approval gate
- [x] ComplianceRequirement with owner-only verification and approval blocking
- [x] Dashboard: today, pipeline, financial, top opportunities, notifications, audit log, agent runs & costs,
      settings (editable thresholds + owner profile)
- [x] Untrusted-content sanitisation, injection detection and penalties; secrets never exposed
- [x] Background scheduler; JSON API; seed data; pytest suite
- [x] RequirementsAgent: extracts certifications/insurance/forms/deadlines into `ComplianceRequirement` rows,
      always unverified; mandatory unverified rows block approval
- [x] Proposal copy button + Markdown export for manual submission
- [x] ntfy push transport (opt-in via NOTIFY_ADAPTERS + NTFY_URL/NTFY_TOPIC); manual scans run in the background

## Done (Market Challenge module)
- [x] `TradingChallenge`, `MarketPosition`, `TradeProposal`, `TradeExecution`, `MarketSnapshot`, `PortfolioSnapshot`,
      `MarketWatchlist` tables; default $200 → $1,000 by 2027-01-01 challenge; empty watchlist seed
- [x] `MarketDataProvider` abstraction with read-only yfinance + deterministic mock (`MARKET_MOCK`); failures audited, never fatal
- [x] MarketResearchAgent + PortfolioAgent (BaseAgent/run_structured, AgentRun, prompts in `app/prompts/`)
- [x] Deterministic universe/limit checks (long-only, stocks/ETFs, fractional, no leverage/margin/negative cash)
- [x] Trade approvals through the existing immutable `approvals` table (`object_type=market_trade`); approval never executes
- [x] Record Fill (the only ledger mutation): TradeExecution, cash, weighted-average cost, realised/unrealised P&L,
      PortfolioSnapshot, audit entries
- [x] `/market` command center, `/market/settings` (+watchlist), `/api/market/*`, scheduled + manual market scans,
      duplicate-proposal prevention, expiry, edit/reanalyze/cancel
- [x] Offline test coverage for balances, limits, approvals, fills, P&L, goal progress, HOLD, mock scan

## Done (contract-AI RFP intake)
- [x] Contract-AI relevance taxonomy + deterministic scorer (`app/sources/rfp.py`), auditable via stored
      score/matched terms and the source run log
- [x] `ContractAIRelevanceFilter` source wrapper: transparent to de-duplication, drops off-domain notices
      before they reach the database
- [x] `SamGovSource` promoted from stub to a working adapter against the official public API (one query per
      NAICS code, optional description fetch, API key scrubbed from every error)
- [x] Separate `RFP_RSS_FEED_URLS` / `RFP_JSON_FEED_URLS` so the filter applies only to RFP intake
- [x] Negation-aware licensed-professional rule, so "the contractor shall not provide legal advice" no longer
      auto-rejects contract-analytics RFPs
- [x] 27 offline tests covering scoring, filtering, SAM.gov mapping, key redaction and registry wiring

## Next (contract-AI RFP intake)
- [ ] Verify the SAM.gov adapter against the live API (parameter names, pagination beyond the first page,
      rate limits) - it is written to the documented shape but has not made a real call
- [ ] Pagination past `SAM_GOV_LIMIT` notices per NAICS code
- [ ] Field-map support for RFP JSON feeds whose keys do not match the normaliser's fallbacks
- [ ] A separate threshold profile for RFPs (the freelance thresholds reject six-figure solicitations)
- [ ] Attachment handling: most solicitations put the real scope in linked documents, not the notice body
- [ ] Tune the taxonomy against real aggregator output and measure precision/recall on a labelled sample

## Done (Market Challenge risk management)
- [x] `app/market/indicators.py`: trend, average daily move, volatility, drawdown, range position and volume
      trend measured in Python and handed to the research agent as facts instead of a raw price array
- [x] `app/market/risk.py`: volatility-derived stop and target, risk-per-share, and position sizing bounded by
      a per-trade risk budget rather than by available cash
- [x] Exit monitor: a breached stop or target raises a SELL proposal deterministically, with no model call
- [x] Exit plan carried from proposal to position on fill, and cleared when the position goes flat
- [x] Dashboard, proposal detail, fill form and JSON API surface stop, target, dollars at risk and a
      portfolio-level "at risk to stops" figure; positions with no stop are flagged
- [x] `db.ensure_columns()`: additive SQLite column upgrades so existing databases survive new columns
- [x] 33 offline tests covering statistics, levels, sizing, exits, the fill hand-off and the schema upgrade

## Done (Market Challenge performance feedback)
- [x] `app/market/performance.py`: round trips reconstructed first-in-first-out from recorded fills, carrying
      holding period, R multiple and the confidence stated on the entry proposal; P&L reconciles with the ledger
- [x] Win rate, profit factor, expectancy, average R and exit-reason breakdown, with small samples flagged
      rather than presented as findings
- [x] Confidence calibration table: is a 90+ call actually better than a 55 call?
- [x] Counterfactual scorecard prices every idea including rejected and expired ones, separating idea quality
      from decision quality
- [x] `/market/performance` page, `/api/market/performance`, nav entry
- [x] Track record fed back to the PortfolioAgent once the sample is meaningful, with explicit prompt rules
      against sizing up on a good run or chasing a bad one
- [x] 18 offline tests (126 total) including ledger reconciliation and first-in-first-out lot matching

## Done (Market Challenge backtester)
- [x] `app/market/backtest.py`: replays the deterministic rules over historical bars through the *same*
      `indicators.py` and `risk.py` functions the live scan uses, with a shared cash pool, position limits,
      configurable fees and slippage, and forced close-out at the last bar
- [x] Buy-and-hold benchmark on every result, and taking zero trades is reported as abstention rather than
      as beating the benchmark
- [x] Four mechanical entry rules (always / trend / dip / breakout) standing in for the entry signal, since
      replaying an LLM over history is slow, costly and contaminated by hindsight
- [x] Look-ahead bias closed and proved by test: decisions see only bars up to and including the decision bar
- [x] Every result carries its own caveats (close-only exits, survivorship, costs, curve fitting)
- [x] `scripts/backtest.py` CLI with a readable comparison table and optional JSON export
- [x] 26 offline tests (152 total) including the look-ahead guarantee and the never-spend-what-you-lack check

## Done (getting it running)
- [x] `start.sh`: fresh clone to a served app in one command. Creates `.env`, builds a virtualenv, installs
      dependencies, migrates the database, runs preflight, then serves. Idempotent, with `--check`,
      `--offline`, `--seed`, `--port`, `--docker` and `--test`.
- [x] `scripts/doctor.py`: preflight that answers whether a real stock quote can actually be fetched, plus
      Python, dependencies, `.env`, database and schema, Claude live or mocked, watchlist and scheduler.
      Every problem carries the command that fixes it, and secrets are never printed.
- [x] Verified end to end from a genuinely fresh clone on Python 3.13
- [x] 13 offline tests (165 total) including a leak test that asserts no API key reaches the output

## Next (Market Challenge)
- [ ] Trailing stops: raise the stop as a position moves in your favour, instead of a fixed level. The
      backtest shows fixed targets cut winners short in a trending market, so this is the obvious next test.
- [ ] Drawdown tracking and a circuit breaker: pause proposals after N consecutive losses or an X% drawdown
- [ ] Walk-forward validation: tune on one period, verify on the next, to catch curve fitting honestly
- [ ] Run the backtest against real yfinance history once outbound access is confirmed
- [ ] Correlation check: several positions in one sector is one position wearing a disguise
- [ ] Price history / sparkline on the dashboard from `market_snapshots`
- [ ] Optional news/fundamentals inputs for MarketResearchAgent (owner-supplied only; still never fetched by agents)
- [ ] Trailing-stop / take-profit *reminders* (proposals only, still owner-executed)
- [ ] Per-ticker research cache to avoid re-running research on unchanged prices
- [ ] Export ledger to CSV for tax records

## Next
- [ ] Run the seed set against the real Claude API and tune prompts/thresholds with actual outputs
- [ ] Prompt-cache the guardrails/system prompt across agents (already marked `cache_control`; verify hit rate)
- [ ] Owner "capability inventory" table (tools, accounts, verified skills) to feed proposals and requirements
- [ ] ScoutAgent triage switch per source for noisy feeds
- [ ] Web-fetch based buyer research (Claude web fetch tool) with domain allow-list and owner opt-in
- [ ] email / Slack / SMS transports (ntfy is done, opt-in)
- [ ] Batch API for bulk qualification when a feed delivers many items at once
- [ ] Per-source rate limits and fetch state (last seen id / etag)
- [ ] Alembic migrations once the schema stabilises

## Phase 2
- [ ] SAM.gov adapter via official API (api.data.gov key, UEI); notice → ComplianceRequirement extraction agent
- [ ] State/municipal/university/school district procurement adapters where official feeds exist
- [ ] Tune RequirementsAgent on real RFP/solicitation text (basic version already runs on every qualified listing)
- [ ] Bid package assembly (forms, attachments) with owner sign-off per document
- [ ] Source-specific submission integrations triggered only from READY_TO_SUBMIT by the owner
- [ ] WorkAgent execution: agent-produced deliverables in `workspace/`, QAAgent reviews, owner approves delivery
- [ ] Invoice generation (draft only) and payment tracking; realised effective hourly rate
- [ ] Multi-user auth if the dashboard is ever exposed beyond localhost
