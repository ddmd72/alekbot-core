# RFC: Agent Skills — provider-agnostic, self-authored procedures

**Status:** Proposed — revision 2 after architecture review (2026-09-30)
**Date:** 2026-09-30
**Owner decisions:** autonomous authoring with a per-user confirm switch; system skills in git
(read-only) + custom skills per user × orchestrator; text + files in v1, scripts reserved; every
orchestrator (Smart, Tutor, Lelik) gets its own skill set; one folder layout for both origins with
Firestore as a derived index.
**Amends:** `decisions/standing_directives.md` ("autonomous self-notes rejected permanently", §6);
`VOICE_COMPANION_RFC.md` §4.15 (Lelik allowlist, §3.4).

## 1. Problem

In the middle of a conversation the orchestrator recognises "this is a procedure we will repeat" —
a review routine, a report format for a particular reader, a debugging protocol the owner walked it
through — and has nowhere to put it. The three existing persistence kinds each lose it:

| Container | Why a procedure does not survive there |
|-----------|----------------------------------------|
| Facts (`save_to_memory`, consolidation) | Consolidation atomises multi-concept text and rewrites facts in place (TD-4). Retrieval is by similarity to the current query, so a procedure appears only when the wording happens to match, and in fragments. |
| Standing directives | Always injected, hard cap 15, one terse imperative line each. The SCOPE test (`decisions/directive_applicability_gate.md`) demotes situational rules by design — a procedure is situational. |
| Self-reminders | Fire on a schedule as a new conversation. They carry an obligation, not knowledge. |

A **skill** is the missing fourth kind: a named, situational procedure whose *trigger* is always
visible and whose *body* is loaded only when the trigger matches.

| Kind | Home | Property |
|------|------|----------|
| Facts about the user | `knowledge_base` | retrieved by relevance |
| Rules for the agent, always in force | `standing_directives` | always injected, binding |
| Deferred obligation | `active_reminders` | fires later |
| **Procedure for a kind of task** | **`available_skills` + `use_skill`** | **trigger always visible, body on demand** |

## 2. Prior art and why we rebuild it

Anthropic Skills (Claude Code, Claude API) implement exactly this: a `SKILL.md` with YAML
frontmatter (`name`, `description`), a markdown body, optional bundled files and scripts; only
`name + description` sit in context, the body is read when the model decides the skill applies
(progressive disclosure). The mechanism is native to Anthropic's runtime. alekbot runs Smart on
OpenAI/Gemini/Claude/Grok per user and Lelik on OpenAI Realtime or xAI, so the mechanism has to live
in our own layer. We copy the **format and the disclosure model**, not the runtime.

## 3. Decision

### 3.1 Two origins, one scope rule

- **System skills** — shipped in git under `src/skills/<scope>/<name>/`, where `<scope>` is an
  orchestrator scope (`smart`, `tutor`, `lelik`) or `_shared` (every orchestrator). Loaded once at
  startup by `FileSystemSkillSource`. Reviewed in PRs, versioned by git, read-only at runtime.
  First one: `_shared/writing-skills`.
- **Custom skills** — authored by the model at runtime, stored per **user × orchestrator scope**.
  Lelik's skills and Alek's skills are separate: they serve different media (a phone call vs chat)
  and would mislead each other.

**Visible set for an orchestrator** = `_shared` system ∪ own-scope system ∪ own-scope custom.
**Names are unique within that set.** A custom skill may not take a name already used by a visible
system skill (write rejected). A Lelik custom skill *may* reuse the name of a Smart-only system
skill — they never meet. The USER > SYSTEM override familiar from prompt tokens is deliberately not
offered: system skills will carry migrated `PROTOCOL_*` behaviour (§8), and a self-authored override
would be a drift channel into it.

Scope names map from agent types: `smart_response → smart`, `tutor → tutor`, `lelik → lelik`.
Any other caller has no scope (§3.4).

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
- `description`: the trigger, **≤ 250 chars**, phrased "Use when …". It is the only part the model
  sees before loading, and it is re-sent on every request (§3.5), so it is short by rule.
- body: markdown; whole `SKILL.md` ≤ 20 KB.
- files: text attachments (templates, examples, reference lists) under `files/`, ≤ 1 MB each,
  stored as UTF-8 `text/plain` whatever the extension — every input path already yields text
  (model-written `skill_file_text`, or a `file_ref` upload converted by the coordinator).
- The owning scope is **not** a frontmatter field; it is derived from the caller (§3.4).
- **Frontmatter subset.** `domain/` may use only stdlib + pydantic, so the parser accepts the
  single-line subset: `key: value`, the value plain, `"double-quoted"` (JSON escapes) or
  `'single-quoted'`, and `scripts: []`. Block scalars (`>`, `|`) and nested maps are rejected with a
  clear error. The renderer always writes `description` double-quoted, so every file it writes
  parses back identically and stays valid YAML for Anthropic tooling.

### 3.3 Storage — one folder layout for both origins

A skill is always a **folder of the same shape**. For custom skills, every version is such a folder
and is immutable.

```
Skill folder shape
  SKILL.md              frontmatter + body
  files/…               attachments, addressed by path relative to the folder
  scripts/…             reserved (§7); v1 rejects any object here

System:  src/skills/<scope>/<name>/                              (git)
Custom:  gs://<GCS_MEDIA_BUCKET>/skills/<user_id>/<scope>/<name>/v<n>/   (one folder per version)
```

Why one layout:

- **One parser, one read path.** `use_skill` resolves a name to a folder and reads `SKILL.md` or
  `files/<path>` the same way for both origins — the Claude Code model of "a skill is a directory".
- **Promotion is a copy.** A custom skill that proves useful becomes a system skill by copying its
  current version folder into `src/skills/<scope>/<name>/` in a PR. `PROTOCOL_*` migration (§8)
  produces the same shape.
- **Scripts need no redesign.** A future sandbox mounts or copies the folder (§7).

**Firestore is an index, not storage.** `{prefix}skills/{user_id}:{scope}:{name}` holds only what
the catalog and the write path need: `user_id`, `account_id`, `scope`, `name`, `description`,
`current` (version number or `null` when deleted), `next_version`, `updated_at`. The per-request
catalog is one Firestore query instead of N GCS reads. The index is derived: GCS is always written
before the index points at it, so the index never references missing content, and in a disaster it
can be rebuilt by listing the user's prefix (highest `v<n>`, parse its `SKILL.md`). v1 ships no
rebuild code — nothing produces drift that needs it.

**Write protocol — immutable versions, pointer flip.** No live folder is overwritten, so no step
needs GCS atomicity:

1. **Reserve** `n = next_version` and increment it in a Firestore transaction
   (`@firestore.async_transactional`, as in `firestore_agent_note_adapter.py`). Two concurrent
   writes get different `n`.
2. **Write** the complete new folder to `v<n>/` (for `attach_file` and `update`, copy the unchanged
   objects of `v<current>/` into `v<n>/` first). A failure here leaves an orphan folder that nothing
   points to — harmless.
3. **Flip** `current = n` (and `description`) in a second transaction, only if `current` is still
   the value read in step 1; otherwise fail with a conflict the orchestrator sees as a tool error.

Delete is a pointer flip to `current = null` (tombstone); the folders stay. Restore is a write whose
new `v<n>/` is a copy of the chosen old version. No path destroys content — the lesson of TD-4.
Bucket-level Object Versioning is not used: the bucket is shared with user media and the setting
is bucket-wide.

**Path confinement.** Every file path from model arguments is normalised and must resolve under
`files/` of the skill folder: `..`, absolute paths and backslashes are rejected; for system skills
the resolved path must satisfy `is_relative_to(<skill folder>)`. Without this, `use_skill` on a
system skill would read arbitrary files from the container.

Components (each respects REQ-ARCH-22/23):

- Domain `src/domain/skill.py` — `Skill`, `SkillFile`, `SkillIndexEntry`, `SkillOrigin`,
  `SkillScope`; pure `parse_skill_md` / `render_skill_md`, name/description/path validation,
  `render_catalog`, the path scheme (`skill_folder`, `version_folder`).
- `FileSystemSkillSource` (adapter) — reads `src/skills/` once at startup.
- Port `SkillContentStore` → `GcsSkillContentStore`: put/get/list/copy objects under a prefix
  (`google-cloud-storage` via `run_in_executor`, as `gcs_media_adapter.py` does).
  The existing `FileStoragePort` is **not** reused: it is built for user uploads (Finder-style name
  dedup, `{user_id}/files/` layout); `MediaStoragePort` has exact keys but no list/copy.
- Port `SkillIndexRepository` → `FirestoreSkillIndexRepository`: query by (user, scope), the two
  transactions above.
- `SkillService` (`src/services/skill_service.py`) owns the write protocol and the rules: merges the
  visible set, enforces the name rule, caps (§3.5), size limits, versioning, restore.
  When `GCS_MEDIA_BUCKET` is unset the store is `None`: system skills are still served, writes fail
  with a clear error.

### 3.4 Tools — `SkillsAgent` (zero-LLM)

A specialist in `agent_manifest.py`, same shape as `save_to_memory` and FileManagement: structured
`context_schemas`, no LLM, everything read from **`message.payload`** (context params are spread
there by the coordinator — reading `message.context` is a known silent no-op).

Two intents:

- `use_skill` (SYNC) — `skill_name` → `SKILL.md` body + list of files; with `skill_file` → that
  file's text.
- `manage_skills` (SYNC) — `skill_action` ∈ `create | update | delete | restore | attach_file |
  list_versions`, with `skill_name`, `skill_description`, `skill_body`, `skill_file`,
  `skill_file_text`, `skill_version`. Uploading a user's file uses the existing `file_ref`; it
  arrives as converted text (the coordinator's resolver), which is what v1 stores. If `file_ref` is
  given and no `file_content` arrives, the write fails explicitly.

**Field names are prefixed `skill_`** because `_build_delegate_tool_declaration` merges every
intent's context fields into one flat object and the first definition of a name wins
(`base_agent.py:772-780`); unprefixed `name`/`body`/`file_ref` would collide with other intents.

**`mode` is stripped** from both intents before dispatch (as `LelikAgent.delegate` already does):
forced `later` would turn them into a Cloud Task whose result `AgentWorkerHandler` never delivers.

**Scope comes from the caller, never from arguments.** Today every SYNC delegation reaches the
specialist with `sender="coordinator"` (`agent_coordinator.py:538`), and `_call_chain` is inherited
across hops (Smart running for Lelik's `ask_alek` starts with `lelik_agent` in its chain), so neither
identifies the caller. Fix: `DelegationEngine.dispatch` sets a **per-hop** key
`_caller_agent_id = calling_agent_id` on every call — overwritten, never inherited.
`SkillsAgent` strips the `_{user_id}` suffix, resolves the descriptor's `agent_type` through the
registry (as `_try_lazy_load` does) and maps it to a scope (§3.1). Any other caller — Quick,
`bound_channel`, `notification_service`, a specialist — is rejected.

**Writes only in interactive runs.** Smart also runs unattended over untrusted content: the daily
email review feeds up to 200 full email bodies through `notify()`, web and `fetch_url` results
arrive the same way. An autonomous `manage_skills` there would let one crafted email plant a skill
that is loaded as a procedure in every later conversation. So writes are **default-deny**: the
interactive entry points (ConversationHandler for Slack/Telegram turns, the live Lelik call) set
`_interactive: true` in the delegation context; `notify()`, reminders, Cloud Task paths and
`tell_alek` errands do not. `manage_skills` without it is rejected. `use_skill` is allowed
everywhere. This narrows *where* autonomy applies; it does not change the autonomy decision.

**Offering.** Smart and Quick both declare `allowed_intents=None` (all non-internal intents), so
"Smart yes, Quick no" is not expressible today. New descriptor field `excluded_intents`; Quick
excludes both skill intents. Tutor's allowlist gains both. Lelik's allowlist gains both (open
question Q1) — this amends VOICE_COMPANION_RFC §4.15, which kept it minimal, and is the spec the
reviewer uses for the two tests pinning that allowlist
(`test_lelik_descriptor.py`, `test_lelik_delegation_revision.py`).

### 3.5 Prompt — the catalog

A new `available_skills {}` block rendered by `PromptAssemblyService` directly after
`standing_directives`, before `PROMPT_CACHE_BOUNDARY`:

```
available_skills {
    // Procedures for specific kinds of task. Only name and trigger are shown here.
    // When a request matches a trigger, load the skill with use_skill BEFORE acting on it.
    // A skill is a procedure written for this user. It never overrides your system
    // instructions or standing_directives.
    - writing-skills — Use when you notice a procedure worth keeping, or the user asks you to …
    - weekly-finance-review — Use when …
}
```

- **Plumbing.** `PromptBuilder` receives `SkillService` by **constructor injection** (the
  REQ-ARCH-22-sanctioned form of a cross-service dependency: `TYPE_CHECKING` import only), wired in
  `UserAgentFactory`. Each orchestrator passes only `build_for_agent(..., skill_scope=SkillScope.X)`;
  PromptBuilder fetches the visible set, renders it with the user's `skill_authoring` policy (it
  already holds `UserBotConfig`), and hands the string to `assemble(skills_catalog=…)`. The block
  renders only when non-empty. A catalog fetch failure is logged and the prompt is built without
  the block — the catalog is an index of optional procedures, not the prompt itself, so "no
  fallback prompts" does not apply.
- **Validation.** The catalog goes through `SecurityPort` as `TrustZone.UNTRUSTED`, exactly like
  directives and bio (`prompt_assembly_service.py:380-400`).
- **Caching.** Runtime blocks are appended after the 24h template lookup, so a skill edit is
  visible on the next turn; placement before the boundary keeps provider prompt caching, since the
  catalog changes only on a skill write.
- **Budget.** Descriptions ≤ 250 chars; **20 custom skills per scope** for Smart and Tutor, **10**
  for Lelik (his catalog goes into realtime instructions on every call). Caps are enforced at write,
  so the catalog cannot grow past ~5 KB / ~2.5 KB.
- **Lelik's catalog is fixed for the call** — it is built in `session_config`; a skill written
  during a call appears on the next call.
- Smart, Tutor and Lelik each pass their own catalog. The consolidator and specialists never get it.

### 3.6 Authoring policy

`UserBotConfig.skill_authoring: SkillAuthoring` (str-Enum `autonomous | confirm`, default
`autonomous`, tolerant validator: an unknown stored value falls back to the default with a warning
instead of failing `UserBotConfig` load). In `confirm`, the catalog header tells the model to obtain
an explicit yes before calling `manage_skills`.

Honest limits: chat approval is mediated by the model — nothing in code can prove the user said
yes; per-user config is cached with the agent (~1 h TTL), so a toggle can take up to an hour; v1 has
no Cabinet or command to flip it (Firestore edit). The real safety net is versions (§3.3), the
interactive-only rule (§3.4) and the marker (§3.7).

### 3.7 Chat marker — posted by the agent, not the model

A marker phrased by the orchestrator would be a prompt rule: droppable, rephrasable, and lost on
paths that drop delivery items (Tutor, Lelik's `delegate`, `notify()`). So **`SkillsAgent` posts it
itself** through `UserNotificationService.notify_raw`, with `origin_channel_id` / `origin_platform`
from the delegation context — the pattern FileManagement already uses. One line, no body:
`📘 skill weekly-finance-review → v3`, `🗑 skill weekly-finance-review removed`,
`↩ skill weekly-finance-review restored from v2`. Strings go through `LocalizationPort` (new keys
in `src/locales/{uk,en,fr,es}.py`). During a Lelik call it lands in the caller's primary chat.

### 3.8 System skill #1 — `writing-skills`

In `_shared`, so every orchestrator has it. Content: when a skill is the right container (vs
directive, memory, reminder); the description is the trigger and must say *when*, not *what*, in
≤ 250 chars; the body is a concise imperative procedure; update an existing skill instead of
creating a near-duplicate; use files for templates and examples; never put secrets or PII in a
skill; the caps per scope.

## 4. Why not reuse prompt tokens

Tokens were the obvious candidate (they already have per-agent profiles and USER > SYSTEM
priority). Rejected as storage:

- a token is one-per-category — the category is the dedup key during override resolution; skills
  are open-ended in number;
- a token is rendered in full; a skill is rendered as a one-line trigger and loaded on demand;
- tokens live in the static template cached 24h in memory — a custom skill edit would be invisible
  for a day;
- tokens have no files and no version history.

What *is* shared: the prompt rendering seam (a per-request block next to `standing_directives`)
and the migration path in §8, where a `PROTOCOL_*` token becomes a system skill.

## 5. Alternatives rejected

- **A `procedure` fact domain** — consolidation would atomise and rewrite it; similarity retrieval
  cannot guarantee the procedure appears when needed.
- **Situational directives** — contradicts the SCOPE gate; bloats the always-injected block.
- **Provider-native Skills on Claude only** — Smart is multi-provider per user; Lelik is realtime.
- **A dedicated per-provider tool instead of intents** — the delegation engine is the one tool
  channel every orchestrator already has; a second channel means per-adapter work for no gain.
- **Chat approval by default** — owner preference is Claude-Code-style autonomy; kept as a switch.
- **Overwrite-in-place with a `.versions/` backup copy** — racy (two writers pick the same `n`) and
  a GCS "move" is N copies plus deletes; immutable version folders with a pointer flip need neither.
- **Caller identity from `_call_chain`** — inherited across hops, so it names the first agent of the
  chain, not the caller.

## 6. Revision of "autonomous self-notes rejected permanently"

`decisions/standing_directives.md` rejects autonomous agent self-notes: agent-graded
self-corrections drift. This RFC **deliberately revises** that for procedures — the model decides
on its own when a procedure is worth keeping (§3.6, §3.8). What is different from the failed
self-notes: skills are procedures (how to do a kind of task), not the agent grading its own
behaviour; they are loaded only on a matching trigger, not injected everywhere; every write is an
immutable, restorable version; every write posts a deterministic marker; writes happen only in
interactive runs. The decision record is amended with a pointer here.

**Revert trigger** (measured from the index and the markers, reviewed one month after launch):
more than 3 restores, or more than ~2 writes per active day sustained over a week, or any skill the
owner did not recognise → switch the default to `confirm`.

## 7. Scripts (reserved, not built)

The `scripts` frontmatter field is parsed and must be empty; objects under `scripts/` are rejected.
Executing skill scripts needs a real sandbox (Gemini `code_execution` is compute-only, no files, no
network). A future RFC decides the sandbox; storage and format need no change.

## 8. Migrating PROTOCOL tokens (follow-up)

A `PROTOCOL_*` token that applies only to some requests becomes a system skill: its trigger goes
into `description`, its text into the body, and the token leaves the profile. This shrinks the
static prompt and moves situational guidance behind progressive disclosure. First candidate chosen
after v1 is live; the daily-email-review protocol is a likely one because it runs in a single task
type.

## 9. Out of scope for v1

Script execution; Cabinet editor/review UI and a toggle for `skill_authoring`; consolidation reading
or curating skills (possible later bridge: Stage 2b demotions of situational directives could
propose a skill); PROTOCOL migration; Quick.

## 10. Deliverables and verification

Deliverables beyond the code in §3:

- `src/utils/capabilities.py` (`get_help`) entry.
- Orchestrator guidance: **no prompt-token change in v1.** The Claude Code model is enough — the
  catalog header says when to load, and `writing-skills` (itself in every catalog) carries the
  authoring guidance behind its own trigger. A `PROTOCOL_*` / Lelik few-shot addition is made only
  if live use shows skills are never created or never loaded (then per NEW_AGENT_PLAYBOOK Phase 3,
  behavioural only, Lelik via few-shot examples).
- Docs: roster rows in root `CLAUDE.md` and `src/agents/CLAUDE.md`; amendment pointers in
  `decisions/standing_directives.md` and VOICE_COMPANION_RFC §4.15; a decision record.
- NEW_AGENT_PLAYBOOK Phase 0 answers: zero-LLM specialist, no prompt profile (like FileManagement).

Verification:

- Unit: domain parse/validate/render and path confinement; `SkillService` visible set, name rule,
  caps, write protocol (reserve → write → flip, conflict on stale `current`), tombstone, restore,
  store-is-None; `SkillsAgent` actions, reads from `payload`, scope from
  `_caller_agent_id`, rejection of unknown callers and of non-interactive writes, marker posted via
  notification service; `DelegationEngine` sets `_caller_agent_id` per hop and does not inherit it;
  Quick's `excluded_intents`; prompt assembly renders the block only when non-empty, validated,
  before the boundary. `make check` (includes architecture tests).
- Integration: `GcsSkillContentStore` and `FirestoreSkillIndexRepository`, same pattern as the other
  GCS/Firestore adapters.
- Live (dev): ask Alek in chat to keep a protocol → marker in chat, `v1/` folder under
  `skills/<user>/smart/…` in GCS, index doc in `development_skills`; next conversation → catalog in
  BigQuery `prompt_content.request_text`, `use_skill` in the Logfire trace; trigger
  `daily_email_review` → no skill writes possible; a Lelik call → catalog in session config,
  `use_skill` through `/voice/delegate`.

## 11. Open questions

- **Q1 — Lelik authoring.** Reviewer's concern: writing a multi-kilobyte `skill_body` through a
  realtime voice model is unlikely to produce a good skill, and the tool arguments are generated
  while the caller waits. Options: (a) Lelik gets both intents as designed; (b) Lelik gets
  `use_skill` only in v1, his custom scope is filled later. Recommendation: (a) with the 10-skill
  cap — the owner wants Lelik's skills to be his own, and (b) leaves his custom scope with no writer.
