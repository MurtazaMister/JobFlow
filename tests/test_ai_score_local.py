"""Tests for local Codex AI scoring helpers."""

from types import SimpleNamespace

from scripts.ai_score_local import eligible_ai_score, fetch_json_rows, is_blocked_staffing_source


def _row(url="", company="", title="Software Engineer", location="US", desc="Python role"):
    return (url, company, title, location, desc, None)


def test_blocked_staffing_sources_match_company_and_url():
    assert is_blocked_staffing_source(_row(company="RemoteHunter"))
    assert is_blocked_staffing_source(_row(company="Quik Hire Staffing"))
    assert is_blocked_staffing_source(_row(company="Beacon FIre"))
    assert is_blocked_staffing_source(_row(company="BeaconFire Inc."))
    assert is_blocked_staffing_source(_row(company="Helic & Co."))
    assert is_blocked_staffing_source(_row(company="Helic and Co"))
    assert is_blocked_staffing_source(_row(company="Jack & Jill"))
    assert is_blocked_staffing_source(_row(company="Jack and Jill"))
    assert is_blocked_staffing_source(_row(company="Jobs Via DIce"))


def test_legitimate_company_is_not_blocked():
    assert not is_blocked_staffing_source(_row(company="Stripe", url="https://stripe.com/jobs/123"))


def test_jobright_reposts_are_allowed():
    assert not is_blocked_staffing_source(_row(company="Nutanix", url="https://jobright.ai/jobs/info/abc"))


def test_eligible_ai_score_only_allows_unscored_by_default():
    assert eligible_ai_score(None)
    assert not eligible_ai_score(0)
    assert not eligible_ai_score(7)


def test_eligible_ai_score_allows_existing_scores_when_rescore_requested():
    assert eligible_ai_score(0, rescore=True)
    assert eligible_ai_score(7, rescore=True)


def test_json_fetch_skips_scores_from_any_engine_by_default():
    args = SimpleNamespace(hours=0, limit=0, rescore=False, engine="claude")
    store = {
        "jobs": {
            "unscored": {
                "url": "https://example.com/unscored",
                "company": "Example",
                "title": "Software Engineer",
                "location": "US",
                "description_preview": "Python",
                "ai_score": None,
                "ai_model": None,
            },
            "codex-scored": {
                "url": "https://example.com/codex",
                "company": "Example",
                "title": "Software Engineer",
                "location": "US",
                "description_preview": "Python",
                "ai_score": 8,
                "ai_model": "codex",
            },
            "groq-zero": {
                "url": "https://example.com/groq",
                "company": "Example",
                "title": "Software Engineer",
                "location": "US",
                "description_preview": "Python",
                "ai_score": 0,
                "ai_model": "groq",
            },
        }
    }

    rows = fetch_json_rows(store, args)

    assert [row[6] for row in rows] == ["unscored"]


def test_json_fetch_includes_existing_scores_only_with_rescore():
    args = SimpleNamespace(hours=0, limit=0, rescore=True, engine="claude")
    store = {
        "jobs": {
            "unscored": {"url": "https://example.com/unscored", "ai_score": None},
            "codex-scored": {"url": "https://example.com/codex", "ai_score": 8, "ai_model": "codex"},
        }
    }

    rows = fetch_json_rows(store, args)

    assert {row[6] for row in rows} == {"unscored", "codex-scored"}
