"""Shared AI-scoring prompt and parsing — the single source of truth used by
every scorer: the local CLI script (scripts/ai_score_local.py), the server-side
Anthropic-API scorer (jobflow/ai_scorer_anthropic.py), and the local CLI client
(`jobflow score`). Keeping the prompt and the parse/normalize logic here means
all three score jobs identically; only the transport (subprocess CLI vs API)
differs.
"""

import json
import re

from .filter import STAFFING_BLOCK_REASON, text_has_blocked_source  # noqa: F401 (re-exported)

BATCH_SIZE = 15

BATCH_PROMPT = """You are a strict job-eligibility screener and relevance scorer for a new grad / entry-level software engineer on F1 OPT visa looking for their first full-time role in the US. Treat the facts stated in the candidate profile (education, experience, degree-substitution rules, visa) as ground truth.

## Candidate Profile
{profile}

## HARD REJECT — score MUST be 0
Give a score of 0 ONLY if ANY of these are true. Be strict — only these 6 conditions warrant a 0:

1. **Explicit no sponsorship**: The posting explicitly says "no sponsorship", "will not sponsor", "cannot sponsor", "must be authorized to work without sponsorship", "US citizen only", "permanent resident only", "green card required". Also score 0 if it requires security clearance (TS/SCI, Secret, DoD). NOTE: "Must be authorized to work in the US" ALONE is NOT a rejection — OPT holders ARE authorized.

2. **Experience requirement the candidate cannot meet**: Apply the EXPERIENCE RULES below. Reject only via that procedure — never from a gut read of the years number.

3. **Senior/Staff/Lead role**: Clearly senior-level, staff, principal, architect, VP, director, or management. Must be obvious from title or JD — don't assume.

4. **Not a software engineering role at all**: QA-only, technical writing, product management, sales engineering, IT support. NOTE: Frontend, Full-Stack, iOS, Android, Data Science WITH coding, DevOps WITH development — these ARE software engineering. Score them low (2-4) if poor fit, but NOT 0.

5. **Not US-based**: Located outside the US with no remote-US option.

6. **Blocked staffing/spam source**: The job is from Remotehunter, Quik Hire Staffing, Beacon Fire, Helic & Co., Jack and Jill, or Jobs Via Dice.

IMPORTANT: The candidate's "Avoid" preferences (e.g., "Avoid: Frontend-only") should LOWER the score (2-4) but NEVER cause a score of 0. A frontend SWE role is still a software engineering role — it's just a weak fit, not a hard reject.

## EXPERIENCE RULES (for reject #2)
Job descriptions use "years of experience" in two different senses. Classify every experience line before judging:

**A. Positional experience** — tenure in a professional software engineering JOB: "X+ years of professional/industry software engineering experience", "X years working as a software engineer", "X years of industry experience". Judge it with this procedure:
- Take the MINIMUM of any range: "2-4+ years" means 2; "1-3 years" means 1.
- Look for an alternative path the candidate meets: "BS + 2 years OR MS + 0 years", "Master's degree may substitute for experience", "X years or equivalent experience", "new grads with advanced degrees welcome". A completed Master's degree satisfies positional requirements up to 2 years whenever the JD offers ANY such alternative/equivalence wording.
- Count internships and research experience toward positional requirements exactly as far as the candidate profile says they count — no further.
- REJECT (score 0) only when the minimum positional requirement still clearly exceeds what the candidate satisfies after substitutions. Concretely, for this candidate: minimum ≤ 1 year → eligible; minimum of 2 years → eligible ONLY via a Master's alternative/equivalence path in the JD, otherwise 0; minimum of 3+ years with no explicit path the candidate meets → 0.

**B. Skill experience** — years attached to a technology, language, or domain: "2+ years of experience with C++", "3 years of Python", "experience with Kubernetes". This is NOT positional tenure — academic work, research, internships, and personal projects all count toward it. NEVER score 0 for skill-years. If the candidate's exposure to that specific skill looks thin, lower the score (3-5) instead.

Traps to avoid:
- "BS/MS in Computer Science **or equivalent**" — that "or equivalent" modifies the DEGREE, not a separate experience requirement elsewhere in the JD. "B.S., M.S., or PhD ... AND 2+ years of industry experience" still requires the 2 years; reject unless the experience line has its own alternative path.
- Garbled numbers ("24+ years" on a non-senior title) are usually a mangled range ("2-4+ years") — read the minimum as 2 and apply the procedure; do NOT excuse it as a typo and wave it through.
- A "preferred"/"nice to have" experience line is not a requirement — only lines in required/minimum qualifications can trigger a reject.
- If the description is cut off and you cannot see the requirements, judge from the title and visible text; do not invent a requirement, and do not assume eligibility for clearly senior titles.

In your one-sentence reason, when experience decided the outcome, cite it explicitly (e.g. "2+ yrs industry required, no MS path" or "MS+0 alternative stated").

## MISSING OR THIN DESCRIPTIONS
Many jobs arrive with only a title (or a one-line description). That is NEVER a reason for 0:
- Score from the title, company and location alone: 3-7. Reserve 8-10 for jobs whose description you could actually read.
- Give 0 for a title-only job ONLY if the title/location itself triggers a hard reject (e.g. "Senior"/"Staff" in the title, a non-US city, or an explicitly non-SWE role).
- Do NOT assume restrictions that are not written down. A defense, aerospace or government employer whose posting does not state citizenship/clearance requirements is NOT a 0 — lower the score (1-3) for the likely restriction instead.

## COMMON MISTAKES — check before answering
- Read the TITLE for location too: "... - Dubai/Abu Dhabi" or "London" in the title means not US-based (0) even if the Location field says a US city.
- "N+ years of <technology/domain>" ("3+ years in distributed file systems", "2+ years of Elixir", "2+ years with PyTorch") is SKILL experience (rule B): lower the score, never 0.
- QA-only / manual-test / SDET roles with no development work are reject #4 (0), not a weak fit.

## SCORING GUIDE (only if no hard reject applies)

**9-10 — Perfect fit:** Entry-level/new grad SWE, ML, Backend, Data Engineer. Python/ML/backend stack. Sponsors visas. Reputable company.
**7-8 — Strong fit:** SWE at right level, good stack overlap, US-based, no sponsorship denial.
**5-6 — Decent fit:** Relevant SWE but weaker stack match (Java, .NET, frontend). Level ambiguous.
**3-4 — Weak fit:** SWE but poor overlap (iOS, Salesforce, embedded, frontend-only). Borderline exp.
**1-2 — Very poor fit:** Barely related to skills. Multiple weak signals.

## Jobs to Score

{jobs_block}

## Instructions
Return ONLY a valid JSON array with one object per job, in the same order. Nothing else:
[{{"id": 1, "score": <0-10>, "reason": "<one sentence>"}}, ...]"""


def build_jobs_block(batch) -> str:
    """Format a batch of jobs for the prompt.

    Each row is a tuple starting (url, company, title, location, desc, ...);
    trailing fields are ignored.
    """
    parts = []
    for i, (url, company, title, location, desc, *_) in enumerate(batch, 1):
        parts.append(
            f"### Job {i}\nTitle: {title}\nCompany: {company}\n"
            f"Location: {location}\nDescription: {desc or ''}\n"
        )
    return "\n".join(parts)


def normalize_row_score(score_data: dict) -> tuple[int, str, int, bool]:
    """Convert one model score object into stored fields:
    (ai_score 0-10, ai_reason<=200 chars, score_pct = ai_score*10, recommended = ai_score>=7).
    """
    ai_score = max(0, min(10, int(score_data.get("score", 5))))
    ai_reason = str(score_data.get("reason", ""))[:200]
    score_pct = ai_score * 10
    recommended = ai_score >= 7
    return ai_score, ai_reason, score_pct, recommended


def eligible_ai_score(ai_score, rescore: bool = False) -> bool:
    """True when a job should be scored. Any non-null ai_score (incl. 0) means
    already judged; skip unless rescore is explicit.
    """
    return rescore or ai_score is None


def is_blocked_staffing_source(row) -> bool:
    """True if a job row (url, company, title, location, desc, ...) is from a
    blocked staffing/spam source — short-circuited to 0 without a model call.
    """
    url, company, title, location, desc, *_ = row
    haystack = " ".join(str(value or "") for value in (url, company, title, location, desc))
    return text_has_blocked_source(haystack)


def parse_scores(text: str):
    """Parse a model's text response into a list of score objects.

    Tolerates markdown fences, stray backticks, and surrounding prose by
    extracting the first JSON array. Returns None on empty/unparseable input.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    text = text.strip("`")
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        text = m.group()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  Batch error: could not parse model output as JSON: {e}")
        return None
