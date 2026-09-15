"""Owner-editable settings stored in the database (thresholds, weights, owner profile)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Setting


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: Any
    value_type: str
    description: str
    group: str = "thresholds"


DEFAULT_OWNER_PROFILE = """Independent technical consultant focused on automation and integration.
Verified skills: n8n, Workato, REST API integrations, Python scripting, data processing, Excel/spreadsheet
automation, CRM cleanup, Salesforce configuration & data work, document processing/classification, OCR
workflows, AI agents and AI workflow automation, reporting & dashboards, data migration, Squarespace and
simple websites, Docker, Linux, server automation, system integration, security questionnaire assistance,
SOC 2 implementation support, Google Workspace and Microsoft 365 automation, PDF/document extraction,
database transformations.
Certifications: none verified. Insurance: none verified. Do NOT claim any certification, license,
insurance, named customers, or specific past results."""

SETTING_SPECS: list[SettingSpec] = [
    SettingSpec("minimum_project_value", 500, "float", "Reject if the best-case budget is below this (USD)."),
    SettingSpec("minimum_opportunity_score", 75, "float", "Minimum OPPORTUNITY_SCORE (0-100) to recommend."),
    SettingSpec("minimum_ai_completable_percentage", 70, "float", "Minimum % of the work AI can perform."),
    SettingSpec("maximum_human_hours", 10, "float", "Reject if estimated human hours exceed this."),
    SettingSpec("minimum_expected_profit", 400, "float", "Reject if expected profit (USD) is below this."),
    SettingSpec("maximum_risk_score", 60, "float", "Reject if the risk score (0-100) exceeds this."),
    SettingSpec("minimum_scope_clarity", 40, "float", "Reject if scope clarity (0-100) is below this."),
    SettingSpec("minimum_technical_fit", 50, "float", "Reject if technical fit (0-100) is below this."),
    SettingSpec("minimum_confidence", 30, "float", "Reject if the agent's confidence (0-100) is below this."),
    SettingSpec("minimum_hourly_budget", 60, "float", "For hourly jobs: reject below this USD/hour."),
    SettingSpec("target_effective_hourly_rate", 250, "float",
                "Target expected profit per human hour; used when pricing and scoring.", "pricing"),
    SettingSpec("api_cost_per_agent_hour", 6.0, "float", "Rough Claude cost (USD) per agent hour of work.",
                "pricing"),
    SettingSpec("notify_min_score", 80, "float", "Send a NEW MONEY OPPORTUNITY notification at/above this score.",
                "notifications"),
    SettingSpec("owner_profile", DEFAULT_OWNER_PROFILE, "text",
                "The ONLY facts the proposal agent may claim about you. Keep it verified and honest.", "owner"),
    SettingSpec("preferred_work", "n8n, Workato, API integrations, REST APIs, Python scripts, data processing, "
                "Excel automation, spreadsheet transformation, CRM cleanup, Salesforce, document processing, "
                "document classification, OCR, AI agents, AI workflow automation, reporting, dashboards, data "
                "migration, Squarespace, simple websites, Docker, Linux, server automation, system integration, "
                "security questionnaires, SOC 2 support, Google Workspace automation, Microsoft automation, "
                "document extraction, PDF processing, database transformations", "text",
                "Comma-separated preferred work types (used by the qualification agent).", "owner"),
    SettingSpec("avoid_work", "onsite labor, physical installation, large teams, licensed professional services, "
                "legal advice, medical advice, major custom application builds, ongoing manual work, large unknown "
                "expenses, deceptive activity, fake reviews, spam, scraping that violates platform terms", "text",
                "Comma-separated work types to avoid.", "owner"),
]



def _market_specs() -> list[SettingSpec]:
    """Market Challenge settings; defaults come from .env (MARKET_*) so nothing is hard-coded twice."""
    env = get_settings()
    return [
        SettingSpec("market_scan_interval_minutes", env.market_scan_interval_minutes, "int",
                    "Minutes between scheduled market scans (research + proposals only; never trades).", "market"),
        SettingSpec("market_max_position_pct", env.market_max_position_pct, "float",
                    "Max % of portfolio value in a single ticker after a proposed BUY.", "market"),
        SettingSpec("market_max_single_trade_pct", env.market_max_single_trade_pct, "float",
                    "Max % of portfolio value a single proposed BUY may deploy.", "market"),
        SettingSpec("market_min_cash_reserve_pct", env.market_min_cash_reserve_pct, "float",
                    "Minimum % of portfolio value kept as cash after a proposed BUY.", "market"),
        SettingSpec("market_model", env.market_model or env.claude_model, "str",
                    "Claude model for MarketResearchAgent / PortfolioAgent.", "market"),
        SettingSpec("market_effort", env.market_effort or env.claude_effort, "str",
                    "Effort level for market agents: low | medium | high | xhigh | max.", "market"),
    ]


SETTING_SPECS.extend(_market_specs())
MARKET_SETTING_KEYS = [s.key for s in SETTING_SPECS if s.group == "market"]
SPEC_BY_KEY = {s.key: s for s in SETTING_SPECS}


def _coerce(raw: str, value_type: str) -> Any:
    if value_type == "int":
        return int(float(raw))
    if value_type == "float":
        return float(raw)
    if value_type == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return raw


def seed_default_settings(db: Session) -> None:
    existing = {s.key for s in db.query(Setting).all()}
    for spec in SETTING_SPECS:
        if spec.key not in existing:
            db.add(Setting(key=spec.key, value=str(spec.default), value_type=spec.value_type,
                           description=spec.description, group=spec.group))
    db.flush()


def get_setting(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    spec = SPEC_BY_KEY.get(key)
    if row is None:
        if spec is None:
            raise KeyError(key)
        return spec.default
    return _coerce(row.value, row.value_type)


def get_all_settings(db: Session) -> dict[str, Any]:
    return {s.key: _coerce(s.value, s.value_type) for s in db.query(Setting).all()}


def set_setting(db: Session, key: str, value: Any) -> Setting:
    row = db.get(Setting, key)
    if row is None:
        spec = SPEC_BY_KEY.get(key)
        row = Setting(key=key, value_type=spec.value_type if spec else "str",
                      description=spec.description if spec else "", group=spec.group if spec else "general")
        db.add(row)
    # validate type
    _coerce(str(value), row.value_type)
    row.value = str(value)
    db.flush()
    return row


class Thresholds:
    """Convenience snapshot of numeric thresholds."""

    def __init__(self, db: Session):
        values = get_all_settings(db)
        for spec in SETTING_SPECS:
            setattr(self, spec.key, values.get(spec.key, spec.default))
