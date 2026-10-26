"""Detect jobs you've already applied to from resume-tailoring output folders.

Each application gets its own folder, named ``YYYY-MM-DD_<company>_<title>``
(e.g. ``2026-10-23_cursor_software-engineer-new-grad-2027``), usually with a
``jd.txt`` whose header has a ``Source: <url>`` line. A feed job counts as
applied when its URL matches a folder's source URL, or its company + title
slugs match the folder name.

Folders are configured with ``applied_dirs`` in config.yaml (paths relative to
the project root) or ``JOBFLOW_APPLIED_DIRS`` (os.pathsep-separated). The index
is cached and rebuilt only when a directory's mtime changes, so a new folder
shows up on the next feed refresh.
"""

import os
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path

from .linkedin_store import normalize_url

_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_([^_]+)_(.+)$")
_SOURCE_RE = re.compile(r"^\s*Source:\s*(https?://\S+)", re.IGNORECASE | re.MULTILINE)
# Title slugs this similar (after dropping punctuation) count as the same role,
# covering small wording drift like "SDE I" vs "SDE 1 (2026)" in folder names.
_TITLE_SIMILARITY = 0.9

_lock = threading.Lock()
_cache: dict = {"key": None, "index": None}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def applied_dirs(config: dict) -> list[Path]:
    """Configured applied-folder roots that exist on this machine."""
    raw = os.environ.get("JOBFLOW_APPLIED_DIRS", "")
    entries = [e for e in raw.split(os.pathsep) if e.strip()] if raw else list(config.get("applied_dirs") or [])
    root = Path(config.get("_root", "."))
    dirs = []
    for e in entries:
        p = Path(e).expanduser()
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
        if p.is_dir():
            dirs.append(p)
    return dirs


def _build_index(dirs: list[Path]) -> dict:
    urls: dict[str, str] = {}
    slugs: list[tuple[str, str, str]] = []   # (company_slug, title_slug, folder)
    for d in dirs:
        for folder in d.iterdir():
            if not folder.is_dir():
                continue
            m = _FOLDER_RE.match(folder.name)
            if m:
                slugs.append((m.group(1), m.group(2), folder.name))
            jd = folder / "jd.txt"
            if jd.is_file():
                try:
                    head = jd.read_text(encoding="utf-8", errors="replace")[:2000]
                except OSError:
                    head = ""
                for url in _SOURCE_RE.findall(head):
                    urls[normalize_url(url)] = folder.name
    return {"urls": urls, "slugs": slugs}


def load_index(config: dict) -> dict:
    """The applied index for the configured dirs, rebuilt when any dir changes."""
    dirs = applied_dirs(config)
    key = tuple((str(d), d.stat().st_mtime_ns) for d in dirs)
    with _lock:
        if _cache["key"] != key:
            _cache["index"] = _build_index(dirs)
            _cache["key"] = key
        return _cache["index"]


def match(job: dict, index: dict) -> str | None:
    """The applied folder name matching ``job``, or None."""
    if not index:
        return None
    url = normalize_url(job.get("url") or "")
    if url and url in index["urls"]:
        return index["urls"][url]
    co = slugify(job.get("company", ""))
    title = slugify(job.get("title", ""))
    if not co or not title:
        return None
    co_flat = co.replace("-", "")
    for f_co, f_title, folder in index["slugs"]:
        f_co_flat = f_co.replace("-", "")
        if not (co_flat == f_co_flat or co_flat.startswith(f_co_flat) or f_co_flat.startswith(co_flat)):
            continue
        if title == f_title or SequenceMatcher(None, title, f_title).ratio() >= _TITLE_SIMILARITY:
            return folder
    return None


def annotate(jobs: list[dict], config: dict) -> list[dict]:
    """Set ``_applied`` (folder name or None) on each job dict in place."""
    try:
        index = load_index(config)
    except OSError:
        index = None
    for job in jobs:
        job["_applied"] = match(job, index) if index else None
    return jobs
