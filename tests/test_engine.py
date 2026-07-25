"""End-to-end engine test with a stub provider — no network, no AI spend."""

from __future__ import annotations

from dealfinder.appraiser import available_providers, get_appraiser
from dealfinder.core.schemas import AppraisalResult, RawListing, RawPhoto
from dealfinder.engine import run_valuation


class StubProvider:
    """Returns a canned appraisal keyed off asking price, so the engine can be exercised."""

    name = "stub"

    def appraise(self, listing, vertical, *, image_paths=None):
        ask = listing.asking_price_cents or 0
        return AppraisalResult(
            identified_item="dresser",
            maker_guess="Lane" if "lane" in listing.title.lower() else None,
            est_asis_value_cents=ask,
            est_restored_resale_value_cents=max(ask * 5, 30000),
            est_restoration_cost_cents=4000,
            est_restoration_effort_hours=4.0,
            confidence=0.75,
            deal_score=60.0,
        )


def _l(id_, title="solid oak dresser", price=4000, photos=1, was=None):
    r = RawListing(
        fb_listing_id=id_, title=title, asking_price_cents=price,
        photos=[RawPhoto(remote_url="u")] * photos,
    )
    if was is not None:
        r.raw_json["_was_price_cents"] = was
    return r


def test_engine_runs_and_ranks():
    listings = [
        _l("a", title="solid oak dresser", price=3000),
        _l("b", title="Lane walnut credenza", price=6000),
        _l("junk", title="IKEA Malm particle board", price=4000),
    ]
    res = run_valuation(listings, seen={}, provider=StubProvider(), hourly_rate_cents=3000)
    # Junk dropped by pre-screen; two real pieces appraised and ranked.
    assert res.plan.dropped_by_prescreen == 1
    assert len(res.pieces) == 2
    assert res.pieces[0].priority >= res.pieces[1].priority
    # Each piece carries a resale suggestion and a deal score.
    for p in res.pieces:
        assert p.resale.headline_cents > 0
        assert 0 <= p.priority <= 100


def test_engine_skips_already_seen():
    listings = [_l("a", price=4000), _l("b", price=5000)]
    seen = {"a": 4000}  # 'a' seen at same price -> skip; 'b' is new
    res = run_valuation(listings, seen=seen, provider=StubProvider())
    ids = {p.listing.fb_listing_id for p in res.pieces}
    assert ids == {"b"}
    assert res.plan.skipped_seen == 1


def test_engine_flags_out_of_radius():
    listings = [_l("a", price=4000)]
    res = run_valuation(
        listings, seen={}, provider=StubProvider(),
        in_radius=lambda loc: False,  # everything out of range
    )
    assert res.pieces[0].out_of_radius


def test_engine_accepts_apify_records():
    records = [{
        "id": "z", "listingTitle": "solid walnut dresser",
        "listingPrice": {"amount": "40.00"}, "listingPhotos": [{"image": {"uri": "u"}}],
        "locationText": {"text": "Lexington, KY"},
    }]
    res = run_valuation(records, seen={}, provider=StubProvider())
    assert len(res.pieces) == 1
    assert res.pieces[0].listing.title == "solid walnut dresser"


def test_provider_factory():
    assert "claude-api" in available_providers()
    assert get_appraiser("claude-api").name == "claude-api"


def test_evaluate_piece_matches_run_valuation():
    """A stored appraisal must re-score identically to the live loop — this is what makes
    the catalogue trustworthy (scores are recomputed, never persisted)."""
    from dealfinder.engine import evaluate_piece
    listing = _l("p1", title="solid walnut credenza", price=6000)
    res = run_valuation([listing], seen={}, provider=StubProvider(), hourly_rate_cents=3000)
    live = res.pieces[0]
    stored = evaluate_piece(listing, live.appraisal, hourly_rate_cents=3000)
    for f in ("deal_score", "cash_margin_cents", "liquidity", "heat", "priority",
              "is_killer", "price_dropped", "out_of_radius"):
        assert getattr(stored, f) == getattr(live, f), f
    assert stored.resale.headline_cents == live.resale.headline_cents
    assert [b.label for b in stored.badges] == [b.label for b in live.badges]


def test_price_drop_rescores_without_any_ai_call():
    """The cost fix in one test: re-ranking a price drop must not invoke the provider."""
    from dealfinder.engine import evaluate_piece
    listing = _l("p2", title="solid oak dresser", price=8000)
    appraisal = StubProvider().appraise(listing, None)
    before = evaluate_piece(listing, appraisal)
    cheaper = listing.model_copy(update={"asking_price_cents": 3000})
    after = evaluate_piece(cheaper, appraisal)
    assert after.cash_margin_cents > before.cash_margin_cents
    assert after.priority >= before.priority


def test_thin_and_full_records_are_distinguishable():
    from dealfinder.sources.apify import record_to_listing
    thin = record_to_listing({"id": "a", "listingTitle": "Dresser",
                              "primaryListingPhoto": {"photo_image_url": "u"}})
    full = record_to_listing({"id": "a", "listingTitle": "Dresser",
                              "description": {"text": "solid oak"},
                              "listingPhotos": [{"image": {"uri": "u"}}]})
    assert not thin.detail_fetched and full.detail_fetched
    assert thin.photos  # primary photo fallback still gives prescreen something to keep


def test_the_board_headline_is_the_market_price_not_your_cost_basis():
    """AUDIT 4: logging six hours and $40 of materials must change only your numbers."""
    from dealfinder.engine import evaluate_piece
    from dealfinder.resale import PieceCosts

    listing = _l("q1", title="Lane walnut credenza", price=6000)
    appraisal = StubProvider().appraise(listing, None)

    bare = evaluate_piece(listing, appraisal, hourly_rate_cents=3000)
    logged = evaluate_piece(
        listing, appraisal, hourly_rate_cents=3000,
        logged_costs=PieceCosts(acquisition_cents=6000, materials_cents=4000, labor_hours=6.0),
    )
    assert bare.resale.headline_cents == logged.resale.headline_cents
    assert bare.priority == logged.priority          # ranking is market-driven too
    assert logged.resale.yours.cash_outlay_cents == 10000
    assert logged.resale.yours.costs.labor_hours == 6.0
