# JobFlow

**Personalized job intelligence for new-grad / entry-level software engineers.**

JobFlow scans fresh SWE job postings around the clock, then gives **each signed-in user their
own privately-scored, personalized feed** — ranked against *their* tech stack, visa needs, and
seniority preferences. Sign in with Google, tune your profile, and (optionally) let AI score every
job for fit.

**Live:** https://jobflow-ktem.onrender.com

---

## Table of contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Using JobFlow (as a signed-in user)](#using-jobflow-as-a-signed-in-user)
- [AI scoring: two ways](#ai-scoring-two-ways)
- [Self-hosting / local development](#self-hosting--local-development)
- [Configuration & environment variables](#configuration--environment-variables)
- [The scoring engine](#the-scoring-engine)
- [Scanning](#scanning)
- [Resume tailoring (CLI)](#resume-tailoring-cli)
- [CLI reference](#cli-reference)
- [Project structure](#project-structure)
- [Deployment](#deployment)
- [Documentation](#documentation)

---

## What it does

- 🔎 **Scans SWE jobs 24/7** — LinkedIn (guest-API scraper) every 30 min through GitHub Actions,
  plus Lever / Greenhouse / Ashby / GitHub aggregators on demand.
- 👤 **Multi-user** — sign in with Google; every user gets their **own** feed, scored and filtered
  by *their* profile, over one shared pool of postings.
- 🎯 **Personalized scoring** — a fast rule-based engine ranks each job 0–100% against your stack,
  target roles, seniority band, and sponsorship needs.
- ✨ **AI scoring, your choice** — layer Claude on top using **your own API key** (server-side) or
  **a local Gemma model or your own Claude/Codex CLI** (free). Per-user, opt-in.
- 🧵 **Live dashboard** — spreadsheet-style feed with time buckets, filter chips, search, and
  per-user status tracking (Tracking / Applied / Not Interested).
- 📄 **Resume tailoring** (CLI) — Claude rewrites your LaTeX resume per job description and compiles
  a one-page PDF.

---

## How it works

```
  GitHub Actions (every 30 min)
        │  scan LinkedIn + ATS boards
        ▼
  shared `jobs` pool  ──────────────┐  (one row per posting)
        │                            │
        │  fan-out: score per user   │  each user's FilterProfile
        ▼                            ▼
  user_job_state(A)          user_job_state(B)        …
  (Alice's scores +          (Bob needs no visa,
   status + AI scores)        wants Java roles)
        │                            │
        ▼                            ▼
   Alice's feed                 Bob's feed          ← Google-authenticated
```

**One shared job pool, per-user scoring.** Postings are scanned once and stored once. Each user's
preferences produce their *own* scored/ranked view in `user_job_state` — so the same job can be a
92% match for one person and a hard reject for another. See [docs/MULTIUSER.md](docs/MULTIUSER.md).

---

## Using JobFlow (as a signed-in user)

1. **Sign in with Google** at the live URL (your email must be on the instance's allow-list).
2. **Build your profile** in **Settings**:
   - Do you need visa sponsorship? US-only?
   - Target seniority / experience band, recommend threshold.
   - Search terms and a free-text "about me" used for AI scoring.
3. **Browse your feed** — jobs are ranked by match %, with time buckets and filter chips. Mark jobs
   Tracking / Applied / Not Interested (per-user, private to you).
4. **(Optional) Turn on AI scoring** — pick a method in Settings (below), then hit **AI Score**.

Your feed, statuses, scores, and profile are private to your account.

---

## AI scoring: two ways

Every job already gets a **rule-based** score instantly. For sharper, LLM-judged relevance, each
user can opt into one of two AI methods in **Settings → AI scoring method**:

### A) Your Anthropic API key (server-side) — easiest
Paste your Anthropic API key (stored **encrypted** at rest) and pick a model (Haiku 4.5 / Sonnet 5 /
Opus 4.8; Haiku default). Click **AI Score** and the server scores your jobs and writes them to your
feed. You pay for your own usage; runs are **capped per click** so a single run can't surprise you.

### B) Locally (free) — bring your own compute
No API key, no server cost — score on your own machine with a local open-weight model (default:
Gemma 4 26B-A4B via [llama.cpp](https://github.com/ggml-org/llama.cpp), ~14 GB RAM), or with the
Claude/Codex subscription you already have (`--engine claude|codex`):

```bash
pip install "git+https://github.com/kiiriis/JobFlow"
jobflow login --token <your-pairing-token>   # token shown in Settings
jobflow score                                 # scores YOUR unscored jobs locally
```

The client pulls your unscored jobs from the server, scores them locally (Gemma via `llama-server`,
or your **signed-in** `claude` / `codex` CLI), and pushes the results back — all authenticated by a per-user pairing token you
can regenerate (which revokes the old one) any time.

> The **operator** (the account that runs the server / owns the original data) can also score via a
> local CLI subprocess directly from the dashboard's **AI Score** button — no token needed.

---

## Self-hosting / local development

```bash
git clone https://github.com/kiiriis/JobFlow.git
cd JobFlow
pip install -e .

# Personal config (git-ignored)
cp config/config.example.yaml config/config.yaml
cp config/profile.example.txt config/profile.txt   # your skills/roles/visa for AI scoring

# Run the dashboard
jobflow web
```

**Two run modes**, chosen automatically:

| Mode | When | Behavior |
|------|------|----------|
| **Single-user** | No `GOOGLE_CLIENT_ID` set | No login. You are the operator; full access to your feed. |
| **Multi-user** | `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` set | Google login required; per-user feeds. |

And **two storage backends**, chosen automatically:

| Backend | When | Notes |
|---------|------|-------|
| **PostgreSQL** (Neon) | `DATABASE_URL` set | Persistent, required for multi-user. |
| **JSON file** | No `DATABASE_URL` | `data/ci/linkedin_jobs.json`; single-user/local dev. |

**Requirements:** Python 3.11+ · Optional: Postgres/Neon (`DATABASE_URL`), a signed-in Claude/Codex
CLI (local AI scoring), `pdflatex` (resume PDFs).

---

## Configuration & environment variables

Local dev reads `.env` (git-ignored); production reads the host's env (Render dashboard). Existing
host vars always win over `.env`.

| Variable | Required for | Purpose |
|----------|-------------|---------|
| `DATABASE_URL` | Multi-user / persistence | Neon/Postgres DSN. `channel_binding` is stripped automatically. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Multi-user login | Google OAuth app credentials. |
| `OAUTH_REDIRECT_URI` | Deployed behind a proxy | e.g. `https://<host>/auth/google/callback` (avoids scheme mismatch). |
| `JOBFLOW_SECRET_KEY` | Multi-user | Flask session signing **and** API-key encryption key. |
| `JOBFLOW_OPERATOR_EMAIL` | Multi-user | The email bound to the operator account (`user_id=1`, inherits existing data). |
| `ALLOWED_EMAILS` | Multi-user | Comma-separated allow-list of who may sign in. |
| `JOBFLOW_CONFIG` | Deploy | Path to the YAML config (CI uses `config/config.ci.yaml`). |
| `JOBRIGHT_COOKIE` | Jobright recommendations (local) | `Cookie` header from a signed-in jobright.ai browser session. |

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for a step-by-step Render + Google OAuth setup.

---

## The scoring engine

Each job is scored 0–100% against **your** `FilterProfile` (tech stack, target roles, seniority
band, sponsorship/location gates). Defaults reproduce a Python/ML/backend new-grad profile; every
knob is per-user configurable.

| Signal | Points | Example |
|--------|--------|---------|
| Keyword match | 3–10 each | Python(10), PyTorch(8), AWS(7), FastAPI(7) |
| Synergy combos | +6 to +10 | Python + FastAPI + AWS = +10 |
| Title / role fit | +6 to +12 | "Backend Engineer", "ML Engineer" |
| Level detection | +5 to +20 | New Grad = +20, Entry = +15 |
| Experience fit | +0 to +10 | 0–2 years = +10 |
| Recency | −5 to +10 | < 6h = +10, > 48h = −5 |
| H1B mention | +8 | "will sponsor" (if you need sponsorship) |
| US location | +10 | US city/state/remote (if US-only) |

**Soft demotions, not deletions.** Weak seniority proxies (a 4+ yr requirement, a senior-range
salary) *penalize* a job rather than dropping it, so the AI scorer can still rescue misreads.
Only structural certainties hard-reject (non-SWE title, non-US when US-only, explicit
no-sponsorship when you need it, blocked staffing source). Full details:
[docs/SCORING.md](docs/SCORING.md).

---

## Scanning

```bash
jobflow scan                                  # all sources
jobflow scan --platform linkedin --hours 24   # LinkedIn only, last 24h
jobflow scan --platform greenhouse --hours 24 # one ATS platform
```

**Sources:** LinkedIn (guest-API scraper, multiple search terms) · Lever · Greenhouse · Ashby ·
GitHub aggregators (SimplifyJobs / Jobright) · Jobright entry-level SWE sheet + your Jobright
recommendations (`JOBRIGHT_COOKIE`). Company endpoints live in
[config/job_boards.json](config/job_boards.json). New postings fan out to every active user's feed
at merge time. See [docs/SCANNING.md](docs/SCANNING.md).

---

## Resume tailoring (CLI)

```bash
jobflow apply "https://job-url" --paste -t "SWE" -c "Stripe" -l "SF"
jobflow save --dir data/output/Stripe_SWE_2026-04-09
```

Claude auto-selects a resume variant (SE / ML / AppDev), rewrites experience/projects/skills while
preserving your header + education, and compiles a one-page PDF with `pdflatex`. See
[docs/TAILORING.md](docs/TAILORING.md).

---

## CLI reference

| Command | What it does |
|---------|--------------|
| `jobflow web` | Launch the dashboard (single- or multi-user by env). |
| `jobflow scan [--platform …] [--hours N]` | Scan sources; merge + fan-out to users. |
| `jobflow login --token <t> [--server <url>]` | Pair the local CLI client to your account. |
| `jobflow score [--engine gemma\|claude\|codex] [--hours N] [--limit N] [--rescore]` | Score *your* jobs locally, push results back. |
| `jobflow apply <url> …` / `jobflow save --dir <path>` | Tailor + compile a resume. |
| `jobflow migrate-multiuser` | Seed the operator user + migrate legacy single-user data. |
| `jobflow init` | First-time local setup. |

Run `jobflow --help` for the full list.

---

## Project structure

```
JobFlow/
├── jobflow/                     # Application package
│   ├── cli.py                   # Typer CLI (web, scan, login, score, apply, …)
│   ├── config.py                # YAML + .env loader
│   ├── auth.py                  # Google OAuth, sessions, per-request user (g.user_id)
│   ├── crypto.py                # Fernet encrypt/decrypt for stored API keys
│   ├── db.py                    # PostgreSQL backend (users, profiles, per-user state)
│   ├── db_migrate.py            # Single-user → multi-user migration helper
│   ├── linkedin_store.py        # JSON backend + dedup + rescoring (single-user/local)
│   ├── models.py                # JobPosting, FilterResult dataclasses
│   ├── filter.py                # Multi-signal scoring engine
│   ├── filter_profile.py        # Per-user FilterProfile (tunable scoring knobs)
│   ├── scanner.py               # Source scanners (LinkedIn/Lever/Greenhouse/Ashby/GitHub)
│   ├── ai_prompt.py             # Shared scoring prompt + parse/normalize helpers
│   ├── ai_local.py              # Local scoring engines (gemma / claude / codex)
│   ├── ai_llama.py              # Local Gemma model via llama.cpp llama-server
│   ├── ai_scorer_anthropic.py   # Server-side Anthropic API scoring (per-user key)
│   ├── ai_scorer.py             # Legacy optional Groq scorer
│   ├── scraper.py · tailor.py · latex.py · tracker.py   # Resume tailoring + tracking
│   └── web/                     # Flask app (routes, templates, static)
├── scripts/
│   ├── ai_score_local.py        # Operator's local AI-scoring runner (DB or JSON)
│   └── backfill_ai_scores.py    # One-off scoring backfill
├── config/                      # config.yaml (local), config.ci.yaml (CI), job_boards.json
├── data/ci/                     # Git-tracked CI scan output + JSON store
├── docs/                        # Deep-dive docs (see below)
├── tests/                       # pytest suite
├── .github/workflows/           # Every-30-min scan cron
├── wsgi.py · Procfile · render.yaml   # Production entry + deploy config
└── pyproject.toml               # Package + dependencies
```

> The `jobflow/` package is intentionally a **flat module layout** (a standard, well-supported
> Flask structure) so imports stay simple and the hourly CI + WSGI entry points remain stable.
> Modules are grouped by concern in the tree above.

---

## Deployment

Render (free tier) with GitHub auto-deploy, plus a GitHub Actions cron:

```
render.yaml   — Render Blueprint (build: pip install -e . ; start: gunicorn wsgi:app)
Procfile      — gunicorn -w 1 -b 0.0.0.0:$PORT wsgi:app
wsgi.py       — from jobflow.web import create_app; app = create_app()
```

Every 30 minutes GitHub Actions scans LinkedIn, merges into the shared pool (fanning out to all
users), and commits results — which triggers a Render redeploy and keeps the free instance warm.
Full setup (Google OAuth, env vars, Neon): [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Documentation

| Doc | Content |
|-----|---------|
| [Multi-user](docs/MULTIUSER.md) | Auth, per-user data model, dual AI scoring, pairing tokens |
| [Architecture](docs/ARCHITECTURE.md) | System overview, data flow, design decisions |
| [Scoring](docs/SCORING.md) | Full scoring-engine breakdown |
| [API](docs/API.md) | HTTP endpoints reference |
| [Data Models](docs/DATA_MODELS.md) | DB / JSON / CSV schemas |
| [Deployment](docs/DEPLOYMENT.md) | Render + Google OAuth + Neon setup, env vars |
| [Scanning](docs/SCANNING.md) | Sources, search terms, dedup |
| [Tailoring](docs/TAILORING.md) | Resume flow, Claude integration |
| [CLI](docs/CLI.md) | All commands with examples |

## License

MIT
