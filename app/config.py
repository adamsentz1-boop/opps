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

    # PDF ingestion via the existing local Stirling PDF service (never a third-party OCR service)
    pdf_ingestion_enabled: bool = True
    pdf_service_url: str = "http://host.docker.internal:8080"
    pdf_service_type: str = "stirling"
    pdf_service_api_key: str = ""          # only if Stirling login/security is enabled (X-API-KEY)
    pdf_request_timeout_seconds: int = 120
    pdf_ocr_languages: str = "eng"
    pdf_max_file_mb: int = 40
    pdf_min_chars_per_page: int = 40       # below this the PDF is treated as scanned and sent to OCR
    pdf_max_text_chars: int = 60000        # cap on extracted text passed to agents per attachment
    pdf_download_allowed_hosts: str = "sam.gov,api.sam.gov,beta.sam.gov"  # auto-download only from these hosts

    # TED - EU Tenders Electronic Daily (public, keyless Search API v3)
    ted_enabled: bool = False
    ted_cpv_codes: str = ""        # blank -> taxonomy defaults
    ted_keywords: str = ""         # blank -> taxonomy defaults
    ted_countries: str = ""        # ISO-3 buyer countries, blank = all
    ted_days_back: int = 7
    ted_max_results: int = 100

    # UK Find a Tender Service (public, keyless OCDS API)
    fts_enabled: bool = False
    fts_days_back: int = 7
    fts_max_results: int = 100

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
    # Generic outbound webhook - point it at an n8n Webhook node to fan alerts out to phone/email/Slack.
    webhook_url: str = ""
    webhook_token: str = ""

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
    def ted_cpv_list(self) -> list[str]:
        from app.taxonomy import DEFAULT_CPV_CODES
        codes = [c.strip() for c in self.ted_cpv_codes.split(",") if c.strip()]
        return codes or DEFAULT_CPV_CODES

    @property
    def ted_keyword_list(self) -> list[str]:
        from app.taxonomy import DEFAULT_KEYWORD_QUERIES
        kws = [k.strip() for k in self.ted_keywords.split(",") if k.strip()]
        return kws or DEFAULT_KEYWORD_QUERIES

    @property
    def ted_country_list(self) -> list[str]:
        return [c.strip().upper() for c in self.ted_countries.split(",") if c.strip()]

    @property
    def pdf_allowed_hosts(self) -> list[str]:
        return [h.strip().lower() for h in self.pdf_download_allowed_hosts.split(",") if h.strip()]

    @property
    def pdf_languages(self) -> list[str]:
        return [x.strip() for x in self.pdf_ocr_languages.replace("+", ",").split(",") if x.strip()] or ["eng"]

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
