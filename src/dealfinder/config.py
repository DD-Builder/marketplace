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

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError as exc:  # pragma: no cover — exercised only on a base install
    raise ImportError(
        "The metered-API path needs the optional extra: pip install 'dealfinder[api]'. "
        "The default subscription path (APPRAISER_PROVIDER=claude-code) never loads this."
    ) from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    appraise_model: str = Field(default="claude-opus-5", alias="APPRAISE_MODEL")
    negotiation_model: str = Field(default="claude-sonnet-5", alias="NEGOTIATION_MODEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
