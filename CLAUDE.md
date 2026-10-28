# JobFlow

Multi-user job-intelligence platform for new grad / entry-level SWE roles. One shared pool of
scanned postings, scored per-user against each user's profile. Also does CLI resume tailoring.
See [README.md](README.md) for the overview and [docs/MULTIUSER.md](docs/MULTIUSER.md) for the
multi-tenant architecture.

## Project Structure

```
JobFlow/
├── config/
│   ├── config.yaml          # Local config (resume paths, output dirs) — git-ignored
│   ├── config.ci.yaml       # CI config (outputs to data/ci/)
│   └── job_boards.json      # Company API endpoints
├── data/ci/                 # CI output (git-tracked, pushed by GitHub Actions)
│   ├── scan_results.json    # Raw scan output
│   ├── linkedin_jobs.json   # JSON store (single-user/local fallback)
│   └── seen_jobs.json       # Dedup tracking
├── jobflow/
│   ├── cli.py               # Typer CLI (web, scan, login, score, apply, migrate-multiuser, …)
│   ├── config.py            # YAML + .env loader
│   ├── auth.py              # Google OAuth, sessions, g.user_id (resolve_request_user)
│   ├── crypto.py            # Fernet encrypt/decrypt for stored API keys
│   ├── db.py                # PostgreSQL backend: users, user_profiles, user_job_state, fan-out
│   ├── db_migrate.py        # Single-user → multi-user migration
│   ├── linkedin_store.py    # JSON backend + dedup + _rescore_entry (single-user/local)
│   ├── models.py            # JobPosting, FilterResult
│   ├── filter.py            # Multi-signal scoring engine (evaluate_job)
│   ├── filter_profile.py    # Per-user FilterProfile + DEFAULT_PROFILE + config (de)serialize
│   ├── scanner.py           # Scanners (Lever, Greenhouse, Ashby, LinkedIn, GitHub README repos) + scan_sources()
│   ├── applied.py           # Applied detection from tailored-application output folders
│   ├── instagram_stories.py # @zero2sudo story drops: viewer sites → OCR → Gemma → search → posting
│   ├── linkedin_scraper.py  # LinkedIn guest-API transport/parsing (429 backoff, pagination)
│   ├── jobright.py          # jobright.ai: entry-level SWE sheet (Airtable) + signed-in recommendations
│   ├── ai_prompt.py         # Shared scoring prompt + parse/normalize (used by all scorers)
│   ├── ai_local.py          # Local scoring engines: gemma (default) / claude / codex CLI dispatch
│   ├── ai_llama.py          # Local open-weight model (Gemma 4) via llama.cpp llama-server
│   ├── ai_scorer_anthropic.py # Server-side Anthropic API scoring (per-user key)
│   ├── ai_scorer.py         # Legacy optional Groq scorer
│   ├── tracker.py           # CSV application tracking (CLI only)
│   ├── tailor.py · latex.py · scraper.py   # Resume tailoring (CLI only)
│   └── web/
│       ├── __init__.py      # Flask app factory + all routes + ProxyFix
│       ├── static/          # style.css, icon.svg
│       └── templates/       # base, linkedin, login, settings, _partials/
├── scripts/
│   ├── ai_score_local.py    # Operator's local AI-scoring runner (--user-id, DB or JSON)
│   ├── bench_story_providers.py # Times the anonymous Instagram story-viewer providers
│   └── backfill_ai_scores.py
├── docs/                    # Documentation (see README's Documentation table)
├── .github/workflows/scan-jobs.yml   # Every-30-min LinkedIn scan + merge/fan-out
├── wsgi.py · Procfile · render.yaml  # Production entry + deploy
└── pyproject.toml
```

Package layout is intentionally flat (standard Flask structure) — keep it that way so the CI
workflow's inline `from jobflow.db import …`, `wsgi:app`, and `jobflow.cli:app` stay stable.

## Multi-user & auth
- Two auto-detected switches: **auth** on when `GOOGLE_CLIENT_ID`+`GOOGLE_CLIENT_SECRET` set;
  **DB** on when `DATABASE_URL` set. Neither set = original single-user + JSON behavior.
- `auth.resolve_request_user()` sets `g.user_id` per request (operator = `DEFAULT_USER_ID` = 1).
- Multi-user is DB-only. Every `db.py` query is scoped by `user_id` and JOINs `user_job_state` to
  `jobs`. The shared `jobs` table holds the posting; per-user scoring/state/status lives in
  `user_job_state`. Never add a query path that isn't `user_id`-scoped.
- Env vars: `JOBFLOW_SECRET_KEY` (session + key encryption), `JOBFLOW_OPERATOR_EMAIL`,
  `ALLOWED_EMAILS`, `OAUTH_REDIRECT_URI`. `db.py` strips `channel_binding` from `DATABASE_URL`.

## Commands
- `jobflow web` — Launch dashboard (single- or multi-user by env)
- `jobflow scan [--platform linkedin] [--hours N]` — Scan; merge + fan-out to users (used by CI)
- `jobflow login --token <t>` / `jobflow score [--engine gemma|claude|codex]` — Local scoring client (a user's own machine)
- `jobflow apply <url> --paste -t … -c … -l …` / `jobflow save --dir <path>` — Resume tailoring
- `jobflow migrate-multiuser` — Seed operator + migrate legacy single-user data
- `jobflow list` · `jobflow init`

## Web Routes
- `/` → `/linkedin` — the per-user job feed (filter/sort/time buckets/bulk actions)
- `/settings` — per-user profile: eligibility, seniority, search terms, AI method (none/anthropic/local-cli), API key, model, pairing token
- `/auth/login`, `/auth/google`, `/auth/google/callback`, `/auth/logout` — Google OAuth
- `/api/linkedin/*` — feed data (jobs fragment, meta counts, delete, refresh)
- `/api/scan/*` — background scan + GitHub Actions "Scan Now" trigger; `/api/scan/sources` lists the
  sources (LinkedIn + GitHub new-grad READMEs in `job_boards.json`) with live per-source status
- `/api/aiscore/*` — AI Score button: operator local subprocess, or server-side Anthropic run (per-user)
- `/api/score/pending`, `/api/score/submit` — token-authed endpoints for the local-CLI client (login-gate exempt, scoped to the token's user)

Application tracking, /boards, /scan, /tailor pages were removed from the web UI; `tracker.py` /
`tailor.py` remain CLI-only.

## Applied jobs
Feed rows are struck through when a folder in `applied_dirs` (config.yaml, or `JOBFLOW_APPLIED_DIRS`)
matches the job — by the posting URL in the folder's `jd.txt` (`Source:` line or a bare URL in the
header), by the posting ID ending the `YYYY-MM-DD_company_title-<id>` folder name, or, for folders with
neither, by an exact company + title slug. Tuned for precision: a missed strike is fine, a wrong one is
not, so there is no fuzzy matching. Operator only (the folders live on the operator's machine). See
`jobflow/applied.py`.

## Scoring
Multi-signal scoring (0–100%) via `filter.evaluate_job(job, profile=…)`. Scoring is **per-user**:
`filter_profile.FilterProfile` holds the tunable knobs (stack + weights, synergy, sponsorship/US
gates, seniority band, recommend bar); `DEFAULT_PROFILE` reproduces the original behavior. Weak
seniority proxies (4+ yrs, senior salary) are **soft demotions, not hard rejects**, so the AI
scorer can rescue misreads. When no AI score exists, `filter.algo_recommended()` flags high-scoring
entry-level jobs as Recommended. See [docs/SCORING.md](docs/SCORING.md).

## Local AI scoring (Gemma)
The AI Score button and `jobflow score` default to **gemma**: Gemma 4 26B-A4B (GGUF) run by llama.cpp's
`llama-server` on this machine — no subscription or API key. `ai_llama.LlamaServer` starts the server
for a run and stops it after (~14 GB RAM); a server already on `LLAMA_PORT` is reused. Output is
grammar-constrained JSON; batches of 5, descriptions capped at 6000 chars. Paths via `.env`
(`LLAMA_SERVER_PATH`, `LLAMA_MODEL_PATH`), else `~/tools/llama.cpp/` and `~/models/`. Claude/Codex
remain selectable. `scripts/bench_local_models.py` compares local models against stored Claude scores
(results in git-ignored `data/eval/bench/`). The hover card on a row's match % shows the scorer's
one-sentence reason (`ai_reason`).

## Instagram story source
`instagram_stories.py` turns job-drop stories (default account `zero2sudo`) into postings, local-only
(Scan Now / `jobflow scan -p instagram`; skipped in CI). Stories come from anonymous viewer sites
(`PROVIDERS`, fastest-first, plain HTTP — storynavigation.com, insta-stories-viewer.com,
insta-stories.io; re-rank with `scripts/bench_story_providers.py`). Each frame (video poster) is OCR'd
with RapidOCR, then local Gemma classifies it (only `job_posting` survives — DMs, offers, recaps,
news, Q&A are dropped) and extracts company/title/location/link domain. The link is found via
Greenhouse/Lever/Ashby board APIs, else `ddgs` web search + Gemma pick, guarded so career home pages
don't pass. No Claude calls. Verdicts are cached per story id in git-ignored
`data/instagram/stories.json`. Extras: `pip install -e .[instagram]`.

## Jobright source
`jobright.py` (`--platform jobright`): the public entry-level SWE sheet (an Airtable shared view
embedded on jobright.ai/entry-level-jobs?k=swe; runs in CI) and the operator's jobright
recommendations (`/swan/recommend/list/jobs`, filters saved on the jobright account; local-only,
needs `JOBRIGHT_COOKIE` = a signed-in browser's Cookie header). Job URLs are
`jobright.ai/jobs/info/<id>` so all jobright feeds dedupe. jobright.ai reposts are NOT blocklisted.

## Default filter criteria (operator profile)
New grad / entry-level / SDE 1 · USA-based · must not deny sponsorship · OPT/F1 friendly · SWE
roles only. Other users override these in their own `FilterProfile`.

## Deployment
Render.com free tier + GitHub Actions every-30-min cron. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
