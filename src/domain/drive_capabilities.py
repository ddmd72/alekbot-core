"""What a connection's drive can do, as product effects (MULTI_DRIVE_RFC §7.1, T-7).

A field exists only when a service reads it and a contract case covers each value; fields grow per stage. Per handle,
because a grant can decide some of them. Stage 1 has one field:
- `max_path_chars`: the longest absolute path the provider accepts, read by the user drive service's path check
  (the App Folder: 400).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class DriveCapabilities:
    max_path_chars: int

    def __post_init__(self) -> None:
        if self.max_path_chars <= 0:
            raise ValueError(f"max_path_chars must be positive: {self.max_path_chars}")
