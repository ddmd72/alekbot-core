<!-- living-doc
covers:
  src/skills/
  src/domain/skill.py
  src/ports/skill_repository.py
  src/adapters/filesystem_skill_loader.py
  src/adapters/firestore_skill_repository.py
  src/services/skill_service.py
  src/services/skill_file_resolver.py
  src/infrastructure/skill_tools.py
  src/utils/skill_md.py
  scripts/skills/
  src/domain/workspace.py
  src/ports/workspace_store_port.py
  src/adapters/gcs_workspace_adapter.py
  src/services/workspace_service.py
  src/services/dry_run_workspace.py
  src/infrastructure/workspace_tools.py
  src/utils/safe_regex.py
  scripts/validation/workspace_review*.py
-->
# Agent Skills and Workspace

## Purpose

Two things Smart (the main chat agent) keeps for its own work, both **pulled, never pushed**.

A **skill** is a named procedure: a trigger line plus a body of instructions. The prompt carries only the catalog (name
and trigger); when a request matches, Smart calls `use_skill` and the body comes back as a tool result. Skills are
served by Smart itself as local tools, not by delegation to a specialist, because a procedure is not a specialist.
There are two origins: **system skills** ship in git and are read-only; **custom skills** belong to one user, live in
Firestore and are written only by the owner's confirmation.

The **workspace** is a set of versioned files Smart builds and follows between conversations and runs (project logs,
ledgers, reference notes, drafts, copies of uploaded files), behind one local tool, `workspace`. Nothing from it is put
into the prompt. Smart uses it when the user asks, when an instruction it follows (a skill, a reminder) names a file or
asks it to keep state, or when a firing reminder carries a file.

What they are not: neither stores facts about the user (memory does, and the bot never writes memory itself: see
[memory](../memory/README.md)); the workspace is not the user's drive ([user_drive](../user_drive/README.md)); a skill
is not a prompt token (tokens ship by token upload, skills by save or by deploy).

## Owned data

**Custom skills** — Firestore, `EnvironmentConfig.skills_collection`, document id `{user_id}:{name}`
(`src/adapters/firestore_skill_repository.py::FirestoreSkillRepository`).
- The *index document* denormalizes the current version (description, body, file manifest, `current`), so a listing
  needs one query and no subcollection reads. It is read on **every** Smart request.
- `versions/v<n>` is the immutable history. `files/<sha256>` holds a text file's content, content-addressed and shared
  by every version that lists the hash; it never carries `expires_at`.
- A skill holds text files only (the extensions in `SKILL_FILE_EXTENSIONS`), capped per file and per skill
  (`MAX_SKILL_FILE_BYTES`, `MAX_FILES_PER_SKILL`); a user holds at most `MAX_CUSTOM_SKILLS_PER_USER`. The body is capped
  by the Firestore document size, not by style (`MAX_SKILL_MD_BYTES`).

**Skill drafts** — `EnvironmentConfig.skill_drafts_collection`, id `{user_id}:{code}`, plus `draft_files/<sha256>` for
content the skill does not hold yet. Drafts and staged files carry `expires_at` and a Firestore TTL policy covers them
(`DRAFT_TTL`); the subcollection is named `draft_files`, never `files`, because TTL policies are keyed by
collection-group name across the database and one on `files` would delete saved skills' content. `get_draft`
re-checks the expiry because the TTL sweep lags.

**System skills** — files in `src/skills/smart/<name>/` (`SKILL.md` plus text files), loaded once at startup into
memory. Nothing is stored.

**Workspace files** — a dedicated, versioned GCS bucket, object name `{user_id}/{path}`, path under one of four
folders: `projects/`, `references/`, `scratch/` (all text) and `binary_files/` (binary only). Every version carries
object metadata: `origin` (who wrote it: `chat`, `background:<task or kind>`, `voice:<tool>`), `fire` (the reminder fire
key, when a reminder run wrote it), `write_id`, `restored_from`; a deleted version carries `deleted_by`, `deleted_fire`,
`renamed_to`, `delete_id`. Writer: `GcsWorkspaceAdapter`, only through `WorkspaceService`. Readers: the tool, the
`workspace:` ref pipeline, the review script. Caps (`MAX_FILES_PER_USER`, `MAX_BYTES_PER_USER`, `MAX_FILE_BYTES`,
`MAX_TEXT_FILE_BYTES`) are enforced in code and are soft: two concurrent creates can land one file over.
`scratch/` expiry, history age and version count are **bucket lifecycle rules, not code**; the rule set and the
bucket's other settings are in [`07_deployment/README.md`](../../07_deployment/README.md).

**Reminder binding** — one optional field, `workspace_file`, on a reminder (`src/domain/agent_note.py`), stored with the
note.

## Flows

### Skill: catalog, load, history

1. Each Smart request lists the user's skills (`src/agents/core/smart_response_agent.py::SmartResponseAgent._load_skills`
   → `SkillService.list_skills`): system skills merged with the user's custom ones, sorted by name. A store failure
   means no skills for that request, never a failed request.
2. `render_catalog` (`src/domain/skill.py`) produces the `available_skills` block and the prompt assembly places it
   before the standing directives (`PromptAssemblyService`, `skills_catalog`). The block is empty-omitted.
3. When a request matches a trigger, `use_skill` (`src/infrastructure/skill_tools.py::make_use_skill_handler`) returns a
   `[Skill "<name>" v<n>]` marked body plus a list of the skill's files as `skill:` refs. The handler is built **per
   execution attempt**, because provider rotation restarts the transcript.
4. The body is persisted into history by `ConversationHandler` through `fold_skill_contexts`
   (`src/domain/skill.py`): the raw block goes into the turn's `full_text`, a neutral stub into its summary. History
   tiering later drops `full_text` for old turns, so the body disappears from the prompt and the stub tells Smart it
   can load it again.
5. `visible_skill_names` reads the marked bodies still visible in model text; `use_skill` answers "already above" only
   when the visible body is the **current version**, otherwise it loads again.

Only `ConversationHandler` writes history, so a skill loaded in a reminder, a daily review or a voice call is used for
that run only.

### Skill: authoring and `$skill`

1. In an interactive chat turn only (`message.context["interactive_delivery"]`), Smart also gets `draft_skill`
   (`make_draft_skill_handler` → `SkillService.draft`). A reminder or background run has no authoring tool.
2. `draft` checks the text, applies the requested file changes onto the current version's files (untouched files are
   inherited), resolves `from_file` sources through `FileConversionService.resolve_bytes` (an upload, one of the
   owner's own skills' files, or a `workspace:` text file; never a delivered document and never a system skill's
   file), stores a draft under a random short code and returns a `skill_preview` delivery item carrying the rendered
   `SKILL.md` and the command `$skill save <code>`.
3. **Only the owner's pasted command saves it** (`ConversationHandler._handle_skill_save` → `SkillService.save_draft`
   → `SkillRepository.save_version`). `save_version` is one transaction: next version number, staged files copied into
   the skill's `files/`, inherited hashes checked present, the index document flipped, drafts of that name consumed.
   Afterwards a system-voice note and a model reply are appended to history so the next turn knows it happened.
4. `$skill list` and `$skill delete <name>` are the other sub-commands of `_handle_skill_command`.
5. `scripts/skills/seed_custom_skill.py` writes a skill for a user through the same `SkillService.save`; the personal
   text stays in the gitignored `scripts/memory/`. `scripts/skills/skill_trigger_eval.py` is the owner-run check that
   Smart loads a skill when it should (it spends API budget).

### Skill files

A skill's text files are read through the ordinary file pipeline as `skill:<name>/<path>` refs: `open_file` →
`FileConversionService` → `src/services/skill_file_resolver.py::SkillFileResolver.read`. A user's custom skill of that
name wins, and its content always comes from Firestore; otherwise the system bundle's in-memory contents answer.

### System skills

`src/adapters/filesystem_skill_loader.py::load_system_skill_bundle` is called from the service container at startup and
**raises** on any packaging fault (missing `SKILL.md`, name different from the folder, symlink, non-UTF-8 or oversized
file, empty result), so a malformed skill stops the boot instead of vanishing. It is a plain function, not a second
`SkillRepository`: system skills are read-only, and a write port's `save` and `delete` on them would be meaningless.

### Workspace: one command

1. Smart gets the `workspace` tool on **every** path where the service is configured, interactive or not, outside the
   skills branch, so a skills-store failure does not take the workspace away
   (`SmartResponseAgent`, `build_workspace_tool_declaration`, `make_workspace_handler`). No bucket configured: no tool,
   no `workspace:` refs, and the reminder field is refused.
2. The handler (`src/infrastructure/workspace_tools.py::make_workspace_handler`) holds the execution's session: the
   generations seen so far, and one lock every command takes first, so a batch of calls applies in the order the model
   wrote it. A mutation runs as a shielded task that records its generation and releases the lock itself, so a turn
   cut off by the clock cannot leave a half-applied write or a held lock.
3. `WorkspaceService.run` dispatches the commands (`COMMANDS` in `src/domain/workspace.py`: view, create, str_replace,
   insert, delete, rename, copy, history, restore, grep). It never raises for a rule, a conflict, a missing file or a
   store failure: each becomes a `Tool note:` result, and every command writes one `[Workspace]` log line (content is
   never logged).
4. `GcsWorkspaceAdapter` performs the I/O with a generation precondition on every mutation.

### Workspace: writing and growing files

- `create` over an existing path needs this execution to have viewed it; `str_replace` and `insert` at the end do not
  (they read the current generation themselves and retry once on a conflict). An `insert` at a middle line is valid only
  for a generation whose line numbers this execution saw, by a view or a grep.
- Files are meant to grow: `insert` at `INSERT_END` appends without a view, a negative `view` offset reads the tail, a
  long file viewed without a range returns an outline of its headings, write results state the new size, and the text
  cap's refusal names its fix (rename to an archive, start a new file).
- `delete` and `rename` are soft: the version stays restorable, and they warn when a loaded skill's body or a reminder
  in this run names the path (`find_references`).
- `copy` brings a chat upload, a delivered document, a drive file or another workspace path in; text goes to the text
  folders, binary only to `binary_files/`, byte for byte. A `skill:` source is refused: open it instead.
- `grep` is literal by default; regex mode uses RE2 (`src/utils/safe_regex.py::compile_line_matcher`) because a pattern
  written by a model must run in linear time on a one-CPU service. Scan size, pattern length, match count and output
  size are bounded.

### Workspace: reminders

1. `manage_self_reminders` accepts a `workspace_file` context field. `NotesAgent.execute` binds it **in code**
   (`_check_workspace_file`: the workspace must be configured, the path must parse; the file need not exist yet) because
   the agent's model rewrites reminder text and loses paths written in prose.
2. At fire time (`ExecuteReminderTask`) `build_reminder_alert` (`src/domain/reminder_alert.py`) adds one line naming the
   file, and `notify` is called with `origin_detail` and a `fire_key` from `fire_key(note_id, due_at)`: the same key on
   every Cloud Tasks re-run of one fire.
3. Smart's run carries `run_origin` and `run_fire_key` (`UserNotificationService.notify`; voice runs set their own origin
   in the Alek gateway agent); the handler stamps them on every write. A `view` whose file was written by an earlier
   attempt of the **same fire** says so and names the version from before the fire, because that attempt's reply may
   never have reached the user.

### Workspace: review and dry runs

`scripts/validation/workspace_review.py` reads the `[Workspace]` log lines and the matching `prompt_content` rows and
writes the review report (it names paths, so only under gitignored `scripts/memory/`). Harnesses that must not write
wrap the service in `src/services/dry_run_workspace.py::DryRunWorkspace`: `view` and `history` are real, every other
command answers "accepted" and does nothing.

## Invariants and traps

- **A skill is never saved by the model.** `draft_skill` stores a draft and delivers a code; only the owner's
  `$skill save` writes. Enforced by the tool's result text and by `tests/unit/infrastructure/test_skill_tools_draft.py`,
  `tests/unit/handlers/test_conversation_handler_skill_command.py`.
- **A skill is followed verbatim, so its text is checked at every write.** `SkillService._check` runs the security port
  on description, body and every written file, and refuses a new custom skill named like a system skill
  (`SkillNameReserved`). System skills skip this check: they arrive by pull request review.
  `tests/unit/services/test_skill_service.py`.
- **A skill's own files never expire.** TTL covers `skill_drafts` and `draft_files` only, and `save_version` writes a
  saved file's document with content, size and creation time, never `expires_at`. Exercised by the store kit
  `tests/unit/adapters/test_skill_store_kit.py`.
- **A stale visible body must not stand in for a newer version.** `tests/unit/infrastructure/test_use_skill_version_aware.py`,
  `tests/unit/domain/test_skill_visible_versions.py`.
- **The stub is neutral on purpose.** The voice companion reads model summaries and has no `use_skill`; a stub that
  told it to call a tool would mislead it. The marker regex is whole-line and strict so the stub never matches it.
- **Code-written text in a tool result is a `Tool note:`**, never the system's voice
  (`src/infrastructure/skill_tools.py`, `src/services/workspace_service.py`; repo-wide rule in `CLAUDE.md`).
- **The catalog is not cached.** Every Smart request reads all of the user's index documents, each of which holds a
  full body (up to about a megabyte, up to `MAX_CUSTOM_SKILLS_PER_USER` of them). A save is therefore visible on every
  instance at once; the cost is latency, and a field projection is the lever if the catalog grows.
- **A workspace delete must never address a generation.** In a versioned bucket a generation-addressed delete erases
  that version for good. The adapter patches the metadata of the live object, then deletes the live object through a
  *fresh* blob (the patched blob would send its generation), both under `if_generation_match`; if the patch landed and
  a write replaced the version before the delete, the stamp is taken off again.
  `tests/unit/adapters/test_gcs_workspace_adapter_sdk_wire.py`, `tests/unit/adapters/test_workspace_store_contract.py`.
- **A client retry of a landed write is success, not a conflict.** Both writes and deletes read back their own
  `write_id` / `delete_id` before reporting a precondition failure.
- **`rename` is read, put, delete, never a server-side copy** (a copy would carry the old `customTime` of a scratch
  file). A failed delete rolls the new copy back; if the rollback fails the result says both files exist.
- **The tenancy is the object-name prefix.** The user id comes from the execution, never a tool argument, and paths are
  validated in domain (`parse_workspace_path`) before they reach the adapter, so a path cannot leave the user's prefix.
  `tests/unit/domain/test_workspace_paths.py`.
- **Wrong store, right tool.** A `skill:` or `drive:` ref given to the workspace is refused with the tool to use
  instead; a bare file name is told to use `copy`.
- **A missing file named by a skill or a reminder is reported, never recreated silently.** `view` explains who deleted
  or renamed it and offers restore; a file that never existed may be created.
- **Origin labels, not write gates.** Background runs may write; the label, history and restore are the safety net.
  The workspace has no injection protection of its own: the bot can schedule any instruction by itself, so the
  defence is the system-turn fingerprint, not folder rules (owner ruling in the RFC).
- **Facts about the user never go in the workspace by design.** The consolidator reads every user-role message as the
  owner's words, and the bot never writes memory itself.
- **Dry-run stand-ins must not touch the bucket.** Local development shares the live bucket when the project is set
  (`GCS_WORKSPACE_BUCKET` is fetched from Secret Manager), which is why harnesses use `DryRunWorkspace`.
- **`binary_files/` holds binary only.** Text cannot be created there and a binary cannot be created anywhere else;
  `rename` cannot cross the boundary. `tests/unit/services/test_workspace_binary_folder.py`.
- **The grep scan has a size ceiling.** Over `GREP_MAX_SCAN_BYTES` of text in scope the command refuses and asks for a
  narrower path rather than reading the lot.

## Links

- [`AGENT_SKILLS_RFC.md`](../../10_rfcs/AGENT_SKILLS_RFC.md): catalog, history shape, authoring, text files (§15).
- [`AGENT_WORKSPACE_RFC.md`](../../10_rfcs/AGENT_WORKSPACE_RFC.md): the owner decisions D1–D18, storage, the tool
  contract, reminders, origin, review criterion.
- Decisions: [`agent_skills.md`](../../04_solution_strategy/decisions/agent_skills.md),
  [`agent_workspace_storage.md`](../../04_solution_strategy/decisions/agent_workspace_storage.md).
- Neighbours: [file_conversion](../file_conversion/README.md) (`skill:` and `workspace:` refs through `open_file`),
  [memory](../memory/README.md), [prompt_design_system_v3](../prompt_design_system_v3/README.md) (where the catalog
  sits), [`SYSTEM_TURN_FINGERPRINT_RFC.md`](../../10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md).
- Deployment: bucket creation, lifecycle, IAM, erasure in [`07_deployment/README.md`](../../07_deployment/README.md);
  drafts' TTL policy in the same file.

## Where to start reading

1. `src/infrastructure/skill_tools.py::make_use_skill_handler`: what a load returns and when it short-circuits.
2. `src/services/skill_service.py::SkillService.draft`: how a proposal becomes a draft with files.
3. `src/adapters/firestore_skill_repository.py::FirestoreSkillRepository.save_version`: the one transaction behind a save.
4. `src/domain/skill.py::fold_skill_contexts`: how a body lives in history and becomes a stub.
5. `src/infrastructure/workspace_tools.py::make_workspace_handler`: the per-execution session, lock and shielded writes.
6. `src/services/workspace_service.py::WorkspaceService.run`: the commands and their refusals.
7. `src/adapters/gcs_workspace_adapter.py::GcsWorkspaceAdapter._delete_sync`: the soft delete in a versioned bucket.
