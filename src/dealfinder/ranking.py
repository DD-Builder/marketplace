"""Viewing priority — how the feed decides what you look at first.

The deal score answers "how much money is on the table." But two pieces with the same
margin are not equal: one is a signed maker piece that sells itself, the other a no-name
that'll sit for months. So priority blends three axes:

* **deal** — net margin x confidence (the money), computed upstream in scoring.
* **liquidity** — how *easily* it resells (identifiable maker, hot category, genuine).
* **heat** — a buy-side momentum proxy (a fresh price drop, perennially-hot keywords).

Authenticity red flags apply a penalty — a look-alike deal is worth less than it looks.
The feed sorts by the composite; a killer deal earns a star. `heat` is a heuristic proxy
built from signals we already have, not live market data — real sell-through velocity wires
in later from eBay sold comps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dealfinder.authenticity import AuthenticityAssessment

# Category liquidity hints for furniture (extend per vertical as you learn your market).
_LIQUID_ITEMS = ("dresser", "table", "desk", "nightstand", "credenza", "sideboard", "chair", "stool")
_SLOW_ITEMS = ("armoire", "entertainment center", "hutch", "wardrobe", "curio", "organ")
_HOT_KEYWORDS = ("mid century", "mid-century", "mcm", "walnut", "teak", "danish", "brass", "burl", "oak")


def liquidity_score(
    *,
    maker_guess: str | None,
    confidence: float,
    identified_item: str,
    authenticity: AuthenticityAssessment,
) -> float:
    """0-100: how easily this resells once restored."""
    score = 40.0
    item = identified_item.lower()
    if maker_guess and not authenticity.is_red_flag:
        score += 25  # a named, genuine maker sells itself
    if confidence >= 0.70:
        score += 15  # a well-understood piece moves faster
    if any(k in item for k in _LIQUID_ITEMS):
        score += 10
    if any(k in item for k in _SLOW_ITEMS):
        score -= 25  # big niche casegoods are slow movers
    if authenticity.is_red_flag:
        score -= 25  # look-alikes are harder to place at a good price
    return round(max(0.0, min(100.0, score)), 1)


def heat_score(*, text: str, prescreen_score: int, price_dropped: bool) -> float:
    """0-100 buy-side momentum proxy — motivated seller + perennially-hot signals."""
    score = 25.0
    low = text.lower()
    if price_dropped:
        score += 30  # a fresh price cut is the strongest buy signal we can see
    if any(k in low for k in _HOT_KEYWORDS):
        score += 20
    if prescreen_score >= 3:
        score += 15
    return round(max(0.0, min(100.0, score)), 1)


def roi_to_score(
    restored_cents: int,
    cash_outlay_cents: int,
    *,
    cap: float = 6.0,
    meaningful_margin_cents: int = 15000,
) -> float:
    """0-100 from return multiple (restored / money-in).

    Lets a cheap 10x flip rank like the killer it is instead of losing to a bulky piece
    with more *absolute* dollars — but a near-free item would otherwise post an enormous
    multiple on a trivial profit and dominate the feed. (A real run had a $1 listing worth
    $50 ranking first.) The multiple is therefore scaled down until the actual margin is
    worth a trip.
    """
    if cash_outlay_cents <= 0:
        return 100.0
    mult = restored_cents / cash_outlay_cents
    raw = 100.0 * min(mult, cap) / cap
    margin = restored_cents - cash_outlay_cents
    if margin < meaningful_margin_cents:
        raw *= max(0.0, margin) / meaningful_margin_cents
    return round(raw, 1)


def viewing_priority(
    *,
    deal_score: float,
    liquidity: float,
    heat: float,
    authenticity: AuthenticityAssessment,
    roi_score: float = 0.0,
    out_of_radius: bool = False,
) -> float:
    """Composite 0-100 the feed sorts by.

    Blends absolute margin (deal), return-on-cost (roi), resale ease (liquidity), and
    buy-side heat. Look-alikes and out-of-radius pieces are penalised — the second matters
    because a great piece 80 miles away isn't a great piece for you.
    """
    base = deal_score * 0.40 + roi_score * 0.20 + liquidity * 0.25 + heat * 0.15
    if authenticity.is_red_flag:
        base *= 0.6
    if out_of_radius:
        base *= 0.7  # a great piece 80 miles away is a poor piece for a radius-bound buyer
    return round(max(0.0, min(100.0, base)), 1)


def is_killer_deal(
    *,
    deal_score: float,
    confidence: float,
    authenticity: AuthenticityAssessment,
    net_margin_cents: int | None = None,
    asking_price_cents: int | None = None,
) -> bool:
    """The star. Two ways to earn it, both requiring a genuine, reasonably-confident piece:

    * a big *absolute* margin (deal_score >= 70), or
    * a big *return multiple* on a cheap piece — e.g. a $20 armoire that restores to $250.
      A hobbyist rightly calls that a killer even though the absolute dollars are modest.
    """
    if authenticity.is_red_flag or confidence < 0.5:
        return False
    if deal_score >= 70 and confidence >= 0.65:
        return True
    if (
        net_margin_cents is not None
        and asking_price_cents
        and asking_price_cents <= 8000  # a cheap entry...
        and net_margin_cents >= 4 * asking_price_cents  # ...that returns 4x+ over cost
    ):
        return True
    return False


@dataclass
class Badge:
    icon: str
    label: str
    tone: str  # good | warn | info


def badges(
    *,
    killer: bool,
    heat: float,
    liquidity: float,
    price_dropped: bool,
    authenticity: AuthenticityAssessment,
    out_of_radius: bool = False,
) -> list[Badge]:
    """Glanceable status chips, ordered by how loudly they should read."""
    out: list[Badge] = []
    if killer:
        out.append(Badge("★", "Killer deal", "good"))
    if authenticity.is_red_flag:
        out.append(Badge("⚠", "Look-alike", "warn"))
    if price_dropped:
        out.append(Badge("▼", "Price drop", "good"))
    if heat >= 60:
        out.append(Badge("◉", "Hot", "info"))
    if liquidity >= 70:
        out.append(Badge("≈", "Sells fast", "info"))
    elif liquidity <= 25:
        out.append(Badge("∼", "Slow mover", "warn"))
    if authenticity.verdict == "hedged":
        out.append(Badge("?", "Maker unconfirmed", "info"))
    if out_of_radius:
        out.append(Badge("⤢", "Out of radius", "warn"))
    return out


@dataclass
class RankedPiece:
    listing_id: str
    priority: float
    deal_score: float
    liquidity: float
    heat: float
    is_killer: bool
    badges: list[Badge] = field(default_factory=list)
