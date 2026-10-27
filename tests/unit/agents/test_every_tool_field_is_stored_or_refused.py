"""Every declared tool field is stored, refused or allowlisted (TEST_COVERAGE_SYSTEMIC_RFC §5 P14, §1.3.5 as a rule).

`tests/support/tool_fields.py` `declared()` lists every field a model can send to a tool that writes something (the six
P14 surfaces). The ledger `tool_field_ledger.txt` has one sorted line per declared field:

    <surface>:<tool>.<path>  # <class> <evidence>

Classes:
- `stored <ref>` — the referenced test asserts the stored effect of the field;
- `refused <ref>` / `noted <ref>` — the field's value is refused, or not used and named in the tool result (an expiry on
  a closing state, `derived_from` on a conversation pass);
- `prompt-input <ref>` — the field reaches the agent's model request and is stored nowhere (Tasks `background`);
- `verification <ref>` — the field is checked and never stored (the review's `basis.quote`);
- `read-only <at least two words>` — only for a field of a tool in `READ_ONLY` or a field in `READ_ONLY_FIELDS`;
- `defect:P14-Dn <ref>` — the declared field itself is the defect; the test is a strict xfail carrying that id.
A `<ref>` is `<repo path>::<test name>`.

The checks, each its own test, fail both ways:
1. the ledger's keys equal `declared()` (a declared field with no line fails, and so does a line for a field that is no
   longer declared), sorted, each once;
2. every class is one of the above; `read-only` carries at least two words, every other class exactly one reference;
3. `read-only` lines are exactly the fields of `READ_ONLY` tools plus `READ_ONLY_FIELDS` (a `read-only` line on a
   writing field fails, and so does another class on a read-only field);
4. every reference resolves: its file is the surface's e2e file (`SURFACE_FILES`), a test of that name is defined at
   module level there (AST), and the field's leaf name (`kind` of `updates.expiry.kind`, `path` of `files[].path`)
   occurs as a dict-key string or a call keyword in the test's body, or in what that body loads: the value of a
   module-level assignment, or the body of a module-level `def` (a builder: card C writes its scripted calls as
   functions because the fact ids exist only after seeding), followed transitively through the names those load.
   Docstrings, comments, subscripts (`x["kind"]`), classes and `async def` helpers do not count;
5. a `defect:` line cites a test marked `pytest.mark.xfail(strict=True, ...)` with that id in its reason; every other
   line cites a test with no `xfail` mark (no coverage is claimed from a test expected to fail).
The self-tests at the end run the checker on in-memory ledgers and in-memory test sources.

What this meta-test does not see: keys a model sends that no declaration names (P14-D1, an update of undeclared keys);
the flat union of every delegate intent's context fields that a provider may merge (only each intent's own schema is
enumerated); a bad value of a declared field (P14-D2..D5: those are the cards' strict xfails, beside the `stored` test
of the same field); whether the referenced test really asserts the field's effect (the leaf-name check proves the test
sends the field, the reviewer of the e2e file proves the assertion).

How to add a field: send it in a scripted call of the surface's e2e file (a module constant or a builder), assert its
stored effect (or refusal, note, prompt input) in a test there, then add its line here in sorted order.
"""
import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple, Union

from tests.support.tool_fields import READ_ONLY, READ_ONLY_FIELDS, declared, tool_of

REPO = Path(__file__).resolve().parents[3]
LEDGER = Path(__file__).with_name("tool_field_ledger.txt")

CONSOLIDATION_FILE = "tests/unit/agents/test_consolidation_tool_fields_e2e.py"
REVIEW_FILE = "tests/unit/services/test_fact_review_tool_fields_e2e.py"
NOTES_FILE = "tests/unit/agents/test_notes_agent_tool_fields_e2e.py"
TASKS_FILE = "tests/unit/agents/test_tasks_agent_tool_fields_e2e.py"
SMART_FILE = "tests/unit/agents/core/test_smart_local_tools_e2e.py"
DRIVE_FILE = "tests/unit/agents/test_file_management_drive_tool_fields_e2e.py"
# Keyed by `<surface>:<tool>` first, then by `<surface>`.
SURFACE_FILES: Dict[str, str] = {
    "consolidation": CONSOLIDATION_FILE,
    "consolidation_review": REVIEW_FILE,
    "notes": NOTES_FILE,
    "delegate:manage_self_reminders": NOTES_FILE,
    "tasks": TASKS_FILE,
    "delegate:manage_user_tasks": TASKS_FILE,
    "smart_local": SMART_FILE,
    **{f"delegate:{intent}": DRIVE_FILE for intent in (
        "save_file_to_drive", "list_files_in_drive", "open_file_from_drive", "move_file_in_drive",
        "create_folder_in_drive", "update_file_in_drive", "delete_file_from_drive")},
}

READ_ONLY_CLASS = "read-only"
REFERENCE_CLASSES = frozenset({"stored", "refused", "noted", "prompt-input", "verification"})
DEFECT = re.compile(r"defect:(P14-D[1-5])")
REFERENCE = re.compile(r"(tests/[\w/]+\.py)::(\w+)")

Source = Callable[[str], Optional[str]]
FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]


@dataclass(frozen=True)
class Line:
    number: int
    key: str
    cls: str
    evidence: str


# ------------------------------------------------------------------------------------------------- the ledger


def parse(text: str) -> Tuple[List[Line], List[str]]:
    """The ledger's lines (blank lines and lines starting with `#` are skipped) and the malformed ones."""
    lines: List[Line] = []
    problems: List[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, sep, rest = raw.partition("#")
        words = rest.split(None, 1)
        if not sep or not key.strip() or not words:
            problems.append(f"line {number}: not `<key>  # <class> <evidence>`: {raw!r}")
            continue
        lines.append(Line(number, key.strip(), words[0], words[1].strip() if len(words) > 1 else ""))
    return lines, problems


def leaf(key: str) -> str:
    """`kind` of `consolidation:update_fact.updates.expiry.kind`, `path` of `smart_local:draft_skill.files[].path`."""
    return key.split(".", 1)[1].split(".")[-1].replace("[]", "")


def surface_file(key: str, surface_files: Dict[str, str]) -> Optional[str]:
    tool = tool_of(key)
    return surface_files.get(tool) or surface_files.get(tool.split(":", 1)[0])


def is_read_only(key: str, read_only: FrozenSet[str], read_only_fields: FrozenSet[str]) -> bool:
    return tool_of(key) in read_only or key in read_only_fields


# ------------------------------------------------------------------------------------------------- test sources


@dataclass
class Module:
    """What check 4 and 5 read from one test file."""

    tests: Dict[str, FunctionNode]           # every module-level function, sync or async
    builders: Dict[str, ast.FunctionDef]     # module-level `def` (not `async def`)
    values: Dict[str, ast.expr]              # module-level assignments
    xfail_module: bool                       # a module-level `pytestmark` holding an xfail


def read_module(source: str) -> Module:
    tree = ast.parse(source)
    tests: Dict[str, FunctionNode] = {}
    builders: Dict[str, ast.FunctionDef] = {}
    values: Dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            tests[node.name] = node
            if isinstance(node, ast.FunctionDef):
                builders[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            values[node.target.id] = node.value
    pytestmark = values.get("pytestmark")
    xfail_module = pytestmark is not None and any(_is_xfail(n) for n in ast.walk(pytestmark))
    return Module(tests, builders, values, xfail_module)


def _body(node: ast.AST) -> List[ast.AST]:
    """A function's statements and argument defaults (not its decorators), or an expression itself."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [*node.body, node.args]
    return [node]


def _keys_and_names(node: ast.AST) -> Tuple[Set[str], Set[str]]:
    """Dict-key strings and call keywords under `node`, and the names it loads."""
    keys: Set[str] = set()
    names: Set[str] = set()
    for part in _body(node):
        for sub in ast.walk(part):
            if isinstance(sub, ast.Dict):
                keys |= {k.value for k in sub.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            elif isinstance(sub, ast.Call):
                keys |= {kw.arg for kw in sub.keywords if kw.arg}
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                names.add(sub.id)
    return keys, names


def mentions(module: Module, test: str) -> Set[str]:
    """The field names a test sends: its own dict keys and keywords, and those of every module-level value and `def`
    it loads, transitively."""
    keys, frontier = _keys_and_names(module.tests[test])
    seen: Set[str] = {test}
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        node: Optional[ast.AST] = module.values.get(name) or module.builders.get(name)
        if node is None:
            continue
        more_keys, more_names = _keys_and_names(node)
        keys |= more_keys
        frontier |= more_names - seen
    return keys


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def _is_xfail(node: ast.AST) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, (ast.Attribute, ast.Name)) and _dotted(target).split(".")[-1] == "xfail"


def xfail_marks(module: Module, test: str) -> List[ast.AST]:
    return [d for d in module.tests[test].decorator_list if _is_xfail(d)]


def _strict_with_reason(mark: ast.AST, defect_id: str) -> bool:
    if not isinstance(mark, ast.Call):
        return False
    kwargs = {kw.arg: kw.value for kw in mark.keywords if kw.arg}
    strict, reason = kwargs.get("strict"), kwargs.get("reason")
    return (isinstance(strict, ast.Constant) and strict.value is True
            and isinstance(reason, ast.Constant) and isinstance(reason.value, str) and defect_id in reason.value)


# ------------------------------------------------------------------------------------------------- the checks


def check_keys(lines: List[Line], declared_keys: Iterable[str]) -> List[str]:
    keys = [line.key for line in lines]
    wanted = set(declared_keys)
    problems = [f"declared, no ledger line: {k}" for k in sorted(wanted - set(keys))]
    problems += [f"line {line.number}: not declared (stale): {line.key}" for line in lines if line.key not in wanted]
    seen: Set[str] = set()
    for line in lines:
        if line.key in seen:
            problems.append(f"line {line.number}: listed twice: {line.key}")
        seen.add(line.key)
    if keys != sorted(keys):
        problems.append("the ledger is not sorted")
    return problems


def check_classes(lines: List[Line]) -> List[str]:
    problems: List[str] = []
    for line in lines:
        where = f"line {line.number} ({line.key})"
        if line.cls == READ_ONLY_CLASS:
            if len(line.evidence.split()) < 2:
                problems.append(f"{where}: read-only needs at least two words of evidence")
        elif line.cls in REFERENCE_CLASSES or DEFECT.fullmatch(line.cls):
            if not REFERENCE.fullmatch(line.evidence):
                problems.append(f"{where}: {line.cls} needs exactly one `<tests/...py>::<test>`, got {line.evidence!r}")
        else:
            problems.append(f"{where}: unknown class {line.cls!r}")
    return problems


def check_read_only(lines: List[Line], read_only: FrozenSet[str], read_only_fields: FrozenSet[str]) -> List[str]:
    problems: List[str] = []
    for line in lines:
        allowed = is_read_only(line.key, read_only, read_only_fields)
        if line.cls == READ_ONLY_CLASS and not allowed:
            problems.append(f"line {line.number}: read-only on a writing field: {line.key}")
        elif line.cls != READ_ONLY_CLASS and allowed:
            problems.append(f"line {line.number}: {line.cls} on a read-only field (class it read-only): {line.key}")
    return problems


def _referenced(lines: List[Line]) -> List[Tuple[Line, str, str]]:
    out = []
    for line in lines:
        match = REFERENCE.fullmatch(line.evidence)
        if (line.cls in REFERENCE_CLASSES or DEFECT.fullmatch(line.cls)) and match:
            out.append((line, match.group(1), match.group(2)))
    return out


def _modules(source: Source) -> Callable[[str], Optional[Module]]:
    @lru_cache(maxsize=None)
    def load(path: str) -> Optional[Module]:
        text = source(path)
        return None if text is None else read_module(text)
    return load


def check_references(lines: List[Line], surface_files: Dict[str, str], source: Source) -> List[str]:
    load = _modules(source)
    problems: List[str] = []
    for line, path, name in _referenced(lines):
        where = f"line {line.number} ({line.key})"
        expected = surface_file(line.key, surface_files)
        if path != expected:
            problems.append(f"{where}: cites {path}, the surface's e2e file is {expected}")
            continue
        module = load(path)
        if module is None:
            problems.append(f"{where}: no file {path}")
        elif not name.startswith("test_") or name not in module.tests:
            problems.append(f"{where}: no test {name} in {path}")
        elif leaf(line.key) not in mentions(module, name):
            problems.append(f"{where}: {name} does not send {leaf(line.key)!r} (a dict key or keyword in its body, "
                            f"or in a module-level value or def it loads)")
    return problems


def check_xfail_marks(lines: List[Line], source: Source) -> List[str]:
    load = _modules(source)
    problems: List[str] = []
    for line, path, name in _referenced(lines):
        module = load(path)
        if module is None or name not in module.tests:
            continue  # check 4 reports it
        marks = xfail_marks(module, name)
        defect = DEFECT.fullmatch(line.cls)
        where = f"line {line.number} ({line.key})"
        if defect:
            if not any(_strict_with_reason(m, defect.group(1)) for m in marks):
                problems.append(f"{where}: {name} is not a strict xfail naming {defect.group(1)}")
        elif marks or module.xfail_module:
            problems.append(f"{where}: {line.cls} cites {name}, an xfail test (no coverage from a test expected "
                            f"to fail)")
    return problems


def problems_of(text: str, declared_keys: Iterable[str], source: Source,
                read_only: FrozenSet[str] = READ_ONLY, read_only_fields: FrozenSet[str] = READ_ONLY_FIELDS,
                surface_files: Optional[Dict[str, str]] = None) -> List[str]:
    """Every check on one ledger text (the self-tests' entry point)."""
    files = SURFACE_FILES if surface_files is None else surface_files
    lines, malformed = parse(text)
    return (malformed + check_keys(lines, declared_keys) + check_classes(lines)
            + check_read_only(lines, read_only, read_only_fields)
            + check_references(lines, files, source) + check_xfail_marks(lines, source))


def repo_source(path: str) -> Optional[str]:
    file = REPO / path
    return file.read_text(encoding="utf-8") if file.is_file() else None


def ledger() -> Tuple[List[Line], List[str]]:
    return parse(LEDGER.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------- the ledger's tests


def test_1_the_ledger_lists_every_declared_field_once_and_nothing_else():
    lines, malformed = ledger()
    assert malformed + check_keys(lines, declared()) == []


def test_2_every_line_has_a_known_class_and_its_evidence():
    lines, _ = ledger()
    assert check_classes(lines) == []


def test_3_read_only_lines_are_exactly_the_read_only_tools_and_fields():
    lines, _ = ledger()
    assert check_read_only(lines, READ_ONLY, READ_ONLY_FIELDS) == []


def test_4_every_reference_is_a_test_of_the_surfaces_file_that_sends_the_field():
    lines, _ = ledger()
    assert check_references(lines, SURFACE_FILES, repo_source) == []


def test_5_only_defect_lines_cite_xfail_tests_and_those_name_the_id():
    lines, _ = ledger()
    assert check_xfail_marks(lines, repo_source) == []


def test_every_declared_tool_has_an_existing_surface_file():
    tools = sorted({tool_of(k) for k in declared()})
    assert [t for t in tools if surface_file(f"{t}.x", SURFACE_FILES) is None] == []
    assert [f for f in sorted(set(SURFACE_FILES.values())) if repo_source(f) is None] == []


# ------------------------------------------------------------------------------------------------- self-tests

F = "tests/unit/agents/test_fake_e2e.py"        # the surface `s` and the tool `s:write`
G = "tests/unit/agents/test_other_e2e.py"       # the surface `o`
FAKE_FILES = {"s": F, "o": G}
FAKE_DECLARED = ["o:put.value", "s:read.query", "s:write.items[].label", "s:write.note", "s:write.when.kind"]
FAKE_READ_ONLY = frozenset({"s:read"})
FAKE_SOURCE = '''
import pytest

WRITE_ALL = {"note": "n", "when": {"kind": "date"}}
ITEMS = [{"label": "x"}]
NESTED = {"items": ITEMS}


def WRITE_CALL(fact_id):
    return {"fact_id": fact_id, "note": "n", "when": {"kind": "date"}}


async def run(store):
    return dict(label=1)


def test_write_all():
    send(WRITE_ALL, NESTED)


def test_write_builder():
    send(WRITE_CALL("f1"))


def test_write_through_async_helper():
    run(None)


def test_write_inline():
    send({"note": "n"}, call(kind="date", query="q"))


def test_write_docstring_only():
    """It sends "note" and {"kind": 1}."""
    send(x["note"])  # "note"


def test_nothing():
    pass


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P14-D2: the note is dropped")
def test_write_xfail():
    send(WRITE_ALL)


@pytest.mark.xfail(reason="P14-D2: not strict")
def test_write_loose_xfail():
    send(WRITE_ALL)


def helper_not_a_test():
    send(WRITE_ALL)
'''
OTHER_SOURCE = '''
def test_put():
    send({"value": 1, "note": "n"})
'''
GOOD = f"""# a header comment
o:put.value  # stored {G}::test_put
s:read.query  # read-only reading tool, writes nothing
s:write.items[].label  # stored {F}::test_write_all
s:write.note  # noted {F}::test_write_builder
s:write.when.kind  # stored {F}::test_write_inline
"""


def _problems(text: str) -> List[str]:
    sources = {F: FAKE_SOURCE, G: OTHER_SOURCE}
    return problems_of(text, FAKE_DECLARED, sources.get, FAKE_READ_ONLY, frozenset(), FAKE_FILES)


def _swap(old: str, new: str) -> str:
    assert GOOD.count(old) == 1, old
    return GOOD.replace(old, new)


def test_self_the_good_ledger_passes():
    assert _problems(GOOD) == []


def test_self_a_missing_line_fails():
    assert _problems(_swap(f"s:write.note  # noted {F}::test_write_builder\n", "")) == [
        "declared, no ledger line: s:write.note"]


def test_self_a_stale_line_fails():
    stale = f"s:write.note  # noted {F}::test_write_builder\ns:write.old  # stored {F}::test_write_all\n"
    assert _problems(_swap(f"s:write.note  # noted {F}::test_write_builder\n", stale)) == [
        "line 6: not declared (stale): s:write.old",
        f"line 6 (s:write.old): test_write_all does not send 'old' (a dict key or keyword in its body, or in a "
        f"module-level value or def it loads)"]


def test_self_a_duplicate_or_unsorted_line_fails():
    duplicate = _swap("s:read.query  # read-only reading tool, writes nothing\n",
                      "s:read.query  # read-only reading tool, writes nothing\n" * 2)
    assert _problems(duplicate) == ["line 4: listed twice: s:read.query"]
    lines = GOOD.splitlines()
    unsorted = "\n".join([lines[0], lines[2], lines[1], *lines[3:]])
    assert _problems(unsorted) == ["the ledger is not sorted"]


def test_self_a_malformed_line_fails():
    assert _problems(GOOD + "s:write.note stored\n")[0].startswith("line 7: not `<key>  # <class> <evidence>`")


def test_self_an_unknown_class_fails():
    assert _problems(_swap("# noted", "# kept")) == ["line 5 (s:write.note): unknown class 'kept'"]


def test_self_read_only_needs_two_words_and_a_reference_class_one_reference():
    assert _problems(_swap("read-only reading tool, writes nothing", "read-only reading tool")) == []
    assert _problems(_swap("read-only reading tool, writes nothing", "read-only reading-tool")) == [
        "line 3 (s:read.query): read-only needs at least two words of evidence"]
    assert _problems(_swap(f"noted {F}::test_write_builder", f"noted {F}::test_write_builder and more")) == [
        f"line 5 (s:write.note): noted needs exactly one `<tests/...py>::<test>`, got "
        f"'{F}::test_write_builder and more'"]


def test_self_a_read_only_line_on_a_writing_field_fails():
    assert _problems(_swap(f"noted {F}::test_write_builder", "read-only only read, honest")) == [
        "line 5: read-only on a writing field: s:write.note"]


def test_self_a_writing_class_on_a_read_only_field_fails():
    assert _problems(_swap("read-only reading tool, writes nothing", f"stored {F}::test_write_inline")) == [
        "line 3: stored on a read-only field (class it read-only): s:read.query"]


def test_self_a_test_in_another_surfaces_file_fails():
    assert _problems(_swap(f"stored {F}::test_write_inline", f"stored {G}::test_put")) == [
        f"line 6 (s:write.when.kind): cites {G}, the surface's e2e file is {F}"]


def test_self_a_test_name_that_does_not_exist_fails():
    assert _problems(_swap(f"stored {F}::test_write_inline", f"stored {F}::test_write_nowhere")) == [
        f"line 6 (s:write.when.kind): no test test_write_nowhere in {F}"]
    assert _problems(_swap(f"stored {F}::test_write_inline", f"stored {F}::helper_not_a_test")) == [
        f"line 6 (s:write.when.kind): no test helper_not_a_test in {F}"]


def test_self_a_test_that_does_not_mention_the_field_fails():
    assert _problems(_swap(f"stored {F}::test_write_inline", f"stored {F}::test_nothing")) == [
        "line 6 (s:write.when.kind): test_nothing does not send 'kind' (a dict key or keyword in its body, or in a "
        "module-level value or def it loads)"]


def test_self_a_test_that_mentions_the_field_only_through_a_module_constant_passes():
    # `label` sits in ITEMS, which the test reaches only through NESTED: the constants are followed transitively.
    assert "label" in mentions(read_module(FAKE_SOURCE), "test_write_all")
    assert _problems(_swap(f"stored {F}::test_write_inline", f"stored {F}::test_write_all")) == []


def test_self_a_test_that_mentions_the_field_only_through_a_builder_it_calls_passes():
    # The arbiter's ruling: card C writes its scripted calls as module-level `def`s (the fact ids exist only after
    # seeding); a key in the body of a builder the test calls counts.
    assert {"note", "kind"} <= mentions(read_module(FAKE_SOURCE), "test_write_builder")
    assert _problems(_swap(f"stored {F}::test_write_inline", f"stored {F}::test_write_builder")) == []


def test_self_an_async_helper_is_not_a_builder():
    assert "label" not in mentions(read_module(FAKE_SOURCE), "test_write_through_async_helper")
    assert _problems(_swap(f"stored {F}::test_write_all", f"stored {F}::test_write_through_async_helper")) == [
        "line 4 (s:write.items[].label): test_write_through_async_helper does not send 'label' (a dict key or "
        "keyword in its body, or in a module-level value or def it loads)"]


def test_self_a_test_that_mentions_the_field_only_in_its_docstring_or_a_subscript_fails():
    assert mentions(read_module(FAKE_SOURCE), "test_write_docstring_only") == set()
    assert _problems(_swap(f"noted {F}::test_write_builder", f"noted {F}::test_write_docstring_only")) == [
        "line 5 (s:write.note): test_write_docstring_only does not send 'note' (a dict key or keyword in its body, "
        "or in a module-level value or def it loads)"]


def test_self_a_non_defect_line_citing_an_xfail_test_fails():
    assert _problems(_swap(f"noted {F}::test_write_builder", f"noted {F}::test_write_xfail")) == [
        f"line 5 (s:write.note): noted cites test_write_xfail, an xfail test (no coverage from a test expected to "
        f"fail)"]


def test_self_a_module_level_xfail_mark_counts_as_xfail():
    marked = "import pytest\npytestmark = pytest.mark.xfail(strict=True)\n" + OTHER_SOURCE
    sources = {F: FAKE_SOURCE, G: marked}
    assert problems_of(GOOD, FAKE_DECLARED, sources.get, FAKE_READ_ONLY, frozenset(), FAKE_FILES) == [
        "line 2 (o:put.value): stored cites test_put, an xfail test (no coverage from a test expected to fail)"]


def test_self_a_defect_line_needs_a_strict_xfail_with_its_id():
    assert _problems(_swap(f"noted {F}::test_write_builder", f"defect:P14-D2 {F}::test_write_xfail")) == []
    assert _problems(_swap(f"noted {F}::test_write_builder", f"defect:P14-D3 {F}::test_write_xfail")) == [
        "line 5 (s:write.note): test_write_xfail is not a strict xfail naming P14-D3"]
    assert _problems(_swap(f"noted {F}::test_write_builder", f"defect:P14-D2 {F}::test_write_loose_xfail")) == [
        "line 5 (s:write.note): test_write_loose_xfail is not a strict xfail naming P14-D2"]
    assert _problems(_swap(f"noted {F}::test_write_builder", f"defect:P14-D2 {F}::test_write_builder")) == [
        "line 5 (s:write.note): test_write_builder is not a strict xfail naming P14-D2"]
    assert _problems(_swap(f"noted {F}::test_write_builder", f"defect:P14-D9 {F}::test_write_xfail")) == [
        "line 5 (s:write.note): unknown class 'defect:P14-D9'"]


def test_self_leaf_names():
    assert leaf("consolidation:update_fact.updates.expiry.kind") == "kind"
    assert leaf("smart_local:draft_skill.files[].path") == "path"
    assert leaf("delegate:save_file_to_drive.text") == "text"
