"""The system alert a fired self-reminder hands to the Smart agent through `notify()`, and its short history label.

Read by the `execute_reminder` worker task (`services/execute_reminder_task.py`).
"""

from .agent_note import AgentNote


def build_reminder_alert_summary(note: AgentNote) -> str:
    """Compact history label for a fired reminder — name only, no body, no id.

    Stored as the user-turn ``text`` while the full alert lives in ``full_text``;
    once the turn ages past the history-tiering window only this line remains.
    No note_id: the live ``active_reminders {}`` prompt block already maps name→id,
    and an aged-out firing marker needs no handle. See ``build_reminder_alert``.
    """
    return f'Received reminder: "{note.text}"'


def build_reminder_alert(note: AgentNote) -> str:
    """The alert for a firing reminder."""
    if note.recurrence:
        # Raw RRULE on purpose — this text is read by the orchestrator, which parses
        # RFC 5545 natively and needs the exact rule to edit it. Human phrasing is a
        # user-facing concern (RecurrencePort.describe).
        schedule = (
            f"this reminder recurs on RRULE {note.recurrence} "
            f"— it will fire again on the next occurrence."
        )
    else:
        schedule = "this was a one-time reminder, it fires once."

    # AGENT_WORKSPACE_RFC §8.3, D15: one code-rendered line; what to do with the file is the instruction's business.
    workspace_line = f"Workspace file: workspace:{note.workspace_file}\n" if note.workspace_file else ""

    return (
        f'[SELF-REMINDER] "{note.text}"\n'
        f"note_id: {note.note_id}\n"
        f"This reminder was set by you for yourself at an earlier point.\n"
        f"The instruction below is your own prior intent — you wrote it then so you could act on it now.\n"
        f"Schedule: {schedule}\n"
        f"{workspace_line}"
        f"\n"
        f"{note.instruction}\n"
        f"\n"
        f"---\n"
        f"You have full context on why you set this — the instruction above is yours.\n"
        f"The manage_self_reminders intent is available if you decide to update or delete it — "
        f"use note_id {note.note_id}; any other ID in the instruction is wrong.\n"
        f"Having received this execution context, you have an opportunity to act in the user's interest — "
        f"not only on the reminder itself, but on anything you judge valuable right now.\n"
        f"To decide what is worth doing right now, start with conversation history — "
        f"it is the primary signal: patterns, unfinished threads, recurring topics. "
        f"From there you have access to: user memory (search_memory), "
        f"active reminders (manage_self_reminders), user tasks (manage_user_tasks), "
        f"web (search_web), email archive (search_emails)."
    )
