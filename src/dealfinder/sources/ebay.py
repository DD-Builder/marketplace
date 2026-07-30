"""eBay Browse API — real market prices to anchor the appraisal against.

Until now the appraiser answered "what is this worth restored?" from photographs and a
seller's description alone, with no market data at all. This supplies comparables.

Two honest limitations, both stated in the prompt so the model can weight them properly:

* **These are asking prices, not sold prices.** eBay decommissioned ``findCompletedItems``
  in February 2025, and its replacement (Marketplace Insights, the only official source of
  sold data) is a Limited Release that is not accepting new applications. Asking prices run
  optimistic — an anchor, not an answer.
* **The query is the seller's own title**, which is often poor ("Dresser $50 OBO"). A weak
  query yields weak comps; the model is told to discount them accordingly.

Free tier is ~5,000 calls/day, which is orders of magnitude more than a dozen appraisals a
day needs. Credentials are optional: with none configured this degrades to no comps at
all, which is exactly the behaviour that shipped before.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from dealfinder.logging import get_logger
from dealfinder.valuation.comparables import Comp

log = get_logger(__name__)

_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"

#: Words that make a Marketplace title useless as an eBay query. Sellers pack titles with
#: price, urgency and location noise; searching on it verbatim returns nothing useful.
_NOISE = re.compile(
    r"\$\s?\d[\d,.]*|\b(obo|o\.b\.o|firm|cash|porch|delivery|available|"
    r"must go|moving|priced to sell|no holds|first come|reduced|new price)\b|"
    # Trailing qualifiers travel with their noun; stripping "pickup" alone strands "only".
    r"\bpick ?up(\s+only)?\b|\bcash\s+only\b|\blocal\s+pick ?up\b",
    re.I,
)


def clean_query(title: str, *, max_words: int = 8) -> str:
    """Turn a Marketplace title into something worth sending to a search engine."""
    text = _NOISE.sub(" ", title or "")
    text = re.sub(r"[^\w\s-]", " ", text)
    words = [w for w in text.split() if len(w) > 1]
    return " ".join(words[:max_words]).strip()


def _money_to_cents(value) -> int | None:
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


class EbayBrowseComps:
    """Comparable *asking* prices from eBay's Browse API.

    Implements :class:`~dealfinder.valuation.comparables.PricingComparableSource`.
    """

    name = "ebay-browse"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        limit: int = 8,
        timeout: float = 20.0,
        category_ids: str = "",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.limit = limit
        self.timeout = timeout
        self.category_ids = category_ids
        self._token = ""
        self._token_expires = 0.0
        #: Total matches for the last query — a free measure of how much competing
        #: supply exists, which is a far better liquidity signal than a keyword list.
        self.last_total: int | None = None

    # --- auth ---------------------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires:
            return self._token
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": _SCOPE}
        ).encode()
        req = urllib.request.Request(
            _OAUTH_URL,
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        self._token = payload["access_token"]
        # Refresh a minute early rather than discovering expiry mid-run.
        self._token_expires = time.monotonic() + max(60, int(payload.get("expires_in", 7200)) - 60)
        return self._token

    # --- the seam -----------------------------------------------------------------------

    def get_comps(self, item_descriptor: str) -> list[Comp]:
        query = clean_query(item_descriptor)
        self.last_total = None
        if len(query) < 3:
            return []
        params = {
            "q": query,
            "limit": str(self.limit),
            # Used goods only: a new-in-box reproduction is not a comp for a 1962 credenza.
            "filter": "conditions:{USED|UNSPECIFIED}",
        }
        if self.category_ids:
            params["category_ids"] = self.category_ids
        url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            log.warning("ebay_search_failed", status=exc.code, query=query, detail=detail)
            return []
        except Exception as exc:  # noqa: BLE001 — comps are a bonus, never fatal
            log.warning("ebay_search_failed", query=query, error=str(exc)[:200])
            return []

        self.last_total = payload.get("total")
        now = datetime.now(timezone.utc)
        out: list[Comp] = []
        for item in payload.get("itemSummaries") or []:
            cents = _money_to_cents((item.get("price") or {}).get("value"))
            if cents is None:
                continue
            out.append(
                Comp(
                    source="eBay (asking)",
                    title=str(item.get("title", ""))[:160],
                    price_cents=cents,
                    is_sold=False,
                    condition=str(item.get("condition", "") or ""),
                    url=item.get("itemWebUrl"),
                    observed_at=now,
                )
            )
        log.info("ebay_comps", query=query, found=len(out), total=self.last_total)
        return out


def from_env() -> EbayBrowseComps | None:
    """Build a comps source from the environment, or None when unconfigured.

    Absent credentials are not an error — the appraiser simply works as it did before,
    estimating unaided.
    """
    import os

    cid = (os.getenv("EBAY_CLIENT_ID") or "").strip()
    secret = (os.getenv("EBAY_CLIENT_SECRET") or "").strip()
    if not (cid and secret):
        return None
    return EbayBrowseComps(cid, secret)
