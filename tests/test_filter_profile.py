"""Tests for per-user FilterProfile behaviour in the scoring engine.

Phase 0 of the multi-user conversion: evaluate_job() gains an optional
FilterProfile. The DEFAULT_PROFILE must reproduce the original single-user
behaviour exactly (covered here by a regression test), while a custom profile
changes sponsorship/location gates, the tech stack, and the seniority band.
"""

import pytest

from jobflow.models import JobPosting
from jobflow.filter import evaluate_job, DEFAULT_PROFILE
from jobflow.filter_profile import (
    FilterProfile, profile_from_config, profile_to_config,
)


def _job(title="Software Engineer", company="Acme", location="Remote, US", description="Python"):
    return JobPosting(url="x", title=title, company=company, location=location, description=description)


class TestSponsorshipGate:
    """requires_sponsorship controls whether 'no sponsorship' jobs are rejected."""

    def test_default_rejects_no_sponsorship(self):
        job = _job(description="Python role. We do not offer sponsorship.")
        result = evaluate_job(job)  # DEFAULT_PROFILE requires sponsorship
        assert result.reject_reason
        assert result.score == 0

    def test_citizen_keeps_no_sponsorship(self):
        job = _job(description="Python role. We do not offer sponsorship.")
        citizen = FilterProfile(requires_sponsorship=False)
        result = evaluate_job(job, profile=citizen)
        assert not result.reject_reason
        assert result.score > 0

    def test_citizen_gets_no_h1b_bonus(self):
        job = _job(description="Python backend. Will sponsor H1B visa for the right candidate.")
        citizen = FilterProfile(requires_sponsorship=False, prefer_h1b_bonus=False)
        result = evaluate_job(job, profile=citizen)
        assert "H1B" not in result.reason
        # The default profile, by contrast, awards the bonus.
        assert "H1B" in evaluate_job(job).reason


class TestLocationGate:
    """us_only controls whether non-US jobs are hard-rejected."""

    def test_default_rejects_non_us(self):
        job = _job(location="Toronto, Canada")
        result = evaluate_job(job)
        assert result.reject_reason
        assert "Non-US" in result.reason

    def test_global_profile_keeps_non_us(self):
        job = _job(location="Toronto, Canada")
        worldwide = FilterProfile(us_only=False)
        result = evaluate_job(job, profile=worldwide)
        assert not result.reject_reason
        assert "Non-US" not in result.reason


class TestCustomStack:
    """A user's own tech stack changes which JDs score highest."""

    def test_java_profile_outscores_default_on_java_jd(self):
        job = _job(
            title="Backend Engineer",
            description="Java and Spring Boot backend services. New grad role.",
        )
        java_profile = FilterProfile(
            stack_categories={"core": {"java": 10, "spring": 8, "python": 2}},
        )
        java_score = evaluate_job(job, profile=java_profile).score
        default_score = evaluate_job(job).score  # default weights java at 4, no spring
        assert java_score > default_score


class TestSeniorityBand:
    """max_min_exp shifts the overqualified threshold."""

    def test_default_demotes_four_years(self):
        job = _job(title="Backend Engineer",
                   description="We need 4+ years building scalable APIs in Python.")
        result = evaluate_job(job)
        assert "Overqualified" in result.reason

    def test_senior_profile_keeps_four_years(self):
        job = _job(title="Backend Engineer",
                   description="We need 4+ years building scalable APIs in Python.")
        senior_ok = FilterProfile(max_min_exp=5)
        result = evaluate_job(job, profile=senior_ok)
        assert "Overqualified" not in result.reason


class TestConfigRoundTrip:
    """profile_to_config / profile_from_config are lossless and partial-safe."""

    def test_default_round_trip(self):
        assert profile_from_config(profile_to_config(DEFAULT_PROFILE)) == DEFAULT_PROFILE

    def test_custom_round_trip(self):
        p = FilterProfile(
            requires_sponsorship=False, us_only=False, max_min_exp=6,
            stack_categories={"core": {"rust": 9, "go": 7}},
            recommended_min_pct=70,
        )
        assert profile_from_config(profile_to_config(p)) == p

    def test_partial_config_fills_defaults(self):
        p = profile_from_config({"requires_sponsorship": False})
        assert p.requires_sponsorship is False
        # Everything else falls back to the defaults.
        assert p.us_only == DEFAULT_PROFILE.us_only
        assert p.stack_categories == DEFAULT_PROFILE.stack_categories
        assert p.recommended_min_pct == DEFAULT_PROFILE.recommended_min_pct

    def test_empty_config_is_default(self):
        assert profile_from_config({}) == DEFAULT_PROFILE
        assert profile_from_config(None) == DEFAULT_PROFILE

    def test_new_fields_round_trip(self):
        p = FilterProfile(keyword_match="word", stack_score_max=55,
                          disqualifying_phrases=["citizens only"])
        assert profile_from_config(profile_to_config(p)) == p


class TestDisqualifyingPhrases:
    """The sponsorship blocklist is per-user (OPT is authorized to work now)."""

    JD = "Python role. Candidates must be authorized to work in the US."

    def test_default_rejects_authorization_phrase(self):
        assert evaluate_job(_job(description=self.JD)).reject_reason

    def test_custom_list_keeps_authorization_phrase(self):
        opt = FilterProfile(disqualifying_phrases=["will not sponsor"])
        assert not evaluate_job(_job(description=self.JD), profile=opt).reject_reason
        no_sponsor = _job(description="Python role. We will not sponsor visas.")
        assert evaluate_job(no_sponsor, profile=opt).reject_reason


class TestKeywordMatching:
    """keyword_match="word" stops substring false positives."""

    STACK = {"x": {"rag": 7, "aws": 6, "api": 2}}

    def test_substring_mode_counts_false_positives(self):
        p = FilterProfile(stack_categories=self.STACK)
        jd = "Leverage storage per applicable laws."
        assert "Stack +13" in evaluate_job(_job(description=jd), profile=p).reason

    def test_word_mode_ignores_false_positives(self):
        p = FilterProfile(stack_categories=self.STACK, keyword_match="word")
        jd = "Leverage storage per applicable laws."
        assert "Stack" not in evaluate_job(_job(description=jd), profile=p).reason

    def test_word_mode_matches_whole_words_and_plurals(self):
        p = FilterProfile(stack_categories=self.STACK, keyword_match="word")
        jd = "Build RAG pipelines and REST APIs on AWS."
        assert "Stack +15" in evaluate_job(_job(description=jd), profile=p).reason

    def test_word_mode_applies_to_synergy(self):
        p = FilterProfile(stack_categories=self.STACK, keyword_match="word",
                          synergy_combos=[({"rag", "aws"}, 9)])
        assert "Synergy" not in evaluate_job(_job(description="Leverage laws."), profile=p).reason
        assert "Synergy +9" in evaluate_job(_job(description="RAG on AWS."), profile=p).reason

    def test_stack_score_cap(self):
        p = FilterProfile(stack_categories=self.STACK, keyword_match="word", stack_score_max=8)
        jd = "Build RAG pipelines and REST APIs on AWS."
        assert "Stack +8" in evaluate_job(_job(description=jd), profile=p).reason


class TestDefaultProfileRegression:
    """evaluate_job(job) must be byte-identical to passing DEFAULT_PROFILE."""

    @pytest.mark.parametrize("job", [
        _job(description="Python, FastAPI, AWS. New grad backend role."),
        _job(title="Senior Software Engineer", description="Python"),  # title reject
        _job(location="London, UK", description="Python"),             # non-US reject
        _job(description="Salesforce and ABAP consultant. Mainframe COBOL."),  # poor fit
        _job(title="ML Engineer", description="Machine learning, PyTorch, Python. 0-2 years."),
        _job(description="We require 6 years of experience. $180,000 salary."),  # demotions
    ])
    def test_no_profile_equals_default_profile(self, job):
        assert evaluate_job(job) == evaluate_job(job, profile=DEFAULT_PROFILE)


class TestRescoreEntryBreakdown:
    """Per-user re-scores must refresh the match breakdown shown in the feed."""

    def test_reason_and_variant_follow_profile(self):
        from jobflow.linkedin_store import _rescore_entry
        entry = {"url": "x", "title": "Software Engineer", "company": "Acme",
                 "location": "Remote, US", "description_preview": "Python and Rust.",
                 "reason": "stale"}
        p = FilterProfile(stack_categories={"x": {"rust": 9}})
        out = _rescore_entry(dict(entry), p)
        assert "Stack +9" in out["reason"]
        assert out["variant"]


class TestSearchTerms:
    """Settings search terms are added to the default LinkedIn searches."""

    def test_merge_dedupes_and_keeps_defaults_first(self):
        from jobflow.scanner import merge_search_terms, LINKEDIN_SEARCH_TERMS
        terms = merge_search_terms(["Backend Engineer", " backend engineer ", "",
                                    LINKEDIN_SEARCH_TERMS[0].upper()])
        assert terms == [*LINKEDIN_SEARCH_TERMS, "Backend Engineer"]

    def test_merge_caps_total(self):
        from jobflow.scanner import merge_search_terms, MAX_SEARCH_TERMS
        assert len(merge_search_terms([f"term {i}" for i in range(50)])) == MAX_SEARCH_TERMS
