"""Multi-platform job scanner — aggregates listings from LinkedIn, Lever,
Greenhouse, Ashby, and GitHub new-grad repos.

Data flow:
    1. scan_all_api_boards() orchestrates all platform scanners
    2. Each scanner (scan_lever, scan_linkedin, etc.) returns [JobPosting]
    3. Every JobPosting is scored by evaluate_job() → (JobPosting, FilterResult)
    4. deduplicate_results() removes already-seen jobs via seen_jobs.json
    5. Results saved to scan_results.json, then merged into linkedin_jobs.json

Platform differences:
    - Lever:       REST API, JSON, epoch ms timestamps, no auth needed
    - Greenhouse:  REST API, JSON, ISO timestamps, no auth needed
    - Ashby:       REST API, JSON, ISO timestamps, no auth needed
    - LinkedIn:    guest API scraper (jobflow.linkedin_scraper), two-phase
    - GitHub:      Raw README table parsing (SimplifyJobs, SpeedyApply, Jobright repos)

Deduplication (seen_jobs.json):
    Tracks previously seen job URLs with timestamps. Entries expire after
    48 hours (SEEN_TTL_HOURS) so reposted/updated jobs can resurface.
    Format: {"url": "ISO_timestamp_EST", ...}
    Backward-compatible: auto-migrates old array format on first load.
"""

import json
import os
import random
import re
import ssl
import time
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from html import unescape
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .filter import evaluate_job
from .linkedin_store import is_db_enabled
from .models import JobPosting, FilterResult

console = Console()

# Build a default SSL context and URL opener with browser-like headers.
# Using browser User-Agent prevents some ATS platforms from blocking requests.
_SSL_CTX = ssl.create_default_context()
_OPENER = urllib.request.build_opener()
_OPENER.addheaders = [
    ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"),
    ("Accept", "application/json"),
]


def _fetch_json(url: str, retries: int = 3) -> dict | list | None:
    """Fetch JSON from a URL using stdlib urllib with retry/backoff.

    Uses exponential backoff (3^attempt seconds) on failure. Rate limit
    responses (429) respect the Retry-After header if present. We use
    stdlib urllib instead of requests to avoid the extra dependency for
    simple GET requests.
    """
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 3 ** (attempt + 1)))
                console.print(f"  [yellow]Rate limited, waiting {wait}s...[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"  [red]Failed to fetch {url}: {e}[/red]")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(3 ** (attempt + 1))
                continue
            console.print(f"  [red]Failed to fetch {url}: {e}[/red]")
            return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    """Check if text contains any of the filter keywords."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


# Role must be software/engineering related.
# This is a pre-filter applied BEFORE evaluate_job() to reduce noise. It's
# intentionally broad — we'd rather let a borderline role through to the
# scoring engine than miss a valid job. The scoring engine handles the
# fine-grained filtering (senior, sponsorship, etc.).
SWE_ROLE_KEYWORDS = [
    "software", "engineer", "developer", "sde", "swe", "backend",
    "frontend", "full stack", "fullstack", "full-stack", "platform",
    "infrastructure", "devops", "systems engineer", "data engineer",
    "machine learning", "ml engineer", "ai engineer", "applied scientist",
    "research engineer", "security engineer", "site reliability",
    "cloud engineer", "distributed systems",
    "member of technical staff", "mts", "data scientist", "data analyst",
]


def _is_swe_role(title: str) -> bool:
    """Check if the job title is a software engineering role."""
    lower = title.lower()
    return any(kw in lower for kw in SWE_ROLE_KEYWORDS)


def _is_recent(posted_at: str | int | None, max_age_hours: int) -> bool:
    """Check if a job was posted within max_age_hours."""
    if not posted_at or max_age_hours <= 0:
        return True  # no filter if no timestamp or disabled

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    try:
        if isinstance(posted_at, (int, float)):
            # Lever uses epoch milliseconds
            ts = datetime.fromtimestamp(posted_at / 1000, tz=timezone.utc)
        elif isinstance(posted_at, str):
            # ISO format from Greenhouse/Ashby
            posted_at = posted_at.replace("Z", "+00:00")
            ts = datetime.fromisoformat(posted_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        else:
            return True
        return ts >= cutoff
    except (ValueError, OSError):
        return True  # can't parse → include it


# ---------------------------------------------------------------------------
# Platform-specific scanners
# ---------------------------------------------------------------------------

def scan_lever(company: str, api_url: str, include_kw: list[str], max_age_hours: int = 0) -> list[JobPosting]:
    """Scan a Lever company's job board via API.

    Lever's public API (api.lever.co/v0/postings/{company}) returns JSON with
    no auth required. Uses createdAt (epoch milliseconds) for recency filtering.
    include_kw is a list of entry-level keywords from job_boards.json that
    must appear in title/commitment/team to pass the pre-filter.
    """
    data = _fetch_json(api_url)
    if not data or not isinstance(data, list):
        return []

    jobs = []
    for post in data:
        title = post.get("text", "")
        loc = post.get("categories", {}).get("location", "")
        commitment = post.get("categories", {}).get("commitment", "")
        team = post.get("categories", {}).get("team", "")
        desc_html = post.get("descriptionPlain") or post.get("description", "")
        desc = _strip_html(desc_html) if "<" in desc_html else desc_html
        url = post.get("hostedUrl", "")

        # Recency check (Lever uses createdAt in epoch ms)
        if not _is_recent(post.get("createdAt"), max_age_hours):
            continue

        # Must be a software engineering role
        if not _is_swe_role(title):
            continue

        # Keyword pre-filter on title for new-grad/entry-level signals
        combined = f"{title} {commitment} {team}".lower()
        if include_kw and not _matches_keywords(combined, include_kw):
            continue

        jobs.append(JobPosting(
            url=url,
            title=title,
            company=company.capitalize(),
            location=loc,
            description=desc[:6000],
        ))
    return jobs


def scan_greenhouse(company: str, api_url: str, include_kw: list[str], max_age_hours: int = 0) -> list[JobPosting]:
    """Scan a Greenhouse company's job board via API.

    Greenhouse API (boards-api.greenhouse.io/v1/boards/{company}/jobs)
    returns JSON with job objects nested under a "jobs" key. Uses updated_at
    (ISO format) for recency. Description is in HTML (content field).
    """
    data = _fetch_json(api_url)
    if not data or "jobs" not in data:
        return []

    jobs = []
    for post in data["jobs"]:
        # Recency check (Greenhouse uses updated_at in ISO format)
        if not _is_recent(post.get("updated_at"), max_age_hours):
            continue

        title = post.get("title", "")
        loc_name = post.get("location", {}).get("name", "")
        desc_html = post.get("content", "")
        desc = _strip_html(desc_html) if desc_html else ""
        url = post.get("absolute_url", "")
        departments = ", ".join(d.get("name", "") for d in post.get("departments", []))

        if not _is_swe_role(title):
            continue

        combined = f"{title} {departments}".lower()
        if include_kw and not _matches_keywords(combined, include_kw):
            continue

        jobs.append(JobPosting(
            url=url,
            title=title,
            company=company.capitalize(),
            location=loc_name,
            description=desc[:6000],
        ))
    return jobs


def scan_ashby(company: str, api_url: str, include_kw: list[str], max_age_hours: int = 0) -> list[JobPosting]:
    """Scan an Ashby company's job board via API.

    Ashby API returns jobs under a "jobs" key with publishedAt (ISO format)
    for recency. Location can be a string or dict with a "name" key.
    """
    data = _fetch_json(api_url)
    if not data:
        return []

    job_list = data.get("jobs", [])
    if not job_list:
        return []

    jobs = []
    for post in job_list:
        # Recency check (Ashby uses publishedAt in ISO format)
        if not _is_recent(post.get("publishedAt"), max_age_hours):
            continue

        title = post.get("title", "")
        loc = post.get("location", "")
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        desc = post.get("descriptionPlain") or _strip_html(post.get("descriptionHtml", ""))
        url = post.get("jobUrl") or post.get("applyUrl", "")
        department = post.get("department", "")
        team = post.get("team", "")

        if not _is_swe_role(title):
            continue

        combined = f"{title} {department} {team}".lower()
        if include_kw and not _matches_keywords(combined, include_kw):
            continue

        jobs.append(JobPosting(
            url=url,
            title=title,
            company=company.capitalize(),
            location=loc if isinstance(loc, str) else "",
            description=desc[:6000],
        ))
    return jobs


# ---------------------------------------------------------------------------
# Main scan orchestrator
# ---------------------------------------------------------------------------

def load_job_boards(config: dict) -> dict:
    """Load JobBoards_Links.json."""
    path = config.get("job_boards")
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"JobBoards_Links.json not found at {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# Sources we know about but can't scan automatically. They're listed in the
# Scan Now panel so it's clear what is (and isn't) being aggregated.
UNAVAILABLE_SOURCES = [
    {"key": "instagram_zero2sudo", "label": "Instagram · @zero2sudo stories",
     "url": "https://www.instagram.com/zero2sudo/",
     "note": "Stories need a logged-in Instagram session; not integrated yet"},
]


def scan_sources(config: dict, platforms: list[str] | None = None) -> list[dict]:
    """Every source a scan aggregates from, in scan order, for the UI.

    Each entry: {key, label, url, status, note}. Status starts "pending" for
    sources the scan will hit and "unavailable" for the known-but-unscannable
    ones above.
    """
    sources = []
    if not platforms or "linkedin" in platforms:
        sources.append({"key": "linkedin", "label": "LinkedIn (guest search)",
                        "url": "https://www.linkedin.com/jobs/", "status": "pending", "note": ""})
    if not platforms or "github" in platforms:
        try:
            repos = (load_job_boards(config).get("new_grad_aggregators", {})
                     .get("github_repos", {}).get("repos", {}))
        except FileNotFoundError:
            repos = {}
        for key, info in repos.items():
            if info.get("raw_readme"):
                sources.append({"key": key, "label": info.get("label", key),
                                "url": info.get("url", ""), "status": "pending", "note": ""})
    if not platforms or "notify" in platforms:
        from .notify_careers import credentials
        has_creds = credentials() is not None
        sources.append({"key": "notify_careers", "label": "notify.careers (subscription)",
                        "url": "https://app.notify.careers/postings",
                        "status": "pending" if has_creds else "unavailable",
                        "note": "" if has_creds else "Set NOTIFY_CAREERS_EMAIL / _PASSWORD in .env"})
    sources.extend({**s, "status": "unavailable"} for s in UNAVAILABLE_SOURCES)
    return sources


def scan_all_api_boards(
    config: dict,
    platforms: list[str] | None = None,
    max_age_hours: int = 0,
    on_source=None,
) -> list[tuple[JobPosting, 'FilterResult']]:
    """Main scan orchestrator — scans all platforms and scores every job.

    This is the entry point called by both the CLI (`jobflow scan`) and the
    web dashboard's "Scan Now" button. It iterates through all configured
    platforms, collects JobPostings, and scores each one with evaluate_job().

    Args:
        config: Loaded config dict with job_boards path
        platforms: Filter to specific platforms (e.g., ["linkedin"]), or None for all
        max_age_hours: Only include jobs posted within this window (0 = default;
                       LinkedIn then uses a 4h window). Passed to LinkedIn's
                       f_TPR time filter and used for timestamp filtering on
                       ATS platforms.
        on_source: Optional ``(key, status, count=None, error=None)`` progress
                   callback, keyed like scan_sources().

    Returns:
        List of (JobPosting, FilterResult) tuples — includes both passing and
        rejected jobs so callers can show skip counts.
    """
    boards = load_job_boards(config)
    keywords = boards.get("scraping_tips", {}).get("keyword_filters_for_new_grad", [])

    if max_age_hours > 0:
        console.print(f"[dim]Filtering to jobs posted in the last {max_age_hours} hours[/dim]")

    all_results = []

    # Scan LinkedIn via the guest API (jobflow.linkedin_scraper)
    if not platforms or "linkedin" in platforms:
        terms = merge_search_terms(_user_search_terms())
        console.print(f"\n[bold cyan]Scanning LinkedIn ({len(terms)} search terms)...[/bold cyan]")
        if on_source:
            on_source("linkedin", "running")
        try:
            known = _known_job_urls(config)
            if known:
                console.print(f"  [dim]{len(known)} stored jobs will be skipped if re-listed[/dim]")
            jobs = scan_linkedin(max_age_hours, known_urls=known, search_terms=terms)
        except Exception as e:
            # With a progress callback, one failed source shouldn't sink the
            # whole scan; without one (CLI), keep the original fail-loud path.
            if not on_source:
                raise
            on_source("linkedin", "error", error=str(e))
            jobs = []
        else:
            if on_source:
                on_source("linkedin", "done", count=len(jobs))
        console.print(f"  [green]{len(jobs)} new jobs[/green]")
        for job in jobs:
            result = evaluate_job(job)
            all_results.append((job, result))

    # Scan GitHub new-grad repos
    if not platforms or "github" in platforms:
        ng = boards.get("new_grad_aggregators", {})
        gh_repos = ng.get("github_repos", {})
        if gh_repos:
            console.print("\n[bold cyan]Scanning GitHub new-grad repos...[/bold cyan]")
            # Hold each repo at "running" until its JDs are fetched below.
            parsed: dict[str, int] = {}

            def repo_progress(key, status, count=None, error=None):
                if status == "done":
                    parsed[key] = count or 0
                    status = "running"
                if on_source:
                    on_source(key, status, count=count, error=error)

            jobs = scan_github_repos(gh_repos, keywords, on_source=repo_progress)
            console.print(f"  [green]{len(jobs)} matches[/green]")

            # Like LinkedIn: jobs already stored with a real JD are skipped, so
            # each posting's apply page is fetched once, not every scan.
            known = _known_job_urls(config)
            if known:
                from .linkedin_store import normalize_url
                jobs = [j for j in jobs if normalize_url(j.url) not in known]
            console.print(f"  [dim]Fetching job descriptions for {len(jobs)} new postings...[/dim]")
            from .jd_fetch import enrich_descriptions
            fetched, attempted = enrich_descriptions(jobs)
            console.print(f"  [green]{fetched}/{attempted} descriptions fetched[/green]")

            new_by_source: dict[str, int] = {}
            for j in jobs:
                new_by_source[j.source] = new_by_source.get(j.source, 0) + 1
            for key in parsed:
                src = gh_repos.get("repos", {}).get(key, {}).get("source", "github")
                if on_source:
                    on_source(key, "done", count=new_by_source.get(src, 0))
            for job in jobs:
                result = evaluate_job(job)
                all_results.append((job, result))

    # notify.careers (paid aggregator; needs credentials in .env, so it is
    # skipped quietly where they're absent, e.g. CI)
    if not platforms or "notify" in platforms:
        from .notify_careers import credentials, scan_notify_careers
        if credentials():
            console.print("\n[bold cyan]Scanning notify.careers...[/bold cyan]")
            if on_source:
                on_source("notify_careers", "running")
            try:
                jobs = scan_notify_careers()
            except Exception as e:
                console.print(f"  [red]notify.careers failed: {e}[/red]")
                if on_source:
                    on_source("notify_careers", "error", error=str(e))
                jobs = None
            if jobs is not None:
                known = _known_job_urls(config)
                if known:
                    from .linkedin_store import normalize_url
                    jobs = [j for j in jobs if normalize_url(j.url) not in known]
                from .jd_fetch import enrich_descriptions
                fetched, attempted = enrich_descriptions(jobs)
                console.print(f"  [green]{len(jobs)} new jobs, {fetched}/{attempted} descriptions fetched[/green]")
                if on_source:
                    on_source("notify_careers", "done", count=len(jobs))
                for job in jobs:
                    all_results.append((job, evaluate_job(job)))

    return all_results


# ---------------------------------------------------------------------------
# LinkedIn scanner (guest API via jobflow.linkedin_scraper)
# ---------------------------------------------------------------------------
# Two-phase scan: (1) cheap listing pages for every search term, deduped by
# job id across terms; (2) full descriptions fetched ONLY for jobs the store
# doesn't already have. Descriptions are ~10x the request volume of listings,
# and re-fetching JDs for already-stored jobs is what used to trip LinkedIn's
# rate limiter and silently truncate every scan to a small sample.

LINKEDIN_SEARCH_TERMS = [
    "New Grad Software Engineer",
    "Junior Software Engineer",
    "Associate Software Engineer",
    "Entry Level Jobs 2026",
    "New Grad Machine Learning Engineer",
]

# Upper bound on searches per scan (defaults + users' Settings terms) so a long
# list can't starve the description-fetch budget below.
MAX_SEARCH_TERMS = 15

RESULTS_PER_TERM = 200
# Cap on description fetches per scan so a cold store can't blow past the CI
# job's 15-minute timeout. Anything past the cap is reported, kept with a
# title-only description, and re-fetched by the next scan.
MAX_DESC_FETCHES = 300
DESC_DELAY_S = (0.7, 1.5)
# Hard wall-clock budget for one LinkedIn scan. CI kills the whole job at 15
# minutes and scan results are only written at the end — running past this
# would lose everything, so wind down early and save what we have.
SCAN_TIME_BUDGET_S = 720
# A stored description this short is a placeholder (title fallback / failed
# fetch), not a real JD — treat the job as unknown so its JD is re-fetched.
MIN_STORED_DESC_LEN = 200


def _known_job_urls(config: dict) -> set[str]:
    """Normalized URLs of stored jobs that already have a real description.

    Prefers the DB, falls back to the JSON store; returns an empty set on any
    failure (the scan then degrades to fetching every description — the old
    behavior, minus jobspy's bugs).
    """
    from .linkedin_store import normalize_url

    if os.environ.get("DATABASE_URL"):
        try:
            from . import db
            db.init_db()
            conn = db.get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT url FROM jobs WHERE LENGTH(description_preview) >= %s",
                        (MIN_STORED_DESC_LEN,),
                    )
                    return {normalize_url(r[0]) for r in cur.fetchall()}
            finally:
                db.put_conn(conn)
        except Exception as e:
            console.print(
                f"  [yellow]Could not load known jobs from DB ({e}); "
                f"trying JSON store[/yellow]"
            )

    try:
        from .linkedin_store import load_store
        store_path = config["_root"] / "data" / "ci" / "linkedin_jobs.json"
        jobs = load_store(store_path).get("jobs", {})
        return {
            normalize_url(job.get("url", "") or key)
            for key, job in jobs.items()
            if len(job.get("description_preview") or "") >= MIN_STORED_DESC_LEN
        }
    except Exception:
        return set()


def merge_search_terms(user_terms) -> list[str]:
    """The default searches plus users' Settings terms, deduped
    case-insensitively in order, capped at MAX_SEARCH_TERMS."""
    terms, seen = [], set()
    for term in [*LINKEDIN_SEARCH_TERMS, *user_terms]:
        term = (term or "").strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms[:MAX_SEARCH_TERMS]


def _user_search_terms() -> list[str]:
    """Search terms saved in Settings by every active user (DB backend only).

    The posting pool is shared, so one scan covers everyone's searches; each
    user's own profile then scores the results.
    """
    if not os.environ.get("DATABASE_URL"):
        return []
    try:
        from . import db
        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT p.search_terms FROM user_profiles p "
                    "JOIN users u ON u.id = p.user_id WHERE u.is_active ORDER BY u.id"
                )
                return [t for (terms,) in cur.fetchall() for t in (terms or [])
                        if isinstance(t, str)]
        finally:
            db.put_conn(conn)
    except Exception as e:
        console.print(f"  [yellow]Could not load Settings search terms ({e}); using defaults[/yellow]")
        return []


def scan_linkedin(max_age_hours: int = 0, known_urls: set[str] | None = None,
                  search_terms: list[str] | None = None) -> list[JobPosting]:
    """Scan LinkedIn's guest API. Returns NEW jobs only (not in known_urls).

    Known jobs are skipped entirely rather than re-emitted: merging a job
    without its description would let the merge's per-user re-score overwrite
    a good algo score with one computed from an empty JD, and new users get
    the existing posting pool via db.backfill_user at signup anyway.
    ``search_terms`` defaults to LINKEDIN_SEARCH_TERMS.
    """
    from . import linkedin_scraper as li
    from .linkedin_store import normalize_url

    known_urls = known_urls or set()
    search_terms = search_terms or LINKEDIN_SEARCH_TERMS
    # Default to a 4h window when no limit is specified — matches the CI cron
    # (every 30 min with --hours 4); anything older is already in the store,
    # and a wider window just burns request budget re-listing known jobs.
    hours = max_age_hours if max_age_hours > 0 else 4

    session = li.new_session()
    seen_ids: set[str] = set()
    listings: list[dict] = []
    blocked = False
    deadline = time.monotonic() + SCAN_TIME_BUDGET_S

    # Phase 1 — listings (cheap: ~10 jobs per request).
    for i, term in enumerate(search_terms):
        if time.monotonic() > deadline:
            console.print("  [yellow]Scan time budget reached — skipping remaining search terms.[/yellow]")
            break
        console.print(f"  [dim]Search: \"{term}\"...[/dim]", end=" ")
        try:
            found = li.search_listings(session, term, hours, RESULTS_PER_TERM, seen_ids)
        except li.LinkedInBlocked as e:
            console.print(f"[red]blocked: {e}[/red]")
            blocked = True
            break
        except Exception as e:
            console.print(f"[red]error: {e}[/red]")
            continue
        kept = [
            j for j in found
            if j["title"] and j["company"] and _is_swe_role(j["title"])
        ]
        console.print(f"[green]{len(kept)} kept[/green] [dim]({len(found)} listed)[/dim]")
        listings.extend(kept)
        if i < len(search_terms) - 1:
            time.sleep(random.uniform(2, 4))

    if blocked:
        console.print("  [yellow]Rate-limited — continuing with listings collected so far.[/yellow]")

    new_listings = [j for j in listings if normalize_url(j["url"]) not in known_urls]
    skipped_known = len(listings) - len(new_listings)
    to_fetch = new_listings[:MAX_DESC_FETCHES]
    deferred = len(new_listings) - len(to_fetch)
    console.print(
        f"  [dim]{len(listings)} listings: {skipped_known} already stored, "
        f"{len(new_listings)} new; fetching {len(to_fetch)} descriptions"
        + (f", {deferred} deferred to next scan" if deferred else "")
        + "[/dim]"
    )

    # Phase 2 — descriptions, only for jobs the store doesn't have.
    jobs: list[JobPosting] = []
    budget_reported = False
    for i, listing in enumerate(to_fetch):
        if not blocked and not budget_reported and time.monotonic() > deadline:
            budget_reported = True
            blocked = True  # stop fetching; keep title-only fallbacks below
            console.print(
                f"  [yellow]Scan time budget reached ({i}/{len(to_fetch)} "
                f"descriptions fetched) — remaining jobs kept with title-only "
                f"descriptions, re-fetched next scan.[/yellow]"
            )
        description = ""
        if not blocked:
            try:
                description = li.fetch_description(session, listing["job_id"])
            except li.LinkedInBlocked:
                blocked = True
                console.print(
                    f"  [yellow]Rate-limited during description fetch "
                    f"({i}/{len(to_fetch)} done) — remaining jobs kept with "
                    f"title-only descriptions, re-fetched next scan.[/yellow]"
                )
            if not blocked and i < len(to_fetch) - 1:
                time.sleep(random.uniform(*DESC_DELAY_S))

        date_posted = listing["date_posted"]
        if date_posted and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_posted):
            date_posted += "T00:00:00+00:00"

        jobs.append(JobPosting(
            url=listing["url"],
            title=listing["title"],
            company=listing["company"],
            location=listing["location"],
            description=description[:6000] if description else listing["title"],
            date_posted=date_posted,
            source="linkedin",
        ))
    return jobs


def _fetch_text(url: str, retries: int = 3) -> str | None:
    """Fetch text/HTML from a URL with retry/backoff."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            })
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 3 ** (attempt + 1)))
                console.print(f"  [yellow]Rate limited, waiting {wait}s...[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"  [red]Failed: {e}[/red]")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(3 ** (attempt + 1))
                continue
            console.print(f"  [red]Failed: {e}[/red]")
            return None


# ---------------------------------------------------------------------------
# GitHub new-grad repo scanner
# ---------------------------------------------------------------------------
# Community repos (SimplifyJobs, SpeedyApply, Jobright) keep new-grad postings
# in README tables that are updated several times a day. Each table has a
# company, role, location, apply link and an age column ("3d", "2mo", or a
# "Oct 26" date). The READMEs hold months of history, so only rows younger
# than README_MAX_AGE_DAYS are kept — the store's TTL would expire anything
# older within a day anyway.

README_MAX_AGE_DAYS = 7

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def scan_github_repos(repos_config: dict, include_kw: list[str],
                      on_source=None) -> list[JobPosting]:
    """Scan GitHub new-grad repos by parsing their README tables.

    ``on_source(key, status, count=None, error=None)`` is called as each repo
    starts ("running") and finishes ("done" / "error") so the web UI can show
    per-source progress. Each repo's jobs carry the repo key's ``source`` tag
    (e.g. "simplify"), falling back to "github".
    """
    repos = repos_config.get("repos", {})
    all_jobs = []
    seen: set = set()

    for name, info in repos.items():
        raw_url = info.get("raw_readme", "")
        if not raw_url:
            continue
        if on_source:
            on_source(name, "running")

        console.print(f"  [dim]{name}...[/dim]", end=" ")
        md = _fetch_text(raw_url)
        if not md:
            console.print("[red]failed[/red]")
            if on_source:
                on_source(name, "error", error="README fetch failed")
            continue

        jobs = _parse_github_readme(md, include_kw, seen, source=info.get("source", "github"))
        console.print(f"[green]{len(jobs)} entries[/green]")
        if on_source:
            on_source(name, "done", count=len(jobs))
        all_jobs.extend(jobs)

    return all_jobs


def _parse_age(text: str, now: datetime | None = None) -> datetime | None:
    """Turn a README age cell ("0d", "3d", "2mo", "5h", "1w", "Oct 26") into a
    UTC datetime. Returns None when the cell isn't an age."""
    now = now or datetime.now(timezone.utc)
    t = _strip_html(text).strip().lower()
    m = re.fullmatch(r"(\d+)\s*(h|d|w|mo|y)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"h": n / 24, "d": n, "w": n * 7, "mo": n * 30, "y": n * 365}[unit]
        return now - timedelta(days=days)
    m = re.fullmatch(r"([a-z]{3})\w*\s+(\d{1,2})", t)
    if m and m.group(1) in _MONTHS:
        try:
            ts = now.replace(month=_MONTHS[m.group(1)], day=int(m.group(2)),
                             hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
        if ts > now + timedelta(days=1):   # "Dec 30" seen in January → last year
            ts = ts.replace(year=ts.year - 1)
        return ts
    return None


def _parse_github_readme(md: str, include_kw: list[str], seen: set,
                         source: str = "github",
                         max_age_days: int = README_MAX_AGE_DAYS) -> list[JobPosting]:
    """Parse job tables from SimplifyJobs (HTML <tr>/<td>) and SpeedyApply /
    Jobright (markdown |) READMEs.

    Simplify marks extra roles at the same company with a "↳" company cell, so
    the previous row's company is carried forward.
    """
    rows: list[list[str]] = []
    html_rows = re.findall(r'<tr>(.*?)</tr>', md, re.DOTALL)
    for row in html_rows:
        cells = re.findall(r'<td>(.*?)</td>', row, re.DOTALL)
        if len(cells) >= 3:
            rows.append(cells)

    for line in md.split("\n"):
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|\s*:?-{3,}", line):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 3 or cols[0].lower() in ("company", "**company**"):
            continue
        rows.append(cols)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days) if max_age_days > 0 else None
    jobs = []
    prev_company = ""
    for cols in rows:
        if _strip_html(cols[0]).strip() == "↳":
            cols = [prev_company] + cols[1:]
        else:
            prev_company = cols[0]

        posted = _parse_age(cols[-1], now)
        if cutoff and posted and posted < cutoff:
            continue
        job = _parse_table_row(cols, seen, source=source)
        if job:
            if posted:
                job.date_posted = posted.isoformat()
            jobs.append(job)

    return jobs


def _extract_apply_url(cols: list[str]) -> str:
    """Extract the apply/job-posting URL from table columns.

    Each GitHub repo puts the apply link in a different place:
    - SimplifyJobs (HTML <td>): column 4 has the apply <a href>, skip simplify.jobs/c/ company profiles
    - Jobright (markdown |): column 2 has [Title](jobright.ai/...) — the apply link is IN the title
    - SpeedyApply (markdown | with HTML): last column with an <a href> img is the apply button

    Strategy: look for hrefs in columns 3+ first (apply columns), fall back to column 2 (title link).
    Skip simplify.jobs/c/ (company profiles) and bare homepages (no path).
    """
    # First pass: search columns 3+ for apply links (where apply buttons live)
    for col in cols[3:] if len(cols) > 3 else []:
        hrefs = re.findall(r'href=["\']?(https?://[^"\'> ]+)', col)
        for href in hrefs:
            if "simplify.jobs/c/" in href:
                continue
            cleaned = href.split("?utm_source")[0].split("&utm_source")[0]
            if urlparse(cleaned).path.rstrip("/"):
                return cleaned

    # Second pass: column 2 (role/title) — Jobright puts the apply link here
    if len(cols) > 1:
        # Check for markdown link [Title](url)
        url_m = re.search(r'\[.*?\]\((https?://[^)]+)\)', cols[1])
        if url_m:
            cleaned = url_m.group(1).split("?utm_source")[0].split("&utm_source")[0]
            if urlparse(cleaned).path.rstrip("/"):
                return cleaned
        # Check for HTML href in title column
        hrefs = re.findall(r'href=["\']?(https?://[^"\'> ]+)', cols[1])
        for href in hrefs:
            cleaned = href.split("?utm_source")[0].split("&utm_source")[0]
            if urlparse(cleaned).path.rstrip("/"):
                return cleaned

    # Third pass: column 3 (sometimes apply is here)
    if len(cols) > 2:
        hrefs = re.findall(r'href=["\']?(https?://[^"\'> ]+)', cols[2])
        for href in hrefs:
            if "simplify.jobs/c/" in href:
                continue
            cleaned = href.split("?utm_source")[0].split("&utm_source")[0]
            if urlparse(cleaned).path.rstrip("/"):
                return cleaned

    return ""


def _md_text(cell: str) -> str:
    """Plain text of a README cell: markdown links → their text, HTML stripped."""
    cell = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', cell)
    cell = cell.replace("</br>", ", ").replace("<br>", ", ")
    return re.sub(r"\s+", " ", _strip_html(cell)).strip(" *")


def _parse_table_row(cols: list[str], seen: set, source: str = "github") -> JobPosting | None:
    """Parse a single table row (HTML or markdown) into a JobPosting."""
    # Skip closed
    if any("🔒" in c for c in cols):
        return None

    company = _md_text(cols[0]).replace("🔥", "").strip(" *")
    role = _md_text(cols[1])
    location = _md_text(cols[2]) if len(cols) > 2 else ""
    # Simplify collapses multi-location rows into "<summary>4 locations</summary>A, B…"
    location = re.sub(r"^\d+ locations\s*", "", location)

    if not company or not role:
        return None
    # Must be SWE-related
    if not _is_swe_role(role):
        return None

    # Extract application URL from the correct column
    url = _extract_apply_url(cols)

    # Dedup across repos: same posting URL, or same company+role+location
    key = url.lower() if url else f"{company}_{role}_{location}".lower()
    if key in seen:
        return None
    seen.add(key)

    return JobPosting(
        url=url,
        title=role,
        company=company,
        location=location,
        description=f"{role} at {company}. Location: {location}",
        source=source,
    )


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------
# seen_jobs.json tracks which jobs we've already processed to avoid showing
# the same job twice across scans. Format: {"url_or_key": "EST_timestamp"}.
#
# The 48-hour TTL ensures that:
# 1. Jobs that get reposted/updated resurface after 2 days
# 2. The file doesn't grow unboundedly (was 5,573 entries before TTL was added)
# 3. If a job appears in multiple search terms, it's still deduped within a scan
#
# All timestamps use US/Eastern timezone for consistency with the user.

SEEN_TTL_HOURS = 48
EST = ZoneInfo("US/Eastern")

def load_seen_jobs(config: dict) -> dict[str, str]:
    """Load previously seen job URLs, pruning entries older than 48h."""
    if is_db_enabled():
        try:
            from .db import load_seen_jobs as db_load
            return db_load()
        except Exception:
            pass
    path = config["output_dir"] / "seen_jobs.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Backward compat: convert old list format to dict with current timestamp
    if isinstance(data, list):
        now = datetime.now(EST).isoformat()
        data = {url: now for url in data}
    # Prune expired entries
    cutoff = datetime.now(EST) - timedelta(hours=SEEN_TTL_HOURS)
    return {
        url: ts for url, ts in data.items()
        if datetime.fromisoformat(ts) > cutoff
    }


def save_seen_jobs(config: dict, seen: dict[str, str]) -> None:
    """Save seen job URLs with timestamps."""
    if is_db_enabled():
        try:
            from .db import save_seen_jobs_bulk as db_save
            db_save(seen)
            return
        except Exception:
            pass
    path = config["output_dir"] / "seen_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(seen.items())), f, indent=2)


def deduplicate_results(
    results: list[tuple[JobPosting, 'FilterResult']],
    seen: dict[str, str],
) -> tuple[list[tuple[JobPosting, 'FilterResult']], dict[str, str]]:
    """Remove already-seen jobs. Returns (new_results, updated_seen_dict)."""
    from .linkedin_store import normalize_url
    new_results = []
    now = datetime.now(EST).isoformat()
    for job, filt in results:
        key = normalize_url(job.url) if job.url else f"{job.company}_{job.title}"
        if key not in seen:
            seen[key] = now
            new_results.append((job, filt))
    return new_results, seen


def print_scan_results(results: list[tuple[JobPosting, 'FilterResult']]) -> None:
    """Pretty-print scan results in a table."""
    if not results:
        console.print("[yellow]No jobs found.[/yellow]")
        return

    # Split for display only: jobs flagged by a hard-reject rule carry a
    # reject_reason. They are still kept downstream (the AI scorer can rescue
    # them), but showing them apart from clean matches keeps the table readable.
    relevant = [(j, r) for j, r in results if not r.reject_reason]
    flagged = [(j, r) for j, r in results if r.reject_reason]

    if relevant:
        table = Table(title=f"Relevant Jobs ({len(relevant)})", border_style="green")
        table.add_column("#", style="dim", width=4)
        table.add_column("Company", style="cyan", max_width=15)
        table.add_column("Role", max_width=40)
        table.add_column("Location", max_width=20)
        table.add_column("Score", justify="right", width=6)
        table.add_column("Variant", width=8)
        table.add_column("URL", max_width=50)

        for i, (job, filt) in enumerate(sorted(relevant, key=lambda x: x[1].score, reverse=True), 1):
            table.add_row(
                str(i), job.company, job.title, job.location,
                str(filt.score), filt.resume_variant,
                job.url[:50] + "..." if len(job.url) > 50 else job.url,
            )
        console.print(table)

    if flagged:
        console.print(f"\n[dim]Flagged by filter rules: {len(flagged)} jobs (sponsorship, senior-level, non-US, etc.) — kept for AI re-scoring[/dim]")

    console.print(f"\n[bold]Total: {len(results)} scanned, {len(relevant)} relevant, {len(flagged)} flagged[/bold]")
