"""The access policy of one drive connection (MULTI_DRIVE_RFC §11.3; slice 2b). Pure.

`decide` is called by `UserDriveService` before every provider call; the model is told the policy (the preflight lines)
but cannot widen it: the scope, the write folder and the primary are Cabinet settings (§11.9).

- Reads (read, list, open, search) stay inside the scope: the selected folders' subtrees, the whole drive, or with
  neither the feed's default (`FeedScope.AREA`: the area root, i.e. everything; `ACCOUNT`: nothing).
- Writes need a writable drive and a write folder (none: read-only, D11), stay inside the write folder's subtree (a
  create or save: the destination folder; a move: the source and the destination) and inside the scope on the same
  chain (a write folder the owner moved out of the scope on the drive accepts nothing), never rename, move or delete a
  scope folder or the write folder (protected like the root), and respect the item's own access. On an ACCOUNT feed
  the root as the write folder admits only items whose chain reaches the root (a shared item with no reachable parent
  is in the whole-drive scope, not in the root's subtree).
- A move or copy across connections is refused (D6).

Ancestry is walked only when a rule needs it (`requires_ancestry`): the App Folder default (no folders chosen, write
folder = root) never needs a chain, so it costs no provider call (G3).

Texts are English, code-built and name no provider. The Cabinet-side lines (`WRITE_FOLDER_OUTSIDE_SCOPE_LINE`,
`SCOPE_LEAVES_WRITE_FOLDER_LINE`, `SCOPE_BOTH_LINE`) are raised by `DriveConnectionService` as `DriveSettingRefused`.

Public interface:
    READ_OPS, WRITE_OPS
    scope_key(folder_ids, whole_drive) -> str
    in_scope(access, capabilities, ancestry) -> bool
    inside_write_folder(access, capabilities, ancestry) -> bool
    requires_ancestry(op, access, capabilities) -> bool
    decide(op, access, capabilities, item_access, ancestry, *, item_id="", destination=None, destination_access=None,
           other_connection=False) -> AccessDecision
    Texts: OUTSIDE_SCOPE_LINE, NO_WRITE_FOLDER_LINE, OUTSIDE_WRITE_FOLDER_LINE, READ_ONLY_DRIVE_LINE, PROTECTED_LINE,
    ITEM_FORBIDS, WRITE_FOLDER_OUTSIDE_SCOPE_LINE, SCOPE_LEAVES_WRITE_FOLDER_LINE, SCOPE_BOTH_LINE
"""
from __future__ import annotations

import hashlib
from typing import Dict, FrozenSet, Iterable, Optional

from .drive_access_decision import ALLOWED, AccessDecision
from .drive_access_op import AccessOp
from .drive_ancestry import Ancestry
from .drive_capabilities import DriveCapabilities
from .drive_connection import ROOT_FOLDER
from .drive_connection_access import ConnectionAccess
from .drive_feed_scope import FeedScope
from .drive_item_access import ItemAccess
from .drive_mounts import CROSS_DRIVE_LINE

__all__ = [
    "READ_OPS", "WRITE_OPS", "scope_key", "in_scope", "inside_write_folder", "requires_ancestry", "decide",
    "OUTSIDE_SCOPE_LINE", "NO_WRITE_FOLDER_LINE", "OUTSIDE_WRITE_FOLDER_LINE", "READ_ONLY_DRIVE_LINE",
    "PROTECTED_LINE", "ITEM_FORBIDS", "WRITE_FOLDER_OUTSIDE_SCOPE_LINE", "SCOPE_LEAVES_WRITE_FOLDER_LINE",
    "SCOPE_BOTH_LINE", "CROSS_DRIVE_LINE",
]

READ_OPS: FrozenSet[AccessOp] = frozenset({AccessOp.READ, AccessOp.LIST, AccessOp.OPEN, AccessOp.SEARCH})
WRITE_OPS: FrozenSet[AccessOp] = frozenset({AccessOp.CREATE, AccessOp.SAVE, AccessOp.APPEND, AccessOp.REPLACE,
                                            AccessOp.RENAME, AccessOp.MOVE, AccessOp.DELETE})

# Texts (RFC §11.3 rows 1 and 2 verbatim; the others are new, the RFC leaves their wording open)
OUTSIDE_SCOPE_LINE = ("This folder is not among the folders indexed for {drive}; the owner can add it in the "
                      "Cabinet.")
NO_WRITE_FOLDER_LINE = "This drive is read-only until a write folder is chosen in the Cabinet."
OUTSIDE_WRITE_FOLDER_LINE = ("{drive} can be changed only inside its write folder; the owner can choose another "
                             "in the Cabinet.")
READ_ONLY_DRIVE_LINE = "{drive} does not accept changes."
PROTECTED_LINE = ("The write folder and the folders {drive} indexes cannot be moved, renamed or deleted; change "
                  "them in the Cabinet first.")
ITEM_FORBIDS: Dict[AccessOp, str] = {
    AccessOp.OPEN: "The drive does not let the bot open this file.",
    AccessOp.READ: "The drive does not let the bot open this file.",
    AccessOp.APPEND: "The drive does not let the bot change this file's content.",
    AccessOp.REPLACE: "The drive does not let the bot change this file's content.",
    AccessOp.RENAME: "The drive does not let the bot move or rename this item.",
    AccessOp.MOVE: "The drive does not let the bot move or rename this item.",
    AccessOp.DELETE: "The drive does not let the bot delete this item.",
    AccessOp.CREATE: "The drive does not let the bot add anything to this folder.",
    AccessOp.SAVE: "The drive does not let the bot add anything to this folder.",
}
WRITE_FOLDER_OUTSIDE_SCOPE_LINE = "The write folder must be inside the folders {drive} indexes."
SCOPE_LEAVES_WRITE_FOLDER_LINE = ("The write folder {drive} has would be outside the folders indexed: choose "
                                  "another write folder first.")
SCOPE_BOTH_LINE = "Choose folders to index or the whole drive, not both."

_PROTECTABLE = frozenset({AccessOp.RENAME, AccessOp.MOVE, AccessOp.DELETE})
_SCOPE_KEY_HEX = 16


def scope_key(folder_ids: Iterable[str], whole_drive: bool) -> str:
    """A stable key of a scope, what the indexer stamps as the scope it applied: "" for the default (no folders, not
    whole), "whole" for the whole drive, else the first hex digits of the sha256 of the sorted, de-duplicated ids."""
    if whole_drive:
        return "whole"
    ids = sorted(set(folder_ids))
    if not ids:
        return ""
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:_SCOPE_KEY_HEX]


def _write_folder_is_root(access: ConnectionAccess) -> bool:
    folder = access.write_folder_id
    return folder == ROOT_FOLDER or (bool(access.root_item_id) and folder == access.root_item_id)


def _needs(ancestry: Optional[Ancestry], what: str) -> Ancestry:
    if ancestry is None:
        raise ValueError(f"the access policy needs the item's ancestry to decide {what}")
    return ancestry


def in_scope(access: ConnectionAccess, capabilities: DriveCapabilities, ancestry: Optional[Ancestry]) -> bool:
    """The item lies inside the connection's scope: the whole drive admits everything (a parentless item included);
    no folders chosen admit everything on an AREA feed (the area root is the default scope) and nothing on an ACCOUNT
    feed; else the ancestry (required) holds a scope folder."""
    if access.scope_whole_drive:
        return True
    if not access.scope_folder_ids:
        return capabilities.feed_scope is FeedScope.AREA
    chain = _needs(ancestry, "a scope with folders")
    return any(chain.contains(folder) for folder in access.scope_folder_ids)


def inside_write_folder(access: ConnectionAccess, capabilities: DriveCapabilities,
                        ancestry: Optional[Ancestry]) -> bool:
    """The item lies inside the write folder's subtree: never without a write folder; with the root as the write folder
    always on an AREA feed (no ancestry needed) and on an ACCOUNT feed only when the ancestry (required) reached the
    root; else the ancestry (required) holds the write folder."""
    if access.write_folder_id is None:
        return False
    if _write_folder_is_root(access):
        if capabilities.feed_scope is FeedScope.AREA:
            return True
        return _needs(ancestry, "the root write folder of an account-wide drive").reached_root
    return _needs(ancestry, "a write folder below the root").contains(access.write_folder_id)


def _folders_chosen(access: ConnectionAccess) -> bool:
    return bool(access.scope_folder_ids) and not access.scope_whole_drive


def requires_ancestry(op: AccessOp, access: ConnectionAccess, capabilities: DriveCapabilities) -> bool:
    """Whether `decide` needs the item's chain: a read when folders are chosen (not the whole drive); a write that can
    pass the first write checks (a writable drive with a write folder) when the write folder is a folder below the
    root, when folders are chosen (the scope check on the same chain), or on an ACCOUNT feed (the root's subtree). The
    App Folder default (AREA, no folders, the root as the write folder) needs none."""
    if op in READ_OPS:
        return _folders_chosen(access)
    if not capabilities.writable or access.write_folder_id is None:
        return False
    return (not _write_folder_is_root(access) or _folders_chosen(access)
            or capabilities.feed_scope is FeedScope.ACCOUNT)


def _item_forbids(op: AccessOp, item_access: Optional[ItemAccess]) -> bool:
    if item_access is None:
        return False
    if op in (AccessOp.APPEND, AccessOp.REPLACE):
        return not item_access.can_write_content
    if op in (AccessOp.RENAME, AccessOp.MOVE):
        return not item_access.can_rename_move
    if op is AccessOp.DELETE:
        return not item_access.can_delete
    return not item_access.can_add_children     # CREATE and SAVE: `decide` calls this for write ops only


def decide(op: AccessOp, access: ConnectionAccess, capabilities: DriveCapabilities,
           item_access: Optional[ItemAccess], ancestry: Optional[Ancestry], *, item_id: str = "",
           destination: Optional[Ancestry] = None, destination_access: Optional[ItemAccess] = None,
           other_connection: bool = False) -> AccessDecision:
    """Every row of RFC §11.3, first refusal wins. `ancestry` is the item's chain, for a create or save the destination
    folder's (and `item_access` that folder's access); `destination` and `destination_access` are a move's destination
    folder. ValueError when a rule needs a chain that was not given (`requires_ancestry`): a programming error, never a
    refusal."""
    if other_connection:
        return AccessDecision(False, CROSS_DRIVE_LINE)
    if ancestry is None and requires_ancestry(op, access, capabilities):
        raise ValueError(f"the access policy needs the item's ancestry for {op.value} on this connection")
    drive = access.drive_name
    if op in READ_OPS:
        if not in_scope(access, capabilities, ancestry):
            return AccessDecision(False, OUTSIDE_SCOPE_LINE.format(drive=drive))
        if op in (AccessOp.OPEN, AccessOp.READ) and item_access is not None and not item_access.can_download:
            return AccessDecision(False, ITEM_FORBIDS[op])
        return ALLOWED
    if not capabilities.writable:
        return AccessDecision(False, READ_ONLY_DRIVE_LINE.format(drive=drive))
    if access.write_folder_id is None:
        return AccessDecision(False, NO_WRITE_FOLDER_LINE)
    if op in _PROTECTABLE:
        target = item_id or (ancestry.item_id if ancestry is not None else "")
        protected = target in access.scope_folder_ids or (
            not _write_folder_is_root(access) and target == access.write_folder_id)
        if target and protected:
            return AccessDecision(False, PROTECTED_LINE.format(drive=drive))
    chains = [ancestry] + ([destination] if op is AccessOp.MOVE and destination is not None else [])
    for chain in chains:
        if not inside_write_folder(access, capabilities, chain):
            return AccessDecision(False, OUTSIDE_WRITE_FOLDER_LINE.format(drive=drive))
        if not in_scope(access, capabilities, chain):       # the write folder left the scope on the drive's side
            return AccessDecision(False, OUTSIDE_SCOPE_LINE.format(drive=drive))
    if _item_forbids(op, item_access):
        return AccessDecision(False, ITEM_FORBIDS[op])
    if op is AccessOp.MOVE and destination_access is not None and not destination_access.can_add_children:
        return AccessDecision(False, ITEM_FORBIDS[AccessOp.CREATE])
    return ALLOWED
