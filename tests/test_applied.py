"""Applied-job detection from tailored-application output folders."""

from jobflow.applied import annotate, load_index, match


def _make(tmp_path, name, source=None):
    d = tmp_path / "output" / name
    d.mkdir(parents=True)
    if source:
        (d / "jd.txt").write_text(f"Source: {source}\nFetched: 2026-10-23\n", encoding="utf-8")
    return d


def _config(tmp_path):
    return {"_root": tmp_path, "applied_dirs": ["output", "missing"]}


def test_matches_by_source_url_ignoring_query(tmp_path):
    _make(tmp_path, "2026-10-23_cursor_swe", "https://cursor.com/careers/swe?src=Linkedin")
    idx = load_index(_config(tmp_path))
    assert match({"company": "Other", "title": "Other", "url": "https://cursor.com/careers/swe"}, idx)


def test_matches_by_company_and_title_slug(tmp_path):
    _make(tmp_path, "2026-10-23_stripe_software-engineer-early-career-immediate-start")
    idx = load_index(_config(tmp_path))
    assert match({"company": "Stripe", "title": "Software Engineer, Early Career (Immediate Start)",
                  "url": "https://www.linkedin.com/jobs/view/1"}, idx)
    assert match({"company": "Stripe", "title": "Software Engineer, Infrastructure", "url": ""}, idx) is None
    assert match({"company": "Plaid", "title": "Software Engineer, Early Career (Immediate Start)", "url": ""}, idx) is None


def test_annotate_sets_flag_and_picks_up_new_folders(tmp_path):
    _make(tmp_path, "2026-10-23_nooks_software-engineer-new-grad")
    jobs = [{"company": "Nooks", "title": "Software Engineer - New Grad", "url": ""},
            {"company": "Ramp", "title": "Software Engineer", "url": ""}]
    annotate(jobs, _config(tmp_path))
    assert jobs[0]["_applied"] and jobs[1]["_applied"] is None

    _make(tmp_path, "2026-10-24_ramp_software-engineer")
    annotate(jobs, _config(tmp_path))
    assert jobs[1]["_applied"] == "2026-10-24_ramp_software-engineer"


def _make_new_format(tmp_path, name, url):
    """The current tailor layout: title line, then the posting URL bare."""
    d = tmp_path / "output" / name
    d.mkdir(parents=True)
    (d / "jd.txt").write_text(f"Acme | Engineer | SF\n{url}\n\nAbout us: https://acme.com\n",
                              encoding="utf-8")
    return d


def test_matches_bare_url_header_and_url_variants(tmp_path):
    _make_new_format(tmp_path, "2026-10-27_numeric_software-engineer-applied-ai-f2db141e",
                     "https://jobs.ashbyhq.com/numeric/f2db141e-1548-48ed-98ba-63ea0f7549e4/application")
    _make_new_format(tmp_path, "2026-10-27_trm-labs_university-grad-product-engineer-4380944856",
                     "https://www.linkedin.com/jobs/view/4380944856")
    idx = load_index(_config(tmp_path))
    assert match({"company": "Numeric", "title": "Software Engineer, Applied AI",
                  "url": "https://jobs.ashbyhq.com/numeric/f2db141e-1548-48ed-98ba-63ea0f7549e4?utm=x"}, idx)
    assert match({"company": "TRM Labs", "title": "University Grad - Product Engineer (2026-2027)",
                  "url": "https://linkedin.com/jobs/view/university-grad-at-trm-labs-4380944856/"}, idx)
    # A link in the JD body is not the posting URL.
    assert match({"company": "Acme", "title": "x", "url": "https://acme.com"}, idx) is None


def test_matches_trailing_posting_id_only_for_same_company(tmp_path):
    _make(tmp_path, "2026-10-27_sysco-labs_software-engineer-r270991")
    idx = load_index(_config(tmp_path))
    url = "https://wd5.myworkdaysite.com/recruiting/sysco/job/Colombo/Software-Engineer_R270991"
    assert match({"company": "Sysco Labs", "title": "Software Engineer", "url": url}, idx)
    assert match({"company": "Other Co", "title": "Software Engineer", "url": url}, idx) is None
    # ID-tagged folders never fall back to title matching.
    assert match({"company": "Sysco Labs", "title": "Software Engineer",
                  "url": "https://www.linkedin.com/jobs/view/99999999"}, idx) is None


def test_no_fuzzy_title_or_company_prefix_matches(tmp_path):
    _make(tmp_path, "2026-10-23_amazon_software-development-engineer")
    _make(tmp_path, "2026-10-23_meta_software-engineer")
    idx = load_index(_config(tmp_path))
    assert match({"company": "Amazon", "title": "Software Development Engineer II", "url": ""}, idx) is None
    assert match({"company": "Metabase", "title": "Software Engineer", "url": ""}, idx) is None
    assert match({"company": "Amazon", "title": "Software Development Engineer", "url": ""}, idx)


def test_folders_with_a_url_do_not_title_match(tmp_path):
    _make(tmp_path, "2026-10-23_ramp_software-engineer", "https://jobs.ashbyhq.com/ramp/11111111-1111-1111-1111-111111111111")
    idx = load_index(_config(tmp_path))
    assert match({"company": "Ramp", "title": "Software Engineer",
                  "url": "https://jobs.ashbyhq.com/ramp/22222222-2222-2222-2222-222222222222"}, idx) is None
