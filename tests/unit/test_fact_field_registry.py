"""H5 field registry (P20): every `FactEntity` field is read by some code in `src/`, or is on a reasoned list.

A field that is written and never read is either an audit stamp (fine, listed below with its reason) or a defect
the owner has not seen: a stored value nobody uses is where "the model produced it, the code dropped it" hides.
The check is AST-only over `src/` and the root `*_main.py` entry points; nothing is imported from `src/` but the
field list cross-check.

What counts as a reader, in a module that handles facts: it imports `FactEntity`, `StoredFact` or the fact repository
(`FactRepository`, `FirestoreFactRepository`: they hand out the dict form), or it is one of `FACT_MODULE_STEMS`, the
services named in a planned reader (the baseline `biographical_context_service`, named for `user_identity`; PR 11 of
FACT_PROFILE_RFC put that read in the repository's identity query, which closed P20-D2):
- `<x>.field` in load context;
- `<x>["field"]` in load context, and `.get("field")` / `.pop("field")`;
- `FieldFilter("field", ...)`, `.where("field", ...)`, `.order_by("field")` (a query reads the stored value);
- the name of a vector field passed to a call whose name says vector or nearest (`_nearest(...)`,
  `get_fact_vectors(...)`, `_search_by_vector_field(...)`: a vector search reads the stored embedding).
What does not count: `FactEntity(**data)` and `model_dump()` (the whole document moves, nothing is looked at), a
`select([...])` projection list, a dict literal key or an attribute/subscript store, and the persistence plumbing
that re-reads a key only to rewrite it: the argument of an `isinstance(d["k"], ...)` test and the right-hand side of
`d["k"] = f(d["k"])`. A decision read (`if d["state"] == "current": d["state"] = "stale"`) counts as a reader.

Known limits: an attribute name shared with another class in the same module (`id`, `text`, `state`) counts as a
reader; the test is a floor for the rare names, not a proof for the common ones. Scripts under `scripts/` read
documents too (the data audit) but are not product code, so they never count.
"""
import ast
import re
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTITIES = "src/domain/entities.py"
ENTRY_POINTS = ("main.py", "indexer_main.py", "job_main.py", "relay_main.py", "sandbox_main.py")
FACT_TYPES = {"FactEntity", "StoredFact", "FactRepository", "FirestoreFactRepository"}  # importing any: handles facts
FACT_MODULE_STEMS = {"biographical_context_service"}  # handles facts without importing the types (see the docstring)
QUERY_CALLS = {"FieldFilter", "where", "order_by"}
DICT_READS = {"get", "pop"}

# Written and read by no code, on purpose. One line each: the reason, and the writer (path:line) at the time of writing.
# The list only shrinks: an entry whose field gained a reader fails `test_allow_list_has_no_stale_entry`.
# `visibility` is not listed: it is unenforced by owner ruling (memory README:276, :156) and no writer sets user_private
# but the legacy migration, yet that migration's `data["visibility"] == "private"` test (firestore_repo.py:205) is a
# decision read, so the scanner finds a reader. Should that migration go, add the entry with the ruling as its reason.
WRITE_ONLY_BY_DESIGN: dict[str, str] = {
    "last_updated": "audit stamp of every in-place write, 'read by no code' (memory README, fields table); writer src/domain/fact_update_plan.py:52",
    "is_current": "'latest document of its lineage', no src code reads it, only the data audit (memory README §2.3); writer src/domain/fact_lifecycle.py:33",
    "replaces_fact_id": "history link to the replaced document, followed by hand in the console history procedure (memory README:1768) and checked by the data audit (README:1861), §2.4; no src code; writer src/domain/fact_update_plan.py:50",
    "profile_model": "observability stamp of the profiler step (FACT_PROFILE_RFC §2.1: 'code, observability'); writer src/services/fact_profile_service.py:468",
    "profiled_at": "observability stamp of the profiler step (FACT_PROFILE_RFC §2.1: 'code, observability'); writer src/services/fact_profile_service.py:468",
}

# Fields written, meant to be read, read by no code yet. Each has its own strict xfail below, `P20-D<n>: `field` ...`;
# the one that gets a reader flips its test to XPASS (strict), which forces the pin and this entry out.
PINNED_DEFECTS: dict[str, str] = {}


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


def handles_facts(tree: ast.AST, stem: str = "") -> bool:
    return stem in FACT_MODULE_STEMS or any(
        isinstance(n, ast.ImportFrom) and any(a.name in FACT_TYPES for a in n.names) for n in ast.walk(tree)
    )


def _plumbing(tree: ast.AST) -> set[int]:
    """`id()`s of subscript loads that only feed a rewrite of the same key: an `isinstance(d["k"], ...)` argument and
    the right-hand side of `d["k"] = f(d["k"])`. Any other read, a comparison included, is a decision and counts."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted_tail(node.func) == "isinstance" and node.args:
            for n in ast.walk(node.args[0]):
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load):
                    skip.add(id(n))
        elif isinstance(node, ast.Assign):
            stored = {
                k for t in node.targets if isinstance(t, ast.Subscript) and isinstance(t.ctx, ast.Store)
                and (k := _const_str(t.slice))
            }
            for n in ast.walk(node.value):
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and _const_str(n.slice) in stored:
                    skip.add(id(n))
    return skip


def reads(source: str, fields: set[str], stem: str = "") -> list[tuple[str, int, str]]:
    """(field, line, kind) of every reader of a field in one module; [] when the module handles no facts."""
    tree = ast.parse(source)
    if not handles_facts(tree, stem):
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
        if path.stem not in FACT_MODULE_STEMS and not any(t in text for t in FACT_TYPES):
            continue
        for field, line, kind in reads(text, set(fields), path.stem):
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


def test_p20_d2_user_identity_is_read():
    # Reviewer edit (FACT_PROFILE_RFC §6 row 1, PR 11): the pinned defect is fixed (the identity block's query filters on
    # the field), so the strict xfail and its PINNED_DEFECTS entry go; the test stays as the guard that it is read.
    assert _registry()[1]["user_identity"]


def test_pinned_defects_match_their_tests():
    """The strict xfail tests of this module name exactly the fields and ids of PINNED_DEFECTS."""
    import sys

    named = {}
    for name, fn in vars(sys.modules[__name__]).items():
        if not (name.startswith("test_") and callable(fn)):
            continue
        for mark in getattr(fn, "pytestmark", []):
            if mark.name == "xfail":
                assert mark.kwargs.get("strict") is True, f"{name}: an xfail here must be strict"
                m = re.match(r"(P20-D\d+): `(\w+)`", mark.kwargs.get("reason", ""))
                assert m, f"{name}: the xfail reason must start 'P20-D<n>: `field`'"
                named[m.group(2)] = m.group(1)
    assert named == PINNED_DEFECTS


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
        "def f(fact):\n    return fact.text",  # a module that handles no facts
        "from x import FactEntity\ndef f(s):\n    return s.build('tags_vector')",  # not a vector call
    ],
)
def test_scanner_ignores_non_readers(source):
    assert reads(source, FIELDS) == []


def test_a_decision_read_counts_but_a_rewrite_of_the_same_key_does_not():
    decision = (
        "from x import FactEntity\n"
        "def f(d):\n"
        "    if d['state'] == 'current':\n"
        "        d['state'] = 'stale'\n"
    )
    assert reads(decision, FIELDS) == [("state", 3, "key")]
    rewrite = "from x import FactEntity\ndef f(d):\n    d['state'] = norm(d['state'])\n"
    assert reads(rewrite, FIELDS) == []
    other_key = "from x import FactEntity\ndef f(d):\n    d['text'] = d['state']\n"
    assert reads(other_key, FIELDS) == [("state", 3, "key")]


def test_a_module_named_in_the_pin_is_scanned_without_importing_the_types():
    src = "def f(fact):\n    return fact.user_identity\n"
    assert reads(src, {"user_identity"}) == []
    assert reads(src, {"user_identity"}, "biographical_context_service") == [("user_identity", 2, "attribute")]
    assert reads("from x import FirestoreFactRepository\n" + src, {"user_identity"}) == [("user_identity", 3, "attribute")]


def test_fact_fields_reads_the_class_only():
    src = "x: int = 1\nclass Other:\n    a: int\nclass FactEntity:\n    b: int\n    c: str = ''\n    def m(self): ...\n"
    assert fact_fields(src) == ["b", "c"]
