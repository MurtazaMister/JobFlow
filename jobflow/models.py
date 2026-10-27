"""Core data models shared across all modules.

These dataclasses are intentionally flat and serialization-friendly so they
can be round-tripped to JSON (scan_results.json, linkedin_jobs.json) and
displayed in both the CLI (Rich tables) and web dashboard (Jinja2 templates).
"""

from dataclasses import dataclass


@dataclass
class JobPosting:
    """A single job listing scraped from any source.

    Fields are normalized across all platforms (LinkedIn, Lever, Greenhouse,
    Ashby, GitHub repos) so downstream code doesn't need source-specific logic.
    """
    url: str
    title: str
    company: str
    location: str
    description: str                # Full JD text, truncated to 6K chars by scanners
    date_posted: str = ""           # ISO timestamp from the source (may be date-only from LinkedIn)
    source: str = "linkedin"        # 'linkedin' (guest API), 'github' (SimplifyJobs/Jobright repos), 'notify', 'instagram'


@dataclass
class FilterResult:
    """Output of evaluate_job() — the scoring engine's verdict on a JobPosting.

    score:          Raw points (0-130 scale, clamped to 0-100 for storage)
    score_pct:      Normalized percentage (0-100), used for display and threshold checks
    reason:         Human-readable explanation of score components or rejection reason
    resume_variant: Which base resume to use — "se" (default), "ml", or "appdev"
    level:          Detected seniority — "New Grad", "Entry", "Mid", or "Unknown"
    min_exp/max_exp: Parsed experience requirements from JD (None = not specified)
    competition:    Estimated applicant competition (0-10), based on company tier + age
    keyword_hits:   Count of tech stack keywords found in the JD
    reject_reason:  Non-empty when a hard-reject fired (e.g. "Non-US location: ...").
                    Hard-rejected jobs are still returned (and kept downstream) so the
                    AI scorer can rescue false positives — reject_reason is the signal
                    that the rule-based pass flagged the job, not score alone.
    """
    score: int
    score_pct: int
    reason: str
    resume_variant: str
    level: str = "Unknown"
    min_exp: int | None = None
    max_exp: int | None = None
    competition: int = 0
    keyword_hits: int = 0
    reject_reason: str = ""
