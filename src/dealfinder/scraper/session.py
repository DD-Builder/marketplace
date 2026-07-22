"""Fetching pages with a logged-out-first, burner-fallback strategy.

Default path is logged-out. If Facebook gates the content (login wall / redirect to
``/login`` / missing expected markup), we retry once with the warmed burner session.
If the burner itself hits a checkpoint, we raise ``BlockedError(checkpoint=True)`` so
the caller can cool that identity down — we never try to solve challenges.
"""

from __future__ import annotations

from pathlib import Path

from dealfinder.config import get_settings
from dealfinder.logging import get_logger
from dealfinder.scraper.browser import launch_context
from dealfinder.scraper.errors import BlockedError
from dealfinder.scraper.pacing import human_pause

log = get_logger(__name__)

# Markers that indicate we've been bounced to a login/checkpoint wall.
_LOGIN_MARKERS = (
    "login_form",
    'name="login"',
    "checkpoint",
    "You must log in to continue",
)


def _looks_gated(html: str, final_url: str) -> bool:
    if "/login" in final_url or "/checkpoint" in final_url:
        return True
    lowered = html[:20000].lower()
    return any(marker.lower() in lowered for marker in _LOGIN_MARKERS)


async def _fetch_once(url: str, storage_state: Path | None) -> tuple[str, str]:
    """Return (html, final_url) for a single context/attempt."""
    async with launch_context(storage_state) as context:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await human_pause()
        # A gentle scroll nudges lazy content in.
        await page.mouse.wheel(0, 2000)
        await human_pause()
        html = await page.content()
        final_url = page.url
        return html, final_url


async def fetch_html(url: str, *, allow_burner: bool = True) -> str:
    """Fetch a page, trying logged-out first then the burner session.

    Raises :class:`BlockedError` if both paths are gated (or the only path is gated and
    no burner is configured).
    """
    settings = get_settings()

    html, final_url = await _fetch_once(url, storage_state=None)
    if not _looks_gated(html, final_url):
        return html

    burner = Path(settings.fb_session_path) if settings.fb_session_path else None
    if not (allow_burner and burner and burner.exists()):
        raise BlockedError(f"logged-out access gated for {url}", needs_auth=True)

    log.info("falling_back_to_burner", url=url[:120])
    html, final_url = await _fetch_once(url, storage_state=burner)
    if _looks_gated(html, final_url):
        raise BlockedError(
            f"burner session challenged for {url}",
            needs_auth=True,
            checkpoint=True,
        )
    return html
