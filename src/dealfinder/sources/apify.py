"""Apify Facebook-Marketplace source: pull listings over the REST API — no copy-paste.

Two entry points:

* :func:`records_to_listings` — the *adapter*, verified against a real 91-listing export.
  The actor nests almost everything (price/description/location are objects, photos are
  doubly nested ``{image:{uri}}``), so this maps the real field names into ``RawListing``.
* :func:`run_and_fetch` — trigger the actor and get its dataset back in one authenticated
  call (``run-sync-get-dataset-items``). This is what makes the pipeline hands-off: the
  engine calls Apify itself on a schedule; the JSON never touches a human.

Network calls use urllib (stdlib) so the package pulls in no HTTP dependency. Apify's host
is unreachable from some sandboxes — that's an environment limit, not a code one; the
adapter is exercised by tests on a captured record either way.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Iterable

from dealfinder.core.schemas import RawListing, RawPhoto

_API_BASE = "https://api.apify.com/v2"
_DEFAULT_ACTOR = "apify~facebook-marketplace-scraper"

# --- Adapter (verified against a real apify/facebook-marketplace-scraper export) ---------

_TITLE_KEYS = ["listingTitle", "title", "name", "marketplaceListingTitle"]
_PRICE_KEYS = ["listingPrice", "price", "amount", "priceAmount"]
_DESC_KEYS = ["description", "redactedDescription", "desc"]
_LOC_KEYS = ["locationText", "location", "city", "locationName"]
_URL_KEYS = ["itemUrl", "listingUrl", "url", "link", "facebookUrl"]
_ID_KEYS = ["id", "listingId", "itemId", "fbid"]
_IMG_KEYS = ["listingPhotos", "images", "photos", "imageUrls", "primaryPhotoUrls"]


def _first(rec: dict, keys: list[str]) -> Any:
    for k in keys:
        if k in rec and rec[k] not in (None, "", []):
            return rec[k]
    return None


def _text(v: Any) -> str:
    """Flatten a field that may be a bare string or a {'text': ...} / {'label': ...} object."""
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("text") or v.get("label") or "")
    return str(v)


def _to_cents(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, dict):
        raw = v.get("amount_with_offset_in_currency") or v.get("amount_with_offset")
        if raw:
            try:
                return int(str(raw))
            except ValueError:
                pass
        v = v.get("amount")  # e.g. "80.00" (dollars)
    try:
        return int(round(float(str(v).replace("$", "").replace(",", "")) * 100))
    except (TypeError, ValueError):
        return None


def _images(rec: dict) -> list[str]:
    val = _first(rec, _IMG_KEYS) or []
    urls: list[str] = []
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                img = item.get("image") if isinstance(item.get("image"), dict) else item
                u = img.get("uri") or img.get("url") or img.get("src")
                if u:
                    urls.append(u)
    if not urls:
        prim = rec.get("primaryListingPhoto") or {}
        u = prim.get("photo_image_url") or prim.get("uri")
        if u:
            urls.append(u)
    return urls


def _parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def record_to_listing(
    rec: dict, idx: int = 0, *, detail_fetched: bool | None = None
) -> RawListing:
    """Map one actor record to a :class:`RawListing`.

    ``detail_fetched=None`` infers provenance from the payload — a description or a photo
    gallery can only have come from a detail page. That keeps ``--from-json`` exports and
    the measurement pilot correct without their callers knowing about the two-stage scrape.
    """
    fb_id = str(_first(rec, _ID_KEYS) or f"row-{idx}")
    imgs = _images(rec)
    price = _first(rec, _PRICE_KEYS)
    prev = rec.get("strikethroughPrice")  # a present strikethrough = an already-dropped price
    raw = dict(rec)
    if prev:
        raw["_was_price_cents"] = _to_cents(prev)

    desc = _text(_first(rec, _DESC_KEYS))
    gallery = rec.get("listingPhotos") or []
    inferred = bool(desc) or (isinstance(gallery, list) and len(gallery) > 0)

    return RawListing(
        fb_listing_id=fb_id,
        title=_text(_first(rec, _TITLE_KEYS)),
        description=desc,
        asking_price_cents=_to_cents(price),
        location_text=_text(_first(rec, _LOC_KEYS)),
        url=_text(_first(rec, _URL_KEYS)),
        photos=[RawPhoto(remote_url=u, position=i) for i, u in enumerate(imgs)],
        raw_json=raw,
        detail_fetched=inferred if detail_fetched is None else detail_fetched,
        is_sold=rec.get("isSold"),
        is_live=rec.get("isLive"),
        posted_at=_parse_ts(rec.get("timestamp")),
    )


def records_to_listings(
    records: Iterable[dict], *, detail_fetched: bool | None = None
) -> list[RawListing]:
    return [
        record_to_listing(r, i, detail_fetched=detail_fetched)
        for i, r in enumerate(records)
    ]


# --- REST client ------------------------------------------------------------------------

class ApifyError(RuntimeError):
    """An Apify API call failed, carrying whatever explanation Apify returned."""


def _raise_with_body(exc: urllib.error.HTTPError, what: str) -> None:
    """Re-raise an HTTPError with Apify's own error text, which explains the cause.

    A bare 'HTTP Error 400: Bad Request' is useless — Apify puts the actual reason
    (bad input, exhausted credit, actor failure, rate limit) in the response body.
    """
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        body = ""
    detail = body
    try:
        parsed = json.loads(body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            detail = f"{err.get('type', '')}: {err.get('message', '')}".strip(": ")
    except json.JSONDecodeError:
        pass
    raise ApifyError(f"{what} failed with HTTP {exc.code}. Apify said: {detail[:500]}") from exc


def _post_json(url: str, payload: dict, timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_with_body(exc, "Apify actor run")


def _get_json(url: str, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_with_body(exc, "Apify dataset fetch")


def run_and_fetch(
    run_input: dict,
    *,
    token: str,
    actor: str = _DEFAULT_ACTOR,
    timeout: float = 300.0,
) -> list[RawListing]:
    """Trigger the actor and return its dataset items as RawListings, in one call.

    ``run_input`` is the actor's input JSON, e.g.::

        {"startUrls": [{"url": "https://www.facebook.com/marketplace/lexington/search/?query=dresser"}],
         "resultsLimit": 200, "includeListingDetails": True}

    Requires an Apify API token. Raises on network/HTTP error — the caller decides retry.
    """
    q = urllib.parse.urlencode({"token": token})
    url = f"{_API_BASE}/acts/{actor}/run-sync-get-dataset-items?{q}"
    records = _post_json(url, run_input, timeout)
    if isinstance(records, dict):  # some responses wrap items
        records = records.get("items") or records.get("data") or []
    return records_to_listings(records)


def fetch_dataset(dataset_id: str, *, token: str, timeout: float = 120.0) -> list[RawListing]:
    """Fetch an already-produced dataset by id (e.g. to re-process a prior run)."""
    q = urllib.parse.urlencode({"token": token, "format": "json", "clean": "true"})
    url = f"{_API_BASE}/datasets/{dataset_id}/items?{q}"
    records = _get_json(url, timeout)
    return records_to_listings(records)


def list_runs(
    *, token: str, actor: str | None = None, limit: int = 20, timeout: float = 60.0
) -> list[dict]:
    """Your recent actor runs, newest first, each with the dataset it produced.

    The point of this is recovery. A run you already paid for keeps its results in an
    Apify dataset, and *reading* that dataset is not another run — no actor starts, no
    compute is billed. So a scrape whose output got lost (or which failed downstream, as
    the first real run here did) can be pulled back for nothing.

    Datasets are not kept forever — unnamed ones expire, typically within days on the free
    plan — so recovery is worth doing promptly, and an expired dataset simply returns no
    items rather than an error.
    """
    q = urllib.parse.urlencode({"token": token, "desc": "true", "limit": limit})
    path = f"acts/{actor}/runs" if actor else "actor-runs"
    payload = _get_json(f"{_API_BASE}/{path}?{q}", timeout)
    items = (payload or {}).get("data", {}).get("items", []) if isinstance(payload, dict) else []
    return [
        {
            "id": r.get("id"),
            "actor_id": r.get("actId"),
            "status": r.get("status"),
            "started_at": r.get("startedAt"),
            "finished_at": r.get("finishedAt"),
            "dataset_id": r.get("defaultDatasetId"),
        }
        for r in items
        if r.get("defaultDatasetId")
    ]


def recover_runs(
    *, token: str, actor: str | None = None, limit: int = 20, timeout: float = 120.0
) -> tuple[list[RawListing], list[dict]]:
    """Pull back every listing from your recent runs. Returns (listings, per-run report).

    Deliberately tolerant: one expired or unreadable dataset must not stop the others
    from being recovered.
    """
    listings: list[RawListing] = []
    report: list[dict] = []
    for run in list_runs(token=token, actor=actor, limit=limit):
        try:
            got = fetch_dataset(run["dataset_id"], token=token, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — an expired dataset is normal, not fatal
            report.append({**run, "recovered": 0, "error": str(exc)[:200]})
            continue
        listings += got
        report.append({**run, "recovered": len(got), "error": ""})
    return listings, report
