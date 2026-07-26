"""Free heuristic pre-screen — the funnel stage before any AI call.

Kills the obvious junk (flat-pack, particleboard, no photos, absurd prices) so the paid
triage/appraisal only ever sees plausible candidates. Pure string/number rules, zero cost.

The *what-counts-as-junk* knowledge lives in a :class:`~dealfinder.verticals.Vertical`, so
the same machinery screens furniture, art, electronics, or any niche you add — just pass a
different vertical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from dealfinder.core.schemas import RawListing
from dealfinder.verticals import DEFAULT_VERTICAL, Vertical


@dataclass
class PreScreenResult:
    keep: bool
    score: int
    reasons: list[str] = field(default_factory=list)


@lru_cache(maxsize=None)
def _term_re(term: str) -> re.Pattern:
    # Word-boundary match so "mold" doesn't fire on "crown molding" and "oak"
    # doesn't fire inside unrelated words. \b sits fine against spaces/hyphens,
    # so multi-word terms like "particle board" and "mid-century" still match.
    return re.compile(rf"\b{re.escape(term)}\b")


def _hits(text: str, terms: frozenset[str] | set[str]) -> list[str]:
    return sorted(t for t in terms if _term_re(t).search(text))


def prescreen(
    listing: RawListing,
    vertical: Vertical = DEFAULT_VERTICAL,
    *,
    require_photo: bool = True,
) -> PreScreenResult:
    """Score a listing against a vertical's junk/signal rules. Zero cost, pure heuristics."""
    text = f"{listing.title}\n{listing.description}".lower()
    reasons: list[str] = []
    score = 0

    neg = _hits(text, vertical.negative)
    if neg:
        return PreScreenResult(False, -10, [f"negative:{t}" for t in neg])

    if require_photo and not listing.photos:
        return PreScreenResult(False, 0, ["no photos — can't appraise by image"])

    price = listing.asking_price_cents
    if price is not None:
        if price < vertical.min_price_cents:
            # The vertical calls this scam/typo/free-pile territory; keeping it while
            # merely noting the reason let $1 junk through to paid appraisal.
            return PreScreenResult(False, 0, ["price implausibly low — scam/typo/free-pile range"])
        if price > vertical.max_price_cents:
            return PreScreenResult(False, 0, ["price above flip range"])

    pos = _hits(text, vertical.positive)
    makers = _hits(text, vertical.makers)
    score += len(pos) + 2 * len(makers)
    reasons += [f"+{t}" for t in pos] + [f"maker:{t}" for t in makers]

    # Keep anything with a positive signal, OR anything with a photo and a real price —
    # because the juiciest deals are *mistitled* pieces the seller described badly, and a
    # keyword filter alone would throw those away. Vision triage catches those downstream
    # (these zero-score keeps are the "wildcard" pool the selection stage samples from).
    keep = score >= 1 or (bool(listing.photos) and price is not None)
    if not reasons:
        reasons.append("no strong signal — kept for vision triage")
    return PreScreenResult(keep, score, reasons)
