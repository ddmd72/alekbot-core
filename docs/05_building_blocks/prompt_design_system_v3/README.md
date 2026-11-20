<!-- living-doc
covers:
  src/domain/prompt_v3/agent_profile.py
  src/domain/prompt_v3/blueprint.py
  src/domain/prompt_v3/profile_slot.py
  src/domain/prompt_v3/section.py
  src/domain/prompt_v3/slot.py
  src/domain/prompt_v3/token.py
  src/ports/prompt_v3/
  src/adapters/prompt_v3/
  src/services/prompt_v3/
  src/services/prompt_builder.py
  src/ports/prompt_builder_port.py
  src/composition/prompt_assembly_setup.py
  src/domain/prompt.py
  firestore_utils/
  prompts_snapshot/
-->
# Prompt Design System v3

## Purpose

This block turns stored fragments into the system instruction of every agent. A prompt is never written in code: it is
assembled at request time from **tokens** (named, validated fragments of Groovy-style text), arranged by a **blueprint**
(the outer class and the order of its sections) and selected by an **agent profile** (which tokens an agent uses and in
what order). The owner's per-account and per-user choices (humor, voice, language) are **overrides** that swap one
token for another of the same kind. Agents never build prompt text; they call `PromptBuilderPort.build_for_agent`.

The assembly has two phases with different lifetimes: a **static template** (tokens, blueprint, overrides; cached
in-process) and **runtime context** (biographical facts, directives, reminders, time; appended on every request). A
cache boundary marker in the final string separates what a provider may cache from what changes per request.

What it is not: it does not hold prompt wording (the text lives in tokens, see below), it does not decide which agent
runs, and it does not write memory. Why prompts are written as a Groovy-style class and how to write a token:
[`groovy_prompt_pattern.md`](../../08_concepts/groovy_prompt_pattern.md).

The name keeps `v3` because the collections do; the assembly algorithm is the "class-collection" model (v4) that replaced
placeholder substitution (`PROMPT_BUILDER_V4_RFC.md`).

## Owned data

Four Firestore collections, names from `src/config/environment.py::EnvironmentConfig` (`domain_prompt_tokens_collection`
plus the `_system` / `_user` suffixes, `domain_prompt_blueprints_collection`, `domain_prompt_profiles_collection`,
`domain_prompt_overrides_collection`), wired once in `src/composition/prompt_assembly_setup.py::build_prompt_assembly_service`
(shared by the main service and the drive indexer, so both assemble from the same stores).

- **Token** (`..._tokens_v3_system`, `..._tokens_v3_user`; id = `token_id`). `class` is the section the token renders into,
  `category` is the semantic group and the **override key**, `content` is bare Groovy with no section wrapper (the
  wrapper is generated). The system collection holds engineering-owned tokens; the user collection holds the choices an
  owner can switch to (humor, archetype, voice, vibe, response style, motto). `FirestoreTokenRepository.get` looks in
  system first, then user. Domain type: `src/domain/prompt_v3/token.py::Token`.
- **Blueprint** (`..._blueprints_v3`; id = `blueprint_id`): `outer_class`, ordered `class_order`, optional
  `tail_categories`. `Blueprint.validate` rejects an empty outer class, an empty order and duplicate section names.
- **Agent profile** (`..._profiles_v3`; id = `agent_id`): `blueprint_id` and a `tokens` map
  `token_id -> {order, non_overridable}`. The blueprint an agent uses is read from its profile, never mapped in code.
  Many profiles share a blueprint (`universal_agent_v1` is used by Smart, Quick and others); a profile is per agent or
  per mode. `order` is the position inside the token's section.
- **Override** (`..._overrides_v3`, a separate collection from the profiles; id `{OWNER_TYPE}_{owner_id}` with the type
  upper-cased, e.g. an account or user prefix): the same `tokens` map, written by `FirestoreAgentProfileRepository.set_override_tokens`.

Who writes: engineering uploads tokens, blueprints and profiles by hand; `prompts_snapshot/` is a git mirror of those
three (overrides are excluded, they are personal data). Firestore is the source of truth, the mirror makes drift and
edits reviewable; the pull, the drift check and the human-only apply gate are described in
[`prompts_snapshot/README.md`](../../../prompts_snapshot/README.md). Scripts under `firestore_utils/` write the live
store and are run by the owner only. Who reads: `PromptAssemblyService` through the three repository ports in
`src/ports/prompt_v3/`. The snapshot can lag or differ from the live store; nothing in the application reads it.

## Flows

### A prompt for one agent turn

1. An agent calls `src/services/prompt_builder.py::PromptBuilder.build_for_agent` (the port is
   `src/ports/prompt_builder_port.py::PromptBuilderPort`; `UserPromptBuilder` only binds a user id and config).
2. The builder loads the account's cached biographical facts (skipped with `include_biographical=False`; a missing
   `account_id` loads nothing, there is no fallback to the user id) and splits the list into three channels in one
   place: standing directives (`fact_lifecycle.is_directive`, dropped with `include_directives=False`), query-specific
   facts (tagged `semantic_lens` by `merge_enriched_context_with_biographical`) and the rest, the static biography.
3. `PromptAssemblyService.assemble` looks up the static template by cache key, assembling it on a miss
   (below), then injects runtime context (below) and returns one string.

### Static template

`prompt_assembly_service.py::PromptAssemblyService._assemble_static_template`:

1. Read the agent profile (sequential: it names the blueprint).
2. Read the blueprint and the ACCOUNT and USER override documents in parallel.
3. Fetch every referenced token in parallel (`asyncio.gather` with `return_exceptions=True`).
4. Apply account overrides, then user overrides (`_apply_overrides`): an override token replaces the active token with the
   same `class` and `category`, unless that profile entry is `non_overridable`. The override's own `order` is used.
5. Group the active tokens by `class`, sort by `order`, render each section in `blueprint.class_order` order (empty
   sections are skipped), wrap in `class {outer_class} { ... }`, normalise whitespace.
6. Two special parts are held apart from the sections. The `provenance` class (`src/domain/system_fingerprint.py::PROVENANCE_CLASS`)
   is a top-level head before the outer class. Tokens whose category is listed in `Blueprint.tail_categories` are rendered
   as trailing top-level blocks and stored after `_TAIL_MARKER` inside the same cached string, so a template and its
   tail are one value; `assemble` splits them again.

### Runtime context

`PromptAssemblyService._inject_runtime_context`, on every request, in this order:

1. Static facts, directives, the query-specific context and any conversation history are formatted
   (`BiographicalFactsFormatter`, `ContextFormatter`) and validated through `SecurityPort` in the UNTRUSTED zone.
2. `knowledge_base {}` (location, biography, history; only the non-empty parts, never an empty wrapper) and
   `extra_static_blocks`. With `kb_preamble=True` they go **before** the blueprint class (Smart, Quick, Router, Tutor:
   context first, instructions last, for recency) and the provenance head is lifted to stay before them; with the
   default `False` they follow the class (consolidation and the batch agents, whose payload is the task).
3. The skills catalog, then `standing_directives {}` as the last static block.
4. `PROMPT_CACHE_BOUNDARY`.
5. The dynamic part: `active_reminders {}` (orchestrator-supplied notes, trusted, not validated), `current_date_time {}`
   only with `include_datetime=True` (by default the time is stamped into user messages instead, so the cached prefix
   stays stable), `query_specific_context`, the `tail_categories` blocks, and `session_key {}` always last
   (`SYSTEM_TURN_FINGERPRINT_RFC.md` §4.2; an invalid key raises).

Conversation history for chat agents travels as `messages`, not in the prompt. Consolidation passes its batch as
`conversation_history`, which lands in the static `knowledge_base` and is cached across the run.

### Cache boundary at the provider

`src/domain/llm.py::PROMPT_CACHE_BOUNDARY` is the one marker. `ClaudeAdapter` (when `cache_config.enabled`) splits the
instruction there into a static block with `cache_control: ephemeral` and a dynamic block; with no marker it caches the
whole instruction, and it never sends an empty block (Anthropic answers 400). `GeminiAdapter`, `GrokAdapter` and
`OpenAIAdapter` strip the marker so the literal never reaches the model; Grok and OpenAI use the static part as their
cache-key seed. Details: `HEXAGONAL_PROMPT_CACHING_RFC.md`.

### Language

Language is a standard override, not a code path. Smart and Quick assign `LANG_MIRROR` (class `policies`, category
`output_language`, not `non_overridable`); `src/services/language_preference_service.py::LanguagePreferenceService.set_preference`
writes a `LANG_FIXED_<code>` token into the USER override (clearing the other `LANG_*` ids), or clears them for mirror
mode, then clears the assembly cache. User-facing language policy: [`localization_system`](../localization_system/README.md).

### Cache lifetime

The static template is cached in a per-process dict with a TTL (the `cache_ttl` default of `PromptAssemblyService`) under the key
`_build_cache_key` builds from agent type, account id and user id (the builder substitutes the anonymous ids for
missing ones). `preload_cache` warms the agent types the factory lists (`_preload_agent_types`) when a user's agents are created. `invalidate_cache` clears all;
`invalidate_assembled` drops one key (`PromptBuilder.invalidate_agent_prompt`). Callers: `$admin_cache_reset`
(`src/agents/core/router_agent.py`, `src/handlers/conversation_handler.py`), the language change, and agents that
drop a template that came out with required sections missing (tokens or profile not yet uploaded) so it is rebuilt
after the upload (`email_card_agent.py`, `fact_profiler_agent.py`, `src/infrastructure/drive_model_call.py`).

## Invariants and traps

- **A token that cannot be loaded is silently left out.** Step 3 captures exceptions: a token that is missing or whose
  document lacks `token_id` becomes a warning log and the section renders without it. A profile that lists a typo gives a
  prompt missing a rule, with no error. Check the warning `Agent token ... not found` after an upload.
  `FirestoreTokenRepository.get` also runs `Token.create` on every read, but in the TRUSTED zone, which the regex
  adapter returns unscanned (`src/adapters/security/regex_adapter.py`), so a read does not reject a token for risk.
- **A missing profile degrades, a missing blueprint raises.** `get_agent_profile` returns an empty profile with
  `blueprint_id = {agent_id}_agent_v1` when the document is absent; `FirestoreBlueprintRepository.get` raises `KeyError`
  if that blueprint is absent, and a profile with no tokens assembles to an empty outer class. The no-fallback-prompt
  rule applies to agents: an agent whose `build_for_agent` fails returns a failure, it does not use inline text.
- **An override only replaces, and only the first match.** An override token with no active token of the same
  `class` and `category` is ignored; it never adds a section. A `non_overridable` match blocks it. The `provenance` class
  is never overridable (`_apply_overrides`).
- **`class + category` is the identity of a choice.** A new user-selectable token needs the category its default has, in
  the same class, or the switch silently does nothing.
- **Two `OwnerType` enums.** Assembly uses `src/domain/prompt_v3/slot.py::OwnerType` (three members; override ids
  upper-case the value). `src/domain/prompt.py::OwnerType` is a different, four-member enum that includes `SYSTEM`.
  Override ids come out the same from either; the wrong one matters only where code compares against `OwnerType.AGENT`
  (`FirestoreAgentProfileRepository.delete_profile`).
- **`set_override_tokens` is a read then a write, not a transaction.** Two concurrent changes of one user's overrides can
  lose one. The language path accepts this.
- **The cache is per process and expires on a TTL.** An uploaded token, blueprint or profile reaches a warm instance only after
  the TTL, a restart or an invalidation, and `$admin_cache_reset` clears only the instance that handled it. A template
  assembled while an invalidation ran is not saved (`_cache_epoch`); this version counter, not a lock, is the guard
  (`src/services/CLAUDE.md`).
- **The cache key holds no runtime data.** Anything user- or request-specific belongs in runtime context; putting it in a
  token makes it stale until the TTL. Biography is above the boundary on purpose (changes only when consolidation refreshes
  its cache); query-specific facts are below it because they change with every message.
- **Never put a date or other per-request text above the boundary.** It changes the cached prefix on every call
  (`src/agents/CLAUDE.md`). A large static payload goes through `extra_static_blocks`, not the user message.
- **Untrusted text is validated at injection.** Biography, directives, query-specific context and history pass
  `SecurityPort` in the UNTRUSTED zone; reminders and `extra_static_blocks` are orchestrator-built and are not.
- **`provenance` is a snapshot fact, not a live one.** `POLICY_SYSTEM_PROVENANCE` is assigned in `prompts_snapshot/profiles/`
  for Smart and Quick (`non_overridable`); the live store does **not** hold the token, the class or the assignments (not
  uploaded, by owner decision, `SYSTEM_TURN_FINGERPRINT_RFC.md` §7), so the live prompt has no provenance head. A blueprint without the class, or a profile without the token, renders no head.
- **Prompt wording lives in Firestore, not in this repo's code.** Tokens ship by upload, not by deploy
  (root `CLAUDE.md`). A change to prompt text is a token edit in `prompts_snapshot/tokens/` plus an owner-run upload;
  this document never copies prompt text.
- **Open question: the blueprint layer.** The owner has said the blueprint approach may be dropped, since every agent
  has its own profile and sharing a blueprint is the only benefit. Until it is decided, keep using existing blueprints
  for new agents and do not build new mechanics tied to blueprint ids; profile changes are safe.
- **`tail_categories` exists for recency.** The voice blueprint (`lelik_agent_v1`) uses it to put the output-language rule
  at the very end: facts and chat history follow the middle of the instructions, and on providers that own the turns no
  per-turn anchor repeats that rule, so it is placed where it is read last.
- **Tests:** `tests/unit/services/prompt_v3/test_prompt_assembly_service.py` (assembly, overrides, cache) and the
  store-over-fake resolution test `test_prompt_resolution_over_stores.py`; the real-Firestore parity case is
  `tests/cloud/test_prompt_4level_cloud.py`.

## Links

- `docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md` (the class-collection model), `PROMPT_TOKEN_SNAPSHOT_RFC.md` (the mirror),
  `HEXAGONAL_PROMPT_CACHING_RFC.md` (the boundary), `SYSTEM_TURN_FINGERPRINT_RFC.md` (provenance head, session key).
- [`groovy_prompt_pattern.md`](../../08_concepts/groovy_prompt_pattern.md), [`security_validation`](../security_validation/README.md),
  [`localization_system`](../localization_system/README.md), [`DATABASE_SCHEMA.md`](../../08_concepts/DATABASE_SCHEMA.md).

## Where to start reading

- `src/services/prompt_builder.py::PromptBuilder.build_for_agent` (the fact channels and the call into assembly)
- `src/services/prompt_v3/prompt_assembly_service.py::PromptAssemblyService._assemble_static_template`
- `src/services/prompt_v3/prompt_assembly_service.py::PromptAssemblyService._apply_overrides`
- `src/services/prompt_v3/prompt_assembly_service.py::PromptAssemblyService._inject_runtime_context`
- `src/adapters/prompt_v3/firestore_agent_profile_repository.py::FirestoreAgentProfileRepository.get_override_tokens`
- `src/composition/prompt_assembly_setup.py::build_prompt_assembly_service`
- `src/adapters/claude_adapter.py::ClaudeAdapter` (the boundary split)
