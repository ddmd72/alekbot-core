# Prompt Token Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build `snapshot_upload.py` — the reverse of the pull: unwind an edited `prompts_snapshot/` file via the existing round-trip serializer and upsert it to the matching Firestore token, dry-run by default, `--apply` human-only and hard-gated.

**Architecture:** A thin CLI over pure helpers. Pure/testable: `parse_file` (path → kind, doc_id, doc via serializer), `_collection_name`, `diff_doc` (field-level diff), `_confirm` (interactive hard gate). Thin I/O: `main` (Firestore get + merge-set). Reuses `snapshot_serializer.py` (already built + tested). Decisions U1–U3 in `docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md` §10.

**Tech Stack:** Python 3.13, PyYAML, `google.cloud.firestore`, pytest. Loads the sibling serializer via importlib (same pattern as `snapshot_pull.py`).

---

## File Structure

- **Create** `firestore_utils/snapshot_upload.py` — CLI + helpers (see Task 1).
- **Create** `tests/unit/firestore_utils/test_snapshot_upload.py` — unit tests for `parse_file`, `diff_doc`, `_confirm`, `_collection_name`.

No changes to `snapshot_serializer.py` or `snapshot_pull.py`.

---

### Task 1: snapshot_upload.py + unit tests

**Files:**
- Create: `firestore_utils/snapshot_upload.py`
- Test: `tests/unit/firestore_utils/test_snapshot_upload.py`

- [ ] **Step 1: Write the CLI + helpers**

Create `firestore_utils/snapshot_upload.py`:

```python
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
```

- [ ] **Step 2: Write the unit tests**

Create `tests/unit/firestore_utils/test_snapshot_upload.py`:

```python
import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))  # tests/unit/firestore_utils -> repo root
_MODPATH = os.path.join(_ROOT, "firestore_utils", "snapshot_upload.py")
_spec = importlib.util.spec_from_file_location("snapshot_upload", _MODPATH)
up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(up)


def _write(base, rel, text):
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_parse_file_token(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_SNAPSHOT_DIR", str(tmp_path))
    text = up.serializer.token_to_file(
        {"token_id": "X", "category": "c", "class": "C", "content": "body\ntext", "metadata": {}}
    )
    path = _write(str(tmp_path), "tokens/system/X.groovy", text)
    kind, doc_id, doc = up.parse_file(path)
    assert kind == "tokens_system"
    assert doc_id == "X"
    assert doc["content"] == "body\ntext"


def test_parse_file_blueprint_and_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_SNAPSHOT_DIR", str(tmp_path))
    bp = _write(str(tmp_path), "blueprints/B.yaml", up.serializer.doc_to_yaml({"blueprint_id": "B", "outer_class": "a", "class_order": []}))
    pr = _write(str(tmp_path), "profiles/P.yaml", up.serializer.doc_to_yaml({"blueprint_id": "B", "tokens": {}}))
    assert up.parse_file(bp)[0] == "blueprints"
    assert up.parse_file(pr)[0] == "profiles"


def test_parse_file_rejects_unknown_path(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_SNAPSHOT_DIR", str(tmp_path))
    bad = _write(str(tmp_path), "random/thing.txt", "x")
    with pytest.raises(ValueError):
        up.parse_file(bad)


def test_diff_doc_reports_changed_new_kept():
    current = {"content": "old", "created_at": "2026-01-01"}
    new = {"content": "new", "category": "c"}
    lines = up.diff_doc(current, new)
    assert any(l.startswith("  M content") for l in lines)
    assert any(l.startswith("  + category") for l in lines)
    assert any("created_at" in l and "merge keeps" in l for l in lines)


def test_confirm_aborts_non_interactive(monkeypatch):
    def _raise(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", _raise)
    assert up._confirm("X") is False


def test_confirm_requires_exact_id(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "X")
    assert up._confirm("X") is True
    monkeypatch.setattr("builtins.input", lambda _: "wrong")
    assert up._confirm("X") is False
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_upload.py -v`
Expected: 6 passed. Then run the whole subsystem: `python -m pytest tests/unit/firestore_utils/ -v` — expect 21 passed (12 serializer + 3 pull I/O + 6 upload).

- [ ] **Step 4: Commit**

⚠️ EXPLICIT paths only (never `git add -A`):
```bash
git add firestore_utils/snapshot_upload.py tests/unit/firestore_utils/test_snapshot_upload.py
git commit -m "feat(prompt-snapshot): upload path (dry-run diff + human-gated --apply, merge writes)"
```

---

## Self-Review

**Spec coverage (RFC §10):** U1 merge → `set(new_doc, merge=True)` (main); U2 dry-run default + field diff → `diff_doc` + dry-run branch; U3 `--apply` human-only hard gate → `_confirm` aborts on EOFError. Mechanism (unwind file → doc → upsert) → `parse_file` + serializer reuse. ✓

**Placeholder scan:** none — full code + runnable tests. ✓

**Type consistency:** `parse_file -> (kind, doc_id, doc)`, `_collection_name(cfg, kind) -> str`, `diff_doc(current, new) -> list[str]`, `_confirm(doc_id) -> bool` used consistently. ✓

**Out of scope:** `--changed` batch mode, git-authoritative GitOps, deleting/replacing the legacy `upload.py`.
