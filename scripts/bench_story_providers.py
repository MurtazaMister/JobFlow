"""Benchmark the anonymous Instagram story providers in jobflow.instagram_stories.

Times each provider's full story-list fetch (interleaved runs, so they see the
same moment) and checks they agree on the story ids. Use it to re-order
PROVIDERS when a site slows down or dies.

    python scripts/bench_story_providers.py [username] [--runs 3]
"""

import argparse
import statistics
import time

from jobflow.instagram_stories import PROVIDERS, fetch_stories


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("username", nargs="?", default="zero2sudo")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    times: dict[str, list[float]] = {name: [] for name in PROVIDERS}
    ids: dict[str, set[str]] = {}
    errors: dict[str, str] = {}
    for _ in range(args.runs):
        for name in PROVIDERS:
            t = time.perf_counter()
            try:
                stories, _ = fetch_stories(args.username, [name])
            except Exception as e:
                errors[name] = str(e)[:120]
                continue
            times[name].append(time.perf_counter() - t)
            ids[name] = {s.id for s in stories}

    union = set().union(*ids.values()) if ids else set()
    print(f"{'provider':28} {'median':>8}  {'runs':24} stories  missing")
    for name in sorted(PROVIDERS, key=lambda n: statistics.median(times[n]) if times[n] else 1e9):
        if not times[name]:
            print(f"{name:28} {'FAIL':>8}  {errors.get(name, '')}")
            continue
        runs = " / ".join(f"{t:.2f}" for t in times[name])
        print(f"{name:28} {statistics.median(times[name]):7.2f}s  {runs:24} {len(ids[name]):7}  "
              f"{len(union - ids[name])}")


if __name__ == "__main__":
    main()
