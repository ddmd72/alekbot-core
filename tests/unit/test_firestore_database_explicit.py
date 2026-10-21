"""Every Firestore client construction names its database (the live one is `us-production`, never `(default)`)."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIRESTORE_MODULES = {
    "google.cloud.firestore",
    "google.cloud.firestore_v1",
    "google.cloud.firestore_v1.client",
    "google.cloud.firestore_v1.async_client",
}
CLIENT_NAMES = {"Client", "AsyncClient"}
# The helper is where scripts get a client; its own calls pass database= through **kwargs.
HELPER = "scripts/_firestore.py"
# Index scripts queued for deletion by another task; the integrator removes these entries with the files.
PENDING_DELETION: set[str] = {
    "scripts/" + "infrastructure/" + name
    for name in (
        "create_prod_vector_indexes.py",
        "create_prod_multi_vector_indexes.sh",
        "create_hnsw_indexes.py",
        "migrate_to_hnsw_indexes.sh",
        "trigger_vector_index_creation.py",
        "create_missing_vector_index.sh",
        "create_vector_indexes_gcloud.sh",
        "create_approximate_index.py",
        "create_firestore_indexes.sh",
        "setup-email-watchdog-scheduler.sh",
    )
}

# F4 deletions in owner-run dirs: legacy-collection one-offs, deleted with their files by the integrator.
PENDING_DELETION |= {
    "scripts/" + d + "/" + n
    for d, n in (
        ("infrastructure", "backup_firestore.py"),
        ("infrastructure", "clone_dev_to_prod.py"),
        ("infrastructure", "delete_collections.py"),
        ("infrastructure", "export_facts_yaml.py"),
        ("infrastructure", "warmup_production_cache.py"),
        ("migration", "add_state_field_to_legacy_facts.py"),
        ("migration", "cleanup_old_collections.py"),
        ("migration", "create_blueprints.py"),
        ("migration", "create_default_profiles.py"),
        ("migration", "create_token_library.py"),
        ("migration", "migrate_account_id_prefix.py"),
        ("migration", "migrate_profiles_split.py"),
        ("migration", "migrate_to_firestore.py"),
        ("migration", "migrate_to_semantic_naming.py"),
        ("migration", "migrate_tokens_split.py"),
        ("migration", "migrate_visibility_v3.py"),
        ("migration", "test_prompt_assembly.py"),
        ("migration", "update_universal_blueprint_slots.py"),
    )
}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def find_violations(source: str) -> list[int]:
    """Line numbers of Firestore Client/AsyncClient calls without a `database=` keyword."""
    tree = ast.parse(source)
    module_names: set[str] = set(FIRESTORE_MODULES)  # dotted names a firestore module can be called by
    class_names: set[str] = set()  # names bound to Client / AsyncClient
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in FIRESTORE_MODULES and a.asname:
                    module_names.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if f"{node.module}.{a.name}" in FIRESTORE_MODULES:
                    module_names.add(a.asname or a.name)
                elif node.module in FIRESTORE_MODULES and a.name in CLIENT_NAMES:
                    class_names.add(a.asname or a.name)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        hit = (isinstance(f, ast.Name) and f.id in class_names) or (
            isinstance(f, ast.Attribute) and f.attr in CLIENT_NAMES and _dotted(f.value) in module_names
        )
        if hit and not any(k.arg == "database" or k.arg is None for k in node.keywords):
            bad.append(node.lineno)
    return bad


def _files() -> list[Path]:
    out = [ROOT / n for n in ("main.py", "job_main.py", "indexer_main.py", "sandbox_main.py", "relay_main.py")]
    out += list((ROOT / "src").rglob("*.py"))
    out += [p for p in (ROOT / "scripts").rglob("*.py") if p.relative_to(ROOT / "scripts").parts[0] != "memory"]
    return [p for p in out if p.exists()]


def test_every_firestore_client_names_its_database():
    offenders = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if rel == HELPER or rel in PENDING_DELETION:
            continue
        text = p.read_text(encoding="utf-8")
        try:
            offenders += [f"{rel}:{n}" for n in find_violations(text)]
        except SyntaxError:
            # an unparseable file cannot be proven clean; one that never mentions Firestore needs no proof
            if "firestore" in text.lower():
                offenders.append(f"{rel}: unparseable")
    assert not offenders, "Firestore client without database=:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "source",
    [
        "from google.cloud import firestore\ndb = firestore.AsyncClient(project='p')",
        "from google.cloud import firestore as fs\ndb = fs.Client()",
        "from google.cloud.firestore import AsyncClient\ndb = AsyncClient()",
        "from google.cloud.firestore_v1 import Client as C\ndb = C(project='p')",
        "from google.cloud.firestore_v1.async_client import AsyncClient\ndb = AsyncClient(project='p')",
        "import google.cloud.firestore\ndb = google.cloud.firestore.Client()",
    ],
)
def test_scanner_catches_missing_database(source):
    assert find_violations(source) == [2]


@pytest.mark.parametrize(
    "source",
    [
        "from google.cloud import firestore\ndb = firestore.AsyncClient(project='p', database='x')",
        "from google.cloud.firestore import AsyncClient\ndb = AsyncClient(**kw)",
        "import httpx\nc = httpx.AsyncClient()",
        "from google.cloud import bigquery\nc = bigquery.Client()",
        "from google.cloud.tasks_v2 import CloudTasksAsyncClient\nc = CloudTasksAsyncClient()",
        "from google.cloud import firestore\ns = 'firestore.AsyncClient()'  # firestore.Client()",
    ],
)
def test_scanner_ignores_compliant_and_foreign_clients(source):
    assert find_violations(source) == []


def test_helper_default_matches_environment_config(monkeypatch):
    import scripts._firestore as helper
    from src.config.environment import EnvironmentConfig

    monkeypatch.delenv("FIRESTORE_DATABASE", raising=False)
    assert helper._kwargs()["database"] == EnvironmentConfig().firestore_database_id == "us-production"
    monkeypatch.setenv("FIRESTORE_DATABASE", "other")
    assert helper._kwargs()["database"] == "other"
