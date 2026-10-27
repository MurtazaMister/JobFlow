"""LinkedIn jobs persistent store — the canonical job database.

Dual-backend: uses PostgreSQL (Neon) when DATABASE_URL is set, falls back
to JSON files otherwise. This allows local dev without a database while
Render + CI use persistent storage.

When DATABASE_URL is set:
    - All public functions delegate to jobflow.db
    - TTL: jobs auto-expire after 3 days unless status is Tracking/Applied
    - No file I/O — all data lives in PostgreSQL

When DATABASE_URL is NOT set:
    - JSON file behavior (data/ci/linkedin_jobs.json)
    - Same 3-day TTL semantics as PostgreSQL using expires_at

Key operations:
    - merge_scan_results(): Integrates new scan results with deduplication
    - prune_old_jobs(): Removes expired jobs using DB-equivalent TTL semantics
    - get_filtered_jobs(): Returns sorted, filtered job list for the dashboard
    - get_time_counts(): Computes dynamic time buckets for the sidebar
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .sponsors import SPONSOR_STATUS, is_top_sponsor

_DISABLE_DB_VALUES = {"1", "true", "yes", "on"}


def is_db_enabled() -> bool:
    """Return whether PostgreSQL should be used instead of JSON storage."""
    disabled = os.environ.get("JOBFLOW_DISABLE_DB", "").lower() in _DISABLE_DB_VALUES
    return bool(os.environ.get("DATABASE_URL")) and not disabled


STORE_LOCK = threading.RLock()


def normalize_url(url: str) -> str:
    """Canonicalize a job URL for use as a dedup key.

    Strips query string, fragment, trailing slash, and lowercases scheme/host.
    Every source we scan (LinkedIn, Greenhouse, Lever, Ashby, GitHub) carries
    the job identity in the URL path — query strings only hold tracking tokens
    (trk, utm_*, gh_src, refId, etc.), so dropping them collapses the same job
    seen via email alerts, search results, and direct links onto one row.
    """
    if not url:
        return url
    try:
        p = urlparse(url.strip())
    except (ValueError, TypeError):
        return url
    if not p.scheme or not p.netloc:
        return url
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))

LINKEDIN_STATUSES = ["Tracking", "Applied", "Not Interested"]
RETENTION_DAYS = 3
# Jobs with these statuses survive TTL pruning — the user explicitly
# marked them as important, so we keep them regardless of age.
KEEP_STATUSES = {"Tracking", "Applied"}


def load_store(path: Path) -> dict:
    """Load linkedin_jobs.json. Returns empty store if missing."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            pass
    return {"jobs": {}, "last_updated": ""}


def save_store(path: Path, store: dict) -> None:
    """Write linkedin_jobs.json."""
    store["last_updated"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    tmp.replace(path)


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp string to datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _expires_at_for_status(status: str, base: datetime | None = None) -> str | None:
    """Return JSON expires_at value using the same status policy as the DB."""
    if status in KEEP_STATUSES:
        return None
    base = base or datetime.now(timezone.utc)
    return (base + timedelta(days=RETENTION_DAYS)).isoformat()


def _lookup_key(jobs: dict, key: str) -> str | None:
    """Resolve a UI/API key to the stored JSON key, accepting canonical URLs."""
    if key in jobs:
        return key
    normalized = normalize_url(key)
    if normalized in jobs:
        return normalized
    return None


def _dedup_key(entry: dict) -> str:
    """Generate a dedup key: prefer URL (canonicalized), fall back to company+title."""
    url = normalize_url(entry.get("url", ""))
    if url:
        return url
    co = entry.get("company", "").lower().strip()
    title = entry.get("title", "").lower().strip()
    return f"{co}_{title}"


def _rescore_entry(entry: dict, profile=None) -> dict:
    """Re-score a store dict entry using the canonical filter.evaluate_job().

    This is the single re-scoring path that runs on every merge so filter
    changes (updated rules, new synergy combos, a different user's profile)
    retroactively apply to stored jobs. It builds a JobPosting from the entry,
    runs evaluate_job() with the given ``profile`` (defaults to DEFAULT_PROFILE,
    i.e. the original single-user behaviour), then copies the FilterResult back
    onto the dict. Routing through evaluate_job() keeps this in lockstep with
    scan-time scoring instead of duplicating the hard-reject pipeline.

    AI-score override (unique to the store): if the entry has an ai_score (from
    scripts/ai_score_local.py or jobflow/ai_scorer.py), it takes priority —
    ai_score (0-10) becomes score_pct (0-100%) and only AI-scored jobs with
    ai_score >= 7 are "recommended". Hard-rejected jobs stay at score_pct 0.
    """
    from .filter import evaluate_job, algo_recommended, DEFAULT_PROFILE
    from .models import JobPosting

    profile = profile if profile is not None else DEFAULT_PROFILE

    job = JobPosting(
        url=entry.get("url", ""),
        title=entry.get("title", ""),
        company=entry.get("company", ""),
        location=entry.get("location", ""),
        # The store keeps only a preview of the JD; score against what we have.
        description=entry.get("description_preview", ""),
    )
    result = evaluate_job(job, first_seen=entry.get("first_seen"), profile=profile)

    entry["score"] = result.score
    # The breakdown and variant must track the profile used here, or per-user
    # re-scores (backfill, Settings saves) leave a blank/stale match tooltip.
    entry["reason"] = result.reason
    entry["variant"] = result.resume_variant
    entry["level"] = result.level
    entry["min_exp"] = result.min_exp
    entry["max_exp"] = result.max_exp
    entry["competition"] = result.competition
    entry["keyword_hits"] = result.keyword_hits

    if result.reject_reason:
        # Hard-rejected jobs stay at 0 and are never recommended (the AI scorer
        # only sees non-rejected jobs at scan time).
        entry["score_pct"] = 0
        entry["recommended"] = False
        entry["reject_reason"] = result.reject_reason
        return entry

    # Match %: AI score (0-10 → 0-100%) takes priority, algo is the fallback.
    ai_score = entry.get("ai_score")
    if ai_score is not None:
        entry["score_pct"] = int(ai_score) * 10
        entry["recommended"] = int(ai_score) >= 7
    else:
        entry["score_pct"] = result.score_pct
        entry["recommended"] = algo_recommended(result.score_pct, result.level, profile)
    entry.pop("reject_reason", None)
    return entry


def merge_scan_results(store: dict, scan_results: list[dict]) -> dict:
    """Merge new jobs into the store with deduplication and re-scoring.

    Two-phase merge:
    1. Pre-dedup scan results by company+title (LinkedIn often returns the same
       job in multiple locations — keep the one with a URL)
    2. Merge each job into the store:
       - Existing job: update last_seen, carry AI scores, preserve user status
       - New job: create entry with first_seen = date_posted (or merge time)

    All jobs are re-scored using _rescore_entry() so filter improvements
    apply retroactively.
    """
    now = datetime.now(timezone.utc).isoformat()
    jobs = store.get("jobs", {})

    # Re-key URL-bearing entries whose stored key predates URL canonicalization
    # (e.g. old keys with tracking params). Entries with empty/non-URL keys are
    # left alone — their storage key may have been hand-assigned. When two keys
    # canonicalize to the same URL, merge to preserve user status / AI scores.
    def _score(j):
        return (
            1 if j.get("status") else 0,
            1 if j.get("ai_score") is not None else 0,
            len(j.get("description_preview") or ""),
        )

    rekeyed: dict = {}
    for old_key, job in jobs.items():
        stored_url = job.get("url", "")
        new_key = normalize_url(stored_url) if stored_url else old_key
        if not new_key:
            new_key = old_key
        existing = rekeyed.get(new_key)
        if existing is None:
            rekeyed[new_key] = job
            continue
        winner, loser = (job, existing) if _score(job) > _score(existing) else (existing, job)
        if not winner.get("status") and loser.get("status"):
            winner["status"] = loser["status"]
        if winner.get("ai_score") is None and loser.get("ai_score") is not None:
            winner["ai_score"] = loser["ai_score"]
            winner["ai_reason"] = loser.get("ai_reason", "")
        rekeyed[new_key] = winner
    jobs = rekeyed

    # Pre-dedup scan results: group by company+title, keep best per group
    seen_combos: dict[str, dict] = {}
    for entry in scan_results:
        co = entry.get("company", "").lower().strip()
        title = entry.get("title", "").lower().strip()
        combo = f"{co}|{title}"
        if combo in seen_combos:
            # Keep the one with a URL, or the first one
            if not seen_combos[combo].get("url") and entry.get("url"):
                seen_combos[combo] = entry
        else:
            seen_combos[combo] = entry
    deduped_results = list(seen_combos.values())

    dismissed = set(store.get("dismissed", []))
    dismissed = {normalize_url(k) for k in dismissed if k}

    for entry in deduped_results:
        key = _dedup_key(entry)
        if not key or key in dismissed:
            continue

        if key in jobs:
            jobs[key]["last_seen"] = now
            # Migrate old statuses
            if jobs[key].get("status") in ("Should Apply", "New"):
                jobs[key]["status"] = ""
            # Keep user status, update description if better
            if entry.get("description_preview") and len(entry.get("description_preview", "")) > len(jobs[key].get("description_preview", "")):
                jobs[key]["description_preview"] = entry["description_preview"]
            # Update date_posted if we now have one and didn't before
            if entry.get("date_posted") and not jobs[key].get("date_posted"):
                jobs[key]["date_posted"] = entry["date_posted"]
                jobs[key]["first_seen"] = entry["date_posted"]
            # Fill in source if missing (older rows pre-source field)
            if entry.get("source") and not jobs[key].get("source"):
                jobs[key]["source"] = entry["source"]
            # Carry AI scores from new scan if not already present
            if entry.get("ai_score") and not jobs[key].get("ai_score"):
                jobs[key]["ai_score"] = entry["ai_score"]
                jobs[key]["ai_reason"] = entry.get("ai_reason", "")
            jobs[key]["expires_at"] = _expires_at_for_status(jobs[key].get("status", ""))
            # Re-score with latest logic (preserves ai_score/ai_reason)
            jobs[key] = _rescore_entry(jobs[key])
        else:
            # Use date_posted from source if available, else merge time
            posted = entry.get("date_posted", "") or now
            jobs[key] = {
                "company": entry.get("company", ""),
                "title": entry.get("title", ""),
                "location": entry.get("location", ""),
                "url": entry.get("url", ""),
                "description_preview": entry.get("description_preview", ""),
                "variant": entry.get("variant", "se"),
                "reason": entry.get("reason", ""),
                "status": "",
                "first_seen": posted,
                "last_seen": now,
                "expires_at": _expires_at_for_status(""),
                "date_posted": entry.get("date_posted", ""),
                "search_term": entry.get("search_term", ""),
                "source": entry.get("source", "linkedin"),
                # AI scores from scan (if available)
                "ai_score": entry.get("ai_score"),
                "ai_reason": entry.get("ai_reason", ""),
                # Placeholders — _rescore_entry fills these
                "score": 0, "score_pct": 0, "level": "Unknown",
                "min_exp": None, "max_exp": None, "competition": 0,
                "keyword_hits": 0, "recommended": False,
            }
            jobs[key] = _rescore_entry(jobs[key])

    store["jobs"] = jobs
    return store


def prune_old_jobs(store: dict, days: int = RETENTION_DAYS) -> dict:
    """Remove expired jobs unless they have a keep-status.

    New JSON rows carry expires_at, matching the Postgres backend. Older JSON
    rows are interpreted from last_seen + days so legacy stores still prune.
    """
    now = datetime.now(timezone.utc)
    jobs = store.get("jobs", {})
    pruned = {}
    for key, job in jobs.items():
        if job.get("status") in KEEP_STATUSES:
            job["expires_at"] = None
            pruned[key] = job
            continue

        expires_at = _parse_iso(job.get("expires_at", ""))
        if expires_at is None:
            last_seen = _parse_iso(job.get("last_seen", ""))
            if last_seen is None:
                last_seen = now
            expires_at = last_seen + timedelta(days=days)
            job["expires_at"] = expires_at.isoformat()
        if expires_at >= now:
            pruned[key] = job
    store["jobs"] = pruned
    return store


def update_job_status(store: dict, key: str, status: str) -> bool:
    """Update a job's status. Returns True if found."""
    jobs = store.get("jobs", {})
    stored_key = _lookup_key(jobs, key)
    if stored_key and (status in LINKEDIN_STATUSES or status == ""):
        jobs[stored_key]["status"] = status
        jobs[stored_key]["expires_at"] = _expires_at_for_status(status)
        return True
    return False


def delete_job(store: dict, key: str) -> bool:
    """Delete a JSON job and remember its canonical key so it does not reappear."""
    jobs = store.get("jobs", {})
    stored_key = _lookup_key(jobs, key)
    dismissed_key = normalize_url(key)
    if dismissed_key:
        dismissed = {normalize_url(k) for k in store.get("dismissed", []) if k}
        dismissed.add(dismissed_key)
        store["dismissed"] = sorted(dismissed)
    if stored_key:
        del jobs[stored_key]
        return True
    return False


def get_filtered_jobs(
    store: dict,
    status: str = "",
    level: str = "",
    query: str = "",
    search_term: str = "",
    time_range: str = "",
    bucket_filter: str = "",
    sort_col: str = "first_seen",
    sort_dir: str = "desc",
    tz_offset: int = 0,
    source: str = "",
    limit: int | None = 250,
) -> list[dict]:
    """Return jobs as a sorted list with multi-dimensional filtering.

    This powers the main /linkedin dashboard table. All filters are AND-combined.
    The tz_offset parameter enables timezone-aware time filters — "today" means
    the user's local midnight, not UTC midnight.

    Args:
        status: Filter by user status ("Tracking", "Applied", "Not Interested",
                "Recommended", "FAANG" — virtual statuses: AI score / top H-1B sponsor company)
        level: Filter by detected seniority ("New Grad", "Entry", "Mid", "Unknown")
        query: Free-text search across company, title, location
        search_term: Filter by the LinkedIn search term that found this job
        time_range: "hour" (last 1h), "today" (since local midnight), "yesterday"
        bucket_filter: Exact bucket key like "2026-04-14_13:30" for drill-down
        sort_col: Column to sort by (default: last_seen)
        sort_dir: "asc" or "desc"
        tz_offset: Browser's timezone offset in minutes from UTC (JS: new Date().getTimezoneOffset())

    Sorting: "Not Interested" jobs are always pushed to the bottom regardless
    of sort order, since the user has dismissed them.
    """
    jobs = store.get("jobs", {})
    result = []
    q_lower = query.lower().strip() if query else ""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    # Compute time boundaries in UTC based on user's local time
    time_cutoff = None
    time_end = None
    if time_range == "hour":
        time_cutoff = now_utc - timedelta(hours=1)
    elif time_range == "today":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_cutoff = local_midnight.astimezone(timezone.utc)
    elif time_range == "yesterday":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_cutoff = (local_midnight - timedelta(days=1)).astimezone(timezone.utc)
        time_end = local_midnight.astimezone(timezone.utc)

    # Bucket filter: dynamic-size bucket by key (e.g. "2026-04-14_13:30")
    bucket_start_utc = None
    bucket_end_utc = None
    if bucket_filter:
        try:
            # Parse "YYYY-MM-DD_HH:MM" as local time
            local_start = datetime.strptime(bucket_filter, "%Y-%m-%d_%H:%M").replace(tzinfo=user_tz)
            bm = _bucket_minutes(local_start)
            bucket_start_utc = local_start.astimezone(timezone.utc)
            bucket_end_utc = bucket_start_utc + timedelta(minutes=bm)
        except (ValueError, TypeError):
            pass

    for key, job in jobs.items():
        entry = {"_key": key, **job}

        # Source filter (linkedin vs github)
        if source and (entry.get("source") or "linkedin") != source:
            continue

        # Time range filter
        if time_cutoff or bucket_start_utc:
            fs = _parse_iso(entry.get("first_seen", ""))
            if not fs:
                continue
            if time_cutoff and fs < time_cutoff:
                continue
            if time_end and fs >= time_end:
                continue
            if bucket_start_utc and (fs < bucket_start_utc or fs >= bucket_end_utc):
                continue

        # Status filter (special case: "Recommended" is not a real status)
        if status == "Recommended":
            if not entry.get("recommended") or entry.get("status") == "Not Interested":
                continue
        elif status == SPONSOR_STATUS:
            if not is_top_sponsor(entry.get("company", "")) or entry.get("status") == "Not Interested":
                continue
        elif status and entry.get("status") != status:
            continue

        # Level filter
        if level and level != "All" and entry.get("level", "Unknown") != level:
            continue

        # Text search (company, title, location)
        if q_lower:
            searchable = f"{entry.get('company', '')} {entry.get('title', '')} {entry.get('location', '')}".lower()
            if q_lower not in searchable:
                continue

        # Search term filter
        if search_term and entry.get("search_term", "") != search_term:
            continue

        # Add computed recency display
        entry["_recency"] = format_recency(entry.get("first_seen", ""))

        result.append(entry)

    def primary_key(j):
        val = j.get(sort_col, "")
        if sort_col in ("score", "score_pct", "competition", "min_exp", "ai_score"):
            return int(val) if val is not None and val != "" else -1
        return str(val or "")

    # Applied / Not Interested always at bottom, then custom sort
    bottom = [j for j in result if j.get("status") in ("Applied", "Not Interested")]
    rest = [j for j in result if j.get("status") not in ("Applied", "Not Interested")]

    # Stable layered sort: primary respects requested direction, while quality
    # tie-breakers always prefer better/newer jobs regardless of primary direction.
    def apply_sort(items):
        items.sort(key=lambda j: int(j.get("score_pct", 0) or 0), reverse=True)
        if sort_col == "ai_score":
            items.sort(key=lambda j: str(j.get("first_seen", "") or ""), reverse=True)
        else:
            items.sort(key=lambda j: int(j.get("ai_score") or 0), reverse=True)
        items.sort(key=primary_key, reverse=(sort_dir == "desc"))

    apply_sort(rest)
    apply_sort(bottom)
    combined = rest + bottom
    return combined[:limit] if limit else combined


def get_status_counts(store: dict, source: str = "") -> dict[str, int]:
    """Return count of jobs per status + recommended count."""
    counts = {s: 0 for s in LINKEDIN_STATUSES}
    counts["Recommended"] = 0
    counts[SPONSOR_STATUS] = 0
    total = 0
    for job in store.get("jobs", {}).values():
        if source and (job.get("source") or "linkedin") != source:
            continue
        total += 1
        s = job.get("status", "")
        if s in counts:
            counts[s] += 1
        if job.get("recommended") and s != "Not Interested":
            counts["Recommended"] += 1
        if s != "Not Interested" and is_top_sponsor(job.get("company", "")):
            counts[SPONSOR_STATUS] += 1
    counts["All"] = total
    return counts


def get_level_counts(store: dict, source: str = "") -> dict[str, int]:
    """Return count of jobs per level tag."""
    counts = {"All": 0, "New Grad": 0, "Entry": 0, "Mid": 0, "Unknown": 0}
    for job in store.get("jobs", {}).values():
        if source and (job.get("source") or "linkedin") != source:
            continue
        level = job.get("level", "Unknown")
        if level in counts:
            counts[level] += 1
        else:
            counts["Unknown"] += 1
    counts["All"] = sum(v for k, v in counts.items() if k != "All")
    return counts


def get_filtered_counts(
    store: dict,
    time_range: str = "",
    bucket_filter: str = "",
    tz_offset: int = 0,
    query: str = "",
    search_term: str = "",
    source: str = "",
) -> dict:
    """Compute status + level counts respecting time/search filters.

    Returns {"status": {All, Recommended, Tracking, ...}, "level": {All, New Grad, ...}}.
    Uses the same time boundary logic as get_filtered_jobs().
    """
    jobs = store.get("jobs", {})
    q_lower = query.lower().strip() if query else ""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    # Compute time boundaries (same logic as get_filtered_jobs)
    time_cutoff = None
    time_end = None
    if time_range == "hour":
        time_cutoff = now_utc - timedelta(hours=1)
    elif time_range == "today":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_cutoff = local_midnight.astimezone(timezone.utc)
    elif time_range == "yesterday":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_cutoff = (local_midnight - timedelta(days=1)).astimezone(timezone.utc)
        time_end = local_midnight.astimezone(timezone.utc)

    bucket_start_utc = None
    bucket_end_utc = None
    if bucket_filter:
        try:
            local_start = datetime.strptime(bucket_filter, "%Y-%m-%d_%H:%M").replace(tzinfo=user_tz)
            bm = _bucket_minutes(local_start)
            bucket_start_utc = local_start.astimezone(timezone.utc)
            bucket_end_utc = bucket_start_utc + timedelta(minutes=bm)
        except (ValueError, TypeError):
            pass

    status_counts = {s: 0 for s in LINKEDIN_STATUSES}
    status_counts["Recommended"] = 0
    status_counts[SPONSOR_STATUS] = 0
    level_counts = {"New Grad": 0, "Entry": 0, "Mid": 0, "Unknown": 0}
    total = 0

    for job in jobs.values():
        # Apply source filter
        if source and (job.get("source") or "linkedin") != source:
            continue

        # Apply time filter
        if time_cutoff or bucket_start_utc:
            fs = _parse_iso(job.get("first_seen", ""))
            if not fs:
                continue
            if time_cutoff and fs < time_cutoff:
                continue
            if time_end and fs >= time_end:
                continue
            if bucket_start_utc and (fs < bucket_start_utc or fs >= bucket_end_utc):
                continue

        # Apply text search
        if q_lower:
            searchable = f"{job.get('company', '')} {job.get('title', '')} {job.get('location', '')}".lower()
            if q_lower not in searchable:
                continue

        # Apply search term filter
        if search_term and job.get("search_term", "") != search_term:
            continue

        # Job passed time/search filters — count it
        total += 1
        s = job.get("status", "")
        if s in status_counts:
            status_counts[s] += 1
        if job.get("recommended") and s != "Not Interested":
            status_counts["Recommended"] += 1
        if s != "Not Interested" and is_top_sponsor(job.get("company", "")):
            status_counts[SPONSOR_STATUS] += 1
        level = job.get("level", "Unknown")
        if level in level_counts:
            level_counts[level] += 1
        else:
            level_counts["Unknown"] += 1

    status_counts["All"] = total
    level_counts["All"] = total
    return {"status": status_counts, "level": level_counts}


def get_search_terms(store: dict, source: str = "") -> list[str]:
    """Return sorted list of distinct search_term values."""
    terms = set()
    for job in store.get("jobs", {}).values():
        if source and (job.get("source") or "linkedin") != source:
            continue
        t = job.get("search_term", "")
        if t:
            terms.add(t)
    return sorted(terms)


def format_recency(iso_timestamp: str) -> str:
    """Server-side fallback: 'Xh ago (HH:MM UTC)'. JS overrides with local time."""
    if not iso_timestamp:
        return "--"
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(tz=timezone.utc) - dt
        hours = diff.total_seconds() / 3600
        clock = dt.strftime("%I:%M %p").lstrip("0")
        if hours < 1:
            rel = "just now"
        elif hours < 24:
            rel = f"{int(hours)}h ago"
        elif diff.days == 1:
            rel = "1d ago"
        elif diff.days < 7:
            rel = f"{diff.days}d ago"
        else:
            return dt.strftime("%b %d")
        return f"{rel} ({clock})"
    except (ValueError, TypeError):
        return "--"


def _bucket_minutes(local_dt: datetime) -> int:
    """Return the time-bucket size in minutes — always 30.

    The CI scan cron runs every 30 minutes (`*/30 * * * *`), so each bucket maps
    to roughly one scan window. The argument is accepted (and the indirection
    kept) only so callers don't need to special-case a constant.
    """
    return 30


def _bucket_start(local_dt: datetime) -> datetime:
    """Snap a local datetime down to its 30-minute bucket start (:00 or :30)."""
    m = 0 if local_dt.minute < 30 else 30
    return local_dt.replace(minute=m, second=0, microsecond=0)


def _bucket_label(local_dt: datetime, bm: int) -> str:
    """Human-readable label for a bucket."""
    return local_dt.strftime("%I:%M %p").lstrip("0")


def _bucket_key(local_dt: datetime) -> str:
    """Unique key for a bucket: 'YYYY-MM-DD_HH:MM'."""
    return local_dt.strftime("%Y-%m-%d_%H:%M")


def get_time_counts(store: dict, tz_offset: int = 0, time_range: str = "", source: str = "") -> dict:
    """Return counts for time range tabs and dynamic bucket breakdown.

    Powers the sidebar time filters on the /linkedin page. Returns:
    - this_hour: count of jobs first_seen in the last 60 minutes
    - today: count since user's local midnight
    - yesterday: count from yesterday (local time)
    - buckets: list of {key, label, count, minutes, start_iso} matching the active time range

    Buckets are a uniform 30 minutes (see _bucket_minutes), matching the
    every-30-minute CI scan cron so each bucket ≈ one scan window.
    """
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = local_midnight.astimezone(timezone.utc)
    yesterday_start_utc = (local_midnight - timedelta(days=1)).astimezone(timezone.utc)
    one_hour_ago = now_utc - timedelta(hours=1)

    # Determine bucket window based on active time range
    if time_range == "hour":
        bucket_range_start = one_hour_ago
        bucket_range_end = None
    elif time_range == "today":
        bucket_range_start = today_start_utc
        bucket_range_end = None
    elif time_range == "yesterday":
        bucket_range_start = yesterday_start_utc
        bucket_range_end = today_start_utc
    else:
        bucket_range_start = None
        bucket_range_end = None

    hour_count = 0
    today_count = 0
    yesterday_count = 0

    buckets = {}

    for job in store.get("jobs", {}).values():
        if source and (job.get("source") or "linkedin") != source:
            continue
        fs = _parse_iso(job.get("first_seen", ""))
        if not fs:
            continue
        if fs >= one_hour_ago:
            hour_count += 1
        if fs >= today_start_utc:
            today_count += 1
        if yesterday_start_utc <= fs < today_start_utc:
            yesterday_count += 1

        # Check if this job falls within the bucket window
        in_range = True
        if bucket_range_start and fs < bucket_range_start:
            in_range = False
        if bucket_range_end and fs >= bucket_range_end:
            in_range = False

        if in_range:
            fs_local = fs.astimezone(user_tz)
            bs = _bucket_start(fs_local)
            bk = _bucket_key(bs)
            if bk not in buckets:
                bm = _bucket_minutes(bs)
                buckets[bk] = {
                    "key": bk,
                    "label": _bucket_label(bs, bm),
                    "count": 0,
                    "minutes": bm,
                    "start_iso": bs.isoformat(),
                }
            buckets[bk]["count"] += 1

    bucket_list = sorted(buckets.values(), key=lambda b: b["start_iso"], reverse=True)

    return {
        "this_hour": hour_count,
        "today": today_count,
        "yesterday": yesterday_count,
        "buckets": bucket_list,
    }


def backfill_job(job: dict) -> dict:
    """Re-score and fill all fields on a job entry.

    Called on every job during CI merge to ensure consistency. Migrates
    old status values ("Should Apply", "New") to empty string and adds
    missing fields like search_term. Then re-scores to apply any filter
    rule changes.
    """
    # Migrate old statuses
    if job.get("status") in ("Should Apply", "New"):
        job["status"] = ""

    if "search_term" not in job:
        job["search_term"] = ""

    # Source inference for pre-existing rows: use URL as a tiebreaker.
    # linkedin.com URLs → 'linkedin', others → 'github' (covers GitHub + old ATS scans).
    if not job.get("source"):
        url = job.get("url") or ""
        job["source"] = "linkedin" if "linkedin.com" in url else "github"

    if job.get("status") in KEEP_STATUSES:
        job["expires_at"] = None
    elif "expires_at" not in job:
        last_seen = _parse_iso(job.get("last_seen", ""))
        job["expires_at"] = _expires_at_for_status("", base=last_seen)

    # Always re-score to ensure consistency
    return _rescore_entry(job)
