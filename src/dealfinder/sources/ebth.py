"""EBTH (Everything But The House) — the auction-house source.

Marketplace listings have a sticker price; EBTH lots have a *price discovery process*:
bids sit low for days, then most of the real money arrives in the closing hours. That
changes what a source must return — not "what does it cost" but "where is the bidding
now, how many bids, and when does it end" — and the auction pipeline built on top of
this (:mod:`dealfinder.auctions`) is what turns those into a max-bid recommendation.

What the probe established (see ``run_auctions --probe`` and the committed reports):
ebth.com is a locked-down React SPA on a Rails backend. Every URL returns a byte-
identical empty shell, its lots arrive over a GraphQL API that refuses anonymous callers
with ``{"error":"Invalid client"}``, and there is no server-rendered fallback — plain
HTTP gets nothing. So the real fetch path is a headless browser
(:mod:`dealfinder.sources.ebth_browser`) that runs the app's own JavaScript, lets it
authenticate itself, and **captures the JSON the app fetches for itself** — the same
structured payloads it consumes, obtained without extracting or replaying any credential.

Extraction is still layered from most- to least-durable, so the parser needs no knowledge
of EBTH's exact query shape and a redesign degrades coverage rather than zeroing it:

1. **captured GraphQL/XHR payloads** (the browser path) — walked shape-agnostically for
   dicts that look like lots (an id + a bid-ish or end-time-ish field). The field aliases
   (``highBidAmount``, ``endsAt``, ``aasmState``, ``bidCount``) are confirmed against
   EBTH's own compiled bundle;
2. **JSON-LD and embedded JSON state** — for any server-rendered page or SEO markup;
3. **HTML regexes** for the handful of fields worth a last-ditch guess.

``probe()`` reports which layers fired and what field coverage they achieved — keys and
counts only, never values — so extraction stays tightened from evidence, not guesswork.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dealfinder.logging import get_logger

log = get_logger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: Seconds between requests. An hourly job watching a few dozen lots is a rounding error
#: to a site this size, and keeping it that way is what keeps it working.
_POLITE_DELAY = 1.0

_BASE = "https://www.ebth.com"


# --- the record the auction pipeline consumes -------------------------------------------

@dataclass
class AuctionItem:
    """One lot, as extracted from a search page or an item page."""

    item_id: str
    title: str = ""
    url: str = ""
    description: str = ""
    photo_urls: list[str] = field(default_factory=list)
    current_bid_cents: int | None = None
    bid_count: int | None = None
    ends_at: datetime | None = None
    #: Auction state as the page reports it, when it says at all.
    is_ended: bool | None = None
    #: Which extraction layer produced this record — kept for the probe report and for
    #: debugging a partial harvest ("why does everything lack an end time?").
    parsed_by: str = ""
    raw: dict = field(default_factory=dict)


# --- money / time coercion --------------------------------------------------------------

_MONEY_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d{1,2})?)")


def parse_money_cents(value, key: str = "") -> int | None:
    """Coerce a scraped money value to integer cents.

    The unit convention is unknown territory: a JSON field could carry dollars ("45.00",
    45.0) or cents (4500). The heuristic — strings and floats are dollars; ints are
    dollars unless the key says cents — is stated here once and validated against the
    probe report rather than guessed at every call site.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if "cent" in key.lower() else value * 100
    if isinstance(value, float):
        return round(value * 100)
    if isinstance(value, str):
        m = _MONEY_RE.search(value)
        if not m:
            return None
        try:
            return round(float(m.group(1).replace(",", "")) * 100)
        except ValueError:
            return None
    return None


def parse_when(value) -> datetime | None:
    """ISO strings (with or without Z), epoch seconds, or epoch milliseconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if value > 1e12:      # epoch millis
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            if value > 1e9:       # epoch seconds (2001+)
                return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


# --- layer 2: shape-agnostic JSON harvest -----------------------------------------------

# Field aliases, most-specific first. Lowercased, underscores stripped, so "currentBid",
# "current_bid" and "CurrentBid" all resolve alike.
# Confirmed against EBTH's own bundle (the probe pulled these names straight from their
# compiled JS): a lot carries highBidAmount / minimumBidAmount, bidCount, endsAt, and an
# aasmState string ("active" | "ended" | ...). The generic aliases stay so the harvester
# still works on other sources, but EBTH's real names lead.
_BID_KEYS = (
    "highbidamount", "currentbidcents", "currentbid", "currentbidamount", "highbid",
    "highestbid", "winningbid", "minimumbidamount", "nextbidamount", "currentprice",
    "salesprice", "price",
)
_COUNT_KEYS = ("bidcount", "bidscount", "numberofbids", "totalbids", "bids")
_END_KEYS = (
    "endsat", "biddingendsat", "endat", "endtime", "endingat", "auctionend", "enddate",
    "endson", "saleendsat", "availabilityends", "scheduledendtime",
)
_ID_KEYS = ("itemid", "id", "uuid", "slug")
_TITLE_KEYS = ("title", "name", "shortdescription")
_IMAGE_KEYS = ("imageurl", "image", "images", "photos", "mainimage", "primaryimage", "heroimage")
#: State fields. ``aasmState`` is AASM (the Rails state-machine gem) — a *string*, so it
#: needs interpreting, not truthiness: "active" is live, "ended"/"sold"/"closed" is done.
_ENDED_KEYS = ("isended", "ended", "isclosed", "closed", "iscomplete", "issold")
_STATE_KEYS = ("aasmstate", "state", "status", "salestate", "itemstate")
_ENDED_STATES = frozenset({"ended", "closed", "sold", "complete", "completed",
                           "finished", "won", "unsold", "passed"})


def _norm_key(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


def _first(d: dict, aliases: tuple[str, ...], *, allow_dict: bool = False):
    """The first alias present, returned with its ORIGINAL key (unit hints live there).

    Dict-valued keys are skipped by default: a scalar field like ``price`` must not match
    a nested ``price: {amount, currency}`` object and return the whole dict. Image fields
    are the exception (``primaryImage: {url: ...}`` is exactly the shape we want), so they
    pass ``allow_dict=True``.
    """
    normed = {
        _norm_key(k): (k, v) for k, v in d.items()
        if allow_dict or not isinstance(v, dict)
    }
    for a in aliases:
        if a in normed:
            return normed[a]
    return None, None


def _image_urls(value) -> list[str]:
    """Flatten whatever an image field holds into plain URL strings."""
    urls: list[str] = []

    def add(v):
        if isinstance(v, str) and v.startswith("http"):
            urls.append(v)
        elif isinstance(v, dict):
            for k in ("url", "src", "large", "original", "medium"):
                if isinstance(v.get(k), str) and v[k].startswith("http"):
                    urls.append(v[k])
                    break
        elif isinstance(v, list):
            for item in v[:6]:
                add(item)

    add(value)
    return urls[:6]


def _walk(obj, depth: int = 0):
    """Yield every dict inside a JSON blob, bounded so a pathological page can't recurse."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:400]:
            yield from _walk(v, depth + 1)


def _interpret_ended(ended_val, state_val) -> bool | None:
    """Resolve a lot's over/not-over status from a boolean flag or a state string.

    A boolean ``isEnded`` is taken at face value. A state string (AASM's ``aasmState``)
    is matched against the known terminal states — crucially, "active" must read as *not
    ended*, which naive truthiness (``bool("active") is True``) would get exactly wrong.
    """
    if isinstance(ended_val, bool):
        return ended_val
    if ended_val is not None and not isinstance(ended_val, str):
        return bool(ended_val)
    for val in (ended_val, state_val):
        if isinstance(val, str) and val.strip():
            return val.strip().lower() in _ENDED_STATES
    return None


def _looks_like_lot(d: dict) -> bool:
    """An id plus at least one auction-shaped field. Titles alone are everywhere."""
    _, ident = _first(d, _ID_KEYS)
    if ident is None:
        return False
    has_bid = _first(d, _BID_KEYS)[1] is not None
    has_end = _first(d, _END_KEYS)[1] is not None
    has_count = _first(d, _COUNT_KEYS)[1] is not None
    return has_bid or has_end or has_count


def harvest_json(blob, *, base_url: str = _BASE, parsed_by: str = "embedded-json") -> list[AuctionItem]:
    """Walk arbitrary JSON and pull out everything that looks like an auction lot."""
    items: dict[str, AuctionItem] = {}
    for d in _walk(blob):
        if not _looks_like_lot(d):
            continue
        _, ident = _first(d, _ID_KEYS)
        item_id = str(ident)
        bid_key, bid_val = _first(d, _BID_KEYS)
        _, count_val = _first(d, _COUNT_KEYS)
        _, end_val = _first(d, _END_KEYS)
        _, title_val = _first(d, _TITLE_KEYS)
        _, image_val = _first(d, _IMAGE_KEYS, allow_dict=True)
        _, ended_val = _first(d, _ENDED_KEYS)
        _, state_val = _first(d, _STATE_KEYS)
        # EBTH's search payload names the lot URL ``public_url``; the generic aliases
        # keep other sources working. Confirmed against the live probe's raw keys.
        _, url_val = _first(d, ("publicurl", "url", "path", "href", "itemurl", "webpath",
                                "permalink", "canonicalurl"))

        url = str(url_val) if isinstance(url_val, str) else ""
        if url.startswith("/"):
            url = urllib.parse.urljoin(base_url, url)

        candidate = AuctionItem(
            item_id=item_id,
            title=str(title_val or "")[:300],
            url=url,
            photo_urls=_image_urls(image_val),
            current_bid_cents=parse_money_cents(bid_val, bid_key or ""),
            bid_count=int(count_val) if isinstance(count_val, (int, float))
            and not isinstance(count_val, bool) else None,
            ends_at=parse_when(end_val),
            is_ended=_interpret_ended(ended_val, state_val),
            parsed_by=parsed_by,
            raw={k: d[k] for k in list(d)[:40] if isinstance(d[k], (str, int, float, bool))},
        )
        # Two harvested shards of the same lot (a summary dict and a detail dict) merge,
        # fuller record wins field-by-field.
        prev = items.get(item_id)
        if prev is None:
            items[item_id] = candidate
        else:
            for attr in ("title", "url", "description"):
                if not getattr(prev, attr) and getattr(candidate, attr):
                    setattr(prev, attr, getattr(candidate, attr))
            for attr in ("current_bid_cents", "bid_count", "ends_at", "is_ended"):
                if getattr(prev, attr) is None and getattr(candidate, attr) is not None:
                    setattr(prev, attr, getattr(candidate, attr))
            if not prev.photo_urls:
                prev.photo_urls = candidate.photo_urls
    return list(items.values())


# --- layer 1: JSON-LD -------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def harvest_jsonld(html: str, *, page_url: str = "") -> list[AuctionItem]:
    """schema.org Product/Offer markup — the layer most likely to survive a redesign."""
    items: list[AuctionItem] = []
    for m in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                nodes.extend(n for n in graph if isinstance(n, dict))
                continue
            if str(node.get("@type", "")).lower() not in ("product", "individualproduct"):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            url = str(node.get("url") or offers.get("url") or page_url)
            item_id = url.rstrip("/").rsplit("/", 1)[-1] or str(node.get("sku", ""))
            if not item_id:
                continue
            items.append(AuctionItem(
                item_id=item_id,
                title=str(node.get("name", ""))[:300],
                url=url,
                description=str(node.get("description", ""))[:2000],
                photo_urls=_image_urls(node.get("image")),
                current_bid_cents=parse_money_cents(offers.get("price"), "price"),
                ends_at=parse_when(offers.get("availabilityEnds")
                                   or offers.get("priceValidUntil")),
                parsed_by="json-ld",
            ))
    return items


# --- layer 3: HTML fallbacks ------------------------------------------------------------

_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/json["\'][^>]*>(.*?)</script>', re.S | re.I
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id\s*=\s*["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I
)
_STATE_ASSIGN_RE = re.compile(
    r'window\.__(?:INITIAL_STATE|PRELOADED_STATE|APOLLO_STATE|DATA)__\s*=\s*({.*?})\s*[;<]',
    re.S,
)
# EBTH item paths. Both singular and plural accepted; the probe will show which is live.
_ITEM_LINK_RE = re.compile(r'href="((?:https?://[^"/]*ebth\.com)?/items?/[^"#?]+)"')


def item_links(html: str, *, base_url: str = _BASE, cap: int = 200) -> list[str]:
    """Item-page URLs found in a search/browse page, deduped, order preserved."""
    seen: dict[str, None] = {}
    for m in _ITEM_LINK_RE.finditer(html):
        url = urllib.parse.urljoin(base_url, m.group(1))
        if url not in seen:
            seen[url] = None
        if len(seen) >= cap:
            break
    return list(seen)


def _embedded_json_blobs(html: str) -> list:
    blobs = []
    for rx in (_NEXT_DATA_RE, _SCRIPT_JSON_RE):
        for m in rx.finditer(html):
            try:
                blobs.append(json.loads(m.group(1).strip()))
            except json.JSONDecodeError:
                continue
    for m in _STATE_ASSIGN_RE.finditer(html):
        try:
            blobs.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return blobs


def parse_page(html: str, *, page_url: str = "", captures=()) -> list[AuctionItem]:
    """All layers over one page, best record per id.

    ``captures`` are JSON payloads the page fetched for itself (the app's own GraphQL/XHR
    responses, grabbed by the browser fetcher). They are the richest source when the HTML
    is an empty SPA shell, and are harvested through the same shape-agnostic walk as any
    embedded blob — so the parser needs no knowledge of EBTH's exact query shape.
    """
    merged: dict[str, AuctionItem] = {}
    harvested = harvest_jsonld(html, page_url=page_url)
    for blob in _embedded_json_blobs(html):
        harvested += harvest_json(blob)
    for blob in captures:
        harvested += harvest_json(blob)
    for item in harvested:
        prev = merged.get(item.item_id)
        if prev is None:
            merged[item.item_id] = item
            continue
        for attr in ("title", "url", "description"):
            if not getattr(prev, attr) and getattr(item, attr):
                setattr(prev, attr, getattr(item, attr))
        for attr in ("current_bid_cents", "bid_count", "ends_at", "is_ended"):
            if getattr(prev, attr) is None and getattr(item, attr) is not None:
                setattr(prev, attr, getattr(item, attr))
        if not prev.photo_urls:
            prev.photo_urls = item.photo_urls
    return list(merged.values())


def item_id_from_url(url: str) -> str:
    """The stable id for a lot: the last path segment ("12345-walnut-credenza")."""
    return urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


# --- shell forensics --------------------------------------------------------------------

_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src\s*=\s*["\']([^"\']+)["\']', re.I)
#: URLs (absolute or path-relative) that smell like data endpoints, as referenced
#: anywhere in an app shell — markup, inline bootstrap code, link preloads.
_API_URL_RE = re.compile(
    r'["\'](?:(https?://[^"\']*(?:api|graphql|algolia|search)[^"\']*)'
    r'|(/(?:api|graphql)[^"\'\s]*))["\']', re.I
)
_VENDOR_HINTS = ("algolia", "typesense", "elastic", "graphql", "apollo", "relay",
                 "next", "nuxt", "webpack", "vite", "react", "turbo", "pubnub")

#: Quoted strings inside a JS bundle that look like routes or endpoints. The React app's
#: data paths are compiled into its bundle as plain string literals, so mining them is
#: how we learn the real API without being able to run the app.
_BUNDLE_STRING_RE = re.compile(
    r'"(/[a-zA-Z0-9_\-/.{}$:]{3,90})"|"(https?://[^"\\\s]{8,140})"'
)
_INTERESTING_PATH = re.compile(
    r"api|graphql|algolia|search|item|sale|browse|bid|listing|lot|\.json", re.I
)


def mine_bundle(js: str, *, cap: int = 100) -> list[str]:
    """Route/endpoint-shaped string literals from a JS bundle, deduped in order."""
    found: dict[str, None] = {}
    for m in _BUNDLE_STRING_RE.finditer(js):
        s = m.group(1) or m.group(2)
        if _INTERESTING_PATH.search(s) and not s.endswith((".js", ".css", ".png",
                                                           ".svg", ".woff", ".woff2")):
            found[s] = None
        if len(found) >= cap:
            break
    return list(found)


#: Identifiers a bidding UI cannot avoid naming. Context windows around these inside the
#: bundle reveal the GraphQL field names and query shapes the app actually uses.
_BID_IDENT_RE = re.compile(
    r"currentBid|highBid|bidCount|endsAt|saleEndsAt|minimumBid|nextBid|startingBid|"
    r"biddingEndsAt|auctionEnd",
)


def mine_bid_context(js: str, *, window: int = 90, cap: int = 24) -> list[str]:
    """Code snippets around bid-ish identifiers — query shapes, not page content."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _BID_IDENT_RE.finditer(js):
        snippet = js[max(0, m.start() - window): m.end() + window]
        snippet = " ".join(snippet.split())
        key = snippet[:40]
        if key not in seen:
            seen.add(key)
            out.append(snippet)
        if len(out) >= cap:
            break
    return out


def _applied_parameters(captures: list) -> dict | None:
    """The search API's own echo of what it understood from the request, when present —
    exactly what's needed to tell a real filtered search from a degraded browse-all."""
    for c in captures:
        if isinstance(c, dict) and isinstance(c.get("applied_parameters"), dict):
            return c["applied_parameters"]
    return None


def _search_page_count(captures: list) -> dict | None:
    for c in captures:
        if isinstance(c, dict) and isinstance(c.get("pages"), dict):
            return c["pages"]
    return None


def _non_item_fields(payload, *, max_depth: int = 2) -> dict:
    """A search response's top-level fields, minus the big listing arrays — this is
    where pagination (page/total/per_page) and facets live, and unlike listing content
    it's safe to report verbatim: it describes the response shape, not any one lot."""
    if not isinstance(payload, dict):
        return {}

    def small(v, depth=0):
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if depth >= max_depth:
            return f"{type(v).__name__}(...)"
        if isinstance(v, list):
            if len(v) > 5 or (v and isinstance(v[0], dict) and len(v[0]) > 6):
                return f"array[{len(v)}]"  # this is the listing array — key names only
            return [small(x, depth + 1) for x in v]
        if isinstance(v, dict):
            return {k: small(x, depth + 1) for k, x in list(v.items())[:15]}
        return str(type(v))

    return {k: small(v) for k, v in payload.items()}


def _tally(items) -> dict:
    """{value: count}, for a compact status/kind histogram in the probe report."""
    out: dict = {}
    for it in items:
        out[it] = out.get(it, 0) + 1
    return dict(sorted(out.items()))


def analyze_shell(html: str) -> dict:
    """Forensics on an app shell that rendered nothing: which script bundles it loads,
    which API-ish URLs its code references, and which frameworks it names. Everything
    reported is a URL or a framework token — no page content."""
    scripts = [m.group(1) for m in _SCRIPT_SRC_RE.finditer(html)]
    api_urls: dict[str, None] = {}
    for m in _API_URL_RE.finditer(html):
        api_urls[m.group(1) or m.group(2)] = None
    low = html.lower()
    return {
        "script_srcs": scripts[:20],
        "api_urls": list(api_urls)[:30],
        "framework_hints": [v for v in _VENDOR_HINTS if v in low],
    }


# --- the client -------------------------------------------------------------------------

def build_client(*, mode: str | None = None, timeout: float = 25.0,
                 delay: float = _POLITE_DELAY, **browser_kwargs) -> "EbthClient":
    """Construct an :class:`EbthClient` with the right fetch path for the environment.

    ``mode`` (or ``EBTH_FETCH``): ``"browser"`` (default) runs headless Chromium and
    intercepts the app's own JSON — the only thing that actually gets data off this SPA;
    ``"http"`` is the plain-HTTP path, kept for the item-page/HTML case and for anywhere
    a browser can't run. If the browser is requested but Playwright/Chromium isn't
    available, this logs and falls back to HTTP rather than failing the run.
    """
    import os

    chosen = (mode or os.getenv("EBTH_FETCH") or "browser").strip().lower()
    if chosen == "browser":
        try:
            from dealfinder.sources.ebth_browser import BrowserSession

            session = BrowserSession(**browser_kwargs)
            client = EbthClient(timeout=timeout, delay=delay, fetch=session.fetch)
            client._closer = session.close
            return client
        except Exception as exc:  # noqa: BLE001 — includes PlaywrightUnavailable
            log.warning("ebth_browser_unavailable", error=str(exc)[:160],
                        action="falling back to http")
    return EbthClient(timeout=timeout, delay=delay)


class EbthClient:
    """Polite fetch + parse against ebth.com. All knobs overridable for tests."""

    def __init__(self, *, timeout: float = 25.0, delay: float = _POLITE_DELAY,
                 fetch=None, post=None, closer=None) -> None:
        self.timeout = timeout
        self.delay = delay
        self._fetch = fetch or self._http_get
        self._post = post or self._http_post_json
        self._closer = closer
        self._last_request = 0.0

    def close(self) -> None:
        """Release any browser the fetcher owns. Safe to call on an HTTP-only client."""
        if self._closer is not None:
            self._closer()

    def _fetch_page(self, url: str) -> tuple[str, list]:
        """Fetch ``url`` and, if the fetcher captured the page's own JSON traffic (the
        browser path), return those payloads alongside the HTML.

        The capture channel is duck-typed: a browser session exposes ``drain_captures``
        on the object that owns the ``fetch`` method, so a plain-HTTP or lambda fetcher
        simply yields no captures and the caller falls back to HTML parsing. This keeps
        the browser dependency out of every existing test's injected ``fetch=lambda``.
        """
        html = self._fetch(url)
        owner = getattr(self._fetch, "__self__", None)
        drain = getattr(owner, "drain_captures", None)
        captures = drain() if callable(drain) else []
        return html, captures

    def _drain_netlog(self) -> list:
        """The browser fetcher's structural network log for the last fetch, if any."""
        owner = getattr(self._fetch, "__self__", None)
        drain = getattr(owner, "drain_netlog", None)
        return drain() if callable(drain) else []

    def _inspect_search_ui(self, url: str) -> dict | None:
        owner = getattr(self._fetch, "__self__", None)
        inspect = getattr(owner, "inspect_search_ui", None)
        return inspect(url) if callable(inspect) else None

    def _http_post_json(self, url: str, payload: dict) -> tuple[int, str]:
        """POST JSON, returning (status, body) — GraphQL answers 4xx with a body worth
        reading, so unlike GET this never raises on HTTP errors."""
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"User-Agent": _UA, "Content-Type": "application/json",
                     "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, (exc.read() or b"").decode("utf-8", errors="replace")
        finally:
            self._last_request = time.monotonic()

    def _http_get(self, url: str) -> str:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                self._last_request = time.monotonic()
                return resp.read().decode("utf-8", errors="replace")
        finally:
            self._last_request = time.monotonic()

    def search(self, url: str, *, follow_items: int = 0, max_pages: int = 5) -> list[AuctionItem]:
        """Harvest a search/browse page, paging through EBTH's own result count.

        The search response tells us exactly how many pages exist
        (``pages.total_pages``), so this fetches page 1, then follows up to
        ``max_pages - 1`` more real page loads — each is a full browser navigation, so
        the cost is genuinely ``max_pages`` fetches, not one. ``max_pages`` bounds that
        cost per query per run: five pages of 48 covers any query up to 240 results,
        which comfortably covers a well-scoped vertical search without turning an
        hourly job into a 129-page crawl of the entire site.
        """
        html, captures = self._fetch_page(url)
        items = parse_page(html, page_url=url, captures=captures)
        by_id = {i.item_id: i for i in items}

        pages_meta = _search_page_count(captures)
        total_pages = pages_meta.get("total_pages") if pages_meta else None
        if isinstance(total_pages, (int, float)) and total_pages > 1:
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            for page_num in range(2, min(int(total_pages), max_pages) + 1):
                query["page"] = [str(page_num)]
                next_url = parsed._replace(
                    query=urllib.parse.urlencode(query, doseq=True)
                ).geturl()
                try:
                    p_html, p_captures = self._fetch_page(next_url)
                except Exception as exc:  # noqa: BLE001 — a bad page shouldn't lose the rest
                    log.warning("ebth_page_failed", url=next_url, error=str(exc)[:160])
                    continue
                for item in parse_page(p_html, page_url=next_url, captures=p_captures):
                    by_id.setdefault(item.item_id, item)

        links = item_links(html)
        # Grid records lacking an id are invisible above; links are the safety net.
        for link in links:
            lid = item_id_from_url(link)
            if lid not in by_id:
                by_id[lid] = AuctionItem(item_id=lid, url=link, parsed_by="link-only")
            elif not by_id[lid].url:
                by_id[lid].url = link
        thin = [i for i in by_id.values() if i.ends_at is None and i.url]
        for item in thin[:follow_items]:
            try:
                detail = self.item(item.url)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                log.warning("ebth_item_failed", url=item.url, error=str(exc)[:120])
                continue
            if detail is not None:
                by_id[item.item_id] = detail
        return list(by_id.values())

    def item(self, url: str) -> AuctionItem | None:
        """Fetch one lot's page and return its fullest record."""
        html, captures = self._fetch_page(url)
        items = parse_page(html, page_url=url, captures=captures)
        wanted = item_id_from_url(url)
        for it in items:
            if it.item_id == wanted:
                if not it.url:
                    it.url = url
                return it
        # Fall back to the record with the most auction-shaped fields on the page.
        scored = sorted(
            items,
            key=lambda i: sum(x is not None for x in
                              (i.current_bid_cents, i.ends_at, i.bid_count)),
            reverse=True,
        )
        if scored and (scored[0].current_bid_cents is not None or scored[0].ends_at):
            best = scored[0]
            best.item_id = wanted        # trust the URL over a harvested internal id
            best.url = url
            return best
        return None

    # --- probe --------------------------------------------------------------------------

    def probe(self, search_urls: list[str]) -> dict:
        """Fetch the configured pages and report — structurally, no content — what the
        site serves and which parse layers fire. Committed by CI so extraction can be
        tightened from evidence instead of guessed at from a network-blind dev box."""
        report: dict = {"probed_at": datetime.now(timezone.utc).isoformat(), "pages": []}
        first_item_url = ""
        shell_html = ""
        for url in search_urls:
            page: dict = {"url": url, "kind": "search"}
            try:
                html, captures = self._fetch_page(url)
            except Exception as exc:  # noqa: BLE001 — the report IS the error channel
                page["error"] = f"{type(exc).__name__}: {exc}"[:300]
                report["pages"].append(page)
                continue
            page.update(self._analyze(html, url, captures=captures))
            links = item_links(html)
            page["item_links"] = len(links)
            if links and not first_item_url:
                first_item_url = links[0]
            netlog = self._drain_netlog()
            if netlog:
                # The decisive diagnostic on the browser path: did the app's own data
                # calls succeed here, or are they refused the way our direct probes were?
                # Report the API/GraphQL responses and a status tally — never a body.
                api = [n for n in netlog
                       if any(h in n["path"].lower() for h in
                              ("graphql", "/api/", "search", "items", "bid"))]
                page["network"] = {
                    "responses": len(netlog),
                    "api_calls": api[:25],
                    "status_tally": _tally(str(n["status"]) for n in netlog),
                }
            if captures:
                # The browser path: report what the app fetched for itself (structure
                # only — top-level keys and how many lots harvest from each payload).
                page["captured_payloads"] = [
                    {
                        "top_level_keys": sorted(c)[:20] if isinstance(c, dict)
                        else f"array[{len(c)}]",
                        "harvested_items": len(harvest_json(c)),
                        # Small scalar/dict top-level fields, in full — this is where a
                        # search API reports its pagination (page/total/per_page), and
                        # that shape is metadata about the *response*, not listing data,
                        # so it's safe to report verbatim rather than just its keys.
                        "non_item_fields": _non_item_fields(c),
                    }
                    for c in captures[:12]
                ]
            if page.get("harvested_items", 0) == 0 and not captures:
                # A raw app shell with no capture channel (HTTP path). The lots arrive
                # over XHR after JS runs, so the evidence is the endpoints the code names.
                page["shell"] = analyze_shell(html)
                shell_html = shell_html or html
            report["pages"].append(page)
            applied = _applied_parameters(captures)
            if applied is not None and applied.get("q") is None:
                # The query text never reached their API (search silently degraded to
                # "browse everything" — total_items came back as the whole catalogue,
                # not a filtered result). Try known EBTH query-param shapes and report
                # which one, if any, actually gets echoed back non-null.
                trials = self._try_query_variants(url)
                report.setdefault("query_variant_trials", []).extend(trials)
                if all(t.get("applied_q") is None for t in trials):
                    # No URL shape filtered anything — this is very likely a
                    # client-driven (type + submit) search, not a navigable URL. Read
                    # the real search box off the DOM instead of guessing further.
                    ui = self._inspect_search_ui(_BASE)
                    if ui is not None:
                        report["search_ui"] = ui
        if first_item_url:
            page = {"url": first_item_url, "kind": "item"}
            try:
                html, captures = self._fetch_page(first_item_url)
                page.update(self._analyze(html, first_item_url, captures=captures))
                if captures:
                    page["captured_payloads"] = [
                        {"top_level_keys": sorted(c)[:20] if isinstance(c, dict)
                         else f"array[{len(c)}]",
                         "harvested_items": len(harvest_json(c))}
                        for c in captures[:12]
                    ]
                netlog = self._drain_netlog()
                if netlog:
                    api = [n for n in netlog
                           if any(h in n["path"].lower() for h in
                                  ("graphql", "/api/", "items", "bid"))]
                    page["network"] = {"responses": len(netlog), "api_calls": api[:25],
                                       "status_tally": _tally(str(n["status"])
                                                              for n in netlog)}
            except Exception as exc:  # noqa: BLE001
                page["error"] = f"{type(exc).__name__}: {exc}"[:300]
            report["pages"].append(page)
        if shell_html and search_urls:
            shell = analyze_shell(shell_html)
            # The app's real data routes live inside its compiled bundle as string
            # literals. Mine the public chunks (never the vendors — they're framework).
            mined: list[str] = []
            bid_context: list[str] = []
            for src in shell.get("script_srcs", []):
                if "public" not in src or "vendors" in src:
                    continue
                try:
                    js = self._fetch(urllib.parse.urljoin(_BASE, src))
                except Exception as exc:  # noqa: BLE001
                    report.setdefault("bundle_errors", []).append(
                        f"{src}: {type(exc).__name__}"[:160])
                    continue
                mined += mine_bundle(js)
                bid_context += mine_bid_context(js)
                if len(mined) >= 100:
                    break
            report["bundle_paths"] = mined[:100]
            report["bundle_bid_context"] = bid_context[:24]
            shell["api_urls"] = list(dict.fromkeys(
                shell.get("api_urls", [])
                + [p for p in mined if _INTERESTING_PATH.search(p)]
            ))
            report["endpoint_trials"] = self._try_endpoints(shell, search_urls[0])
            report["graphql_trials"] = self._try_graphql(mined)
            report["sales_index"] = self._analyze_sales_index()
        return report

    def _try_graphql(self, mined_paths: list[str]) -> list[dict]:
        """Handshake with every GraphQL-shaped route: a {__typename} probe, then — if it
        answers — introspection of the query root's field names. Field names are the
        entire prize: they are what a real client's queries get written against."""
        endpoints = [p for p in dict.fromkeys(mined_paths) if "graphql" in p.lower()]
        endpoints = endpoints or ["/anon-graphql", "/graphql"]
        trials: list[dict] = []
        for path in endpoints[:4]:
            url = urllib.parse.urljoin(_BASE, path)
            trial: dict = {"url": url}
            try:
                status, body = self._post(url, {"query": "{__typename}"})
                trial["handshake_status"] = status
                trial["handshake_body"] = body[:300]
                if status == 200:
                    status2, body2 = self._post(url, {
                        "query": "{__schema{queryType{fields{name}}}}"
                    })
                    trial["introspection_status"] = status2
                    try:
                        fields = json.loads(body2)["data"]["__schema"]["queryType"]["fields"]
                        trial["query_fields"] = sorted(f["name"] for f in fields)[:80]
                    except (json.JSONDecodeError, KeyError, TypeError):
                        trial["introspection_body"] = body2[:400]
            except Exception as exc:  # noqa: BLE001
                trial["error"] = f"{type(exc).__name__}: {exc}"[:200]
            trials.append(trial)
        return trials

    def _try_query_variants(self, search_url: str) -> list[dict]:
        """When a query string produced ``applied_parameters.q == null`` (search silently
        degraded to browse-everything), try the URL shapes EBTH's search box plausibly
        uses and report which one gets echoed back non-null — the evidence for how to
        actually filter, rather than guessing one shape and hoping.
        """
        parsed = urllib.parse.urlparse(search_url)
        q = (urllib.parse.parse_qs(parsed.query).get("q") or ["furniture"])[0]
        enc = urllib.parse.quote(q)
        slug = q.replace(" ", "-").lower()
        candidates = [
            f"{_BASE}/search?query={enc}",
            f"{_BASE}/search?keywords={enc}",
            f"{_BASE}/search?term={enc}",
            f"{_BASE}/search?search={enc}",
            f"{_BASE}/search?utf8=%E2%9C%93&q={enc}",
            f"{_BASE}/search/{slug}",
            f"{_BASE}/marketplace/search?q={enc}",
        ]
        trials: list[dict] = []
        for url in candidates:
            trial: dict = {"url": url}
            try:
                _html, captures = self._fetch_page(url)
            except Exception as exc:  # noqa: BLE001
                trial["error"] = f"{type(exc).__name__}: {exc}"[:160]
                trials.append(trial)
                continue
            applied = _applied_parameters(captures)
            pages = _search_page_count(captures)
            trial["applied_q"] = applied.get("q") if applied else None
            trial["total_items"] = pages.get("total_items") if pages else None
            trial["harvested_items"] = sum(len(harvest_json(c)) for c in captures)
            trials.append(trial)
        return trials

    def _analyze_sales_index(self) -> dict:
        """The /sales/ page came back 3x the shell's size — server-rendered content.
        Report what it links to and whether the standard layers can already read it."""
        out: dict = {"url": f"{_BASE}/sales/"}
        try:
            html = self._fetch(f"{_BASE}/sales/")
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]
            return out
        out.update(self._analyze(html, f"{_BASE}/sales/"))
        sale_links = list(dict.fromkeys(
            m.group(1) for m in re.finditer(r'href="(/sales/[^"#?]+)"', html)
        ))
        out["sale_links"] = len(sale_links)
        out["sample_sale_links"] = sale_links[:8]
        out["item_links"] = len(item_links(html))
        # One level down: does a sale page carry its lots server-side?
        if sale_links:
            try:
                sale_html = self._fetch(urllib.parse.urljoin(_BASE, sale_links[0]))
                sub = self._analyze(sale_html, sale_links[0])
                sub["item_links"] = len(item_links(sale_html))
                sub["url"] = sale_links[0]
                out["first_sale_page"] = sub
            except Exception as exc:  # noqa: BLE001
                out["first_sale_page"] = {"error": f"{type(exc).__name__}"[:120]}
        return out

    def _try_endpoints(self, shell: dict, search_url: str) -> list[dict]:
        """Knock politely on every API-shaped door the shell references (plus the usual
        suspects) and record what answers. Status codes and top-level JSON keys only —
        enough to write a real client against, nothing more."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(search_url).query)
        q = (query.get("q") or ["furniture"])[0]
        candidates: list[str] = []
        # Rails answers the .json format suffix on ordinary routes — the single most
        # likely door on a sprockets/webpacker app, so it knocks first.
        candidates.append(f"{_BASE}/search.json?q={urllib.parse.quote(q)}")
        for u in shell.get("api_urls", []):
            if "{" in u or "$" in u:      # a route template needs params we don't have
                continue
            full = urllib.parse.urljoin(_BASE, u)
            # Endpoints that look like search/browse get the query attached.
            if any(w in full.lower() for w in ("search", "browse", "query", "item", "sale")):
                sep = "&" if "?" in full else "?"
                candidates.append(f"{full}{sep}q={urllib.parse.quote(q)}")
            else:
                candidates.append(full)
        for path in ("/api/v2/search", "/api/v3/search", "/api/search", "/api/v2/items",
                     "/api/items"):
            candidates.append(f"{_BASE}{path}?q={urllib.parse.quote(q)}")

        trials: list[dict] = []
        seen: set[str] = set()
        for url in candidates:
            if url in seen or len(trials) >= 14:
                continue
            seen.add(url)
            trial: dict = {"url": url}
            try:
                body = self._fetch(url)
            except urllib.error.HTTPError as exc:
                trial["status"] = exc.code
                trials.append(trial)
                continue
            except Exception as exc:  # noqa: BLE001
                trial["error"] = f"{type(exc).__name__}: {exc}"[:200]
                trials.append(trial)
                continue
            trial["status"] = 200
            trial["bytes"] = len(body)
            text = body.strip()
            if text[:1] in ("{", "["):
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    trial["shape"] = "json-ish but unparseable"
                else:
                    trial["shape"] = "json"
                    trial["top_level_keys"] = (
                        sorted(data)[:25] if isinstance(data, dict)
                        else f"array[{len(data)}]"
                    )
                    trial["harvested_items"] = len(harvest_json(data))
            else:
                trial["shape"] = "html"
            trials.append(trial)
        return trials

    @staticmethod
    def _analyze(html: str, url: str, *, captures=()) -> dict:
        items = parse_page(html, page_url=url, captures=captures)

        def coverage(attr) -> int:
            return sum(1 for i in items if getattr(i, attr) not in (None, "", []))

        return {
            "bytes": len(html),
            "jsonld_blocks": len(_JSONLD_RE.findall(html)),
            "embedded_json_blobs": len(_embedded_json_blobs(html)),
            "harvested_items": len(items),
            "field_coverage": {
                a: coverage(a)
                for a in ("title", "current_bid_cents", "bid_count", "ends_at",
                          "photo_urls", "url")
            },
            "parsed_by": sorted({i.parsed_by for i in items}),
            # Keys only, never values: enough to write a parser against, nothing private.
            "sample_raw_keys": sorted(items[0].raw)[:30] if items else [],
        }
