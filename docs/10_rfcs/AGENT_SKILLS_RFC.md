# RFC: Agent Skills — provider-agnostic, self-authored procedures

**Status:** Proposed (design approved in conversation 2026-09-30, awaiting RFC review)
**Date:** 2026-09-30
**Owner decisions:** autonomous authoring with a per-user confirm switch; system skills in git
(read-only) + custom skills per user × orchestrator; text + files in v1, scripts reserved; every
orchestrator (Smart, Tutor, Lelik) gets its own skill set.

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
frontmatter (`name`, `description`), a markdown body, optional bundled files and scripts;
only `name + description` sit in context, the body is read when the model decides the skill
applies (progressive disclosure). The mechanism is native to Anthropic's runtime. alekbot runs
Smart on OpenAI/Gemini/Claude/Grok per user, Lelik on OpenAI Realtime or xAI, so the mechanism has
to live in our own layer. We copy the **format and the disclosure model**, not the runtime.

## 3. Decision

### 3.1 Two origins

- **System skills** — shipped in git under `src/skills/<scope>/<name>/SKILL.md` (+ `files/…`),
  where `<scope>` is an orchestrator type (`smart`, `tutor`, `lelik`) or `_shared` (every
  orchestrator). Loaded once at startup by `FileSystemSkillSource`. Reviewed in PRs, versioned by
  git, read-only at runtime. First one: `_shared/writing-skills`.
- **Custom skills** — authored by the model at runtime, stored per **user × orchestrator**.
  Lelik's skills and Alek's skills are separate namespaces: they serve different media (a phone
  call vs chat) and would mislead each other.

**No override.** A custom skill may not take a system skill's name (write rejected). The USER >
SYSTEM override familiar from prompt tokens is deliberately not offered: system skills will carry
migrated `PROTOCOL_*` behaviour (§8), and a self-authored override would be a drift channel into it.

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

- `name`: kebab-case, ≤ 64 chars, unique within (user, orchestrator) ∪ system scope.
- `description`: the trigger, ≤ 1024 chars, phrased "Use when …". This is the only part the model
  sees before loading, so it decides whether the skill is ever used.
- body: markdown, ≤ ~20 KB.
- files: text-like attachments (templates, examples, reference lists), ≤ 1 MB each, read by path.
- The owning orchestrator is **not** a frontmatter field the model writes; it is derived (§3.4).

### 3.3 Storage

- Domain: `src/domain/skill.py` — `Skill`, `SkillFile`, `SkillVersion`, `SkillOrigin`;
  pure `parse_skill_md` / `render_skill_md`, validation, `render_catalog`.
- Port `SkillRepository` (`src/ports/skill_repository.py`) — custom skills only; justified as a
  system boundary (Firestore + GCS) with test substitution.
- `FirestoreSkillRepository`: collection `{prefix}skills`, doc id `{user_id}:{agent}:{name}`,
  subcollection `versions`. Files in GCS under `skills/{user_id}/{agent}/{name}/{path}` through the
  existing `FileStoragePort`.
- **Every write stores an immutable version** (create, update, delete, attach). Delete is a
  tombstone version, so it can be restored. This is the lesson of TD-4: no path destroys content.
- `SkillService` (`src/services/skill_service.py`): merges system + custom for (user, agent),
  enforces the no-override rule, caps (30 custom skills per orchestrator, body and file sizes,
  mime allowlist), versioning, restore.

### 3.4 Tools — `SkillsAgent` (zero-LLM)

A specialist in `agent_manifest.py`, same shape as `save_to_memory`: structured `context_schemas`,
no LLM parsing — the orchestrator writes the skill text itself, the agent persists it verbatim.

- `use_skill` (SYNC) — `context.name` → body + file list; `context.file` → that file's text.
- `manage_skills` (SYNC) — `context.action` ∈ `create | update | delete | restore | attach_file |
  list_versions`, with `name`, `description`, `body`, `file_path`, `file_content` or `file_ref`
  (a user upload becomes a skill file), `version`.

**Namespace comes from the caller, never from arguments.** The owning orchestrator is resolved
from `AgentMessage.sender` (agent id → agent type) as it arrives through `DelegationEngine`. Lelik
cannot write into Smart's namespace by naming it. Implementation must verify `sender` survives the
`/voice/delegate` → `LelikAgent.delegate` → `DelegationEngine.dispatch` path; if it does not, that
path is fixed, not bypassed.

Offered to Smart (`allowed_intents=None`), and added to the allowlists of Tutor and Lelik.
Quick is excluded — it is the fallback/formatter, not an orchestrator that should learn.

### 3.5 Prompt — the catalog

A new `available_skills {}` block rendered by `PromptAssemblyService` directly after
`standing_directives`, before `PROMPT_CACHE_BOUNDARY`:

```
available_skills {
    // Procedures for specific kinds of task. Only name and trigger are shown here.
    // When a request matches a trigger, load the skill with use_skill BEFORE acting on it.
    - writing-skills — Use when you notice a procedure worth keeping, or the user asks you to …
    - weekly-finance-review — Use when …
}
```

- Plumbed exactly like directives: `PromptBuilder.build_for_agent(..., skills_catalog=…)` →
  `assemble()`; the block renders only when non-empty. It is not part of the 24h-cached static
  template — it is rendered per request from `SkillService`, so an edit is visible on the next turn.
- Placement before the boundary keeps provider prompt caching: the catalog changes only on a skill
  write.
- Smart, Tutor and Lelik each pass their own catalog. The consolidator and specialists never get it.

### 3.6 Authoring policy

`UserBotConfig.skill_authoring: "autonomous" | "confirm"`, default `autonomous` (Claude Code
behaviour: the model decides when a procedure is worth keeping). In `confirm`, the catalog header
tells the model to obtain an explicit yes before calling `manage_skills`.

Honest limit: chat approval is mediated by the model; nothing in code can prove the user said yes.
The actual safety net is the version history (§3.3) plus the chat marker (§3.7). A Cabinet view is
the eventual review surface (§9).

### 3.7 Chat marker

The skill body is never posted to chat. After a successful write the orchestrator adds one line —
`📘 skill weekly-finance-review → v3` / `🗑 skill weekly-finance-review removed` — so drift is
noticeable without reading anything. On Lelik the marker is not spoken; it is recorded in the call
summary.

### 3.8 System skill #1 — `writing-skills`

Given to every orchestrator. Content: when a skill is the right container (vs directive, memory,
reminder); the description is the trigger and must say when, not what; body is a concise
imperative procedure; update an existing skill instead of creating a near-duplicate; use files for
templates and examples; never put secrets or PII in a skill.

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
- **Load the body through a dedicated per-provider tool instead of an intent** — the delegation
  engine is the one tool channel every orchestrator already has; a second channel means per-adapter
  work for no gain.
- **Chat approval by default** — owner preference is Claude-Code-style autonomy; kept as a switch.

## 6. Relation to "self-notes rejected permanently"

`decisions/standing_directives.md` rejects autonomous agent self-notes: agent-graded
self-corrections drift. Skills differ in three ways that address the failure: they are procedures
the owner walked through, not the agent grading itself; every write is versioned and restorable;
every write leaves a visible marker. If drift shows up anyway, the switch to `confirm` is one
config field.

## 7. Scripts (reserved, not built)

The `scripts` frontmatter field is parsed and must be empty in v1. Executing skill scripts needs a
real sandbox (Gemini `code_execution` is compute-only, no files, no network). A future RFC decides
the sandbox; the storage and format need no change.

## 8. Migrating PROTOCOL tokens (follow-up)

A `PROTOCOL_*` token that applies only to some requests becomes a system skill: its trigger goes
into `description`, its text into the body, and the token leaves the profile. This shrinks the
static prompt and moves situational guidance behind progressive disclosure. First candidate to be
chosen after v1 is live; the daily-email-review protocol is a likely one because it runs in a
single task type.

## 9. Out of scope for v1

Script execution; Cabinet editor/review UI; consolidation reading or curating skills (possible
later bridge: Stage 2b demotions of situational directives could propose a skill); PROTOCOL
migration; Quick.

## 10. Verification

- Unit: domain parse/validate/render; service merge, no-override, caps, versions, restore
  (mocked repository); SkillsAgent actions and namespace-from-sender; prompt assembly renders the
  block only when non-empty and before the boundary; manifest/architecture tests via `make check`.
- Integration: Firestore repository, same pattern as the other Firestore repos.
- Live (dev): ask Alek to keep a protocol → marker in chat, doc + version in `development_skills`;
  next conversation → catalog in BigQuery `prompt_content.request_text`, `use_skill` in the Logfire
  trace; a Lelik call → catalog in session config, `use_skill` through `/voice/delegate`.
