# RFC: Owner notes on drive files

**Status:** Proposed, rev 2 (2026-10-21). Docs only; nothing is built.
**Date:** rev 1 2026-10-21; rev 2 2026-10-21, after the builder-check review (`scripts/memory/review_drive_file_notes_rfc.md`,
private): C1 and I1–I10 fixed, the cheap minors folded in, the per-slice test gaps added (§0).
**Related:** `USER_DRIVE_INDEXING_RFC.md` (the index, cards §25.2.7, keys §25.2.4 and D31, the transcript store §27.8 b,
the G5 "no `CARD_VERSION` bump" precedent §27.8 a), `USER_DRIVE_RFC.md` (Step 1, `drive:` refs, `Trivial_Exclusions`
§4.12), `GMAIL_EMAIL_INDEXING_RFC.md` (§3.2: `domain_email_facts_v1`, a bot-owned collection that mirrors `FactEntity`,
with its own vectors and lifecycle, the precedent the owner named), `MEMORY_FACT_CONFIRMATION_RFC.md` (why the bot never
writes memory itself; the interactive-only local tool precedent), `AGENT_WORKSPACE_RFC.md` (a Smart local tool with a
per-execution lock and undo), `AGENT_SKILLS_RFC.md` (system skills, `use_skill`), `docs/05_building_blocks/user_drive/README.md`
(the owner's map of the drive; update it when this is built).
**Research behind it** (private, gitignored, not quoted for owner data): `scripts/memory/research_drive_file_annotations.md`
("Option A": a bot-side note store) and `scripts/memory/research_drive_notes_mirror_critique.md` (why notes as drive files
break). §13 lists where this RFC departs from them.
**Branch base:** line references are to `main` at `a0d9809b` (transcripts T1 merged), checked 2026-10-21.
**Where the code and this text differ once it is built, the code is the truth.**

---

## 0. Changes in rev 2

| Review item | Fix | Where |
|---|---|---|
| C1 — a record leaves the index without the file being gone (moved into a repository, a folder gains `.git`, a dense tree, a rename into an OS artefact: `drive_index_planner.py:510-514`) | Each note check carries why the record went. Only a delete that says "gone" is confirmed with one `get_change`, and only a not-found archives. A record dropped because it is no longer stored never archives. | §7.1 |
| I1 — a `NOTE_MERGE_VERSION` bump re-merged nothing (the upgrade pass runs only on `CURRENT_VERSIONS`) | `notes_merge_version` on the sync state; a mismatch resets the watermark, so the notes step re-merges every note from the store. | §5.1 b, §5.5 |
| I2 — `_resolve_step` creates records outside `_apply` (`:1286`) | `_build` flags every created record `note_lookup_due`; the lookup runs in `_commit`, the one write gate. | §5.1 a |
| I3 — the history Smart holds is timestamped and anchored | The owner corpus is built before the anchor is injected, with the timestamp prefix stripped. | §4.5 |
| I4 — a classifier-only correction was undefined | A note is the triple (text, doc type, life areas); text may be empty when a classifier is set; an empty triple deletes. | §4.2, §4.3, §5.3 |
| I5 — the 240-key arithmetic was wrong and the cut order unspecified | Measured numbers; classifier names first (never cut), then numbers and dates, then words; `NOTE_KEYS_MAX` 320. | §5.4, N1 |
| I6 — `content_terms` as `v:` would key fragments ("250" in "1 250,00") in the exact tier | The profile's own recipe: `_value_forms`, `_date_items`, `_month_year_spans`. | §5.4 |
| I7 — duplicate collapse needed a lean field | `owner_note_id` joins `RECORD_LEAN_FIELDS`. | §3.3, §6.1 |
| I8 — a dormant note, or any note while disconnected, could not be deleted | Delete, undo and erase need no Graph call and no connection; notes are addressed by a provider-free ref key or by `drive_note:<note_id>`. | §4.4, §6.4 |
| I9 — clear and erase left the note in `keys`, `note_vector`, `snapshot` | Clear empties every derived field; the erase stub holds no content. | §4.4 |
| I10 — the classifiers were ungrounded | A classifier passes only if one of its names is in the owner's words. | §4.5 |
| D8 / OD5 wording | OD5 is stated as a deferral of D8's provider-switch re-binding. | §12 |
| OD3 | Per-note history erase and erase-all, each behind a confirmation code; the default changed. | §4.8, §12 |
| OD1 | A worked example of the refused "bot proposes, owner says yes" flow and the wording that passes. | §12 |
| Minors 1–19 | Folded in where cheap (skill-gate plumbing, deterministic first id, idempotent writes, one lock, string flag parse, queries and indexes, watermark paging, tick accumulator, rendering call sites, SCD2 wording, 404 order, folder re-binding, the anti-goal test, adapter readers, kill-switch scope). | throughout |

---

## Owner decisions

Binding. Recorded, not reopened.

| # | Decision |
|---|----------|
| D1 | **Nothing goes to the drive.** Notes are internal, possibly sensitive records "under the hood". Never the item's `description` or any other metadata (files get shared and their metadata travels with them), never a `Notes/` folder, never a projection file. |
| D2 | **Storage: a separate Firestore collection `{prefix}drive_notes`**, the email-facts pattern: SCD2 versions (`valid_from` / `valid_to` / `is_current`, as `FactEntity`) for history and undo, its own vector(s) and exact keys. **Not** the memory facts collection (the consolidator and the biographical cache must never see notes) and **not** a drive-index collection (those are disposable: a rebuild, an opt-out and an access-loss purge delete them). |
| D3 | **Notes are durable.** They survive an index rebuild, indexing opt-out, a drive disconnect and a provider switch. They are deleted only on the owner's explicit command, and on account deletion. |
| D4 | **Smart writes notes through a system skill** (loaded on demand, like `drive-search`) **and ONE local tool that replaces the note**: replace-only, an empty note deletes it, a cap of about 80–100 words (this RFC picks and justifies the number, N1), a verbatim receipt, undo = restore the previous version. |
| D5 | **Interactive chat turns only.** Not voice (Lelik's `ask_alek` / `tell_alek`), not background runs (reminders, the daily review, `/worker`). |
| D6 | **Classifiers:** life area and document type, from the existing fixed enums (`LIFE_DOMAINS`, `DOC_TYPES`), as exact keys. Everything else is free text: people, dates and places reach search through the note's words and its embedding. |
| D7 | **The index merges the note into the file's card while the file exists**, as a top owner segment that outranks the model's guesses. The owner's doc type and life areas override the profile's **at read time**; the stored profile is never changed; notes are excluded from card accuracy. No `CARD_VERSION` bump (the G5 precedent). Avoid a `KEYS_VERSION` / `index_tables_digest` bump (the research proposes reusing the `e:` family under a new `AboutField.OWNER_NOTE` in the About tier; verified in §5.4). |
| D8 | **Binding by the provider item id.** Re-bind by content key (`quickXorHash:size`, the transcript store's key) after a reconnect, another drive or a provider switch, **exact match only**. A copy does not inherit the note. Notes on folders are allowed, with no inheritance to children. |
| D9 | **A note whose file is gone is archived** (a state plus when the file was last seen). The default drive search **never** returns archived notes; only an explicit "search in deleted files" request does (otherwise search returns irrelevant old things forever). |
| D10 | **Voice and speaker recognition are out of scope** (a separate future feature). |

---

## 1. Context, goals, non-goals, anti-goals

### 1.1 Why

The owner knows things about his files that no model can read from them: who sent an estimate, which project a scan
belongs to, that a PDF the profiler typed `invoice` is in fact a `contract`. Today there is nowhere to put that:

- The index is a disposable mirror of the drive (README §1: "Everything in it can be rebuilt from the drive"), so a
  hand-written fact on an index record would die at the next rebuild, opt-out or access-loss purge.
- Memory is the wrong place. Of the 90 `save_to_memory` calls measured before that intent was removed, 8 were notes
  about files (`MEMORY_FACT_CONFIRMATION_RFC.md` §1). They became facts the consolidator curated, and search could not
  connect them to the file.
- The drive itself is ruled out by D1.

### 1.2 Goals

1. The owner says in chat what a file or folder is or relates to; Smart stores it as a note bound to that item.
2. While the item exists and is indexed, its card shows the note first and the search finds it by the note's words,
   in the **About** tier (exact), and by the owner's doc type and life areas as exact keys.
3. The owner's classifiers outrank the model's in the card, the `l:` grouping keys and the D31 type and area words,
   without touching the stored profile.
4. A change costs one card re-embedding. No model call, no re-read, no version bump for anyone.
5. The note outlives everything disposable (D3) and follows the item across renames and moves.
6. A note whose file is gone stays retrievable, but only on explicit request (D9).

### 1.3 Non-goals (v1)

- Bulk annotation ("note every PDF in this folder as …"): one call per item; a batch of up to `NOTE_WRITES_PER_TURN`.
- Inheritance from a folder's note to its children (D8).
- A Cabinet page for notes (§12, OD10).
- Notes written by voice or by background runs (D5).
- A search over **active** notes as a list ("show all my notes"): the card is the surface.
- Structured people, dates or places on the note (D6).
- Speaker naming in transcripts (D10).

### 1.4 Anti-goals (things this design must make impossible)

- A note on the drive in any form (D1).
- The consolidator, the biographical cache, `SearchEnrichmentService` or the remote MCP server's `get_user_context`
  reading `{prefix}drive_notes`. Structural: none of them is given the repository, and an architecture test enforces it
  (§11 S1).
- A note written from untrusted content (an email, a file's text, a web page) and then ranked with owner authority
  (§4.5).
- A note silently lost: replace keeps the previous version; delete keeps the history; archive keeps the note.
- An archived note in a default search result (D9, tested as an invariant, §11).
- Changing any existing card's text or hash, or any key-digest pin, for a user with no notes.

---

## 2. How it works, in one page

```
Owner in chat: "this one is the contractor's estimate for the garden fence; it's a contract, not an invoice"
  Smart (interactive turn)
    → use_skill("drive-file-notes")                       (once per conversation; the tool refuses without it, N7)
    → annotate_drive_file(file_ref="drive:<id>", note="the contractor's estimate for the garden fence",
                          doc_type="contract")
        DriveNoteService.set   (main service; per-execution lock in the handler, Firestore transaction)
          validate → security check → grounding check (the words are the owner's) → resolve the item (Graph)
          → write version v(n+1) + the note document → embed the note → nudge the indexer
        ← receipt: delivery item `drive_note_receipt` (the stored text, verbatim) + history context with the ref
  ConversationHandler posts the receipt to the chat.

Indexer tick (alek-bot-indexer-dev, under the lease)
  _commit (every write) ── created records: look up notes by binding key ──────┐
  notes step: notes changed since the watermark → their records                ├→ with_owner_note(rec, note)
  round end: deleted records with a note → archive; content-key re-binding    ┘   → planned() → card due (class 0)
  card work: card_text now holds "noted by the owner: …" → one embedding call

Search (zero-LLM)
  e:/ex:/exl:/v:/y:/m: keys of the note's words and the owner's classifier names → About tier, "matched: your note …"
  card: a "noted by the owner: …" line inside the nonce-tagged <drive_document> wrapper
  drive_deleted=true (explicit request only) → archived notes, from the note store, never from the index
```

The note store is the record. The index record carries a **denormalized copy** (like `place_name`), rebuilt from the
store whenever the record is rebuilt. Nothing about a note lives only in the index.

---

## 3. Data model

### 3.1 The note document — `{prefix}drive_notes/{user_id}:{note_id}`

`DriveNote` (pydantic, `src/domain/drive_note.py`). One document per noted item (a lineage); `note_id` is our own
opaque id (`uuid4().hex`), never the provider's, so a re-binding or a provider switch never changes a document id.

| Field | Type | What |
|---|---|---|
| `note_id`, `user_id`, `account_id` | str | identity and tenant (multi-tenant rule: always `account_id`). A first write uses the deterministic id `sha256(i-key)[:32]` (below), so two concurrent first writes for one item collide on one document id and the transaction lets one win; when that id is taken by a note with another binding (a note re-bound away from this key), a random id is used |
| `state` | str | `active` (bound, applies to the card) / `cleared` (the owner deleted the note; history kept until he erases it) / `archived` (the item is gone, D9). Plain string, `NoteState` str-enum in the domain |
| `current_version` | int | the version the fields below copy; 0 never happens on a stored note |
| `text` | str | the current note text, after normalization (§4.3); may be `""` when a classifier is set (§4.3: a note is the triple text, doc type, life areas); `""` when `cleared` |
| `doc_type` | str | `""` or one `DOC_TYPES` value (`drive_labels.py:22`) |
| `life_areas` | list[str] | 0–3 `LIFE_DOMAINS` values (`drive_labels.py:29`; `LIFE_DOMAINS_MAX` = 3, `drive_profile.py:124`) |
| `binding` | map | `provider_key` (e.g. `onedrive`), `drive_id`, `item_id`, `bound_at`, `bound_by` (`chat` / `restore` / `rebind_content_key`) |
| `snapshot` | map | the item as last seen by the bot: `name`, `path` (decoded, relative to the area root), `parent_item_id`, `is_folder`, `kind`, `mime_type`, `size_bytes`, `fs_created_at`, `fs_modified_at`, `effective_date`, `content_key` (`content_hash_of(change)`, `drive_card.py:69`: `quickXorHash:size`, the transcript store's key, §27.8 b), `content_key_family` (`quickxor`), `snapshot_at` |
| `match_keys` | list[str] | what a note is looked up by: `i:{provider_key}:{drive_id}:{item_id}` (the binding, the indexer's key); `r:{item_id}` (the ref key: resolves a `drive:<id>` ref with no connection and no drive id, for delete, undo and erase, I8); `c:{content_key_family}:{content_key}` when the snapshot has a content key (files only: folders have no hash). Never a provider URL |
| `keys` | list[str] | the note's exact keys (§5.4: `note_keys(text, doc_type, life_areas)`) plus `name_keys_v2(snapshot.name)`; used only by the deleted-files search (§6.4); `[]` when cleared |
| `keys_version` | str | `"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"`; a mismatch recomputes `keys` in memory at search time |
| `note_vector` | vector(768) | `embed(text + doc type names + life area names + snapshot.name)`, `gemini-embedding-2/768`; removed when cleared |
| `embedding_space`, `embedding_pending` | str, bool | the space the vector is in; `true` when the embedding failed (the indexer repairs it, §5.1 b) |
| `created_at`, `updated_at` | timestamp | **`updated_at` is always a Firestore server timestamp**, never a service clock (the indexer's watermark, §5.1 b) |
| `archived_at`, `last_seen_at` | timestamp | set when archived (§7) |
| `cleared_at` | timestamp | set when the owner deleted the note |
| `erase_pending` | bool | an erased note's contentless stub, waiting for the indexer to clear the record's copy (§4.4) |
| `last_writer` | map | `origin` (`chat`, or `indexer` for lifecycle writes), `session_id`, `turn_id` |

### 3.2 Versions — `{prefix}drive_notes/{user_id}:{note_id}/versions/v{n}`

`DriveNoteVersion` (same module). SCD2, as `FactEntity` (`src/domain/entities.py:95`). A version's content fields
(`text`, `doc_type`, `life_areas`, `action`, provenance) never change; closing `v(n)` updates only its `valid_to` and
`is_current`, in the transaction that writes `v(n+1)`. Only an erase (§4.8) deletes versions.

| Field | What |
|---|---|
| `version` | n, 1-based |
| `text`, `doc_type`, `life_areas` | the note as written (`""` / `[]` for a delete) |
| `action` | `set` / `clear` / `restore` (`restored_from`: the version copied) |
| `valid_from`, `valid_to`, `is_current` | SCD2: a new version closes the previous one in the same transaction |
| `origin`, `session_id`, `turn_id` | provenance: which chat turn wrote it (an injected note is traceable and undoable) |

The layout copies the skills store (`{prefix}skills/{user_id}:{name}` + `versions/v<n>`), so a note's current state is
one document read, history is one subcollection read, and the parent document is the only thing any query touches.
Binding and lifecycle changes (archive, re-activation, re-binding, snapshot refresh) update the **parent** in place:
they are facts about the item, not about the owner's words, and are not versions.

### 3.3 Fields on the index record (`DriveIndexRecord`, `src/domain/drive_index.py:247`)

A denormalized copy, plain fields (Δ47's rollback invariant: no new enum value), neutral defaults:

| Field | Default | What |
|---|---|---|
| `owner_note_id` | `""` | the note bound to this item; also what a delete of this record reports (§7.1) |
| `owner_note_version` | 0 | the note version merged |
| `owner_note_text` | `""` | the note text |
| `owner_doc_type` | `""` | the owner's doc type |
| `owner_life_areas` | `()` | the owner's life areas |
| `owner_note_merge_version` | 0 | `NOTE_MERGE_VERSION` the merge was made under (§5.5) |
| `note_lookup_due` | `False` | set by `_build` on a record it creates (`existing is None`); `_commit` looks up its note and clears it (§5.1 a) |

`_build` (`drive_index_planner.py:304`) builds on `replace(existing or DriveIndexRecord(...), …)`, so the note fields
carry across an observation with no extra code. `firestore_drive_index_repo.record_to_doc` / `record_from_doc` and the
in-memory repository map them (`upsert_records` writes with `merge=True` and `record_from_doc` ignores unknown fields,
so old code tolerates them). **One lean field:** `owner_note_id` joins `DriveRecordLean` and `RECORD_LEAN_FIELDS` in
both adapters (`drive_index_lean.py`), so the duplicate collapse can prefer the noted copy (§6.1). Coverage needs
nothing more: it reads `about_keys`, which hold the note's keys; cards and `matched:` lines are built from full records.

### 3.4 Fields on the sync state (`DriveSyncState`)

Purged with the state, which is correct: each is re-derivable after a purge.

| Field | What |
|---|---|
| `notes_applied_at` | the notes step's watermark: the `updated_at` of the last note it processed (§5.1 b); `None` = re-read every note |
| `notes_merge_version` | the `NOTE_MERGE_VERSION` the merged copies were made under; a mismatch resets `notes_applied_at` (§5.5) |
| `notes_space` | the embedding space the user's note vectors were last repaired into (§5.1 b) |
| `note_checks` | records with a note deleted in the current round: `[{note_id, item_id, why, last_seen_at}]`, `why` = `gone` / `unstored`, cap `NOTE_CHECKS_MAX` (§7.1) |
| `note_rebind_candidates` | `{note_id: [item_id, …]}` seen this round by content key (§7.3) |
| `notes_audited_at` | the daily audit's clock (§7.4) |

`firestore_drive_index_repo` needs each in `STATE_FIELDS` and a reader in `_STATE_READERS` (`:247-260`): `_dt` for
the timestamps, and a reader for the list of maps whose `last_seen_at` is a datetime. The tick collects checks and
candidates in a tick-scoped accumulator (a `ContextVar`, like `_TICK_CLOSED`, `drive_indexing_service.py:433`), because
`_apply`, `_cascade_step` and `_sweep` return no state; it is folded into the state at every save, the error paths'
saves of `saved.state` included. A check lost on a failed save is not data loss: the audit (§7.4) is the backstop.

### 3.5 Firestore indexes (owner step, `config/firestore.indexes.json` + the vector script)

On `{prefix}drive_notes`:
1. `user_id ASC, match_keys ARRAY_CONTAINS` — every lookup by key: the indexer's lookup of created records' notes
   (`array-contains-any`, ≤ 30 keys), `find_bound`, the ref-key resolution.
2. `user_id ASC, updated_at ASC` — the notes step (ordered, paged).
3. `user_id ASC, state ASC, archived_at DESC` — the deleted-files search's archived notes.
4. `user_id ASC, state ASC, updated_at DESC` — the active notes the deleted-files search filters for dormant ones and
   the audit filters for record-less ones (a binding's `drive_id` is compared in memory: `!=` on a map field would need
   its own index for little gain at ≤ 5 000 notes).
5. `user_id ASC, embedding_pending ASC` — the vector repair.
6. A vector index on `note_vector` (768, COSINE) with `user_id`, `state` and `embedding_space` as prefilter fields,
   created by a script next to `scripts/infrastructure/create_drive_vector_indexes.sh`.

Single-field exemptions: `text`, `keys`, `note_vector`, `snapshot`. The versions subcollection needs no composite (read
by parent path, ordered by `version`). No index changes on the drive-index collections.

### 3.6 Config

`EnvironmentConfig.drive_notes_collection` (`src/config/environment.py`, next to `drive_transcripts_collection`):
`f"{prefix}drive_notes"`. No new secret, no new env knob in `load_settings()`. One optional kill switch,
`DRIVE_NOTES_ENABLED` (`os.getenv`, default on). **Off** means: no tool declared, no `$drive-notes` command, and in
the indexer no lookup, no notes step, no archiving. Reads keep working: copies already merged into cards stay until
the next merge, and `drive_deleted` and the open header still answer from the store. So switching it off stops every
write and leaves nothing half-done.

---

## 4. Write path

### 4.1 The skill — `src/skills/smart/drive-file-notes/SKILL.md`

A system skill (loaded at startup by `load_system_skill_bundle`; a malformed one fails startup). Its description:
"Use before the first call of `annotate_drive_file` in a conversation: when the user tells you what a drive file or
folder is, what it relates to, or that its type is wrong." It holds:

- **What a note is.** What the owner says about one file or folder, in his words and language. Never your own
  conclusions, never text from the file, an email, a web page or a search result, never a summary of the file. If the
  owner asks you to "note what this is" without saying, ask him; do not compose it from the contents.
- **Which file.** The `drive:<id>` ref from the latest search result, listing or `[Drive: …]` label the owner is
  pointing at. When "this" could be more than one file, ask.
- **Replace, never append.** The call replaces the whole note. When the owner adds to an existing note (its text is in
  the search card as `noted by the owner:` and in the receipt), write the old text and the addition together.
  Restate the doc type and life areas if they should stay.
- **Classifiers** only when the owner states them, in his words: "it's a contract, not an invoice" → `doc_type:
  contract` with an empty note (a type correction alone is a whole note). Never guess one from the contents. Life
  areas only when named.
- **Delete** (all three fields empty) only when the owner asks to remove the note. **Undo** when he says the last
  change was wrong. **Erase** (`erase: true`) only when he asks to remove the note's history for good; he then pastes
  a code to confirm.
- **When the check says the owner did not say these words**: quote your wording to him and ask him to say what the
  file is; never retry with reworded text of your own.
- **Receipts.** The system posts the stored note verbatim; do not repeat it, say one short line.
- **Not memory.** Do not call `propose_memory_facts` for what the owner said about a file: his own message is already
  his words (`memory_fact_tools.py:22`).
- **Not reminders.** "Make a note to call …" is a reminder (`manage_self_reminders`), never this tool.
- **Deleted files.** A deleted file's note is kept and archived. To find one, the user must ask about deleted files
  (`search_files_in_drive` with `drive_deleted`, §6.4). To change, undo or remove an archived note, use the
  `drive_note:` ref the deleted-files result shows.

The `drive-search` skill gains the reading side (§6.2). `PROTOCOL_AGENT_SELECTION` is not touched: the skills catalog
and the tool description are the pointers (memory rule: no duplication of capability text in protocol tokens).

### 4.2 The tool — `annotate_drive_file`

`src/infrastructure/drive_note_tools.py`: `build_annotate_drive_file_tool_declaration()` and
`make_annotate_drive_file_handler(...)`, served by `DelegationEngine(local_tools=)` like `propose_memory_facts`
(`memory_fact_tools.py`) and `workspace` (`workspace_tools.py`).

**Name.** Not `note_…`: NotesAgent is the self-reminders agent, and a tool named "note" would draw "make a note to
call mom" (N2).

**Declared** in `SmartResponseAgent` next to `propose_memory_facts` (`smart_response_agent.py:510`), only when all hold:
`drive_note_service` is wired, `DRIVE_NOTES_ENABLED`, `prompt_user_id`, and `message.context["interactive_delivery"]`
(set only by `ConversationHandler`, `conversation_handler.py:935`). So reminder runs, the daily review, `ask_alek` /
`tell_alek`, `/worker` and every other background path cannot see it (D5). A voice memo the owner sends in chat is a
chat turn: its transcript is his own message (`decisions/voice_message_transcription.md`), so the tool is there (OD9).

**Schema** — flat, liberal input; only `file_ref` is required; no enum, pattern or `maxItems` in the schema (limits in
the descriptions, enforced in code; `decisions/mcp_tool_schema_liberal_input.md`):

| Parameter | Type | Description (abridged) |
|---|---|---|
| `file_ref` | string | The `drive:<id>` ref of the file or folder, copied from a search result, a listing or a `[Drive: …]` label; or the `drive_note:<id>` ref a deleted-files result shows. |
| `note` | string | The owner's words about the item, in his language: what it is, who or what it relates to. At most 80 words and 600 characters. May be empty when you set only `doc_type` or `life_areas`. |
| `doc_type` | string | Only when the owner says what kind of document it is: one of `receipt, invoice, …, other` (the 31 `DOC_TYPES`, listed). Empty: none. |
| `life_areas` | array of string | Only when the owner names them: up to 3 of `finance, health, …` (the 14 `LIFE_DOMAINS`, listed). |
| `undo` | boolean | `true`: put back the note as it was before its last change. Other fields are ignored. |
| `erase` | boolean | `true` only when the owner asks to erase the note's history for good: the owner gets a code to confirm (§4.8); nothing is erased by this call. Other fields are ignored. |

The tool description states the replace rule once: **"Every call replaces the whole note — its text, its doc type and
its life areas. A field you leave empty is removed. All three empty deletes the note."**

Accepting liberally: `doc_type` and `life_areas` also accept any name of `DOC_TYPE_NAMES` / `LIFE_DOMAIN_NAMES`
(`drive_labels.py:93`, `:153`; "договор", "contrato", "житло") matched by the keyer's own forms, and map them to the key.
An unknown value is refused with the list, never dropped silently.

**Handler.** Built per execution attempt, as the workspace handler: one **per-execution** `asyncio.Lock` taken first
by every call (a batch applies in emission order; safety across turns and channels rests on the transaction, §4.4), the
mutation under `asyncio.shield` (a cut turn still finishes and logs the write it started), and a per-execution count
(`NOTE_WRITES_PER_TURN` = 10, then a refusal: a steered mass write is bounded). A write identical to the current
version (same triple) is a no-op success with no new version, so a cross-provider failover that re-runs the transcript
with a fresh handler does not double versions.

**Declared outside `if skills:`** (`smart_response_agent.py:496`), as the workspace tool is, so the tool exists even
when the skills store fails. The skill gate (§4.5 layer 2) then fails closed: with no skills loaded (`_load_skills`
degraded to `[]`, so no `use_skill`), every call is refused with "notes are unavailable right now". Stated, accepted.

### 4.3 Validation and normalization (`src/domain/drive_note.py`, pure)

- NFC; every run of whitespace (newlines included) becomes one space; trimmed. The card segment is one line.
- At most `NOTE_WORDS_MAX` = 80 words (`token_spans` of the keyer, so the count is script-independent) and
  `NOTE_CHARS_MAX` = 600 characters. Over either: refused with the counts ("shorten it to the owner's essentials"),
  never cut silently.
- A tag look-alike of the drive wrapper is not refused here (the renderer neutralises it on every render,
  `drive_search.py:109-131`); control characters are removed.
- `doc_type` / `life_areas` validated and mapped (§4.2). Life areas deduplicated, in the owner's order, at most 3.
- **A note is the triple (text, doc type, life areas).** Text may be empty when a classifier is set: "it's a contract,
  not an invoice" is `note = ""`, `doc_type = contract`, the headline correction of §1.1. **An empty triple deletes the
  note.** An empty triple with no current note: a no-op success ("there was no note").

### 4.4 `DriveNoteService` (`src/services/drive_note_service.py`)

Constructor ports: `DriveNoteRepository`, `UserDrivePort`, `DriveIndexRepository` (read only: searchability),
`EmbeddingService`, `SecurityPort`, the drive `TaskQueue` (nudges; optional), `LocalizationPort` not needed (the
receipt is localized by `ConversationHandler`). No other service is imported (REQ-ARCH-22).

The handler routes a call to one of four methods: a non-empty triple → `set`; an empty triple → `clear`;
`undo = true` → `undo`; `erase = true` → `request_erase` (§4.8). **Only `set` touches the drive.** `clear`, `undo` and `erase` (§4.8) resolve the note from the
store alone, so they work on archived and dormant notes and while the drive is disconnected (D3: a note is deleted on
the owner's command, in every state):
- a `drive_note:<note_id>` ref is the note itself;
- a `drive:<id>` ref resolves by the ref key `r:{id}` (`find_by_match_keys`, no drive id needed). One active, cleared
  or archived note → that note. Several (the same item id on two drives, practically never) → the one bound to the
  connected drive, else refused with the `drive_note:` refs of each.

`set(user_id, account_id, ref, note, doc_type, life_areas, provenance) -> NoteReceipt`:

1. **Gates** (§4.5): security check, then grounding check (text and classifiers), both before any I/O on the drive.
2. **Resolve the item.** `connection(user_id)` gives `drive_id`; `set` is refused when not connected ("connect the
   drive first": the item cannot be verified). The order on a 404 (both "gone" and "cannot open" answer 404, §26.2):
   1. read the index record (`DriveIndexRepository.get_records`); `inaccessible_since` set → refused with the
      `drive-search` skill's copy instruction (OD12);
   2. else `get_change(user_id, item_id)` (name, parent, size, dates, the hashes: the content key) and `get_item` (the
      decoded path): two Graph GETs, the endpoint Step 1 already calls;
   3. `DriveItemNotFoundError` there → refused ("the file is not on the drive"; with indexing off, an item the bot
      cannot open reads the same way: named residual).
3. **Find the lineage.** `find_bound(user_id, i-key)` (one `array-contains` query on `match_keys`). One note per item:
   a cleared note is reused, so history and undo stay continuous. No note → a new lineage with the deterministic id
   (§3.1), created inside the transaction with a must-not-exist precondition, so a concurrent first write from another
   turn or channel loses the race and retries into the existing lineage.
4. **Embed** the new triple (one call). A failure sets `embedding_pending`; the write goes on.
5. **Transaction** (`write_version`, precondition `current_version == read value`): close the current version
   (`valid_to`, `is_current = false`), add `v(n+1)`, write the parent (text, classifiers, state, binding, snapshot,
   `match_keys`, `keys`, vector, `updated_at = SERVER_TIMESTAMP`, `last_writer`). A precondition failure (a concurrent
   write) is retried once from step 3, then refused.
6. **Nudge** `[item_id]` through the same `drive_sync` path as `UserDriveService._nudge`
   (`user_drive_service.py:205`): never raises; a failure is logged and the next fan-out picks the note up.
7. **Searchability**, from `DriveIndexRepository.get_records(user_id, [item_id])` and the opt-in flag: `soon` (a
   carded record: the card shows it within minutes), `indexing_off`, `not_indexed` (no record: inside a folder the
   index keeps as one card, or not seen yet), `no_card` (card-capped, unresolved).
8. Log one line `[DriveNote] set user=… note=… v=… words=… status=…` — ids, counts and the status, **never the text**.

`clear(...)` writes a `clear` version (empty triple) and sets on the parent `state = cleared`, `cleared_at`, and
empties **every field derived from the note**: `text`, `doc_type`, `life_areas`, `keys` (`[]`), `note_vector`
(removed), `embedding_pending` (false). The snapshot (the file's name and path, not the owner's words) and `match_keys`
stay, so undo can find the note. The history stays until the owner erases it (§4.8); the receipt says so: "history
kept; undo possible; say 'erase the history' to remove it". A cleared archived note stays `cleared`.

`undo(...)` copies the version before the current one into a `restore` version and re-derives `keys` and the vector:
undo of a first note clears it; undo of an undo restores the undone version (redo), stated in the receipt. Undo of an
`archived` note restores the text and keeps `archived`; undo of a `cleared` note that was active brings it back
`active`.

Limits: `NOTES_PER_USER_MAX` = 5 000 notes (a count aggregation before a new lineage; refused beyond). Versions are not
capped: a note changed a hundred times is a hundred small documents.

### 4.5 Guards against a note written from untrusted content

The real risk (research §E) is **indirect injection at write time**: an email, a file's text or a web page steering
Smart into writing a note, which then ranks About with owner authority. Five layers, in order:

1. **Interactive only** (D5, §4.2): no background path can write.
2. **Skill gate** (N7): the handler refuses ("load the drive-file-notes skill first") until `drive-file-notes` was
   loaded in this conversation — in this execution through `use_skill`, or earlier, as seen by `visible_skill_names`
   (`src/domain/skill.py:324`). `make_use_skill_handler` keeps its `loaded_now` set private (`skill_tools.py:56`), so it
   gains a parameter: a `LoadedSkills` set object Smart creates per execution and passes to both handlers. The skill's
   "owner's words only" rules are then always in context. The cost is one extra round trip per conversation at most.
3. **Security check**: `SecurityPort.validate(text, context="drive_note_user_…", zone=TrustZone.UNTRUSTED)`, the same
   call `MemoryFactService._check` makes (`memory_fact_service.py:79-92`); blocked or flagged is refused.
4. **Grounding check** (N8, OD1): the note must be mostly the owner's own words, and each classifier must be one he
   said.
   - **The owner corpus** is built in `SmartResponseAgent` and passed to the handler. It is the raw current message
     (`current_message_parts`) and the last `NOTE_GROUNDING_TURNS` (6) user turns of the conversation history **as
     loaded, before `_inject_user_turn_anchor`** (`base_agent.py:366-395`): the `USER_TURN_SYSTEM_ANCHOR` rules are
     never part of it. `_inject_timestamps` (`base_agent.py:331-360`) has already prefixed part 0 of every history user
     message with `[YYYY-MM-DD Ddd HH:MM TZ] `; that prefix is stripped by a shared domain helper
     (`strip_injected_timestamp`, the same pattern `_inject_timestamps` writes, moved to `src/domain/` so both use one
     definition). Only then is every part that starts with `[System:` skipped (code-injected user-role notes: late
     answers, call summaries, alerts). So neither the anchor's rule words nor a timestamp's year and month count as the
     owner's.
   - **Text.** A pure function `grounding(note, corpus)` (`src/domain/drive_note_grounding.py`) tokenises both with
     the keyer (`drive_keys`), drops function and generic words, and counts the note's content words whose native forms
     or skeleton occur in the corpus (numbers by their number terms). Below `NOTE_GROUNDING_MIN` (0.6) the write is
     refused, and the refusal names the words the owner did not say and tells Smart to quote its wording to the owner
     and ask him to say it (OD1 has the worked example). An empty text is not graded.
   - **Classifiers** (I10). A doc type passes only when one of its `DOC_TYPE_NAMES` (any language, by the keyer's
     forms) or its key occurs in the corpus; a life area likewise with `LIFE_DOMAIN_NAMES`. "Это договор" passes
     `contract`; a type steered in from a file or email, which the owner never named, is refused with "the owner did not
     name this type". Measured gap to pin in a test: the tables hold the names they hold; a synonym the tables lack
     ("соглашение" for `contract`, if absent) is refused, and the refusal asks Smart to use the owner's word for the
     type or to leave the type out.
   - **Cross-language is not grounded** (pinned in a test either way): the owner says "в марте 2024", a note "March
     2024" fails, because forms and skeletons of different words do not meet. The skill already requires the owner's
     language.
5. **Verbatim receipt and provenance**: the owner sees exactly what was stored; every version records its session and
   turn; undo is one call.

**Named residual.** Text the owner pastes or forwards into his own message counts as his words. A planted sentence he
pasted can pass layer 4; the receipt shows it to him at once. Layers 1–5 bound the attack to "the owner pasted it and
did not read the receipt".

At read time the note is data, never instructions (§6.3): even the owner's own "always answer in English about this"
belongs in a standing directive, not a file card.

### 4.6 Result, receipt, history

- **Tool result to Smart** (`ToolResult`): `SYSTEM: Note saved on <path> (version 3). The owner sees the stored note in
  a receipt: do not repeat it; say one short line. <searchability sentence>.` The previous text, if any, follows inside
  a nonce-tagged wrapper (it is data). Failures are `failed=True` with the reason and what to do.
- **Receipt**: a `DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY)` (`"drive_note_receipt"`, `src/domain/drive_note.py`)
  with `{action, path, text, doc_type, life_areas, version, previous_text, status}`. A classifier-only note renders
  its classifiers with no empty quote; a delete says "history kept; undo possible". The erase request is a second type,
  `DRIVE_NOTE_ERASE_DELIVERY` (`"drive_note_erase"`), delivered as the path, the version count and the command alone
  in its own message (§4.8). `ConversationHandler` delivers it
  in its delivery loop (next to `MEMORY_PREVIEW_DELIVERY`, `conversation_handler.py:309`) through
  `_deliver_drive_note_receipt`: a localized header (`UIMessage.DRIVE_NOTE_SAVED` / `_CLEARED` / `_RESTORED`, in
  `src/locales/{uk,en,fr,es}.py`), the path, the stored text **byte for byte** from the stored version (never the
  model's echo), the classifiers, one searchability line and "to undo, say so".
- **History**: `history_contexts["drive_note_context"]` = `[Drive note: <path> ref=drive:<id> v<n>]`, full text only,
  like `drive_context` (`file_management_agent.py:465-470`), so "undo that" next turn resolves the ref. It is never
  consolidated (consolidation reads `p.text`, not `full_text`).

### 4.7 The owner's chat sentence and memory

The note store is never read by the consolidator (anti-goal §1.4). The owner's own chat message ("this is the contractor's
estimate for the garden fence") is still his message and goes through ordinary consolidation, like everything he says; the
file operation itself is already excluded by `Trivial_Exclusions` (`USER_DRIVE_RFC.md` §4.12). Whether the sentence's
life content should be excluded too is OD4 (default: no change).

### 4.8 Erasing history (S3, OD3)

Deleting a note (empty triple) keeps its history so undo works. Erasing removes the history too, and is irreversible,
so it never runs on one model call or one paste. Both forms use a confirmation code, the `$memory save <code>` pattern:

- **One note.** The tool's `erase: true` (a sixth parameter: "only when the owner asks to erase a note's history for
  good") stores an erase request `{note_ids}` under `{user_id}:{code}` in a `FirestoreEphemeralStore`
  (`src/ports/ephemeral_store.py`; its own collection `{prefix}drive_note_erase_requests`, TTL `ERASE_CODE_TTL` 10 min
  through the store's `expires_at` TTL policy; redeemed with the atomic `get_and_delete`, so a code works once). It
  holds note ids only, never text. The tool returns a `drive_note_erase` delivery: the note's path, its version count, and the command
  `` `$drive-notes erase <code>` `` as a separate message. Nothing is erased by the tool.
- **All notes.** The owner types `$drive-notes erase-all`; `ConversationHandler` answers, before any LLM call, with
  the count of his notes and `` `$drive-notes erase-all <code>` ``.
- **Pasting the command** (`ConversationHandler._handle_drive_notes_command`, before any LLM call, own notes only, like
  `$skill`) calls `DriveNoteService.erase(user_id, code)`. It deletes every version and leaves per note a contentless
  stub: only `note_id`, `user_id`, `account_id`, `state = cleared`, `erase_pending = true`, `match_keys` and
  `updated_at` (bumped). Text, classifiers, `keys`, `note_vector`, `snapshot`, `binding` and `last_writer` are gone at
  once. The stub exists only so the indexer's notes step sees the change (§5.1 b), clears the record's copy and then
  deletes the stub: a document that is simply deleted never appears in a `changed_since` query, so the copy on the
  record would outlive the note. With indexing off there is no copy (the opt-out purged the index) and the stubs are
  deleted at once. The reply confirms the count erased.
- A wrong, expired or someone else's code: "no such erase request"; nothing happens.

---

## 5. Index merge

### 5.1 Where in the tick

The indexer is the only writer of `drive_index_records` (lease fence, README §3, §5). The main service writes the note
store and nudges; the tick merges. Three entry points, all in `DriveIndexingService` (`src/services/drive_indexing_service.py`),
all through one pure function:

```
with_owner_note(rec, view: Optional[NoteView]) -> DriveIndexRecord     # src/domain/drive_note_merge.py
```

`NoteView` = `(note_id, version, text, doc_type, life_areas)` of an `active` note bound to this item on the current
drive, else `None` (clears the fields). The result goes through `planned()` (`drive_enrich_plan.py:459`: `about_keys`
are computed only there) and `schedule()`; the changed `card_hash` makes card work due in class 0 (`_card_due`,
`drive_index_planner.py:617`).

**(a) Created records, in `_commit`** (`:934`, the one write gate). `_build` sets `note_lookup_due = True` on every
record it creates (`existing is None`). Creation happens in `_apply` (`:821`) and also outside it — `_resolve_step`
creates an out-of-order child's parent folder through `_commit_plan(user_id, plan_observation(parent_change, None, …))`
(`:1286`), and `_commit_plan` (`:1363`) ends in `_commit` — so the hook sits in `_commit`, not in its callers. Before
the write, every flagged record gives its binding key `i:{provider}:{drive_id}:{item_id}` and, with a content hash,
its `c:` key. They are looked up in batches of 30 keys (`find_by_match_keys`, one `array-contains-any` query per
batch). Every flagged record leaves with `note_lookup_due = False`, found or not. For each flagged record:
- an `active` note with that `i:` key → merged in the same write;
- an `archived` note with that `i:` key → re-activated (§7.2) and merged;
- an `active` note matching only by `c:` and bound to **another** drive → a re-binding candidate (§7.3), not merged.

This covers onboarding after any purge, a recycle-bin restore, a subtree re-listing, an opaque folder opening and a
resolved parent: every way a record comes into existence goes through `_commit`. A tick first runs one `count()` of the
user's notes (one read) and skips every lookup at zero (the flag is still cleared), so a user without notes pays one
read per tick.

**(b) The notes step**, every tick, after observe and before work-off.
1. When `state.notes_merge_version != NOTE_MERGE_VERSION`, the watermark is reset to `None` and the version stamped
   (§5.5).
2. `changed_since(user_id, after = notes_applied_at − NOTE_WATERMARK_SKEW_S, limit = NOTE_PAGE)`, ordered by
   `updated_at` ascending (60 s skew; idempotent re-merges make the overlap free). `None` reads from the start: a full,
   paged re-read of the user's notes.
3. For each note in the page, the record of its bound item (when the binding is on the current drive) is read in one
   batch and reconciled: `with_owner_note(rec, view)`, or `with_owner_note(rec, None)` for a cleared or archived note.
   Records that do not exist are skipped: entry (a) covers them when they appear. An `erase_pending` stub is deleted
   once its record is cleared (or has no record).
4. The watermark advances to the `updated_at` of the **last processed** note and is saved with the state (under the
   lease). A full page (`NOTE_PAGE` = 200) means more: the tick asks for a continuation, so a re-read of 5 000 notes
   takes 25 ticks, never one unbounded read.
5. Vector repair: up to `NOTE_EMBED_PER_TICK` (20) notes with `embedding_pending` (index 5) are re-embedded. On an
   embedding-space switch (`state.applied_space` ≠ `state.notes_space`) every note is re-embedded, paged through
   `changed_since` from the start like step 2, and `notes_space` is stamped when the pass ends; until then the
   deleted-files search's vector fallback prefilters on the current `embedding_space`, so a vector in the old space is
   never compared. A user with indexing off keeps a pending vector, which only costs the deleted-files search its
   meaning fallback.

**(c) Round end**, at the start of `_work_off` when `state.round_complete`: the archive checks (§7.1) and the
re-binding decisions (§7.3). Cheap, no share of the start budget.

**Lease.** Record writes go through `_commit` under the lease fence as today. Note-store writes from the indexer
(archive, re-activation, re-binding, snapshot refresh) are transactions on the note's parent document, idempotent, and
stop at the same clock (`lease_until − LEASE_MARGIN`) as every tick write.

**Why a watermark, not a counter (N3).** The research's option (b) needs a per-user `notes_rev` counter document,
bumped in every write transaction. Firestore server timestamps already give a monotone, externally consistent order of
commits, so `updated_at` is the counter, with no second document to keep in step. Entry (a) removes the research's
"load every note after a purge" pass.

### 5.2 Latency and cost

- A note shows in search after one queue hop and one tick: seconds; 60 s when the lease is busy (README §5 step 2);
  the next 10-minute fan-out at worst.
- Per note change: one note embedding (main service), one card embedding (indexer), a handful of Firestore reads and
  writes. ≈ $0.000006. No model call, no download, no re-read.
- Per tick, for a user with notes: one count and one `changed_since` query (2 reads when nothing changed), plus one
  query per 15 created records. For a user without notes: one read.
- A `NOTE_MERGE_VERSION` bump or a space switch: one read per note, paged, plus one card embedding per noted record
  (the bump) or one note embedding per note (the switch).

### 5.3 The card segment (`src/domain/drive_card.py`)

`owner_segment(rec)` = `noted by the owner: ` followed by the present parts joined by `; `: the text, `type <doc_type>`,
`life areas <a>, <b>`. A classifier-only note reads `noted by the owner: type contract` — never an empty quote. `""`
when the record has no note.

`card_text(rec, *, owner: bool = True)` (`:170`; the new keyword is declared here and used only by the renderer, §6.2)
puts the segment **right after `name | kind`** in every branch: the profiled file, the text file without a profile,
machine text, the photograph, the transcribed recording, the collapsed folder (after its label) and the plain folder
(`name | folder | <segment>`, built with `_joined` so a folder without a note stays `name | folder`). In the profiled
branch the profile's doc type segment is **dropped** when the owner gave a type (the owner segment already says
`type contract`; the type is never shown twice), and is `profile.doc_type` otherwise.

**No `CARD_VERSION` bump (G5, D7).** `_joined` drops empty segments, so every record without a note keeps a
byte-identical `card_text` and `card_hash`; only noted records re-embed, through ordinary class-0 card work. A test
pins it on a fixture of every branch (§11 S2).

The card vector includes the note, so the Related-by-meaning tier benefits too.

### 5.4 Keys: `e:` under `AboutField.OWNER_NOTE`, no version bump (verified)

`note_keys(text, doc_type, life_areas)` is a new public function of `drive_keys.py` (the one normaliser, P3), used by
`_about_items` and by the note store's `keys`. `_about_items` (`drive_keys.py:368`) emits the note's items **first**, so
the `ABOUT_KEYS_PER_RECORD` cap (600, earliest kept) can never cut them. Within the note, in this order, and the
note's own cap `NOTE_KEYS_MAX` (320) cuts only the tail:

1. **The owner's classifiers, never cut.** Each name of `DOC_TYPE_NAMES[doc_type]` as an `e:` phrase, detail `"type"`;
   each life area's `LIFE_DOMAIN_NAMES` likewise, detail `"area"`. Native forms only, **no skeletons**, as the D31
   `lw:`/`dw:` names (`drive_keys.py:438-453`). Measured with the real keyer: up to 21 keys for one doc type
   (`certificate`) and up to 62 for three life areas (`vehicle`, `education`, `leisure`): at most ≈ 85, inside the cap
   by construction; a table test pins that the worst type plus the worst three areas stays under 100.
2. **Numbers and dates, with the profile's own recipe** (no fragment keys in the exact tier): for each number span
   `query_term_spans` finds, `_value_forms` (`drive_keys.py:333-340`: the maximal forms the query emits, so "1 250,00"
   keys `v:1250.00` and `v:1250`, never `v:250`) and `_unspaced_id_forms` for an id; for each date `_date_items`
   (`v:` the ISO form, plus `y:` and `m:`, `:355-359`); for each month-year span `_month_year_spans` → `m:YYYY-MM`; a
   standalone four-digit year 1900–2099 → `y:`. Detail `"number"` / `"date"`.
3. **Words.** Each content word (function, generic and time words removed, as the query does) as its own phrase via
   `add_phrase(_E, word, AboutField.OWNER_NOTE, "word", skeletons=True)`: `e:` native forms (About) and its `ex:` /
   `exl:` skeleton (By translation). Measured: 183 keys for a Russian note of 84 tokens (65 content words), 197 for
   the Ukrainian equivalent; a noun-dense 80-word note reaches ≈ 240.

So the worst case is ≈ 85 + a few number keys + 240 ≈ 330, just over the cap: a maximal Cyrillic note with three areas
may lose its last words' keys. Those words stay in the card text and its vector (Related); the classifiers and the
earlier words are never cut. A full rich profile plus a maximal note may push the record past 600, cutting the file's
own latest automatic keys (`keys_capped`, "keys cut" on the card). `planned()` keeps `keys_capped = rec.keys_capped or
capped` (`drive_enrich_plan.py:466`), so that flag stays after the note is cleared (named residual, as for any other
key growth today).

**Overrides at read time (D7).** `l:type:` carries `effective_doc_type(rec)` = `rec.owner_doc_type or
profile.doc_type`, and is emitted whenever the owner gave a type, **also on a record with no profile** (today `l:type:`
is emitted only `if profile is not None`, `drive_keys.py:384-387`), so goal 3's grouping holds for an unread file too;
`l:domain:` likewise for the owner's areas. The D31 names the profile's doc type and areas add under `lw:` / `dw:` (`drive_keys.py:438-447`) are left out
for an overridden field (the owner's names are under `e:` already); the model's own `label_words`, `topic_words` and
`domain_words` stay (they are the model's words, Labelled). Named residual: a file the owner calls a contract can still
appear Labelled for "invoice" through a word the profiler wrote.

**Why this needs no bump — checked against the code:**
- `index_tables_digest()` (`drive_keys.py:748`) hashes the tables and constants and `[f.value for f in KeyFamily]`
  (`:755`). No family is added (`e:`, `ex:`, `exl:`, `v:`, `y:`, `m:` exist) and `AboutField` is not in it.
- `golden_digest()` (`:774`) hashes the keyer's output on fixed texts and one record built with `RecordKeyInputs`
  (`drive_key_sources.py:100`). The new `RecordKeyInputs` fields default to empty, so that record emits no note items:
  unchanged. The capture and scene digests (`test_drive_keys_tables.py`) likewise.
- The query side already emits `e:`, `ex:`/`exl:`, `v:`, `y:`, `m:` for every word and number (`_word_concept`,
  `:604-629`; `_number_concept`, `:523`).
- The source class follows from the prefix (`FAMILY_SOURCE_CLASS`, `drive_match.py:70`): `e:`, `v:`, `y:`, `m:` are
  VERIFIED_RECORD, so the About tier; skeletons are TRANSLATION. Owner words are owner-verified, so the class is right.
- `NOTE_WORDS_MAX`, `NOTE_KEYS_MAX` and the note rules are kept **out** of `index_tables_digest` on purpose: they shape
  only noted records, and every noted record is re-merged by `NOTE_MERGE_VERSION` (§5.5). Their own pin is
  `note_keys_digest()` (fixed notes in four languages), pinned per `NOTE_MERGE_VERSION` in a new test.

**The cost of reusing `e:`.** `e:` stops meaning only "printed in the file". The `KeyFamily.ENTITY` comment
(`drive_match.py:33`), README §3.1's table and the RFC's §25.2.4 table say "or the owner's note"; the `matched:` line
says where it came from (§6.1). A new `on:` family would be cleaner and forces a whole-drive re-key of every user
(`_keys_step`, `:2884`): rejected by D7.

`KeyedRecord` (`drive_key_sources.py`) gains `owner_note_text`, `owner_doc_type`, `owner_life_areas`; `RecordKeyInputs`
gains them with empty defaults.

### 5.5 Versions

- `NOTE_MERGE_VERSION` (`src/domain/drive_note_merge.py`, initial 1): the card segment and the note's keys. **Not** in
  `CURRENT_VERSIONS`, and not through the upgrade pass: that pass starts only when `applied_versions` differs from
  `_target_versions()`, which is built from `CURRENT_VERSIONS` (`drive_indexing_service.py:3473-3506`), and putting the
  version there would page through every record of every user (N9). Instead the sync state stores
  `notes_merge_version`; a mismatch resets the notes watermark (§5.1 b step 1), so the notes step re-reads every note
  from the store and re-merges exactly the noted records: a few hundred reads, idempotent. The same reset repairs a
  rollback followed by a roll-forward (old code re-plans noted records without note keys; the fields survive, and the
  roll-forward's version mismatch re-merges them).
- `NOTE_KEYS_VERSION` (`src/domain/drive_note.py`, initial 1): the note store's `keys` layout (§3.1), recomputed in
  memory on a mismatch.
- A `KEYS_VERSION` bump re-keys noted records like any other (`planned()` reads the note fields). A `CARD_VERSION` bump
  re-embeds them with the rest. An embedding-space switch re-embeds their cards and, through §5.1 (b), the note vectors.

### 5.6 What the note never changes

The stored profile (D21 of the index RFC: the profile is a function of the file's content, and the profiler is never
given the note), the accuracy score (`drive_accuracy.py` grades the profile's dates, values and entities; the note and
the owner's classifiers are not graded values), the record's `effective_date`, the content work, the transcript job.

---

## 6. Search

### 6.1 Default search

Nothing new in the pipeline. A noted record's `about_keys` hold the note's keys, so the about stream finds it, strict AND
applies across the query's words, and a word covered by the note counts as verified (About) or, by skeleton, By
translation. `drive_tier_explain._about_text` (`drive_tier_explain.py`) renders `AboutField.OWNER_NOTE` as
`your note "<word>"`, `your note: type "contract"`, `your note: life area "home"`.

**Archived notes never reach it (D9).** They are merged into no record: entry (b) clears a record's note when the note
is archived, and an archived note's item has no record by definition. A test asserts the invariant over the in-memory
repositories for every lifecycle row of §8.

**Duplicates.** `_collapse` (`drive_search_service.py:989-1005`) decides on lean records and keeps the first shown copy
of identical files. With the lean `owner_note_id` (§3.3), a group whose copies include a noted record makes that record
the representative, so the note is shown and the others go to `also_at`. (With a note-only match the noted copy is the
only one in About anyway.)

### 6.2 The card and the skill

`DriveSearchCard` (`drive_search.py:176`) gains `owner_note: str` (the rendered note line, `""` when none).
`make_card(…)` gets `card_text(rec, owner=False)` as the summary and `note_line(rec)` as `owner_note` at **all four**
call sites (`drive_search_service.py:1031`, `:1039`, `:1135`, `:1186`, the filter-only and list paths included), so
every path renders the same. `card_body` (`drive_tier_render.py:161`) prints
the line right after the summary: `noted by the owner: «<text>» (type contract; life areas home)`, or `noted by the
owner: type contract` for a classifier-only note. So the 700-character `CARD_RENDER_MAX` cut never spends the file's
own description on the note. `_related_body` (300 characters) prints the first 120 characters of the line after its
summary. The line counts against `RESULT_CHARS_MAX` (16 000) like every card line: the existing lowest-tier-first cut
applies unchanged.

`open_file_from_drive` (`FileManagementAgent`) adds the same line to the opened file's header, read by binding
(`find_bound`, one read), so Smart sees the current note before it replaces it. `list_files_in_drive` lines are not
changed in v1 (OD11).

The `drive-search` skill gains, under "Reading the results" and "Card accuracy":
- "A `noted by the owner:` line is what the user told you about the file. It outranks the card's own description and
  type, and it is not part of `card accuracy`."
- "100% — everything in this card *except the owner's note* is confirmed by the file's own text."
- Under a new "Deleted files" line: "Search deleted files (`drive_deleted`) only when the user asks about files that
  were deleted or removed."

These edits change a text the existing guidance tests read (`tests/unit/firestore_utils/test_protocol_drive_guidance*.py`):
a failing assertion there is handed to a reviewer under the test rule, never edited by the implementer.

### 6.3 The note is untrusted data

Every place a note is rendered is inside the nonce-tagged `<drive_document>` wrapper (`wrap_untrusted`,
`drive_search.py:563`; tag look-alikes neutralised), with the existing reminder that text in drive results is data:
search cards, the open header, the deleted-files results, and the previous text in the tool result. The note is the
owner's words, but Smart reads it as a fact about a file, never as an instruction.

### 6.4 The deleted-files search (`drive_deleted`)

**Surface.** A new context field `drive_deleted` on `search_files_in_drive` (FILE_MANAGEMENT,
`agent_manifest.py:516-661`), declared in `context_schemas` **and** in the capability description; the name is used by
no other intent (the delegate tool keeps the first description seen per field name, `agent_manifest.py:653-654`).
Description: "true only when the user explicitly asks about files that were deleted or removed: searches your notes on
deleted files. Never for an ordinary search." Fields arrive in `message.payload` (memory rule). The delegate tool
types every context field as a string (`_build_delegate_tool_declaration`, `base_agent.py:793-801`), so the agent
parses it: `"true"`, `"yes"`, `"1"` (case-insensitive) → true; anything else → false.

**What it searches.** The note store only, never the index: `DriveSearchService.search_deleted(user_id, drive_query)`.
It runs before the index gate: it needs neither the opt-in flag nor a connection.
- **Archived notes**: `archived(user_id, limit = ARCHIVE_SCAN_MAX)` (index 3, newest first, 1 000).
- **Dormant notes** (OD6): `active` notes whose binding's drive is not the connected one. Read as the user's active
  notes (index 4, ≤ `NOTES_PER_USER_MAX`, a projection without `note_vector`) and filtered in memory on `binding.drive_id`. **While disconnected** there is no
  connected drive to compare with: dormant notes are not shown, and the result says "the drive is not connected; notes
  on its files are not shown here".
1. `query_concepts(drive_query)` as the default search does (one normaliser).
2. Strict AND, in memory: a note matches when every concept's about keys meet the note's `keys` or its word keys meet
   the snapshot name's keys. No allowances: the set is small.
3. Nothing exact: one `find_nearest` on `note_vector` (prefilter `user_id`, `state = archived`, `embedding_space` =
   the current space), at most 2 lines labelled "closest by meaning, unverified". Dormant notes have no meaning
   fallback (the prefilter cannot express "another drive"): keys only.
4. An empty `drive_query` lists the newest 10.

**Render** (`src/domain/drive_note_render.py`): a heading "Deleted files with your notes (N):"; per note one wrapper
holding `<name> | <last known path> | gone from the drive (deleted or moved out of the bot's folder), last seen
<date>`, the note line, `matched: your note "…"`, and the ref `drive_note:<note_id>` with "the file is gone: this ref
works only for annotate_drive_file (to change, undo or remove the note)". A dormant note says "on a drive that is not
connected" instead. A coverage line names a scan cut ("the oldest N archived notes were not searched").

---

## 7. Lifecycle mechanics

### 7.1 Archiving

A record leaves the index for two different reasons, and only one of them means the file is gone (review C1).
`plan_observation` deletes a record whenever the item is no longer *stored* (`drive_index_planner.py:510-514`: `if
not cls.stored: … delete_item_id=existing.item_id`), and that happens to files that still exist: a file moved into a
repository, a `node_modules` or a dot-folder; a folder that gains a `.git` / `pyvenv.cfg` marker or becomes a dense
tree (the rescope removes its children's records); a rename into an OS or lock artefact. So every check carries why
the record went:

| `why` | Deletes | What the round end does |
|---|---|---|
| `gone` | a tombstone in `_apply` (`change.deleted`); a cascade under a deleted folder (`_cascade_step`, `:1198`); the epoch sweep (`_sweep`, `:997`; its projection gains `owner_note_id`); an orphan removal (a parent that is definitely not found) | **confirm with one `get_change`** |
| `unstored` | `plan_observation`'s `not cls.stored` branch; a rescope cascade (a folder that became opaque, marker or dense) | **never archive**: the note stays `active`, its snapshot is refreshed from the change the planner had, and it is now in the receipt's `not_indexed` state; the audit (§7.4) watches it from then on |

The planner reports the reason with the delete (`ObservationPlan` gains `delete_why`; the cascade step knows whether it
runs for a deleted folder or a rescope). Each check is `{note_id, item_id, why, last_seen_at = state.observed_as_of}`,
collected in the tick's accumulator (§3.4). **A purge is not a planner decision** (`_purge`, `:3312`, deletes by user):
opt-out, access loss, a different drive and the rebuild add no checks and archive nothing.

At round end (§5.1 c), for each `gone` check:
1. Read the record by item id. Present again (a recycle-bin restore arriving in the same round after the tombstone,
   §7.2 of the index RFC; a move back in) → dropped.
2. Absent → `get_change(user_id, item_id)`, within `ITEM_CALLS_PER_TICK` (20; checks beyond the budget wait for the
   next round end):
   - `DriveItemNotFoundError` → the note is archived in a transaction (`state = archived`, `archived_at`,
     `last_seen_at`; the snapshot path refreshed from the parent chain when `parent_item_id`'s record still exists,
     else kept). A tombstone, a sweep or an orphan never comes from an item the bot cannot open (§26.2: delta keeps
     listing those, and the planner marks them, never deletes them), so a 404 here is a real absence;
   - found → the note stays `active` and its snapshot is refreshed (the file exists outside the index's view);
   - any other error → the check stays for the next round end (per-item error class, §8.2 of the index RFC).

The decision is never taken inside `_apply`, so a delete and a restore in one round never archive.

`note_checks` past `NOTE_CHECKS_MAX` (500): the round end processes the first 500 and leaves the rest for the next round
end; the audit (§7.4) catches anything lost with a purge or a failed save.

### 7.2 Re-activation

An `archived` note whose `i:` key reappears (entry (a): the same item id on the same drive — a recycle-bin restore, a
move back into the App Folder) becomes `active` again, `bound_by = restore`, and is merged in the same write. Same item
id = same file; no content check.

### 7.3 Re-binding by content key (D8)

Only an `active` note bound to **another** drive than the connected one can re-bind; never a note whose drive is
connected, so a copy on the same drive never inherits it.
- Entry (a) records each created record whose `c:` key meets such a note in `note_rebind_candidates`.
- At the end of the **complete** round: a note with exactly one candidate item, and no other note already bound to
  that item, is re-bound (`binding` → the current drive and item, `bound_by = rebind_content_key`, `match_keys`
  rewritten, `updated_at` bumped), so the next notes step merges it. Several candidates (copies on the new drive) or a
  target that already has a note: no re-binding; the note stays dormant and is counted in the tick's log
  (`notes_rebind_ambiguous`).
- The key family must match. A provider that does not expose `quickXorHash` gives no `c:` match, so **v1 defers D8's
  re-binding after a provider switch** until a second provider is real (OD5); such notes stay dormant and visible to
  `drive_deleted`.
- **Folders never re-bind**: a folder has no content hash, so its note has no `c:` key. A folder note on a switched
  drive stays dormant; the owner notes the folder again.

### 7.4 The daily audit

A note bound to an item with no record (never indexed: inside an opaque folder, a capped area; or dropped as
`unstored`, §7.1) is never archived by the round end. Once a day (`notes_audited_at`), the tick reads the user's
`active` notes (index 4), keeps those on the connected drive whose item has no record (one batched record read), and
reads each with `get_change`, within `ITEM_CALLS_PER_TICK` (20): a not-found archives the note; found refreshes its
snapshot; another error leaves it for the next audit. A note audited within the last 7 days is skipped
(`snapshot.snapshot_at`). Named residual: an item inside an opaque folder that the bot also cannot open (§26.2) reads
as not found and is archived; if it ever gets a record again, §7.2 re-activates it.

### 7.5 Snapshot refresh

When entry (a) or (b) merges a note whose record's name, parent or content hash differs from the snapshot, the note's
snapshot is refreshed in the round-end step (path from the parent chain, read through the tick's record cache). A folder
rename that touches no noted record leaves descendants' snapshot paths stale: the path is "last known", refreshed
whenever the noted item itself is observed (named residual).

---

## 8. Lifecycle table

| Event | The note | The card and search |
|---|---|---|
| Rename, move (inside the App Folder) | Same item id: stays bound. Snapshot refreshed (§7.5). | Card re-embeds only if the name changed (as today); note kept. |
| Copy | Not inherited (new item id, D8). The original keeps it. | The copy has no note; identical copies collapse onto the noted one (§6.1). |
| Content edit (new `cTag`) | Unchanged: it is about the file, not one version. Snapshot content key refreshed. | The card re-renders with the new profile and the same note. |
| Delete (by the bot or in OneDrive), move out of the App Folder | Archived at the round end after a `get_change` confirms the absence (§7.1). | Record and card gone: invisible to the default search; found by `drive_deleted`. |
| Moved into a repository, `node_modules` or dot-folder; its folder gains `.git` / `pyvenv.cfg` or becomes a dense tree; renamed into an OS or lock artefact | **Stays `active`** (`why = unstored`, never archived, §7.1); snapshot refreshed; the audit watches it. | No record: not searchable by the note until the item is indexed on its own again, when entry (a) merges it. |
| Folder delete (cascade) | Each noted descendant and the folder's own note archived, each confirmed by id. | As above. |
| Recycle-bin restore, move back in | Re-activated (§7.2); in the same round, never archived at all. | Merged into the new record at once. |
| Index rebuild (`drive_index_rebuild.py`) | Untouched (purge ≠ planner delete). | Onboarding re-creates records; entry (a) merges every note. |
| Indexing opt-out | Kept, dormant (no tick runs). Tool writes still allowed (status `indexing_off`). | Search is off; `drive_deleted` still works. |
| Opt back in | Untouched. | Onboarding merges (entry a). |
| Drive disconnected (access lost) | Kept. New notes and edits refused until reconnected; **delete, undo and erase still work** (no Graph call, §4.4). | The index keeps cards (with notes) for the 30-day grace; after the purge, nothing until reconnect. |
| Reconnect, same drive | Bindings still valid (same item ids). | Records re-created after a purge get their notes (entry a). |
| A different drive connected | Old-drive notes dormant; files re-bound by unique exact content key (§7.3); folder notes never re-bind. A dormant note can be deleted or undone by its `drive_note:` ref. | Re-bound notes merge in the next tick; dormant ones appear only in `drive_deleted`. |
| Provider switch | Document ids unchanged; re-binding deferred (OD5): dormant. | As above. |
| Item the bot cannot open (§26.2) | Kept and merged (the record and card stay). New notes on it refused (OD12). | Card shows the note and the access line. |
| Card capped / released | Kept; merged into the record, shown when the card returns. | No card while capped. |
| Inside an opaque folder | Stored; no record to merge into. Receipt says `not_indexed`. Audit archives it if the item goes. | Not searchable by the note. |
| Owner deletes the note (empty triple) | `cleared`; every derived field emptied; history kept until erased; undo restores. Works in every state, connected or not. | Card re-embeds without the note. |
| Undo | New `restore` version. | Card re-embeds. |
| `KEYS_VERSION` / `CARD_VERSION` bump, embedding switch | Untouched (note vectors re-embedded on a switch). | Noted records re-keyed / re-embedded with the rest. |
| Erase one note or all (OD3, behind a confirmation code, §4.8) | Text, classifiers, keys, vector, snapshot and versions deleted at once; contentless stubs until the next tick. | The next tick clears the record copies, then deletes the stubs. |
| Account deletion | No account-deletion flow exists in the repo today. When one is built it must delete `{prefix}drive_notes` (stated in the repository's docstring and in CLAUDE.md). | — |

---

## 9. Privacy and security

- **Never on the drive** (D1). No Graph write anywhere in this RFC; `UserDrivePort` gains no write method.
- **Never in memory.** No consolidator, biographical cache, `SearchEnrichmentService` or MCP `get_user_context` reads
  the collection (§1.4). The skill tells Smart not to propose memory facts for a note (§4.1).
- **Where the text does go.** (1) Gemini embedding, for the note vector and the card vector — the same processor that
  already embeds the files' own text. (2) Smart's prompts and tool calls, so the note appears in BigQuery
  `prompt_content` (30-day TTL) and Logfire with content capture, like all chat content. (3) The chat, in the receipt.
  Accepted, the same as any drive card.
- **Logs** carry ids, counts, versions and statuses, never the text. A future search-metrics event
  (`DRIVE_SEARCH_METRICS_RFC.md`) must store `noted: true`, never the note.
- **Sensitive notes** stay out of other surfaces: no Cabinet listing in v1, no export, no default search of archived
  notes (D9).
- **Tenancy.** `user_id` and `account_id` on every document; every query filters `user_id ==`.
- **Injection.** Write-time guards §4.5; read-time wrapper §6.3. A compromised Smart on an interactive turn can still
  write a note: the receipt and undo are the recovery, and §4.5 layer 4 makes it need the owner's own words.

---

## 10. Failure modes

| Failure | Behaviour |
|---|---|
| Drive not connected / Graph error resolving the item | `set` refused, nothing stored; the reason in the tool result. Delete, undo and erase are unaffected (no Graph call). |
| Grounding refuses a classifier the owner did not name | Refused with "the owner did not name this type"; Smart asks or leaves the type out. |
| The `get_change` confirming an absence fails | The check stays for the next round end; nothing is archived on an error. |
| The skills store fails (`skills == []`) | The skill gate fails closed: notes unavailable for that turn (§4.2). |
| Security check blocks or flags | Refused, logged at WARNING with the patterns, not the text. |
| Grounding below the bar | Refused with the words the owner did not say; Smart rewrites or asks. |
| Concurrent write (precondition failed) | Retried once from the lookup, then refused ("try again"). |
| Embedding fails at write | Stored with `embedding_pending`; the indexer re-embeds; the deleted-files search loses only its meaning fallback for it. |
| Nudge fails | Logged; the next fan-out (≤ 10 min) merges. |
| The turn fails after the write | The note stands (it was committed and shielded); the receipt is lost with the answer. The next search card shows it; the history context is lost too, so undo needs the ref again. Named residual. |
| Indexer down or lease busy | The note waits in the store; the watermark catches it up; nothing is lost. |
| A note-store write from the tick fails | Logged at ERROR; the check stays in `note_checks` (or the candidate list) for the next round end. Never ends the tick: note lifecycle is not systemic. |
| A purge mid-round | `note_checks` die with the state: nothing archived by mistake; the audit catches a real deletion later. |
| Ambiguous re-binding | No re-binding; counted; the note stays dormant and appears in `drive_deleted` as "on a drive that is not connected". |
| A Firestore index missing | The lookup or the deleted-files query raises; the tick logs and skips the note step (cards keep their last merged note); the tool refuses new lineages only if `find_bound` fails. The owner steps in §11 create the indexes before the deploy. |
| Kill switch off | No writes anywhere (§3.6); reads keep working; merged copies stay until the next merge. |
| An erase code expires or is pasted twice | "No such erase request"; nothing erased (atomic `get_and_delete`). |

---

## 11. Slices and tests

One branch, one merge when all slices are done (owner rule: no phase-by-phase merges). Slices are review units; each
ends green on `make check`.

### S1 — the store, the tool, the skill, the receipt

Files: `src/domain/drive_note.py` (models, normalization, classifier mapping, `NoteState`, constants, delivery type),
`src/domain/drive_note_grounding.py`, `src/ports/drive_note_repository.py`, `src/adapters/firestore_drive_note_repo.py`,
`src/adapters/in_memory_drive_note_repo.py`, `src/services/drive_note_service.py`, `src/infrastructure/drive_note_tools.py`,
`src/skills/smart/drive-file-notes/SKILL.md`, `EnvironmentConfig.drive_notes_collection`, Smart wiring
(`smart_response_agent.py`), `ConversationHandler._deliver_drive_note_receipt`, locales (4), composition (main service),
`config/firestore.indexes.json` (indexes 1–5), `src/utils/capabilities.py` (user-facing capability line),
`strip_injected_timestamp` moved to `src/domain/` (used by `_inject_timestamps` and the owner corpus), the shared
`LoadedSkills` set (`skill_tools.py`).

Tests (new files):
- `tests/unit/domain/test_drive_note.py` — normalization (NFC, whitespace, control characters); word and character caps
  in en/es/uk/ru, refused never cut; classifier mapping from keys and from `DOC_TYPE_NAMES` / `LIFE_DOMAIN_NAMES` in four
  languages; unknown refused; at most 3 areas; `match_keys` format (`i:`, `r:`, `c:`; no `c:` for a folder); the
  triple rule (classifier-only note valid; empty triple = delete); undo semantics (first note → clear; undo of undo →
  redo; undo of archived keeps `archived`; undo of cleared brings it back); SCD2 flags on a version chain, content
  fields of a closed version unchanged.
- `tests/unit/domain/test_drive_note_grounding.py` — owner corpus share; the corpus excludes the
  `USER_TURN_SYSTEM_ANCHOR` text and strips the injected timestamp, so a timestamped `[System: …]` part is still
  skipped and "2026" / the month of a timestamp do not ground a note (I3); cross-script by skeleton; numbers by number
  terms; empty text not graded; **classifier grounding**: "это договор" passes `contract`, a type the owner never named
  is refused, a synonym missing from the tables is refused (I10); cross-language ("в марте 2024" vs "March 2024")
  pinned as refused; the refusal names the ungrounded words; threshold edge.
- `tests/unit/services/test_drive_note_service.py` — set / clear / undo end to end on in-memory ports; one lineage per
  item, a cleared note reused; **two concurrent first writes give one lineage** (deterministic id, must-not-exist
  precondition) and a taken deterministic id falls back to a random one; precondition conflict retried once; **an
  identical write is a no-op with no new version**; classifier-only write and its receipt; security reject; grounding
  reject; `set` refused when not connected; **clear and undo work while disconnected and on dormant and archived notes,
  by `drive:` (ref key) and by `drive_note:` refs, with no Graph call**; the 404 order (inaccessible from the record
  first, then not found); **clear empties `text`, classifiers, `keys` and `note_vector`**; embedding failure → pending;
  nudge failure never fails the write; searchability statuses; `NOTES_PER_USER_MAX`; logs carry no text.
- `tests/unit/adapters/test_firestore_drive_note_repo.py` — document and version mapping, server timestamp on
  `updated_at`, the transaction's precondition, each query's filters (fake client, as the other Firestore repos).
- `tests/unit/infrastructure/test_drive_note_tools.py` — declaration: only `file_ref` required, no enum / pattern /
  `maxItems`; every parameter has a description; handler: argument parsing, failures as `failed=True` with the reason;
  the skill gate with the shared `LoadedSkills` set (loaded in this execution, or visible in history); `skills == []`
  fails closed; `NOTE_WRITES_PER_TURN`; the per-execution lock orders a batch; shield; delivery item and
  `drive_note_context`; `erase` returns only a code delivery.
- `tests/unit/agents/test_smart_drive_note_tool_gating.py` — declared only with the service, the switch, a user and
  `interactive_delivery`, and outside `if skills:`; absent on `notify`, `ask_alek`, `tell_alek` and `/worker`
  contexts; the corpus handed to the handler is built before the anchor is injected (I3).
- `tests/unit/test_arch_drive_notes_isolation.py` (next to `test_req_arch_01_hexagonal_isolation.py`) — `ConsolidationService`, `SearchEnrichmentService`, the MCP
  composition (`composition/mcp_*.py`) and the biographical-cache code never import or receive `DriveNoteRepository`
  or `DriveNoteService` (the §1.4 anti-goal).
- `tests/unit/handlers/test_conversation_handler_drive_note_receipt.py` — the stored text byte for byte, localized
  headers in four languages, the searchability line, a failed post logged.
- A system-skill bundle test: `drive-file-notes` loads, and its description names the tool.

### S2 — the merge into cards and search

Files: `drive_index.py` (record fields), `drive_note_merge.py` (`NoteView`, `with_owner_note`, `effective_doc_type`,
`NOTE_MERGE_VERSION`), `drive_card.py` (segment), `drive_keys.py` (`note_keys`, `_about_items`, `note_keys_digest`),
`drive_key_sources.py` (`AboutField.OWNER_NOTE`, `KeyedRecord`, `RecordKeyInputs`), `drive_match.py` (comment only),
`drive_index_planner.py` (`_build` carries the fields), `drive_enrich_plan.py` (nothing beyond `planned()` reading
the record), `drive_indexing_service.py` (entries a and b, the count gate, vector repair), `firestore_drive_index_repo.py`
and the in-memory repository (mapping), `drive_tier_explain.py`, `drive_tier_render.py` (`card_body`),
`drive_search_service.py` (`_collapse`), `FileManagementAgent` (open header), the `drive-search` skill, composition
(indexer and main).

Tests:
- `tests/unit/domain/test_drive_card_owner_note.py` — **no note → byte-identical `card_text` and `card_hash`** on a
  fixture of every branch (the G5 invariant); with a note: the segment right after `name | kind` in each branch, the
  plain folder, the classifier-only segment (`noted by the owner: type contract`, no empty quote), the profile's
  doc type segment dropped when the owner gave one (never twice), `card_text(rec, owner=False)`.
- `tests/unit/domain/test_drive_keys_owner_note.py` — note keys under `e:`/`ex:`/`exl:`/`v:`/`y:`/`m:` with
  `AboutField.OWNER_NOTE`; emitted first, in the order classifiers → numbers and dates → words; **a maximal Cyrillic
  note plus the worst doc type and three areas keeps every classifier key** and the cap cuts only trailing words
  (I5); classifier names without skeletons; the worst type + three areas < 100 keys (table test); **no fragment
  `v:` key: a note with "1 250,00" is not About for the query "250"** but is for "1250" and "1 250,00"; a date keys
  `v:` ISO + `y:` + `m:`; "March 2024" keys `m:2024-03`; a standalone year keys `y:` (I6); a full profile plus a full
  note never cuts the note's keys; `l:type` / `l:domain` overrides, **`l:type` also on a record with no profile**;
  D31 `lw:`/`dw:` names dropped only for an overridden field; source classes;
  the query "contract" / "договор" covers through the owner's type; `note_keys_digest` pinned per `NOTE_MERGE_VERSION`.
  **The existing `index_tables_digest`, `golden_digest` and capture pins must pass unchanged** — they are the guard
  that no version bump is needed; a failure there means the design is wrong, not the test.
- `tests/unit/domain/test_drive_note_merge.py` — `with_owner_note` set, change and clear are idempotent; `_build`
  carries the fields across an observation; `planned()` recomputes `about_keys`; card work due in class 0.
- `tests/unit/services/test_drive_indexing_notes.py` — entry (a) on onboarding, restore and re-listing, **and on a
  parent folder created by `_resolve_step`** (I2); `note_lookup_due` cleared found or not; entry (b) merges and
  clears; the watermark and its skew; **paging: a `None` watermark re-reads in pages of `NOTE_PAGE`, advances to the
  last processed note and continues** (Minor 7); **a `NOTE_MERGE_VERSION` bump re-merges every noted record and only
  those** (I1), including after a rollback and roll-forward; the count gate (zero → one read, no lookup); the vector
  repair and the space-switch pass; the lease clock stops note writes; a nudge after a write merges in one tick; a
  purge leaves the store untouched; a `KEYS_VERSION` re-key keeps the note keys; the sync-state readers round-trip
  the new fields.
- `tests/unit/domain/test_drive_tier_explain_owner_note.py` — `matched: your note "…"`; a note-only match is About.
- `tests/unit/services/test_drive_search_owner_note.py` — the note line inside the wrapper, a tag look-alike in a note
  neutralised; the 700-character summary cut never spends on the note; **every `make_card` call site renders the note
  line** (tier, folder-scope, filter-only, list); the Related card's 120-character note line; accuracy line unchanged;
  the noted copy is the collapse representative, decided on the lean `owner_note_id` (I7).
- `tests/unit/agents/test_file_management_open_note_header.py`.

### S3 — archive, re-binding, deleted-files search, erase

Files: `drive_indexing_service.py` (delete hooks, round-end checks, re-binding, audit), `DriveSyncState` fields,
`drive_note_render.py`, `DriveSearchService.search_deleted`, `agent_manifest.py` (`drive_deleted`), the vector index
script, the erase-request `FirestoreEphemeralStore` and `ConversationHandler` (`$drive-notes erase <code>`,
`$drive-notes erase-all [<code>]`, OD3), `ObservationPlan.delete_why`, locales.

Tests:
- `tests/unit/services/test_drive_note_lifecycle.py` — tombstone → archived at the round end only after `get_change`
  answers not found; found → stays active; an error → retried at the next round end; **a noted file moved into a
  repository folder, a folder that gains `.git`, a folder that becomes dense, and a rename into an OS artefact each
  stay `active`** (C1); delete and restore in one round → never archived; cascade under a deleted folder vs a rescope
  cascade; sweep hooks; checks folded into the state at each save (error paths included); **a purge (opt-out, access loss, different drive, rebuild) never
  archives**; re-activation on the same item id; re-binding on a unique exact content key on another drive; several
  candidates → none; a target that already has a note → none; **a copy on the connected drive never inherits**; a
  folder note never re-binds; `NOTE_CHECKS_MAX` carry-over.
- `tests/unit/services/test_drive_note_search_deleted.py` — **the default search never returns an archived note**
  (an invariant over every row of §8, in-memory repositories); `drive_deleted` strict AND over note and name keys; the
  meaning fallback labelled and prefiltered on the current space; dormant notes labelled and keys-only; disconnected →
  archived only, with the line; `drive_deleted` parsed from `"true"` / `"yes"` / `"1"` strings, anything else false;
  the `drive_note:` ref rendered; the scan cap line; an empty query lists the newest.
- `tests/unit/infrastructure/test_agent_manifest_drive_deleted.py` — the field is in `context_schemas` and the
  capability description, its name is unique across intents, its description says "explicitly".
- `tests/unit/services/test_drive_note_audit.py` — a not-found archives; the item-call budget; the 7-day skip.
- `tests/unit/handlers/test_conversation_handler_drive_notes_command.py` — `$drive-notes erase-all` answers with the
  count and a code and erases nothing; pasting the code erases; a per-note code from the tool erases only that note;
  a wrong, expired or reused code erases nothing; own notes only; handled before any LLM call; **the erase stub holds
  only tenant fields, `state`, `erase_pending`, `match_keys`, `updated_at`** (I9); the next tick clears the record copy
  and deletes the stub; with indexing off the stubs go at once.

### Owner steps and rollout

1. Create indexes 1–5 (`gcloud firestore indexes composite create … config/firestore.indexes.json`), the vector
   index (the script) and the TTL policy on `expires_at` of `{prefix}drive_note_erase_requests` before the deploy;
   wait for `READY`.
2. Deploy the main service and the indexer (the indexer after the main service, as today).
3. Live check: note one file in chat, see the receipt, search for a note word, delete the file in OneDrive, search
   again (absent), search deleted files (present), restore from the recycle bin, search again (present).
4. Rollback: the kill switch; merged copies on records are harmless to old code (plain fields, `merge=True` writes;
   old `card_text` ignores them, so noted cards re-embed once on the old code). On the roll-forward the
   `notes_merge_version` mismatch re-merges every note (§5.5).

Docs at build time: README `docs/05_building_blocks/user_drive/README.md` (a new §4.24 and the key table), CLAUDE.md
(the User Drive paragraph, one line), `decisions/drive_file_notes.md`, `docs/07_deployment/README.md` (the indexes).

---

## 12. Open decisions for the owner (recommended default in bold)

| # | Question | Default | Why |
|---|---|---|---|
| OD1 | Grounding gate: must most of the note be the owner's own words, and each classifier one he named (§4.5 layer 4)? | **On: 0.6 of content words, last 6 owner turns; every classifier by one of its names.** The flow "bot proposes a description, owner says yes" is refused (example below). | The only structural defence against a note or a type composed from a file or email. Alternative: off, relying on the skill and the receipt; or on for text only. |
| OD2 | Skill gate: the tool refuses until `drive-file-notes` is loaded (§4.5 layer 2)? | **On.** Fails closed when the skills store fails. | The "owner's words only" rules are then always in context; costs at most one round trip per conversation. |
| OD3 | How is note history erased? A delete keeps history (undo needs it). | **Both a per-note erase and erase-all, each behind a confirmation code** (§4.8): the tool's `erase` or the owner's `$drive-notes erase-all` gives `$drive-notes erase <code>` / `$drive-notes erase-all <code>`; pasting it erases text, keys, vector, snapshot and every version. The delete receipt says "history kept; undo possible". | D1's sensitivity needs a per-note way out; an irreversible action never runs on one model call or one paste (the `$memory save <code>` pattern). Alternative: erase-all only, or erase on delete with no undo. |
| OD4 | Should the owner's chat sentence that describes a file be kept out of memory (a `Trivial_Exclusions` line)? | **No change.** | It is his own message; excluding it would also drop real life facts he states while annotating. |
| OD5 | **A deferral of D8's provider-switch re-binding.** D8 asks for re-binding by content key after a provider switch; no other provider exposes `quickXorHash`, so v1's content key never matches there. | **Defer it: v1 stores only the provider's own content key, no extra hashing.** After a switch, notes stay dormant (visible to `drive_deleted`, deletable by their `drive_note:` ref). The schema keeps a key family, so a `sha256:size` key can be added when a second provider is real, without migration. | YAGNI until a second provider is real. Alternative: the indexer stores a `sha256` whenever it downloads a file (content kinds only; images are triaged by thumbnail and would still not match). |
| OD6 | Does `drive_deleted` also show dormant notes (bound to a drive that is not connected)? | **Yes, labelled, keys only** (no meaning fallback); not while disconnected (no drive to compare with). | Otherwise a note from a switched drive is unreachable. |
| OD7 | A deleted file uploaded again (new item id, same content, same drive): re-bind? | **No.** Exact rule: only another drive re-binds by content. The archived note is found by `drive_deleted`; the owner can note the new file. | Same content on the same drive is exactly a copy (D8). |
| OD8 | Writing while indexing is off, or while disconnected | **Indexing off: allowed, stored, receipt says so. Disconnected: new notes and edits refused; delete, undo and erase allowed** (they need no Graph call). | An item can be verified only with a connection; a delete must work in every state (D3). |
| OD9 | Voice memos in chat count as chat turns | **Yes.** | The transcript is the owner's own message; Lelik stays excluded (D5). |
| OD10 | A Cabinet page listing notes | **Later.** | Sensitive by D1's reasoning; chat and search cover v1. |
| OD11 | The note in `list_files_in_drive` lines | **Not in v1** (search cards and the open header only). | Lists are long; the open header covers "what did I note about this". |
| OD12 | Notes on files the bot cannot open (§26.2) | **Refused** with the copy instruction. | The item cannot be verified by id. Alternative: accept from the index record's snapshot. |

**OD1, worked example** (synthetic names). The owner asks what a file is; Smart opens it and answers.

1. Smart: "It is an estimate from Ivanenko Bud for kitchen works, March 2024, 4 800 EUR. Shall I note that?"
2. Owner: "yes".
3. Smart calls `annotate_drive_file(note="Estimate from Ivanenko Bud for kitchen works, March 2024", doc_type="plan")`.
   **Refused**: "estimate", "ivanenko", "bud", "kitchen", "works", "march", "2024" are not in the owner's words (0 of 7),
   and the owner named no type. The refusal tells Smart to quote its wording and ask the owner to say what the file is.
4. Smart: "I can only note your own words. What is it for you?"
5. Owner: "да, это смета Пети на ремонт кухни".
6. Smart calls `annotate_drive_file(note="смета Пети на ремонт кухни")`. **Passes**: "смета", "пети", "ремонт",
   "кухни" are all in his message (4 of 4). No type: the owner named none ("смета" is not a `DOC_TYPES` name).

The friction is step 4: one extra turn in the most natural flow after "what is this file?". The gain is that nothing in
step 1, which came from the file, reaches a card with owner authority. With the gate off, step 3 would store the
file's own words as the owner's.

---

## 13. Deltas from the research reports

| Research said | This RFC | Why |
|---|---|---|
| Mirror the note into the OneDrive `description` behind a `UserDrivePort` capability (Option A step 2; spike §A.1). | **Dropped.** No spike needed. | D1. |
| Critique: a hybrid — the store as the record, the drive as an editable projection through the T2 mirror machinery, edits read back. | **Rejected.** No projection, no read-back, no notes root, no Step 1 guard for it. | D1. The critique's other findings (purge root identity, Step 1 writes into mirror roots) stay relevant to T2 itself, not to notes. |
| A FILE_MANAGEMENT intent `note_file_in_drive` (zero-LLM, under the agent's lock), with the open question whether `interactive_delivery` reaches a specialist. | **A Smart local tool `annotate_drive_file` + a system skill** (N2). The interactive gate is Smart's own, as for `propose_memory_facts`. | D4. Without a provider write there is no reason to sit under FileManagement's lock. |
| `drive_note_mode`: `replace` / `add` / `clear` / `undo`. | **Replace-only, empty = delete, `undo`.** | D4. |
| Collection `{prefix}drive_file_notes`, a `history/{n}` subcollection of previous texts. | **`{prefix}drive_notes`, SCD2 `versions/v{n}`** (`valid_from` / `valid_to` / `is_current`). | D2. |
| Cap 500 characters. | **80 words and 600 characters** (N1). | D4 asked for 80–100 words; see N1. |
| Free text with optional classifiers; empty note = clear. | **A note is the triple**; a classifier-only note is valid; an empty triple deletes. | The headline correction "it's a contract" has no free text (review I4). |
| Note words keyed first; classifier names after. | **Classifier names first and never cut**, then numbers and dates (the profile's `_value_forms` / `_date_items` recipe), then words. | Measured key counts (review I5, I6). |
| Archive on the orphan TTL after any record delete. | **Archive only a `gone` delete confirmed by `get_change`**; an `unstored` delete never archives. | A record leaves the index for files that still exist (review C1). |
| A deleted file's note is orphaned and deleted after 30 days (the transcript orphan TTL). | **Archived, kept until an explicit erase**; default search never returns it; `drive_deleted` does. | D3, D9. |
| Re-binding on a provider switch: content hash, else name + size + path, else ask. | **Exact content key only**; otherwise dormant. | D8. |
| Tick learns about notes through a `notes_rev` counter and `applied_notes_rev` (option b), loading every note after a purge. | **An `updated_at` watermark** (server timestamps) **plus a lookup by binding key when a record is created** (N3). | No second document to keep in step; every way a record appears is covered by one hook. |
| Note words as `e:` phrases. | Same, **plus the note's numbers and dates as `v:` / `y:` / `m:`** and the owner's classifier names as `e:` (the critique's C.vi a). | D6: dates and numbers "via words" need the number families the query emits; all are existing families, so still no bump. |
| The note as a card segment "directly after kind". | Same in `card_text` (hash and embedding); **rendered as its own line** in `card_body`. | The 700-character render cut would otherwise spend the file's own description on the note. |
| Critique D.2: deleting a file through the bot offers "also its note?". | **Not adopted.** A deleted file's note is archived; the owner deletes a note with an empty note. | D3, D9: archive is the owner's intended default. |
| Notes on files without a card: stored, receipt says not searchable. | Same, plus **a daily audit** archives such notes when the item goes (§7.4). | Otherwise they would stay `active` forever after the item is deleted. |
| Grounding of the note in the owner's words was a prompt rule. | **A code check over text and classifiers** (OD1), plus a skill gate (OD2) and a per-turn write cap. | The steering risk the research names needs a structural layer; a steered type would otherwise get owner authority (review I10). |
| Deleting a note while the drive is disconnected or bound elsewhere: not addressed. | **Delete, undo and erase need no Graph call**; a provider-free ref key and a `drive_note:` ref. | D3 in every state (review I8). |

---

## 14. Engineering decisions (N1–N9)

| # | Decision | Reason |
|---|---|---|
| N1 | **Cap: 80 words and 600 characters.** | One to three sentences say what a file is. Each extra word dilutes the card vector, where the note sits beside the file's own description, and costs keys under the 600-key record cap. Measured: 183–197 word keys for a 65-content-word Russian or Ukrainian note, ≈ 240 for a noun-dense 80-word one, plus up to ≈ 85 classifier names; `NOTE_KEYS_MAX` 320 holds nearly all of it and leaves ≥ 280 keys for the file's own. 100 words would push a Cyrillic note to ≈ 300 word keys alone. 600 characters keep the note line short enough to show beside a 700-character card. The lower end of D4's range, because overflow is refused, not cut. |
| N2 | Tool name `annotate_drive_file`; skill `drive-file-notes`. | "Note" is NotesAgent's word (self-reminders); a `note_…` tool would draw reminder requests. The collection keeps the owner's name `drive_notes`. |
| N3 | Watermark on server timestamps, not a counter document. | §5.1. |
| N4 | One lineage per item; a cleared note is reused. | Undo and history stay continuous; the lookup by binding returns at most one note. |
| N5 | Binding and lifecycle changes update the parent document in place; only the owner's words are versioned. | SCD2 records what the owner said; where the file is now is a fact about the drive. |
| N6 | The deleted-files search reads the note store in memory (≤ 1 000) and never the index. | Archived notes have no records; a small set needs no key streams; default search stays untouched. |
| N7 | Skill gate in the handler. | OD2. |
| N8 | Grounding check in code. | OD1. |
| N9 | `NOTE_MERGE_VERSION` outside `CURRENT_VERSIONS` and the key digests. | It shapes only noted records; a bump re-merges a few hundred records, never the whole drive. |

---

## 15. Parameters (initial)

| Name | Value | Where |
|---|---|---|
| `NOTE_WORDS_MAX` / `NOTE_CHARS_MAX` | 80 / 600 | `drive_note.py` |
| `NOTE_KEYS_MAX` | 320 (classifiers ≈ ≤ 85 first, never cut) | `drive_note.py` |
| `NOTE_PAGE` | 200 | `drive_note_merge.py` |
| `ERASE_CODE_TTL` | 10 min | `drive_note.py` |
| `NOTE_WRITES_PER_TURN` | 10 | `drive_note.py` |
| `NOTES_PER_USER_MAX` | 5 000 | `drive_note.py` |
| `NOTE_GROUNDING_MIN` / `NOTE_GROUNDING_TURNS` | 0.6 / 6 | `drive_note_grounding.py` |
| `NOTE_WATERMARK_SKEW_S` | 60 | `drive_note_merge.py` |
| `NOTE_EMBED_PER_TICK` | 20 | `drive_note_merge.py` |
| `NOTE_CHECKS_MAX` | 500 | `drive_note_merge.py` |
| `ARCHIVE_SCAN_MAX` | 1 000 | `drive_note.py` |
| `NOTE_AUDIT_EVERY` / `NOTE_AUDIT_SKIP_DAYS` | 24 h / 7 days | `drive_note_merge.py` |
| `NOTE_MERGE_VERSION` / `NOTE_KEYS_VERSION` | 1 / 1 | `drive_note_merge.py` / `drive_note.py` |
| `DRIVE_NOTES_ENABLED` | on | `os.getenv`, main and indexer |
