# RFC: The user drive through one specialist (`work_with_drive_files`)

**Status:** Accepted (owner, 2026-10-22), rev 4.6 (§0.0.3: §9 as built on `feat/drive-notes`; rev 4.5, §0.0.2, rows 8-9 of 2026-10-28); built on branch `feat/drive-specialist`; deployed: no.
Rev 2 applied the rulings on the three rev 1 reviews, rev 3 the scoped re-review of rev 2, rev 4 the live A/B of a
spike and the owner's decisions on it, rev 4.3 the deltas that the implementation plan recorded against the real code,
rev 4.4 the deltas accepted in the cross-cutting review round of that plan (§0, §15), rev 4.5 the deltas of the port,
rev 4.6 §9 as built.
**Related:** `USER_DRIVE_RFC.md` (Step 1 operations, `drive:` refs, the mutation lock and shield §4.13, receipts §4.10),
`USER_DRIVE_INDEXING_RFC.md` (Search v2 §25, the `drive-search` skill D32, inaccessible files §26.2, transcripts §27),
`DRIVE_FILE_NOTES_RFC.md` rev 3.3 (notes; the notes change amends D4, D12 and N2 there, §9), `AGENT_SKILLS_RFC.md`,
`TASKS_AGENT_RFC.md`, `PROACTIVE_SELF_REMINDERS_RFC.md` (the two specialists this one copies).
**Research behind it** (private, gitignored, not quoted for owner data): `scripts/memory/drive_specialist_sketch.md`,
`scripts/memory/drive_specialist_research/01`–`08`, the reviews `rfc_review_1_parity.md` (R1), `rfc_review_2_tool_shape.md`
(R2), `rfc_review_3_feasibility.md` (R3), the binding rulings `rfc_rev1_rulings.md`, and the re-review of rev 2
`rfc_rev2_rereview.md` (RR) with its arbiter rulings, research 09 (`09_who_iterates.md`, who iterates: variant A′), and
the live A/B reports of the spike branch `spike/drive-specialist` (`scripts/memory/drive_g3/specialist_ab_*`,
`specialist_replay_*`, private). Where this RFC differs from a
report, the owner's ruling of 2026-10-22 or the arbiter decided it.
**Branch base:** `main` after PR #132, a commit from before the history rewrite of 2026-10-25 (its hash is gone). Line
references are to that base and have drifted since; look a symbol up by name, not by line.
**Where the code and this text differ once it is built, the code is the truth.**

---

## 0. Changes

### 0.0.3 rev 4.5 → rev 4.6 (§9 as built on `feat/drive-notes`)

§9 is corrected where the build differs: the dead commit id of the notes branch, the receipts riding the pass-through
collector (§9.3 A7/A8), and §9.6, what the build decided that §9 left open or said otherwise.

### 0.0.2 rev 4.4 → rev 4.5 (deltas of the port onto the current `main`)

The body above is not rewritten; where it differs from these lines, these lines and the code win.

| # | Change |
|---|---|
| 1 | Receipts and late-failure notices are sent literally: `DriveOperations._receipt` calls `notify_raw(..., literal=True)` (`src/infrastructure/drive_operations.py`), `main`'s owner rule for text the bot did not write (the path is the user's or the model's text). |
| 2 | A plain-text final reply of the specialist beside `report_to_orchestrator` is the report's note when the note is empty (`DriveReport.of(args, plain_text)`, `src/domain/drive_report.py`); `DelegationEngine` now carries the text written beside a terminal call. |
| 3 | P14: the specialist's twelve tools are a tool-field surface (`drive` and `delegate:work_with_drive_files` in `tests/support/tool_fields.py`), covered by the e2e card `tests/unit/agents/test_drive_agent_tool_fields_e2e.py`; FileManagement's drive card is retired. |
| 4 | The new modules (`drive_operations`, `drive_tools`, `drive_agent`, `drive_answer`, `drive_pass_through`, `drive_file_contents`) are registered for the weekly mutation run (`tests/support/mutation_targets.py`). |
| 5 | The G3 eval (`smart_dry_run.py`) can run the branch's prompt files before any upload and pick the specialist's provider and tier (`--prompts-from-snapshot`, `--specialist-provider`, `--specialist-tier`). |
| 6 | G3's `--record` writes no bars while today's Smart fails a hard check (verdict "regression", nothing recorded); a before/after comparison is read from the two reports. |
| 7 | The drive-search skill's search discipline reaches both sides again (G3 found the specialist fanning out one search per synonym or translation Smart listed): Smart's `query_language` exception and the manifest ask for the user's own few key words in one language (§4.1, §4.3 blocks updated); the find sentence regains 'show me' and 'everything about X'; the specialist's tokens state that a subject is searched once with the user's words and that a list of alternatives is not a list of searches (§2.3.11). No cap was added. |
| 8 | The specialist's prompt is rewritten as one procedure with each rule once (task, searching, results, again, opening, changes, then the report): owner decision 2026-10-28 lifts §7.6's "skill sentences move verbatim" for the specialist. Rules about answering, quoting and computing stay Smart-side reading lines. `date_from` / `date_to` filter the file's own date; a date written inside a document goes in `query` with the subject. |
| 9 | The specialist's prompt carries no user location: `build_for_agent(include_location=False)` (default `True`, as `include_directives`), passed by `DriveAgent`. |

### 0.0.1 rev 4.3 → rev 4.4 (deltas from the cross-cutting review round of the implementation plan)

Sources: arbiter = `plan_arbiter_xcut.md` (rulings X1-X12, R1-R5); re-review = `xcut_rereview.md` (F1, F2, M4). Only accepted deltas are written in.

| # | Change | Source |
|---|---|---|
| 1 | §7.2 row 2, §5.4: a change cut after it started is `still_running` with today's `STILL_RUNNING` text, never `unfinished`; its op line verdict is the new `still running`, never `not done` | arbiter X1; re-review F1, M4 |
| 2 | §7.2, §7.5: a change queued behind a still-running sibling is not started, with the new string `not started: an earlier change in this request is still running` (no false "time ran out") | arbiter X1; re-review F2 |
| 3 | §5.5: a path-like ref (contains `/`, no known prefix) in open, append, move, delete or replace gets the correctable `REF_REQUIRED` refusal; a bare name keeps today's wrong-store text; the prompt says "search before you change or open" | arbiter X2, X3; re-review M4 |
| 4 | §7.2: "a save, listing or open carries no indexing text" is replaced: when the search line is not `on`, no search ran and the answer holds reads only, the answer carries today's disabled / rebuilding text; saves stay clean | arbiter X6; re-review M4 |
| 5 | §7.2: for a search read classed `not_found`, "What to do" is the answer-from-the-results line, not the read text | arbiter X4 |
| 6 | §7.2: a `reconnect needed` connection state is handled as `not connected` (today's text, status `not_connected`) | arbiter X5 |
| 7 | §7.3: the not-found reading lines fire only for search blocks, not when the status comes from a change and the search only found the ref | arbiter X7 |
| 8 | §5.4: the turn-cap alert post is bounded by a 5 s timeout inside the existing try; a hanging sink still returns the answer | arbiter X9 |
| 9 | §5.1: `"drive"` joins the cacheable and multi-turn agents; the pre-build check verifies cache reads on the second turn of a commission | arbiter X10 |
| 10 | §7.2: the `unfinished` line's "What to do" gains: "Tell the user what ran and what did not; do not commission the same request again in this turn." No hard third-call refusal | arbiter X11 |
| 11 | §4.2: the Smart-era drive fields (`drive_query`, `drive_folder`, `drive_kind`, `drive_date_from`, `drive_date_to`, `drive_deleted`, `new_name`, `source_ref`, `original`, `name`) are passed through as labelled extras under "Also given with the commission:" | arbiter X12 |
| 12 | §5.1 Logging, §10.1: the `[DriveAgent]` line gains `model=`; new owner step O0 (check `provider_preference` / `agent_providers`; if the preference is `openai`, set `agent_providers["drive"]="claude"`), a check only: today's defaults apply | arbiter X8 |
| 13 | §13, §10.1 step 7: the rollback runs in the forward order (revert on a branch, apply the restored mirror just before the deploy, deploy, `$admin_cache_reset`, PR the revert); window of up to two self-corrected calls; a failed deploy gets a new revision of the current image, not a retry on the restored protocol | arbiter R1; re-review F3 |
| 14 | §10 step 6: the scan also covers `domain=preference` facts, the workspace `scratch/` and the text files in `files/`; only the owner is scanned; `skill_file` hits are redrafted in chat | arbiter R2 |
| 15 | §12: `drive_routing_probe.py` takes a required `--max-usd`; overshoot is about one call per parallel specialist | arbiter R3 |
| 16 | §10 step 7: one live smoke message after the deploy, plus a check of the `[DriveAgent] status= model=` line and `prompt_content` for `agent_type=drive` (model, cache reads) | arbiter R4 |
| 17 | §14, Appendix A: the Grok 9-round-trip case was 8 re-opens of one PDF that `GrokAdapter` drops, an existing adapter gap (backlog), not one commission per file | arbiter R5 |

### 0.0.0 rev 4.2 → rev 4.3 (deltas from the implementation plan, verified against the real code)

Sources: plan A = `drive-specialist-A.md` §7; plan B = `drive-specialist-B.md` §7; arbiter A and B =
`plan_arbiter_A.md`, `plan_arbiter_B.md`; re-review B = `plan_rereview_B.md`. Only accepted deltas are written in.

| # | Change | Source |
|---|---|---|
| 1 | §7.2 row 2: a run that ended without the specialist's report (deadline, engine exception, turn limit) is `unfinished`, whatever finished; a reported run keeps rows 3-9 | plan A RFC text changes; arbiter A I1 |
| 2 | §7.2: a change's op line is never dropped; a settled correctable error is left out of the status only | plan A RFC text changes; arbiter A I2 |
| 3 | §5.1: the sol fallback covers provider errors on the first call only; the Sonnet stall is bounded by 6 turns and 150 s | plan A RFC text changes; arbiter A I3 |
| 4 | §5.4: a turn-cap exit without a report logs ERROR and posts one ops alert; `DriveAgent(alert_sink=…)` | plan A RFC text changes; arbiter A I3, arbiter B (New from A) |
| 5 | §5.4 "No delegation" also covers a delegate call co-emitted with the terminal tool | plan A §7 |
| 6 | §5.3 sources: `UserDriveService.connection`, a `SearchLine` enum from a pure `search_line()`, an injected clock for `now` | plan A §7 |
| 7 | §5.5: hooks stay on FileManagement; `DriveOperations` logs through `logger`; it gains `READ_METHODS`, `MUTATION_METHODS`, `OP_OF` | plan A §7 |
| 8 | §5.6 LLM calls: `min(120 s, time left)`, not the adapter default | plan A §7 |
| 9 | §6, §7.3 triggers come from `ReadFacts` set by `DriveOperations` from the typed result, plus `DriveOutcome.body` for stand-ins, not from rendered text | plan A §7 |
| 10 | §7.2 table: gate `rebuilding` (a purge) is `not_found`; an ambiguous folder is `ok`; a deleted search without hits is `not_found` | plan A §7 |
| 11 | §7.5 and §8.1: more new strings (partial, no-operation, extra-commission lines, unknown-item refusal, `{field} is required: {what}.`); `report_to_orchestrator` description; `move_file` checks "at least one of them" in the tool; the move sentence is one domain constant | plan A §7; arbiter A M3, M7 |
| 12 | §7.6: model-facing texts move verbatim; only references to removed intents and fields are renamed; the rule also covers docstrings and the result strings of `FileConversionService` and `UserDriveService` | re-review B §0; plan B §7 item 7 |
| 13 | §10 steps 1-2 are one task; step 1 "green unedited" holds for all but three FileManagement tests that patch module names of the moved code; step 3 scan script is tracked in `scripts/validation/` (outputs only in `scripts/memory/`) | plan A §7; plan B §7 item 6 |
| 14 | §11.1 Chain test starts at Smart's engine, not at the Router; §11.2 two strategy and tier tests need `"drive"` in their lists | plan B §7 item 8; plan A §7 |
| 15 | §12 and §4.1: the harness write stub is at the `DriveOperations` class, in `scripts/`; `ab_agent_models.py` gets no drive stub; eval metrics read the `DriveOperations` log; G3 counts a call to a removed intent as a hard failure; descriptor name `DRIVE_SPECIALIST` | plan B §7 items 1-5 |

### 0.0 rev 3 → rev 4 (what changed, and why)

| # | Change | Source |
|---|---|---|
| 1 | Division of labour A′ (§5.8): the specialist owns search mechanics (wording, tiers, re-search, leads in search results) and the opens Smart asks for; Smart owns content judgement and re-commissions with what opened files name. A reading line on opened-file blocks says so (§7.3) | research 09; owner decision |
| 2 | The specialist reads search results before it reports (no co-emitted report after a search); the note never claims completeness; the execute-as-given rule for named refs or keys (C′ as one prompt rule) (§5.2, §5.4) | research 09 §6.4, §6.5, §6.12; owner decision |
| 3 | Code skips a `search_drive` call identical to one already run in the commission (§5.4) | research 09 §6.6; replay: up to 9 searches per commission |
| 4 | Smart's description gains the find rule, the sentence validated in stage 2 (§4.1) | live A/B stage 1 → 2; owner decision |
| 5 | `PROTOCOL_DRIVE_SEARCH` gains a recordings line (audio or video, `.webm` is video; dates in names as `YYYY-MM-DD`; a media folder listing as fallback) (§5.2) | live A/B: `ab-recording-link`; owner decision |
| 6 | Specialist model: `claude-sonnet-5-5`, adaptive thinking, effort medium; fallback `gpt-6.1-sol`; luna, grok-4.7 and `gemini-3.8-flash` rejected (§5.1) | live A/B stage 3 and the final model probe (§15.5); owner decision 2026-10-22 |
| 7 | Smart's model is unchanged by this RFC; Sonnet 5.5 was left out of the Smart-side check because of its known filler-tool stall (§15.4) | live A/B stage 4; owner decision |
| 9 | No variant re-search by the specialist (owner 2026-10-22): the skill's "one language, no translations" stays as today. Transliteration is already exact in the index (`x:` keys); a translation gap on an unprofiled file is closed by Smart re-commissioning with other words (§2.3, §14) | research 09; owner decision |
| 8 | New §15 "Live A/B results"; §12 updated (a stage-1 rerun with the Sonnet specialist is the pre-build check); the paid-evals question closed (§14); one open issue for the build (§14) | owner decision |

### 0.1 rev 2 → rev 3 (what changed, which review asked)

| # | Change | Asked by |
|---|---|---|
| 1 | The coordinator's removed-intent line is dropped; today's generic unknown-intent error stays (residual in the decision record) | RR §4, M10, M11; arbiter |
| 2 | Search-state lines appear only on answers that ran a search; a connection-state line only when no operation ran (§7.2) | RR I2; arbiter |
| 3 | `report_to_orchestrator.not_done`: `not_possible` also after reads; a preflight state ranks above `not_possible`; the missing-item refusal is classed `not_possible`; an unknown `text_item` is refused and never written (§7.2, §8.1) | RR I1, M1, M2; arbiter |
| 4 | Rollback also scans for texts written after the deploy (§13); the scan patterns are listed (§10 step 6) | RR I3, M5; arbiter |
| 5 | `include_directives=False` declared in §2.3 | RR M6; arbiter |
| 6 | `run_deadline` becomes `CURRENT_RUN_DEADLINE`, a process-local `ContextVar`, never in the context or a Cloud Task payload; cleared when an orchestrator runs without a timeout (§5.6) | RR M4; arbiter |
| 7 | Sources: `workspace:<path>`; `copy_file_to_drive.file_name` keeps the source's extension when omitted, as today (§8.1) | RR M3 |
| 8 | Notes: `add_to_note`, `set_note_type`, `add_life_areas` create the note when none exists; `get_note` takes the ordering lock; `replace_note_text` refused if the note changed since that read; a specialist-side line for note words found in a file item (§9.2) | RR M7, M8; arbiter |
| 9 | `file_texts` → `file_contents`, described as file content only (§4.2) | RR M7.3; arbiter |
| 10 | R2 I4's zero-hit half added to the "Not applied" table | RR M9; arbiter |

### 0.2 rev 1 → rev 2 (what changed, which review asked)

| # | Change | Asked by |
|---|---|---|
| 1 | Note tools: one tool per edit kind (`add_to_note`, `replace_note_text` only after `get_note`, `set_note_type` / `clear_note_type`, `add_life_areas` / `remove_life_areas`); classifier rules in the parameter descriptions; `item_ref`; `create_note.text` optional (§8.2, §9.2) | R2 C1, C2, I3, M6, M10 |
| 2 | Drive tool parameters described for the specialist (no Smart-era texts); `folder` takes a path or a `drive:` ref everywhere, "created if missing" named; `search_deleted_recordings_and_notes`; `replace_file_content(target_ref, new_content_ref)`; `report_to_orchestrator` in the tool list; tools renamed `open_drive_file` / `delete_drive_item`; video side effect named (§8.1) | R2 I1, I2, I4, I8, I10, M1, M5; R1 F9, F14; R3 M8 |
| 3 | `text_to_write` → `file_texts` (rev 3: `file_contents`), a list of items bound by code; batches of composed texts work as today; status `not_possible` (§4.2, §7.2) | R1 F1; R2 I5, I7; R3 M5 |
| 4 | Every read block reaches Smart verbatim, in run order; no `attach`, no default selection (§6) | R1 F2; R2 M2 |
| 5 | Move and rename: one tool, one `svc.move` (§8.1) | R1 F3 |
| 6 | Tool descriptions name today's accepted sources (`workspace:`, `skill:`, `drive:`, chat files, delivered keys); Smart's description asks for delivered document keys (§4.1, §8.1) | R1 F4; R2 I6 |
| 7 | Not connected, not configured, indexing off and rebuilding: code always delivers today's text (§7.2) | R1 F5 |
| 8 | No notes sentence on Smart until notes ship (§4.1, §9.5) | R1 F7 |
| 9 | Smart-side skill rules (excerpt, 90 % value rule, "may be out of date", scan, versions, document vs memory, all/every) reach Smart as code-rendered reading lines with stated triggers (§7.3) | R1 F6 |
| 10 | Deadlines: the engine runs as a task under the soft deadline, cancelled on timeout or `still_running`; reads and LLM calls clamped; the caller's budget respected on background runs (`run_deadline`); status `unfinished`; 6 turns / 150 s declared (§5.6) | R3 I1, I2, I6, M9; R1 F10, F12 |
| 11 | Rollback: the owner's Firestore texts are rewritten intent-free (work before and after a revert), backed up first; workspace and prompt overrides scanned; §13 order restore → deploy → cache reset (§10, §13) | R3 C1, I7; R1 F8, F11 |
| 12 | Migration green at every step: FileManagement keeps the internals tests touch; late-failure notices re-keyed on an operation enum; `smart_dry_run.py` and its 9 tests fixed in the cut (§10) | R3 I3, I4, I5 |
| 13 | Notes core: `update` re-embeds on a re-merge, a same-content re-run is a no-op; one shared `DriveOperations` per user in a factory slot (§5.5, §9.2) | R3 I9, I10 |
| 14 | A latency bar on drive-turn time, before vs after (§12) | R3 I8 |
| 15 | Minors applied: status precedence (R1 F12); `user_message` labelled by origin, `file_data` parts filtered (R1 F13, R3 M4); partial ambiguity (R1 F15); full list of new strings, blocks keep their own reminder (R1 F16, R3 M2); prefetch caller named (R1 F17); safety wording (R1 F18, R3 M3); no dedup of `drive_context`, `ToolResult.failed` named (R1 F19); one op line per mutating call (R1 F20); success exceptions named (R3 M1); `include_directives=False` (R3 M6); the gate read and the notes switch injected (R3 M7); cost wording (R3 M10); the notes branch is already wired (R3 M11); token frontmatter (R3 M12); plain-text final reply (R3 M13); note listing out of scope (R2 M9); the per-op log line kept (R1 §4) | as named |

**Not applied, with the reason (rev 2 and rev 3):**

| Item | Reason |
|---|---|
| R2 M3: a truncated listing makes a batch `partial` | Behaviour change: today the truncation line reaches Smart and nothing marks it partial. It still reaches Smart verbatim |
| R2 M4: keep the old extension when `new_name` has none | Behaviour change: today the name is used as given. The parameter description says "extension included" |
| R2 M7: "restoring is done in the recycle bin" on Smart | A new rule; a restore commission ends as `not_possible` with the specialist's note |
| R2 M8: rename `erase_note` | Ruling 1 names `erase_note`; its description says nothing is erased now |
| R2 I9: "put a FILE anywhere" vs the workspace | Not ruled; today's wording is kept verbatim |
| R2 I6, second half: "an email attachment cannot be saved" | Not ruled; stays a backlog item (Appendix A) |
| R2 I4, second half: a zero-hit line for the deleted search | A new string; the tool's description and Smart's already name what that search covers |
| R3 M14: the base is behind `main` | The base is `main` after PR #132, newer than the commit R3 read (both hashes are gone since the 2026-10-25 history rewrite) |

---

## 1. Context

Smart, the orchestrator, carries the whole drive in its own prompt. Measured on Smart's production turns in
`prompt_content` (research 01; the token sizes are sizes of the prompt text):

| Fact | Measure |
|---|---|
| Fixed drive surface on **every** Smart call: 8 drive intents (≈ 945 tok), their 13 context fields (≈ 816), `open_file`/`delete_file` (≈ 161), the `drive_specialist` protocol block (≈ 235), the drive lines of the skill catalogue (40–145) | **≈ 2 200 tokens** |
| `drive-search` skill body loaded before a search, kept in history `full_text` until tiering, often in several copies | ≈ 1 100–1 500 tokens per copy; 2–4k per Smart call on average, also on days with no drive work |
| How often Smart uses the drive | **often**, since it went live (10-10) |
| Invented `drive:` refs, field confusions, model-made rejections | **none** (every ref in the sample was copied correctly; every rejection was a code gap, since fixed) |
| Follow-up searches after a search that had results | seen in production: some switched script or language, some were "verify / confirm / repeat" re-runs |
| Drive turns with 10 or more drive calls | seen in production (the worst ran on with no stopping) |
| Smart input tokens in drive turns spent on rounds whose only job was to read drive-only results | seen in production |

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
10. The specialist's prompt has no standing directives (`include_directives=False`, §5.2): they are written for the
    orchestrator (the Lelik precedent). Smart still applies them in its reply and can carry one into the commission.
11. **No variant re-search (owner 2026-10-22).** The specialist searches with the user's words in one language, as
    the skill says today; it does not re-search in another script or in a translation. Owner's reasons: transliteration
    is already exact in the index (`x:` skeleton keys, so "Dogovor" and "Договор" meet on the first search); meaning
    search finds cross-language matches; the index keys carry translations ("By translation" tier); and the specialist
    returns to Smart, which re-commissions with other words when the coverage line shows a gap — as Smart decides
    today. Recorded as a known question in §14.

---

## 3. Design in one screen

```
Smart ──delegate_to_specialist(work_with_drive_files, query, {file_contents?})──▶ DriveAgent (claude-sonnet-5-5)
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
| Search before mutate, then act by ref | `TasksAgent` | The ref is today's `drive:<id>`, shown in every card and label. Smart copied every ref in the sample correctly, so no second short-ref scheme (it would need a second rendering of every card) |
| Multi-step loop: read, read, write, answer | `TasksAgent` | The drive needs "find, then move" and "open these five" in one commission |
| Receipts written by code | `NotesAgent`, today's FileManagement | Unchanged: delete and replace post `notify_raw` receipts |
| Strict code validation with correctable tool errors; content bound by code, never retyped | `NotesAgent` (`workspace_file`) | `file_contents` items are bound by code like `workspace_file` |
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

### 4.1 The intent (`src/infrastructure/agent_manifest.py`, new descriptor `DRIVE_SPECIALIST`; part A's config instance is `DRIVE`)

```python
Intent.WORK_WITH_DRIVE_FILES: (
    "The user's drive: their own long-term files (documents, scans, photos, recordings). "
    "Finding files is part of it: by name (exact, partial, transliterated or misspelled), by what a file is or what is "
    "written inside (a subject, a phrase, a number, an amount), type, date, folder, photo details and place; deleted "
    "files only when the user asks about them (transcripts of deleted recordings and the notes on deleted files). Use it "
    "when the answer may be in the user's files, and before saying a file is not there. "
    "A find ('find', 'show me', 'do I have', 'where is', 'which files', 'everything about X') is answered from the search results alone; ask to open a "
    "file only when the user wants something written inside it. "
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
    'File contents to save or append: context={"file_contents": [<one item per file>]}. '
    "Returns a status and a 'What to do:' line (follow it), then every result verbatim; images and PDFs come attached."
),
```

About 2 050 characters (≈ 510 tokens), replacing ≈ 1 760 tokens of intent descriptions and fields. The find sentence is
the one validated in the live A/B (§15.2: it removed the opens on a find); every other sentence is a rule or capability
Smart holds today, moved from the 8 descriptions (`agent_manifest.py:546-613`); the procedural parts
(Inbox default, never-overwrite, extensions, `_Transcripts`) move to the specialist. One paragraph, because
`_build_delegate_tool_declaration` renders each intent as one `- name: description` line (`base_agent.py:789`). **No
notes sentence:** the notes change adds it (§9.5).

### 4.2 The one field

```python
context_schemas={Intent.WORK_WITH_DRIVE_FILES: {
    "file_contents": {
        "type": "array",
        "description": (
            "work_with_drive_files only: the content of text files, one item per file, each exactly as it goes into "
            "the file (text you composed or the user gave): a new text file as {\"name\": \"<file name with "
            "extension>\", \"text\": \"<full content>\"}, or text to add at the end of a text file as {\"target\": "
            "\"<drive: ref of that file>\", \"text\": \"<text to add>\"}. Only what is written into a file's "
            "content. Leave it out when no file content is written."
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
  part of the commission, never rejected (`decisions/mcp_tool_schema_liberal_input.md`). The Smart-era drive fields
  (`drive_query`, `drive_folder`, `drive_kind`, `drive_date_from`, `drive_date_to`, `drive_deleted`, `new_name`,
  `source_ref`, `original`, `name`) that a stale skill or reminder may still make Smart send are passed through as
  labelled extras under "Also given with the commission:" (`_STRAY_FIELDS`; an empty value or `False` is left out), so
  the value is not lost silently.
- The name is unique in the union schema, so its description is not shadowed (`base_agent.py:796`). The prose names
  only `file_contents`, so `test_manifest_prose_matches_tool_schema.py` passes (no nested keys in the prose).

### 4.3 The protocol (`prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy`; the live copy, not
`firestore_utils/downloads/`, which is stale)

Line 26, edited in place, so the exception sits where the rule sits (research 03 A.2: with the exception only in the
description, the generic rule wins: nearly all Tasks commissions were English):
```
query_language: "Always English — regardless of the user's input language — except the words to find on the drive in work_with_drive_files: quote the user's own few key words, in ONE language, as the user wrote them — no synonym lists, no translations, no extra words."
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

---

## 5. The specialist

### 5.1 Identity and configuration

| Item | Value |
|---|---|
| Class, file | `DriveAgent(BaseAgent)`, `src/agents/drive_agent.py` |
| Descriptor | `agent_id="drive_agent"`, `agent_type="drive"`, `capabilities={WORK_WITH_DRIVE_FILES: SYNC}`, `allowed_intents=frozenset()`, `prefetch_file_ref=False` (the coordinator must not download a `file_ref` first, as for FileManagement), `internal=False` |
| Tier, model | **`claude-sonnet-5-5` (Claude BALANCED), adaptive thinking, effort `medium`** (`_DEFAULT_AGENT_TIERS["drive"] = BALANCED`, `agent_thinking["drive"] = "medium"`; owner decision 2026-10-22). Fastest specialist in the final probe (§15.5) |
| Strategy | `STRATEGIES["drive"]`: default `claude`, allowed `claude`/`openai`, `native_tools` required, fallback `openai` with `fallback_tier=PERFORMANCE` (→ `gpt-6.1-sol`; OpenAI's BALANCED is luna). The fallback covers provider errors on the first call only: after a tool turn the transcript is provider-locked and `BaseAgent` raises instead of failing over. It is not a guard against the filler-tool stall (below) |
| Sonnet 5.5 stall | The known filler-tool stall (`decisions/sonnet55_adaptive_thinking_stall.md`) did **not** appear in the specialist role. The owner's explanation: it needs Smart's complex structured answer plus many intents; the specialist has a few tools and a plain answer. The stall is bounded by 6 turns and 150 s, and a turn-cap exit without a report is logged at ERROR and alerted (`decisions/sonnet55_adaptive_thinking_stall.md`: no workaround, loud monitoring) |
| Rejected (§15.3, §15.5) | `gpt-6-luna`: too weak (owner) and slower in the final probe (91 s of specialist time against Sonnet's 38 s for 5 cases). `grok-4.7`: slowest (151 s+), most expensive, hit the cost cap. `gemini-3.8-flash`: ≈ 15–20× luna's price and ≈ 3× slower in the replay |
| Retry | `RETRY_POLICY = NO_RETRY_POLICY` (the `AlekGatewayAgent` precedent): a whole-execution retry after a write repeats the write. The same-provider retry inside `_call_llm` stays |
| Config | `DriveAgentConfig(max_tokens=4096, max_turns=6, timeout_ms=180_000)` in `agent_config.py` |
| Cost | ≈ **$0.05 per commission** in the final probe ($0.24 of Sonnet for 5 commissions, `billing.py:203`: $2 / $10 per M). The probe read nothing from cache; the static prefix is cacheable at 0.05×. `"drive"` joins `CACHEABLE_AGENTS` and `MULTI_TURN_AGENTS` (the last-message breakpoint); the pre-build check verifies `cache_read_tokens > 0` on the second turn of a commission |
| Latency (measured) | with a luna specialist: median drive-turn wall time equal to today, a simple find or save +5–10 s (§15.1); with Sonnet the specialist time fell to 38 s from luna's 91 s over 5 cases (§15.5), so the turn is expected faster; the stage-1 rerun measures it |
| Logging | Overrides `_on_agent_start` (the default logs the commission's first 60 characters). One line per commission: `[DriveAgent] user=<8> status=… model=<model that ran> ops=search:ok,move:ok turns=N ms=…`. `model=` is read from the execution's billing ledger (`TokenLedger.by_model`, keyed by the model that served each call), so a cross-provider failover shows as the fallback model, several models are joined by `+`, and the configured model appears only when no call was billed; it serves owner step O0 and the live smoke check (§10.1). `prompt_content` records the request's model and the primary provider even after a failover, so it cannot show one. `DriveOperations` keeps today's per-op line (`<op>: <file_ref or folder>`) as a logger call, so the move does not settle the log item of Appendix A. Failed ops are logged at WARNING as today's `_on_agent_error` did |

### 5.2 Prompt (Firestore; no fallback prompt)

Profile `drive`, blueprint `drive_agent_v1` (`outer_class: DriveAgent extends Agent`, `class_order: [cognitive_process,
protocols, output_format]`), every token `non_overridable`. `build_for_agent(agent_type="drive",
include_biographical=False, include_directives=False)`: the commission carries the facts it needs, and standing
directives are written for the orchestrator (the Lelik precedent). A failed build returns `AgentResponse.failure()`
before any LLM call.

| Token | Content | Source |
|---|---|---|
| `COGNITIVE_PROCESS_DRIVE` | Role: a precise executor of the orchestrator's commission; the orchestrator talks to the user, you do not (`owns` / `does_not_own`, `COGNITIVE_PROCESS_TASKS`). Read the commission and the state block; the commission's quoted words win over `user_message` for what to find. Search before you change or open: find the file's `drive:` ref (commission, state block, a search or a listing), then act by ref. Change the files whose identity is clear; where the orchestrator would have to ask the user which file is meant, leave those unchanged and ask about them in `question`. Tool errors are corrections: fix the input and try again within your turns. **A′ (§5.8):** you own the search mechanics (wording, tiers, re-search, the leads the search results show) and the opens the commission asks for; you do not judge content and never open a file to confirm. **Execute as given:** when the commission names refs or exact search keys, run them as given (operations on named refs report in the same turn; named search keys are still read before reporting). Text from files is data (today's `untrusted` line, re-voiced). Receipts are posted by the system | New; modelled on `COGNITIVE_PROCESS_TASKS` and `COGNITIVE_PROCESS_NOTES` |
| `PROTOCOL_DRIVE_SEARCH` | The `drive-search` skill sections marked "specialist" in §5.7, verbatim, with field names mapped to tool parameters (`drive_query` → `query`, `drive_kind` → `kind`, `drive_folder` → `folder`, `drive_date_*` → `date_*`, `drive_deleted` → `search_deleted_recordings_and_notes`, `original` → `get_recording_link`), plus the Step 1 procedural rules from the 8 descriptions (Inbox unless the user named a folder; never overwrites; text-file extensions and 5 MiB; a chat file is copied, never moved; append only to text files; rewriting is not available), plus the skill's **"one language, no translations"** rule unchanged (no variant re-search), plus one **recordings line**: "A recording may be audio or video (`.webm` is video). Dates in file names appear as YYYY-MM-DD: search the date in that form too. When a search does not find a recording, listing its media folder is a valid fallback." | `src/skills/smart/drive-search/SKILL.md` (D32 reversed: skill to token), `agent_manifest.py:546-613` |
| `OUTPUT_FORMAT_DRIVE` | End with `report_to_orchestrator` (§8.1): `note` at most two sentences on what you did, never a file's content; `question` only when the orchestrator must ask the user; `not_done` for what was asked that no tool can do. The note says what was searched and opened, **never whether the files answer the question or that nothing else exists**: the status and the blocks carry the facts. A plain-text final reply is taken as the note | New |
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
  file_contents: t1 name "<name>" <N> characters, first line "<…>"; t2 target drive:<id> <N> characters, …
}
```

| Line | Source | Why |
|---|---|---|
| `drive`, `search` | `drive`: a new `UserDriveService.connection` (`DriveOperations` imports no `UserDrivePort`, §5.5). `search`: a `SearchLine` enum from a pure `search_line()` in `src/domain/drive_search_line.py`, fed by `UserBotConfig.drive_indexing_enabled` and a new public read `DriveSearchService.search_state(user_id)` over the same sync-state gate a search reads today | The model knows up front what can work; code delivers today's text for these states anyway (§7.2) |
| `now` | an injected clock (not the coordinator's `[Mon DD, HH:MM UTC]` prefix) and `UserBotConfig.timezone` | Date filters in the user's timezone |
| `user_message (<origin>)` | text parts of `context["current_message_parts"]` without `file_data` parts (`[File: …]` labels are filtered). The origin label is `chat`, `call (Lelik's words)` on `ask_alek`/`tell_alek` (`alek_gateway_agent.py:101`), `background: <task type>` on notify paths, `fallback` on the Quick fallback (`agent_fallback_service.py:126`) | **The user's own words** on chat turns, required by strict-AND search keys (§2.3.5). Off chat the label says whose words they are; on a call they are not the owner's spoken words |
| `attached_now` | `MessagePart.file_data` of the same parts | "Save this" without Smart copying the ref |
| `file_contents` | the commission's field (§4.2) | The model picks the tool and the item; it never types the content |

The specialist does not read session history. Smart holds `drive_context` and passes refs in the commission
(research 04 §5): no new plumbing.

### 5.4 The loop

`DriveAgent.execute` starts `DelegationEngine.execute(local_tools=handlers, terminal_tool="report_to_orchestrator",
max_turns=6, use_turn_clock=False, allow_delegation=False)` **as a task** and waits on it (§5.6). Handlers are built per
execution (the `workspace_tools.py` pattern) and write every outcome into the per-execution collector (§6).

| Rule | Mechanism |
|---|---|
| No delegation | `allowed_intents=frozenset()`, no `delegate_to_specialist` declared. **Engine delta:** `execute(..., allow_delegation: bool = True)`; `False` drops `DELEGATE_TOOL_NAME` from `declared` (`delegation_engine.py:595`), so a stray call gets the existing unknown-tool answer. It also covers a delegate call co-emitted with the terminal tool: the sibling dispatch uses the same declared set |
| Read before you report | `report_to_orchestrator` may arrive in the same turn only with operations on given refs (open, copy, save, move, delete); the engine runs siblings first (`:339`). After a search it comes in a later turn, so the specialist sees "Without X", folder cards and coverage, and follows their leads (A′) |
| Identical search skipped | A `search_drive` call identical to one already run in this commission (same normalised query, folder, kind and dates) is not re-executed; its tool result points at the earlier block (`same as r1`). The replay showed up to 9 searches in one commission (§15.3) |
| Batch order | Every mutating handler first awaits a per-execution ordering lock: `gather` creates the tasks in emission order and `asyncio.Lock` is FIFO (the `drive_note_tools.py` pattern) |
| Per-user mutation lock, shield, still-running, late-failure notices | Unchanged, on the per-user `DriveOperations` (§5.5): a shielded task outlives a cancelled run |
| A change cut after it started | The engine's cancel reaching a change that has started is not a failure: it is recorded as `still_running` with today's `STILL_RUNNING` text, never `unfinished` or "did not finish" (a read cut the same way stays `did not finish in time`). A change that takes the ordering lock after a still-running outcome of this run is not started: `not started: an earlier change in this request is still running`. A change cut by the deadline before it started is `not started: the time for this request ran out`. The late notice is as today |
| `still_running` ends the commission | The collector sets an `asyncio.Event`; `DriveAgent` waits on `{engine task, event}` with `FIRST_COMPLETED`, cancels the engine task and builds the answer. No engine stop flag |
| Calls emitted on the last turn | They run (mutations included); the model never sees their results, the ledger reports them |
| Deadline, engine exception, turns exhausted | Code builds the answer from the collector; the status is `unfinished` whatever finished, because the run has no report (§7.2), with the blocks and op lines kept. A turn-cap exit without a report is also logged at ERROR (the per-turn tool-name sequence and the first 8 characters of the user id, never arguments or file names) and posts one 🚨 line through the ops alert sink (`DriveAgent(alert_sink=…)`, best-effort, `asyncio.wait_for(…, timeout=5)` inside the existing try, so a hanging sink still returns the answer) |
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
| Hooks | FileManagement's `_on_agent_success` / `_on_agent_error` stay on FileManagement, where its own code calls them; `DriveOperations` logs through `logger` directly, with the same content. It exposes `READ_METHODS`, `MUTATION_METHODS` and `OP_OF` (method to `DriveOp`), with a completeness test; `DriveAgent` has an `operations` property |
| Why here | REQ-ARCH-24 forbids one agent importing a sibling agent module; precedent `workspace_tools.py`, `drive_note_tools.py`. Services are constructor-injected; REQ-ARCH-14 allows `TYPE_CHECKING` imports |
| One instance per user | The factory memoises it in the per-user context (`_UserContext`); `_build_drive` and `_build_file_management` (`user_agent_factory.py:855-873`) both read that slot. The lock, `_shielded`, `_armed`, `_targets`, `_notices` live on it |
| Path-like refs | A ref that contains `/` and has no known prefix (`drive:`, `workspace:`, `skill:`, a delivered-document key) in `open_drive_file`, `append_to_file`, `move_file`, `delete_drive_item` or `replace_file_content`'s target gets the correctable `REF_REQUIRED` refusal (class `REQUEST_ERROR`: find it with `search_drive`), instead of a storage fetch or a wrong-store text. A bare name keeps today's wrong-store text. Chat file names, `docs/`, `email_review/`, `deep_research/`, `video_generation/` keys and any string holding a `drive:` ref are not affected |
| Unchanged guarantees | The `_Transcripts` guard, root protection, never-overwrite, append-only and the move rules stay in `UserDriveService`; `DriveOperations` imports no `UserDrivePort` and no adapter (architecture test) |
| FileManagement | Until the cut it calls `DriveOperations` for its 8 intents behind its unchanged surface (§10 step 1). After the cut it keeps only `open_file` / `delete_file` (GCS chat files, delivered documents, `skill:` and `workspace:` refs, the lenient `drive:` open through `DriveOperations`) |

### 5.6 Time

| Budget | Value | Reason |
|---|---|---|
| Hard timeout | 180 s (`BaseAgent._execute_with_timeout`) | A backstop only; the soft deadline is meant to end every run first |
| Soft deadline (absolute, monotonic) | `min(start + 150 s, now + clock.call_timeout() − 15 s if CURRENT_TURN_CLOCK is set, CURRENT_RUN_DEADLINE − 20 s if set)` | The run ends with its collector before any outer limit, so Smart learns what was done |
| `CURRENT_RUN_DEADLINE` | **New:** a process-local `ContextVar` (monotonic), the `CURRENT_TURN_CLOCK` precedent. `BaseAgent._execute_with_timeout` sets it for orchestrators from their own timeout and **clears** it when an orchestrator runs without one (`base_agent.py:684`); each orchestrator overwrites the outer value. It is never put in `message.context`, so it never rides into a Cloud Task payload (`agent_coordinator.py:429`), where a monotonic value means nothing; `gather` copies it into the specialist's task | Background SLAs: an ECO reminder fire gives Smart 180 s (`notification_sla.py:84-89`), interactive notify and deep-research delivery 300 s, none with a turn clock. Today FileManagement stops at 120 s (`user_agent_factory.py:864`) |
| Engine task | `asyncio.wait_for` until the soft deadline; on timeout it is cancelled, shielded mutations keep running | Every exit returns the answer from the collector |
| LLM calls | `DriveAgent`'s `call_llm` closure passes `timeout = min(120 s, soft deadline left)` | An overrunning call cannot cross the deadline |
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

### 5.8 Division of labour (A′, research 09)

Production data decided it (research 09 §1): most words in Smart's follow-up searches came from the conversation, a
few from earlier cards or listings, the rest were Smart's own variants, and **none appeared only in a file opened that
turn**; most opens were images or PDFs, which only Smart sees. So the specialist stays blind to opened content
(§6), takes the mechanical loop, and Smart keeps every judgement about content.

| Decision | Owner |
|---|---|
| Is the drive relevant; find or value; the user's words to find | **Smart** (the find rule in §4.1 states find vs value) |
| Search mechanics: key words from the user's words, one language, tiers, one re-search after "Without X", folder cards, partial folder names, a date filter dropped (§2.3.11: no variant re-search), leads the search results show | **specialist** |
| The opens the commission asks for; which file to open for a value (the 90 % rule) | **specialist** |
| Never re-run a search that ran; never open to confirm | **specialist** (prompt) + **code** (an identical search is skipped, §5.4) |
| Is this the answer, is it complete, which version is current, document vs memory | **Smart** (reading lines, §7.3) |
| A follow-up from what an opened file names (a file, a person, a date, a term) | **Smart**, by commissioning again (the opened-file reading line, §7.3) |
| Ordinals; what a vague change covers | **Smart** |
| Which file is meant when the identity is unclear | **specialist** asks through `question`; **Smart** asks the user |
| A commission that names refs or exact keys | **specialist** executes them as given (C′ as one prompt rule, §5.2). Smart already writes keys into commissions on its own (§15) |

The specialist's note never claims completeness (§5.2). **Considered and not taken** (research 09 §4): B (the
specialist reads opened files: gains only in a case that did not occur, fails on scans and vision), C and D (Smart
picks opens or re-searches: a full Smart round per decision), C′ as a policy (search rules back on Smart, today's
trial-and-error returns); C′ survives as the execute-as-given rule.

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

**Triggers.** The reading lines (§7.3) and the state lines (§7.2) are decided from `ReadFacts`, which `DriveOperations` sets from the typed result, plus `DriveOutcome.body` for stand-ins. They are never decided by parsing the rendered text.

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
<state line>                     (today's text for a drive or search state, §7.2)
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
  (→ `ambiguous` when no change succeeded, else `partial` with the question as the line), `not_done` (→
  `not_possible` when no change succeeded, else `partial`) and its note, rendered after the status lines. Both can
  only lower a status, never raise one to `ok`.
- `ToolResult.failed` is never set on a drive answer; nothing reads it outside fan-out (`delegation_engine.py:807-855`),
  and FileManagement's returned failures counted as successes for the circuit breaker anyway.

### 7.2 Statuses, precedence, and today's messages

The status is the first that applies. A run that reported keeps the precedence of rows 3-9. A cut run keeps its blocks and its op lines (`done`, `not done` and, for a change that is still running, `still running`).

| # | Status | When | "What to do" |
|---|---|---|---|
| 1 | `still_running` | a mutation passed its wait, or a change was cut after it started (its op line says `still running`; a change queued behind it is `not done: not started: an earlier change in this request is still running`) | today's `STILL_RUNNING`; the late notice as today |
| 2 | `unfinished` | the run ended without the specialist's report (the soft deadline, an engine exception or the turn limit), whatever finished; or a read was cut by the soft deadline or a call was not started after it. A **change** cut after it started is row 1 (`still_running`), not this row | `The drive assistant could not finish; what ran is listed below. Tell the user what ran and what did not; do not commission the same request again in this turn.` |
| 3 | `partial` | some changes done, some not (or a `question` or `not_done` after some changes) | `Only part was done:` + the op lines |
| 4 | the first failed change's status (`refused`, `failed`, `not_found`, `not_connected`, `not_possible`) | changes attempted, none done; a text tool refused for a missing or unknown item counts as a failed change of class `not_possible` | today's text of that op; for a missing item: `Nothing was written: the text was not attached. Commission again with context.file_contents holding the full text.` |
| 5 | `ambiguous` | the model's `question`, no change done | the question, with the candidates' cards among the blocks |
| 6 | the preflight's connection state (`not_connected`, which also covers `reconnect needed`; `failed` for not configured) | no operation ran at all | today's text: `_NOT_CONNECTED` ("…Ask them to connect it in the Cabinet.") / `The drive is not configured.` |
| 7 | `not_possible` | `not_done` given and no change done, **reads included**; or no operation ran and no question | `Nothing was done: <not_done or specialist note>` |
| 8 | `ok` | at least one change done and none failed, or a read produced content | `Done.` / `Answer from the results below.` |
| 9 | the most informative read failure: `not_connected`, `rebuilding`, `indexing_off`, `refused`, `failed`, `not_found` | reads only, none produced content | today's text of that read; for a `search` read classed `not_found`, `Answer from the results below.` (the results carry the not-found facts) |

**State lines, delivered by code.**
- **Connection state** (`not connected`, `not configured`): when the preflight (§5.3) shows it and **no operation ran**
  (the model skipped the tools), code renders today's text as the state line (row 6). Where an operation ran, its own
  result carries today's text, as now.
- **Search state** (`indexing off`, `rebuilding`): on an answer that **ran a search**, that search's own text
  (the `disabled` text, `REBUILDING_LINE`) is raised into the state line. When the preflight's search line is not `on`,
  **no search ran** and the answer holds reads only, the answer carries today's `disabled` / `REBUILDING_LINE` text as
  its state line (a listing or an open during a rebuild says so). A change-only answer (a save, a move) stays clean.
- A missing drive service (`not configured`) answers at once, with no LLM call.

| Status | Today's outcomes (`file_management_agent.py`, `drive_search.py`; research 05 §1 has the full list) |
|---|---|
| `ok` | every success today: saved, already there, saved as a suffixed name, listed, opened, transcript, link, folder created or existed, moved, appended, replaced, deleted, search with cards, deleted search with hits; an ambiguous folder; the success-shaped refusals (HEIC, image > 5 MB, PDF > 20 MB) |
| `not_connected` | `_NOT_CONNECTED`; search `disconnected` |
| `indexing_off` | search `disabled` |
| `rebuilding` | D30 `REBUILDING_LINE`; `other_drive` |
| `not_found` | the gate `rebuilding` (a purge); a deleted search without hits; `_GONE`; chat file not found; `No matching files or folders.`; D29 lines; no conjunction; folder not found |
| `refused` | `ACCESS_NOTE_FILE`/`_FOLDER`; `_NAME_TAKEN`; too large; root protected; `DrivePathError` texts; `DRIVE_MIRROR_WRITE_REFUSED`; request-shape errors the specialist could not fix in its turns |
| `failed` | `Drive operation failed: <Type>.` (including the provider `TimeoutError` inside a mutation, pinned by `test_provider_timeout_inside_a_mutation_is_a_failure`); `The drive is not configured.` |

Request-shape errors (both or neither source, an empty field, a bad date or kind, `NOTHING_TO_SEARCH`, a folder ref
given as a file) go back to the specialist's model as tool results in today's words (§7.6) and are corrected in its
loop. A correctable error the specialist fixed in a later step (a later call of the same operation that is not itself correctable) is left out of the status, and for a read out of the blocks. A change's op line is never dropped: a settled change error stays as its `N. not done: <today's text>` line, so the orchestrator sees every failed call, as it does today.

### 7.3 Reading lines for Smart (the skill's Smart-side rules, verbatim, with triggers)

| Trigger (decided by code from `ReadFacts` and `DriveOutcome.body`, not from the rendered text; §6) | Line(s) from `drive-search` SKILL.md |
|---|---|
| a search block with cards | the tiers paragraph (`PROTOCOL_TIERS_LINE`) and its three bullets; "Find … answer from the search results alone"; "Many results: …"; "'All', 'every', 'everything about' is still a find: … do not … compute totals unless the user asked for a total or a value" |
| a card with a passage | "A passage is a window cut from the file … If the file cannot be opened …, quote the passage and say it is an excerpt." |
| a card with `card accuracy:` | the card-accuracy paragraph, "find … at any accuracy", and "A question about a value in a file …: answer from the card when its accuracy is 90% or more. Below that, open the file and check the value before answering." |
| a card whose capture is a scan or a document photo | "For a scan or a photo of a document, the file's text is a reading of the image; when a value matters to the user, say where you read it." |
| a card or result with the bot-cannot-open line | the whole bot-cannot-open bullet, including "answer from the card and say it may be out of date; do not open the file" |
| an image card | "Photos: answer from the description, place and date in the result." |
| two or more cards | the Versions bullet |
| a not-found, or coverage incomplete, or the index building, **in a search block** | the three "When nothing is found" bullets (not when the status comes from a change and the search only found the ref) |
| an opened-file block | the Numbers bullet ("compute from the opened text") and "Document vs memory"; and one new line (A′): "The drive assistant did not read this file; if it points to another file or a term to look for, commission again with it." |

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
out`, `not started: an earlier change in this request is still running`, the op verdict `still running`, the `REF_REQUIRED` refusal (§5.5), the `unfinished` line, the two `not_possible` lines, the A′ opened-file reading line (§7.3) and the `same as rN`
result of a skipped identical search (§5.4); and, from the build: `Not done: {not_done}` (partial), `no drive operation ran`, `Also given with the commission:`, the unknown-item refusal (`No file_contents item '…' (items: …). Nothing was written …`, which §7.2 names but does not spell) and `{field} is required: {what}.` (an empty `file_ref` or `source_ref` is refused in the tool before any call). Every
other line Smart or the specialist reads is today's text (after §7.6).

### 7.6 Strings that name intents or fields Smart no longer has

About 30 strings name old intents or fields (`list_files_in_drive`, `open_file_from_drive`, `drive_folder`,
`drive_query`, `drive_kind`, `drive_date_*`, `save_file_to_drive`, `move_file_in_drive`, `delete_file_from_drive`) in
`drive_search.py`, `drive_search_texts.py`, `drive_search_service.py`, `drive_deleted_search.py`,
`drive_transcript_render.py`, `file_conversion_service.py`, `user_drive_service.py` and FileManagement. The guard also covers docstrings that name a removed intent, and the result strings of `FileConversionService` and `UserDriveService`. **Model-facing texts move verbatim (a move, not a rewrite):** today's descriptions, protocol lines and skill sentences are carried over word for word; only references to removed intents and fields are renamed, and the sentences the rules below name are dropped. Two rules:

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
| `copy_file_to_drive` | Keep a copy of a file on the drive; never overwrites, the result states the name used. | `source_ref`: a chat file name from `[File: …]`, a delivered document key, a `workspace:<path>` file or a `skill:` file; never a `drive:` ref. `folder` (opt, only if the user named one; default Inbox; created if missing). `file_name` (opt; the source's extension is kept if omitted) | `copy` |
| `save_text_file` | Save one attached text as a new text file; never overwrites. | `text_item`: `t1`… from `file_contents` (a value that names no item is refused and nothing is written). `file_name` (opt: defaults to the item's name; ends in .md .txt .csv .json .yaml .yml .html .svg or .xml). `folder` (opt; created if missing) | `save_text`; content = the item's text |
| `create_folder` | Create a folder and any missing parents. | `folder` | `create_folder` |
| `move_file` | Move and/or rename a file or folder in one step. | `file_ref`; `folder` (opt: destination; created if missing); `new_name` (opt: extension included for files); at least one of them, checked in the tool with the service's own refusal text before any call (correctable, not a `refused` change); the sentence is one domain constant | `move` (one `svc.move(folder, new_name)`, as today) |
| `append_to_file` | Add one attached text at the end of a text file. | `text_item`: `t1`… (a value that names no item is refused and nothing is written); `file_ref` (opt: defaults to the item's target) | `append`; text = the item's text |
| `replace_file_content` | Replace a drive file's whole content with another file's; the user gets a notice with old and new size. | `target_ref`: the `drive:` file whose content is replaced. `new_content_ref`: the file whose content goes in: a chat file, a delivered document, a `workspace:<path>` file, a `skill:` file, or another `drive:` file | `replace` + receipt |
| `delete_drive_item` | Delete a file or a whole folder (recoverable from the recycle bin); the user gets a notice. | `file_ref` | `delete` + receipt |
| `report_to_orchestrator` (terminal) | Finish: what you did, in one or two sentences. | `note`: at most two sentences, never a file's content (the description adds: "The system delivers every result to the orchestrator."). `question` (opt): only when the orchestrator must ask the user which file is meant; name the candidates. `not_done` (opt): what was asked that no tool can do; empty otherwise | — (code builds the answer; **every** read block is delivered, §6) |

`save_text_file` and `append_to_file` are always declared; without a matching item the handler refuses with no service
call, and the refusal is classed `not_possible` (§7.2). `OUTPUT_FORMAT_DRIVE` describes `not_done` next to `question`.

**Built, not deployed:** `USER_DRIVE_INDEXING_RFC.md` §28 (answer from content, 2026-10-28) adds the read tools `find_passages` and `read_passages`, which the specialist sees in full; it amends §2.3.11, §5.8 (A′), §6 and `execute_as_given` for reads (§28.3 lists each), and, by the owner's decision of 2026-10-28 (OQ-A1), §2.2 row 1: a run that is not a chat turn (a background run or a call) refuses its changes for the rest of the run once it has read passages (§28.3 A14).

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
| "Restore the file I deleted" | perhaps a search first, then `report_to_orchestrator(not_done="restoring a deleted file")` → `not_possible` | no false `ok`, also after reads |

---

## 9. Notes inside the specialist (a later, separate change)

The frozen core (`src/domain/drive_note.py`, the port, both repositories, `DriveNoteService`) and a Smart-wired build
(the `annotate_drive_file` local tool in `smart_response_agent.py`, the open-header note in `file_management_agent.py`,
the engine's sibling-history hunk, the `$drive-notes` commands, the `drive-file-notes` skill) are on
`plan/drive-notes-assembled`; the plan is `docs/superpowers/plans/2026-10-22-drive-notes.md` there. This RFC ships
first. The notes change then **removes the Smart-wired commits** and rebuilds part A inside the specialist (§9.3);
parts B and C arrive with one merge of that branch. **Built on `feat/drive-notes`** (not deployed); §9.6 lists where
the build differs from this text.

### 9.1 The owner's rules, and where each is enforced

| Rule | Enforced by |
|---|---|
| A note is written only on an owner turn (chat, or a call through `ask_alek` / `tell_alek`) | The write tools are declared only when `is_owner_turn(message.context)` (the plan's layer 1, unchanged) and each write handler checks it again before any service call. The gate reads `message.context`, never `payload` (the model's `context` argument lands in `payload`, `agent_coordinator.py:526-532`) |
| Classifiers only when the owner names or confirms them | In the **parameter descriptions** of the tools that set them (§9.2), plus `PROTOCOL_DRIVE_NOTES` and Smart's sentence (§9.5); the core resolves names against the fixed enums |
| Erase needs a code | `erase_note` → `request_erase`; the code rides only in `DeliveryItem.data`, never in a `result_str`; `$drive-notes erase <code>` runs in `ConversationHandler` before any LLM call (plan C6) |
| D21: an archived or other-drive note is edited, undone, cleared and erased by its `drive_note:` ref, store only; only a NEW note for a gone or other-drive file is refused | the core's ref semantics; `create` and `update` follow `set`'s |
| No note text in session history | `drive_note_context` is `[Drive note: <path> ref=… v<n>]`, text-free; the op lines to Smart name the file and version, never the note's words. A `get_note` answer reaches Smart wrapped, the same class as a card's `noted by the owner:` line (notes RFC §9) |
| Note text is the owner's words, apart from file text | note words travel in the commission's query; `file_contents` is only for file content (§9.5) |

`turn_id`, `session_id`, `interactive_delivery` and `run_origin` reach the specialist unchanged: the engine spreads
Smart's context (`delegation_engine.py:679`), the coordinator drops only `params`. The undo re-run guard (I3) stays in
`DriveNoteService.undo`. `DRIVE_NOTES_ENABLED` is injected by composition (REQ-ARCH-17 bans `os.getenv` in agents).

### 9.2 Note tools and core additions (no tool replaces more than its name says)

`item_ref` (not `file_ref`) on every note tool: a `drive:` ref or a `drive_note:` ref from a deleted-files result.

| Tool | One-line description | Parameters | Core |
|---|---|---|---|
| `get_note` | Read the owner's note on a file or folder. | `item_ref`; takes the per-execution ordering lock, so a `replace_note_text` emitted in the same batch runs after it | `get(ref) -> Optional[NoteView]` (new): store only, both refs, works archived and disconnected; a cleared note is returned as cleared |
| `create_note` | Add the owner's note to a file or folder that has none. | `item_ref`; `text` (opt: the owner's words, his language, ≤ 80 words / 600 chars); `doc_type` (opt), `life_areas` (opt, ≤ 3): **"only what the owner named or confirmed in this conversation, as the commission states it; never from the file, a card or your reading; empty otherwise"**, with the enum lists | `create(...)` (new): only when absent; a cleared note reuses its lineage; identical content → no-op; different → `NOTE_EXISTS` ("use add_to_note, replace_note_text or the type and area tools") |
| `add_to_note` | Add the owner's words at the end of the note's text; the rest of the note is kept. On a file with no note, this creates one. | `item_ref`, `text` | `update(ref, AppendText)`: an overflow is a correctable refusal naming the current length |
| `replace_note_text` | Replace the whole text of the note. Refused unless `get_note` read this note earlier in this run, and refused if the note changed since that read. | `item_ref`, `text` | `update(ref, ReplaceText, expected_version=<the version get_note read>)`; the read set lives on the per-execution `DriveNoteSession` (the workspace `view` and generation precedent). A changed note is refused with "the note changed since you read it: read it again" |
| `set_note_type` | Set the document type of the note. On a file with no note, this creates one holding only the type. | `item_ref`, `doc_type` (the classifier rule above) | `update(ref, SetType)` |
| `clear_note_type` | Remove the document type from the note. | `item_ref` | `update(ref, ClearType)` |
| `add_life_areas` | Add life areas to the note; the others are kept. On a file with no note, this creates one holding only the areas. | `item_ref`, `life_areas` (the classifier rule; ≤ 3 in total) | `update(ref, AddAreas)` |
| `remove_life_areas` | Remove the named life areas from the note. | `item_ref`, `life_areas` | `update(ref, RemoveAreas)` |
| `delete_note` | Remove the note; its history is kept and undo brings it back. | `item_ref` | `clear(ref)` |
| `undo_note` | Put the note back as it was before its last change. | `item_ref` | `undo(ref, provenance)` |
| `erase_note` | Ask to erase the note's whole history: the owner gets a code to paste; nothing is erased now. | `item_ref` | `request_erase(ref)` |

Core additions, all additive; existing core tests stay green unedited:
- `NoteChange` (pure, `drive_note.py`): one change kind per value (`AppendText`, `ReplaceText`, `SetType`,
  `ClearType`, `AddAreas`, `RemoveAreas`), each validated by the existing checks; `NoteView`; refusals
  `NOTE_EXISTS`, `EMPTY_CHANGE`.
- `update(ref, change, expected_version=None)`: on a note with content it applies the change. With no note, or a
  cleared one, the additive kinds (`AppendText`, `SetType`, `AddAreas`) create the note holding only that change, under
  `create`'s rules (a cleared note reuses its lineage; D17 and D21: no NEW note for a gone or other-drive file); the
  other kinds answer `NO_NOTE` ("use create_note"). `drive:` ref follows `set`'s live path (D17), `drive_note:` store
  only (D21). `expected_version` (from `get_note`) refuses when the current version differs.
- private `_write(compose)` shared by `set`, `create`, `update`: `compose(current)` runs **inside** the conflict-retry
  loop on each attempt, so a concurrent write is re-merged, never overwritten; the content is **re-embedded whenever
  it differs from the content last embedded**; a composed triple equal to the current one is a **no-op receipt, never
  a refusal**, and removing an absent type or area is a no-op (a re-run after a provider rotation stays safe).
- `set`'s public behaviour is unchanged. No tool can call `set` with an empty triple.

### 9.3 Revised part A of the notes plan

| Task | Verdict | What |
|---|---|---|
| 0 | = | core cleanup |
| A1 `drive-file-notes` skill | **replaced** | its text becomes `PROTOCOL_DRIVE_NOTES`, re-voiced, with one tool per edit kind; "replace, never append" retired; one added line: "A `file_contents` item is never a note's words; when a note change has its words only there, ask through `question`." |
| A2 `LoadedSkills` (skill gate) | **dropped** | the rules are always in the writer's prompt |
| A3 receipt renderer, `UIMessage.DRIVE_NOTE_*`, locales | = | the emitter moves, the receipt does not |
| A4 `annotate_drive_file` | **rewritten** | eleven handlers in `drive_note_tools.py`: `note_provenance`, `DriveNoteSession` (lock shared with the drive mutations and taken by `get_note`; the read set with each note's version), shield, `NOTE_WRITES_PER_TURN`, the truncated-JSON refusal and the receipt builders kept; the skill gate, the flags and the empty-triple routing removed. `turn_identity.py` kept; the engine hunk dropped (the collector covers it) |
| A'1 (new) | new | `NoteChange`, `NoteView`, the refusals (three, §9.6) |
| A'2 (new, Opus) | new | `get`, `create`, `update`, `_write(compose)` with re-embed and no-op rules |
| A5 Smart wiring | **replaced** | the service and `DRIVE_NOTES_ENABLED` go to `DriveAgent`; six of its nine cases are ported (including the rotation re-run keeping the turn id), three retire with the skill gate (§9.6); the Smart-wired commits are removed |
| A6 turn id on the three owner paths | = | |
| A7 / A8 receipts in chat and on the call path | = | the items now come from `DriveAgent` through Smart's fold. `DriveAgent` takes them, and the `drive_note_context` lines, from its pass-through collector, never from its engine: a run cut by the soft deadline has no engine result, and a stored note must keep its receipt. Success on every status keeps them. Both are sent literally (§9.6) |
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
the query. Not for reminders ("make a note to call…")." To the `file_contents` description: "Never a note's words." A pin test asserted both were absent until then; both are in `agent_manifest.py` now.

### 9.6 As built (`feat/drive-notes`)

Where the build decided what §9 left open or said otherwise (the port design's Δ numbers, as cited in the code). The
notes RFC records the same in its §0.5.

| # | Built |
|---|---|
| Δ3 | `NoteChangeKind`, `NoteChange` (+ `apply_change`) and `NoteView` are in their own modules (`drive_note_change_kind.py`, `drive_note_change.py`, `drive_note_view.py`), not in `drive_note.py` (one public class per file); `drive_note.py` gains only the refusals |
| Δ4 | Three refusals, not two: `NOTE_EXISTS`, `EMPTY_CHANGE`, and `CHANGED_SINCE_READ` for the stale read §9.2 describes |
| Δ5 | A change that would leave a note empty (clearing a type-only note's type, removing its last area, a replace with no text) is refused with "use delete_note", never a silent delete |
| Δ6 | `get_note` is declared on every run where the note service is wired, `DRIVE_NOTES_ENABLED` is on and the run has a user; only the ten writers need an owner turn |
| Δ7 | Receipts, erase requests and `drive_note_context` ride the pass-through collector (`DriveOutcome.delivery`, `.note_context`), so a cut run keeps them; `DriveAgent` never reads its engine's `delivery_items` or `history_contexts`, and the engine's sibling-history hunk is not carried |
| Δ8 | No op line or tool result carries the note's words except `get_note`'s wrapped block: the old result's "previous text, as data" is gone |
| Δ9 | The open header (B8) reads the drive id through `UserDriveService.connection` in `DriveOperations`, never a port; it adds `ReadFacts.owner_note`, which triggers Smart's owner-note reading line |
| Δ10 | A write is a task held by the per-user `DriveOperations.keep_alive` and awaited under `asyncio.shield`. A write the soft deadline cuts after it started is recorded as still running (`NOTE_STILL_RUNNING`) and, when it lands, posts its receipt late through `DriveOperations.post_late_note_receipt` → `UserNotificationService.notify_drive_note_receipt`: exactly one receipt either way |
| Δ11 | The receipt and the erase request's path line are sent literally (`send_literal`) in chat and on the call path; the erase command alone is a message |
| — | `add_to_note` is re-run safe: a provider-rotation re-run with the same session and turn id, whose note already ends with the appended words, writes nothing (undo's I3 guard, mirrored) |
| Δ16 | One fixed tool order: the twelve drive tools, `get_note`, the ten writers, the terminal tool; an owner turn and any other run are two cacheable prefixes |

---

## 10. Migration

One feature branch, one PR, merged when fully green. Every step is a commit that passes `make check`. No switch.

| Step | What | Existing tests |
|---|---|---|
| 0 | **[OWNER]** G3 "before" on current code, with drive-turn wall times (§12), after a 1-repeat cost probe | — |
| 1-2 (one task) | `DriveOperations` takes FileManagement's drive code **behind FileManagement's unchanged surface**: FileManagement keeps `MUTATION_WAIT_S` and passes it per call; read-only properties forward `_shielded`, `_notices`, `_armed`, `_targets`, `_localization`, `_conversion_service`; the BaseAgent hooks become logger calls; one `DriveOperations` per user in the factory slot (§5.5). Not a pure move, said so | green unedited, except three FileManagement tests that patch module names of the moved code (two `_fetch` binary tests, one save-naming test); they go to the reviewer with a proposed re-target and unchanged assertions |
|  | Typed outcomes and `DriveOp` in `DriveOperations` (late-failure notices re-keyed; FileManagement maps outcomes back to today's `AgentResponse`); `drive_pass_through.py`, the answer composer (`drive_answer.py`), `drive_tool_texts.py`; `build_tool_turn` list + adapter wire tests; engine `allow_delegation`; `CURRENT_RUN_DEADLINE` in `BaseAgent._execute_with_timeout`; `DriveSearchService.search_state` | green; new tests for each |
| 3 | `DriveAgent`, `drive_tools.py`, `DRIVE_SPECIALIST` descriptor, tier, strategy, config, factory wiring (lazy builder, `preload_cache`); prompt files under `prompts_snapshot/` (§10.1); the class-level `DriveOperations` write stub for G3 and its test (§12); the owner-text scan script (`scripts/validation/drive_intent_text_scan.py`, tracked and read-only; its outputs go only to `scripts/memory/`; it gains `--check-rewrites` and the drafts store) | green (the default-tier and prose-schema tests cover the new agent) |
| 4 | **The cut**, one commit: remove the 8 intents from `FILE_MANAGEMENT` and `Intent`; delete `src/skills/smart/drive-search/`; the protocol edit (mirror + `firestore_utils/uploads/PROTOCOL_AGENT_SELECTION.json`, pinned equal); §4.4 and §7.6 wording; `smart_dry_run.py`'s intent reads (`:101-108, 304-383`) and its metrics (`drive_status` instead of `failed`, `:331,345,383`) rewritten together with its 9 importing test files; FileManagement drive tests re-targeted to `DriveOperations` / the tools | reviewer verdicts applied in this commit (§11.2) |
| 5 | `drive_routing_probe.py` and `drive_query_vocabulary.py` read the specialist's tool calls; docs: root `CLAUDE.md` roster (new row, FileManagement row), `src/agents/CLAUDE.md`, `docs/05_building_blocks/user_drive/README.md`, `NEW_AGENT_PLAYBOOK.md` stale token name, decision record `decisions/drive_specialist.md`, which records the residual "for a few turns after the deploy, a stale skill copy in history can cause one rejected call to a removed intent, which Smart self-corrects from today's unknown-intent error" | green |
| 6 | **[OWNER], before the deploy:** run the scan over the owner's custom skills, stored reminder instructions, standing directives, workspace files (`projects/`, `references/`, `scratch/` and the text files in `files/`), `domain=preference` facts and user/account prompt overrides; only the owner is scanned, and a `skill_file` hit is redrafted in chat; **back up** every hit to `scripts/memory/` (skills with their version numbers); rewrite each hit so it names **no intent** ("find it on the drive", "save it to the drive"), as a new skill version where the store is versioned. **Patterns:** the 8 intent names; the 13 field names (`drive_query`, `drive_folder`, `drive_kind`, `drive_date_from`, `drive_date_to`, `drive_deleted`, `original`, `append_text`, `source_ref`, `new_name`, `folder`, `text`, `file_ref` next to a drive intent); `drive-search` (a skill that says "load drive-search first" fails after the cut); and `work_with_drive_files` (used again for §13). Intent-free text works with the old code and the new, so it can land first and survives a revert | — |
| 7 | **[OWNER]** prompt upload (§10.1), deploy, `$admin_cache_reset`, one live smoke message (a find), a check of the `[DriveAgent] status= model=` line (the model that ran, failover included) and of `prompt_content` for `agent_type=drive` (cache reads; its model column is the request's, so it cannot show a failover), G3 "after" | — |
| 8 | The notes change (§9), separate | per the plan |

Unchanged callers: Lelik and the Alek gateway reach the drive through Smart (the chain through Router and Smart to
`DriveAgent` stays within `MAX_DELEGATION_DEPTH = 8`, `agent_coordinator.py:41`); the deep-research drive copy calls
`UserDriveService.save` directly; NotesAgent cannot delegate a drive intent; the coordinator's `file_ref` prefetch for
DocPlanner, PdfGenerator and HtmlPageGenerator still resolves `drive:` refs copied from cards through
`FileConversionService` (`agent_coordinator.py:544-546`); the Cabinet names no intent; `capabilities.py` lists no intent.

### 10.1 Owner prompt-upload steps
0. **O0, the model check (before step 1):** read the owner's `provider_preference` and `agent_providers`. If the preference is `openai`, set `agent_providers["drive"] = "claude"`.
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
7. **If the deploy fails after step 5:** restore `PROTOCOL_AGENT_SELECTION` from the step 1 backup, then redeploy the
   **current** image as a new revision (fresh instances; the command is in `docs/07_deployment/README.md`). Do not
   retry the deploy on the restored protocol: the new code must not meet the old protocol.

---

## 11. Tests

### 11.1 New

| Area | Tests |
|---|---|
| Step 1 | every existing FileManagement test runs unedited, except the three of §10 step 1; both agents of one user hold the same `DriveOperations` |
| Outcomes | each exception class → its outcome class with today's text byte for byte; a delete whose provider call fails after the wait still posts the late-failure notice, keyed on `DriveOp` |
| Answer | each status and the precedence table; "What to do" is today's text byte for byte; one op line per mutating call, in step order ("Already on the drive" and "It already existed" included); a connection-state line only when no operation ran; a search-state line only on an answer that ran a search (a save during a rebuild carries no `REBUILDING_LINE`); `not_possible` for a no-op run, for a text tool without its item, and for deleted search + `not_done`; an unknown `text_item` is refused and nothing is written; `question` cannot override a failure or a done change |
| Reading lines | each trigger of §7.3 adds exactly its lines, and none without its trigger |
| Pass-through | blocks byte-identical to today's renders, each with its own reminder; **search + open → both blocks reach Smart**; two searches → both; a listing before moves is labelled; the stand-in of an open holds no body; several vision files reach Smart's transcript in order |
| `drive_context` | equals the concatenation of today's per-op entries, no dedup; one level after Smart's fold |
| Tools | each tool → one `DriveOperations` call with mapped arguments; an empty or omitted field never mutates; `folder` as a path and as a `drive:` ref give the same call; one `move_file` with both fields makes one `svc.move`; each source kind of `copy_file_to_drive` and `replace_file_content` (chat, delivered key, `workspace:`, `skill:`, `drive:` for replace only); two `save_text_file` with two items in one commission are written in order with their own texts; no parameter named `name`; parameter descriptions name no Smart-era field and no "not read" clause |
| Agent | `AgentResponse.success` on every status, and no "Correct your input" in Smart's tool result; `delivery_items` forwarded; `NO_RETRY_POLICY`; missing prompt → failure, no LLM call; `include_directives=False`; `_on_agent_start` logs no commission text; `allow_delegation=False` → unknown-tool answer; one per-user lock across two concurrent executions; batch order |
| Time | a read still running at the soft deadline ends the run with the earlier mutation's op line intact; an LLM call is clamped; `still_running` cancels the engine and keeps the shielded mutation; `CURRENT_RUN_DEADLINE` from a 180 s reminder run lowers the soft deadline, is cleared under an orchestrator without a timeout, and is absent from an enqueued Cloud Task payload; the mutation wait floor |
| Engine, domain | `allow_delegation`; `build_tool_turn` with a dict, a list, none; wire tests: two files after one `tool_response` (Claude nested, OpenAI one content item, Gemini parts, Grok images only) |
| Manifest, Smart | the descriptor (SYNC, `prefetch_file_ref=False`, empty allowlist); description anchors (find, the FILE rule, delivered document key, `file_contents`) and **no notes sentence**; Smart's and Quick's declarations hold no old drive intent; `drive-search` absent from the catalogue; the protocol exception and block |
| State block | `user_message` labelled by origin; `file_data` parts filtered |
| Chain | chat context at Smart's engine (the Router routes every chat turn to Smart; its own tests cover that hop) → coordinator → coordinator → `DriveAgent` → handler sees `interactive_delivery`, `session_id`, `turn_id`; vision reaches Smart; a delete receipt is posted; a `drive:` ref from a card reaches `create_pdf` through the prefetch |
| Architecture | `drive_operations.py` / `drive_tools.py` import no `UserDrivePort` and no adapter |
| Harness | a drive write inside the specialist is stubbed in the G3 dry run (lands before any dry run) |
| Notes (notes change) | the plan's tests, ported; each of the eleven tools → its one core call; `replace_note_text` refused without an earlier `get_note` and after a version change; `get_note` + `replace_note_text` in one batch run in order; `set_note_type` / `add_life_areas` / `add_to_note` on a file with no note create it; `add_to_note` and `add_life_areas` keep what was there; a conflict re-merge re-embeds; a re-run with the same change is a no-op receipt; a forged `params` gate refused; erase code never in `result_str`; a `partial` commission still delivers a note receipt |

### 11.2 Existing tests that go to a reviewer (reviewer rule; the implementer never edits them)

57 test files name the old intents, `FileManagementAgent` or the `drive-search` skill (grep at the branch base; 68 when
field-name pins are counted).

| Group | Files (approx.) | Why they change |
|---|---|---|
| `tests/unit/agents/test_file_management*.py`, `test_save_*` (drive parts) | 18 | the 8 intents leave FileManagement; re-targeted to `DriveOperations` and the tools with assertions unchanged where the text is unchanged |
| `test_agent_manifest_drive*.py`, `test_agent_manifest.py`, `test_save_text_to_drive.py`, `test_agent_manifest_drive_search.py` | 9 | the 8 intents and their schemas leave the manifest |
| `tests/unit/skills/test_drive_search_skill*.py` (5), `tests/unit/firestore_utils/test_protocol_drive_*.py` (4), `test_drive_inaccessible_fix_wording.py`, `test_filesystem_skill_loader*.py` | 12 | the skill moves to a token and reading lines; the protocol block changes |
| string pins in domain and services tests (`list_files_in_drive`, `drive_folder`, `drive_query`, …) | 8–19 | §7.6 wording |
| workspace tests (`test_workspace_paths.py`, `test_workspace_service_copy.py`, `test_workspace_service_view.py`) | 3 | §4.4 wording |
| `tests/unit/scripts/test_drive_eval_g3_*.py`, `test_drive_eval_smart_dry_run.py`, `test_drive_eval_s26_additions.py`, `test_drive_query_vocabulary.py` | 10 | the harness reads the specialist's calls and `drive_status`; decided in the step 4 commit |

Two more: `test_strategies_has_no_orphans` needs `"drive"` in its factory list, and `test_only_the_drive_agents_name_a_fallback_tier` needs `"drive"` in its expected set.

The reviewer decides each test against this RFC: an edit where the spec changed it on purpose, otherwise a code fix.

---

## 12. Evals

| Eval | Before (step 0) | After (step 7) | Bar |
|---|---|---|---|
| G3 `scripts/validation/drive_eval/smart_dry_run` (gold 35 + 14 cases), same gold and `--repeats` | current code; records `bars_g3.json` (it does not exist yet) | new code, Sonnet specialist | no regression on the hard checks (injection bar, `must_open` / `must_not_open`), plus: Smart made no call to a removed intent |
| **Drive-turn wall time** (G3 per case, and production logs) | recorded per case | same cases | **p50 after ≤ p50 before + 25 %**; if the Sonnet specialist misses it, effort `low` is the first lever |
| **Pre-build check (owner-approved)** | stage 1 of §15 (26 cases, today vs specialist, Smart on sol) | the same 26 cases with the Sonnet specialist | correctness at parity; the latency bar; Sonnet's specialist time and cost per commission |
| Logs (free) | research 01 is the production baseline | Smart input tokens per call; Smart calls per drive turn; re-searches; specialist turns, p50/p95 latency and cost per commission (the `[DriveAgent]` line) | reported, not gating |

**Harness safety first.** G3 and `ab_agent_models.py` stub writes per intent at `DelegationEngine.dispatch`, and the
specialist's tools do not go through it. G3 gets a class-level write stub on `DriveOperations`, in `scripts/` (not an instance wrapper in `src/services/`): a class patch covers every instance, the factory's included, and the install refuses an unclassified public method (`READ_METHODS` / `MUTATION_METHODS`, §5.5). Reads go to the fixture drive and search (the `DryRunWorkspace` precedent, `smart_dry_run.py:596`), writes are recorded and never executed. `ab_agent_models.py` gets no drive stub: it stubs every intent outside its read-only set at Smart's dispatch, and the specialist is never reached there (pinned by a test). The eval metrics read the `DriveOperations` log, the one choke point, which records the outcome class and also catches Smart's own `open_file` on a `drive:` ref. In G3 a call to a removed intent goes to Smart's own engine and counts as a hard failure. The test
that a write is stubbed lands before any dry run.

`drive_routing_probe.py` takes a required `--max-usd`; the cap is checked before each call, so the overshoot is about one call per parallel specialist.

**Cost.** One G3 run with `--repeats 3` is about 105 Smart turns plus specialist calls (research 07 guesses $1–4).
A 1-repeat probe gives the real number first; anything above $3 needs the owner's yes.

---

## 13. Rollback

1. `git revert -m 1 <merge>` on a branch (commits reach `main` through a PR).
2. **[OWNER]** Upload the mirror file of `PROTOCOL_AGENT_SELECTION` that the revert restored (`snapshot_upload.py` dry run,
   then `--apply`), just before the deploy and from that tree; the §10.1 step 1 backup is the cross-check, not the source.
3. `make deploy` from the reverted tree.
4. `$admin_cache_reset`, so no instance keeps a template built from the new protocol. The reset comes after the deploy,
   not in the restore block. Between the restore and the reset, up to two self-corrected calls are possible.
5. `snapshot_pull.py`, commit the mirror; open the PR that carries the revert.
6. **[OWNER]** Texts written **after** the deploy: Smart may have written `work_with_drive_files` into reminder
   instructions, its workspace (`projects/`, `references/`), skill drafts it saved, or text the consolidator turned into
   directives. Run the §10 step 6 scan for `work_with_drive_files` over the same stores, back up the hits, and rewrite each
   intent-free, before or right after the revert deploy. Residual: one self-corrected rejected call per stale text not
   yet rewritten.

Not covered by git otherwise: the owner's texts rewritten in step 6 name no intent, so they work with the reverted
code; their originals are in `scripts/memory/` if wanted. The new drive tokens are unused after a
revert and can stay. The drive, the index and the receipts are written by the same code in both versions. If the notes
change was built on top, it is reverted first. A deploy that fails during the rollback is answered as in §10.1 step 7: a new revision of the current image.

---

## 14. Open questions and open issues

- **Known question — variant re-search by the specialist (owner 2026-10-22: not now).** Research 09 §6.10
  proposed one script or translation re-search when no exact tier holds the user's word. Not built: transliteration
  is exact in the index, meaning search covers cross-language matches, the keys carry translations, and Smart can
  re-commission with other words. Revisit only if the live logs show finds lost to a missing variant.

**Owner questions: none open.**
- The owner's custom skills that name a removed intent: rewritten intent-free in step 6
  (§10), as ruled. Retiring one instead stays his call, per skill.
- The paid evals: **done** (§15, ≈ $4.4). The stage-1 rerun with the Sonnet specialist is the owner-approved pre-build
  check (§12).

**Open issue for the build (corrected, rev 4.4).** With Grok as Smart, "find then open the second" made 9 single-step
round trips (222 s, §15.4). They were not one commission per file: they were 8 re-opens of one PDF that `GrokAdapter`
drops (`grok_adapter.py:640`), so Smart never saw it. That is an existing adapter gap, not a property of this design;
it goes to Appendix A as backlog. The build still checks that the answer shape lets Smart batch a follow-up (refs and
labels of every card in the blocks, the step headers) and that the §4.1 description invites one commission per step.

---

## 15. Live A/B results (spike `spike/drive-specialist`, 2026-10-22)

A spike built the specialist path behind the G3 harness and ran today's design against it on the G3 fixture drive
(synthetic data, writes stubbed). Reports: `scripts/memory/drive_g3/` on that branch (private). **Stages 1–4 ran with a
luna (`gpt-6-luna`, medium) specialist; the final probe (§15.5) chose the model.** A full rerun of stage 1 with the
Sonnet specialist is the pre-build check, approved by the owner (§12).

### 15.1 Headline

| Finding | Value |
|---|---|
| Correctness | at parity with today, after the gold artefacts were fixed (§15.2) |
| Smart calls | **−36 %** (stage 1: 66 against 103) |
| Smart input tokens | **−42 %** (median) |
| Drive-turn wall time | median equal with a luna specialist; a simple find or save +5–10 s; expected better with Sonnet (§15.5) |
| Smart's own keys | Smart wrote search keys into commissions on its own; the execute-as-given rule serves them |
| Total spend | ≈ $4.4 |

### 15.2 Stages 1 and 2: today vs specialist (Smart on `gpt-6.1-sol`, luna specialist)

| Stage | Design | Cases | Correct | Wall s (sum) | Smart calls | Specialist calls | $ |
|---|---|---|---|---|---|---|---|
| 1 (`…_144534`) | today | 26 | 24 | 788.7 | 103 | 0 | 0.51 |
| 1 | specialist | 26 | 22 | 887.6 | 66 | 78 | 0.62 |
| 2 (`…_160550`, 7 cases × 3) | today | 21 | 19 | 437.4 | 79 | 0 | 0.36 |
| 2 | specialist | 21 | 20 | 465.3 | 43 | 51 | 0.28 |

Stage 1's specialist failures and what fixed them: two opens on a find (`find-nda`, `translit-dogovor`) → the find rule
in Smart's description (§4.1), clean in stage 2; one missing date (`car-insurance`) → a gold artefact, clean in stage 2;
the vague delete failed in both designs in both stages (Smart's judgement, unchanged). The recording-link misses of the
replay led to the recordings line (§5.2).

### 15.3 Stage 3: specialist replay (stage-1 commissions, specialist only)

| Model | Commissions | Gold in content | Wall p50 | LLM calls | $ |
|---|---|---|---|---|---|
| luna `low` (`…_150644`) | 24 | 19 | 10.0 s | 72 | 0.025 |
| luna `medium` (`…_152910`) | 24 | 19 | 13.0 s | 77 | 0.034 |
| `gemini-3.8-flash` (`…_154832`) | 8 (stopped at the cap) | 6 | 30.2 s | 35 | 0.444 |

The replay also showed up to 9 searches in one commission, several identical: the code skip of §5.4.

### 15.4 Stage 4: other Smart models (luna specialist)

| Smart | Cases | Correct | Wall s (sum) | Smart calls | $ | Note |
|---|---|---|---|---|---|---|
| `claude-sonnet-5-5` (`…_161300`) | 4 (stopped at the cap) | 2 | 292.0 | 52 | 0.39 | the known filler-tool stall: 40 Smart calls on one case. **Excluded** from the Smart-side check; this RFC does not change Smart's model |
| `grok-4.7` (`…_162126`) | 5 | 5 | 378.1 | 21 | 0.45 | "find then open the second": 9 round trips, 222 s (the open issue in §14) |

### 15.5 Final probe: the specialist's model (Smart on `gpt-6.1-sol`, 5 cases)

| Specialist | Report | Correct | Specialist time (sum) | Case wall s (sum) | $ per 5 cases |
|---|---|---|---|---|---|
| `claude-sonnet-5-5` | `…_170000` | 5 / 5 | **38 s** | 127.4 | 0.33 |
| `gpt-6.1-sol` | `…_165022` | 5 / 5 | 61 s | 149.1 | 0.21 |
| `gpt-6-luna` | `…_164600` | 5 / 5 | 91 s | 203.5 | 0.21 |
| `grok-4.7` | `…_165558` | 4 / 5 (cap hit) | 151 s+ | 227.0+ | 0.36+ |

Owner decision: Sonnet (§5.1). It read nothing from cache in the probe, so its cost per commission has room to fall.

---

## Appendix A. Found on the way: separate backlog, not decided here

| Item | Today | Source |
|---|---|---|
| `GrokAdapter` drops a PDF file part, so a Grok Smart re-opens the same PDF (8 times in the §15.4 run) | `grok_adapter.py:640` | arbiter R5 |
| Opened drive text has no character cap: a 4.9 MB text file reaches Smart whole | `convert_file_to_text` does not truncate | 04 D1 |
| Temp files written for drive vision are never deleted (Cloud Run `/tmp` is memory) | `_open_drive_item` | 04 D2 |
| `[System: User attempted to attach …]` status text sits inside the `<drive_document>` wrapper | `_resolve_drive_content`, `convert_file_to_text` | 05 §0.4 |
| Generic failures reach the reader as a class name (`Drive operation failed: DriveStorageFull.`) | `_drive_call` | 05 §0.3 |
| An append can run twice on a whole-turn re-run (provider rotation, Quick fallback) | FileManagement, unchanged | 06 §4.2 |
| The Quick fallback drops `interactive_delivery`, so an owner turn on fallback is not an owner turn for notes | `agent_fallback_service.py:121` | 06 §2.1 |
| A sizeable share of `open_file` calls reopen a file attached this turn | Smart | 01 §2 |
| No "card by ref" read; done today by a name search. No note listing | — | 01 §5.4; R2 M9 |
| Listings are not inside the nonce wrapper | `_list_drive` | 04 §1.3 |
| File names in INFO logs (`file_conversion`, two `UserDriveService` warnings, the per-op line, a local tool's `name` in a span attribute) | — | 06 §6.1 |
| An email attachment cannot be saved to the drive as the original, and nothing tells Smart | no storage ref | 02 #30; R2 I6 |
| "put a FILE anywhere" in the save rule predates the workspace's `files/` | today's wording | R2 I9 |
| A stale memory fact says drive search is metadata-only | memory | 01 §4 |
| `TasksAgent` proceeds with an empty prompt on a build failure, pinned by `test_prompt_build_failure_still_succeeds` | `tasks_agent.py` | 03 A.1 |
