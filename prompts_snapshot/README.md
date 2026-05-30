# prompts_snapshot — read-only mirror

This tree is a **git-tracked mirror** of the shared prompt layer in Firestore.
**Firestore is the source of truth. Do not edit these files** — edits here have no effect.

Refresh: `python firestore_utils/snapshot_pull.py`
Drift check (writes nothing, exits non-zero on drift): `python firestore_utils/snapshot_pull.py --check`

Mirrored: tokens (system + user, both system-level), blueprints, profiles.
Excluded: account/user overrides (PII).
See docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md.
