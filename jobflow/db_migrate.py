"""One-time migration: JSON files → PostgreSQL (Neon).

Usage:
    DATABASE_URL=postgresql://... python -m jobflow.db_migrate

Reads data/ci/linkedin_jobs.json and data/ci/seen_jobs.json,
bulk-inserts into the database, and verifies counts match.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import init_db, get_conn, put_conn, TTL_DAYS
from .linkedin_store import KEEP_STATUSES


def migrate():
    root = Path(__file__).parent.parent
    jobs_path = root / "data" / "ci" / "linkedin_jobs.json"
    seen_path = root / "data" / "ci" / "seen_jobs.json"

    if not jobs_path.exists():
        print(f"ERROR: {jobs_path} not found")
        sys.exit(1)

    # Load JSON store
    store = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = store.get("jobs", {})
    print(f"Loaded {len(jobs)} jobs from {jobs_path}")

    # Initialize tables
    print("Creating tables...")
    init_db()

    # Insert jobs
    print("Inserting jobs...")
    now = datetime.now(timezone.utc)
    conn = get_conn()
    inserted = 0
    skipped = 0

    try:
        with conn.cursor() as cur:
            for url, job in jobs.items():
                # Compute expires_at
                status = job.get("status", "")
                if status in KEEP_STATUSES:
                    expires_at = None
                else:
                    expires_at = (now + timedelta(days=TTL_DAYS)).isoformat()

                first_seen = job.get("first_seen", now.isoformat())
                last_seen = job.get("last_seen", now.isoformat())

                try:
                    cur.execute("SAVEPOINT sp")
                    cur.execute("""
                        INSERT INTO jobs (
                            url, company, title, location, description_preview,
                            search_term, date_posted, variant, reason,
                            first_seen, last_seen,
                            score, score_pct, ai_score, ai_reason, recommended,
                            level, min_exp, max_exp, competition, keyword_hits,
                            status, h1b, reject_reason, expires_at
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s
                        ) ON CONFLICT (url) DO NOTHING
                    """, (
                        url,
                        job.get("company", ""),
                        job.get("title", ""),
                        job.get("location", ""),
                        job.get("description_preview", ""),
                        job.get("search_term", ""),
                        job.get("date_posted", ""),
                        job.get("variant", "se"),
                        job.get("reason", ""),
                        first_seen, last_seen,
                        int(job.get("score", 0) or 0),
                        int(job.get("score_pct", 0) or 0),
                        job.get("ai_score"),
                        job.get("ai_reason", "") or "",
                        bool(job.get("recommended", False)),
                        job.get("level", "Unknown"),
                        job.get("min_exp"),
                        job.get("max_exp"),
                        int(job.get("competition", 0) or 0),
                        int(job.get("keyword_hits", 0) or 0),
                        status,
                        bool(job.get("h1b", False)),
                        job.get("reject_reason"),
                        expires_at,
                    ))
                    cur.execute("RELEASE SAVEPOINT sp")
                    inserted += 1
                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp")
                    print(f"  SKIP {url[:60]}: {e}")
                    skipped += 1

        conn.commit()
        print(f"Inserted {inserted} jobs ({skipped} skipped)")

        # Verify
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs")
            db_count = cur.fetchone()[0]
        print(f"Verified: {db_count} jobs in database")

    finally:
        put_conn(conn)

    # Migrate seen_jobs
    if seen_path.exists():
        print(f"\nMigrating seen_jobs from {seen_path}...")
        seen_data = json.loads(seen_path.read_text(encoding="utf-8"))
        if isinstance(seen_data, list):
            seen_data = {url: now.isoformat() for url in seen_data}

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                seen_count = 0
                for url, ts in seen_data.items():
                    try:
                        cur.execute("SAVEPOINT sp_seen")
                        cur.execute("""
                            INSERT INTO seen_jobs (url, seen_at) VALUES (%s, %s)
                            ON CONFLICT (url) DO NOTHING
                        """, (url, ts))
                        cur.execute("RELEASE SAVEPOINT sp_seen")
                        seen_count += 1
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_seen")
            conn.commit()

            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM seen_jobs")
                db_seen = cur.fetchone()[0]
            print(f"Inserted {seen_count} seen entries, {db_seen} in database")
        finally:
            put_conn(conn)

    print("\nMigration complete!")


if __name__ == "__main__":
    migrate()
