# Job Scanning System

## Overview

JobFlow scans multiple job board platforms and aggregates results into a unified feed. The scanner runs both locally (via CLI) and in CI (GitHub Actions, every 30 minutes).

## Platforms

### LinkedIn (guest API)

**Files**: `jobflow/linkedin_scraper.py` (transport + parsing) and
`jobflow/scanner.py` — `scan_linkedin()` (orchestration)

Self-contained scraper for LinkedIn's public guest API (no auth). It replaced
python-jobspy, which silently truncated scans on the first 429, skipped result
ranges due to a pagination bug, and burned ~10x the request budget re-fetching
descriptions for jobs already in the store.

**Search Terms** (5 queries, up to 200 results each):
1. "New Grad Software Engineer"
2. "Junior Software Engineer"
3. "Associate Software Engineer"
4. "Entry Level Jobs 2026"
5. "New Grad Machine Learning Engineer"

Trimmed from 8 to 5: dropped "Software engineer new grad posted in the past 24
hours" (a natural-language string LinkedIn's title search never matched — 0
results every run), plus "Entry Level Software Engineer" and "Entry Level AI
Engineer" (0-4 jobs/run, almost entirely overlapping what the Junior/Associate
terms already catch).

**Two-phase scan:**
1. **Listings** — cheap search pages (~10 jobs/request) for every term,
   deduped by job id across terms, filtered to SWE titles.
2. **Descriptions** — full JDs fetched **only for jobs not already stored
   with a real description** (known URLs come from the DB, falling back to
   the JSON store). Known jobs are skipped from the scan output entirely;
   new users get the existing pool via `db.backfill_user` at signup.

**Rate-limit handling:** 429/999 responses are retried with
Retry-After/exponential backoff (up to 4 retries, capped at 180s). If
LinkedIn still blocks, the scan keeps what it has: remaining new jobs are
saved with title-only descriptions and their JDs are re-fetched next scan
(any stored description under 200 chars marks the job as still unknown).

**Configuration:**
- Location: "United States"
- Time window: `--hours N` → LinkedIn's `f_TPR` filter; default 4h when unset
- Descriptions capped at 6,000 chars; max 300 description fetches per scan
  (excess deferred to the next scan and reported)
- Random 2-4s delay between terms, 0.7-1.5s between description fetches

**Known Limitation:** LinkedIn's `date_posted` is date-only (posting day, no
time). Jobs missing it are timestamped with the scan time (when discovered).

### Lever API

**File**: `jobflow/scanner.py` — `scan_lever()`

Fetches from Lever's public JSON API (`/postings?mode=json`).

**Companies**: 11 (configured in `config/job_boards.json`)
- Example: Anduril, Cloudflare, Notion, Plaid, etc.

### Greenhouse API

**File**: `jobflow/scanner.py` — `scan_greenhouse()`

Fetches from Greenhouse's public JSON API (`/boards/{id}/jobs`).

**Companies**: 40 (configured in `config/job_boards.json`)
- Example: Stripe, Airbnb, Coinbase, DoorDash, etc.

### Ashby API

**File**: `jobflow/scanner.py` — `scan_ashby()`

Fetches from Ashby's public JSON API (`/posting-api/job-board/{id}`).

**Companies**: 31 (configured in `config/job_boards.json`)
- Example: Ramp, Figma, Linear, etc.

### GitHub Repos

**File**: `jobflow/scanner.py` — `scan_github_repos()`

Parses markdown tables from new-grad aggregator repos:
- [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions)
- [Jobright-AI/2025-New-Grad-Intern](https://github.com/Jobright-AI/2025-New-Grad-Intern)

### Jobright (`--platform jobright`)

**File**: `jobflow/jobright.py`

- **Entry-level SWE sheet** — [jobright.ai/entry-level-jobs?k=swe](https://jobright.ai/entry-level-jobs?k=swe)
  embeds an Airtable shared view (~a week of entry-level SWE postings, updated hourly). The scanner
  reads the embed page's signed `readSharedViewData` URL and keeps rows dated today or yesterday
  (the sheet has dates only) whose title passes the SWE pre-filter. No login, so it runs in CI.
- **My recommendations** — `jobright.ai/jobs/recommend` via the signed-in `/swan/recommend/list/jobs`
  API. What it returns is set by the filters saved on the jobright account (job functions,
  Intern/New Grad + Entry Level, past 24 hours, US, H-1B only, no clearance / US-citizen-only /
  staffing agencies), so edit them on jobright. Needs `JOBRIGHT_COOKIE` in `.env`: the whole `Cookie`
  request header from a signed-in browser (DevTools → Network → any `/swan/` request). Local-only;
  skipped quietly without it. A 401 means the session expired: copy a fresh header.

Both use `jobright.ai/jobs/info/<id>` as the job URL, so the same posting from the sheet, the
recommendations, and the Jobright GitHub README collapses onto one row. Those pages sit behind a bot
check that trips after a handful of fetches, so they are never scraped: recommendations fetch JDs from
the employer's apply link (`JobPosting.apply_url`), and sheet rows keep the sheet's own fields.

## Pre-filters (Title-level)

Before full scoring, jobs are pre-filtered by title:

**Must match** (SWE_ROLE_KEYWORDS): software, engineer, developer, swe, sde, backend, frontend, full stack, machine learning, data scientist, ml engineer, ai engineer, applied scientist

**Role exclusion** (in evaluate_job): senior, staff, principal, lead, manager, director (only penalized, not hard-rejected, unless 3+ signals with 0 entry signals)

## Scan Output

Results saved to `scan_results.json`:

```json
[
  {
    "index": 1,
    "company": "Stripe",
    "title": "Software Engineer, New Grad",
    "location": "San Francisco, CA",
    "url": "https://...",
    "score": 45,
    "score_pct": 35,
    "level": "New Grad",
    "min_exp": 0,
    "max_exp": 2,
    "competition": 5,
    "variant": "se",
    "reason": "Stack +24; Synergy +10; ...",
    "description_preview": "...",
    "date_posted": ""
  }
]
```

Capped at 500 entries, sorted by score descending. Merges with existing results (dedup by URL).

## GitHub Actions Workflow

**File**: `.github/workflows/scan-jobs.yml`

```
Schedule: Every 30 minutes (cron: '*/30 * * * *')
Also: Manual trigger from Actions tab or the feed's "Scan Now" button

Steps:
1. Checkout repo + git pull (latest JSON store)
2. Setup Python 3.12
3. pip install -e .
4. JOBFLOW_CONFIG=config/config.ci.yaml jobflow scan --platform linkedin,github,jobright --save --hours 4
5. Merge scan_results.json into Postgres (fan-out per user) and linkedin_jobs.json
6. git add data/ci/ && git commit && git push
```

The `--hours 4` window gives each 30-minute run padding to catch jobs missed
by earlier runs; already-stored jobs are skipped by the two-phase scan, so the
overlap costs only cheap listing requests.

## Deduplication

Three levels:
1. **Within a scan**: By URL (across search terms)
2. **Across scans**: Via `seen_jobs.json` (with `--new` flag)
3. **In the store**: By company+title (same role in multiple cities collapsed)

## Rate Limiting

- LinkedIn: 2-4s random delay between search terms
- All platforms: 3-retry exponential backoff on HTTP 429
- Fetch timeout: 15s per request
