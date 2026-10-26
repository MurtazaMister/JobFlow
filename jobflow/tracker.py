"""CSV-based application tracker — records every job the user processes.

This is a simple append-only CSV that tracks the full lifecycle of each
application: from initial discovery (date_found) through status changes
(Pending → Applied → Interview → Offer/Rejected) to final outcome.

The CSV is the source of truth for the `jobflow list` command (CLI-only;
application tracking was removed from the web dashboard). It's separate from
linkedin_jobs.json — that store tracks discovered jobs, while this CSV tracks
jobs the user has acted on.

Deduplication: Jobs are deduped by URL first, then by company+role combo
to prevent the same application from being tracked twice.

Schema migration: If new columns are added to HEADERS, _migrate_csv()
will automatically add the missing columns to existing CSV files without
data loss.
"""

import csv
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

HEADERS = [
    "company",
    "role",
    "link",
    "score",
    "variant",
    "status",
    "source",
    "resume_path",
    "date_found",
    "date_applied",
    "notes",
]

# Valid status transitions
STATUSES = ["Pending", "Skipped", "Applied", "Interview", "OA", "Rejected", "Offer", "Withdrawn"]

console = Console()


def init_csv(path: Path) -> None:
    """Create CSV with headers if it doesn't exist or is empty."""
    if path.exists() and path.stat().st_size > 0:
        # Migrate old CSV if it has fewer columns
        _migrate_csv(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(HEADERS)


def _migrate_csv(path: Path) -> None:
    """Add missing columns to an existing CSV without losing data."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_headers = reader.fieldnames or []
        rows = list(reader)

    missing = [h for h in HEADERS if h not in existing_headers]
    if not missing:
        return

    # Rewrite with all headers, filling missing fields with ""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Map old field names to new ones
            if "date" in row and "date_found" not in row:
                row["date_found"] = row.get("date", "")
            for h in HEADERS:
                row.setdefault(h, "")
            writer.writerow(row)

    console.print(f"[dim]Migrated CSV: added columns {missing}[/dim]")


def _load_links(path: Path) -> set:
    """Load all existing job links for dedup."""
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row.get("link", "").strip() for row in reader if row.get("link", "").strip()}


def is_duplicate(path: Path, link: str, company: str = "", role: str = "") -> bool:
    """Check if a job already exists in the CSV."""
    if not path.exists():
        return False

    link = link.strip()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # Match by URL first
            if link and row.get("link", "").strip() == link:
                return True
            # Fallback: match by company + role
            if (company and role
                    and row.get("company", "").strip().lower() == company.strip().lower()
                    and row.get("role", "").strip().lower() == role.strip().lower()):
                return True
    return False


def append_job(
    path: Path,
    company: str,
    role: str,
    link: str,
    score: int,
    status: str = "Pending",
    resume_path: str = "",
    variant: str = "",
    source: str = "",
    notes: str = "",
) -> bool:
    """Append a job to the CSV. Returns False if duplicate."""
    init_csv(path)

    if is_duplicate(path, link, company, role):
        console.print(f"[yellow]Duplicate skipped:[/yellow] {company} — {role}")
        return False

    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            company, role, link, score, variant, status, source,
            resume_path, date.today().isoformat(), "", notes,
        ])
    return True


def update_status(path: Path, index: int, new_status: str, notes: str = "") -> bool:
    """Update the status of a job by row index (1-based)."""
    if new_status not in STATUSES:
        console.print(f"[red]Invalid status: {new_status}. Must be one of: {', '.join(STATUSES)}[/red]")
        return False

    rows = list_jobs(path)
    if index < 1 or index > len(rows):
        console.print(f"[red]Invalid index: {index}. Must be 1-{len(rows)}[/red]")
        return False

    row = rows[index - 1]
    old_status = row.get("status", "")
    row["status"] = new_status

    if new_status == "Applied" and not row.get("date_applied"):
        row["date_applied"] = date.today().isoformat()

    if notes:
        existing = row.get("notes", "")
        row["notes"] = f"{existing}; {notes}".strip("; ") if existing else notes

    # Rewrite entire CSV
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
        writer.writeheader()
        for i, r in enumerate(rows):
            if i == index - 1:
                writer.writerow(row)
            else:
                writer.writerow(r)

    console.print(f"[green]Updated #{index}:[/green] {row['company']} — {old_status} -> {new_status}")
    return True


def list_jobs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def print_jobs(path: Path, status_filter: str = "") -> None:
    jobs = list_jobs(path)
    if not jobs:
        console.print("[yellow]No jobs tracked yet.[/yellow]")
        return

    if status_filter:
        jobs = [j for j in jobs if j.get("status", "").lower() == status_filter.lower()]
        if not jobs:
            console.print(f"[yellow]No jobs with status '{status_filter}'.[/yellow]")
            return

    table = Table(title="Tracked Applications", show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Company", style="cyan", min_width=8, no_wrap=True)
    table.add_column("Role", min_width=10)
    table.add_column("Score", justify="right", width=5)
    table.add_column("Status", width=9)
    table.add_column("Found", width=10)
    table.add_column("Applied", width=10)
    table.add_column("Notes", max_width=15)

    status_styles = {
        "Applied": "green",
        "Interview": "bold green",
        "OA": "bold cyan",
        "Offer": "bold magenta",
        "Skipped": "dim",
        "Rejected": "red",
        "Withdrawn": "dim red",
    }

    for i, job in enumerate(jobs, 1):
        status = job.get("status", "")
        style = status_styles.get(status)
        table.add_row(
            str(i),
            job.get("company", ""),
            job.get("role", ""),
            job.get("score", ""),
            status,
            job.get("date_found", ""),
            job.get("date_applied", ""),
            job.get("notes", ""),
            style=style,
        )

    console.print(table)

    # Summary counts
    counts = {}
    for j in list_jobs(path):
        s = j.get("status", "Unknown")
        counts[s] = counts.get(s, 0) + 1
    summary = " | ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    console.print(f"\n[dim]{summary} | Total: {sum(counts.values())}[/dim]")
