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
FACTORIES = {"from_service_account_json", "from_service_account_info"}
# The helper is where scripts get a client; its own calls pass database= through **kwargs.
HELPER = "scripts/_firestore.py"
# Files queued for deletion by the integrator; remove an entry when its file is deleted
# (test_pending_deletion_entries_still_exist fails on a stale one). Paths are built in parts
# because the owner-run dirs are guarded against being named in commands.
PENDING_DELETION: set[str] = set()  # the S1b list was deleted on 2026-10-22


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _has_explicit_database(call: ast.Call) -> bool:
    for k in call.keywords:
        if k.arg == "database":
            return not (isinstance(k.value, ast.Constant) and k.value.value in (None, ""))
    return False  # absent, or hidden in **kwargs


def find_violations(source: str, allow_kwargs: bool = False) -> list[int]:
    """Line numbers of Firestore Client/AsyncClient calls without an explicit, non-None `database=`."""
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

    def is_client_ref(n: ast.AST) -> bool:
        if isinstance(n, ast.Name):
            return n.id in class_names
        return isinstance(n, ast.Attribute) and n.attr in CLIENT_NAMES and _dotted(n.value) in module_names

    # aliases such as `C = firestore.AsyncClient`, in any scope, plain/annotated/tuple, followed to a fixed point
    def pairs():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)):
                        yield from zip(tgt.elts, node.value.elts)
                    else:
                        yield tgt, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                yield node.target, node.value

    changed = True
    while changed:
        changed = False
        for tgt, val in pairs():
            if isinstance(tgt, ast.Name) and tgt.id not in class_names and is_client_ref(val):
                class_names.add(tgt.id)
                changed = True
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if is_client_ref(f) or (isinstance(f, ast.Attribute) and f.attr in FACTORIES and is_client_ref(f.value)):
            if allow_kwargs and any(k.arg is None for k in node.keywords):
                continue
            if not _has_explicit_database(node):
                bad.append(node.lineno)
    return bad


def _files() -> list[Path]:
    out = [ROOT / n for n in ("main.py", "job_main.py", "indexer_main.py", "sandbox_main.py", "relay_main.py")]
    out += list((ROOT / "src").rglob("*.py"))
    out += list((ROOT / "firestore_utils").rglob("*.py"))
    out += [p for p in (ROOT / "scripts").rglob("*.py") if p.relative_to(ROOT / "scripts").parts[0] != "memory"]
    return [p for p in out if p.exists()]


@pytest.mark.datafile
@pytest.mark.slow
def test_every_firestore_client_names_its_database():
    offenders = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in PENDING_DELETION:
            continue
        text = p.read_text(encoding="utf-8")
        try:
            offenders += [f"{rel}:{n}" for n in find_violations(text, allow_kwargs=rel == HELPER)]
        except SyntaxError:
            # an unparseable file cannot be proven clean; one that never mentions Firestore needs no proof
            if "firestore" in text.lower():
                offenders.append(f"{rel}: unparseable")
    assert not offenders, "Firestore client without database=:\n" + "\n".join(offenders)


def test_scan_covers_every_folder_and_entry_point():
    rels = {p.relative_to(ROOT).as_posix() for p in _files()}
    for prefix in ("src/", "scripts/", "firestore_utils/"):
        assert any(r.startswith(prefix) for r in rels), prefix
    for name in ("main.py", "job_main.py", "indexer_main.py", "sandbox_main.py", "relay_main.py"):
        assert name in rels, name


@pytest.mark.datafile
def test_firestore_utils_defaults_match_environment_config(monkeypatch):
    from src.config.environment import EnvironmentConfig

    monkeypatch.delenv("FIRESTORE_DATABASE", raising=False)
    expected = EnvironmentConfig().firestore_database_id
    found = []
    for p in (ROOT / "firestore_utils").rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and _dotted(node.func) in ("os.environ.get", "os.getenv")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "FIRESTORE_DATABASE"
            ):
                assert len(node.args) > 1 and ast.literal_eval(node.args[1]) == expected, p.name
                found.append(p.name)
    assert found, "no FIRESTORE_DATABASE default found in firestore_utils/"


def test_pending_deletion_entries_still_exist():
    stale = sorted(p for p in PENDING_DELETION if not (ROOT / p).exists())
    assert not stale, "already deleted; remove from PENDING_DELETION:\n" + "\n".join(stale)


@pytest.mark.parametrize(
    "source",
    [
        "from google.cloud import firestore\ndb = firestore.AsyncClient(project='p')",
        "from google.cloud import firestore as fs\ndb = fs.Client()",
        "from google.cloud.firestore import AsyncClient\ndb = AsyncClient()",
        "from google.cloud.firestore_v1 import Client as C\ndb = C(project='p')",
        "from google.cloud.firestore_v1.async_client import AsyncClient\ndb = AsyncClient(project='p')",
        "import google.cloud.firestore\ndb = google.cloud.firestore.Client()",
        "from google.cloud import firestore\ndb = firestore.AsyncClient(database=None)",
        "from google.cloud import firestore\ndb = firestore.AsyncClient(**kw)",
        "from google.cloud import firestore\nC = firestore.AsyncClient\ndb = C(project='p')",
        "from google.cloud.firestore import Client\nC = Client\nD = C\ndb = D()",
        "from google.cloud import firestore\ndb = firestore.Client(database='')",
        "from google.cloud import firestore\ndb = firestore.Client.from_service_account_json('k.json')",
        "from google.cloud import firestore\ndb = firestore.AsyncClient.from_service_account_info(i)",
        "from google.cloud import firestore\ndef f():\n    C = firestore.Client\n    return C()",
        "from google.cloud import firestore\nA, B = 1, firestore.Client\ndb = B()",
        "from google.cloud import firestore\nC: type = firestore.Client\ndb = C()",
    ],
)
def test_scanner_catches_missing_database(source):
    n = source.count("\n") + 1
    assert find_violations(source) == [n]


@pytest.mark.parametrize(
    "source",
    [
        "from google.cloud import firestore\ndb = firestore.AsyncClient(project='p', database='x')",
        "from google.cloud import firestore\nC = firestore.AsyncClient\ndb = C(database='x')",
        "from google.cloud import firestore\ndb = firestore.Client.from_service_account_json('k', database='x')",
        "from google.cloud import bigquery\nc = bigquery.Client.from_service_account_json('k')",
        "import httpx\nc = httpx.AsyncClient()",
        "from google.cloud import bigquery\nc = bigquery.Client()",
        "from google.cloud.tasks_v2 import CloudTasksAsyncClient\nc = CloudTasksAsyncClient()",
        "from google.cloud import firestore\ns = 'firestore.AsyncClient()'  # firestore.Client()",
    ],
)
def test_scanner_ignores_compliant_and_foreign_clients(source):
    assert find_violations(source) == []


def test_helper_may_forward_kwargs_but_nobody_else():
    src = "from google.cloud import firestore\ndb = firestore.AsyncClient(**kw)"
    assert find_violations(src, allow_kwargs=True) == []
    assert find_violations(src) == [2]


def test_helper_default_matches_environment_config(monkeypatch):
    import scripts._firestore as helper
    from src.config.environment import EnvironmentConfig

    monkeypatch.delenv("FIRESTORE_DATABASE", raising=False)
    assert helper._kwargs()["database"] == EnvironmentConfig().firestore_database_id == "us-production"
    monkeypatch.setenv("FIRESTORE_DATABASE", "other")
    assert helper._kwargs()["database"] == "other"
