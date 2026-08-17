"""EBTH (Everything But The House) — the auction-house source.

Marketplace listings have a sticker price; EBTH lots have a *price discovery process*:
bids sit low for days, then most of the real money arrives in the closing hours. That
changes what a source must return — not "what does it cost" but "where is the bidding
now, how many bids, and when does it end" — and the auction pipeline built on top of
this (:mod:`dealfinder.auctions`) is what turns those into a max-bid recommendation.

A structural honesty note: this module was written *blind*. The development environment
cannot reach ebth.com (egress-filtered), while the GitHub Actions runners that actually
execute the pipeline can. So instead of hard-coding one page shape and hoping, extraction
is layered from most- to least-durable:

1. **JSON-LD** (``<script type="application/ld+json">``) — schema.org Product/Offer
   markup that commerce sites ship for SEO and rarely break;
2. **any embedded JSON state** (``__NEXT_DATA__``, ``window.__INITIAL_STATE__``, plain
   application/json scripts) — walked shape-agnostically for dicts that *look like*
   auction lots (an id + a bid-ish or end-time-ish field), so a framework migration on
   their side degrades coverage rather than zeroing it;
3. **HTML regexes** for the handful of fields worth a last-ditch guess.

``probe()`` fetches the configured pages and reports which layers fired and what field
coverage they achieved — run it in CI (``run_auctions --probe``) and read the committed
report to see what the site actually serves, then tighten the parsers from evidence.
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
_BID_KEYS = (
    "currentbidcents", "currentbid", "currentbidamount", "highbid", "highestbid",
    "winningbid", "currentprice", "salesprice", "price",
)
_COUNT_KEYS = ("bidcount", "bidscount", "numberofbids", "totalbids", "bids")
_END_KEYS = (
    "endsat", "endat", "endtime", "endingat", "auctionend", "enddate", "endson",
    "saleendsat", "availabilityends", "scheduledendtime",
)
_ID_KEYS = ("itemid", "id", "uuid", "slug")
_TITLE_KEYS = ("title", "name", "shortdescription")
_IMAGE_KEYS = ("imageurl", "image", "images", "photos", "mainimage", "primaryimage", "heroimage")
_ENDED_KEYS = ("isended", "ended", "isclosed", "closed", "iscomplete", "issold")


def _norm_key(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


def _first(d: dict, aliases: tuple[str, ...]):
    """The first alias present, returned with its ORIGINAL key (unit hints live there)."""
    normed = {_norm_key(k): (k, v) for k, v in d.items() if not isinstance(v, dict)}
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
        _, image_val = _first(d, _IMAGE_KEYS)
        _, ended_val = _first(d, _ENDED_KEYS)
        _, url_val = _first(d, ("url", "path", "href", "itemurl", "webpath"))

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
            is_ended=bool(ended_val) if ended_val is not None else None,
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


def parse_page(html: str, *, page_url: str = "") -> list[AuctionItem]:
    """All layers over one page, best record per id."""
    merged: dict[str, AuctionItem] = {}
    harvested = harvest_jsonld(html, page_url=page_url)
    for blob in _embedded_json_blobs(html):
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
                 "next", "nuxt", "webpack", "vite", "react", "turbo")


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

class EbthClient:
    """Polite fetch + parse against ebth.com. All knobs overridable for tests."""

    def __init__(self, *, timeout: float = 25.0, delay: float = _POLITE_DELAY,
                 fetch=None) -> None:
        self.timeout = timeout
        self.delay = delay
        self._fetch = fetch or self._http_get
        self._last_request = 0.0

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

    def search(self, url: str, *, follow_items: int = 0) -> list[AuctionItem]:
        """Harvest a search/browse page; optionally fetch the first N item pages whose
        grid records came back thin (no end time — useless to an auction tracker)."""
        html = self._fetch(url)
        items = parse_page(html, page_url=url)
        by_id = {i.item_id: i for i in items}
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
        html = self._fetch(url)
        items = parse_page(html, page_url=url)
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
                html = self._fetch(url)
            except Exception as exc:  # noqa: BLE001 — the report IS the error channel
                page["error"] = f"{type(exc).__name__}: {exc}"[:300]
                report["pages"].append(page)
                continue
            page.update(self._analyze(html, url))
            links = item_links(html)
            page["item_links"] = len(links)
            if links and not first_item_url:
                first_item_url = links[0]
            if page.get("harvested_items", 0) == 0:
                # An app shell. The lots arrive over XHR after JavaScript runs, so the
                # useful evidence is which endpoints the shell's code references.
                page["shell"] = analyze_shell(html)
                shell_html = shell_html or html
            report["pages"].append(page)
        if first_item_url:
            page = {"url": first_item_url, "kind": "item"}
            try:
                html = self._fetch(first_item_url)
                page.update(self._analyze(html, first_item_url))
            except Exception as exc:  # noqa: BLE001
                page["error"] = f"{type(exc).__name__}: {exc}"[:300]
            report["pages"].append(page)
        if shell_html and search_urls:
            report["endpoint_trials"] = self._try_endpoints(
                analyze_shell(shell_html), search_urls[0]
            )
        return report

    def _try_endpoints(self, shell: dict, search_url: str) -> list[dict]:
        """Knock politely on every API-shaped door the shell references (plus the usual
        suspects) and record what answers. Status codes and top-level JSON keys only —
        enough to write a real client against, nothing more."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(search_url).query)
        q = (query.get("q") or ["furniture"])[0]
        candidates: list[str] = []
        for u in shell.get("api_urls", []):
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
            if url in seen or len(trials) >= 10:
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
    def _analyze(html: str, url: str) -> dict:
        items = parse_page(html, page_url=url)

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
