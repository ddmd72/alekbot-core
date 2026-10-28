# Decision: The owner's drive file notes are written inside the drive specialist, one tool per edit kind

**Date:** 2026-10-28
**Status:** Accepted with the drive specialist RFC (§9); built on `feat/drive-notes`, not deployed.
**Context:** [DRIVE_FILE_NOTES_RFC.md](../../10_rfcs/DRIVE_FILE_NOTES_RFC.md) rev 3.3 §0.5; [DRIVE_SPECIALIST_RFC.md](../../10_rfcs/DRIVE_SPECIALIST_RFC.md)
§9; owner's map [user_drive/README.md](../../05_building_blocks/user_drive/README.md) §4.24; [drive_specialist.md](drive_specialist.md).

**Decision:** Smart has no note tool. It commissions `work_with_drive_files` with the note's words in the query, and the
drive specialist calls eleven one-purpose note tools (`get_note`; nine edits; `erase_note`), each one `DriveNoteService`
call. The rules are the specialist's non-overridable token `PROTOCOL_DRIVE_NOTES`; the writers are declared only on an
owner turn and re-check it per call. No tool replaces more than its name says, a change that would empty a note is
refused, and `replace_note_text` needs a `get_note` read in the same run. Receipts ride the specialist's pass-through
collector, sent literally, and a write cut by the deadline posts its receipt late. The store, the index merge, search,
archive and erase of the notes RFC are unchanged.

**Rejected alternatives:**
- *Smart's local tool `annotate_drive_file` behind the `drive-file-notes` skill* (notes RFC rev 3.2, built and removed):
  a second drive surface on Smart beside the specialist; a replace-only call made every addition retype the old text,
  and the skill gate left a same-batch residual.
- *One tool with a mode* (`drive_note_mode`: replace / add / clear / undo, the research's shape, notes RFC §13): a
  mode-flag shape a model must not hold, where an empty field can destroy.
- *A silent delete when a change empties a note:* a tool doing more than its name says.

**Trigger to revisit:** the specialist loses the drive (a revert of `work_with_drive_files`), a second surface needs to
write notes (the Cabinet, D19), or the logs show steered notes despite the owner-turn gate and the receipt.
