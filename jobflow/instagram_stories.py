"""Instagram story scanner (@zero2sudo and similar job-drop accounts).

Stories carry the posting as a screenshot plus a link sticker, and a story
viewer exposes only the media, not the sticker's URL. So each story goes
through:

    1. Fetch the account's current stories from an anonymous story-viewer site
       (no Instagram login). Several providers, tried fastest-first — they come
       and go, so one failing just falls through to the next.
    2. OCR the frame (a video story's poster frame) with RapidOCR — PaddleOCR
       models on ONNX, local and free.
    3. The local Gemma model (ai_llama) reads the OCR text and decides whether
       the story is a single job posting or noise (DM / offer screenshots,
       recaps, news, Q&A boxes, app screenshots) and extracts company, title,
       location and the link sticker's domain. No Claude calls on this path.
    4. Web search (DuckDuckGo metasearch via `ddgs`) for the posting; Gemma
       picks the result that is this exact role's posting page, which is then
       checked by pulling its JD (jd_fetch).

Each story's verdict is cached by story id, so a story is OCR'd and classified
once, not on every scan. Needs the optional deps (`pip install -e .[instagram]`)
and the local model, so it only runs on the operator's machine, not in CI.
"""

import base64
import html as html_lib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

from .models import JobPosting

DEFAULT_ACCOUNTS = ["zero2sudo"]
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT_S = 30
# Cached verdicts older than a story's lifetime are pruned; a story that failed
# link resolution is retried while it is still up.
CACHE_TTL_H = 48
SEARCH_RESULTS = 8
# Link lookups for a posting before it's dropped (one per scan while it's up).
MAX_RESOLVE_ATTEMPTS = 3


class InstagramError(RuntimeError):
    pass


@dataclass
class Story:
    id: str
    kind: str             # "image" | "video"
    image_url: str        # the image, or a video's poster (first) frame
    video_url: str
    taken_at: str         # ISO, approximate (providers give "N hours ago")


# ---------------------------------------------------------------------------
# Story providers
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _ago_to_iso(text: str, now: datetime | None = None) -> str:
    """'23 hours ago' / '5 minutes ago' / '1 day ago' -> ISO timestamp."""
    now = now or datetime.now(timezone.utc)
    m = re.search(r"(\d+|an?|one)\s*(second|sec|minute|min|hour|hr|day|week)s?", text or "", re.I)
    if not m:
        return now.isoformat()
    n = 1 if m.group(1).lower() in ("a", "an", "one") else int(m.group(1))
    unit = m.group(2).lower()
    secs = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[
        "m" if unit.startswith("mi") else unit[0]]
    return (now - timedelta(seconds=n * secs)).isoformat()


def _story_id(media_url: str) -> str:
    """Stable id from a cdninstagram URL: the media pk in `ig_cache_key`, else
    the CDN file name. Provider proxies wrap the URL, so unwrap it first."""
    inner = unquote(media_url)
    m = re.search(r"[?&]media=([^&]+)", media_url)
    if m:
        inner = unquote(m.group(1))
    m = re.search(r"ig_cache_key=([^&]+)", inner)
    if m:
        key = unquote(m.group(1))
        try:
            pk = base64.b64decode(key.split(".")[0] + "==").decode()
            if pk.isdigit():
                return pk
        except (ValueError, UnicodeDecodeError):
            pass
    name = urlparse(inner).path.rsplit("/", 1)[-1]
    return re.sub(r"\.\w+$", "", name) or inner[:80]


def _insta_stories_io(username: str) -> list[Story]:
    """insta-stories.io: a profile lookup, then the story list as an HTML
    fragment of cards (<video poster=… ><source src=…> or <img>, "N hours ago")."""
    s = _session()
    base = "https://insta-stories.io"
    s.headers["Referer"] = f"{base}/en/"
    first = s.get(f"{base}/content.php", params={"url": username}, timeout=TIMEOUT_S).json()
    if first.get("status") == "error":
        raise InstagramError(first.get("msg") or "profile lookup failed")
    if first.get("source") == "AccountPrivate":
        raise InstagramError("account is private")
    data = s.get(f"{base}/content.php", params={"url": username, "method": "allstories"},
                 timeout=TIMEOUT_S).json()
    if data.get("status") == "error":
        raise InstagramError(data.get("msg") or "story list failed")
    stories = []
    for card in (data.get("html") or "").split('<div class="col-md-4 position-relative">')[1:]:
        video = re.search(r'<source src="([^"]+)"', card)
        image = re.search(r'<video[^>]*poster="([^"]+)"', card) or re.search(r'<img[^>]*src="([^"]+)"', card)
        if not image:
            continue
        ago = re.search(r"fa-clock[^>]*></i>\s*([^<]+)<", card)
        img_url = html_lib.unescape(image.group(1))
        stories.append(Story(
            id=_story_id(img_url),
            kind="video" if video else "image",
            image_url=img_url,
            video_url=html_lib.unescape(video.group(1)) if video else "",
            taken_at=_ago_to_iso(ago.group(1) if ago else ""),
        ))
    return stories


def _epoch_iso(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _storynavigation(username: str) -> list[Story]:
    """storynavigation.com (Laravel): the profile page carries a CSRF token and
    the IG user id; one JSON POST returns the stories with base64-encoded,
    direct cdninstagram URLs and absolute timestamps."""
    s = _session()
    base = "https://storynavigation.com"
    page = s.get(f"{base}/user/{username}/", timeout=TIMEOUT_S).text
    csrf = re.search(r'csrf-token" content="([^"]+)"', page)
    if not csrf:
        raise InstagramError("no CSRF token on profile page")
    headers = {"X-CSRF-TOKEN": csrf.group(1), "X-Requested-With": "XMLHttpRequest",
               "Referer": f"{base}/user/{username}/", "Origin": base, "Accept": "application/json"}
    uid = re.search(r'user-info-prop="\{&quot;id&quot;:(\d+)', page)
    uid = uid.group(1) if uid else None
    if not uid:
        info = s.post(f"{base}/get-user-profile", json={"userName": username},
                      headers=headers, timeout=TIMEOUT_S).json()
        uid = str((info.get("accountInfo") or {}).get("id") or "") or None
    if not uid:
        raise InstagramError("Instagram user id not found")
    data = s.post(f"{base}/get-user-last-stories", headers=headers, timeout=60, json={
        "userName": username, "isPrivate": False, "instagramUserId": uid}).json()
    stories = []
    for it in data.get("lastStories") or []:
        url = base64.b64decode(it.get("url") or "").decode()
        thumb = base64.b64decode(it.get("thumbnailUrl") or "").decode() or url
        video = it.get("type") == "video"
        stories.append(Story(id=str(it["id"]).split("_")[0], kind="video" if video else "image",
                             image_url=thumb, video_url=url if video else "",
                             taken_at=_epoch_iso(it["taken_at"])))
    return stories


def _insta_stories_viewer(username: str) -> list[Story]:
    """insta-stories-viewer.com: a socket.io server, spoken here as raw
    Engine.IO v4 HTTP long-polling (connect token -> handshake -> namespace
    connect -> "search" event -> wait for "searchResult"). Media URLs are
    encrypted blobs served through the site's image proxy."""
    import random
    import string
    base = "https://insta-stories-viewer.com"
    s = _session()
    s.headers.update({"Referer": f"{base}/{username}/", "Origin": base})
    text_hdr = {"Content-Type": "text/plain;charset=UTF-8"}

    def nonce():
        return "".join(random.choices(string.ascii_letters + string.digits, k=7))

    token = s.get(f"{base}/connect/", timeout=TIMEOUT_S).json()["token"]
    hs = s.get(f"{base}/socket.io/", params={"EIO": 4, "transport": "polling", "t": nonce()},
               timeout=TIMEOUT_S)
    q = {"EIO": 4, "transport": "polling", "sid": json.loads(hs.text[1:])["sid"]}
    s.post(f"{base}/socket.io/", params={**q, "t": nonce()}, data="40", headers=text_hdr, timeout=TIMEOUT_S)
    s.get(f"{base}/socket.io/", params={**q, "t": nonce()}, timeout=TIMEOUT_S)
    msg = ["search", {"username": username, "date": int(time.time() * 1000),
                      "token": token, "serverType": "stories"}]
    s.post(f"{base}/socket.io/", params={**q, "t": nonce()}, data="42" + json.dumps(msg),
           headers=text_hdr, timeout=TIMEOUT_S)
    result = None
    deadline = time.time() + 40
    while result is None and time.time() < deadline:
        r = s.get(f"{base}/socket.io/", params={**q, "t": nonce()}, timeout=45)
        for pkt in r.text.split("\x1e"):
            if pkt == "2":      # ping -> pong
                s.post(f"{base}/socket.io/", params={**q, "t": nonce()}, data="3",
                       headers=text_hdr, timeout=TIMEOUT_S)
            elif pkt.startswith("42"):
                event = json.loads(pkt[2:])
                if event[0] == "searchResult":
                    result = event[1]
    if not result:
        raise InstagramError("no searchResult before timeout")
    result = result.get("data") or result
    if result.get("status") != "success":
        raise InstagramError(f"search status {result.get('status')}")
    proxy = "https://cdn.insta-stories-viewer.com/img.php?url="
    stories = []
    for it in (result.get("user") or {}).get("reels") or []:
        img = it.get("thumbnail_src") or it.get("display_url") or ""
        stories.append(Story(
            id=str(it["id"]).split("_")[0], kind="video" if it.get("is_video") else "image",
            image_url=proxy + quote(img, safe="") if img else "",
            video_url=proxy + quote(it["video_url"], safe="") if it.get("video_url") else "",
            taken_at=_epoch_iso(it["taken_at"])))
    return stories


# Fastest-first. Plain-HTTP median for a full story list in the 2026-10-26
# benchmark: storynavigation 0.8s, insta-stories-viewer 1.9s, insta-stories.io
# 2.7-9s. Others checked (anonyig, fastdl, instastory.app, storysaver.net,
# mollygram, igexport, dumpor, imginn, picnob, anonstories, inflact) sit
# behind Turnstile / Cloudflare challenges or a paywall, so aren't usable.
PROVIDERS = {
    "storynavigation.com": _storynavigation,
    "insta-stories-viewer.com": _insta_stories_viewer,
    "insta-stories.io": _insta_stories_io,
}


def fetch_stories(username: str, providers: list[str] | None = None) -> tuple[list[Story], str]:
    """Current (last 24h) stories of ``username`` from the first provider that
    answers. Returns (stories, provider name). Raises InstagramError if all fail."""
    errors = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24, minutes=5)).isoformat()
    for name in providers or list(PROVIDERS):
        try:
            stories = PROVIDERS[name](username)
        except Exception as e:  # one dead provider shouldn't sink the source
            errors.append(f"{name}: {e}")
            continue
        # Some providers serve a few-minutes-stale cache that still holds
        # expired stories.
        stories = [st for st in stories if st.image_url and st.taken_at >= cutoff]
        if stories:
            return list({st.id: st for st in stories}.values()), name
        errors.append(f"{name}: no stories")
    raise InstagramError("; ".join(errors) or "no providers")


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

_ocr_engine = None


def ocr_image(data: bytes) -> str:
    """Text lines of a story frame, top to bottom, joined with newlines."""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    result, _ = _ocr_engine(data)
    # result rows: [box, text, confidence]; drop low-confidence noise
    return "\n".join(r[1] for r in (result or []) if r[2] >= 0.5)


# ---------------------------------------------------------------------------
# Gemma: classify + extract, and pick the posting link
# ---------------------------------------------------------------------------

CLASSIFY_PROMPT = """You are screening Instagram stories from a creator who shares new tech job postings.
Below is the OCR text of ONE story frame (OCR often drops spaces between words — read through that).

Decide what the story is:
- "job_posting": a screenshot of ONE specific job posting page / job card (a role title at a company,
  usually with location, job id, "Apply", and a link sticker showing a domain like
  JOB-BOARDS.GREENHOUSE.IO or CAREERS.HPE.COM).
- "offer_or_dm": a DM / chat screenshot, someone thanking the creator, an offer announcement, "replied to your story".
- "recap_or_list": a recap or list of many roles, an essay or announcement text.
- "news": a news article or social post screenshot.
- "other": anything else (app screenshots, Q&A box, memes, interview advice).

Only "job_posting" stories count. For a job_posting extract:
- company: the hiring company's name as normally written (e.g. "Wells Fargo", "Booz Allen Hamilton", "Man Group"),
  never the job board (Greenhouse, WayUp, LinkedIn, Workday) unless nothing else names the company.
- title: the exact role title with normal spacing restored (e.g. "Software Engineer, Intern (Summer 2027)").
- location: as shown, or "".
- link_domain: the link-sticker / browser-bar domain in lowercase (e.g. "careers.hpe.com"), or "".
For non-postings leave those fields "".

OCR text:
\"\"\"
{text}
\"\"\"
"""

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["job_posting", "offer_or_dm", "recap_or_list", "news", "other"]},
        "company": {"type": "string", "maxLength": 80},
        "title": {"type": "string", "maxLength": 160},
        "location": {"type": "string", "maxLength": 160},
        "link_domain": {"type": "string", "maxLength": 80},
    },
    "required": ["kind", "company", "title", "location", "link_domain"],
}

PICK_PROMPT = """Find the web page for this exact job posting.

Job: "{title}" at {company}{loc}{domain}

Search results:
{results}

Answer with the number of the result that is the posting page for this exact role (same title, same company):
prefer the company's own careers site or its applicant-tracking system (Greenhouse, Lever, Ashby, Workday,
iCIMS, Eightfold, Oracle, SmartRecruiters…) over LinkedIn and job aggregators; use an aggregator only when
no direct posting is listed. Search-results pages, career home pages, blogs and different roles don't count.
Answer -1 if none is this posting.
"""


def _llama_json(prompt: str, schema: dict, max_tokens: int = 400) -> dict | int | None:
    from . import ai_llama
    name = Path(ai_llama.model_path() or ai_llama.DEFAULT_MODEL_FILE).name.lower()
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
        "chat_template_kwargs": next(
            (kw for fam, kw in ai_llama._TEMPLATE_KWARGS.items() if fam in name), {}),
    }
    try:
        resp = requests.post(f"{ai_llama._base()}/v1/chat/completions", json=body,
                             timeout=ai_llama.REQUEST_TIMEOUT)
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"].get("content") or "null")
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"  local model error: {e}")
        return None


def classify_story(text: str) -> dict | None:
    """Gemma's verdict on one story's OCR text, or None on model failure."""
    if len(text.strip()) < 15:
        return {"kind": "other", "company": "", "title": "", "location": "", "link_domain": ""}
    out = _llama_json(CLASSIFY_PROMPT.format(text=text[:3000]), _CLASSIFY_SCHEMA)
    if not isinstance(out, dict):
        return None
    if out.get("kind") == "job_posting" and not (out.get("company", "").strip() and out.get("title", "").strip()):
        out["kind"] = "other"     # a posting we can't name can't be searched for
    return out


# Job boards / aggregators: acceptable, but only when no direct posting turns up.
_AGGREGATORS = ("linkedin.com", "indeed.com", "glassdoor.", "builtin.com", "startup.jobs",
                "jobera.com", "ziprecruiter", "simplyhired", "wayup.com", "handshake",
                "jobright.ai", "simplify.jobs", "levels.fyi", "1752.vc", "talent.com",
                "monster.com", "dice.com", "careerbuilder", "lensa.com", "joinhandshake")
_NOT_POSTINGS = ("reddit.com", "youtube.com", "instagram.com", "tiktok.com/@", "wikipedia.org",
                 "facebook.com", "x.com", "twitter.com", "medium.com", "quora.com")


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 1}


def _rank(results: list[dict], job: dict) -> list[dict]:
    """Drop obvious non-postings and order the rest by title overlap, with the
    link sticker's site and direct (non-aggregator) pages first."""
    want = _tokens(job["title"])
    sticker = (job.get("link_domain") or "").lower().removeprefix("www.")
    co = _tokens(job["company"])
    ranked = []
    for r in results:
        host = urlparse(r["href"]).netloc.lower().removeprefix("www.")
        if not host or any(b in host for b in _NOT_POSTINGS):
            continue
        overlap = len(want & _tokens(r.get("title", "") + " " + r["href"])) / max(len(want), 1)
        score = overlap
        if sticker and (host == sticker or host.endswith("." + sticker) or sticker.endswith(host)):
            score += 0.6
        if any(a in host for a in _AGGREGATORS):
            score -= 0.4
        if co & _tokens(host):
            score += 0.2
        ranked.append({**r, "_score": round(score, 2)})
    return sorted(ranked, key=lambda r: -r["_score"])


def search_web(query: str, max_results: int = SEARCH_RESULTS) -> list[dict]:
    """[{title, href, body}] from DuckDuckGo's metasearch (no API key)."""
    from ddgs import DDGS
    for attempt in range(2):
        try:
            return list(DDGS().text(query, max_results=max_results) or [])
        except Exception as e:
            if attempt:
                print(f"  search failed: {e}")
            time.sleep(2)
    return []


def _title_match(want: str, got: str) -> float:
    """Token Jaccard of two job titles (0..1)."""
    a, b = _tokens(want), _tokens(got)
    return len(a & b) / max(len(a | b), 1)


_ATS_SLUG_RES = [
    ("greenhouse", re.compile(r"(?:job-boards(?:\.eu)?|boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)")),
]


def _ats_postings(ats: str, slug: str) -> list[tuple[str, str]]:
    """(title, url) of every open posting on a public ATS board, [] if none."""
    try:
        if ats == "greenhouse":
            data = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                                timeout=15).json()
            return [(j["title"], j["absolute_url"]) for j in data.get("jobs", [])]
        if ats == "lever":
            data = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=15).json()
            return [(j["text"], j["hostedUrl"]) for j in data] if isinstance(data, list) else []
        if ats == "ashby":
            data = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=15).json()
            return [(j["title"], j["jobUrl"]) for j in data.get("jobs", [])]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass
    return []


def _ats_lookup(job: dict, results: list[dict]) -> str | None:
    """The posting straight from the company's Greenhouse/Lever/Ashby board.
    Board slugs come from ATS links among the search results, plus guesses
    from the company name; the title has to match closely."""
    slugs: dict[tuple[str, str], None] = {}
    for r in results:
        for ats, rx in _ATS_SLUG_RES:
            m = rx.search(r["href"])
            if m:
                slugs[(ats, m.group(1).lower())] = None
    words = re.findall(r"[a-z0-9]+", job["company"].lower())
    for guess in {"".join(words), words[0] if words else ""} - {""}:
        for ats in ("greenhouse", "lever", "ashby"):
            slugs[(ats, guess)] = None
    best, best_score = None, 0.0
    for ats, slug in list(slugs)[:12]:
        for title, url in _ats_postings(ats, slug):
            score = _title_match(job["title"], title)
            if score > best_score:
                best, best_score = url, score
    return best if best_score >= 0.75 else None


def _names_role(job: dict, result: dict) -> bool:
    """Guard against career home pages and search pages: the result's title or
    URL has to carry most of the role title's words."""
    want = _tokens(job["title"])
    got = _tokens(result.get("title", "") + " " + result["href"].replace("-", " "))
    return len(want & got) / max(len(want), 1) >= 0.5


def _clean_url(url: str) -> str:
    # Ashby apply pages: link the posting, not its application form.
    return re.sub(r"(jobs\.ashbyhq\.com/[^/]+/[0-9a-f-]{36})/application/?$", r"\1", url)


def resolve_link(job: dict) -> str | None:
    """URL of the posting for an extracted story job, or None."""
    query = f"{job['company']} {job['title']}"
    results = _rank(search_web(query), job)
    if job.get("link_domain") and not any(job["link_domain"] in r["href"] for r in results[:3]):
        # The sticker's site often isn't in a broad search; ask for it directly.
        results = _rank(search_web(f"{query} site:{job['link_domain']}", 4) + results, job)
    direct = _ats_lookup(job, results)
    if direct:
        return direct
    results = list({r["href"]: r for r in results}.values())[:6]
    if not results:
        return None
    listing = "\n".join(f"{i}. {r.get('title', '')}\n   {r['href']}\n   {(r.get('body') or '')[:200]}"
                        for i, r in enumerate(results))
    prompt = PICK_PROMPT.format(
        title=job["title"], company=job["company"],
        loc=f" ({job['location']})" if job.get("location") else "",
        domain=f"; the story linked to {job['link_domain']}" if job.get("link_domain") else "",
        results=listing)
    pick = _llama_json(prompt, {"type": "integer", "minimum": -1, "maximum": len(results) - 1}, 20)
    if isinstance(pick, int) and 0 <= pick < len(results) and _names_role(job, results[pick]):
        return _clean_url(results[pick]["href"])
    # The model passed on all of them; still take a result whose page title is
    # this exact role at this company (typically an aggregator or LinkedIn).
    company = _tokens(job["company"])
    for r in results:
        page = r.get("title", "")
        if company & _tokens(f"{page} {r['href']} {r.get('body', '')}") and _title_match(job["title"], page) >= 0.6 \
                and _tokens(job["title"]) <= _tokens(page):
            return _clean_url(r["href"])
    return None


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def default_cache_path(config: dict | None = None) -> Path:
    root = Path((config or {}).get("_root") or Path(__file__).resolve().parent.parent)
    return root / "data" / "instagram" / "stories.json"


def _load_cache(path: Path) -> dict:
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_H)).isoformat()
    return {k: v for k, v in cache.items() if v.get("seen_at", "") >= cutoff}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")


def missing_setup() -> str | None:
    """Why the story scanner can't run here, or None when it can."""
    try:
        import ddgs  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return "Install the extras: pip install -e .[instagram]"
    from .ai_llama import missing_setup as llama_missing
    return llama_missing()


def scan_instagram_stories(accounts: list[str] | None = None, cache_path: Path | None = None,
                           log=print) -> list[JobPosting]:
    """Job postings from the accounts' current stories (see module docstring)."""
    from .ai_llama import LlamaServer
    from .jd_fetch import fetch_jd

    raw_log = log

    def log(msg: str) -> None:
        # OCR text is full of odd glyphs; Windows consoles (cp1252) can't print them.
        raw_log(msg.encode("ascii", "replace").decode("ascii"))

    cache_path = cache_path or default_cache_path()
    cache = _load_cache(cache_path)
    now = datetime.now(timezone.utc).isoformat()
    todo: list[tuple[str, Story]] = []
    for account in accounts or DEFAULT_ACCOUNTS:
        stories, provider = fetch_stories(account)
        log(f"  @{account}: {len(stories)} stories via {provider}")
        for st in stories:
            entry = cache.get(st.id)
            if entry and (entry.get("kind") != "job_posting" or entry.get("url")
                          or entry.get("attempts", 0) >= MAX_RESOLVE_ATTEMPTS):
                continue        # settled: noise, resolved, or given up on
            todo.append((account, st))

    jobs: list[JobPosting] = []
    if not todo:
        log("  nothing new since the last scan")
        return jobs
    s = _session()
    with LlamaServer():
        for account, st in todo:
            entry = cache.get(st.id) or {"story": asdict(st), "account": account}
            entry["seen_at"] = now
            if "kind" not in entry:
                try:
                    img = s.get(st.image_url, timeout=TIMEOUT_S)
                    img.raise_for_status()
                    entry["ocr"] = ocr_image(img.content)
                except Exception as e:
                    log(f"  story {st.id}: image/OCR failed ({e})")
                    continue
                verdict = classify_story(entry["ocr"])
                if verdict is None:
                    continue        # model hiccup: retry next scan
                entry.update(verdict)
            cache[st.id] = entry
            _save_cache(cache_path, cache)
            if entry["kind"] != "job_posting":
                log(f"  skip [{entry['kind']}] " + " ".join(entry["ocr"].split())[:60])
                continue
            entry["url"] = resolve_link(entry) or ""
            entry["attempts"] = entry.get("attempts", 0) + 1
            if not entry["url"]:
                log(f"  ? {entry['company']} — {entry['title']}: no posting link found")
                continue
            log(f"  + {entry['company']} — {entry['title']} -> {entry['url']}")
            _save_cache(cache_path, cache)
            jobs.append(_to_posting(entry, fetch_jd(entry["url"])))
    _save_cache(cache_path, cache)
    return jobs


def _to_posting(entry: dict, jd: str | None) -> JobPosting:
    title, company = entry["title"].strip(), entry["company"].strip()
    desc = jd or (f"{title} at {company}. Location: {entry.get('location') or 'n/a'}.\n"
                  f"From an Instagram story (OCR):\n{entry.get('ocr', '')}")
    return JobPosting(
        url=entry["url"], title=title, company=company,
        location=entry.get("location", "").strip(),
        description=desc, date_posted=entry["story"]["taken_at"], source="instagram",
    )

