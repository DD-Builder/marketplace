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
    # Ambiguous — but only worth a ceiling-test if it actually looks desirable
    # (restored value comfortably above as-is), else it's just an unknown, price at market.
    if appraisal.confidence < 0.55 and (
        appraisal.est_restored_resale_value_cents
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

    if posture is Posture.CEILING_TEST:
        base = round(market * (1 + ceiling_markup_pct)) + premium
        why = (
            f"Ambiguous but desirable (confidence {appraisal.confidence:.0%}); "
            f"list {ceiling_markup_pct:.0%} over market to test the ceiling — you can come down."
        )
    elif posture is Posture.KNOWN_PREMIUM:
        base = market + premium
        why = (
            f"Identifiable ({appraisal.maker_guess or 'known type'}, "
            f"confidence {appraisal.confidence:.0%}); anchor to market plus your premium."
        )
    else:
        base = market + premium
        why = "Standard piece; price at market plus your premium."

    # Never list below the walk-away floor (covers money out + your time + margin).
    list_price = max(base, floor)

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
        if list_price == floor and base < floor:
            why += " Raised to your cost-plus floor."
    viable = status != "underwater"

    return ResaleSuggestion(
        list_price_cents=list_price,
        floor_price_cents=floor,
        market_anchor_cents=market,
        posture=posture,
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
