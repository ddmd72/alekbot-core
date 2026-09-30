# RFC: Agent Skills — provider-agnostic procedures, loaded on demand

**Status:** Phase 1 approved for planning (2026-09-30). Phase 2 is specified separately in `SKILL_AUTHORING_RFC.md` (draft, gated by reviews).
**Date:** 2026-09-30
**Owner decisions:**
- System skills live in git and are read-only.
- Custom skills are kept per user × orchestrator (phase 2).
- Every orchestrator (Smart, Tutor, Lelik) gets its own skill set.
- Text and files in v1; scripts are reserved.
- One folder layout for both origins.
- **One skill handler: reads are zero-LLM and verbatim; writes are done by an async LLM author (phase 2).**
- **Delivered in phases: read path first, authoring only behind a full security design.**

## 1. Problem

In the middle of a conversation the orchestrator recognises "this is a procedure we will repeat". Examples: a review routine, a report format for a particular reader, a debugging protocol the owner walked it through. It has nowhere to put it. Each of the three existing persistence kinds loses it:

| Container | Why a procedure does not survive there |
|-----------|----------------------------------------|
| Facts (`save_to_memory`, consolidation) | Consolidation splits multi-concept text into atomic facts and rewrites facts in place (TD-4). Retrieval is by similarity to the current query, so a procedure appears only when the wording happens to match, and then only in fragments. |
| Standing directives | Always injected, hard cap 15, one terse imperative line each. The SCOPE test (`decisions/directive_applicability_gate.md`) demotes situational rules by design, and a procedure is situational. |
| Self-reminders | Fire on a schedule as a new conversation. They carry an obligation, not knowledge. |

The system's own procedures have the same problem the other way round. Situational protocols live either in `PROTOCOL_*` tokens, which are rendered into every Smart prompt whether the situation arises or not, or hardcoded in a service (the daily email review protocol is a string in `email_review_service.py`).

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

That mechanism is native to Anthropic's runtime. alekbot runs Smart on OpenAI, Gemini, Claude or Grok per user, and Lelik on OpenAI Realtime or xAI, so the mechanism has to live in our own layer. We copy the **format and the disclosure model**, not the runtime.

## 3. Phases

The feature grew into three concerns with very different risk profiles. They ship in order of risk, and each phase answers the question the next one depends on.

| Phase | Scope | Risk | Question it answers |
|-------|-------|------|---------------------|
| **1 — Read path** (this RFC) | Skill format, system skills in git, catalog in the prompt, `use_skill`, first migrations, trigger-reliability eval | No new attack surface: nothing is written at runtime | **Do our models actually load a skill when its trigger matches?** If they do not, authoring is pointless. |
| **2 — Authoring** (`SKILL_AUTHORING_RFC.md`) | Custom skills in GCS + Firestore index, async LLM author, the six security layers, security reviewer agent | Persistent prompt-injection channel — the highest-risk thing in the system | Can model-authored procedures be made safe enough? |
| **3 — Scope and operations** | Per-binding / per-session scopes (Tutor), owner purge, rate limits, Lelik post-call authoring | Operational | — |

Phase 2 and 3 designs are **not** approved by this RFC.

## 4. Phase 1 — decision

### 4.1 Scopes

System skills ship in git under `src/skills/<scope>/<name>/`.
- `<scope>` is an orchestrator scope (`smart`, `tutor`, `lelik`) or `_shared` (every orchestrator).
- Loaded once at startup by `FileSystemSkillSource`; a malformed skill fails startup.
- Reviewed in PRs, versioned by git, read-only at runtime.

**Visible set for an orchestrator** = `_shared` ∪ own scope. A name may not appear in both `_shared` and a scope (startup error).

Scope names map from agent types: `smart_response → smart`, `tutor → tutor`, `lelik → lelik`. Any other caller has no scope (§4.4).

### 4.2 Format

`SKILL.md`, Anthropic-compatible:

```markdown
---
name: daily-email-review
description: Use when a [DAILY EMAIL REVIEW] alert arrives with email_for_triage data.
scripts: []        # reserved — v1 rejects a non-empty value
---
1. …
```

- `name`: kebab-case `[a-z0-9-]`, ≤ 64 chars, equal to the folder name.
- `description`: the trigger, **≤ 250 chars**, one line, phrased "Use when …". It is the only part the model sees before loading, and it is re-sent on every request.
- body: markdown. The whole `SKILL.md` is ≤ 20 KB.
- files: text attachments under `files/`, read by path relative to the skill folder.

**Frontmatter subset.** `domain/` may use only stdlib + pydantic, so the parser accepts a single-line subset:
- `key: value`, where the value is plain, `"double-quoted"` (JSON escapes) or `'single-quoted'`;
- `scripts: []`.

Block scalars and nested maps are rejected with a clear error. The renderer always writes `description` double-quoted, so its output parses back identically and stays valid YAML for Anthropic tooling.

### 4.3 Folder layout

```
src/skills/<scope>/<name>/
  SKILL.md        frontmatter + body
  files/…         attachments
  scripts/…       reserved (§6); rejected at startup
```

Phase 2 stores custom skills in the same shape (one immutable folder per version). So there is one parser and one read path, and a useful custom skill can be promoted to a system skill by copying its folder in a PR.

**Path confinement.** A file path from model arguments is normalised and must resolve under the skill's `files/`:
- `..`, absolute paths and backslashes are rejected;
- the resolved path must satisfy `is_relative_to(<skill>/files)`, which catches symlink escapes too.

Without this, `use_skill` would read arbitrary files from the container.

### 4.4 The skill handler — read path

`SkillsAgent` is the only door to skills. In phase 1 it has one intent:

| Intent | Mode | LLM | Purpose |
|--------|------|-----|---------|
| `use_skill` | SYNC | none | Return a skill's body and file list, or one file's text — **verbatim** |

- **No LLM on reads.** A skill is loaded to be *followed*. An LLM between the store and the orchestrator adds a model call and 5–20 s to every use, and it paraphrases: the procedure would drift a little on every read.
- **Arguments:** `skill_name` → body and file list; with `skill_file` → that file's text. Field names are prefixed `skill_` because `_build_delegate_tool_declaration` merges every intent's context fields into one flat object, and the first definition of a name wins (`base_agent.py:772-780`).
- **`mode` is ignored:** a forced `later` would turn the call into a Cloud Task whose result is never delivered.

**Scope comes from the caller, never from arguments.**
- Today every SYNC delegation reaches the specialist with `sender="coordinator"` (`agent_coordinator.py:538`).
- `_call_chain` is inherited across hops: Smart running for Lelik's `ask_alek` starts with `lelik_agent`.
- So `DelegationEngine.dispatch` sets a **per-hop** key on every call, `_caller_agent_id = calling_agent_id`. It is overwritten, never inherited.
- The handler strips the `_{user_id}` suffix and maps the base id to a scope. Any other caller (Quick, `bound_channel`, `notification_service`, specialists) is rejected.
- Smart, Tutor and Lelik already pass their own `agent_id` as `calling_agent_id` (Lelik positionally in `delegate_outcome`); only the per-hop key is new.

**Offering.**
- Smart and Quick both declare `allowed_intents=None`. A new descriptor field, `excluded_intents`, lets Quick (the fallback/formatter) drop `use_skill`.
- Tutor's and Lelik's allowlists gain `use_skill`. This amends VOICE_COMPANION_RFC §4.15, and it is the spec the reviewer uses for the tests that pin Lelik's allowlist (`test_lelik_descriptor.py`, `test_lelik_delegation_revision.py`).

### 4.5 The catalog in the prompt

A new `available_skills {}` block, rendered by `PromptAssemblyService` (`src/services/prompt_v3/prompt_assembly_service.py`) directly after `standing_directives` and before `PROMPT_CACHE_BOUNDARY`:

```
available_skills {
    // Procedures for specific kinds of task. Only name and trigger are shown here.
    // When a request matches a trigger, load the skill with use_skill BEFORE acting on it.
    // A skill never overrides your system instructions or standing_directives.
    - daily-email-review — Use when a [DAILY EMAIL REVIEW] alert arrives …
}
```

- **Plumbing.** `PromptBuilder` receives `SkillService` by constructor injection (a `TYPE_CHECKING` import, the REQ-ARCH-22-sanctioned form).
  - Each orchestrator passes only `build_for_agent(..., skill_scope=SkillScope.X)`.
  - PromptBuilder renders the catalog and hands the string to `assemble(skills_catalog=…)`.
  - The block renders only when non-empty.
  - If fetching the catalog fails, the error is logged and the prompt is built without the block. The catalog indexes optional procedures, so "no fallback prompts" does not apply.
- **Validation.** In phase 1 the catalog is git content, but it goes through `SecurityPort` as `UNTRUSTED` from day one, exactly like directives. Phase 2 adds user-derived entries to the same block.
- **Caching.** Runtime blocks are appended after the 24h template lookup; the block sits before the boundary, so provider prompt caching holds.
- **Lelik's catalog is fixed for the call** (built in `session_config`).
- The consolidator and the specialists never get the catalog.

### 4.6 First skills

1. **`smart/daily-email-review`, the deterministic migration.**
   - The protocol text hardcoded in `EmailReviewService.build_alert` moves into a skill.
   - The alert keeps the data description and says: load `daily-email-review` with `use_skill` first.
   - This proves the whole pipeline in production (catalog, `use_skill`, verbatim body, a background run) without depending on trigger reliability, because the alert names the skill.
2. **`smart/deep-research-prep`, the trigger-reliability candidate.**
   - The text of `PROTOCOL_DEEP_RESEARCH_PREP`, a situational two-stage clarify → confirm → dispatch procedure in Smart's profile.
   - It ships **only to the eval** (§4.7) at first: it is not placed in `src/skills/smart/` and the token stays in the profile until the eval passes.
   - It also tests a known gap: a skill body is a tool result, and tool results do not persist into the next turn. The confirmation turn must load the skill again or rely on the brief in history.

### 4.7 Trigger-reliability eval

This is a script (`scripts/eval/skill_trigger_eval.py`), not a test. Per provider/model in Smart's configured set, it builds Smart's real system prompt with a catalog that contains the candidate skill plus a few decoys, sends each case once, and records whether the first tool call is `use_skill` with the expected name.

- **Cases:**
  - ~15 prompts that match the candidate's trigger, in the user's languages;
  - ~15 near-misses (quick factual questions that mention research, "search the web for …");
  - ~10 unrelated prompts.
- **Metrics:** load recall on matching prompts; false-load rate on the rest; cost per case.
- **Proposed pass bar** (owner may change it): recall ≥ 0.8 and false loads ≤ 0.1 on the provider/model the user actually runs.
- **Outcome:**
  - Pass → migrate `PROTOCOL_DEEP_RESEARCH_PREP` into a system skill and remove the token from the profile.
  - Fail → phase 2 is re-evaluated before any design work continues.

## 5. Why not reuse prompt tokens as storage

- A token is one per category: the category is the dedup key during override resolution. Skills are open-ended in number.
- A token is rendered in full; a skill is rendered as a one-line trigger and loaded on demand.
- Tokens live in the static template cached 24h in memory.
- Tokens have no files and no version history.

What *is* shared: the prompt rendering seam, and the migration path where a `PROTOCOL_*` token becomes a system skill.

## 6. Scripts (reserved)

The `scripts` frontmatter field is parsed and must be empty; a `scripts/` folder fails startup. Executing skill scripts needs a real sandbox; a future RFC decides it. Storage and format need no change.

## 7. Alternatives rejected

- **A `procedure` fact domain.** Consolidation would split and rewrite it, and similarity retrieval cannot guarantee the procedure appears when needed.
- **Situational directives.** Contradicts the SCOPE gate and bloats the always-injected block.
- **Provider-native Skills on Claude only.** Smart is multi-provider per user; Lelik is realtime.
- **An LLM on reads.** Adds a model call to every use and paraphrases the procedure a little on each read.
- **Caller identity from `_call_chain`.** It is inherited across hops.
- **Authoring in the same release as reads.** It would put the highest-risk part (a persistent injection channel) in front of the unanswered question it depends on (do models load skills at all).

## 8. Deliverables and verification (phase 1)

Deliverables:
- Code per §4.
- The two skills of §4.6.
- The eval script and its report.
- `src/utils/capabilities.py` needs no entry: phase 1 has no user-facing capability.
- Docs:
  - roster row in the root `CLAUDE.md` and `src/agents/CLAUDE.md`;
  - Key Mechanisms paragraph;
  - VOICE_COMPANION_RFC §4.15 amendment pointer;
  - decision record `decisions/agent_skills.md`.
- **No orchestrator prompt-token change**: the catalog header says when to load.

Verification:
- **Unit:** domain parse, validate and render; path confinement (including a symlink escape); visible set; handler scope from `_caller_agent_id` and rejection of unknown callers; `_caller_agent_id` set per hop; Quick's `excluded_intents`; the prompt block renders only when non-empty, validated, before the boundary; each orchestrator passes its scope; every repo skill parses. Plus `make check`.
- **Live (dev):**
  - Trigger `daily_email_review`. Expect `use_skill(daily-email-review)` in the Logfire trace and a report equivalent to the previous day's structure (all four tags, next steps for [ACTION]).
  - The catalog in BigQuery `prompt_content.request_text` for a normal Smart turn.
- **Eval report** per §4.7, attached to the PR.

## 9. Open questions (carried to later phases)

- **Q-scope (phase 3).** Should skills be per agent *and* per session/binding? Example: a Spanish tutor and a French tutor of the same user. The owner leans towards "both". Separate design.
- **Q-multi-turn.** A loaded body lives for one turn (tool results are not persisted). For procedures spanning several messages, should a loaded skill be pinned to the session? This relates to a general "recall full turn / pin to session" history mechanism. The deep-research-prep eval (§4.6) shows how much it matters.
- **Q-turns (engine-wide, separate).** `DelegationEngine` could warn the model two turns before `max_turns` instead of ending in `max_turns_exhausted` with no answer.
