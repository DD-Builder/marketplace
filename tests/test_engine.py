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
        assert p.resale.list_price_cents > 0
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
