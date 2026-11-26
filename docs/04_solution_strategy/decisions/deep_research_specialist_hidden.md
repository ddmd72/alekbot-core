# Decision: Hide the deep research specialist; deep research is the owner's skill over an outside worker

**Date:** 2026-11-26
**Status:** Accepted (owner decision 2026-11-26), temporary
**Context:** The deep research specialist (`docs/05_building_blocks/deep_research/README.md`) runs a paid API job and
reaches Smart two ways: the `deep_research` intent in the delegation list and the `PROTOCOL_DEEP_RESEARCH_PREP` token
in Smart's profile. The owner now runs deep research as an outside worker on the external task queue
(`docs/05_building_blocks/external_task_queue/README.md`), briefed by a personal skill, and wants Smart to use only
that path.

## Decision

- **The intent is offered to no model.** `DEEP_RESEARCH_AGENT` is `internal=True`; no allowlist names it, so neither
  Smart, Quick nor Lelik sees it. The agent, the Cloud Run Job, the delivery path and the Cabinet switches stay as
  built.
- **No system instruction on deep research.** `PROTOCOL_DEEP_RESEARCH_PREP` leaves Smart's profile; the token stays in
  Firestore, unread. If deep research returns as a system capability, it comes back as a system skill, not as a
  protocol token.
- **The help text** (`src/utils/capabilities.py`) no longer lists deep research.
- **Longer leases on the queue.** `MAX_RENEWALS_PER_TASK` 8 → 16: a worker renewing every 8 minutes keeps a task for
  about 2.4 hours instead of about 80 minutes, because a deep research run is allowed to take an hour and more.
- **Longer briefs on the queue** (owner, same day). `TASK_TEXT_MAX_CHARS` 3 000 → 10 000: a research brief with its
  question, scope and the owner's situation outgrew 3 000 characters, and the cap had no measured basis (the receipt is a
  file, not a chat message).

## Rejected

- **A per-user gate** (offer the intent only to users without such a worker): it needs the per-user intent
  availability of the planned config hierarchy, not built yet; the specialist is used rarely.
- **Keeping the intent with a "prefer the worker" rule**: two routes for one request leave the choice to the model.

## Triggers to revisit

A system skill for deep research is written, or a user other than the owner needs deep research.
