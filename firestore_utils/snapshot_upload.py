"""Push edited prompt-snapshot files back to Firestore (the reverse of snapshot_pull.py).

Usage:
  python firestore_utils/snapshot_upload.py <file>...           # dry-run: per-field diff, no write
  python firestore_utils/snapshot_upload.py --apply <file>...    # write (human-only, interactive confirm)

Each <file> is a path under prompts_snapshot/. The script unwinds it via the round-trip serializer,
derives the target collection + doc id from the path layout, and upserts with merge=True (so fields
not present in the snapshot — e.g. server timestamps stripped on pull — are preserved).

⚠️ --apply is human-only. It requires an interactive confirmation; non-interactive shells abort
without writing. AI must only ever run the dry-run.

See docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md §10.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

from google.cloud import firestore

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location(
    "snapshot_serializer", os.path.join(_HERE, "snapshot_serializer.py")
)
serializer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serializer)

from src.config.environment import EnvironmentConfig  # noqa: E402

_SNAPSHOT_DIR = os.path.join(_ROOT, "prompts_snapshot")
_DATABASE = os.environ.get("FIRESTORE_DATABASE", "us-production")


def parse_file(path: str) -> tuple[str, str, dict]:
    """Snapshot file path -> (kind, doc_id, doc_dict). Inverse of the pull's layout + serializer."""
    rel = os.path.relpath(os.path.abspath(path), _SNAPSHOT_DIR)
    parts = rel.split(os.sep)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if parts[0] == "tokens" and len(parts) == 3 and parts[2].endswith(".groovy"):
        kind = f"tokens_{parts[1]}"          # tokens_system | tokens_user
        doc_id = parts[2][: -len(".groovy")]
        doc = serializer.token_from_file(text)
    elif parts[0] == "blueprints" and len(parts) == 2 and parts[1].endswith(".yaml"):
        kind = "blueprints"
        doc_id = parts[1][: -len(".yaml")]
        doc = serializer.doc_from_yaml(text)
    elif parts[0] == "profiles" and len(parts) == 2 and parts[1].endswith(".yaml"):
        kind = "profiles"
        doc_id = parts[1][: -len(".yaml")]
        doc = serializer.doc_from_yaml(text)
    else:
        raise ValueError(f"path is not under a recognized snapshot collection: {rel}")
    return kind, doc_id, doc


def _collection_name(cfg: "EnvironmentConfig", kind: str) -> str:
    base = cfg.domain_prompt_tokens_collection
    return {
        "tokens_system": f"{base}_system",
        "tokens_user": f"{base}_user",
        "blueprints": cfg.domain_prompt_blueprints_collection,
        "profiles": cfg.domain_prompt_profiles_collection,
    }[kind]


def diff_doc(current: dict, new: dict) -> list[str]:
    """Per-field diff describing what `set(new, merge=True)` would change in Firestore."""
    lines: list[str] = []
    for key in sorted(set(new) | set(current)):
        if key not in current:
            lines.append(f"  + {key}: (new) {new[key]!r}")
        elif key not in new:
            lines.append(f"  ~ {key}: not in file; merge keeps the Firestore value")
        elif current[key] != new[key]:
            lines.append(f"  M {key}:\n      - {current[key]!r}\n      + {new[key]!r}")
    return lines


def _confirm(doc_id: str) -> bool:
    """Human-only hard gate: require typing the exact doc id. Aborts in non-interactive shells."""
    try:
        answer = input(f"Type the doc id '{doc_id}' to APPLY this write (anything else skips): ")
    except EOFError:
        print("  ✗ non-interactive shell — --apply aborted (no write).")
        return False
    return answer.strip() == doc_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Push snapshot files back to Firestore (reverse of pull).")
    parser.add_argument("files", nargs="+", help="snapshot file paths under prompts_snapshot/")
    parser.add_argument("--apply", action="store_true",
                        help="WRITE to Firestore (human-only; interactive confirmation required).")
    args = parser.parse_args()

    cfg = EnvironmentConfig()
    db = firestore.Client(database=_DATABASE)

    for path in args.files:
        kind, doc_id, new_doc = parse_file(path)
        collection = _collection_name(cfg, kind)
        snap = db.collection(collection).document(doc_id).get()
        current = snap.to_dict() if snap.exists else {}
        print(f"\n=== {collection}/{doc_id} (from {path}) ===")
        lines = diff_doc(current, new_doc)
        if not lines:
            print("  (no changes)")
            continue
        for line in lines:
            print(line)
        if not args.apply:
            print("  [dry-run] no write.")
            continue
        if _confirm(doc_id):
            db.collection(collection).document(doc_id).set(new_doc, merge=True)
            print(f"  ✓ applied (merge) to {collection}/{doc_id}")
        else:
            print("  skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
