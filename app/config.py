"""Application configuration loaded from environment / .env.

Secrets are never logged or exposed through the UI. Threshold settings that the
owner can tune live in the database (see settings_service), not here.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT_DIR = BASE_DIR / "app" / "prompts"
WORKSPACE_DIR = BASE_DIR / "workspace"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5"
    claude_effort: str = "medium"
    claude_max_tokens: int = 8000
    llm_mock: bool = False

    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'opportunity_engine.db'}"

    host: str = "0.0.0.0"
    port: int = 8000

    scheduler_enabled: bool = True
    scan_interval_minutes: int = 30

    rss_feed_urls: str = ""
    json_feed_urls: str = ""

    # SAM.gov (official public API; requires a free api.data.gov key)
    sam_gov_api_key: str = ""
    sam_gov_naics: str = ""            # comma-separated NAICS codes, e.g. 541511,541512,541519,518210
    sam_gov_keywords: str = ""         # comma-separated title keywords, each searched separately
    sam_gov_notice_types: str = "o,k,p"  # o=solicitation k=combined synopsis p=presolicitation r=sources sought
    sam_gov_days_back: int = 7
    sam_gov_max_results: int = 50

    notify_adapters: str = "dashboard"
    ntfy_url: str = ""
    ntfy_topic: str = ""

    owner_name: str = ""
    owner_title: str = "Independent automation & integration consultant"
    owner_timezone: str = "America/New_York"

    @property
    def llm_enabled(self) -> bool:
        """True when a real Claude API key is configured and mock mode is off."""
        return bool(self.anthropic_api_key) and not self.llm_mock

    @property
    def rss_feeds(self) -> list[str]:
        return [u.strip() for u in self.rss_feed_urls.split(",") if u.strip()]

    @property
    def json_feeds(self) -> list[str]:
        return [u.strip() for u in self.json_feed_urls.split(",") if u.strip()]

    @property
    def sam_gov_naics_list(self) -> list[str]:
        return [n.strip() for n in self.sam_gov_naics.split(",") if n.strip()]

    @property
    def sam_gov_keyword_list(self) -> list[str]:
        return [k.strip() for k in self.sam_gov_keywords.split(",") if k.strip()]

    @property
    def notification_adapters(self) -> list[str]:
        return [a.strip().lower() for a in self.notify_adapters.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
