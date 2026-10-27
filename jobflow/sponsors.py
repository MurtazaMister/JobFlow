"""Top H-1B sponsor list (config/h1b_sponsors.json) + company-name matching.

Powers the feed's "FAANG" tab: a virtual status, like "Recommended", that keeps
only jobs whose company is a well-paying employer with verified H-1B SWE filings.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

SPONSORS_PATH = Path(__file__).resolve().parent.parent / "config" / "h1b_sponsors.json"

# Virtual status name used by the feed (alongside "Recommended").
SPONSOR_STATUS = "FAANG"

# Legal-entity / noise words stripped from the end of a company name.
_SUFFIXES = {
    "inc", "llc", "lp", "llp", "ltd", "limited", "corp", "corporation", "co",
    "company", "plc", "group", "holdings", "pbc", "usa", "us", "na",
}
# Aliases at least this long (or multi-word) also match as a leading prefix,
# e.g. "amazon" matches "Amazon Web Services". Shorter ones must match exactly,
# so "block" doesn't catch "Block Renovation" and "meta" doesn't catch "Metaview".
_PREFIX_MIN_LEN = 6


def normalize_company(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)           # "(AWS)", "(d/b/a Airtable)"
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9.]+", " ", s)
    s = re.sub(r"(?<![a-z])\.|\.(?![a-z])", " ", s)  # keep dots only inside words ("x.ai")
    words = s.split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, dict], tuple[tuple[str, dict], ...]]:
    try:
        data = json.loads(SPONSORS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, ()
    exact: dict[str, dict] = {}
    prefixes: list[tuple[str, dict]] = []
    for c in data.get("companies", []):
        # Look-alike companies that must never match (e.g. "Bloomberg Industry Group").
        for name in c.get("exclude", []):
            exact[normalize_company(name)] = None
        for alias in [c["name"], *c.get("aliases", [])]:
            a = normalize_company(alias)
            if not a:
                continue
            exact.setdefault(a, c)  # never overrides an exclusion
            if not c.get("exact") and (len(a) >= _PREFIX_MIN_LEN or " " in a):
                prefixes.append((a + " ", c))
    # Longest prefix first so "citadel securities" wins over "citadel".
    prefixes.sort(key=lambda p: len(p[0]), reverse=True)
    return exact, tuple(prefixes)


@lru_cache(maxsize=8192)
def match_sponsor(company: str) -> dict | None:
    """Return the sponsor entry for a posting's company name, or None."""
    norm = normalize_company(company)
    if not norm:
        return None
    exact, prefixes = _load()
    if norm in exact:
        return exact[norm]
    for prefix, entry in prefixes:
        if norm.startswith(prefix):
            return entry
    return None


def is_top_sponsor(company: str) -> bool:
    return match_sponsor(company) is not None


def sponsor_companies(companies) -> list[str]:
    """Filter raw company names down to the ones on the sponsor list."""
    return [c for c in companies if c and is_top_sponsor(c)]
