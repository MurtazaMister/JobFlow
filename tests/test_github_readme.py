"""README-table parsing for the GitHub new-grad repos (Simplify, SpeedyApply, Jobright)."""

from datetime import datetime, timedelta, timezone

from jobflow.scanner import _parse_age, _parse_github_readme

SIMPLIFY = """
<table><tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/Amazon?utm_source=GHList">🔥 Amazon</a></strong></td>
<td>Software Development Engineer I</td>
<td><details><summary><strong>2 locations</strong></summary>Seattle, WA</br>Austin, TX</details></td>
<td><div align="center"><a href="https://amazon.jobs/en/jobs/1/sde?utm_source=Simplify&ref=Simplify"><img alt="Apply"></a> <a href="https://simplify.jobs/p/abc?utm_source=GHList"><img alt="Simplify"></a></div></td>
<td>1d</td>
</tr>
<tr>
<td>↳</td>
<td>Software Engineer, New Grad</td>
<td>NYC</td>
<td><div align="center"><a href="https://amazon.jobs/en/jobs/2/swe"><img alt="Apply"></a></div></td>
<td>2d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Old">OldCo</a></strong></td>
<td>Software Engineer</td>
<td>Boston, MA</td>
<td><a href="https://oldco.com/jobs/9">Apply</a></td>
<td>2mo</td>
</tr>
<tr>
<td><strong>ClosedCo</strong></td>
<td>Software Engineer</td>
<td>Boston, MA</td>
<td>🔒</td>
<td>0d</td>
</tr>
</tbody></table>
"""

SPEEDY = """
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://www.twitch.tv"><strong>Twitch</strong></a> | Software Engineer I | Seattle, WA | $193k/yr | <a href="https://job-boards.greenhouse.io/twitch/jobs/8906750002"><img alt="Apply"/></a> | 3d |
| <a href="https://x.com"><strong>X</strong></a> | Product Manager | Austin, TX | $100k/yr | <a href="https://x.com/jobs/1"><img alt="Apply"/></a> | 1d |
"""


def test_simplify_rows_carry_company_and_skip_old_and_closed():
    jobs = _parse_github_readme(SIMPLIFY, [], set(), source="simplify")
    assert [(j.company, j.title) for j in jobs] == [
        ("Amazon", "Software Development Engineer I"),
        ("Amazon", "Software Engineer, New Grad"),
    ]
    assert jobs[0].location == "Seattle, WA, Austin, TX"
    assert jobs[0].url == "https://amazon.jobs/en/jobs/1/sde"
    assert jobs[0].source == "simplify"
    assert jobs[0].date_posted


def test_speedyapply_markdown_table_filters_non_swe():
    jobs = _parse_github_readme(SPEEDY, [], set(), source="speedyapply")
    assert len(jobs) == 1
    assert jobs[0].company == "Twitch"
    assert jobs[0].url == "https://job-boards.greenhouse.io/twitch/jobs/8906750002"


def test_dedup_across_repos_by_url():
    seen: set = set()
    _parse_github_readme(SPEEDY, [], seen)
    assert _parse_github_readme(SPEEDY, [], seen) == []


def test_parse_age_formats():
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    assert _parse_age("3d", now) == now - timedelta(days=3)
    assert _parse_age("2mo", now) == now - timedelta(days=60)
    assert _parse_age("Oct 24", now).day == 24
    assert _parse_age("Dec 30", now).year == 2025
    assert _parse_age("Apply", now) is None
