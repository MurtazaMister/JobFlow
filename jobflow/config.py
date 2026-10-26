"""YAML configuration loader with automatic path resolution.

Two config files exist:
    config/config.yaml     — Local development (output → data/output/)
    config/config.ci.yaml  — CI/GitHub Actions (output → data/ci/)

The JOBFLOW_CONFIG env var selects which one to use at runtime.
All paths in the config are relative to the project root (parent of
the config/ directory) and get resolved to absolute Path objects here,
so no other module needs to worry about relative path resolution.

The returned dict includes a special "_root" key pointing to the project
root, used by web/__init__.py to locate data/ci/linkedin_jobs.json.
"""

import os
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = "config/config.yaml"
FALLBACK_CONFIG_PATH = "config/config.ci.yaml"


def load_dotenv(root: Path | None = None) -> None:
    """Load simple KEY=VALUE pairs from .env without overriding shell env."""
    env_path = (root or Path.cwd()) / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path: str = "") -> dict:
    """Load and validate config, resolving all paths to absolute.

    Resolution strategy: config lives at config/config.yaml, so the project
    root is config_path.parent.parent. All relative paths in the YAML are
    resolved against this root.

    Returns a dict with Path objects for: output_dir, csv_path, resume_prompt,
    job_boards, resumes[variant], and _root.
    """
    if not path:
        path = os.environ.get("JOBFLOW_CONFIG", DEFAULT_CONFIG_PATH)
    config_path = Path(path)
    # Fall back to config.ci.yaml if the primary config is missing — keeps
    # production deploys (where personal config.yaml is gitignored) working
    # even if JOBFLOW_CONFIG isn't overridden in the host's env.
    if not config_path.exists() and Path(FALLBACK_CONFIG_PATH).exists():
        config_path = Path(FALLBACK_CONFIG_PATH)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Run 'jobflow init' first."
        )

    root = config_path.parent.parent
    load_dotenv(root)

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    required = ["resumes", "output_dir", "csv_path", "resume_prompt"]
    for key in required:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")

    # Project root = parent of config/ directory (e.g., JobFlow/)
    config["_root"] = root
    config["output_dir"] = root / config["output_dir"]
    config["csv_path"] = root / config["csv_path"]
    config["resume_prompt"] = root / config["resume_prompt"]

    if "job_boards" in config:
        config["job_boards"] = root / config["job_boards"]

    # Resolve each resume variant path (se, ml, appdev)
    for variant, rel_path in config["resumes"].items():
        config["resumes"][variant] = root / rel_path

    return config
