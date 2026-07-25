"""JSON-first extraction of Facebook Marketplace listings.

Facebook embeds listing data as GraphQL-style JSON inside inline ``<script>`` tags.
Parsing that JSON is far more stable across Facebook's frequent DOM reshuffles than
CSS-selector scraping, so this module walks the embedded JSON and pulls listings out of
it by their GraphQL field names (``marketplace_listing_title``, ``listing_price``, ...).

Required fields absent -> :class:`LayoutChangedError`, so silent breakage surfaces
loudly and a saved snapshot can be used to repair the parser offline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from dealfinder.core.schemas import RawListing, RawPhoto
from dealfinder.scraper.errors import LayoutChangedError

_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _iter_json_blobs(html: str) -> Iterator[Any]:
    for match in _SCRIPT_RE.finditer(html):
        blob = match.group(1).strip()
        if not blob:
            continue
        try:
            yield json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue


_MAX_DEPTH = 60  # Facebook payloads are deep; guard against runaway recursion (B10).


def _walk(obj: Any, depth: int = 0) -> Iterator[dict]:
    """Yield every dict nested anywhere inside ``obj``, bounded by a depth limit."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, depth + 1)


def _is_listing_node(node: dict) -> bool:
    # Require the marketplace title so generic id-bearing nodes aren't mistaken for
    # listings (finding B10). Real Marketplace cards carry this field.
    return "marketplace_listing_title" in node and _listing_id(node) is not None


def _price_to_cents(price: Any) -> int | None:
    """Facebook's ``listing_price`` -> integer cents."""
    if not isinstance(price, dict):
        return None
    # amount_with_offset is already in the minor unit (cents) as a string.
    offset = price.get("amount_with_offset")
    if offset is not None:
        try:
            return int(str(offset))
        except (TypeError, ValueError):
            pass
    amount = price.get("amount")
    if amount is not None:
        try:
            return int(round(float(str(amount).replace(",", "")) * 100))
        except (TypeError, ValueError):
            pass
    return None


def _currency(price: Any) -> str:
    if isinstance(price, dict):
        return price.get("currency") or "USD"
    return "USD"


def _extract_photos(node: dict) -> list[RawPhoto]:
    photos: list[RawPhoto] = []
    seen: set[str] = set()

    def add(uri: str | None) -> None:
        if uri and uri not in seen:
            seen.add(uri)
            photos.append(RawPhoto(remote_url=uri, position=len(photos)))

    primary = node.get("primary_listing_photo")
    if isinstance(primary, dict):
        img = primary.get("image") or {}
        if isinstance(img, dict):
            add(img.get("uri"))

    listing_photos = node.get("listing_photos")
    if isinstance(listing_photos, list):
        for p in listing_photos:
            if isinstance(p, dict):
                img = p.get("image") or {}
                if isinstance(img, dict):
                    add(img.get("uri"))
    return photos


def _text_of(node: dict, key: str) -> str:
    value = node.get(key)
    if isinstance(value, dict):
        return str(value.get("text") or "")
    if isinstance(value, str):
        return value
    return ""


def _listing_id(node: dict) -> str | None:
    for key in ("id", "story_key", "legacy_id"):
        val = node.get(key)
        if val:
            return str(val)
    return None


def parse_search_ids(html: str) -> list[str]:
    """Return the Facebook listing IDs present on a search-results page."""
    ids: list[str] = []
    seen: set[str] = set()
    for blob in _iter_json_blobs(html):
        for node in _walk(blob):
            if _is_listing_node(node):
                fb_id = _listing_id(node)
                if fb_id and fb_id not in seen:
                    seen.add(fb_id)
                    ids.append(fb_id)
    return ids


def _seller(node: dict) -> tuple[str, str | None]:
    seller = node.get("marketplace_listing_seller") or node.get("story_seller")
    if isinstance(seller, dict):
        name = str(seller.get("name") or "")
        sid = seller.get("id")
        url = f"https://www.facebook.com/{sid}" if sid else None
        return name, url
    return "", None


def parse_listing_detail(html: str, url: str = "") -> RawListing:
    """Parse a listing detail page into a :class:`RawListing`.

    Raises :class:`LayoutChangedError` if no listing node with a title can be found.
    """
    candidate: dict | None = None
    for blob in _iter_json_blobs(html):
        for node in _walk(blob):
            if "marketplace_listing_title" in node and _listing_id(node):
                # Prefer the richest node (one that also carries a description/photos).
                if candidate is None or len(node) > len(candidate):
                    candidate = node

    if candidate is None:
        raise LayoutChangedError(
            "no marketplace listing node found in embedded JSON",
            snapshot=html[:20000],
        )

    fb_id = _listing_id(candidate)
    if not fb_id:
        raise LayoutChangedError("listing node missing id", snapshot=html[:20000])

    description = (
        _text_of(candidate, "redacted_description")
        or _text_of(candidate, "description")
    )
    location = (
        _text_of(candidate, "location_text")
        or _text_of(candidate, "location_vanity_or_id")
    )
    seller_name, seller_url = _seller(candidate)
    price = candidate.get("listing_price")

    return RawListing(
        fb_listing_id=fb_id,
        title=str(candidate.get("marketplace_listing_title") or ""),
        description=description,
        asking_price_cents=_price_to_cents(price),
        currency=_currency(price),
        location_text=location,
        seller_name=seller_name,
        seller_profile_url=seller_url,
        url=url or f"https://www.facebook.com/marketplace/item/{fb_id}/",
        photos=_extract_photos(candidate),
        raw_json=candidate,
    )
