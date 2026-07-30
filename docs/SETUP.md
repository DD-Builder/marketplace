# Setting this up from an iPad — free

Everything runs on GitHub's free compute and publishes to a free GitHub Pages site.
Valuations run on **your Claude subscription**, not a metered API bill.

Running total: **$0/month** for hosting and AI. The only spend is Apify scraping, which
fits inside its free monthly credit at the default caps below.

You do this once. After that, running the app is: open the board → tap **Scrape now**
(or let the daily schedule do it).

---

## 1. Mint a Claude token (so valuations bill to your subscription)

`claude setup-token` is a **terminal command** (Claude Code CLI). No computer handy? Use
GitHub Codespaces from the iPad: repo → **Code** → **Codespaces** → create one, then in
its terminal:

```
npm install -g @anthropic-ai/claude-code
claude setup-token
```

Authorize in the browser tab it opens. It prints a long-lived token starting with
`sk-ant-oat...`. Copy the **whole line** — no spaces, no quotes; a partial paste is the
most common cause of every-appraisal-fails runs. Delete the codespace afterwards.

> Treat it like a password. It goes straight into GitHub's encrypted secrets, never into
> the repo.

## 2. Get an Apify token (the scraper)

1. Sign in at [apify.com](https://apify.com).
2. **Settings → API & Integrations → Personal API tokens**.
3. Create a token and copy it.

Apify's free plan includes a monthly credit. The defaults below are sized to stay inside
it — the very first version of this app shipped with `RESULTS_LIMIT=150` and burned a
month's credit in two runs, which is why the default is now 60 and why you should raise
it only after you've seen what a week actually costs you.

## 3. Put both tokens in GitHub

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

| Secret name | Value |
| --- | --- |
| `CLAUDE_CODE_OAUTH_TOKEN` | the `sk-ant-oat...` token from step 1 |
| `APIFY_TOKEN` | the Apify token from step 2 |

## 3b. Optional: eBay comps (free, and it makes the prices real)

Without this, the appraiser judges what a piece is worth restored from photographs and
the seller's blurb alone — an educated guess with no market data behind it. eBay's Browse
API gives it comparable listings to anchor against. It's free (~5,000 calls a day, far
more than a dozen valuations needs) and entirely optional: with no keys configured the app
behaves exactly as it did before.

1. Sign in at [developer.ebay.com](https://developer.ebay.com) and create an application.
2. Take the **Production** keyset. Copy the **App ID (Client ID)** and **Cert ID (Client Secret)**.
3. eBay will ask you to handle *marketplace account deletion notifications* before the
   keyset activates. Take the **opt-out** — it applies to apps that don't store eBay user
   data, which is this one. Without doing one or the other the keys silently won't work.
4. Add two repository **secrets**: `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`.

Honest limitation: this returns **asking** prices, not sold prices. eBay retired its
completed-listings API in February 2025, and the official replacement is a closed
programme not accepting applicants. Asking prices run optimistic, so the appraiser is told
explicitly which kind it's looking at and to treat them as a soft ceiling.

## 4. Tell it what to hunt

Same page, the **Variables** tab. All optional — unset means the default.

| Variable | Default | What it does |
| --- | --- | --- |
| `SEARCH_URLS` | one Lexington dresser search | One Marketplace search URL per line. Overlap is fine — duplicates collapse before anything is paid for. |
| `REGION_LABEL` | `Lexington · 40 mi` | Shown in the site header. |
| `IN_RADIUS_TOWNS` | Lexington + surrounds | Towns you'll actually drive to; anything else is flagged **⤢ Out of radius**. |
| `RESULTS_LIMIT` | `60` | Listings requested per search URL — the main scrape-cost dial. See the warning in step 2 before raising it. |
| `MAX_APPRAISALS` | `12` | Hard cap on AI valuations per run, wildcards included. |
| `MIN_PRICE_DOLLARS` / `MAX_PRICE_DOLLARS` | unset | Pushed into the search URL so junk is never billed. |
| `DAYS_SINCE_LISTED` / `SEARCH_RADIUS_KM` | unset | Same idea — filter at the source. |
| `HOURLY_RATE_CENTS` | `3000` | What your restoration time is worth ($30/hr). Feeds profit, hourly-wage and walk-away math. |
| `APIFY_ACTOR` / `APIFY_DETAIL_ACTOR` | the standard scraper | Only if you switch actors. `APIFY_ACTOR` also scopes recovery to that actor's past runs. |
| `WILDCARDS`, `MAX_CARDS`, `MAX_PHOTO_BACKFILL`, `VERTICAL` | sensible | Finer dials; the defaults are fine. |

## 5. Turn on the website

**Settings → Pages → Build and deployment**

- Source: **Deploy from a branch**
- Branch: your default branch, folder **`/docs`**
- Save.

Your board lives at `https://<your-username>.github.io/<repo>/`.

## 6. Run it

**Actions → Deal board → Run workflow.** Three knobs:

- **max_appraisals** — this run's AI cap (blank = the variable/default).
- **dry_run** — skip AI entirely; free pre-screen only. Never touches the catalogue's
  stored appraisals.
- **recover** — instead of scraping, re-read the datasets your past Apify runs already
  produced. Reading a stored dataset starts no actor and costs **no credit** — use it to
  rescue a scrape you paid for, e.g. after a failed run or when the quota is exhausted.

It also runs itself **daily at 13:00 UTC**. Daily is deliberately the *cheap* cadence:
measured on real data, ~70% of a daily scan is already known, and the two-stage scrape
only pays for the new 30%.

## 7. Put it on your home screen

Open your Pages URL in Safari → **Share → Add to Home Screen**. It opens like an app.

---

## What each run costs you

| Piece | Cost |
| --- | --- |
| GitHub Actions compute | Free |
| GitHub Pages hosting | Free |
| Claude valuations | Free — drawn from your subscription |
| Apify scraping | A few cents per run; the free monthly credit covers the default caps |

The engine also protects that budget on its own:

- **Cross-search dedup** — overlapping searches collapse to one block before billing.
- **The catalogue** (`docs/catalog.json`) — every appraisal is stored, so a later run
  skips pieces already valued and only spends on genuinely new listings, price-dropped
  detail pages, or pieces that gained real new evidence (a description, a photo).
- **A hard appraisal cap** — `MAX_APPRAISALS` is the total, wildcards included.

## Exit codes (what a red X means)

| Code | Meaning |
| --- | --- |
| 0 | Ran clean. |
| 2 | Configuration problem (missing token / bad URLs). Nothing was spent. |
| 3 | `CLAUDE_CODE_OAUTH_TOKEN` missing in CI. Caught **before** the scrape is paid for. |
| 4 | Scrape worked but every appraisal failed — almost always an expired Claude token. The scan data is still saved and published. |
| 5 | No search reached Marketplace (usually Apify's monthly limit). The board is still re-ranked and published from the catalogue. |
| 6 | `catalog.json` is corrupt. It was backed up, not overwritten — inspect before re-running. |

The board itself shows a banner for 4/5/6, so you don't need the Actions tab to know.

## If something goes wrong

| Symptom | Cause |
| --- | --- |
| `APIFY_TOKEN is not set` | Secret missing or misnamed (step 3). |
| `Monthly usage hard limit exceeded` | Apify's free credit is spent. Wait for the month to reset (the daily runs keep re-ranking what's already known), or add a few dollars. |
| `claude CLI failed` / zero-token auth errors | Token expired or was pasted incompletely — redo step 1 and update the secret. |
| Board renders but some pieces have no photo | Facebook photo links expire within hours. Photos land on the next *fresh* scan of that listing; pieces valued from an earlier committed photo already show it. |
| Everything says "already-seen" | Working as intended — nothing new since the last run. The catalogue is the memory; there is no ledger file to delete, and deleting `catalog.json` would throw away every paid appraisal. |
| A negotiation draft never appears | Open the card's panel again — errors are written into the same file the page polls, with the reason. |

## Privacy note

Negotiation drafts (including seller conversation text you paste) are committed to
`.drafts/` — outside the published website. On a **public** repo they are still readable
by anyone via the repository itself. If that ever matters, make the repo private
(Pages on the free plan then stops; Actions and the Contents-API buttons keep working).
