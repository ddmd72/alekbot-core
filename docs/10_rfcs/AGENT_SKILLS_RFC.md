# RFC: Agent Skills — provider-agnostic, self-authored procedures

**Status:** Proposed — revision 3: a single skill handler; reads verbatim, writes by an async LLM author (2026-09-30)
**Date:** 2026-09-30
**Owner decisions:**
- Authoring is autonomous, with a per-user confirm switch.
- System skills live in git and are read-only.
- Custom skills are kept per user × orchestrator.
- v1 stores text and files; scripts are reserved.
- Every orchestrator (Smart, Tutor, Lelik) gets its own skill set.
- One folder layout for both origins, with Firestore as a derived index.
- **One skill handler.** Reads are zero-LLM and verbatim; writes are done by an LLM author, always asynchronously.

**Amends:**
- `decisions/standing_directives.md` ("autonomous self-notes rejected permanently"), see §6.
- `VOICE_COMPANION_RFC.md` §4.15 (Lelik allowlist), see §3.4.

## 1. Problem

In the middle of a conversation the orchestrator recognises "this is a procedure we will repeat". Examples: a review routine, a report format for a particular reader, a debugging protocol the owner walked it through. It has nowhere to put it. Each of the three existing persistence kinds loses it:

| Container | Why a procedure does not survive there |
|-----------|----------------------------------------|
| Facts (`save_to_memory`, consolidation) | Consolidation splits multi-concept text into atomic facts and rewrites facts in place (TD-4). Retrieval is by similarity to the current query, so a procedure appears only when the wording happens to match, and then only in fragments. |
| Standing directives | Always injected, hard cap 15, one terse imperative line each. The SCOPE test (`decisions/directive_applicability_gate.md`) demotes situational rules by design, and a procedure is situational. |
| Self-reminders | Fire on a schedule as a new conversation. They carry an obligation, not knowledge. |

A **skill** is the missing fourth kind: a named, situational procedure. Its *trigger* is always visible; its *body* is loaded only when the trigger matches.

| Kind | Home | Property |
|------|------|----------|
| Facts about the user | `knowledge_base` | retrieved by relevance |
| Rules for the agent, always in force | `standing_directives` | always injected, binding |
| Deferred obligation | `active_reminders` | fires later |
| **Procedure for a kind of task** | **`available_skills` + `use_skill`** | **trigger always visible, body on demand** |

## 2. Prior art and why we rebuild it

Anthropic Skills (Claude Code, Claude API) implement exactly this:
- a `SKILL.md` with YAML frontmatter (`name`, `description`), a markdown body, optional bundled files and scripts;
- only `name + description` sit in context;
- the body is read when the model decides the skill applies (progressive disclosure).

That mechanism is native to Anthropic's runtime, and Anthropic's API offers skill *use* inside its own container, not authoring into a third-party store. alekbot runs Smart on OpenAI, Gemini, Claude or Grok per user, and Lelik on OpenAI Realtime or xAI, so the mechanism has to live in our own layer. We copy the **format and the disclosure model**, not the runtime.

## 3. Decision

### 3.1 Two origins, one scope rule

- **System skills** ship in git under `src/skills/<scope>/<name>/`.
  - `<scope>` is an orchestrator scope (`smart`, `tutor`, `lelik`) or `_shared` (every orchestrator).
  - Loaded once at startup by `FileSystemSkillSource`.
  - Reviewed in PRs, versioned by git, read-only at runtime.
- **Custom skills** are written by the skill author (§3.4) and stored per **user × orchestrator scope**. Lelik's skills and Alek's skills are kept separate: they serve different media (a phone call vs chat) and would mislead each other.

**Visible set for an orchestrator** = `_shared` system ∪ own-scope system ∪ own-scope custom.

**Names are unique within that set.**
- A custom skill may not take the name of a visible system skill; the write is rejected.
- A Lelik custom skill *may* reuse the name of a Smart-only system skill, because the two never meet.
- The USER > SYSTEM override familiar from prompt tokens is deliberately not offered. System skills will carry migrated `PROTOCOL_*` behaviour (§8), and a self-authored override would be a drift channel into it.

Scope names map from agent types: `smart_response → smart`, `tutor → tutor`, `lelik → lelik`. Any other caller has no scope (§3.4).

### 3.2 Format

`SKILL.md`, Anthropic-compatible:

```markdown
---
name: weekly-finance-review
description: Use when the user asks for the weekly finance review or "how did the week go money-wise".
scripts: []        # reserved — v1 rejects a non-empty value
---
1. …
```

- `name`: kebab-case `[a-z0-9-]`, ≤ 64 chars.
- `description`: the trigger, **≤ 250 chars**, phrased "Use when …". It is the only part the model sees before loading, and it is re-sent on every request (§3.5), so it is short by rule.
- body: markdown. The whole `SKILL.md` is ≤ 20 KB.
- files: text attachments (templates, examples, reference lists) under `files/`, ≤ 1 MB each. They are stored as UTF-8 `text/plain` whatever the extension, because the author only produces text.
- The owning scope is **not** a frontmatter field; it is derived from the caller (§3.4).

**Frontmatter subset.** `domain/` may use only stdlib + pydantic, so the parser accepts a single-line subset:
- `key: value`, where the value is plain, `"double-quoted"` (JSON escapes) or `'single-quoted'`;
- `scripts: []`.

Block scalars (`>`, `|`) and nested maps are rejected with a clear error. The renderer always writes `description` double-quoted, so every file it writes parses back identically and stays valid YAML for Anthropic tooling.

### 3.3 Storage — one folder layout for both origins

A skill is always a **folder of the same shape**. For custom skills, every version is its own folder and is immutable.

```
Skill folder shape
  SKILL.md              frontmatter + body
  files/…               attachments, addressed by path relative to the folder
  scripts/…             reserved (§7); v1 rejects any object here

System:  src/skills/<scope>/<name>/                                        (git)
Custom:  gs://<GCS_MEDIA_BUCKET>/skills/<user_id>/<scope>/<name>/v<n>/     (one folder per version)
```

Why one layout:

- **One parser, one read path.** `use_skill` resolves a name to a folder and reads `SKILL.md` or `files/<path>` the same way for both origins. This is the Claude Code model of "a skill is a directory".
- **Promotion is a copy.** A custom skill that proves useful becomes a system skill by copying its current version folder into `src/skills/<scope>/<name>/` in a PR. `PROTOCOL_*` migration (§8) produces the same shape.
- **Scripts need no redesign.** A future sandbox mounts or copies the folder (§7).

**Firestore is an index, not storage.**
- `{prefix}skills/{user_id}:{scope}:{name}` holds only what the catalog and the write path need: `user_id`, `account_id`, `scope`, `name`, `description`, `current` (a version number, or `null` when deleted), `next_version`, `last_request_id` (§3.4 idempotency), `updated_at`.
- The per-request catalog is one Firestore query instead of N GCS reads.
- The index is derived. GCS is always written before the index points at it, so the index never references missing content. In a disaster it can be rebuilt by listing the user's prefix (highest `v<n>`, parse its `SKILL.md`). v1 ships no rebuild code, because nothing produces drift that needs it.

**Write protocol: immutable versions, then a pointer flip.** No live folder is ever overwritten, so no step needs GCS atomicity:

1. **Reserve.** Take `n = next_version` and increment it in a Firestore transaction (`@firestore.async_transactional`, as in `firestore_agent_note_adapter.py`). Two concurrent writes get different `n`.
2. **Write** the complete new folder to `v<n>/`. For an edit that keeps files, copy the unchanged objects of `v<current>/` into `v<n>/` first. A failure here leaves an orphan folder that nothing points to, which is harmless.
3. **Flip** `current = n` (and `description`, `last_request_id`) in a second transaction, only if `current` is still the value read in step 1. Otherwise it is a conflict: the author reloads and retries once (§3.4).

- Delete is a pointer flip to `current = null` (a tombstone); the folders stay.
- Restore is a write whose new `v<n>/` is a copy of the chosen old version.
- No path destroys content — the lesson of TD-4.
- Bucket-level Object Versioning is not used: the bucket is shared with user media and the setting is bucket-wide.

**Path confinement.** Every file path from model arguments is normalised and must resolve under `files/` of the skill folder:
- `..`, absolute paths and backslashes are rejected;
- for system skills the resolved path must also satisfy `is_relative_to(<skill folder>/files)`, which catches symlink escapes too.

Without this, `use_skill` on a system skill would read arbitrary files from the container.

Components (each respects REQ-ARCH-22/23):

- **Domain** `src/domain/skill.py`:
  - `Skill`, `SkillFile`, `SkillIndexEntry`, `SkillOrigin`, `SkillScope`, `SkillAuthoring`;
  - pure `parse_skill_md` / `render_skill_md`;
  - name, description and path validation;
  - `render_catalog`, and the path scheme (`skill_prefix`, `version_prefix`).
- **`FileSystemSkillSource`** (adapter, behind port `SystemSkillSource`): reads `src/skills/` once at startup.
- **Port `SkillContentStore` → `GcsSkillContentStore`**: put, get, list and copy objects under a prefix (`google-cloud-storage` via `asyncio.to_thread`).
  - The existing `FileStoragePort` is **not** reused: it is built for user uploads (Finder-style name dedup, `{user_id}/files/` layout).
  - `MediaStoragePort` has exact keys but no list or copy.
- **Port `SkillIndexRepository` → `FirestoreSkillIndexRepository`**: query by (user, scope), plus the two transactions above.
- **`SkillService`** (`src/services/skill_service.py`) owns the write protocol and the rules:
  - merges the visible set;
  - enforces the name rule, the caps (§3.5) and the size limits;
  - versioning and restore.

  When `GCS_MEDIA_BUCKET` is unset the store is `None`: system skills are still served, and writes fail with a clear error.

### 3.4 The skill handler — `SkillsAgent`

One agent is the only door to skills. It has two intents with deliberately different machinery:

| Intent | Mode | LLM | Purpose |
|--------|------|-----|---------|
| `use_skill` | SYNC | none | Return a skill's body and file list, or one file's text — **verbatim** |
| `author_skill` | **ASYNC** (Cloud Task) | yes — the skill author | Create, change or delete a skill from a natural-language brief |

**Why reads have no LLM.** A skill is loaded to be *followed*. An LLM between the store and the orchestrator adds a model call and 5–20 s to every use. Worse, it paraphrases: the procedure would drift a little on every read. `use_skill` is a store lookup.

**Why writes are an LLM author.** When the orchestrator writes a skill itself, five problems appear, and a dedicated author removes all of them:

| Problem when the orchestrator writes | With the author |
|--------------------------------------|-----------------|
| Writing costs several tool turns inside the user's request (Tutor has 5 in total) | The orchestrator spends one call; the author runs its own loop in its own task |
| The orchestrator sees older model turns only as ≤ 300-char summaries (tiered history loading) — a procedure worked out 8 turns ago arrives lossy | The author reads the session **straight from the session store with `full_text`**, not through the tiering |
| Smart's tier is chosen per request; "save this as a protocol" reads as simple, so an ECO model would write a skill that every later run follows | The author has its own fixed tier (PERFORMANCE) |
| 20 KB of markdown as a JSON tool argument risks malformed arguments on long generations | The orchestrator sends a short brief; the author writes the body in its own output |
| Writing guidance (`writing-skills`) would sit in every orchestrator's catalog | The guidance is the author's own prompt; orchestrator catalogs stay lean |

**Why always async.** One behaviour for every orchestrator:
- The orchestrator answers "writing it down" and continues.
- The author posts the marker when done (§3.7).
- For Lelik this is what makes authoring possible at all. A call can be cut at any moment, but the brief and the call excerpt are already in the Cloud Task payload, so the author finishes after the hang-up.

**`use_skill` arguments:** `skill_name` → body and file list; with `skill_file` → that file's text.

**`author_skill` arguments:**
- `query` — the brief, in natural language: what to capture, change or delete, and in which skill if known. Example: "Save how we just planned the weekly finance review — the steps the user corrected are the final ones."
- `context.skill_material` — optional. Material the author cannot find in history: results of tools the orchestrator ran in *this* request. Tool results are never stored in session history, so without this field the author would not see them.

The orchestrator does not write the skill text.

**What the author reads.** The author gets its inputs pre-loaded, not through tools that browse user data:
- The brief and `skill_material`.
- **The source conversation:**
  - For chat (Smart, Tutor): the last K messages of the origin session (`session_id` from the delegation context), read with `full_text`. K is a config value, 40 by default. A `read_history(before)` tool pages further back.
  - For Lelik: `call_context` (the last 12 call exchanges, which Lelik's delegation already carries).
- **The target scope's catalog**, plus a `read_skill(name)` tool for the bodies it needs, so it can update instead of duplicating.

It gets no email, web or document tools. The procedure comes from the conversation.

**What the author produces.** One structured output (the Agent Output Format standard: `OUTPUT_FORMAT_SKILL_AUTHOR` token, `json.loads`, retry on invalid, no regex fallback). The output is a list of operations — `create`, `update`, `delete`, `attach_file` — or an explicit `no_op` with a reason, for example "nothing procedural in this conversation". `SkillService` applies them through the write protocol. On a conflict at the pointer flip, the author reloads the skill and retries that operation once.

**Idempotency.** A Cloud Task whose handler raises is retried, and a retried author run must not create a second version.
- The coordinator stamps a `skill_request_id` into the task payload at dispatch.
- The flip stores it as `last_request_id`.
- An operation whose target already carries that id is skipped.
- The author catches everything and returns a failure (with a failure marker) rather than raising.

**Scope comes from the caller, never from arguments.** Today every SYNC delegation reaches the specialist with `sender="coordinator"` (`agent_coordinator.py:538`). `_call_chain` is inherited across hops: when Smart runs because of Lelik's `ask_alek`, its chain starts with `lelik_agent`. So neither identifies the caller.

Fix: `DelegationEngine.dispatch` sets a **per-hop** key on every call, `_caller_agent_id = calling_agent_id`, which is overwritten, never inherited. The handler strips the `_{user_id}` suffix and maps the base id to a scope (§3.1). Any other caller — Quick, `bound_channel`, `notification_service`, a specialist — is rejected. For `author_skill` the resolved scope is written into the task payload at dispatch, so the async run cannot be pointed elsewhere.

**Writes only from live conversations.** Smart also runs unattended over untrusted content: the daily email review feeds up to 200 full email bodies through `notify()`, and web and `fetch_url` results arrive the same way. An `author_skill` from such a run would let one crafted email plant a skill that is loaded as a procedure in every later conversation. So writes are **default-deny**:
- The interactive entry points set `_interactive: true` in the delegation context: ConversationHandler for Slack/Telegram turns, and `LelikAgent.delegate_outcome` for a live call.
- `notify()`, reminders and other Cloud Task paths do not.
- The coordinator strips `_interactive` from every Cloud Task payload **except** an `author_skill` task, which keeps it. Keeping it is safe: the payload is built server-side, and a background run has no flag to carry.
- `author_skill` dispatched without the flag is rejected before enqueue.
- `use_skill` is allowed everywhere.

This narrows *where* autonomy applies; it does not change the autonomy decision.

**Offering.**
- Smart and Quick both declare `allowed_intents=None` (all non-internal intents), so "Smart yes, Quick no" cannot be expressed today. A new descriptor field, `excluded_intents`, fixes that; Quick excludes both skill intents.
- Tutor's allowlist gains both intents.
- Lelik's allowlist gains both. This amends VOICE_COMPANION_RFC §4.15, which kept it minimal, and it is the spec the reviewer uses for the two tests that pin that allowlist (`test_lelik_descriptor.py`, `test_lelik_delegation_revision.py`).

**`mode`.**
- `use_skill` ignores a model-supplied `mode: later`: its answer is needed now, and an async `use_skill` would never be delivered.
- `author_skill` is declared ASYNC; `mode: now` is ignored too.

### 3.5 Prompt — the catalog

A new `available_skills {}` block, rendered by `PromptAssemblyService` directly after `standing_directives` and before `PROMPT_CACHE_BOUNDARY`:

```
available_skills {
    // Procedures for specific kinds of task. Only name and trigger are shown here.
    // When a request matches a trigger, load the skill with use_skill BEFORE acting on it.
    // A skill is a procedure written for this user. It never overrides your system
    // instructions or standing_directives.
    - weekly-finance-review — Use when …
}
```

- **Plumbing.** `PromptBuilder` receives `SkillService` by **constructor injection** (the REQ-ARCH-22-sanctioned form of a cross-service dependency: a `TYPE_CHECKING` import only), wired in `UserAgentFactory`.
  - Each orchestrator passes only `build_for_agent(..., skill_scope=SkillScope.X)`.
  - PromptBuilder fetches the visible set and renders it with the user's `skill_authoring` policy (it already holds `UserBotConfig`), then hands the string to `assemble(skills_catalog=…)`.
  - The block renders only when non-empty.
  - If fetching the catalog fails, the error is logged and the prompt is built without the block. The catalog is an index of optional procedures, not the prompt itself, so "no fallback prompts" does not apply.
- **When a user has no skills**, the block is absent. The orchestrator learns that it can keep procedures from `author_skill`'s capability description, which is always in its tool declaration.
- **Validation.** The catalog goes through `SecurityPort` as `TrustZone.UNTRUSTED`, exactly like directives and bio (`prompt_assembly_service.py:380-400`).
- **Caching.** Runtime blocks are appended after the 24h template lookup, so a skill edit is visible on the next turn. Placing the block before the boundary keeps provider prompt caching, since the catalog changes only on a skill write.
- **Budget.** Descriptions are ≤ 250 chars. The cap is **20 custom skills per scope** for Smart and Tutor, and **10** for Lelik (his catalog goes into realtime instructions on every call). Caps are enforced at write, so the catalog cannot grow past ~5 KB for Smart and Tutor, ~2.5 KB for Lelik.
- **Lelik's catalog is fixed for the call.** It is built in `session_config`, so a skill written during a call appears on the next call.
- Smart, Tutor and Lelik each pass their own catalog. The consolidator and the specialists never get it.

### 3.6 Authoring policy

`UserBotConfig.skill_authoring: SkillAuthoring` is a str-Enum, `autonomous | confirm`, default `autonomous`. A tolerant validator makes an unknown stored value fall back to the default instead of failing `UserBotConfig` load.

- `autonomous`: the orchestrator sends `author_skill` whenever it judges a procedure worth keeping, as in Claude Code.
- `confirm`: the catalog header and the capability description tell it to get an explicit yes from the user before sending one.

Honest limits:
- Chat approval is mediated by the model; nothing in code can prove the user said yes.
- Per-user config is cached with the agent (~1 h TTL), so a toggle can take up to an hour to apply.
- v1 has no Cabinet page or command to flip it; it is a Firestore edit.

The real safety nets are versions (§3.3), the live-conversation rule (§3.4) and the marker (§3.7).

### 3.7 Chat marker — posted by the handler, not the model

A marker phrased by the orchestrator would be a prompt rule: the model could drop or rephrase it, and several paths drop delivery items anyway (Tutor, Lelik's `delegate`, `notify()`). So the **author posts the marker itself** when its run ends. It uses `UserNotificationService.notify_raw`, with `origin_channel_id` / `origin_platform` from the task context, the same pattern FileManagement uses.

The marker is one line per operation and never includes the body:
- `📘 skill weekly-finance-review → v3`
- `🗑 skill weekly-finance-review removed`
- `↩ skill weekly-finance-review restored from v2`

A run that changed nothing posts its reason, for example `📘 nothing to save: …`. A failed run posts a failure line. Strings go through `LocalizationPort` (new keys in `src/locales/{uk,en,fr,es}.py`). For a Lelik call the marker lands in the caller's primary chat.

### 3.8 The author's guidance

The author's prompt profile (tokens in Firestore, per NEW_AGENT_PLAYBOOK) carries what an earlier revision put in a `writing-skills` catalog skill:
- when a skill is the right container, versus a directive, memory or a reminder;
- the description is the trigger, must say *when* rather than *what*, and stays ≤ 250 chars;
- the body is a concise imperative procedure;
- update an existing skill instead of creating a near-duplicate;
- use files for templates and examples;
- never put secrets or PII in a skill;
- write the final, corrected procedure — the user's later corrections win over earlier drafts in the conversation.

Nothing about authoring remains in the orchestrators' catalogs. System skills in `src/skills/` start empty; the first ones will come from PROTOCOL migration (§8).

## 4. Why not reuse prompt tokens

Tokens were the obvious candidate, since they already have per-agent profiles and USER > SYSTEM priority. They are rejected as storage:
- A token is one per category: the category is the dedup key during override resolution. Skills are open-ended in number.
- A token is rendered in full; a skill is rendered as a one-line trigger and loaded on demand.
- Tokens live in the static template cached 24h in memory, so a custom skill edit would be invisible for a day.
- Tokens have no files and no version history.

What *is* shared: the prompt rendering seam (a per-request block next to `standing_directives`) and the migration path in §8, where a `PROTOCOL_*` token becomes a system skill.

## 5. Alternatives rejected

- **A `procedure` fact domain.** Consolidation would split and rewrite it, and similarity retrieval cannot guarantee the procedure appears when needed.
- **Situational directives.** Contradicts the SCOPE gate and bloats the always-injected block.
- **Provider-native Skills on Claude only.** Smart is multi-provider per user; Lelik is realtime.
- **The orchestrator writes the skill itself through `manage_skills` (revision 2).** Rejected for the five problems in the §3.4 table.
- **An LLM on reads too ("return skill X" through the author).** Adds a model call to every use and paraphrases the procedure a little on each read.
- **Synchronous authoring in chat, async only for Lelik.** Two behaviours for one feature, and the chat user waits 20–60 s for a background concern.
- **Overwrite in place with a `.versions/` backup copy.** Racy (two writers pick the same `n`), and a GCS "move" is N copies plus deletes. Immutable version folders with a pointer flip need neither.
- **Caller identity from `_call_chain`.** It is inherited across hops, so it names the first agent of the chain, not the caller.

## 6. Revision of "autonomous self-notes rejected permanently"

`decisions/standing_directives.md` rejects autonomous agent self-notes, because agent-graded self-corrections drift. This RFC **deliberately revises** that for procedures: the model decides on its own when a procedure is worth keeping (§3.6).

What differs from the self-notes that failed:
- Skills are procedures (how to do a kind of task), not the agent grading its own behaviour.
- They are loaded only on a matching trigger, not injected everywhere.
- They are written by a dedicated author from the full conversation, not by whichever tier the request happened to get.
- Every write is an immutable, restorable version and posts a deterministic marker.
- Writes happen only from live conversations.

The decision record is amended with a pointer here.

**Revert trigger**, measured from the index and the markers and reviewed one month after launch. If any of the following holds, switch the default to `confirm`:
- more than 3 restores;
- more than ~2 writes per active day, sustained over a week;
- any skill the owner did not recognise.

## 7. Scripts (reserved, not built)

The `scripts` frontmatter field is parsed and must be empty; objects under `scripts/` are rejected. Executing skill scripts needs a real sandbox (Gemini `code_execution` is compute-only, with no files and no network). A future RFC decides the sandbox; storage and format need no change.

## 8. Migrating PROTOCOL tokens (follow-up)

A `PROTOCOL_*` token that applies only to some requests becomes a system skill: its trigger goes into `description`, its text into the body, and the token leaves the profile. This shrinks the static prompt and moves situational guidance behind progressive disclosure. The first candidate is chosen after v1 is live. The daily-email-review protocol is a likely one, because it runs in a single task type.

## 9. Out of scope for v1

- Script execution.
- A Cabinet editor or review UI, and a toggle for `skill_authoring`.
- Consolidation reading or curating skills (see Q3).
- PROTOCOL migration.
- Quick.
- A general "recall a full older turn" / "pin to session" mechanism. That is a history concern, not a skills one, and it gets its own RFC if needed. The author avoids the problem by reading `full_text` directly.

## 10. Deliverables and verification

Deliverables beyond the code in §3:
- The author's prompt profile in Firestore: blueprint, `COGNITIVE_PROCESS_SKILL_AUTHOR` and `OUTPUT_FORMAT_SKILL_AUTHOR` tokens.
- The `src/utils/capabilities.py` (`get_help`) entry.
- **No orchestrator prompt-token change in v1.** The catalog header says when to load a skill, and `author_skill`'s capability description says when to write one. A `PROTOCOL_*` or Lelik few-shot addition is made only if live use shows skills are never created or never loaded. It then follows NEW_AGENT_PLAYBOOK Phase 3: behavioural guidance only, and Lelik steered by few-shot examples.
- Docs:
  - roster rows in the root `CLAUDE.md` and in `src/agents/CLAUDE.md`;
  - amendment pointers in `decisions/standing_directives.md` and VOICE_COMPANION_RFC §4.15;
  - a decision record.
- NEW_AGENT_PLAYBOOK Phase 0 answers for the handler: one agent with a zero-LLM read path and an LLM write path.

Verification:
- **Unit tests:**
  - Domain: parse, validate and render; path confinement.
  - `SkillService`: the visible set, the name rule, caps, the write protocol (reserve → write → flip, conflict on a stale `current`), tombstone, restore, store-is-None.
  - Handler read path: scope from `_caller_agent_id`, rejection of unknown callers.
  - Author: operations applied through the service, `no_op`, idempotent retry, failure marker without raising, full-text history loading, the Lelik `call_context` source.
  - Dispatch: `_caller_agent_id` set per hop; `_interactive` kept only for `author_skill` tasks; `author_skill` rejected without it.
  - Quick's `excluded_intents`.
  - Prompt assembly renders the block only when non-empty, validated, and before the boundary.
  - `make check`.
- **Integration:** `GcsSkillContentStore` and `FirestoreSkillIndexRepository`.
- **Eval before rollout (Q4):** does each provider actually call `use_skill` when a trigger matches, and `author_skill` when it should?
- **Live (dev):**
  - Work out a procedure with Alek in chat and ask him to keep it. He answers "writing it down", then the marker arrives, a `v1/` folder appears in GCS and an index doc in `development_skills`.
  - Next conversation: the catalog is in BigQuery `prompt_content.request_text`, and `use_skill` appears in the Logfire trace.
  - Trigger `daily_email_review`: no `author_skill` is possible.
  - A Lelik call where the owner asks to keep something, then hangs up at once: the marker still arrives.

## 11. Open questions

- **Q1 — Lelik authoring.** The async author removes the hang-up problem. Open: is 12 exchanges of `call_context` enough material? The alternative is running the author after the call on the full transcript (`/voice/submit-transcript` already receives it), with Lelik only marking "worth keeping" during the call. **Owner to decide.**
- **Q2 — Tool-turn exhaustion in general.** It is less pressing for skills now (one call). `DelegationEngine` could warn the model two turns before `max_turns` so it can wrap up instead of ending in `max_turns_exhausted` with no answer. This is engine-wide, so it is a separate change.
- **Q3 — Consolidation duplicates skills.** Consolidation sees the same conversation and may extract the procedure as facts, and Stage 2b may promote parts of it to a directive. The consolidator does not know skills exist. Options: tell consolidation that a conversation span produced a skill (a marker in history) so it skips it, or accept duplication for v1 and measure.
- **Q4 — Trigger reliability per provider.** Claude follows "load the skill before acting" well. GPT, Gemini and Grok on our prompts are unknown. This needs an eval before rollout; skills that are written and never loaded are worse than none.
