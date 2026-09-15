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

    notify_adapters: str = "dashboard"
    ntfy_url: str = ""
    ntfy_topic: str = ""

    owner_name: str = ""
    owner_title: str = "Independent automation & integration consultant"
    owner_timezone: str = "America/New_York"

    # --- Market Challenge module (research + proposals only; never connects to a brokerage) ---
    market_challenge_enabled: bool = True
    market_data_provider: str = "yfinance"      # yfinance | mock
    market_mock: bool = False                   # force deterministic fake prices (tests / offline)
    market_allowed_asset_types: str = "stocks,etfs"
    market_starting_capital: float = 200.0
    market_target_value: float = 1000.0
    market_target_date: str = "2027-01-01"
    market_max_position_pct: float = 60.0
    market_max_single_trade_pct: float = 60.0
    market_min_cash_reserve_pct: float = 0.0
    market_scan_enabled: bool = True
    market_scan_interval_minutes: int = 60
    market_proposal_ttl_hours: int = 72
    market_model: str = ""                      # blank = CLAUDE_MODEL
    market_effort: str = ""                     # blank = CLAUDE_EFFORT

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
    def notification_adapters(self) -> list[str]:
        return [a.strip().lower() for a in self.notify_adapters.split(",") if a.strip()]

    @property
    def market_asset_types(self) -> list[str]:
        return [a.strip().lower() for a in self.market_allowed_asset_types.split(",") if a.strip()]

    @property
    def market_mock_enabled(self) -> bool:
        """Deterministic fake market data: explicit MARKET_MOCK or MARKET_DATA_PROVIDER=mock."""
        return self.market_mock or self.market_data_provider.strip().lower() == "mock"


@lru_cache
def get_settings() -> Settings:
    return Settings()
