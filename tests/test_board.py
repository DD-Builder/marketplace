"""The board page: it must render offline, and every button must be honest about failing.

These parse the generated HTML rather than eyeballing it, because the page is the product
— a card that renders but whose Save button silently does nothing is the worst outcome
this project can produce.
"""

from __future__ import annotations

import json
import re

from dealfinder.board import BoardMeta, render_board, write_site
from dealfinder.core.schemas import AppraisalResult, RawListing, RawPhoto
from dealfinder.engine import RunResult, evaluate_piece
from dealfinder.resale import PieceCosts
from dealfinder.selection import AppraisalPlan


def _piece(pid="abc", title="Lane walnut credenza", price=25000, **kw):
    listing = RawListing(
        fb_listing_id=pid, title=title, asking_price_cents=price,
        location_text="Lexington, KY", url=f"https://fb.com/{pid}",
        photos=[RawPhoto(remote_url="u")],
    )
    appraisal = AppraisalResult(
        identified_item="credenza", maker_guess="Lane", est_asis_value_cents=price,
        est_restored_resale_value_cents=90000, est_restoration_cost_cents=5000,
        est_restoration_effort_hours=6.0, confidence=0.8, deal_score=70.0,
        reasoning="dovetailed drawers, solid walnut",
    )
    return evaluate_piece(listing, appraisal, hourly_rate_cents=3000, **kw)


def _page(pieces=None, meta=None):
    return render_board(
        RunResult(pieces=pieces or [_piece()], plan=AppraisalPlan()),
        meta=meta or BoardMeta(repo="me/marketplace", branch="main"),
    )


# --- it renders at all ------------------------------------------------------------------

def test_the_page_is_self_contained_and_needs_no_network_to_render():
    page = _page()
    assert page.startswith("<!doctype html>")
    assert "<script src=" not in page and "<link rel=\"stylesheet\"" not in page
    # The only outbound calls are the ones you press a button for.
    assert page.count("https://api.github.com") >= 1
    assert "prefers-color-scheme" in page          # both themes


def test_no_placeholder_survives_into_the_output():
    assert not re.search(r"\{\{[A-Z_]+\}\}", _page())


def test_every_card_carries_its_listing_id_for_the_write_side():
    page = _page([_piece("id1"), _piece("id2", title="oak dresser")])
    assert page.count('<article class="card') == 2
    assert 'data-id="id1"' in page and 'data-id="id2"' in page
    assert page.count('class="logform"') == 2 and page.count('class="negoform"') == 2


# --- config -----------------------------------------------------------------------------

def _config(page):
    return json.loads(re.search(r"const CFG = (\{.*?\});", page, re.S).group(1))


def test_the_page_knows_which_repo_and_branch_to_write_to():
    cfg = _config(_page(meta=BoardMeta(repo="me/marketplace", branch="feature-x")))
    assert cfg["repo"] == "me/marketplace" and cfg["branch"] == "feature-x"
    assert cfg["boardWorkflow"] == "deal-board.yml"
    assert cfg["negotiateWorkflow"] == "negotiate.yml"


def test_a_board_built_without_a_repo_says_so_instead_of_failing_silently():
    page = _page(meta=BoardMeta(repo=""))
    assert _config(page)["repo"] == ""
    assert "read-only board" in page
    assert "built without a repo" in page          # the message a button would show


def test_config_cannot_break_out_of_the_script_block():
    page = _page(meta=BoardMeta(repo="me/</script><script>alert(1)</script>"))
    body = re.search(r"const CFG = (\{.*?\});", page, re.S).group(1)
    assert "</script>" not in body
    assert json.loads(body)["repo"].startswith("me/")


# --- failure paths ----------------------------------------------------------------------

def test_every_http_failure_maps_to_a_sentence_you_can_act_on():
    page = _page()
    for needle in (
        "GitHub rejected the token",       # 401
        "Token lacks permission",          # 403
        "Not found",                       # 404
        "GitHub refused the write",        # 409/422
        "No token yet",                    # no credential at all
        "Gave up waiting",                 # the poll timing out
    ):
        assert needle in page, needle


def test_the_negotiation_panel_states_that_nothing_is_sent():
    page = _page()
    assert "nothing is ever sent for you" in page.lower()
    assert "above your walk-away" in page          # the guard is rendered, not just computed


def test_the_token_note_is_honest_about_the_risk():
    """The dialog must state the real blast radius, not minimise it: Contents-write can
    push workflow changes that read repo secrets, and localStorage is shared across
    every GitHub Pages site on the same username origin."""
    page = _page()
    assert "push code" in page and "secrets" in page
    assert "localStorage" in page and "shared" in page
    assert "rotate" in page.lower()


# --- the two resale tiers on the card ---------------------------------------------------

def test_the_card_headlines_the_market_price_and_folds_your_numbers_away():
    page = _page()
    assert "Sell target" in page
    assert "<summary>Your numbers</summary>" in page
    assert "estimated: bought at ask" in page


def test_logged_costs_change_only_the_personal_panel():
    bare = _page([_piece()])
    logged = _page([_piece(logged_costs=PieceCosts(
        acquisition_cents=25000, materials_cents=4000, labor_hours=6.0))])

    target = re.search(r"Sell target <b>([^<]+)</b>", bare).group(1)
    assert f"Sell target <b>{target}</b>" in logged
    assert "your logged costs" in logged and "your logged costs" not in bare


# --- writing to disk ---------------------------------------------------------------------

def test_write_site_is_idempotent_across_runs(tmp_path):
    """The second run used to raise SameFileError when a catalogue photo was handed back
    as its own source."""
    photo = tmp_path / "src.jpg"
    photo.write_bytes(b"jpegbytes")
    result = RunResult(pieces=[_piece()], plan=AppraisalPlan())

    page = write_site(result, tmp_path / "site", photo_files={"abc": photo})
    assert page.exists() and (tmp_path / "site" / "photos" / "abc.jpg").exists()

    again = write_site(
        result, tmp_path / "site",
        photo_files={"abc": tmp_path / "site" / "photos" / "abc.jpg"},
    )
    assert again.exists()


def test_pieces_appraised_on_earlier_runs_keep_their_committed_photos(tmp_path):
    result = RunResult(pieces=[_piece("old")], plan=AppraisalPlan())
    write_site(result, tmp_path / "site", extra_photo_map={"old": "photos/old.jpg"})
    assert 'src="photos/old.jpg"' in (tmp_path / "site" / "index.html").read_text()


# --- the photo placeholder ----------------------------------------------------------------

def test_a_sentence_length_item_name_does_not_break_the_thumbnail():
    """A real appraisal names the piece in a full sentence. Rendered raw, it spilled out
    of the 104px thumbnail column and across the whole card."""
    from dealfinder.board import _short_label

    assert _short_label(
        "mixed furniture lot: 5-drawer light-wood dresser, dark-wood media console"
    ) == "Mixed Furniture Lot"
    assert _short_label("walnut credenza") == "Walnut Credenza"
    # A hyphen is part of the name, not a clause break.
    assert _short_label("three-piece bedroom set: large dresser") == "Three-Piece Bedroom Set"
    assert _short_label("white-painted 2-drawer nightstand, MCM-style legs") \
        == "White-Painted 2-Drawer Nightstand"
    # Long single clauses are cut on a word boundary, and nothing renders empty.
    assert len(_short_label("a " * 60)) <= 45
    assert _short_label("") == "No Photo"


def test_the_placeholder_is_clipped_in_css_too():
    page = _page()
    assert ".thumb.ph{" in page and "overflow:hidden" in page
    assert "-webkit-line-clamp:4" in page


def test_an_underwater_piece_says_do_not_buy_instead_of_a_sell_target():
    """9 of 24 cards hit this path on a real run, so it needs pinning."""
    from dealfinder.core.schemas import AppraisalResult

    listing = RawListing(fb_listing_id="bad", title="oak dresser", asking_price_cents=50000)
    appraisal = AppraisalResult(
        identified_item="dresser", est_asis_value_cents=50000,
        est_restored_resale_value_cents=20000,      # worth less than you'd pay
        est_restoration_cost_cents=5000, est_restoration_effort_hours=4.0,
        confidence=0.5, deal_score=5.0,
    )
    piece = evaluate_piece(listing, appraisal, hourly_rate_cents=3000)
    page = _page([piece])
    assert "Don't buy at this price" in page and "loses money" in page
    assert "Sell target" not in page
    # ...but the personal panel still shows the numbers behind that verdict.
    assert "<summary>Your numbers</summary>" in page


def test_a_warning_is_printed_once_per_card_not_twice():
    """It was rendered by both the resale row and the personal panel."""
    from dealfinder.core.schemas import AppraisalResult

    listing = RawListing(fb_listing_id="thin", title="oak dresser", asking_price_cents=5000)
    appraisal = AppraisalResult(
        identified_item="dresser", est_asis_value_cents=5000,
        est_restored_resale_value_cents=15000, est_restoration_cost_cents=2000,
        est_restoration_effort_hours=4.0, confidence=0.5, deal_score=40.0,
    )
    piece = evaluate_piece(listing, appraisal, hourly_rate_cents=3000)
    assert piece.resale.yours.warning                      # this piece does warn
    page = _page([piece])
    assert page.count("Fine if you enjoy the work") == 1


# --- regressions from the frontend audit --------------------------------------------------

def test_scraped_text_cannot_steer_the_template():
    """Cards are substituted LAST: a listing titled '{{CONFIG}}' used to get the page's
    config JSON injected into its own heading."""
    page = _page([_piece(title="{{CONFIG}} {{TITLE}} bargain")])
    assert "{{CONFIG}} {{TITLE}} bargain" in page          # survives, literally, escaped
    assert page.count('"boardWorkflow"') == 1              # config appears exactly once


def test_a_negative_margin_is_red_and_reads_negative():
    """10 of 25 committed cards showed a loss in GREEN, formatted '$-1,150'."""
    losing = _piece()
    losing.cash_margin_cents = -115000
    page = _page([losing])
    assert "fig net neg" in page
    assert "−$1,150" in page                               # sign before the currency
    assert "$-" not in page


def test_a_missing_price_is_unknown_not_free():
    from dealfinder.board import _money

    assert _money(None) == "—"
    assert _money(0) == "$0"
    assert _money(-12000) == "−$120"
    assert _money(123456) == "$1,235"


def test_only_https_listing_urls_become_links():
    """An empty href reloads the page (destroying half-typed notes); a javascript: URL
    from a third-party actor must never reach the browser."""
    bad = _piece()
    bad.listing = bad.listing.model_copy(update={"url": ""})
    evil = _piece()
    evil.listing = evil.listing.model_copy(update={"url": "javascript:alert(1)"})
    page = _page([bad, evil])
    assert "View listing" not in page
    assert "javascript:" not in page


def test_the_legend_is_generated_from_the_badge_definitions():
    """The shipped legend documented 6 chips while ranking.py emitted 9 — it drifted
    because it was hand-maintained. Now both come from BADGE_DEFS."""
    from dealfinder.ranking import BADGE_DEFS

    page = _page()
    for icon, label, _tone in BADGE_DEFS.values():
        assert label in page, f"legend is missing {label!r}"


def test_reasoning_is_clipped_at_a_word_with_an_ellipsis():
    wordy = _piece()
    wordy.appraisal = wordy.appraisal.model_copy(
        update={"reasoning": "flatscreen wall-mounting has collapsed demand " * 30}
    )
    page = _page([wordy])
    assert "demand …" in page or "demand…" in page or "wall-mounting…" in page
    assert "for large con<" not in page                    # the old mid-word chop


def test_cards_carry_machine_readable_data_attributes():
    """The JS must never scrape badge text or headings again — filters, sort, search and
    the log form all read data-*."""
    page = _page()
    for attr in ("data-title=", "data-priority=", "data-margin=", "data-ask=",
                 "data-fresh=", "data-killer=", "data-flag=", "data-oor=", "data-photos="):
        assert attr in page, f"missing {attr}"


def test_the_templates_ship_as_package_data():
    from importlib.resources import files

    tpl = files("dealfinder").joinpath("templates")
    for name in ("board.html", "board.css", "board.js"):
        assert tpl.joinpath(name).is_file(), f"{name} missing from package data"


def test_the_page_has_a_csp_and_no_tooltip_only_errors():
    page = _page()
    assert "Content-Security-Policy" in page
    assert "connect-src 'self' https://api.github.com" in page
    assert "el.title = err.message" not in page            # errors render visibly now


def test_every_theme_pairing_clears_wcag_aa():
    """4.5:1 for the text/background pairs actually used. Computed, not eyeballed."""
    import re as _re

    from dealfinder.board import _template

    css = _template()

    def lum(hexcolor):
        r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (0, 2, 4))
        f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def contrast(a, b):
        la, lb = lum(a), lum(b)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    blocks = _re.findall(r":root\{(.*?)\}", css, _re.S)
    assert len(blocks) >= 2, "expected a light and a dark palette"
    for block in blocks[:2]:
        v = dict(_re.findall(r"--([\w-]+):\s*#([0-9a-fA-F]{6})", block))
        pairs = [
            ("ink", "paper"), ("ink", "card"), ("soft", "card"), ("soft", "paper"),
            ("accent", "card"), ("teal", "card"), ("good", "card"),
            ("warn", "warn-bg"), ("crit", "crit-bg"), ("crit", "card"),
            ("star-ink", "star"), ("tag-ink", "tag"),
        ]
        for fg, bg in pairs:
            if fg in v and bg in v:
                c = contrast(v[fg], v[bg])
                assert c >= 4.5, f"--{fg} on --{bg} is {c:.2f}:1 (< 4.5)"
