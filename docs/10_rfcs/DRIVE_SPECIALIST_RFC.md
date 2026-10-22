# RFC: The user drive through one specialist (`work_with_drive_files`)

**Status:** Proposed, rev 2 (2026-10-22): the rulings on the three rev 1 reviews applied (§0). Nothing is built.
**Related:** `USER_DRIVE_RFC.md` (Step 1 operations, `drive:` refs, the mutation lock and shield §4.13, receipts §4.10),
`USER_DRIVE_INDEXING_RFC.md` (Search v2 §25, the `drive-search` skill D32, inaccessible files §26.2, transcripts §27),
`DRIVE_FILE_NOTES_RFC.md` rev 3.2 (notes; the notes change amends D4, D12 and N2 there, §9), `AGENT_SKILLS_RFC.md`,
`TASKS_AGENT_RFC.md`, `PROACTIVE_SELF_REMINDERS_RFC.md` (the two specialists this one copies).
**Research behind it** (private, gitignored, not quoted for owner data): `scripts/memory/drive_specialist_sketch.md`,
`scripts/memory/drive_specialist_research/01`–`08`, the reviews `rfc_review_1_parity.md` (R1), `rfc_review_2_tool_shape.md`
(R2), `rfc_review_3_feasibility.md` (R3) and the binding rulings `rfc_rev1_rulings.md`. Where this RFC differs from a
report, the owner's ruling of 2026-10-22 or the arbiter decided it.
**Branch base:** `main` at `1e553d83` (after PR #132). Line references are to that commit; re-check them when building.
**Where the code and this text differ once it is built, the code is the truth.**

---

## 0. Changes: rev 1 → rev 2 (what changed, which review asked)

| # | Change | Asked by |
|---|---|---|
| 1 | Note tools: one tool per edit kind (`add_to_note`, `replace_note_text` only after `get_note`, `set_note_type` / `clear_note_type`, `add_life_areas` / `remove_life_areas`); classifier rules in the parameter descriptions; `item_ref`; `create_note.text` optional (§8.2, §9.2) | R2 C1, C2, I3, M6, M10 |
| 2 | Drive tool parameters described for the specialist (no Smart-era texts); `folder` takes a path or a `drive:` ref everywhere, "created if missing" named; `search_deleted_recordings_and_notes`; `replace_file_content(target_ref, new_content_ref)`; `report_to_orchestrator` in the tool list; tools renamed `open_drive_file` / `delete_drive_item`; video side effect named (§8.1) | R2 I1, I2, I4, I8, I10, M1, M5; R1 F9, F14; R3 M8 |
| 3 | `text_to_write` → `file_texts`, a list of items bound by code; batches of composed texts work as today; status `not_possible` (§4.2, §7.2) | R1 F1; R2 I5, I7; R3 M5 |
| 4 | Every read block reaches Smart verbatim, in run order; no `attach`, no default selection (§6) | R1 F2; R2 M2 |
| 5 | Move and rename: one tool, one `svc.move` (§8.1) | R1 F3 |
| 6 | Tool descriptions name today's accepted sources (`workspace:`, `skill:`, `drive:`, chat files, delivered keys); Smart's description asks for delivered document keys (§4.1, §8.1) | R1 F4; R2 I6 |
| 7 | Not connected, not configured, indexing off and rebuilding: code always delivers today's text (§7.2) | R1 F5 |
| 8 | No notes sentence on Smart until notes ship (§4.1, §9.5) | R1 F7 |
| 9 | Smart-side skill rules (excerpt, 90 % value rule, "may be out of date", scan, versions, document vs memory, all/every) reach Smart as code-rendered reading lines with stated triggers (§7.3) | R1 F6 |
| 10 | Deadlines: the engine runs as a task under the soft deadline, cancelled on timeout or `still_running`; reads and LLM calls clamped; the caller's budget respected on background runs (`run_deadline`); status `unfinished`; 6 turns / 150 s declared (§5.6) | R3 I1, I2, I6, M9; R1 F10, F12 |
| 11 | Rollback: the owner's Firestore texts are rewritten intent-free (work before and after a revert), backed up first; workspace and prompt overrides scanned; §13 order restore → deploy → cache reset; a removed-intent line in the coordinator for one release (§10, §13) | R3 C1, I7; R1 F8, F11 |
| 12 | Migration green at every step: FileManagement keeps the internals tests touch; late-failure notices re-keyed on an operation enum; `smart_dry_run.py` and its 9 tests fixed in the cut (§10) | R3 I3, I4, I5 |
| 13 | Notes core: `update` re-embeds on a re-merge, a same-content re-run is a no-op; one shared `DriveOperations` per user in a factory slot (§5.5, §9.2) | R3 I9, I10 |
| 14 | A latency bar on drive-turn time, before vs after (§12) | R3 I8 |
| 15 | Minors applied: status precedence (R1 F12); `user_message` labelled by origin, `file_data` parts filtered (R1 F13, R3 M4); partial ambiguity (R1 F15); full list of new strings, blocks keep their own reminder (R1 F16, R3 M2); prefetch caller named (R1 F17); safety wording (R1 F18, R3 M3); no dedup of `drive_context`, `ToolResult.failed` named (R1 F19); one op line per mutating call (R1 F20); success exceptions named (R3 M1); `include_directives=False` (R3 M6); the gate read and the notes switch injected (R3 M7); cost wording (R3 M10); the notes branch is already wired (R3 M11); token frontmatter (R3 M12); plain-text final reply (R3 M13); note listing out of scope (R2 M9); the per-op log line kept (R1 §4) | as named |

**Not applied, with the reason:**

| Item | Reason |
|---|---|
| R2 M3: a truncated listing makes a batch `partial` | Behaviour change: today the truncation line reaches Smart and nothing marks it partial. It still reaches Smart verbatim |
| R2 M4: keep the old extension when `new_name` has none | Behaviour change: today the name is used as given. The parameter description says "extension included" |
| R2 M7: "restoring is done in the recycle bin" on Smart | A new rule; a restore commission ends as `not_possible` with the specialist's note |
| R2 M8: rename `erase_note` | Ruling 1 names `erase_note`; its description says nothing is erased now |
| R2 I9: "put a FILE anywhere" vs the workspace | Not ruled; today's wording is kept verbatim |
| R2 I6, second half: "an email attachment cannot be saved" | Not ruled; stays a backlog item (Appendix A) |
| R3 M14: the base is behind `main` | `1e553d83` is `main` after PR #132, newer than `4439d181` |

---

## 1. Context

Smart, the orchestrator, carries the whole drive in its own prompt. Measured over the 30 days of `prompt_content`
(research 01, 2026-09-22 to 2026-10-22, production turns only):

| Fact | Number |
|---|---|
| Fixed drive surface on **every** Smart call: 8 drive intents (≈ 945 tok), their 13 context fields (≈ 816), `open_file`/`delete_file` (≈ 161), the `drive_specialist` protocol block (≈ 235), the drive lines of the skill catalogue (40–145) | **≈ 2 200 tokens** |
| `drive-search` skill body loaded before a search, kept in history `full_text` until tiering, often in several copies | ≈ 1 100–1 500 tokens per copy; 2–4k per Smart call on average, also on days with no drive work |
| Share of Smart turns that use the drive since it went live (10-10) | **52 %** (80 of 155) |
| Invented `drive:` refs, field confusions, model-made rejections | **0** (123 of 123 refs copied correctly; all 6 rejections were code gaps since fixed) |
| Follow-up searches after a search that had results | 57 in 43 search turns: 21 script or language switches, 16 "verify / confirm / repeat" re-runs |
| Drive turns with 10 or more drive calls | 9 (worst: 25 calls, no stopping) |
| Smart input tokens in drive turns spent on rounds whose only job was to read drive-only results | 43 % |

The case is **attention and rounds, not errors and not dollars**: the fixed drive tokens are mostly billed at
cache-read rates. The waste is re-searches and re-opens, each paid as a full Smart round (30–40k input tokens). Smart
reads four layers of drive rules (the protocol block, the system skill, the owner's custom drive skills, the intent
prose) and learns search semantics by trial.

## 2. Goals and non-goals

### 2.1 Goals
1. **No behaviour change.** The drive does exactly what it does today: the same operations (batches included), the
   same rules, the same texts, the same rights on background runs (reminders, the daily review). The only changes are
   the ones the move itself requires (§2.3).
2. Smart loses the 8 drive intents, their fields, the drive rules of the protocol block and the `drive-search` skill.
   It keeps **one** commissioning intent, `work_with_drive_files`, with a short description and one field.
3. One LLM specialist, `DriveAgent`, does the drive work in its own loop through small CRUD-shaped tools that call
   today's code.
4. Drive file notes (not merged yet) are built inside the specialist, on the frozen notes core, in their own later
   change (§9).
5. A clean replacement: one change removes the old path. Rollback is `git revert` plus the prompt restore (§13).

### 2.2 Non-goals (proposals from the research that change behaviour; dropped by the owner, 2026-10-22)

| Dropped | From |
|---|---|
| Background runs lose delete, replace, move and rename | 06 §2.3 |
| A code-bound commission field `changes` that whitelists the kinds of change | 06 §3.3 |
| A code rule that a change needs one explicit ref or exactly one search candidate (no batches over a named set) | 06 §1, sketch §5 |
| "The specialist never picks among candidates" | 06 §1.2, 03 A.3 |
| `outcome_unknown` semantics for a provider timeout inside a mutation; today's text stays | 05 §7.1 |
| A known-refs rule (a ref must have been shown in this run), run-local short handles | 03 §5, A.3 |
| Per-run write caps, tool-call caps, a 3-search cap per commission | 06 §5, 01 §5.2 |
| An idempotency memo against whole-turn re-runs | 06 §4.2, 03 §3.3 |
| A character cap on opened text, an answer budget, vision part caps, a default selection of blocks | 04 §4.1, §2.2 |
| New statuses that reword today's messages (retry-or-not lines, "do not use the drive again" where today says nothing) | 05 §3 |
| A "card by ref" operation; a note listing | 01 §5.4; R2 M9 |
| Moving `open_file` / `delete_file` (GCS chat files) into the specialist | sketch §7 |
| A per-user switch and a side-by-side period | 07 §4 |

Items found on the way that are real but separate are listed in Appendix A as backlog.

### 2.3 What the move itself requires (in scope)
1. Every opened document, search result, listing and vision file reaches Smart **verbatim**, in the order it was
   produced; none passes through the specialist's model as text Smart reads (§6).
2. The answer to Smart has **one shape**: status, a "What to do" line, the verbatim content. Code builds it from
   today's messages, which are kept as they are and only collected into one place (§7). New strings are listed in §7.5.
3. The engine's `Correct your input and try again.` suffix (`delegation_engine.py:741`) never reaches a drive answer.
   The only exits that still reach Smart as a failure: a missing prompt (before any LLM call), a circuit-breaker
   refusal, an external cancellation, and a hard timeout the soft deadline failed to prevent (§5.6).
4. `drive_context` history refs keep follow-ups working, in today's shape (§7.4).
5. The user's own words reach the specialist: drive search keys are strict AND on them, and the protocol says
   "query always English" (§4.3, §5.3).
6. Today's texts that name an intent or field Smart no longer has are reworded: for Smart, the name becomes plain
   words; in parameter descriptions and errors the specialist acts on, it becomes the tool's parameter name (§7.6).
7. One commission runs at most **6 model turns and 150 s** (less when the caller's budget is shorter); Smart may
   commission again, as it may call again today. Mutation waits, reads and LLM calls are clamped to that deadline
   (§5.6).
8. A `still_running` mutation ends the commission: a later mutation would wait behind the per-user lock anyway, and
   Smart gets today's still-running text at once (§5.4).
9. Who asks "which file?": the specialist cannot ask the user, so it returns a question to Smart where Smart would
   have asked (`ambiguous`, §7.2).

---

## 3. Design in one screen

```
Smart ──delegate_to_specialist(work_with_drive_files, query, {file_texts?})──▶ DriveAgent (BALANCED, gpt-6-luna)
                                                                               │ DelegationEngine task, local tools only,
                                                                               │ under the soft deadline
                                                                               ▼
     search_drive · search_deleted_recordings_and_notes · list_folder · open_drive_file · get_recording_link ·
     copy_file_to_drive · save_text_file · create_folder · move_file · append_to_file · replace_file_content ·
     delete_drive_item · report_to_orchestrator   (+ 11 note tools in the notes change)
                                                                               │ thin handlers, one call each
                                                                               ▼
                 DriveOperations  (FileManagementAgent's drive code: handlers, error mapping, per-user lock, shield,
                                   still-running, receipts, late-failure notices; one instance per user)
                                                                               │
                                  UserDriveService · DriveSearchService · FileConversionService · DriveNoteService
◀── one AgentResponse.success: "Drive result: <status>" + "What to do: …" + op lines + reading lines + every block
    verbatim; file_data list (vision); history_context {drive_context}; delivery_items
```

| What | Copied from | Why |
|---|---|---|
| Natural-language commission, one optional field, "give it the full picture" | `TasksAgent` (`manage_user_tasks`) | Smart writes rich commissions and cannot get a field wrong |
| Search before mutate, then act by ref | `TasksAgent` | The ref is today's `drive:<id>`, shown in every card and label. Smart copied 123 of 123 correctly, so no second short-ref scheme (it would need a second rendering of every card) |
| Multi-step loop: read, read, write, answer | `TasksAgent` | The drive needs "find, then move" and "open these five" in one commission |
| Receipts written by code | `NotesAgent`, today's FileManagement | Unchanged: delete and replace post `notify_raw` receipts |
| Strict code validation with correctable tool errors; content bound by code, never retyped | `NotesAgent` (`workspace_file`) | `file_texts` items are bound by code like `workspace_file` |
| Fail fast on a missing prompt | `NotesAgent` (not `TasksAgent`, which degrades to an empty prompt) | Root `CLAUDE.md`: no fallback prompts |
| Loop engine | `DelegationEngine` with local tools (not a hand-rolled loop like either) | It already folds `history_context`, `delivery_items` and `file_data`, keeps the 1:1 tool-result guard and the spans (`src/agents/CLAUDE.md`: do not hand-roll) |

**Why local CRUD tools, not today's 8 intents dispatched through the coordinator** (07 proposed the latter, 03 the
former). The 8 intents are shaped for Smart, with mode flags: `save` takes `file_ref` XOR `text`+`name`, `update` takes
`append_text` XOR `source_ref`, `open` takes `original`, `search` takes `drive_deleted`. That is the shape
`feedback_llm_tool_shape_crud` forbids for a model's tools. Through the coordinator each operation also comes back as
a rendered `AgentResponse`, whose failure branch adds the "Correct your input" suffix and drops receipts and history
refs, while the pass-through and the status need the typed outcome. And the 8 would stay registered intents that any
orchestrator can still reach, since the coordinator does not check what a caller was offered. Local tools whose
handlers call `DriveOperations` (FileManagement's own handler bodies, moved) avoid all three. The one cost 07 named,
that the dry-run harnesses stub writes at `DelegationEngine.dispatch`, is paid once with a dry-run wrapper of
`DriveOperations` (§12), the `DryRunWorkspace` precedent.

---

## 4. What Smart sees

### 4.1 The intent (`src/infrastructure/agent_manifest.py`, new descriptor `DRIVE`)

```python
Intent.WORK_WITH_DRIVE_FILES: (
    "The user's drive: their own long-term files (documents, scans, photos, recordings). "
    "Finding files is part of it: by name (exact, partial, transliterated or misspelled), by what a file is or what is "
    "written inside (a subject, a phrase, a number, an amount), type, date, folder, photo details and place; deleted "
    "files only when the user asks about them (transcripts of deleted recordings and the notes on deleted files). Use it "
    "when the answer may be in the user's files, and before saying a file is not there. "
    "It also opens files (a recording opens as its transcript; the recording itself comes as a link when the user asks "
    "for it), lists folders, keeps a copy of a chat file, a document you delivered, a workspace copy or a skill file, "
    "saves a text you composed, moves and renames, creates folders, appends text to a text file, replaces a file's "
    "content with another file (the user gets a notice; rewriting with new text is not available) and deletes "
    "(recoverable from its recycle bin; the user gets a notice). "
    "Chat files and documents you produce are temporary: to remember, save, keep or put a FILE anywhere, use this "
    "intent (facts from a document go to memory only through propose_memory_facts, when the user asks). "
    "Write query as a natural-language instruction with the full picture: what the user wants, the words to find as "
    "the user wrote them, every drive: ref, [File: …] label and delivered document key it points at, and the facts "
    "from this conversation it needs. "
    'Texts to save or append: context={"file_texts": [<one item per file>]}. '
    "Returns a status and a 'What to do:' line (follow it), then every result verbatim; images and PDFs come attached."
),
```

About 1 900 characters (≈ 475 tokens), replacing ≈ 1 760 tokens of intent descriptions and fields. Every sentence is a
rule or capability Smart holds today, moved from the 8 descriptions (`agent_manifest.py:546-613`); the procedural parts
(Inbox default, never-overwrite, extensions, `_Transcripts`) move to the specialist. One paragraph, because
`_build_delegate_tool_declaration` renders each intent as one `- name: description` line (`base_agent.py:789`). **No
notes sentence:** the notes change adds it (§9.5).

### 4.2 The one field

```python
context_schemas={Intent.WORK_WITH_DRIVE_FILES: {
    "file_texts": {
        "type": "array",
        "description": (
            "work_with_drive_files only: one item per text to write, each exactly as it is to be written (text you "
            "composed or the user gave): a new text file as {\"name\": \"<file name with extension>\", \"text\": "
            "\"<full content>\"}, or text to add at the end of a text file as {\"target\": \"<drive: ref of that "
            "file>\", \"text\": \"<text to add>\"}. Leave it out when nothing is written."
        ),
        "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "target": {"type": "string"}, "text": {"type": "string"}}},
    },
}},
```

- `TasksAgent`'s one field is `background`. Here background goes into the query; the one thing that must not pass
  through a second model is the content to write: today's `text` (save) and `append_text` (update), one item each, as
  many as today's batch of calls. The specialist has no content parameter: code binds the items (§8.1).
- The state block numbers the items `t1`, `t2`, … with name or target, length and first line (§5.3); a tool names its
  item, and code writes that item's text. Items are written in emission order under the ordering lock, as today's
  batch.
- **Accept liberally:** a single string becomes one unnamed item; items with `text`/`append_text` keys, a stray
  `context.file_ref` or `context.folder` (the union schema still shows `file_ref` through `open_file`) are taken as
  part of the commission, never rejected (`decisions/mcp_tool_schema_liberal_input.md`).
- The name is unique in the union schema, so its description is not shadowed (`base_agent.py:796`). The prose names
  only `file_texts`, so `test_manifest_prose_matches_tool_schema.py` passes (no nested keys in the prose).

### 4.3 The protocol (`prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy`; the live copy, not
`firestore_utils/downloads/`, which is stale)

Line 26, edited in place, so the exception sits where the rule sits (research 03 A.2: with the exception only in the
description, the generic rule wins: 15 of 16 Tasks commissions were English):
```
query_language: "Always English — regardless of the user's input language — except the words to find on the drive in work_with_drive_files: quote them as the user wrote them."
```

Lines 111–119, the block:
```
drive_specialist {
    intent: "work_with_drive_files"

    knowledge_base: <today's line, verbatim>

    untrusted: <today's line, verbatim>
}
```
`applies_to` (names `search_files_in_drive`) and `skill` (the `drive-search` pointer) go. `knowledge_base` and
`untrusted` stay word for word. The token is shared with Quick (`profiles/quick.yaml:30`), which gets the same intent.

### 4.4 Other Smart-facing texts (wording required by the move)

| Where | Today | New |
|---|---|---|
| `DELETE_FILE` description (`agent_manifest.py:556`), `file_management_agent.py:177` | "deleted with delete_file_from_drive instead" | "deleted through work_with_drive_files instead" |
| `domain/workspace.py:75`, `services/workspace_service.py:451-453` | "open it with open_file_from_drive" | "open it through work_with_drive_files" |
| `OPEN_FILE` description | unchanged: it still opens a `drive:` ref leniently (`file_management_agent.py:169`), through `DriveOperations` | — |
| Coordinator, for one release | an unknown intent gets "No agent registered…" | the 8 removed names get one fixed line: "This operation is now done through work_with_drive_files: describe the request there." (old skill bodies stay in history `full_text` for up to 5 turns) |

---

## 5. The specialist

### 5.1 Identity and configuration

| Item | Value |
|---|---|
| Class, file | `DriveAgent(BaseAgent)`, `src/agents/drive_agent.py` |
| Descriptor | `agent_id="drive_agent"`, `agent_type="drive"`, `capabilities={WORK_WITH_DRIVE_FILES: SYNC}`, `allowed_intents=frozenset()`, `prefetch_file_ref=False` (the coordinator must not download a `file_ref` first, as for FileManagement), `internal=False` |
| Tier, model | **BALANCED, OpenAI `gpt-6-luna`, thinking `medium`** (`_DEFAULT_AGENT_TIERS["drive"]`, `agent_thinking["drive"]`). Luna for latency: this hop sits inside a chat turn; sol's Smart p50 was 56 s against luna's 35 s (`decisions/model_refresh_2026_10.md`) |
| Strategy | `STRATEGIES["drive"]`: default `openai`, allowed `openai`/`claude`/`gemini`, `native_tools` required, fallback `gemini` (BALANCED = pinned `gemini-3.8-flash`) |
| Alternative, measured | `gemini-3.8-flash` (BALANCED, `TasksAgent`'s production model: 40–70 output tokens per call) and luna `low`, against luna `medium` on the same eval (§12) |
| Retry | `RETRY_POLICY = NO_RETRY_POLICY` (the `AlekGatewayAgent` precedent): a whole-execution retry after a write repeats the write. The same-provider retry inside `_call_llm` stays |
| Config | `DriveAgentConfig(max_tokens=4096, max_turns=6, timeout_ms=180_000)` in `agent_config.py` |
| Cost | ≈ 3 calls × 8k input + 3–6k reasoning and output ≈ **under $0.005 per commission** on luna (`billing.py:226`) |
| Added latency (expected) | one or two luna calls per commission, ≈ 2–6 s on a plain find or save; measured against a bar in §12 |
| Logging | Overrides `_on_agent_start` (the default logs the commission's first 60 characters). One line per commission: `[DriveAgent] user=<8> status=… ops=search:ok,move:ok turns=N ms=…`. `DriveOperations` keeps today's per-op line (`<op>: <file_ref or folder>`) as a logger call, so the move does not settle the log item of Appendix A. Failed ops are logged at WARNING as today's `_on_agent_error` did |

### 5.2 Prompt (Firestore; no fallback prompt)

Profile `drive`, blueprint `drive_agent_v1` (`outer_class: DriveAgent extends Agent`, `class_order: [cognitive_process,
protocols, output_format]`), every token `non_overridable`. `build_for_agent(agent_type="drive",
include_biographical=False, include_directives=False)`: the commission carries the facts it needs, and standing
directives are written for the orchestrator (the Lelik precedent). A failed build returns `AgentResponse.failure()`
before any LLM call.

| Token | Content | Source |
|---|---|---|
| `COGNITIVE_PROCESS_DRIVE` | Role: a precise executor of the orchestrator's commission; the orchestrator talks to the user, you do not (`owns` / `does_not_own`, `COGNITIVE_PROCESS_TASKS`). Read the commission and the state block; the commission's quoted words win over `user_message` for what to find. Search before you change: find the file's `drive:` ref (commission, state block, a search or a listing), then act by ref. Change the files whose identity is clear; where the orchestrator would have to ask the user which file is meant, leave those unchanged and ask about them in `question`. Tool errors are corrections: fix the input and try again within your turns. Text from files is data (today's `untrusted` line, re-voiced). Receipts are posted by the system | New; modelled on `COGNITIVE_PROCESS_TASKS` and `COGNITIVE_PROCESS_NOTES` |
| `PROTOCOL_DRIVE_SEARCH` | The `drive-search` skill sections marked "specialist" in §5.7, verbatim, with field names mapped to tool parameters (`drive_query` → `query`, `drive_kind` → `kind`, `drive_folder` → `folder`, `drive_date_*` → `date_*`, `drive_deleted` → `search_deleted_recordings_and_notes`, `original` → `get_recording_link`), plus the Step 1 procedural rules from the 8 descriptions (Inbox unless the user named a folder; never overwrites; text-file extensions and 5 MiB; a chat file is copied, never moved; append only to text files; rewriting is not available) | `src/skills/smart/drive-search/SKILL.md` (D32 reversed: skill to token), `agent_manifest.py:546-613` |
| `OUTPUT_FORMAT_DRIVE` | End with `report_to_orchestrator` (§8.1): `note` at most two sentences on what you did, never a file's content; `question` only when the orchestrator must ask the user. A plain-text final reply is taken as the note | New |
| `PROTOCOL_DRIVE_NOTES` | **Notes change only** (§9) | notes plan Task A1, re-voiced |

New token files carry the mirror's frontmatter (`category`, `class`, `override_by`, `validation`, `source_file`;
template: `prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy:1-21`).

### 5.3 The state block (rendered by code, after `PROMPT_CACHE_BOUNDARY`)

```
drive_state {
  drive: connected | reconnect needed | not connected | not configured
  search: on | off (drive indexing not turned on; the deleted recordings and notes search still works) | rebuilding | still building
  now: <date time> <user timezone>
  user_message (<origin>): "<text parts of the turn's message, verbatim>"
  attached_now: [File: "<name>" (<size>)] …
  file_texts: t1 name "<name>" <N> characters, first line "<…>"; t2 target drive:<id> <N> characters, …
}
```

| Line | Source | Why |
|---|---|---|
| `drive`, `search` | `UserDrivePort.connection`, `UserBotConfig.drive_indexing_enabled`, and a new public read `DriveSearchService.search_state(user_id)` over the same sync-state gate a search reads today | The model knows up front what can work; code delivers today's text for these states anyway (§7.2) |
| `now` | the coordinator's `[Mon DD, HH:MM UTC]` prefix and `UserBotConfig.timezone` | Date filters in the user's timezone |
| `user_message (<origin>)` | text parts of `context["current_message_parts"]` without `file_data` parts (`[File: …]` labels are filtered). The origin label is `chat`, `call (Lelik's words)` on `ask_alek`/`tell_alek` (`alek_gateway_agent.py:101`), `background: <task type>` on notify paths, `fallback` on the Quick fallback (`agent_fallback_service.py:126`) | **The user's own words** on chat turns, required by strict-AND search keys (§2.3.5). Off chat the label says whose words they are; on a call they are not the owner's spoken words |
| `attached_now` | `MessagePart.file_data` of the same parts | "Save this" without Smart copying the ref |
| `file_texts` | the commission's field (§4.2) | The model picks the tool and the item; it never types the content |

The specialist does not read session history. Smart holds `drive_context` and passes refs in the commission
(research 04 §5): no new plumbing.

### 5.4 The loop

`DriveAgent.execute` starts `DelegationEngine.execute(local_tools=handlers, terminal_tool="report_to_orchestrator",
max_turns=6, use_turn_clock=False, allow_delegation=False)` **as a task** and waits on it (§5.6). Handlers are built per
execution (the `workspace_tools.py` pattern) and write every outcome into the per-execution collector (§6).

| Rule | Mechanism |
|---|---|
| No delegation | `allowed_intents=frozenset()`, no `delegate_to_specialist` declared. **Engine delta:** `execute(..., allow_delegation: bool = True)`; `False` drops `DELEGATE_TOOL_NAME` from `declared` (`delegation_engine.py:595`), so a stray call gets the existing unknown-tool answer |
| One call for a plain read | The model may emit `report_to_orchestrator` with `search_drive` or `open_drive_file` in the same turn; the engine runs siblings first (`:339`) |
| Batch order | Every mutating handler first awaits a per-execution ordering lock: `gather` creates the tasks in emission order and `asyncio.Lock` is FIFO (the `drive_note_tools.py` pattern) |
| Per-user mutation lock, shield, still-running, late-failure notices | Unchanged, on the per-user `DriveOperations` (§5.5): a shielded task outlives a cancelled run |
| `still_running` ends the commission | The collector sets an `asyncio.Event`; `DriveAgent` waits on `{engine task, event}` with `FIRST_COMPLETED`, cancels the engine task and builds the answer. No engine stop flag |
| Calls emitted on the last turn | They run (mutations included); the model never sees their results, the ledger reports them |
| Deadline, engine exception, turns exhausted | Code builds the answer from the collector; status `unfinished` if an operation was cut or none could finish (§7.2) |
| Any exit | `AgentResponse.success` with the code-built answer (§7), so the suffix never applies and receipts and refs always ride; exceptions in §2.3.3 |

### 5.5 `DriveOperations` (`src/infrastructure/drive_operations.py`)

`FileManagementAgent`'s drive code moved: `_drive_call` (the error mapping and the mutation runner), `_search_drive`,
`_list_drive`, `_open_drive` / `_open_drive_item` and the `_fetch` fallback for a non-drive ref, `_save_to_drive` /
`_store`, `_move_in_drive`, create folder, `update_file_in_drive`'s two modes, delete, `_receipt`,
`_arm_late_failure_notice`, `_on_mutation_done`, `_mirror_refused_line`, `_access_note`.

| Rule | Detail |
|---|---|
| Typed outcomes | Each public method returns today's text (success or error), the drive items touched, any `file_data`, and an outcome class (§7.2). The exception chain of `_drive_call` maps to the class and keeps today's text |
| Operation enum | `DriveOp` (`search`, `list`, `open`, `copy`, `save_text`, `create_folder`, `move`, `append`, `replace`, `delete`). Late-failure notices are keyed on it (`delete`, `replace`), not on `Intent` constants (`file_management_agent.py:563-578`), so they keep firing after the cut |
| Mutation wait per call | `wait_s` is a call argument (today's class constant `MUTATION_WAIT_S`, `:125`); callers pass it |
| Hooks | `_on_agent_start` / `_on_agent_success` / `_on_agent_error` become logger calls with the same content |
| Why here | REQ-ARCH-24 forbids one agent importing a sibling agent module; precedent `workspace_tools.py`, `drive_note_tools.py`. Services are constructor-injected; REQ-ARCH-14 allows `TYPE_CHECKING` imports |
| One instance per user | The factory memoises it in the per-user context (`_UserContext`); `_build_drive` and `_build_file_management` (`user_agent_factory.py:855-873`) both read that slot. The lock, `_shielded`, `_armed`, `_targets`, `_notices` live on it |
| Unchanged guarantees | The `_Transcripts` guard, root protection, never-overwrite, append-only and the move rules stay in `UserDriveService`; `DriveOperations` imports no `UserDrivePort` and no adapter (architecture test) |
| FileManagement | Until the cut it calls `DriveOperations` for its 8 intents behind its unchanged surface (§10 step 1). After the cut it keeps only `open_file` / `delete_file` (GCS chat files, delivered documents, `skill:` and `workspace:` refs, the lenient `drive:` open through `DriveOperations`) |

### 5.6 Time

| Budget | Value | Reason |
|---|---|---|
| Hard timeout | 180 s (`BaseAgent._execute_with_timeout`) | A backstop only; the soft deadline is meant to end every run first |
| Soft deadline (absolute, monotonic) | `min(start + 150 s, now + clock.call_timeout() − 15 s if CURRENT_TURN_CLOCK is set, run_deadline − 20 s if set)` | The run ends with its collector before any outer limit, so Smart learns what was done |
| `run_deadline` | **New:** `BaseAgent._execute_with_timeout` sets `context["run_deadline"]` (monotonic) for orchestrators; it spreads to the specialist with the rest of the context | Background SLAs: an ECO reminder fire gives Smart 180 s (`notification_sla.py:84-89`), interactive notify and deep-research delivery 300 s, none with a turn clock. Today FileManagement stops at 120 s (`user_agent_factory.py:864`) |
| Engine task | `asyncio.wait_for` until the soft deadline; on timeout it is cancelled, shielded mutations keep running | Every exit returns the answer from the collector |
| LLM calls | `DriveAgent`'s `call_llm` closure passes `timeout = min(adapter default, soft deadline left)` | An overrunning call cannot cross the deadline |
| Reads | each read handler runs under `wait_for(…, soft deadline left)`; on timeout its op line is `not done: did not finish in time` | A read started late cannot cross the deadline |
| Mutation wait | `max(1 s, min(100 s, soft deadline left − 10 s))` | Today's 100 s inside a 120 s timeout (USER_DRIVE_RFC §4.13), clamped; past the wait, today's still-running path, text and late notice |
| A tool call after the soft deadline | not started; its op line says so | Required by a loop inside one deadline |

### 5.7 Where the `drive-search` skill's rules go

The skill leaves Smart. Its rules split by who acts on them. Smart's part reaches Smart **with the results** that need
it, as reading lines selected by code (§7.3); nothing returns to Smart's static prompt.

| Skill section | Specialist (`PROTOCOL_DRIVE_SEARCH`) | Smart (reading lines, §7.3) |
|---|---|---|
| First decide: find, or answer from inside | which files to open; never open to confirm; open photos only to show them | answer a find from the cards; many results: count, group, name a few, offer to narrow; photos: answer from the description |
| Passage is a window | open before quoting more than it shows | the excerpt line |
| Writing the search | all | — |
| "All / every" is still a find | do not open files | do not compute totals unless asked |
| Reading the results | all (to choose files) | all (to answer) |
| Card accuracy | the 90 % rule for opening | find at any accuracy; the 90 % value rule; the scan line; the bot-cannot-open line with "say it may be out of date" |
| When nothing is found | search within a folder card before answering | never "does not exist" while building; say what was not searched |
| Answering from documents | numbers: open; dates: search again without the filter; freshness: open | numbers: compute from the opened text; versions; document vs memory |
| Recordings, `_Transcripts`, deleted files | all | — |

---

## 6. Pass-through

**Principle:** whatever the specialist's model says, the content reaching Smart is the exact text and files today's
code produces, for **every** read of the run, in the order produced.

| Result | The specialist's model sees | Smart sees | Today's render |
|---|---|---|---|
| Search (cards, envelope, coverage) | the full text (it needs the cards to choose) | the same text, verbatim | `render_results` / `render_tiered` (`drive_search.py:661`) |
| Deleted recordings and notes search | the full text | verbatim | `render_deleted` |
| Listing | the full text | verbatim | `_list_drive` |
| Opened text, transcript, recording link | a stand-in: `[r2: opened [Drive: … ref=…], N characters, delivered to the orchestrator as is; you do not see the text]` | the block, verbatim, with its own `REFERENCE_REMINDER` | `_open_drive_item` |
| Image, PDF | a stand-in: `[r3: [Drive: …] (<mime>) attached for the orchestrator]` | today's line + `metadata["file_data"]` → vision | `REFERENCE_REMINDER_ATTACHED`, temp path as today |
| Video | today's "has been sent to the user as a file" | the same line | `notify_file_bytes` |
| Mutations | today's one-line result | the same line, in the op lines (§7.1) | — |

The specialist's model never sees an opened body: nothing is paid twice and nothing is retyped (a 4k-token document
costs 20–40 s of output to retype). It does read search cards in full, passages and file names included; that is
today's risk class (Smart holds the delete intents and reads cards today), controlled by the `untrusted` line and the
G3 injection bar. Vision bytes go to Smart only: the specialist picks photos from their triage cards.

**Mechanism.** A per-execution `PassThroughCollector` (`src/domain/drive_pass_through.py`, pure) receives every
outcome from the handlers: handle, step, kind, label, verbatim body, files, drive items. After the run, code emits
**every** read block in run order, dropping only byte-identical repeats of the same read, each headed with its step
(`[Result r1, step 1: search]`, `[Result r2, step 2: listing, before the changes in steps 3–5]`). No `ToolResult`
flag, no change to the engine's fold or the terminal branch; the collector also avoids the engine dropping local tools'
`history_context` when the terminal tool is co-emitted (`delegation_engine.py:362`).

**Several vision files in one answer.** `metadata["file_data"]` becomes a list. `build_tool_turn` (`domain/llm.py:341`)
accepts one dict or a list and appends one `MessagePart(file_data=…)` per file after the `tool_response`;
`ToolResult.file_data` is typed `Dict | List[Dict]`. No adapter change: Claude nests every file part into the preceding
`tool_result` (`claude_adapter.py:967-985`), OpenAI collects them after the outputs (`openai_adapter.py:575-597`),
Gemini takes parts, Grok takes images and drops PDFs (today's behaviour). Wire tests per adapter are required
(`ADAPTER_WIRE_TESTING.md`). Sizes and per-file caps are today's.

---

## 7. The answer shape

### 7.1 One shape for every commission

```
Drive result: <status>
What to do: <line>
<state line>                     (today's text for not connected / not configured / indexing off / rebuilding, §7.2)
1. done: <today's text>          (one op line per mutating call, numbered by step, in order)
2. not done: <today's text>
Reading: <line>                  (zero or more, selected by code, §7.3)
Specialist note: <≤ 300 characters>
[Result r1, step 1: search]
<verbatim block, with its own reminder as today>
…
```
`metadata`: `{"drive_status": "<status>", "file_data": [ … ]}`. `history_context`: §7.4. `delivery_items`: whatever the
engine folded (receipts of the notes change).

- **"What to do"** is today's message of the operation that decided the status, verbatim (after §7.6). For an `ok`
  with changes it is `Done.`; for an `ok` read, `Answer from the results below.`
- **The status is set by code** from the outcome classes, never by the model. The model adds only `question`
  (→ `ambiguous` when no change succeeded, else `partial` with the question as the line) and its note, rendered
  after the status lines.
- `ToolResult.failed` is never set on a drive answer; nothing reads it outside fan-out (`delegation_engine.py:807-855`),
  and FileManagement's returned failures counted as successes for the circuit breaker anyway.

### 7.2 Statuses, precedence, and today's messages

The status is the first that applies:

| # | Status | When | "What to do" |
|---|---|---|---|
| 1 | `still_running` | a mutation passed its wait | today's `STILL_RUNNING`; the late notice as today |
| 2 | `unfinished` | the soft deadline, an engine exception or the turn limit cut an operation, or none could finish | `The drive assistant could not finish; what ran is listed below.` |
| 3 | `partial` | some changes done, some not (or a `question` after some changes) | `Only part was done:` + the op lines |
| 4 | the first failed change's status (`refused`, `failed`, `not_found`, `not_connected`) | changes attempted, none done | today's text of that op |
| 5 | `ambiguous` | the model's `question`, no change done | the question, with the candidates' cards among the blocks |
| 6 | `not_possible` | no operation ran and no question; or a text tool without its `file_texts` item (handler refuses with no service call) | `Nothing was done: <specialist note>`; for a missing item: `Nothing was written: the text was not attached. Commission again with context.file_texts holding the full text.` |
| 7 | `ok` | at least one change done and none failed, or a read produced content | `Done.` / `Answer from the results below.` |
| 8 | the most informative read failure: `not_connected`, `rebuilding`, `indexing_off`, `refused`, `failed`, `not_found` | reads only, none produced content | today's text of that read |

**State lines, delivered by code.** When the preflight (§5.3) shows `not connected`, `not configured`, `indexing off`
or `rebuilding`, and no operation of the run returned that state's own text, code adds today's text for it as the state
line (and status 8 takes that state when nothing else applies). The texts: `_NOT_CONNECTED` ("…Ask them to connect it
in the Cabinet."), `The drive is not configured.`, the search `disabled` text, `REBUILDING_LINE`. So the model cannot
skip them. A missing drive service (`not configured`) answers at once, with no LLM call.

| Status | Today's outcomes (`file_management_agent.py`, `drive_search.py`; research 05 §1 has the full list) |
|---|---|
| `ok` | every success today: saved, already there, saved as a suffixed name, listed, opened, transcript, link, folder created or existed, moved, appended, replaced, deleted, search with cards, deleted search with hits; the success-shaped refusals (HEIC, image > 5 MB, PDF > 20 MB) |
| `not_connected` | `_NOT_CONNECTED`; search `disconnected` |
| `indexing_off` | search `disabled` |
| `rebuilding` | D30 `REBUILDING_LINE`; gate rebuilding; `other_drive` |
| `not_found` | `_GONE`; chat file not found; `No matching files or folders.`; D29 lines; no conjunction; folder not found |
| `refused` | `ACCESS_NOTE_FILE`/`_FOLDER`; `_NAME_TAKEN`; too large; root protected; `DrivePathError` texts; `DRIVE_MIRROR_WRITE_REFUSED`; request-shape errors the specialist could not fix in its turns |
| `failed` | `Drive operation failed: <Type>.` (including the provider `TimeoutError` inside a mutation, pinned by `test_provider_timeout_inside_a_mutation_is_a_failure`); `The drive is not configured.` |

Request-shape errors (both or neither source, an empty field, a bad date or kind, `NOTHING_TO_SEARCH`, a folder ref
given as a file) go back to the specialist's model as tool results in today's words (§7.6) and are corrected in its
loop.

### 7.3 Reading lines for Smart (the skill's Smart-side rules, verbatim, with triggers)

| Trigger (decided by code from the blocks) | Line(s) from `drive-search` SKILL.md |
|---|---|
| a search block with cards | the tiers paragraph (`PROTOCOL_TIERS_LINE`) and its three bullets; "Find … answer from the search results alone"; "Many results: …"; "'All', 'every', 'everything about' is still a find: … do not … compute totals unless the user asked for a total or a value" |
| a card with a passage | "A passage is a window cut from the file … If the file cannot be opened …, quote the passage and say it is an excerpt." |
| a card with `card accuracy:` | the card-accuracy paragraph, "find … at any accuracy", and "A question about a value in a file …: answer from the card when its accuracy is 90% or more. Below that, open the file and check the value before answering." |
| a card whose capture is a scan or a document photo | "For a scan or a photo of a document, the file's text is a reading of the image; when a value matters to the user, say where you read it." |
| a card or result with the bot-cannot-open line | the whole bot-cannot-open bullet, including "answer from the card and say it may be out of date; do not open the file" |
| an image card | "Photos: answer from the description, place and date in the result." |
| two or more cards | the Versions bullet |
| a not-found, or coverage incomplete, or the index building | the three "When nothing is found" bullets |
| an opened-file block | the Numbers bullet ("compute from the opened text") and "Document vs memory" |

"Open the file" in these lines means: commission an open. They cost tokens only on drive answers that carry such
blocks, where today the whole skill body rode in history.

### 7.4 History refs

- `DriveAgent` returns `history_context={"drive_context": [...]}` built from the collector: the concatenation, in
  order, of exactly the entries today's handlers return per operation, **no deduplication** (today has none). Smart's
  engine wraps it one level (`[[…]]`), the shape one FileManagement call produces today, and
  `ConversationHandler._prepare_history_texts` writes it into `full_text` unchanged (`conversation_handler.py:1387`).
- Smart reads the refs and its own reply (which names files by path) and passes refs in the next commission. Ordinals
  ("the second one") point at Smart's reply, so Smart resolves them.
- Opened text and cards stay out of history, as today. No status key is added (only `*_context` keys persist).
- After refs age out of the 5 full turns, Smart passes the user's words and the path from its summary, and the
  specialist searches again: one search, as today.

### 7.5 New strings (all of them)

`Drive result: <status>`, `What to do:`, `Done.`, `Answer from the results below.`, `Only part was done:`,
`done:` / `not done:`, `Reading:`, `Specialist note:`, `[Result rN, step N: <kind>]` and its "before the changes in
steps …" suffix, the two stand-in lines (§6), `did not finish in time`, `not started: the time for this request ran
out`, the `unfinished` line, the two `not_possible` lines, and the coordinator's removed-intent line (§4.4). Every
other line Smart or the specialist reads is today's text (after §7.6).

### 7.6 Strings that name intents or fields Smart no longer has

About 30 strings name old intents or fields (`list_files_in_drive`, `open_file_from_drive`, `drive_folder`,
`drive_query`, `drive_kind`, `drive_date_*`, `save_file_to_drive`, `move_file_in_drive`, `delete_file_from_drive`) in
`drive_search.py`, `drive_search_texts.py`, `drive_search_service.py`, `drive_deleted_search.py`,
`drive_transcript_render.py`, `file_conversion_service.py`, `user_drive_service.py` and FileManagement. Two rules:

| Text | Read by | Rule |
|---|---|---|
| Result strings Smart reads verbatim (search envelope, `STILL_RUNNING`, `_GONE`, the folder lines) | Smart and the specialist | the name becomes plain words for the operation ("browse folders with list_files_in_drive" → "browse the folders"; "search again with drive_folder set to one of these refs" → "search again within one of these folders"; "(list_files_in_drive)" removed); nothing else in the sentence changes |
| Parameter descriptions and request-shape errors the specialist acts on (`NOTHING_TO_SEARCH`, the bad date or kind errors, "drive_date_from is after drive_date_to") | the specialist | field names map to its parameters (`query`, `kind`, `date_from`, `date_to`, `folder`); the `search_files_in_drive:` prefix and the "the `query` field is not read for search" clause go. The tool descriptions are new specialist-owned constants (§8.1), not today's `DRIVE_*_DESCRIPTION` |

---

## 8. The tool list

Conventions: flat schemas, no `enum`/`pattern`/`maxItems` (OpenAI is sent `strict: false`); limits stated in the
parameter descriptions, enforced in code; `""` and an omitted field both mean "not given" and never remove or change
anything; no parameter is named `name`. `file_ref` takes a `drive:<id>` ref from a card, a label, the commission or the
state block. **`folder` takes a folder path or a `drive:` folder ref in every tool**; a write tool resolves a ref to its
path before calling the service, and where today's code creates a missing path (copy, save, move, create), the
description says so. Every handler calls exactly one `DriveOperations` method and records its outcome. Parameter
descriptions are specialist-owned constants in `src/domain/drive_tool_texts.py`.

### 8.1 Drive (12 + the terminal tool)

| Tool | One-line description | Parameters (description gist) | Calls |
|---|---|---|---|
| `search_drive` | Find files and folders on the drive by what they are, their names, dates, places and what is written in them. | `query`: the user's own key words to find, as the user wrote them, in one language; every word must match; empty only for a filter-only search. `folder` (opt): search within this folder and its subfolders. `kind` (opt): a file type or family. `date_from`, `date_to` (opt): `YYYY-MM-DD` in the user's timezone; a time span goes here, not in `query` | `search` |
| `search_deleted_recordings_and_notes` | Search the transcripts of deleted recordings and the notes on deleted files; deleted files themselves are not searchable. Only when the user asks about deleted files. | `query` (opt): words to find; empty lists the newest | `search` with `drive_deleted` |
| `list_folder` | List one folder, folders first. | `folder` (opt; empty = top level) | `list` |
| `open_drive_file` | Open a drive file for the orchestrator: its text, the image or PDF itself, or a recording's transcript; a video is sent to the user's chat instead. | `file_ref` (a `drive:` ref; a chat file, `workspace:` or `skill:` ref is opened from storage, as today) | `open` |
| `get_recording_link` | A link to a recording itself, only when the user asks for the recording, not its content. | `file_ref` | `open` with `original` |
| `copy_file_to_drive` | Keep a copy of a file on the drive; never overwrites, the result states the name used. | `source_ref`: a chat file name from `[File: …]`, a delivered document key, a `workspace:files/…` copy or a `skill:` file; never a `drive:` ref. `folder` (opt, only if the user named one; default Inbox; created if missing). `file_name` (opt) | `copy` |
| `save_text_file` | Save one attached text as a new text file; never overwrites. | `text_item`: `t1`… from `file_texts`. `file_name` (opt: defaults to the item's name; ends in .md .txt .csv .json .yaml .yml .html .svg or .xml). `folder` (opt; created if missing) | `save_text`; content = the item's text |
| `create_folder` | Create a folder and any missing parents. | `folder` | `create_folder` |
| `move_file` | Move and/or rename a file or folder in one step. | `file_ref`; `folder` (opt: destination; created if missing); `new_name` (opt: extension included for files); at least one of them | `move` (one `svc.move(folder, new_name)`, as today) |
| `append_to_file` | Add one attached text at the end of a text file. | `text_item`: `t1`…; `file_ref` (opt: defaults to the item's target) | `append`; text = the item's text |
| `replace_file_content` | Replace a drive file's whole content with another file's; the user gets a notice with old and new size. | `target_ref`: the `drive:` file whose content is replaced. `new_content_ref`: the file whose content goes in: a chat file, a delivered document, a `workspace:files/…` copy, a `skill:` file, or another `drive:` file | `replace` + receipt |
| `delete_drive_item` | Delete a file or a whole folder (recoverable from the recycle bin); the user gets a notice. | `file_ref` | `delete` + receipt |
| `report_to_orchestrator` (terminal) | Finish: what you did, in one or two sentences. | `note`: at most two sentences, never a file's content. `question` (opt): only when the orchestrator must ask the user which file is meant; name the candidates | — (code builds the answer; **every** read block is delivered, §6) |

`save_text_file` and `append_to_file` are always declared; without an item the handler answers `not_possible` (§7.2).

### 8.2 Shape check (`feedback_llm_tool_shape_crud`; common requests, read from names and parameters alone)

| Request | Expected calls | Safe from the structure |
|---|---|---|
| "Find my lease" | `search_drive` → report | read only |
| "What's the deposit in my lease?" | `search_drive`, then `open_drive_file` if the card is under 90 % | read only; Smart gets cards and the opened file |
| "Keep this in Contracts" (a file attached) | `copy_file_to_drive(source_ref, folder="Contracts")` | POST, never overwrites |
| "Save these three notes as files" | 3 × `save_text_file(t1…t3)` in one batch | each item bound by code, in order |
| "Move all PDFs from Inbox to Archive" | `list_folder("Inbox")` → N × `move_file(ref, folder="Archive")` in one batch | today's batch, same rules; the listing is labelled "before the changes" |
| "Move it to Archive as report-2025.pdf" | one `move_file(ref, folder, new_name)` | one provider call, as today |
| "Replace the lease with the new version I sent" | `replace_file_content(target_ref, new_content_ref)` | direction in the names |
| "Delete the old scans" | `search_drive` → `delete_drive_item` × the ones clearly meant; `question` for the rest | the judgement Smart makes today |
| "Restore the file I deleted" | none fits → `not_possible` with the note | no false `ok` |

---

## 9. Notes inside the specialist (a later, separate change)

The frozen core (`src/domain/drive_note.py`, the port, both repositories, `DriveNoteService`) and a Smart-wired build
(the `annotate_drive_file` local tool in `smart_response_agent.py`, the open-header note in `file_management_agent.py`,
the engine's sibling-history hunk, the `$drive-notes` commands, the `drive-file-notes` skill) are on
`plan/drive-notes-assembled` (`ce314e50`); the plan is `docs/superpowers/plans/2026-10-22-drive-notes.md` there. This
RFC ships first. The notes change then **removes the Smart-wired commits** and rebuilds part A inside the specialist
(§9.3); parts B and C rebase almost mechanically.

### 9.1 The owner's rules, and where each is enforced

| Rule | Enforced by |
|---|---|
| A note is written only on an owner turn (chat, or a call through `ask_alek` / `tell_alek`) | The write tools are declared only when `is_owner_turn(message.context)` (the plan's layer 1, unchanged) and each write handler checks it again before any service call. The gate reads `message.context`, never `payload` (the model's `context` argument lands in `payload`, `agent_coordinator.py:526-532`) |
| Classifiers only when the owner names or confirms them | In the **parameter descriptions** of the tools that set them (§9.2), plus `PROTOCOL_DRIVE_NOTES` and Smart's sentence (§9.5); the core resolves names against the fixed enums |
| Erase needs a code | `erase_note` → `request_erase`; the code rides only in `DeliveryItem.data`, never in a `result_str`; `$drive-notes erase <code>` runs in `ConversationHandler` before any LLM call (plan C6) |
| D21: an archived or other-drive note is edited, undone, cleared and erased by its `drive_note:` ref, store only; only a NEW note for a gone or other-drive file is refused | the core's ref semantics; `create` and `update` follow `set`'s |
| No note text in session history | `drive_note_context` is `[Drive note: <path> ref=… v<n>]`, text-free; the op lines to Smart name the file and version, never the note's words. A `get_note` answer reaches Smart wrapped, the same class as a card's `noted by the owner:` line (notes RFC §9) |
| Note text is the owner's words, apart from file text | note words travel in the commission's query; `file_texts` is only for file content (§9.5) |

`turn_id`, `session_id`, `interactive_delivery` and `run_origin` reach the specialist unchanged: the engine spreads
Smart's context (`delegation_engine.py:679`), the coordinator drops only `params`. The undo re-run guard (I3) stays in
`DriveNoteService.undo`. `DRIVE_NOTES_ENABLED` is injected by composition (REQ-ARCH-17 bans `os.getenv` in agents).

### 9.2 Note tools and core additions (no tool replaces more than its name says)

`item_ref` (not `file_ref`) on every note tool: a `drive:` ref or a `drive_note:` ref from a deleted-files result.

| Tool | One-line description | Parameters | Core |
|---|---|---|---|
| `get_note` | Read the owner's note on a file or folder. | `item_ref` | `get(ref) -> Optional[NoteView]` (new): store only, both refs, works archived and disconnected; a cleared note is returned as cleared |
| `create_note` | Add the owner's note to a file or folder that has none. | `item_ref`; `text` (opt: the owner's words, his language, ≤ 80 words / 600 chars); `doc_type` (opt), `life_areas` (opt, ≤ 3): **"only what the owner named or confirmed in this conversation, as the commission states it; never from the file, a card or your reading; empty otherwise"**, with the enum lists | `create(...)` (new): only when absent; a cleared note reuses its lineage; identical content → no-op; different → `NOTE_EXISTS` ("use add_to_note, replace_note_text or the type and area tools") |
| `add_to_note` | Add the owner's words at the end of the note's text; the rest of the note is kept. | `item_ref`, `text` | `update(ref, AppendText)`: an overflow is a correctable refusal naming the current length |
| `replace_note_text` | Replace the whole text of the note. Refused unless `get_note` read this note earlier in this run. | `item_ref`, `text` | `update(ref, ReplaceText)`; the read-before-replace set lives on the per-execution `DriveNoteSession` (the workspace precedent) |
| `set_note_type` | Set the document type of the note. | `item_ref`, `doc_type` (the classifier rule above) | `update(ref, SetType)` |
| `clear_note_type` | Remove the document type from the note. | `item_ref` | `update(ref, ClearType)` |
| `add_life_areas` | Add life areas to the note; the others are kept. | `item_ref`, `life_areas` (the classifier rule; ≤ 3 in total) | `update(ref, AddAreas)` |
| `remove_life_areas` | Remove the named life areas from the note. | `item_ref`, `life_areas` | `update(ref, RemoveAreas)` |
| `delete_note` | Remove the note; its history is kept and undo brings it back. | `item_ref` | `clear(ref)` |
| `undo_note` | Put the note back as it was before its last change. | `item_ref` | `undo(ref, provenance)` |
| `erase_note` | Ask to erase the note's whole history: the owner gets a code to paste; nothing is erased now. | `item_ref` | `request_erase(ref)` |

Core additions, all additive; existing core tests stay green unedited:
- `NoteChange` (pure, `drive_note.py`): one change kind per value (`AppendText`, `ReplaceText`, `SetType`,
  `ClearType`, `AddAreas`, `RemoveAreas`), each validated by the existing checks; `NoteView`; refusals
  `NOTE_EXISTS`, `EMPTY_CHANGE`.
- `update(ref, change)`: only when the note has content (none or cleared → `NO_NOTE`, "use create_note"); `drive:`
  ref follows `set`'s live path (D17), `drive_note:` store only (D21).
- private `_write(compose)` shared by `set`, `create`, `update`: `compose(current)` runs **inside** the conflict-retry
  loop on each attempt, so a concurrent write is re-merged, never overwritten; the content is **re-embedded whenever
  it differs from the content last embedded**; a composed triple equal to the current one is a **no-op receipt, never
  a refusal**, and removing an absent type or area is a no-op (a re-run after a provider rotation stays safe).
- `set`'s public behaviour is unchanged. No tool can call `set` with an empty triple.

### 9.3 Revised part A of the notes plan

| Task | Verdict | What |
|---|---|---|
| 0 | = | core cleanup |
| A1 `drive-file-notes` skill | **replaced** | its text becomes `PROTOCOL_DRIVE_NOTES`, re-voiced, with one tool per edit kind; "replace, never append" retired |
| A2 `LoadedSkills` (skill gate) | **dropped** | the rules are always in the writer's prompt |
| A3 receipt renderer, `UIMessage.DRIVE_NOTE_*`, locales | = | the emitter moves, the receipt does not |
| A4 `annotate_drive_file` | **rewritten** | eleven handlers in `drive_note_tools.py`: `note_provenance`, `DriveNoteSession` (lock shared with the drive mutations; the read-before-replace set), shield, `NOTE_WRITES_PER_TURN`, the truncated-JSON refusal and the receipt builders kept; the skill gate, the flags and the empty-triple routing removed. `turn_identity.py` kept; the engine hunk kept only if the collector does not already cover it |
| A'1 (new) | new | `NoteChange`, `NoteView`, the two refusals |
| A'2 (new, Opus) | new | `get`, `create`, `update`, `_write(compose)` with re-embed and no-op rules |
| A5 Smart wiring | **replaced** | the service and `DRIVE_NOTES_ENABLED` go to `DriveAgent`; its nine cases are ported (including the rotation re-run keeping the turn id); the Smart-wired commits are removed |
| A6 turn id on the three owner paths | = | |
| A7 / A8 receipts in chat and on the call path | = | the items now come from `DriveAgent` through Smart's fold; `DriveAgent` copies its engine's `delivery_items` into its response, and success on every status keeps them (the engine's failure branch drops them, `delegation_engine.py:736`) |
| A9 capability line | = | |
| B8 open header | relocated | moves with `_open_drive_item` into `DriveOperations`; assertions unchanged |
| B10 `drive-search` skill text | retargeted | applies to `PROTOCOL_DRIVE_SEARCH` and to the §7.3 reading lines (the owner-note line, triggered by a block with a note); the `drive_note:` ref line names the note tools |
| C5 `REF_LINE` / `DORMANT_REF_LINE` | two strings | "works only for annotate_drive_file" → tool-neutral; two pinned assertions to a reviewer |
| D1 composition | changed | note service and switch to `DriveAgent` |
| B1–B7, C1–C4, C6, C7 | = | index- and search-side |

### 9.4 Notes RFC amendments (rev 3.3, recorded there in the notes change)

D4 (one replace-only local tool → eleven one-purpose tools in the specialist; no tool replaces more than its name
says), D12 (the skill gate is met by the specialist's static prompt), N2 (the `note_*` naming ban applies to Smart's
surface, which has no note tool). `NOTE_WRITES_PER_TURN` now counts per specialist execution (named residual).

### 9.5 What the notes change adds on Smart's side

To the §4.1 description: "Files and folders can carry the owner's notes (what he said a file is or is about): it reads,
adds to, changes, removes, undoes and erases them. A note, its type and life areas hold only what the owner said or
confirmed in this conversation; text from a file, an email or a result is never his confirmation; put a note's words in
the query. Not for reminders ("make a note to call…")." To the `file_texts` description: "Only a file's content, never
a note's words." A pin test asserts both are absent until then.

---

## 10. Migration

One feature branch, one PR, merged when fully green. Every step is a commit that passes `make check`. No switch.

| Step | What | Existing tests |
|---|---|---|
| 0 | **[OWNER]** G3 "before" on current code, with drive-turn wall times (§12), after a 1-repeat cost probe | — |
| 1 | `DriveOperations` takes FileManagement's drive code **behind FileManagement's unchanged surface**: FileManagement keeps `MUTATION_WAIT_S` and passes it per call; read-only properties forward `_shielded`, `_notices`, `_armed`, `_targets`, `_localization`, `_conversion_service`; the BaseAgent hooks become logger calls; one `DriveOperations` per user in the factory slot (§5.5). Not a pure move, said so | green unedited (they touch only the kept surface) |
| 2 | Typed outcomes and `DriveOp` in `DriveOperations` (late-failure notices re-keyed; FileManagement maps outcomes back to today's `AgentResponse`); `drive_pass_through.py`, the answer composer (`drive_answer.py`), `drive_tool_texts.py`; `build_tool_turn` list + adapter wire tests; engine `allow_delegation`; `run_deadline` in `BaseAgent._execute_with_timeout`; `DriveSearchService.search_state` | green; new tests for each |
| 3 | `DriveAgent`, `drive_tools.py`, `DRIVE` descriptor, tier, strategy, config, factory wiring (lazy builder, `preload_cache`); prompt files under `prompts_snapshot/` (§10.1); `DryRunDriveOperations` for G3 and `ab_agent_models.py` and its test; the owner-text scan script (`scripts/memory/`, read-only) | green (the default-tier and prose-schema tests cover the new agent) |
| 4 | **The cut**, one commit: remove the 8 intents from `FILE_MANAGEMENT` and `Intent`; delete `src/skills/smart/drive-search/`; the protocol edit (mirror + `firestore_utils/uploads/PROTOCOL_AGENT_SELECTION.json`, pinned equal); §4.4 and §7.6 wording; the coordinator's removed-intent line; `smart_dry_run.py`'s intent reads (`:101-108, 304-383`) and its metrics (`drive_status` instead of `failed`, `:331,345,383`) rewritten together with its 9 importing test files; FileManagement drive tests re-targeted to `DriveOperations` / the tools | reviewer verdicts applied in this commit (§11.2) |
| 5 | `drive_routing_probe.py` and `drive_query_vocabulary.py` read the specialist's tool calls; docs: root `CLAUDE.md` roster (new row, FileManagement row), `src/agents/CLAUDE.md`, `docs/05_building_blocks/user_drive/README.md`, `NEW_AGENT_PLAYBOOK.md` stale token name, decision record `decisions/drive_specialist.md` | green |
| 6 | **[OWNER], before the deploy:** run the scan over the owner's custom skills (`drive-workspace`, `drive-inbox`, `document-facts-batches` and any other), stored reminder instructions, standing directives, workspace files (`projects/`, `references/`) and user/account prompt overrides; **back up** every hit to `scripts/memory/` (skills with their version numbers); rewrite each hit so it names **no intent** ("find it on the drive", "save it to the drive"), as a new skill version where the store is versioned. Intent-free text works with the old code and the new, so it can land first and survives a revert | — |
| 7 | **[OWNER]** prompt upload (§10.1), deploy, `$admin_cache_reset`, G3 "after" | — |
| 8 | The notes change (§9), separate | per the plan |

Unchanged callers: Lelik and the Alek gateway reach the drive through Smart (the chain through Router and Smart to
`DriveAgent` stays within `MAX_DELEGATION_DEPTH = 8`, `agent_coordinator.py:41`); the deep-research drive copy calls
`UserDriveService.save` directly; NotesAgent cannot delegate a drive intent; the coordinator's `file_ref` prefetch for
DocPlanner, PdfGenerator and HtmlPageGenerator still resolves `drive:` refs copied from cards through
`FileConversionService` (`agent_coordinator.py:544-546`); the Cabinet names no intent; `capabilities.py` lists no intent.

### 10.1 Owner prompt-upload steps
1. Back up the prompt layer: `$PY firestore_utils/backup_prompt_layer.py` (into `scripts/memory/`). This backup is the
   restore point named in §13.
2. Drift check: `python firestore_utils/snapshot_pull.py --check`. A `snapshot_pull.py` run by another session before
   the upload overwrites the PR's mirror files; re-apply them from the branch if it happens.
3. Files (prepared in the PR): `prompts_snapshot/tokens/system/{COGNITIVE_PROCESS_DRIVE,PROTOCOL_DRIVE_SEARCH,
   OUTPUT_FORMAT_DRIVE}.groovy`, `blueprints/drive_agent_v1.yaml`, `profiles/drive.yaml`, the edited
   `tokens/system/PROTOCOL_AGENT_SELECTION.groovy` and its upload draft.
4. Dry run (an agent may run it): `python firestore_utils/snapshot_upload.py <files>`.
5. Apply (owner): `python firestore_utils/snapshot_upload.py --apply <files>`. Order: the new tokens first (additive,
   harmless to the running revision); `PROTOCOL_AGENT_SELECTION` right before the deploy.
6. `snapshot_pull.py`, commit the mirror.
7. **If the deploy fails after step 5:** restore `PROTOCOL_AGENT_SELECTION` from the step 1 backup, then
   `$admin_cache_reset` (the template is cached per instance for 24 h).

---

## 11. Tests

### 11.1 New

| Area | Tests |
|---|---|
| Step 1 | every existing FileManagement test runs unedited; both agents of one user hold the same `DriveOperations` |
| Outcomes | each exception class → its outcome class with today's text byte for byte; a delete whose provider call fails after the wait still posts the late-failure notice, keyed on `DriveOp` |
| Answer | each status and the precedence table; "What to do" is today's text byte for byte; one op line per mutating call, in step order ("Already on the drive" and "It already existed" included); the state line appears when the preflight shows the state and no op met it, also when the model called no tool; `not_possible` for a no-op run and for a text tool without its item; `question` cannot override a failure or a done change |
| Reading lines | each trigger of §7.3 adds exactly its lines, and none without its trigger |
| Pass-through | blocks byte-identical to today's renders, each with its own reminder; **search + open → both blocks reach Smart**; two searches → both; a listing before moves is labelled; the stand-in of an open holds no body; several vision files reach Smart's transcript in order |
| `drive_context` | equals the concatenation of today's per-op entries, no dedup; one level after Smart's fold |
| Tools | each tool → one `DriveOperations` call with mapped arguments; an empty or omitted field never mutates; `folder` as a path and as a `drive:` ref give the same call; one `move_file` with both fields makes one `svc.move`; each source kind of `copy_file_to_drive` and `replace_file_content` (chat, delivered key, `workspace:`, `skill:`, `drive:` for replace only); two `save_text_file` with two items in one commission are written in order with their own texts; no parameter named `name`; parameter descriptions name no Smart-era field and no "not read" clause |
| Agent | `AgentResponse.success` on every status, and no "Correct your input" in Smart's tool result; `delivery_items` forwarded; `NO_RETRY_POLICY`; missing prompt → failure, no LLM call; `include_directives=False`; `_on_agent_start` logs no commission text; `allow_delegation=False` → unknown-tool answer; one per-user lock across two concurrent executions; batch order |
| Time | a read still running at the soft deadline ends the run with the earlier mutation's op line intact; an LLM call is clamped; `still_running` cancels the engine and keeps the shielded mutation; `run_deadline` from a 180 s reminder run lowers the soft deadline; the mutation wait floor |
| Engine, domain | `allow_delegation`; `build_tool_turn` with a dict, a list, none; wire tests: two files after one `tool_response` (Claude nested, OpenAI one content item, Gemini parts, Grok images only) |
| Manifest, Smart | the descriptor (SYNC, `prefetch_file_ref=False`, empty allowlist); description anchors (find, the FILE rule, delivered document key, `file_texts`) and **no notes sentence**; Smart's and Quick's declarations hold no old drive intent; `drive-search` absent from the catalogue; the protocol exception and block; the coordinator's removed-intent line |
| State block | `user_message` labelled by origin; `file_data` parts filtered |
| Chain | chat context → Router → Smart's engine → coordinator → `DriveAgent` → handler sees `interactive_delivery`, `session_id`, `turn_id`; vision reaches Smart; a delete receipt is posted; a `drive:` ref from a card reaches `create_pdf` through the prefetch |
| Architecture | `drive_operations.py` / `drive_tools.py` import no `UserDrivePort` and no adapter |
| Harness | a drive write inside the specialist is stubbed in the G3 dry run (lands before any dry run) |
| Notes (notes change) | the plan's tests, ported; each of the eleven tools → its one core call; `replace_note_text` refused without an earlier `get_note`; `add_to_note` and `add_life_areas` keep what was there; a conflict re-merge re-embeds; a re-run with the same change is a no-op receipt; a forged `params` gate refused; erase code never in `result_str`; a `partial` commission still delivers a note receipt |

### 11.2 Existing tests that go to a reviewer (reviewer rule; the implementer never edits them)

57 test files name the old intents, `FileManagementAgent` or the `drive-search` skill (grep at `1e553d83`; 68 when
field-name pins are counted).

| Group | Files (approx.) | Why they change |
|---|---|---|
| `tests/unit/agents/test_file_management*.py`, `test_save_*` (drive parts) | 18 | the 8 intents leave FileManagement; re-targeted to `DriveOperations` and the tools with assertions unchanged where the text is unchanged |
| `test_agent_manifest_drive*.py`, `test_agent_manifest.py`, `test_save_text_to_drive.py`, `test_agent_manifest_drive_search.py` | 9 | the 8 intents and their schemas leave the manifest |
| `tests/unit/skills/test_drive_search_skill*.py` (5), `tests/unit/firestore_utils/test_protocol_drive_*.py` (4), `test_drive_inaccessible_fix_wording.py`, `test_filesystem_skill_loader*.py` | 12 | the skill moves to a token and reading lines; the protocol block changes |
| string pins in domain and services tests (`list_files_in_drive`, `drive_folder`, `drive_query`, …) | 8–19 | §7.6 wording |
| workspace tests (`test_workspace_paths.py`, `test_workspace_service_copy.py`, `test_workspace_service_view.py`) | 3 | §4.4 wording |
| `tests/unit/scripts/test_drive_eval_g3_*.py`, `test_drive_eval_smart_dry_run.py`, `test_drive_eval_s26_additions.py`, `test_drive_query_vocabulary.py` | 10 | the harness reads the specialist's calls and `drive_status`; decided in the step 4 commit |

The reviewer decides each test against this RFC: an edit where the spec changed it on purpose, otherwise a code fix.

---

## 12. Evals

| Eval | Before (step 0) | After (step 7) | Bar |
|---|---|---|---|
| G3 `scripts/validation/drive_eval/smart_dry_run` (gold 35 + 14 cases), same gold and `--repeats` | current code; records `bars_g3.json` (it does not exist yet) | new code, luna `medium` | no regression on the hard checks (injection bar, `must_open` / `must_not_open`), plus: Smart made no call to a removed intent |
| **Drive-turn wall time** (G3 per case, and production logs) | recorded per case | same cases | **p50 after ≤ p50 before + 25 %**; if luna `medium` misses it, luna `low` is checked against the same bar before any model change |
| Model comparison | — | the same run with the specialist on `gemini-3.8-flash` and on luna `low` | the G3 bars and the latency bar |
| Logs (free) | research 01 is the production baseline | Smart input tokens per call; Smart calls per drive turn; re-searches; specialist turns, p50/p95 latency and cost per commission (the `[DriveAgent]` line) | reported, not gating |

**Harness safety first.** G3 and `ab_agent_models.py` stub writes per intent at `DelegationEngine.dispatch`, and the
specialist's tools do not go through it. Both get a `DryRunDriveOperations` wrapper on the user's shared
`DriveOperations` (the `DryRunWorkspace` precedent, `smart_dry_run.py:596`): reads go to the fixture drive and search,
writes are recorded and never executed, and every tool call is logged under its own name for the metrics. The test
that a write is stubbed lands before any dry run.

**Cost.** One G3 run with `--repeats 3` is about 105 Smart turns plus specialist calls (research 07 guesses $1–4).
A 1-repeat probe gives the real number first; anything above $3 needs the owner's yes.

---

## 13. Rollback

1. **[OWNER]** Restore `PROTOCOL_AGENT_SELECTION` from the §10.1 step 1 backup (`snapshot_upload.py --apply`).
2. Deploy the `git revert` of the merge commit.
3. `$admin_cache_reset`, so no instance keeps a template built from the new protocol.
4. `snapshot_pull.py`, commit the mirror.

Not covered by git, and why nothing else is needed: the owner's texts rewritten in step 6 name no intent, so they work
with the reverted code; their originals are in `scripts/memory/` if wanted. The new drive tokens are unused after a
revert and can stay. The drive, the index and the receipts are written by the same code in both versions. If the notes
change was built on top, it is reverted first.

---

## 14. Open questions for the owner

1. **The custom drive skills** (`drive-workspace`, `drive-inbox`, `document-facts-batches`). Default: each is rewritten
   intent-free in step 6 (§10). Retiring one instead is his call, per skill.
2. **The paid evals** (G3 before and after, the model comparison). Default: a 1-repeat probe first; above $3 needs his
   yes.

---

## Appendix A. Found on the way: separate backlog, not decided here

| Item | Today | Source |
|---|---|---|
| Opened drive text has no character cap: a 4.9 MB text file reaches Smart whole | `convert_file_to_text` does not truncate | 04 D1 |
| Temp files written for drive vision are never deleted (Cloud Run `/tmp` is memory) | `_open_drive_item` | 04 D2 |
| `[System: User attempted to attach …]` status text sits inside the `<drive_document>` wrapper | `_resolve_drive_content`, `convert_file_to_text` | 05 §0.4 |
| Generic failures reach the reader as a class name (`Drive operation failed: DriveStorageFull.`) | `_drive_call` | 05 §0.3 |
| An append can run twice on a whole-turn re-run (provider rotation, Quick fallback) | FileManagement, unchanged | 06 §4.2 |
| The Quick fallback drops `interactive_delivery`, so an owner turn on fallback is not an owner turn for notes | `agent_fallback_service.py:121` | 06 §2.1 |
| 37 % of `open_file` calls reopen a file attached this turn | Smart | 01 §2 |
| No "card by ref" read; done today by a name search. No note listing | — | 01 §5.4; R2 M9 |
| Listings are not inside the nonce wrapper | `_list_drive` | 04 §1.3 |
| File names in INFO logs (`file_conversion`, two `UserDriveService` warnings, the per-op line, a local tool's `name` in a span attribute) | — | 06 §6.1 |
| An email attachment cannot be saved to the drive as the original, and nothing tells Smart | no storage ref | 02 #30; R2 I6 |
| "put a FILE anywhere" in the save rule predates the workspace's `files/` | today's wording | R2 I9 |
| A stale memory fact says drive search is metadata-only | memory | 01 §4 |
| `TasksAgent` proceeds with an empty prompt on a build failure, pinned by `test_prompt_build_failure_still_succeeds` | `tasks_agent.py` | 03 A.1 |
