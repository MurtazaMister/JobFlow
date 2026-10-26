"""notify.careers scanner: Typesense hit parsing and search-key extraction (offline)."""

from jobflow.notify_careers import _KEY_RE, parse_hit

DOC = {
    "company_name": "Nordstrom",
    "title": "Engineer 1: POS+ (Hybrid - Seattle, WA)",
    "url": "https://nordstrom.wd501.myworkdayjobs.com/nordstrom_careers/job/Seattle-WA/R-891900",
    "locations": ["Washington, United States", "Seattle, Washington, United States"],
    "countries": ["United States"],
    "experience_levels": ["Junior", "Mid Level"],
    "posted": 1793034705.2,
    "salary_min": 90000, "salary_max": 120000, "salary_period": "YEAR",
}


def test_parse_hit():
    job = parse_hit(DOC)
    assert job.company == "Nordstrom"
    assert job.location == "Seattle, Washington, United States"
    assert job.source == "notify"
    assert job.date_posted.startswith("2026-10-26")
    assert "$90,000–$120,000/yr" in job.description
    assert "Junior, Mid Level" in job.description


def test_parse_hit_needs_url_title_company():
    assert parse_hit({**DOC, "url": ""}) is None


def test_key_regex_matches_bundle_config():
    js = ('x={apiKey:"AbC123abc456DEF789ghi012JKL",nodes:[{host:"search.notify.careers",'
          'protocol:"https",port:443}],cacheSearchResultsForSeconds:300}')
    assert _KEY_RE.search(js).groups() == ("AbC123abc456DEF789ghi012JKL", "search.notify.careers", "https", "443")
