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

## Next
- [ ] Run the seed set against the real Claude API and tune prompts/thresholds with actual outputs
- [ ] Prompt-cache the guardrails/system prompt across agents (already marked `cache_control`; verify hit rate)
- [ ] Owner "capability inventory" table (tools, accounts, verified skills) to feed proposals and requirements
- [ ] ScoutAgent triage switch per source for noisy feeds
- [ ] Web-fetch based buyer research (Claude web fetch tool) with domain allow-list and owner opt-in
- [x] Outbound webhook adapter for n8n (alerts out, with routable economics in the payload)
- [ ] email / Slack / SMS transports (ntfy and webhook are done, opt-in)
- [ ] Batch API for bulk qualification when a feed delivers many items at once
- [ ] Per-source rate limits and fetch state (last seen id / etag)
- [ ] Alembic migrations once the schema stabilises

## Phase 2 - done
- [x] SAM.gov adapter via the official public API (NAICS + keyword queries, description fetch, de-dupe)
- [x] Bid-type opportunities: agency, solicitation, set-aside, NAICS, estimated value, deadline; deadline rule
- [x] Structural + extracted ComplianceRequirements for bids, all unverified until the owner verifies
- [x] BidAgent bid package (cover letter, technical, price, past performance, forms checklist) with per-document
      owner sign-off, placeholder guard, redraft that keeps approved documents
- [x] Work execution: WorkAgent drafts deliverables into `workspace/`, QAAgent review, per-deliverable approval,
      delivery gate, draft invoice on INVOICED
- [x] Forward-only SQLite column migration so existing databases upgrade in place
- [x] Deadlines widget, bid filter, bid fields on the manual intake form

## Worldwide legal-tech sourcing - done
- [x] Profile modes (`solo` / `vendor`) with mode-specific prompt addenda, thresholds and dashboard labels
- [x] Legal-technology classifier: 13 categories, legal-buyer signals, legal-services exclusion, CPV hints
- [x] Currency normalisation to USD via a local editable rate table
- [x] TED (EU) adapter on the public Search API v3, with field-rejection fallback
- [x] UK Find a Tender adapter on the public OCDS API, paginated and defensively parsed
- [x] Region gating (`target_regions` / `excluded_regions`) and USD deal-value floor
- [x] Verified product profile that agents may never fill in; stubs for CanadaBuys, AusTender, UNGM, World Bank
- [x] `scripts/seed_legaltech.py` vendor-mode demo

## Worldwide legal-tech sourcing - next
- [ ] Run TED and Find a Tender against the live APIs and tune the CPV/keyword sets from real hit rates
- [ ] Non-English notices: TED returns many languages; decide translate-then-classify vs per-language keywords
- [ ] Framework/DPS awareness (a call-off is a different economic shape from a new award)
- [ ] Incumbent detection from prior award notices on the same buyer + CPV
- [ ] CanadaBuys and AusTender adapters against their official open-data exports

## Phase 2 - next
- [x] Attachment ingestion for solicitations through the existing local Stirling PDF service (text layer first,
      OCR only for scanned PDFs, sequential, graceful when the container is stopped, SAM.gov resource links queued)
- [ ] DOCX/XLSX attachments via Stirling's convert endpoints (verify `/api/v1/convert/file/pdf` first)
- [ ] Auto-reanalyze an opportunity once all its pending attachments finish extracting
- [ ] State/municipal/university/school district procurement adapters where official feeds exist (PA eMarketplace
      has none; keep manual/JSON inbox)
- [ ] Tune BidAgent / RequirementsAgent prompts on real solicitations; page-limit and format constraints
- [ ] Source-specific electronic submission helpers (pre-filled portal checklists), still owner-triggered
- [ ] Work execution: multi-file deliverables, agent-run test execution in a sandbox, revision loop from QA findings
- [ ] Realised effective hourly rate per work order on the dashboard
- [ ] Multi-user auth if the dashboard is ever exposed beyond localhost
