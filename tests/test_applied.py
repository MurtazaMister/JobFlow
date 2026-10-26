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
