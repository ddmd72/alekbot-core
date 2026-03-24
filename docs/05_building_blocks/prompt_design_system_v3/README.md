# Prompt Design System — Class-Collection Assembly Model

## How To Use This Document

**For AI Agents:** Read before modifying prompt assembly logic, token library, agent profiles,
or blueprint structure.

**For Developers:** Read when adding tokens, changing agent behavior, debugging wrong prompts,
or understanding the assembly pipeline.

**Update this document when:**
- Token schema or collection structure changes
- Blueprint fields or semantics change
- Profile/override document ID scheme changes
- Assembly algorithm changes (step order, override rules, section rendering)
- New Firestore collections or renaming
- Cache logic or TTL changes
- New agent types added to blueprint mapping

**Cross-references:**
- Assembly mechanics: [../../08_concepts/prompt_assembly_guide.md](../../08_concepts/prompt_assembly_guide.md)
- Implementation RFC: [../../10_rfcs/PROMPT_BUILDER_V4_RFC.md](../../10_rfcs/PROMPT_BUILDER_V4_RFC.md)
- Groovy DSL conventions: [../../08_concepts/groovy_prompt_pattern.md](../../08_concepts/groovy_prompt_pattern.md)
- Security validation: [../security_validation/README.md](../security_validation/README.md)
- Language policy tokens (LANG_*): [../localization_system/README.md](../localization_system/README.md)

---

## 1. Overview

The **Prompt Design System** assembles LLM system instructions from pre-approved, validated
fragments (tokens) rather than free-form text. Prompts are never hardcoded — they are built
at runtime by combining tokens according to a blueprint, with optional per-account and
per-user customizations.

**Core principle:** Users never inject raw text into system instructions. They select from a
whitelisted token library. Tokens are validated at creation time. Blueprint structure is
controlled by engineering. Agents never construct their own prompts — they call
`PromptBuilderPort.build_for_agent()`.

### Assembly Model (v4 — Class-Collection)

The assembly model is called **class-collection**: each token belongs to a Groovy **class**
(a named section), and the blueprint declares the ordered list of classes. Assembly groups
tokens by class, sorts within each class, renders sections, and wraps them in an outer Groovy
class declaration.

This replaced the v3 placeholder-substitution model (described in
[PROMPT_BUILDER_V4_RFC.md Section 1](../../10_rfcs/PROMPT_BUILDER_V4_RFC.md)).
The Firestore collections retain the `_v3` suffix — the "v4" refers to the assembly
algorithm version, not the collection schema generation.

---

## 2. Data Model

Three types of Firestore documents define a prompt:

```
Blueprint ──────────────────────────────────── defines structure
    outer_class: "Alek extends Agent"
    class_order: [properties, cognitive_process, ...]

Token ───────────────────────────────────────── defines content fragment
    token_id: "HUMOR_PRESET_RANEVSKAYA"
    class:    "properties"          ← which Groovy section it renders into
    category: "humor_engine"        ← semantic group (dedup key for overrides)
    content:  "humor_engine { ... }" ← bare Groovy block(s), no outer section wrapper

AgentProfile ────────────────────────────────── defines which tokens an agent uses
    blueprint_id: "universal_agent_v1"  ← which blueprint to assemble against
    agent_id:     "quick"
    tokens: {
      "HUMOR_PRESET_RANEVSKAYA": {order: 60},
      "COGNITIVE_PROCESS_QUICK": {order: 10, non_overridable: true},
      ...
    }
```

### 2.1 Blueprint (`development_domain_prompt_blueprints_v3`)

Document ID: `{blueprint_id}` (e.g., `universal_agent_v1`)

```json
{
  "blueprint_id": "universal_agent_v1",
  "outer_class": "Alek extends Agent",
  "class_order": [
    "properties",
    "cognitive_process",
    "policies",
    "protocols",
    "few_shot_examples",
    "output_format",
    "final_directives"
  ]
}
```

**Fields:**
- `outer_class` — Groovy class declaration: `class {outer_class} { ... }`
- `class_order` — ordered list of Groovy section names. Sections are rendered in this order.
  Sections with no assigned tokens are silently skipped.

**No template string. No classes dict. No default tokens.** All of that has moved to the
token and profile.

### 2.2 Token (`development_domain_prompt_tokens_v3_system` / `_user`)

Document ID: `{token_id}` (e.g., `HUMOR_PRESET_RANEVSKAYA`)

```json
{
  "token_id": "HUMOR_PRESET_RANEVSKAYA",
  "class": "properties",
  "category": "humor_engine",
  "content": "humor_engine {\n    persona: \"Faina Ranevskaya\"\n    ...\n}",
  "metadata": {
    "version": "2.0",
    "description": "Ranevskaya persona with dark wit and aphoristic style"
  }
}
```

**Fields:**
- `class` — Groovy section name this token renders into (`properties`, `cognitive_process`,
  `policies`, `protocols`, `few_shot_examples`, `output_format`, `final_directives`).
  The assembly service uses this to group tokens into sections.
- `category` — semantic group used as the **deduplication key** for overrides.
  When an account or user provides a token with the same `class + category` as an agent
  token, the override replaces the agent token. Examples: `humor_engine`, `voice`,
  `cognitive_process`, `archetype`, `vibe`, `policy`, `protocol`, `output_format`.
- `content` — bare Groovy block(s). **No outer section wrapper.** The section wrapper
  (`properties { ... }`) is generated by the assembly service. The token only provides
  what goes *inside* the section.

**Two token collections:**
- `_system` — system-controlled tokens (cognitive_process, policies, protocols, directives,
  output_format, few_shot_examples, behavior_guide). Only engineering can modify these.
- `_user` — user-customizable tokens (humor_preset, archetype, voice, vibe, response_style,
  motto). Account/user overrides point to tokens from this collection.

Both collections are queried via `FirestoreTokenRepository` which tries `_system` first,
then `_user`. Code: `src/adapters/prompt_v3/firestore_token_repository.py`.

### 2.3 Agent Profile (`development_domain_prompt_profiles_v3`)

Document ID: `{agent_id}` (e.g., `quick`, `router`, `consolidation`)

```json
{
  "blueprint_id": "universal_agent_v1",
  "agent_id": "quick",
  "tokens": {
    "COGNITIVE_PROCESS_QUICK":        {"order": 10, "non_overridable": true},
    "ARCHETYPE_INTELLECTUAL_SNIPER":  {"order": 20, "non_overridable": true},
    "VIBE_BATTLE_WEARY":              {"order": 30, "non_overridable": true},
    "VOICE_APHORISTIC":               {"order": 40},
    "BEHAVIOR_GUIDE_RANEVSKAYA_MODE": {"order": 50},
    "HUMOR_PRESET_RANEVSKAYA":        {"order": 60},
    "RESPONSE_CONCISE":               {"order": 70},
    "MOTTO_DEFAULT":                  {"order": 80},
    "FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY": {"order": 90, "non_overridable": true},
    "OUTPUT_FORMAT_JSON":             {"order": 100},
    "PROTOCOL_SEARCH_MEMORY":         {"order": 200},
    "PROTOCOL_QUICK_AGENT_SELECTION": {"order": 210, "non_overridable": true},
    "POLICY_OUTPUT_LANGUAGE":         {"order": 300, "non_overridable": true},
    "POLICY_PRIVACY":                 {"order": 310},
    "POLICY_NO_OPEN_LOOPS":           {"order": 320},
    "POLICY_ANTI_GUARDIAN":           {"order": 330},
    "POLICY_WITTY_ACCENTUATION":      {"order": 340},
    "POLICY_ALIGN_WITH_ANCHORS":      {"order": 350},
    "DIRECTIVE_SLACK_FORMATTING":     {"order": 400},
    "DIRECTIVE_BREVITY":              {"order": 410}
  }
}
```

**Key design decisions:**
- Document ID = `agent_id` only. The `blueprint_id` field **inside** the document is the
  source of truth for which blueprint to use. The assembly service reads it from the profile —
  no hardcoded mapping in application code.
- `tokens` map (not an array). Key = `token_id`, value = `{order, non_overridable}`.
  Map constraint prevents duplicate assignments.
- `order` — integer, step 10 by convention. Controls rendering order within the section.
  Lower order = rendered earlier.
- `non_overridable: true` — account and user overrides cannot replace this token.
  Used for core cognitive process, key protocols, and structural tokens.

### 2.4 Override Profiles (Account/User)

Document ID: `{OWNER_TYPE}_{owner_id}` (e.g., `ACCOUNT_acc_123`, `USER_user_456`)

```json
{
  "owner_type": "ACCOUNT",
  "owner_id": "acc_123",
  "tokens": {
    "VOICE_FORMAL": {"order": 40}
  }
}
```

Override tokens are matched to agent tokens by `class + category`. If the agent profile has
a token with the same `class` + `category` and `non_overridable != true`, the override token
replaces it. Overrides **cannot add new classes** — they can only replace existing ones.

**Domain type:** `AgentProfile` in `src/domain/prompt_v3/agent_profile.py` holds the resolved
profile returned by `get_agent_profile()`. Override tokens are returned as
`Dict[str, ProfileToken]` from `get_override_tokens()`.

---

## 3. Agent-to-Blueprint Mapping

Each agent type maps to exactly one blueprint. The mapping lives in Firestore (in the agent
profile document), not in application code.

| Agent type | Blueprint ID | Outer class | Notes |
|------------|-------------|-------------|-------|
| `quick` | `universal_agent_v1` | `Alek extends Agent` | User-facing; account/user overrides allowed |
| `smart` | `universal_agent_v1` | `Alek extends Agent` | User-facing; account/user overrides allowed |
| `router` | `router_agent_v1` | `RouterAgent extends Agent` | Internal; all tokens non_overridable |
| `websearch` | `websearch_agent_v1` | `WebSearchAgent extends Agent` | Internal |
| `websearch_light` | `websearch_light_agent_v1` | `WebSearchLightAgent extends Agent` | Internal |
| `consolidation` | `consolidation_agent_v1` | `ConsolidationAgent extends Agent` | Internal |
| `memorysearch` | `memorysearch_agent_v1` | `MemorySearchAgent extends Agent` | Internal |

**quick and smart** share `universal_agent_v1` — same outer class, same section order, different
token assignments (e.g., `COGNITIVE_PROCESS_QUICK` vs `COGNITIVE_PROCESS_SMART`). Account/user
overrides are meaningful here.

**Internal agents** (router, websearch, websearch_light, consolidation, memorysearch) each have
their own blueprint with domain-specific section names (e.g., `taxonomy`, `execution`,
`anti_patterns`). All their tokens are `non_overridable: true`. User overrides are irrelevant.

---

## 4. Assembly Algorithm

Implemented in `src/services/prompt_v3/prompt_assembly_service.py`,
method `_assemble_static_template()`.

```
Input: agent_type, account_id?, user_id?

STEP 1 — Agent Profile fetch (sequential, needed to get blueprint_id)
  profile = get_agent_profile(agent_type)
  blueprint_id = profile.blueprint_id
  agent_tokens = profile.tokens           # Dict[str, ProfileToken]

STEP 2 — Blueprint + overrides in parallel (asyncio.gather)
  blueprint = blueprint_repo.get(blueprint_id)
  account_overrides = get_override_tokens(ACCOUNT, account_id)   # if account_id
  user_overrides    = get_override_tokens(USER,    user_id)       # if user_id

STEP 3 — Apply account overrides
  For each token in account_overrides:
    Find agent token with matching (class + category)
    If found AND agent token.non_overridable == False:
      Replace agent token with override token in working set

STEP 4 — Apply user overrides
  Same logic as step 3, applied on top of the result from step 3.
  User overrides take priority over account overrides.

STEP 5 — Fetch all active token documents in parallel
  Collect all token_ids from working set → asyncio.gather(token_repo.get(id) for each)

STEP 6 — Group by class
  tokens_by_class: Dict[str, List[Tuple[ProfileToken, Token]]]
  Group each fetched token by its Token.class_ field.

STEP 7 — Render each section (iterating blueprint.class_order)
  For each class_name in blueprint.class_order:
    If no tokens for this class → skip (section omitted entirely)
    Sort tokens by ProfileToken.order (ascending)
    Join token content with "\n\n"
    Render: "    {class_name} {\n\n{indented_content}\n\n    }"

STEP 8 — Wrap in outer class
  "class {outer_class} {\n\n{all_sections}\n\n}"

Output: static_template (str, ~3-8k tokens depending on agent)
```

### Override example

Agent quick has: `HUMOR_PRESET_RANEVSKAYA` (class=`properties`, category=`humor_engine`)

Account override provides: `HUMOR_PRESET_FAMILY_FRIENDLY` (class=`properties`, category=`humor_engine`)

Result: `HUMOR_PRESET_RANEVSKAYA` is replaced by `HUMOR_PRESET_FAMILY_FRIENDLY` in the
`properties` section. The `order` value from the override's `ProfileToken` is used for
positioning.

If `HUMOR_PRESET_RANEVSKAYA` were marked `non_overridable: true`, the override would be
silently ignored.

---

## 5. Two-Phase Assembly

The assembly service splits prompt construction into two phases with different caching
characteristics.

### Phase 1 — Static Template (Cached 24h)

Steps 1-8 above. Result is stored in an in-memory dict with 24h TTL.

- **Cache key:** `prompt:{agent_type}:acc:{account_id}:usr:{user_id}`
- **Cold start:** ~110ms (Firestore reads + asyncio.gather)
- **Cache hit:** ~5ms
- **Invalidation:** `$admin_cache_reset` via Slack, or `assembly_service.invalidate_cache()`

Phase 1 is bypassed for each unique `(agent_type, account_id, user_id)` triple.
For internal agents (router, consolidation, memorysearch) with no user/account context,
the cache key resolves to a constant — effectively a permanent warm cache.

### Phase 2 — Runtime Injection (Every Request)

Appends dynamic context to the cached static template. Never cached.

1. Split `biographical_facts` by `semantic_lens` tag → static bio (before boundary) vs
   query-specific facts (after boundary)
2. Format + validate static bio via `SecurityPort` (UNTRUSTED zone)
3. Append `knowledge_base {}` block (bio + optionally conversation_history) — only if non-empty
4. Append `<!-- CACHE_BOUNDARY -->`
5. Append `active_reminders {}` (only if active reminders present) — user's self-reminders fetched by RouterAgent via `AgentNotePort.list_active_notes()` at turn start; TRUSTED zone, no `SecurityPort` validation
6. Append `current_date_time {}` (always)
7. Append `query_specific_context` (only if Q-S facts present)

The `ClaudeAdapter` splits at `<!-- CACHE_BOUNDARY -->` and applies
`cache_control: ephemeral` to the static prefix — Anthropic caches it server-side for ~5 min.

See [prompt_assembly_guide.md Section 7](../../08_concepts/prompt_assembly_guide.md#7-cache-boundary-and-anthropic-prompt-caching)
for full cache boundary mechanics.

---

## 6. Assembled Prompt Structure

```
[STATIC PREFIX — cached 24h in-memory + ~5 min by Anthropic]

class Alek extends Agent {

    properties {

        archetype { ... }         ← ARCHETYPE token (order 20)
        vibe { ... }              ← VIBE token (order 30)
        voice { ... }             ← VOICE token (order 40)
        behavior_guide { ... }    ← BEHAVIOR_GUIDE token (order 50)
        humor_engine { ... }      ← HUMOR_PRESET token (order 60, user-overridable)
        response { ... }          ← RESPONSE token (order 70)
        motto { ... }             ← MOTTO token (order 80)
    }

    cognitive_process {           ← COGNITIVE_PROCESS token (order 10)

        ...
    }

    policies {

        policy_output_language { ... }   ← 6 POLICY tokens (orders 300-350)
        policy_privacy { ... }
        ...
    }

    protocols {

        protocol_search_memory { ... }   ← PROTOCOL tokens (orders 200-210)
        protocol_quick_agent_selection { ... }
    }

    few_shot_examples {                  ← FEW_SHOT_EXAMPLES token (order 90)

        ...
    }

    output_format {                      ← OUTPUT_FORMAT token (order 100)

        ...
    }

    final_directives {

        directive_slack_formatting { ... }   ← DIRECTIVE tokens (orders 400-410)
        directive_brevity { ... }
    }
}

knowledge_base {                         ← Phase 2: static bio facts (before boundary)
  biographical_context: '''
    **Biographical**
    - Born in Kyiv
    **Work**
    - Software engineer at ...
  '''
}

[DYNAMIC SUFFIX — sent fresh every request, NOT cached by Anthropic]

<!-- CACHE_BOUNDARY -->

active_reminders {          ← only when active reminders exist
    // Reminders you scheduled for yourself. Not visible to the user. Snapshot from turn start — trust tool results for changes made this turn.
    // IDs are Unix timestamps (ms) — use to gauge reminder age relative to current_date_time.
    - "Send Valencia morning news briefing" (fires: Mar 23 08:00 UTC) [id: 1742700000000]
    - "Weekly project check-in" (fires: Mar 28 09:00 UTC) [id: 1742800000000]
}

current_date_time {
    2026-02-27 15:42 Thursday (CET)
}

query_specific_context: '''          ← Phase 2: only when router found semantic facts
    **Query-Specific Context:**
    - User mentioned travel plans last week
'''
```

**Key rules:**
- `knowledge_base` block only appended when content is non-empty (no empty wrappers)
- Sections with no tokens are silently omitted
- Token order within a section is controlled by `ProfileToken.order` (ascending)
- Sections are ordered by `blueprint.class_order`

---

## 7. Code References

### Domain layer (`src/domain/prompt_v3/`)

| File | Type | Key fields |
|------|------|-----------|
| `blueprint.py` | `Blueprint` | `id`, `outer_class`, `class_order: List[str]` |
| `token.py` | `Token` | `id`, `category`, `class_: TokenClass`, `content` |
| `profile_slot.py` | `ProfileToken` | `token_id`, `order: int`, `non_overridable: bool` |
| `agent_profile.py` | `AgentProfile` | `blueprint_id: str`, `tokens: Dict[str, ProfileToken]` |
| `slot.py` | `OwnerType` | Enum: `AGENT`, `ACCOUNT`, `USER` |

### Ports (`src/ports/prompt_v3/`)

| File | Interface | Key methods |
|------|-----------|------------|
| `blueprint_repository.py` | `BlueprintRepository` | `get(blueprint_id)` |
| `token_repository.py` | `TokenRepository` | `get(token_id)`, `list_by_class()` |
| `agent_profile_repository.py` | `AgentProfileRepository` | `get_agent_profile(agent_id) -> AgentProfile`, `get_override_tokens(owner_type, owner_id) -> Dict[str, ProfileToken]`, `set_override_tokens(owner_type, owner_id, tokens, clear_ids)` |

### Adapters (`src/adapters/prompt_v3/`)

| File | Implements | Notes |
|------|-----------|-------|
| `firestore_blueprint_repository.py` | `BlueprintRepository` | Reads `outer_class` + `class_order` |
| `firestore_token_repository.py` | `TokenRepository` | Dual-collection lookup: system first, then user |
| `firestore_agent_profile_repository.py` | `AgentProfileRepository` | Profile doc ID = `agent_id`; override doc ID = `{OWNER_TYPE}_{owner_id}` |

### Assembly service (`src/services/prompt_v3/`)

| File | Purpose |
|------|---------|
| `prompt_assembly_service.py` | Two-phase assembly, 24h cache, override resolution |
| `biographical_formatter.py` | Domain-grouped Markdown formatting of biographical facts |
| `context_formatter.py` | Conversation history formatting |

### Entry point

`src/services/prompt_builder.py` — `UserPromptBuilder` (merged into `prompt_builder.py` in 2026-03-08 hexagonal audit) implements `PromptBuilderPort`.
All agents call `await prompt_builder.build_for_agent(agent_type, user_id, ...)`.
Agents never interact with `PromptAssemblyService` directly.

### Firestore collections

| Collection | Contains | Document ID scheme |
|-----------|---------|-------------------|
| `development_domain_prompt_blueprints_v3` | Blueprint documents | `{blueprint_id}` |
| `development_domain_prompt_tokens_v3_system` | System tokens | `{token_id}` |
| `development_domain_prompt_tokens_v3_user` | User tokens | `{token_id}` |
| `development_domain_prompt_profiles_v3` | Agent profiles + overrides | `{agent_id}` (profiles) / `{OWNER_TYPE}_{owner_id}` (overrides) |

---

## 8. Security Model

Three validation layers:

1. **Token creation** — every token's `content` must pass `SecurityPort.validate()` at creation
   time before being written to Firestore. Tokens with UNSAFE content are rejected.

2. **Override assignment** — overrides can only replace tokens in classes the agent already has.
   `non_overridable: true` blocks replacement entirely. Users cannot inject new Groovy sections.

3. **Runtime context** — biographical facts and conversation history are treated as `UNTRUSTED`
   zone and validated via `SecurityPort` before being appended to the prompt.

These three layers together ensure no raw text ever enters the system instruction without
either being pre-approved (tokens) or sanitized (runtime context).

---

## 9. What Does NOT Change Per Request

These components are stable once deployed and do not change without a code+Firestore update:

| Component | Stability | Lives in |
|-----------|-----------|---------|
| Blueprint structure | Immutable (until next migration) | Firestore |
| Token content | Immutable (tokens are versioned, not edited) | Firestore |
| Agent profile token assignments | Changes only on explicit profile update | Firestore |
| Assembly algorithm | Code changes require deployment | Python |
| Override logic (class+category match) | Code changes require deployment | Python |

These change per request:

| Component | Changes | Source |
|-----------|---------|--------|
| Biographical facts (static) | Whenever consolidation runs (~every 100 messages) | Firestore (BiographicalContextService) |
| Query-specific context | Every request (based on user query) | Router enrichment |
| Current datetime | Every request | System clock |
| Account/user overrides | Whenever user changes preferences | Firestore |

---

## 10. Status

**Status:** ✅ Production Ready

**Assembly model:** v4 class-collection (implemented 2026-02-27)

**Previous model:** v3 placeholder-substitution (archived in
[PROMPT_BUILDER_V4_RFC.md Section 1](../../10_rfcs/PROMPT_BUILDER_V4_RFC.md))

**Test coverage:**
- `tests/unit/services/prompt_v3/test_prompt_assembly_service.py` — unit (assembly logic, overrides, cache)
- `tests/unit/ports/test_prompt_v3_contracts.py` — port contracts
- `tests/unit/adapters/prompt_v3/test_firestore_repositories.py` — adapter unit tests
- `tests/integration/test_prompt_4level_assembly.py` — integration (real Firestore)

**Last Updated:** 2026-03-24

---

## 11. Language Policy Tokens (LANG_* family)

Language control for bot responses is implemented as a standard override, not as a special
code path. Five tokens in `development_domain_prompt_tokens_v3_system` cover all language modes:

| Token ID | Behavior | Groovy rule name |
|----------|---------|-----------------|
| `LANG_MIRROR` | Mirrors user input language (default) | `Output_Language_Mirror` |
| `LANG_FIXED_UK` | Always Ukrainian | `Output_Language_Fixed_UK` |
| `LANG_FIXED_EN` | Always English | `Output_Language_Fixed_EN` |
| `LANG_FIXED_FR` | Always French | `Output_Language_Fixed_FR` |
| `LANG_FIXED_ES` | Always Spanish | `Output_Language_Fixed_ES` |

**Schema:** `class: policies`, `category: output_language`, `non_overridable: false`.

`non_overridable: false` is the key design decision: it allows USER-level overrides to replace
the default `LANG_MIRROR` with a fixed-language token via the standard override mechanism
(class + category match). No special assembly code is needed.

### Default in agent profiles

Quick and Smart profiles have `LANG_MIRROR` at `order: 300`:

```json
"LANG_MIRROR": {"order": 300, "non_overridable": false}
```

This replaced `POLICY_OUTPUT_LANGUAGE` (`non_overridable: true`) — the old token was
hardcoded and could not be user-overridden.

### User override — fixed language

`LanguagePreferenceService.set_preference()` calls `set_override_tokens()`:

```python
set_override_tokens(
    OwnerType.USER, user_id,
    tokens={LANG_FIXED_EN: ProfileToken(token_id=..., order=70)},
    clear_ids={LANG_MIRROR, LANG_FIXED_UK, LANG_FIXED_FR, LANG_FIXED_ES},
)
```

`set_override_tokens()` performs an atomic read-modify-write on the override document
(`USER_{user_id}` in `domain_prompt_overrides_v3`):
1. Read existing override tokens
2. Remove all IDs in `clear_ids` (other LANG_* entries)
3. Write new token(s)
4. `doc_ref.set(...)` — awaited (AsyncClient)

Result: the override doc contains exactly one LANG_* token. Assembly replaces `LANG_MIRROR`
from the agent profile with `LANG_FIXED_EN` from the user override.

### User override — mirror mode

```python
set_override_tokens(OwnerType.USER, user_id, tokens={}, clear_ids=ALL_LANG_IDS)
```

Clears all LANG_* entries from the override doc. Assembly falls back to `LANG_MIRROR` from
the agent profile. No override doc deletion needed — an empty `tokens` map is sufficient.

### Upload files

```
firestore_utils/uploads/LANG_MIRROR.json
firestore_utils/uploads/LANG_FIXED_UK.json
firestore_utils/uploads/LANG_FIXED_EN.json
firestore_utils/uploads/LANG_FIXED_FR.json
firestore_utils/uploads/LANG_FIXED_ES.json
```

Upload to `development_domain_prompt_tokens_v3_system` with `--format json`.

See full language system documentation: [../localization_system/README.md](../localization_system/README.md)
