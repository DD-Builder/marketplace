"""Free heuristic pre-screen — the funnel stage before any AI call.

Kills the obvious junk (flat-pack, particleboard, no photos, absurd prices) so the paid
triage/appraisal only ever sees plausible candidates. Pure string/number rules, zero cost.
Tunable — this is where you encode your own niche and your market's junk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from dealfinder.core.schemas import RawListing

# Signals that a piece is plausibly worth restoring/flipping.
POSITIVE = {
    "solid wood", "solid oak", "teak", "walnut", "mahogany", "rosewood", "oak",
    "maple", "cherry", "mid century", "mid-century", "mcm", "danish", "antique",
    "vintage", "dovetail", "brass", "marble", "cast iron", "wrought iron", "burl",
    "handmade", "hardwood", "art deco",
}
# Known makers punch above generic keywords.
MAKERS = {
    "eames", "herman miller", "knoll", "drexel", "henredon", "broyhill", "brasilia",
    "thomasville", "stickley", "ercol", "g plan", "g-plan", "lane", "dux", "baker",
    "kittinger", "widdicomb", "heywood", "bassett", "milo baughman", "kroehler",
}
# Signals of low-value fast furniture / non-restorable.
NEGATIVE = {
    "ikea", "particle board", "particleboard", "mdf", "laminate", "faux",
    "pressboard", "press board", "wayfair", "fast furniture", "flat pack",
    "flat-pack", "melamine", "veneer peeling", "water damaged beyond", "mold",
}


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


def _hits(text: str, terms: set[str]) -> list[str]:
    return sorted(t for t in terms if _term_re(t).search(text))


def prescreen(
    listing: RawListing,
    *,
    min_price_cents: int = 500,       # below this is usually a typo/scam/"free pile"
    max_price_cents: int = 300_000,   # above this it's not a flip, it's a purchase
    require_photo: bool = True,
) -> PreScreenResult:
    text = f"{listing.title}\n{listing.description}".lower()
    reasons: list[str] = []
    score = 0

    neg = _hits(text, NEGATIVE)
    if neg:
        return PreScreenResult(False, -10, [f"negative:{t}" for t in neg])

    if require_photo and not listing.photos:
        return PreScreenResult(False, 0, ["no photos — can't appraise by image"])

    price = listing.asking_price_cents
    if price is not None:
        if price < min_price_cents:
            reasons.append("price implausibly low")
        elif price > max_price_cents:
            return PreScreenResult(False, 0, ["price above flip range"])

    pos = _hits(text, POSITIVE)
    makers = _hits(text, MAKERS)
    score += len(pos) + 2 * len(makers)
    reasons += [f"+{t}" for t in pos] + [f"maker:{t}" for t in makers]

    # Keep anything with a positive signal, OR anything with a photo and a real price —
    # because the juiciest deals are *mistitled* pieces the seller described badly, and a
    # keyword filter alone would throw those away. Vision triage catches those downstream.
    keep = score >= 1 or (bool(listing.photos) and price is not None)
    if not reasons:
        reasons.append("no strong signal — kept for vision triage")
    return PreScreenResult(keep, score, reasons)
