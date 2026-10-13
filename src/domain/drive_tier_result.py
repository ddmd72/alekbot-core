"""
Drive tier result — what a Search v2 query result carries besides its cards (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§25.5.6, render contract §25.13.6). Pure data; `drive_tier_render` turns it into the text Smart reads.

Several public classes in one module, by decision: one result contract, read together (CLAUDE.md's one-class rule is
waived here on purpose, as in `drive_match`).

Public interface:
    GroupMember     a tier member past the shown cards: its top folder, kind and month (group lines)
    TierBlock       one tier's totals and its unshown members
    CoverageFacts   what the coverage line names (P7)
    TieredResult    the searched line, coverage, tiers, D29 and footer facts of one query
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .drive_match import MatchTier


@dataclass(frozen=True)
class GroupMember:
    """A tier member that is not shown as a card: grouped by its folder × kind, with its month (`YYYY-MM`, "" when
    undated)."""
    folder: str
    kind: str
    month: str


@dataclass(frozen=True)
class TierBlock:
    """One non-empty tier: how many files it holds (`at_least` when a candidate page was full) and the members past
    the shown cards (the render groups them and cuts more cards into them to fit RESULT_CHARS_MAX)."""
    tier: MatchTier
    total: int
    at_least: bool = False
    rest: Tuple[GroupMember, ...] = ()
    nonce: str = ""                  # the group lines' wrapper


@dataclass(frozen=True)
class CoverageFacts:
    """The coverage line's parts (§25.5.6), each named only when non-zero. Never counts folders, photos, audio,
    video or other kinds that hold no text."""
    not_described: int = 0          # text files whose profile or passages job is ready, deferred or parked
    not_describable: int = 0        # ... held
    no_passages: int = 0            # text files without doc2query by policy (books, sheets, data); shown only when
                                    # the Mentioned and By-translation tiers are empty
    not_read_yet: int = 0
    unread: int = 0
    building: bool = False
    reading: bool = False
    refreshing: bool = False        # vectors in another embedding space: meaning was not searched
    rekeying: bool = False          # a keys pass is in progress (§25.4.8)
    full_page: bool = False         # a candidate page was full: more files may match
    keys_cut: int = 0               # shown files whose keys a cap cut
    cut: Tuple[str, ...] = ()       # words past MAX_CONCEPTS
    names_only: bool = False        # the policy admits no content: only names and metadata were searched


@dataclass(frozen=True)
class TieredResult:
    """A query result's facts besides its cards (the cards carry their tier). `absent` words are held by no file
    user-wide; `absent_in_scope` exist outside the filter or folder only. `without` counts the files matching the
    other words (D29), None when there are no other words. `conjunction` is the empty-conjunction footer's (word,
    files) pairs."""
    concepts: Tuple[str, ...]
    ignored: Tuple[str, ...] = ()
    common: Tuple[str, ...] = ()
    migration: bool = False
    coverage: CoverageFacts = CoverageFacts()
    blocks: Tuple[TierBlock, ...] = ()
    related_small: bool = False
    absent: Tuple[str, ...] = ()
    absent_in_scope: Tuple[str, ...] = ()
    without: Optional[int] = None
    without_at_least: bool = False
    conjunction: Tuple[Tuple[str, int], ...] = ()
    expired: int = 0

    def block(self, tier: MatchTier) -> Optional[TierBlock]:
        return next((b for b in self.blocks if b.tier is tier), None)
