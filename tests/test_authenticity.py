"""Fake / knock-off detection tests — the red-flag layer."""

from __future__ import annotations

from dealfinder.authenticity import assess_authenticity
from dealfinder.core.schemas import RawListing


def _l(title="", desc=""):
    return RawListing(fb_listing_id="x", title=title, description=desc)


def test_eames_esque_is_a_red_flag():
    a = assess_authenticity(_l(title="Eames-esque lounge chair"))
    assert a.is_red_flag and a.verdict == "styled_after"
    assert a.value_basis == "lookalike"


def test_barcelona_style_is_a_red_flag():
    a = assess_authenticity(_l(title="Barcelona style leather chair"))
    assert a.is_red_flag and a.verdict == "styled_after"


def test_explicit_reproduction_flagged():
    a = assess_authenticity(_l(title="Tulip table", desc="This is a reproduction, not original"))
    assert a.is_red_flag and a.verdict == "reproduction"


def test_manner_of_wegner_flagged():
    a = assess_authenticity(_l(title="Wishbone chair in the manner of Wegner"))
    assert a.is_red_flag


def test_genuine_maker_claim_not_flagged():
    # A confident maker claim with no styling qualifier isn't a text red flag —
    # vision still verifies, but the seller isn't hedging.
    a = assess_authenticity(_l(title="Herman Miller Eames lounge chair", desc="authentic, labeled"))
    assert not a.is_red_flag
    assert a.verdict == "clear"


def test_generic_style_is_soft_note_not_red_flag():
    a = assess_authenticity(_l(title="Mid-century style dresser"))
    assert not a.is_red_flag
    assert a.verdict == "generic_style"


def test_hedged_attribution():
    a = assess_authenticity(_l(title="Dresser", desc="Unmarked, we think it might be Broyhill"))
    assert not a.is_red_flag
    assert a.verdict == "hedged" and a.value_basis == "unconfirmed"


def test_plain_listing_is_clear():
    a = assess_authenticity(_l(title="Solid oak 6-drawer dresser", desc="good condition"))
    assert a.verdict == "clear" and not a.warnings
