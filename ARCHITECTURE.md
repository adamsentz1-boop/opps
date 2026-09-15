# Architecture

## Principles

1. **Human approval is a first-class component.** State transitions that have external consequences
   (`READY_TO_SUBMIT`, requirement verification, delivery approval) only happen through `app/approvals.py`,
   which writes an immutable `Approval` row and an audit event. Agents have no code path to those functions.
2. **Maximise expected profit per human hour.** The pipeline rejects aggressively; the dashboard ranks by
   `profit_per_human_hour`, not by count.
3. **Modular sources.** Every source implements `OpportunitySource` and produces `NormalizedOpportunity`.
   Adding a source never touches the pipeline.
4. **Untrusted content stays untrusted.** External text is sanitised, delimited and flagged; system
   instructions are separate from data on every Claude call.
5. **Auditable.** Every agent call (`agent_runs`) and every important action (`audit_log`) is stored.

## Components

### Source adapters (`app/sources/`)
`OpportunitySource` interface: `source_name`, `display_name`, `enabled`, `fetch_new_opportunities()`,
`fetch_opportunity_details(external_id)`, `source_url(external_id)`, `requirements`.

| Adapter | Status | Notes |
|---|---|---|
| `ManualSource` | enabled | dashboard form, JSON API, `data/manual_inbox.json`, seed script |
| `GenericRSSSource` | enabled via `RSS_FEED_URLS` | RSS 2.0 + Atom, stdlib parser |
| `GenericJSONSource` | enabled via `JSON_FEED_URLS` | URL or file, optional field map |
| `UpworkSource` | stub | needs approved official API/OAuth app; no scraping |
| `SamGovSource` | stub | official public API + api.data.gov key + UEI |
| `PennsylvaniaProcurementSource` | stub | no API; permitted feed/export or manual only |
| `PrivateRFPFeedSource` | stub | subscriber RSS/JSON under aggregator terms |

`registry.get_sources()` builds the active list from `.env`.

### Normalizer (`app/normalizer.py`)
Maps any payload to `NormalizedOpportunity` (title, description, buyer, budget min/max/type, dates, url,
skills, deliverables, raw text). Parses budgets from free text (`$1,500-2,000`, `$45/hr`). De-duplicates on
`(source, external_id)`. Runs injection detection on insert and stores `injection_flags`.

### Pipeline (`app/pipeline/runner.py`)
`process_opportunity(db, id)`:
1. `pre_rules` (no tokens): physical/onsite, licensed, ToS/deceptive, full-time, low budget, ≥3 injection
   patterns, unrealistic deadline → `REJECTED` (stage `rules`).
2. `QualificationAgent` → `OpportunityAnalysis`; `scoring.apply_to_analysis` computes economics and
   `OPPORTUNITY_SCORE`; `post_rules` checks thresholds → `REJECTED` (stage `thresholds`) or `QUALIFIED`.
3. `ResearchAgent` → `Buyer` (non-fatal on failure).
4. `SolutionArchitectAgent` → `SolutionPlan`; rejects if not feasible/profitable or owner hours too high
   (stage `solution`).
5. `RequirementsAgent` → `ComplianceRequirement` rows (`verified=false`, owner-verified rows preserved on
   reanalysis; non-fatal on failure).
6. `ProposalAgent` → `Proposal` v1.
7. `AWAITING_APPROVAL`; notification if score ≥ `notify_min_score`.

Errors set `ERROR` with `last_error`; `reanalyze_opportunity` re-runs and increments proposal versions.

`run_scan()` iterates sources, records `source_runs`, then processes all `NEW` rows. The APScheduler job
calls it every `SCAN_INTERVAL_MINUTES`; it is guarded by a lock so runs never overlap.

### Scoring (`app/pipeline/scoring.py`)
```
profitability' = 0.5·agent_profitability + 0.5·min(100, 100·profit_per_human_hour / (2·target_rate))
score = 0.24·profitability' + 0.20·(100−human_effort) + 0.18·ai_completable + 0.14·(100−risk)
      + 0.10·scope_clarity + 0.08·technical_fit + 0.03·buyer_quality + 0.03·(100−competition)
score = 50 + (score−50)·(0.6 + 0.4·confidence/100)   # low confidence pulls toward the middle
−15 if injection detected; capped at 40 if the agent recommends rejection
expected_profit = revenue − api_cost − other_cost
expected_value  = p(win) × expected_profit
```

### Agents (`app/agents/`, `app/prompts/`)
Each role has a system prompt file (`_guardrails.md` + `<role>.md`) and a Pydantic output schema
(`app/schemas.py`). `BaseAgent.opportunity_block()` renders trusted metadata plus untrusted text blocks.
`LLMClient.complete_structured()` calls `client.messages.parse(output_format=Model)` on `claude-opus-5`
(configurable), checks `stop_reason` for refusal/truncation, and records an `AgentRun` with token usage and
estimated cost. In mock mode (`app/llm/mock.py`) deterministic heuristics produce the same schemas.

### Approval system (`app/approvals.py`)
* `approve_opportunity` → `READY_TO_SUBMIT` (blocked if any mandatory `ComplianceRequirement` is unverified)
* `reject_opportunity` → `DECLINED`
* `edit_proposal` → new `Proposal` version; an edit after approval returns to `AWAITING_APPROVAL`
* `request_reanalysis` → approval record, then pipeline re-run
* `record_outcome` → `SUBMITTED` / `INTERVIEWING` / `WON` / `LOST` (owner reports what happened externally)
* `verify_requirement` → the only path to `verified=True`, requires evidence
* `approve_delivery` → work order `READY_FOR_DELIVERY`

`Approval` and `AuditLog` rows raise on update/delete (SQLAlchemy event guards).

### Work execution (`app/work_orders.py`) - Phase 2 ready
`WorkOrder` statuses: NEW → PLANNING → WAITING_FOR_INPUT/IN_PROGRESS → QA → AWAITING_OWNER_APPROVAL →
READY_FOR_DELIVERY (only via `approve_delivery`) → DELIVERED → INVOICED → PAID. `WorkAgent` produces the plan,
tasks with dependencies, required inputs, deliverables, QA checklist and approval checkpoints. `QAAgent`
reviews deliverable content against the checklist. Nothing is sent to a client by the system.

### Data model (SQLite)
`opportunities`, `opportunity_analysis`, `solution_plans`, `buyers`, `proposals`, `approvals`, `source_runs`,
`agent_runs`, `work_orders`, `work_tasks`, `deliverables`, `compliance_requirements`, `audit_log`, `settings`,
`notifications`.

### Opportunity status flow
```
NEW → QUALIFYING → REJECTED
                 → QUALIFIED → AWAITING_APPROVAL → DECLINED
                                                 → READY_TO_SUBMIT → SUBMITTED → INTERVIEWING → WON → (WorkOrder)
                                                                                             → LOST
                 → ERROR (reanalyze to retry)
```

### Cost tracking (`app/costs.py`)
Per-run cost from token usage × model price table; summaries per agent, day, month, opportunity and work
order feed the dashboard's AI cost, gross profit and effective hourly rate.

### Notifications (`app/notifications/`)
`NotificationAdapter.send()`; `DashboardAdapter` persists rows. `NtfyAdapter` POSTs the notification text to
`NTFY_URL/NTFY_TOPIC` only when both are set and `ntfy` is listed in `NOTIFY_ADAPTERS`. `email`, `slack`, `sms`
are stubs that never transmit.

## Phase 2 bid-to-cash flow
Opportunity discovered → Qualification → **Requirements extraction** (agents create `ComplianceRequirement`
rows, always `verified=false`) → Profitability analysis → Solution plan → Bid creation → **OWNER APPROVAL**
→ Bid submission (source-specific integration, owner-triggered) → Award (`WON` → `WorkOrder`) → Work execution
→ QA → **OWNER APPROVAL** (`approve_delivery`) → Delivery → Invoice → Payment tracking (`INVOICED`, `PAID`).

## Extending
* New source: subclass `OpportunitySource`, return `NormalizedOpportunity` items, register in
  `sources/registry.py`.
* New agent: prompt file in `app/prompts/`, output schema in `schemas.py`, class in `agents/`, mock builder in
  `llm/mock.py`.
* New threshold: add a `SettingSpec` in `settings_service.py`; it appears on `/settings` automatically.
* New notification channel: subclass `NotificationAdapter`, register in `notifications/adapters.py`.
