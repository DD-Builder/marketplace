"""The combined landing page across every source."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry, save_auction_catalog
from dealfinder.catalog import Catalog, CatalogEntry, save_catalog
from dealfinder.core.schemas import AppraisalResult
from dealfinder.home import auction_picks, build, marketplace_picks

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _appraisal(asis=60000, restored=90000, cost=5000, conf=0.8):
    return AppraisalResult(
        identified_item="walnut credenza", maker_guess="Lane",
        est_asis_value_cents=asis, est_restored_resale_value_cents=restored,
        est_restoration_cost_cents=cost, est_restoration_effort_hours=3.0,
        confidence=conf, deal_score=60.0,
    )


def _market(n=1, ask=10000, **kw) -> Catalog:
    cat = Catalog()
    for i in range(n):
        cat.listings[f"m-{i}"] = CatalogEntry(
            id=f"m-{i}", title=f"Marketplace piece {i}", state="live",
            asking_price_cents=ask, first_seen=NOW, last_seen=NOW,
            appraisal=_appraisal(**kw),
        )
    return cat


def _auctions(n=1, bid=5000, ends_in_h=10.0, **kw) -> AuctionCatalog:
    cat = AuctionCatalog()
    for i in range(n):
        cat.lots[f"a-{i}"] = AuctionEntry(
            id=f"a-{i}", title=f"Auction lot {i}", watch=True, state="ending",
            vertical="jewelry", first_seen=NOW, last_seen=NOW,
            ends_at=NOW + timedelta(hours=ends_in_h),
            current_bid_cents=bid, appraisal=_appraisal(**kw),
        )
    return cat


def test_marketplace_picks_net_out_the_restoration_this_side_actually_does():
    picks = marketplace_picks(_market(ask=10000, asis=20000, restored=90000, cost=5000))
    assert len(picks) == 1
    # $900 restored - $100 ask - $50 of work.
    assert picks[0].margin_cents == 90000 - 10000 - 5000
    assert picks[0].source == "marketplace"


def test_marketplace_skips_pieces_with_no_margin_left():
    assert marketplace_picks(_market(ask=95000, restored=90000, cost=5000)) == []


def test_auction_picks_are_valued_as_is_and_net_of_getting_them_home():
    picks = auction_picks(_auctions(bid=5000, asis=60000), now=NOW)
    assert len(picks) == 1
    p = picks[0]
    assert p.source == "auction"
    assert p.value_cents == 60000                      # as-is, not the restored figure
    # $600 worth, minus a $50 bid + 15% premium, minus $35 shipping.
    assert p.margin_cents == 60000 - round(5000 * 1.15) - 3500


def test_auction_picks_only_surface_lots_actually_worth_bidding_on():
    """The home page is a shortlist, not a dump — a lot the tracker says to pass on has
    no business being the first thing you see."""
    priced_out = _auctions(bid=90000, asis=60000)
    assert auction_picks(priced_out, now=NOW) == []


def test_unappraised_and_unwatched_lots_never_reach_the_home_page():
    cat = _auctions()
    cat.lots["a-0"].appraisal = None
    assert auction_picks(cat, now=NOW) == []
    cat2 = _auctions()
    cat2.lots["a-0"].watch = False
    assert auction_picks(cat2, now=NOW) == []


def test_ranking_puts_the_best_value_multiple_first_across_both_sources(tmp_path):
    """A $40 lot worth $400 and a $400 piece worth $4,000 are equally good finds; what
    ranks them against each other is the multiple, not the raw dollars."""
    market = _market(ask=100000, asis=20000, restored=150000, cost=0)   # 1.5x
    auctions = _auctions(bid=2000, asis=60000)                          # ~26x
    save_catalog(market, tmp_path / "catalog.json")
    save_auction_catalog(auctions, tmp_path / "auctions" / "catalog.json")

    build(tmp_path, now=NOW)
    page = (tmp_path / "index.html").read_text()
    assert page.index("Auction lot 0") < page.index("Marketplace piece 0")


def test_the_page_is_built_from_both_catalogues_and_links_to_both_boards(tmp_path):
    save_catalog(_market(), tmp_path / "catalog.json")
    save_auction_catalog(_auctions(), tmp_path / "auctions" / "catalog.json")

    out = build(tmp_path, now=NOW)
    assert out.name == "index.html"
    page = out.read_text()
    assert "Marketplace piece 0" in page and "Auction lot 0" in page
    # Both tabs reachable, and the auction photo path is rewritten for the parent dir.
    assert 'href="board.html"' in page
    assert 'href="auctions/index.html"' in page


def test_a_missing_catalogue_yields_an_empty_page_rather_than_a_crash(tmp_path):
    out = build(tmp_path, now=NOW)
    assert out.exists()
    assert "Nothing surfaced yet" in out.read_text()


def test_a_corrupt_catalogue_says_so_instead_of_silently_showing_half_the_site(tmp_path):
    (tmp_path / "auctions").mkdir(parents=True)
    (tmp_path / "auctions" / "catalog.json").write_text('{"lots": "not-a-dict"}')
    save_catalog(_market(), tmp_path / "catalog.json")

    page = build(tmp_path, now=NOW).read_text()
    assert "auction catalogue could not be read" in page
    assert "Marketplace piece 0" in page, "the readable half must still render"


def test_auction_photo_paths_are_rewritten_relative_to_the_site_root(tmp_path):
    cat = _auctions()
    cat.lots["a-0"].photo_rel = "photos/a-0.jpg"
    save_auction_catalog(cat, tmp_path / "auctions" / "catalog.json")
    page = build(tmp_path, now=NOW).read_text()
    # The auction board writes photos/ relative to docs/auctions/; from docs/ it needs
    # the prefix or every thumbnail on the landing page 404s.
    assert 'src="auctions/photos/a-0.jpg"' in page
