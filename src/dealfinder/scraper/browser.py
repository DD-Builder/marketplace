"""patchright (stealth Playwright) building blocks.

patchright is a patched Playwright fork that closes the CDP ``Runtime.enable`` and
``navigator.webdriver`` automation tells that vanilla Playwright + JS-injection stealth
cannot fully hide. We drive the real installed Chrome (``channel="chrome"``) with a
fixed, realistic fingerprint. The residential IP of the host machine is the strongest
stealth signal — don't undermine it with a datacenter-looking browser.

Lifecycle (launching Chrome, creating contexts) is owned by
:class:`dealfinder.scraper.session.BrowserSession`, which keeps one browser open for a
whole scrape cycle instead of relaunching per page (finding B5).
"""

from __future__ import annotations

from typing import Any

from dealfinder.config import get_settings

VIEWPORT = {"width": 1440, "height": 900}


def context_kwargs(storage_state_path: str | None = None) -> dict[str, Any]:
    """Human-like context options; optionally load a saved (burner) session."""
    kwargs: dict[str, Any] = {
        "viewport": VIEWPORT,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "device_scale_factor": 1,
    }
    if storage_state_path:
        kwargs["storage_state"] = storage_state_path
    return kwargs


async def launch_browser(playwright: Any) -> Any:
    """Launch the real Chrome build patchright drives."""
    settings = get_settings()
    return await playwright.chromium.launch(
        channel=settings.browser_channel,
        headless=settings.headless,
    )
