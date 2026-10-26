"""pdflatex compilation wrapper — compiles .tex files to PDF.

Used by the CLI resume workflow (`jobflow save`) to compile a tailored resume
into a PDF. Runs pdflatex twice to resolve cross-references (page numbers,
hyperlinks). Resume tailoring is CLI-only — the web dashboard's tailor page
was removed.

Requirements:
    - pdflatex must be on PATH (from MacTeX on macOS, texlive on Linux).
      If it's missing, compile_pdf() returns None and the caller keeps the
      .tex file.
"""

import shutil
import subprocess
from pathlib import Path


def check_pdflatex() -> bool:
    """Check if pdflatex is available on PATH."""
    return shutil.which("pdflatex") is not None


def compile_pdf(tex_path: Path, final_name: str = "") -> Path | None:
    """Compile a .tex file to PDF using pdflatex.

    Args:
        tex_path: Path to the .tex file.
        final_name: If provided, rename the output PDF to this (e.g. "Stripe_SDE1.pdf").

    Returns PDF path or None on failure.
    """
    if not check_pdflatex():
        return None

    output_dir = tex_path.parent

    # Run pdflatex twice for proper cross-references
    # Use just the filename since cwd is set to output_dir
    for _ in range(2):
        result = subprocess.run(
            [
                "pdflatex",
                "-interaction=nonstopmode",
                tex_path.name,
            ],
            capture_output=True,
            timeout=30,
            cwd=str(output_dir),
        )

    pdf_path = tex_path.with_suffix(".pdf")
    if pdf_path.exists():
        # Clean up auxiliary files
        for ext in [".aux", ".log", ".out"]:
            aux = tex_path.with_suffix(ext)
            if aux.exists():
                aux.unlink()

        # Rename to final name if provided and different
        if final_name and not final_name.endswith(".pdf"):
            final_name += ".pdf"
        if final_name and pdf_path.name != final_name:
            final_path = output_dir / final_name
            pdf_path.rename(final_path)
            return final_path

        return pdf_path

    # If compilation failed, print the error
    if result.returncode != 0:
        log_path = tex_path.with_suffix(".log")
        if log_path.exists():
            log_content = log_path.read_text(encoding="utf-8", errors="replace")
            errors = [l for l in log_content.split("\n") if l.startswith("!")]
            if errors:
                print("LaTeX errors:\n" + "\n".join(errors[:5]))

    return None
