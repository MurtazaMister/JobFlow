"""Local AI scoring using the signed-in Claude or Codex CLI with batching.

Usage:
    python scripts/ai_score_local.py                       # defaults to --engine claude
    python scripts/ai_score_local.py --engine codex
    python scripts/ai_score_local.py --limit 50
    python scripts/ai_score_local.py --hours 12
    python scripts/ai_score_local.py --hours 12 --limit 100
    python scripts/ai_score_local.py --backend json --hours 24
    python scripts/ai_score_local.py --backend json --hours 24 --rescore
    python scripts/ai_score_local.py --recommended-only   # re-audit recommendations

Reads DATABASE_URL from .env when available, fetches unscored jobs, scores
them in batches of 15 with the local Claude or Codex CLI session, and updates
Postgres or data/ci/linkedin_jobs.json. This uses the signed-in Claude/Codex
app/CLI account instead of an API key.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

from jobflow.db import get_conn, put_conn, init_db, DEFAULT_USER_ID
from jobflow.linkedin_store import save_store
from jobflow.filter import STAFFING_BLOCK_REASON
# Shared scoring core (single source of truth across all scorers). Re-exported
# so existing imports of these names from this module keep working.
from jobflow.ai_prompt import (
    BATCH_SIZE, BATCH_PROMPT, build_jobs_block, normalize_row_score,
    eligible_ai_score, is_blocked_staffing_source, parse_scores,
)
from jobflow.ai_local import (
    score_batch_with_claude, score_batch_with_codex, score_batch,
)

PROFILE_PATH = ROOT / "config" / "profile.txt"
PROFILE_EXAMPLE_PATH = ROOT / "config" / "profile.example.txt"
JSON_STORE_PATH = ROOT / "data" / "ci" / "linkedin_jobs.json"

ENGINES = ("claude", "codex")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Score recent/unscored JobFlow jobs with the signed-in Claude or Codex CLI.",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINES,
        default="claude",
        help="Which signed-in CLI to score with. Default: claude.",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=0,
        help="Score only the latest N eligible jobs by first_seen. Default: no limit.",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=0,
        help="Score only eligible jobs first seen in the past H hours. Example: --hours 12.",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "db", "json"),
        default="auto",
        help="Storage backend to score. Default: auto tries DB, then JSON.",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Include jobs that already have any AI score. Default only scores unscored rows.",
    )
    parser.add_argument(
        "--recommended-only",
        action="store_true",
        help="Rescore only currently-recommended jobs (implies --rescore). "
             "Use after tightening the prompt/profile to re-audit past recommendations.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=DEFAULT_USER_ID,
        help="Which user's per-job state to score (DB backend). Default: operator.",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be 0 or greater")
    if args.hours < 0:
        parser.error("--hours must be 0 or greater")
    if args.recommended_only:
        args.rescore = True
    return args


def parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp into an aware datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_profile() -> str:
    """Load the local candidate profile used for AI scoring."""
    if PROFILE_PATH.exists():
        return PROFILE_PATH.read_text(encoding="utf-8").strip()
    print(f"ERROR: {PROFILE_PATH} not found")
    print(f"Create it with: cp {PROFILE_EXAMPLE_PATH} {PROFILE_PATH}")
    print("Then edit it with your real skills, target roles, visa needs, and preferences.")
    sys.exit(1)


def connect_db():
    """Initialize and return True when Postgres is reachable."""
    if not os.environ.get("DATABASE_URL"):
        return False
    try:
        init_db()
        return True
    except Exception as e:
        print(f"DB unavailable, using JSON fallback: {e}")
        return False


def fetch_db_rows(args):
    """Fetch eligible rows for one user from Postgres (per-user state + posting)."""
    conditions = ["s.user_id = %s"]
    params = [args.user_id]
    if getattr(args, "recommended_only", False):
        conditions.append("s.recommended = true")
    elif not args.rescore:
        conditions.append("s.ai_score IS NULL")
    if args.hours:
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        conditions.append("s.first_seen >= %s")
        params.append(since)

    where_clause = f"WHERE {' AND '.join(conditions)}"
    sql = f"""
        SELECT j.url, j.company, j.title, j.location, j.description_preview, s.ai_model
        FROM user_job_state s
        JOIN jobs j ON j.url = s.url
        {where_clause}
        ORDER BY s.first_seen DESC
    """
    if args.limit:
        sql += " LIMIT %s"
        params.append(args.limit)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        put_conn(conn)


def load_json_store() -> dict:
    """Load the JSON fallback store."""
    if not JSON_STORE_PATH.exists():
        print(f"ERROR: {JSON_STORE_PATH} not found")
        sys.exit(1)
    try:
        return json.loads(JSON_STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse {JSON_STORE_PATH}: {e}")
        sys.exit(1)


def fetch_json_rows(store: dict, args):
    """Fetch eligible rows from data/ci/linkedin_jobs.json."""
    rows = []
    cutoff = None
    if args.hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    for key, job in store.get("jobs", {}).items():
        ai_score = job.get("ai_score")
        ai_model = job.get("ai_model")
        if not eligible_ai_score(ai_score, args.rescore):
            continue
        if getattr(args, "recommended_only", False) and not job.get("recommended"):
            continue
        if cutoff:
            first_seen = parse_iso(job.get("first_seen", ""))
            if not first_seen or first_seen < cutoff:
                continue
        url = job.get("url") or key
        rows.append((
            url,
            job.get("company", ""),
            job.get("title", ""),
            job.get("location", ""),
            job.get("description_preview", ""),
            ai_model,
            key,
        ))

    rows.sort(
        key=lambda row: parse_iso(store.get("jobs", {}).get(row[6], {}).get("first_seen", ""))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if args.limit:
        rows = rows[:args.limit]
    return rows


def update_db_blocked(blocked_rows, engine, user_id):
    """Persist blocked-source scores to one user's per-job state."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for url, *_ in blocked_rows:
                cur.execute("""
                    UPDATE user_job_state
                    SET ai_score = 0, ai_reason = %s,
                        score_pct = 0, recommended = false,
                        ai_model = %s
                    WHERE user_id = %s AND url = %s
                """, (STAFFING_BLOCK_REASON, engine, user_id, url))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def update_db_scores(scored_batch, scores, engine, user_id):
    """Persist engine scores to one user's per-job state."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for (url, *_), score_data in zip(scored_batch, scores):
                ai_score, ai_reason, score_pct, recommended = normalize_row_score(score_data)
                cur.execute("""
                    UPDATE user_job_state
                    SET ai_score = %s, ai_reason = %s,
                        score_pct = %s, recommended = %s,
                        ai_model = %s
                    WHERE user_id = %s AND url = %s
                """, (ai_score, ai_reason, score_pct, recommended, engine, user_id, url))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def update_json_blocked(store: dict, blocked_rows, engine):
    """Persist blocked-source scores to the JSON store in memory."""
    jobs = store.get("jobs", {})
    for url, *rest in blocked_rows:
        key = rest[5] if len(rest) > 5 else url
        job = jobs.get(key)
        if not job:
            continue
        job["ai_score"] = 0
        job["ai_reason"] = STAFFING_BLOCK_REASON
        job["score_pct"] = 0
        job["recommended"] = False
        job["ai_model"] = engine


def update_json_scores(store: dict, scored_batch, scores, engine):
    """Persist engine scores to the JSON store in memory."""
    jobs = store.get("jobs", {})
    for (url, *rest), score_data in zip(scored_batch, scores):
        key = rest[5] if len(rest) > 5 else url
        job = jobs.get(key)
        if not job:
            continue
        ai_score, ai_reason, score_pct, recommended = normalize_row_score(score_data)
        job["ai_score"] = ai_score
        job["ai_reason"] = ai_reason
        job["score_pct"] = score_pct
        job["recommended"] = recommended
        job["ai_model"] = engine


def print_summary(rows, args, backend: str):
    """Print scoring summary for either backend."""
    total = len(rows)
    unscored = sum(1 for r in rows if r[5] is None)
    rescored = total - unscored
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Engine: {args.engine}")
    print(f"Backend: {backend}")
    print(f"Found {total} jobs to score ({batches} batches of {BATCH_SIZE})")
    if args.hours:
        print(f"  Window: first_seen in past {args.hours:g} hours")
    if args.limit:
        print(f"  Limit: latest {args.limit} eligible jobs")
    if args.rescore:
        print(f"  {unscored} unscored, {rescored} already scored")
    else:
        print(f"  {unscored} unscored")


def save_json_store(store: dict):
    """Write the JSON store using the same atomic helper as the dashboard."""
    save_store(JSON_STORE_PATH, store)


def main():
    args = parse_args()
    profile = load_profile()

    store = None
    if args.backend == "json":
        backend = "json"
        store = load_json_store()
        rows = fetch_json_rows(store, args)
    elif args.backend == "db":
        if not connect_db():
            print("ERROR: DB backend requested, but DATABASE_URL is missing or unavailable.")
            sys.exit(1)
        backend = "db"
        rows = fetch_db_rows(args)
    else:
        if connect_db():
            backend = "db"
            rows = fetch_db_rows(args)
        else:
            backend = "json"
            store = load_json_store()
            rows = fetch_json_rows(store, args)

    total = len(rows)
    print_summary(rows, args, backend)
    if not rows:
        print("Nothing to score!")
        return

    scored = 0
    failed = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch = rows[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n--- Batch {batch_num}/{total_batches} ({len(batch)} jobs) ---")

        blocked_rows = []
        scorable_batch = []
        for row in batch:
            if is_blocked_staffing_source(row):
                blocked_rows.append(row)
            else:
                scorable_batch.append(row)

        if blocked_rows:
            try:
                if backend == "db":
                    update_db_blocked(blocked_rows, args.engine, args.user_id)
                else:
                    update_json_blocked(store, blocked_rows, args.engine)
                for _, company, title, *_ in blocked_rows:
                    scored += 1
                    print(f"    0 | {company} — {title} ({STAFFING_BLOCK_REASON})")
            except Exception as e:
                print(f"  {backend.upper()} error: {e}")
                failed += len(blocked_rows)

        if not scorable_batch:
            continue

        scores = score_batch(scorable_batch, profile, args.engine)
        if not scores or len(scores) != len(scorable_batch):
            # Fallback: if batch fails or count mismatch, mark as failed
            for url, company, title, *_ in scorable_batch:
                print(f"  SKIP | {company} — {title}")
                failed += 1
            continue

        try:
            if backend == "db":
                update_db_scores(scorable_batch, scores, args.engine, args.user_id)
            else:
                update_json_scores(store, scorable_batch, scores, args.engine)
            for (_, company, title, *_), score_data in zip(scorable_batch, scores):
                ai_score, _, _, recommended = normalize_row_score(score_data)
                scored += 1
                tag = "REC" if recommended else f"  {ai_score}"
                print(f"  {tag} | {company} — {title}")
        except Exception as e:
            print(f"  {backend.upper()} error: {e}")
            failed += len(scorable_batch)

    if backend == "json":
        save_json_store(store)
        print(f"Saved JSON scores to {JSON_STORE_PATH}")

    print(f"\nDone: {scored} scored, {failed} failed out of {total} total")


if __name__ == "__main__":
    main()
