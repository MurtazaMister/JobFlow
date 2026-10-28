"""Row parsing for the jobright.ai sheet (Airtable) and recommendation feed."""

from jobflow.jobright import job_url, parse_recommendation, parse_sheet_row


def _sheet_row(**kw):
    row = {
        "Position Title": "Software Engineer I",
        "Date": "2026-10-28",
        "Apply": {"label": "👉 Apply",
                  "url": "https://jobright.ai/jobs/info/6ae24e301f9d9308370b079b?utm_source=1100"},
        "Work Model": "Hybrid",
        "Location": "Boston, MA, United States",
        "Company": "Wayfair",
        "Salary": "$120000-$140000 /yr",
        "Qualifications": "1. BS in Computer Science",
        "H1b Sponsored": "yes",
    }
    row.update(kw)
    return row


def test_sheet_row_uses_canonical_jobright_url():
    job = parse_sheet_row(_sheet_row())
    assert job.url == job_url("6ae24e301f9d9308370b079b")
    assert (job.title, job.company, job.source) == ("Software Engineer I", "Wayfair", "jobright")
    assert job.date_posted == "2026-10-28"
    assert "H-1B sponsored" in job.description and "BS in Computer Science" in job.description


def test_sheet_row_without_apply_link_is_skipped():
    assert parse_sheet_row(_sheet_row(Apply=None)) is None
    assert parse_sheet_row(_sheet_row(Company="")) is None


def test_recommendation_parses_job_and_company():
    item = {
        "jobResult": {
            "jobId": "6ae258a8de23f466ec8c55ac", "jobTitle": "Software Engineer, New Grad",
            "jobLocation": "Palo Alto, CA", "isRemote": False, "jobSeniority": "New Grad",
            "publishTime": "2026-10-28 20:18:48", "minYearsOfExperience": 0,
            "jobSummary": "Build things.", "coreResponsibilities": ["Ship code"],
        },
        "companyResult": {"companyName": "Cursor"},
    }
    job = parse_recommendation(item)
    assert job.url == job_url("6ae258a8de23f466ec8c55ac")
    assert (job.company, job.source) == ("Cursor", "jobright_rec")
    assert job.date_posted.startswith("2026-10-28T20:18:48")
    assert "Ship code" in job.description and "Minimum experience: 0 years" in job.description
    assert job.apply_url == ""


def test_recommendation_keeps_employer_link_for_jd_fetch():
    item = {"jobResult": {"jobId": "abc123", "jobTitle": "SWE",
                          "originalUrl": "https://job-boards.greenhouse.io/acme/jobs/1"},
            "companyResult": {"companyName": "Acme"}}
    assert parse_recommendation(item).apply_url == "https://job-boards.greenhouse.io/acme/jobs/1"
    item["jobResult"]["originalUrl"] = "https://jobright.ai/jobs/info/abc123"
    assert parse_recommendation(item).apply_url == ""


def test_recommendation_without_company_is_skipped():
    assert parse_recommendation({"jobResult": {"jobId": "x", "jobTitle": "SWE"}}) is None
