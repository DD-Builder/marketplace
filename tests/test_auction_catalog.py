"""Auction catalogue: bid histories, the state machine, and calibration capture."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dealfinder.auctions.catalog import (
    AuctionCatalog,
    AuctionCatalogCorrupt,
    AuctionEntry,
    bid_near,
    calibration_pairs,
    load_auction_catalog,
    observe_auctions,
    prune_auctions,
    save_auction_catalog,
    snapshot_due,
)
from dealfinder.sources.ebth import AuctionItem

T0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _item(item_id="1-credenza", bid=1000, count=2, ends_in_h=72.0, **kw):
    return AuctionItem(
        item_id=item_id, title="Walnut Credenza", url=f"https://ebth.com/items/{item_id}",
        current_bid_cents=bid, bid_count=count,
        ends_at=T0 + timedelta(hours=ends_in_h), **kw,
    )


def test_observe_creates_then_snapshots():
    cat = AuctionCatalog()
    observe_auctions(cat, [_item(bid=1000, count=2)], now=T0)
    observe_auctions(cat, [_item(bid=1500, count=4)], now=T0 + timedelta(hours=1))
    entry = cat.lots["1-credenza"]
    assert entry.current_bid_cents == 1500
    assert [p.bid_cents for p in entry.bid_history] == [1000, 1500]


def test_unchanged_observations_refresh_rather_than_append():
    """A week of quiet must not crowd the history cap out of holding the endgame."""
    cat = AuctionCatalog()
    for h in range(10):
        observe_auctions(cat, [_item(bid=1000, count=2)], now=T0 + timedelta(hours=h))
    entry = cat.lots["1-credenza"]
    assert len(entry.bid_history) == 1
    assert entry.bid_history[0].at == T0 + timedelta(hours=9)   # liveness still advances


def test_the_clock_moves_a_lot_into_the_endgame():
    cat = AuctionCatalog()
    observe_auctions(cat, [_item(ends_in_h=30)], now=T0)
    assert cat.lots["1-credenza"].state == "live"
    rep = observe_auctions(cat, [_item(ends_in_h=30, bid=1200)],
                           now=T0 + timedelta(hours=10))
    assert cat.lots["1-credenza"].state == "ending"
    assert rep.entered_endgame == ["1-credenza"]


def test_ending_captures_final_price_and_the_t24_snapshot():
    """The calibration pair is the whole reason ended lots are kept."""
    cat = AuctionCatalog()
    ends = T0 + timedelta(hours=30)
    observe_auctions(cat, [_item(bid=1000, ends_in_h=30)], now=T0)                # T-30h
    observe_auctions(cat, [_item(bid=1400, ends_in_h=30)],
                     now=ends - timedelta(hours=25))                              # T-25h
    observe_auctions(cat, [_item(bid=2000, ends_in_h=30)],
                     now=ends - timedelta(hours=3))                               # T-3h
    rep = observe_auctions(cat, [_item(bid=9000, ends_in_h=30)],
                           now=ends + timedelta(minutes=10))                      # after
    entry = cat.lots["1-credenza"]
    assert entry.state == "ended"
    assert rep.finalized == ["1-credenza"]
    assert entry.final_price_cents == 9000
    # Nearest snapshot at-or-before T-24h is the T-25h one.
    assert entry.t24_bid_cents == 1400
    assert calibration_pairs(cat) == [(1400, 9000)]


def test_a_lot_discovered_inside_the_endgame_still_calibrates():
    """bid_near falls forward to the earliest snapshot when nothing precedes T-24h —
    a late discovery is a worse anchor, not a discarded one."""
    cat = AuctionCatalog()
    ends = T0 + timedelta(hours=5)
    observe_auctions(cat, [_item(bid=3000, ends_in_h=5)], now=T0)
    observe_auctions(cat, [_item(bid=7000, ends_in_h=5)], now=ends + timedelta(hours=1))
    assert cat.lots["1-credenza"].t24_bid_cents == 3000


def test_the_clock_alone_can_finalize_a_vanished_lot():
    """A lot we never see again after its end time must still close the books."""
    cat = AuctionCatalog()
    observe_auctions(cat, [_item(bid=2500, ends_in_h=2)], now=T0)
    rep = observe_auctions(cat, [], now=T0 + timedelta(hours=3))
    entry = cat.lots["1-credenza"]
    assert entry.state == "ended"
    assert entry.final_price_cents == 2500      # best information we have
    assert rep.finalized == ["1-credenza"]


def test_bid_near_picks_latest_at_or_before():
    from dealfinder.auctions.catalog import BidPoint

    entry = AuctionEntry(id="x", first_seen=T0, last_seen=T0)
    entry.bid_history = [
        BidPoint(at=T0, bid_cents=100),
        BidPoint(at=T0 + timedelta(hours=2), bid_cents=200),
        BidPoint(at=T0 + timedelta(hours=4), bid_cents=300),
    ]
    assert bid_near(entry, T0 + timedelta(hours=3)) == 200


def test_snapshot_due_always_includes_the_endgame():
    cat = AuctionCatalog()
    observe_auctions(cat, [
        _item("a-soon", ends_in_h=10),      # inside 24h
        _item("b-later", ends_in_h=100),    # quiet, seen just now
    ], now=T0)
    for e in cat.lots.values():
        e.watch = True
    due = {e.id for e in snapshot_due(cat, now=T0 + timedelta(hours=1))}
    assert due == {"a-soon"}
    # After six quiet hours the far lot earns a look too.
    due = {e.id for e in snapshot_due(cat, now=T0 + timedelta(hours=7))}
    assert due == {"a-soon", "b-later"}


def test_prune_keeps_ended_lots_but_drops_their_photos():
    cat = AuctionCatalog()
    observe_auctions(cat, [_item(bid=2000, ends_in_h=1)], now=T0)
    entry = cat.lots["1-credenza"]
    entry.watch = True
    entry.photo_rel = "photos/1-credenza.jpg"
    observe_auctions(cat, [], now=T0 + timedelta(hours=2))     # finalizes
    removed = prune_auctions(cat, now=T0 + timedelta(days=10))
    assert removed == []
    assert cat.lots["1-credenza"].photo_rel is None            # numbers stay, bulk goes
    assert cat.lots["1-credenza"].final_price_cents == 2000
    # Months later the entry itself ages out.
    removed = prune_auctions(cat, now=T0 + timedelta(days=200))
    assert removed == ["1-credenza"]


def test_prune_ages_out_unwatched_lots_quickly():
    cat = AuctionCatalog()
    observe_auctions(cat, [_item("junk-lot", ends_in_h=1000)], now=T0)
    assert prune_auctions(cat, now=T0 + timedelta(days=30)) == ["junk-lot"]


def test_roundtrip_and_corruption_quarantine(tmp_path: Path):
    cat = AuctionCatalog()
    observe_auctions(cat, [_item()], now=T0)
    path = tmp_path / "auctions.json"
    save_auction_catalog(cat, path)
    loaded = load_auction_catalog(path)
    assert loaded.lots["1-credenza"].current_bid_cents == 1000

    path.write_text('{"version": 1, "lots": "not-a-dict"}')
    with pytest.raises(AuctionCatalogCorrupt):
        load_auction_catalog(path)
    assert list(tmp_path.glob("auctions.json.corrupt-*")), "damaged file must be preserved"


def test_appraisal_is_reserved_for_lots_inside_the_decision_window():
    """Valuation is the only expensive step in a run. A lot closing next week will have
    its bid move many times before any decision is due, so paying to value it now buys
    nothing that paying in two days wouldn't."""
    from dealfinder.auctions.catalog import unappraised_watch

    cat = AuctionCatalog()
    for lot_id, hours in (("soon", 10), ("tomorrow", 30), ("next-week", 24 * 7)):
        observe_auctions(cat, [_item(lot_id, ends_in_h=hours)], now=T0)
        cat.lots[lot_id].watch = True

    inside = [e.id for e in unappraised_watch(cat, within_days=2, now=T0)]
    assert inside == ["soon", "tomorrow"]          # soonest first, next-week excluded
    # Without a window every watched lot is still fair game (the old behaviour).
    assert len(unappraised_watch(cat)) == 3


def test_a_lot_with_no_end_time_is_not_treated_as_imminent():
    """'Unknown' is not 'closing now' — guessing it in would spend real money on a lot
    that might not close for a month."""
    from dealfinder.auctions.catalog import unappraised_watch

    cat = AuctionCatalog()
    cat.lots["dateless"] = AuctionEntry(
        id="dateless", title="No end time", first_seen=T0, last_seen=T0,
        watch=True, state="live",
    )
    assert unappraised_watch(cat, within_days=2, now=T0) == []
    assert len(unappraised_watch(cat)) == 1
