# Decision: The indexing toggle keeps the whole-document user write

**Date:** 2026-10-12
**Status:** Accepted (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §6; `PUT /api/drive/indexing` in
`src/web/user_cabinet_app.py`, `FirestoreUserRepository.update_user`.

**Decision:** The Cabinet switch reads the user profile, sets `config.drive_indexing_enabled` and writes the whole
document back (`update_user` is a full `set`), as every other Cabinet setting does (timezone, location, language,
Gmail options). A concurrent writer that read the profile earlier can overwrite the switch, or the switch can
overwrite its change: a lost update. The window is one read-to-write gap of another writer of the same user's profile.
Every reader of the flag reads it uncached (the tick's gate, search step 0, the fan-out), so a lost write shows at once
in the Cabinet and is fixed by switching again; turning indexing off still enqueues its `toggle` task.

**Rejected alternatives:**
- *A field-level update for this flag only (`config.drive_indexing_enabled`):* a new user-repository method for one
  field, while the same race stays open for every other setting; the fix belongs to the repository as a whole.
- *A transaction around read-modify-write in `update_user`:* changes every caller's write semantics at once, outside
  this feature.

**Trigger to revisit:** a switch that does not stick, a background writer of the user profile (one that runs while
the user is in the Cabinet), or moving user settings to field-level writes in general.
