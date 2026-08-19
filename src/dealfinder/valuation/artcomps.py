"""Comp-weighted valuation for art, built on realised sale prices.

The failure this exists to fix: asked to value a Nino Pippa oil, the appraiser reasoned
from "working regional landscape artists of this calibre" and returned a single number,
$1,200. The artist's realised results run $111-$401. A point estimate produced by
category judgement has no error bars, no audit trail, and no way to be wrong out loud —
it just quietly sets a maximum bid twice the top of an artist's market.

So the model's job here is narrowed to what it is actually good at: *finding and reading
comparable sales*. The arithmetic is done here, in code you can inspect, from records you
can list. The output is a distribution — a weighted median with a low/high band — because
an artist whose work realises $111 to $401 does not have "a value", and pretending
otherwise is how the $1,200 happened.

Weighting, in descending order of how much it should matter:

* **Realised, not asked.** A hammer price is a transaction; a gallery or artist-direct
  ask is marketing. For living decorative artists the two differ several-fold, and the
  ask is the number that will lose you money. Asks are admitted at a heavy discount only
  so a sparse artist still yields *something*, and never dominate a single realised sale.
* **Medium.** An original oil and a giclée reproduction of that same oil are different
  objects at different prices, and their titles can be identical.
* **Size.** Value scales with area but sub-linearly — a canvas of twice the area is not
  worth twice as much — so proximity in area is what earns weight.
* **Recency.** Markets drift. A 2015 result is evidence about 2015.

Nothing here is art-specific in its mechanics; it is separated out because art is where
the point-estimate failure was measured, and because medium and size are the axes that
matter for pictures. A rug or a watch would want different axes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

#: Below this total weight there is not enough evidence to call it a valuation. Returning
#: nothing is a result: it routes the lot back to the market anchor, which at least knows
#: what the room is paying, rather than dressing up one stray comp as an estimate.
MIN_TOTAL_WEIGHT = 1.2

#: How much an asking price counts against a realised sale. Deliberately punitive.
ASK_WEIGHT = 0.15

#: Years after which a result carries roughly half its original weight.
RECENCY_HALFLIFE_YEARS = 6.0


@dataclass(frozen=True)
class SoldComp:
    """One comparable, ideally a realised sale."""

    price_cents: int
    title: str = ""
    medium: str = ""
    #: Longest and shortest dimension in inches, either order; 0 when unknown.
    width_in: float = 0.0
    height_in: float = 0.0
    year_sold: int = 0
    source: str = ""
    venue: str = ""
    url: str = ""
    #: False means someone was *asking* this. See ASK_WEIGHT.
    is_sold: bool = True

    @property
    def area_sq_in(self) -> float:
        return self.width_in * self.height_in


@dataclass(frozen=True)
class ValueRange:
    """A weighted distribution, not a point. ``mid`` is the figure to price off."""

    low_cents: int
    mid_cents: int
    high_cents: int
    n_comps: int
    n_sold: int
    total_weight: float
    basis: str

    @property
    def spread_ratio(self) -> float:
        """high/low. A wide band is a real finding — say it rather than average it away."""
        return (self.high_cents / self.low_cents) if self.low_cents else 0.0


def _medium_family(medium: str) -> str:
    """Collapse a free-text medium onto the distinction that moves the price.

    The one that matters is original-vs-reproduction: a textured giclée on canvas is
    built to be mistaken for the oil it copies, and sells for a fraction of it.
    """
    m = (medium or "").lower()
    if any(w in m for w in ("giclee", "giclée", "print", "reproduction", "litho",
                            "serigraph", "offset", "poster")):
        return "reproduction"
    if any(w in m for w in ("oil", "acrylic", "gouache", "tempera")):
        return "painting"
    if any(w in m for w in ("watercolor", "watercolour", "pastel", "charcoal", "ink",
                            "pencil", "drawing")):
        return "works-on-paper"
    if any(w in m for w in ("bronze", "marble", "sculpture", "carved")):
        return "sculpture"
    return ""


def _medium_weight(comp_medium: str, target_medium: str) -> float:
    a, b = _medium_family(comp_medium), _medium_family(target_medium)
    if not a or not b:
        return 0.6           # unknown on either side: usable, not trusted
    if a == b:
        return 1.0
    # An original and a reproduction of it are the confusable pair, and conflating them
    # is precisely the error that inflates a valuation. Nearly worthless as a comp.
    if {a, b} == {"painting", "reproduction"}:
        return 0.1
    return 0.3


def _size_weight(comp: SoldComp, target_area: float) -> float:
    """Closeness in area, on a log scale so 2x and 0.5x are penalised equally."""
    if target_area <= 0 or comp.area_sq_in <= 0:
        return 0.7           # unknown size: usable, mildly discounted
    ratio = comp.area_sq_in / target_area
    return float(math.exp(-abs(math.log(ratio)) / 1.2))


def _recency_weight(year_sold: int, this_year: int) -> float:
    if not year_sold or year_sold > this_year:
        return 0.7
    age = max(0, this_year - year_sold)
    return float(0.5 ** (age / RECENCY_HALFLIFE_YEARS))


def comp_weight(comp: SoldComp, *, target: SoldComp, this_year: int) -> float:
    """How much this record should count. Zero means don't count it at all."""
    if comp.price_cents <= 0:
        return 0.0
    w = _medium_weight(comp.medium, target.medium)
    w *= _size_weight(comp, target.area_sq_in)
    w *= _recency_weight(comp.year_sold, this_year)
    if not comp.is_sold:
        w *= ASK_WEIGHT
    return w


def _weighted_quantile(pairs: list[tuple[int, float]], q: float) -> int:
    """``pairs`` is (price, weight), sorted by price. Interpolation-free on purpose."""
    total = sum(w for _p, w in pairs)
    if total <= 0:
        return 0
    cutoff = total * q
    run = 0.0
    for price, w in pairs:
        run += w
        if run >= cutoff:
            return price
    return pairs[-1][0]


def estimate(
    comps: list[SoldComp],
    *,
    target: SoldComp,
    this_year: int,
    min_total_weight: float = MIN_TOTAL_WEIGHT,
) -> ValueRange | None:
    """A weighted median and band over ``comps``, or None when the evidence is too thin.

    Returning None is the honest outcome for an artist with two stray results, and the
    caller is expected to fall back rather than treat a lone comp as a valuation.
    """
    scored = [
        (c, comp_weight(c, target=target, this_year=this_year))
        for c in comps
    ]
    scored = [(c, w) for c, w in scored if w > 0]
    if not scored:
        return None

    total = sum(w for _c, w in scored)
    n_sold = sum(1 for c, _w in scored if c.is_sold)
    if total < min_total_weight or n_sold == 0:
        return None

    pairs = sorted(((c.price_cents, w) for c, w in scored), key=lambda t: t[0])
    mid = _weighted_quantile(pairs, 0.5)
    low = _weighted_quantile(pairs, 0.25)
    high = _weighted_quantile(pairs, 0.75)

    # A single dominant comp gives low == mid == high, which would read as certainty we
    # do not have. Widen to the observed spread so the band never claims more precision
    # than the records behind it.
    if low == high and len(pairs) > 1:
        low, high = pairs[0][0], pairs[-1][0]

    # An ask is not evidence that anything changed hands at that price, so it must never
    # drag the estimate above what has actually been realised. Tuning ASK_WEIGHT down
    # until that stopped happening would be fitting a magic number to one artist; this is
    # the structural version of the same rule. Measured on the Pippa comps, a lone $2,400
    # artist-direct ask was moving the median $80 against five realised sales.
    realised_max = max((c.price_cents for c, _w in scored if c.is_sold), default=0)
    if realised_max:
        mid = min(mid, realised_max)
        high = min(high, realised_max)
        low = min(low, mid)

    prices = [p for p, _w in pairs]
    basis = (
        f"{n_sold} realised sale(s) of {len(pairs)} comparable(s), "
        f"raw range ${min(prices) / 100:,.0f}-${max(prices) / 100:,.0f}, "
        f"unweighted median ${median(prices) / 100:,.0f}"
    )
    return ValueRange(
        low_cents=low, mid_cents=mid, high_cents=high,
        n_comps=len(pairs), n_sold=n_sold, total_weight=round(total, 2), basis=basis,
    )
