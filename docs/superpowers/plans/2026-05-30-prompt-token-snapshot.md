# Prompt Token Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a git-tracked, diffable mirror of the shared Firestore prompt layer (tokens/blueprints/profiles) so prompt changes are reviewable in git and Firestore↔git drift is visible.

**Architecture:** A pure, round-trippable serializer + planner (PyYAML only, no I/O, no `src/` imports) is fully unit-tested. A thin CLI shell (`snapshot_pull.py`) resolves collections from `EnvironmentConfig`, reads Firestore, calls the planner, and writes/diffs `prompts_snapshot/`. Firestore stays source of truth; the mirror is read-only.

**Tech Stack:** Python 3.13, PyYAML 6.0.3 (already installed), `google.cloud.firestore` sync client, pytest. Spec: `docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md`.

---

## File Structure

- **Create** `firestore_utils/snapshot_serializer.py` — pure functions: token frontmatter+body (de)serialization, blueprint/profile YAML (de)serialization, `is_pii_doc` guard, `relpath_for`, `plan_snapshot`. No I/O, only PyYAML.
- **Create** `firestore_utils/snapshot_pull.py` — CLI shell: resolves collections via `EnvironmentConfig`, fetches Firestore docs, calls `plan_snapshot`, writes/diffs `prompts_snapshot/`. The only file that does I/O.
- **Create** `prompts_snapshot/README.md` — written by the pull script (read-only-mirror notice).
- **Create** `tests/unit/firestore_utils/__init__.py` — make the test dir collectible (mirrors `tests/unit/scripts/`).
- **Create** `tests/unit/firestore_utils/test_snapshot_serializer.py` — round-trip + guard + planner tests (loads the module by path via `importlib`, the repo's established pattern for non-package tooling).
- **Create** `tests/unit/firestore_utils/test_snapshot_pull_io.py` — write/orphan/check round-trip against a `tmp_path`.

No changes to `src/`. The global `*.groovy` ignore was a defensive leftover (set when prompt leakage was a worry); it is removed because the snapshot deliberately tracks prompts. Tokens use `.groovy` (the body is groovy DSL); the gitignored scratch dirs `firestore_utils/uploads/`+`downloads/` stay ignored by their own dir-level rules.

---

### Task 1: Token serializer round-trip

**Files:**
- Create: `firestore_utils/snapshot_serializer.py`
- Create: `tests/unit/firestore_utils/__init__.py` (empty)
- Test: `tests/unit/firestore_utils/test_snapshot_serializer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/firestore_utils/__init__.py` as an empty file, then create `tests/unit/firestore_utils/test_snapshot_serializer.py`:

```python
import importlib.util
import os

# Load the non-package tooling module by path (repo's established pattern;
# see tests/unit/scripts/test_migrate_to_embedding_v2.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))  # tests/unit/firestore_utils -> repo root
_MODPATH = os.path.join(_ROOT, "firestore_utils", "snapshot_serializer.py")
_spec = importlib.util.spec_from_file_location("snapshot_serializer", _MODPATH)
ser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ser)


def test_token_round_trip_preserves_content_and_metadata():
    doc = {
        "token_id": "COGNITIVE_PROCESS_SMART",
        "category": "cognitive_process",
        "class": "COGNITIVE_PROCESS",
        "content": "cognitive_process {\n    INTENT → delegate → FORMAT\n}\n",
        "metadata": {"author": "system", "version": 3},
    }
    text = ser.token_to_file(doc)
    assert text.startswith("---\n")
    assert "cognitive_process {" in text  # body is readable, real newlines
    assert ser.token_from_file(text) == doc


def test_token_body_with_internal_delimiter_round_trips():
    doc = {
        "token_id": "X",
        "category": "c",
        "class": "C",
        "content": "line1\n---\nline2",  # body itself contains a --- line
        "metadata": {},
    }
    assert ser.token_from_file(ser.token_to_file(doc)) == doc


def test_token_to_file_drops_volatile_keys():
    doc = {"token_id": "X", "category": "c", "class": "C", "content": "x",
           "metadata": {}, "created_at": "2026-01-01", "updated_at": "2026-02-02"}
    parsed = ser.token_from_file(ser.token_to_file(doc))
    assert "created_at" not in parsed and "updated_at" not in parsed
    assert parsed["content"] == "x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_serializer.py -v`
Expected: FAIL — `FileNotFoundError` / `ModuleNotFoundError` (snapshot_serializer.py does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `firestore_utils/snapshot_serializer.py`:

```python
"""Pure, round-trippable serialization for the prompt-token git mirror.

No I/O, no Firestore, no src/ imports — only PyYAML. Three document shapes:
  token     -> YAML frontmatter (metadata) + body (content), ".groovy"
  blueprint -> plain YAML
  profile   -> plain YAML

See docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md.
"""
from __future__ import annotations

import yaml

# Server-managed fields excluded from the snapshot — they change on every Firestore
# write and would add diff noise even when prompt content is identical.
_VOLATILE_KEYS = {"created_at", "updated_at"}

_DELIM = "---"


def _strip_volatile(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in _VOLATILE_KEYS}


def token_to_file(doc: dict) -> str:
    """token doc -> 'frontmatter + body' text. `content` becomes the body; rest is frontmatter."""
    front = _strip_volatile(doc)
    body = front.pop("content", "")
    front_yaml = yaml.safe_dump(
        front, sort_keys=True, allow_unicode=True, default_flow_style=False
    ).strip()
    return f"{_DELIM}\n{front_yaml}\n{_DELIM}\n{body}"


def token_from_file(text: str) -> dict:
    """Inverse of token_to_file. Reconstructs the doc dict (minus volatile keys)."""
    if not text.startswith(f"{_DELIM}\n"):
        raise ValueError("token file missing leading frontmatter delimiter")
    after = text[len(f"{_DELIM}\n"):]
    front_yaml, sep, body = after.partition(f"\n{_DELIM}\n")
    if not sep:
        raise ValueError("token file missing closing frontmatter delimiter")
    doc = yaml.safe_load(front_yaml) or {}
    doc["content"] = body
    return doc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_serializer.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add firestore_utils/snapshot_serializer.py tests/unit/firestore_utils/__init__.py tests/unit/firestore_utils/test_snapshot_serializer.py
git commit -m "feat(prompt-snapshot): token frontmatter+body serializer (round-trippable)"
```

---

### Task 2: Blueprint + profile YAML serializer

**Files:**
- Modify: `firestore_utils/snapshot_serializer.py`
- Test: `tests/unit/firestore_utils/test_snapshot_serializer.py`

- [ ] **Step 1: Write the failing test** (append to the test file)

```python
def test_blueprint_round_trip():
    doc = {"blueprint_id": "universal_agent_v1", "outer_class": "agent",
           "class_order": ["A", "B", "C"]}
    assert ser.doc_from_yaml(ser.doc_to_yaml(doc)) == doc


def test_profile_round_trip():
    doc = {"blueprint_id": "universal_agent_v1",
           "tokens": {"COGNITIVE_PROCESS_SMART": {"order": 10, "non_overridable": True}}}
    assert ser.doc_from_yaml(ser.doc_to_yaml(doc)) == doc


def test_doc_to_yaml_drops_volatile_keys():
    doc = {"blueprint_id": "B", "outer_class": "a", "class_order": [], "updated_at": "x"}
    assert "updated_at" not in ser.doc_from_yaml(ser.doc_to_yaml(doc))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_serializer.py -k yaml_or_round_trip -v` (or run the whole file)
Expected: FAIL — `AttributeError: module ... has no attribute 'doc_to_yaml'`.

- [ ] **Step 3: Write minimal implementation** (append to `snapshot_serializer.py`)

```python
def doc_to_yaml(doc: dict) -> str:
    """blueprint/profile doc -> plain YAML text."""
    return yaml.safe_dump(
        _strip_volatile(doc), sort_keys=True, allow_unicode=True, default_flow_style=False
    )


def doc_from_yaml(text: str) -> dict:
    """Inverse of doc_to_yaml."""
    return yaml.safe_load(text) or {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_serializer.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add firestore_utils/snapshot_serializer.py tests/unit/firestore_utils/test_snapshot_serializer.py
git commit -m "feat(prompt-snapshot): blueprint/profile YAML serializer"
```

---

### Task 3: PII guard

**Files:**
- Modify: `firestore_utils/snapshot_serializer.py`
- Test: `tests/unit/firestore_utils/test_snapshot_serializer.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_pii_guard_flags_user_keyed_fields():
    assert ser.is_pii_doc("COGNITIVE_PROCESS_SMART", {"user_id": "u1", "content": "x"}) is True
    assert ser.is_pii_doc("X", {"account_id": "a1"}) is True


def test_pii_guard_flags_uuid_like_doc_id():
    assert ser.is_pii_doc("3f2504e0-4f89-41d3-9a0c-0305e82c3301", {"content": "x"}) is True
    assert ser.is_pii_doc("0123456789abcdef0123456789abcdef", {"content": "x"}) is True


def test_pii_guard_allows_named_system_tokens():
    assert ser.is_pii_doc("COGNITIVE_PROCESS_SMART",
                          {"token_id": "COGNITIVE_PROCESS_SMART", "content": "x"}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_serializer.py -k pii -v`
Expected: FAIL — `AttributeError: ... 'is_pii_doc'`.

- [ ] **Step 3: Write minimal implementation** (append to `snapshot_serializer.py`, after `_VOLATILE_KEYS`)

```python
# Presence of any of these fields marks a document as account/user-scoped (PII).
# Such documents are never written to the git mirror (SECRETS RULE).
_PII_KEYS = {"user_id", "account_id"}


def is_pii_doc(doc_id: str, doc: dict) -> bool:
    """True if the document is account/user-scoped and must not be mirrored."""
    if _PII_KEYS & set(doc.keys()):
        return True
    # Mirrored collections hold named definitions ("COGNITIVE_PROCESS_SMART").
    # A uuid / 32-hex doc id signals a user-keyed document that slipped in.
    compact = doc_id.replace("-", "").lower()
    if len(compact) >= 32 and all(c in "0123456789abcdef" for c in compact):
        return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_serializer.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add firestore_utils/snapshot_serializer.py tests/unit/firestore_utils/test_snapshot_serializer.py
git commit -m "feat(prompt-snapshot): PII guard (skip account/user-scoped docs)"
```

---

### Task 4: Path layout + plan_snapshot planner

**Files:**
- Modify: `firestore_utils/snapshot_serializer.py`
- Test: `tests/unit/firestore_utils/test_snapshot_serializer.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_relpath_layout():
    assert ser.relpath_for("tokens_system", "X") == "tokens/system/X.groovy"
    assert ser.relpath_for("tokens_user", "X") == "tokens/user/X.groovy"
    assert ser.relpath_for("blueprints", "B") == "blueprints/B.yaml"
    assert ser.relpath_for("profiles", "P") == "profiles/P.yaml"


def test_plan_snapshot_serializes_and_skips_pii():
    fetched = {
        "tokens_system": {
            "COGNITIVE_PROCESS_SMART": {"token_id": "COGNITIVE_PROCESS_SMART",
                                        "category": "c", "class": "C", "content": "x", "metadata": {}},
            "deadbeefdeadbeefdeadbeefdeadbeef": {"content": "leaked", "user_id": "u1"},  # PII
        },
        "tokens_user": {},
        "blueprints": {"universal_agent_v1": {"blueprint_id": "universal_agent_v1",
                                              "outer_class": "a", "class_order": []}},
        "profiles": {},
    }
    files, skipped = ser.plan_snapshot(fetched)
    assert "tokens/system/COGNITIVE_PROCESS_SMART.groovy" in files
    assert "blueprints/universal_agent_v1.yaml" in files
    assert "tokens_system/deadbeefdeadbeefdeadbeefdeadbeef" in skipped
    assert not any("deadbeef" in p for p in files)  # PII doc not written
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_serializer.py -k "relpath or plan_snapshot" -v`
Expected: FAIL — `AttributeError: ... 'relpath_for'`.

- [ ] **Step 3: Write minimal implementation** (append to `snapshot_serializer.py`)

```python
# Mirrored collection kinds, in stable order.
_KINDS = ("tokens_system", "tokens_user", "blueprints", "profiles")


def relpath_for(kind: str, doc_id: str) -> str:
    """Relative path under prompts_snapshot/ for a given collection kind + doc id."""
    return {
        "tokens_system": f"tokens/system/{doc_id}.groovy",
        "tokens_user": f"tokens/user/{doc_id}.groovy",
        "blueprints": f"blueprints/{doc_id}.yaml",
        "profiles": f"profiles/{doc_id}.yaml",
    }[kind]


def plan_snapshot(fetched: dict) -> tuple:
    """Pure planner.

    fetched: {kind: {doc_id: doc_dict}} for kind in _KINDS.
    Returns (files, skipped):
      files   = {relpath: file_text} for every non-PII document
      skipped = ["kind/doc_id", ...] for PII documents the guard excluded
    """
    files: dict = {}
    skipped: list = []
    for kind in _KINDS:
        for doc_id, doc in fetched.get(kind, {}).items():
            if is_pii_doc(doc_id, doc):
                skipped.append(f"{kind}/{doc_id}")
                continue
            if kind in ("tokens_system", "tokens_user"):
                files[relpath_for(kind, doc_id)] = token_to_file(doc)
            else:
                files[relpath_for(kind, doc_id)] = doc_to_yaml(doc)
    return files, skipped
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_serializer.py -v`
Expected: PASS (11 passed).

- [ ] **Step 5: Commit**

```bash
git add firestore_utils/snapshot_serializer.py tests/unit/firestore_utils/test_snapshot_serializer.py
git commit -m "feat(prompt-snapshot): path layout + plan_snapshot planner"
```

---

### Task 5: Pull CLI shell (I/O) + write/check round-trip test

**Files:**
- Create: `firestore_utils/snapshot_pull.py`
- Test: `tests/unit/firestore_utils/test_snapshot_pull_io.py`

- [ ] **Step 1: Write the CLI shell**

Create `firestore_utils/snapshot_pull.py`:

```python
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

_README = """# prompts_snapshot — read-only mirror

This tree is a **git-tracked mirror** of the shared prompt layer in Firestore.
**Firestore is the source of truth. Do not edit these files** — edits here have no effect.

Refresh: `python firestore_utils/snapshot_pull.py`
Drift check (writes nothing, exits non-zero on drift): `python firestore_utils/snapshot_pull.py --check`

Mirrored: tokens (system + user, both system-level), blueprints, profiles.
Excluded: account/user overrides (PII).
See docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md.
"""


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
            if n == "README.md" and root == base_dir:
                continue
            found.add(os.path.relpath(os.path.join(root, n), base_dir))
    return found


def write_snapshot(files: dict, base_dir: str = _SNAPSHOT_DIR) -> tuple:
    os.makedirs(base_dir, exist_ok=True)
    with open(os.path.join(base_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(_README)
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
```

- [ ] **Step 2: Write the failing I/O test**

Create `tests/unit/firestore_utils/test_snapshot_pull_io.py`:

```python
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MODPATH = os.path.join(_ROOT, "firestore_utils", "snapshot_pull.py")
_spec = importlib.util.spec_from_file_location("snapshot_pull", _MODPATH)
pull = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pull)


def test_write_then_check_is_clean(tmp_path):
    base = str(tmp_path)
    files = {"tokens/system/A.groovy": "---\nx: 1\n---\nbody", "blueprints/B.yaml": "k: v\n"}
    pull.write_snapshot(files, base_dir=base)
    assert os.path.exists(os.path.join(base, "tokens/system/A.groovy"))
    assert os.path.exists(os.path.join(base, "README.md"))
    assert pull.check_snapshot(files, base_dir=base) == []  # no drift right after write


def test_check_detects_added_removed_modified(tmp_path):
    base = str(tmp_path)
    pull.write_snapshot({"tokens/system/A.groovy": "v1", "profiles/P.yaml": "p\n"}, base_dir=base)
    drift = pull.check_snapshot(
        {"tokens/system/A.groovy": "v2", "blueprints/NEW.yaml": "n\n"}, base_dir=base
    )
    assert any(d.startswith("M tokens/system/A.groovy") for d in drift)
    assert any(d.startswith("+ blueprints/NEW.yaml") for d in drift)
    assert any(d.startswith("- profiles/P.yaml") for d in drift)


def test_write_removes_orphans(tmp_path):
    base = str(tmp_path)
    pull.write_snapshot({"tokens/system/A.groovy": "x", "tokens/system/B.groovy": "y"}, base_dir=base)
    pull.write_snapshot({"tokens/system/A.groovy": "x"}, base_dir=base)  # B gone from source
    assert not os.path.exists(os.path.join(base, "tokens/system/B.groovy"))
```

- [ ] **Step 3: Run test to verify it fails, then passes**

Run: `python -m pytest tests/unit/firestore_utils/test_snapshot_pull_io.py -v`
Expected: PASS (3 passed) — the shell from Step 1 already implements `write_snapshot`/`check_snapshot`. (If Step 1 was skipped it would fail with `ModuleNotFoundError`.)

Note: importing `snapshot_pull` pulls in `EnvironmentConfig` + `google.cloud.firestore` at module load; both are installed, so import succeeds without touching Firestore. No network call happens until `main()` runs.

- [ ] **Step 4: Run the full new test module**

Run: `python -m pytest tests/unit/firestore_utils/ -v`
Expected: PASS (14 passed total).

- [ ] **Step 5: Commit**

```bash
git add firestore_utils/snapshot_pull.py tests/unit/firestore_utils/test_snapshot_pull_io.py
git commit -m "feat(prompt-snapshot): pull CLI shell (write/check/orphan) + I/O tests"
```

---

### Task 6: First real pull + commit the initial snapshot

**Files:**
- Create (by running the script): `prompts_snapshot/**`

This task runs against the live dev Firestore (read-only) and is verified by inspection, not a unit test.

- [ ] **Step 1: Confirm environment points at dev**

Run: `python -c "from src.config.environment import EnvironmentConfig as E; c=E(); print(c.firestore_collection_prefix, c.domain_prompt_tokens_collection)"`
Expected: prints `development_ development_domain_prompt_tokens_v3` (dev prefix). If it prints an empty prefix, set `ENVIRONMENT=development` before the pull so the dev collections are mirrored.

- [ ] **Step 2: Dry drift check first (writes nothing)**

Run: `python firestore_utils/snapshot_pull.py --check`
Expected: prints the resolved collections, any `⚠️ PII guard skipped` lines, then a long list of `+ ...` drift (everything is "missing on disk" because `prompts_snapshot/` does not exist yet). Exit code 1. **Review the PII-skip lines:** they must be empty or only obviously user-keyed docs. If a named system token is being skipped, stop — the guard is too aggressive; fix `is_pii_doc` before proceeding.

- [ ] **Step 3: Verify the `_user` token premise**

Run: `python firestore_utils/download.py development_domain_prompt_tokens_v3_user --list`
Expected: a list of named token IDs (e.g. `LANG_FIXED_UK`, ...), NOT uuids / user-id-looking strings. This confirms RFC D2's premise that `_user` tokens are system-level. If the IDs look user-keyed, stop and re-scope (the guard will have skipped them anyway, but the collection should then be excluded entirely).

- [ ] **Step 4: Write the snapshot**

Run: `python firestore_utils/snapshot_pull.py`
Expected: `Wrote N files; removed 0 orphans.`

- [ ] **Step 5: Review the snapshot, then verify idempotency**

Run: `git status --short prompts_snapshot/ | head` and open 2-3 token `.groovy` files to confirm frontmatter+body reads cleanly.
Run: `python firestore_utils/snapshot_pull.py --check`
Expected: `No drift.` (exit 0) — proves the round-trip is stable.

- [ ] **Step 6: Commit the initial snapshot**

```bash
git add prompts_snapshot/
git commit -m "chore(prompt-snapshot): initial mirror of dev prompt layer"
```

---

## Self-Review

**1. Spec coverage:**
- D1 mirror (Firestore source) → Task 5/6 (read-only pull). ✓
- D2 scope + PII exclusion → `_collections` (no overrides) + `is_pii_doc` (Task 3) + Task 6 Step 3 premise check. ✓
- D3 file-per-doc → `relpath_for` (Task 4). ✓
- D4 frontmatter+body `.groovy` / YAML → Tasks 1, 2. ✓
- D5 `prompts_snapshot/` location → `_SNAPSHOT_DIR` (Task 5). ✓
- D6 config-driven env → `_collections(EnvironmentConfig())` (Task 5), Task 6 Step 1. ✓
- D7 script, no port → `firestore_utils/*.py`, no `src/` changes. ✓
- §6 pull + --check → `main()` (Task 5). ✓
- §7 PII guard mechanism → Task 3 + skip-logging in `main()`. ✓
- §8 testing (round-trip, guard) → Tasks 1-5. ✓
- §10 upload follow-up → out of scope, untouched. ✓

**2. Placeholder scan:** No TBD/TODO; every step has runnable code/commands and expected output. ✓

**3. Type consistency:** `token_to_file`/`token_from_file`, `doc_to_yaml`/`doc_from_yaml`, `is_pii_doc(doc_id, doc)`, `relpath_for(kind, doc_id)`, `plan_snapshot(fetched) -> (files, skipped)`, `write_snapshot(files, base_dir)`, `check_snapshot(files, base_dir)` — names and signatures are used identically across tasks. ✓

**Out of scope (per RFC §9):** git→Firestore push, overrides mirroring, multi-env subtrees/`--env`, CI/pre-commit wiring, cleaning the gitignored scratch dirs. The upload-path question (RFC §10) is deferred to after this lands.
