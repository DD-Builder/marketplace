"""The engine: one call runs the whole funnel and hands back a ranked, priced board.

``run_valuation`` composes every piece built so far —

    records -> cost-control plan (dedup / seen-diff / cap)
            -> appraise the survivors (via the configured provider)
            -> deterministic deal score
            -> authenticity check
            -> provisional resale suggestion (if you bought at ask and restored it)
            -> liquidity / heat / priority / badges
            -> sorted board + a full audit of what it cost to get here

It is provider-agnostic (pass any ``ValuationProvider``) and source-agnostic (pass Apify
records or already-built listings), so it is exercised end-to-end in tests with a stub
provider on synthetic data — no network, no AI spend.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field

from dealfinder.appraiser import ValuationProvider
from dealfinder.authenticity import AuthenticityAssessment, assess_authenticity
from dealfinder.core.schemas import AppraisalResult, RawListing
from dealfinder.logging import get_logger
from dealfinder.ranking import (
    Badge,
    badges,
    heat_score,
    is_killer_deal,
    liquidity_score,
    roi_to_score,
    viewing_priority,
)
from dealfinder.resale import PieceCosts, ResaleSuggestion, suggest_resale_price
from dealfinder.selection import AppraisalPlan, plan_appraisals
from dealfinder.sources.apify import records_to_listings
from dealfinder.valuation.scoring import compute_deal_score
from dealfinder.verticals import DEFAULT_VERTICAL, Vertical

log = get_logger(__name__)


@dataclass
class EvaluatedPiece:
    listing: RawListing
    appraisal: AppraisalResult
    authenticity: AuthenticityAssessment
    deal_score: float
    cash_margin_cents: int
    resale: ResaleSuggestion
    liquidity: float
    heat: float
    priority: float
    is_killer: bool
    price_dropped: bool
    out_of_radius: bool
    badges: list[Badge] = field(default_factory=list)


@dataclass
class RunResult:
    pieces: list[EvaluatedPiece]      # sorted by viewing priority, best first
    plan: AppraisalPlan               # cost-control audit (what was scraped/skipped/appraised)

    @property
    def killers(self) -> list[EvaluatedPiece]:
        return [p for p in self.pieces if p.is_killer]


def _price_dropped(listing: RawListing) -> bool:
    was = listing.raw_json.get("_was_price_cents")
    cur = listing.asking_price_cents
    return bool(was) and cur is not None and cur < was


def run_valuation(
    source: Iterable[dict] | Iterable[RawListing],
    seen: Mapping[str, int | None] | None = None,
    *,
    provider: ValuationProvider,
    vertical: Vertical = DEFAULT_VERTICAL,
    hourly_rate_cents: int = 3000,
    top_n: int = 20,
    wildcards: int = 5,
    in_radius: Callable[[str], bool] | None = None,
    image_paths_by_id: Mapping[str, list] | None = None,
    backfill: Iterable[RawListing] = (),
    already_valued: Collection[str] = (),
) -> RunResult:
    """Run the funnel over a batch and return a ranked, priced board.

    ``source`` may be raw Apify records or ready ``RawListing`` objects. ``seen`` is the
    cross-run ledger (``{id: last_price_cents}``) so already-evaluated pieces are skipped.
    ``in_radius(location_text) -> bool`` flags distance; omit to treat everything as in-range.
    ``image_paths_by_id`` supplies already-downloaded photo files per listing — required by
    the subscription (Claude Code) appraiser, which reads images off disk.
    """
    items = list(source)
    listings = (
        records_to_listings(items) if items and isinstance(items[0], dict) else list(items)
    )

    plan = plan_appraisals(
        listings, seen or {}, vertical=vertical, top_n=top_n, wildcards=wildcards,
        backfill=backfill, already_valued=already_valued,
    )

    pieces: list[EvaluatedPiece] = []
    for listing in plan.to_appraise:
        try:
            imgs = (image_paths_by_id or {}).get(listing.fb_listing_id)
            appr = provider.appraise(listing, vertical, image_paths=imgs)
        except Exception as exc:  # noqa: BLE001 — one bad item shouldn't sink the run
            log.warning("appraisal_failed", listing=listing.fb_listing_id, error=str(exc))
            continue
        pieces.append(
            evaluate_piece(
                listing, appr, hourly_rate_cents=hourly_rate_cents, in_radius=in_radius
            )
        )

    pieces.sort(key=lambda p: p.priority, reverse=True)
    return RunResult(pieces=pieces, plan=plan)


def evaluate_piece(
    listing: RawListing,
    appraisal: AppraisalResult,
    *,
    hourly_rate_cents: int = 3000,
    in_radius: Callable[[str], bool] | None = None,
) -> EvaluatedPiece:
    """Score one listing against an appraisal — no AI, no I/O, pure computation.

    Separating this from :func:`run_valuation` is what makes a stored appraisal reusable:
    an appraisal answers "what is this object and what is it worth restored", which does
    not change when the seller cuts the price. So a price drop can be re-ranked against
    today's asking price for zero cost, and improvements to scoring or resale logic apply
    retroactively to every piece already in the catalogue.
    """
    auth = assess_authenticity(listing)
    ask = listing.asking_price_cents or 0
    deal = compute_deal_score(appraisal, listing.asking_price_cents, hourly_rate_cents)
    cash_margin = (
        appraisal.est_restored_resale_value_cents - ask - appraisal.est_restoration_cost_cents
    )
    dropped = _price_dropped(listing)
    oor = bool(in_radius) and not in_radius(listing.location_text)

    # Provisional resale target: if you bought at ask and restored per the estimate.
    provisional_costs = PieceCosts(
        acquisition_cents=ask,
        materials_cents=appraisal.est_restoration_cost_cents,
        labor_hours=appraisal.est_restoration_effort_hours,
    )
    resale = suggest_resale_price(appraisal, provisional_costs, hourly_rate_cents)

    liq = liquidity_score(
        maker_guess=appraisal.maker_guess, confidence=appraisal.confidence,
        identified_item=appraisal.identified_item, authenticity=auth,
    )
    heat = heat_score(
        text=f"{listing.title} {listing.description}",
        prescreen_score=0, price_dropped=dropped,
    )
    roi = roi_to_score(
        appraisal.est_restored_resale_value_cents, ask + appraisal.est_restoration_cost_cents
    )
    killer = is_killer_deal(
        deal_score=deal, confidence=appraisal.confidence, authenticity=auth,
        net_margin_cents=cash_margin, asking_price_cents=listing.asking_price_cents,
    )
    prio = viewing_priority(
        deal_score=deal, liquidity=liq, heat=heat, authenticity=auth,
        roi_score=roi, out_of_radius=oor,
    )
    return EvaluatedPiece(
        listing=listing, appraisal=appraisal, authenticity=auth, deal_score=deal,
        cash_margin_cents=cash_margin, resale=resale, liquidity=liq, heat=heat,
        priority=prio, is_killer=killer, price_dropped=dropped, out_of_radius=oor,
        badges=badges(killer=killer, heat=heat, liquidity=liq, price_dropped=dropped,
                      authenticity=auth, out_of_radius=oor),
    )
