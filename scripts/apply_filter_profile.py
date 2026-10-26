"""Apply a tailored scoring profile from JSON to a user, then re-score their feed.

Usage:
    python scripts/apply_filter_profile.py                          # config/filter_profile.json -> operator
    python scripts/apply_filter_profile.py --file my.json --user-id 2

The JSON holds any subset of FilterProfile fields (see jobflow/filter_profile.py)
under "filter_config", plus an optional "search_terms" list (the extra LinkedIn
searches shown in Settings). Fields it omits keep their stored values, so the
Settings page's own knobs survive. Requires DATABASE_URL (reads .env).
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from jobflow.config import load_dotenv  # noqa: E402

load_dotenv(ROOT)

from jobflow import db  # noqa: E402
from jobflow.filter_profile import profile_from_config, profile_to_config  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", default=str(ROOT / "config" / "filter_profile.json"))
    parser.add_argument("--user-id", type=int, default=db.DEFAULT_USER_ID)
    args = parser.parse_args()

    # utf-8-sig: Notepad / Windows PowerShell save JSON with a BOM.
    spec = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    db.init_db()
    stored = (db.get_user_profile(args.user_id) or {}).get("filter_config") or {}
    merged = {**stored, **spec.get("filter_config", {})}
    # Round-trip through FilterProfile to validate and normalize the config.
    config = profile_to_config(profile_from_config(merged))

    fields = {"filter_config": config}
    if "search_terms" in spec:
        fields["search_terms"] = [t.strip() for t in spec["search_terms"] if t.strip()]
    db.upsert_user_profile(args.user_id, **fields)

    rescored = db.backfill_user(args.user_id)
    print(f"Applied {args.file} to user {args.user_id}; re-scored {rescored} jobs.")
    if "search_terms" in fields:
        print(f"Extra LinkedIn searches: {', '.join(fields['search_terms'])}")


if __name__ == "__main__":
    main()
