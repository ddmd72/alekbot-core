"""H5 field registry (P20): every `FactEntity` field is read by some code in `src/`, or is on a reasoned list.

A field that is written and never read is either an audit stamp (fine, listed below with its reason) or a defect
the owner has not seen: a stored value nobody uses is where "the model produced it, the code dropped it" hides.
The check is AST-only over `src/` and the root `*_main.py` entry points; nothing is imported from `src/` but the
field list cross-check.

What counts as a reader, in a module that imports `FactEntity` or `StoredFact` (the repository that receives the
dict form is one of them):
- `<x>.field` in load context;
- `<x>["field"]` in load context, and `.get("field")` / `.pop("field")`;
- `FieldFilter("field", ...)`, `.where("field", ...)`, `.order_by("field")` (a query reads the stored value);
- the name of a vector field passed to a call whose name says vector or nearest (`_nearest(...)`,
  `get_fact_vectors(...)`, `_search_by_vector_field(...)`: a vector search reads the stored embedding).
What does not count: `FactEntity(**data)` and `model_dump()` (the whole document moves, nothing is looked at), a
`select([...])` projection list, a dict literal key or an attribute/subscript store, and the persistence plumbing
that re-reads a key only to rewrite it (`if "k" in data and isinstance(data["k"], Vector): data["k"] = list(...)`).

Known limits: an attribute name shared with another class in the same module (`id`, `text`, `state`) counts as a
reader; the test is a floor for the rare names, not a proof for the common ones. Scripts under `scripts/` read
documents too (the data audit) but are not product code, so they never count.
"""
import ast
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTITIES = "src/domain/entities.py"
ENTRY_POINTS = ("main.py", "indexer_main.py", "job_main.py", "relay_main.py", "sandbox_main.py")
FACT_TYPES = {"FactEntity", "StoredFact"}  # a module importing either handles facts
QUERY_CALLS = {"FieldFilter", "where", "order_by"}
DICT_READS = {"get", "pop"}

# Written and read by no code, on purpose. One line each: the reason, and the writer (path:line) at the time of writing.
# The list only shrinks: an entry whose field gained a reader fails `test_allow_list_has_no_stale_entry`.
WRITE_ONLY_BY_DESIGN: dict[str, str] = {
    "last_updated": "audit stamp of every in-place write, 'read by no code' (memory README, fields table); writer src/domain/fact_update_plan.py:52",
    "is_current": "'latest document of its lineage', no src code reads it, only the data audit (memory README §2.3); writer src/domain/fact_lifecycle.py:33",
    "replaces_fact_id": "history link from a version to the document it replaced, kept for the audit and the lineage trail (memory README §2.4); writer src/domain/fact_update_plan.py:50",
    "profile_model": "observability stamp of the profiler step (FACT_PROFILE_RFC §2.1: 'code, observability'); writer src/services/fact_profile_service.py:468",
    "profiled_at": "observability stamp of the profiler step (FACT_PROFILE_RFC §2.1: 'code, observability'); writer src/services/fact_profile_service.py:468",
}

# Fields that look like a defect: written, meant to be read, read by no code. Each has its own strict xfail below;
# the one that gets a reader flips its test to XPASS (strict), which forces the pin and this entry out.
PINNED_DEFECTS: dict[str, str] = {
    "visibility": "P20-D1",
    "user_identity": "P20-D2",
}


def _dotted_tail(node: ast.AST) -> str:
    """The called name: `f` for `f(...)`, `m` for `x.m(...)`, '' for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def fact_fields(source: str) -> list[str]:
    """The annotated attributes of the class `FactEntity` in `source`, in declaration order."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == "FactEntity":
            return [n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
    raise AssertionError("class FactEntity not found")


def handles_facts(tree: ast.AST) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and any(a.name in FACT_TYPES for a in n.names) for n in ast.walk(tree)
    )


def _plumbing(tree: ast.AST) -> set[int]:
    """`id()`s of subscript loads that only feed a store of the same key inside the same `if` (a conversion)."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        scope = [node.test, *node.body]
        stored = {
            k
            for part in node.body
            for n in ast.walk(part)
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and (k := _const_str(n.slice))
        }
        for part in scope:
            for n in ast.walk(part):
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and _const_str(n.slice) in stored:
                    skip.add(id(n))
    return skip


def reads(source: str, fields: set[str]) -> list[tuple[str, int, str]]:
    """(field, line, kind) of every reader of a field in one module; [] when the module handles no facts."""
    tree = ast.parse(source)
    if not handles_facts(tree):
        return []
    plumbing = _plumbing(tree)
    found: list[tuple[str, int, str]] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and n.attr in fields:
            found.append((n.attr, n.lineno, "attribute"))
        elif isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and id(n) not in plumbing:
            key = _const_str(n.slice)
            if key in fields:
                found.append((key, n.lineno, "key"))
        elif isinstance(n, ast.Call):
            name = _dotted_tail(n.func)
            first = _const_str(n.args[0]) if n.args else None
            if first in fields and (
                (name in DICT_READS and isinstance(n.func, ast.Attribute)) or name in QUERY_CALLS
            ):
                found.append((first, n.lineno, "query" if name in QUERY_CALLS else name))
            elif ("vector" in name or "nearest" in name) and name != "model_dump":
                for arg in [*n.args, *(k.value for k in n.keywords)]:
                    const = _const_str(arg)
                    if const in fields and const.endswith("vector"):
                        found.append((const, n.lineno, "vector-search"))
    return found


@lru_cache(maxsize=1)
def _registry() -> tuple[list[str], dict[str, list[str]]]:
    fields = fact_fields((ROOT / ENTITIES).read_text(encoding="utf-8"))
    readers: dict[str, list[str]] = {f: [] for f in fields}
    paths = list((ROOT / "src").rglob("*.py")) + [ROOT / n for n in ENTRY_POINTS if (ROOT / n).exists()]
    for path in sorted(paths):
        rel = path.relative_to(ROOT).as_posix()
        if rel == ENTITIES:
            continue
        text = path.read_text(encoding="utf-8")
        if not any(t in text for t in FACT_TYPES):
            continue
        for field, line, kind in reads(text, set(fields)):
            readers[field].append(f"{rel}:{line} ({kind})")
    return fields, readers


def test_registry_sees_the_entity_and_its_readers():
    fields, readers = _registry()
    from src.domain.entities import FactEntity

    assert fields == [f for f in FactEntity.model_fields], "the AST field list drifted from the model"
    assert len(fields) >= 50
    assert any(r.startswith("src/adapters/firestore_repo.py") for rs in readers.values() for r in rs)
    assert any(r.startswith("src/services/fact_write_service.py") for rs in readers.values() for r in rs)
    assert readers["text"] and readers["state"] and readers["expires_at"]


def test_every_fact_field_has_a_reader_or_a_reason():
    _, readers = _registry()
    unread = sorted(f for f, rs in readers.items() if not rs and f not in WRITE_ONLY_BY_DESIGN and f not in PINNED_DEFECTS)
    assert not unread, (
        "FactEntity fields written but read by no code in src/: " + ", ".join(unread)
        + ". Read them, delete them, or add a WRITE_ONLY_BY_DESIGN line with a reason (a field meant to be read: a pinned defect)."
    )


def test_allow_list_has_no_stale_entry():
    fields, readers = _registry()
    assert set(WRITE_ONLY_BY_DESIGN) | set(PINNED_DEFECTS) <= set(fields), "an entry names a field FactEntity no longer has"
    assert not set(WRITE_ONLY_BY_DESIGN) & set(PINNED_DEFECTS)
    now_read = sorted(f for f in WRITE_ONLY_BY_DESIGN if readers[f])
    assert not now_read, "listed as write-only but read now, remove from WRITE_ONLY_BY_DESIGN: " + ", ".join(now_read)
    assert all(len(reason.split()) >= 4 for reason in WRITE_ONLY_BY_DESIGN.values()), "an entry needs a reason"


@pytest.mark.xfail(
    strict=True,
    reason="P20-D1: `visibility` (account_shared / user_private) is stored on every fact and enforced by no read path; "
    "the cabinet and search show every member every fact (memory README: 'never filtered on read')",
)
def test_p20_d1_visibility_is_read():
    assert _registry()[1]["visibility"]


@pytest.mark.xfail(
    strict=True,
    reason="P20-D2: `user_identity` is written by the profiler step and read by no code; its reader (the baseline "
    "identity block) is PR 11 of FACT_PROFILE_RFC",
)
def test_p20_d2_user_identity_is_read():
    assert _registry()[1]["user_identity"]


def test_pinned_defects_match_their_tests():
    assert PINNED_DEFECTS == {"visibility": "P20-D1", "user_identity": "P20-D2"}


FIELDS = {"text", "state", "tags_vector", "visibility", "stale_since"}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from x import FactEntity\ndef f(fact):\n    return fact.text", [("text", 3, "attribute")]),
        ("from x import FactEntity\ndef f(d):\n    return d['state']", [("state", 3, "key")]),
        ("from x import FactEntity\ndef f(d):\n    return d.get('state')", [("state", 3, "get")]),
        ("from x import FactEntity\ndef f(d):\n    return d.pop('state')", [("state", 3, "pop")]),
        ("from x import StoredFact\ndef f(s):\n    return s.fact.text", [("text", 3, "attribute")]),
        ("from x import FactEntity\ndef f(q):\n    return q.where(filter=FieldFilter('state', '==', 1))",
         [("state", 3, "query")]),
        ("from x import FactEntity\ndef f(q):\n    return q.order_by('stale_since')", [("stale_since", 3, "query")]),
        ("from x import FactEntity\ndef f(s):\n    return s._nearest(1, 'tags_vector', 3)",
         [("tags_vector", 3, "vector-search")]),
    ],
)
def test_scanner_counts_readers(source, expected):
    assert reads(source, FIELDS) == expected


@pytest.mark.parametrize(
    "source",
    [
        "from x import FactEntity\ndef f(d):\n    return FactEntity(**d)",
        "from x import FactEntity\ndef f(d):\n    return FactEntity(text='a', state=1)",
        "from x import FactEntity\ndef f(fact):\n    fact.text = 'a'",
        "from x import FactEntity\ndef f(d):\n    d['state'] = 1",
        "from x import FactEntity\ndef f():\n    return {'state': 1, 'text': 'a'}",
        "from x import FactEntity\ndef f(q):\n    return q.select(['text', 'state'])",
        "from x import FactEntity\ndef f(d):\n    return d.model_dump()",
        "from x import FactEntity\ndef f(d):\n    return d.get('other')",
        "from x import FactEntity\ndef f(d):\n    if 'tags_vector' in d and isinstance(d['tags_vector'], V):\n        d['tags_vector'] = list(d['tags_vector'])",
        "from x import FactEntity\ndef f(d):\n    if 'visibility' not in d:\n        d['visibility'] = 'a'\n    elif d['visibility'] == 'p':\n        d['visibility'] = 'b'",
        "def f(fact):\n    return fact.text",  # a module that handles no facts
        "from x import FactEntity\ndef f(s):\n    return s.build('tags_vector')",  # not a vector call
    ],
)
def test_scanner_ignores_non_readers(source):
    assert reads(source, FIELDS) == []


def test_conversion_plumbing_is_per_if_not_per_module():
    src = (
        "from x import FactEntity\n"
        "def f(d):\n"
        "    if d['tags_vector']:\n"
        "        d['tags_vector'] = 1\n"
        "def g(d):\n"
        "    return d['tags_vector']\n"
    )
    assert reads(src, FIELDS) == [("tags_vector", 6, "key")]


def test_fact_fields_reads_the_class_only():
    src = "x: int = 1\nclass Other:\n    a: int\nclass FactEntity:\n    b: int\n    c: str = ''\n    def m(self): ...\n"
    assert fact_fields(src) == ["b", "c"]
