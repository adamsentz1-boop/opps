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
| `TEDSource` | enabled via `TED_ENABLED` | EU TED Search API v3, public and keyless; CPV + keyword + country queries, multilingual value flattening, automatic retry with a minimal field list if TED rejects the requested fields |
| `FindATenderSource` | enabled via `FTS_ENABLED` | UK Find a Tender OCDS release packages, public and keyless, paginated, defensive OCDS parsing |
| `CanadaBuysSource`, `AusTenderSource`, `UNGMSource`, `WorldBankSource` | stubs | each documents the official access required |
| `SamGovSource` | enabled via `SAM_GOV_API_KEY` | official public API; NAICS/keyword queries, description fetch, de-dupe by noticeId |
| `PennsylvaniaProcurementSource` | stub | no API; permitted feed/export or manual only |
| `PrivateRFPFeedSource` | stub | subscriber RSS/JSON under aggregator terms |

`registry.get_sources()` builds the active list from `.env`.

### Normalizer (`app/normalizer.py`)
Maps any payload to `NormalizedOpportunity` (title, description, buyer, budget min/max/type, dates, url,
skills, deliverables, raw text). Parses budgets from free text (`$1,500-2,000`, `$45/hr`). De-duplicates on
`(source, external_id)`. Runs injection detection on insert and stores `injection_flags`.

### Document ingestion (`app/documents/`)
`StirlingClient` talks to the owner's existing Stirling PDF container (`PDF_SERVICE_URL`, default
`http://host.docker.internal:8080`; `X-API-KEY` only if Stirling security is on). Verified endpoints:
`GET /api/v1/info/status`, `POST /api/v1/convert/pdf/text` (PDFBox text layer), `POST /api/v1/misc/ocr-pdf`
(Tesseract, `sidecar=true` returns a zip with the `.txt`), `POST /api/v1/security/get-info-on-pdf`.
`extract()` takes the text layer first and OCRs only when a document is effectively image-only.
`service.py` stores `Attachment` rows (files under `data/attachments/<opportunity>/`), downloads URLs only from
`PDF_DOWNLOAD_ALLOWED_HOSTS`, ingests **sequentially under a lock**, keeps unreachable-service failures as
`PENDING` (auto-retry), other failures as `FAILED` (manual retry), and exposes `attachment_blocks()` which
renders extracted text as `<untrusted_opportunity_data>` blocks for `BaseAgent.opportunity_block()`. Injection
flags found in attachments are merged into the opportunity's flags before scoring. `process_pending()` runs
`ingest_pending()` first, wrapped so that ingestion can never stop the scheduler.

### Profile modes (`app/profiles.py`)
`solo` frames the shared Opportunity/Analysis schema as an independent consultant's delivery hours; `vendor`
frames the same fields as product fit and internal bid effort for an organisation answering RFPs. The mode
selects a prompt addendum (`app/prompts/_profile_solo.md` / `_profile_vendor.md`) appended to the shared
guardrails, changes which threshold settings apply, and relabels the dashboard. One pipeline, one audit trail.

### Legal-technology classification (`app/taxonomy.py`) and currency (`app/fx.py`)
`classify()` scores a notice against thirteen legal-tech category keyword families, legal-buyer signals, and
classification codes, and separates legal *services* from legal *technology*. It runs at insert time in
`enrich_opportunity()` - locally, before any Claude call - so a worldwide feed can be filtered without token
spend. `fx.to_usd()` normalises deal values through an editable local rate table (`fx_rates`); unknown
currencies return `None` and are reported rather than guessed.

### Pipeline (`app/pipeline/runner.py`)
`process_opportunity(db, id)`:
1. `pre_rules` (no tokens): physical/onsite, licensed, ToS/deceptive, full-time, low budget, ≥3 injection
   patterns, unrealistic deadline → `REJECTED` (stage `rules`).
2. `QualificationAgent` → `OpportunityAnalysis`; `scoring.apply_to_analysis` computes economics and
   `OPPORTUNITY_SCORE`; `post_rules` checks thresholds → `REJECTED` (stage `thresholds`) or `QUALIFIED`.
3. `ResearchAgent` → `Buyer` (non-fatal on failure).
4. `SolutionArchitectAgent` → `SolutionPlan`; rejects if not feasible/profitable or owner hours too high
   (stage `solution`).
5. `ensure_structural_requirements` (bids: registration, set-aside eligibility, deadline) and
   `RequirementsAgent` → `ComplianceRequirement` rows (`verified=false`, owner-verified rows preserved on
   reanalysis; non-fatal on failure).
5b. Bids only: `BidAgent` → `BidDocument` rows (`DRAFT`); owner-approved documents survive redrafts; the
   package price updates the analysis economics.
6. `ProposalAgent` → `Proposal` v1 (for bids: the executive summary).
7. `AWAITING_APPROVAL`; notification if score ≥ `notify_min_score`.

In vendor mode `pre_rules` additionally rejects non-legal-tech notices (when `legal_tech_only`), countries in
`excluded_regions` or outside `target_regions`, and deal values below `minimum_deal_value` after USD
conversion; `post_rules` measures effort against `maximum_bid_effort_hours`. `pre_rules` also rejects any
opportunity whose response deadline has already passed, and uses
`estimated_value` as the budget for bids that state no budget.

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
* `approve_opportunity` → `READY_TO_SUBMIT` (blocked if any mandatory `ComplianceRequirement` is unverified, or,
  for bids, if any `BidDocument` is not `OWNER_APPROVED`)
* `edit_bid_document` / `approve_bid_document` → per-document sign-off; `[OWNER: ...]` placeholders block approval
* `approve_deliverable` → single deliverable approved for delivery (QA failure blocks it)
* `reject_opportunity` → `DECLINED`
* `edit_proposal` → new `Proposal` version; an edit after approval returns to `AWAITING_APPROVAL`
* `request_reanalysis` → approval record, then pipeline re-run
* `record_outcome` → `SUBMITTED` / `INTERVIEWING` / `WON` / `LOST` (owner reports what happened externally)
* `verify_requirement` → the only path to `verified=True`, requires evidence
* `approve_delivery` → work order `READY_FOR_DELIVERY`

`Approval` and `AuditLog` rows raise on update/delete (SQLAlchemy event guards).

### Work execution (`app/work_orders.py`)
`WorkOrder` statuses: NEW → PLANNING → WAITING_FOR_INPUT/IN_PROGRESS → QA → AWAITING_OWNER_APPROVAL →
READY_FOR_DELIVERY (only via `approve_delivery`) → DELIVERED → INVOICED → PAID. `WorkAgent` produces the plan,
tasks with dependencies, required inputs, deliverables, QA checklist and approval checkpoints. `QAAgent`
reviews deliverable content against the checklist. Nothing is sent to a client by the system.

Execution: `execute_task` (owner-triggered, agent-owned tasks only, dependencies must be DONE) asks
`WorkAgent.draft_deliverable` for content and writes it to `workspace/<work_order_id>/<file>-v<n>.<ext>`;
`run_qa` asks `QAAgent` to review the file against the QA checklist and sets `qa_passed`; moving to
`AWAITING_OWNER_APPROVAL` is refused while any deliverable has failed QA; `DELIVERED` requires
`approve_delivery`; `INVOICED` writes `invoice-draft.md` (data-driven, no LLM) for the owner to send.

### Data model (SQLite)
`opportunities`, `opportunity_analysis`, `solution_plans`, `buyers`, `proposals`, `bid_documents`, `approvals`,
`source_runs`, `agent_runs`, `work_orders`, `work_tasks`, `deliverables`, `compliance_requirements`, `audit_log`,
`settings`, `notifications`, `attachments`. `init_db` adds any columns missing from an existing SQLite file (forward-only
migration), so upgrading keeps your data.

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
`NotificationAdapter.send()`; `DashboardAdapter` persists rows. `WebhookAdapter` (registered as both `webhook`
and `n8n`) POSTs the notification as JSON to `WEBHOOK_URL` with an optional `X-Engine-Token`, carrying the
opportunity's economics in `meta` so an n8n workflow can branch on score, profit, country or deadline without
calling back. `NtfyAdapter` POSTs the notification text to
`NTFY_URL/NTFY_TOPIC` only when both are set and `ntfy` is listed in `NOTIFY_ADAPTERS`. `email`, `slack`, `sms`
are stubs that never transmit.

## Bid-to-cash flow (implemented)
Opportunity discovered (SAM.gov / manual / feed) → Qualification → Requirements extraction (always
`verified=false`) → Profitability analysis → Solution plan → Bid package (`BidDocument` drafts) →
**OWNER APPROVAL** (each document, each mandatory requirement, then the opportunity) → `READY_TO_SUBMIT` →
owner submits through the portal and records `SUBMITTED` → Award (`WON` → `WorkOrder`) → Work execution
(agent drafts in `workspace/`) → QA (`QAAgent`) → **OWNER APPROVAL** (`approve_deliverable`,
`approve_delivery`) → Delivery (owner) → Invoice draft → Payment tracking (`INVOICED`, `PAID`).
Source-specific electronic submission is deliberately not implemented; every submission is a human act.

## Extending
* New source: subclass `OpportunitySource`, return `NormalizedOpportunity` items, register in
  `sources/registry.py`.
* New agent: prompt file in `app/prompts/`, output schema in `schemas.py`, class in `agents/`, mock builder in
  `llm/mock.py`.
* New threshold: add a `SettingSpec` in `settings_service.py`; it appears on `/settings` automatically.
* New notification channel: subclass `NotificationAdapter`, register in `notifications/adapters.py`.
