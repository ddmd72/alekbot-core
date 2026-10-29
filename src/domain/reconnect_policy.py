"""R1: when an integration shows Reconnect and sends its one chat notice (SECRETS_AT_REST_RFC §6.2, F1, F2).

An integration whose own feature state shows a past connection (Gmail indexing state, drive sync state, To Do config
with a primary list) but whose credential is gone needs a reconnect. Two exceptions: a deliberate disconnect marks the
feature state "disconnected by user" (F1; Gmail and Drive only, D-P5), and the one notice is sent once per transition,
through a flag on the feature state (F2).

Public interface:
    Integration                                   the three integrations R1 covers
    Cleared.MARK                                  "clear the stored value" for a field-level flag update
    R1_NOTICES[integration]                       the English system_alert of the one notice
    reconnect_needed(has_credential, past_connection, disconnected_by_user) -> bool     Cabinet shows Reconnect
    r1_due(has_credential, past_connection, disconnected_by_user, notified) -> bool     the one notice is due
"""
from __future__ import annotations

from enum import Enum


class Integration(str, Enum):
    GMAIL = "gmail"
    DRIVE = "drive"
    TODO = "todo"


class Cleared(Enum):
    """Passed to a field-level flag update whose `None` means "leave as stored": the stored value becomes empty."""

    MARK = "cleared"


# The one notice per integration (an English `system_alert` the formatter agent turns into the user's language). No
# provider-side cause is guessed: the stored access is simply gone. The drive's provider is never named to the model
# (USER_DRIVE_RFC Step 1 §4.3).
R1_NOTICES = {
    Integration.GMAIL: (
        "Alek's saved access to the user's Gmail is no longer there, so the connection has to be made again. Until "
        "the user reconnects, the daily email review, email auto-indexing and email search do not run; the emails "
        "already indexed are kept. Tell them, and that the fix is Cabinet → Gmail → Reconnect."
    ),
    Integration.DRIVE: (
        "Alek's saved access to the user's drive is no longer there, so the connection has to be made again. Until "
        "the user reconnects, the drive index is not updated; searching it still works from the last update. Tell "
        "them, and that the fix is Cabinet → Drive → Reconnect."
    ),
    Integration.TODO: (
        "Alek's saved access to the user's To Do tasks is no longer there, so the connection has to be made again. "
        "Until the user reconnects, Alek cannot read or change their tasks and changes made in To Do are not "
        "synced. Tell them, and that the fix is Cabinet → To Do → Reconnect."
    ),
}


def reconnect_needed(has_credential: bool, past_connection: bool, disconnected_by_user: bool) -> bool:
    """No credential, a past connection, and not disconnected on purpose."""
    return not has_credential and past_connection and not disconnected_by_user


def r1_due(has_credential: bool, past_connection: bool, disconnected_by_user: bool, notified: bool) -> bool:
    """The one notice is due: a reconnect is needed and this transition was not notified yet."""
    return reconnect_needed(has_credential, past_connection, disconnected_by_user) and not notified
