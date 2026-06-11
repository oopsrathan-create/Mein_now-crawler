# mein-now course tracker

Tracks your brand's course listings on [mein-now.de](https://mein-now.de/weiterbildungssuche/)
by pulling structured data from the mein-NOW / KURSNET search backend (the same
API the public search page uses — no account or scraping of rendered HTML needed).

## What it captures

Per run (daily), for the providers and keywords you configure:

- **Inventory** — every course listed under your provider name(s): title, locations,
  start dates, cost band, course format (Vollzeit/Teilzeit, Combined Learning, …),
  number of sessions, and the full description text (your keyword source).
- **Ranking / visibility** — for each tracked search keyword, the position your
  courses occupy in the result list. Lower rank number = more visible.
- **Changes** — a daily diff: which listings are new, which dropped off.

### What it does NOT capture

Clicks, impressions, and engagement are **not available to any crawler** — they
live in the platform's private analytics. To track clicks, use your mein-NOW
provider dashboard or Google Analytics / Search Console on your own pages.

## Keyword tracker (SERP-style rank tracking)

`keyword_tracker.py` is a second tool, modelled on a keyword rank tracker. For
every keyword in [`keywords.txt`](keywords.txt) it records, daily:

- **Brand rank** — ecomex's best position and how many ecomex listings appear.
- **Market landscape** — total results for the keyword.
- **Competitor share** — for the visible first page (`top_n`, default 20), each
  provider's share of results and their best position. Shows who dominates.
- **Keyword discovery** — candidate keywords mined from ecomex's own course
  titles, scored by how well ecomex ranks, so you can promote good ones into
  `keywords.txt`.

Edit the tracked terms in `keywords.txt` (one per line). Tracker tuning lives in
the `keyword_tracker` block of `config.json`.

Run locally: `python keyword_tracker.py`

### Listing keyword library

[`listing_keywords.txt`](listing_keywords.txt) is the full, searchable list of
every keyword the team uses across ecomex's listings (one per line). It's a
reference list — separate from `keywords.txt`, which is the curated set the
crawler actively rank-tracks. The dashboard shows it in the **Listing keyword
library** section: search the list, filter by tracked / not-yet-tracked, and for
any keyword the crawler already tracks see ecomex's best rank and page-1 status.
Anyone can add keywords by editing the file; to start rank-tracking one, copy it
into `keywords.txt`.

## Setup

1. Edit [`config.json`](config.json):
   - `providers`: your brand name(s) exactly as shown on mein-now.de (matching is
     case-insensitive substring — `"GFN"` matches `"GFN GmbH"`).
   - `ranking_keywords`: the search terms prospective students would type.
2. Run locally: `pip install -r requirements.txt && python crawl.py`
3. Outputs land in `data/`.

## Hosting (free, GitHub Actions)

The workflow at `.github/workflows/crawl.yml` runs the crawler daily and commits
the new snapshots back to the repo — $0/month, full history in git.

- Push this repo to GitHub.
- Settings → Actions → General → Workflow permissions → enable **Read and write**.
- The job runs daily at 05:15 UTC; trigger it manually any time from the **Actions**
  tab → *mein-now course crawl* → *Run workflow*.

## Dashboard (shareable web viewer)

A static dashboard lives at [`/index.html`](../index.html) in the repo root. It
reads `rankings.csv` and `latest_inventory.csv` and shows:

- **Scorecard tiles** — courses listed, keywords on page 1, best/weakest keyword.
- **Listing keyword library** — searchable list of every listing keyword (from
  `listing_keywords.txt`), flagged tracked / not-tracked with best rank.
- **Keyword rankings** — where ecomex ranks per keyword, sortable.
- **Ranking trend** — rank-over-time line chart (fills in as daily crawls run).
- **Course inventory** — searchable table of every course and where it appears.

### Publish it (free, anyone with the link)

1. GitHub → **Settings → Pages**.
2. **Source: Deploy from a branch**, branch **main**, folder **/ (root)**, Save.
3. After a minute the dashboard is live at
   `https://oopsrathan-create.github.io/claude-test/`.

It refreshes automatically each day once the crawl workflow commits new data.

## Output files

| File | Contents |
|------|----------|
| `data/snapshots/inventory_YYYY-MM-DD.csv` | One row per course — daily snapshot |
| `data/snapshots/inventory_YYYY-MM-DD.jsonl` | Same, full detail per line |
| `data/rankings.csv` | Appended every run: `date, keyword, provider, course_id, title, rank, total_results` — your visibility over time |
| `data/changes/changes_YYYY-MM-DD.md` | Human-readable diff vs the previous snapshot |
| `data/keyword_ranks.csv` | Appended every run: brand rank + total results per keyword over time |
| `data/keyword_competitors.csv` | Appended every run: competitor share of the top results per keyword |
| `data/keyword_discovery.csv` | Refreshed every run: candidate keywords ecomex already ranks for |

Open `rankings.csv` in Excel/Sheets and pivot by keyword to chart rank trends.

## API notes

- Backend: `https://rest.mein-now.de/now-prod/suche/pc/v1/bildungsangebot`
- Auth header: `X-API-Key: infosysbub-nowsuche` (a public client id read from the
  site config — not a secret). If crawls start returning 404, the host or client id
  has changed; re-read it from `https://mein-now.de/weiterbildungssuche/assets/config.json`
  and update `config.json`.
