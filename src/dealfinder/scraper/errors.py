"""Scraper exception types."""

from __future__ import annotations


class ScraperError(Exception):
    """Base class for scraper failures."""


class BlockedError(ScraperError):
    """Facebook gated the request (login wall, checkpoint, or challenge).

    ``needs_auth`` means a logged-out request hit a wall and could be retried with the
    burner session. ``checkpoint`` means an authenticated identity itself got challenged
    and should be put on a long cooldown — never auto-solved.
    """

    def __init__(
        self, message: str, *, needs_auth: bool = False, checkpoint: bool = False
    ) -> None:
        super().__init__(message)
        self.needs_auth = needs_auth
        self.checkpoint = checkpoint


class LayoutChangedError(ScraperError):
    """Required fields were absent — Facebook likely changed its DOM/JSON shape.

    Carries a snapshot so the parser can be repaired offline against real markup.
    """

    def __init__(self, message: str, *, snapshot: str | None = None) -> None:
        super().__init__(message)
        self.snapshot = snapshot
