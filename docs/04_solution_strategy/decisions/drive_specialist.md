# Decision: Smart reaches the user's drive through one LLM specialist, `work_with_drive_files`

**Date:** 2026-10-22
**Status:** Accepted (owner, 2026-10-22); built on the drive specialist branch, not deployed.
**Context:** [DRIVE_SPECIALIST_RFC.md](../../10_rfcs/DRIVE_SPECIALIST_RFC.md) rev 4 (the live A/B of a spike, §15); owner's map
[user_drive/README.md](../../05_building_blocks/user_drive/README.md) §6.7; rollout `docs/07_deployment/README.md` → "The drive
specialist".

## Decision

1. **A move, not a revision.** The drive does what it did: the same operations (batches included), rules, texts and
   background-run rights. The only changes are those the move needs (RFC §2.3).
2. **One intent for Smart and Quick, `work_with_drive_files`:** a natural-language commission and one field,
   `file_contents` (texts to save or append, bound by code, never retyped). The 8 drive intents, their 13 fields, the
   `drive-search` system skill and the skill pointer in `PROTOCOL_AGENT_SELECTION` are gone in the same change. No
   switch, no side-by-side period.
3. **`DriveAgent`, an LLM specialist** (`claude-sonnet-5-5`, adaptive thinking medium; fallback `gpt-6.1-sol`) runs
   the commission in its own `DelegationEngine` loop (≤ 6 turns, 150 s soft deadline) through local CRUD tools, each
   calling one method of the user's `DriveOperations` — FileManagement's drive code moved, one instance per user shared
   with FileManagement (lock, shield, receipts, late-failure notices keyed on `DriveOp`).
4. **Pass-through:** every read block reaches Smart verbatim, in run order; the specialist's model never sees an opened
   body. Code builds the answer (status, "What to do", op lines, reading lines); the model can only lower a status.
5. **Division of labour A′:** the specialist owns search mechanics and the opens the commission asks for; Smart owns
   content judgement and re-commissions with what opened files name. No variant re-search (one language, as today).
6. **`open_file` / `delete_file` stay on a slimmed FileManagement** (chat files, delivered documents, `skill:` and
   `workspace:` refs, the lenient `drive:` open through `DriveOperations`).
7. **The evals stub drive writes at the DriveOperations class** (`scripts/validation/drive_eval/dry_run_drive.py`),
   installed before any agent is built, and refuse to run while a public method is unclassified.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| The specialist dispatches today's 8 intents through the coordinator (research 07) | Mode-flag shapes a model must not hold (`feedback_llm_tool_shape_crud`); a failure comes back with the "Correct your input" suffix and without receipts or refs; the 8 would stay registered intents any orchestrator could reach |
| A per-user switch and a side-by-side period | Owner: a clean replacement; rollback is `git revert` + the prompt restore |
| Background runs lose delete, replace, move and rename; write caps; a 3-search cap | Behaviour changes the owner dropped (RFC §2.2) |
| `gpt-6-luna`, `grok-4.7`, `gemini-3.8-flash` as the specialist | Too weak, slowest and dearest, 3× slower (RFC §15.3, §15.5) |
| Moving `open_file` / `delete_file` into the specialist | Small saving; `open_file` is the general read for five stores and its texts live on in stored history |
| A `DryRunDriveOperations` wrapper on one instance (RFC §12) | A class-level stub covers every instance in the process, as G3's patch of `DelegationEngine.dispatch` did; no wiring path can write to the owner's OneDrive |
| A `DryRunDriveOperations` for `ab_agent_models.py` (RFC §12) | That harness stubs every intent outside its read-only set at Smart's dispatch, as it stubbed the 8: the specialist is never reached there (pinned by a test) |

## Residuals

- **Stale texts.** For a few turns after the deploy, a stale skill copy in history can cause one rejected call to a
  removed intent, which Smart self-corrects from the coordinator's unknown-intent error. The owner's stored texts
  (custom skills, reminders, directives, workspace files, prompt overrides) are scanned and rewritten intent-free before
  the deploy (`scripts/validation/drive_intent_text_scan.py`, output in `scripts/memory/`), so they work before and
  after a revert.
- **Cross-cut review (2026-10-22).** A batch cut by `still_running` reports its change as still running ("may complete"),
  not as "did not finish"; a change after a still-running outcome is "not started". A path where a `drive:` ref is needed
  is a correctable `file_ref is required` error, not a "chat file" hint. The model that serves the specialist is logged
  (`model=`) and checked before the deploy (the global `provider_preference` can override the tier default). The drive
  agent type is prompt-cached. The turn-cap alert is bounded to 5 s. An `unfinished` answer tells Smart not to
  commission the same request again in this turn; there is no hard third-call refusal.
- **Latency.** Each drive commission adds the specialist's turns; the bar is p50 of the drive-turn wall time after ≤
  p50 before + 25 % (G3 and the specialist eval record it). Effort `low` is the first lever if Sonnet misses it.
- **The Sonnet 5.5 filler-tool stall** (`decisions/sonnet55_adaptive_thinking_stall.md`) did not appear in the
  specialist role. It is bounded by 6 turns and the 150 s soft deadline; a turn-cap exit without a report is logged at
  ERROR and posts one 🚨 line to the ops alert sink. The sol fallback covers provider errors on the first call only.
- **Grok as Smart** took 9 round trips on "find then open the second" (RFC §14). It was not one commission per file:
  the run was one search and 8 re-opens of the same PDF. `GrokAdapter._convert_messages` drops every non-image file with
  a warning (`grok_adapter.py:640`), so the PDF never reaches Grok, while the answer says the file is attached; the
  specialist is blind to bodies by design (A′), so re-commissioning cannot help. It is an existing adapter gap (the same
  with today's Grok Smart) and goes to RFC Appendix A as backlog (a PDF text fallback or adapter work, a behaviour
  change); a G3 case on a Grok Smart bounds the round trips.
- **The specialist reads search cards in full**, passages included: the same risk class as Smart reading them before,
  held by the `untrusted` line and G3's injected case.

## Rollback

In the forward order: `git revert -m 1 <merge>` on a branch (it restores the mirror file of `PROTOCOL_AGENT_SELECTION`), then
`snapshot_upload.py` dry run and `--apply` of that one file just before `make deploy` from that tree (the pre-upload
backup is the cross-check), `$admin_cache_reset`, a PR of the revert, `snapshot_pull.py`. The reset never runs before the
revert is live. The window between the restored protocol and the revert going live costs up to two self-corrected calls
per drive turn. After a failed deploy, redeploy the current image as a new revision rather than resetting one
instance. Then scan for `work_with_drive_files` in texts written after the deploy
(`drive_intent_text_scan.py --include-new`) and rewrite each intent-free (RFC §13). The new drive tokens can stay.
