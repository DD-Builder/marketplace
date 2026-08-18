"""Max-bid math and endgame dynamics — what a lot is worth *to you*, and when to act.

Auctions invert the Marketplace problem. There, the seller names a price and the
question is whether it's low enough. Here the price finds itself, mostly in the last
hours, and the discipline that makes money is deciding your ceiling *before* the
endgame starts and never chasing past it. Everything in this module exists to produce
one number — ``max_bid_cents`` — and the context that makes it credible.

Three parts:

* **The ceiling.** Work backwards from the appraisal: what the restored piece sells
  for, minus restoration cost, your hours at your rate, your minimum margin, shipping,
  and the buyer's premium the house adds on top of the hammer. What's left is the most
  the *hammer price* can be before the flip stops paying. No emotion in it.
* **The projection.** A lot at $40 with a day left is not a $40 lot. The endgame
  multiplier — learned from this catalogue's own ended lots, shrunk toward a prior
  while the sample is small — says what T-24h prices tend to become. Projection above
  your ceiling means the deal is already gone; you just can't see it yet.
* **The stance.** "bid" / "watch" / "outpriced", plus the standing advice that never
  changes: enter late. An early bid is a gift of information to the room and pressure
  on the price you'll pay; the recommendation is always a late bid at your number.

Pure functions, integer cents, no I/O — same contract as :mod:`dealfinder.resale`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median

from dealfinder.auctions.catalog import ENDGAME_HOURS, AuctionEntry
from dealfinder.auctions.logistics import acquisition_cost
from dealfinder.core.schemas import AppraisalResult

#: What T-24h prices tend to become by close, before this catalogue has seen enough of
#: its own ended lots to know better. Deliberately aggressive-side: overestimating the
#: endgame makes the tracker warn early, and a false "already outpriced" costs a
#: bargain while a false "still winnable" costs real money.
DEFAULT_ENDGAME_MULTIPLIER = 2.0

#: How many observed lots it takes for the learned multiplier to dominate the prior.
_CALIBRATION_WEIGHT = 6

#: EBTH adds a buyer's premium on top of the hammer price. The exact percentage is
#: policy on their side and changes; override with EBTH_PREMIUM_PCT once confirmed.
DEFAULT_PREMIUM_PCT = 0.15

_DEFAULT_MARGIN_PCT = 0.25   # minimum margin the flip must clear at your max bid


def endgame_multiplier(
    pairs: list[tuple[int, int]],
    *,
    prior: float = DEFAULT_ENDGAME_MULTIPLIER,
    weight: int = _CALIBRATION_WEIGHT,
) -> float:
    """Learned final/T-24h ratio, shrunk toward the prior while the sample is thin.

    The median ratio (robust to the one lot that went 40x) is blended with the prior by
    sample size: one observed auction barely moves it, a season of them owns it. The
    scanner's last look can trail the true hammer, so observed ratios run *low* — the
    learned number is a floor, and the prior it starts from is set high on purpose.
    """
    ratios = [final / t24 for t24, final in pairs if t24 > 0]
    if not ratios:
        return prior
    observed = median(ratios)
    n = len(ratios)
    blended = (prior * weight + observed * n) / (weight + n)
    return max(1.0, blended)


def projected_final_cents(
    entry: AuctionEntry,
    *,
    multiplier: float,
    now: datetime | None = None,
) -> int | None:
    """Where this lot is likely to close, given where the bidding is on the clock.

    The multiplier describes the whole endgame, so it applies in full with 24h left and
    fades linearly to nothing at the close — at which point the current bid *is* the
    price. Further than 24h out the endgame hasn't started, so the full multiplier
    applies (and the projection is soft, which the caller says out loud on the board).
    """
    if entry.current_bid_cents is None:
        return None
    now = now or datetime.now(timezone.utc)
    left = entry.hours_left(now)
    if left is None:
        return round(entry.current_bid_cents * multiplier)
    if left <= 0:
        return entry.current_bid_cents
    endgame_fraction = min(1.0, left / ENDGAME_HOURS)
    return round(entry.current_bid_cents * (1.0 + (multiplier - 1.0) * endgame_fraction))


def bid_velocity_cents_per_hour(
    entry: AuctionEntry, *, window_hours: float = 6.0, now: datetime | None = None
) -> float | None:
    """How fast the price is moving over the recent window. None until two points."""
    now = now or datetime.now(timezone.utc)
    pts = [
        p for p in entry.bid_history
        if p.bid_cents is not None
        and (now - p.at).total_seconds() / 3600 <= window_hours
    ]
    if len(pts) < 2:
        return None
    dt_h = (pts[-1].at - pts[0].at).total_seconds() / 3600
    if dt_h <= 0:
        return None
    return (pts[-1].bid_cents - pts[0].bid_cents) / dt_h


def resale_value_cents(appraisal: AppraisalResult) -> int:
    """What the lot is worth resold **as it arrives** — no restoration assumed.

    This is the ``est_asis_value`` line, deliberately *not* ``est_restored_resale_value``.
    The Marketplace side of this project buys projects and fixes them; the auction side
    buys finished pieces to resell as-found, so pricing off the restored figure would
    credit the bid with value that only exists after work nobody is going to do — the
    single easiest way to talk yourself into overpaying at an auction.

    Falls back to the restored figure only when an appraisal carries no as-is number at
    all (older stored appraisals), since a zero there would read as "worthless" rather
    than "unstated".
    """
    asis = max(0, appraisal.est_asis_value_cents)
    return asis or max(0, appraisal.est_restored_resale_value_cents)


def max_bid_cents(
    appraisal: AppraisalResult,
    *,
    hourly_rate_cents: int = 3000,
    premium_pct: float = DEFAULT_PREMIUM_PCT,
    shipping_cents: int = 0,
    margin_pct: float = _DEFAULT_MARGIN_PCT,
) -> int:
    """The most the hammer can reach before the flip stops paying.

    Derivation, all in cents::

        proceeds   = as-is resale value        # sold as it arrives; nothing restored
        keep       = proceeds * (1 - margin)   # your minimum margin comes off the top
        all_in_cap = keep - acquisition cost   # shipping, or the round trip to collect
        max_hammer = all_in_cap / (1 + premium)  # the house's cut rides the hammer

    Restoration cost and bench hours are deliberately absent: these lots are bought to
    resell as-found, so charging the bid for work that will not happen would understate
    the ceiling as badly as pricing off the restored value would overstate it.

    Confidence scales the margin, not the value: a shaky appraisal demands more cushion,
    which shrinks the ceiling instead of pretending the estimate is better than it is.
    Never negative — a lot that can't pay returns 0, which reads as "don't bid at all".
    """
    value = resale_value_cents(appraisal)
    # Below 0.55 confidence the cushion grows: at 0.3 confidence a 25% margin becomes 40%.
    cushion = margin_pct + max(0.0, (0.55 - appraisal.confidence)) * 0.6
    keep = value * (1.0 - min(0.9, cushion))
    all_in_cap = keep - shipping_cents
    if all_in_cap <= 0:
        return 0
    return int(all_in_cap / (1.0 + max(0.0, premium_pct)))


@dataclass(frozen=True)
class BidGuidance:
    max_bid_cents: int                 # your ceiling on the hammer price
    all_in_at_max_cents: int           # what winning at the ceiling actually costs you
    projected_final_cents: int | None  # where the bidding likely lands
    multiplier: float                  # the endgame multiplier used (learned or prior)
    calibration_n: int                 # how many observed endings back that multiplier
    stance: str                        # bid | watch | outpriced | no-value
    reason: str
    velocity_cents_per_hour: float | None = None
    headroom_cents: int | None = None  # max bid minus current bid; negative = gone
    notes: list[str] = field(default_factory=list)
    #: The as-is resale estimate the ceiling was derived from — the "what it's worth"
    #: half of the worth-vs-bid comparison the board leads with.
    value_cents: int = 0
    #: Cost of taking possession, and whether that's a parcel or a drive.
    logistics_cents: int = 0
    logistics_label: str = ""
    logistics_detail: str = ""
    #: Estimated profit if you win at the current bid and resell at the estimate. None
    #: when nobody has bid yet, since there is no "current" to reason about.
    margin_at_current_cents: int | None = None


def guide(
    entry: AuctionEntry,
    *,
    multiplier: float,
    calibration_n: int = 0,
    hourly_rate_cents: int = 3000,
    premium_pct: float = DEFAULT_PREMIUM_PCT,
    shipping_cents: int | None = None,
    margin_pct: float = _DEFAULT_MARGIN_PCT,
    now: datetime | None = None,
) -> BidGuidance | None:
    """Turn an appraised, tracked lot into a stance and a number. None if unappraised.

    ``shipping_cents`` defaults to the lot's own logistics — a flat parcel rate for a
    shippable category, a real round-trip drive for a bulky one — rather than a single
    site-wide guess. Pass an explicit value to override.
    """
    if entry.appraisal is None:
        return None
    now = now or datetime.now(timezone.utc)

    logistics = acquisition_cost(entry.vertical, hourly_rate_cents=hourly_rate_cents)
    if shipping_cents is None:
        shipping_cents = logistics.cost_cents

    ceiling = max_bid_cents(
        entry.appraisal, hourly_rate_cents=hourly_rate_cents,
        premium_pct=premium_pct, shipping_cents=shipping_cents, margin_pct=margin_pct,
    )
    # What winning at the ceiling actually costs: hammer + the house's cut + getting it
    # home. No restoration line — these are bought to resell as they arrive.
    all_in = round(ceiling * (1 + premium_pct)) + shipping_cents
    value = resale_value_cents(entry.appraisal)
    projected = projected_final_cents(entry, multiplier=multiplier, now=now)
    velocity = bid_velocity_cents_per_hour(entry, now=now)
    current = entry.current_bid_cents
    left = entry.hours_left(now)
    headroom = (ceiling - current) if current is not None else None

    notes: list[str] = []
    if left is not None and left > ENDGAME_HOURS:
        notes.append(
            "Never bid early — it only feeds the price. Hold until the final hours."
        )
    if calibration_n < 3:
        notes.append(
            f"Endgame multiplier is mostly prior ({multiplier:.1f}x) — "
            f"only {calibration_n} observed ending(s) so far. It sharpens as lots close."
        )

    margin_at_current = (
        value - round(current * (1 + premium_pct)) - shipping_cents
        if current is not None else None
    )

    if ceiling <= 0:
        stance, reason = "no-value", (
            f"The economics never work: the buyer's premium and "
            f"{logistics.label.lower()} eat the whole ${value / 100:,.0f} resale value."
        )
    elif current is not None and current >= ceiling:
        stance, reason = "outpriced", (
            f"Bidding already passed your ceiling — walk away at "
            f"${ceiling / 100:,.0f} and let it go."
        )
    elif projected is not None and projected > ceiling:
        stance, reason = "outpriced", (
            f"Current bid still clears, but the projection (${projected / 100:,.0f}) "
            "says the endgame takes it past your ceiling. Watch, don't chase."
        )
    elif left is not None and left <= ENDGAME_HOURS:
        stance, reason = "bid", (
            f"Inside the final day with headroom. Set a late bid at your ceiling "
            f"(${ceiling / 100:,.0f}) and do not chase past it."
        )
    else:
        stance, reason = "watch", (
            "Early innings — the current bid means little yet. Track it into the "
            "final day before acting."
        )

    return BidGuidance(
        max_bid_cents=ceiling,
        all_in_at_max_cents=all_in,
        projected_final_cents=projected,
        multiplier=round(multiplier, 2),
        calibration_n=calibration_n,
        stance=stance,
        reason=reason,
        velocity_cents_per_hour=velocity,
        headroom_cents=headroom,
        notes=notes,
        value_cents=value,
        logistics_cents=logistics.cost_cents,
        logistics_label=logistics.label,
        logistics_detail=logistics.detail,
        margin_at_current_cents=margin_at_current,
    )
