"""The drive ref codec (MULTI_DRIVE_RFC §9, E10): `drive:<tag>:<item_id>`.

The tag names the connection: 2 to 6 characters `[a-z0-9]`, starting with a letter, random, unique per user, immutable,
never reused (a DISCONNECTED connection keeps its own). The item id is the provider's, opaque, and may itself hold `:`:
parsing splits on the FIRST `:` after `drive:`. A part before that colon that is not a tag (or no colon at all) makes the
ref untagged: the form written before the drives were reorganised, which is never resolved (D13).

A ref copied out of a label may carry the label's punctuation (`ref=drive:a1:x]`, `"drive:a1:x"`); `parse` finds the ref
inside any text and stops at whitespace, quotes, brackets, parentheses and commas, as the untagged codec did.

Public interface:
    DriveRef(tag, item_id)                     a tagged ref; str() is its text
    format(tag, item_id) -> str                `drive:<tag>:<item_id>`; ValueError for a bad tag or an empty item id
    parse(text) -> DriveRef | UntaggedRef | None   the first `drive:` ref in the text; None when there is none
    is_drive_ref(text) -> bool                 any `drive:` ref, tagged or not
    is_valid_tag(tag) -> bool
    REF_PREFIX, TAG_MIN_LEN, TAG_MAX_LEN, TAG_FIRST_ALPHABET, TAG_ALPHABET
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union

from .drive_untagged_ref import UntaggedRef

REF_PREFIX = "drive:"
TAG_MIN_LEN = 2
TAG_MAX_LEN = 6
TAG_FIRST_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
TAG_ALPHABET = TAG_FIRST_ALPHABET + "0123456789"

_TAG_RE = re.compile(rf"[a-z][a-z0-9]{{{TAG_MIN_LEN - 1},{TAG_MAX_LEN - 1}}}")
# The ref body runs to the first character a label or a sentence puts around it.
_REF_RE = re.compile(r"drive:([^\s\"'\]\[)(,]+)")


@dataclass(frozen=True)
class DriveRef:
    tag: str
    item_id: str

    def __post_init__(self) -> None:
        if not is_valid_tag(self.tag):
            raise ValueError(f"not a drive ref tag: {self.tag!r}")
        if not self.item_id:
            raise ValueError("a drive ref needs an item id")

    def __str__(self) -> str:
        return f"{REF_PREFIX}{self.tag}:{self.item_id}"


def is_valid_tag(tag: str) -> bool:
    return bool(_TAG_RE.fullmatch(tag or ""))


def format(tag: str, item_id: str) -> str:   # the RFC's name, shadowing the builtin here only: call `drive_ref.format`
    return str(DriveRef(tag=tag, item_id=item_id))


def parse(text: str) -> Optional[Union[DriveRef, UntaggedRef]]:
    if not text or REF_PREFIX not in text:
        return None
    match = _REF_RE.search(text)
    if match is None:
        return None
    body = match.group(1)
    head, sep, rest = body.partition(":")
    if sep and rest and is_valid_tag(head):
        return DriveRef(tag=head, item_id=rest)
    return UntaggedRef(raw=f"{REF_PREFIX}{body}")


def is_drive_ref(text: str) -> bool:
    return parse(text) is not None
