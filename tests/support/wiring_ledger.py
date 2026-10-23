"""The two wiring ledgers of the production graph and the checks that hold them exact in both directions (P9 design §5a, §5e).

A ledger line is `<key>  # <reason> <evidence>`. The reason is one word of a fixed vocabulary:

- `off-in-prod`: the deploy switches the feature off (the evidence names the variable);
- `optional-knob`: an optional setting that production leaves unset;
- `lazy`: a client or cache that is built on first use;
- `runtime`: state that only a served request fills;
- `by-design`: the code says so (the evidence names file and line, or the document);
- `defect:<id>`: a dependency production should have and does not; the id is the one in the P9 defect table.

The evidence is a few words naming where the reason is read. A line with no evidence fails.

None ledger keys: a path from `app` (exact), `Class.attr` (every walked instance of the class that has the attribute is
`None`; one instance with a value fails the line, so a half-wired class cannot hide behind it), or `Class.attr*` (any
instance may be `None`: an optional declaration field; refused on adapter, service, handler and composition
classes). Instance ledger lines: `<module.Class> <count>`.
"""
import re
from collections import Counter
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

VOCABULARY = ("off-in-prod", "optional-knob", "lazy", "runtime", "by-design")
_REASON = re.compile(r"^(?P<reason>" + "|".join(VOCABULARY) + r"|defect:D\d+)\s+(?P<evidence>\S.*)$")
# A loop that `start()` creates: it must be running, so it never has a ledger line (R3-B1).
STARTED_LOOPS = ("_flush_task", "_sweep_task")
# `Class.attr*` (any instance may be None) is for declarations; on these layers a half-wired instance must not hide.
WIRING_LAYERS = ("src.adapters.", "src.services.", "src.handlers.", "src.composition.")


class Entry(NamedTuple):
    key: str
    reason: str
    evidence: str
    line: int


def parse(text: str) -> List[Entry]:
    """Entries of a ledger. Blank lines and lines starting with `#` are skipped; a malformed line raises with its number."""
    entries = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        left, sep, right = line.partition("#")
        match = _REASON.match(right.strip()) if sep else None
        if not left.strip() or match is None or len(match["evidence"].split()) < 2:
            raise ValueError(f"line {number}: expected `<key>  # <reason> <evidence of at least two words>`, got {raw!r}")
        entries.append(Entry(left.strip(), match["reason"], match["evidence"], number))
    return entries


def _duplicates(entries: Sequence[Entry]) -> List[str]:
    seen: Dict[str, int] = {}
    problems = []
    for entry in entries:
        if entry.key in seen:
            problems.append(f"line {entry.line}: {entry.key!r} repeats line {seen[entry.key]}")
        seen.setdefault(entry.key, entry.line)
    return problems


def _split_class_attr(key: str) -> Optional[Tuple[str, str, bool]]:
    match = re.fullmatch(r"([A-Za-z_]\w*)\.(\w+)(\*?)", key)
    return (match[1], match[2], bool(match[3])) if match else None


def hits_owner(observed: Sequence[Any], hits: Sequence[int], klass: str):
    """The classes named `klass` in the MRO of the owners of the observed rows `hits`."""
    for i in hits:
        for candidate in observed[i].owner.__mro__:
            if candidate.__name__ == klass:
                yield candidate


def compare_nones(observed: Sequence[Any], entries: Sequence[Entry], values: Dict[Tuple[str, str], List[Any]]) -> List[str]:
    """Problems between the observed `None` attributes and the ledger, both ways.

    `observed`: `NoneAttribute` rows (path, owner class, attr). `values`: `graph_walk.attribute_values`.
    """
    problems = _duplicates(entries)
    covered = set()
    for entry in entries:
        if entry.key.startswith("app."):
            hits = [i for i, row in enumerate(observed) if row.path == entry.key]
            if not hits:
                problems.append(f"line {entry.line}: stale path {entry.key!r}: that attribute is not None now (or the path moved)")
            covered.update(hits)
            continue
        parts = _split_class_attr(entry.key)
        if parts is None:
            problems.append(f"line {entry.line}: {entry.key!r} is neither a path from `app` nor `Class.attr`")
            continue
        klass, attr, any_instance = parts
        hits = [i for i, row in enumerate(observed) if row.attr == attr and klass in {c.__name__ for c in row.owner.__mro__}]
        if not hits:
            problems.append(f"line {entry.line}: stale {entry.key!r}: no walked {klass} has {attr} = None")
            continue
        covered.update(hits)
        if any_instance:
            owner = next(c for c in hits_owner(observed, hits, klass))
            if owner.__module__.startswith(WIRING_LAYERS):
                problems.append(f"line {entry.line}: {entry.key!r}: `*` is refused on adapters, services, handlers and composition "
                                f"classes ({owner.__module__}); list the None instances by path")
        valued = [v for v in values.get((klass, attr), []) if v is not None]
        if valued and not any_instance:
            problems.append(
                f"line {entry.line}: {entry.key!r} claims every {klass} has {attr} = None, but {len(valued)} instance(s) have a value "
                f"(list the None ones by path, or end the key with `*` if any instance may be None)")
    for i, row in enumerate(observed):
        if i not in covered:
            problems.append(f"unledgered None: {row.path} ({row.owner.__name__}.{row.attr})")
    for entry in entries:
        if any(loop in entry.key for loop in STARTED_LOOPS):
            problems.append(f"line {entry.line}: {entry.key!r}: a started loop is never ledgered (it must be running after start())")
    return problems


def compare_store_counts(full_walk_counts: Counter, entries: Sequence[Entry]) -> List[str]:
    """Store classes counted over the full walk (agents included) must equal the ledger, so a second repository
    built inside an agent build is reported even though the container-owned count does not see it."""
    expected = parse_counts(entries)
    problems = []
    for name in sorted(full_walk_counts):
        if full_walk_counts[name] != expected.get(name, 0):
            problems.append(f"{name}: {full_walk_counts[name]} instance(s) in the full walk (agents included), "
                            f"the ledger says {expected.get(name, 0)}")
    return problems


def parse_counts(entries: Sequence[Entry]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for entry in entries:
        match = re.fullmatch(r"([\w.]+)\s+(\d+)", entry.key)
        if match is None:
            raise ValueError(f"line {entry.line}: expected `<module.Class> <count>`, got {entry.key!r}")
        counts[match[1]] = int(match[2])
    return counts


def compare_counts(observed: Counter, entries: Sequence[Entry]) -> List[str]:
    """Problems between the observed instance counts and the ledger, both ways."""
    problems = _duplicates([Entry(e.key.split()[0], e.reason, e.evidence, e.line) for e in entries])
    expected = parse_counts(entries)
    for name in sorted(set(observed) | set(expected)):
        have, want = observed.get(name, 0), expected.get(name)
        if want is None:
            problems.append(f"unledgered class: {name} has {have} instance(s)")
        elif have != want:
            problems.append(f"{name}: ledger says {want}, the graph has {have}")
    return problems
