"""
What `FileConversionService.open` and `.prefetch` return (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.2, §4.4, P18). Pure.

Three public classes on purpose (CLAUDE.md's one-class rule waived): the answers of one open path, read together.

`OpenedFile` is exactly one of three answers: `body`, today's `"[File: <name>]\\n<text>\\n[/File: <name>]"`, raw,
unmarked and uncut (the store's presenter marks and cuts it, §4.5, §4.6); `note`, a `[System: …]` note; or a native
file, `native_path` with its `native_mime`, for the consumer to attach. `outcome` is the read's, None when no read
happened (a refusal before download).

`Prefetched` is a specialist prefetch's answer (P18): the text as `content`, or a `note` the specialist reports when
the file could not be read.

`EmailOpening` is `FileConversionService.open_email`'s answer (§4.7, D12, plan U4 §6): the `opened` file, or a `note`
when no read happened (the message is gone, the part is missing or empty, the mailbox did not answer, not connected,
not configured, or a choice is needed). `ref`, `name`, `size` and `mime` are the opened part's (its `label` and the
`email_file_context` entry are built from them), empty otherwise. `candidates` are the `(label, ref)` pairs of the
parts to choose from, each label the full `email_file_label`, when several matched (a shared name, or no name and no
ref for an email with several attachments); then nothing is opened, `note` lists them and `needs_choice` is True.
A choice is a successful answer, never an exception: the caller tells it from an open by `needs_choice`.

Public interface:
    OpenedFile(outcome, body="", note="", native_path="", native_mime="")   ValueError unless exactly one answer
    Prefetched(content="", note="")                                         ValueError when both are set
    EmailOpening(opened=None, ref="", name="", size=0, mime="", candidates=(), note="")
                                                                            ValueError unless exactly one of opened
                                                                            and note; candidates only with a note
        .needs_choice -> bool                                               several parts matched, none opened
        .label -> str                                                       the opened part's label; "" without a ref
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .document_read import ReadOutcome
from .email_ref import email_file_label


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


@dataclass(frozen=True)
class EmailOpening:
    opened: Optional[OpenedFile] = None
    ref: str = ""
    name: str = ""
    size: int = 0
    mime: str = ""
    candidates: Tuple[Tuple[str, str], ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if (self.opened is None) == (not self.note):
            raise ValueError("an email opening is exactly one of an opened file or a note")
        if self.candidates and self.opened is not None:
            raise ValueError("candidates are listed only when nothing was opened")

    @property
    def needs_choice(self) -> bool:
        return bool(self.candidates)

    @property
    def label(self) -> str:
        return email_file_label(self.name, self.size, self.mime, self.ref) if self.ref else ""
