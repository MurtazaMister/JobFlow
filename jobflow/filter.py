"""Multi-signal job scoring engine with a hard-reject pipeline.

This is the core evaluation logic that decides whether a job is worth applying
to. Every scanned job passes through evaluate_job(), which works in two phases:

Phase 1 — Hard Reject (instant score=0, reject_reason set):
    Six ordered checks that disqualify a job immediately:
    1. Company blocklist    — spam aggregators (Dice, Turing, Jobot, etc.)
    2. Title patterns       — senior/staff/principal/lead/QA/architect/VP
    3. Sponsorship phrases  — 47 patterns catching "no sponsorship", citizenship, clearance
    4. Non-US location      — regex matching international cities/countries
    5. Overqualified exp    — min_exp >= 4 years or "5-8 years" patterns
    6. Senior salary        — $130K+ with zero entry-level signals in the JD

    Hard rejects are ordered by cheapest-to-check first. Sponsorship phrases
    use plain string matching (_has_phrase) instead of regex for speed since
    this runs on every job.

Phase 2 — Scoring (if all hard filters pass):
    Additive scoring across multiple signals, normalized to 0-100%:
    - keyword_score:    Tech stack matches (Python=10, PyTorch=8, AWS=7, etc.)
    - synergy_bonus:    Extra points for common stack combos (Python+FastAPI+AWS = +10)
    - level_points:     New Grad=+20, Entry=+15, Mid=+5, Unknown=+4
    - experience_score: Sweet spot is 0-2 years (+10), penalty for 3+ years
    - recency_score:    Freshly posted jobs get +10, >48h old gets -5
    - loc_score:        US location = +10, non-US = -10
    - h1b_bonus:        Mentions visa sponsorship = +8
    - senior_penalty:   3+ senior signals with 0 entry signals = -30

    Raw score (max ~130) is normalized: score_pct = raw / 130 * 100

Note on hard rejects: a hard-rejected job is NOT dropped. evaluate_job() still
returns a FilterResult (with score=0 and reject_reason set) so the job flows
into the store, where the AI scorer can override the rule-based verdict. Callers
distinguish rejected jobs by checking `reject_reason`, not by a boolean gate.

Variant Selection:
    select_variant() picks which base resume to use:
    - "ml"     if 2+ ML keywords (pytorch, tensorflow, llm, etc.)
    - "appdev" if 2+ frontend keywords (react, angular, full-stack, etc.)
    - "se"     default (backend/infra/general SWE)
"""

import re
from datetime import datetime, timezone
from functools import lru_cache

from .models import FilterResult, JobPosting

# ── Sponsorship / Citizenship / Clearance — hard reject (case-insensitive) ──
# These are plain strings (not regex) checked via `phrase in text` for speed.
# Covers three categories: sponsorship denial, citizenship requirements, and
# security clearance requirements. Any match = immediate disqualification
# because the user needs visa sponsorship (F1/OPT).
DISQUALIFYING_PHRASES = [
    # Sponsorship rejection
    "no sponsorship", "no visa sponsorship", "no need for visa sponsorship",
    "will not sponsor", "cannot sponsor", "does not sponsor",
    "not sponsor", "unable to sponsor", "not able to sponsor",
    "not offer sponsorship", "not offering sponsorship",
    "sponsorship not available", "sponsorship is not available",
    "sponsorship: not available", "not available for this role",
    "sponsorship not provided", "sponsorship is not provided",
    "no visa", "visa: not available",
    "without sponsorship", "without the need for sponsorship",
    "without requiring sponsorship",
    "work authorization without sponsorship",
    "work in the us without sponsorship",
    # Citizenship / permanent residency
    "us citizen only", "u.s. citizen only", "citizens only",
    "must be a us citizen", "must be a u.s. citizen",
    "must be a united states citizen",
    "us citizenship required", "u.s. citizenship required",
    "united states citizenship required", "citizenship is required",
    "gc holder", "green card holder", "green card only",
    "permanent resident only", "lawful permanent resident",
    "must be authorized to work", "must have authorization to work",
    "authorization to work in the us",
    "authorization to work in the united states",
    "authorized to work in the us without sponsorship",
    "authorized to work in the united states without sponsorship",
    "eligible to work in the us without sponsorship",
    "legally authorized to work",
    # Security clearance
    "security clearance required", "clearance required",
    "active clearance", "active secret clearance", "active top secret",
    "top secret clearance", "top-secret clearance",
    "ts/sci", "ts sci", "top secret/sci",
    "sci eligibility", "sci access",
    "dod clearance", "dod secret", "government clearance",
    "public trust clearance", "secret clearance",
    "must have clearance", "must hold clearance",
    "ability to obtain a clearance", "ability to obtain and maintain",
    "obtain a government clearance", "obtain a security clearance",
]

# ── Overqualified experience patterns — hard reject ─────────────────────────
# Regex patterns that catch "4+ years", "5-8 years", "minimum 4 years", etc.
# These fire BEFORE the numeric extract_experience() check as a safety net
# for unusual phrasing that the parser might miss.
OVERQUALIFIED_PATTERNS = [
    r"\b[4-9]\+?\s*(?:years?|yrs?)\s+(?:\w+\s+){0,5}experience",
    r"\b1[0-9]\+?\s*(?:years?|yrs?)",
    r"(?:minimum|at\s+least|requires?)\s+(?:of\s+)?[4-9]\+?\s*(?:years?|yrs?)",
    r"\b[4-9]\s*(?:or\s+more)\s*(?:years?|yrs?)",
    r"\b[4-9]\s*[-–to]+\s*[5-9]\d?\s*\+?\s*(?:years?|yrs?)",
    r"\b[3-9]\s*[-–]\s*[8-9]\+?\s*(?:years?|yrs?)",
]

# ── Title-level hard reject patterns ────────────────────────────────────────
# Matched against the job TITLE only (not description) to avoid false positives
# from phrases like "work with senior engineers" in JD body text.
TITLE_REJECT_PATTERNS = [
    r"\bsenior\b", r"\bsr\.?\s", r"\bstaff\b", r"\bprincipal\b",
    r"\blead\s+(?:engineer|developer|software)\b",
    r"\bmanager\b", r"\bdirector\b",
    r"\barchitect\b", r"\bvp\b", r"\bvice\s+president\b", r"\bhead\s+of\b",
    # QA / Testing roles
    r"\bsdet\b", r"\bqa\s+engineer\b", r"\bquality\s+assurance\b",
    r"\bquality\s+engineer\b",
    r"\btest\s+(?:automation|engineer)\b", r"\btesting\s+engineer\b",
]

# ── Company blocklist (job aggregators / spam) ──────────────────────────────
# These companies are staffing agencies or aggregators that repost jobs
# from other companies. Applying through them adds no value. Kept in sync
# with the staffing blocklist in scripts/ai_score_local.py.
COMPANY_BLOCKLIST = {
    "dice", "remotehunter", "jobs via dice", "jobot", "cybercoders",
    "lancesoft", "haystack", "turing", "micro1", "hackajob",
    "crossover", "toptal", "andela", "revelo", "tira",
    "jack & jill", "jack and jill",
    "jobright", "jobright.ai", "quik hire staffing", "beacon fire",
    "helic & co.", "helic and co",
}

# URL fragments that identify aggregator/staffing reposts even when the
# company field looks legitimate (e.g. jobright.ai deep links).
BLOCKED_URL_SIGNS = ("jobright.ai", "jobs-via-dice")

# Substring blocklist used by the AI scorers (jobflow/ai_scorer.py and
# scripts/ai_score_local.py) to short-circuit obvious aggregator/staffing
# reposts to a score of 0 without spending a model call. Unlike COMPANY_BLOCKLIST
# (exact company-name match), this is matched as a substring against a haystack
# built from a job's url/company/title/location/description.
STAFFING_SOURCE_BLOCKLIST = (
    "jobright.ai", "remotehunter", "quik hire staffing", "beacon fire",
    "helic & co.", "helic and co", "jack & jill", "jack and jill",
    "jobs via dice",
)
_STAFFING_SOURCE_BLOCKLIST_COMPACT = tuple(
    re.sub(r"[^a-z0-9]+", "", s.lower()) for s in STAFFING_SOURCE_BLOCKLIST
)
# Standard ai_reason recorded for a blocked-source job.
STAFFING_BLOCK_REASON = "Blocked staffing/spam source."

# ── Senior salary pattern (>= $130K suggests non-entry) ────────────────────
# A salary floor of $130K+ without any entry-level signals SUGGESTS (but does
# not prove) a mid/senior role. This is a weak proxy — new-grad roles at big
# tech routinely pay $130K+ — so it DEMOTES rather than hard-rejects (see below).
SENIOR_SALARY_PATTERN = r"\$1[3-9]\d[,.]?\d{3}|\$[2-9]\d\d[,.]?\d{3}"

# ── Soft demotion penalties (NOT hard rejects) ─────────────────────────────
# Salary and free-text experience numbers are unreliable seniority proxies:
# they misfire on well-paid new-grad roles ("Software Engineer III" at FAANG)
# and on JDs that mention years in passing. A wrong HARD reject deletes a good
# job before the AI rescorer ever sees it (the costliest error), so these
# signals instead subtract a large penalty — the job stays in the pool, ranks
# low, and the AI makes the final call. Only structural certainties (non-SWE
# title, non-US, explicit no-sponsorship, blocked source) remain hard rejects.
OVERQUALIFIED_PENALTY = -45   # experience requirement >= 4 yrs / overqualified phrasing
SENIOR_SALARY_PENALTY = -45   # $130K+ floor with no entry-level signals

# ── Entry-level signals ─────────────────────────────────────────────────────
# Used as a counterbalance: if a job has senior salary BUT also has these
# entry signals, it's not hard-rejected (could be a well-paying entry role).
# Also used in the soft senior_penalty calculation.
ENTRY_LEVEL_SIGNALS = [
    r"\bnew[\s-]*grad", r"\bnew[\s-]*graduate\b", r"\bcampus\s+hire\b",
    r"\bentry[\s-]*level\b",
    r"\bjunior\b", r"\bjr\.?\s", r"\bjr\s+",
    r"\b0[\s-]*[12]\s*(?:years?|yrs?)\b",
    r"\b0\s+to\s+2\s*(?:years?|yrs?)\b",
    r"\b1\+?\s*(?:years?|yrs?)\b",
    r"\bearly\s+career\b",
    r"\brecent\s+graduate?\b", r"\brecent\s+grad\b",
    r"\buniversity\s+grad",
    r"\bsde[\s-]*[i1]\b", r"\bswe[\s-]*[i1]\b", r"\beng[\s-]*[i1]\b",
    r"\blevel[\s-]*[i1]\b", r"\blevel[\s-]*1\b",
    r"\bassociate\b",
    r"\bfirst\s+opportunity\b",
    r"\bintern\b",
]

# ── Description-level senior signals (soft penalty) ─────────────────────────
# Unlike TITLE_REJECT_PATTERNS (hard reject), these are checked in the JD body
# and only apply a -30 penalty if 3+ match with zero entry signals. This
# handles JDs that mention "3+ years" once casually vs. those that repeatedly
# emphasize senior requirements.
SENIOR_DESC_SIGNALS = [
    r"\b[3-9]\+?\s*(?:years?|yrs?)\b",
    r"\b1[0-9]\+?\s*(?:years?|yrs?)\b",
    r"\bsenior\b", r"\bstaff\b", r"\bprincipal\b",
    r"\blead\b", r"\bmanager\b", r"\bdirector\b",
]

# ── ML/AI keywords for variant selection ────────────────────────────────────
ML_KEYWORDS = [
    r"\bmachine\s+learning\b", r"\bdeep\s+learning\b", r"\bml\s+engineer\b",
    r"\bdata\s+scien", r"\bcomputer\s+vision\b", r"\bnlp\b", r"\bllm\b",
    r"\bpytorch\b", r"\btensorflow\b", r"\bmodel\s+training\b",
]

# ── Full-stack / AppDev keywords ────────────────────────────────────────────
APPDEV_KEYWORDS = [
    r"\bfull[\s-]*stack\b", r"\bfrontend\b", r"\bfront[\s-]*end\b",
    r"\breact\b", r"\bangular\b", r"\bvue\b", r"\bnext\.?js\b",
    r"\bweb\s+developer\b", r"\bui/ux\b",
]

# ── Personal tech stack (categorized) ───────────────────────────────────────
# Weights reflect how central each technology is to the user's skill set.
# Higher weight = stronger match signal. Categories are used for organization
# only — scoring sums across all categories.
#
# Scoring is binary presence: if "python" appears anywhere in title+description,
# add 10 points. No frequency weighting — mentioning Python 5 times doesn't
# score higher than mentioning it once.
STACK_CATEGORIES = {
    "core": {
        "python": 10, "c++": 6, "java": 4, "sql": 5, "go": 4,
    },
    "ml_ai": {
        "machine learning": 10, "deep learning": 8, "pytorch": 8, "tensorflow": 7,
        "llm": 8, "rag": 7, "langchain": 6, "hugging face": 5,
        "computer vision": 5, "nlp": 6, "transformers": 6,
    },
    "backend": {
        "distributed systems": 8, "rest": 4, "api": 4, "fastapi": 7,
        "flask": 5, "microservices": 5, "grpc": 5,
    },
    "cloud": {
        "aws": 7, "gcp": 4, "azure": 3, "lambda": 3, "ec2": 3,
    },
    "devops": {
        "docker": 5, "kubernetes": 6, "ci/cd": 4, "terraform": 4, "linux": 4,
    },
    "data": {
        "postgresql": 5, "mongodb": 4, "redis": 5, "kafka": 5,
        "spark": 4, "airflow": 4, "elasticsearch": 4,
    },
}

# Bonus when full tech combos appear together.
# Synergy rewards jobs that match the user's actual project experience
# (e.g., "Python + FastAPI + AWS" = a real stack the user has built with).
# All keywords in the combo must be present to earn the bonus.
SYNERGY_COMBOS = [
    ({"python", "fastapi", "aws"}, 10),
    ({"python", "pytorch", "aws"}, 10),
    ({"machine learning", "python", "docker"}, 8),
    ({"llm", "python", "aws"}, 10),
    ({"python", "docker", "kubernetes"}, 8),
    ({"python", "kafka", "distributed systems"}, 8),
    ({"postgresql", "redis", "api"}, 6),
]

# ── Title role-fit bonus ────────────────────────────────────────────────────
# The job TITLE is the strongest single relevance signal we have without AI.
# A title that names the user's target role family gets a strong bonus; an
# adjacent role family gets a smaller one. Checked against title only.
TITLE_FIT_STRONG = [
    r"\bsoftware\s+(?:engineer|developer)\b", r"\bswe\b", r"\bsde\b",
    r"\bback[\s-]*end\b", r"\bbackend\b", r"\bfull[\s-]*stack\b",
    r"\bmachine\s+learning\b", r"\bml\s+engineer\b", r"\bai\s+engineer\b",
    r"\bdata\s+engineer\b", r"\bplatform\s+engineer\b",
    r"\binfrastructure\s+engineer\b",
]
TITLE_FIT_ADJACENT = [
    r"\bdevops\b", r"\bsite\s+reliability\b", r"\bsre\b",
    r"\bcloud\s+engineer\b", r"\bdata\s+scientist\b",
    r"\bsoftware\b", r"\bdeveloper\b", r"\bengineer\b",
]
TITLE_FIT_STRONG_POINTS = 12
TITLE_FIT_ADJACENT_POINTS = 6

# Cap on extra points for stack keywords that appear in the TITLE itself
# ("Python Backend Engineer" is a far stronger signal than a JD that lists
# Python among 30 technologies).
TITLE_KEYWORD_BOOST_MAX = 10

# ── Poor-fit tech penalties ─────────────────────────────────────────────────
# Stacks that are clearly outside the user's Python/ML/backend profile.
# These are soft penalties (never a hard reject — the AI scorer can still
# rescue edge cases). Total penalty is capped at POOR_FIT_MAX_PENALTY.
POOR_FIT_PATTERNS = [
    (r"\bsalesforce\b", 8), (r"\bservicenow\b", 8), (r"\babap\b", 8),
    (r"\bpeoplesoft\b", 8), (r"\bworkday\s+(?:consultant|developer)\b", 8),
    (r"\bmainframe\b", 10), (r"\bcobol\b", 10),
    (r"\bsharepoint\b", 6), (r"\bwordpress\b", 6), (r"\bdrupal\b", 6),
    (r"\bverilog\b", 8), (r"\bvhdl\b", 8), (r"\bfpga\b", 8),
    (r"\brtl\s+design\b", 8), (r"\bembedded\s+firmware\b", 6),
    (r"\bmanual\s+test", 6), (r"\bhelp\s*desk\b", 8), (r"\bservice\s+desk\b", 8),
    (r"\.net\b", 3), (r"\bc#", 3), (r"\bphp\b", 4),
]
POOR_FIT_MAX_PENALTY = 20

# Maximum theoretical raw score. Used to normalize to 0-100%.
# Approximate breakdown: stack ~50 + synergy ~10 + level 20 + exp 10 + recency 10
# + location 10 + h1b 8 + title fit 12 + title boost 10 ≈ 140
SCORE_MAX_RAW = 140

# Algo-recommended threshold: when a job has no AI score yet, the dashboard
# falls back to flagging high-scoring entry-level jobs as recommended.
RECOMMENDED_MIN_PCT = 65

# Big Tech companies get a +5 competition score — more applicants means
# the user should prioritize these applications (higher urgency, not penalty).
BIG_TECH = {
    "google", "amazon", "meta", "apple", "microsoft",
    "netflix", "uber", "airbnb", "stripe", "openai",
}

# Positive sponsorship signals — if the JD explicitly mentions visa sponsorship,
# the job gets a +8 bonus since the user needs F1/OPT sponsorship.
H1B_PREFER = [
    "h1b", "h-1b", "visa sponsorship", "will sponsor",
    "sponsorship available", "sponsorship provided", "open to sponsorship",
]

# ── US location detection ───────────────────────────────────────────────────
# Allowlist approach: a location counts as US if it names the country, a US
# state abbreviation, a known major US city, or a remote/hybrid arrangement.
# Anything else (London, Bangalore, Toronto, ...) is treated as non-US without
# having to enumerate every foreign place.
US_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
US_LOCATION_PATTERNS = [
    r"\bunited\s+states\b", r"\busa\b", r"\bus\b", r"\bu\.s\.?\b",
    r"\bremote\b", r"\bhybrid\b",
    # Major US cities
    r"\bnew\s+york\b", r"\bsan\s+francisco\b", r"\bseattle\b",
    r"\baustin\b", r"\bboston\b", r"\bchicago\b", r"\blos\s+angeles\b",
    r"\bdenver\b", r"\batlanta\b", r"\bsunnyvale\b", r"\bmountain\s+view\b",
    r"\bpalo\s+alto\b", r"\bsan\s+jose\b", r"\bsan\s+diego\b",
    r"\bportland\b", r"\bphiladelphia\b", r"\bwashington\b",
    r"\braleigh\b", r"\bcharlotte\b", r"\bmiami\b", r"\bdallas\b",
    r"\bhouston\b", r"\bphoenix\b", r"\bsalt\s+lake\b", r"\bdetroit\b",
    r"\bminneapolis\b", r"\bpittsburgh\b", r"\bcleveland\b", r"\bcolumbus\b",
    r"\bindianapolis\b", r"\bnashville\b", r"\bsan\s+antonio\b",
    r"\bjacksonville\b", r"\bmemphis\b", r"\bsacramento\b",
    r"\bkansas\s+city\b", r"\btampa\b", r"\borlando\b", r"\bcincinnati\b",
    r"\bst\.?\s*louis\b", r"\bsaint\s+louis\b", r"\bbaltimore\b",
    r"\brichmond\b", r"\bdurham\b", r"\bchapel\s+hill\b", r"\bboulder\b",
    r"\bann\s+arbor\b", r"\bprovo\b", r"\bredmond\b", r"\bkirkland\b",
    r"\birvine\b", r"\bsanta\s+clara\b", r"\bcupertino\b", r"\bmenlo\s+park\b",
    r"\bcambridge\b", r"\bbellevue\b", r"\barlington\b",
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def count_matches(text: str, patterns: list[str]) -> int:
    count = 0
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            count += 1
    return count


def has_match(text: str, patterns: list[str]) -> bool:
    return count_matches(text, patterns) > 0


def _has_phrase(text_lower: str, phrases: list[str]) -> bool:
    """Fast plain-string match for disqualifying phrases."""
    for phrase in phrases:
        if phrase in text_lower:
            return True
    return False


def select_variant(description: str) -> str:
    ml_score = count_matches(description, ML_KEYWORDS)
    appdev_score = count_matches(description, APPDEV_KEYWORDS)
    if ml_score > appdev_score and ml_score >= 2:
        return "ml"
    if appdev_score > ml_score and appdev_score >= 2:
        return "appdev"
    return "se"


# ── Scoring components ──────────────────────────────────────────────────────

@lru_cache(maxsize=4096)
def _word_pattern(keyword: str) -> re.Pattern:
    # Whole-word match with an optional plural "s": "api" hits "APIs" but
    # "rag" no longer hits "leverage" and "aws" no longer hits "laws".
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword) + r"s?(?![a-z0-9])")


def _has_keyword(keyword: str, lower: str, whole_word: bool) -> bool:
    if whole_word:
        return _word_pattern(keyword).search(lower) is not None
    return keyword in lower


def keyword_score(text: str, stack=None, whole_word: bool = False) -> tuple[int, int]:
    """Binary presence match across all stack categories. Returns (raw_score, hit_count).

    Scans title+description for each keyword in ``stack`` (defaults to the
    canonical STACK_CATEGORIES). Each keyword is checked once (binary: present
    or not). The score is the sum of weights for all matched keywords; hit_count
    tracks how many distinct keywords matched. Pass a per-user stack to score a
    job against that user's tech preferences instead of the default.
    ``whole_word`` switches from substring to whole-word matching.
    """
    stack = stack if stack is not None else STACK_CATEGORIES
    lower = text.lower()
    score = 0
    hits = 0
    for category in stack.values():
        for keyword, weight in category.items():
            if _has_keyword(keyword, lower, whole_word):
                score += weight
                hits += 1
    return score, hits


def synergy_bonus(text: str, combos=None, whole_word: bool = False) -> int:
    combos = combos if combos is not None else SYNERGY_COMBOS
    lower = text.lower()
    bonus = 0
    for keywords, points in combos:
        if all(_has_keyword(k, lower, whole_word) for k in keywords):
            bonus += points
    return bonus


def title_fit_bonus(title: str, profile=None) -> int:
    """Role-family fit from the title alone. Strong > adjacent > none."""
    profile = profile if profile is not None else _default_profile()
    lower = title.lower()
    if has_match(lower, profile.title_fit_strong):
        return profile.title_fit_strong_points
    if has_match(lower, profile.title_fit_adjacent):
        return profile.title_fit_adjacent_points
    return 0


def title_keyword_boost(title: str, profile=None) -> int:
    """Extra (capped) points when stack keywords appear in the title itself."""
    profile = profile if profile is not None else _default_profile()
    score, _ = keyword_score(title, profile.stack_categories,
                             whole_word=profile.keyword_match == "word")
    return min(score, profile.title_keyword_boost_max)


def poor_fit_penalty(text_lower: str, patterns=None, cap=None) -> int:
    """Negative points for clearly off-profile stacks. Returns <= 0, capped."""
    patterns = patterns if patterns is not None else POOR_FIT_PATTERNS
    cap = cap if cap is not None else POOR_FIT_MAX_PENALTY
    penalty = 0
    for pattern, points in patterns:
        if re.search(pattern, text_lower):
            penalty += points
            if penalty >= cap:
                return -cap
    return -penalty


def senior_desc_penalty(senior_count: int, entry_count: int) -> int:
    """Graded penalty for senior-leaning JDs with zero entry signals."""
    if entry_count > 0:
        return 0
    if senior_count >= 4:
        return -35
    if senior_count >= 3:
        return -30
    if senior_count == 2:
        return -15
    return 0


def is_blocked_source(company: str, url: str = "") -> bool:
    """True when the job comes from a blocklisted staffing/aggregator source.

    Matched three ways so real-world name variants don't slip through:
      1. Exact company-name match against COMPANY_BLOCKLIST.
      2. Blocked URL fragments (aggregator deep links).
      3. The AI scorer's fuzzy substring check (text_has_blocked_source), so
         "BeaconFire Inc." matches the "beacon fire" entry even though the
         exact-match in (1) would miss it. This keeps the rule engine in sync
         with the AI scorer, which previously caught staffing reposts the rule
         engine waved through (and even recommended).
    """
    if company.lower().strip() in COMPANY_BLOCKLIST:
        return True
    url_lower = (url or "").lower()
    if any(sign in url_lower for sign in BLOCKED_URL_SIGNS):
        return True
    return text_has_blocked_source(f"{company} {url}")


def text_has_blocked_source(text: str) -> bool:
    """True when free text mentions a blocked staffing/spam source.

    Checks both the lowercased text and an alphanumeric-only ("compact") form so
    punctuation/spacing variants ("Helic & Co." vs. "helicandco") still match.
    Used by the AI scorers against a haystack of a job's text fields.
    """
    low = text.lower()
    if any(s in low for s in STAFFING_SOURCE_BLOCKLIST):
        return True
    compact = re.sub(r"[^a-z0-9]+", "", low)
    return any(s in compact for s in _STAFFING_SOURCE_BLOCKLIST_COMPACT)


def algo_recommended(score_pct: int, level: str, profile=None) -> bool:
    """Fallback 'recommended' flag when no AI score exists yet."""
    profile = profile if profile is not None else _default_profile()
    return score_pct >= profile.recommended_min_pct and level in profile.recommended_levels


def is_us_location(location: str) -> bool:
    """True when a location string looks US-based (or remote/hybrid).

    A blank location returns True: an absent location is not evidence of a
    foreign role, so we never hard-reject on it. Used by both the scan-time
    scorer (evaluate_job) and the store re-scorer (linkedin_store._rescore_entry)
    so the two stay consistent.
    """
    if not location or not location.strip():
        return True
    if has_match(location.lower(), US_LOCATION_PATTERNS):
        return True
    # State abbreviations are matched against the original (upper) case.
    return any(word in US_STATE_ABBREVS for word in re.findall(r"\b[A-Z]{2}\b", location))


def level_tag(title: str, description: str = "") -> str:
    """Categorize job as New Grad / Entry / Mid / Unknown."""
    text = f"{title} {description}".lower()

    # New Grad
    new_grad = [r"\bnew[\s-]*grad", r"\bnew[\s-]*graduate\b", r"\bcampus\s+hire\b",
                r"\brecent\s+graduate?\b", r"\brecent\s+grad\b", r"\buniversity\s+grad"]
    for p in new_grad:
        if re.search(p, text):
            return "New Grad"

    # Entry
    entry = [
        r"\bentry[\s-]*level\b", r"\bjunior\b", r"\bjr\.?\s",
        r"\bassociate\b", r"\bearly\s+career\b", r"\bfirst\s+opportunity\b",
        r"\bsde[\s-]*[i1]\b", r"\bswe[\s-]*[i1]\b", r"\beng[\s-]*[i1]\b",
        r"\blevel[\s-]*[i1]\b", r"\blevel[\s-]*1\b",
        r"\b0[\s-]*(?:to[\s-]*)?2\s*(?:years?|yrs?)\b",
        r"\bintern\b",
    ]
    for p in entry:
        if re.search(p, text):
            return "Entry"

    # Mid
    mid = [
        r"\bsde[\s-]*(?:ii|2)\b", r"\bswe[\s-]*(?:ii|2)\b", r"\beng[\s-]*(?:ii|2)\b",
        r"\bsoftware\s+engineer\s*(?:ii|2)\b", r"\bmid[\s-]*level\b",
        r"\blevel[\s-]*(?:ii|2)\b",
        r"\b2[\s-]*(?:to[\s-]*)?5\s*(?:years?|yrs?)\b",
        r"\b3[\s-]*(?:to[\s-]*)?5\s*(?:years?|yrs?)\b",
    ]
    for p in mid:
        if re.search(p, text):
            return "Mid"

    return "Unknown"


def extract_experience(text: str) -> tuple[int | None, int | None]:
    """Parse experience requirements from JD text. Returns (min_exp, max_exp)."""
    text = text.lower()
    m = re.search(r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:\+\s*)?(?:years?|yrs?)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\+\s*(?:years?|yrs?)", text)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?:minimum|at\s+least|min)\s*(?:of\s+)?(\d+)\s*(?:years?|yrs?)", text)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(\d+)\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)", text)
    if m:
        return int(m.group(1)), int(m.group(1))
    return None, None


def experience_score(min_exp: int | None, max_exp: int | None) -> int:
    """Score based on parsed experience requirements.

    Sweet spot for a new grad: 0-2 years (+10). The further from this
    range, the lower the score. Jobs requiring 3+ years get reduced scores
    since they're borderline for a new grad applicant.
    """
    if min_exp is None and max_exp is None:
        return 0
    if (min_exp is None or min_exp <= 2) and (max_exp is None or max_exp >= 2):
        return 10
    if max_exp is not None and max_exp <= 1:
        return 8
    if min_exp is not None and min_exp == 3:
        return 6
    if min_exp is not None and min_exp > 3:
        return 0
    return 4


def recency_score(iso_timestamp: str | None) -> int:
    """Time-decay scoring — freshly posted jobs are more valuable.

    Applying early increases chances. Jobs older than 48h get a penalty (-5)
    because they likely already have many applicants.
    """
    if not iso_timestamp:
        return 0
    try:
        posted = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        hours = max((datetime.now(tz=timezone.utc) - posted).total_seconds() / 3600, 0)
    except (ValueError, TypeError):
        return 0
    if hours < 6:
        return 10
    if hours < 12:
        return 8
    if hours < 24:
        return 5
    if hours < 48:
        return 2
    return -5


def competition_estimate(company: str, hours_old: float = 0) -> int:
    """Estimate applicant competition (0-10) for prioritization.

    Big Tech companies (+5) and older postings (+2-5) tend to have more
    applicants. This score is informational — it doesn't affect filtering,
    but helps the user prioritize which jobs to apply to first.
    """
    company_lower = company.lower()
    score = 0
    if any(bt in company_lower for bt in BIG_TECH):
        score += 5
    if hours_old > 48:
        score += 5
    elif hours_old > 24:
        score += 2
    return min(10, score)


def _level_points(level: str, profile=None) -> int:
    profile = profile if profile is not None else _default_profile()
    return profile.level_points.get(level, 4)


# ── Main evaluation ─────────────────────────────────────────────────────────

def evaluate_job(job: JobPosting, first_seen: str | None = None,
                 profile=None) -> FilterResult:
    """Evaluate a job posting with multi-signal scoring and hard rejection.

    ``profile`` (a :class:`~jobflow.filter_profile.FilterProfile`) selects the
    per-user tunable knobs — tech stack, sponsorship/location gates, seniority
    band, recommend bar. It defaults to ``DEFAULT_PROFILE``, which reproduces
    the original single-user behaviour exactly.
    """
    profile = profile if profile is not None else _default_profile()
    title_lower = job.title.lower()
    text = f"{job.title} {job.description}"
    text_lower = text.lower()
    variant = select_variant(text_lower)
    level = level_tag(job.title, job.description)

    def _reject(reason: str) -> FilterResult:
        # Keep the job in the pipeline with score=0 so the AI rescorer
        # (scripts/ai_score_local.py) can correct false positives. The AI
        # handles sponsorship/seniority nuance better than regex phrase matching.
        return FilterResult(
            score=0, score_pct=0, reason=reason,
            resume_variant=variant, level=level, reject_reason=reason,
        )

    # ── 1. Company / staffing-source blocklist (company name or URL) ──
    if is_blocked_source(job.company, getattr(job, "url", "")):
        return _reject(f"Blocked company: {job.company}")

    # ── 2. Title-level hard reject (senior, QA, architect, VP) ──
    for pattern in TITLE_REJECT_PATTERNS:
        if re.search(pattern, title_lower):
            return _reject(f"Title disqualified: {job.title}")

    # ── 3. Sponsorship / citizenship / clearance ──
    # Only a hard reject for users who need sponsorship; a citizen/GC holder
    # should still see these jobs.
    if profile.requires_sponsorship and _has_phrase(text_lower, profile.disqualifying_phrases):
        return _reject("No visa sponsorship or requires citizenship/clearance")

    # ── 4. Non-US location ──
    if profile.us_only and not is_us_location(job.location):
        return _reject(f"Non-US location: {job.location}")

    # ── 5. Overqualified experience (soft demotion, not a hard reject) ──
    # extract_experience occasionally over-counts ("3–8+ yrs" → false senior),
    # so instead of deleting the job we flag it for a penalty below and let the
    # AI rescorer make the call. The acceptable band is per-user (max_min_exp).
    min_exp, max_exp = extract_experience(text_lower)
    overqualified = (
        (min_exp is not None and min_exp > profile.max_min_exp)
        or any(re.search(p, text_lower) for p in OVERQUALIFIED_PATTERNS)
    )

    # ── 6. Senior salary signal (soft demotion if no entry signals) ──
    has_senior_salary = bool(re.search(SENIOR_SALARY_PATTERN, text))
    has_entry_signals = has_match(text_lower, ENTRY_LEVEL_SIGNALS)
    senior_salary = has_senior_salary and not has_entry_signals

    # ══ Passed all hard filters — compute additive score ══
    # Each component adds to a raw score (max ~130), then normalized to 0-100%.
    # The reasons list tracks which components contributed for the UI display.
    reasons = []

    # Keyword matching — how well does this JD match the user's tech stack?
    whole_word = profile.keyword_match == "word"
    ks, hits = keyword_score(text, profile.stack_categories, whole_word=whole_word)
    if profile.stack_score_max is not None:
        ks = min(ks, profile.stack_score_max)
    if ks > 0:
        reasons.append(f"Stack +{ks}")

    # Synergy bonus
    sb = synergy_bonus(text, profile.synergy_combos, whole_word=whole_word)
    if sb > 0:
        reasons.append(f"Synergy +{sb}")

    # Title role-fit + title keyword boost — the title is the strongest
    # non-AI relevance signal, so it earns dedicated points.
    tf = title_fit_bonus(job.title, profile)
    if tf > 0:
        reasons.append(f"Title fit +{tf}")
    tb = title_keyword_boost(job.title, profile)
    if tb > 0:
        reasons.append(f"Title stack +{tb}")

    # Off-profile stack penalty (Salesforce, mainframe, FPGA, helpdesk, ...)
    pf = poor_fit_penalty(text_lower, profile.poor_fit_patterns, profile.poor_fit_max)
    if pf < 0:
        reasons.append(f"Poor fit {pf}")

    # Level
    lp = _level_points(level, profile)
    if level != "Unknown":
        reasons.append(f"{level} +{lp}")

    # Experience scoring
    es = experience_score(min_exp, max_exp)
    if es > 0:
        reasons.append(f"Exp fit +{es}")

    # Recency
    rs = recency_score(first_seen)
    if rs != 0:
        reasons.append(f"Recency {'+' if rs > 0 else ''}{rs}")

    # Location bonus — use the shared is_us_location so the gate and the score
    # agree (under us_only the only jobs reaching here are US, so this is +10;
    # under us_only=False a non-US job still scores -10 to rank it lower).
    loc_score = 10 if is_us_location(job.location) else -10
    if loc_score > 0:
        reasons.append("US +10")

    # H1B bonus — only relevant to users who want visa sponsorship.
    h1b_bonus = 0
    if profile.prefer_h1b_bonus and any(p in text_lower for p in profile.h1b_phrases):
        h1b_bonus = 8
        reasons.append("H1B +8")

    # Competition
    hours_old = 0.0
    if first_seen:
        try:
            posted = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            hours_old = max((datetime.now(tz=timezone.utc) - posted).total_seconds() / 3600, 0)
        except (ValueError, TypeError):
            pass
    comp = competition_estimate(job.company, hours_old)

    # Description-level senior penalty (soft — JD may mention "work with senior engineers")
    senior_count = count_matches(text_lower, SENIOR_DESC_SIGNALS)
    entry_count = count_matches(text_lower, ENTRY_LEVEL_SIGNALS)
    senior_penalty = senior_desc_penalty(senior_count, entry_count)
    if senior_penalty < 0:
        reasons.append(f"Senior desc {senior_penalty}")

    # Soft demotions for unreliable seniority proxies (sections 5 & 6). These
    # were hard rejects; demoting keeps the job visible for AI rescoring while
    # pushing its score down so it ranks below clean entry-level matches.
    demotion = 0
    if overqualified:
        demotion += OVERQUALIFIED_PENALTY
        reasons.append(f"Overqualified {OVERQUALIFIED_PENALTY}")
    if senior_salary:
        demotion += SENIOR_SALARY_PENALTY
        reasons.append(f"Senior salary {SENIOR_SALARY_PENALTY}")

    # ── Aggregate: sum all components, normalize to 0-100% ──
    raw = ks + sb + tf + tb + pf + lp + es + rs + loc_score + h1b_bonus + senior_penalty + demotion
    score_pct = min(100, max(0, round(raw / SCORE_MAX_RAW * 100)))
    score = max(0, min(100, raw))
    reason = "; ".join(reasons) if reasons else "Meets basic criteria"

    return FilterResult(
        score=score, score_pct=score_pct,
        reason=reason, resume_variant=variant, level=level,
        min_exp=min_exp, max_exp=max_exp, competition=comp, keyword_hits=hits,
    )


# ── Per-user profile bridge (cycle-free) ────────────────────────────────────
# filter_profile.py imports this module's constants at its top. To avoid an
# import cycle, filter.py does NOT import filter_profile at module load; instead
# it resolves DEFAULT_PROFILE lazily (first use) and exposes the FilterProfile /
# DEFAULT_PROFILE names via module __getattr__ (PEP 562). So `from jobflow.filter
# import DEFAULT_PROFILE` and `filter.DEFAULT_PROFILE` both work, in any import
# order, without either module needing the other fully initialized at import.
_DEFAULT_PROFILE = None


def _default_profile():
    global _DEFAULT_PROFILE
    if _DEFAULT_PROFILE is None:
        from .filter_profile import DEFAULT_PROFILE as _dp
        _DEFAULT_PROFILE = _dp
    return _DEFAULT_PROFILE


def __getattr__(name):
    if name in ("FilterProfile", "DEFAULT_PROFILE"):
        from . import filter_profile
        return getattr(filter_profile, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
