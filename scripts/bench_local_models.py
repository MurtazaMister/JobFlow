"""Benchmark local open-weight models (llama.cpp llama-server) against the
operator's existing Claude AI scores, without writing anything to the DB.

Usage:
    python scripts/bench_local_models.py --server C:/path/llama-server.exe \
        --model C:/models/gpt-oss-20b-Q8_0.gguf --model C:/models/gemma-4-26B-A4B-it-UD-IQ4_XS.gguf

    python scripts/bench_local_models.py ... --n 60 --batch 5 --desc-cap 4000
    python scripts/bench_local_models.py ... --server-args "--n-cpu-moe 8"   # spill experts to CPU

The sample (Claude-scored jobs, stratified by score, staffing-blocked rows
excluded since those never reach a model) is frozen to sample.json on the
first run, so every model sees exactly the same jobs. Each model gets its own
llama-server process, started and stopped by this script. Output goes to
data/eval/bench/: results.json (every score), summary.md (agreement + speed),
disagreements.md (the biggest gaps, for reading by hand).
"""

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

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
from jobflow.ai_prompt import BATCH_PROMPT, build_jobs_block, is_blocked_staffing_source, parse_scores
from jobflow.ai_llama import _TEMPLATE_KWARGS, _batch_schema

OUT_DIR = ROOT / "data" / "eval" / "bench"
PROFILE_PATH = ROOT / "config" / "profile.txt"

def parse_args():
    p = argparse.ArgumentParser(description="Compare local llama.cpp models with existing Claude AI scores.")
    p.add_argument("--server", required=True, help="Path to llama-server(.exe).")
    p.add_argument("--model", action="append", required=True, help="GGUF file; repeat for several models.")
    p.add_argument("--n", type=int, default=100, help="Sample size. Default: 100.")
    p.add_argument("--batch", type=int, default=5, help="Jobs per model call. Default: 5.")
    # 6000 = jd_fetch.MAX_DESC_LEN, i.e. exactly what the Claude scores were based on;
    # a lower cap drops requirement lines that often sit late in long JDs.
    p.add_argument("--desc-cap", type=int, default=6000, help="Max description chars sent per job. Default: 6000.")
    p.add_argument("--ctx", type=int, default=16384, help="Context window. Default: 16384.")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--server-args", default="", help="Extra llama-server args, e.g. '--n-cpu-moe 8'.")
    p.add_argument("--tag", default="", help="Label appended to the result name, e.g. 'prompt v2'.")
    p.add_argument("--resample", action="store_true", help="Draw a fresh sample instead of reusing sample.json.")
    p.add_argument("--user-id", type=int, default=DEFAULT_USER_ID)
    return p.parse_args()


# ── Sample ──────────────────────────────────────────────────────────────

def draw_sample(n: int, user_id: int) -> list[dict]:
    """Claude-scored jobs: every one with a real JD, topped up with title-only
    rows, spread across score buckets so rejects don't swamp the sample."""
    init_db()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT j.url, j.company, j.title, j.location, j.description_preview,
                          j.source, s.ai_score, s.ai_reason
                   FROM user_job_state s JOIN jobs j ON j.url = s.url
                   WHERE s.user_id = %s AND s.ai_model = 'claude' AND s.ai_score IS NOT NULL""",
                (user_id,),
            )
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    jobs = [dict(zip(("url", "company", "title", "location", "desc", "source", "claude", "claude_reason"), r))
            for r in rows]
    jobs = [j for j in jobs if not is_blocked_staffing_source(
        (j["url"], j["company"], j["title"], j["location"], j["desc"]))]

    rng = random.Random(42)
    bucket = lambda s: 0 if s == 0 else 1 if s <= 4 else 2 if s <= 6 else 3
    with_jd = [j for j in jobs if len(j["desc"] or "") >= 200]
    title_only = [j for j in jobs if len(j["desc"] or "") < 200]
    picked = []
    for pool, share in ((with_jd, 0.7), (title_only, 0.3)):
        want = round(n * share) if pool is with_jd else n - len(picked)
        by_bucket = {b: [j for j in pool if bucket(j["claude"]) == b] for b in range(4)}
        for group in by_bucket.values():
            rng.shuffle(group)
        # Round-robin over buckets, so scarce non-zero scores are all kept.
        while len([j for j in picked if j in pool]) < want and any(by_bucket.values()):
            for b in range(4):
                if by_bucket[b] and len([j for j in picked if j in pool]) < want:
                    picked.append(by_bucket[b].pop())
    rng.shuffle(picked)
    return picked


# ── Server ──────────────────────────────────────────────────────────────

def start_server(args, model_path: str, log_path: Path) -> subprocess.Popen:
    cmd = [args.server, "-m", model_path, "--port", str(args.port), "-c", str(args.ctx),
           "-ngl", "99", "--jinja", "-np", "1", "--no-webui", *args.server_args.split()]
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 900          # big GGUFs off disk can take minutes
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited early; see {log_path}")
        try:
            if requests.get(f"http://127.0.0.1:{args.port}/health", timeout=2).status_code == 200:
                return proc
        except requests.RequestException:
            pass
        time.sleep(3)
    proc.kill()
    raise RuntimeError(f"llama-server not ready after 15 min; see {log_path}")


def stop_server(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def score_batch(args, model_path: str, profile: str, batch: list[dict]):
    rows = [(j["url"], j["company"], j["title"], j["location"], (j["desc"] or "")[:args.desc_cap])
            for j in batch]
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=build_jobs_block(rows))
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 400 * len(batch) + 1024,
        "response_format": {"type": "json_schema", "json_schema": {"schema": _batch_schema(len(batch))}},
        "chat_template_kwargs": next((kw for fam, kw in _TEMPLATE_KWARGS.items()
                                      if fam in Path(model_path).name.lower()), {}),
    }
    resp = requests.post(f"http://127.0.0.1:{args.port}/v1/chat/completions", json=body, timeout=1800)
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"].get("content") or ""
    scores = parse_scores(text)
    return scores, data.get("timings", {})


# ── Metrics ─────────────────────────────────────────────────────────────

def spearman(a, b):
    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    try:
        return statistics.correlation(ra, rb)
    except statistics.StatisticsError:
        return float("nan")


def summarize(results: list[dict]) -> dict:
    got = [r for r in results if r.get("local") is not None]
    if not got:
        return {"scored": 0}
    c = [r["claude"] for r in got]
    m = [r["local"] for r in got]
    rej_c = [x == 0 for x in c]
    rej_m = [x == 0 for x in m]
    rec_c = [x >= 7 for x in c]
    rec_m = [x >= 7 for x in m]
    tp = sum(a and b for a, b in zip(rej_c, rej_m))
    return {
        "scored": len(got),
        "failed": len(results) - len(got),
        "reject_agree": sum(a == b for a, b in zip(rej_c, rej_m)) / len(got),
        "reject_recall": tp / max(1, sum(rej_c)),        # Claude's rejects the model also rejected
        "reject_precision": tp / max(1, sum(rej_m)),     # the model's rejects Claude agreed with
        "recommend_agree": sum(a == b for a, b in zip(rec_c, rec_m)) / len(got),
        "recommend_missed": sum(a and not b for a, b in zip(rec_c, rec_m)),
        "recommend_extra": sum(b and not a for a, b in zip(rec_c, rec_m)),
        "within_1": sum(abs(a - b) <= 1 for a, b in zip(c, m)) / len(got),
        "mae": statistics.mean(abs(a - b) for a, b in zip(c, m)),
        "spearman": spearman(c, m),
    }


# ── Main ────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profile = PROFILE_PATH.read_text(encoding="utf-8").strip()

    sample_path = OUT_DIR / "sample.json"
    if sample_path.exists() and not args.resample:
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
    else:
        sample = draw_sample(args.n, args.user_id)
        sample_path.write_text(json.dumps(sample, indent=1), encoding="utf-8")
    print(f"Sample: {len(sample)} jobs ({sum(len(j['desc'] or '') >= 200 for j in sample)} with a JD)")

    results_path = OUT_DIR / "results.json"
    all_results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {}

    for model_path in args.model:
        name = f"{Path(model_path).stem} (cap {args.desc_cap}{', ' + args.tag if args.tag else ''})"
        print(f"\n=== {name} ===")
        proc = start_server(args, model_path, OUT_DIR / f"{name}.server.log")
        results, timings, t0 = [], [], time.time()
        try:
            for start in range(0, len(sample), args.batch):
                batch = sample[start:start + args.batch]
                t = time.time()
                try:
                    scores, tm = score_batch(args, model_path, profile, batch)
                except requests.RequestException as e:
                    scores, tm = None, {}
                    print(f"  request error: {e}")
                ok = bool(scores) and len(scores) == len(batch)
                for i, j in enumerate(batch):
                    s = scores[i] if ok else {}
                    results.append({"url": j["url"], "title": j["title"], "company": j["company"],
                                    "has_jd": len(j["desc"] or "") >= 200, "claude": j["claude"],
                                    "claude_reason": j["claude_reason"],
                                    "local": max(0, min(10, int(s["score"]))) if ok else None,
                                    "local_reason": s.get("reason", "")})
                timings.append({"secs": time.time() - t, **tm})
                print(f"  batch {start // args.batch + 1}/{-(-len(sample) // args.batch)}: "
                      f"{time.time() - t:.0f}s, prompt {tm.get('prompt_per_second', 0):.0f} tok/s, "
                      f"gen {tm.get('predicted_per_second', 0):.1f} tok/s{'' if ok else '  FAILED'}")
        finally:
            stop_server(proc)

        total = time.time() - t0
        summary = summarize(results)
        summary.update({
            "secs_total": total,
            "secs_per_job": total / max(1, len(sample)),
            "prompt_tok_s": statistics.mean([t.get("prompt_per_second", 0) for t in timings]),
            "gen_tok_s": statistics.mean([t.get("predicted_per_second", 0) for t in timings]),
            "batch": args.batch, "desc_cap": args.desc_cap, "server_args": args.server_args,
        })
        all_results[name] = {"summary": summary, "results": results}
        results_path.write_text(json.dumps(all_results, indent=1), encoding="utf-8")
        print(json.dumps(summary, indent=1))

    write_reports(all_results)


def write_reports(all_results: dict):
    lines = [f"# Local model benchmark vs Claude ({datetime.now():%Y-%m-%d %H:%M})", "",
             "| Model | Scored | Reject agree | Reject recall | Reject precision | Rec. agree "
             "| Rec. missed | Rec. extra | ±1 | MAE | Spearman | s/job | prompt tok/s | gen tok/s |",
             "|---" * 14 + "|"]
    for name, r in all_results.items():
        s = r["summary"]
        if not s.get("scored"):
            lines.append(f"| {name} | 0 | " + " | ".join(["–"] * 12) + " |")
            continue
        lines.append(
            f"| {name} | {s['scored']}/{s['scored'] + s['failed']} | {s['reject_agree']:.0%} | "
            f"{s['reject_recall']:.0%} | {s['reject_precision']:.0%} | {s['recommend_agree']:.0%} | "
            f"{s['recommend_missed']} | {s['recommend_extra']} | {s['within_1']:.0%} | {s['mae']:.2f} | "
            f"{s['spearman']:.2f} | {s['secs_per_job']:.1f} | {s['prompt_tok_s']:.0f} | {s['gen_tok_s']:.1f} |")
    (OUT_DIR / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    dis = ["# Biggest disagreements with Claude", ""]
    for name, r in all_results.items():
        dis += [f"## {name}", ""]
        worst = sorted((x for x in r["results"] if x["local"] is not None),
                       key=lambda x: -abs(x["claude"] - x["local"]))[:12]
        for x in worst:
            dis += [f"- **{x['company']} — {x['title']}** (JD: {'yes' if x['has_jd'] else 'title only'}): "
                    f"Claude {x['claude']}, local {x['local']}",
                    f"  - Claude: {x['claude_reason']}", f"  - Local: {x['local_reason']}"]
        dis.append("")
    (OUT_DIR / "disagreements.md").write_text("\n".join(dis), encoding="utf-8")
    print(f"\nWrote {OUT_DIR / 'summary.md'} and {OUT_DIR / 'disagreements.md'}")


if __name__ == "__main__":
    main()
