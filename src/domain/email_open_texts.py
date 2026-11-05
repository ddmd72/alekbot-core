"""
The notes an email attachment open can come back as (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.7 "Notes", D11, D12). Pure.

One wording for every path that resolves an `email:` ref (`FileConversionService.open_email` now, `resolve_file` in
U4b), so the model reads the same line whichever tool met the case. The reader's own outcomes (a scan not read in time,
a file above a cap) keep `document_open_texts`' notes. The not-connected line is today's (`EmailSearchService`).

Public interface:
    EMAIL_GONE_NOTE                  the message is gone (Gmail 404, or the mailbox is another account now)
    ATTACHMENT_MISSING_NOTE          the message exists, the part does not
    ATTACHMENT_EMPTY_NOTE            the part has neither an attachment id nor data
    MAILBOX_UNAVAILABLE_NOTE         Gmail 429 / 5xx / timeout
    EMAIL_NOT_CONNECTED_NOTE         no mail credential, or it was disconnected; the user reconnects
    EMAIL_OPEN_NOT_CONFIGURED_NOTE   no file storage, so no open path is built (§4.7)
    EMAIL_FILE_NOT_TRANSFERABLE_NOTE an `email:` ref given to save or send before U4b builds it
    NO_ATTACHMENTS_NOTE              the email has no attachment at all
    choose_one_note(labels, name="") D12 and plan U4 §6: several parts match (a name shared by several, or no name and
                                     no ref given for an email with several); none is opened, their labels are listed.
                                     A successful answer, not a failure: the model picks one ref and calls again.
    active_kind_refusal_note(kind)   D13: `send_file_to_user` refuses an active kind from an email (`active_kind`); for
                                     `UNCHECKED` the macro check could not run, so the note says to try again
"""
from __future__ import annotations

from typing import Sequence

from .active_kind import ACTIVE_KIND_LABELS, UNCHECKED
from .email_ref import escape_email_name

EMAIL_GONE_NOTE = "[System: That email is no longer in the mailbox.]"
ATTACHMENT_MISSING_NOTE = "[System: That attachment is no longer in this email.]"
ATTACHMENT_EMPTY_NOTE = "[System: That attachment is empty.]"
MAILBOX_UNAVAILABLE_NOTE = "[System: The mailbox did not answer; try again shortly.]"
EMAIL_NOT_CONNECTED_NOTE = (
    "[Error: Gmail is not connected for this user. Ask the user to reconnect via /link-gmail.]")
NO_ATTACHMENTS_NOTE = "[System: That email has no attachments.]"
EMAIL_OPEN_NOT_CONFIGURED_NOTE = "[System: Attachment opening is not configured.]"
EMAIL_FILE_NOT_TRANSFERABLE_NOTE = (
    "[System: An email attachment cannot be saved to the drive or sent to the chat by this route. Read it with open_email_attachment or send "
    "it with send_file_to_user; to use it here, keep it on the drive first (work_with_drive_files copies it) and "
    "pass the drive: ref that returns. The user can open it in Gmail.]")


def active_kind_refusal_note(kind: str) -> str:
    """The refusal of D13 (EXTERNAL_FILES_RFC §4.2): the kind in words, that such files are not forwarded, and Gmail.
    `kind` is an `active_kind` id; an unknown id is named generically. Not a failure of the turn: the caller answers
    it as the tool's result."""
    if kind == UNCHECKED:
        return ("[System: This email attachment could not be checked for macros right now, so it was not forwarded "
                "to the chat; do not retry in this turn; the user can ask again in a few minutes or open it in Gmail.]")
    label = ACTIVE_KIND_LABELS.get(kind, "an active file")
    return (f"[System: This attachment is {label} from an email; such files are not forwarded to the chat "
            f"by any route (no drive copy, no re-send); tell the user to open it in Gmail.]")


def choose_one_note(labels: Sequence[str], name: str = "") -> str:
    """Several attachments match, so none is opened: `labels` are their `email_file_label`s (each carries its ref).
    `name` is the name given, the sender's text: quoted and escaped (`escape_email_name`); empty when no name was
    given (an email with several attachments, opened without a ref or a name)."""
    head = (f'Several attachments of this email are named "{escape_email_name(name)}"' if name
            else "This email has several attachments")
    listed = "\n".join(labels)
    return f"[System: {head}; none was opened. Pass one ref in file_ref:\n{listed}]"
