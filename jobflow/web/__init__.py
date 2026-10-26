"""JobFlow Web Dashboard — single-page job feed.

The web app is exactly one page: the LinkedIn job feed.

Routes:
    /                    → Redirects to /linkedin
    /linkedin            → The job feed (filtering, sorting, time buckets)
    /health              → JSON health check for uptime monitoring
    /api/linkedin/jobs   → Filtered table rows (HTML fragment + count headers)
    /api/linkedin/meta   → Filter counts as JSON
    /api/linkedin/...    → Delete / bulk-delete / refresh
    /api/scan/*          → Background scan (powers the feed's "Scan Now" button)
    /api/aiscore/*       → Local AI scoring runner (claude/codex CLI subprocess)

Data flow:
    1. "Scan Now" → /api/scan/trigger → _run_scan() fetches + filters + dedups
    2. Results saved to scan_results.json and merged into the store
       (PostgreSQL when DATABASE_URL is set, else data/ci/linkedin_jobs.json)
    3. /api/linkedin/jobs returns the filtered HTML table; response headers
       carry count metadata (X-Counts, X-Time-Counts, ...) so the page JS can
       update chips and stat tiles without a second request
    4. "AI Score" → /api/aiscore/trigger runs scripts/ai_score_local.py with
       the signed-in claude/codex CLI and streams progress to the modal

Auto-pull (local JSON backend only):
    A background thread runs `git pull --rebase` every hour to pick up CI scan
    results, then re-merges into the store. Disabled on Render.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, render_template, request, jsonify, redirect, make_response, g

from ..config import load_config
from ..filter import algo_recommended
from ..linkedin_store import (
    load_store, save_store, merge_scan_results, prune_old_jobs,
    get_filtered_jobs, get_status_counts,
    get_level_counts, get_filtered_counts, get_search_terms,
    get_time_counts,
    backfill_job,
    delete_job as delete_json_job,
    STORE_LOCK,
    is_db_enabled,
)


# Shared scan state for the background scan thread.
# Since Flask runs in a single process (gunicorn -w 1), this dict is safe
# for thread communication without locks — only one scan runs at a time.
scan_state = {
    "running": False,
    "error": None,
    "total": 0,
    "relevant": 0,
    "skipped": 0,
    # Per-source progress for the Scan Now panel: [{key, label, url, status,
    # note, count, error}] with status pending/running/done/error/unavailable.
    "sources": [],
}

# Shared state for the GitHub Actions scan workflow trigger. One dispatch is
# tracked at a time; a background thread polls the run to completion so the
# "Scan Now" button can stay in a loading state for the whole run.
workflow_state = {
    "running": False,
    "run_id": None,
    "status": "",        # gh run status: queued / in_progress / completed
    "conclusion": "",    # success / failure / cancelled (when completed)
    "url": "",
    "error": None,
    "mode": "",          # github / local (see _scan_mode)
    "started_at": None,
    "finished_at": None,
}

# Shared state for the local AI scoring run (scripts/ai_score_local.py).
# Same thread-safety story as scan_state — one scoring run at a time.
ai_state = {
    "running": False,
    "engine": "",
    "hours": 0.0,
    "limit": 0,
    "rescore": False,
    "total": 0,        # jobs found to score
    "batch": 0,        # current batch number
    "batches": 0,      # total batches
    "scored": 0,
    "failed": 0,
    "log": [],         # tail of subprocess output lines
    "error": None,
    "ok": None,        # None while running, True/False when finished
    "cancelled": False,
    "started_at": None,
    "finished_at": None,
    "_process": None,
}

AI_ENGINES = ("claude", "codex")
AI_LOG_TAIL = 100  # keep this many output lines for the status endpoint

# The web server often runs with a minimal PATH (launched from an IDE, launchd,
# or a service manager) that misses the dirs where CLIs like codex/claude live.
# Search these well-known install locations in addition to the inherited PATH,
# and hand the augmented PATH to subprocesses so they resolve the CLIs too.
_EXTRA_BIN_DIRS = (
    Path.home() / ".npm-global" / "bin",   # npm prefix installs (codex)
    Path.home() / ".local" / "bin",        # pipx / local installs (claude)
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path.home() / ".bun" / "bin",
)


def _augmented_path() -> str:
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    seen = set(parts)
    candidates = list(_EXTRA_BIN_DIRS)
    candidates += sorted((Path.home() / ".nvm" / "versions" / "node").glob("*/bin"))
    for d in candidates:
        s = str(d)
        if s not in seen and d.is_dir():
            parts.append(s)
            seen.add(s)
    return os.pathsep.join(parts)


def _which_cli(name: str) -> str | None:
    """shutil.which() over the augmented PATH."""
    return shutil.which(name, path=_augmented_path())


def _ai_state_snapshot() -> dict:
    """JSON-safe copy of ai_state for the status endpoint."""
    snap = {k: v for k, v in ai_state.items() if not k.startswith("_")}
    snap["log"] = list(snap["log"])[-25:]
    return snap


def _run_ai_score(root: Path, engine: str, hours: float, limit: int, rescore: bool,
                  user_id: int = 1):
    """Run scripts/ai_score_local.py as a subprocess and stream its progress.

    The script prints well-known lines we parse for live progress:
        "Found N jobs to score (B batches of 15)"
        "--- Batch i/N (k jobs) ---"
        "Done: X scored, Y failed out of Z total"
        "Nothing to score!"
    """
    script = root / "scripts" / "ai_score_local.py"
    cmd = [sys.executable, "-u", str(script), "--engine", engine,
           "--user-id", str(user_id)]
    if hours:
        cmd += ["--hours", str(hours)]
    if limit:
        cmd += ["--limit", str(limit)]
    if rescore:
        cmd += ["--rescore"]

    done_line_seen = False
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # PYTHONUTF8: the child prints job titles; Windows' cp1252 pipe
            # encoding would crash it on non-Latin characters.
            env={**os.environ, "PATH": _augmented_path(), "PYTHONUTF8": "1"},
        )
        ai_state["_process"] = proc

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            log = ai_state["log"]
            log.append(line)
            if len(log) > AI_LOG_TAIL:
                del log[: len(log) - AI_LOG_TAIL]

            m = re.search(r"Found (\d+) jobs to score \((\d+) batch", line)
            if m:
                ai_state["total"] = int(m.group(1))
                ai_state["batches"] = int(m.group(2))
                continue
            m = re.match(r"--- Batch (\d+)/(\d+)", line)
            if m:
                ai_state["batch"] = int(m.group(1))
                ai_state["batches"] = int(m.group(2))
                continue
            m = re.match(r"Done: (\d+) scored, (\d+) failed out of (\d+) total", line)
            if m:
                ai_state["scored"] = int(m.group(1))
                ai_state["failed"] = int(m.group(2))
                ai_state["total"] = int(m.group(3))
                done_line_seen = True
                continue
            if "Nothing to score!" in line:
                done_line_seen = True

        returncode = proc.wait()

        if ai_state["cancelled"]:
            ai_state["ok"] = False
            ai_state["error"] = "Cancelled by user."
        elif returncode != 0:
            ai_state["ok"] = False
            ai_state["error"] = f"Scorer exited with code {returncode}."
        elif not done_line_seen:
            ai_state["ok"] = False
            ai_state["error"] = "Scorer ended without reporting results."
        elif ai_state["failed"] > 0:
            ai_state["ok"] = False
            ai_state["error"] = f"{ai_state['failed']} job(s) failed to score."
        else:
            ai_state["ok"] = True
    except Exception as e:
        ai_state["ok"] = False
        ai_state["error"] = str(e)
    finally:
        ai_state["running"] = False
        ai_state["finished_at"] = time.time()
        ai_state["_process"] = None


# Per-run cap for API-key (server-side) scoring so one click can't run a huge
# bill on a friend's key. Effective limit = min(requested or default, hard cap).
API_DEFAULT_LIMIT = 200
API_MAX_PER_RUN = 250


def _run_ai_score_api(user_id: int, hours: float, limit: int, rescore: bool):
    """Score a user's eligible jobs server-side via their stored Anthropic key.

    In-process analog of _run_ai_score (which shells out to a signed-in CLI).
    Updates ``ai_state`` so the existing modal/progress UI works unchanged.
    """
    from .. import db
    from ..ai_prompt import BATCH_SIZE, is_blocked_staffing_source, STAFFING_BLOCK_REASON
    from ..ai_scorer_anthropic import score_batch, normalize_model
    from ..crypto import decrypt_secret

    try:
        prof = db.get_user_profile(user_id) or {}
        api_key = decrypt_secret(db.get_user_api_key_enc(user_id))
        model = normalize_model(prof.get("ai_model"))
        profile_text = prof.get("ai_profile_text") or ""
        if not api_key:
            ai_state["error"] = "No Anthropic API key set — add one in Settings."
            ai_state["ok"] = False
            return

        eff_limit = min(limit or API_DEFAULT_LIMIT, API_MAX_PER_RUN)
        rows = db.get_unscored_user_jobs(user_id, hours=hours, limit=eff_limit, rescore=rescore)
        total = len(rows)
        ai_state["total"] = total
        ai_state["batches"] = (total + BATCH_SIZE - 1) // BATCH_SIZE
        if not rows:
            ai_state["ok"] = True
            return

        scored_n = failed_n = 0
        for i in range(0, total, BATCH_SIZE):
            if ai_state.get("cancelled"):
                break
            batch = rows[i:i + BATCH_SIZE]
            ai_state["batch"] = i // BATCH_SIZE + 1
            blocked = [r for r in batch if is_blocked_staffing_source(r)]
            scorable = [r for r in batch if not is_blocked_staffing_source(r)]
            if blocked:
                db.apply_user_ai_scores(
                    user_id,
                    [(r[0], {"score": 0, "reason": STAFFING_BLOCK_REASON}) for r in blocked],
                    model,
                )
                scored_n += len(blocked)
            if scorable:
                scores = score_batch(scorable, profile_text, api_key, model)
                if scores and len(scores) == len(scorable):
                    db.apply_user_ai_scores(
                        user_id, [(r[0], s) for r, s in zip(scorable, scores)], model
                    )
                    scored_n += len(scorable)
                else:
                    failed_n += len(scorable)
            ai_state["scored"] = scored_n
            ai_state["failed"] = failed_n
        ai_state["ok"] = failed_n == 0
    except Exception as e:
        ai_state["ok"] = False
        ai_state["error"] = str(e)
    finally:
        ai_state["running"] = False
        ai_state["finished_at"] = time.time()


def _parse_iso(ts: str) -> float | None:
    """Parse a gh ISO-8601 timestamp (e.g. 2026-06-24T06:23:00Z) to epoch secs."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _track_workflow_run(root: Path, dispatch_ts: float):
    """Find the run our dispatch created and poll it until it completes.

    Keeps ``workflow_state`` current so the front-end can show a live loading
    state. Runs in a daemon thread.
    """
    gh = _which_cli("gh")
    if not gh:
        workflow_state["error"] = "'gh' CLI not found."
        workflow_state["running"] = False
        workflow_state["finished_at"] = time.time()
        return

    def _gh_json(args):
        proc = subprocess.run(
            [gh, *args], cwd=str(root), capture_output=True, text=True,
            timeout=30, env={**os.environ, "PATH": _augmented_path()},
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip())
        return json.loads(proc.stdout or "null")

    try:
        # The dispatched run takes a moment to register — look for a
        # workflow_dispatch run created at/after our trigger time.
        run = None
        find_deadline = time.time() + 60
        while time.time() < find_deadline and not run:
            runs = _gh_json([
                "run", "list", "--workflow", "scan-jobs.yml",
                "--event", "workflow_dispatch", "--limit", "10",
                "--json", "databaseId,status,conclusion,createdAt,url",
            ]) or []
            for r in runs:
                created = _parse_iso(r.get("createdAt", ""))
                if created is not None and created >= dispatch_ts - 15:
                    run = r
                    break
            if run:
                break
            time.sleep(3)

        if not run:
            workflow_state["error"] = "Triggered the workflow, but couldn't locate the run to track it."
            return

        workflow_state["run_id"] = run["databaseId"]
        workflow_state["url"] = run.get("url", "")
        workflow_state["status"] = run.get("status", "queued")

        # Poll until the run completes.
        while True:
            data = _gh_json([
                "run", "view", str(run["databaseId"]),
                "--json", "status,conclusion,url",
            ]) or {}
            workflow_state["status"] = data.get("status", "")
            workflow_state["url"] = data.get("url", workflow_state["url"])
            if data.get("status") == "completed":
                workflow_state["conclusion"] = data.get("conclusion", "")
                break
            time.sleep(5)
    except Exception as e:
        workflow_state["error"] = str(e)
    finally:
        workflow_state["running"] = False
        workflow_state["finished_at"] = time.time()


def create_app():
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )

    # Behind Render's TLS proxy: trust X-Forwarded-Proto/Host so Flask builds
    # https URLs and the OAuth session round-trips correctly.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    # Session cookie must survive the top-level redirect back from Google.
    app.config.update(
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
    )

    config = load_config()
    app.config["JOBFLOW"] = config

    # ── Auth (Google OAuth when configured, else single-user operator) ──
    from ..auth import bp as auth_bp, init_oauth, resolve_request_user, auth_enabled
    init_oauth(app)
    app.register_blueprint(auth_bp)

    @app.before_request
    def _resolve_user():
        return resolve_request_user()

    @app.context_processor
    def _inject_user():
        return {"current_user": getattr(g, "user", None), "auth_enabled": auth_enabled()}

    linkedin_store_path = config["_root"] / "data" / "ci" / "linkedin_jobs.json"
    scan_results_path = config["_root"] / "data" / "ci" / "scan_results.json"

    # ── DB backend plumbing (falls back to JSON store) ───────────

    db_state = {"module": None, "last_attempt": 0.0, "last_error": ""}

    def _get_db():
        """Return the DB module when configured and reachable, else None."""
        if not is_db_enabled():
            db_state["module"] = None
            return None
        if db_state["module"] is not None:
            return db_state["module"]
        now = time.time()
        if db_state["last_attempt"] and now - db_state["last_attempt"] < 60:
            return None
        db_state["last_attempt"] = now
        try:
            from .. import db as db_module
            db_module.init_db()
            db_state["module"] = db_module
            db_state["last_error"] = ""
            return db_module
        except Exception as e:
            db_state["last_error"] = str(e)
            db_state["module"] = None
            return None

    def _try_db(fn):
        db = _get_db()
        if db is None:
            return False, None
        try:
            return True, fn(db)
        except Exception as e:
            db_state["last_error"] = str(e)
            db_state["module"] = None
            db_state["last_attempt"] = time.time()
            return False, None

    def _do_linkedin_merge():
        """Merge scan_results.json into linkedin_jobs.json (JSON backend only)."""
        if _get_db() is not None:
            return  # DB backend handles merges directly
        if not scan_results_path.exists():
            return
        try:
            scan_data = json.loads(scan_results_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return
        with STORE_LOCK:
            store = load_store(linkedin_store_path)
            store = merge_scan_results(store, scan_data)
            store = prune_old_jobs(store)
            for key in store.get("jobs", {}):
                store["jobs"][key] = backfill_job(store["jobs"][key])
            save_store(linkedin_store_path, store)

    # Merge once on startup (JSON backend only)
    _do_linkedin_merge()

    # Auto-pull thread (JSON backend, local dev only)
    def _auto_pull_loop():
        while True:
            time.sleep(3600)
            try:
                subprocess.run(
                    ["git", "pull", "--rebase", "origin", "main"],
                    cwd=str(config["_root"]),
                    capture_output=True,
                    timeout=30,
                )
                _do_linkedin_merge()
            except Exception:
                pass

    if not is_db_enabled() and not os.environ.get("RENDER"):
        pull_thread = threading.Thread(target=_auto_pull_loop, daemon=True)
        pull_thread.start()

    # ── Feed helpers ─────────────────────────────────────────────

    # Source filter for the feed queries. Empty = every source (LinkedIn plus
    # the GitHub new-grad repos); set e.g. "linkedin" to narrow the feed.
    SOURCE = ""

    def _annotate_applied(jobs):
        """Mark jobs that have a tailored-application folder (operator only —
        the folders live on the operator's machine)."""
        from ..applied import annotate
        from ..db import DEFAULT_USER_ID
        if g.get("user_id", DEFAULT_USER_ID) != DEFAULT_USER_ID:
            return jobs
        return annotate(jobs, config)

    def _feed_args():
        try:
            tz_offset = int(request.args.get("tz", "0") or "0")
        except ValueError:
            tz_offset = 0
        try:
            limit = int(request.args.get("limit", "250") or "250")
        except ValueError:
            limit = 250
        limit = min(max(limit, 50), 500)
        return {
            "status_filter": request.args.get("status", ""),
            "level_filter": request.args.get("level", ""),
            "query": request.args.get("q", ""),
            "search_term": request.args.get("search_term", ""),
            "time_range": request.args.get("time", ""),
            "bucket_filter": request.args.get("bucket", ""),
            "sort_col": request.args.get("sort", "first_seen"),
            "sort_dir": request.args.get("dir", "desc"),
            "tz_offset": tz_offset,
            "limit": limit,
            "include_time_meta": request.args.get("meta", "1") == "1",
        }

    def _feed_counts(args: dict):
        ok, counts = _try_db(lambda db: db.get_filtered_counts(
            user_id=g.user_id,
            time_range=args["time_range"], bucket_filter=args["bucket_filter"],
            tz_offset=args["tz_offset"], query=args["query"],
            search_term=args["search_term"], source=SOURCE,
        ))
        if ok:
            return counts

        store = load_store(linkedin_store_path)
        return get_filtered_counts(
            store, time_range=args["time_range"], bucket_filter=args["bucket_filter"],
            tz_offset=args["tz_offset"], query=args["query"],
            search_term=args["search_term"], source=SOURCE,
        )

    def _feed_meta(args: dict):
        fc = _feed_counts(args)
        payload = {
            "counts": fc["status"],
            "level_counts": fc["level"],
            "total": fc["status"].get("All", 0),
        }
        if args["include_time_meta"]:
            ok, time_counts = _try_db(lambda db: db.get_time_counts(
                user_id=g.user_id,
                tz_offset=args["tz_offset"], time_range=args["time_range"], source=SOURCE,
            ))
            if not ok:
                store = load_store(linkedin_store_path)
                time_counts = get_time_counts(
                    store, tz_offset=args["tz_offset"], time_range=args["time_range"], source=SOURCE,
                )
            payload["time_counts"] = {
                "this_hour": time_counts["this_hour"],
                "today": time_counts["today"],
                "yesterday": time_counts["yesterday"],
            }
            payload["buckets"] = time_counts.get("buckets", [])
        return payload

    # ── Page Routes ──────────────────────────────────────────────

    @app.route("/")
    def index():
        return redirect("/linkedin")

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.route("/linkedin")
    def linkedin_page():
        ok, payload = _try_db(lambda db: (
            db.get_status_counts(user_id=g.user_id, source=SOURCE),
            db.get_level_counts(user_id=g.user_id, source=SOURCE),
            db.get_search_terms(user_id=g.user_id, source=SOURCE),
            db.get_time_counts(user_id=g.user_id, source=SOURCE, time_range="today"),
            db.get_last_updated(user_id=g.user_id),
        ))
        if ok:
            counts, level_counts, search_terms, time_counts, last_updated = payload
        else:
            store = load_store(linkedin_store_path)
            counts = get_status_counts(store, source=SOURCE)
            level_counts = get_level_counts(store, source=SOURCE)
            search_terms = get_search_terms(store, source=SOURCE)
            time_counts = get_time_counts(store, source=SOURCE, time_range="today")
            last_updated = store.get("last_updated", "")
        return render_template(
            "linkedin.html",
            counts=counts,
            level_counts=level_counts,
            search_terms=search_terms,
            time_counts=time_counts,
            last_updated=last_updated,
            scan_running=scan_state["running"],
            workflow_running=workflow_state["running"],
            ai_running=ai_state["running"],
            ai_provider=(_try_db(lambda db: db.get_user_profile(g.user_id))[1] or {}).get("ai_provider", "none"),
        )

    # ── Settings (per-user profile; the web-form onboarding path) ──

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        import dataclasses
        from ..filter_profile import FilterProfile, profile_from_config, profile_to_config

        db = _get_db()
        if db is None:
            # Settings need the multi-user DB backend (profiles aren't stored in
            # the local JSON store).
            return render_template("settings.html", db_off=True, p=None,
                                   profile=None, saved=False)

        prof = db.get_user_profile(g.user_id) or {}
        fp = profile_from_config(prof.get("filter_config") or {})

        if request.method == "POST":
            def _int(v, default):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return default
            form = request.form
            new_fp = dataclasses.replace(
                fp,
                requires_sponsorship=("requires_sponsorship" in form),
                us_only=("us_only" in form),
                max_min_exp=_int(form.get("max_min_exp"), fp.max_min_exp),
                recommended_min_pct=_int(form.get("recommended_min_pct"), fp.recommended_min_pct),
            )
            search_terms = [t.strip() for t in form.get("search_terms", "").split(",") if t.strip()]
            from ..ai_scorer_anthropic import normalize_model
            db.upsert_user_profile(
                g.user_id,
                filter_config=profile_to_config(new_fp),
                ai_profile_text=form.get("ai_profile_text", ""),
                search_terms=search_terms,
                ai_provider=form.get("ai_provider", "none"),
                ai_model=normalize_model(form.get("ai_model", "")),
            )
            # API key: blank input leaves it unchanged; "remove_api_key" clears it;
            # otherwise encrypt + store. The plaintext never round-trips to the client.
            if "remove_api_key" in form:
                db.set_user_api_key_enc(g.user_id, None)
            elif form.get("api_key", "").strip():
                from ..crypto import encrypt_secret
                db.set_user_api_key_enc(g.user_id, encrypt_secret(form["api_key"].strip()))
            # Generate/rotate the local-CLI pairing token on request (rotating revokes the old).
            if "regenerate_token" in form:
                import secrets as _secrets
                db.set_pairing_token(g.user_id, _secrets.token_urlsafe(32))
            # Re-score the user's pool with the new profile, off the request.
            uid = g.user_id
            threading.Thread(target=db.backfill_user, args=(uid,), daemon=True).start()
            return redirect("/settings?saved=1")

        return render_template(
            "settings.html",
            db_off=False,
            p=fp,
            profile=prof,
            search_terms=", ".join(prof.get("search_terms") or []),
            saved=request.args.get("saved") == "1",
        )

    # ── Feed API ─────────────────────────────────────────────────

    @app.route("/api/linkedin/jobs")
    def api_linkedin_jobs():
        args = _feed_args()

        ok, jobs = _try_db(lambda db: db.get_filtered_jobs(
            user_id=g.user_id,
            status=args["status_filter"], level=args["level_filter"], query=args["query"],
            search_term=args["search_term"], time_range=args["time_range"],
            bucket_filter=args["bucket_filter"], sort_col=args["sort_col"],
            sort_dir=args["sort_dir"], tz_offset=args["tz_offset"], source=SOURCE,
            limit=args["limit"],
        ))
        if not ok:
            store = load_store(linkedin_store_path)
            jobs = get_filtered_jobs(
                store, status=args["status_filter"], level=args["level_filter"], query=args["query"],
                search_term=args["search_term"], time_range=args["time_range"],
                bucket_filter=args["bucket_filter"], sort_col=args["sort_col"],
                sort_dir=args["sort_dir"], tz_offset=args["tz_offset"], source=SOURCE,
                limit=args["limit"],
            )

        meta = _feed_meta(args)
        resp = make_response(render_template(
            "_partials/linkedin_tbody.html",
            jobs=_annotate_applied(jobs),
        ))
        resp.headers["X-Counts"] = json.dumps(meta["counts"])
        resp.headers["X-Level-Counts"] = json.dumps(meta["level_counts"])
        if "time_counts" in meta:
            resp.headers["X-Time-Counts"] = json.dumps(meta["time_counts"])
            resp.headers["X-Buckets"] = json.dumps(meta.get("buckets", []))
        resp.headers["X-Total"] = str(meta["total"])
        resp.headers["X-Displayed"] = str(len(jobs))
        return resp

    @app.route("/api/linkedin/meta")
    def api_linkedin_meta():
        return jsonify(_feed_meta(_feed_args()))

    @app.route("/api/linkedin/jobs/<path:key>", methods=["DELETE"])
    def api_linkedin_delete(key):
        ok, _ = _try_db(lambda db: db.delete_job(key, user_id=g.user_id))
        if not ok:
            with STORE_LOCK:
                store = load_store(linkedin_store_path)
                delete_json_job(store, key)
                save_store(linkedin_store_path, store)
        return "", 204

    @app.route("/api/linkedin/jobs/bulk-delete", methods=["POST"])
    def api_linkedin_bulk_delete():
        data = request.get_json(silent=True) or {}
        keys = data.get("keys") or []
        if not isinstance(keys, list):
            keys = []

        deleted = 0
        db = _get_db()
        if db is not None:
            try:
                for key in keys:
                    if key and db.delete_job(key, user_id=g.user_id):
                        deleted += 1
                return jsonify({"requested": len(keys), "deleted": deleted})
            except Exception as e:
                db_state["last_error"] = str(e)
                db_state["module"] = None
                db_state["last_attempt"] = time.time()

        with STORE_LOCK:
            store = load_store(linkedin_store_path)
            for key in keys:
                if key and delete_json_job(store, key):
                    deleted += 1
            if keys:
                save_store(linkedin_store_path, store)

        return jsonify({"requested": len(keys), "deleted": deleted})

    @app.route("/api/linkedin/refresh", methods=["POST"])
    def api_linkedin_refresh():
        ok, _ = _try_db(lambda db: db.prune_expired_jobs())
        if ok:
            return jsonify({"ok": True})
        try:
            subprocess.run(
                ["git", "pull", "--rebase", "origin", "main"],
                cwd=str(config["_root"]),
                capture_output=True,
                timeout=30,
            )
            _do_linkedin_merge()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ── Scan (powers the feed's "Scan Now" button) ───────────────

    @app.route("/api/scan/trigger", methods=["POST"])
    def api_trigger_scan():
        if scan_state["running"]:
            return jsonify({"error": "Scan already running"}), 409

        data = request.form or request.get_json(silent=True) or {}
        hours = int(data.get("hours", 0) or 0)
        new_only = data.get("new_only") in ("true", "on", True, "1")

        thread = threading.Thread(
            target=_run_scan,
            args=(config, None, hours, new_only),
            daemon=True,
        )
        thread.start()
        return jsonify({"running": True})

    @app.route("/api/scan/workflow", methods=["POST"])
    def api_scan_workflow():
        """Trigger the GitHub Actions scan workflow (workflow_dispatch).

        Equivalent to running `gh workflow run scan-jobs.yml --ref main` by
        hand. The scan then runs on GH's runners and commits results back; the
        feed picks them up on the next Refresh / hourly auto-pull.
        """
        if workflow_state["running"]:
            return jsonify({"ok": False, "error": "A scan is already running."}), 409

        gh = _which_cli("gh")
        if _scan_mode(gh) == "local":
            # Scan in-process (same LinkedIn scan as CI) and merge straight into
            # this server's store — for local installs whose DB the GitHub
            # workflow doesn't feed, or machines without the gh CLI.
            if scan_state["running"]:
                return jsonify({"ok": False, "error": "A scan is already running."}), 409
            workflow_state.update({
                "running": True, "run_id": None, "status": "in_progress",
                "conclusion": "", "url": "", "error": None, "mode": "local",
                "started_at": time.time(), "finished_at": None,
            })
            threading.Thread(target=_run_local_scan_tracked, args=(config,), daemon=True).start()
            return jsonify({"ok": True, "running": True, "mode": "local"})
        if not gh:
            return jsonify({
                "ok": False,
                "error": "'gh' CLI not found. Install GitHub CLI and run 'gh auth login', then restart the server.",
            }), 400
        try:
            proc = subprocess.run(
                [gh, "workflow", "run", "scan-jobs.yml", "--ref", "main"],
                cwd=str(config["_root"]),
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PATH": _augmented_path()},
            )
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip() or f"gh exited {proc.returncode}"
            return jsonify({"ok": False, "error": msg}), 500

        dispatch_ts = time.time()
        workflow_state.update({
            "running": True, "run_id": None, "status": "queued",
            "conclusion": "", "url": "", "error": None, "mode": "github",
            "started_at": dispatch_ts, "finished_at": None,
        })
        threading.Thread(
            target=_track_workflow_run,
            args=(config["_root"], dispatch_ts),
            daemon=True,
        ).start()
        return jsonify({"ok": True, "running": True})

    @app.route("/api/scan/workflow/status")
    def api_scan_workflow_status():
        return jsonify({**workflow_state, "sources": _scan_sources_view(config)})

    @app.route("/api/scan/sources")
    def api_scan_sources():
        """Sources Scan Now aggregates from (with live progress mid-scan)."""
        return jsonify({"sources": _scan_sources_view(config),
                        "mode": _scan_mode(_which_cli("gh"))})

    @app.route("/api/scan/status")
    def api_scan_status():
        return jsonify({
            "running": scan_state["running"],
            "error": scan_state.get("error"),
            "total": scan_state.get("total", 0),
            "relevant": scan_state.get("relevant", 0),
            "skipped": scan_state.get("skipped", 0),
            "sources": scan_state.get("sources", []),
        })

    # ── AI Scoring Runner (local CLI, no API key) ────────────────

    @app.route("/api/aiscore/trigger", methods=["POST"])
    def api_aiscore_trigger():
        if ai_state["running"]:
            return jsonify({"error": "Scoring already running"}), 409

        data = request.form or request.get_json(silent=True) or {}
        try:
            hours = float(data.get("hours", 0) or 0)
            limit = int(data.get("limit", 0) or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "hours and limit must be numbers"}), 400
        if hours < 0 or limit < 0:
            return jsonify({"error": "hours and limit must be >= 0"}), 400
        rescore = str(data.get("rescore", "")).lower() in ("true", "on", "1")

        # Which scoring path? Depends on the signed-in user's chosen provider.
        provider = "none"
        ok_db, prof = _try_db(lambda db: db.get_user_profile(g.user_id))
        if ok_db and prof:
            provider = prof.get("ai_provider", "none")

        # API-key path: score server-side with the user's stored Anthropic key.
        if provider == "anthropic":
            ai_state.update({
                "running": True, "engine": "anthropic", "hours": hours, "limit": limit,
                "rescore": rescore, "total": 0, "batch": 0, "batches": 0,
                "scored": 0, "failed": 0, "log": [], "error": None, "ok": None,
                "cancelled": False, "started_at": time.time(), "finished_at": None,
            })
            threading.Thread(
                target=_run_ai_score_api,
                args=(g.user_id, hours, limit, rescore),
                daemon=True,
            ).start()
            return jsonify(_ai_state_snapshot())

        # Local-CLI path for a friend: the server can't reach their machine.
        if provider == "local-cli":
            return jsonify({
                "error": "Your account scores with your own local CLI. "
                         "Run `jobflow score` on your machine.",
                "mode": "local-cli",
            }), 400

        # Operator / default: shell out to the signed-in CLI on this server.
        engine = (data.get("engine") or "claude").strip().lower()
        if engine not in AI_ENGINES:
            return jsonify({"error": f"Unknown engine '{engine}'"}), 400
        if not _which_cli(engine):
            return jsonify({"error": f"'{engine}' CLI not found. Configure AI scoring in Settings, "
                                     "or install the CLI and sign in, then restart the server."}), 400

        ai_state.update({
            "running": True, "engine": engine, "hours": hours, "limit": limit,
            "rescore": rescore, "total": 0, "batch": 0, "batches": 0,
            "scored": 0, "failed": 0, "log": [], "error": None, "ok": None,
            "cancelled": False, "started_at": time.time(), "finished_at": None,
        })

        thread = threading.Thread(
            target=_run_ai_score,
            args=(config["_root"], engine, hours, limit, rescore, g.user_id),
            daemon=True,
        )
        thread.start()
        return jsonify(_ai_state_snapshot())

    @app.route("/api/aiscore/status")
    def api_aiscore_status():
        return jsonify(_ai_state_snapshot())

    @app.route("/api/aiscore/cancel", methods=["POST"])
    def api_aiscore_cancel():
        if ai_state["running"]:
            ai_state["cancelled"] = True
            proc = ai_state.get("_process")
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        return jsonify(_ai_state_snapshot())

    # ── Local-CLI scoring client API (token-authed, no session) ──────────
    # The `jobflow score` client (a friend's machine, using their signed-in
    # Claude/Codex CLI) pulls unscored jobs here and posts scores back. Auth is
    # the per-user pairing token from Settings; every row is scoped to that user.

    def _score_client_user():
        """Resolve the pairing-token user for the score client, or (None, error_response)."""
        db = _get_db()
        if db is None:
            return None, None, (jsonify({"error": "scoring API unavailable (no database)"}), 503)
        data = request.get_json(silent=True) or {}
        user = db.get_user_by_pairing_token((data.get("token") or "").strip())
        if not user:
            return None, None, (jsonify({"error": "invalid or missing pairing token"}), 401)
        return db, user, None

    @app.route("/api/score/pending", methods=["POST"])
    def api_score_pending():
        db, user, err = _score_client_user()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        try:
            hours = float(data.get("hours", 0) or 0)
            limit = int(data.get("limit", 0) or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "hours and limit must be numbers"}), 400
        rescore = bool(data.get("rescore"))
        eff_limit = min(limit or 250, 500)
        rows = db.get_unscored_user_jobs(user["id"], hours=hours, limit=eff_limit, rescore=rescore)
        prof = db.get_user_profile(user["id"]) or {}
        jobs = [
            {"url": r[0], "company": r[1], "title": r[2], "location": r[3], "description": r[4]}
            for r in rows
        ]
        return jsonify({
            "profile": prof.get("ai_profile_text", ""),
            "model": prof.get("ai_model", ""),
            "jobs": jobs,
        })

    @app.route("/api/score/submit", methods=["POST"])
    def api_score_submit():
        db, user, err = _score_client_user()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        model = data.get("model") or "local-cli"
        pairs = []
        for s in (data.get("scores") or []):
            url = s.get("url")
            if url:
                pairs.append((url, {"score": s.get("score", 0), "reason": s.get("reason", "")}))
        updated = db.apply_user_ai_scores(user["id"], pairs, model) if pairs else 0
        return jsonify({"updated": updated})

    return app


def _scan_mode(gh: str | None) -> str:
    """Where "Scan Now" runs: "github" (dispatch the Actions workflow) or
    "local" (scan in this process). JOBFLOW_SCAN_MODE overrides; otherwise
    GitHub when the gh CLI is available."""
    mode = os.environ.get("JOBFLOW_SCAN_MODE", "").strip().lower()
    if mode in ("github", "local"):
        return mode
    return "github" if gh else "local"


# Platforms the in-process Scan Now covers (same set as the CI workflow).
LOCAL_SCAN_PLATFORMS = ["linkedin", "github"]


def _scan_sources_view(config):
    """Per-source state for the UI: live progress while a local scan runs or
    after it finished; otherwise the configured list. A GitHub Actions scan
    runs remotely, so its sources show as "remote" while it is in flight."""
    if scan_state.get("sources"):
        return scan_state["sources"]
    from ..scanner import scan_sources
    try:
        sources = scan_sources(config, LOCAL_SCAN_PLATFORMS)
    except Exception:
        return []
    if workflow_state["running"] and workflow_state.get("mode") == "github":
        for src in sources:
            if src["status"] == "pending":
                src["status"] = "remote"
    return sources


def _run_local_scan_tracked(config):
    """Run the CI-equivalent LinkedIn scan in-process, reporting through
    ``workflow_state`` so the Scan Now button's poller works unchanged."""
    try:
        hours = int(os.environ.get("JOBFLOW_SCAN_HOURS", "24") or 24)
    except ValueError:
        hours = 24
    try:
        _run_scan(config, LOCAL_SCAN_PLATFORMS, hours, False)
        if scan_state.get("error"):
            workflow_state["error"] = scan_state["error"]
        else:
            workflow_state["conclusion"] = "success"
    except Exception as e:
        workflow_state["error"] = str(e)
    finally:
        workflow_state["status"] = "completed"
        workflow_state["running"] = False
        workflow_state["finished_at"] = time.time()


def _run_scan(config, platforms, hours, new_only):
    """Run scan in background thread.

    Called when the user clicks "Scan Now" on the feed. Runs the full scan
    pipeline: fetch jobs → score → dedup → save → merge into the store.

    All jobs passing hard rejects (company blocklist, senior titles, non-US)
    are saved to scan_results.json and merged into the store. AI scoring is
    the real quality gate — algo score is the fallback.
    """
    from ..scanner import (scan_all_api_boards, scan_sources, load_seen_jobs,
                           save_seen_jobs, deduplicate_results)

    scan_state["running"] = True
    scan_state["error"] = None
    try:
        scan_state["sources"] = scan_sources(config, platforms)
    except Exception:
        scan_state["sources"] = []
    by_key = {s["key"]: s for s in scan_state["sources"]}

    def on_source(key, status, count=None, error=None):
        src = by_key.get(key)
        if src is not None:
            src.update(status=status, count=count, error=error)

    try:
        results = scan_all_api_boards(config, platforms, max_age_hours=hours,
                                      on_source=on_source)

        if new_only:
            seen = load_seen_jobs(config)
            results, seen = deduplicate_results(results, seen)
            save_seen_jobs(config, seen)

        # Jobs flagged by a hard-reject rule carry a reject_reason. They are
        # still saved and merged (the AI scorer is the real quality gate), but
        # counted separately so the UI can report clean matches vs. flagged.
        flagged = [(j, r) for j, r in results if r.reject_reason]

        scan_state["total"] = len(results)
        scan_state["relevant"] = len(results) - len(flagged)
        scan_state["skipped"] = len(flagged)

        # Save every scanned job (flagged ones included).
        output = []
        for i, (job, filt) in enumerate(sorted(results, key=lambda x: x[1].score, reverse=True), 1):
            output.append({
                "index": i,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "url": job.url,
                "score": filt.score,
                "score_pct": filt.score_pct,
                "level": filt.level,
                "min_exp": filt.min_exp,
                "max_exp": filt.max_exp,
                "competition": filt.competition,
                "variant": filt.resume_variant,
                "reason": filt.reason,
                "reject_reason": filt.reject_reason,
                "recommended": algo_recommended(filt.score_pct, filt.level),
                "description_preview": job.description or "",
                "date_posted": getattr(job, "date_posted", ""),
                "source": getattr(job, "source", "linkedin"),
            })

        results_path = config["output_dir"] / "scan_results.json"
        config["output_dir"].mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

        # Merge into store so new jobs appear in the feed. Prefer DB when it is
        # configured and reachable, then fall back to JSON.
        try:
            if is_db_enabled():
                try:
                    from ..db import merge_scan_results as db_merge
                    db_merge(output)
                    return
                except Exception:
                    pass
            store_path = config["_root"] / "data" / "ci" / "linkedin_jobs.json"
            with STORE_LOCK:
                store = load_store(store_path)
                store = merge_scan_results(store, output)
                store = prune_old_jobs(store)
                for key in store.get("jobs", {}):
                    store["jobs"][key] = backfill_job(store["jobs"][key])
                save_store(store_path, store)
        except Exception:
            pass

    except Exception as e:
        scan_state["error"] = str(e)
    finally:
        scan_state["running"] = False
