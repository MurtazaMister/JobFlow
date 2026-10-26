"""Resume LaTeX manipulation — merging, tailoring, and file management.

Resume tailoring is CLI-only (the web dashboard's tailor page was removed).
The workflow is `jobflow apply` → `jobflow save`:
    1. build_tailor_prompt() assembles: master_prompt + JD + base .tex
    2. User feeds this to Claude externally
    3. extract_preamble_and_education() splits base .tex at \\section{Experience}
    4. merge_resume() combines preamble with Claude's tailored sections
    5. save_tailored_resume() writes the final .tex file

The merge_resume() function handles cleanup of Claude's output:
    - Strips markdown code fences (```) that Claude sometimes adds
    - Removes markdown headers (## Section 1 — ...)
    - Strips "Company — Role" header lines
    - Collapses excessive blank lines
    - Ensures \\end{document} is present

Three resume variants exist:
    - "se"     — Software Engineering (default, backend/infra focus)
    - "ml"     — Machine Learning (PyTorch, model training, NLP)
    - "appdev" — App Development (React, full-stack, UI/UX)
"""

import re
from pathlib import Path

from .models import JobPosting


def load_base_resume(variant: str, config: dict) -> str:
    """Load the base resume .tex for the given variant (se, ml, or appdev)."""
    path = config["resumes"].get(variant)
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"Base resume not found for variant '{variant}': {path}")
    return Path(path).read_text(encoding="utf-8")


def load_master_prompt(config: dict) -> str:
    """Load the master resume editing prompt."""
    path = config["resume_prompt"]
    if not Path(path).exists():
        raise FileNotFoundError(f"Resume prompt not found: {path}")
    return Path(path).read_text(encoding="utf-8")


def extract_preamble_and_education(tex: str) -> str:
    """Extract everything from \\documentclass through end of Education section."""
    # Find the end of Education section (before Experience starts)
    match = re.search(r"(%-+EXPERIENCE-+|\\section\{Experience\})", tex)
    if match:
        return tex[:match.start()].rstrip() + "\n\n"
    # Fallback: return everything before \section{Experience}
    idx = tex.find(r"\section{Experience}")
    if idx != -1:
        return tex[:idx].rstrip() + "\n\n"
    raise ValueError("Could not find Experience section boundary in base resume")


def merge_resume(preamble: str, tailored_sections: str) -> str:
    """Merge preamble+education with tailored Experience/Projects/Skills sections."""
    # Clean up the tailored sections - remove ALL markdown artifacts
    cleaned = tailored_sections.strip()

    # Remove all markdown code fences (including mid-content ones)
    cleaned = re.sub(r"```(?:latex)?\s*\n?", "", cleaned)

    # Remove markdown section headers (## SECTION 1 — ..., etc.)
    cleaned = re.sub(r"^#{1,3}\s+.*$", "", cleaned, flags=re.MULTILINE)

    # Remove markdown horizontal rules
    cleaned = re.sub(r"^---+\s*$", "", cleaned, flags=re.MULTILINE)

    # Remove any "Company Name + Role" header line if present
    # (the master prompt outputs this before the LaTeX)
    lines = cleaned.split("\n")
    start_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("%-") or stripped.startswith("\\section{") or stripped.startswith("\\resumeSubHeadingListStart"):
            start_idx = i
            break
        # Skip lines that look like headers (e.g., "Stripe — SDE 1, New Grad")
        if not stripped.startswith("\\") and not stripped.startswith("%") and "—" in stripped:
            continue
        if stripped.startswith("\\"):
            start_idx = i
            break

    cleaned = "\n".join(lines[start_idx:]).strip()

    # Collapse excessive blank lines
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned)

    # Ensure \end{document} is present
    if r"\end{document}" not in cleaned:
        cleaned += "\n\n\n\\end{document}\n"

    return preamble + cleaned


def build_tailor_prompt(job: JobPosting, base_tex: str, master_prompt: str) -> str:
    """Assemble the full tailoring prompt for Claude."""
    return (
        f"{master_prompt}\n\n"
        f"---\n\n"
        f"## Job Description\n\n"
        f"**Company:** {job.company}\n"
        f"**Role:** {job.title}\n"
        f"**Location:** {job.location}\n\n"
        f"{job.description}\n\n"
        f"---\n\n"
        f"## My Current Resume (LaTeX)\n\n"
        f"```latex\n{base_tex}\n```\n\n"
        f"CRITICAL: The tailored resume MUST fit on exactly ONE page. "
        f"Keep bullets concise (1-2 lines max). Do NOT modify margins or font sizes."
    )


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    name = re.sub(r"[/\\:*?\"<>|,;()]+", "", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_- ")


def save_tailored_resume(tex_content: str, output_dir: Path, company: str = "", role: str = "") -> Path:
    """Save the tailored .tex file. Named Company_Role.tex if provided."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if company and role:
        filename = f"{_sanitize_filename(company)}_{_sanitize_filename(role)}.tex"
    else:
        filename = "tailored_resume.tex"

    tex_path = output_dir / filename
    tex_path.write_text(tex_content, encoding="utf-8")
    return tex_path


def make_output_dirname(company: str, role: str, date_str: str) -> str:
    """Create a sanitized output directory name."""
    name = f"{company}_{role}_{date_str}"
    return _sanitize_filename(name)
