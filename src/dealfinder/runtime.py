"""Shared plumbing for a pipeline run: environment, credentials, photos, exit reasons.

These were originally private helpers inside the Marketplace runner. That runner is gone —
it drove a data vendor that collected listings automatically — but the helpers themselves
were never specific to it: reading an environment variable, checking a token is present
before anything bills, pulling image bytes before their URLs expire, and turning a list of
failures into one honest exit reason. They live here so the auction pipeline can use them
without depending on a module that no longer exists.
"""

from __future__ import annotations

import os
import shutil
import sys
import urllib.request
from pathlib import Path

from dealfinder.logging import get_logger

log = get_logger(__name__)


def _env(name: str, default: str) -> str:
    """Read an env var, treating empty/whitespace as unset.

    GitHub Actions substitutes an unset repository variable as an empty string rather
    than omitting it, so ``os.getenv(name, default)`` returns "" and never the default —
    which then blows up ``int("")``. Anything optional must go through here.
    """
    return (os.getenv(name) or "").strip() or default


def _int_env(name: str) -> int | None:
    raw = _env(name, "")
    try:
        return int(raw) if raw else None
    except ValueError:
        log.warning("bad_int_env", name=name, value=raw)
        return None


def _search_urls(raw: str) -> list[str]:
    import re

    parts: list[str] = []
    for line in raw.split("\n"):
        # A comma separates URLs only when the next chunk starts one — a comma *inside*
        # a query value must not split the URL into two garbage halves.
        parts += [p.strip() for p in re.split(r",(?=\s*https?://)", line.strip())]
    return [p for p in parts if p.startswith("http")]


def _check_credentials(provider: str) -> int:
    """Return a non-zero exit code (and explain) if the chosen appraiser can't authenticate.

    Only enforced in CI. Locally the Claude Code CLI carries its own stored login, so
    demanding the env var there would reject a perfectly working setup.
    """
    in_ci = os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
    if (
        in_ci
        and provider == "claude-code"
        and not os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    ):
        print(
            "CLAUDE_CODE_OAUTH_TOKEN is empty — the subscription appraiser cannot "
            "authenticate, so every valuation would fail.\n"
            "Fix: add a repository SECRET (not a variable) named exactly "
            "CLAUDE_CODE_OAUTH_TOKEN, holding the sk-ant-oat... value printed by "
            "`claude setup-token`.",
            file=sys.stderr,
        )
        return 3
    if provider == "claude-api" and not os.getenv("ANTHROPIC_API_KEY", "").strip():
        print("APPRAISER_PROVIDER=claude-api but ANTHROPIC_API_KEY is empty.", file=sys.stderr)
        return 3
    return 0


def failure_reason(failures: list[str]) -> str:
    """One line saying why the valuations actually failed, for the board's banner.

    Worth the plumbing: the banner used to assert "usually an expired
    CLAUDE_CODE_OAUTH_TOKEN" no matter what went wrong. On 2026-08-06 every appraisal
    failed with "You've hit your session limit · resets 4:10pm (UTC)" — a spent
    subscription quota that fixes itself in an hour — and the board sent its operator off
    to regenerate a credential that was working perfectly. Reporting the real message is
    the difference between waiting an hour and losing an evening.

    The *most common* message wins rather than the first: one odd listing shouldn't get to
    describe a run where the other eleven died of the same thing.
    """
    if not failures:
        return ""
    common = Counter(str(f) for f in failures).most_common(1)[0][0]
    return _SECRETISH.sub("[redacted]", " ".join(common.split()))[:200]


def _download_photos(
    listings, out_dir: Path, per_listing: int = 3, timeout: float = 12.0
) -> dict[str, list[Path]]:
    """Grab photo bytes now — signed image URLs typically expire within hours.

    Fails fast and gives up entirely after a few consecutive failures: in a network that
    can't reach the photo CDN, retrying every URL just burns the job's time budget.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    got: dict[str, list[Path]] = {}
    consecutive_failures = 0
    for listing in listings:
        if consecutive_failures >= 6:
            log.warning("photo_download_abandoned", reason="photo host unreachable")
            break
        paths: list[Path] = []
        for i, photo in enumerate(listing.photos[:per_listing]):
            if not photo.remote_url.startswith("http"):
                # A local: sentinel from the catalogue — nothing to download, and it must
                # not count against the circuit breaker. The on-disk supplement covers it.
                continue
            dest = out_dir / f"{listing.listing_id}_{i}.jpg"
            try:
                req = urllib.request.Request(
                    photo.remote_url, headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    dest.write_bytes(resp.read())
                paths.append(dest)
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 — a missing photo isn't fatal
                consecutive_failures += 1
                log.warning("photo_failed", listing=listing.listing_id, error=str(exc)[:120])
                break  # the rest of this listing's photos will fail the same way
        if paths:
            got[listing.listing_id] = paths
    return got
