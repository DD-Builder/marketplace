"""What the board says when every valuation failed.

The banner used to assert one cause regardless of the evidence:

    'every AI valuation failed — usually an expired CLAUDE_CODE_OAUTH_TOKEN'

On 2026-08-06 all twelve appraisals failed with ``You've hit your session limit · resets
4:10pm (UTC)`` — a spent subscription quota that clears itself in an hour. The run had
that sentence in hand, logged it, and dropped it on the floor; the board then told its
operator to go regenerate a credential that was working perfectly. Hours went into
re-minting tokens that were never the problem.

A diagnostic that guesses is worse than one that says nothing, because it is believed.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from dealfinder.run_board import failure_reason


# --- the reason itself --------------------------------------------------------------------

def test_the_reported_reason_is_the_one_that_actually_happened():
    quota = "claude CLI failed: You've hit your session limit · resets 4:10pm (UTC)"
    assert failure_reason([quota] * 12) == quota


def test_the_common_failure_wins_not_the_first():
    """One odd listing must not get to describe a run that died of something else."""
    odd = "no JSON object in claude CLI output"
    quota = "You've hit your session limit"
    assert failure_reason([odd] + [quota] * 11) == quota


def test_no_failures_means_no_claim():
    assert failure_reason([]) == ""


def test_a_credential_in_the_message_never_reaches_the_public_site():
    """status.json is served from a public Pages site, and this string is written by a
    third-party CLI rather than by us. If a token ever appears in an error, publishing it
    verbatim would leak it to anyone who opens the board."""
    for secret in ("sk-ant-oat01-abc123XYZ", "github_pat_11ABCDEFG0hunter2",
                   "ghp_aaaaaaaaaaaaaaaaaaaa", "apify_api_deadbeef00"):
        out = failure_reason([f"auth failed for {secret} on retry"])
        assert secret not in out, out
        assert "[redacted]" in out


def test_the_reason_is_one_bounded_line():
    """It lands in a banner. A multi-line stack trace would wreck the layout, and an
    unbounded string would bloat every status.json the run commits."""
    messy = "first line\n  second line\ttabbed\n\n" + "x" * 500
    out = failure_reason([messy])
    assert "\n" not in out and "\t" not in out
    assert "first line second line tabbed" in out
    assert len(out) <= 200


# --- the plumbing -------------------------------------------------------------------------

def test_the_engine_keeps_the_failures_instead_of_only_logging_them():
    from dealfinder.engine import run_valuation

    class Broken:
        name = "broken"

        def appraise(self, listing, vertical, *, image_paths=None, comps=None, venue=""):
            raise RuntimeError("You've hit your session limit")

    # Raw records, as the pipeline feeds them: prescreen refuses a listing with no photo,
    # so a hand-built RawListing never reaches the appraiser at all.
    listings = [
        {"id": str(i), "listingTitle": "solid walnut dresser",
         "listingPrice": {"amount": "40.00"},
         "listingPhotos": [{"image": {"uri": "u"}}]}
        for i in range(3)
    ]
    result = run_valuation(listings, provider=Broken())
    assert not result.pieces
    assert result.failures, "the reason was logged and then thrown away"
    assert all("session limit" in f for f in result.failures)


def test_a_run_that_values_nothing_publishes_why(monkeypatch):
    """End to end: the sentence the CLI produced has to survive all the way into the file
    the page reads. This is the hop that was missing."""
    from dealfinder import run_board

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")

    class OutOfQuota:
        name = "broken"

        def appraise(self, listing, vertical, *, image_paths=None, comps=None, venue=""):
            raise RuntimeError("claude CLI failed: You've hit your session limit "
                               "· resets 4:10pm (UTC)")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "listings.json").write_text(json.dumps([
            {"id": "a", "listingTitle": "solid walnut dresser",
             "listingPrice": {"amount": "40.00"},
             "listingPhotos": [{"image": {"uri": "u"}}]},
        ]))
        orig_get, orig_photos = run_board.get_appraiser, run_board._download_photos
        run_board.get_appraiser = lambda name: OutOfQuota()
        run_board._download_photos = lambda listings, out_dir, **kw: {}
        try:
            rc = run_board.main([
                "--from-json", str(tmp / "listings.json"), "--out", str(tmp / "site"),
                "--catalog", str(tmp / "catalog.json"), "--seen", str(tmp / "seen.json"),
                "--pieces", str(tmp / "pieces.json"),
            ])
        finally:
            run_board.get_appraiser, run_board._download_photos = orig_get, orig_photos

        assert rc == 4
        status = json.loads((tmp / "site" / "status.json").read_text())

    assert status["state"] == "appraisals_failed"
    assert "session limit" in status["reason"]
    assert "4:10pm" in status["reason"], "the actionable half — when it clears"


# --- the banner ---------------------------------------------------------------------------

def test_the_banner_no_longer_blames_the_token_by_default():
    """The specific sentence that sent an operator token-hunting for hours."""
    js = (Path(__file__).resolve().parents[1]
          / "src" / "dealfinder" / "templates" / "board.js").read_text()
    assert "usually an expired CLAUDE_CODE_OAUTH_TOKEN" not in js
    assert "s.reason" in js, "the banner must show the run's own reason"
    # Third-party text: it may only ever be set as text, never parsed as markup.
    assert "n.innerHTML" not in js
