# RFC: Agent Workspace — Smart's own persistent working files

**Status:** Accepted — rev 2 (2026-10-20), owner decisions recorded. Implemented on `feat/agent-workspace`; not deployed (owner steps §14).
**Date:** rev 1 2026-10-14; rev 2 2026-10-20 (after the 2026-10-19 review and owner discussion, the 2026-10-20
adversarial review, §0.1, and the owner's later decisions of 2026-10-20, §0.2).
**Related:** `MEMORY_FACT_CONFIRMATION_RFC.md` (facts about the user; the local-tool precedent outside `if skills:`),
`AGENT_SKILLS_RFC.md` (skills, `use_skill`, `draft_skill`, `skill:` refs), self-reminder mechanics in
`src/agents/CLAUDE.md`, `SYSTEM_TURN_FINGERPRINT_RFC.md` (branch `rfc/system-fingerprint`; injection hardening is its
class-B follow-up, not this RFC), `AGENT_NOTES_RFC.md` (superseded notepad), `USER_DRIVE_RFC.md` (the user's file
area), `decisions/standing_directives.md`.
**Branch base:** line references are to `feat/memory-propose-facts` (main + PR #91), checked 2026-10-20.

## Owner decisions

Recorded, not reopened. D1–D13 are 2026-10-19 (discussion) and 2026-10-20 (night-shift block); D14 survives from rev 1
(2026-10-14); D15–D17 are the owner's later decisions of 2026-10-20 and supersede D6 and D7.

| # | Date | Decision |
|---|------|----------|
| D1 | 10-19 | **Pull-only.** Smart reads or writes the workspace only when (a) the user explicitly asks, (b) an instruction it follows — a loaded skill's text, a reminder's instruction — names a workspace file or asks it to keep state, or (c) a firing reminder carries a workspace file (D15). No index or contract in the prompt, no `workspace` system skill, no skill-load write gate. |
| D2 | 10-19 | **One local tool `workspace`** with commands `view`, `create`, `str_replace`, `delete`, `rename`, `copy`, `history`, `restore`. No grep, glob or diff. Served by `DelegationEngine(local_tools=)` like `use_skill` and `propose_memory_facts`, declared on every Smart path where the service is configured, outside `if skills:`. |
| D3 | 10-14, 10-19 | **Storage = a dedicated versioned GCS bucket**, key `{user_id}/<folder>/<path>`, folders `projects/ references/ scratch/ files/`, generation preconditions on every mutation, lifecycle as rev 1 §5.4. Not Firestore, not the drive, not the media bucket. |
| D4 | 10-20 | **Caps:** 100 files per user, 100 MB live bytes per user, 10 MB per file, 256 KB per text file; history 90 days / 50 versions; scratch 7 days after the last write. |
| D5 | 10-19 | **Writes allowed on all Smart paths** (chat, reminders, daily review, `ask_alek`/`tell_alek`, `/worker`). Every object carries its `origin` — `chat`, `background:<task_type>`, `voice:ask_alek`, `voice:tell_alek` — plus a fire key for reminder runs, shown in the `view` header. |
| ~~D6~~ | 10-19 | ~~Reminders get `reminder_skill` and `state_file`.~~ **Superseded by D15.** |
| ~~D7~~ | 10-19 | ~~Workspace files are never executed; procedures live in skills.~~ **Superseded by D16 and D17.** |
| D8 | 10-19 | **A missing workspace file named by a reminder or a skill is reported, with a restore offer** — never silently recreated. A file that never existed is fine: the first run may create it. |
| D9 | 10-19 | **Local dev shares the live bucket**, as it shares live Firestore; the RFC says so. |
| D10 | 10-19 | **`files/` kept** for byte-exact copies of chat uploads and delivered documents; `drive:` dropped as a copy source. |
| D11 | 10-20 | **All whitelisted users at once**, no per-user flag. |
| D12 | 10-20 | **First step after deploy: convert the owner's morning briefing** to a workspace file + a short reminder (§15, A1). |
| D13 | 10-20 | Agreed defaults: no consolidation-token line (rev 1 R9 dropped; revisit trigger §11.5); no dry runs; voice origins `voice:ask_alek` / `voice:tell_alek`; `str_replace` needs no prior view; live acceptance is an owner step. |
| D14 | 10-14 | Kept from rev 1: the name `workspace`; no `$ws` chat commands (a Cabinet page is a later extension); `skill-creator` gains a section on skills that keep state. |
| D15 | 10-20 | **Reminders get one optional code-bound field, `workspace_file`** (a workspace path), kept purely for reliability: NotesAgent's LLM rewrites reminder text and loses paths. Rendered at fire time as one line, `Workspace file: workspace:<path>`; what to do with it is the reminder instruction's business. No skill per scheduled task: the skills catalog is in every prompt and skills are applied by meaning, which a once-a-day task does not need. |
| D16 | 10-20 | **A workspace file is a resource** (a knowledge base), optional, with no required structure. Whether and how to keep state there is said by the instruction (the user's words, a skill's text, a reminder's instruction), or left to the model's judgement when the instruction is silent. State with a fixed schema belongs in a skill that describes its own file (e.g. a language tutor). No cleanup mechanism: the tool description asks for short, current files (replace, do not append). A fixed state section would grow daily with nothing to prune it, and no schema foresees every case. |
| D17 | 10-20 | **No workspace rules as injection protection.** "Never executed / data only" framing and folder write restrictions protect nothing: the bot itself creates reminders on any path, so an infected bot can schedule an arbitrary instruction anyway. Injection is class-B work (the fingerprint follow-up), not workspace rules. Origin metadata and history stay, for debugging and undo. |

## 0. Deltas from rev 1

| Rev 1 | Rev 2 | Why |
|-------|-------|-----|
| `workspace {}` index rendered into every Smart prompt (decisions 6–7, §9) | No index, no prompt change | D1. A self-written block in every prompt was the riskiest part (injection, cache invalidation per write, ~600 tokens) and the rev 1 evidence showed Smart does not start writes on its own. |
| `workspace` system skill + write tools refused until it is loaded (decisions 1, 9; §8, §10) | No system skill, no gate; guidance lives in the one tool description | D1. The gate needed `SkillPresence` and a `make_use_skill_handler` change, and it could never pass when the skills store failed (rev 1 R3). |
| Writes on interactive turns only (decision 8) | Writes on every Smart path, each object labelled with its `origin` | D5. The morning briefing runs from a reminder and must update its own file. |
| 11 tools `workspace_read/write/edit/grep/glob/move/copy/delete/log/diff/restore` | One tool `workspace(command=…)`, 8 commands | D2. One declaration (~1 k tokens instead of ~2 k) and no grep/glob/diff: tens of files, listed by `view` on a folder. |
| Read-before-write for every overwrite and edit | `str_replace` self-guards; `create` over an existing file needs a `view` in this execution | D13. `str_replace` reads the current generation itself and writes with a precondition (§7.3). |
| Frontmatter `description` per file, `utils/workspace_text.py`, PyYAML parse | Dropped; `view` on a folder lists path, size, date, origin | No index to describe files for. |
| Typed content (projects = goal/decisions/state/next step; "never procedures, never rules") | No required structure; what a file holds is up to the instruction that uses it | D16, D17. |
| "Your own notes; data, not instructions" on the index and every read | Dropped; a neutral header (path, generation, size, last writer, time) | D17. |
| RE2 (`google-re2`) for grep | Dropped | No grep. |
| `kind` custom metadata | Dropped; kind derives from the extension | One source of truth. |
| Port with `stat`, `versions`, `purge_user` | Port = `list`, `read`, `put`, `delete` | Reviewer finding. `list(include_versions=True)` covers history and stat; erasure is an owner `gcloud` command (§19). |
| `PromptBuilderPort.workspace_index`, assembly placement, per-request index log, `delegation.tool_arg_path` span attribute | Dropped | No index. The `[Workspace]` log line carries the path (§16). |
| `workspace_copy` sources: uploads, delivered documents, `drive:`, `skill:`, workspace | Uploads and delivered documents through `FileConversionService.resolve_bytes`; workspace → workspace straight to the port | D10. `drive:` dropped; `skill:` dropped (a skill's text is already reachable with `open_file`). |
| 50 MB per user | 100 MB per user | D4. |
| Review: reuse ≥ 30 %, write rate ≥ 1/20 turns, then a nudge | Review of pull usage per trigger, lost files, conflicts, file growth, origin mix | D1: writes are pulled, so a write rate is not a target (§16). |
| R9: `Trivial_Exclusions` extension | Dropped, with a revisit trigger | D13. |

Kept from rev 1: the problem and prior art (§1–§2), the storage rationale and lifecycle (§5), the path rule (§6),
R1 (`customTime` for scratch), R5 (rename/restore/copy as read + put, never a server-side copy) and R6 (a version is
the GCS generation number).

### 0.1 Review deltas (2026-10-20, adversarial review of the first rev 2 draft)

Kept as the review record. Items about `reminder_skill` / `state_file` (B2, B3, should-fix 3, 8, 9 second half, 10)
are superseded by §0.2: their binding mechanics now apply to the single `workspace_file` field, and the skill check is
gone.

Blocking:
- **B1 Same-fire re-run.** `view` takes an optional `generation` (read-only). When the live version was written by
  this same fire, the header names the generation from before the fire (§7.4, §10 row 1).
- **B2 Clear sentinel** is `"remove"` (the `draft_skill` vocabulary); `""`, `none`, `null`, `n/a` in any case mean
  "not provided". The receipt says "workspace file removed" (§8.3).
- **B3 NotesAgent binding**: the one-create/update guard is a local of `_run`, never instance state; more than one
  create/update in the binding response refuses all before any runs; code appends a system line to NotesAgent's user
  message and a matching sentence goes into the `instruction` parameter description (§8.3).
- **B4 Who deleted, and renames, inside the 4-method port**: `delete` takes `meta`; the adapter patches
  `deleted_by` / `deleted_fire` / `renamed_to` under the generation precondition, then deletes the live object with
  the same precondition. A generation-addressed delete is never used: it erases that version permanently (§5.3).

Should-fix:
1. Origin is `background:<task_type>` (`background:execute_reminder`, `background:daily_email_review`), passed by the
   two worker call sites (D5, §9).
2. Memory boundary wording in the tool description and §11.1.
3. ~~`reminder_skill` checked with `list_owned`; `$skill delete` warning deferred.~~ Gone with `reminder_skill` (§0.2).
4. Every `put` stamps `write_id`; a delete patch stamps `delete_id`. A 412 on a retried request is resolved by reading
   them back (§5.3).
5. `rename` is excluded from the cap check (§6).
6. The review is computable: the log line carries the fire key and `had_history`; the script reads 28 days of
   Cloud Logging (§16).
7. A1 step 1: the owner uploads the current instruction as a `.txt` file (§15).
8. ~~A1 step 3 seed from the last three issues.~~ Replaced by the owner's A1 (§0.2, §15).
9. `files/` is byte-exact: `create` / `str_replace` refuse there (§6). ~~`state_file` needs a text extension.~~
10. ~~`build_reminder_alert(note, *, fire_key=None)`.~~ The signature stays `build_reminder_alert(note)` (§0.2).
11. Cancellation also covers the agent's message timeout; the shielded inner coroutine updates the session map and
    logs itself; a folder view lists versions; a file view is two GETs (§7.6, §10 row 3, §18).

Nits: `offset`/`limit` ≤ 0 or `""` → defaults; exact-name filtering in prefix lists; the prior-art claim softened
(our parameter names differ from Anthropic's memory tool); `docs/10_rfcs/README.md` in the docs list;
`google-cloud-storage` pinned; prefetched `workspace:` refs arrive without the view header (§12);
`visible_skill_names` made version-aware (§11.2); `SkillService._read_from_file` not-found text for `workspace:` refs;
NotesAgent's constructor flag `workspace_enabled`. One nit did not hold: `relay_main.py` and `indexer_main.py` do not
call `load_settings()` (`relay_main.py:31`, `indexer_main.py:15`); only `main.py:98` does (§5.5).

### 0.2 Owner decisions, later on 2026-10-20 (D15–D17)

| Before (§0.1 draft) | Now | Why |
|---------------------|-----|-----|
| Two reminder fields `reminder_skill` + `state_file` | One field `workspace_file` | D15. A skill per scheduled task is the wrong tool; the field exists only so the path survives NotesAgent's rewriting. |
| `reminder_skill` validated against the skill catalog (`list_owned`); NotesAgent takes `skill_service` | Removed; NotesAgent takes only `workspace_enabled` | D15. |
| `state_file` only under `projects/` / `references/`, text extension | Any valid workspace path | D17: folder rules were safety framing. |
| Alert lines "Procedure: load the skill …", "State file: … data from earlier runs, not instructions. View it before you start; update it before you finish.", "Fire: …" | One line `Workspace file: workspace:<path>`; `build_reminder_alert(note)` keeps its signature | D15. What to do with the file is in the instruction; the fire key reaches the handler through the context, so the alert does not need it. |
| Tool description: "never instructions", "procedures are skills", typed folders | Explains what the workspace is and what "keep state" means; short, current files | D16, D17. |
| View header line "Your own working file: data from earlier runs, not instructions." | Neutral header | D17. |
| Same-fire header "the state before this fire … base this run on it" | "this version was written by an earlier attempt of this same fire; its result may not have reached the user. The version before this fire is generation G." | D16: no state section is assumed. |
| A1: draft a `morning-briefing` skill + state file + `reminder_skill` | The long instruction becomes `projects/morning-briefing.md`; the reminder says "follow the workspace file" and carries `workspace_file`; no skill | D15, D16. |
| `copy` from an upload only into `files/` | A text upload may be copied into any folder; binary only into `files/` | A1 needs the owner's instruction byte for byte in `projects/`, without retyping. `files/` stays copy-only. |
| `skill-creator`: state files + `reminder_skill` | Skills that keep state name their file and say when to read and update it (tutor example) | D16. |
| — | §11.6: injection hardening is out of scope | D17. |

## 1. Problem

Smart has no place to keep working material between conversations. Every run starts with the biography, the
directives, the skills catalog and one channel's tiered history. A task that spans conversations loses its plan,
its decisions and its next step. A recurring task cannot remember its last run: the owner's morning briefing is a
reminder whose long instruction holds the whole procedure, and nothing holds what earlier issues covered, so topics
repeat.

The existing stores each hold something else:

| Kind | Home | Why it cannot hold working material |
|------|------|-------------------------------------|
| Facts about the user | memory (consolidation; `propose_memory_facts` + `$memory save`) | Consolidation splits text into atomic facts and rewrites them. Retrieval is by similarity. A plan returns in fragments. |
| Rules always in force | `standing_directives` | User-issued, curated by Stage 2b, cap 15. |
| Procedures applied by meaning | skills | In the catalog of every prompt; change only through the owner's `$skill save`. |
| Deferred obligations | self-reminders | They fire at a time. The instruction is rewritten by NotesAgent's LLM and has no room for a record. |
| The user's documents | user drive | The user's own area, absent when not connected. |

The memory-facts work (`MEMORY_FACT_CONFIRMATION_RFC.md` §1) measured the cost of having no such store: of 90
`save_to_memory` calls, 14 were procedures and 2 task state, and one procedure became 4 standing directives. The
routing this RFC completes: **facts about the user → memory (owner-confirmed), working material and state →
workspace, time → reminders.**

**Prior attempts.** `AGENT_NOTES_RFC.md` built a notepad injected every turn; the model used it as a sticky note and
it became self-reminders. Lesson: no push into every prompt, and a clear job. `decisions/standing_directives.md:54-55`
rejects autonomous self-corrections in the prompt; the workspace never enters the prompt by itself (D1).

**Evidence** (2026-10-14, `prompt_content`): a personal skill asking Smart to keep notes in a drive folder was
loaded in 11 traces and never read or wrote the folder. Request-triggered persistence works (reminders in 41 of 322
Smart traces, `use_skill` in 34). Hence D1: the workspace is pulled by a request, an instruction or a reminder,
never pushed into every prompt.

## 2. Prior art

Claude Code's memory directory and Anthropic's memory tool: plain files under fixed folders, a command-style tool
over paths (`view`, `create`, `str_replace`, `rename`, `delete`), line-numbered views, exact-string replacement with a
uniqueness rule. The `workspace` tool follows that shape and the command names; its parameter names are its own
(`path`, `new_path`, `old_str`, `generation`, …) and differ from Anthropic's in places. It does not copy the
always-loaded index (D1) or a rules file (rules are standing directives).

## 3. What the workspace is for

**A knowledge base the bot keeps for its own work** (D16): files an instruction asks it to keep or follow, kept
between runs. Nothing in it is required, and no file has a required structure. Four folders organise it:

| Folder | Typically holds | Lifetime |
|--------|-----------------|----------|
| `projects/` | one file per task or recurring job: a plan, a running record, a long instruction a reminder follows | until deleted |
| `references/` | where things live and how to find them: folders, senders, ids, search words that worked | until deleted |
| `scratch/` | drafts and intermediate results | 7 days after the last write |
| `files/` | byte-exact copies of chat uploads and delivered documents | until deleted |

**Routing, not rules** (said in the tool description, §7.1): facts about the user go to memory; something to do at a
time is a reminder; documents for the user go to the drive or a delivered document. A procedure applied by meaning
across conversations is a skill; a skill with fixed state describes its own workspace file (§11.2).

Folders, not frontmatter: the lifecycle needs a per-object signal at write time (R1), and a path cannot disagree
with itself.

## 4. Mechanism

```
 trigger (D1)                       Smart run, any path                         storage
 ────────────                       ───────────────────                         ───────
 user asks in chat ───────┐
 an instruction (skill,   ├──►  Smart ── workspace(command, …) ──►  handler, built per execution:
   reminder) names a file │       local tool (DelegationEngine)      lock · {path: generation} ·
 reminder alert renders   ┘                                          origin · fire key · references
   "Workspace file: …"                                                       │
                                                                              ▼
                                                     WorkspaceService (services/): paths, kinds, caps,
                                                     copy sources, warnings, [Workspace] log line
                                                        │                              │
                       FileConversionService.resolve_bytes                             ▼
                       (copy from uploads / delivered docs)        WorkspaceStorePort ── GcsWorkspaceAdapter
                                                                    bucket: {user_id}/<folder>/<path>, versioned
                                                                              ▲
 open_file / edit_image / draft_skill from_file ──► FileConversionService ───┘  (`workspace:` refs, §12)
```

Zero new intents, zero LLM calls, zero prompt changes. The prompt carries only the tool declaration (static, in the
cached prefix). Nothing is read unless a trigger asks.

## 5. Storage

### 5.1 Why a dedicated GCS bucket (kept from rev 1)

- **Arbitrary files.** Copies of PDFs and scans are part of the job; a Firestore document is capped at 1 MiB.
- **History for free.** Object versioning gives `history` and `restore`: every overwrite and delete keeps the
  previous generation, with no version documents to write.
- **Directory layout.** Paths map to object names; one prefix list returns every file with its metadata.
- **Not the media bucket.** Uploads there expire ("files are kept for 90 days", `file_management_agent.py:236`).
  Mixing lifecycles in one bucket would make every rule a prefix puzzle.
- **Not the user's drive.** Absent when not connected, it is the user's area, and the drive experiment failed (§1).

### 5.2 Layout and metadata

- Object name `{user_id}/{path}`, e.g. `{user_id}/projects/morning-briefing.md`. The adapter builds the name; the
  path is validated in domain (§6), so it cannot leave the user's prefix.
- Custom metadata on every `put`: `origin` (§9); `fire` (the fire key, reminder runs only); `write_id` (a fresh UUID
  per put); `restored_from=<generation>` on a restore. `Content-Type` comes from the extension. `customTime` is set
  on every write under `scratch/` only (R1).
- Custom metadata patched onto the live version just before a `delete`: `deleted_by` (origin), `deleted_fire`
  (fire key, when set), `delete_id` (a fresh UUID), and `renamed_to=<new path>` when the delete is the second step of
  a rename. The deleted version keeps them as a noncurrent object, so `view` and `history` can say who deleted a file
  and where it went.
- No description, no `kind`: the listing shows path, size, date and origin, and the kind is the extension (§6).
  Origin and history serve debugging and undo, not protection (D17).

### 5.3 Versioning and preconditions

- **Object versioning on.** An overwrite or delete turns the live object noncurrent; it stays readable by generation.
- **A precondition on every mutation.** `create` of a new path uses `if_generation_match=0`; every other write and
  every delete uses the generation the command read. HTTP 412 (`PreconditionFailed`) becomes `WorkspaceConflict`.
  google-cloud-storage 3.9.0 (installed; `requirements.txt:3` is unpinned and gets pinned, §13) accepts
  `if_generation_match` on `upload_from_string`, `patch`, `delete` and `download_as_bytes`, `generation=` on `blob()`
  for reads, and `versions=` on `list_blobs`.
- **Delete = patch, then delete, both under `if_generation_match=g`** (port `delete(user_id, path,
  if_generation_match, meta)`). A metadata patch changes only the metageneration, so the generation precondition
  still holds for the delete. If the delete then fails for a reason other than 412, the live version carries
  `deleted_*` keys while still live; renderers ignore them on live objects. **The adapter never deletes through a
  generation-addressed blob**: in a versioned bucket that permanently erases the version instead of making the live
  object noncurrent. That includes the blob just patched: `patch()` reloads `blob.generation`, and `Blob.delete()`
  then sends it, so the delete goes through a fresh blob object for the same name. Rollbacks and ordinary deletes
  alike use `if_generation_match`.
- **A 412 on a retried request is checked, not assumed.** The client retries a request whose response was lost; the
  retry of a write that already landed meets its own result and gets 412. On 412 the adapter reads the live
  metadata: `write_id` equal to ours → the write succeeded. For a delete: no live object and the newest noncurrent
  version carries our `delete_id` → success. Anything else is a real `WorkspaceConflict`.
- **Rename / copy / restore = read + put (+ delete), never a server-side copy (R5).** A copy would carry the
  source's `customTime`, so a restored old scratch version would expire at once. Rename: read the source (generation
  g), put the destination with `if_generation_match=0`, delete the source with `if_generation_match=g` and
  `renamed_to` in `meta`. If that delete conflicts, the destination is deleted again with `if_generation_match` = its
  new generation and the command reports the conflict; if that rollback also fails, both paths exist, the line is
  logged at ERROR and the result names both paths.
- **A version is the GCS generation number (R6),** passed as a string. Sequential numbers would shift when the
  lifecycle deletes old versions.
- **Exact names in prefix lists.** A list with prefix `projects/a.md` also returns `projects/a.md.bak`; every
  file-level list keeps only entries whose name equals the path.

### 5.4 Lifecycle (kept from rev 1)

```json
{"rule": [
  {"action": {"type": "Delete"}, "condition": {"daysSinceCustomTime": 7}},
  {"action": {"type": "Delete"}, "condition": {"isLive": false, "daysSinceNoncurrentTime": 90}},
  {"action": {"type": "Delete"}, "condition": {"isLive": false, "numNewerVersions": 50}}
]}
```

- **Scratch: 7 days after the last write (R1).** `matchesPrefix` cannot express `*/scratch/`, because keys start
  with `{user_id}/`. Each scratch write sets a fresh `customTime`; objects without one never match. The noncurrent
  version still carries its old `customTime`, so scratch history goes within about a day. GCS applies lifecycle
  actions asynchronously, up to a day late.
- **History: 90 days and 50 versions.** The undo of an autonomous writer. Worst-case storage is bounded by D4
  (100 files × 50 versions × 10 MB), in practice a few KB per text version: cents per month.
- **Soft delete off.** Versioning already gives recovery; a hidden 7-day soft-deleted copy would survive an
  erasure (§19).

### 5.5 Configuration

`GCS_WORKSPACE_BUCKET` is required configuration for the feature, so it goes into `load_settings()` with an empty
default (`settings.py:48-53` rule; precedent `GCS_MEDIA_BUCKET`, `settings.py:79`), not `os.getenv` at a call site.
On Cloud Run the value comes from a Secret Manager secret of the same name in the main service's `--set-secrets`
(`cloudbuild-dev.yaml:30`, precedent `SERVICE_ACCOUNT_EMAIL`), never a Cloud Build substitution, so the
deploy-substitution trap cannot bake a local value into the revision. A test pins the entry
(`tests/unit/test_cloudbuild_indexer_model_secrets.py` precedent) and `test_arch_config_key_registration.py` keeps
passing. Only `main.py:98` calls `load_settings()` (the relay and the indexer read their own env,
`relay_main.py:31`, `indexer_main.py:15`), so only the main service resolves the key; with `--set-secrets` it is
never empty there, so no extra Secret Manager lookup happens on boot.

**Local dev uses the live bucket (D9).** `load_settings()` chases every empty key into Secret Manager when
`GOOGLE_CLOUD_PROJECT` is set (`settings.py:119-136`), so a local run with an empty `.env` value reads the live
secret and writes to the live bucket, exactly as it writes to the live `development_` Firestore collections. A local
instance needs the developer's own credentials to have object access on the bucket. An empty value cannot switch
the feature off locally while `GOOGLE_CLOUD_PROJECT` is set; to point elsewhere, set another bucket name in `.env`.

When the key resolves empty (no secret, emulator), no adapter is built, Smart gets no `workspace` tool,
`FileConversionService` refuses `workspace:` refs, NotesAgent is built with `workspace_enabled=False` and refuses
`workspace_file`, and every path behaves as today.

## 6. Paths, kinds, limits

**Path rule** (domain, pure, `parse_workspace_path`): lowercase `[a-z0-9-/._]`; starts with `projects/`,
`references/`, `scratch/` or `files/`; segments non-empty, never `.` or `..`, starting with `[a-z0-9]`; no leading or
trailing `/`, no `//`; the last segment has an extension; at most 120 chars and 4 segments. A `workspace:` prefix is
accepted and stripped. `view` and `history` also accept a folder prefix (`projects/`, `projects/garden/`) or the empty
path (the whole workspace).

**Kinds.** A file is `text` when its extension is `.md .txt .csv .tsv .json .yaml .yml` (the skill-file set,
`domain/skill.py:30`) and it decodes as UTF-8; every other file is `binary`.
- `create` and `str_replace` write text, and only under `projects/`, `references/` and `scratch/`.
- `files/` is byte-exact (content integrity, not safety): it is filled only by `copy` and changed only by `copy`,
  `rename`, `delete`, `restore`. Binary files live only there.
- `copy` of a text file (an upload or a workspace file) may land in any folder; a text copy outside `files/` is then
  an ordinary editable file.

| Limit | Value | Why |
|-------|-------|-----|
| Live files per user | 100 | D4. A command that would add a file beyond it is refused: "workspace full (100 files); delete or merge first". Soft: two concurrent creates can land at 101. |
| Live bytes per user | 100 MB | D4. Checked the same way, counting the new size. |
| File | 10 MB | D4. Under Gemini's 20 MB inline cap for `open_file` vision. |
| Text file | 256 KB | Same as skill files (`domain/skill.py:31`). Applies in every folder, `files/` included: a UTF-8 text-extension upload over 256 KB cannot be copied. |
| `view` window | 2,000 lines by default (`offset`, `limit`), each cut at 2,000 chars, ≤ 100 KB per call | Claude Code's read contract; a larger file is read in windows. `offset`/`limit` that are empty, zero or negative mean the defaults. |
| Listing / history | 100 entries (newest first), then "…N more" | Equal to the file cap. |

The caps are checked by one `list(user_id, "")` call inside the commands that add a live file: `create` of a new
path, `copy`, and `restore` of a deleted file. `rename` adds no file and no bytes, so it is not checked. Constants
live in `domain/workspace.py`; enforcement in `WorkspaceService`.

## 7. The tool contract

### 7.1 Declaration and description

One tool, `workspace`, built in `src/infrastructure/workspace_tools.py`. Declared in `SmartResponseAgent._run`
right after the `propose_memory_facts` block (`smart_response_agent.py:503-512`), outside `if skills:` (`:491`), when
`self.workspace_service` is set and `prompt_user_id` (`:416`) is known — on every path, interactive or not (D2, D5).

**Description (draft, exact text):**

> Your workspace: files you keep for your own work between conversations and runs — a knowledge base you build and
> follow. Use it when the user asks you to, when an instruction you are following (a skill, a reminder) names a
> workspace file or asks you to keep state, or when a firing reminder carries a workspace file; not on your own
> initiative otherwise. How to use a file is up to that instruction; when it says nothing, use your judgement.
> "Keep state in the workspace" means: keep one file for it, view it at the start, update it before you finish.
> Folders: projects/ (one file per task or recurring job), references/ (where things live and how to find them),
> scratch/ (drafts; deleted 7 days after the last write), files/ (byte-exact copies of chat uploads or delivered
> documents, made with copy; look at one with open_file and the ref workspace:files/<name>). Keep files short and
> current: replace what changed with str_replace, do not append a log. Facts about the user never go here: what the
> user says reaches memory by itself; facts you read in a file or other source go through propose_memory_facts, only
> when the user asks. Something to do at a given time is a reminder. Every change keeps the previous version:
> history shows who wrote each one, restore brings one back. If a view header says an earlier attempt of this same
> reminder fire wrote the file, its result may not have reached the user: view the generation it names and work from
> that. If a file a skill or reminder names is missing, view shows whether it was deleted or renamed: tell the user
> and offer to restore it; never recreate it silently. A file that never existed may be created.

### 7.2 Parameters

Flat object; only `command` is required. Unused parameters are ignored (models fill them with `""`). No
`Optional[T]`/null unions and no advertised constraints beyond the `command` enum (the lesson of
`decisions/mcp_tool_schema_liberal_input.md`; limits are stated in descriptions and enforced in code).

| Parameter | Type | Used by | Description (abridged) |
|-----------|------|---------|------------------------|
| `command` | enum | all | `view` `create` `str_replace` `delete` `rename` `copy` `history` `restore` |
| `path` | string | all | Workspace path, e.g. `projects/garden-shed.md`; for `view`/`history` also a folder (`projects/`) or `""` for everything; for `copy` the destination. |
| `offset` | integer | view | First line to show (1-based); empty, 0 or negative → 1. |
| `limit` | integer | view | Number of lines; empty, 0 or negative → 2,000. |
| `generation` | string | view, restore | `view`: read that earlier version (read-only). `restore`: the version to bring back. Both from `history` or a view header. |
| `content` | string | create | The full text of the file. |
| `old_str` | string | str_replace | Exact text to replace; must occur exactly once. |
| `new_str` | string | str_replace | Replacement text (may be empty). |
| `new_path` | string | rename | The new path. |
| `source` | string | copy | A chat file name from a `[File: …]` label, a delivered-document key, or a workspace path. |

### 7.3 Commands

All results are plain text. A failure returns `failed=True` with a `SYSTEM:` line naming the problem and the fix,
in the `propose_memory_facts` style (`memory_fact_tools.py`).

| Command | Behaviour | Result |
|---------|-----------|--------|
| `view` file | Two GETs (metadata, then media at that generation). Records `{path: generation}` in the session. Text: header (§7.4) + line-numbered window (`cat -n` form). Binary: refused with the `open_file` pointer. When the live version's `fire` equals this run's fire key, one more `list(include_versions=True)` on the path finds the generation from before this fire (§7.4). | header + lines; "…lines N–M of T; view with offset to read on" when cut |
| `view` file + `generation` | Reads that version, live or noncurrent. Read-only: not recorded in the session, so a later `create` cannot overwrite on its strength. | header ("version G, not current") + lines |
| `view` folder / `""` | One `list(prefix, include_versions=True)`: live files listed, deleted files counted. | listing (§7.4) |
| `view` missing path | One `list(include_versions=True)` on the path. | "deleted … by …" or "renamed to …" + versions (§7.4), or "never existed" |
| `create` | Under `projects/`, `references/`, `scratch/` only, text kinds only. New path → caps list, one versions list of the path (logged as `had_history`), put with `if_generation_match=0`. Existing path → allowed only when this execution viewed its current version; put with `if_generation_match` = that generation (overwrite). | "created" / "overwritten", path, generation, lines |
| `str_replace` | Same folders. Self-guarding (D13): read the current generation; `old_str` must match exactly once ("not found" / "found N matches; include more context"); put with `if_generation_match` = that generation. On 412: re-read, re-apply once; a second 412 → conflict. Updates the session generation. | "replaced", generation, the changed lines with numbers |
| `delete` | Generation from the session, else from one `list` call; `delete(…, if_generation_match, meta={deleted_by, deleted_fire})` (§5.3). Warns on references (§8.4). | "deleted; restorable from history" + warnings |
| `rename` | §5.3. `new_path` must not exist. A file in `files/` stays in `files/`; a file may not move into `files/`. Warns on references to the old path. No cap check. | "renamed", new generation + warnings |
| `copy` | Source = workspace path → port `read` + `put`. Source = chat upload or delivered key → `FileConversionService.resolve_bytes` (ownership check, `file_conversion_service.py:140-162`). Binary → destination under `files/`; text → any folder. `drive:` and `skill:` sources are refused with the right tool named. The destination must not exist, or this execution must have viewed its current version. | "copied", path, size |
| `history` file / folder | `list(include_versions=True)`, exact-name filtered for a file: generation, time (UTC), size, origin, fire key, `current` / `deleted by …` / `renamed to …`. A folder also lists deleted files. | version table |
| `restore` | Reads the chosen generation; puts it as a new live version with `if_generation_match` = current generation, or 0 when the path is deleted (then caps checked); `restored_from` set; origin = this run. Nothing is lost: the replaced version becomes noncurrent. | "restored generation G as generation H" |

**Wrong store.** A `drive:` ref, a `skill:` ref, or an upload name passed as `path` is refused with the right tool
named (`open_file_from_drive`, `open_file`, or `copy` with `source`).

### 7.4 What `view` and `history` show

File header (one block, then the lines). It names the file and its last writer, for history and debugging; it is not
a protection device (D17). Example generation numbers are shortened.

```
[workspace:projects/morning-briefing.md · generation 1760943600123 · 38 lines · 2.1 KB
 last written 2026-10-20 06:01 UTC by background:execute_reminder (fire reminder:1729401234567@2026-10-20T06:00:00+00:00)]
     1	# Morning briefing
     …
```

**Same fire (B1).** When the live version's `fire` equals the current run's fire key, the run is a re-run of a fire
whose earlier attempt already wrote the file (§10 row 1). The handler looks up the newest version whose `fire`
differs (live or noncurrent) and adds:

```
(this version was written by an earlier attempt of this same fire; its result may not have reached the user.
The version before this fire is generation 1760900000000.)
```

Not shown for a version this execution wrote or already viewed.

If no such version exists, the line reads `… The file did not exist before this fire.` What to do with that is the
model's call under its instruction; the tool description says to work from the earlier version. For `chat` /
`voice:*` origins the fire parenthesis is omitted.

Folder listing:

```
workspace:projects/ — 3 files
  projects/garden-shed.md · 4.2 KB · 2026-10-18 · chat
  projects/morning-briefing.md · 2.1 KB · 2026-10-20 · background:execute_reminder
  projects/conference-talk.md · 1.0 KB · 2026-10-02 · voice:tell_alek
(2 deleted files in this folder — history projects/ lists them)
```

Missing path (`render_missing`), from the newest noncurrent version's patched metadata:

```
workspace:projects/morning-briefing.md does not exist.
It was deleted 2026-10-19 14:02 UTC by chat. Versions kept:
  1760900000000 · 2026-10-19 09:10 UTC · chat · 2.0 KB
  …
Tell the user it is missing and offer `restore` with one of these generations. Do not recreate it.
```

For a rename the second line reads `It was renamed to projects/briefing.md on 2026-10-19 14:02 UTC by chat; update
the reminder's workspace_file (manage_self_reminders, context={"workspace_file": "projects/briefing.md"}) or rename it
back.` A path with no versions at all reads `… does not exist and never did.` — a first run may then create it.

### 7.5 Errors

| Case | Result (`failed=True`) |
|------|------------------------|
| Invalid path / wrong folder for the command or kind | `SYSTEM: invalid workspace path …: <rule>.` (e.g. "files/ holds copies only; use copy") |
| `create` on an existing path whose current version was not viewed in this execution | `SYSTEM: <path> exists. View it first and use str_replace, or view it and create again to overwrite.` |
| Precondition conflict (another run wrote it) | `SYSTEM: <path> changed since you read it (another conversation or run wrote it). View it again and redo your change.` |
| `str_replace` no match / several | `SYSTEM: old_str not found in <path>` / `found N matches; include more surrounding text.` |
| Caps | `SYSTEM: workspace full (100 files / 100 MB) …` / `file over 10 MB` / `text file over 256 KB` |
| Binary `view` | `SYSTEM: <path> is binary; open it with open_file and file_ref workspace:<path>.` |
| Unknown `generation` | `SYSTEM: <path> has no version <G>; history lists them.` |
| Store failure | `SYSTEM: the workspace is unavailable right now; nothing was changed.` (ERROR logged) |
| Missing file for `delete`/`rename`/`str_replace` | `SYSTEM: <path> does not exist; view it to see its history.` |
| Arguments cut off at the output limit | SYSTEM: your arguments were cut off at the output limit; nothing was written. Create a short file, then fill sections with str_replace. |

### 7.6 The per-execution handler session

`make_workspace_handler(service, user_id, origin, fire_key, references)` builds one handler per execution attempt,
beside the other local tools (`smart_response_agent.py:489-522`), so provider rotation starts it fresh. It holds:

- `generations: Dict[path, int]` — filled by `view` of the current version, `create`, `str_replace`, `rename`,
  `copy`, `restore`;
- one `asyncio.Lock`, taken as the **first await of every command**, reads included. `_dispatch_local` awaits the
  handler with nothing before it (`delegation_engine.py:613-619`), and `_execute_tool_calls` creates the batch's
  tasks in emission order under `asyncio.gather` (`:590-598`). `asyncio.Lock` wakes waiters FIFO, so the commands of
  one batch apply in the order the model wrote them: `view` then `str_replace` of the same file in one batch works,
  and two edits of one file never race;
- **mutations under `asyncio.shield`**, awaited inside the lock. The shielded inner coroutine itself updates the
  session map and writes the `[Workspace]` log line, so both stay true when the outer task is cancelled (§10 row 3);
- `origin` and `fire_key` (§9), stamped on every write;
- `references` — the request's skill bodies (`skills`, loaded at `:433-435`) and `message.context["agent_notes"]`
  (`:415`, present on Router-routed runs; each carries `workspace_file`), used only for the warnings of §8.4.

The handler sets no `history_context`: nothing from the workspace enters session history (§11.5).

## 8. Triggers

### 8.1 The user asks

"Keep a file for the garden shed build", "where are we with the conference talk", "show me your workspace". The tool
description is the only guidance; no prompt block. A user who never asks never sees a workspace call, unless an
instruction or reminder they set up names a file.

### 8.2 An instruction names a file or asks for state

- **A skill.** A skill whose procedure has state of its own describes its file and when to read and update it,
  e.g. a language tutor skill: "At the start, view `workspace:projects/spanish-tutor.md` (words learned, mistakes
  that repeat); at the end, update it." A fixed state schema belongs in such a skill (D16). `skill-creator` teaches
  the pattern (§11.2).
- **A reminder's instruction or the user's words.** "Use the workspace to keep track of what you covered" needs no
  file name: the tool description tells the model what that means (keep one file, view at the start, update before
  finishing), and the model picks a path under `projects/`.
- **The model's judgement.** When the instruction that brought the workspace into play says nothing about how to use
  it, the model decides (D16). It does not open the workspace on its own initiative otherwise (D1).

### 8.3 A firing reminder carries a workspace file

**Field.** `manage_self_reminders` gains one context field, declared in `NOTES.context_schemas`
(`agent_manifest.py:379-405`; NOTES has none today). The delegate tool merges context fields first-wins across
intents (`base_agent.py:793-802`), so the name is unique:

| Field | Description (exact) |
|-------|---------------------|
| `workspace_file` | `Optional: a workspace path (e.g. projects/morning-briefing.md) for the reminder's run to use as its instruction says. Passed here, the path is kept exactly. "remove" takes it off the reminder.` |

The capability prose (`agent_manifest.py:391-403`) keeps "Do not format JSON, do not invent field names" for the
query and adds: `When the reminder's run should use a workspace file, pass context={"workspace_file": "<workspace
path>"} and keep the reminder text short, referring to the file.`
`tests/unit/infrastructure/test_manifest_prose_matches_tool_schema.py` then checks that the prose field is in the
generated schema.

**Why a field at all (D15).** NotesAgent's own LLM rewrites the reminder text from Smart's query; a path written in
prose comes back altered or not at all. The field carries the path past that LLM; nothing else about it is special.

**Value parsing.** `"remove"` (any case) clears the field. `""`, `none`, `null`, `n/a` (any case) and absence mean
"not provided": models fill unused fields that way, and clearing must never happen by accident.

**Validation, before any LLM call.** The coordinator spreads `context` into the specialist's payload
(`agent_coordinator.py:519-531`), so NotesAgent reads `message.payload["workspace_file"]` (never `message.context`).
`execute` (`notes_agent.py:266`) checks that the agent was built with `workspace_enabled=True` (a named constructor
flag, False when no workspace is configured) and that the path parses (§6). Any folder is accepted (D17). The file
need not exist: a first run may create it. An invalid value fails the delegation with the reason.

**Binding by code.**
- **The guard is a local of `_run`** (`notes_agent.py:299`), passed into `_execute_tool`, never instance state:
  NotesAgent is a per-user singleton, and two channels can run it concurrently.
- **More than one create/update refuses all.** NotesAgent executes every CRUD call of a response and only then
  collects errors (`notes_agent.py:392-412`), returning after the first CRUD batch (`:399-413`). So when the field is
  set and the first response that holds create/update calls holds more than one, every call in that response gets the
  tool error "workspace_file applies to one reminder per request; create or update exactly one" before any runs, and
  the LLM gets its next turn.
- **The value overrides** whatever the LLM sent for that one create/update call. An execution that ends without a
  create/update says so in its summary ("workspace_file was not applied").
- **Code tells the LLM what will happen.** When the field is set, `_run` appends to NotesAgent's user message
  (`notes_agent.py:343`): `System: workspace_file=<path> will be attached by code to the reminder you create or update
  in this request. Call update_self_reminder with its note_id even if nothing else changes. Keep the instruction short
  and let it refer to the file.` The `instruction` parameter description of both tool declarations
  (`notes_agent.py:90-98`, `:140`) gains, in git: `When a workspace file is attached to the reminder, keep this short
  and let it refer to the file.`
  For "remove" the line is: `System: the workspace file will be removed by code from the reminder you update in this request. Call update_self_reminder with its note_id even if nothing else changes.` "remove" is accepted even when no workspace is configured, so a stale field can be cleared.

**Storage.** `AgentNote`, `NoteCreate`, `NoteUpdate` (`domain/agent_note.py:18, 45, 56`) gain `workspace_file`
(optional); `NoteUpdate` gains `clear_workspace_file`, the `clear_recurrence` pattern (`:65-68`).
`FirestoreAgentNoteAdapter` writes and reads it (`firestore_agent_note_adapter.py:53-89, 109-145, 345-366`).
Optional field, no migration.

**Rendering** — everywhere a reminder is shown:

| Where | File | Rendered |
|-------|------|----------|
| Fire-time alert | `reminders_service.build_reminder_alert` (`reminders_service.py:243-275`) | `Workspace file: workspace:<path>` after the `Schedule:` line |
| Smart's `active_reminders` | `prompt_assembly_service.py:495-533` | `, workspace file: workspace:<path>` in the note line |
| Router → Smart | `router_agent.py:340-360` | `workspace_file` in each `agent_notes` dict |
| NotesAgent's own list | `notes_agent.py:316-334` | `workspace file: …` line beside `instruction:` |
| Receipt | `notes_agent.py:468-472, 511-515` (`notify_raw`) | ` · workspace file <path>`; on clear ` · workspace file removed` |
| Cabinet | `user_cabinet_app.py:613-654` (GET) + `src/web/static/cabinet.html` | one read-only line; edited through chat |

The alert line is exactly `Workspace file: workspace:<path>`: no "follow it", no framing. What to do with the file is
in the reminder's instruction (D15). `build_reminder_alert(note)` keeps its signature and renders the line only when
`note.workspace_file` is set, so `tests/unit/services/test_reminders_service.py:405` onward is untouched.
`worker_handler._handle_execute_reminder` (`worker_handler.py:747`) builds the fire key with
`domain.workspace.fire_key(note_id, due_at)` and passes it to `notify` (§9); the alert does not render it, the view
header does.

### 8.4 Lost, renamed and deleted files

- **Missing at fire time (D8).** The alert names the file; `view` shows "deleted on … by …" or "renamed to …" with its
  versions, or "never existed" (§7.4). The tool description says: report and offer restore, never recreate silently;
  a file that never existed may be created.
- **Warnings.** `delete` and `rename` scan `references`: any skill body that contains the old path (with or without
  `workspace:`), and any `agent_notes` entry whose `workspace_file` equals it. A match appends
  `Warning: skill "<name>" names this path` / `Warning: reminder "<text>" [id] uses this workspace file; update it
  (manage_self_reminders, context={"workspace_file": …}) or restore the file.` The command still runs (history keeps
  the old version). On paths without `agent_notes` (reminder, daily review), only skills are checked; the reminder
  run's own alert names its file.

## 9. Origin

Every write stamps `origin` (and `fire` when set) as object metadata. Derived per execution from a new context key
`run_origin` (plus `run_fire_key` for reminder runs):

| Path | Set where | `run_origin` |
|------|-----------|--------------|
| Chat (Slack/Telegram) | derived in Smart: `interactive_delivery` is set only by `ConversationHandler` (`conversation_handler.py:935`) | `chat` |
| Reminder fire | `worker_handler.py:836-845` calls `notify(..., origin_detail="execute_reminder", fire_key=…)`; `UserNotificationService.notify` (`user_notification_service.py:277`) sets `run_origin="background:<origin_detail>"` and `run_fire_key` | `background:execute_reminder` |
| Daily email review | `worker_handler.py:969-985`, `origin_detail="daily_email_review"` | `background:daily_email_review` |
| Other `notify` calls | `notify` falls back to `background:<kind>` when no `origin_detail` is given | e.g. `background:document_delivery` |
| `ask_alek` / `tell_alek` | `AlekGatewayAgent.execute` adds it to the routed context (`alek_gateway_agent.py:85, 98`) | `voice:ask_alek` / `voice:tell_alek` |
| Anything else reaching Smart | Smart's fallback, logged WARNING | `background:unknown` |

`notify()` builds a fresh context (`user_notification_service.py:368-386`), so an origin never leaks from the
caller. Only the two worker calls reach Smart today (`agent_id_override` at `worker_handler.py:843, 974`); the others
go to Quick, which has no workspace, and are labelled by the fallback at no cost. `tell_alek` runs as an
`agent_execution` Cloud Task (`/worker`); the gateway sets the origin there. The context key propagates downstream
like every context key (`DelegationEngine` passthrough); nothing else reads it.

## 10. Concurrency, idempotency, failure modes

Ranked by likelihood × damage.

| # | Failure | Mechanism | Mitigation |
|---|---------|-----------|------------|
| 1 | **A reminder fire runs twice and builds on its own unsent result** | `execute_reminder` returns 500 when delivery fails after Smart already wrote (`worker_handler.py:850-859`); Cloud Tasks re-runs the whole fire. The `last_delivered_due` guard (`:799`) only stops re-runs after success. A record the earlier attempt wrote ("covered: X") may be false, because its issue never reached the user. | Same `due_at` → same fire key. The `view` header detects a live version written by this same fire and names the version before it (§7.4, B1); the tool description says to work from that version. Short, replace-not-append files make a re-run cheap to redo. |
| 2 | **Two runs edit one file** (chat + reminder, two channels) | Separate executions, separate sessions. | Generation preconditions. `str_replace` re-reads and re-applies once; `create` refuses; the model gets the conflict text and re-views. Nothing is overwritten silently. |
| 3 | **A cancelled run lands a write but reports nothing or "not finished"** | Two cancellers: the long-turn clock cuts a tool batch with `asyncio.wait_for` (`delegation_engine.py:414-426`); and `BaseAgent` wraps `execute` in `asyncio.wait_for` on the message timeout (`base_agent.py:682-695`), which is the only limit on reminder runs (no turn clock). A cancelled multi-step mutation (rename: put, then delete) could stop between steps. | Every mutation runs under `asyncio.shield` inside the lock, and the shielded inner coroutine updates the session map and logs itself, so the mutation completes and is recorded. After a cut batch only the wrap-up call follows, which dispatches nothing (`:303-306`, `:488-528`; dropped calls already logged at WARNING, `:502-516`). After a message timeout the run fails; for a reminder that means a 500 and a re-run of the same fire, which row 1 covers. The answer may misreport; the next `view` shows the truth. |
| 4 | **Files grow without bound** | No cleanup mechanism, by decision (D16). A run told to "keep track" may append. | The tool description asks for short, current files (replace, do not append). Caps bound the worst case. The review reports per-file growth (§16); a file that only grows is fixed in its instruction, not by a mechanism. |
| 5 | **A reminder's file is deleted or renamed** | User request, a careless rename, a background run. | §8.4: warnings at the moment of the change; at the next fire `view` says who deleted it or where it went, with restore; 90 days of history. |
| 6 | **A retried request reports a false conflict** | The storage client retries after a lost response; the retry meets its own landed write and gets 412. | `write_id` / `delete_id` read back on 412 (§5.3). |
| 7 | **A co-emitted write fails unseen** (Grok `deliver_response` siblings are dispatched, results unread, `delegation_engine.py:337-367`) | The answer is already written. | The handler logs every failed mutation at WARNING (`[Workspace] … result=conflict|refused|error`). |
| 8 | **Partial rename** | Source delete conflicts and the rollback delete also fails. | ERROR log; result names both paths (§5.3). |
| 9 | **Store outage** | GCS errors. | Tool error; the run continues without the workspace; a reminder run says so in its message. |
| 10 | **Cap overshoot** | Two concurrent creates both pass the list check. | Soft caps; one batch over at most. |
| 11 | **Provider rotation mid-run** | `_run` re-enters with a fresh transcript. | New handler, empty session map. Writes of the failed attempt stay; a `create` over them is refused until a `view`. |
| 12 | **A reminder's file sits in `scratch/` and expires** | Any folder is allowed (D17); scratch expires 7 days after the last write. | A file the run updates on every fire stays alive if it fires at least weekly; otherwise the next fire's `view` reports it deleted by the lifecycle. The tool description calls scratch "drafts". |
| 13 | **Local runs write to live data** | D9. | Stated; same as Firestore today. |

Prompt injection through workspace content is not in this table: see §11.6.

## 11. Interactions

### 11.1 Memory facts

The store boundary (`MEMORY_FACT_CONFIRMATION_RFC.md` §2): facts about the user never go to the workspace. What the
user says reaches memory by itself, through consolidation of the conversation; facts the bot read in a file or other
source go through `propose_memory_facts`, only when the user asks. That tool is declared on interactive turns only
(`smart_response_agent.py:503-512`); the workspace on every path. A background run that learns a fact mentions it in
its message; the user's reply or a later proposal carries it.

### 11.2 Skills and `skill-creator`

Skills are procedures applied by meaning, saved only by `$skill save`. A skill with state of its own (a fixed schema,
e.g. a tutor's learned words) names its workspace file in its body (D16). `draft_skill`'s `from_file` (per skill text
file, `skill_tools.py:142-150`) accepts `workspace:<path>` once `FileConversionService` resolves it (§12):
`SkillService._read_from_file` (`skill_service.py:182-211`) already checks the extension and reads through
`resolve_bytes`. Two small changes there: the `from_file` description names `workspace:` refs, and the not-found
message for a `workspace:` ref says "not found in your workspace — view the folder to check the path" instead of
"pass the bare filename from the file label". The skill **body** is still passed inline in `body`.

**`visible_skill_names` becomes version-aware** (included task). It returns names whose body markers are visible in
the tiered history (`domain/skill.py:324-336`), and `use_skill` answers "already in your context" for them
(`skill_tools.py:51-80`). The marker carries the version (`[Skill "x" v3]`, `:39`, `:308`), but the name set drops it.
A skill used daily in one channel (the tutor) is loaded again and again; after the owner saves a new version, a stale
body from the previous session, still in the full-text window, would short-circuit the load. The domain function
returns `{name: version}`, and the handler short-circuits only when the visible version equals the current one
(caller at `smart_response_agent.py:495`).

New section in `src/skills/smart/skill-creator/SKILL.md`, after "Keep the body lean: files" (draft, exact text):

```markdown
## Skills that keep state between runs

Some procedures carry state from one run to the next: a language tutor tracks the words learned and the mistakes that repeat; a weekly report compares with last week. Keep that state in a workspace file, not in the skill: the body is the procedure and changes only when the owner saves a new version; the file changes every run.

- Name the file in the body with its full ref, describe what it holds, and say when to read and update it, e.g. "At the start, view `workspace:projects/spanish-tutor.md`. Before you finish, update its Learned and Repeating mistakes sections with str_replace."
- Keep the file short and current: say in the body what to replace and what to drop, so it does not grow into a log.
- If the file is missing, the view says whether it was deleted or renamed: tell the owner and offer to restore it. Create it only if it never existed.
- Long reference files can be drafted in `scratch/` and passed as `from_file: "workspace:scratch/<name>.md"`.
```

### 11.3 Fingerprint (`SYSTEM_TURN_FINGERPRINT_RFC.md`)

- **Scrubber scope.** Its §4.5 scrubs tool-call arguments "before dispatch", placed in `_call_llm` / the delegation
  dispatch. Local tools bypass `dispatch()`: `_execute_tool_calls` routes them to `_dispatch_local`
  (`delegation_engine.py:590-597`). The scrub must cover `_dispatch_local` too, or the per-turn key can be written
  into a workspace file (`content`, `new_str`) and persist across turns. One place covers both: scrub
  `response.tool_calls` once per turn before `_execute_tool_calls`. Recorded here so whichever RFC lands second
  carries it.
- **Input hygiene.** `view` output is a tool result, so the fingerprint's tag stripping applies to it: a workspace
  file cannot imitate a system turn.
- **Reminder line.** `Workspace file: workspace:<path>` is code-rendered from a validated path; the fingerprint may
  wrap it with the rest of the alert. The `instruction` stays model-written text and gets no new authority (its §8).

### 11.4 Lelik, Tutor, Quick

None gets the tool. Lelik reaches it only through `ask_alek` / `tell_alek` → Router → Smart, labelled `voice:*`
(§9). His warm context reads stored summaries (`p.text`), which hold no workspace content. Quick, Smart's fallback,
works without it.

### 11.5 History and consolidation

No workspace content enters session history: the handler sets no `history_context` (the fold that would carry it is
`conversation_handler.py:1387`), and consolidation serialises model parts as `p.text`, the summary
(`domain/consolidation_serialization.py:22-36`). A reply that says "I updated the briefing file" can reach Stage 1
as a summary line; that is not a fact about the user. **No taxonomy change (D13).** Revisit trigger: stored facts of
the form "bot updated file X" or file contents restated as user facts; the fix then is the rev 1 R9 line in
`Trivial_Exclusions`, owner-applied after a dry run.

Tool arguments and results are recorded in `prompt_content` (`bigquery_prompt_content_adapter.py:193-198`, 30-day
TTL) and in Logfire, like every tool call. Workspace content therefore appears there; no new exposure class.

### 11.6 Prompt injection: out of scope

This RFC adds no injection hardening and claims none (D17). A workspace file is read when an instruction points at
it, and an instruction can tell the bot to follow it; the bot can equally schedule any instruction as a reminder
today. Telling instructions from untrusted text is the class-B follow-up of `SYSTEM_TURN_FINGERPRINT_RFC.md` and
applies to the workspace like any other input. What the workspace does give is visibility and undo: every version
records its writer (origin, fire key), and `history` / `restore` reverse any change for 90 days.

## 12. `workspace:` refs in the file pipeline

Minimal support so copied documents work with the existing readers. `FileConversionService` takes the **port**
(`workspace: Optional[WorkspaceStorePort]` in its constructor, `file_conversion_service.py:73-90`), not
`WorkspaceService`, so there is no service-to-service runtime import and no cycle: `WorkspaceService` →
`FileConversionService` (copy sources, `TYPE_CHECKING` import, precedent `skill_service.py:40-41`) →
`WorkspaceStorePort` (REQ-ARCH-22).

- `_download_by_ref` (`:140-162`): a `workspace:` branch first, reading the live object for the request's user (the
  user comes from the request, as for `skill:`).
- `_is_drive_ref` (`:49-58`): returns False for `workspace:` before `parse_drive_ref`, like `skill:`.
- `resolve_content` (`:209-230`): text kinds are returned decoded as UTF-8 in `[File: …]` markers with no mime
  guessing (the `skill:` rule, `:226-228`); binary goes through `convert_file_to_text`. Used by the coordinator's
  `file_ref` prefetch (`main.py:310`, `agent_coordinator.py:583-618`), so a copied PDF can be handed to `create_pdf`.
- `resolve_bytes` (`:255-263`): consumers are `open_file` vision (`file_management_agent.py:270-299`), `edit_image`
  (`image_generation_agent.py:214`), `save_file_to_drive` sources (`file_management_agent.py:560, 610, 620`) and
  `draft_skill` `from_file` (§11.2).
- `FileManagementAgent._fetch` (`:194-255`): accepts `workspace:` refs **only under `files/`**; a ref to a file in
  another folder is refused with "view it with the workspace tool". One reader per kind of file, so every note comes
  with its header (writer, generation, same-fire line).
- `FileManagementAgent._delete` (`:354-379`): refuses `workspace:` refs with "use the workspace tool's delete" (the
  `skill:` precedent at `:370`).
- `open_file`'s description and `file_ref` schema (`agent_manifest.py:535-541, 603-608`) name `workspace:files/<name>`.

A byte read of any folder stays allowed in `FileConversionService` (needed for `from_file: workspace:scratch/…`);
the `files/`-only rule is `open_file`'s. A `workspace:` ref prefetched for another specialist (`create_pdf`,
`create_html_page`) arrives as plain `[File: …]` content without the view header, as every uploaded file does today.
`FileConversionService` exists only when the media bucket is set (`service_container.py:350-357`); without it, `copy`
from uploads and `workspace:` refs are unavailable (local only).

## 13. Layering and files (slice 1)

| Layer | File | Responsibility |
|-------|------|----------------|
| domain | `domain/workspace.py` (new) | Folders, kinds, caps; `parse_workspace_path`; `WorkspaceEntry` (path, generation, size, updated, origin, fire, write_id, live, deleted_at, deleted_by, renamed_to), `WorkspaceBlob`, `WorkspaceWriteMeta`, `WorkspaceDeleteMeta` (pydantic); `fire_key(note_id, due_at)`; `parse_reminder_field` (remove / not provided); `number_lines` / windowing; `apply_str_replace` (unique match); `render_view_header` (incl. same-fire line), `render_listing`, `render_missing`, `render_history`; `find_references(path, skills, notes)`. Pure. |
| domain | `domain/exceptions.py` | `WorkspaceConflict`, `WorkspaceRefused`. Missing objects raise `FileNotFoundError` (the `FileStoragePort` contract). |
| domain | `domain/agent_note.py` | `workspace_file` on `AgentNote`/`NoteCreate`/`NoteUpdate`; `clear_workspace_file`. |
| domain | `domain/skill.py` | `visible_skill_names` → `{name: version}` (§11.2). |
| ports | `ports/workspace_store_port.py` (new) | `list(user_id, prefix, include_versions=False) -> List[WorkspaceEntry]`; `read(user_id, path, generation=None) -> WorkspaceBlob` (bytes + entry); `put(user_id, path, data, meta, if_generation_match) -> WorkspaceEntry`; `delete(user_id, path, if_generation_match, meta)`. Justified: a system boundary (GCS) with an in-memory fake for service and handler tests. |
| adapters | `adapters/gcs_workspace_adapter.py` (new) | Lazy `google.cloud.storage` client, sync calls in an executor (`gcs_media_adapter.py:38-53` precedent); builds `{user_id}/{path}`; metadata incl. `write_id`; `customTime` for `scratch/`; delete = patch, then delete of the live object through a fresh blob, both under `if_generation_match`, never a generation-addressed delete; 412 read-back of `write_id` / `delete_id`; exact-name filtering; `PreconditionFailed` → `WorkspaceConflict`, `NotFound` → `FileNotFoundError`. |
| adapters | `adapters/firestore_agent_note_adapter.py` | Write, update (incl. the clear flag) and read `workspace_file`. |
| services | `services/workspace_service.py` (new) | `WorkspaceService(store, file_conversion)`: every command's semantics, caps, kinds, copy sources, multi-step rollback, same-fire lookup, warnings, the `[Workspace]` log line. No per-execution state. `FileConversionService` under `TYPE_CHECKING`. |
| services | `services/file_conversion_service.py` | `workspace:` branch (§12); constructor takes the port. |
| services | `services/skill_service.py` | Not-found message for `workspace:` refs in `_read_from_file`. |
| services | `services/user_notification_service.py` | `notify(..., origin_detail=None, fire_key=None)`: sets `run_origin` and `run_fire_key`. |
| services | `services/reminders_service.py` | `build_reminder_alert(note)` renders `Workspace file: workspace:<path>` when set; signature unchanged. |
| services | `services/prompt_v3/prompt_assembly_service.py` | `active_reminders` line shows the workspace file. |
| infrastructure | `infrastructure/workspace_tools.py` (new) | Declaration (§7.1–7.2), `WorkspaceSession`, `make_workspace_handler(...)`: lock, shield (inner coroutine updates the map and logs), session generations, origin, WARNING on failed mutations. `WorkspaceService` typed under `TYPE_CHECKING` only (REQ-ARCH-14, `test_req_arch_01_hexagonal_isolation.py:201-217`). |
| infrastructure | `infrastructure/agent_manifest.py` | `NOTES.context_schemas` (`workspace_file`) + prose (§8.3); `open_file` description and schema (§12). |
| infrastructure | `infrastructure/skill_tools.py` | `from_file` description names `workspace:` refs; `make_use_skill_handler` compares versions (§11.2). |
| agents | `agents/core/smart_response_agent.py` | `workspace_service` constructor arg (`TYPE_CHECKING`, like `MemoryFactService` at `:65`); declare + build the handler outside `if skills:`; derive `run_origin`; pass the version map to `use_skill`. |
| agents | `agents/notes_agent.py` | Validate and bind `workspace_file` by code (§8.3): `_run`-local guard, refuse-all on several create/update, appended system line, `instruction` description sentence; render in its list and receipts; constructor flag `workspace_enabled`. |
| agents | `agents/alek_gateway_agent.py` | `run_origin` = `voice:ask_alek` / `voice:tell_alek`. |
| agents | `agents/file_management_agent.py` | `workspace:files/` in `_fetch`; refusal in `_delete`. |
| agents | `agents/core/router_agent.py` | `workspace_file` in `agent_notes`. |
| handlers | `handlers/worker_handler.py` | Build the fire key; pass `origin_detail` (+ `fire_key`) to both Smart-bound `notify` calls. |
| web | `web/user_cabinet_app.py`, `web/static/cabinet.html` | GET returns `workspace_file`; the list shows it read-only. |
| composition | `service_container.py`, `user_agent_factory.py` | Build order adapter → `FileConversionService(workspace=port)` → `WorkspaceService`; `agent_services()` (`:400`) carries it; Smart gets `workspace_service`, NotesAgent gets `workspace_enabled`. |
| config | `config/settings.py` | `GCS_WORKSPACE_BUCKET` in `load_settings()`. |
| deploy | `cloudbuild-dev.yaml`, `requirements.txt` | `GCS_WORKSPACE_BUCKET=GCS_WORKSPACE_BUCKET:latest` in the main service `--set-secrets` (name only); `google-cloud-storage` pinned to the installed 3.9.0 (`requirements.txt:3`). |
| skills | `skills/smart/skill-creator/SKILL.md` | §11.2 section. |
| user-facing | `utils/capabilities.py` | Two lines: ask the bot to keep working files for a long task or a recurring job, and to show them; a reminder can carry a workspace file that its run follows. |
| locales | — | None: tool results are model-facing English; NotesAgent receipts are not localized today. |

Not touched (rev 1 had them): `utils/workspace_text.py`, `google-re2`, `SkillPresence`, `prompt_builder.py`,
`ports/prompt_builder_port.py`, `delegation_engine.py`. Injection hardening is not in this table (§11.6).

**Docs kept in sync on the implementation branch:** the root `CLAUDE.md` Workspace paragraph (since moved into this RFC and
`decisions/agent_workspace_storage.md`; agent table Notes row), `src/agents/CLAUDE.md` (local tools list; Notes/Self-Reminders field), `docs/10_rfcs/README.md`
(status), `docs/05_building_blocks/file_storage/README.md` (`workspace:` refs), `docs/07_deployment/README.md` (§14
steps), decision record `decisions/agent_workspace_storage.md` (GCS, pull-only, origin over interactive-only writes,
D15–D17), and one "see also" line in `decisions/standing_directives.md`.

## 14. Config, deploy, owner steps

1. **Bucket**, in the service's region, with uniform access, public access prevention, soft delete off, versioning
   on and the §5.4 lifecycle:
   ```bash
   gcloud storage buckets create gs://<WORKSPACE_BUCKET> --project=<PROJECT_ID> --location=<REGION> \
     --uniform-bucket-level-access --public-access-prevention --soft-delete-duration=0
   gcloud storage buckets update gs://<WORKSPACE_BUCKET> --versioning
   gcloud storage buckets update gs://<WORKSPACE_BUCKET> --lifecycle-file=<path to the §5.4 JSON>
   ```
2. **IAM on that bucket only:** `roles/storage.objectUser` for the **main service's runtime service account**. The
   indexer, relay and Cloud Run Jobs get nothing.
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://<WORKSPACE_BUCKET> \
     --member=serviceAccount:<MAIN_SERVICE_RUNTIME_SA> --role=roles/storage.objectUser
   ```
3. **Secret:** `printf '%s' <WORKSPACE_BUCKET> | gcloud secrets create GCS_WORKSPACE_BUCKET --data-file=- --project=<PROJECT_ID>`
   (`printf`, not `echo`: a trailing newline broke eight secrets before). Grant the runtime SA
   `roles/secretmanager.secretAccessor` on it if the project does not grant it broadly.
4. **Deploy** (`make deploy`). No Firestore prompt change, no taxonomy change, no index, no TTL policy.
5. **Morning briefing conversion** (D12, acceptance A1), the first action after deploy. No skill is drafted.
6. Optional, recommended: delete the personal `drive-workspace` skill (`$skill delete drive-workspace`); it asks for
   drive-folder notes the workspace now replaces.

No bucket name, project, account or user id appears in tracked files; the `--set-secrets` entry names the secret.

## 15. Acceptance scenarios (owner-run after deploy)

**A1 — Morning briefing conversion (D12, first step).**
1. The owner copies the reminder's current instruction from the Cabinet reminder list (it shows `instruction`,
   `user_cabinet_app.py:633`; Smart's `active_reminders` shows only the name), saves it as a `.txt` file and uploads it
   in chat, asking the bot to move it into the workspace and shorten the reminder. An upload stays a reference in
   history (no content in history or consolidation), unlike pasted text.
2. The bot copies the upload to `projects/morning-briefing.md` (`workspace copy`, text into `projects/`, byte for
   byte; origin `chat`). Whether the file keeps a record of covered topics, and how, is the owner's wording in the
   file (e.g. a line "keep a short list of the topics of the last five issues at the end of this file; replace it each
   run"). The owner may edit the file through the bot.
3. The bot updates the reminder through `manage_self_reminders` with
   `context={"workspace_file": "projects/morning-briefing.md"}` and a query asking for a short instruction ("Morning
   briefing: follow the workspace file"). The appended system line makes NotesAgent call `update_self_reminder` and
   keep the instruction short. The receipt shows the workspace file; the Cabinet shows it.
4. Next fire: the alert carries `Workspace file: workspace:projects/morning-briefing.md`; the run views the file,
   follows it, and updates it if the file says so (origin `background:execute_reminder`, fire key in `history`).
5. If the file asks for a record of covered topics, the fire after that does not repeat the previous issue's topics.

**A2 — User-asked project file.** "Keep a file for the garden shed build" → `create projects/garden-shed.md`; in a new thread,
"where are we with the shed" → `view` before answering.
**A3 — Conflict.** Two channels edit the same file close together: the second gets the conflict text, re-views,
redoes the change; `history` shows both versions.
**A4 — Lost file.** The owner asks to delete `projects/morning-briefing.md`: the reply carries the reminder warning. At
the next fire the run reports "deleted … by chat" and offers restore; `restore` brings it back.
**A5 — Rename.** Renaming a file a skill names warns with the skill's name; a `view` of the old path says where it
went.
**A6 — Files.** A chat PDF is copied to `files/lease.pdf`; `open_file` with `workspace:files/lease.pdf` shows it;
`edit_image` works from a copied photo.
**A7 — Skill with state.** A skill drafted with `skill-creator` names `workspace:projects/<x>.md`; after
`$skill save`, using it creates the file on the first run and updates it on the next.
**A8 — Voice errand.** A `tell_alek` errand that updates a project file shows `voice:tell_alek` in `history`.

The same-fire re-run (B1) cannot be provoked safely live; unit tests cover it (§17). The implementer runs no live LLM
call and no GCS write.

## 16. Observability and the review criterion

**Log line** (one per command, written by `WorkspaceService` from inside the shielded call; the handler logs its own
refusals the same way):
`[Workspace] op=<cmd> user=<8> path=<path> origin=<origin> fire=<note_id>@<due_at>|- had_history=<0|1|-> bytes=<n>
gen=<g> result=ok|refused:<reason>|conflict|not_found|error ms=<n>`. `had_history` is set by `create` of a new path
(one versions list of that path). Failed mutations at WARNING, store errors at ERROR. Content is never logged.

**Data** for the review: a one-off script `scripts/validation/workspace_review.py` reads Cloud Logging directly with
a 28-day filter on `[Workspace]` (`google.cloud.logging`, or `gcloud logging read … --freshness=28d`), not
`make fetch-logs`, which keeps only the last K entries. It joins `prompt_content.tool_calls` (every `workspace` call
with its arguments) and the first turn's `request_text` to classify the trigger: a `[SELF-REMINDER]` with a
`Workspace file:` line → reminder; a preceding `use_skill` whose body names `workspace:` → skill; otherwise a user ask
or an instruction. `prompt_content` keeps 30 days, so the script runs by day 28.

**Review — 4 weeks after deploy.** Reported per trigger and per origin:

| Measure | Computed from | Bar | If missed |
|---------|---------------|-----|-----------|
| Reminder fires with a `workspace_file` whose run views it | reminder runs (traces) whose first request has a [SELF-REMINDER] with a Workspace file: line vs those with a workspace view call | ≥ 90 % | Sharpen the reminder's instruction or the field's wording. |
| Files recreated after a deletion without a restore or a user request | `op=create had_history=1` with a non-`chat` origin and no `op=restore` for the path | 0 | Code guard: `create` on a path with a deletion in history refuses outside chat. |
| Same-fire re-runs that ignored the earlier version | traces whose later turn shows the same-fire header and whose first write is not preceded by a view with generation | reported | Make the overwrite automatic when the fire key matches. |
| File growth | `bytes` of successive writes per path | reported per file | A file that only grows gets a pruning line in its instruction (D16: no mechanism). |
| Conflicts | `result=conflict` | each one read | Explain or fix. |
| Origin mix | `origin=` | reported | — |
| Junk memory facts about workspace operations | stored facts mentioning workspace paths | 0 | Apply the R9 line (§11.5). |

Usage volume is reported, not targeted: under D1 a workspace nobody asks for is the correct outcome, and the tool
costs one cached declaration.

## 17. Tests (slice 1)

New behaviour gets new tests; an existing test broken by the change goes to a reviewer (CLAUDE.md reviewer rule),
e.g. assertions on Smart's exact tool list or on NOTES having no `context_schemas`. `test_reminders_service.py`
stays untouched by construction (§8.3).

- **Domain:** path rule (valid/invalid, traversal, `:`, folders, prefixes), kinds, folder rules (`files/` copy-only),
  caps; `apply_str_replace` (one, none, several); windows, line cuts, `offset`/`limit` defaults for `""`, 0, negative;
  neutral header with and without fire key; the same-fire line with a prior generation and with none; listing with
  deleted count; `render_missing` for deleted-by, renamed-to and never-existed; history; `find_references` (skill
  bodies, `workspace_file`); `fire_key`; `parse_reminder_field` (`remove`; `""`/`none`/`null`/`n/a` any case → not
  provided); `visible_skill_names` versions.
- **Adapter wire tests** at the SDK boundary (stubbed `storage.Client`): key construction, metadata incl. `write_id`,
  `Content-Type`, `customTime` only under `scratch/`, `if_generation_match` on every mutation (0 for create); delete =
  patch then delete of the live object through a fresh blob, both with `if_generation_match=g`, and **no**
  generation-addressed delete; 412 read-back (own `write_id` → success, other → conflict; delete gone + own
  `delete_id` → success); exact-name filtering; `versions=True` listings with `timeDeleted`; `NotFound` →
  `FileNotFoundError`.
- **In-memory fake** of the port (`tests/unit/services/workspace_fakes.py`) with generations, metagenerations and
  noncurrent versions, plus a contract test shared with the wire semantics.
- **Service:** every command and error; `view` with `generation` (read-only, not recorded); same-fire detection;
  conflict on a concurrent write; `str_replace` re-apply once then conflict; rename rollback via
  `if_generation_match`; restore of a deleted file; copy from each source shape (text upload into `projects/`, binary
  only into `files/`) and refusals (`drive:`, `skill:`); `create`/`str_replace` refused under `files/`; caps (rename
  not checked); warnings; `had_history`.
- **Handler:** lock FIFO across a gather batch (view + str_replace + str_replace of one file); shield (cancel the
  outer mid-rename, by turn-budget cut and by message timeout: both steps land, session map updated, log line
  written); `create` over an existing file with and without a prior view; provider rotation builds a fresh session;
  no `history_context`; WARNING on failed mutations.
- **Smart:** tool declared on chat, notify and gateway messages when the service is set, also when the skill store
  fails; absent without the service; `run_origin` derivation incl. the fallback; `use_skill` loads a newer version
  over a stale visible body.
- **NotesAgent:** payload `workspace_file` bound to the one create/update, overriding LLM args; two create/update
  calls in the binding response → all refused, none executed; the guard is per execution (two concurrent executions on
  one instance do not share it); appended system line present only when the field is set; invalid path → failure
  before any LLM call; any folder accepted; `workspace_enabled=False` refuses a path (`remove` still clears); `remove` clears; receipt and
  list rendering.
- **Reminders:** alert line with and without `workspace_file` (and no other new line); `active_reminders` and Router
  dicts; Cabinet GET; Firestore adapter round trip incl. the clear flag and old documents without the field.
- **Origin:** `notify` sets `run_origin` from `origin_detail` and falls back to the kind; `run_fire_key`; the gateway
  sets `voice:*`; the worker passes `origin_detail` and the fire key at both call sites.
- **File pipeline:** `workspace:` through `resolve_content`/`resolve_bytes`; `_is_drive_ref` order; `open_file`
  refuses non-`files/` refs; `delete_file` refusal; `from_file: workspace:…` in `SkillService`, incl. its not-found
  message.
- **Manifest:** `test_manifest_prose_matches_tool_schema.py` passes with the new prose; the field name is unique.
- **Config/deploy:** key registered; `--set-secrets` pin; `google-cloud-storage` pin; wiring (container builds the
  service only when the key is set; Smart and NotesAgent receive their dependency and flag).
- **Provider wire tests:** Smart's full tool list incl. `workspace` serialises on each provider.
- `make check`.

## 18. Cost

- **Declaration:** one tool, ~1 k tokens, static, in the cached prefix (0.1× when cached). No per-request block.
- **Calls:** none unless a trigger asks. A file `view` is two GETs (metadata, media), plus one versions list when the
  fire key matches; a folder `view` is one versions list; a `create` of a new path adds the caps list and one versions
  list of the path.
- **Storage:** cents per month at D4 bounds.

## 19. Deletion, opt-out, multi-user

- **Multi-user.** Every object sits under `{user_id}/`, from the execution, never an argument; the path rule makes
  traversal impossible; only the adapter builds names.
- **The user's deletions** go through `delete` with history. "Delete permanently" is not a command; history ages out
  in 90 days.
- **Erasure** (no user-erasure path exists today): an owner command removes every generation, and with soft delete
  off nothing remains:
  ```bash
  gcloud storage rm --all-versions "gs://<WORKSPACE_BUCKET>/<USER_ID>/**"
  ```
- **No opt-out, no per-user flag (D11).** The workspace is the bot's working material, like conversation history,
  used only when pulled. A Cabinet page is the place for a toggle if one is ever wanted.

## 20. Alternatives rejected (short)

- **An index in every prompt + a `workspace` system skill + a write gate** (rev 1): see §0; D1.
- **Interactive-only writes** (rev 1): the briefing's file is written from a reminder; origin labels replace the ban.
- **A skill per scheduled task (`reminder_skill`)**: the catalog is in every prompt and skills are applied by
  meaning; a once-a-day task needs neither (D15).
- **A `state_file` with folder rules and "data, not instructions" framing**: no real protection, since the bot can
  schedule any instruction itself (D17); injection is class-B work.
- **A fixed state section in workspace files**: grows daily with nothing to prune it, and no schema foresees every
  case (D16). Fixed schemas live in the skill that owns them.
- **Firestore documents:** 1 MiB cap, no free history, poor for binaries.
- **A folder on the user's drive:** absent for unconnected users, mixes bot and user files, failed in practice.
- **Prose paths in the reminder instruction:** NotesAgent's LLM rewrites the instruction and loses them (D15).
- **grep / glob / diff:** tens of files, listed by `view`; `history` shows every version and `restore` brings one back.
- **Server-side copy and sequential version numbers** (R5, R6); **generation-addressed deletes** (permanent erase, §5.3).
- **A per-user feature flag:** D11.

### 20.1 Deferred extensions

- **A Cabinet workspace page** (view, history, restore, an opt-out toggle if wanted).
- **`body_from_file` on `draft_skill`**, if iterating long skill bodies in `scratch/` proves common.

## 21. Open questions

None needing the owner. Calls made in this revision and listed for review:

1. The fire key rides in its own context key `run_fire_key` beside `run_origin`; the alert does not render it (the
   view header does), so `build_reminder_alert` keeps its signature.
2. `workspace_file` need not exist at bind time, and any folder is accepted (D17); a file in `scratch/` that fires less
   than weekly will expire (§10 row 12).
3. `open_file` accepts `workspace:` refs only under `files/`; `FileConversionService` reads any folder, because
   `from_file: workspace:scratch/…` needs it.
4. `copy` puts a text upload into any folder (A1 needs it); binary stays in `files/`, and `files/` stays copy-only.
5. The Cabinet shows the field read-only in slice 1.
6. A delete's identity check uses its own `delete_id` rather than `deleted_by`: the origin label is not unique per
   operation.
