"""
The email attachment ref codec and label (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.1, §4.7 P8a/P8b, D4). Pure.

An email attachment is addressed as `email:<message id>/<part id>`: the provider's message id and the MIME part id
(Gmail's `partId`, dotted digits for a nested part: `0`, `1.2`). The provider's attachment id is never in a ref: it is
not stable across message reads, so a ref is resolved through one fresh part listing per open.

`EMAIL_FILE_CONTEXT_KEY` is the history-context key under which `get_email_details` and `open_email_attachment` keep
`[{label, ref}]` for the next turn (P8b, plan U4 §6: the full label, not the name alone); the generic `*_context`
persistence carries it.

Public interface:
    EMAIL_FILE_CONTEXT_KEY
    format_email_ref(message_id, part_id) -> str        ValueError for an id of the wrong shape
    parse_email_ref(ref) -> (message_id, part_id)       ValueError for a ref of the wrong shape
    escape_email_name(name) -> str                      a sender's name, safe inside quotes in a system-made line
    email_file_label(name, size_bytes, mime, ref) -> str
                                                        `[Email file: "<name>" (size, mime) ref=email:<msg>/<part>]`
"""
from __future__ import annotations

import re
from typing import Tuple

from .file_ref import EMAIL_REF_PREFIX
from .untrusted_text import escape_attribute
from .user_drive import format_size

EMAIL_FILE_CONTEXT_KEY = "email_file_context"

_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]+")
_PART_ID = re.compile(r"[0-9]+(?:\.[0-9]+)*")
# Control characters (a newline first) in a sender's name would let it print a line of its own under the label.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A bare mime type (RFC 6838 restricted names): the part's declared type is the sender's text and sits unquoted in the
# label, so anything else is left out rather than printed.
_MIME = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")


def _check(message_id: str, part_id: str) -> None:
    if not _MESSAGE_ID.fullmatch(message_id):
        raise ValueError(f"not an email message id: {message_id!r}")
    if not _PART_ID.fullmatch(part_id):
        raise ValueError(f"not an email part id: {part_id!r}")


def format_email_ref(message_id: str, part_id: str) -> str:
    _check(message_id, part_id)
    return f"{EMAIL_REF_PREFIX}{message_id}/{part_id}"


def parse_email_ref(ref: str) -> Tuple[str, str]:
    if not ref.startswith(EMAIL_REF_PREFIX):
        raise ValueError(f"not an email ref: {ref!r}")
    message_id, slash, part_id = ref[len(EMAIL_REF_PREFIX):].partition("/")
    if not slash:
        raise ValueError(f"an email ref names a message and a part: {ref!r}")
    _check(message_id, part_id)
    return message_id, part_id


def escape_email_name(name: str) -> str:
    """A sender's attachment name as it may stand inside quotes in a system-made line: escaped as a wrapper attribute
    (it can neither end the quoted value nor open a tag), control characters shown as spaces (no line of its own)."""
    return escape_attribute(_CONTROL.sub(" ", name))


def email_file_label(name: str, size_bytes: int, mime: str, ref: str) -> str:
    """The label the model sees for one attachment (§4.1; plan U4 §6 adds the mime). The name is the sender's text,
    quoted and escaped by `escape_email_name`; the mime is the part's declared type, printed bare and lowercase only
    when it has a mime's shape (a parameter is dropped); the ref is checked, never printed unparsed. A system-made line: it goes after any untrusted wrapper, never inside one."""
    parse_email_ref(ref)
    bare = mime.split(";")[0].strip().lower()
    kind = f", {bare}" if _MIME.fullmatch(bare) else ""
    return f'[Email file: "{escape_email_name(name)}" ({format_size(size_bytes)}{kind}) ref={ref}]'
