"""
The ref classifier (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.1, P9). Pure.

A ref names the store its bytes live in. `classify_ref` only names that store; resolving a drive ref stays with
`DriveConnectionService.resolve_ref`. The order is fixed: a prefix decides before the drive codec, because
`drive_ref.is_drive_ref` finds `drive:` anywhere in the string (label punctuation tolerance), so a skill, workspace
or delivered ref whose file name holds `drive:` keeps its own store; `email:` decides first of all. A ref no rule
claims is a chat upload's name.

The kinds' values double as the `store` strings of the caps table (`document_limits`) and of the marking function
(`untrusted_text`).

Public interface:
    EMAIL_REF_PREFIX
    FileRefKind
    classify_ref(ref) -> FileRefKind
"""
from __future__ import annotations

from enum import Enum

from . import drive_ref
from .skill import DELIVERED_REF_PREFIXES, SKILL_REF_PREFIX
from .workspace import WORKSPACE_REF_PREFIX

EMAIL_REF_PREFIX = "email:"


class FileRefKind(str, Enum):
    EMAIL = "email"
    SKILL = "skill"
    WORKSPACE = "workspace"
    DELIVERED = "delivered"
    DRIVE = "drive"
    CHAT = "chat"


def classify_ref(ref: str) -> FileRefKind:
    if ref.startswith(EMAIL_REF_PREFIX):
        return FileRefKind.EMAIL
    if ref.startswith(SKILL_REF_PREFIX):
        return FileRefKind.SKILL
    if ref.startswith(WORKSPACE_REF_PREFIX):
        return FileRefKind.WORKSPACE
    if ref.startswith(DELIVERED_REF_PREFIXES):
        return FileRefKind.DELIVERED
    if drive_ref.is_drive_ref(ref):
        return FileRefKind.DRIVE
    return FileRefKind.CHAT
