"""Negotiation drafting: pure prompt logic, the walk-away guard, and the publish path.

No model is ever invoked here — the drafter is injected, which is exactly why the prompt
building had to be a pure function.
"""

from __future__ import annotations

import json

import pytest

from dealfinder.core.schemas import NegotiationDraft, NegotiationDrafts
from dealfinder.negotiation.drafts import (
    build_prompt,
    draft_replies,
    get_drafter,
    offers_above,
)
from dealfinder.negotiation.posture import posture_params


class StubDrafter:
    name = "stub"

    def __init__(self, drafts=None, boom=None):
        self.drafts = drafts
        self.boom = boom
        self.prompt = ""

    def draft(self, prompt):
        self.prompt = prompt
        if self.boom:
            raise RuntimeError(self.boom)
        return self.drafts or NegotiationDrafts(drafts=[
            NegotiationDraft(text="  Would you take $80?  ", rationale="  anchors low  "),
        ])


# --- posture --------------------------------------------------------------------------

def test_posture_moves_from_ready_to_walk_to_pay_asking():
    assert posture_params(0).label == "aggressive"
    assert posture_params(40).label == "measured"
    assert posture_params(70).label == "keen"
    assert posture_params(100).label == "eager"
    assert posture_params(-50).label == "aggressive"     # clamped
    assert posture_params(999).label == "eager"


def test_the_prompt_carries_the_posture_and_the_walk_away():
    prompt = build_prompt(
        posture=10, listing_title="Lane walnut credenza",
        asking_price_cents=25000, walkaway_price_cents=14000,
        condition_notes="veneer lifting on the left door",
        conversation="Seller: it's still available",
    )
    assert "Lane walnut credenza" in prompt
    assert "$250" in prompt and "$140" in prompt
    assert "aggressive" in prompt
    assert "veneer lifting" in prompt
    assert "Seller: it's still available" in prompt


def test_an_empty_thread_asks_for_an_opener():
    assert "write the opener" in build_prompt(posture=50, listing_title="dresser")


def test_an_unknown_walk_away_is_stated_not_faked():
    prompt = build_prompt(posture=50, listing_title="dresser", asking_price_cents=None)
    assert "Asking price: unknown" in prompt
    assert "walk-away (the most I will pay): unknown" in prompt


def test_long_input_is_bounded_so_one_paste_cannot_blow_up_the_call():
    prompt = build_prompt(
        posture=50, listing_title="x" * 999, conversation="y" * 9999,
        condition_notes="z" * 999,
    )
    # Measure the longest run of each filler character, so the prompt's own prose
    # (which contains plenty of x, y and z) doesn't muddy the assertion.
    import re

    longest = lambda ch: max((len(m) for m in re.findall(ch + "+", prompt)), default=0)
    assert longest("x") == 200 and longest("y") == 4000 and longest("z") == 800


# --- drafting -------------------------------------------------------------------------

def test_drafts_come_back_trimmed():
    stub = StubDrafter()
    out = draft_replies(posture=30, listing_title="dresser", drafter=stub)
    assert out.drafts[0].text == "Would you take $80?"
    assert out.drafts[0].rationale == "anchors low"


def test_an_empty_response_is_an_error_not_an_empty_panel():
    stub = StubDrafter(drafts=NegotiationDrafts(drafts=[]))
    with pytest.raises(RuntimeError, match="no candidates"):
        draft_replies(posture=30, listing_title="dresser", drafter=stub)


def test_unknown_drafter_names_fail_loudly():
    with pytest.raises(ValueError, match="unknown drafter"):
        get_drafter("telepathy")
    assert get_drafter("claude-code").name == "claude-code"


# --- the walk-away guard --------------------------------------------------------------

def test_a_draft_offering_more_than_your_walk_away_is_flagged():
    """The one failure mode that makes this feature actively harmful."""
    assert offers_above("I could stretch to $150 today", 12000) == [15000]
    assert offers_above("Would you take $95?", 12000) == []
    assert offers_above("$1,200 is over", 100000) == [120000]
    assert offers_above("$99.99 works", 9000) == [9999]


def test_no_walk_away_means_nothing_to_flag():
    assert offers_above("I'll pay $500", None) == []


# --- the publish path -----------------------------------------------------------------

def test_deal_score_and_killer_gate_agree_a_real_margin_is_a_killer():
    """The old gate (score>=70 @ conf>=0.65) was unreachable: score = base x conf with
    base < 100, so at the confidence floor it topped out at 65. Every star came from the
    cheap-flip branch — 'killer deal' silently meant 'cheap item'."""
    from dealfinder.core.schemas import AppraisalResult
    from dealfinder.ranking import is_killer_deal
    from dealfinder.valuation.scoring import compute_deal_score
    from dealfinder.authenticity import AuthenticityAssessment

    clean = AuthenticityAssessment(verdict="clear", is_red_flag=False, value_basis="genuine_ok")

    def score(margin_dollars, conf, ask=20000):
        appr = AppraisalResult(
            identified_item="credenza", est_asis_value_cents=ask,
            est_restored_resale_value_cents=ask + margin_dollars * 100 + 5000 + 6000,
            est_restoration_cost_cents=5000, est_restoration_effort_hours=2.0,
            confidence=conf, deal_score=0.0,
        )
        return compute_deal_score(appr, ask, 3000)

    # A genuine $1,000-net-margin piece at 0.75 confidence IS a killer...
    s = score(1000, 0.75)
    assert s >= 50
    assert is_killer_deal(deal_score=s, confidence=0.75, authenticity=clean,
                          net_margin_cents=100000, asking_price_cents=20000)
    # ...a $300 flip at high confidence is a fine deal but not a star...
    s2 = score(300, 0.9)
    assert not is_killer_deal(deal_score=s2, confidence=0.9, authenticity=clean,
                              net_margin_cents=30000, asking_price_cents=20000)
    # ...and the docstring's own scale is finally true: $500 -> 50 (pre-confidence).
    assert abs(score(500, 1.0) - 50.0) < 1.0


def test_free_junk_no_longer_tops_the_roi_axis():
    """outlay<=0 returned a perfect 100, skipping the meaningful-margin guard that was
    added because 'a real run had a $1 listing worth $50 ranking first'."""
    from dealfinder.ranking import roi_to_score

    free_junk = roi_to_score(5000, 0)          # free item, restores to $50
    cheap_gem = roi_to_score(40000, 2000)      # $20 item, restores to $400
    assert free_junk < 40                      # scaled down hard by the margin guard
    assert cheap_gem > free_junk               # the real flip outranks the freebie


def test_a_genuine_knoll_is_not_flagged_for_the_word_after():
    from dealfinder.authenticity import assess_authenticity
    from dealfinder.core.schemas import RawListing

    genuine = assess_authenticity(RawListing(
        listing_id="1", title="Knoll desk",
        description="Selling after moving to a smaller place.",
    ))
    assert genuine.is_red_flag is False

    fake = assess_authenticity(RawListing(
        listing_id="2", title="Desk styled after Florence Knoll",
    ))
    assert fake.is_red_flag is True and fake.verdict == "styled_after"

    # Hedges are no longer swallowed by a stray style word.
    hedged = assess_authenticity(RawListing(
        listing_id="3", title="Danish style dresser",
        description="Unmarked, no markings anywhere. Solid teak.",
    ))
    assert hedged.verdict == "hedged" and hedged.value_basis == "unconfirmed"
