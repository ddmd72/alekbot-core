"""Stubs for `FileConversionService.open`: the `OpenedFile` a fake conversion service answers (U3 part D)."""
from typing import Optional

from src.domain.document_open import OpenedFile
from src.domain.document_read import ReadOutcome, ReadStatus


def opened_body(text: str, outcome: Optional[ReadOutcome] = None) -> OpenedFile:
    """A read that gave text."""
    return OpenedFile(outcome=outcome or ReadOutcome(status=ReadStatus.TEXT, text=text), body=text)


def opened_note(note: str, status: ReadStatus = ReadStatus.ERROR, detail: str = "") -> OpenedFile:
    """A read that gave a `[System: …]` note."""
    return OpenedFile(outcome=ReadOutcome(status=status, detail=detail), note=note)


def opened_native(path: str, mime: str) -> OpenedFile:
    """A file the consumer attaches natively."""
    return OpenedFile(outcome=ReadOutcome(status=ReadStatus.NATIVE, native_mime=mime), native_path=path,
                      native_mime=mime)
