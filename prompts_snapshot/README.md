# prompts_snapshot — git mirror of the Firestore prompt layer

This tree is a **git-tracked, read-only mirror** of the shared prompt layer that lives in
Firestore (the tokens / blueprints / profiles the Prompt Builder assembles into every agent's
system prompt). **Firestore is the source of truth.** Editing files here changes nothing on its
own — you push changes back with the upload script (below).

Why it exists: prompt changes used to be invisible to git (Firestore-only). This mirror makes them
**reviewable and diffable**, and makes Firestore↔git drift visible. Full design:
[`docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md`](../docs/10_rfcs/PROMPT_TOKEN_SNAPSHOT_RFC.md).

## Layout

```
prompts_snapshot/
  README.md                      ← this file (hand-maintained; the pull never touches it)
  tokens/system/<id>.groovy      ← system-owned tokens   (frontmatter + groovy-DSL body)
  tokens/user/<id>.groovy        ← "user"-namespace tokens (also system-level, not per-user)
  blueprints/<id>.yaml           ← blueprints (outer_class + class_order)
  profiles/<id>.yaml             ← agent profiles (blueprint_id + token assignments)
```

Each token file is YAML frontmatter (metadata) + a readable body (the prompt content). Blueprints
and profiles are plain YAML. **Excluded:** account/user `overrides` (PII) — never mirrored.

## Scripts (all in `firestore_utils/`, tracked in git)

| Script | Role |
|--------|------|
| `snapshot_serializer.py` | Pure (de)serialization between Firestore docs and these files. No I/O. Unit-tested. |
| `snapshot_pull.py` | Firestore → files (the mirror). |
| `snapshot_upload.py` | files → Firestore (push edits back). |

`download.py` / `upload.py` in the same dir are the **legacy** single-doc tools — unrelated to this
mirror; ignore them for snapshot work.

## Credentials

The scripts talk to the dev Firestore (`development_*` collections, database `us-production`). They
authenticate with **GCP Application Default Credentials** — either `gcloud auth application-default
login` (ADC), or `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service-account key (the app reads
this from `.env`). No credentials live in this repo. The mirror cannot reach real Firestore without
them.

## Workflows

**Refresh the mirror (Firestore → git):**
```bash
python firestore_utils/snapshot_pull.py          # overwrite prompts_snapshot/ from Firestore
git diff prompts_snapshot/                        # see what drifted
git commit prompts_snapshot/ -m "..."             # record it
```

**See drift without writing (CI-friendly, exits non-zero on drift):**
```bash
python firestore_utils/snapshot_pull.py --check
```

**Push an edit back (git → Firestore):**
1. Edit the token body in `prompts_snapshot/tokens/.../<id>.groovy`.
2. Dry-run — shows the per-field diff that *would* be written, writes nothing (AI may run this):
   ```bash
   python firestore_utils/snapshot_upload.py prompts_snapshot/tokens/system/<id>.groovy
   ```
3. Apply — **human only.** Writes to Firestore with `merge=True` (preserves fields not in the file,
   e.g. timestamps). Requires typing each doc id to confirm; aborts in non-interactive shells:
   ```bash
   python firestore_utils/snapshot_upload.py --apply prompts_snapshot/tokens/system/<id>.groovy ...
   ```
4. `snapshot_pull.py` to re-sync, then commit.

> Safety: `--apply` is gated so an AI/non-interactive shell cannot write. Editing files without
> `--apply` does nothing. Before a risky bulk push, back up first (see below).

## Backup before bulk pushes

A full dump of the current Firestore prompt layer can be saved as one JSON to the gitignored,
PII-safe `scripts/memory/` (per the project secrets rule). This is the restore point if an upload
goes wrong. (See `scripts/memory/prompt_tokens_firestore_backup_*.json` if one was taken.)

## Note on PII

Token few-shot examples can carry indirect personal data (this mirror's first pull surfaced a real
car and medical specifics). The pull has a guard that skips any account/user-keyed document, but
content-level PII inside system tokens is a human review concern — scan new diffs before committing.
