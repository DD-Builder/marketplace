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
    # A free (or unpriced) item is not an infinite-return jackpot — it still has to be
    # worth the trip. Treat outlay as at least a dollar so the meaningful-margin scaler
    # below applies to it like everything else; the old `<= 0 -> 100.0` early return let
    # free-pile junk skip the exact guard this function exists to enforce.
    mult = restored_cents / max(cash_outlay_cents, 100)
    raw = 100.0 * min(mult, cap) / cap
    margin = restored_cents - max(cash_outlay_cents, 0)
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
    days_since_seen: float = 0.0,
) -> float:
    """Composite 0-100 the feed sorts by.

    Blends absolute margin (deal), return-on-cost (roi), resale ease (liquidity), and
    buy-side heat. Look-alikes and out-of-radius pieces are penalised — the second matters
    because a great piece 80 miles away isn't a great piece for you.

    Staleness is penalised hardest of all, because the best deal on the board is worthless
    if it sold on Tuesday. A piece we haven't confirmed in a week is far more likely gone
    than available, and it should not be occupying the top slot.
    """
    base = deal_score * 0.40 + roi_score * 0.20 + liquidity * 0.25 + heat * 0.15
    if authenticity.is_red_flag:
        base *= 0.6
    if out_of_radius:
        base *= 0.7  # a great piece 80 miles away is a poor piece for a radius-bound buyer
    base *= staleness_factor(days_since_seen)
    return round(max(0.0, min(100.0, base)), 1)


#: Board tiers, coarsest first. Keyed by the *restored* value rather than the asking
#: price on purpose: a $220 Brasilia credenza that restores to $1,500 is an estate find,
#: not a cheap flip, and bucketing on ask would file it with the nightstands.
TIERS: tuple[tuple[str, str, str], ...] = (
    ("estate", "Estate pieces", "The ones worth clearing a weekend for"),
    ("mid", "Mid-tier", "Solid pieces, real money, manageable work"),
    ("quick", "Quick flips", "Small margins, small effort — take them or leave them"),
)


def price_tier(
    restored_value_cents: int,
    *,
    estate_floor_cents: int = 70000,
    mid_floor_cents: int = 20000,
) -> str:
    """Which band a piece belongs in, by what it's worth restored.

    Exists because one ranked list is dominated by whatever has the best *ratio*, and a
    $10 nightstand worth $50 beats a $220 credenza worth $1,500 on every percentage
    measure while being worth a fraction as much money and just as much of a Saturday.
    """
    if restored_value_cents >= estate_floor_cents:
        return "estate"
    if restored_value_cents >= mid_floor_cents:
        return "mid"
    return "quick"


def staleness_factor(days_since_seen: float) -> float:
    """How much to trust that a piece is still for sale, by age of the last sighting.

    Full weight for a day, then a steady decay: a listing confirmed today is real, one from
    a week ago is a coin toss, and one from a fortnight ago is mostly a memory.
    """
    if days_since_seen <= 1:
        return 1.0
    if days_since_seen >= 14:
        return 0.25
    return round(1.0 - 0.75 * (days_since_seen - 1) / 13, 3)


def is_killer_deal(
    *,
    deal_score: float,
    confidence: float,
    authenticity: AuthenticityAssessment,
    net_margin_cents: int | None = None,
    asking_price_cents: int | None = None,
) -> bool:
    """The star. Two ways to earn it, both requiring a genuine, reasonably-confident piece:

    * a big *absolute* margin (deal_score >= 50 — with the $500-half-point curve that is
      roughly a $1,000 net margin at 0.75 confidence), or
    * a big *return multiple* on a cheap piece — e.g. a $20 armoire that restores to $250.
      A hobbyist rightly calls that a killer even though the absolute dollars are modest.

    The old gate (deal_score >= 70 @ confidence >= 0.65) was unreachable: the score is
    base x confidence with base < 100, so at the 0.65 floor the product topped out at 65.
    Every star on the board was coming from the cheap-flip branch.
    """
    if authenticity.is_red_flag or confidence < 0.5:
        return False
    if deal_score >= 50 and confidence >= 0.65:
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


#: Single source of truth for every chip the board can show. badges() draws from this and
#: the page's legend is generated from it, so the two can never drift apart again — the
#: shipped legend documented six chips while this module emitted nine.
BADGE_DEFS: dict[str, tuple[str, str, str]] = {
    "killer": ("★", "Killer deal", "good"),
    "lookalike": ("⚠", "Look-alike", "warn"),
    "drop": ("▼", "Price drop", "good"),
    "hot": ("◉", "Hot", "info"),
    "fast": ("≈", "Sells fast", "info"),
    "slow": ("∼", "Slow mover", "warn"),
    "hedged": ("?", "Maker unconfirmed", "info"),
    "oor": ("⤢", "Out of radius", "warn"),
    "stale": ("◷", "Unconfirmed lately", "warn"),
}


def _badge(key: str, label: str | None = None, tone: str | None = None) -> Badge:
    icon, deflabel, deftone = BADGE_DEFS[key]
    return Badge(icon, label or deflabel, tone or deftone)


def badges(
    *,
    killer: bool,
    heat: float,
    liquidity: float,
    price_dropped: bool,
    authenticity: AuthenticityAssessment,
    out_of_radius: bool = False,
    days_since_seen: float = 0.0,
) -> list[Badge]:
    """Glanceable status chips, ordered by how loudly they should read."""
    out: list[Badge] = []
    if killer:
        out.append(_badge("killer"))
    if authenticity.is_red_flag:
        out.append(_badge("lookalike"))
    if price_dropped:
        out.append(_badge("drop"))
    if heat >= 60:
        out.append(_badge("hot"))
    if liquidity >= 70:
        out.append(_badge("fast"))
    elif liquidity <= 25:
        out.append(_badge("slow"))
    if authenticity.verdict == "hedged":
        out.append(_badge("hedged"))
    if out_of_radius:
        out.append(_badge("oor"))
    if days_since_seen >= 7:
        out.append(_badge("stale", f"Unconfirmed {int(days_since_seen)}d"))
    elif days_since_seen >= 2:
        out.append(_badge("stale", f"Last seen {int(days_since_seen)}d ago", "info"))
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
