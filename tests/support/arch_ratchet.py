"""The source scan and the ratcheting allowlist shared by the composition-root guards (`tests/unit/architecture/`).

Each guard reads `src/` with `ast` (nothing is imported) and compares what it finds with its allowlist, which froze the
offenders the composition-root assessment found. The list can only shrink: an unlisted offender fails, and so does a
line whose offence is gone or whose count no longer matches.

A line is `<key>  # <count>  # <reason>` for a counted guard and `<key>  # <reason>` for the others; blank lines and
lines starting with `#` are comments. A count of `*` (any number) is accepted only on a line whose reason starts with
`permanent`. A reason has at least three words. No line numbers in keys.
"""
import ast
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, NamedTuple, Optional, Tuple, Union

ROOT = Path(__file__).resolve().parents[2]
ANY = "*"

FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]


class Entry(NamedTuple):
    key: str
    count: Optional[str]     # an int as text, `*`, or None for an uncounted guard
    reason: str
    line: int


# ----------------------------------------------------------------------------- the scan

def sources(*roots: str) -> List[str]:
    """Repo-relative POSIX paths of every `.py` file under each root (a root may be one file), sorted."""
    found = set()
    for root in roots:
        path = ROOT / root
        files = [path] if path.is_file() else path.rglob("*.py")
        found.update(p.relative_to(ROOT).as_posix() for p in files)
    return sorted(found)


@lru_cache(maxsize=None)
def tree(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(), filename=rel)


def _blocks(stmt: ast.AST) -> Iterator[ast.stmt]:
    """The statements directly nested in `stmt` (bodies, else and finally blocks, except handlers, match cases)."""
    for field in ("body", "orelse", "finalbody"):
        block = getattr(stmt, field, None)
        if isinstance(block, list):
            yield from block
    for part in [*getattr(stmt, "handlers", ()), *getattr(stmt, "cases", ())]:
        yield from part.body


def statements(module: ast.AST) -> Iterator[Tuple[ast.stmt, str, Optional[ast.ClassDef]]]:
    """Every statement with the dotted name of its scope (`<module>`, `Class`, `Class.method`, `outer.inner`) and the
    class whose body it sits in directly (None inside a function). Only statements are walked, never expressions: a
    def, a class and an assignment target are always statements, and skipping the expressions keeps the scan fast."""
    def walk(stmt: ast.AST, scope: str, owner: Optional[ast.ClassDef]):
        for child in _blocks(stmt):
            yield child, scope, owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = child.name if scope == "<module>" else f"{scope}.{child.name}"
                yield from walk(child, inner, child if isinstance(child, ast.ClassDef) else None)
            else:
                yield from walk(child, scope, owner)
    yield from walk(module, "<module>", None)


def _qualified(scope: str, name: str) -> str:
    return name if scope == "<module>" else f"{scope}.{name}"


def functions(module: ast.AST) -> Iterator[Tuple[str, FunctionNode, Optional[ast.ClassDef]]]:
    """Every function with its dotted qualified name (`Class.method`, `outer.inner`) and the class it is a method of."""
    for stmt, scope, owner in statements(module):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield _qualified(scope, stmt.name), stmt, owner


def classes(module: ast.AST) -> Iterator[Tuple[str, ast.ClassDef]]:
    """Every class with its dotted qualified name (a nested class is `Outer.Inner`)."""
    for stmt, scope, _ in statements(module):
        if isinstance(stmt, ast.ClassDef):
            yield _qualified(scope, stmt.name), stmt


# ----------------------------------------------------------------------------- the allowlist

def parse(text: str, *, counted: bool) -> Dict[str, Entry]:
    """The entries of an allowlist by key. A malformed or repeated line raises with its number."""
    entries: Dict[str, Entry] = {}
    shape = "<key>  # <count>  # <reason>" if counted else "<key>  # <reason>"
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("  # ")]
        if counted:
            ok = len(parts) >= 3 and (parts[1].isdigit() or (parts[1] == ANY and parts[2].startswith("permanent")))
            key, count, reason = (parts[0], parts[1], "  # ".join(parts[2:])) if ok else ("", None, "")
        else:
            ok = len(parts) >= 2
            key, count, reason = (parts[0], None, "  # ".join(parts[1:])) if ok else ("", None, "")
        if not ok or not key or " " in key or len(reason.split()) < 3:
            raise ValueError(f"line {number}: expected `{shape}` (a reason of at least three words; a `*` count only "
                             f"with a `permanent` reason), got {raw!r}")
        if key in entries:
            raise ValueError(f"line {number}: {key!r} repeats line {entries[key].line}")
        entries[key] = Entry(key, count, reason, number)
    return entries


def load(path: Path, *, counted: bool) -> Dict[str, Entry]:
    return parse(path.read_text(), counted=counted)


def compare(found: Mapping[str, int], listed: Mapping[str, Entry], *, allowlist: str, unit: str, fix: str,
            ceiling: bool = False) -> List[str]:
    """What breaks the ratchet: `found` maps each offender found today to its count (1 for an uncounted guard).

    Exact (the default): a listed count must equal the count found; a higher count is regrowth, a lower one asks for the
    line to be lowered. `ceiling=True`: the listed count is a ceiling; only growth above it fails. Both: an unlisted
    offender fails with `fix`, and a listed key that is no longer an offender fails with "delete the line"."""
    problems = []
    for key in sorted(found):
        n = found[key]
        entry = listed.get(key)
        if entry is None:
            problems.append(f"{key}: {n} {unit}, and it is not in {allowlist}. {fix}")
        elif entry.count is None or entry.count == ANY:
            continue
        elif n > int(entry.count):
            problems.append(f"{key}: {n} {unit}, but its line in {allowlist} allows {entry.count}; the list only "
                            f"shrinks. {fix}")
        elif n < int(entry.count) and not ceiling:
            problems.append(f"{key}: {n} {unit}, its line in {allowlist} says {entry.count}: lower the line to {n}")
    for key in sorted(set(listed) - set(found)):
        problems.append(f"{key} is listed in {allowlist} (line {listed[key].line}) but its offence is gone: delete "
                        f"the line")
    return problems
