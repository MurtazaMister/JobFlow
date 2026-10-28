"""Tests for jobflow/filter.py — the scoring engine and hard rejection logic."""

import pytest
from datetime import datetime, timedelta, timezone

from jobflow.models import JobPosting, FilterResult
from jobflow.filter import (
    evaluate_job, keyword_score, synergy_bonus, level_tag,
    extract_experience, experience_score, recency_score,
    competition_estimate, select_variant, count_matches, has_match,
    DISQUALIFYING_PHRASES, COMPANY_BLOCKLIST, TITLE_REJECT_PATTERNS,
    OVERQUALIFIED_PATTERNS, ENTRY_LEVEL_SIGNALS, SENIOR_DESC_SIGNALS,
    STACK_CATEGORIES, SYNERGY_COMBOS, BIG_TECH, H1B_PREFER,
)


# ═══════════════════════════════════════════════════════════════════════════
# HARD REJECTION TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestHardRejectTitle:
    """Jobs with senior/QA/architect titles should be instantly rejected."""

    @pytest.mark.parametrize("title", [
        "Senior Software Engineer",
        "Sr. Backend Developer",
        "Staff Engineer",
        "Principal Engineer",
        "Lead Software Engineer",
        "Engineering Manager",
        "Director of Engineering",
        "Solutions Architect",
        "VP of Engineering",
        "Vice President of Engineering",
        "Head of Engineering",
        "QA Engineer",
        "SDET",
        "Quality Assurance Engineer",
        "Test Automation Engineer",
        "Testing Engineer",
        "Quality Engineer",
    ])
    def test_title_reject(self, title):
        job = JobPosting(url="x", title=title, company="Acme", location="US", description="Python")
        result = evaluate_job(job)
        assert result.score == 0
        assert result.reject_reason
        assert "Title disqualified" in result.reason

    @pytest.mark.parametrize("title", [
        "Software Engineer",
        "Software Engineer, New Grad",
        "Junior Software Developer",
        "Entry Level Backend Engineer",
        "Associate Software Engineer",
        "ML Engineer",
        "Data Engineer",
        "Full Stack Developer",
        "Software Engineer II",
        "SDE I",
    ])
    def test_title_pass(self, title):
        job = JobPosting(url="x", title=title, company="Acme", location="Remote, US", description="Python developer role")
        result = evaluate_job(job)
        assert result.score > 0 or "Title disqualified" not in result.reason


class TestHardRejectCompany:
    """Spam aggregator companies should be instantly rejected."""

    @pytest.mark.parametrize("company", [
        "Dice", "Jobs via Dice", "CyberCoders", "Jobot", "Turing",
        "Micro1", "Hackajob", "Toptal", "Crossover", "LanceSoft",
        "Jack & Jill", "Jack and Jill",
    ])
    def test_blocked_company(self, company):
        job = JobPosting(url="x", title="SWE", company=company, location="US", description="Python")
        result = evaluate_job(job)
        assert result.score == 0
        assert "Blocked company" in result.reason

    def test_legit_company_passes(self):
        job = JobPosting(url="x", title="SWE", company="Stripe", location="SF", description="Python")
        result = evaluate_job(job)
        assert "Blocked company" not in result.reason

    def test_jobright_repost_passes(self):
        job = JobPosting(url="https://jobright.ai/jobs/info/abc", title="SWE", company="Nutanix",
                         location="SF", description="Python")
        result = evaluate_job(job)
        assert "Blocked company" not in result.reason


class TestHardRejectSponsorship:
    """Jobs that explicitly deny sponsorship should be rejected."""

    @pytest.mark.parametrize("phrase", [
        "We will not sponsor visas for this role.",
        "No sponsorship available.",
        "Must be authorized to work in the United States without sponsorship.",
        "US citizenship required for this position.",
        "Requires active Top Secret/SCI clearance.",
        "DoD Secret clearance required.",
        "Must be a U.S. citizen.",
        "Green card holder only.",
        "Sponsorship is not available for this position.",
        "Eligible to work in the US without sponsorship.",
        "Without the need for sponsorship.",
        "Must hold clearance.",
        "Ability to obtain a security clearance.",
        "Lawful permanent resident required.",
    ])
    def test_sponsorship_reject(self, phrase):
        job = JobPosting(url="x", title="SWE", company="Acme", location="US", description=f"Python role. {phrase}")
        result = evaluate_job(job)
        assert result.score == 0
        assert result.reject_reason

    def test_sponsorship_positive_passes(self):
        """Job that offers sponsorship should NOT be rejected."""
        job = JobPosting(url="x", title="SWE", company="Acme", location="San Francisco, CA", description="Python. Will sponsor H1B visa.")
        result = evaluate_job(job)
        assert result.score > 0


class TestDemoteOverqualified:
    """Jobs requiring 4+ years are DEMOTED (penalized), not hard rejected.

    Experience text is an unreliable seniority proxy, so the job stays in the
    pool (no reject_reason) but its score is pushed down for the AI to confirm.
    """

    @pytest.mark.parametrize("desc", [
        "Requires at least 5 years of experience in software engineering.",
        "Minimum 4 years of professional experience.",
        "7+ years of hands-on experience.",
        "10 years of industry experience required.",
        "At least 3–8+ years of professional working experience.",
        "5-8 years of experience in backend development.",
        "Requires 6 years of Python experience.",
    ])
    def test_overqualified_demoted(self, desc):
        job = JobPosting(url="x", title="Software Engineer", company="Acme", location="Remote, US", description=desc)
        result = evaluate_job(job)
        assert not result.reject_reason          # kept in the pool, not deleted
        assert "Overqualified" in result.reason  # but penalized
        assert result.score_pct < 65             # demoted below the recommend bar

    @pytest.mark.parametrize("desc", [
        "0-2 years of experience preferred.",
        "1+ years of experience.",
        "No experience required.",
        "Great for recent graduates with 0-1 years experience.",
        "Entry level, 2 years experience preferred.",
    ])
    def test_entry_experience_passes(self, desc):
        job = JobPosting(url="x", title="Junior SWE", company="Acme", location="Remote, US", description=f"Python. {desc}")
        result = evaluate_job(job)
        assert result.score > 0


class TestHardRejectLocation:
    """Non-US locations should be rejected."""

    @pytest.mark.parametrize("location", [
        "London, UK", "Bangalore, India", "Berlin, Germany",
        "Toronto, Canada", "Singapore", "Sydney, Australia",
        "Tokyo, Japan", "Seoul, Korea", "Tel Aviv, Israel",
    ])
    def test_non_us_reject(self, location):
        job = JobPosting(url="x", title="SWE", company="Acme", location=location, description="Python")
        result = evaluate_job(job)
        assert result.score == 0 or result.score == 10
        assert result.reject_reason
        assert "Non-US" in result.reason

    @pytest.mark.parametrize("location", [
        "San Francisco, CA", "New York, NY", "Remote",
        "Seattle, WA", "Austin, TX", "Boston, MA",
    ])
    def test_us_location_passes(self, location):
        job = JobPosting(url="x", title="SWE", company="Acme", location=location, description="Python")
        result = evaluate_job(job)
        assert "Non-US" not in result.reason


class TestDemoteSalary:
    """High salary with no entry signals is DEMOTED, not hard rejected.

    $130K+ is a weak seniority proxy (FAANG new-grad roles pay more), so the
    job is penalized but stays in the pool for the AI to confirm.
    """

    def test_high_salary_no_entry(self, high_salary_no_entry_signals):
        result = evaluate_job(high_salary_no_entry_signals)
        assert not result.reject_reason          # kept, not deleted
        assert "Senior salary" in result.reason  # but penalized
        assert result.score_pct < 65             # demoted below recommend bar

    def test_high_salary_with_entry_signals_passes(self):
        job = JobPosting(
            url="x", title="Software Engineer, New Grad",
            company="Stripe", location="SF, CA",
            description="Compensation: $140,000. New grad position. Python, AWS.",
        )
        result = evaluate_job(job)
        assert result.score > 0  # New Grad in title = entry signal


# ═══════════════════════════════════════════════════════════════════════════
# SCORING TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestKeywordScoring:
    """Test keyword matching against personal tech stack."""

    def test_python_match(self):
        score, hits = keyword_score("Python developer needed")
        assert score >= 10  # python = 10 points
        assert hits >= 1

    def test_full_ml_stack(self):
        text = "Python, PyTorch, machine learning, deep learning, AWS, Docker, Kubernetes, distributed systems"
        score, hits = keyword_score(text)
        assert score > 50
        assert hits > 7

    def test_no_match(self):
        score, hits = keyword_score("Marketing manager for sales team")
        assert score == 0
        assert hits == 0

    def test_case_insensitive(self):
        s1, _ = keyword_score("PYTHON developer")
        s2, _ = keyword_score("python developer")
        assert s1 == s2


class TestSynergyBonus:
    """Test synergy combo detection."""

    def test_python_fastapi_aws(self):
        bonus = synergy_bonus("Python, FastAPI, AWS Lambda")
        assert bonus >= 10

    def test_python_pytorch_aws(self):
        bonus = synergy_bonus("Python, PyTorch, AWS SageMaker")
        assert bonus >= 10

    def test_no_synergy(self):
        bonus = synergy_bonus("Java, Spring Boot, Oracle")
        assert bonus == 0

    def test_partial_combo_no_bonus(self):
        bonus = synergy_bonus("Python, FastAPI")  # Missing AWS
        assert bonus == 0


class TestLevelTag:
    """Test job level detection from title + description."""

    @pytest.mark.parametrize("title,expected", [
        ("Machine Learning Engineer – New Grad", "New Grad"),
        ("Software Engineer (New Graduate)", "New Grad"),
        ("Campus Hire - SWE", "New Grad"),
        ("Recent Grad Software Engineer", "New Grad"),
    ])
    def test_new_grad(self, title, expected):
        assert level_tag(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("Junior Software Developer", "Entry"),
        ("Entry-Level Backend Engineer", "Entry"),
        ("Associate Software Engineer", "Entry"),
        ("SDE I", "Entry"),
        ("SWE I - Platform", "Entry"),
        ("Software Engineer Intern", "Entry"),
    ])
    def test_entry(self, title, expected):
        assert level_tag(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("Software Engineer II", "Mid"),
        ("SDE II - Backend", "Mid"),
        ("SWE II", "Mid"),
    ])
    def test_mid(self, title, expected):
        assert level_tag(title) == expected

    def test_unknown(self):
        assert level_tag("Software Engineer") == "Unknown"

    def test_description_level(self):
        """Level can be detected from description if not in title."""
        assert level_tag("Software Engineer", "This is a new grad position") == "New Grad"


class TestExtractExperience:
    """Test experience range extraction from JD text."""

    @pytest.mark.parametrize("text,expected_min,expected_max", [
        ("2-4 years of experience", 2, 4),
        ("3+ years", 3, None),
        ("minimum 5 years", 5, None),
        ("at least 2 years of experience", 2, None),
        ("3 years of experience required", 3, 3),
        ("0-2 years preferred", 0, 2),
        ("1 to 3 years", 1, 3),
    ])
    def test_extraction(self, text, expected_min, expected_max):
        min_exp, max_exp = extract_experience(text)
        assert min_exp == expected_min
        assert max_exp == expected_max

    def test_no_experience_mentioned(self):
        min_exp, max_exp = extract_experience("Great team environment. Python skills needed.")
        assert min_exp is None
        assert max_exp is None


class TestExperienceScore:
    """Test experience scoring based on parsed ranges."""

    def test_sweet_spot(self):
        assert experience_score(0, 2) == 10

    def test_one_year_max(self):
        assert experience_score(0, 1) == 8

    def test_three_year_min(self):
        assert experience_score(3, None) == 6

    def test_overqualified(self):
        assert experience_score(5, None) == 0
        assert experience_score(4, 8) == 0

    def test_no_data(self):
        assert experience_score(None, None) == 0


class TestRecencyScore:
    """Test freshness scoring."""

    def test_very_fresh(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
        assert recency_score(ts) == 10

    def test_same_day(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(hours=8)).isoformat()
        assert recency_score(ts) == 8

    def test_old_posting(self):
        ts = (datetime.now(tz=timezone.utc) - timedelta(days=3)).isoformat()
        assert recency_score(ts) == -5

    def test_none(self):
        assert recency_score(None) == 0

    def test_invalid(self):
        assert recency_score("not-a-date") == 0


class TestCompetition:
    """Test competition estimate."""

    def test_big_tech(self):
        assert competition_estimate("Google", 0) >= 5

    def test_old_posting(self):
        assert competition_estimate("StartupCo", 72) >= 5

    def test_fresh_startup(self):
        assert competition_estimate("StartupCo", 2) == 0


class TestVariantSelection:
    """Test resume variant auto-selection."""

    def test_ml_variant(self):
        assert select_variant("machine learning, deep learning, pytorch, nlp") == "ml"

    def test_appdev_variant(self):
        assert select_variant("react, frontend, full-stack, vue, angular") == "appdev"

    def test_default_se(self):
        assert select_variant("python, backend, api, docker") == "se"


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — Full evaluate_job()
# ═══════════════════════════════════════════════════════════════════════════

class TestEvaluateJobIntegration:
    """End-to-end scoring with realistic job descriptions."""

    def test_perfect_new_grad_ml(self, new_grad_ml_job):
        result = evaluate_job(new_grad_ml_job, first_seen=(datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat())
        assert not result.reject_reason
        assert result.score_pct >= 50
        assert result.level == "New Grad"
        assert result.resume_variant == "ml"

    def test_entry_backend(self, entry_backend_job):
        result = evaluate_job(entry_backend_job)
        assert not result.reject_reason
        assert result.level == "Entry"

    def test_senior_rejected(self, senior_job):
        result = evaluate_job(senior_job)
        assert result.score == 0
        assert result.reject_reason

    def test_overqualified_demoted(self, overqualified_job):
        # Experience/salary are unreliable proxies — demoted, not hard rejected,
        # so the AI rescorer can rescue misreads.
        result = evaluate_job(overqualified_job)
        assert not result.reject_reason
        assert result.score_pct < 65

    def test_no_sponsorship_rejected(self, no_sponsorship_job):
        result = evaluate_job(no_sponsorship_job)
        assert result.score == 0
        assert result.reject_reason

    def test_clearance_rejected(self, clearance_job):
        result = evaluate_job(clearance_job)
        assert result.score == 0

    def test_spam_rejected(self, spam_company_job):
        result = evaluate_job(spam_company_job)
        assert result.score == 0

    def test_qa_rejected(self, qa_job):
        result = evaluate_job(qa_job)
        assert result.score == 0

    def test_non_us_rejected(self, non_us_job):
        result = evaluate_job(non_us_job)
        assert result.reject_reason

    def test_mid_level_passes_but_lower(self, mid_level_job):
        result = evaluate_job(mid_level_job)
        assert result.level == "Mid"
        # Mid jobs pass but score lower than entry/new grad
        assert result.score > 0

    def test_ambiguous_scores_reasonably(self, ambiguous_job):
        result = evaluate_job(ambiguous_job)
        assert result.level == "Unknown"
        assert result.score > 0

    def test_real_world_10a_labs(self):
        """Senior ML JD ($150-250K, 8+ yrs): demoted hard, but kept for the AI.

        Even with a strong ML stack, the experience + salary penalties push it
        well below the recommend bar — without deleting it outright.
        """
        job = JobPosting(
            url="x", title="ML Engineer", company="10a Labs",
            location="Fully remote, U.S.-based",
            description=(
                "About The role: We're looking for an experienced ML engineer. "
                "At least 3–8+ Years of Industry Experience Required. "
                "Build and deploy a multi-stage classification system. "
                "Salary Range: $150K–$250K, depending on professional experience. "
                "Python, PyTorch, LLMs, fine-tuning, OpenAI, Claude, LLaMA. "
                "AWS, GCP deployment experience required."
            ),
        )
        result = evaluate_job(job)
        assert not result.reject_reason
        assert result.score_pct < 65
        assert "Overqualified" in result.reason or "Senior salary" in result.reason

    def test_walmart_entry_level_passes(self):
        """Walmart SWE with '1+ years' should pass (not be falsely rejected)."""
        job = JobPosting(
            url="x", title="Software Engineer",
            company="Walmart", location="Bentonville, AR",
            description=(
                "Software Engineer position. 1+ years of experience preferred. "
                "Python, Java, REST APIs, microservices. "
                "Great for early career engineers."
            ),
        )
        result = evaluate_job(job)
        assert not result.reject_reason
        assert result.score > 0
