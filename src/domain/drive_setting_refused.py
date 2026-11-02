"""A Cabinet setting of a drive connection refused by a rule (MULTI_DRIVE_RFC §11.3, §11.7, I2): the write folder
outside the scope, a scope that leaves the write folder, a read-only primary, an invalid heir. `line` is shown to the
owner as it is."""
from __future__ import annotations


class DriveSettingRefused(Exception):

    def __init__(self, line: str) -> None:
        super().__init__(line)
        self.line = line
