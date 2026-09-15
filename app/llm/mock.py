"""Deterministic heuristic stand-ins for Claude, used when no API key is configured.

The heuristics are intentionally simple keyword models. They exist so the platform can be
exercised end-to-end (pipeline, rejection, approval queue, dashboard) without network access.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, TypeVar

from pydantic import BaseModel

from app.schemas import (BuyerResearchOutput, MarketResearchOutput, PortfolioDecisionOutput, ProposalOutput, QAOutput,
                         QualificationOutput, RequirementItem, RequirementsOutput, ScoutOutput, SolutionOutput,
                         WorkPlanOutput, WorkTaskOutput)

T = TypeVar("T", bound=BaseModel)

PREFERRED = [
    "n8n", "workato", "api", "rest", "python", "data processing", "excel", "spreadsheet", "csv", "crm",
    "salesforce", "hubspot", "document", "classification", "ocr", "ai agent", "automation", "workflow",
    "report", "dashboard", "migration", "squarespace", "website", "docker", "linux", "server", "integration",
    "security questionnaire", "soc 2", "soc2", "google workspace", "google sheets", "microsoft", "power automate",
    "extraction", "pdf", "database", "sql", "zapier", "make.com", "airtable", "etl", "scrap", "webhook",
    "claude", "gpt", "llm", "chatbot", "openai", "anthropic",
]
AVOID = [
    "onsite", "on-site", "on site", "in person", "in-person", "physically install", "physical", "hardware install",
    "warehouse", "team of 5", "legal advice", "attorney", "lawyer", "medical", "clinical", "diagnos", "licensed", "electrician",
    "plumb", "construction", "fake review", "spam", "bypass", "captcha", "bot farm", "mass dm", "scrape linkedin",
    "scrape instagram", "full-time", "full time", "40 hours", "long-term daily", "manual data entry",
    "cold calling", "video editing", "voice over", "translation",
]
BIG_BUILD = ["mobile app", "ios app", "android app", "saas platform", "marketplace", "from scratch", "mvp", "full stack"]


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def _score_keywords(text: str, words: list[str]) -> int:
    lower = text.lower()
    return sum(1 for w in words if w in lower)


def _extract_budget(ctx: dict[str, Any]) -> tuple[float | None, str]:
    bmax = ctx.get("budget_max") or ctx.get("budget_min")
    return (float(bmax) if bmax else None), ctx.get("budget_type", "fixed")


def _qualification(ctx: dict[str, Any], text: str) -> QualificationOutput:
    lower = text.lower()
    seed = _seed(text)
    pref_hits = _score_keywords(lower, PREFERRED)
    avoid_hits = _score_keywords(lower, AVOID)
    big_hits = _score_keywords(lower, BIG_BUILD)
    budget, budget_type = _extract_budget(ctx)
    injection = bool(ctx.get("injection_flags"))

    technical_fit = max(5, min(98, 35 + pref_hits * 12 - avoid_hits * 25 - big_hits * 10))
    ai_pct = max(5, min(95, 40 + pref_hits * 10 - avoid_hits * 20 - big_hits * 15))
    words = len(lower.split())
    scope_clarity = max(10, min(95, 42 + min(words, 400) // 8 - (25 if "not sure" in lower or "tbd" in lower else 0)))
    risk = max(5, min(95, 25 + avoid_hits * 20 + big_hits * 10 + (35 if injection else 0)
                      + (15 if budget is None else 0) - pref_hits * 3))
    competition = 40 + (seed % 30)
    buyer_quality = max(10, min(90, 45 + (10 if ctx.get("buyer_name") else 0) + (seed % 20)
                                 - (25 if "asap" in lower and "cheap" in lower else 0)))

    if budget_type == "hourly":
        rate = budget or 40.0
        est_hours_total = 10 + big_hits * 30 + avoid_hits * 10
        human_hours = round(est_hours_total * (1 - ai_pct / 100) + 1.0, 1)
        recommended_price = round(max(rate, 75.0), 0)
        total_value = recommended_price * est_hours_total
    else:
        total_value = budget or (600.0 + pref_hits * 200)
        base_hours = max(2.0, total_value / 400.0) + big_hits * 20 + avoid_hits * 6
        human_hours = round(base_hours * (1 - ai_pct / 100) + 0.5, 1)
        recommended_price = round(max(total_value * (0.85 if pref_hits >= 3 else 0.75), 200.0), -1)

    agent_hours = round(max(0.5, (base_hours if budget_type != "hourly" else est_hours_total) * ai_pct / 100), 1)
    api_cost = round(agent_hours * 6.0, 2)
    other_cost = 0.0 if avoid_hits == 0 else 150.0
    expected_profit = recommended_price - api_cost - other_cost if budget_type != "hourly" else total_value * 0.9
    human_effort = max(2, min(98, int(human_hours * 8)))
    profitability = max(5, min(98, int(min(expected_profit / max(human_hours, 0.5), 600) / 6)))
    p_win = round(max(0.05, min(0.6, 0.15 + technical_fit / 400 - competition / 500)), 2)
    confidence = max(20, min(90, 52 + min(words, 300) // 8))

    red_flags: list[str] = []
    if injection:
        red_flags.append("Listing contains text that attempts to instruct the AI (prompt injection)")
    if avoid_hits:
        red_flags.append("Mentions work types the owner avoids (physical/licensed/manual/ToS-risky)")
    if budget is None:
        red_flags.append("No budget stated")
    if big_hits:
        red_flags.append("Looks like a large custom build")
    reject = technical_fit < 45 or avoid_hits >= 2 or (injection and risk > 70)
    reject_reasons = []
    if reject:
        reject_reasons = ["Low technical fit" if technical_fit < 45 else "Work type the owner avoids"]

    return QualificationOutput(
        category=ctx.get("category_hint") or ("automation" if pref_hits else "general"),
        summary=f"Buyer needs: {ctx.get('title', 'unspecified work')[:120]}.",
        technical_fit=technical_fit, ai_completable_percentage=ai_pct, human_effort=human_effort,
        profitability=profitability, scope_clarity=scope_clarity, buyer_quality=buyer_quality, risk=risk,
        competition=competition, confidence=confidence, estimated_human_hours=human_hours,
        estimated_agent_hours=agent_hours, estimated_api_cost=api_cost, estimated_other_cost=other_cost,
        recommended_price=recommended_price, estimated_probability_of_win=p_win, red_flags=red_flags,
        reject_recommended=reject, reject_reasons=reject_reasons,
        reasoning=(f"[MOCK] preferred-keyword hits={pref_hits}, avoid hits={avoid_hits}, big-build hits={big_hits}, "
                   f"budget={budget}, type={budget_type}. Scores derived heuristically."),
        injection_detected=injection,
    )


def _research(ctx: dict[str, Any], text: str) -> BuyerResearchOutput:
    buyer = ctx.get("buyer_name")
    lower = text.lower()
    industry = None
    for word, label in [("ecommerce", "E-commerce"), ("shopify", "E-commerce"), ("real estate", "Real estate"),
                        ("law firm", "Legal"), ("clinic", "Healthcare"), ("saas", "Software"), ("agency", "Agency"),
                        ("school", "Education"), ("nonprofit", "Non-profit"), ("logistics", "Logistics"),
                        ("accounting", "Accounting"), ("marketing", "Marketing")]:
        if word in lower:
            industry = label
            break
    urls = re.findall(r"https?://[^\s)]+", text)
    return BuyerResearchOutput(
        company=buyer, industry=industry, website=urls[0] if urls else None,
        likely_company_size="unknown (not stated)" if not industry else "small business (inferred)",
        relevant_context="[MOCK] No external research performed in mock mode; only listing text was used.",
        project_motivation="The buyer appears to want to reduce manual effort or fix a data/process gap (inferred).",
        potential_red_flags=[f for f in ["Budget not stated" if not ctx.get("budget_max") else ""] if f],
        personalization=[f"Refer to their stated goal: {ctx.get('title', '')[:80]}"],
        uncertain_items=["industry (inferred from keywords)", "company size (inferred)"],
        confidence=35,
    )


def _solution(ctx: dict[str, Any], text: str) -> SolutionOutput:
    lower = text.lower()
    tools = [t for t in ["n8n", "Workato", "Python", "Salesforce", "Google Sheets", "Docker", "Squarespace",
                         "Airtable", "Zapier", "HubSpot", "Excel", "Postgres", "Claude API"]
             if t.lower() in lower] or ["Python", "n8n"]
    apis = [a for a in ["Salesforce API", "HubSpot API", "Google Sheets API", "Shopify API", "QuickBooks API",
                        "Stripe API", "Slack API", "Notion API", "Airtable API"] if a.split()[0].lower() in lower]
    human_hours = float(ctx.get("estimated_human_hours") or 2.0)
    agent_hours = float(ctx.get("estimated_agent_hours") or 4.0)
    feasible = "onsite" not in lower and "in person" not in lower
    profitable = float(ctx.get("expected_profit") or 0) >= 200
    return SolutionOutput(
        feasible=feasible, profitable=profitable,
        verdict=("[MOCK] Deliverable with AI-generated workflows/scripts plus a short owner review; "
                 + ("profitable at the recommended price." if profitable else "margin looks thin.")),
        proposed_solution=f"Build the requested automation using {', '.join(tools[:3])}, with Claude generating the "
                          f"workflow/script, test data, and documentation; owner reviews and deploys.",
        implementation_steps=["Confirm access, sample data and acceptance criteria with buyer",
                              "Claude drafts the workflow/script and test cases",
                              "Run against sample data; fix edge cases",
                              "Owner reviews output and security of credentials handling",
                              "Deploy to buyer environment; hand over documentation"],
        required_tools=tools, apis_required=apis,
        external_accounts_required=[f"Buyer-provided access to {a.replace(' API', '')}" for a in apis],
        likely_blockers=["Buyer slow to provide credentials or sample data", "Undocumented edge cases in data"],
        assumptions=["Buyer supplies API access and sample data", "Scope stays as described in the listing"],
        claude_involvement="Drafts all code/workflows, documentation, test cases, and handover notes.",
        human_involvement="Kick-off call, credential handling, final review, deployment sign-off.",
        estimated_human_hours=human_hours, estimated_agent_hours=agent_hours,
        qa_strategy="Run against buyer sample data; verify counts/fields; dry-run before production changes.",
        deliverables=ctx.get("deliverables") or ["Working automation", "Documentation", "Handover notes"],
        risk_factors=["Scope creep", "Third-party API limits"],
    )


def _proposal(ctx: dict[str, Any], text: str) -> ProposalOutput:
    price = float(ctx.get("recommended_price") or 500)
    title = ctx.get("title", "Proposal")
    body = (
        f"Hi{(' ' + ctx['buyer_name']) if ctx.get('buyer_name') else ''},\n\n"
        f"I read your post about {title.lower()}. Here is how I would approach it:\n\n"
        f"1. Confirm the exact inputs, outputs and acceptance criteria with you (30 min).\n"
        f"2. Build the automation using {', '.join((ctx.get('required_tools') or ['proven tooling'])[:3])}.\n"
        f"3. Test against your real sample data and fix edge cases.\n"
        f"4. Hand over documentation and a short walkthrough.\n\n"
        f"Price: ${price:,.0f} fixed. Timeline: {max(3, int(ctx.get('estimated_human_hours', 2)) + 3)} days after "
        f"access is provided.\n\n"
        f"What I need from you: access to the systems involved and a small sample of real data.\n\n"
        f"Happy to answer questions before you decide."
    )
    return ProposalOutput(title=f"Proposal: {title[:80]}", body=body, price=price, pricing_model="fixed",
                          timeline_days=float(max(3, int(ctx.get("estimated_human_hours", 2)) + 3)),
                          milestones=["Kick-off & requirements", "Build & test", "Handover"],
                          questions_for_buyer=["Which systems/accounts will you provide access to?",
                                               "Can you share a sample of the real data?"],
                          capability_claims=["Automation & integration consultant (from owner profile)"])


def _work_plan(ctx: dict[str, Any], text: str) -> WorkPlanOutput:
    return WorkPlanOutput(
        project_plan="[MOCK] Kick-off -> build -> test -> owner QA -> owner approval -> delivery.",
        tasks=[WorkTaskOutput(title="Collect inputs from buyer", owner="human", estimated_hours=0.5),
               WorkTaskOutput(title="Draft solution", owner="agent", depends_on=["Collect inputs from buyer"],
                              estimated_hours=0),
               WorkTaskOutput(title="Test against sample data", owner="agent", depends_on=["Draft solution"]),
               WorkTaskOutput(title="Owner review", owner="human", depends_on=["Test against sample data"],
                              estimated_hours=1.0, requires_owner_approval=True),
               WorkTaskOutput(title="Deliver to buyer", owner="human", depends_on=["Owner review"],
                              estimated_hours=0.5, requires_owner_approval=True)],
        required_inputs=["System access", "Sample data", "Acceptance criteria"],
        deliverables=ctx.get("deliverables") or ["Working automation", "Documentation"],
        qa_checklist=["Output matches acceptance criteria", "No credentials in deliverables",
                      "Edge cases handled", "Documentation complete"],
        approval_checkpoints=["Before any production change", "Before delivery", "Before invoicing"],
    )


def _qa(ctx: dict[str, Any], text: str) -> QAOutput:
    return QAOutput(passed=True, findings=["[MOCK] No automated QA performed"], blocking_issues=[],
                    summary="Mock QA: manual owner review still required.")


_REQ_PATTERNS = [
    ("soc 2", "SOC 2 report or attestation", "certification"), ("iso 27001", "ISO 27001 certification", "certification"),
    ("cmmc", "CMMC certification", "certification"), ("hipaa", "HIPAA compliance / BAA", "representation"),
    ("insurance", "Proof of insurance", "insurance"), ("liability insurance", "General liability insurance", "insurance"),
    ("w-9", "W-9 form", "form"), ("w9", "W-9 form", "form"), ("nda", "Signed NDA", "contract_clause"),
    ("background check", "Background check", "representation"), ("us citizen", "US citizenship", "representation"),
    ("u.s. citizen", "US citizenship", "representation"), ("vendor registration", "Vendor/portal registration", "form"),
    ("licensed", "Professional licence", "license"), ("certified", "Named certification", "certification"),
]


def _requirements(ctx: dict[str, Any], text: str) -> RequirementsOutput:
    lower = text.lower()
    seen, items = set(), []
    for key, label, rtype in _REQ_PATTERNS:
        if key in lower and label not in seen:
            seen.add(label)
            items.append(RequirementItem(requirement=label, type=rtype, mandatory="must" in lower or "required" in lower,
                                         notes=f"[MOCK] keyword '{key}' found in listing"))
    return RequirementsOutput(requirements=items, summary=f"[MOCK] {len(items)} formal requirement(s) detected.")


def _scout(ctx: dict[str, Any], text: str) -> ScoutOutput:
    lower = text.lower()
    return ScoutOutput(worth_qualifying=_score_keywords(lower, PREFERRED) > 0,
                       reason="[MOCK] keyword triage", extracted_skills=[w for w in PREFERRED if w in lower][:8])


# --------------------------------------------------------------------------- market challenge mocks
def _market_research(ctx: dict[str, Any], text: str) -> MarketResearchOutput:
    """Deterministic research from the supplied quote only. Never invents news, earnings or ratings."""
    ticker = str(ctx.get("ticker", "")).upper()
    price = float(ctx.get("price") or 0.0)
    prev = float(ctx.get("previous_close") or price or 0.0)
    change_pct = ((price - prev) / prev * 100.0) if prev else 0.0
    seed = _seed(ticker)
    upside = round(8.0 + (seed % 25), 1)                       # 8..32 %
    downside = round(4.0 + ((seed // 7) % 16), 1)              # 4..19 %
    rr = round(upside / downside, 2) if downside else 0.0
    confidence = 45 + (seed % 35)
    avoid = bool(ctx.get("injection_flags")) or price <= 0 or ctx.get("asset_type") not in (None, "stock", "etf", "unknown")
    if ctx.get("force_avoid"):
        avoid = True
    gaps = ["No fundamentals supplied", "No news supplied", "No analyst data supplied"]
    return MarketResearchOutput(
        ticker=ticker,
        summary=(f"[MOCK] {ticker} last {price:.2f} vs previous close {prev:.2f} ({change_pct:+.2f}%). "
                 f"Only the supplied quote was analysed; no fundamentals or news were available."),
        bull_case=f"[MOCK] If recent price behaviour continues, a move of roughly +{upside:.0f}% is plausible.",
        bear_case=f"[MOCK] A reversal of roughly -{downside:.0f}% is plausible given normal volatility.",
        catalysts=["[MOCK] No catalysts supplied by the application"],
        risks=["Small-account concentration risk", "Volatility could exceed the modelled downside",
               "No news or fundamentals were available to the agent"],
        time_horizon="1-3 months", confidence=confidence, expected_upside_pct=upside,
        expected_downside_pct=downside, risk_reward_ratio=rr,
        avoid_trade=avoid, avoid_reason=("Data unavailable or asset type not allowed" if avoid else ""),
        data_gaps=gaps, injection_detected=bool(ctx.get("injection_flags")),
    )


def _portfolio_decision(ctx: dict[str, Any], text: str) -> PortfolioDecisionOutput:
    """Deterministic decision layer: buy the best-ranked eligible research candidate that fits in cash, else HOLD."""
    if ctx.get("force_hold"):
        return PortfolioDecisionOutput(action="HOLD", reason_for_trade="[MOCK] Forced HOLD for testing.")
    cash = float(ctx.get("cash") or 0.0)
    portfolio_value = float(ctx.get("portfolio_value") or cash)
    max_trade_pct = float(ctx.get("max_single_trade_pct") or 60.0)
    max_position_pct = float(ctx.get("max_position_pct") or 60.0)
    reserve_pct = float(ctx.get("min_cash_reserve_pct") or 0.0)
    positions = {str(p.get("ticker")).upper(): p for p in (ctx.get("positions") or [])}
    candidates = []
    for r in ctx.get("research") or []:
        if r.get("avoid_trade") or not r.get("price"):
            continue
        if float(r.get("risk_reward_ratio") or 0) < 1.5 or int(r.get("confidence") or 0) < 50:
            continue
        candidates.append(r)
    candidates.sort(key=lambda r: (-float(r["risk_reward_ratio"]), -int(r["confidence"]), r["ticker"]))
    spendable = min(cash - portfolio_value * reserve_pct / 100.0, portfolio_value * max_trade_pct / 100.0)
    for r in candidates:
        ticker = str(r["ticker"]).upper()
        price = float(r["price"])
        held_value = float(positions.get(ticker, {}).get("market_value") or 0.0)
        room = portfolio_value * max_position_pct / 100.0 - held_value
        budget = round(min(spendable, room), 2)
        if budget < 1.0:
            continue
        qty = round(budget / price, 4)
        total = round(qty * price, 2)
        return PortfolioDecisionOutput(
            action="BUY", ticker=ticker, quantity=qty, estimated_price=price, estimated_total=total,
            thesis=f"[MOCK] {ticker} offers the best supplied risk/reward ({r['risk_reward_ratio']}x) among the watchlist.",
            reason_for_trade=(f"[MOCK] Deploys ${total:.2f} of ${cash:.2f} cash within the {max_trade_pct:.0f}% "
                              f"single-trade limit. Not a guaranteed outcome."),
            catalysts=list(r.get("catalysts") or []), risks=list(r.get("risks") or []),
            time_horizon=str(r.get("time_horizon") or "1-3 months"), confidence=int(r.get("confidence") or 0),
            expected_upside_pct=float(r.get("expected_upside_pct") or 0),
            expected_downside_pct=float(r.get("expected_downside_pct") or 0),
            risk_reward_ratio=float(r.get("risk_reward_ratio") or 0),
        )
    return PortfolioDecisionOutput(action="HOLD",
                                   reason_for_trade="[MOCK] No candidate met the risk/reward and confidence bar "
                                                    "within the available cash, so holding is the right call.")


def mock_response(agent: str, output_model: type[T], ctx: dict[str, Any], user_content: str) -> T:
    # Only the opportunity's own text drives the heuristics - never the trusted system blocks in user_content.
    text = " ".join(str(v) for v in [ctx.get("title", ""), ctx.get("description", "")]).strip() or user_content
    builders = {
        QualificationOutput: _qualification, BuyerResearchOutput: _research, SolutionOutput: _solution,
        ProposalOutput: _proposal, WorkPlanOutput: _work_plan, QAOutput: _qa, ScoutOutput: _scout,
        RequirementsOutput: _requirements, MarketResearchOutput: _market_research,
        PortfolioDecisionOutput: _portfolio_decision,
    }
    builder = builders.get(output_model)
    if builder is None:
        raise ValueError(f"No mock available for {output_model.__name__}")
    return builder(ctx, text)  # type: ignore[return-value]
