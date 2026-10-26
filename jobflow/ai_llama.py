"""Local open-weight scoring — runs a GGUF model (default: Gemma 4 26B-A4B) in a
llama.cpp `llama-server` on this machine and scores batches over its
OpenAI-compatible HTTP API on 127.0.0.1. Nothing leaves the machine.

Configured by env (.env):
    LLAMA_SERVER_PATH   path to llama-server(.exe)   (else PATH, else ~/tools/llama.cpp/)
    LLAMA_MODEL_PATH    path to the .gguf model      (else ~/models/<DEFAULT_MODEL_FILE>)
    LLAMA_PORT          default 8090
    LLAMA_CTX           context window, default 16384
    LLAMA_SERVER_ARGS   extra llama-server args, e.g. "--n-cpu-moe 8"

Output is grammar-constrained to the exact JSON array the shared prompt asks
for, so a batch can't come back as unparseable prose. Benchmarked against the
Claude scores in scripts/bench_local_models.py.
"""

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import requests

from .ai_prompt import BATCH_PROMPT, build_jobs_block, parse_scores

DEFAULT_MODEL_FILE = "gemma-4-26B-A4B-it-UD-IQ4_XS.gguf"
# Smaller batches than the CLI engines: a ~13 GB model reads 5 long JDs far more
# reliably than 15, and a batch stays well inside the context window.
BATCH_SIZE = 5
# Same cap the Claude scores were made with (jd_fetch.MAX_DESC_LEN); requirement
# lines often sit late in long JDs, so cutting lower costs accuracy.
DESC_CAP = 6000
LOAD_TIMEOUT = 900      # loading ~13 GB off disk can take minutes on a cold cache
REQUEST_TIMEOUT = 900

# Per-family chat-template switches: no long hidden reasoning for a batch screener.
_TEMPLATE_KWARGS = {
    "gpt-oss": {"reasoning_effort": "low"},
    "qwen": {"enable_thinking": False},
    "gemma": {"enable_thinking": False},
}


class LlamaError(RuntimeError):
    pass


def server_path() -> str | None:
    env = os.environ.get("LLAMA_SERVER_PATH", "").strip()
    if env:
        return env if Path(env).is_file() else None
    found = shutil.which("llama-server")
    if found:
        return found
    for name in ("llama-server.exe", "llama-server"):
        p = Path.home() / "tools" / "llama.cpp" / name
        if p.is_file():
            return str(p)
    return None


def model_path() -> str | None:
    p = Path(os.environ.get("LLAMA_MODEL_PATH", "").strip()
             or Path.home() / "models" / DEFAULT_MODEL_FILE)
    return str(p) if p.is_file() else None


def missing_setup() -> str | None:
    """Human-readable reason the local model can't run, or None when it can."""
    if not server_path():
        return ("llama-server not found. Install llama.cpp and set LLAMA_SERVER_PATH in .env.")
    if not model_path():
        return (f"Model file not found. Download {DEFAULT_MODEL_FILE} and set LLAMA_MODEL_PATH in .env.")
    return None


def _port() -> int:
    return int(os.environ.get("LLAMA_PORT", "8090"))


def _base() -> str:
    return f"http://127.0.0.1:{_port()}"


def _healthy() -> bool:
    try:
        return requests.get(f"{_base()}/health", timeout=2).status_code == 200
    except requests.RequestException:
        return False


def model_label() -> str:
    """Short model name for logs, e.g. 'gemma-4-26B-A4B-it-UD-IQ4_XS'."""
    return Path(model_path() or DEFAULT_MODEL_FILE).stem


class LlamaServer:
    """Context manager: reuse a llama-server already listening on LLAMA_PORT,
    otherwise start one for the run and stop it afterwards (frees ~13 GB RAM)."""

    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def __enter__(self):
        if _healthy():
            print(f"Using the local model server already running on port {_port()}")
            return self
        problem = missing_setup()
        if problem:
            raise LlamaError(problem)
        cmd = [server_path(), "-m", model_path(), "--port", str(_port()),
               "--host", "127.0.0.1", "-c", os.environ.get("LLAMA_CTX", "16384"),
               "-ngl", "99", "--jinja", "-np", "1", "--no-webui",
               *shlex.split(os.environ.get("LLAMA_SERVER_ARGS", ""))]
        out = open(self.log_path, "w", encoding="utf-8") if self.log_path else subprocess.DEVNULL
        print(f"Loading local model {model_label()} (this takes a minute)…")
        self.proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
        deadline = time.time() + LOAD_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise LlamaError("llama-server exited while loading the model — usually not enough "
                                 "free RAM. Close other apps and try again"
                                 + (f" (log: {self.log_path})" if self.log_path else "."))
            if _healthy():
                print("Local model ready.")
                return self
            time.sleep(2)
        self.__exit__(None, None, None)
        raise LlamaError("llama-server did not become ready in time.")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        return False


def _batch_schema(k: int) -> dict:
    """Exactly k {id, score 0-10, reason} objects — enforced as a grammar."""
    return {
        "type": "array", "minItems": k, "maxItems": k,
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "score": {"type": "integer", "minimum": 0, "maximum": 10},
                "reason": {"type": "string", "maxLength": 300},
            },
            "required": ["id", "score", "reason"],
        },
    }


def score_batch_with_llama(batch, profile: str):
    """Score a batch with the running llama-server. Same contract as the CLI
    scorers: list of {id, score, reason} dicts, or None on failure."""
    rows = [(url, company, title, location, (desc or "")[:DESC_CAP])
            for url, company, title, location, desc, *_ in batch]
    prompt = BATCH_PROMPT.format(profile=profile, jobs_block=build_jobs_block(rows))
    name = Path(model_path() or DEFAULT_MODEL_FILE).name.lower()
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 400 * len(batch) + 1024,
        "response_format": {"type": "json_schema",
                            "json_schema": {"schema": _batch_schema(len(batch))}},
        "chat_template_kwargs": next((kw for fam, kw in _TEMPLATE_KWARGS.items() if fam in name), {}),
    }
    try:
        resp = requests.post(f"{_base()}/v1/chat/completions", json=body, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"].get("content") or ""
        return parse_scores(text)
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"  Batch error: local model — {e}")
        return None
