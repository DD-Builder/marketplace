# The Bench

Valuation and resale maths for second-hand furniture, art and collectibles. You supply the
item — title, description, photos, asking price — and it appraises it, scores the deal,
prices the resale, and works out the most you can bid at auction before the flip stops
paying.

It is a **library**, not a crawler. It has no data acquisition of any kind.

## What this project no longer does

This started as an automated deal finder: it bought Facebook Marketplace listings from a
third-party data vendor and read an auction house's lots by driving a headless browser
against their single-page app.

Both are gone. Not disabled, not behind a flag — **removed**, along with every line of code
that reached them and every byte of data they produced.

The reason is simple. Meta's terms prohibit collecting Marketplace data by automated means,
and a vendor selling that data does not change what was collected or how. The auction house
likewise does not permit automated access to its site, and driving its app to capture the
JSON it fetches for itself is exactly that, however politely it is done. Whether the data is
publicly visible is not the test; whether the terms permit taking it programmatically is.

Removed in full:

| Removed | Why |
|---|---|
| The third-party Marketplace data vendor client and its two-stage fetch | Buying automatically-collected Marketplace data |
| The auction-house source and its headless-browser fetcher | Automated access to a site whose terms forbid it |
| Both pipeline runners, the published boards, and the site they were served on | Existed only to drive and display the above |
| ~44 MB of committed listings, lot histories and downloaded photos | The product of that collection |
| Every workflow that ran any of it on a schedule | Nothing should collect anything on a timer |

The one network client that remains is **eBay's official Browse API**, used with your own
credentials under eBay's developer terms — a sanctioned interface, which is the whole
distinction.

> **Note on git history.** Deleting these files removes them from the working tree and from
> every future checkout, but earlier commits still contain them. If that matters for your
> purposes, the history needs rewriting (`git filter-repo`) and a force-push; that is
> destructive and has not been done here.

## What it does now

```
your items  ->  pre-screen (free, keyword + photo signal)
            ->  AI appraisal of the survivors, capped
            ->  scoring, authenticity check, resale pricing
            ->  for auctions: max bid, projected close, stance
```

Everything is a pure function over data you already hold. No network calls except the
appraiser (your Claude subscription) and, optionally, eBay comps.

### Valuation

`run_valuation(listings, provider=...)` takes `RawListing` objects and returns ranked,
priced, scored pieces. Appraisals are valued once and *scored* every time, so a price change
re-ranks for free.

**Art is valued from realised sales, not from a single asserted number.** The model's job is
narrowed to finding comparable sales; the arithmetic happens in `valuation/artcomps.py`,
which returns a weighted median with a low/high band. Weighting is by medium (an original
and a giclée of it are different objects with the same title), area on a log scale, and
recency. Asking prices count for little and can never lift an estimate above anything
actually realised. Too little evidence returns nothing rather than a confident guess.

This exists because the old point-estimate approach valued a painting at $1,200 whose
artist's work realises $111–$401, and set a maximum bid of $690 against it.

### Resale pricing

The headline sell target is what a piece fetches regionally, independent of you. Your hours
and materials produce a *second* number — profit and effective hourly wage. Folding your
weekend into the ask is how a $450 table gets listed at $978 and never sells.

### Auction maths

`auctions/bidding.py` turns an appraisal into a ceiling: resale value, minus your margin,
freight or the round trip to collect, and the buyer's premium riding the hammer. Decided
before the endgame so the endgame cannot decide it for you.

It also anchors to the room. A lot with twenty-six bids and minutes left has been priced by
people who can see it, and an appraisal that disagrees is likelier to be wrong than they
are — so the ceiling is pulled toward the live price in proportion to how much bidding has
actually happened. A quiet lot keeps its full appraised value.

Describe the lot yourself with `auctions.lot.Lot`; nothing fetches it for you.

## Install and test

```bash
pip install -e '.[dev]'
python -m pytest          # 177 tests, no network, no spend
```

Optional extras: `.[api]` for the metered Anthropic API path (the default uses your Claude
subscription through the Claude Code CLI instead, so there is no API bill).

Two optional secrets unlock eBay comparables: `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`
(free Browse API). Without them the appraiser estimates unaided.

## Layout

| Path | |
|---|---|
| `engine.py` | `run_valuation` / `evaluate_piece` — the funnel over items you supply |
| `selection.py` | cost control: dedup, seen-diff, valuation cap |
| `prescreen.py` / `verticals.py` | the free junk filter and its per-category knowledge |
| `appraiser.py` | provider seam: subscription CLI, metered API, or your own |
| `valuation/artcomps.py` | comp-weighted median over realised sales, with a band |
| `ranking.py` | priority, liquidity, heat, badges |
| `authenticity.py` | look-alike and knockoff detection |
| `resale.py` | market price and your-numbers pricing |
| `restoration.py` | bounds on the model's cost/effort estimate, from published survey data |
| `auctions/lot.py` | a lot you describe yourself |
| `auctions/bidding.py` | max bid, endgame projection, stance |
| `auctions/logistics.py` | parcel rate vs the round trip to collect |
| `pieces.py` | your books: costs, sales, realised hourly wage |
| `negotiation/` | posture and message drafting — you read, edit and send |
| `sources/ebay.py` | official Browse API comps |

## Honest limits

- **Valuations are estimates from photos and text.** Verify condition in person before
  handing over money.
- **Nothing is ever sent to a seller.** The app drafts messages; you send them yourself.
- **Art comps depend on the appraiser finding real sales.** For an unlisted maker it
  returns nothing, and the lot is valued thinly or not at all. That is the honest answer,
  but it is a gap.
- **There is no data source.** Getting items in front of this is now your problem, and any
  source you add is your responsibility to license or collect lawfully.
- **Dealer listings (1stDibs and similar) are treated as heavily-discounted ceilings**, not
  comparables, because they systematically overprice.
