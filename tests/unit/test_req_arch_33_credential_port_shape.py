"""REQ-ARCH-33 — the credential port's shape and where connector kinds live (MULTI_DRIVE_RFC §4.2, §13 T-6).

1. No `OAuthCredentialsPort` method has a `provider` parameter.
2. The four connector-kind literals appear only in `src/composition/connector_registry.py` and `src/adapters/**` (AST
   over `src/`: a string constant equal to a kind), and in no static Cabinet asset (`src/web/static/`: a quoted kind or
   a connect URL naming one).
3. A single-credential service (a class in `src/services` or `src/handlers` whose constructor takes the credential
   port and which lists credentials of one kind) takes a `ConnectorSpec` by constructor.

Named exemptions to (2), each a module-level constant of another concept that happens to share a kind's spelling:
- `src/domain/email.py: GMAIL_INDEX_PROVIDER` — the `provider` key of the Gmail index's own stored records (indexing
  state doc id `{user_id}_{provider}`, the indexing jobs), not a credential kind; changing it would orphan stored state.
- `src/domain/drive_note.py: ONEDRIVE_PROVIDER_KEY` — the provider part of the drive notes' binding key
  `i:{provider}:{drive_id}:{item_id}` (stored); slice 1b replaces it with the spec's `id_space` (§9, §10).
Each exemption is the constant's definition only: the same literal anywhere else in the file still fails.
"""
import ast
import inspect
import os
import re
from pathlib import Path
from typing import Iterator, List, Set, Tuple

import pytest

from src.ports.oauth_credentials_port import OAuthCredentialsPort

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
STATIC = SRC / "web" / "static"
KINDS = frozenset({"gmail", "google_tasks", "microsoft_todo", "microsoft_onedrive"})
REGISTRY = "src/composition/connector_registry.py"
ADAPTERS = "src/adapters/"
EXEMPT_CONSTANTS = {
    ("src/domain/email.py", "GMAIL_INDEX_PROVIDER"),
    ("src/domain/drive_note.py", "ONEDRIVE_PROVIDER_KEY"),
}
SINGLE_CREDENTIAL_LAYERS = ("src/services", "src/handlers")
_LISTING_CALLS = {"list_keys", "list_for_user", "list_user_ids"}
_STATIC_KIND = re.compile(
    r"""(["'`])(?:gmail|google_tasks|microsoft_todo|microsoft_onedrive)\1"""
    r"""|/auth/connect/(?:gmail|google_tasks|microsoft_todo|microsoft_onedrive)\b""")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _py_files(base: Path) -> Iterator[Path]:
    for root, _, files in os.walk(base):
        for name in files:
            if name.endswith(".py"):
                yield Path(root) / name


# ---------------------------------------------------------------- the checkers (pure: source text in, findings out)

def kind_literals(source: str, rel: str) -> List[Tuple[int, str]]:
    """(line, literal) of every string constant equal to a kind, minus the exempt constants' own definitions."""
    tree = ast.parse(source)
    exempt_nodes: Set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if any((rel, name) in EXEMPT_CONSTANTS for name in names) and node.value is not None:
                exempt_nodes.add(id(node.value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in KINDS
            and id(node) not in exempt_nodes]


def static_kinds(text: str) -> List[Tuple[int, str]]:
    return [(i, line.strip()[:120]) for i, line in enumerate(text.splitlines(), 1) if _STATIC_KIND.search(line)]


def _init_annotations(cls: ast.ClassDef) -> List[str]:
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
            args = item.args.posonlyargs + item.args.args + item.args.kwonlyargs
            return [ast.unparse(a.annotation) for a in args if a.annotation is not None]
    return []


def _fixed_kind(arg: ast.expr) -> bool:
    """A kind the class fixes: a string literal, a module constant (an ALL_CAPS name) or an attribute of the instance
    (`self._kind`, `self._spec.kind`). A kind read off a local or a stored record (`connection.kind`) is not one: the
    class then serves every kind its records carry (the drive connections, MULTI_DRIVE_RFC §4.4)."""
    if isinstance(arg, ast.Constant):
        return isinstance(arg.value, str)
    if isinstance(arg, ast.Name):
        return arg.id.isupper()
    while isinstance(arg, ast.Attribute):
        arg = arg.value
    return isinstance(arg, ast.Name) and arg.id in {"self", "cls"}


def _lists_by_kind(cls: ast.ClassDef) -> bool:
    """A call of a credential-listing method with a kind the class fixes (a second positional or a `kind=` argument;
    `list_user_ids` takes the kind first)."""
    for node in ast.walk(cls):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LISTING_CALLS):
            continue
        position = 0 if node.func.attr == "list_user_ids" else 1
        kinds = [k.value for k in node.keywords if k.arg == "kind"]
        if len(node.args) > position:
            kinds.append(node.args[position])
        if any(_fixed_kind(kind) for kind in kinds):
            return True
    return False


def single_credential_without_spec(source: str) -> List[str]:
    """Classes whose constructor takes the credential port, which list credentials by kind, and whose constructor
    takes no ConnectorSpec (a spec, an optional one or a mapping of them)."""
    found = []
    for cls in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef)):
        annotations = _init_annotations(cls)
        if not any("OAuthCredentialsPort" in a for a in annotations) or not _lists_by_kind(cls):
            continue
        if not any("ConnectorSpec" in a for a in annotations):
            found.append(cls.name)
    return found


# ------------------------------------------------------------------------------------------------- the rules

@pytest.mark.requirement("REQ-ARCH-33")
def test_no_credential_port_method_takes_a_provider():
    offenders = []
    for name, member in inspect.getmembers(OAuthCredentialsPort, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        if "provider" in inspect.signature(member).parameters:
            offenders.append(name)
    assert offenders == []


@pytest.mark.slow     # parses all of src/
@pytest.mark.requirement("REQ-ARCH-33")
def test_kind_literals_live_only_in_the_registry_and_the_adapters():
    violations = []
    for path in _py_files(SRC):
        rel = _rel(path)
        if rel == REGISTRY or rel.startswith(ADAPTERS):
            continue
        for line, literal in kind_literals(path.read_text(encoding="utf-8"), rel):
            violations.append(f"  {rel}:{line} {literal!r}")
    assert not violations, ("connector-kind literal outside the registry and the adapters (take the kind from an "
                            "injected ConnectorSpec):\n" + "\n".join(violations))


@pytest.mark.requirement("REQ-ARCH-33")
def test_no_kind_in_the_static_cabinet():
    violations = []
    for path in sorted(STATIC.rglob("*")):
        if not path.is_file() or path.suffix not in {".html", ".js", ".css", ".json", ".webmanifest"}:
            continue
        for line, text in static_kinds(path.read_text(encoding="utf-8")):
            violations.append(f"  {_rel(path)}:{line} {text}")
    assert not violations, ("connector kind in a static Cabinet asset (render it from GET /api/connectors):\n"
                            + "\n".join(violations))


@pytest.mark.slow     # parses all of src/
@pytest.mark.requirement("REQ-ARCH-33")
def test_every_single_credential_service_takes_a_connector_spec():
    violations = []
    for layer in SINGLE_CREDENTIAL_LAYERS:
        for path in _py_files(ROOT / layer):
            for name in single_credential_without_spec(path.read_text(encoding="utf-8")):
                violations.append(f"  {_rel(path)}: {name}")
    assert not violations, ("single-credential service without an injected ConnectorSpec:\n" + "\n".join(violations))


# ----------------------------------------------------------------------------- the checkers catch what they must

def test_the_literal_checker_flags_a_kind_and_spares_only_the_named_definition():
    source = 'GMAIL_INDEX_PROVIDER = "gmail"\nOTHER = "gmail"\ndef f():\n    return g("microsoft_todo", "gmailx")\n'
    assert kind_literals(source, "src/domain/email.py") == [(2, "gmail"), (4, "microsoft_todo")]
    assert kind_literals(source, "src/services/x.py") == [(1, "gmail"), (2, "gmail"), (4, "microsoft_todo")]


def test_the_static_checker_flags_quoted_kinds_and_connect_urls_only():
    text = ('a = "gmail";\nb = `microsoft_onedrive`;\nhref="/auth/connect/google_tasks"\n'
            'id="gmail-container" /api/gmail/status toast.gmail_connected\n')
    assert [line for line, _ in static_kinds(text)] == [1, 2, 3]


def test_the_spec_checker_flags_a_kind_listing_service_without_a_spec():
    bad = ("class S:\n    def __init__(self, creds: OAuthCredentialsPort, kind: str):\n        self._c = creds\n"
           "    async def f(self, u):\n        return await self._c.list_keys(u, self._kind)\n")
    good = bad.replace("kind: str", "connector: ConnectorSpec")
    by_id = ("class R:\n    def __init__(self, creds: OAuthCredentialsPort):\n        self._c = creds\n"
             "    async def f(self, u, i):\n        return await self._c.get(u, i)\n")
    ids = ("class T:\n    def __init__(self, creds: 'Optional[OAuthCredentialsPort]'):\n        self._c = creds\n"
           "    async def f(self):\n        return await self._c.list_user_ids(self._kind)\n")
    assert single_credential_without_spec(bad) == ["S"]
    assert single_credential_without_spec(good) == []
    assert single_credential_without_spec(by_id) == []
    assert single_credential_without_spec(ids) == ["T"]


def test_the_spec_checker_spares_a_class_listing_by_a_kind_read_off_a_record():
    by_record = ("class D:\n    def __init__(self, creds: OAuthCredentialsPort):\n        self._c = creds\n"
                 "    async def f(self, u, connections):\n        for c in connections:\n"
                 "            await self._c.list_keys(u, c.kind)\n")
    fixed = ("class L:\n    def __init__(self, creds: OAuthCredentialsPort):\n        self._c = creds\n"
             "    async def f(self, u):\n        await self._c.list_keys(u, \"x\")\n"
             "        await self._c.list_for_user(u, kind=self._spec.kind)\n        await self._c.list_user_ids(KIND)\n")
    assert single_credential_without_spec(by_record) == []
    assert single_credential_without_spec(fixed) == ["L"]
    for call in ('list_keys(u, "x")', "list_for_user(u, kind=self._spec.kind)", "list_user_ids(KIND)"):
        one = fixed.split("    async def")[0] + f"    async def f(self, u):\n        await self._c.{call}\n"
        assert single_credential_without_spec(one) == ["L"], call
