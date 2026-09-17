"""Seed realistic fake LEGAL-TECHNOLOGY tenders for a vendor-mode demo.

Usage:  python -m scripts.seed_legaltech [--no-process] [--purge]

Switches the engine into vendor mode, then inserts a worldwide mix of legal-tech RFPs plus deliberate noise
(furniture, legal services, tiny pilots, wrong region) so the rejection engine can be seen working.
Seeding with a real ANTHROPIC_API_KEY spends tokens; LLM_MOCK=true gives a free demo.
"""
from __future__ import annotations

import argparse
from datetime import timedelta

from app.db import init_db, session_scope
from app.models import Opportunity, utcnow
from app.normalizer import upsert_opportunity
from app.pipeline import process_opportunity
from app.settings_service import set_setting
from app.sources.manual import normalize_manual

now = utcnow()
PREFIX = "lt-"

SEEDS: list[dict] = [
    {"external_id": "lt-001", "title": "Contract analytics and clause extraction platform",
     "agency": "Ministry of Justice (Ireland)", "buyer_name": "Ministry of Justice (Ireland)", "country": "IRL",
     "currency": "EUR", "estimated_value": 620000, "solicitation_number": "MOJ-IE-2026-114",
     "cpv_codes": ["48311000", "72316000"], "deadline": (now + timedelta(days=28)).isoformat(),
     "description": """The Department seeks a contract analytics platform to support its legal division. Required
capabilities: clause extraction from PDF and Word contracts, lease abstraction, obligation tracking, bulk
contract review, and export to the existing records system. EU data residency is mandatory. Vendor must hold
ISO 27001 and provide two public-sector references. Initial term three years with two one-year extensions.
Estimated value EUR 620,000. Sample contracts are attached."""},

    {"external_id": "lt-002", "title": "eDiscovery and document review platform (framework)",
     "agency": "HM Courts & Tribunals Service", "buyer_name": "HM Courts & Tribunals Service", "country": "GBR",
     "currency": "GBP", "estimated_value": 900000, "solicitation_number": "HMCTS-2026-0098",
     "cpv_codes": ["48311000"], "deadline": (now + timedelta(days=21)).isoformat(),
     "description": """Hosted eDiscovery platform supporting processing, technology assisted review, redaction and
disclosure production for civil litigation. Must support UK data residency, meet the Cyber Essentials Plus
requirement, and integrate with existing case management. Call-off contracts over four years."""},

    {"external_id": "lt-003", "title": "AI-assisted due diligence review for M&A transactions",
     "agency": "Global bank (private RFP)", "buyer_name": "Northbank Group", "country": "USA", "buyer_type": "enterprise",
     "currency": "USD", "estimated_value": 450000, "cpv_codes": [],
     "deadline": (now + timedelta(days=18)).isoformat(),
     "description": """We are running a competitive process for an AI document review tool for transaction due
diligence: contract review, clause extraction, risk flagging, and data-room integration. Deployment must be
SOC 2 Type II. Two-year term with option to extend. Please respond with technical approach and pricing."""},

    {"external_id": "lt-004", "title": "Records management and FOI request processing system",
     "agency": "City of Toronto", "buyer_name": "City of Toronto", "country": "CAN",
     "currency": "CAD", "estimated_value": 380000, "cpv_codes": [],
     "deadline": (now + timedelta(days=35)).isoformat(),
     "description": """Replacement of the corporate records management system, including retention schedules,
information governance workflows, redaction of personal information, and freedom of information request
tracking. Canadian data residency required. Vendor must be registered to do business in Ontario."""},

    {"external_id": "lt-005", "title": "Judicial case management and e-filing modernisation",
     "agency": "Supreme Court of Singapore", "buyer_name": "Supreme Court of Singapore", "country": "SGP",
     "currency": "SGD", "estimated_value": 2100000, "cpv_codes": [],
     "deadline": (now + timedelta(days=45)).isoformat(),
     "description": """Modernisation of court case management including electronic filing, docketing, hearing
scheduling and judgment publication. Requires on-site presence for user acceptance testing, integration with
national digital identity, and a local entity registered in Singapore."""},

    {"external_id": "lt-006", "title": "Legal spend management and e-billing platform",
     "agency": "Federal agency", "buyer_name": "US federal agency", "country": "USA", "set_aside": "Total Small Business",
     "currency": "USD", "estimated_value": 275000, "cpv_codes": [],
     "deadline": (now + timedelta(days=14)).isoformat(),
     "description": """Matter management and legal spend platform for the Office of General Counsel: outside counsel
budgets, e-billing with UTBMS codes, matter intake and reporting. FedRAMP Moderate authorisation required.
Total Small Business set-aside."""},

    # --- deliberate noise: each should be auto-rejected for a different, visible reason
    {"external_id": "lt-101", "title": "Supply and installation of office furniture for the courthouse",
     "agency": "Courts Service", "buyer_name": "Courts Service", "country": "IRL", "currency": "EUR",
     "estimated_value": 300000, "description": "Desks, chairs and filing cabinets, delivered and installed on site."},

    {"external_id": "lt-102", "title": "Panel of law firms for external legal advice",
     "agency": "Department of Finance", "buyer_name": "Department of Finance", "country": "GBR", "currency": "GBP",
     "estimated_value": 4000000,
     "description": "Establishment of a panel of law firms to provide legal advice, legal representation, legal "
                    "opinions and advocacy services across commercial and employment law."},

    {"external_id": "lt-103", "title": "Contract review tool pilot",
     "agency": "Small municipality", "buyer_name": "Rural District Council", "country": "GBR", "currency": "GBP",
     "estimated_value": 9000, "description": "Three month pilot of a contract review tool for a two-person legal team."},

    {"external_id": "lt-104", "title": "Contract lifecycle management platform",
     "agency": "State enterprise", "buyer_name": "State Energy Company", "country": "RUS", "currency": "USD",
     "estimated_value": 800000, "description": "CLM platform with clause library and contract repository."},
]


def _payload(seed: dict) -> dict:
    payload = {"opportunity_type": "bid", "buyer_type": seed.pop("buyer_type", "government"), "budget_type": "unknown"}
    payload.update(seed)
    return payload


def seed(process: bool = True, purge: bool = False) -> None:
    init_db()
    with session_scope() as db:
        if purge:
            removed = 0
            for opp in db.query(Opportunity).filter(Opportunity.source == "manual").all():
                if opp.external_id.startswith(PREFIX):
                    db.delete(opp)
                    removed += 1
            db.flush()
            print(f"purged {removed} legal-tech seed opportunities")
            return
        set_setting(db, "profile_mode", "vendor")
        set_setting(db, "legal_tech_only", "true")
        set_setting(db, "minimum_deal_value", 25000)
        set_setting(db, "target_regions", "worldwide")
        set_setting(db, "excluded_regions", "RUS")
        db.commit()
        print("profile_mode=vendor, legal_tech_only=true, excluded_regions=RUS")
        created = []
        for raw in SEEDS:
            opp, was_new = upsert_opportunity(db, normalize_manual(_payload(dict(raw))))
            if was_new:
                created.append(opp.id)
        db.commit()
        print(f"seeded {len(created)} legal-tech tenders")
        if process:
            for opp_id in created:
                result = process_opportunity(db, opp_id)
                db.commit()
                opp = db.get(Opportunity, opp_id)
                score = f"{opp.analysis.opportunity_score:.0f}" if opp.analysis else "-"
                value = f"${opp.value_usd:,.0f}" if opp.value_usd else "-"
                reason = (opp.rejection_reasons or [""])[0][:52]
                print(f"  [{result:11}] {score:>3} {value:>12} {opp.country or '---'} "
                      f"{(opp.legal_tech_category or '-'):20} {opp.title[:44]:44} {reason}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-process", action="store_true")
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    seed(process=not args.no_process, purge=args.purge)
