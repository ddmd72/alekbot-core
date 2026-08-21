"""Dump the full Firestore prompt layer (tokens + blueprints + profiles) to one JSON file
in scripts/memory/ (gitignored, PII-safe) as a restore point before a risky bulk push.

Usage:
  python firestore_utils/backup_prompt_layer.py

See prompts_snapshot/README.md "Backup before bulk pushes".
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from google.cloud import firestore

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from src.config.environment import EnvironmentConfig  # noqa: E402

_DATABASE = os.environ.get("FIRESTORE_DATABASE", "us-production")
_OUT_DIR = os.path.join(_ROOT, "scripts", "memory")


def _collections(cfg: EnvironmentConfig) -> dict:
    base = cfg.domain_prompt_tokens_collection
    return {
        "tokens_system": f"{base}_system",
        "tokens_user": f"{base}_user",
        "blueprints": cfg.domain_prompt_blueprints_collection,
        "profiles": cfg.domain_prompt_profiles_collection,
    }


def _fetch(db, collections: dict) -> dict:
    return {kind: {d.id: d.to_dict() for d in db.collection(name).stream()}
            for kind, name in collections.items()}


def main() -> int:
    cfg = EnvironmentConfig()
    db = firestore.Client(database=_DATABASE)
    collections = _collections(cfg)
    print(f"Backing up from database={_DATABASE!r}: {collections}")

    data = _fetch(db, collections)
    counts = {k: len(v) for k, v in data.items()}
    print("Doc counts:", counts)

    os.makedirs(_OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(_OUT_DIR, f"prompt_tokens_firestore_backup_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
