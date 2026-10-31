"""A drive ref that resolves to no connection, with the code-built line the caller answers (MULTI_DRIVE_RFC §9, D13).

- `stale_ref_line(ref)`: an untagged ref (written before the drives were reorganised), or a tag the user never had.
- `disconnected_ref_line(ref)`: the tag of a DISCONNECTED connection (tags are never reused, so the ref cannot point to
  a newer connection).
No legacy resolver and no history rewrite: the line is the whole answer, and the model's next search yields tagged refs.
"""
from dataclasses import dataclass

STALE_REF_LINE = "Ref {ref} is from before the drives were reorganised; search for the file again."
DISCONNECTED_REF_LINE = "Ref {ref} belongs to a drive that is no longer connected."


@dataclass(frozen=True)
class DriveRefRefusal:
    line: str


def stale_ref_line(ref: str) -> str:
    return STALE_REF_LINE.format(ref=ref)


def disconnected_ref_line(ref: str) -> str:
    return DISCONNECTED_REF_LINE.format(ref=ref)
