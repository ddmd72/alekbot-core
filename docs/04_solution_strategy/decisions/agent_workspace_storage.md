# Agent workspace: a versioned GCS bucket, pulled on demand, every write labelled with its origin

**Date:** 2026-10-20
**Status:** Implemented on `feat/agent-workspace`, not deployed. RFC: `docs/10_rfcs/AGENT_WORKSPACE_RFC.md` (rev 2,
owner decisions D1–D17).

## Decision

Smart keeps files for its own work — a knowledge base it builds and follows — in a dedicated versioned GCS bucket, key
`{user_id}/<folder>/<path>`, folders `projects/ references/ scratch/ files/`, one local tool `workspace` with eight
commands. The workspace is **pulled**: used only when the user asks, an instruction it follows (a skill, a reminder)
names a file or asks it to keep state, or a firing reminder carries a workspace file; nothing is injected into the
prompt. A file has no required structure; how to use it is the instruction's business (D16). Writes are allowed on
every Smart path, and every object carries its `origin` and, on reminder runs, the fire key. Reminders gain one optional
field, `workspace_file`, bound by code in NotesAgent and rendered at fire time as one line (D15).

## Why

- **Storage.** Copies of PDFs and scans are part of the job (a Firestore document caps at 1 MiB); object versioning
  gives history and restore for free; a dedicated bucket keeps lifecycles apart from the media bucket's 30-day uploads;
  the user's drive is the user's area and is absent when not connected.
- **Pull, not push.** A self-written block in every prompt was the riskiest part of rev 1 (injection, cache
  invalidation per write, ~600 tokens), and the evidence showed Smart does not start writes on its own.
- **Origin labels over interactive-only writes.** The morning briefing runs from a reminder and must update its own
  file; a label makes every background write visible, history and restore undo it.
- **One code-bound reminder field.** NotesAgent's LLM rewrites reminder text and loses paths in prose (D15). Nothing
  else about the field is special.
- **No schema, no rules as protection.** A fixed state section grows daily with nothing to prune it (D16); "data only"
  framing and folder rules protect nothing, since the bot can schedule any instruction itself (D17). Injection is the
  fingerprint's class-B follow-up.

## Alternatives rejected

- **An index in every prompt + a `workspace` system skill + a write gate** (rev 1): risk and cost without evidence of
  use; the gate could never pass when the skills store failed.
- **A skill per scheduled task (`reminder_skill`):** the catalog is in every prompt and skills are applied by meaning;
  a once-a-day task needs neither (D15).
- **A `state_file` with folder rules and "data, not instructions" framing:** no real protection (D17).
- **A fixed state section in workspace files:** grows without pruning (D16); fixed schemas live in the skill that owns
  them.
- **Firestore documents:** 1 MiB cap, no free history, poor for binaries.
- **A folder on the user's drive:** absent for unconnected users, mixes bot and user files, failed in practice.
- **Prose paths in the reminder instruction:** lost by the rewrite.
- **Generation-addressed deletes:** in a versioned bucket they erase the version; deletes patch then delete the live
  object, through a fresh blob, under `if_generation_match`.
- **A per-user feature flag:** all whitelisted users at once (D11).

## Revise when

- The §16 review misses a bar: fewer than 90 % of reminder runs with a workspace file view it; a file recreated after a
  deletion without restore.
- A file only grows (§16 growth report): add a pruning line to its instruction — there is no mechanism by decision (D16).
- Stored memory facts restate workspace operations or file contents as facts about the user: apply the rev 1 R9
  `Trivial_Exclusions` line after a dry run (§11.5).
- A Cabinet workspace page is wanted (view, history, restore, an opt-out).
- `SYSTEM_TURN_FINGERPRINT_RFC.md` lands: its argument scrub must cover local tools too (§11.3), and its class-B work
  applies to workspace content like any input (§11.6).
