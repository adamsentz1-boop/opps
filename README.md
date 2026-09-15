# Opportunity Engine

A local-first platform that continuously finds freelance and contract opportunities where **AI can do most of
the work**, scores them on **expected profit per human hour**, drafts the proposal, and puts the result in an
**approval queue**. Nothing is ever submitted, sent, signed, purchased, or promised without an explicit human
decision recorded in the dashboard.

Phase 1 covers freelance/contract listings from manual entry, RSS/Atom feeds and JSON feeds.
Phase 2 adds formal bids: a SAM.gov adapter (official API), bid-package drafting with per-document owner
sign-off, compliance requirements you verify yourself, and post-award execution where agents draft
deliverables into a workspace, QA reviews them, and you approve delivery and invoicing.

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

## PDF ingestion (RFP attachments) via your existing Stirling PDF

Solicitation PDFs, statements of work and amendments are processed **locally** by the Stirling PDF container
already running on the host. No second PDF stack is installed and nothing is uploaded to a third-party OCR
service. Only the extracted text reaches Claude, and only when the workflow makes an agent call.

```
PDF / RFP attachment ──► Stirling PDF (local) ──► text layer, or OCR only when the PDF is scanned
        ──► sanitisation + prompt-injection scan ──► untrusted blocks in agent prompts ──► RequirementsAgent …
```

Configuration (`.env`):

```
PDF_INGESTION_ENABLED=true
PDF_SERVICE_URL=http://host.docker.internal:8080   # the host's stirling-pdf; 127.0.0.1 inside the container is NOT the host
PDF_SERVICE_TYPE=stirling
PDF_REQUEST_TIMEOUT_SECONDS=120
PDF_SERVICE_API_KEY=                                # only if Stirling login is enabled (X-API-KEY)
PDF_OCR_LANGUAGES=eng
PDF_DOWNLOAD_ALLOWED_HOSTS=sam.gov,api.sam.gov,beta.sam.gov
```

`docker-compose.yml` maps `host.docker.internal` to the host gateway so the app container can reach the
Stirling container published on host port 8080. Humans keep using `http://pdf.home.arpa`.

Behaviour:
- Digital PDFs use the existing text layer (`/api/v1/convert/pdf/text`); OCR (`/api/v1/misc/ocr-pdf`, skip-text,
  sidecar) runs only when a PDF has fewer than `PDF_MIN_CHARS_PER_PAGE` characters per page.
- Attachments are processed one at a time, during scans or on demand, so the Lenovo is never saturated.
- If Stirling is stopped, attachments stay `PENDING` with the error shown on the Sources page and the
  opportunity page; the scheduler and the rest of the pipeline keep running. Start the container and click
  "Ingest pending attachments now" or Retry.
- SAM.gov notice resource links are queued as attachments and downloaded only from allowed hosts; anything
  else is uploaded manually on the opportunity page or the Sources form.
- Extracted text is sanitised and injection-scanned; flags are shown per attachment and count against the
  opportunity. It is never treated as instructions.

## Phase 2: bids, RFPs and work execution

**Sources.** Set `SAM_GOV_API_KEY` (free at api.data.gov) plus `SAM_GOV_NAICS` and/or `SAM_GOV_KEYWORDS` to pull
federal notices through the official SAM.gov API. Notices arrive as `bid`-type opportunities with agency,
solicitation number, set-aside, NAICS and response deadline. State/local RFPs can be entered manually (the
Sources form has a bid section) or fed through a permitted JSON/RSS feed.

**Bid pipeline.** Bids go through the same qualification and solution planning. Then:
- structural requirements are created automatically (SAM.gov registration, set-aside eligibility, deadline),
  and `RequirementsAgent` extracts the rest (insurance, forms, representations). All start `verified=false`.
- `BidAgent` drafts the package: cover letter, technical approach, price schedule, past performance
  (only from your verified profile; otherwise it says so), and a forms/representations checklist marked
  OWNER ACTION.
- You edit and **approve each document**. Documents that still contain `[OWNER: ...]` placeholders cannot be
  approved. The opportunity itself cannot be approved until every document is approved and every mandatory
  requirement is verified with evidence. Approval leads to `READY_TO_SUBMIT`; you submit through the portal.

**Execution after award.** Marking an opportunity WON creates a work order with an agent plan. On the work
order page you can run agent-owned tasks: `WorkAgent` drafts the deliverable (script, workflow JSON, report,
docs) into `workspace/<work_order>/`, `QAAgent` reviews it against the checklist, and you approve each
deliverable and then delivery. `INVOICED` writes a draft invoice into the workspace. The system never sends,
deploys, or invoices anything itself.

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

* **Sources** (`app/sources/`): `ManualSource`, `GenericRSSSource`, `GenericJSONSource`, `SamGovSource`
  (official API), plus documented stubs for Upwork, Pennsylvania procurement and private RFP feeds. No
  scraping: stubs explain what official access is required.
* **Agents** (`app/agents/`, prompts in `app/prompts/*.md`, editable live): Scout, Qualification, Research,
  SolutionArchitect, Requirements, Bid, Proposal, Work, QA. All use Claude structured outputs (`messages.parse`) and every call is
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
* `ComplianceRequirement.verified` defaults to `false` and can only be set through the owner verification route
  with evidence. Unverified mandatory requirements block approval; so do unapproved bid documents.
* Deliverable drafts are written only under `workspace/` and read back only from there; the SAM.gov key is
  sent as the documented query parameter and never logged (HTTP errors are reported without the URL).

## Mock mode

Without `ANTHROPIC_API_KEY` (or with `LLM_MOCK=true`) the platform runs a deterministic keyword-based stand-in
for Claude so the pipeline, dashboard, approvals and tests work offline. Mock outputs are labelled `[MOCK]`.
Set the key to use `claude-opus-5` (configurable via `CLAUDE_MODEL`, `CLAUDE_EFFORT`).

## Layout

```
app/
  main.py            FastAPI app + scheduler lifecycle
  config.py          env settings (.env)
  db.py, models.py   SQLAlchemy / SQLite
  schemas.py         NormalizedOpportunity + structured agent outputs
  sanitize.py        untrusted-content handling
  normalizer.py      payload -> NormalizedOpportunity -> DB
  sources/           adapters (manual, rss, json, sam_gov, stubs, registry)
  agents/            agent roles      prompts/  editable system prompts
  llm/               Claude client + mock
  pipeline/          rejection, scoring, runner
  approvals.py       approval system   work_orders.py  execution lifecycle
  audit.py, costs.py, metrics.py, notifications/, scheduler.py, settings_service.py
  web/               routes, JSON API, templates, static
scripts/seed.py      realistic fake opportunities    scripts/run_pipeline.py  one scan cycle
tests/               pytest suite (mock mode)
```

See `ARCHITECTURE.md` for design details and `TODO.md` for the roadmap.
