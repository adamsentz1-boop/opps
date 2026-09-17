# Opportunity Engine

A local-first platform that continuously finds freelance and contract opportunities where **AI can do most of
the work**, scores them on **expected profit per human hour**, drafts the proposal, and puts the result in an
**approval queue**. Nothing is ever submitted, sent, signed, purchased, or promised without an explicit human
decision recorded in the dashboard.

It runs in two profile modes. **Solo** hunts freelance/contract work for an independent consultant.
**Vendor** hunts public tenders and RFPs worldwide for an organisation selling a product - the mode used for
legal-technology RFP discovery.

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

## Worldwide legal-technology RFP discovery (vendor mode)

Switch the engine from "solo consultant" to "product vendor" in Settings:

```
profile_mode      = vendor         # scores product fit and bid effort instead of personal delivery hours
legal_tech_only   = true           # reject anything the local classifier does not consider legal technology
organization_name = <your entity>
product_profile   = <verified capabilities - see below>
target_regions    = worldwide      # or USA,GBR,IRL,DEU,...
excluded_regions  = <countries you will never bid in>
minimum_deal_value = 25000         # USD, after currency conversion
maximum_bid_effort_hours = 60
```

**Sources.** `TEDSource` (EU Tenders Electronic Daily) and `FindATenderSource` (UK) use public, keyless official
APIs; `SamGovSource` covers US federal. Enable with `TED_ENABLED=true` / `FTS_ENABLED=true`. Canada, Australia,
UN and World Bank ship as documented stubs explaining what official access each needs. Nothing is scraped.

**Legal-tech classifier** (`app/taxonomy.py`). Worldwide tender feeds are enormous, so every notice is
classified locally - no tokens - into one of thirteen categories (contract analytics, CLM, eDiscovery, document
review, legal research, matter management, court/case management, IP, regtech, records/FOI, privacy, legal AI).
Tenders for legal *services* (law-firm panels, legal advice) are separated from legal *technology*. Only
plausible matches reach Claude, which is what keeps the API bill proportionate to a global search.

**Currency.** Deal values are normalised to USD through a local, editable rate table (`fx_rates` in Settings)
so a EUR tender and a SGD tender are compared on one scale. There is no live FX call: the rates are
approximate by design and only need to answer "is this big enough to bid on".

**Verified product profile.** In vendor mode the `product_profile` setting is the only source of claims about
the company - capabilities, certifications, data residency, references, insurance, track record. It ships as a
template full of `[TEAM: ...]` placeholders. **Agents never fill those in.** Anything absent is reported as a
gap and an owner action, never invented.

Demo it offline:

```bash
python -m scripts.seed_legaltech      # worldwide legal-tech tenders + deliberate noise, mock mode
```

```
[recommended]  82   $669,600 IRL contract_analytics  Contract analytics and clause extraction platform
[recommended]  80 $1,143,000 GBR ediscovery          eDiscovery and document review platform (framework)
[rejected   ]   -   $324,000 IRL -                   Office furniture ......... Not a legal-technology opportunity
[rejected   ]   - $5,080,000 GBR -                   Panel of law firms ....... Not a legal-technology opportunity
[rejected   ]   -    $11,430 GBR contract_analytics  Contract review pilot .... Deal value below minimum $25,000
[rejected   ]   -   $800,000 RUS clm                 CLM platform ............. Country RUS is in excluded_regions
```

## n8n at the edges

n8n handles what sits either side of the engine, so the engine itself stays one job: judge the opportunity and
draft the bid.

* **Into the engine.** n8n watches whatever cannot be polled directly - inboxes, portal alert emails, RSS - and
  POSTs each notice to `POST /api/opportunities` (JSON: title, description, buyer, values, deadline, plus the
  bid fields). Set `"process": false` to queue it for the next scan instead of analysing it immediately. Adding
  a source becomes a workflow rather than a code change.
* **Out of the engine.** With `webhook` in `NOTIFY_ADAPTERS`, every alert is POSTed to `WEBHOOK_URL` as JSON.
  n8n routes it - phone, Slack, email, a spreadsheet row - and can branch on the numbers in the payload.

Both directions are owner-configured and off by default. The engine never reaches outward on its own.

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

* **Sources** (`app/sources/`): `ManualSource`, `GenericRSSSource`, `GenericJSONSource`, `SamGovSource`,
  `TEDSource` (EU), `FindATenderSource` (UK) - all official APIs - plus documented stubs for Upwork, PA
  procurement, private RFP feeds, CanadaBuys, AusTender, UNGM and the World Bank. No scraping: stubs explain
  what official access is required.
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
* **Notifications**: dashboard adapter enabled by default. Add `webhook` to `NOTIFY_ADAPTERS` and set
  `WEBHOOK_URL` to POST each alert as JSON to an n8n Webhook node, which then decides where it goes (phone,
  Slack, email). The payload carries the routable numbers: score, expected profit, recommended bid, human
  hours, country, category, deadline and source link. `WEBHOOK_TOKEN` is sent as `X-Engine-Token` so n8n can
  reject anything else. ntfy push works the same way; email/Slack/SMS remain non-sending stubs.

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
  taxonomy.py        legal-tech classification (local, no tokens)   fx.py  currency -> USD
  profiles.py        solo vs vendor profile modes
  sources/           adapters (manual, rss, json, sam_gov, ted, find_a_tender, stubs, registry)
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
