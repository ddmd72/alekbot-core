# A background turn that started is final; a reminder that did not run tells the owner

**Date:** 2026-11-17
**Status:** Built, pending deploy
**Scope:** the `execute_reminder` and `daily_email_review` `/worker` tasks (`ExecuteReminderTask`,
`DailyEmailReviewTask`), `UserNotificationService.notify` / `NotifyResult`. Closes P15-D1
(`docs/10_rfcs/TEST_COVERAGE_SYSTEMIC_RFC.md`).

## Problem

Both tasks answered 500 on any `delivered=False` from `notify()`, so Cloud Tasks re-ran the whole background turn
(the live queue allows up to 5 attempts). Once the agent has run, a re-run repeats what the turn already did:
- a text that was posted before its rich part failed is posted again;
- an ASYNC dispatch the turn made (`create_html_page`, the daily review's report) is enqueued again;
- SYNC tool writes (a task, a reminder, a drive save, a workspace write) run again, and the cost of the turn is
  paid again.

A reminder whose turn ended at the turn cap (`SmartResponseAgent` answers `max_turns_exhausted` when its tool loop
ends without an answer) re-ran the same stall up to four more times, and the owner never heard that the reminder
did not run. Making the failures final without telling the owner would turn a retried fire into a silent loss. The empty answer was already made final for the same reason (`claude_deliver_response_string_tool.md`).

## Decision

1. **`NotifyResult.turn_started`** is set by `notify()` just before it hands the alert to the agent
   (`route_message`); it stays False only when `notify()` stopped before the agent: no channel (`no_channel`) or
   no channel adapter (`channel_factory_returned_none:<platform>`). A router that raises still counts as started.
   `is_retryable_failure(result)` = not delivered and the turn never started.
2. **The two tasks answer 5xx only for `is_retryable_failure`.** Cloud Tasks retries, and nothing repeats because
   nothing ran. Every other non-delivery is final: the reminder fire is stamped (`mark_fire_delivered`, a one-time
   note deleted) and the task answers 200; the daily review keeps its per-date mark and answers 200.
3. **Statuses:** `turn_cap_final` (the reminder's turn reached the cap, nothing sent), `failed_final` (any other
   non-delivery after the turn started: a failed or timed-out turn, a post that raised), next to the existing
   `empty_answer_final` and `ok`. Each final outcome writes one ERROR line (`[Worker] execute_reminder: …` /
   `[Worker] daily_email_review: failed_final …`).
4. **The "did not run" notice.** Every final reminder outcome with nothing sent (`turn_cap_final`,
   `empty_answer_final`, `failed_final` of a failed or timed-out turn; `delivered` and `sent` both False) posts one
   notice to the owner after the stamp, so a redelivery of the same fire answers `already_delivered` and never
   notifies twice. A turn that sent something (a post that raised, a rich part that failed after the text) gets no
   notice: the owner may have the answer. The notice goes through `UserNotificationService.notify_ui_notice`: the
   fixed, cause-neutral UI string `REMINDER_NOT_RUN` (all four locales, `{name}` = the note's text; one string, no
   per-cause variants) in the owner's UI language, sent literally (the name is stored text) to the channel
   `notify()` would have used (primary, else last active). It is not written to history and does not claim the
   system's voice. A notice that fails to post is logged; the outcome stays final. The daily review gets no notice.
   **Nothing after the stamp is retryable:** the notice goes out before a one-time note is deleted, and an error in
   the notice or the delete is logged at ERROR while the answer stays the final 2xx (a retry would only answer
   `already_delivered` and never send the notice). A one-time note whose delete failed stays stored, stamped.
5. **`suppress_transient_retry` stays on** for both tasks: a whole-execution retry would re-run every tool call of
   the turn. The per-call retry in `_call_llm` and Smart's provider rotation still cover a provider blip.

`TURN_CAP_ERROR` (`domain/notify_result.py`) is the one definition of the string; Smart and Quick return it.

## Alternatives

- **Match error strings** (`no_channel`, `channel_factory_returned_none:*`) instead of a field: rejected, a new
  pre-agent stop would be retried or not by accident; the field says what happened.
- **Retry a failed turn when nothing was sent** (the deep-research notice's rule): rejected for these two tasks,
  because a failed turn may already have enqueued work or written through a tool (P15-D1's second case).
- **Re-enable the in-process whole-execution retry** now that the outer retry is gone: rejected for the same
  reason (it re-runs every tool call).

## Consequences

- A provider outage that outlasts the per-call retry and the provider rotation now loses that fire instead of being
  retried by the queue; the owner is told it did not run. A daily review lost the same way is ops-visible only.
- Known residual, accepted: if the stamp itself (`mark_fire_delivered`) raises, the answer is a 500 and the started
  turn runs again.
- A conditional reminder ("tell me only if X") that ends with an empty answer posts the notice on every such fire.
- The deep-research notice (`DeepResearchNoticeSender`) keeps its own rule: 5xx only when nothing was sent.
- Tests: `tests/unit/handlers/test_worker_retry_after_effect.py` (the four P15-D1 pins, now passing),
  `tests/unit/handlers/test_worker_started_turn_is_final.py`, `tests/unit/services/test_notify_turn_started.py`.
