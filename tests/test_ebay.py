"""The eBay Browse comps source.

This is the first time the appraiser has any market data at all — before it, the model
answered "what's this worth restored?" from photos and a seller's blurb, unaided. It is
also code that will never execute in CI (no credentials), so it needs direct tests.
"""

from __future__ import annotations

import io
import json

import pytest

from dealfinder.appraiser import comps_prompt_block
from dealfinder.sources.ebay import EbayBrowseComps, clean_query, from_env
from dealfinder.valuation.comparables import Comp, NoopComparables


# --- query cleaning ------------------------------------------------------------------------

def test_seller_noise_is_stripped_before_searching():
    """Marketplace titles are packed with price and urgency noise. Searched verbatim they
    return nothing useful, so the comps would be silently empty."""
    assert clean_query("Broyhill Brasilia credenza $220 OBO must go!") == \
        "Broyhill Brasilia credenza"
    assert clean_query("MCM Dresser - $50 firm, pickup only") == "MCM Dresser"
    assert "1200" not in clean_query("Danish teak table $1,200 cash")


def test_a_query_too_thin_to_be_useful_is_not_sent():
    """Better no comps than comps for the wrong object entirely."""
    source = EbayBrowseComps("id", "secret")
    assert source.get_comps("$50") == []
    assert source.get_comps("") == []


# --- the API round trip ---------------------------------------------------------------------

class _FakeHTTP:
    """Stands in for urlopen, returning canned OAuth and search payloads."""

    def __init__(self, search_payload, *, fail_status=None):
        self.search_payload = search_payload
        self.fail_status = fail_status
        self.requests = []

    def __call__(self, req, timeout=0):
        url = req.full_url
        self.requests.append(url)
        if "identity/v1/oauth2/token" in url:
            body = {"access_token": "tok-123", "expires_in": 7200}
        else:
            if self.fail_status:
                import urllib.error
                raise urllib.error.HTTPError(url, self.fail_status, "nope", {}, io.BytesIO(b"{}"))
            body = self.search_payload
        resp = io.BytesIO(json.dumps(body).encode())
        resp.__enter__ = lambda s=resp: s
        resp.__exit__ = lambda *a: False
        return resp


_SEARCH = {
    "total": 7,
    "itemSummaries": [
        {"title": "Broyhill Brasilia walnut credenza", "price": {"value": "1450.00"},
         "condition": "Used", "itemWebUrl": "https://ebay.com/itm/1"},
        {"title": "Brasilia style sideboard", "price": {"value": "890.50"},
         "condition": "Used", "itemWebUrl": "https://ebay.com/itm/2"},
        {"title": "broken listing with no price"},
    ],
}


def test_comps_come_back_as_asking_prices_with_the_supply_count(monkeypatch):
    import urllib.request

    http = _FakeHTTP(_SEARCH)
    monkeypatch.setattr(urllib.request, "urlopen", http)

    source = EbayBrowseComps("id", "secret")
    comps = source.get_comps("Broyhill Brasilia credenza $220")

    assert [c.price_cents for c in comps] == [145000, 89050]   # the priceless row is dropped
    assert all(c.is_sold is False for c in comps), "Browse returns asks, never sold prices"
    assert comps[0].source == "eBay (asking)"
    assert source.last_total == 7                              # free competition signal
    # The noisy "$220" never reached eBay.
    assert "220" not in http.requests[-1]


def test_the_oauth_token_is_fetched_once_and_reused(monkeypatch):
    import urllib.request

    http = _FakeHTTP(_SEARCH)
    monkeypatch.setattr(urllib.request, "urlopen", http)

    source = EbayBrowseComps("id", "secret")
    source.get_comps("walnut credenza")
    source.get_comps("teak dining table")

    tokens = [u for u in http.requests if "oauth2/token" in u]
    assert len(tokens) == 1, "a cached token should survive between searches"


def test_an_ebay_outage_costs_comps_not_the_run(monkeypatch):
    """Comps are a bonus. A 500 from eBay must degrade the estimate, never lose the piece."""
    import urllib.request

    http = _FakeHTTP(_SEARCH, fail_status=500)
    monkeypatch.setattr(urllib.request, "urlopen", http)

    source = EbayBrowseComps("id", "secret")
    assert source.get_comps("walnut credenza") == []
    assert source.last_total is None


# --- configuration --------------------------------------------------------------------------

def test_absent_credentials_degrade_to_no_comps(monkeypatch):
    """The behaviour that shipped before: the model estimates unaided rather than erroring."""
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    assert from_env() is None

    from dealfinder.valuation.comparables import default_source
    assert isinstance(default_source(), NoopComparables)

    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    assert isinstance(from_env(), EbayBrowseComps)


# --- how the prompt describes them -----------------------------------------------------------

def test_the_prompt_never_lets_an_asking_price_pass_as_a_sale():
    """The single most dangerous failure here: anchoring restored value to what hopeful
    sellers want rather than what buyers paid, which inflates every estimate."""
    block = comps_prompt_block([
        Comp(source="eBay (asking)", title="Walnut credenza", price_cents=145000),
        Comp(source="auction", title="Walnut credenza, sold", price_cents=62000, is_sold=True),
    ])
    assert "ASKING" in block and "NOT sold" in block
    assert "SOLD prices (what buyers actually paid)" in block
    assert "soft ceiling" in block
    # And it warns that a keyword search returns near-misses.
    assert "wrong item" in block


def test_no_comps_adds_nothing_to_the_prompt():
    assert comps_prompt_block([]) == ""
