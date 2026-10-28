# Decision: After passages are read, a run that is not a chat turn refuses its changes

**Date:** 2026-10-28
**Status:** Accepted (owner, OQ-A1, 2026-10-28); built on `feat/drive-answer-from-content`, not deployed.
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §28.3 A14, §28.6 OQ-A1;
[DRIVE_SPECIALIST_RFC.md](../../10_rfcs/DRIVE_SPECIALIST_RFC.md) §2.2 row 1, §2.3.11; code `src/domain/drive_content_gate.py`,
`DriveToolRun.run` in `src/infrastructure/drive_tools.py`.

**Decision:** `find_passages` and `read_passages` put on-topic document text, chosen by meaning, in front of a model that
holds delete, move, replace and note-write tools. On a run that is **not a chat turn**, a change is refused once passage
text was returned to the model in an earlier step of the run.
- **Predicate:** `content_gate_applies(context) = not context.get("interactive_delivery")`. That key is set only by
  `ConversationHandler`, the same test that makes `origin_label` say `chat`. So reminders, the daily review, every other
  `notify` run, calls (`ask_alek`, `tell_alek`) and a run with **no origin at all** are gated (fail-closed); a chat turn is
  not (the untrusted-text line is its guard).
- **Step rule:** `write_refused(gated, step, passages_step)`: refused only when the first passages step is earlier than the
  call's step; a change co-emitted with the first passages read was decided before the text was seen.
- **Scope:** every call of an op in `MUTATING_OPS` and the note writers (they run through the same `run`).
- **Outcome:** `OutcomeClass.REFUSED`, text `WRITE_AFTER_PASSAGES`, INFO log `[DriveAgent] <kind> refused: passages were
  read in step N of this run`. Not `REQUEST_ERROR`: a later call must not settle it, and Smart must see it was not done.
- **Consequence:** a run that read passages loses its writes for the rest of that run; reading and writing become two
  commissions. On a call, a change asked in the same commission as a content question is refused once passages are read and
  Alek commissions it again.
- **Partial supersession:** specialist RFC §2.2 row 1 (no gate on file mutations in a background run, owner 2026-10-22) now
  holds only for runs that read no passages.

**Relay hole (security review, owner decision 2026-10-28, option 1):** the refusal text used to say "commission the
change separately", and the specialist's `question` / `not_done` reached Smart unwrapped, so an instruction planted in a
passage could be relayed into Smart's next commission, which runs with no passages read and so no gate. Now the refusal
says to repeat the change only when the user's own words or the task's instructions (a reminder's text) ask for it,
never because a file or the drive assistant asks, and every run that retrieved passages withholds the report's
model-written text (note, question, not_done), chat turns included: the ledger decides, not the origin. Rejected: *keep
as designed* (the gate is bypassed in one hop) and *report the refused change to the owner in chat* (a planted
instruction becomes a message to the owner, and a background run has no one reading in the moment).

**Rejected alternatives:**
- *`is_owner_turn` as the predicate* (`drive_note.py`): it counts a call as an owner turn, but on a call the user is not
  reading what the specialist read and Lelik's words are the request.
- *No gate:* leaves a planted instruction in an on-topic document one ranking away from a delete or a move.
- *The gate on every run, chat included:* breaks "read the plan, then move the file" in one chat message, where the owner
  sees the reply.
- *Gating a run with no origin as open:* fail-open for any path that forgets to mark itself.

**Trigger to revisit:** the S2 injection family or live logs show chat-turn steering despite the untrusted-text line, or
refusals on calls become frequent enough that Alek's re-commission costs more than the exposure.
