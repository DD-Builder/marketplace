"""A browser-backed fetcher for EBTH, because the site is a locked-down SPA.

The probe established this the hard way: ebth.com serves a byte-identical empty React
shell for every URL, its data arrives over a GraphQL API that refuses anonymous callers
with ``{"error":"Invalid client"}``, and there is no server-rendered fallback. Plain HTTP
gets nothing.

A real browser gets everything — because it *is* a real client. It runs the app's own
JavaScript, which authenticates itself the way it does for any visitor, fetches the lots
over GraphQL, and renders them. We don't scrape the fragile rendered DOM: we listen to
the network and capture the app's own JSON responses. That is the cleanest possible
data (the same structured payloads the app consumes) obtained the most defensible way
(no credential is extracted or replayed — the app authenticates itself).

This is deliberately a thin, single-purpose adapter. It exposes exactly what
:class:`~dealfinder.sources.ebth.EbthClient` needs through duck typing:

* ``fetch(url) -> str`` — navigate, wait for hydration, return the rendered HTML;
* ``drain_captures() -> list`` — the JSON payloads intercepted during that navigation.

``EbthClient._fetch_page`` finds ``drain_captures`` on the object owning ``fetch`` and
folds the captures into parsing. Playwright is imported lazily so the package installs
and the whole existing test-suite runs without it.
"""

from __future__ import annotations

import os

from dealfinder.logging import get_logger

log = get_logger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: A response is worth capturing only if its URL looks like a data call. Keeps analytics,
#: fonts, images, and beacons out of the harvested payload set.
_CAPTURE_HINTS = ("graphql", "/api/", "search", "items", "sales", "bid", "lot", "auction")

#: Substrings that mark a response as definitely-not-data even though the URL matched a
#: hint (e.g. an analytics endpoint containing "api").
_CAPTURE_EXCLUDE = ("google", "segment", "sentry", "cloudflareinsights", "doubleclick",
                    "facebook", "hotjar", "optimizely", "maxaccess")


class PlaywrightUnavailable(RuntimeError):
    """Playwright (or its Chromium) is not installed in this environment."""


class BrowserSession:
    """A reusable headless-Chromium page that captures the JSON a page fetches for itself.

    One session drives many ``fetch`` calls (the browser launches once and is reused),
    so an hourly run over a few dozen lots pays the launch cost a single time. Always
    ``close()`` it — wrap construction in try/finally or use it as a context manager.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        hydrate_ms: int = 4_000,
        wait_selector: str = 'a[href*="/items/"]',
        capture_hints: tuple[str, ...] = _CAPTURE_HINTS,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.hydrate_ms = hydrate_ms
        self.wait_selector = wait_selector
        self.capture_hints = capture_hints
        self._pw = None
        self._browser = None
        self._page = None
        self._captures: list = []

    # --- lifecycle ----------------------------------------------------------------------

    def _ensure(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover — env-dependent
            raise PlaywrightUnavailable(
                "playwright is not installed. `pip install playwright` and ensure a "
                "Chromium is present (the CI runner has one at /opt/pw-browsers)."
            ) from exc

        self._pw = sync_playwright().start()
        launch_kwargs: dict = {"headless": self.headless}
        # The managed runner ships Chromium at a known path and blocks re-downloads; use
        # it explicitly when present so we never trip the "run playwright install" wall.
        exe = os.getenv("PLAYWRIGHT_CHROMIUM_PATH") or "/opt/pw-browsers/chromium"
        if os.path.exists(exe):
            launch_kwargs["executable_path"] = exe
        try:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception as exc:  # pragma: no cover — env-dependent
            self.close()
            raise PlaywrightUnavailable(f"could not launch Chromium: {exc}") from exc
        self._page = self._browser.new_page(user_agent=_UA)
        self._page.on("response", self._on_response)

    def close(self) -> None:
        for attr, closer in (("_page", None), ("_browser", "close"), ("_pw", "stop")):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                if closer:
                    getattr(obj, closer)()
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
            setattr(self, attr, None)

    def __enter__(self) -> "BrowserSession":
        self._ensure()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- interception -------------------------------------------------------------------

    def _on_response(self, response) -> None:
        url = response.url.lower()
        if any(bad in url for bad in _CAPTURE_EXCLUDE):
            return
        if not any(h in url for h in self.capture_hints):
            return
        ctype = ""
        try:
            ctype = (response.headers or {}).get("content-type", "")
        except Exception:  # noqa: BLE001
            pass
        if "json" not in ctype.lower():
            return
        try:
            self._captures.append(response.json())
        except Exception:  # noqa: BLE001 — a body we can't parse is simply not a capture
            return

    def drain_captures(self) -> list:
        """The JSON payloads seen since the last fetch/drain, then reset."""
        caps, self._captures = self._captures, []
        return caps

    # --- the fetch the client calls -----------------------------------------------------

    def fetch(self, url: str) -> str:
        """Navigate to ``url``, wait for the app to render lots, return the hydrated HTML.

        Captures accumulate as a side effect and are read via :meth:`drain_captures`.
        ``domcontentloaded`` + an explicit hydration wait is used rather than
        ``networkidle`` on purpose: the page holds a live PubNub socket for bid updates,
        so it is *never* network-idle and that wait would always time out.
        """
        self._ensure()
        self._captures = []
        self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            self._page.wait_for_selector(self.wait_selector, timeout=self.hydrate_ms)
        except Exception:  # noqa: BLE001 — a page with no lots simply has no selector
            self._page.wait_for_timeout(self.hydrate_ms)
        return self._page.content()
