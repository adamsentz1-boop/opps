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

## Next
- [ ] Run the seed set against the real Claude API and tune prompts/thresholds with actual outputs
- [ ] Prompt-cache the guardrails/system prompt across agents (already marked `cache_control`; verify hit rate)
- [ ] Owner "capability inventory" table (tools, accounts, verified skills) to feed proposals and requirements
- [ ] ScoutAgent triage switch per source for noisy feeds
- [ ] Web-fetch based buyer research (Claude web fetch tool) with domain allow-list and owner opt-in
- [ ] ntfy transport (opt-in) for NEW MONEY OPPORTUNITY notifications; then email/Slack/SMS
- [ ] Batch API for bulk qualification when a feed delivers many items at once
- [ ] Proposal export (copy button / markdown download) to make manual submission faster
- [ ] Per-source rate limits and fetch state (last seen id / etag)
- [ ] Alembic migrations once the schema stabilises

## Phase 2
- [ ] SAM.gov adapter via official API (api.data.gov key, UEI); notice → ComplianceRequirement extraction agent
- [ ] State/municipal/university/school district procurement adapters where official feeds exist
- [ ] Requirements extraction agent (certifications, representations, insurance, forms, deadlines) that creates
      `ComplianceRequirement` rows with `verified=false`
- [ ] Bid package assembly (forms, attachments) with owner sign-off per document
- [ ] Source-specific submission integrations triggered only from READY_TO_SUBMIT by the owner
- [ ] WorkAgent execution: agent-produced deliverables in `workspace/`, QAAgent reviews, owner approves delivery
- [ ] Invoice generation (draft only) and payment tracking; realised effective hourly rate
- [ ] Multi-user auth if the dashboard is ever exposed beyond localhost
