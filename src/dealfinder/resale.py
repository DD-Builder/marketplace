"""The resale half: what to list a finished piece at, and what you actually made on it.

Two jobs:

1. **Suggest a list price** from the appraisal, your real costs, and a posture. Known,
   identifiable pieces (a Lane Acclaim end table) get anchored to realistic market value
   plus your premium; hot-but-*ambiguous* pieces get priced *above* market to test the
   ceiling — you can always come down, and the right buyer sometimes pays up for a piece
   nobody else can pin down.

2. **Keep your real books.** You tell it acquisition price, materials spent, and hands-on
   hours; it tracks cost basis, profit, and — the number that actually matters — your
   *effective hourly wage* on the labor. That's how you learn which flips are worth your
   Saturday and which aren't.

Money is integer cents throughout. Pure functions — no I/O — so the pipeline and dashboard
layer persistence on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from dealfinder.core.schemas import AppraisalResult

# Tunables (overridable per call). Chosen conservatively; calibrate to your market.
_DEFAULT_PREMIUM_PCT = 0.10        # your restoration/brand premium over raw market
_CEILING_MARKUP_PCT = 0.35         # how far above market to price a hot-but-ambiguous piece
_FLOOR_MARGIN_PCT = 0.15           # minimum margin over fully-loaded cost (your walk-away)
#: A ceiling test extrapolates *upward from the market anchor*, so it is only defensible
#: when the anchor is worth extrapolating from. "Ambiguous" has to mean "I know what this
#: is, I just can't pin the maker" — not "I can't tell what I'm looking at". Below this
#: line the estimate is a guess, and marking a guess up by a third does not test a ceiling,
#: it compounds the error straight into the price the operator asks a real buyer for.
#: Every text-only appraisal this system has ever made lands under it (capped at 0.35 by
#: construction in appraiser.py), so the old rule aimed its most aggressive posture at
#: precisely the pieces it understood least.
_CEILING_TEST_MIN_CONFIDENCE = 0.45


class Posture(str, Enum):
    MARKET = "market"                # standard: price at market + premium
    KNOWN_PREMIUM = "known_premium"  # identifiable/known item: confident market + premium
    CEILING_TEST = "ceiling_test"    # desirable but ambiguous: price above market to test demand


@dataclass(frozen=True)
class PieceCosts:
    """Your real, entered costs for one piece."""

    acquisition_cents: int             # what you paid the seller
    materials_cents: int = 0           # stain, hardware, fabric, etc.
    labor_hours: float = 0.0           # your hands-on restoration time


def _money(cents: int) -> str:
    """Whole dollars, for prose. Cents in a rationale sentence read as false precision."""
    return f"${cents / 100:,.0f}"


def cash_outlay_cents(costs: PieceCosts) -> int:
    """Actual money out of pocket — not counting the value of your time."""
    return costs.acquisition_cents + costs.materials_cents


def loaded_cost_cents(costs: PieceCosts, hourly_rate_cents: int) -> int:
    """Fully-loaded cost: money out *plus* your labor valued at your hourly rate."""
    return cash_outlay_cents(costs) + round(costs.labor_hours * hourly_rate_cents)


def _classify(appraisal: AppraisalResult) -> Posture:
    """Pick a pricing posture from how confidently the piece was identified."""
    known = appraisal.confidence >= 0.70 and bool(appraisal.maker_guess)
    if known or appraisal.confidence >= 0.85:
        return Posture.KNOWN_PREMIUM
    # Ambiguous — but only worth a ceiling-test if it actually looks desirable (restored
    # value comfortably above as-is) *and* the anchor we would mark up is worth trusting.
    # Uncertainty must widen the range you'd accept, never raise the number you ask.
    if (
        _CEILING_TEST_MIN_CONFIDENCE <= appraisal.confidence < 0.55
        and appraisal.est_restored_resale_value_cents
        > appraisal.est_asis_value_cents * 1.5
    ):
        return Posture.CEILING_TEST
    return Posture.MARKET


@dataclass(frozen=True)
class ResaleSuggestion:
    list_price_cents: int        # what to list it at
    floor_price_cents: int       # walk-away — never accept below this
    market_anchor_cents: int     # the appraiser's realistic restored-resale estimate
    posture: Posture
    rationale: str
    #: An optional higher *opening* ask for a desirable piece whose maker can't be pinned
    #: down, offered alongside the recommendation rather than replacing it. Ambiguity is a
    #: reason to widen the range you're willing to work through — start high, come down —
    #: not a reason to raise the number you actually recommend. Keeping the two separate
    #: is what stops "the model doesn't know what this is" from reading, on the card, as
    #: "ask a third more for it". None when no ceiling test is warranted.
    stretch_price_cents: int | None = None
    viable: bool = True          # False when the piece can't clear its own costs
    warning: str = ""            # why it isn't viable, when it isn't
    # 'ok'          — clears cash costs and pays your hourly rate
    # 'thin'        — makes cash, but the hours don't pay your rate; fine for a hobbyist,
    #                 a poor use of a working day. Not a skip.
    # 'underwater'  — loses money out of pocket. A real skip.
    status: str = "ok"


def suggest_resale_price(
    appraisal: AppraisalResult,
    costs: PieceCosts,
    hourly_rate_cents: int,
    *,
    premium_pct: float = _DEFAULT_PREMIUM_PCT,
    ceiling_markup_pct: float = _CEILING_MARKUP_PCT,
    floor_margin_pct: float = _FLOOR_MARGIN_PCT,
) -> ResaleSuggestion:
    """Suggest a list price and a walk-away floor for a finished piece."""
    market = max(0, appraisal.est_restored_resale_value_cents)
    loaded = loaded_cost_cents(costs, hourly_rate_cents)
    floor = round(loaded * (1 + floor_margin_pct))

    posture = _classify(appraisal)
    premium = round(market * premium_pct)

    stretch: int | None = None
    if posture is Posture.CEILING_TEST:
        base = market + premium
        stretch = round(market * (1 + ceiling_markup_pct)) + premium
        why = (
            f"Desirable but the maker isn't pinned down (confidence "
            f"{appraisal.confidence:.0%}); worth opening at {_money(stretch)} to test the "
            f"ceiling and coming down to {_money(base)} — that's the range, not a markup."
        )
    elif posture is Posture.KNOWN_PREMIUM:
        base = market + premium
        why = (
            f"Identifiable ({appraisal.maker_guess or 'known type'}, "
            f"confidence {appraisal.confidence:.0%}); anchor to market plus your premium."
        )
    elif appraisal.confidence < _CEILING_TEST_MIN_CONFIDENCE:
        base = market + premium
        why = (
            f"Low confidence ({appraisal.confidence:.0%}) — the market estimate itself is "
            "soft, so price at it rather than above it, and treat the figure as a "
            "starting point to revise once you have the piece in hand."
        )
    else:
        base = market + premium
        why = "Standard piece; price at market plus your premium."

    # Price to the market, never to your costs. The floor (money out + your time + margin)
    # is what you *need*, not what a buyer will pay: listing a $450 table at $978 because
    # the hours were expensive just means it never sells. So the floor stays advisory —
    # reported for the walk-away decision, and surfaced as the "thin" warning below when
    # the market can't reach it.
    list_price = base

    # Two very different failure modes, previously collapsed into one "underwater" label:
    #
    #   * cash-negative — the piece sells for less than you paid plus materials. A real
    #     skip at any hourly rate.
    #   * labour-thin — it makes cash, but the estimated hours don't pay your rate. Worth
    #     surfacing, yet emphatically NOT a skip for someone who enjoys the bench: a $20
    #     armoire that resells at $200 is a good buy even if it takes a slow weekend.
    cash_out = cash_outlay_cents(costs)
    cash_margin = market - cash_out
    warning = ""
    if cash_margin <= 0:
        status = "underwater"
        warning = (
            f"Loses money: you'd have ${cash_out / 100:.0f} in it and it's only worth "
            f"about ${market / 100:.0f} restored."
        )
    elif floor > market + premium:
        status = "thin"
        labour = round(costs.labor_hours * hourly_rate_cents)
        warning = (
            f"Makes ${cash_margin / 100:.0f} cash, but {costs.labor_hours:.0f}h at "
            f"${hourly_rate_cents / 100:.0f}/hr is ${labour / 100:.0f} of your time. "
            "Fine if you enjoy the work; poor pay if you don't."
        )
    else:
        status = "ok"
    viable = status != "underwater"

    return ResaleSuggestion(
        list_price_cents=list_price,
        floor_price_cents=floor,
        market_anchor_cents=market,
        posture=posture,
        stretch_price_cents=stretch,
        rationale=why,
        viable=viable,
        warning=warning,
        status=status,
    )


@dataclass(frozen=True)
class RealizedOutcome:
    cash_profit_cents: int              # sale - money out of pocket (ignores your time)
    net_profit_cents: int               # sale - fully-loaded cost (values your time)
    effective_hourly_cents: int | None  # cash profit / hours — your $/hr on the labor
    return_on_cash_pct: float | None    # cash profit / money out, as a percent


def realized(
    sale_price_cents: int,
    costs: PieceCosts,
    hourly_rate_cents: int,
) -> RealizedOutcome:
    """Close the books on a sold piece: what you actually made, and your $/hr on it."""
    cash_out = cash_outlay_cents(costs)
    loaded = loaded_cost_cents(costs, hourly_rate_cents)
    cash_profit = sale_price_cents - cash_out
    net_profit = sale_price_cents - loaded

    eff_hourly = (
        round(cash_profit / costs.labor_hours) if costs.labor_hours > 0 else None
    )
    roc = (100.0 * cash_profit / cash_out) if cash_out > 0 else None

    return RealizedOutcome(
        cash_profit_cents=cash_profit,
        net_profit_cents=net_profit,
        effective_hourly_cents=eff_hourly,
        return_on_cash_pct=round(roc, 1) if roc is not None else None,
    )


# --- two-tier pricing --------------------------------------------------------------------

@dataclass(frozen=True)
class YourNumbers:
    """The same piece, priced against *your* books rather than the market's.

    Separated deliberately: what a restored walnut credenza fetches around here has nothing
    to do with how long you spent on it. Folding your hours into the asking price is how you
    end up listing a $450 table at $978 and never selling it.
    """

    costs: PieceCosts
    cash_outlay_cents: int          # money out of pocket
    loaded_cost_cents: int          # money out + your hours at your rate
    floor_price_cents: int          # walk-away: loaded cost + your minimum margin
    projected: RealizedOutcome      # what you'd make selling at the market list price
    status: str                     # ok | thin | underwater
    warning: str = ""
    logged: bool = False            # True = your real entries; False = estimated for you


@dataclass(frozen=True)
class ResalePlan:
    """Both answers at once: what it's worth, and what it's worth *to you*."""

    market: ResaleSuggestion
    yours: YourNumbers

    @property
    def headline_cents(self) -> int:
        """The number on the card. Always the market's, never your cost basis."""
        return self.market.list_price_cents

    @property
    def range_cents(self) -> tuple[int, int]:
        """A defensible ask-range: the plain estimate up to the highest ask worth trying.

        The top is the stretch when one is offered, not the recommendation. Those used to
        be the same number — the ceiling markup was folded straight into ``list_price``,
        so an ambiguous piece had its recommended ask inflated and the range simply
        reported the inflation back. Splitting them lets the range widen with uncertainty
        while the recommendation stays anchored, which is the whole point: you widen the
        band you'll work through, you don't raise the number you stand behind.
        """
        top = max(self.market.list_price_cents, self.market.stretch_price_cents or 0)
        low = min(self.market.market_anchor_cents, self.market.list_price_cents)
        return (low, top)


def price_piece(
    appraisal: AppraisalResult,
    *,
    asking_price_cents: int | None = None,
    logged_costs: PieceCosts | None = None,
    hourly_rate_cents: int = 3000,
    **kwargs,
) -> ResalePlan:
    """Price a piece two ways — as the market sees it, and as your books see it.

    Tier 1 is computed with *no* costs at all, so it is genuinely independent of you and
    cannot drift when you log a long weekend of sanding. Tier 2 uses ``logged_costs`` when
    you've entered them, and otherwise estimates from the appraisal (buy at ask, restore per
    the estimate) so a piece you haven't touched yet still shows honest economics.
    """
    market = suggest_resale_price(appraisal, PieceCosts(acquisition_cents=0), hourly_rate_cents,
                                  **kwargs)

    costs = logged_costs or PieceCosts(
        acquisition_cents=asking_price_cents or 0,
        materials_cents=appraisal.est_restoration_cost_cents,
        labor_hours=appraisal.est_restoration_effort_hours,
    )
    # Reuse the full cost-aware pass purely for its status/warning/floor verdict — the list
    # price it returns is the market's and is already carried by tier 1.
    verdict = suggest_resale_price(appraisal, costs, hourly_rate_cents, **kwargs)

    return ResalePlan(
        market=market,
        yours=YourNumbers(
            costs=costs,
            cash_outlay_cents=cash_outlay_cents(costs),
            loaded_cost_cents=loaded_cost_cents(costs, hourly_rate_cents),
            floor_price_cents=verdict.floor_price_cents,
            projected=realized(market.list_price_cents, costs, hourly_rate_cents),
            status=verdict.status,
            warning=verdict.warning,
            logged=logged_costs is not None,
        ),
    )
