"""Per-user filter configuration for the scoring engine.

The scoring logic in :mod:`jobflow.filter` was historically hardcoded to one
user (Python/ML/backend stack, needs F1/OPT sponsorship, US-only, new-grad).
``FilterProfile`` captures the knobs that legitimately differ between users so
the same engine can score a job pool differently for each person:

  * ``requires_sponsorship`` — a US citizen must NOT reject "no sponsorship"
    jobs and must NOT get the visa-sponsorship bonus.
  * ``us_only`` — some users are open to non-US / remote-international roles.
  * tech stack (``stack_categories``, ``synergy_combos``, ``poor_fit_patterns``)
    and target role family (``title_fit_*``) — the core personalization.
  * seniority band (``level_points``, ``max_min_exp``) and the recommend bar.

``DEFAULT_PROFILE`` reproduces the original hardcoded behaviour *exactly*: its
field defaults are the module-level constants in :mod:`jobflow.filter`. Calling
``evaluate_job(job)`` with no profile is identical to passing ``DEFAULT_PROFILE``.

Storage: ``profile_to_config`` flattens a profile to JSON-serializable types for
``user_profiles.filter_config`` (Postgres JSONB); ``profile_from_config`` rebuilds
one, filling any missing keys from the defaults so partial configs are safe.
"""

from dataclasses import dataclass, field, fields

# Import the canonical constants from filter.py. This is one-directional:
# filter.py imports FilterProfile/DEFAULT_PROFILE at the *bottom* of its module
# (after these constants are defined), so there is no import cycle.
from .filter import (
    DISQUALIFYING_PHRASES,
    STACK_CATEGORIES,
    SYNERGY_COMBOS,
    POOR_FIT_PATTERNS,
    POOR_FIT_MAX_PENALTY,
    TITLE_FIT_STRONG,
    TITLE_FIT_ADJACENT,
    TITLE_FIT_STRONG_POINTS,
    TITLE_FIT_ADJACENT_POINTS,
    TITLE_KEYWORD_BOOST_MAX,
    H1B_PREFER,
    RECOMMENDED_MIN_PCT,
)

# Default level points (was inlined in filter._level_points).
DEFAULT_LEVEL_POINTS = {"New Grad": 20, "Entry": 15, "Mid": 5, "Unknown": 4}
DEFAULT_RECOMMENDED_LEVELS = ("New Grad", "Entry")
# A job whose parsed minimum experience exceeds this is "overqualified" (demoted).
# 3 reproduces the original ">= 4 years" threshold.
DEFAULT_MAX_MIN_EXP = 3


@dataclass(frozen=True)
class FilterProfile:
    """Per-user tunable scoring knobs. Defaults reproduce the original behaviour."""

    # Hard-reject gates that depend on the person, not the product
    requires_sponsorship: bool = True
    us_only: bool = True
    # Phrases that hard-reject a job when requires_sponsorship is set. Per-user
    # because "must be authorized to work in the US" disqualifies some visa
    # situations but not others (e.g. F-1 OPT is authorized now).
    disqualifying_phrases: list = field(default_factory=lambda: DISQUALIFYING_PHRASES)

    # Tech stack / role fit
    stack_categories: dict = field(default_factory=lambda: STACK_CATEGORIES)
    synergy_combos: list = field(default_factory=lambda: SYNERGY_COMBOS)
    poor_fit_patterns: list = field(default_factory=lambda: POOR_FIT_PATTERNS)
    poor_fit_max: int = POOR_FIT_MAX_PENALTY
    title_fit_strong: list = field(default_factory=lambda: TITLE_FIT_STRONG)
    title_fit_adjacent: list = field(default_factory=lambda: TITLE_FIT_ADJACENT)
    title_fit_strong_points: int = TITLE_FIT_STRONG_POINTS
    title_fit_adjacent_points: int = TITLE_FIT_ADJACENT_POINTS
    title_keyword_boost_max: int = TITLE_KEYWORD_BOOST_MAX
    # "substring" (original: "rag" also hits "leverage") or "word" (whole-word,
    # plural-tolerant). Applies to stack keywords and synergy combos.
    keyword_match: str = "substring"
    # Optional ceiling on stack-keyword points so a JD that lists 30
    # technologies can't saturate the match % on its own. None = uncapped.
    stack_score_max: int | None = None

    # Seniority preferences
    level_points: dict = field(default_factory=lambda: dict(DEFAULT_LEVEL_POINTS))
    max_min_exp: int = DEFAULT_MAX_MIN_EXP

    # Visa-sponsorship bonus
    prefer_h1b_bonus: bool = True
    h1b_phrases: list = field(default_factory=lambda: H1B_PREFER)

    # Recommend bar (fallback when no AI score yet)
    recommended_min_pct: int = RECOMMENDED_MIN_PCT
    recommended_levels: tuple = DEFAULT_RECOMMENDED_LEVELS


DEFAULT_PROFILE = FilterProfile()


# ── Serialization (JSONB-friendly) ──────────────────────────────────────────

def profile_to_config(profile: FilterProfile) -> dict:
    """Flatten a FilterProfile into JSON-serializable types for DB storage.

    Sets become sorted lists; tuples become lists. ``profile_from_config``
    reverses this losslessly (set membership and combo identity are preserved).
    """
    return {
        "requires_sponsorship": profile.requires_sponsorship,
        "us_only": profile.us_only,
        "disqualifying_phrases": list(profile.disqualifying_phrases),
        "stack_categories": {
            cat: dict(kws) for cat, kws in profile.stack_categories.items()
        },
        "synergy_combos": [
            {"keywords": sorted(kws), "points": pts}
            for kws, pts in profile.synergy_combos
        ],
        "poor_fit_patterns": [[pat, pts] for pat, pts in profile.poor_fit_patterns],
        "poor_fit_max": profile.poor_fit_max,
        "title_fit_strong": list(profile.title_fit_strong),
        "title_fit_adjacent": list(profile.title_fit_adjacent),
        "title_fit_strong_points": profile.title_fit_strong_points,
        "title_fit_adjacent_points": profile.title_fit_adjacent_points,
        "title_keyword_boost_max": profile.title_keyword_boost_max,
        "keyword_match": profile.keyword_match,
        "stack_score_max": profile.stack_score_max,
        "level_points": dict(profile.level_points),
        "max_min_exp": profile.max_min_exp,
        "prefer_h1b_bonus": profile.prefer_h1b_bonus,
        "h1b_phrases": list(profile.h1b_phrases),
        "recommended_min_pct": profile.recommended_min_pct,
        "recommended_levels": list(profile.recommended_levels),
    }


# Field names that need type reconstruction from their JSON form.
_FIELD_NAMES = {f.name for f in fields(FilterProfile)}


def profile_from_config(config: dict | None) -> FilterProfile:
    """Rebuild a FilterProfile from a stored config dict.

    Any missing key falls back to ``DEFAULT_PROFILE`` so partial configs (e.g.
    a web form that only set ``requires_sponsorship``) are always valid.
    Unknown keys are ignored. The inverse of ``profile_to_config``.
    """
    cfg = dict(config or {})
    kwargs = {}

    # Scalar / passthrough fields: take from config if present, else default.
    for name in (
        "requires_sponsorship", "us_only", "poor_fit_max",
        "title_fit_strong_points", "title_fit_adjacent_points",
        "title_keyword_boost_max", "max_min_exp", "prefer_h1b_bonus",
        "recommended_min_pct", "keyword_match", "stack_score_max",
    ):
        if name in cfg:
            kwargs[name] = cfg[name]

    for name in ("stack_categories", "title_fit_strong", "title_fit_adjacent",
                 "level_points", "h1b_phrases", "disqualifying_phrases"):
        if name in cfg and cfg[name] is not None:
            kwargs[name] = cfg[name]

    if "synergy_combos" in cfg and cfg["synergy_combos"] is not None:
        kwargs["synergy_combos"] = [
            (set(c["keywords"]), c["points"]) for c in cfg["synergy_combos"]
        ]
    if "poor_fit_patterns" in cfg and cfg["poor_fit_patterns"] is not None:
        kwargs["poor_fit_patterns"] = [
            (pat, pts) for pat, pts in cfg["poor_fit_patterns"]
        ]
    if "recommended_levels" in cfg and cfg["recommended_levels"] is not None:
        kwargs["recommended_levels"] = tuple(cfg["recommended_levels"])

    # Guard against stray keys that aren't real fields.
    kwargs = {k: v for k, v in kwargs.items() if k in _FIELD_NAMES}
    return FilterProfile(**kwargs)
