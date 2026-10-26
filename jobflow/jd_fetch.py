"""Fetch full job descriptions from apply links (GitHub new-grad lists, notify.careers).

The README lists only give company / title / location plus an apply link, which
starves the scorer. This follows each link and pulls the JD:

    1. ATS APIs where the URL identifies the job: Greenhouse, Lever, Ashby.
    2. schema.org JobPosting JSON-LD embedded in the page — covers Jobright,
       Workday, iCIMS and most career sites.
    3. The page's meta description, when it's long enough to be the JD.

Anything shorter than MIN_DESC_LEN is treated as a miss and the job keeps its
title-only description (it's re-fetched on the next scan).
"""

import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from urllib.parse import parse_qs, urlparse

_SSL_CTX = ssl.create_default_context()
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# Separate Accept headers: Workday (and others) answer a JSON-accepting request
# with a tiny JSON stub instead of the page.
_HTML_HEADERS = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
                 "Accept-Language": "en-US,en;q=0.9"}
_JSON_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}
MIN_DESC_LEN = 200
MAX_DESC_LEN = 6000
TIMEOUT_S = 15


def _get(url: str, headers: dict = _HTML_HEADERS) -> str | None:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_SSL_CTX) as resp:
            return resp.read(3_000_000).decode("utf-8", errors="replace")
    except Exception:
        return None


def _get_json(url: str):
    body = _get(url, _JSON_HEADERS)
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def html_to_text(html: str) -> str:
    """Readable plain text from JD HTML: block tags become line breaks."""
    text = unescape(html or "")          # some APIs double-encode the HTML
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h\d|/tr)\s*/?>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


# ── ATS APIs ─────────────────────────────────────────────────────

def _greenhouse(u) -> str | None:
    m = re.match(r"/([^/]+)/jobs/(\d+)", u.path)
    if not (m and "greenhouse.io" in u.netloc):
        return None
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}")
    return html_to_text(data.get("content", "")) if isinstance(data, dict) else None


def _lever(u) -> str | None:
    m = re.match(r"/([^/]+)/([0-9a-f-]{36})", u.path)
    if not (m and "lever.co" in u.netloc):
        return None
    data = _get_json(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
    if not isinstance(data, dict):
        return None
    parts = [data.get("descriptionPlain") or html_to_text(data.get("description", ""))]
    for lst in data.get("lists") or []:
        parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}")
    parts.append(data.get("additionalPlain") or "")
    return "\n\n".join(p for p in parts if p).strip()


def _ashby(u) -> str | None:
    m = re.match(r"/([^/]+)/([0-9a-f-]{36})", u.path)
    if not (m and "ashbyhq.com" in u.netloc):
        return None
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}")
    for job in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
        if job.get("id") == m.group(2):
            return job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml", ""))
    return None


def _smartrecruiters(u) -> str | None:
    m = re.match(r"/([^/]+)/(\d+)", u.path)
    if not (m and "smartrecruiters.com" in u.netloc):
        return None
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}")
    if not isinstance(data, dict):
        return None
    sections = (data.get("jobAd") or {}).get("sections") or {}
    parts = [v.get("title", "") + "\n" + html_to_text(v.get("text", ""))
             for v in sections.values() if isinstance(v, dict) and v.get("text")]
    return "\n\n".join(parts).strip() or None


def _greenhouse_embed(u) -> str | None:
    """Custom career domains that embed Greenhouse pass the job id as gh_jid;
    the board token is usually the first host label (careers.roblox.com → roblox)."""
    jid = (parse_qs(u.query).get("gh_jid") or [None])[0]
    if not jid:
        return None
    labels = [l for l in u.netloc.split(".") if l not in ("www", "careers", "jobs", "com", "io", "co")]
    for board in labels[:2]:
        data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}")
        if isinstance(data, dict) and data.get("content"):
            return html_to_text(data["content"])
    return None


# ── Page parsing ─────────────────────────────────────────────────

def _iter_ld(obj):
    if isinstance(obj, list):
        for o in obj:
            yield from _iter_ld(o)
    elif isinstance(obj, dict):
        yield obj
        for key in ("@graph", "itemListElement"):
            if key in obj:
                yield from _iter_ld(obj[key])


def jd_from_html(html: str) -> str | None:
    """JD text from a job page: JSON-LD JobPosting, else a long meta description."""
    for block in re.findall(r'(?is)<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for obj in _iter_ld(data):
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" in types and obj.get("description"):
                parts = [html_to_text(obj["description"])]
                for extra in ("qualifications", "responsibilities", "experienceRequirements"):
                    val = obj.get(extra)
                    if isinstance(val, str) and val.strip():
                        parts.append(html_to_text(val))
                return "\n\n".join(parts)
    m = re.search(r'(?is)<meta[^>]+(?:property|name)=["\'](?:og:)?description["\'][^>]+content=["\'](.*?)["\']', html)
    return html_to_text(m.group(1)) if m else None


# Headings that start the JD body on career pages without structured data.
_JD_START = re.compile(
    r"(?im)^\s*(job description|description|about the (role|job|position|opportunity)|"
    r"what you('|’)ll do|responsibilities|key job responsibilities|the role|overview)\s*:?\s*$")


def jd_from_page_text(html: str) -> str | None:
    """Fallback for plain career pages: page text from the first JD-style heading."""
    body = re.sub(r"(?is)<(nav|header|footer|form)[^>]*>.*?</\1>", " ", html)
    text = html_to_text(body)
    m = _JD_START.search(text)
    return text[m.start():].strip() if m else None


def fetch_jd(url: str) -> str | None:
    """Full JD text for an apply URL, or None when it can't be found."""
    if not url:
        return None
    u = urlparse(url)
    desc = None
    for handler in (_greenhouse, _lever, _ashby, _smartrecruiters, _greenhouse_embed):
        try:
            desc = handler(u)
        except Exception:
            desc = None
        if desc:
            break
    if not desc or len(desc) < MIN_DESC_LEN:
        html = _get(url)
        if html:
            found = jd_from_html(html)
            if not found or len(found) < MIN_DESC_LEN:
                found = jd_from_page_text(html) or found
            desc = found or desc
    if desc and len(desc) >= MIN_DESC_LEN:
        return desc[:MAX_DESC_LEN]
    return None


def enrich_descriptions(jobs, max_fetches: int = 400, time_budget_s: float = 150,
                        workers: int = 8) -> tuple[int, int]:
    """Fill ``job.description`` from each job's apply link, in parallel.

    Bounded by ``max_fetches`` and a wall-clock budget so a cold start can't
    blow the CI timeout; unfetched jobs keep their title-only description and
    are retried next scan. Returns (fetched, attempted).
    """
    todo = [j for j in jobs if j.url][:max_fetches]
    if not todo:
        return 0, 0
    deadline = time.monotonic() + time_budget_s
    fetched = attempted = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(fetch_jd, j.url): j for j in todo}
        for fut in as_completed(futures):
            attempted += 1
            try:
                desc = fut.result()
            except Exception:
                desc = None
            if desc:
                futures[fut].description = desc
                fetched += 1
            if time.monotonic() > deadline:
                break
    finally:
        # Don't wait on stragglers past the budget; queued fetches are dropped.
        pool.shutdown(wait=False, cancel_futures=True)
    return fetched, attempted
