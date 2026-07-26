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

def _seed_catalog(tmp_path):
    from dealfinder import catalog as cat
    from dealfinder.core.schemas import AppraisalResult, RawListing

    c = cat.Catalog()
    cat.observe(c, [RawListing(
        fb_listing_id="abc", title="Lane walnut credenza", asking_price_cents=25000,
    )])
    c.listings["abc"].appraisal = AppraisalResult(
        identified_item="credenza", est_asis_value_cents=25000,
        est_restored_resale_value_cents=90000, est_restoration_cost_cents=5000,
        est_restoration_effort_hours=6.0, confidence=0.8, deal_score=70.0,
        condition_assessment="veneer lifting on the left door",
    )
    path = tmp_path / "catalog.json"
    cat.save_catalog(c, path)
    return path


def test_negotiate_writes_drafts_the_page_can_poll(tmp_path, monkeypatch):
    from dealfinder import negotiate

    catalog_path = _seed_catalog(tmp_path)
    stub = StubDrafter()
    monkeypatch.setattr(negotiate, "get_drafter", lambda name: stub)

    rc = negotiate.main([
        "--listing-id", "abc", "--posture", "20", "--conversation", "Seller: still here",
        "--catalog", str(catalog_path), "--pieces", str(tmp_path / "pieces.json"),
        "--out", str(tmp_path / "drafts"),
    ])
    assert rc == 0

    data = json.loads((tmp_path / "drafts" / "abc.json").read_text())
    assert data["status"] == "ok"
    assert data["posture_label"] == "aggressive"
    assert data["drafts"][0]["text"] == "Would you take $80?"
    # NEVER above ask — you can always simply pay the asking price. The original
    # assertion here was `X > Y or True`, which is unconditionally true; it spent its
    # life hiding a formula that told the model to offer MORE than the seller asked.
    assert data["walkaway_price_cents"] <= data["asking_price_cents"]
    assert "generated_at" in data
    # The stored appraisal supplied the leverage without anyone typing it.
    assert "veneer lifting" in stub.prompt


def test_a_failed_draft_still_writes_a_reason_so_the_page_stops_waiting(tmp_path, monkeypatch):
    from dealfinder import negotiate

    catalog_path = _seed_catalog(tmp_path)
    monkeypatch.setattr(
        negotiate, "get_drafter", lambda name: StubDrafter(boom="CLI not authenticated")
    )
    rc = negotiate.main([
        "--listing-id", "abc", "--catalog", str(catalog_path),
        "--pieces", str(tmp_path / "p.json"), "--out", str(tmp_path / "drafts"),
    ])
    assert rc == 1
    data = json.loads((tmp_path / "drafts" / "abc.json").read_text())
    assert data["status"] == "error" and "not authenticated" in data["error"]


def test_an_unknown_listing_reports_itself_rather_than_hanging(tmp_path):
    from dealfinder import negotiate

    rc = negotiate.main([
        "--listing-id", "nope", "--catalog", str(tmp_path / "missing.json"),
        "--pieces", str(tmp_path / "p.json"), "--out", str(tmp_path / "drafts"),
    ])
    assert rc == 2
    data = json.loads((tmp_path / "drafts" / "nope.json").read_text())
    assert data["status"] == "error" and "catalogue" in data["error"]


def test_drafts_flag_an_over_walkaway_offer_in_the_published_file(tmp_path, monkeypatch):
    from dealfinder import negotiate

    catalog_path = _seed_catalog(tmp_path)
    monkeypatch.setattr(negotiate, "get_drafter", lambda name: StubDrafter(
        drafts=NegotiationDrafts(drafts=[NegotiationDraft(text="I'll pay $9,000 flat")])
    ))
    negotiate.main([
        "--listing-id", "abc", "--catalog", str(catalog_path),
        "--pieces", str(tmp_path / "p.json"), "--out", str(tmp_path / "drafts"),
    ])
    data = json.loads((tmp_path / "drafts" / "abc.json").read_text())
    assert data["drafts"][0]["over_walkaway_cents"] == [900000]


def test_the_walkaway_is_the_price_at_which_the_flip_stops_paying(tmp_path):
    """walkaway = restored − restoration − labour − required margin, capped at ask.

    The formula this replaces was ask + margin/2 — ABOVE the asking price for every
    profitable piece, so the prompt told the model 'the most I will pay is $550' about a
    $250 listing, and offers_above() (comparing against the same number) could not fire.
    """
    from dealfinder.catalog import CatalogEntry
    from dealfinder.core.schemas import AppraisalResult
    from dealfinder.negotiate import _walkaway_cents

    def entry(ask, restored, resto=5000, hours=2.0):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        return CatalogEntry(
            id="x", first_seen=now, last_seen=now, asking_price_cents=ask,
            appraisal=AppraisalResult(
                identified_item="dresser", est_asis_value_cents=ask,
                est_restored_resale_value_cents=restored,
                est_restoration_cost_cents=resto,
                est_restoration_effort_hours=hours, confidence=0.8, deal_score=50.0,
            ),
        )

    # Marginal piece: $400 restored − $50 resto − 2h@$30 − $150 floor = $140 max offer.
    assert _walkaway_cents(entry(25000, 40000), 3000) == 14000

    # A screaming deal caps at the ask — never signals paying more than the seller wants.
    assert _walkaway_cents(entry(25000, 90000, hours=6.0), 3000) == 25000

    # A loser: restoring it costs more than it returns. No number beats a wrong number.
    assert _walkaway_cents(entry(25000, 20000), 3000) is None


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
        fb_listing_id="1", title="Knoll desk",
        description="Selling after moving to a smaller place.",
    ))
    assert genuine.is_red_flag is False

    fake = assess_authenticity(RawListing(
        fb_listing_id="2", title="Desk styled after Florence Knoll",
    ))
    assert fake.is_red_flag is True and fake.verdict == "styled_after"

    # Hedges are no longer swallowed by a stray style word.
    hedged = assess_authenticity(RawListing(
        fb_listing_id="3", title="Danish style dresser",
        description="Unmarked, no markings anywhere. Solid teak.",
    ))
    assert hedged.verdict == "hedged" and hedged.value_basis == "unconfirmed"
