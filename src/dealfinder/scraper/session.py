"""A persistent browser session for one scrape cycle.

One Chrome instance is launched and kept open for the whole cycle (finding B5): the
logged-out context is reused across the search pages and every detail page, and a burner
context is created lazily only if Facebook gates a request. Photos are fetched through
the browser context's request API so the CDN sees the same cookies/UA as the page
(finding P3), instead of a bare httpx client that scontent URLs reject.

Strategy: logged-out first; on a login/checkpoint wall, retry once via the warmed burner
session. If the burner itself is challenged, raise ``BlockedError(checkpoint=True)`` so
the caller cools that identity down — we never try to solve challenges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dealfinder.config import get_settings
from dealfinder.logging import get_logger
from dealfinder.scraper import browser
from dealfinder.scraper.errors import BlockedError
from dealfinder.scraper.pacing import human_pause

log = get_logger(__name__)

_LOGIN_MARKERS = (
    "login_form",
    'name="login"',
    "checkpoint",
    "you must log in to continue",
)


def _looks_gated(html: str, final_url: str) -> bool:
    if "/login" in final_url or "/checkpoint" in final_url:
        return True
    lowered = html[:20000].lower()
    return any(marker in lowered for marker in _LOGIN_MARKERS)


class BrowserSession:
    """Owns the Playwright lifecycle for one scrape cycle."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._logged_out: Any = None
        self._burner: Any = None
        self._burner_tried = False

    async def start(self) -> "BrowserSession":
        from patchright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await browser.launch_browser(self._pw)
        self._logged_out = await self._browser.new_context(**browser.context_kwargs())
        return self

    async def close(self) -> None:
        for ctx in (self._logged_out, self._burner):
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def __aenter__(self) -> "BrowserSession":
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _burner_context(self) -> Any | None:
        if self._burner is not None:
            return self._burner
        path = get_settings().fb_session_path
        if not path or not Path(path).exists():
            return None
        self._burner = await self._browser.new_context(
            **browser.context_kwargs(storage_state_path=path)
        )
        return self._burner

    async def _load(self, context: Any, url: str) -> tuple[str, str]:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await human_pause()
            await page.mouse.wheel(0, 2400)
            # Give client-side GraphQL a moment to stream the grid/detail JSON in.
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 — networkidle is best-effort
                pass
            return await page.content(), page.url
        finally:
            await page.close()

    async def fetch_html(self, url: str, *, allow_burner: bool = True) -> str:
        """Fetch a page logged-out, falling back to the burner if gated."""
        html, final_url = await self._load(self._logged_out, url)
        if not _looks_gated(html, final_url):
            return html

        if allow_burner:
            burner = await self._burner_context()
            if burner is not None:
                log.info("falling_back_to_burner", url=url[:120])
                html, final_url = await self._load(burner, url)
                if _looks_gated(html, final_url):
                    raise BlockedError(
                        f"burner session challenged for {url}",
                        needs_auth=True,
                        checkpoint=True,
                    )
                return html
        raise BlockedError(f"logged-out access gated for {url}", needs_auth=True)

    async def fetch_bytes(self, url: str) -> bytes | None:
        """Fetch a binary asset (photo) through the browser context's request API."""
        ctx = self._burner or self._logged_out
        try:
            resp = await ctx.request.get(url, timeout=30000)
            if not resp.ok:
                log.warning("photo_fetch_status", url=url[:120], status=resp.status)
                return None
            return await resp.body()
        except Exception as exc:  # noqa: BLE001
            log.warning("photo_fetch_failed", url=url[:120], error=str(exc))
            return None
