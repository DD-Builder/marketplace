"""Enumerations shared across the domain."""

from __future__ import annotations

import enum


class ListingStatus(str, enum.Enum):
    NEW = "new"
    ACTIVE = "active"
    SOLD = "sold"
    REMOVED = "removed"
    HIDDEN = "hidden"


class ValuationTier(str, enum.Enum):
    TRIAGE = "triage"
    APPRAISE = "appraise"


class ScrapeRunStatus(str, enum.Enum):
    OK = "ok"
    BLOCKED = "blocked"
    LAYOUT_ERROR = "layout_error"
    ERROR = "error"


class MessageRole(str, enum.Enum):
    """Who authored a negotiation message."""

    SELLER = "seller"          # pasted in by the user, from the seller
    USER_SENT = "user_sent"    # a message the user actually sent
    AI_DRAFT = "ai_draft"      # a candidate reply the app generated
