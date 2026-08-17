"""Photo retention during a scraper blackout.

Retention is measured from ``last_seen``, and ``last_seen`` only advances when a scrape
actually returns rows. So when the scraper goes down, the calendar keeps moving while
every entry's timestamp stands still, and the 30-day window closes on all of them at
once — not because the pieces are stale, but because nobody is looking.

This was not hypothetical. Apify's monthly credit ran out on 2026-07-25 and froze
``last_seen`` on all 329 entries. ``prune()`` ran unconditionally on every subsequent
blind run, so every retained photo was due for deletion on 2026-08-24. Facebook's CDN
links expire within hours, so nothing deleted could ever have been re-fetched — and the
photos are what let an appraisal reach 0.82 confidence, where a text-only re-appraisal is
capped at 0.35. A silent outage would have destroyed the best evidence in the system, on
a date nobody had written down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dealfinder.catalog import Catalog, CatalogEntry, prune

FROZEN = datetime(2026, 7, 25, 17, 0, tzinfo=timezone.utc)   # the last successful scrape
DOOMSDAY = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)  # 30 days later


def _catalog(n: int = 3, *, last_seen: datetime = FROZEN) -> Catalog:
    cat = Catalog()
    for i in range(n):
        cat.listings[str(i)] = CatalogEntry(
            id=str(i), title=f"walnut credenza {i}",
            first_seen=last_seen, last_seen=last_seen,
            photo_rel=f"photos/{i}.jpg", extra_photo_rels=[f"photos/{i}_1.jpg"],
        )
    return cat


def test_a_blind_run_does_not_age_out_a_single_photo():
    """The regression, on its actual date. Thirty days of blackout must delete nothing."""
    cat = _catalog()
    rep = prune(cat, now=DOOMSDAY, photo_retention_days=30, scan_ok=False)

    assert rep.expired_photo_ids == []
    assert rep.photo_expiry_suspended
    for entry in cat.listings.values():
        assert entry.photo_rel, "irreplaceable evidence deleted during an outage"
        assert entry.extra_photo_rels


def test_a_run_that_reached_the_market_still_ages_photos_out():
    """The fix must not become 'photos are kept forever' — they are the only bulk here."""
    cat = _catalog()
    rep = prune(cat, now=DOOMSDAY, photo_retention_days=30, scan_ok=True)

    assert sorted(rep.expired_photo_ids) == ["0", "1", "2"]
    assert not rep.photo_expiry_suspended
    for entry in cat.listings.values():
        assert entry.photo_rel is None and entry.extra_photo_rels == []


def test_a_blackout_does_not_merely_delay_the_deletion():
    """Suspension has to hold for as long as the blindness does. A window that reopens on
    day 31 would destroy the same photos a day later and call it retention."""
    cat = _catalog()
    for day in (31, 60, 120, 400):
        rep = prune(cat, now=FROZEN + timedelta(days=day),
                    photo_retention_days=30, scan_ok=False)
        assert rep.expired_photo_ids == [], f"day {day}"
    assert all(e.photo_rel for e in cat.listings.values())


def test_photos_seen_recently_survive_a_normal_run():
    """The ordinary case stays a no-op, so a working scraper reports nothing to fix."""
    cat = _catalog(last_seen=DOOMSDAY - timedelta(days=3))
    rep = prune(cat, now=DOOMSDAY, photo_retention_days=30, scan_ok=True)
    assert rep.expired_photo_ids == []
    assert all(e.photo_rel for e in cat.listings.values())


def test_suspension_covers_photos_only_and_still_bounds_the_file():
    """A blind run must still drop entries that were already known to be gone, or an
    outage would also disable the only thing keeping catalog.json from growing forever."""
    cat = _catalog(1)
    cat.listings["old"] = CatalogEntry(
        id="old", title="sold long ago",
        first_seen=FROZEN - timedelta(days=400), last_seen=FROZEN - timedelta(days=300),
        state="gone",
    )
    rep = prune(cat, now=DOOMSDAY, photo_retention_days=30, scan_ok=False)

    assert "old" in rep.removed_ids and "old" not in cat.listings
    assert cat.listings["0"].photo_rel, "a live entry's photo must survive the sweep"


def test_the_run_suspends_expiry_exactly_when_the_scan_was_blocked(monkeypatch):
    """The wiring, not just the rule: run_board must pass its own scan state through.
    The bug was never in prune's arithmetic — it was that nobody told prune it was blind."""
    import inspect

    from dealfinder import run_board

    src = inspect.getsource(run_board.main)
    assert "scan_ok=not scan_failed" in src, "prune is still called unconditionally"
