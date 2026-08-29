# Decision: Enable ruff RUF006 as a permanent gate against untracked asyncio.create_task()

**Date:** 2026-08-23
**Status:** Accepted
**Context:** Real incident — `ConversationHandler.handle_message()` fired the notification-channel
save (`src/handlers/conversation_handler.py:428`, best-effort) via `asyncio.create_task(...)` with
no reference kept anywhere. asyncio only holds a *weak* ref to a Task; an unreferenced one can be
garbage-collected mid-flight, silently, with no exception ever logged. Confirmed as the one
genuinely unmitigated fire-and-forget site in the codebase during a broader async-task audit
2026-08-23 — every other spawn site already tracks its Task in a `set()` + `add_done_callback`
(`FirestoreSessionStore._pending_tasks`, `BigQueryPromptContentAdapter._bg_tasks`) or hands it off
to an awaited caller.

**Decision:** Fix the site (track in `ConversationHandler._background_tasks`, same shape as the
existing precedents) and add `RUF006` (`asyncio-dangling-task`) to `ruff.toml`'s `[lint].select`,
as one explicit rule code — not the whole `RUF` category, to avoid pulling in unvetted stylistic
rules alongside it. Verified empirically: `ruff check --select RUF006 src/ main.py` on the full
tree found exactly 2 hits (the fixed site, plus `main.py:291`, see below) and zero false positives
against the ~11 already-correctly-tracked sites — including two scatter-gather patterns that
assign to a list before `asyncio.gather`.

**Known gap, closed 2026-08-25:** `main.py:291` (a nested, untracked `create_task` inside the
consolidation-overflow fallback for when `agent_task_queue` was `None`) also tripped RUF006, but
`make check` only runs `ruff check src/` (documented CLAUDE.md convention), so `main.py` was
outside the CI-enforced lint scope. The branch dated back to a pre-2026-08-16 `is_http_mode` check
that gated the task queue on Socket Mode vs HTTP mode; once Socket Mode was removed entirely, the
queue is created for any `GOOGLE_CLOUD_PROJECT` (`main.py:158`), which is always set in Cloud Run —
so the fallback was unreachable in production. Owner decision: delete the branch outright rather
than fix its task tracking ("мы локально не дебажим") — `overflow_callback` now awaits
`agent_task_queue.enqueue_consolidation_task()` unconditionally inside the `consolidation_queue`
branch. `ruff check --select RUF006 src/ main.py` now finds zero hits.

**Rejected alternatives:**
- *Hand-rolled architecture test (AST/regex grep for bare `create_task(`)* — reinvents what a
  shipped, maintained ruff rule already does correctly; extra code to keep in sync for no benefit.
- *Select the whole `RUF` category* — pulls in unvetted stylistic rules; against this file's own
  "high-signal only" convention (see file header).
- *Widen `make check` to lint `main.py` too* — separate, bigger-blast-radius change (repo-root
  script, not `src/`); not needed to close the specific site under discussion here.

**Trigger to revisit:** if `main.py:291`'s socket-mode-without-task-queue fallback path is
confirmed live in production (not dev-only), fix it directly (track + drain at shutdown like
`FirestoreSessionStore._pending_tasks`) rather than relying on a lint rule CI doesn't enforce there.
