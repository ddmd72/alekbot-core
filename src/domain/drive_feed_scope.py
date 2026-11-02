"""What one change cursor of a drive covers (MULTI_DRIVE_RFC §7.1, §11.3): the adapter declares it in
`DriveCapabilities.feed_scope`; the default scope of a connection with no folders chosen follows from it."""
from __future__ import annotations

from enum import Enum


class FeedScope(str, Enum):
    AREA = "area"          # one cursor covers an area with a root (an App Folder); the area root is the default scope
    ACCOUNT = "account"    # one cursor covers the whole account, no area root; the default scope is nothing
