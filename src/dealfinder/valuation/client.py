"""Thin wrapper around the Anthropic SDK client."""

from __future__ import annotations

from functools import lru_cache

import anthropic

from dealfinder.config import get_settings


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    settings = get_settings()
    # Falls back to ANTHROPIC_API_KEY in the environment if the setting is blank.
    if settings.anthropic_api_key:
        return anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return anthropic.Anthropic()
