"""What a connection's drive can do, as product effects (MULTI_DRIVE_RFC §7.1, T-7).

A field exists only when a service reads it and a contract case covers each value; fields grow per stage. Per handle,
because a grant can decide some of them.
- `max_path_chars`: the longest absolute path the provider accepts, read by the user drive service's path check
  (the App Folder: 400).
- `writable`: the drive accepts changes at all; read by `drive_access_policy.decide` (§11.3, writes).
- `feed_scope`: what one change cursor covers (`FeedScope`); read by `drive_access_policy.in_scope` (the default scope
  of a connection with no folders chosen) and the index planner's scope filter.
"""
from dataclasses import dataclass

from .drive_feed_scope import FeedScope


@dataclass(frozen=True)
class DriveCapabilities:
    max_path_chars: int
    writable: bool = True
    feed_scope: FeedScope = FeedScope.AREA

    def __post_init__(self) -> None:
        if self.max_path_chars <= 0:
            raise ValueError(f"max_path_chars must be positive: {self.max_path_chars}")
