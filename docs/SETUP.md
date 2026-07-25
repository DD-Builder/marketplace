# Setting this up from an iPad — free

Everything runs on GitHub's free compute and publishes to a free GitHub Pages site.
Valuations run on **your Claude subscription**, not a metered API bill.

Running total: **$0/month** for hosting and AI. The only spend is Apify scraping, which
fits inside its free monthly credit at a twice-weekly cadence.

You do this once. After that, running the app is: open GitHub → Actions → **Run workflow**.

---

## 1. Mint a Claude token (so valuations bill to your subscription)

In a Claude Code session, run:

```
claude setup-token
```

Follow the prompt to authorize. It prints a long-lived token starting with `sk-ant-oat...`.
Copy it. This is what lets the pipeline value pieces on your subscription instead of
pay-per-token API billing.

> Treat it like a password. It goes straight into GitHub's encrypted secrets, never into
> the repo.

## 2. Get an Apify token (the scraper)

1. Sign in at [apify.com](https://apify.com) (the account you already made).
2. **Settings → API & Integrations → Personal API tokens**.
3. Create a token and copy it.

Apify's free plan includes a monthly credit. With the caps below (≈150 listings per search,
twice a week) a normal month stays inside it.

## 3. Put both tokens in GitHub

In your repository: **Settings → Secrets and variables → Actions → Secrets → New repository secret**

| Secret name | Value |
| --- | --- |
| `CLAUDE_CODE_OAUTH_TOKEN` | the `sk-ant-oat...` token from step 1 |
| `APIFY_TOKEN` | the Apify token from step 2 |

## 4. Tell it what to hunt

Same page, the **Variables** tab → **New repository variable**:

| Variable | Example | What it does |
| --- | --- | --- |
| `SEARCH_URLS` | `https://www.facebook.com/marketplace/lexington/search/?query=dresser`<br>`https://www.facebook.com/marketplace/lexington/search/?query=mid%20century` | One search URL per line. Overlap is fine — duplicates are removed before anything is paid for. |
| `REGION_LABEL` | `Lexington · 40 mi` | Shown in the site header. |
| `IN_RADIUS_TOWNS` | `lexington,nicholasville,georgetown,richmond,winchester,versailles,paris,berea` | Towns you'll actually drive to. Anything else is flagged **⤢ Out of radius**. |
| `RESULTS_LIMIT` | `150` | Listings requested per search URL. Lower = cheaper scraping. |
| `HOURLY_RATE_CENTS` | `3000` | What your restoration time is worth ($30/hr here). Feeds profit and hourly-wage math. |

## 5. Turn on the website

**Settings → Pages → Build and deployment**

- Source: **Deploy from a branch**
- Branch: your working branch, folder **`/docs`**
- Save.

Your board will live at `https://<your-username>.github.io/<repo>/`.

## 6. Run it

**Actions → Deal board → Run workflow.**

Two knobs each run:

- **max_appraisals** — hard cap on AI valuations (default 12). This is the main cost dial.
- **dry_run** — skip AI entirely and just see what the free pre-screen kept.

It also runs itself **Mondays and Thursdays** — Monday catches weekend price drops, Thursday
catches fresh pre-weekend listings.

## 7. Put it on your home screen

Open your Pages URL in Safari → **Share → Add to Home Screen**. It opens like an app.

---

## What each run costs you

| Piece | Cost |
| --- | --- |
| GitHub Actions compute | Free |
| GitHub Pages hosting | Free |
| Claude valuations | Free — drawn from your subscription |
| Apify scraping | A few cents per run; free monthly credit covers a normal month |

The engine also protects that budget on its own:

- **Cross-search dedup** — searching `dresser`, `mcm`, and `walnut` returns overlapping
  results; they collapse to one block before anything is paid for.
- **A seen ledger** (`docs/seen.json`) — a later run skips listings already evaluated and
  only spends on genuinely new pieces or price drops.
- **A hard appraisal cap** — only the top-ranked pieces plus a few wildcards reach the AI.

## If something goes wrong

| Symptom | Cause |
| --- | --- |
| `APIFY_TOKEN is not set` | Secret missing or misnamed (step 3). |
| `SEARCH_URLS is not set` | Variable missing (step 4). |
| `claude CLI failed` / auth errors | Token expired — re-run `claude setup-token` and update the secret. |
| Board renders but no photos | Facebook's photo links expire within hours; they're downloaded during the run, so this usually means the scrape returned no images. |
| Everything says "already-seen" | Working as intended — nothing new since the last run. Delete `docs/seen.json` to force a full re-evaluation. |
