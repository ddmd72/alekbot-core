"""
What `FileConversionService.open` and `.prefetch` return (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.2, §4.4, P18). Pure.

Two public classes on purpose (CLAUDE.md's one-class rule waived): the two answers of one open path, read together.

`OpenedFile` is exactly one of three answers: `body`, today's `"[File: <name>]\\n<text>\\n[/File: <name>]"`, raw,
unmarked and uncut (the store's presenter marks and cuts it, §4.5, §4.6); `note`, a `[System: …]` note; or a native
file, `native_path` with its `native_mime`, for the consumer to attach. `outcome` is the read's, None when no read
happened (a refusal before download).

`Prefetched` is a specialist prefetch's answer (P18): the text as `content`, or a `note` the specialist reports when
the file could not be read.

Public interface:
    OpenedFile(outcome, body="", note="", native_path="", native_mime="")   ValueError unless exactly one answer
    Prefetched(content="", note="")                                         ValueError when both are set
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .document_read import ReadOutcome


@dataclass(frozen=True)
class OpenedFile:
    outcome: Optional[ReadOutcome]
    body: str = ""
    note: str = ""
    native_path: str = ""
    native_mime: str = ""

    def __post_init__(self) -> None:
        answers = sum(1 for answer in (self.body, self.note, self.native_path) if answer)
        if answers != 1:
            raise ValueError("an opened file is exactly one of a body, a note or a native file")
        if bool(self.native_path) != bool(self.native_mime):
            raise ValueError("a native file needs both its path and its mime type")


@dataclass(frozen=True)
class Prefetched:
    content: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.content and self.note:
            raise ValueError("a prefetch gives its content or a note, not both")
