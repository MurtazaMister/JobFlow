"""PostgreSQL database backend for JobFlow (Neon serverless).

Replaces JSON file storage with persistent PostgreSQL when DATABASE_URL is set.
All functions mirror the linkedin_store.py API so the rest of the codebase
can switch backends transparently.

Connection: uses psycopg2 with a threaded connection pool (1-3 connections),
TCP keepalive to survive Neon's idle timeout, and connection validation.
TTL: jobs auto-expire after 3 days unless user sets status to Tracking/Applied.
"""

import os
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.pool
import psycopg2.extras

from .linkedin_store import (
    LINKEDIN_STATUSES, KEEP_STATUSES,
    _rescore_entry, format_recency,
    _bucket_minutes, _bucket_start, _bucket_label, _bucket_key,
    normalize_url,
)
from .sponsors import SPONSOR_STATUS, sponsor_companies

TTL_DAYS = 3
_pool = None


def _sanitize_dsn(dsn: str) -> str:
    """Clean a Postgres DSN before connecting.

    - Strips surrounding whitespace/newlines (a stray newline in a pasted
      env var otherwise ends up inside a connection option value).
    - Drops ``channel_binding`` — some bundled libpq builds (e.g. Render's
      image) reject ``channel_binding=require`` with "invalid channel_binding
      value", and it's optional for Neon since ``sslmode=require`` already
      secures the connection.
    """
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    dsn = (dsn or "").strip()
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgres", "postgresql") or not parts.query:
        return dsn
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k != "channel_binding"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _get_pool():
    """Get or create the connection pool (thread-safe)."""
    global _pool
    if _pool is None or _pool.closed:
        dsn = _sanitize_dsn(os.environ["DATABASE_URL"])
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 5,
            dsn=dsn,
            # Neon needs TLS; an explicit sslmode in the DSN (e.g. a local
            # Postgres with ?sslmode=disable) wins over this default.
            sslmode="require" if "sslmode=" not in dsn else None,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
    return _pool


def get_conn():
    """Get a validated connection from the pool.

    Checks that the connection is alive before returning it. If the
    connection was killed by Neon's idle timeout, it's discarded and
    a fresh one is returned.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        # Quick validation — detects dead connections from Neon idle timeout
        conn.cursor().execute("SELECT 1")
    except Exception:
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = pool.getconn()
    return conn


def put_conn(conn):
    """Return a connection to the pool."""
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


def init_db():
    """Create tables if they don't exist. Idempotent."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    url                 TEXT PRIMARY KEY,
                    company             TEXT NOT NULL DEFAULT '',
                    title               TEXT NOT NULL DEFAULT '',
                    location            TEXT NOT NULL DEFAULT '',
                    description_preview TEXT NOT NULL DEFAULT '',
                    search_term         TEXT NOT NULL DEFAULT '',
                    date_posted         TEXT NOT NULL DEFAULT '',
                    variant             TEXT NOT NULL DEFAULT 'se',
                    reason              TEXT NOT NULL DEFAULT '',
                    first_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    score               INTEGER NOT NULL DEFAULT 0,
                    score_pct           INTEGER NOT NULL DEFAULT 0,
                    ai_score            INTEGER,
                    ai_reason           TEXT NOT NULL DEFAULT '',
                    ai_model            TEXT,
                    recommended         BOOLEAN NOT NULL DEFAULT FALSE,
                    level               TEXT NOT NULL DEFAULT 'Unknown',
                    min_exp             INTEGER,
                    max_exp             INTEGER,
                    competition         INTEGER NOT NULL DEFAULT 0,
                    keyword_hits        INTEGER NOT NULL DEFAULT 0,
                    status              TEXT NOT NULL DEFAULT '',
                    h1b                 BOOLEAN NOT NULL DEFAULT FALSE,
                    reject_reason       TEXT,
                    expires_at          TIMESTAMPTZ,
                    source              TEXT NOT NULL DEFAULT 'linkedin'
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
                CREATE INDEX IF NOT EXISTS idx_jobs_level ON jobs (level);
                CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs (first_seen);
                CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs (last_seen);
                CREATE INDEX IF NOT EXISTS idx_jobs_expires_at ON jobs (expires_at)
                    WHERE expires_at IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_jobs_company_title
                    ON jobs (LOWER(company), LOWER(title));
                CREATE INDEX IF NOT EXISTS idx_jobs_source_first_seen
                    ON jobs (source, first_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_source_status
                    ON jobs (source, status);
                CREATE INDEX IF NOT EXISTS idx_jobs_source_level
                    ON jobs (source, level);

                CREATE TABLE IF NOT EXISTS seen_jobs (
                    url     TEXT PRIMARY KEY,
                    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_seen_jobs_seen_at ON seen_jobs (seen_at);

                CREATE TABLE IF NOT EXISTS dismissed_jobs (
                    url          TEXT PRIMARY KEY,
                    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                -- ── Multi-user tables ──────────────────────────────────────
                -- The shared `jobs` table above holds the raw posting; per-user
                -- scoring and state live in user_job_state so the same pool can
                -- be ranked differently for each signed-in user.
                CREATE TABLE IF NOT EXISTS users (
                    id            BIGSERIAL PRIMARY KEY,
                    google_sub    TEXT UNIQUE NOT NULL,
                    email         TEXT UNIQUE NOT NULL,
                    name          TEXT NOT NULL DEFAULT '',
                    picture       TEXT NOT NULL DEFAULT '',
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_login_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    is_active     BOOLEAN NOT NULL DEFAULT TRUE
                );

                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id         BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    filter_config   JSONB NOT NULL DEFAULT '{}'::jsonb,
                    ai_profile_text TEXT  NOT NULL DEFAULT '',
                    search_terms    JSONB NOT NULL DEFAULT '[]'::jsonb,
                    ai_provider     TEXT  NOT NULL DEFAULT 'none',
                    ai_api_key_enc  BYTEA,
                    ai_model        TEXT  NOT NULL DEFAULT '',
                    pairing_token   TEXT UNIQUE,
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS user_job_state (
                    user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    url            TEXT   NOT NULL REFERENCES jobs(url)  ON DELETE CASCADE,
                    variant        TEXT   NOT NULL DEFAULT 'se',
                    reason         TEXT   NOT NULL DEFAULT '',
                    score          INTEGER NOT NULL DEFAULT 0,
                    score_pct      INTEGER NOT NULL DEFAULT 0,
                    ai_score       INTEGER,
                    ai_reason      TEXT   NOT NULL DEFAULT '',
                    ai_model       TEXT,
                    recommended    BOOLEAN NOT NULL DEFAULT FALSE,
                    level          TEXT   NOT NULL DEFAULT 'Unknown',
                    min_exp        INTEGER,
                    max_exp        INTEGER,
                    competition    INTEGER NOT NULL DEFAULT 0,
                    keyword_hits   INTEGER NOT NULL DEFAULT 0,
                    status         TEXT   NOT NULL DEFAULT '',
                    h1b            BOOLEAN NOT NULL DEFAULT FALSE,
                    reject_reason  TEXT,
                    first_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at     TIMESTAMPTZ,
                    PRIMARY KEY (user_id, url)
                );
                CREATE INDEX IF NOT EXISTS idx_ujs_user_status
                    ON user_job_state (user_id, status);
                CREATE INDEX IF NOT EXISTS idx_ujs_user_level
                    ON user_job_state (user_id, level);
                CREATE INDEX IF NOT EXISTS idx_ujs_user_first_seen
                    ON user_job_state (user_id, first_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_ujs_user_recommended
                    ON user_job_state (user_id, recommended) WHERE recommended;
                CREATE INDEX IF NOT EXISTS idx_ujs_user_aiscore_null
                    ON user_job_state (user_id) WHERE ai_score IS NULL;
                CREATE INDEX IF NOT EXISTS idx_ujs_expires
                    ON user_job_state (expires_at) WHERE expires_at IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_ujs_url ON user_job_state (url);

                CREATE TABLE IF NOT EXISTS user_dismissed_jobs (
                    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    url          TEXT   NOT NULL,
                    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, url)
                );
            """)
            # Migration for existing databases
            cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_model TEXT")
            cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'linkedin'")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs (source)")
            # When the user last opened the posting from the feed ("Viewed" tag).
            cur.execute("ALTER TABLE user_job_state ADD COLUMN IF NOT EXISTS viewed_at TIMESTAMPTZ")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_search_trgm
                    ON jobs USING GIN (
                        (LOWER(company || ' ' || title || ' ' || location)) gin_trgm_ops
                    )
            """)
            # Retag jobs whose URL clearly isn't LinkedIn — handles pre-existing
            # rows that got the 'linkedin' default but came from GitHub / ATS scans.
            cur.execute(
                "UPDATE jobs SET source = 'github' "
                "WHERE source = 'linkedin' AND url NOT LIKE '%%linkedin.com%%'"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    # Seed the operator user and, on first run for an existing single-user DB,
    # move its per-job state into the multi-tenant tables. Idempotent + guarded
    # so the migration scan runs at most once.
    ensure_default_user()
    _maybe_migrate_multiuser()


def _maybe_migrate_multiuser() -> None:
    """Run migrate_to_multiuser() once, when the operator has no state yet but
    legacy rows exist on the shared `jobs` table to import.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM user_job_state WHERE user_id = %s)",
                (DEFAULT_USER_ID,),
            )
            has_state = cur.fetchone()[0]
            cur.execute("SELECT EXISTS(SELECT 1 FROM jobs)")
            has_jobs = cur.fetchone()[0]
    finally:
        put_conn(conn)
    if has_jobs and not has_state:
        migrate_to_multiuser()


# ---------------------------------------------------------------------------
# Users & profiles (multi-tenant)
# ---------------------------------------------------------------------------

# The operator / single-user fallback. Migrating an existing single-user DB
# seeds this row and moves the per-job state onto it, so the app keeps working
# exactly as before until real Google sign-ins create additional users.
DEFAULT_USER_ID = 1
OPERATOR_SUB = "operator"


def ensure_default_user(email: str = "") -> int:
    """Create the operator user (id=DEFAULT_USER_ID) if absent. Idempotent."""
    email = email or os.environ.get("JOBFLOW_OPERATOR_EMAIL", "operator@localhost")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, google_sub, email, name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (DEFAULT_USER_ID, OPERATOR_SUB, email, "Operator"),
            )
            cur.execute(
                """
                INSERT INTO user_profiles (user_id) VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (DEFAULT_USER_ID,),
            )
            # Keep the BIGSERIAL ahead of any explicit id we inserted.
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('users', 'id'), "
                "GREATEST((SELECT MAX(id) FROM users), 1))"
            )
        conn.commit()
        return DEFAULT_USER_ID
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_or_create_user(google_sub: str, email: str, name: str = "", picture: str = "") -> dict:
    """Resolve a signing-in user, creating them (and a blank profile) on first
    sign-in. Lookup-first by google_sub OR email so it never trips the UNIQUE
    constraints (e.g. Google sometimes returns a new ``sub`` for the same email,
    or the email already belongs to the seeded operator row). Refreshes
    last_login_at. Returns the user dict with a ``created`` flag.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Prefer a google_sub match; fall back to an existing email.
            cur.execute(
                "SELECT id FROM users WHERE google_sub = %s OR email = %s "
                "ORDER BY (google_sub = %s) DESC, id ASC LIMIT 1",
                (google_sub, email, google_sub),
            )
            found = cur.fetchone()
            if found:
                cur.execute(
                    "UPDATE users SET google_sub = %s, email = %s, name = %s, "
                    "picture = %s, last_login_at = NOW() WHERE id = %s "
                    "RETURNING id, google_sub, email, name, picture, is_active",
                    (google_sub, email, name, picture, found[0]),
                )
                created = False
            else:
                cur.execute(
                    "INSERT INTO users (google_sub, email, name, picture, last_login_at) "
                    "VALUES (%s, %s, %s, %s, NOW()) "
                    "RETURNING id, google_sub, email, name, picture, is_active",
                    (google_sub, email, name, picture),
                )
                created = True
            row = cur.fetchone()
            user = {
                "id": row[0], "google_sub": row[1], "email": row[2],
                "name": row[3], "picture": row[4], "is_active": row[5],
                "created": created,
            }
            cur.execute(
                "INSERT INTO user_profiles (user_id) VALUES (%s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user["id"],),
            )
        conn.commit()
        return user
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def bind_operator(google_sub: str, email: str, name: str = "", picture: str = "") -> dict:
    """Attach a Google identity to the operator row (DEFAULT_USER_ID) so the
    person whose email == JOBFLOW_OPERATOR_EMAIL keeps the migrated single-user
    data instead of getting a fresh empty account. Returns the operator user dict.
    """
    ensure_default_user(email)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Remove any stray non-operator row already holding this email or
            # google_sub so the UPDATE below can't hit a UNIQUE conflict. (The
            # operator's email/identity shouldn't legitimately belong to anyone
            # else.) ON DELETE CASCADE cleans up its profile/state.
            cur.execute(
                "DELETE FROM users WHERE id <> %s AND (email = %s OR google_sub = %s)",
                (DEFAULT_USER_ID, email, google_sub),
            )
            cur.execute(
                "UPDATE users SET google_sub = %s, email = %s, name = %s, "
                "picture = %s, last_login_at = NOW() WHERE id = %s "
                "RETURNING id, google_sub, email, name, picture, is_active",
                (google_sub, email, name, picture, DEFAULT_USER_ID),
            )
            row = cur.fetchone()
        conn.commit()
        return {
            "id": row[0], "google_sub": row[1], "email": row[2],
            "name": row[3], "picture": row[4], "is_active": row[5],
            "created": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_user(user_id: int) -> dict | None:
    """Fetch a user row by id, or None."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, google_sub, email, name, picture, is_active "
                "FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0], "google_sub": row[1], "email": row[2],
                "name": row[3], "picture": row[4], "is_active": row[5],
            }
    finally:
        put_conn(conn)


def list_active_user_ids() -> list[int]:
    """Return ids of active users — the set scan fan-out scores against."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE is_active ORDER BY id")
            return [r[0] for r in cur.fetchall()]
    finally:
        put_conn(conn)


def get_user_profile(user_id: int) -> dict | None:
    """Return a user's profile. Never returns the raw API key — only whether one
    is set (``has_api_key``). Use get_user_api_key_enc() for the encrypted bytes.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT filter_config, ai_profile_text, search_terms, ai_provider, "
                "       ai_model, (ai_api_key_enc IS NOT NULL) AS has_key, pairing_token "
                "FROM user_profiles WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "user_id": user_id,
                "filter_config": row[0] or {},
                "ai_profile_text": row[1] or "",
                "search_terms": row[2] or [],
                "ai_provider": row[3] or "none",
                "ai_model": row[4] or "",
                "has_api_key": bool(row[5]),
                "pairing_token": row[6],
            }
    finally:
        put_conn(conn)


# Whitelist of plain (non-secret) profile columns the app may update directly.
_PROFILE_UPDATABLE = {
    "filter_config": psycopg2.extras.Json,
    "ai_profile_text": str,
    "search_terms": psycopg2.extras.Json,
    "ai_provider": str,
    "ai_model": str,
    "pairing_token": str,
}


def upsert_user_profile(user_id: int, **fields) -> None:
    """Update whitelisted profile columns for a user. Unknown keys are ignored.

    The encrypted API key is handled separately by set_user_api_key_enc() so it
    never flows through generic update paths or logs.
    """
    sets, params = [], []
    for key, adapter in _PROFILE_UPDATABLE.items():
        if key in fields and fields[key] is not None:
            value = fields[key]
            sets.append(f"{key} = %s")
            params.append(adapter(value) if adapter in (psycopg2.extras.Json,) else value)
    if not sets:
        return
    sets.append("updated_at = NOW()")
    params.append(user_id)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_profiles (user_id) VALUES (%s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            cur.execute(
                f"UPDATE user_profiles SET {', '.join(sets)} WHERE user_id = %s",
                params,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def set_user_api_key_enc(user_id: int, key_enc: bytes | None) -> None:
    """Store (or clear, with None) the encrypted Anthropic API key for a user."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_profiles (user_id) VALUES (%s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            cur.execute(
                "UPDATE user_profiles SET ai_api_key_enc = %s, updated_at = NOW() "
                "WHERE user_id = %s",
                (psycopg2.Binary(key_enc) if key_enc is not None else None, user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_user_api_key_enc(user_id: int) -> bytes | None:
    """Return the encrypted API key bytes for a user, or None."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ai_api_key_enc FROM user_profiles WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            return bytes(row[0])
    finally:
        put_conn(conn)


def set_pairing_token(user_id: int, token: str) -> None:
    """Store a user's CLI pairing token (regenerating revokes the old one)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_profiles (user_id) VALUES (%s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            cur.execute(
                "UPDATE user_profiles SET pairing_token = %s, updated_at = NOW() "
                "WHERE user_id = %s",
                (token, user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_user_by_pairing_token(token: str) -> dict | None:
    """Resolve an active user from a CLI pairing token, or None."""
    if not token:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.id, u.email, u.is_active FROM user_profiles p "
                "JOIN users u ON u.id = p.user_id WHERE p.pairing_token = %s",
                (token,),
            )
            row = cur.fetchone()
            if not row or not row[2]:
                return None
            return {"id": row[0], "email": row[1], "is_active": row[2]}
    finally:
        put_conn(conn)


def get_unscored_user_jobs(user_id: int, hours: float = 0, limit: int = 0,
                           rescore: bool = False) -> list[tuple]:
    """Return a user's jobs eligible for AI scoring as
    (url, company, title, location, description_preview) tuples — the row shape
    the shared scorer (build_jobs_block / is_blocked_staffing_source) expects.
    Newest first; capped by ``limit`` when > 0.
    """
    conditions = ["s.user_id = %s"]
    params: list = [user_id]
    if not rescore:
        conditions.append("s.ai_score IS NULL")
    if hours:
        conditions.append("s.first_seen >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(hours=hours))
    where = " AND ".join(conditions)
    sql = (
        f"SELECT j.url, j.company, j.title, j.location, j.description_preview "
        f"FROM {_USER_JOBS_FROM} WHERE {where} ORDER BY s.first_seen DESC"
    )
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        put_conn(conn)


def apply_user_ai_scores(user_id: int, scored_pairs, model: str) -> int:
    """Write AI scores to one user's per-job state.

    ``scored_pairs`` is an iterable of (url, score_data) where score_data is a
    dict like {"score": 0-10, "reason": str} (also used for blocked-source 0s).
    Reuses normalize_row_score so the ai_score->score_pct/recommended mapping is
    identical across every scorer. Returns the number of rows updated.
    """
    from .ai_prompt import normalize_row_score

    conn = get_conn()
    updated = 0
    try:
        with conn.cursor() as cur:
            for url, data in scored_pairs:
                ai_score, ai_reason, score_pct, recommended = normalize_row_score(data)
                cur.execute(
                    "UPDATE user_job_state SET ai_score = %s, ai_reason = %s, "
                    "score_pct = %s, recommended = %s, ai_model = %s "
                    "WHERE user_id = %s AND url = %s",
                    (ai_score, ai_reason, score_pct, recommended, model, user_id, url),
                )
                updated += cur.rowcount
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def migrate_to_multiuser(operator_email: str = "") -> dict:
    """One-shot: seed the operator user and move existing single-user per-job
    state onto it. Idempotent — rows already present are left untouched.

    Copies jobs.{status, scores, ai_*, level, ...} -> user_job_state(operator)
    and dismissed_jobs -> user_dismissed_jobs(operator). The shared `jobs` table
    keeps the raw posting; its legacy per-user columns are simply left unread.
    """
    ensure_default_user(operator_email)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_job_state (
                    user_id, url, variant, reason, score, score_pct,
                    ai_score, ai_reason, ai_model, recommended, level,
                    min_exp, max_exp, competition, keyword_hits, status,
                    h1b, reject_reason, first_seen, last_seen, expires_at
                )
                SELECT
                    %s, url, variant, reason, score, score_pct,
                    ai_score, COALESCE(ai_reason, ''), ai_model, recommended, level,
                    min_exp, max_exp, competition, keyword_hits, status,
                    h1b, reject_reason, first_seen, last_seen, expires_at
                FROM jobs
                ON CONFLICT (user_id, url) DO NOTHING
                """,
                (DEFAULT_USER_ID,),
            )
            jobs_copied = cur.rowcount
            cur.execute(
                """
                INSERT INTO user_dismissed_jobs (user_id, url, dismissed_at)
                SELECT %s, url, dismissed_at FROM dismissed_jobs
                ON CONFLICT (user_id, url) DO NOTHING
                """,
                (DEFAULT_USER_ID,),
            )
            dismissed_copied = cur.rowcount
        conn.commit()
        return {"jobs_copied": jobs_copied, "dismissed_copied": dismissed_copied}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row, columns) -> dict:
    """Convert a database row to a job dict matching the JSON store format."""
    d = dict(zip(columns, row))
    for ts_field in ("first_seen", "last_seen", "expires_at"):
        if d.get(ts_field) and isinstance(d[ts_field], datetime):
            d[ts_field] = d[ts_field].isoformat()
    if d.get("ai_score") is None:
        d.pop("ai_score", None)
    if d.get("ai_reason") == "":
        d["ai_reason"] = ""
    return d


JOB_COLUMNS = [
    "url", "company", "title", "location", "description_preview",
    "search_term", "date_posted", "variant", "reason",
    "first_seen", "last_seen",
    "score", "score_pct", "ai_score", "ai_reason", "ai_model", "recommended",
    "level", "min_exp", "max_exp", "competition", "keyword_hits",
    "status", "h1b", "reject_reason", "expires_at", "source",
]

# Columns that live on the shared posting (jobs j); everything else in
# JOB_COLUMNS lives on the per-user state row (user_job_state s).
_POSTING_COLS = {
    "url", "company", "title", "location", "description_preview",
    "search_term", "date_posted", "source",
}


def _col(name: str) -> str:
    """Qualify a JOB_COLUMNS name with its source table alias (j=posting, s=state)."""
    return f"j.{name}" if name in _POSTING_COLS else f"s.{name}"


def _qualified_select() -> str:
    """SELECT list pulling each JOB_COLUMNS field from its owning table."""
    return ", ".join(_col(c) for c in JOB_COLUMNS)


# The standard multi-tenant FROM: per-user state joined to the shared posting.
_USER_JOBS_FROM = "user_job_state s JOIN jobs j ON j.url = s.url"


def _sponsor_company_list(user_id: int) -> list[str]:
    """Raw company names in one user's pool that are on the top-sponsor list
    (the "FAANG" tab). Matched in Python, then passed to SQL as = ANY(%s)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT j.company FROM {_USER_JOBS_FROM} WHERE s.user_id = %s", (user_id,))
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
    return sponsor_companies(r[0] for r in rows)


# Count column for the "FAANG" tab; its %s takes _sponsor_company_list() and must
# come first in params since the SELECT list precedes the WHERE clause.
_SPONSOR_COUNT_SQL = (
    "COUNT(*) FILTER (WHERE j.company = ANY(%s) "
    "AND COALESCE(s.status, '') NOT IN ('Not Interested')) AS sponsor"
)


def _expires_at_for_status(status: str):
    """Return expires_at value based on status. None = never expires."""
    if status in KEEP_STATUSES:
        return None
    return datetime.now(timezone.utc) + timedelta(days=TTL_DAYS)


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------

def _user_profile_obj(user_id: int):
    """Load a user's FilterProfile (falls back to DEFAULT_PROFILE)."""
    from .filter_profile import profile_from_config
    prof = get_user_profile(user_id)
    return profile_from_config((prof or {}).get("filter_config") or {})


def _upsert_posting(cur, entry: dict, now_iso: str, posted: str) -> bool:
    """Upsert the shared posting (no per-user scoring). Returns True if inserted."""
    cur.execute("""
        INSERT INTO jobs (
            url, company, title, location, description_preview,
            search_term, date_posted, first_seen, last_seen, source
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (url) DO UPDATE SET
            last_seen = EXCLUDED.last_seen,
            description_preview = CASE
                WHEN LENGTH(EXCLUDED.description_preview) > LENGTH(jobs.description_preview)
                THEN EXCLUDED.description_preview ELSE jobs.description_preview END,
            search_term = EXCLUDED.search_term,
            source = EXCLUDED.source
        RETURNING (xmax = 0) AS inserted
    """, (
        entry["url"], entry.get("company", ""), entry.get("title", ""),
        entry.get("location", ""), entry.get("description_preview", ""),
        entry.get("search_term", ""), entry.get("date_posted", ""),
        posted, now_iso, entry.get("source", "linkedin"),
    ))
    row = cur.fetchone()
    return bool(row and row[0])


# Upsert for one user's per-job state. Preserves the user's status, AI score,
# and first_seen across re-scores (mirrors the original jobs upsert).
_USER_STATE_UPSERT_SQL = """
    INSERT INTO user_job_state (
        user_id, url, variant, reason, score, score_pct,
        ai_score, ai_reason, ai_model, recommended, level,
        min_exp, max_exp, competition, keyword_hits, status,
        h1b, reject_reason, first_seen, last_seen, expires_at
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (user_id, url) DO UPDATE SET
        last_seen = EXCLUDED.last_seen,
        score = EXCLUDED.score,
        score_pct = CASE
            WHEN user_job_state.ai_score IS NOT NULL THEN user_job_state.score_pct
            ELSE EXCLUDED.score_pct END,
        ai_score = COALESCE(user_job_state.ai_score, EXCLUDED.ai_score),
        ai_reason = CASE
            WHEN user_job_state.ai_score IS NOT NULL THEN user_job_state.ai_reason
            ELSE EXCLUDED.ai_reason END,
        ai_model = CASE
            WHEN user_job_state.ai_score IS NOT NULL THEN user_job_state.ai_model
            ELSE EXCLUDED.ai_model END,
        recommended = CASE
            WHEN user_job_state.ai_score IS NOT NULL THEN user_job_state.recommended
            ELSE EXCLUDED.recommended END,
        level = EXCLUDED.level, min_exp = EXCLUDED.min_exp,
        max_exp = EXCLUDED.max_exp, competition = EXCLUDED.competition,
        keyword_hits = EXCLUDED.keyword_hits, reason = EXCLUDED.reason,
        variant = EXCLUDED.variant, reject_reason = EXCLUDED.reject_reason,
        expires_at = CASE
            WHEN user_job_state.status IN ('Tracking', 'Applied') THEN NULL
            ELSE EXCLUDED.expires_at END
"""


def _user_state_params(user_id: int, scored: dict, now_iso: str, expires) -> tuple:
    """Build the parameter tuple for _USER_STATE_UPSERT_SQL from a scored entry."""
    return (
        user_id, scored["url"], scored.get("variant", "se"), scored.get("reason", ""),
        scored.get("score", 0), scored.get("score_pct", 0), scored.get("ai_score"),
        scored.get("ai_reason", ""), scored.get("ai_model"),
        scored.get("recommended", False), scored.get("level", "Unknown"),
        scored.get("min_exp"), scored.get("max_exp"), scored.get("competition", 0),
        scored.get("keyword_hits", 0), "", scored.get("h1b", False),
        scored.get("reject_reason"), now_iso, now_iso, expires,
    )


def _upsert_user_state(cur, user_id: int, scored: dict, now_iso: str, expires) -> None:
    cur.execute(_USER_STATE_UPSERT_SQL, _user_state_params(user_id, scored, now_iso, expires))


def merge_scan_results(scan_results: list[dict]) -> int:
    """Upsert scan results: postings into the shared `jobs` pool, then fan out
    per-user scoring into user_job_state for every active user.

    Returns the count of NEW postings inserted. Each active user's job state is
    scored with that user's FilterProfile and respects their per-user dismissals;
    status / AI score / first_seen are preserved across re-scores.
    """
    if not scan_results:
        return 0

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires = now + timedelta(days=TTL_DAYS)

    # Pre-dedup scan results by company+title (same as JSON store logic)
    seen_combos: dict[str, dict] = {}
    for entry in scan_results:
        co = entry.get("company", "").lower().strip()
        title = entry.get("title", "").lower().strip()
        combo = f"{co}|{title}"
        if combo in seen_combos:
            if not seen_combos[combo].get("url") and entry.get("url"):
                seen_combos[combo] = entry
        else:
            seen_combos[combo] = entry
    deduped = list(seen_combos.values())

    conn = get_conn()
    new_count = 0
    try:
        with conn.cursor() as cur:
            # Active users + their profiles and per-user dismissals.
            user_ids = [r[0] for r in
                        _fetch(cur, "SELECT id FROM users WHERE is_active ORDER BY id")]
            profiles = {uid: _user_profile_obj(uid) for uid in user_ids}
            dismissed = {}
            for uid in user_ids:
                cur.execute("SELECT url FROM user_dismissed_jobs WHERE user_id = %s", (uid,))
                dismissed[uid] = {normalize_url(r[0]) for r in cur.fetchall()}

            for entry in deduped:
                url = normalize_url(entry.get("url", ""))
                if not url:
                    continue
                entry["url"] = url
                posted = entry.get("date_posted", "") or now_iso

                # 1) Shared posting (no scoring).
                if _upsert_posting(cur, entry, now_iso, posted):
                    new_count += 1

                # 2) Fan out per-user scoring, skipping each user's dismissals.
                for uid in user_ids:
                    if url in dismissed[uid]:
                        continue
                    scored = _rescore_entry(dict(entry), profiles[uid])
                    scored["url"] = url
                    _upsert_user_state(cur, uid, scored, now_iso, expires)

            # Prune expired per-user rows, then postings no user references.
            cur.execute(
                "DELETE FROM user_job_state WHERE expires_at IS NOT NULL AND expires_at < NOW()"
            )
            cur.execute(
                "DELETE FROM jobs j WHERE NOT EXISTS "
                "(SELECT 1 FROM user_job_state s WHERE s.url = j.url) "
                "AND j.first_seen < NOW() - INTERVAL '7 days'"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    return new_count


def _fetch(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def backfill_user(user_id: int) -> int:
    """Score the entire existing posting pool for a (usually new) user.

    Run on signup so a new user immediately has a populated feed. Uses their
    FilterProfile, skips their dismissals, and only inserts state rows that don't
    already exist. Returns the number of rows written.
    """
    profile = _user_profile_obj(user_id)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires = now + timedelta(days=TTL_DAYS)
    conn = get_conn()
    written = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM user_dismissed_jobs WHERE user_id = %s", (user_id,))
            dismissed = {normalize_url(r[0]) for r in cur.fetchall()}
            cur.execute(
                "SELECT url, company, title, location, description_preview, "
                "search_term, date_posted, source, first_seen FROM jobs"
            )
            rows = cur.fetchall()
            batch = []
            for url, company, title, location, desc, search_term, date_posted, source, first_seen in rows:
                if url in dismissed:
                    continue
                entry = {
                    "url": url, "company": company, "title": title, "location": location,
                    "description_preview": desc, "search_term": search_term,
                    "date_posted": date_posted, "source": source,
                    "first_seen": first_seen.isoformat() if first_seen else now_iso,
                }
                scored = _rescore_entry(entry, profile)
                scored["url"] = url
                # Backfilled first_seen = signup time so time-buckets aren't
                # flooded with old postings (see plan).
                batch.append(_user_state_params(user_id, scored, now_iso, expires))
                written += 1
            # One round-trip per page instead of per row (4500+ postings).
            psycopg2.extras.execute_batch(cur, _USER_STATE_UPSERT_SQL, batch, page_size=500)
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def update_job_status(url: str, status: str, user_id: int = DEFAULT_USER_ID) -> bool:
    """Update a user's status on a job. Sets expires_at based on new status."""
    if status not in LINKEDIN_STATUSES and status != "":
        return False

    url = normalize_url(url)
    expires = _expires_at_for_status(status)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if status in KEEP_STATUSES:
                cur.execute(
                    "UPDATE user_job_state SET status = %s, expires_at = NULL "
                    "WHERE user_id = %s AND url = %s",
                    (status, user_id, url),
                )
            else:
                cur.execute(
                    "UPDATE user_job_state SET status = %s, expires_at = %s "
                    "WHERE user_id = %s AND url = %s",
                    (status, expires, user_id, url),
                )
            found = cur.rowcount > 0
        conn.commit()
        return found
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def mark_viewed(url: str, user_id: int = DEFAULT_USER_ID) -> bool:
    """Record that a user opened a job's posting from the feed."""
    url = normalize_url(url)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_job_state SET viewed_at = NOW() WHERE user_id = %s AND url = %s",
                (user_id, url),
            )
            found = cur.rowcount > 0
        conn.commit()
        return found
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def delete_job(url: str, user_id: int = DEFAULT_USER_ID) -> bool:
    """Dismiss a job for one user: remove their state row and remember the
    dismissal so it never re-fans-out to them. The shared posting stays for
    other users.
    """
    url = normalize_url(url)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_dismissed_jobs (user_id, url) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (user_id, url),
            )
            cur.execute(
                "DELETE FROM user_job_state WHERE user_id = %s AND url = %s",
                (user_id, url),
            )
            found = cur.rowcount > 0
        conn.commit()
        return found
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def prune_expired_jobs() -> int:
    """Delete expired per-user state rows, then postings no user references.
    Returns the count of per-user state rows deleted.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_job_state WHERE expires_at IS NOT NULL AND expires_at < NOW()"
            )
            count = cur.rowcount
            cur.execute(
                "DELETE FROM jobs j WHERE NOT EXISTS "
                "(SELECT 1 FROM user_job_state s WHERE s.url = j.url) "
                "AND j.first_seen < NOW() - INTERVAL '7 days'"
            )
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------

def get_filtered_jobs(
    user_id: int = DEFAULT_USER_ID,
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
    """Return one user's filtered, sorted job list. Mirrors the JSON store."""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    conditions = ["s.user_id = %s"]
    params = [user_id]

    if time_range == "hour":
        conditions.append("s.first_seen >= %s")
        params.append(now_utc - timedelta(hours=1))
    elif time_range == "today":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        conditions.append("s.first_seen >= %s")
        params.append(local_midnight.astimezone(timezone.utc))
    elif time_range == "yesterday":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        conditions.append("s.first_seen >= %s AND s.first_seen < %s")
        params.append((local_midnight - timedelta(days=1)).astimezone(timezone.utc))
        params.append(local_midnight.astimezone(timezone.utc))

    if bucket_filter:
        try:
            local_start = datetime.strptime(bucket_filter, "%Y-%m-%d_%H:%M").replace(tzinfo=user_tz)
            bm = _bucket_minutes(local_start)
            bucket_start = local_start.astimezone(timezone.utc)
            bucket_end = bucket_start + timedelta(minutes=bm)
            conditions.append("s.first_seen >= %s AND s.first_seen < %s")
            params.append(bucket_start)
            params.append(bucket_end)
        except (ValueError, TypeError):
            pass

    if status == "Recommended":
        conditions.append("s.recommended = TRUE")
        conditions.append("COALESCE(s.status, '') NOT IN ('Not Interested')")
    elif status == SPONSOR_STATUS:
        conditions.append("j.company = ANY(%s)")
        params.append(_sponsor_company_list(user_id))
        conditions.append("COALESCE(s.status, '') NOT IN ('Not Interested')")
    elif status:
        conditions.append("s.status = %s")
        params.append(status)

    if level and level != "All":
        conditions.append("s.level = %s")
        params.append(level)

    if query:
        conditions.append("LOWER(j.company || ' ' || j.title || ' ' || j.location) LIKE %s")
        q_like = f"%{query.lower().strip()}%"
        params.append(q_like)

    if search_term:
        conditions.append("j.search_term = %s")
        params.append(search_term)

    if source:
        conditions.append("j.source = %s")
        params.append(source)

    where = " AND ".join(conditions)

    allowed_sorts = {
        "last_seen", "first_seen", "score_pct", "ai_score", "score",
        "competition", "min_exp", "level", "company", "title",
    }
    if sort_col not in allowed_sorts:
        sort_col = "first_seen"
    direction = "ASC" if sort_dir == "asc" else "DESC"

    # Paired primary/secondary: when sorting by recency, AI score breaks ties;
    # when sorting by AI score, recency breaks ties. score_pct is the final fallback.
    if sort_col == "ai_score":
        primary_sql = "COALESCE(s.ai_score, 0)"
        secondary_sql = "s.first_seen DESC NULLS LAST"
    elif sort_col == "first_seen":
        primary_sql = "s.first_seen"
        secondary_sql = "COALESCE(s.ai_score, 0) DESC"
    else:
        primary_sql = _col(sort_col)
        secondary_sql = "COALESCE(s.ai_score, 0) DESC"

    order = f"""
        CASE WHEN s.status IN ('Applied', 'Not Interested') THEN 1 ELSE 0 END,
        {primary_sql} {direction} NULLS LAST,
        {secondary_sql},
        s.score_pct DESC
    """

    sql = (f"SELECT {_qualified_select()}, s.viewed_at FROM {_USER_JOBS_FROM} "
           f"WHERE {where} ORDER BY {order}")
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    result = []
    for row in rows:
        d = _row_to_dict(row[:-1], JOB_COLUMNS)
        d["viewed_at"] = row[-1].isoformat() if row[-1] else None
        d["_key"] = d["url"]
        d["_recency"] = format_recency(d.get("first_seen", ""))
        result.append(d)
    return result


def get_status_counts(user_id: int = DEFAULT_USER_ID, source: str = "") -> dict[str, int]:
    """Return count of one user's jobs per status + recommended count."""
    where = "WHERE s.user_id = %s" + (" AND j.source = %s" if source else "")
    params = (_sponsor_company_list(user_id), user_id) + ((source,) if source else ())
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE s.status = 'Tracking') AS tracking,
                    COUNT(*) FILTER (WHERE s.status = 'Applied') AS applied,
                    COUNT(*) FILTER (WHERE s.status = 'Not Interested') AS not_interested,
                    COUNT(*) FILTER (WHERE s.recommended = TRUE AND COALESCE(s.status, '') NOT IN ('Not Interested')) AS recommended,
                    {_SPONSOR_COUNT_SQL}
                FROM {_USER_JOBS_FROM} {where}
            """, params)
            row = cur.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    return {
        "All": row[0],
        "Tracking": row[1],
        "Applied": row[2],
        "Not Interested": row[3],
        "Recommended": row[4],
        SPONSOR_STATUS: row[5],
    }


def get_level_counts(user_id: int = DEFAULT_USER_ID, source: str = "") -> dict[str, int]:
    """Return count of one user's jobs per level tag."""
    where = "WHERE s.user_id = %s" + (" AND j.source = %s" if source else "")
    params = (user_id, source) if source else (user_id,)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT s.level, COUNT(*) FROM {_USER_JOBS_FROM} {where} GROUP BY s.level",
                params,
            )
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    counts = {"All": 0, "New Grad": 0, "Entry": 0, "Mid": 0, "Unknown": 0}
    for level, cnt in rows:
        if level in counts:
            counts[level] = cnt
        else:
            counts["Unknown"] = counts.get("Unknown", 0) + cnt
    counts["All"] = sum(v for k, v in counts.items() if k != "All")
    return counts


def get_filtered_counts(
    user_id: int = DEFAULT_USER_ID,
    time_range: str = "",
    bucket_filter: str = "",
    tz_offset: int = 0,
    query: str = "",
    search_term: str = "",
    source: str = "",
) -> dict:
    """Compute one user's status + level counts with time/search filters applied."""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    conditions = ["s.user_id = %s"]
    params = [user_id]

    if time_range == "hour":
        conditions.append("s.first_seen >= %s")
        params.append(now_utc - timedelta(hours=1))
    elif time_range == "today":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        conditions.append("s.first_seen >= %s")
        params.append(local_midnight.astimezone(timezone.utc))
    elif time_range == "yesterday":
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        conditions.append("s.first_seen >= %s AND s.first_seen < %s")
        params.append((local_midnight - timedelta(days=1)).astimezone(timezone.utc))
        params.append(local_midnight.astimezone(timezone.utc))

    if bucket_filter:
        try:
            local_start = datetime.strptime(bucket_filter, "%Y-%m-%d_%H:%M").replace(tzinfo=user_tz)
            bm = _bucket_minutes(local_start)
            bucket_start = local_start.astimezone(timezone.utc)
            bucket_end = bucket_start + timedelta(minutes=bm)
            conditions.append("s.first_seen >= %s AND s.first_seen < %s")
            params.append(bucket_start)
            params.append(bucket_end)
        except (ValueError, TypeError):
            pass

    if query:
        conditions.append("LOWER(j.company || ' ' || j.title || ' ' || j.location) LIKE %s")
        q_like = f"%{query.lower().strip()}%"
        params.append(q_like)

    if search_term:
        conditions.append("j.search_term = %s")
        params.append(search_term)

    if source:
        conditions.append("j.source = %s")
        params.append(source)

    where = " AND ".join(conditions)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE s.status = 'Tracking') AS tracking,
                    COUNT(*) FILTER (WHERE s.status = 'Applied') AS applied,
                    COUNT(*) FILTER (WHERE s.status = 'Not Interested') AS not_interested,
                    COUNT(*) FILTER (WHERE s.recommended = TRUE AND COALESCE(s.status, '') NOT IN ('Not Interested')) AS recommended,
                    COUNT(*) FILTER (WHERE s.level = 'New Grad') AS new_grad,
                    COUNT(*) FILTER (WHERE s.level = 'Entry') AS entry,
                    COUNT(*) FILTER (WHERE s.level = 'Mid') AS mid,
                    COUNT(*) FILTER (WHERE s.level = 'Unknown') AS unknown,
                    {_SPONSOR_COUNT_SQL}
                FROM {_USER_JOBS_FROM} WHERE {where}
            """, [_sponsor_company_list(user_id)] + params)
            row = cur.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    return {
        "status": {
            "All": row[0],
            "Tracking": row[1],
            "Applied": row[2],
            "Not Interested": row[3],
            "Recommended": row[4],
            SPONSOR_STATUS: row[9],
        },
        "level": {
            "All": row[0],
            "New Grad": row[5],
            "Entry": row[6],
            "Mid": row[7],
            "Unknown": row[8],
        },
    }


def get_time_counts(user_id: int = DEFAULT_USER_ID, tz_offset: int = 0,
                    time_range: str = "", source: str = "") -> dict:
    """Return one user's time tab counts + 30-min bucket breakdown."""
    now_utc = datetime.now(tz=timezone.utc)
    user_tz = timezone(timedelta(minutes=-tz_offset))
    now_local = now_utc.astimezone(user_tz)

    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = local_midnight.astimezone(timezone.utc)
    yesterday_start_utc = (local_midnight - timedelta(days=1)).astimezone(timezone.utc)
    one_hour_ago = now_utc - timedelta(hours=1)

    base = "WHERE s.user_id = %s" + (" AND j.source = %s" if source else "")
    base_param = (user_id, source) if source else (user_id,)

    # Determine bucket query window based on active time range
    if time_range == "hour":
        bucket_start_utc = one_hour_ago
        bucket_end_utc = None
    elif time_range == "today":
        bucket_start_utc = today_start_utc
        bucket_end_utc = None
    elif time_range == "yesterday":
        bucket_start_utc = yesterday_start_utc
        bucket_end_utc = today_start_utc
    else:
        # "all" or empty — show all jobs' buckets
        bucket_start_utc = None
        bucket_end_utc = None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) FILTER (WHERE s.first_seen >= %s) AS this_hour,
                    COUNT(*) FILTER (WHERE s.first_seen >= %s) AS today,
                    COUNT(*) FILTER (WHERE s.first_seen >= %s AND s.first_seen < %s) AS yesterday
                FROM {_USER_JOBS_FROM} {base}
            """, (one_hour_ago, today_start_utc, yesterday_start_utc, today_start_utc) + base_param)
            tab_row = cur.fetchone()

            if bucket_start_utc and bucket_end_utc:
                cur.execute(
                    f"SELECT s.first_seen FROM {_USER_JOBS_FROM} {base} "
                    "AND s.first_seen >= %s AND s.first_seen < %s",
                    base_param + (bucket_start_utc, bucket_end_utc),
                )
            elif bucket_start_utc:
                cur.execute(
                    f"SELECT s.first_seen FROM {_USER_JOBS_FROM} {base} AND s.first_seen >= %s",
                    base_param + (bucket_start_utc,),
                )
            else:
                cur.execute(f"SELECT s.first_seen FROM {_USER_JOBS_FROM} {base}", base_param)
            fs_rows = cur.fetchall()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

    buckets = {}
    for (fs_dt,) in fs_rows:
        if fs_dt is None:
            continue
        if fs_dt.tzinfo is None:
            fs_dt = fs_dt.replace(tzinfo=timezone.utc)
        fs_local = fs_dt.astimezone(user_tz)
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
        "this_hour": tab_row[0],
        "today": tab_row[1],
        "yesterday": tab_row[2],
        "buckets": bucket_list,
    }


def get_search_terms(user_id: int = DEFAULT_USER_ID, source: str = "") -> list[str]:
    """Return sorted list of distinct search_term values among a user's jobs."""
    extra = "AND j.source = %s" if source else ""
    params = (user_id, source) if source else (user_id,)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT j.search_term FROM {_USER_JOBS_FROM} "
                f"WHERE s.user_id = %s AND j.search_term != '' {extra} ORDER BY j.search_term",
                params,
            )
            return [row[0] for row in cur.fetchall()]
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_job(url: str, user_id: int = DEFAULT_USER_ID) -> dict | None:
    """Get a single job (with the user's state) by URL."""
    url = normalize_url(url)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_qualified_select()} FROM {_USER_JOBS_FROM} "
                "WHERE s.user_id = %s AND s.url = %s",
                (user_id, url),
            )
            row = cur.fetchone()
            if not row:
                return None
            d = _row_to_dict(row, JOB_COLUMNS)
            d["_key"] = d["url"]
            return d
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_last_updated(user_id: int = DEFAULT_USER_ID) -> str:
    """Return the most recent last_seen timestamp across one user's jobs."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(last_seen) FROM user_job_state WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row and row[0]:
                return row[0].isoformat()
            return ""
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Seen jobs (dedup tracking)
# ---------------------------------------------------------------------------

SEEN_TTL_HOURS = 48


def load_seen_jobs() -> dict[str, str]:
    """Load seen job URLs, pruning entries older than 48h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_TTL_HOURS)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url, seen_at FROM seen_jobs WHERE seen_at > %s", (cutoff,))
            return {row[0]: row[1].isoformat() for row in cur.fetchall()}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def save_seen_job(url: str) -> None:
    """Mark a job URL as seen."""
    url = normalize_url(url)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO seen_jobs (url, seen_at) VALUES (%s, NOW())
                ON CONFLICT (url) DO UPDATE SET seen_at = NOW()
            """, (url,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def save_seen_jobs_bulk(seen: dict[str, str]) -> None:
    """Bulk upsert seen jobs dict."""
    if not seen:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_TTL_HOURS)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seen_jobs WHERE seen_at < %s", (cutoff,))
            args = [(normalize_url(url), ts) for url, ts in seen.items() if url]
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO seen_jobs (url, seen_at) VALUES (%s, %s)
                ON CONFLICT (url) DO UPDATE SET seen_at = EXCLUDED.seen_at
            """, args)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def prune_seen_jobs() -> int:
    """Delete seen_jobs entries older than 48h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_TTL_HOURS)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seen_jobs WHERE seen_at < %s", (cutoff,))
            count = cur.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# One-time URL canonicalization
# ---------------------------------------------------------------------------

_STATUS_PRIORITY = {"Applied": 3, "Tracking": 2, "Not Interested": 1, "": 0}


def _merge_job_rows(rows: list[dict], canonical_url: str) -> dict:
    """Fold duplicate job rows into one, keyed by the canonical URL.

    Precedence: strongest user status wins; within that, the row with an
    AI score wins; within that, the longest description wins. Earliest
    first_seen and latest last_seen are adopted across the group.
    """
    def rank(j: dict) -> tuple:
        return (
            _STATUS_PRIORITY.get(j.get("status") or "", 0),
            1 if j.get("ai_score") is not None else 0,
            len(j.get("description_preview") or ""),
        )

    rows = sorted(rows, key=rank, reverse=True)
    best = dict(rows[0])
    for other in rows[1:]:
        if best.get("ai_score") is None and other.get("ai_score") is not None:
            best["ai_score"] = other["ai_score"]
            best["ai_reason"] = other.get("ai_reason") or ""
            best["ai_model"] = other.get("ai_model")
            best["recommended"] = other.get("recommended", False)
        if len(other.get("description_preview") or "") > len(best.get("description_preview") or ""):
            best["description_preview"] = other["description_preview"]
        for field, cmp in (("first_seen", min), ("last_seen", max)):
            a, b = best.get(field), other.get(field)
            if a and b:
                best[field] = cmp(a, b)
            elif b:
                best[field] = b
    best["url"] = canonical_url
    return best


def normalize_existing_urls() -> dict:
    """One-shot: rewrite stored URLs in their canonical form across all tables.

    Groups rows by normalize_url(url); when a group has multiple rows they're
    merged via _merge_job_rows() to preserve user status and AI scores before
    the originals are deleted. Safe to run more than once — rows already in
    canonical form are skipped.

    Returns {"jobs_merged", "jobs_rekeyed", "seen_rekeyed", "dismissed_rekeyed"}.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # --- jobs ---
            cur.execute(f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs")
            groups: dict[str, list[dict]] = {}
            for row in cur.fetchall():
                d = dict(zip(JOB_COLUMNS, row))
                norm = normalize_url(d["url"] or "")
                if not norm:
                    continue
                groups.setdefault(norm, []).append(d)

            merged = 0
            rekeyed = 0
            for norm, rows in groups.items():
                if len(rows) == 1 and rows[0]["url"] == norm:
                    continue
                best = _merge_job_rows(rows, norm)
                old_urls = [r["url"] for r in rows]
                cur.execute("DELETE FROM jobs WHERE url = ANY(%s)", (old_urls,))
                cur.execute(f"""
                    INSERT INTO jobs ({', '.join(JOB_COLUMNS)})
                    VALUES ({', '.join(['%s'] * len(JOB_COLUMNS))})
                    ON CONFLICT (url) DO NOTHING
                """, tuple(best.get(c) for c in JOB_COLUMNS))
                if len(rows) > 1:
                    merged += 1
                else:
                    rekeyed += 1

            # --- seen_jobs --- (keep latest seen_at per canonical URL)
            cur.execute("SELECT url, seen_at FROM seen_jobs")
            seen_map: dict[str, tuple[list[str], datetime]] = {}
            for url, seen_at in cur.fetchall():
                norm = normalize_url(url or "")
                if not norm:
                    continue
                if norm not in seen_map:
                    seen_map[norm] = ([url], seen_at)
                else:
                    urls, latest = seen_map[norm]
                    urls.append(url)
                    if seen_at > latest:
                        latest = seen_at
                    seen_map[norm] = (urls, latest)
            seen_rekeyed = 0
            for norm, (urls, seen_at) in seen_map.items():
                if urls == [norm]:
                    continue
                cur.execute("DELETE FROM seen_jobs WHERE url = ANY(%s)", (urls,))
                cur.execute(
                    "INSERT INTO seen_jobs (url, seen_at) VALUES (%s, %s) "
                    "ON CONFLICT (url) DO UPDATE SET seen_at = EXCLUDED.seen_at",
                    (norm, seen_at),
                )
                seen_rekeyed += 1

            # --- dismissed_jobs --- (keep earliest dismissed_at — first dismissal wins)
            cur.execute("SELECT url, dismissed_at FROM dismissed_jobs")
            dis_map: dict[str, tuple[list[str], datetime]] = {}
            for url, dismissed_at in cur.fetchall():
                norm = normalize_url(url or "")
                if not norm:
                    continue
                if norm not in dis_map:
                    dis_map[norm] = ([url], dismissed_at)
                else:
                    urls, earliest = dis_map[norm]
                    urls.append(url)
                    if dismissed_at < earliest:
                        earliest = dismissed_at
                    dis_map[norm] = (urls, earliest)
            dismissed_rekeyed = 0
            for norm, (urls, dismissed_at) in dis_map.items():
                if urls == [norm]:
                    continue
                cur.execute("DELETE FROM dismissed_jobs WHERE url = ANY(%s)", (urls,))
                cur.execute(
                    "INSERT INTO dismissed_jobs (url, dismissed_at) VALUES (%s, %s) "
                    "ON CONFLICT (url) DO NOTHING",
                    (norm, dismissed_at),
                )
                dismissed_rekeyed += 1

        conn.commit()
        return {
            "jobs_merged": merged,
            "jobs_rekeyed": rekeyed,
            "seen_rekeyed": seen_rekeyed,
            "dismissed_rekeyed": dismissed_rekeyed,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
