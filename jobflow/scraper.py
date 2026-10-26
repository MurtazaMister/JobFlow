"""Job description parser — structures raw scraped text into a JobPosting.

This is a thin wrapper used by the `jobflow apply --paste` command. The user
pastes raw JD text, and this module wraps it into a JobPosting object with
the metadata provided via CLI flags (--title, --company, --location).

Also saves the structured JD to a text file for reference/auditing.
"""

from pathlib import Path

from .models import JobPosting


def parse_job_text(
    raw_text: str,
    url: str,
    title: str = "",
    company: str = "",
    location: str = "",
) -> JobPosting:
    """Structure raw scraped text into a JobPosting."""
    return JobPosting(
        url=url,
        title=title.strip(),
        company=company.strip(),
        location=location.strip(),
        description=raw_text.strip(),
    )


def save_job_description(job: JobPosting, output_dir: Path) -> Path:
    """Save the job description to a text file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    jd_path = output_dir / "job_description.txt"
    content = (
        f"Company: {job.company}\n"
        f"Role: {job.title}\n"
        f"Location: {job.location}\n"
        f"URL: {job.url}\n"
        f"\n{'='*60}\n\n"
        f"{job.description}"
    )
    jd_path.write_text(content, encoding="utf-8")
    return jd_path
