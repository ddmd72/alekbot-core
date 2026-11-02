"""The drive operations the access policy decides on (MULTI_DRIVE_RFC §11.3)."""
from __future__ import annotations

from enum import Enum


class AccessOp(str, Enum):
    READ = "read"
    LIST = "list"
    OPEN = "open"
    SEARCH = "search"
    CREATE = "create"
    SAVE = "save"
    APPEND = "append"
    REPLACE = "replace"
    RENAME = "rename"
    MOVE = "move"
    DELETE = "delete"
