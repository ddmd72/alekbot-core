# RFC: Agent Skills — named procedures for Smart, loaded on demand, saved by the user

**Status:** Revision 7 — **G1 passed** (2026-10-04: full reviews of revisions 5 and 6, targeted check of 7; findings resolved, §14). Delivery A (read path) and delivery B (authoring + system skills) both shipped; §3 already reflects the two planning rulings recorded in `docs/superpowers/plans/2026-10-04-agent-skills-delivery-b.md` (no `FileSystemSkillRepository` — a loader instead; `domain-competency-research` as a second system skill). See that plan's Deviations section for the full rulings, including one on §8 step 6's save-transaction boundary. **Delivery C (text files in a skill, §15) — revision 2, G1 passed 2026-10-06; next: owner review, then the plan (G2).**
**Date:** 2026-10-04 (first draft 2026-09-30)
**Owner decisions:**
- Skills are named procedures in Anthropic's `SKILL.md` format, run by our own layer (Smart is multi-provider).
- **v1 is Smart only.** Tutor and Lelik later, on demand.
- Skills are **their own tools** (`use_skill`, `draft_skill`), not intents of `delegate_to_specialist`: a procedure is not a specialist, and a separate tool gives the model a distinct semantic cue.
- **Two deliveries, one RFC** (§10): (A) reading the user's own skills, tested on the owner's real `flight-status` skill; (B) authoring in chat plus system skills. A answers the question B depends on — does Smart load and follow skills at all.
- **Personal procedures are custom skills, never system skills.** The repo is public and system skills are visible to every user.
- **No migration** of existing protocols (`EmailReviewService.build_alert`, `PROTOCOL_*`).
- **A loaded skill lives in history until tiering compresses it**, then leaves a stub (§7). No lifecycle machinery.
- **Authoring follows Anthropic's skill-creator:** Smart drafts in chat and *offers* to save procedures it was taught; a draft becomes active only by the user pasting `$skill save <code>` (§8).
- Fable reviews the RFC, the plan and each branch as a **critique of the idea and its implementation** (§11).

## 1. Problem

The owner explains a procedure to Smart, and Smart has nowhere to keep it. **The motivating case:** checking a flight's status means going to specific web pages and parsing them with a few simple rules. Smart can follow this once explained; next time it has to be explained again.

The existing persistence kinds each lose such a procedure:

| Container | Why a procedure does not survive there |
|-----------|----------------------------------------|
| Facts (`save_to_memory`, consolidation) | Consolidation splits multi-concept text into atomic facts and rewrites them (TD-4). Retrieval is by similarity, so a procedure appears only when the wording matches, and then in fragments. |
| Standing directives | Always injected, hard cap 15, one terse line each. The SCOPE test (`decisions/directive_applicability_gate.md`) demotes situational rules, and a procedure is situational. |
| Self-reminders | Fire on a schedule. They carry an obligation, not knowledge. |

A **skill** is the missing kind: a named, situational procedure. Its *trigger* is always visible; its *body* is loaded only when the trigger matches.

| Kind | Home | Property |
|------|------|----------|
| Facts about the user | `knowledge_base` | retrieved by relevance |
| Rules always in force | `standing_directives` | always injected, binding |
| Deferred obligation | `active_reminders` | fires later |
| **Procedure for a kind of task** | **`available_skills` + `use_skill`** | **trigger always visible, body on demand** |

## 2. Prior art

**Anthropic Skills:** `SKILL.md` with frontmatter (`name`, `description`) and a markdown body; only `name + description` sit in context; the body is read through a dedicated `Skill` tool when the model decides it applies. After compaction Claude Code re-injects invoked skills' bodies (5k tokens each, 25k total); losing them was a reported bug.

**Anthropic's `skill-creator`:** capture the procedure from the current conversation first, ask about gaps, draft with a deliberately "pushy" description (models under-trigger skills), try it on realistic prompts with the user judging *outputs*, revise; **the user installs the result** — the model never installs a skill into itself. Its own Claude.ai variant notes that testing in the same context is weaker than a fresh run.

We copy the format, the dedicated tool, the disclosure model and the authoring model; the runtime is ours.

## 3. Origins and the visible set

| Origin | Where | Written by | Delivery |
|--------|-------|-----------|----------|
| **Custom** (per user) | Firestore (§8) | A: a seeding script run by the developer; B: the user, by `$skill save` | A |
| **System** (all users) | git, `src/skills/smart/<name>/SKILL.md` | developers, via PR | B |

- One port, `SkillRepository`, one adapter: `FirestoreSkillRepository` (custom; delivery A). System skills are read-only, so a second port implementation was dropped in planning: a `FileSystemSkillRepository` would have to implement the write port's `save_version`/drafts/delete over immutable git content, a Liskov violation. Instead a plain loader, `load_system_skills(root) -> List[Skill]` (`src/adapters/filesystem_skill_loader.py`), runs once in composition at startup — a malformed skill fails startup — and `SkillService` receives the resulting list by constructor injection (delivery B).
- **System skills must be generic.** The repo is public and every user sees them. Two ship in git: `skill-creator` and `domain-competency-research` — the latter was added in delivery B (beyond this revision's original plan of `skill-creator` alone) because the procedure is generic, already proven live, and closes roadmap TD-10's prerequisite of having it in git before `DomainResearcherAgent` is retired.
- **Visible set** for a user = system skills ∪ that user's custom skills.
- **Name collisions:** saving a custom skill under a system name is rejected. If a later release ships a system skill whose name a user already has, the custom one shadows it for that user and a warning is logged.

## 4. Format

```markdown
---
name: flight-status
description: "Use when the owner asks about a flight's status, delay, gate or arrival, or gives a flight number."
---
1. …
```

- `name`: kebab-case `[a-z0-9-]`, ≤ 64 chars.
- `description`: the trigger, ≤ 250 chars, one line, "Use when …". Re-sent on every request.
- body: markdown; the whole `SKILL.md` is capped just under the Firestore document limit (1 MiB, `MAX_SKILL_MD_BYTES` = 1 MiB − 32 KB). The cap is a storage bound, not a style limit (owner, 2026-10-05: an earlier 20 KB cap had no basis and rejected real drafts). Concision is guidance for the author.
- **No files and no scripts in deliveries A and B.** Reference material went in the body. Text files are delivery C (§15); binaries come later with their own bucket. Executing scripts needs a sandbox and its own RFC.

**Parsing.** Frontmatter is read with `yaml.safe_load` (PyYAML is already a dependency) in `src/utils/skill_md.py`, shared by the seeding script and delivery B's filesystem adapter; `domain/` holds a pydantic `Skill` model that validates the result.

## 5. The skill tools

Smart gets skill tools next to `delegate_to_specialist`. They are zero-LLM and handled **locally**, not through `AgentCoordinator`.

| Tool | Delivery | Arguments | Result to the model |
|------|----------|-----------|---------------------|
| `use_skill` | A | `name` | the skill body, verbatim — or "already in your context above" (below) |
| `draft_skill` | B | `name`, `description`, `body` | "preview delivered to the owner" (§8) |

**Engine change.** `DelegationEngine` today treats every non-terminal tool call as a delegation and reads `intent` from its arguments (`delegation_engine.py:468-483`). It gains `local_tools: Mapping[str, LocalToolHandler]`: a call whose name is in the map goes to its handler. Its result string, `delivery_items` and `history_context` flow into `DelegationResult` exactly as a delegation's do. One Logfire span per local call. A stray `delegate_to_specialist(intent="use_skill")` fails cleanly at the coordinator (`agent_coordinator.py:389-397`).

**Handler per execution.** The handler factory is `src/infrastructure/skill_tools.py::make_use_skill_handler`
(moved out of `agents/core/` — REQ-ARCH-24 forbids an agent importing a sibling `agents/` module, and
`use_skill`'s handler has no agent-specific logic). Smart builds the handlers for each execution as
closures over:
- `visible`: names whose body is in the **tiered** history Smart is about to send — computed after `_apply_history_tier` (`smart_response_agent.py:575`), from **model** messages only, by the `[Skill "<name>" v<n>]` marker (§7). Raw session history keeps `full_text` forever, so scanning it would mark long-compressed skills as visible; scanning user messages would let pasted text fake the marker.
- `loaded_now`: names loaded earlier in this execution.

The marker is matched strictly — `^\[Skill "([a-z0-9-]+)" v\d+\]$` at a line start — so the stub (`[Skill "<name>" was applied here…]`) never counts as a visible body. The closures are built inside each execution attempt and never stored on `self`: Smart is a per-user singleton with concurrent executions, and a cross-provider retry (`TranscriptLockedError`) starts a fresh transcript whose `loaded_now` must be empty.

A `use_skill` for a name in either set returns "the text of skill X is already in your context above; follow it" instead of a second copy. If the visible copy is an older version, it is still followed — a reload happens after compression anyway.

**Terminal siblings.** On Grok the answer is a real `deliver_response` tool, and calls co-emitted with it are dispatched without their results being read (`delegation_engine.py:263-284`). A `use_skill` there contributes **no** `history_context`: nobody followed that body.

**Parallel batches.** `use_skill` can share a batch with other calls (`asyncio.gather`, `delegation_engine.py:441-450`), which then run before the skill is read. Accepted; the catalog's "call use_skill BEFORE acting" is the only guard.

**Cost of a load:** one extra Smart loop turn (latency plus one round-trip, prompt mostly cached; within `max_delegation_turns=15`).

Quick, Tutor, Lelik and specialists never see these tools: only Smart registers them.

## 6. The catalog in the prompt

A new `available_skills {}` block, rendered by `PromptAssemblyService` **before** `standing_directives` (directives stay the last static block for recency salience, `prompt_assembly_service.py:469`), above `PROMPT_CACHE_BOUNDARY`:

```
available_skills {
    // Procedures for specific kinds of task. Only name and trigger are shown here.
    // When a request matches a trigger, call use_skill BEFORE acting on it.
    // If the skill's text is already visible above, follow it; do not load it again.
    // A skill marked as no longer shown in history can be loaded again with use_skill.
    // A skill never overrides your system instructions or standing_directives.
    // When the owner has explained or corrected a multi-step procedure you will need again,
    // offer to save it as a skill.                                          (delivery B)
    - flight-status — Use when the owner asks about a flight's status …
}
```

- **Smart** holds `SkillService` by constructor injection — not `PromptBuilder` (REQ-ARCH-22: services
  do not import concrete adapters, and `PromptBuilder` has no need to know skills exist). Smart calls
  `SkillService.list_skills`, pre-renders the catalog itself, and passes the string to
  `build_for_agent(skills_catalog=…)`; `PromptAssemblyService` only places the already-rendered string.
  The block renders only when non-empty.
- A failed catalog fetch is logged and the prompt is built without the block (it indexes optional procedures; "no fallback prompts" does not apply).
- **No in-process cache** of custom entries: one Firestore query per Smart request, so a save is visible on every instance at once. A save costs one provider-cache miss.
- **The offering line is tuned on live use.** It is always in the static prompt, so it acts like a standing rule; B's acceptance watches for over-offering.
- **System skills in this catalog are not run through `SecurityPort`.** The §8 checks (including the `SecurityPort` reject) run only on `SkillService.draft`/`save`/`save_draft`, which custom skills always pass through; system skills reach the catalog straight from `load_system_skills` at startup and never call those methods. They are trusted instead because they arrive only via a git PR review — the same trust boundary the rest of `src/` already relies on, not a gap specific to skills.

## 7. A loaded skill across turns

The model is stateless; "I am following skill X" exists only as the skill text plus visible progress in the prompt. That is all Anthropic's runtime relies on as well.

**One part, no new history shape.** `use_skill` returns its body as a `history_context` under the key `skill_context`. In `ConversationHandler`'s `*_context` loop (`conversation_handler.py:844-855`) this key is handled differently from the others:
- the body is appended to `full_text` as a **raw labelled block** (`[Skill "<name>" v<n>]\n<body>`), not `json.dumps` (which would escape the markdown);
- one stub line per loaded skill is appended to `history_text`: `[Skill "<name>" was applied here; its text is no longer shown]`. The wording is neutral on purpose: Lelik's warm context reads model `p.text` (`lelik_persona_service.py:83`) and must not be told to call a tool he lacks. The reload instruction lives in Smart's catalog (§6).
- The loop runs **after** the async summary has resolved (`conversation_handler.py:797-818`), so the summary cannot overwrite the stub. Appending earlier would be silently lost.

The model message stays a single part (`conversation_handler.py:936`). A second text part would be sent by `OpenAIAdapter`/`GrokAdapter` as a list of `input_text` items in an assistant message (`openai_adapter.py:594-596`), a shape the Responses API rejects; and it would buy nothing, since `_apply_history_tier` decides full-or-summary per message (`base_agent.py:422`).

**Window.** The tier counts model messages and keeps `N+1` of them full (`model_turns_from_end <= N`). Smart's `N` resolves USER → ACCOUNT → `SearchConfig.DEFAULT_HISTORY_RECENT_FULL_TURNS = 2`; the owner's live value is 2, so the last **3 exchanges** carry the body. More is a config change.

**Paths that do not persist.** Only `ConversationHandler` writes history. A skill loaded on the `notify()` path or via `ask_alek` is used for that run only — acceptable, those runs are single-turn. The stub reaches consolidation (model `p.text` is serialized); it is one neutral line.

**Trade-offs accepted.** The body rides in the prompt for up to three turns after use. A reload may return a newer version. If the model does not reload after compression, a long procedure stalls; `skill-creator` itself exceeds three turns, so reload-after-compression is an explicit acceptance check in B.

## 8. Authoring (delivery B)

The boundary is **authorization by the user**: a custom skill becomes active only through a command the user types. The command authorizes **the exact content the user was shown**, the preview comes from code, and the code that binds them is unknowable in advance.

1. A system skill `smart/skill-creator` (our adaptation of Anthropic's) is in the catalog. It fires when the owner asks to keep a procedure, and when Smart offers (§6) and the owner agrees.
2. Following it, Smart captures the procedure from the conversation, asks only about gaps, and calls `draft_skill(name, description, body)`.
3. The handler runs the checks below, renders `SKILL.md`, draws a **random save code** (`secrets.token_hex(2)`, lowercase, redrawn on collision with the user's pending drafts), stores an **immutable** draft keyed by that code, and returns a `DeliveryItem(type="skill_preview")`. The tool result the model receives says only "preview delivered".
4. `skill_preview` is a new delivery type, handled **only** by `ConversationHandler._deliver_item`, in two posts:
   - the stored `SKILL.md`, **verbatim, as a file** via `response_channel.send_file` (Slack `slack/response_channel.py:522`, Telegram `telegram/response_channel.py:561`). Text posts are truncated (Slack 2,500 chars, Telegram ~2,867) and reformatted, so they cannot show the stored content;
   - a separate short message holding only the command, e.g. `$skill save 7f3a`, as monospace (tap-to-copy on Telegram; MarkdownV2 escaping must leave it intact), ready to copy and paste back (Slack offers no partial selection of a message).

   Delivery items are dispatched after the main reply, so the order is reply → file → command, and the command is the last message. Keep it that way. Telegram's `send_file` gains `message_thread_id` so both posts land in the same forum topic.

   No other path delivers `skill_preview`: `AgentWorkerHandler` handles only `file_upload`/`document` (`agent_worker_handler.py:216, 250`), and `notify()` ignores delivery items. A draft made on a background path is never shown, so it can never be saved. `file_upload`/`document` are deliberately not reused for that reason.
5. The user pastes `$skill save 7f3a`. Both adapters route `$…` to `ConversationHandler.handle_command` before any LLM (Slack `http_adapter.py:320`, Telegram `webhook_adapter.py:195`; commands are lowercased). Telegram messages with `forward_origin` are not treated as commands.
6. The draft is read by code and the checks are re-run **before** the save: `SkillService.save_draft` calls `repository.get_draft(code)`, then `_check`, outside any transaction. Only then does one Firestore transaction run: count the user's skills against the cap, write the next version, flip `current`, delete **every** pending draft with that name. A concurrent double-paste of the same code can therefore run this read-then-transact sequence twice and write two identical versions — harmless, since drafts are immutable and the content was authorized once. Then a chat reply and a user/model **pair** in the session history (`[System: skill "<name>" v<n> saved]`), following `notify_call_summary` (`user_notification_service.py:233-285`), so the next turn knows.

A stale code (an older draft of the same name) still saves exactly the content it was shown with — that is the authorization. Content cannot be swapped under a code, because drafts are immutable.

**Recommended trial** (in `skill-creator`): save, then try the skill in a **new thread**, then revise. A same-context trial is weak: the model still sees the conversation the skill came from. Trying a procedure can cause real side effects (tasks, reminders, errands); the skill says so.

**Commands** (the requesting user, own skills only): `$skill save <code>`, `$skill list`, `$skill delete <name>`. Delete removes the index document **and** its `versions/*` documents. Editing is a new draft plus `save`.

**Checks** (at draft and at save): name format, no collision with a system skill, pydantic validation, size caps, `SecurityPort` — a flagged description or body is **rejected**, never stored sanitized (a skill is followed verbatim). These are hygiene; the security boundary is the command.

**Storage** (`FirestoreSkillRepository`):
- `{prefix}skills/{user_id}:{name}` — `user_id`, `name`, `description`, `body`, `current`, `account_id`, `updated_at` (the index doc denormalizes the current body, so a request reads it without a second query); versions in a subcollection `versions/v<n>` (`SKILL.md` text, `saved_at`).
- `{prefix}skill_drafts/{user_id}:{code}` — immutable; `user_id`, `name`, `SKILL.md` text, `created_at`. No TTL policy: a stale draft is inert.
- Queries filter by equality on `user_id` (and `name` for drafts) — never by document-id prefix.
- **One write method:** `save_version(user_id, skill, consume_drafts_named: Optional[str])` — one transaction that runs the cap count, assigns the next version, writes it, flips `current`, and, when `consume_drafts_named` is set, deletes every pending draft of that name. `$skill save` calls it with the draft's name; delivery A's seeding script calls it without. Both run the same checks first.
- Cap: 20 custom skills per user.

**Residual risk, accepted:** the user saves a code-delivered preview carrying text injected earlier in the conversation, without reading it. No autonomous write exists, so nothing self-propagates. `decisions/standing_directives.md` ("autonomous self-notes rejected permanently") is unaffected.

## 9. Why not prompt tokens

A token is one per category (the dedup key in override resolution), rendered in full, lives in the 24 h template cache, and has no versions. Skills are open-ended in number and render as a one-line trigger.

## 10. Deliveries and acceptance

**Delivery A — reading custom skills, on the owner's `flight-status`.**
- Code: `Skill` model and parsing, `SkillRepository` + `FirestoreSkillRepository` (read, plus the transactional write the seeding script uses), `local_tools` in `DelegationEngine`, `use_skill` with the visibility sets, the catalog, §7 persistence.
- Seeding: `scripts/skills/seed_custom_skill.py --user <id> --file <path>` runs the save checks (pydantic, size, `SecurityPort` reject) and calls `save_version(..., consume_drafts_named=None)`. It resolves the collection prefix through `EnvironmentConfig`, never hardcoded. The script is tracked; the skill text lives in gitignored `scripts/memory/` — it is personal.
- Content: `flight-status`, drafted with the owner in chat.
- **Gate to B**, both required:
  1. the infra-free trigger eval: Smart's real system prompt, a catalog with `flight-status` plus a few decoys, ~15 matching, ~15 near-miss, ~10 unrelated prompts, per provider/model Smart runs; pass bar recall ≥ 0.8, false loads ≤ 0.1 (owner may change it);
  2. real use: Logfire shows `use_skill("flight-status")` on flight questions, and the answer follows the skill's pages and rules. **Load-and-follow is logged separately from fetch success** — a bot-blocked or JS-rendered page is not a skill failure.
  If either fails, B is reconsidered before any work on it.

**Delivery B — authoring and system skills.**
- Code: `load_system_skills`, `draft_skill`, `skill_preview`, `$skill` commands, the offering line, `smart/skill-creator`.
- Acceptance: the owner creates a second real skill with Smart in chat, saves it, uses it in a new thread; the `skill-creator` run itself survives compression via reload; offering frequency observed and tuned.

**Unit:** parsing and validation; visible set and collisions; `local_tools` dispatch, results, spans; `visible` computed from tiered model messages only and `loaded_now`; no `history_context` on terminal siblings; catalog renders only when non-empty, before directives; `skill_context` persisted as a raw block with a neutral stub, after the summary; save code random, never in the model's tool result; `skill_preview` delivered only by `ConversationHandler`; save transaction (cap, version flip, draft cleanup); stale code saves its own content; forwarded Telegram command ignored; delete removes versions; every repo skill parses. **Adapter wire tests:** a request with three tools on each provider (Gemini emits one `types.Tool` per function, `gemini_adapter.py:374-391`). Plus `make check`.

## 11. Review gates — Fable

Fable (`claude-fable-5-1`), briefed as a **skeptical architect**: is it worth building, is it the simplest design, what is wrong or missing, do the code claims hold.

| Gate | Artefact | Blocks |
|------|----------|--------|
| G1 | this RFC (ran on rev 5 and rev 6; targeted check on rev 7) | the plan |
| G2 | the plan for delivery A; delivery B's plan after A's gate | implementation |
| G3 | each delivery's branch | that merge |

Findings are resolved in the artefact; a gate re-runs when a finding changed the design.

## 12. Open questions

- **Q1 — Consolidation duplication.** The conversation that produced a skill is also consolidated into facts. Proposal: accept and observe.
- **Q2 — Tutor/Lelik.** Out of v1; revisit when a concrete procedure needs them.

## 13. Alternatives rejected

- **Skills as intents of `delegate_to_specialist`** (revisions 1–5). Needed a `SkillsAgent`, caller identity and a `mode` override, and told the model a procedure is a specialist.
- **A background LLM author with six defence layers** (revision 4). Detection after the fact is unreliable; authorization by a typed command removes the need.
- **A command authorizing a name** (rev 5) and **a code derived from the content hash** (rev 6). The first let the model show one text and save another; the second let whoever wrote the content compute the code in advance.
- **A text preview** (rev 6). Channels truncate and reformat it, so it is neither complete nor verbatim.
- **A separate history part for the skill** (rev 5). Invalid for OpenAI/Grok assistant messages; no gain over one part.
- **GCS version folders** (revisions 2–5). A draft cannot carry files; a Firestore document holds up to 1 MiB and the version is written in one transaction.
- **The owner's procedures as system skills.** The repo is public and system skills are shared by all users.
- **Lifecycle machinery** (TTL, `release_skill`, `active_skills`, pinning). Tiering plus a stub does the same with no state.
- **Addressable history** (`expand_history(ids)`). Deferred until a logged case of Smart redoing expensive, non-reproducible work within the window.
- **Migrating `build_alert` / `PROTOCOL_*`; a `procedure` fact domain; situational directives; provider-native Skills on Claude only; an LLM on reads.**

## 14. Resolution of G1 findings

**Revision 5 review** — resolved in revision 6: separate tools (removed SkillsAgent, caller identity, `mode`), one history part, Firestore storage, Smart only, catalog before directives, `yaml.safe_load`, no in-process catalog cache, user/model note pair, Telegram forwards, the real case in §1.

**Revision 6 review:**

| Finding | Resolution |
|---------|------------|
| B1′ text preview truncated and reformatted | `skill_preview`: verbatim file + separate command message, `ConversationHandler` only (§8 step 4) |
| M1′ code computable from content | Random code (§8 step 3) |
| M2′ draft spec inconsistent | Immutable drafts keyed by code; save deletes all drafts of the name (§8 step 6) |
| M3′ already-visible unspecified | `visible` from tiered model messages + `loaded_now`, handler closures per execution (§5) |
| Terminal-sibling phantom bodies | No `history_context` on terminal siblings (§5) |
| Parallel batch before reading | Stated and accepted (§5) |
| Gemini multi-tool | Wire tests for three tools per provider (§10) |
| Stub leaks to Lelik | Neutral stub; reload instruction in Smart's catalog (§6, §7) |
| Stub placement vs async summary | Appended in the loop after the summary resolves (§7) |
| System skills public | Personal skills are custom; A reads custom skills and seeds `flight-status` (§3, §10) |
| A's gate weak | Trigger eval required; load-and-follow logged apart from fetch success (§10) |
| Delete and cap | Versions deleted explicitly; count inside the save transaction (§8) |
| SecurityPort sanitize vs reject | Reject (§8) |
| Offering line always on | Tuned on live use (§6, §10) |

## 15. Delivery C — text files in a skill

**Status:** revision 2. The design was agreed with the owner in chat on 2026-10-06. G1 on revision 1 returned "needs revision"; its findings are resolved below (§15.11). Revision 2 changes the design: files are text only and stored in Firestore. Scripts stay out of scope (§4).

### 15.1 Why

A skill is currently one text. Material needed only some of the time (reference tables, lists, long examples) rides in the body. It therefore enters the prompt every time the skill loads and stays there for up to three exchanges (§7).

Anthropic's skills solve this with progressive disclosure. `SKILL.md` stays short and points at files, and the model opens a file only when it needs it.

**Owner decisions (2026-10-06):**
- **Text files only, in Firestore, one document per file** next to the skill, never inside the skill document. Keeping files inside would run into the document limit.
- **Binary files come later**, with their own bucket. Executing scripts needs a sandbox, not GCS, but bundled binary assets (templates, fonts) will need the bucket. The file entry gains a location field then; refs, manifests and inheritance stay as they are.
- Files arrive two ways only:
  - the model writes a text file while drafting, typically by moving rarely used parts of the body out;
  - the model re-saves a text file the owner sent to the chat.

  There is no separate upload path.
- `skill-creator` is updated to split skills this way.
- No custom skill needs files yet. Acceptance therefore uses a system skill and a chat upload (§15.9).

### 15.2 Storage

- **One document per file, keyed by content:** `{prefix}skills/{user_id}:{name}/files/{sha256}` holds `content`, `size` and `created_at`.
  - The sha256 is of the UTF-8 bytes.
  - A file belongs to one skill. Nothing is shared across skills, so nothing needs reference counting.
- **A manifest per version.** `files: [{path, sha256, size}]` is a list, not a map, because a map keyed by dotted paths would read as nested field paths. The manifest is stored on:
  - the version document;
  - the index document (denormalized, like `body`);
  - the draft document.
- **Old versions' file documents are kept** until the skill is deleted. They are text and cheap. Keeping them means a version never points at a missing file, and it removes any cleanup on save.
- **Limits.** These guard against accidents and change with real use.
  - Per file: 256 KB, roughly 64–80k tokens when opened.
  - Per skill: 20 files.
  - Per draft: at most 5 files the model writes (§15.6).
  - `MAX_FILE_BYTES = 5 MB` (`utils/file_conversion.py:23`) never comes into play.
- **Document budget.** A manifest entry is at most ~300 bytes: a 200-char path, a 64-char hash and a size. Twenty entries come to ~6 KB, inside the 32 KB headroom `MAX_SKILL_MD_BYTES` already leaves (`domain/skill.py:19`). A validator on the domain model enforces the entry and path bounds, so the budget holds by construction, not by hope.
- **Paths.** A path is relative, made of `[A-Za-z0-9._-]` segments joined by `/`, at most 200 chars.
  - No segment may be `.` or `..`.
  - `SKILL.md` is reserved.
  - An extension from the text allowlist is required: `.md .txt .csv .tsv .json .yaml .yml`. The read pipeline picks the conversion from the extension (`file_management_agent.py:106`), so a missing or binary extension would mis-read.
  - Convention, not rule: reference text goes under `references/`.

### 15.3 Versions inherit files

A draft lists **changes** against the skill's current version. Everything not listed carries over. Each saved version still stores its **full** manifest.

Why: editing one line of the body must not make the model regenerate a 50 KB reference. Regenerating costs tokens, and it silently corrupts data, because the model paraphrases instead of copying.

Inheritance is resolved **at draft time**, against the version that is current then, and the draft stores the full resulting manifest. A stale code saves exactly the manifest it was shown with, which matches §8 ("a stale code saves its own content"). Inherited entries point at file documents that already exist, because they are kept until the skill is deleted. If the skill was deleted in between, the save finds a missing hash, aborts, and asks for a new draft.

### 15.4 Reading: `skill:` refs through the existing file pipeline

There is no new read tool. A skill file is addressed as **`skill:<name>/<path>`** and read with **`open_file`**. This is the existing zero-LLM `FileManagementAgent` path.

- **Ref resolution.** `FileConversionService._download_by_ref` already dispatches by ref shape (`file_conversion_service.py:67`). It gains a third shape, `skill:`.
  - Resolution follows the requesting user's **visible set** (§3): the user's custom skill at its current version, or else a system skill.
  - The user comes from the request context (`message.context["user_id"]`), never from the ref. Another user's skill cannot be addressed.
- **Reading as text.** A `skill:` ref skips mime guessing and is decoded as UTF-8 directly: its content is text by construction. Mime guessing would send `.json`/`.yaml` (and `.md` on some Python versions) to markitdown, because `_is_plain_text` checks `text/*` only (`utils/file_conversion.py:185-187`). Uploads keep their current behaviour.
- **`SkillFileResolver`** lives in `services/` and is read-only. It is built over:
  - `SkillRepository`, which gains `get_current(user_id, name)` and `get_file(user_id, name, sha256)`. Listing all skills on every `open_file` would read twenty bodies.
  - the system skills, whose file contents `load_system_skills` puts in memory at startup. `services/` never reads disk.
- **Shadowing in one place.** The rule (custom over system) is moved into one domain function used by both `SkillService.list_skills` and the resolver, so it is not written twice.
- **Errors.** A missing skill or path raises `FileNotFoundError` with skill-specific text. It must not say "re-upload" (`file_management_agent.py:121-125`) and must not show as a conversion error.
- **Dependency direction.** The chain is `SkillService → FileConversionService → SkillFileResolver`, by constructor injection (REQ-ARCH-22). `SkillService` moves below `FileConversionService` in `service_container.py`.
  - `FileConversionService`, and with it `open_file`, exists only when `GCS_MEDIA_BUCKET` is set (`service_container.py:305`). That is true of every file today, and skill files simply share the limitation. In practice it means local dev without the bucket.
- **`use_skill` lists the files.** A `Files` block follows the body, one line per file: `- skill:<name>/<path> — <size>`.
  - The model then knows what it can open, even when the body forgets to mention a file.
  - The list is part of the loaded text and is tiered with it (§7).
- **Opened files do not persist.** A file opened with `open_file` is an ordinary tool result. A body is an instruction followed over several turns; a reference is a lookup, and reopening it is cheap.
- **Current at read time.** A ref resolves against the version current when it is read. If a skill is saved again mid-conversation, a later `open_file` sees the new file. This is the same as a body reload (§7).
- **`delete_file` refuses `skill:` refs.** The check sits in `FileManagementAgent._delete`, which calls `FileStoragePort.delete` directly (`file_management_agent.py:254`) and never reaches the dispatcher. Skill files leave only through a new version or `$skill delete`.
- **No collisions with uploads.** An upload's name must never start with `skill:`. `sanitize_filename` (`gcs_file_storage_adapter.py:21`) maps `:` to `_`, and a test covers it.
- **The model learns about the refs.** `open_file`'s `file_ref` description in `agent_manifest.py` says that `skill:` refs from a `Files` list are valid.

### 15.5 Authoring: `draft_skill(…, files)`

`draft_skill` gains an optional `files` parameter, a list of changes:

| Entry | Meaning |
|-------|---------|
| `{path, content}` | a text file the model writes, new or replacing |
| `{path, from_file}` | re-save a text file from **the owner's own uploads** (a bare filename) or from **one of the owner's own skills** (`skill:` ref) |
| `{path, remove: true}` | drop a file from the new version |

`from_file` deliberately rejects bot-delivered documents (`docs/`, `email_review/`, `deep_research/`, `video_generation/`, `file_conversion_service.py:34`). They carry third-party text the owner never sent, such as email bodies. If the owner wants one in a skill, the model writes its content as a `{path, content}` file, and that file is shown in the preview (§15.6).

An upload is re-saved **as stored**. It must have an allowlisted extension and decode as UTF-8. A PDF cannot be re-saved as is, which follows from text-only: the model opens it and writes the part that matters as a text file.

**Handler steps:**
1. Validate the paths, extensions and limits.
2. Read `from_file` bytes through `FileConversionService.resolve_bytes`.
3. Compute the hashes.
4. Merge the changes into the current manifest. A file whose hash equals the current one is not a change.
5. Store the draft. Content for hashes the skill does not already hold goes in the draft's own `draft_files/{sha256}` subcollection, and the draft doc lists them as `staged: [sha256]`. The file docs are written **before** the draft doc (or in one batch), so a valid code never points at missing files.

**Accepted risks.** Model-written files are bounded in practice by Smart's output tokens, not by 5 × 256 KB; large references arrive via `from_file`. The first split of an existing body retypes text the model already holds, which is the paraphrase risk §15.3 avoids for revisions; the preview shows every model-written file, so the owner catches it there.

**Checks** (§8) extend to files. Every file, whether model-written or re-saved, goes through `SecurityPort` and is **rejected** if flagged, because Smart will read it as part of the procedure. As in §8, these checks are hygiene; the security boundary is the command.

**`skill-creator`** gains a section on structure:
- the body says when the skill applies and what to do;
- rarely needed material goes to `references/`, with a line in the body saying when to open it;
- a file from the chat is re-saved with `from_file`, never retyped;
- a revision lists only what changed.

### 15.6 Preview before `$skill save`

The command authorizes what the owner was shown (§8), so all text the owner did not author is shown. `skill_preview` is posted in this order:

1. `SKILL.md`, as a file. Unchanged.
2. **Each file the model wrote or changed, as its own file.** At most 5 per draft. A larger draft is rejected and the model is told to split it, which keeps the post count within the Slack and Telegram rate limits.
3. **A change summary message**, for example:
   ```
   + references/airlines.md (new, 12 KB)
   ~ references/fees.md (changed)
   + references/rates.csv ← "rates (2).csv" (your upload, 4 KB)
   − old.md (removed)
   = 2 files unchanged
   ```
   Re-saved files name their source and are not posted again: an upload is the owner's own, and a `skill:` source was authorized when it was saved. Without the summary a v2 is opaque, since a removed file does not show in the body. The summary is omitted when the skill has no files and none changed, so file-less skills look exactly as they do today.
4. The `$skill save <code>` message, **last**.

The file texts travel in `DeliveryItem.data`; there are at most 5 of them, each 256 KB or less. `ConversationHandler` needs no repository access, so the late-answer path (`conversation_handler.py:1162`) delivers previews unchanged. Today the abort covers the single `send_file` (`conversation_handler.py:282-315`). It becomes a loop: if any post fails, the command is not posted.

### 15.7 Save, delete, list

- **`$skill save`** runs one Firestore transaction. All reads come first:
  - the index doc;
  - the cap query;
  - the draft's files;
  - a presence check for every inherited hash, via `get_all(refs, field_paths=["size"], transaction=…)` so it does not pull file contents.

  Then the writes:
  - the draft's file documents are copied into the skill's `files/`;
  - the version is written with its manifest;
  - `current` is flipped;
  - consumed drafts are deleted, together with their `draft_files/` docs, addressed by reference from each draft's `staged` list (no extra reads).

  The worst case is about 3.4 MB (5 new files of 256 KB plus the version and index docs, up to ~1 MiB each), under Firestore's 10 MiB request bound. Save stays atomic.
- **Skill file docs never carry `expires_at`.** The copy writes `content`, `size`, `created_at` only; a test asserts it.
- **`$skill delete`** removes the index document, `versions/*` and `files/*`. Nothing outside the skill references its files. References are gathered with `list_documents()` (refs only, no content) and deleted in chunked batches.
- **Unsaved drafts** get a Firestore TTL on a new `expires_at` field, set 30 days ahead, on the drafts collection and on the `draft_files` collection group. TTL policies are keyed by collection-group ID across the whole database, so the draft subcollection has its own name: a policy on `files` would also cover every skill's own files. A code older than that gets the existing "no pending draft" reply. This replaces §8's "no TTL: a stale draft is inert": drafts now carry content beyond one document.
- **`$skill list`** shows each skill's file count.

### 15.8 System skills

A system skill's files live next to its `SKILL.md` in git, for example `src/skills/smart/<name>/references/…`.
- `load_system_skills` reads the files into memory and builds each skill's manifest.
- Each of the following fails startup, like a malformed `SKILL.md`: a path that breaks §15.2, a symlink, a non-UTF-8 file, a file over the limits.
- System files skip `SecurityPort`, for the reason given in §6.

### 15.9 Configuration, deployment, acceptance

- **No new configuration key.**
- **Owner deployment step:** TTL policies on `expires_at` for `{prefix}skill_drafts` and the `draft_files` collection group, recorded in `docs/07_deployment/README.md`.
- **Unit tests:**
  - path, extension and limit validation;
  - the manifest bound proving the document budget;
  - inheritance and the stale-code rule;
  - the deleted-skill abort on save;
  - `skill:` resolution: custom, system, shadowing, unknown skill, unknown path, and no route to another user;
  - the `delete_file` refusal;
  - the `sanitize_filename` colon mapping;
  - `from_file` rejecting delivered refs and non-text uploads;
  - `SecurityPort` on files;
  - the `use_skill` files block;
  - preview order, the 5-file cap and the abort loop;
  - the save transaction: copy, presence check, draft cleanup;
  - delete removing `files/`;
  - loader manifests and startup failures.
- **Live acceptance:**
  1. Move a reference part of `domain-competency-research` into `references/`. Smart opens it via `open_file` when it needs it, and not otherwise.
  2. Re-save a `.md` or `.csv` upload from the chat into a custom skill. The summary names the source, and the file opens in a new thread.
  3. A v2 of a skill with files that changes only the body carries the files over.
  4. `skill-creator` splits a long procedure into a body plus a reference on its own.

### 15.10 Not in delivery C

- Binary files and their bucket (owner, 2026-10-06: later, separately).
- Scripts.
- A Cabinet editor.
- Files for Tutor and Lelik (Q2).
- Pinning a ref to a version.
- Audio transcription through `open_file`: the container's `FileConversionService` has no `audio_service` (`service_container.py:306-309`). This is an existing gap, unrelated to skills.

### 15.11 Resolution of G1 findings (revision 1)

| Finding | Resolution |
|---------|------------|
| B1 `from_file` bypasses "the command authorizes what was shown" via delivered docs | `from_file` limited to own uploads and own `skill:` refs; the summary names the source (§15.5, §15.6) |
| M1 binaries have few consumers; acceptance (2) cannot pass | Text-only in Firestore; binaries deferred with their bucket (§15.1, §15.10); acceptance rewritten (§15.9) |
| M2 limits vs the read pipeline | 256 KB per text file (§15.2) |
| M3 1 MiB budget, `blob_refs` growth, unread old blobs | Files in their own documents; the manifest is a bounded list with a validator; no `blob_refs`; old versions' files kept as a stated choice (§15.2) |
| `delete_file` bypasses the dispatcher; error texts | Refusal in `_delete`; skill-specific `FileNotFoundError` (§15.4) |
| `GcsMediaAdapter` mutates HTML | No GCS in C |
| Wiring: order, bucket-gated pipeline, disk reads in services, duplicated shadowing, `list_current` per read | Order stated; limitation stated; loader holds contents in memory; shadowing in one domain function; `get_current`/`get_file` (§15.4) |
| `sanitize_filename` lets `:` through | Map `:` to `_`, plus a test (§15.4) |
| Preview post count; where file texts live | 5 model-written files per draft; texts in `DeliveryItem.data`; abort loop (§15.6) |
| 7-day draft rule vs "a stale draft is inert" | 30-day TTL on drafts; §8's rule replaced explicitly (§15.7) |
| `content_type` unused; extensionless paths | Extension allowlist required; `content_type` dropped from the manifest (§15.2) |
| "Audio is transcribed" false | Claim removed; gap noted (§15.10) |
| Deploy env var; `open_file` description | No new key; manifest description updated (§15.4, §15.9) |

**Targeted re-check (revision 2): ready for planning after F1–F2.** F1 TTL on a shared `files` group → draft subcollection renamed `draft_files` (§15.5, §15.7). F2 draft cleanup reads → `staged` list, `get_all` with field mask (§15.7). F3 mime guessing → `skill:` refs decoded as UTF-8 directly (§15.4). F4 delete reads contents → `list_documents()` (§15.7). F5 draft before its files → files written first (§15.5). Accepted risks stated (§15.5).
