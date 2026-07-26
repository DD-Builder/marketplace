"""Typed settings for the metered-API valuation path.

Almost nothing is configured here any more. The pipeline runs in GitHub Actions, so its
knobs are plain environment variables read where they're used (see
:mod:`dealfinder.run_board`), and its state is JSON committed next to the site rather than
a database. What's left is the handful of values the ``claude-api`` provider needs when you
choose to pay per token instead of leaning on your subscription.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    appraise_model: str = Field(default="claude-opus-4-8", alias="APPRAISE_MODEL")
    negotiation_model: str = Field(default="claude-opus-4-8", alias="NEGOTIATION_MODEL")

    # Which valuation backend to use. 'claude-code' bills your Max subscription via the
    # Claude Code CLI; 'claude-api' uses the metered API; 'openai'/'gemini'/'grok' are seams.
    appraiser_provider: str = Field(default="claude-code", alias="APPRAISER_PROVIDER")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
