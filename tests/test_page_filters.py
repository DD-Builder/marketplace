"""Filtering actually filters — driven in a real browser, not asserted against markup.

These exist because of a bug that every markup-level assertion would have passed. The
filter JS set ``card.hidden = true`` correctly, the chip lit up correctly, the state
object updated correctly — and not one card disappeared, because ``.lot{display:flex}``
outranks the user-agent's ``[hidden]{display:none}`` on specificity. The page looked
right, the DOM looked right, and the feature did nothing. Only rendering it and counting
what a viewer can actually see catches that class of failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dealfinder.auctions import bidding
from dealfinder.auctions.board import AuctionBoardMeta, write_auction_page
from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry
from dealfinder.core.schemas import AppraisalResult

pytestmark = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("playwright") is None,
    reason="playwright not installed",
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _entry(lot_id: str, vertical: str, title: str, *, bid: int, value: int) -> AuctionEntry:
    e = AuctionEntry(
        id=lot_id, title=title, description=title, url=f"https://x/{lot_id}",
        vertical=vertical, state="ending", watch=True, current_bid_cents=bid,
        first_seen=NOW - timedelta(days=2), last_seen=NOW,
        ends_at=NOW + timedelta(hours=6),
    )
    e.appraisal = AppraisalResult(
        identified_item=title, maker_guess="", est_asis_value_cents=value,
        est_restored_resale_value_cents=value, est_restoration_cost_cents=0,
        est_restoration_effort_hours=0.0, confidence=0.8, deal_score=60.0,
    )
    return e


@pytest.fixture
def page_url(tmp_path):
    cat = AuctionCatalog()
    # A wide spread so a filter that does nothing is indistinguishable from no filter.
    for i in range(4):
        cat.lots[f"j{i}"] = _entry(f"j{i}", "jewelry", f"14K Gold Diamond Ring {i}",
                                   bid=1000, value=90000)
    for i in range(3):
        cat.lots[f"f{i}"] = _entry(f"f{i}", "furniture", f"Danish Teak Sideboard {i}",
                                   bid=900000, value=100)
    cat.lots["c0"] = _entry("c0", "collectibles", "Sterling Silver Cup", bid=1000, value=90000)

    guidance = {}
    for e in cat.lots.values():
        g = bidding.guide(e, multiplier=1.3, now=NOW)
        if g:
            guidance[e.id] = g
    write_auction_page(cat, guidance, tmp_path, meta=AuctionBoardMeta(), now=NOW)
    return "file://" + str((tmp_path / "index.html").resolve())


@pytest.fixture
def page(page_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        except Exception:  # noqa: BLE001 — no browser binary on this machine
            pytest.skip("chromium unavailable")
        pg = browser.new_page()
        pg.goto(page_url)
        yield pg
        browser.close()


def _visible(pg) -> list[str]:
    return pg.evaluate(
        "[...document.querySelectorAll('article')]"
        ".filter(e => e.offsetParent !== null).map(e => e.dataset.vertical)"
    )


def test_a_category_chip_hides_every_other_category(page):
    assert len(_visible(page)) == 8

    page.click("button.chip[data-vertical='jewelry']")

    shown = _visible(page)
    assert set(shown) == {"jewelry"}, "a chip that leaves other categories on screen is not a filter"
    assert len(shown) == 4


def test_a_stance_tile_hides_every_other_stance(page):
    page.click("button.stat[data-stance='bid']")

    stances = page.evaluate(
        "[...document.querySelectorAll('article')]"
        ".filter(e => e.offsetParent !== null).map(e => e.dataset.stance)"
    )
    assert stances, "the fixture should contain at least one biddable lot"
    assert set(stances) == {"bid"}


def test_category_and_stance_compose(page):
    page.click("button.stat[data-stance='bid']")
    page.click("button.chip[data-vertical='jewelry']")

    rows = page.evaluate(
        "[...document.querySelectorAll('article')].filter(e => e.offsetParent !== null)"
        ".map(e => [e.dataset.vertical, e.dataset.stance])"
    )
    assert rows
    assert all(v == "jewelry" and s == "bid" for v, s in rows)


def test_reset_restores_every_card_and_clears_the_search(page):
    page.click("button.chip[data-vertical='furniture']")
    page.fill("#q", "teak")
    page.wait_for_timeout(50)
    assert len(_visible(page)) < 8

    page.click("#reset")

    assert len(_visible(page)) == 8
    assert page.input_value("#q") == ""
