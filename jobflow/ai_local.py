"""Local scoring engines (no API key): a local open-weight model via llama.cpp
(`gemma`, the default — see ai_llama.py) or the signed-in `claude` / `codex` CLI
over a subprocess. Shared by the batch script (scripts/ai_score_local.py) and
the `jobflow score` client so a friend's local machine scores identically to
the operator's.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ai_llama
from .ai_prompt import BATCH_PROMPT, BATCH_SIZE, build_jobs_block, parse_scores

CLI_TIMEOUT = 300


def score_batch_with_claude(batch, profile: str):
    """Score a batch using one signed-in `claude -p` (headless) call."""
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=build_jobs_block(batch))
    claude = shutil.which("claude")
    if not claude:
        print("  Batch error: claude CLI not found on PATH")
        return None
    try:
        # Resolved path + explicit UTF-8: on Windows a bare name can't launch
        # .cmd shims, and the default pipe encoding (cp1252) mangles JDs.
        result = subprocess.run(
            [claude, "-p", "--output-format", "text", "--allowedTools", ""],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=CLI_TIMEOUT,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"  Batch error: claude exited with code {result.returncode}: {err[:500]}")
            return None
        return parse_scores(result.stdout)
    except Exception as e:
        print(f"  Batch error: {e}")
        return None


_codex_flag_cache: dict[str, bool] = {}


def _codex_supports(codex: str, flag: str) -> bool:
    """Whether this codex build's `exec` accepts ``flag`` (older CLIs reject
    unknown flags outright, failing every batch)."""
    if flag not in _codex_flag_cache:
        try:
            out = subprocess.run(
                [codex, "exec", "--help"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            _codex_flag_cache[flag] = flag in (out.stdout or "") + (out.stderr or "")
        except Exception:
            _codex_flag_cache[flag] = False
    return _codex_flag_cache[flag]


def score_batch_with_codex(batch, profile: str, cwd: str | None = None):
    """Score a batch using one signed-in `codex exec` call."""
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=build_jobs_block(batch))
    codex = shutil.which("codex")
    if not codex:
        print("  Batch error: codex CLI not found on PATH")
        return None
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="jobflow_codex_score_", suffix=".txt", delete=False,
        ) as tmp:
            output_path = Path(tmp.name)
        result = subprocess.run(
            [
                codex, "exec",
                "--cd", str(cwd or os.getcwd()),
                "--sandbox", "read-only",
                *(["--ignore-rules"] if _codex_supports(codex, "--ignore-rules") else []),
                "--output-last-message", str(output_path),
                "-",
            ],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=CLI_TIMEOUT,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"  Batch error: codex exited with code {result.returncode}: {err[:500]}")
            return None
        text = ""
        if output_path and output_path.exists():
            text = output_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            text = result.stdout
        return parse_scores(text)
    except Exception as e:
        print(f"  Batch error: {e}")
        return None
    finally:
        if output_path:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass


ENGINES = ("gemma", "claude", "codex")
DEFAULT_ENGINE = "gemma"


def batch_size(engine: str) -> int:
    """Jobs per call: the local model gets smaller batches than the CLIs."""
    return ai_llama.BATCH_SIZE if engine == "gemma" else BATCH_SIZE


def score_batch(batch, profile: str, engine: str, cwd: str | None = None):
    """Dispatch a batch to the selected local engine."""
    if engine == "gemma":
        return ai_llama.score_batch_with_llama(batch, profile)
    if engine == "codex":
        return score_batch_with_codex(batch, profile, cwd=cwd)
    return score_batch_with_claude(batch, profile)
