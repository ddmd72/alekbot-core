# The prompt mirror was two releases stale, and carried PII

**Date:** 2026-08-18
**Status:** Accepted — mirror resynced, PII scrubbed in Firestore and git
**Scope:** `prompts_snapshot/`, `firestore_utils/snapshot_*.py`

## Context

`prompts_snapshot/` is a git mirror of the Firestore prompt layer; Firestore is the source of truth
(see `docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md`). Editing the Stage-1 consolidation token required a
`snapshot_pull.py --check` first, and it reported drift on three tokens unrelated to that work.

## What the drift was

All three drifted in the same direction — **Firestore ahead, git stale** — and none of it was noise:

| token | Firestore updated | what production had that git did not |
|---|---|---|
| `ROUTER_COGNITIVE_PROCESS` | 2026-07-14, `uploaded_by: router-complexity-recalibration-2026-07-14` | the two-axis complexity rework, `default_low`, rewritten `p6_uncertainty` |
| `COGNITIVE_PROCESS_NOTES` | 2026-07-30 | the whole RRULE scheduling contract (`FREQ=WEEKLY;BYDAY=TU,FR`), `clear_recurrence`, `reading_current_state` |
| `PROTOCOL_SMART_AGENT_SELECTION` | 2026-07-30 | the `schedules` block, `pending_notes` → `active_reminders` |

These are two shipped features — the router recalibration and RRULE reminders — whose prompts never
came back to git. The mirror had not been refreshed since the initial one (`f41f40c`, 2026-05-30).

**Production was never at risk; the exposure is the opposite direction.** Anyone editing one of those
files in git and running `snapshot_upload.py --apply` would have pushed May text over live prompts —
erasing the router recalibration, or restoring a reminder contract the code no longer implements. The
consolidation-token edit escaped this only because `--check` was run first and that token was not among
the drifted three.

## PII in a git-tracked file

The refresh made the README's warning concrete. A sweep of all 148 mirrored files found personal data
in five system tokens — most seriously a few-shot example in `CONSOLIDATION_COGNITIVE_PROCESS` built
around the owner's real family-only nickname, repeated seven times across keywords and query strings.
The rest were the owner's city and origin city inside search examples.

All five were rewritten with unrelated values (different nickname, different cities), keeping each
example's structure intact, then pushed to Firestore and re-pulled, so the mirror and the source of
truth agree.

**The nickname predates this work and remains in git history** (`f41f40c`, 2026-05-30). Scrubbing the
current tree does not remove it from past commits; excising it needs a history rewrite, which was not
done and is a separate decision.

## Decision

1. Resync the mirror from Firestore (`snapshot_pull.py`) and commit the two missing releases.
2. Scrub PII from the five tokens **in Firestore first**, then pull — never the reverse, or the mirror
   becomes the lying copy again.
3. Record that `--apply` is human-only by design: the upload script refuses to run non-interactively,
   which is what keeps an agent from pushing a stale mirror over production.

## Consequences

- The mirror is only as truthful as the last pull. Nothing runs `--check` automatically today, which is
  how 47 days of drift went unnoticed; wiring it into CI is the obvious follow-up and was **not** done
  here.
- Content-level PII inside prompt tokens is a review concern with no automated guard. The pull skips
  account/user-keyed documents, but a system token can quote anything the author pasted into it.
- Prompt changes made directly in Firestore stay invisible to review until someone pulls. The two
  features above shipped with their prompts unreviewed in git for over a month.