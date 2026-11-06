"""The scope diff of a drive connection (docs/10_rfcs/DRIVE_SCOPE_WALK_RFC.md §5.1, §5.4, §5.9; step 1). Pure.

When the owner changes what a connection indexes, the tick compares the scope it applied with the connection's and
writes marks (walk starts, removal starts) instead of re-enumerating the account. Everything here is decided over the
stored parent map, with no provider call (W2: coverage over stored records, never id sets).

Two public dataclasses on purpose (a waiver of the one-public-class rule): `AppliedScope` is the diff's input and
`ScopeDiff` its output; neither has a use without the other and the functions below.

Public interface:
    AppliedScope(whole, roots)                                   the applied scope (`DriveSyncState.applied_*`, W1)
    ScopeDiff(restart, walk_roots, remove_roots, keep_roots, from_whole)
    applied_of(state) -> AppliedScope
    shape_valid(state) -> bool                                   applied_* agree with `applied_scope` (plan Delta 1)
    normal_form(applied, feed_scope, root_item_id) -> AppliedScope                    (§5.1)
    covers_everything(applied, feed_scope) -> bool               (as `drive_index_scope.IndexScope.covers_everything`)
    covered(item_id, roots, parents) -> bool                     (§5.1 coverage)
    plan_scope_diff(old, new, *, feed_scope, root_item_id, enumerating, parents) -> ScopeDiff   (§5.4, W13, Delta 3)
    stale_skeletons(stored_ids, ancestors, roots, *, parents=None) -> FrozenSet[str]          (§5.9, W9)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, FrozenSet, Iterable, Mapping, Optional, Set, Tuple

from .drive_access_policy import scope_key
from .drive_feed_scope import FeedScope
from .drive_index import DriveSyncState
from .drive_search import MAX_CHAIN_DEPTH


@dataclass(frozen=True)
class AppliedScope:
    """A scope as the indexer applies it: the whole drive, or a set of folder roots (empty = the provider default)."""
    whole: bool
    roots: Tuple[str, ...]


@dataclass(frozen=True)
class ScopeDiff:
    """What the tick does for one scope change (§5.4). `restart`: `scope_restart` (the new scope covers everything, or
    an enumeration is unfinished); the other fields are then empty. `walk_roots`: roots to walk (§5.5). `remove_roots`:
    roots whose stored subtree gets a guarded removal (§5.6). `keep_roots`: roots of the new scope inside a removed root
    (or, with `from_whole`, every new root): the removal stops at them and turns their path into skeletons.
    `from_whole`: the old scope covered everything, so the removal runs over every stored top-level record."""
    restart: bool
    walk_roots: Tuple[str, ...] = ()
    remove_roots: Tuple[str, ...] = ()
    keep_roots: Tuple[str, ...] = ()
    from_whole: bool = False


_RESTART = ScopeDiff(restart=True)
_NOTHING = ScopeDiff(restart=False)


def applied_of(state: DriveSyncState) -> AppliedScope:
    """The scope the state says it applied (meaningful only when `shape_valid`)."""
    return AppliedScope(whole=state.applied_whole, roots=tuple(state.applied_roots))


def shape_valid(state: DriveSyncState) -> bool:
    """`applied_whole` / `applied_roots` describe the scope `applied_scope` names (plan Delta 1). False on a state the
    step-1 code never stamped, and on one a rolled-back revision moved to a new key under the old roots (its merge save
    leaves fields it does not know): the caller then adopts or restarts (§5.11, D5)."""
    return scope_key(state.applied_roots, state.applied_whole) == state.applied_scope


def normal_form(applied: AppliedScope, feed_scope: FeedScope, root_item_id: str) -> AppliedScope:
    """§5.1: `WHOLE`, or `FOLDERS(sorted unique roots)`; an `AREA` feed's default (no folders) is `FOLDERS({area
    root})`, an `ACCOUNT` feed's default `FOLDERS(∅)`. An `AREA` default with no known root stays `FOLDERS(∅)`."""
    if applied.whole:
        return AppliedScope(whole=True, roots=())
    roots = tuple(sorted(set(applied.roots)))
    if not roots and feed_scope is FeedScope.AREA and root_item_id:
        roots = (root_item_id,)
    return AppliedScope(whole=False, roots=roots)


def covers_everything(applied: AppliedScope, feed_scope: FeedScope) -> bool:
    """The whole drive, or an `AREA` feed's default: built by enumeration, its feed items need no verdict. The same
    rule as `drive_index_scope.IndexScope.covers_everything`; takes the scope as saved, not its normal form."""
    return applied.whole or (feed_scope is FeedScope.AREA and not applied.roots)


def covered(item_id: str, roots: AbstractSet[str], parents: Mapping[str, str]) -> bool:
    """The item is a root, or its stored parent chain reaches one within `MAX_CHAIN_DEPTH` links (§5.1). The walk ends
    "not covered" at a parent `""`, at an item the map does not hold (the drive root has no stored parent), and on a
    chain longer than `MAX_CHAIN_DEPTH` (a cycle or corrupt data)."""
    current = item_id
    for _ in range(MAX_CHAIN_DEPTH + 1):
        if current in roots:
            return True
        parent = parents.get(current)
        if not parent:
            return False
        current = parent
    return False


def plan_scope_diff(old: AppliedScope, new: AppliedScope, *, feed_scope: FeedScope, root_item_id: str,
                    enumerating: bool, parents: Mapping[str, str]) -> ScopeDiff:
    """The diff from the applied scope `old` to the connection's `new` (§5.4), over the stored parent map `parents`
    (item id -> parent id of every stored record, skeletons and `DELETING` ones included).

    - any change while an enumeration is unfinished: restart (W13, extended to folder scopes by plan Delta 3);
    - the same scope in normal form: nothing to do;
    - a new scope that covers everything (`FOLDERS` -> `WHOLE`, or -> an `AREA` default): restart, it is built by
      enumeration (D1);
    - an old scope that covered everything -> folders: `from_whole`, keep = the new roots (case 5a; the OneDrive
      default -> folders, case 5c);
    - `FOLDERS(A)` -> `FOLDERS(B)`: walk = roots of B not covered by A; remove = roots of A not covered by B; keep =
      roots of B covered by a removed root (cases 3a, 3b). A root re-added while its removal runs is not covered by the
      applied roots, so it is walked (case 8c)."""
    if enumerating:
        return _RESTART
    before = normal_form(old, feed_scope, root_item_id)
    after = normal_form(new, feed_scope, root_item_id)
    if before == after:
        return _NOTHING
    if covers_everything(new, feed_scope):
        return _RESTART
    if covers_everything(old, feed_scope):
        return ScopeDiff(restart=False, keep_roots=after.roots, from_whole=True)
    a, b = frozenset(before.roots), frozenset(after.roots)
    walk = tuple(r for r in after.roots if not covered(r, a, parents))
    remove = tuple(r for r in before.roots if not covered(r, b, parents))
    removed = frozenset(remove)
    keep = tuple(r for r in after.roots if removed and covered(r, removed, parents))
    return ScopeDiff(restart=False, walk_roots=walk, remove_roots=remove, keep_roots=keep)


def stale_skeletons(stored_ids: Iterable[str], ancestors: AbstractSet[str], roots: AbstractSet[str], *,
                    parents: Optional[Mapping[str, str]] = None) -> FrozenSet[str]:
    """The stored skeleton records (`stored_ids`) that lie on no current root's path (§5.9, W9). `ancestors`: the
    union of the paths from the drive root to every current root, from a **complete** load (the caller never calls this
    on a partial one). A skeleton that is itself a root, or (with `parents`) is covered by one, is in scope now: it is
    the caller's to re-classify, never stale."""
    stale: Set[str] = set()
    for item_id in stored_ids:
        if item_id in ancestors or item_id in roots:
            continue
        if parents is not None and covered(item_id, roots, parents):
            continue
        stale.add(item_id)
    return frozenset(stale)
