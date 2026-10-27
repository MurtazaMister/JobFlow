"""Detect jobs you've already applied to from resume-tailoring output folders.

Each application gets its own folder, named ``YYYY-MM-DD_<company>_<title>``
(e.g. ``2026-10-23_cursor_software-engineer-new-grad-2027``), optionally ending
in the posting's ID (``…_university-grad-product-engineer-4380944856``). Its
``jd.txt`` names the posting URL, either as a ``Source: <url>`` line or as a
bare URL on its own line near the top.

Matching is tuned for precision — a missed strike-through is fine, striking a
job you never applied to is not:

1. The job's URL matches the folder's posting URL (canonicalized, so tracking
   params, ``/application`` suffixes and LinkedIn/Greenhouse/Ashby/Lever URL
   variants of the same posting collapse together).
2. The folder's trailing posting ID appears as a whole token in the job's URL,
   and the company matches.
3. Only for folders with neither a URL nor an ID: company and title slugs are
   identical.

Folders are configured with ``applied_dirs`` in config.yaml (paths relative to
the project root) or ``JOBFLOW_APPLIED_DIRS`` (os.pathsep-separated). The index
is cached and rebuilt only when a directory's mtime changes, so a new folder
shows up on the next feed refresh.
"""

import os
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .linkedin_store import normalize_url

_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_([^_]+)_(.+)$")
_SOURCE_RE = re.compile(r"^\s*Source:\s*(https?://\S+)", re.IGNORECASE | re.MULTILINE)
_BARE_URL_RE = re.compile(r"^\s*(https?://\S+)\s*$")
# A bare URL only counts as the posting URL in the header, not the JD body
# (which links to company sites, benefits pages, etc.).
_BARE_URL_LINES = 5
# Trailing posting ID in a folder name: a LinkedIn/Greenhouse number, the first
# block of an Ashby/Lever UUID, or a Workday-style req ID (R0138295, JR1045247).
_ID_RE = re.compile(r"^(?:\d{6,}|(?=[a-f]*\d)[0-9a-f]{8}|[a-z]{1,4}\d{5,})$")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_LINKEDIN_ID_RE = re.compile(r"/jobs/view/(?:[^/]*-)?(\d{6,})$")
_GREENHOUSE_ID_RE = re.compile(r"/jobs/(\d{5,})$")

_lock = threading.Lock()
_cache: dict = {"key": None, "index": None}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _flat(slug: str) -> str:
    return slug.replace("-", "")


def url_keys(url: str) -> set[str]:
    """Canonical keys for a posting URL; two URLs share a key iff they name the
    same posting."""
    if not url or not url.strip().lower().startswith(("http://", "https://")):
        return set()
    norm = normalize_url(url)
    p = urlparse(norm)
    if p.path in ("", "/"):
        return set()   # a bare domain is a company site, not a posting
    path = re.sub(r"/(application|apply)$", "", p.path.lower())
    keys = {f"{p.netloc.removeprefix('www.')}{path}"}
    if "linkedin.com" in p.netloc and (m := _LINKEDIN_ID_RE.search(path)):
        keys.add(f"linkedin:{m.group(1)}")
    if "greenhouse.io" in p.netloc and (m := _GREENHOUSE_ID_RE.search(path)):
        keys.add(f"greenhouse:{m.group(1)}")
    gh_jid = parse_qs(urlparse(url.strip()).query).get("gh_jid")
    if gh_jid and gh_jid[0].isdigit():
        keys.add(f"greenhouse:{gh_jid[0]}")
    if ("ashbyhq.com" in p.netloc or "lever.co" in p.netloc) and (m := _UUID_RE.search(path)):
        keys.add(f"uuid:{m.group(0)}")
    return keys


def _posting_url(jd_text: str) -> str | None:
    if m := _SOURCE_RE.search(jd_text):
        return m.group(1)
    for line in jd_text.splitlines()[:_BARE_URL_LINES]:
        if m := _BARE_URL_RE.match(line):
            return m.group(1)
    return None


def _split_id(title_slug: str) -> tuple[str, str | None]:
    """``(title, posting_id)`` from a folder's title slug."""
    head, _, last = title_slug.rpartition("-")
    if head and _ID_RE.match(last):
        return head, last
    return title_slug, None


def _url_tokens(url: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", (url or "").lower())) - {""}


def _build_index(dirs: list[Path]) -> dict:
    urls: dict[str, str] = {}
    ids: list[tuple[str, str, str]] = []     # (company_flat, posting_id, folder)
    slugs: dict[tuple[str, str], str] = {}   # (company_flat, title_slug) -> folder
    for d in dirs:
        for folder in d.iterdir():
            if not folder.is_dir():
                continue
            keys: set[str] = set()
            jd = folder / "jd.txt"
            if jd.is_file():
                try:
                    head = jd.read_text(encoding="utf-8", errors="replace")[:2000]
                except OSError:
                    head = ""
                keys = url_keys(_posting_url(head) or "")
                for k in keys:
                    urls[k] = folder.name
            m = _FOLDER_RE.match(folder.name)
            if not m:
                continue
            company, (title, posting_id) = _flat(m.group(1)), _split_id(m.group(2))
            if posting_id:
                ids.append((company, posting_id, folder.name))
            elif not keys:
                slugs[(company, title)] = folder.name
    return {"urls": urls, "ids": ids, "slugs": slugs}


def load_index(config: dict) -> dict:
    """The applied index for the configured dirs, rebuilt when any dir changes."""
    dirs = applied_dirs(config)
    key = tuple((str(d), d.stat().st_mtime_ns) for d in dirs)
    with _lock:
        if _cache["key"] != key:
            _cache["index"] = _build_index(dirs)
            _cache["key"] = key
        return _cache["index"]


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


def match(job: dict, index: dict) -> str | None:
    """The applied folder name matching ``job``, or None."""
    if not index:
        return None
    url = job.get("url") or ""
    for k in url_keys(url):
        if k in index["urls"]:
            return index["urls"][k]
    company = _flat(slugify(job.get("company", "")))
    if not company:
        return None
    tokens = _url_tokens(url)
    for f_company, posting_id, folder in index["ids"]:
        if f_company == company and posting_id in tokens:
            return folder
    return index["slugs"].get((company, slugify(job.get("title", ""))))


def annotate(jobs: list[dict], config: dict) -> list[dict]:
    """Set ``_applied`` (folder name or None) on each job dict in place."""
    try:
        index = load_index(config)
    except OSError:
        index = None
    for job in jobs:
        job["_applied"] = match(job, index) if index else None
    return jobs
