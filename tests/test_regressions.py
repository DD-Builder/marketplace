"""Regressions for bugs that reached a real run. Each one cost something to find."""

from __future__ import annotations


def test_blank_env_vars_do_not_crash_the_run():
    """GitHub substitutes an unset repository variable as an empty string rather than
    omitting it, so int("") blew up a whole run. Anything optional goes through _env."""
    import os

    from dealfinder.run_board import _env, _int_env

    os.environ["BOARD_TEST_EMPTY"] = "   "
    assert _env("BOARD_TEST_EMPTY", "fallback") == "fallback"
    assert _int_env("BOARD_TEST_EMPTY") is None
    os.environ["BOARD_TEST_JUNK"] = "not-a-number"
    assert _int_env("BOARD_TEST_JUNK") is None
    del os.environ["BOARD_TEST_EMPTY"], os.environ["BOARD_TEST_JUNK"]


def test_a_run_that_values_nothing_reports_failure():
    """20 appraisals failed, the job reported SUCCESS, and published a blank board —
    which read as 'no deals this week' rather than 'your credential is missing'."""
    import json

    from dealfinder import run_board

    class AlwaysFails:
        name = "broken"

        def appraise(self, listing, vertical, *, image_paths=None):
            raise RuntimeError("no credential")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "listings.json").write_text(json.dumps([
            {"id": "a", "listingTitle": "solid walnut dresser",
             "listingPrice": {"amount": "40.00"},
             "listingPhotos": [{"image": {"uri": "u"}}]},
        ]))
        orig_get, orig_photos = run_board.get_appraiser, run_board._download_photos
        run_board.get_appraiser = lambda name: AlwaysFails()
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


def test_a_missing_credential_is_caught_before_the_scrape_is_paid_for(monkeypatch):
    """Otherwise it surfaces as every appraisal failing one by one, by which point the
    Apify credit is already spent."""
    from dealfinder.run_board import _check_credentials

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    assert _check_credentials("claude-code") == 3

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    assert _check_credentials("claude-code") == 0

    # ...but a local run must not be blocked: the CLI carries its own stored login.
    monkeypatch.setenv("GITHUB_ACTIONS", "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    assert _check_credentials("claude-code") == 0
