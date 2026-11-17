"""
NotifyResult — outcome of a ``UserNotificationService.notify()`` call.

Replaces the previous silent-return pattern: ``notify()`` now reports
whether the notification was actually delivered, what status the agent
returned, and the error string when applicable. Callers (workers, cron,
webhooks) use this to decide whether to retry, mark idempotency state,
or escalate.

Frozen value object — no behavior, no I/O, pure data.

See: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 6
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .agent import AgentStatus


# Error string an orchestrator/formatter agent returns when its turn ended with no answer at all.
EMPTY_ANSWER_ERROR = "empty_answer"

# Error string an orchestrator returns when its tool loop reached the turn cap (or its wrap-up gave no answer).
TURN_CAP_ERROR = "max_turns_exhausted"


def is_empty_turn(
    text: Optional[str], structured: object, delivery_items: Sequence[object], async_dispatches: Sequence[object],
) -> bool:
    """Whether a turn ended with nothing the user would see: no text, no rich content, no delivery items and no async
    dispatch still to answer. Every orchestrator that returns ``EMPTY_ANSWER_ERROR`` decides it here; each passes what
    its own delivery carries (an agent that never delivers rich content or items passes ``None`` and ``()``), because a
    part the caller drops cannot make the turn non-empty."""
    return not (text or "").strip() and structured is None and not delivery_items and not async_dispatches


def empty_answer_alert(path: str, user_id: str, note_id: Optional[str] = None) -> str:
    """The ops alert for a background turn that ended with an empty answer (final, not retried): which path, which user
    (the first 8 characters of the id) and, for a reminder, which note. No content."""
    return (
        "⚠️ Background turn ended with an empty answer (final, not retried). "
        f"path={path} user={user_id[:8]}" + (f" note={note_id}" if note_id else "")
    )


@dataclass(frozen=True)
class NotifyResult:
    """Result of a notify() call.

    Attributes:
        delivered: True iff the user actually received a message
            (chat-message text and/or rich content) on their channel.
            False on:
              - no channel resolved (no primary, no last-active)
              - channel factory returned None (unknown platform)
              - agent returned non-SUCCESS status
              - exception during route_message or channel send
        agent_status: status reported by the agent (or SUCCESS by default
            if the failure happened before reaching the agent — e.g. no
            channel resolved). Inspect together with ``delivered``.
        error: human-readable error string, populated when
            ``delivered=False`` due to an exception. ``None`` for
            success or for non-exception failures (e.g. agent reported
            FAILED status).
        sent: True once ``notify()`` reached its first channel send,
            set just before that call: a send that raised still counts,
            because the message may have left. A caller that marks
            "user told" before notifying rolls that back only when
            neither ``delivered`` nor ``sent`` is true.
        turn_started: True once ``notify()`` handed the message to the agent, set just before that call: from then
            on the turn may have acted (a tool write, an ASYNC dispatch, a post), so re-running it may repeat that.
            False only when ``notify()`` stopped before the agent (no channel, no channel adapter).
    """
    delivered: bool
    agent_status: AgentStatus
    error: Optional[str] = None
    sent: bool = False
    turn_started: bool = False


def is_empty_answer(result: NotifyResult) -> bool:
    """The agent ended its turn with no answer and nothing was sent (``empty_answer``).

    In a background path this is final, not transient: the turn may already have run SYNC actions
    (a task, a reminder, a drive save, a workspace write) that a retry would repeat, and a
    conditional job may legitimately end empty every time."""
    return not result.delivered and not result.sent and result.error == EMPTY_ANSWER_ERROR


def is_turn_cap(result: NotifyResult) -> bool:
    """The agent's tool loop reached its turn cap and nothing was sent (``max_turns_exhausted``). Final in a background
    path: a re-run repeats every tool call the turn made and, on the same input, tends to stall the same way."""
    return not result.delivered and not result.sent and result.error == TURN_CAP_ERROR


def is_retryable_failure(result: NotifyResult) -> bool:
    """Not delivered and the turn never started, so a re-run repeats nothing (P15-D1). A Cloud-Task-backed background
    path answers 5xx (Cloud Tasks retries) only for this; once the turn started, the outcome is final."""
    return not result.delivered and not result.turn_started
