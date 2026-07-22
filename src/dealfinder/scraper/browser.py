"""patchright (stealth Playwright) browser context management.

patchright is a patched Playwright fork that closes the CDP ``Runtime.enable`` and
``navigator.webdriver`` automation tells that vanilla Playwright + JS-injection stealth
cannot fully hide. We drive the real installed Chrome (``channel="chrome"``) with a
fixed, realistic fingerprint. The residential IP of the host machine is the strongest
stealth signal — don't undermine it with a datacenter-looking browser.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from dealfinder.config import get_settings
from dealfinder.logging import get_logger

log = get_logger(__name__)

_VIEWPORT = {"width": 1440, "height": 900}


@asynccontextmanager
async def launch_context(
    storage_state_path: Path | None = None,
) -> AsyncIterator[Any]:
    """Yield a Playwright browser context with a stable, human-like fingerprint.

    ``storage_state_path`` loads a saved session (the warmed burner). ``None`` runs
    logged-out.
    """
    # Imported lazily so the rest of the app (and tests) don't require a browser.
    from patchright.async_api import async_playwright

    settings = get_settings()
    context_kwargs: dict[str, Any] = {
        "viewport": _VIEWPORT,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "device_scale_factor": 1,
    }
    if storage_state_path is not None and storage_state_path.exists():
        context_kwargs["storage_state"] = str(storage_state_path)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel=settings.browser_channel,
            headless=settings.headless,
        )
        context = await browser.new_context(**context_kwargs)
        try:
            yield context
        finally:
            await context.close()
            await browser.close()
