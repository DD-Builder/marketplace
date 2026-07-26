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


def test_a_run_that_values_nothing_reports_failure(monkeypatch):
    """20 appraisals failed, the job reported SUCCESS, and published a blank board —
    which read as 'no deals this week' rather than 'your credential is missing'."""
    import json

    from dealfinder import run_board

    # The credential gate fires whenever GITHUB_ACTIONS=true — i.e. on the CI runner —
    # and would end this test at exit 3 before the appraisal stage it exists to pin.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")

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


def test_a_failing_claude_cli_reports_the_reason_not_the_token_counters():
    """The first real Action run failed all 12 appraisals and the log showed only a wall
    of zeroed usage fields — the CLI puts its reason in `result`, past the 400-character
    truncation point, and the is_error branch only ran on a zero exit code."""
    import json

    from dealfinder.appraiser import cli_failure_reason

    # The envelope shape the run actually produced, with `result` where the CLI puts it.
    envelope = json.dumps({
        "is_error": True, "duration_api_ms": 0, "num_turns": 1,
        "stop_reason": "stop_sequence", "session_id": "x" * 36, "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": 0},
        "result": "OAuth token has expired",
    })
    msg = cli_failure_reason(envelope)
    assert "OAuth token has expired" in msg
    assert "setup-token" in msg and "CLAUDE_CODE_OAUTH_TOKEN" in msg
    assert "cache_creation_input_tokens" not in msg      # the noise is gone

    # A refusal or rate-limit is surfaced verbatim, without the credential advice.
    plain = cli_failure_reason(json.dumps({"is_error": True, "result": "rate limit reached"}))
    assert plain == "rate limit reached"

    # No `result` at all still names the distinguishing fields rather than shrugging.
    bare = cli_failure_reason(json.dumps({
        "is_error": True, "stop_reason": "stop_sequence", "num_turns": 1,
        "total_cost_usd": 0, "usage": {"input_tokens": 0},
    }))
    assert "input_tokens=0" in bare and "authentication" in bare

    # Non-JSON output falls back to stderr, then stdout, then a plain statement.
    assert cli_failure_reason("not json", "boom") == "boom"
    assert cli_failure_reason("", "") == "no output at all"


def test_a_quota_blocked_scrape_still_rebuilds_the_board(tmp_path, monkeypatch):
    """Apify's monthly hard limit 403'd every search, and the run exited before rendering.

    Two things were wrong. The board went unrefreshed, so ranking and photo fixes never
    reached the page on a blocked day; and observe() would have counted a miss against all
    319 catalogue entries for a scan that never reached Marketplace, eventually retiring
    listings nobody had looked for.
    """
    import json
    from datetime import datetime, timedelta, timezone

    from dealfinder import run_board
    from dealfinder.catalog import Catalog, CatalogEntry, save_catalog
    from dealfinder.core.schemas import AppraisalResult

    now = datetime.now(timezone.utc)
    cat = Catalog()
    cat.listings["l1"] = CatalogEntry(
        id="l1", first_seen=now - timedelta(days=3), last_seen=now - timedelta(days=3),
        title="Walnut credenza", url="https://example.test/l1",
        location_text="Lexington", asking_price_cents=15000, state="live", misses=1,
        appraisal=AppraisalResult(
            identified_item="mid-century walnut credenza",
            est_asis_value_cents=20000, est_restored_resale_value_cents=60000,
            est_restoration_cost_cents=5000, est_restoration_effort_hours=6.0,
            confidence=0.7, deal_score=7.0, reasoning="stub",
        ),
    )
    catalog_path = tmp_path / "catalog.json"
    save_catalog(cat, catalog_path)

    def boom(*a, **k):
        raise RuntimeError(
            "Apify actor run failed with HTTP 403. Apify said: "
            "platform-feature-disabled: Monthly usage hard limit exceeded"
        )

    monkeypatch.setattr(run_board, "run_and_fetch", boom)
    monkeypatch.setenv("APIFY_TOKEN", "x")
    monkeypatch.setenv("SEARCH_URLS", "https://www.facebook.com/marketplace/lexington/search/?query=dresser")

    out = tmp_path / "site"
    rc = run_board.main([
        "--out", str(out), "--catalog", str(catalog_path),
        "--seen", str(tmp_path / "seen.json"), "--pieces", str(tmp_path / "pieces.json"),
        "--no-photos", "--dry-run",
    ])

    # Non-zero, because a scraper that has stopped working must not look like success.
    assert rc == 5
    # But the board was still written, from the catalogue.
    page = out / "index.html"
    assert page.exists()
    assert "Walnut credenza" in page.read_text()
    # And the failed scan left presence evidence untouched.
    after = json.loads(catalog_path.read_text())
    assert after["listings"]["l1"]["misses"] == 1
    assert after["listings"]["l1"]["state"] == "live"
