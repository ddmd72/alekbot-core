"""Pull the shared prompt layer from Firestore into the git-tracked prompts_snapshot/ mirror.

Usage:
  python firestore_utils/snapshot_pull.py            # write/refresh the snapshot
  python firestore_utils/snapshot_pull.py --check     # report drift, write nothing, exit 1 if drift

Firestore = source of truth; this writes a read-only mirror. Collections are resolved from
EnvironmentConfig (ENVIRONMENT / FIRESTORE_DATABASE env vars), so renames + dev→prod flow
through config. Account/user overrides are never mirrored (SECRETS RULE); a PII guard backstops.

See docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

from google.cloud import firestore

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)  # make `src.config...` importable when run as a script

_spec = importlib.util.spec_from_file_location(
    "snapshot_serializer", os.path.join(_HERE, "snapshot_serializer.py")
)
serializer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serializer)

from src.config.environment import EnvironmentConfig  # noqa: E402

_SNAPSHOT_DIR = os.path.join(_ROOT, "prompts_snapshot")
_DATABASE = os.environ.get("FIRESTORE_DATABASE", "us-production")

def _collections(cfg: "EnvironmentConfig") -> dict:
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


def _existing_snapshot_files(base_dir: str) -> set:
    found = set()
    for root, _dirs, names in os.walk(base_dir):
        for n in names:
            # README.md at the root is a hand-maintained doc, not a mirrored token —
            # never treat it as an orphan (the pull must not delete or overwrite it).
            if n == "README.md" and root == base_dir:
                continue
            found.add(os.path.relpath(os.path.join(root, n), base_dir))
    return found


def write_snapshot(files: dict, base_dir: str = _SNAPSHOT_DIR) -> tuple:
    os.makedirs(base_dir, exist_ok=True)
    for rel, text in files.items():
        path = os.path.join(base_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    orphans = _existing_snapshot_files(base_dir) - set(files.keys())
    for rel in orphans:
        os.remove(os.path.join(base_dir, rel))
    return set(files.keys()), orphans


def check_snapshot(files: dict, base_dir: str = _SNAPSHOT_DIR) -> list:
    drift = []
    on_disk = _existing_snapshot_files(base_dir)
    planned = set(files.keys())
    for rel in sorted(planned - on_disk):
        drift.append(f"+ {rel} (in Firestore, missing on disk)")
    for rel in sorted(on_disk - planned):
        drift.append(f"- {rel} (on disk, gone from Firestore)")
    for rel in sorted(planned & on_disk):
        with open(os.path.join(base_dir, rel), encoding="utf-8") as f:
            if f.read() != files[rel]:
                drift.append(f"M {rel}")
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror the Firestore prompt layer into prompts_snapshot/.")
    parser.add_argument("--check", action="store_true",
                        help="Report drift, write nothing, exit 1 if drift.")
    args = parser.parse_args()

    cfg = EnvironmentConfig()
    db = firestore.Client(database=_DATABASE)
    collections = _collections(cfg)
    print(f"Mirroring from database={_DATABASE!r}: {collections}")

    fetched = _fetch(db, collections)
    files, skipped = serializer.plan_snapshot(fetched)
    for s in skipped:
        print(f"  ⚠️  PII guard skipped (not mirrored): {s}")

    if args.check:
        drift = check_snapshot(files)
        if drift:
            print("DRIFT:")
            for line in drift:
                print(f"  {line}")
            return 1
        print("No drift.")
        return 0

    written, orphans = write_snapshot(files)
    print(f"Wrote {len(written)} files; removed {len(orphans)} orphans.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
