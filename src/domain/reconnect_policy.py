"""R1: when an integration shows Reconnect and sends its one chat notice (SECRETS_AT_REST_RFC §6.2, F1, F2).

An integration whose own feature state shows a past connection (Gmail indexing state, drive sync state, To Do config
with a primary list) but whose credential is gone needs a reconnect. Two exceptions: a deliberate disconnect marks the
feature state "disconnected by user" (F1; Gmail and Drive only, D-P5), and the one notice is sent once per transition,
through a flag on the feature state (F2).

Public interface:
    Integration                                   the three integrations R1 covers
    Cleared.MARK                                  "clear the stored value" for a field-level flag update
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


def reconnect_needed(has_credential: bool, past_connection: bool, disconnected_by_user: bool) -> bool:
    """No credential, a past connection, and not disconnected on purpose."""
    return not has_credential and past_connection and not disconnected_by_user


def r1_due(has_credential: bool, past_connection: bool, disconnected_by_user: bool, notified: bool) -> bool:
    """The one notice is due: a reconnect is needed and this transition was not notified yet."""
    return reconnect_needed(has_credential, past_connection, disconnected_by_user) and not notified
