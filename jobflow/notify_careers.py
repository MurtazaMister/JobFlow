"""notify.careers scanner (paid subscription aggregator).

The postings page is a Next.js app that signs in with NextAuth credentials and
then queries a Typesense cluster (search.notify.careers) straight from the
browser, using a scoped search key shipped in the app's JS bundle. The scanner
does the same as the logged-in browser:

    1. Sign in with NOTIFY_CAREERS_EMAIL / NOTIFY_CAREERS_PASSWORD (.env).
    2. Load /postings and read the current Typesense host + key from its JS
       chunks (the key can rotate with deploys, so it is never hard-coded).
    3. Page through the `postings` collection with the subscription filters
       below, newest first, back to the lookback window.

The index has no JD text for this key, so descriptions are filled from the
apply links by jd_fetch, like the GitHub lists.
"""

import os
import re
from datetime import datetime, timezone

import requests

from .models import JobPosting

BASE = "https://app.notify.careers"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# The filters set up on the notify.careers account: AI & ML or SWE, entry
# level / new grad or junior, United States, sponsors work visas.
FILTER_BY = (
    "fields:=[`AI & Machine Learning`,`Software Engineering`]"
    " && experience_levels:=[`Entry Level/New Grad`,`Junior`]"
    " && countries:=[`United States`]"
    " && offers_sponsorship:=true"
)
LOOKBACK_HOURS = 48
PER_PAGE = 250
MAX_PAGES = 8

_KEY_RE = re.compile(
    r'apiKey:"([^"]{20,})",nodes:\[\{host:"([^"]+)",protocol:"(\w+)",port:(\d+)')


class NotifyCareersError(RuntimeError):
    pass


def credentials() -> tuple[str, str] | None:
    email = os.environ.get("NOTIFY_CAREERS_EMAIL", "").strip()
    password = os.environ.get("NOTIFY_CAREERS_PASSWORD", "")
    return (email, password) if email and password else None


def _login(s: requests.Session, email: str, password: str) -> None:
    csrf = s.get(f"{BASE}/api/auth/csrf", timeout=20).json()["csrfToken"]
    s.post(f"{BASE}/api/auth/callback/credentials", timeout=20, data={
        "email": email.lower(), "password": password, "csrfToken": csrf,
        "callbackUrl": f"{BASE}/postings", "redirect": "false", "json": "true",
    })
    session = s.get(f"{BASE}/api/auth/session", timeout=20).json() or {}
    if not session.get("user"):
        raise NotifyCareersError("sign-in failed (check NOTIFY_CAREERS_EMAIL / _PASSWORD)")


def _search_endpoint(s: requests.Session) -> tuple[str, str]:
    """(multi_search URL, api key) read from the signed-in postings page bundle."""
    html = s.get(f"{BASE}/postings", timeout=20).text
    chunks = sorted(set(re.findall(r'/_next/static/chunks/[^"\'\s]+?\.js', html)),
                    key=lambda c: "layout" not in c)   # the key lives in the root layout
    for chunk in chunks:
        m = _KEY_RE.search(s.get(BASE + chunk, timeout=20).text)
        if m:
            key, host, proto, port = m.groups()
            return f"{proto}://{host}:{port}/multi_search", key
    raise NotifyCareersError("search key not found in the postings page bundle")


def parse_hit(doc: dict) -> JobPosting | None:
    url = (doc.get("url") or "").strip()
    title = (doc.get("title") or "").strip()
    company = (doc.get("company_name") or "").strip()
    if not (url and title and company):
        return None
    locs = doc.get("locations") or []
    # Prefer the most specific location ("City, State, Country") for display.
    location = max(locs, key=lambda l: l.count(","), default="") or ", ".join(doc.get("countries") or [])
    posted = doc.get("posted")
    date_posted = (datetime.fromtimestamp(posted, tz=timezone.utc).isoformat()
                   if isinstance(posted, (int, float)) else "")
    salary = ""
    if doc.get("salary_min") and doc.get("salary_period") == "YEAR":
        lo, hi = int(doc["salary_min"]), int(doc.get("salary_max") or doc["salary_min"])
        salary = f" Salary: ${lo:,}" + (f"–${hi:,}" if hi != lo else "") + "/yr."
    levels = ", ".join(doc.get("experience_levels") or [])
    return JobPosting(
        url=url,
        title=title,
        company=company,
        location=location,
        description=(f"{title} at {company}. Location: {location}. Levels: {levels}."
                     f" Offers visa sponsorship.{salary}"),
        date_posted=date_posted,
        source="notify",
    )


def scan_notify_careers(lookback_hours: int = LOOKBACK_HOURS) -> list[JobPosting]:
    """Jobs matching the subscription filters posted in the last ``lookback_hours``."""
    creds = credentials()
    if not creds:
        raise NotifyCareersError("NOTIFY_CAREERS_EMAIL / NOTIFY_CAREERS_PASSWORD not set")
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    _login(s, *creds)
    url, key = _search_endpoint(s)

    since = int(datetime.now(timezone.utc).timestamp()) - lookback_hours * 3600
    jobs: list[JobPosting] = []
    for page in range(1, MAX_PAGES + 1):
        body = {"searches": [{
            "collection": "postings", "q": "*", "query_by": "title",
            "filter_by": f"{FILTER_BY} && posted:>{since}",
            "sort_by": "posted:desc", "per_page": PER_PAGE, "page": page,
        }]}
        resp = s.post(url, params={"x-typesense-api-key": key}, json=body,
                      headers={"Origin": BASE, "Referer": f"{BASE}/"}, timeout=30)
        if resp.status_code != 200:
            raise NotifyCareersError(f"search HTTP {resp.status_code}")
        result = (resp.json().get("results") or [{}])[0]
        if result.get("error"):
            raise NotifyCareersError(f"search error: {result['error']}")
        hits = result.get("hits") or []
        jobs.extend(j for j in (parse_hit(h.get("document") or {}) for h in hits) if j)
        if len(hits) < PER_PAGE:
            break
    return jobs
