# The Gavel

An auction watch for [Everything But The House](https://www.ebth.com). It tracks lots,
values them with AI, and works out the most you should bid before the endgame decides it
for you.

It runs on GitHub: **Actions** for compute, **Pages** for hosting, and your **Claude
subscription** for the valuations, driven through the Claude Code CLI so there is no
metered API bill.

> **Paused.** Nothing runs on a schedule. Every workflow is `workflow_dispatch` only, so
> the tracker does nothing at all unless you start it by hand.

## ⚠️ Read this first

- **Check EBTH's terms before you run this.** It reads the site by running the site's own
  application in a headless browser and keeping the data the app fetches for itself. That
  is automated access, however politely it is done, and whether it is permitted is
  EBTH's decision to state and yours to comply with — not something this README can settle
  for you. Public visibility is not the test.
- **Valuations are estimates from photos and text.** Verify condition in person before
  bidding real money.

## How it works

```
discover lots by category  ->  refresh bids on everything still in results
                           ->  pre-screen (free, keyword + photo signal)
                           ->  one AI appraisal per lot, ever
                           ->  max bid, projected close, stance
                           ->  a static page committed to docs/auctions/
```

Auctions invert the usual problem. Nobody names a price; the price finds itself, and most
of the money arrives in the closing hours. So the discipline that makes money is deciding
your ceiling *before* the endgame and never chasing past it.

**Value the lot once.** An appraisal answers "what is this and what is it worth", which
does not change when someone outbids you. Appraisals are stored and the *guidance* is
recomputed every run, so a rising bid re-advises for free.

**Discovery is by EBTH's own categories.** `category_slug` is the parameter that actually
narrows a browse — measured, because `category_id`, which the site's own filter block
advertises, leaves the result count untouched. Lots are tagged with the vertical of the
category that found them and appraised against that vertical's rules, so a sterling ring
is not judged by furniture's keyword list.

## What it tells you

- **Your max bid** — worked back from the appraisal: resale value, minus your margin,
  freight or the round trip to collect, and the buyer's premium riding the hammer.
- **Projected close** — the endgame multiplier says what T-24h prices become by the hammer.
  It starts as a prior and is learned from this catalogue's own closed lots.
- **Stance** — `BID LATE`, `WATCH` (never bid early; it only feeds the price), `OUTPRICED`,
  `PASS`.

### The room gets a vote

A lot with twenty-six bids and minutes left has been priced by people who can see it, and
an appraisal that disagrees is likelier to be wrong than they are. So the ceiling is pulled
toward the live price in proportion to how much bidding has actually happened, capped so a
well-evidenced valuation always keeps some weight. A quiet lot — the undiscovered one worth
hunting — is untouched.

This exists because a painting was valued at $1,200 and given a $690 maximum bid while 26
bidders had it at $250 and the artist's work realises $111–$401.

### Art is valued from realised sales

The model's job is narrowed to *finding comparable sales*; the arithmetic happens in
`valuation/artcomps.py`, which returns a weighted median with a low/high band rather than a
single asserted number. Weighting is by medium (an original and a giclée of it are
different objects with the same title), area on a log scale, and recency. Asking prices
count for little and can never lift an estimate above anything actually realised. Too
little evidence returns nothing rather than a confident guess.

## Setup

1. **Fork or clone**, then enable Pages: *Settings → Pages → Deploy from branch → `docs/`*.
2. **Secret** (*Settings → Secrets and variables → Actions*): `CLAUDE_CODE_OAUTH_TOKEN`,
   from `claude setup-token`. This is what makes the valuations free.
3. **Variables** (*Variables* tab) — all optional:

   | Variable | Default | What it does |
   |---|---|---|
   | `APPRAISE_MODEL` | `claude-sonnet-5` | Model for the valuation call. |
   | `MAX_AUCTION_APPRAISALS` | 20 | Cap on AI valuations per run. |
   | `EBTH_MAX_WATCH` | 150 | Watchlist size — the real ceiling on how many lots get valued. |
   | `EBTH_CATEGORIES` | the priceable set | EBTH categories to trawl, by name, slug or id. |
   | `EBTH_DECIDE_WITHIN_DAYS` | 2 | Only value lots closing inside this window. |
   | `EBTH_PREMIUM_PCT` | 0.15 | Buyer's premium. Check the terms of the sale. |
   | `LOT_SHIP_CENTS` | 3500 | Flat parcel cost for a shippable lot. |
   | `HOURLY_RATE_CENTS` | 3000 | What your time is worth, for the collection trip. |

   Two optional secrets unlock eBay comparables: `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`
   (their official Browse API). Without them the appraiser estimates unaided.

4. **Run it**: *Actions → Auction watch → Run workflow*. Nothing runs on a schedule.

## Running it locally

```bash
pip install -e '.[dev]'
python -m pytest              # 253 tests, no network, no spend
```

## Layout

| Path | |
|---|---|
| `run_auctions.py` | the run: discover → snapshot → appraise → advise → publish |
| `runtime.py` | shared plumbing: env, credentials, photo fetch, exit reasons |
| `sources/ebth.py` | layered parsing + the CI structure probe |
| `sources/ebth_browser.py` | headless-Chromium fetcher |
| `auctions/catalog.py` | lot catalogue with full bid history |
| `auctions/bidding.py` | max bid, endgame projection, the market anchor, stance |
| `auctions/logistics.py` | parcel rate vs the round trip to collect |
| `auctions/categories.py` | EBTH's category tree and sort presets |
| `auctions/board.py` | the Gavel page |
| `valuation/artcomps.py` | comp-weighted median over realised sales, with a band |
| `appraiser.py` | provider seam: subscription CLI, metered API, or your own |
| `prescreen.py` / `verticals.py` | the free junk filter and its per-category knowledge |
| `engine.py` / `ranking.py` / `resale.py` | scoring and resale pricing |
| `authenticity.py` | look-alike and knockoff detection |
| `pieces.py` | your books: costs, sales, realised hourly wage |
| `sources/ebay.py` | official Browse API comps |

## Honest limits

- **Art comps depend on the appraiser finding real sales.** For an unlisted maker it
  returns nothing and the lot is valued thinly. That is the honest answer, but it is a gap.
- **The price-history chart plots only closes this tracker has watched.** There is no
  public feed of long-run realised prices, so it fills in from your own observations rather
  than drawing a trend line from data nobody has.
- **The endgame multiplier is mostly prior until lots have closed under it.** The page says
  how many observations are behind it.
- **Dealer listings (1stDibs and similar) are treated as heavily-discounted ceilings**, not
  comparables, because they systematically overprice.
