"""jobright.ai scanners: the public entry-level SWE sheet and a signed-in
account's recommendation feed.

Sheet (no auth, so it also runs in CI):
    jobright.ai/entry-level-jobs?k=swe embeds an Airtable shared view, updated
    hourly with ~a week of entry-level SWE postings. The embed page carries a
    signed ``readSharedViewData`` URL; fetching it returns every row as JSON.
    Rows carry only a date (no time), so "past 24 hours" keeps today + yesterday.

Recommendations (local-only, like notify.careers):
    jobright.ai/jobs/recommend lists jobs matching the filters saved on the
    account (job functions, seniority, date posted, H-1B, ...). The filters live
    server-side, so changing them on jobright changes what this pulls. The API
    signs in with an httpOnly session cookie, so the scanner takes the browser's
    whole Cookie header from JOBRIGHT_COOKIE (.env).

Both use the jobright.ai/jobs/info/<id> page as the job URL, so a posting seen
in both feeds (or the Jobright GitHub README) collapses onto one row. Those
pages sit behind a bot check that trips after a handful of fetches, so they are
never scraped: recommendations carry the employer's apply link (``apply_url``),
which jd_fetch reads instead, and sheet rows keep the sheet's own fields.
"""

import codecs
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from .models import JobPosting

BASE = "https://jobright.ai"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

SHEET_PAGE_URL = f"{BASE}/entry-level-jobs?k=swe"
# The Airtable view embedded on SHEET_PAGE_URL (Software Engineering tab).
SHEET_EMBED_URL = "https://airtable.com/embed/appjDG7vmPOm1pO7S/shr763VHjlzPBDCgN"
RECOMMEND_PAGE_URL = f"{BASE}/jobs/recommend"

LOOKBACK_HOURS = 24
RECOMMEND_PAGE_SIZE = 20
RECOMMEND_MAX_PAGES = 25
# The sheet's dates are in jobright's (US Pacific) day.
_SHEET_TZ = ZoneInfo("America/Los_Angeles")

_EMBED_URL_RE = re.compile(r'urlWithParams:\s*"([^"]+readSharedViewData[^"]*)"')
_APP_ID_RE = re.compile(r'"x-airtable-application-id":\s*"([^"]+)"')
_JOB_ID_RE = re.compile(r"/jobs/info/([0-9a-f]{16,})")


class JobrightError(RuntimeError):
    pass


def job_url(job_id: str) -> str:
    return f"{BASE}/jobs/info/{job_id}"


def cookie() -> str | None:
    value = os.environ.get("JOBRIGHT_COOKIE", "").strip()
    return value or None


# ── Sheet (Airtable shared view) ────────────────────────────────────────────

def _sheet_rows(s: requests.Session) -> list[dict]:
    """Every row of the shared view as {column name: display value}."""
    html = s.get(SHEET_EMBED_URL, timeout=30).text
    m = _EMBED_URL_RE.search(html)
    if not m:
        raise JobrightError("Airtable view data URL not found in the embed page")
    path = codecs.decode(m.group(1), "unicode_escape")
    app = _APP_ID_RE.search(html)
    resp = s.get("https://airtable.com" + path, timeout=60, headers={
        "x-airtable-application-id": app.group(1) if app else SHEET_EMBED_URL.split("/")[4],
        "x-requested-with": "XMLHttpRequest",
        "x-time-zone": "America/Los_Angeles",
        "x-user-locale": "en",
    })
    if resp.status_code != 200:
        raise JobrightError(f"Airtable view HTTP {resp.status_code}")
    table = resp.json()["data"]["table"]
    cols = {c["id"]: c for c in table["columns"]}

    def display(col, value):
        choices = (col.get("typeOptions") or {}).get("choices") or {}
        if col["type"] == "select":
            return choices.get(value, {}).get("name", value)
        if col["type"] == "multiSelect":
            return [choices.get(v, {}).get("name", v) for v in value or []]
        return value

    return [{cols[k]["name"]: display(cols[k], v)
             for k, v in row.get("cellValuesByColumnId", {}).items() if k in cols}
            for row in table.get("rows", [])]


def parse_sheet_row(row: dict) -> JobPosting | None:
    apply = row.get("Apply") or {}
    m = _JOB_ID_RE.search(apply.get("url", "") if isinstance(apply, dict) else "")
    title = (row.get("Position Title") or "").strip()
    company = (row.get("Company") or "").strip()
    if not (m and title and company):
        return None
    location = (row.get("Location") or "").strip()
    parts = [f"{title} at {company}. Location: {location}."]
    if row.get("Work Model"):
        parts.append(f"Work model: {row['Work Model']}.")
    if row.get("Salary"):
        parts.append(f"Salary: {row['Salary']}.")
    if row.get("H1b Sponsored") == "yes":
        parts.append("H-1B sponsored.")
    if row.get("Qualifications"):
        parts.append(f"Qualifications: {row['Qualifications']}")
    return JobPosting(
        url=job_url(m.group(1)),
        title=title,
        company=company,
        location=location,
        description=" ".join(parts),
        date_posted=row.get("Date") or "",
        source="jobright",
    )


def scan_jobright_sheet(lookback_hours: int = LOOKBACK_HOURS,
                        title_filter=None) -> list[JobPosting]:
    """Sheet rows dated within ``lookback_hours`` (rounded out to whole days).

    ``title_filter`` (title -> bool) drops non-SWE rows before any JD fetch;
    the sheet also lists analyst / operator roles.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    since = (datetime.now(_SHEET_TZ) - timedelta(hours=max(lookback_hours, 1))).date().isoformat()
    jobs, seen = [], set()
    for row in _sheet_rows(s):
        if (row.get("Date") or "") < since:
            continue
        job = parse_sheet_row(row)
        if not job or job.url in seen or (title_filter and not title_filter(job.title)):
            continue
        seen.add(job.url)
        jobs.append(job)
    return jobs


# ── Recommendations (signed-in API) ─────────────────────────────────────────

def parse_recommendation(item: dict) -> JobPosting | None:
    job = item.get("jobResult") or {}
    company = ((item.get("companyResult") or {}).get("companyName") or "").strip()
    title = (job.get("jobTitle") or "").strip()
    if not (job.get("jobId") and title and company):
        return None
    location = (job.get("jobLocation") or "").strip()
    if job.get("isRemote") and "remote" not in location.lower():
        location = f"{location} (Remote)".strip()
    parts = [f"{title} at {company}. Location: {location}."]
    for label, key in (("Seniority", "jobSeniority"), ("Work model", "workModel"),
                       ("Salary", "salaryDesc")):
        if job.get(key):
            parts.append(f"{label}: {job[key]}.")
    if job.get("minYearsOfExperience") is not None:
        parts.append(f"Minimum experience: {job['minYearsOfExperience']} years.")
    if job.get("jobSummary"):
        parts.append(job["jobSummary"])
    if job.get("coreResponsibilities"):
        parts.append("Responsibilities: " + " ".join(job["coreResponsibilities"]))
    posted = job.get("publishTime") or ""
    try:
        # "YYYY-MM-DD HH:MM:SS" in UTC
        posted = datetime.fromisoformat(posted).replace(tzinfo=ZoneInfo("UTC")).isoformat()
    except ValueError:
        pass
    apply_url = job.get("originalUrl") or job.get("applyLink") or ""
    return JobPosting(
        url=job_url(job["jobId"]),
        title=title,
        company=company,
        location=location,
        description=" ".join(parts),
        date_posted=posted,
        source="jobright_rec",
        apply_url="" if "jobright.ai" in apply_url else apply_url,
    )


def scan_jobright_recommendations() -> list[JobPosting]:
    """Every job in the account's recommendation feed (its saved filters)."""
    value = cookie()
    if not value:
        raise JobrightError("JOBRIGHT_COOKIE not set")
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Cookie": value, "Accept": "application/json",
                      "Referer": RECOMMEND_PAGE_URL})
    jobs, seen, position = [], set(), 0
    for page in range(RECOMMEND_MAX_PAGES):
        resp = s.get(f"{BASE}/swan/recommend/list/jobs", timeout=30, params={
            "refresh": str(page == 0).lower(), "sortCondition": 1, "position": position,
            "count": RECOMMEND_PAGE_SIZE, "syncRerank": "false",
        })
        if resp.status_code == 401:
            raise JobrightError("signed out: refresh JOBRIGHT_COOKIE from a signed-in browser")
        if resp.status_code != 200:
            raise JobrightError(f"recommend HTTP {resp.status_code}")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise JobrightError("recommend returned a non-JSON page (security check?)") from None
        if not data.get("success"):
            raise JobrightError(f"recommend error: {data.get('errorMsg') or data.get('errorCode')}")
        items = (data.get("result") or {}).get("jobList") or []
        for item in items:
            job = parse_recommendation(item)
            if job and job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
        if len(items) < RECOMMEND_PAGE_SIZE:
            break
        position += len(items)
    return jobs
