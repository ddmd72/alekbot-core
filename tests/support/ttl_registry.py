"""The TTL registry (TEST_COVERAGE_SYSTEMIC_RFC P22): `config/ttl_registry.json` lists every document or value under `src/`
that writes `expires_at` or `purge_after`, and who deletes it; this module loads it and finds the writers in the tree.

A writer is a dict key, a keyword argument, a subscript or attribute assignment, or a class field named like the field
(a module constant bound to the field's name counts as the name). A reader (`data.get("expires_at")`, a comparison) is not one.
"""
import ast
import json
from pathlib import Path
from typing import Dict, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "config" / "ttl_registry.json"
FIELDS = ("expires_at", "purge_after")
DELETED_BY = ("ttl-policy", "janitor", "never", "unverified", "provider", "not-stored")
Writer = Tuple[str, str]      # (path relative to the repo root, field)


def load(path: Path = REGISTRY) -> List[Dict]:
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


def writers_in_source(source: str) -> Set[str]:
    """The fields (of FIELDS) this module's source writes."""
    tree = ast.parse(source)
    aliases = {t.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
               and isinstance(node.value, ast.Constant) and node.value.value in FIELDS
               for t in node.targets if isinstance(t, ast.Name)}

    def name_of(node) -> str:
        if isinstance(node, ast.Constant) and node.value in FIELDS:
            return node.value
        if isinstance(node, ast.Name) and node.id in aliases:
            return aliases[node.id]
        return ""

    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            found.update(filter(None, (name_of(k) for k in node.keys if k is not None)))
        elif isinstance(node, ast.keyword) and node.arg in FIELDS:
            found.add(node.arg)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and name_of(target.slice):
                    found.add(name_of(target.slice))
                elif isinstance(target, ast.Attribute) and target.attr in FIELDS:
                    found.add(target.attr)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id in FIELDS:
                found.add(target.id)
            elif isinstance(target, ast.Attribute) and target.attr in FIELDS:
                found.add(target.attr)
    return found


def scan_tree(root: Path = REPO_ROOT) -> Set[Writer]:
    found: Set[Writer] = set()
    for file in sorted((root / "src").rglob("*.py")):
        text = file.read_text(encoding="utf-8")
        if not any(field in text for field in FIELDS):
            continue
        rel = file.relative_to(root).as_posix()
        found.update((rel, field) for field in writers_in_source(text))
    return found


def registered(entries: List[Dict]) -> Set[Writer]:
    return {(path, entry["field"]) for entry in entries for path in entry["writers"]}


def ephemeral_collections_in_source(source: str) -> Set[str]:
    """The `EnvironmentConfig` properties handed to `FirestoreEphemeralStore(...)` in this source; any other argument raises ValueError
    (a literal or a variable would hide a collection from the registry check)."""
    found: Set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func).endswith("FirestoreEphemeralStore")):
            continue
        arg = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "collection"), None)
        if not (isinstance(arg, ast.Attribute) and ast.unparse(arg.value) == "env_config"):
            raise ValueError(f"FirestoreEphemeralStore collection is not `env_config.<attr>`: {ast.unparse(arg) if arg else 'missing'}")
        found.add(arg.attr)
    return found


def ephemeral_store_collections(root: Path = REPO_ROOT) -> Set[str]:
    """The properties handed to every `FirestoreEphemeralStore(...)` under src/ and the root entry points."""
    found: Set[str] = set()
    for file in sorted(list((root / "src").rglob("*.py")) + list(root.glob("*.py"))):
        text = file.read_text(encoding="utf-8")
        if "FirestoreEphemeralStore(" in text:
            found |= ephemeral_collections_in_source(text)
    return found
