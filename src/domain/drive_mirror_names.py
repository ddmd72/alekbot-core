"""
Drive transcript mirror — names, collisions, path length, placement and move order
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.5, §27.9.6, §27.9.7; Δ189, Δ192, Δ193, Δ216). Pure: no I/O, no clock.

Names are compared case-insensitively after NFC (the drive refuses names that differ only by case). Paths are counted as
the drive counts them: the decoded path from the drive root, App Folder path included, as `/`-joined components with no
leading slash.

Several public classes in one module, by decision: Sibling, Placement and Move are the value types of one contract (the
mirror's naming and ordering), read together as `drive_enrichment` does; the one-class rule is waived here on purpose.

Notes. A losing cut name may exceed MIRROR_NAME_MAX by its suffix (S1 accepted 304 characters). Lengths are code points;
an astral character counts 2 in UTF-16, which the per-action path-too-long route (Δ204, Δ216) covers. The temporary name
of a cycle is not checked against static siblings: a 409 on it is non-destructive.

Public interface:
    fold(name), root_name_candidates(), is_root_name(name), mirror_file_name(source_name), reserved_names(level)
    Sibling, assign_names(siblings, reserved, taken)
    full_path_length(area_path, root_path, parts), fits(area_path, root_path, parts)
    Placement, place(...), archive_place(...)
    Move, temp_name(name, key), order_moves(moves)
    driver_keys(concepts)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .drive_index_params import MIRROR_NAME_MAX, MIRROR_PATH_MARGIN, ONEDRIVE_PATH_MAX
from .drive_match import Concept
from .user_drive import name_key
from .drive_mirror import ARCHIVE_NAME, LONG_PATHS_NAME, README_NAME, ROOT_NAME

LEVEL_ROOT = "root"
LEVEL_DAY = "day"
LEVEL_OTHER = "other"

DRIVER_KEYS_MAX = 30     # `array-contains-any`'s limit (§27.9.7)
_MD = ".md"


def _hex8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


fold = name_key     # the comparison form of a name: casefold(NFC(name)), one definition (user_drive)

_ROOT_NAME_RE = re.compile("^" + re.escape(fold(ROOT_NAME)) + r"( \(alek( [0-9]+)?\))?$")


def root_name_candidates() -> Tuple[str, ...]:
    """The root's names in order of preference (§27.9.2): `_Transcripts`, `_Transcripts (Alek)`, `(Alek 2)` .. `(Alek 5)`."""
    return (ROOT_NAME, f"{ROOT_NAME} (Alek)", *(f"{ROOT_NAME} (Alek {n})" for n in range(2, 6)))


def is_root_name(name: str) -> bool:
    """A name the root takes or would take (§27.9.2, §27.9.9): `_Transcripts`, `_Transcripts (Alek)` or
    `_Transcripts (Alek <n>)`, compared after `fold`. The audit reports a folder of such a name that is no known root
    (the crash orphan of §27.9.2); it never adopts one."""
    return bool(_ROOT_NAME_RE.match(fold(name)))


def mirror_file_name(source_name: str) -> str:
    """`<source name>.md`; one over MIRROR_NAME_MAX is cut, the whole result staying within it, and gets
    `~<8 hex of sha256(source name)>` before `.md` (§27.9.7)."""
    full = source_name + _MD
    if len(full) <= MIRROR_NAME_MAX:
        return full
    tail = f"~{_hex8(source_name)}{_MD}"
    return source_name[:MIRROR_NAME_MAX - len(tail)] + tail


def reserved_names(level: str) -> Tuple[str, ...]:
    """Names a source item may not take: the root's three, a day folder's `Long paths`, none elsewhere."""
    if level == LEVEL_ROOT:
        return (README_NAME, ARCHIVE_NAME, LONG_PATHS_NAME)
    if level == LEVEL_DAY:
        return (LONG_PATHS_NAME,)
    return ()


@dataclass(frozen=True)
class Sibling:
    """One document of ours in a mirror folder: the source item id, the name it wants, and whether it is a folder."""
    item_id: str
    name: str
    is_folder: bool = False


def _suffixed(name: str, item_id: str, is_folder: bool, salt: int) -> str:
    tag = "~" + _hex8(item_id if salt == 0 else f"{item_id}#{salt}")
    if is_folder:
        return name + tag
    dot = name.rfind(".")
    if dot > 0:
        return name[:dot] + tag + name[dot:]
    return name + tag


def assign_names(siblings: Sequence[Sibling], reserved: Iterable[str] = (), taken: Iterable[str] = ()) -> Dict[str, str]:
    """Final name of each sibling, by item id (Δ192). Reserved names and names a foreign item holds (`taken`) belong to
    nobody of ours; among the rest folders go before files, ties by item id, and a loser takes
    `~<8 hex of sha256(item id)>` before a file's extension, at the end of a folder's name. A pure function of the
    current siblings, so a suffix goes when its collision does."""
    blocked = {fold(n) for n in reserved} | {fold(n) for n in taken}
    ordered = sorted(siblings, key=lambda s: (0 if s.is_folder else 1, s.item_id))
    held: Set[str] = set(blocked)
    out: Dict[str, str] = {}
    losers: List[Sibling] = []
    for s in ordered:
        key = fold(s.name)
        if key in held:
            losers.append(s)
        else:
            held.add(key)
            out[s.item_id] = s.name
    for s in losers:
        salt = 0
        name = _suffixed(s.name, s.item_id, s.is_folder, salt)
        while fold(name) in held:
            salt += 1
            name = _suffixed(s.name, s.item_id, s.is_folder, salt)
        held.add(fold(name))
        out[s.item_id] = name
    return out


def _components(path: str) -> List[str]:
    return [c for c in path.split("/") if c]


def full_path_length(area_path: str, root_path: str, parts: Sequence[str]) -> int:
    """Length of the decoded path from the drive root: the App Folder's path, the root's path (as it is now), then
    `parts` (the mirror path below the root), joined by `/` (§27.9.7, Δ193)."""
    comps = _components(area_path) + _components(root_path) + list(parts)
    return sum(len(c) for c in comps) + max(len(comps) - 1, 0)


def fits(area_path: str, root_path: str, parts: Sequence[str]) -> bool:
    """True when the path is within the drive's path limit minus MIRROR_PATH_MARGIN."""
    return full_path_length(area_path, root_path, parts) <= ONEDRIVE_PATH_MAX - MIRROR_PATH_MARGIN


@dataclass(frozen=True)
class Placement:
    """Where a mirror file goes: its path below the root, and whether it was sent to a `Long paths` folder."""
    parts: Tuple[str, ...]
    long_path: bool = False


def place(active_parts: Sequence[str], *, area_path: str, root_path: str) -> Placement:
    """The active location, or `Long paths/<name>` when the path does not fit (§27.9.7)."""
    parts = tuple(active_parts)
    if not parts:
        raise ValueError("place: active_parts is empty")
    if fits(area_path, root_path, parts):
        return Placement(parts)
    return Placement((LONG_PATHS_NAME, parts[-1]), True)


def archive_place(day: str, active_parts: Sequence[str], *, area_path: str, root_path: str) -> Placement:
    """`Archive/<day>/<mirror path>`, or `Archive/<day>/Long paths/<name>` when it does not fit (§27.9.6, Δ189)."""
    if not active_parts:
        raise ValueError("archive_place: active_parts is empty")
    parts = (ARCHIVE_NAME, day, *active_parts)
    if fits(area_path, root_path, parts):
        return Placement(parts)
    return Placement((ARCHIVE_NAME, day, LONG_PATHS_NAME, active_parts[-1]), True)


@dataclass(frozen=True)
class Move:
    """One write of a reconciliation: document `key` goes from (`from_parent`, `from_name`) to (`to_parent`, `to_name`).
    A creation has no origin (both `from_*` None). A case-only rename is one move (Δ216)."""
    key: str
    from_parent: Optional[str]
    from_name: Optional[str]
    to_parent: str
    to_name: str

    def __post_init__(self) -> None:
        if (self.from_parent is None) != (self.from_name is None):
            raise ValueError("Move: from_parent and from_name are both set or both None")

    @property
    def is_create(self) -> bool:
        return self.from_name is None


def temp_name(name: str, key: str) -> str:
    """The temporary name that breaks a cycle: `<name>~tmp<8 hex of sha256(key)>`."""
    return f"{name}~tmp{_hex8(key)}"


def _slot(parent: Optional[str], name: str) -> Tuple[Optional[str], str]:
    return (parent, fold(name))


def order_moves(moves: Sequence[Move]) -> List[Move]:
    """Order `moves` so none targets a name another document of ours still holds (Δ192): the holder vacates first.
    A cycle (`a <-> b`) is broken by moving one member to a temporary name in its own folder (one extra move) and
    finishing it last. A creation whose name a document must yield is ordered after that yield. Deterministic."""
    pending = [m for m in moves if m.is_create or (m.from_parent, m.from_name) != (m.to_parent, m.to_name)]
    out: List[Move] = []
    while pending:
        holders = {_slot(m.from_parent, m.from_name): m for m in pending if not m.is_create and m.from_name is not None}

        def blocker(m: Move) -> Optional[Move]:
            h = holders.get(_slot(m.to_parent, m.to_name))
            return h if h is not None and h.key != m.key else None

        ready = next((m for m in pending if blocker(m) is None), None)
        if ready is not None:
            out.append(ready)
            pending.remove(ready)
            continue
        node = min(pending, key=lambda m: m.key)       # every move is blocked: follow blockers into a cycle
        seen: Dict[str, int] = {}
        chain: List[Move] = []
        while node.key not in seen:
            seen[node.key] = len(chain)
            chain.append(node)
            nxt = blocker(node)
            if nxt is None:                                 # unreachable: every pending move is blocked here
                raise ValueError("order_moves: inconsistent blockers")
            node = nxt
        victim = min(chain[seen[node.key]:], key=lambda m: m.key)
        base = victim.from_name or ""                       # a cycle member is never a creation
        tmp = temp_name(base, victim.key)
        out.append(Move(victim.key, victim.from_parent, base, victim.from_parent or "", tmp))
        pending.remove(victim)
        pending.append(Move(victim.key, victim.from_parent, tmp, victim.to_parent, victim.to_name))
    return out


def driver_keys(concepts: Sequence[Concept]) -> Tuple[str, ...]:
    """Keys that drive the deleted-files query (§27.9.7): the word concept with the longest word (numbers only when the
    query has no word concept), its `word_keys` in the concept's own order, the first DRIVER_KEYS_MAX."""
    words = [c for c in concepts if not c.is_number]
    pool = words or list(concepts)
    if not pool:
        return ()
    best = max(pool, key=lambda c: len(c.word))      # max keeps the first of equals
    return tuple(best.word_keys[:DRIVER_KEYS_MAX])
