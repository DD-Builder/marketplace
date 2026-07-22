"""Typed application settings loaded from the environment / .env file.

Search *targets* (what to scrape) are DB data managed via the dashboard, not config —
only cross-cutting knobs live here. See ``.env.example`` for documentation of each field.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Secrets
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    fb_session_path: str = Field(default="", alias="FB_SESSION_PATH")

    # Models
    triage_model: str = Field(default="claude-haiku-4-5", alias="TRIAGE_MODEL")
    appraise_model: str = Field(default="claude-opus-4-8", alias="APPRAISE_MODEL")
    negotiation_model: str = Field(default="claude-opus-4-8", alias="NEGOTIATION_MODEL")

    # Economics
    hourly_rate_cents: int = Field(default=3000, alias="HOURLY_RATE_CENTS")

    # Scraper / pacing
    rate_max_actions_per_hour: int = Field(default=60, alias="RATE_MAX_ACTIONS_PER_HOUR")
    pacing_min_seconds: float = Field(default=1.5, alias="PACING_MIN_SECONDS")
    pacing_max_seconds: float = Field(default=6.0, alias="PACING_MAX_SECONDS")
    headless: bool = Field(default=False, alias="HEADLESS")
    browser_channel: str = Field(default="chrome", alias="BROWSER_CHANNEL")
    max_search_pages: int = Field(default=10, alias="MAX_SEARCH_PAGES")
    quiet_hours_start: int | None = Field(default=None, alias="QUIET_HOURS_START")
    quiet_hours_end: int | None = Field(default=None, alias="QUIET_HOURS_END")

    # Infra
    database_url: str = Field(default="sqlite:///data/app.db", alias="DATABASE_URL")
    photo_dir: str = Field(default="data/photos", alias="PHOTO_DIR")
    session_dir: str = Field(default="data/sessions", alias="SESSION_DIR")

    @property
    def photo_path(self) -> Path:
        p = Path(self.photo_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def session_path(self) -> Path:
        p = Path(self.session_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Injected via FastAPI ``Depends`` and imported in the worker."""
    return Settings()
