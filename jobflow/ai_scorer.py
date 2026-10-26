"""AI-powered job relevance scoring using Groq (Llama 4 Scout).

LEGACY/OPTIONAL PATH. The canonical AI scorer is scripts/ai_score_local.py,
which drives the signed-in Claude/Codex CLI (no API key) and is what the
dashboard's "AI Score" button runs. This module is a separate, optional layer
used only by `jobflow scan` when GROQ_API_KEY is set — handy for unattended
CI scoring without a CLI session. Both paths write the same fields
(ai_score / ai_reason / ai_model) consumed by linkedin_store._rescore_entry().

This runs AFTER the algorithmic filter (filter.py). While the algo scorer uses
keyword matching and rule-based heuristics, the AI scorer reads the full JD and
makes a holistic judgment.

How it integrates:
    1. scan command calls ai_score_jobs() after saving scan_results.json
    2. Each job gets an ai_score (0-10) and ai_reason (one sentence)
    3. In linkedin_store._rescore_entry(), AI scores override algo scores:
       ai_score * 10 → score_pct (e.g., AI score 8 → 80%)
    4. Only AI-scored jobs can be marked as "recommended" (ai_score >= 7)

The scorer silently skips if:
    - GROQ_API_KEY env var is not set
    - groq package is not installed
    - config/profile.txt doesn't exist (no candidate profile to score against)
    - A job is already scored (idempotent)

Cost: Free (Groq free tier — 30 RPM, 14,400 requests/day)
"""

import json
import os
import re
import time
from pathlib import Path

from .filter import STAFFING_BLOCK_REASON, text_has_blocked_source

SCORE_PROMPT = """You are a job relevance scorer for a new grad / entry-level software engineer on F1 OPT visa looking for their first full-time role in the US.

## Candidate Profile
{profile}

## HARD REJECT — score MUST be 0
Give a score of 0 if ANY of these are true. Check carefully:

1. **No visa sponsorship**: The posting says "no sponsorship", "will not sponsor", "cannot sponsor", "must be authorized to work without sponsorship", "US citizen only", "citizens only", "permanent resident only", "green card required", or any phrasing that means they won't sponsor a work visa. Also score 0 if it requires a security clearance (TS/SCI, Secret, DoD clearance, etc.) since those require citizenship.

2. **Too much experience required**: The posting requires 3 or more years of professional experience (e.g., "3+ years", "4-6 years", "5+ years of experience", "minimum 3 years"). Be precise — "1-2 years" or "0-2 years" is fine. "2+ years" is fine. "3+ years" is NOT fine. Look at the actual minimum, not the preferred/nice-to-have.

3. **Senior/Staff/Lead role**: The role is clearly senior-level, staff-level, principal, architect, VP, director, or management — even if the title doesn't say "Senior" explicitly, if the JD consistently describes 5+ years, team leadership, mentoring, or principal-level scope, score 0.

4. **Not a software engineering role**: The role is primarily QA/testing, technical writing, product management, sales engineering, IT support, or DevOps/SRE-only with no software development. Data Science with heavy statistics and no coding is also a reject.

5. **Not US-based**: The job is located outside the United States with no remote-US option.

6. **Blocked staffing/spam source**: The job is from jobright.ai, Remotehunter, Quik Hire Staffing, Beacon Fire, Helic & Co., Jack and Jill, or Jobs Via Dice.

## SCORING GUIDE (only if no hard reject applies)
Score 1-10 based on how well this job fits the candidate:

**9-10 — Perfect fit, apply immediately:**
- Entry-level / new grad SWE, ML Engineer, Backend Engineer, or Data Engineer
- Mentions Python, ML/AI, distributed systems, or backend technologies the candidate knows
- Explicitly sponsors visas or mentions H1B/OPT
- US-based at a reputable tech company or well-funded startup

**7-8 — Strong fit, definitely apply:**
- SWE / backend / ML role at appropriate level (junior, entry, SDE-1, L3/L4)
- Good tech stack overlap (Python, AWS, Docker, Kubernetes, etc.)
- US-based, doesn't explicitly deny sponsorship
- May have minor gaps (e.g., some unfamiliar technologies, or experience listed as "1-3 years")

**5-6 — Decent fit, worth considering:**
- Relevant SWE role but weaker stack match (e.g., Java-heavy, .NET, frontend-focused)
- Level is ambiguous — could be entry or mid, JD is unclear
- US-based, no sponsorship info either way
- Roles like Full-Stack, Platform Engineer, or Data Engineer with partial overlap

**3-4 — Weak fit, probably skip:**
- Role exists in SWE space but poor overlap (e.g., iOS developer, Salesforce admin, embedded C)
- Experience requirement is borderline (says "2-4 years" — technically the min is 2 but the JD tone suggests mid-level)
- Non-tech company with limited engineering culture
- Heavy on technologies the candidate doesn't know at all

**1-2 — Very poor fit:**
- Barely related to candidate's skills
- Ambiguous sponsorship situation at a company known not to sponsor
- Combination of multiple weak signals

## IMPORTANT NOTES
- When the JD doesn't mention sponsorship at all, do NOT assume the worst. Many companies sponsor but don't advertise it. Only score 0 if there's explicit denial.
- "Must be authorized to work in the US" alone is ambiguous — OPT holders ARE authorized. Only reject if it adds "without sponsorship" or "without company assistance".
- New grad roles at big tech (Google, Amazon, Meta, Apple, Microsoft, etc.) should score high even if the JD is generic, because these companies reliably sponsor.
- If the description is very short or missing, score based on title + company. Don't penalize for lack of info.
- Focus on the MINIMUM experience, not the preferred/desired. "1+ years required, 3+ preferred" = fine.

## Job Posting
Title: {title}
Company: {company}
Location: {location}
Description: {description}

## Instructions
Return ONLY valid JSON, nothing else:
{{"score": <0-10>, "reason": "<one sentence explaining why>"}}"""


def _load_profile(config_root: Path | None = None) -> str:
    """Load the user profile from config/profile.txt."""
    if config_root:
        profile_path = config_root / "config" / "profile.txt"
    else:
        profile_path = Path(__file__).parent.parent / "config" / "profile.txt"
    if profile_path.exists():
        return profile_path.read_text(encoding="utf-8").strip()
    return ""


def _get_client():
    """Create Groq client. Returns None if unavailable."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from groq import Groq
        return Groq(api_key=api_key)
    except ImportError:
        return None


def _is_blocked_ai_source(job: dict) -> bool:
    """Return True if the job comes from a blocked staffing/spam source."""
    haystack = " ".join(
        str(value or "")
        for value in (
            job.get("url", ""),
            job.get("company", ""),
            job.get("title", ""),
            job.get("location", ""),
            job.get("description_preview", ""),
            job.get("description", ""),
        )
    )
    return text_has_blocked_source(haystack)


def score_single_job(client, profile: str, job: dict, max_retries: int = 3) -> dict | None:
    """Score a single job with Llama 4 Scout via Groq. Returns {"ai_score": int, "ai_reason": str} or None."""
    if _is_blocked_ai_source(job):
        return {"ai_score": 0, "ai_reason": STAFFING_BLOCK_REASON}

    prompt = SCORE_PROMPT.format(
        profile=profile,
        title=job.get("title", ""),
        company=job.get("company", ""),
        location=job.get("location", ""),
        description=job.get("description_preview", "") or "",
    )
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=100,
            )
            text = response.choices[0].message.content.strip()
            # Parse JSON — handle markdown code fences, backticks, trailing text
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            text = text.strip("`")
            # Extract first JSON object if there's trailing text
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                text = m.group()
            result = json.loads(text)
            score = max(0, min(10, int(result.get("score", 5))))
            reason = str(result.get("reason", ""))[:200]
            return {"ai_score": score, "ai_reason": reason}
        except Exception as e:
            err_name = type(e).__name__
            if "RateLimit" in err_name or "429" in str(e):
                wait = 5 * (attempt + 1)  # 5s, 10s, 15s
                print(f"  Rate limited, waiting {wait}s (retry {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            return None
    return None


def ai_score_jobs(jobs: list[dict], config_root: Path | None = None, max_score: int = 30) -> list[dict]:
    """Score a list of job dicts with Llama 4 Scout via Groq. Modifies jobs in-place.

    Adds ai_score (1-10), ai_reason, and ai_model to each job.
    Silently skips if GROQ_API_KEY is not set or groq is not installed.
    Caps at max_score jobs per call to avoid blocking scans on rate limits.
    """
    client = _get_client()
    if not client:
        return jobs

    profile = _load_profile(config_root)
    if not profile:
        return jobs

    scored = 0
    failures = 0
    for job in jobs:
        if scored >= max_score:
            break
        # Skip if already scored
        if job.get("ai_score") is not None:
            continue
        result = score_single_job(client, profile, job)
        if result:
            job["ai_score"] = result["ai_score"]
            job["ai_reason"] = result["ai_reason"]
            job["ai_model"] = "groq"
            scored += 1
            # Groq free tier: 30 RPM — 5s sleep = 12 RPM, safe margin
            time.sleep(5)
        else:
            failures += 1
            # Stop early if hitting repeated failures (likely rate limited)
            if failures >= 3:
                break

    return jobs
