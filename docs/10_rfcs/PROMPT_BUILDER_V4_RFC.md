# Prompt Builder v4 RFC

**Status:** ✅ IMPLEMENTED
**RFC Date:** 2026-02-27
**Implementation Date:** 2026-02-27
**Replaces:** Prompt Builder v3 (see Section 1 for archive)

> **Implementation Note:** The v4 class-collection assembly model is fully in production.
> All code changes, Firestore migrations, and UAT are complete.
> See Section 8 for a summary of what changed from the RFC design vs. final implementation.

---

## Motivation

v3 has two structural problems that compound as the agent fleet grows:

1. **Adding a token to an agent requires 3 operations:** modify template string + add class entry to blueprint + assign in profile. Template and blueprint change every time a new capability is added.

2. **Naming mismatch:** `classes` in the blueprint are actually named individual slots (`HUMOR_ENGINE`, `VOICE`), not classes in any OOP or semantic sense. This creates confusion between the slot name, the token category, and the Groovy section.

v4 goal: adding a token to an agent = edit the agent profile only. Blueprint and token library are stable after initial setup.

---

## 1. V3 Archive (Current State — Dev Collections)

### 1.1 Collection Names (dev prefix = `development_`)

| Purpose | Collection |
|---------|-----------|
| System tokens | `development_domain_prompt_tokens_v3` (implicit `_system` suffix in code) |
| User token overrides | `development_domain_prompt_tokens_v3_user` |
| Blueprints | `development_domain_prompt_blueprints_v3` |
| Agent profiles (SYSTEM/AGENT) | `development_domain_prompt_profiles_v3` |
| User/Account overrides | `development_domain_prompt_overrides_v3` |

### 1.2 Token Document (v3)

```json
{
  "token_id": "HUMOR_PRESET_RANEVSKAYA",
  "category": "humor_engine",
  "class": "HUMOR_ENGINE",
  "content": "humor_engine { persona: \"Faina Ranevskaya\" ... }",
  "metadata": {
    "version": "1.0",
    "author": "system",
    "description": "...",
    "validation": {
      "risk_level": "SAFE", "risk_score": 0,
      "patterns_detected": [], "action_taken": "passed",
      "context": "token_creation", "zone": "trusted"
    }
  },
  "updated_at": "...",
  "uploaded_by": "local_script",
  "source_file": "..."
}
```

### 1.3 Blueprint Document (v3)

```json
{
  "blueprint_id": "universal_agent_v1",
  "template": "class Alek extends Agent{\n\n    properties {\n\n        {{ARCHETYPE}}\n\n        {{VIBE}}\n\n        {{MOTTO_DEFAULT}}\n\n        {{VOICE}}\n\n        few_shot_learning {\n            policy: \"Strictly mimic the Tone and Wit of the GOOD_EXAMPLE entries.\"\n        }\n\n        {{BEHAVIOR_GUIDE_RANEVSKAYA_MODE}}\n\n        {{HUMOR_ENGINE}}\n    }\n\n    {{COGNITIVE_PROCESS}}\n\n    policies {\n        {{POLICY_OUTPUT_LANGUAGE}}\n        {{POLICY_PRIVACY}}\n        {{POLICY_NO_OPEN_LOOPS}}\n        {{POLICY_ANTI_GUARDIAN}}\n        {{POLICY_WITTY_ACCENTUATION}}\n        {{POLICY_ALIGN_WITH_ANCHORS}}\n    }\n\n    protocols {\n        {{PROTOCOL_SEARCH_MEMORY}}\n        {{PROTOCOL_WEB_SEARCH}}\n        {{PROTOCOL_SMART_AGENT_SELECTION}}\n        {{PROTOCOL_QUICK_AGENT_SELECTION}}\n    }\n\n    {{OUTPUT_FORMAT}}\n\n    final_directives {\n        {{DIRECTIVE_SLACK_FORMATTING}}\n        {{DIRECTIVE_BREVITY}}\n    }\n}\n",
  "classes": {
    "HUMOR_ENGINE": {
      "allowed_token_categories": ["humor_engine"],
      "overridable_by": ["account", "user"],
      "default_token": "HUMOR_PRESET_RANEVSKAYA"
    },
    "VOICE": { "allowed_token_categories": ["voice"], "overridable_by": ["account", "user"], "default_token": "VOICE_APHORISTIC" },
    "COGNITIVE_PROCESS": { "allowed_token_categories": ["cognitive_process"], "overridable_by": ["system", "agent"], "default_token": "COGNITIVE_PROCESS_QUICK" },
    "ARCHETYPE": { "allowed_token_categories": ["archetype"], "overridable_by": ["account", "user"], "default_token": "ARCHETYPE_INTELLECTUAL_SNIPER" },
    "VIBE": { "allowed_token_categories": ["vibe"], "overridable_by": ["account", "user"], "default_token": "VIBE_BATTLE_WEARY" },
    "MOTTO_DEFAULT": { "allowed_token_categories": ["motto"], "overridable_by": ["system"], "default_token": "MOTTO_DEFAULT" },
    "BEHAVIOR_GUIDE_RANEVSKAYA_MODE": { "allowed_token_categories": ["behavior_guide"], "overridable_by": ["system"], "default_token": "BEHAVIOR_GUIDE_RANEVSKAYA_MODE" },
    "OUTPUT_FORMAT": { "allowed_token_categories": ["output_format"], "overridable_by": ["agent", "account"], "default_token": "OUTPUT_FORMAT_STANDARD" },
    "PROTOCOL_SEARCH_MEMORY": { "allowed_token_categories": ["protocol"], "overridable_by": ["system"], "default_token": "PROTOCOL_SEARCH_MEMORY" },
    "PROTOCOL_WEB_SEARCH": { "allowed_token_categories": ["protocol"], "overridable_by": ["system"], "default_token": "PROTOCOL_WEB_SEARCH" },
    "PROTOCOL_SMART_AGENT_SELECTION": { "allowed_token_categories": ["protocol"], "overridable_by": ["system"], "default_token": "PROTOCOL_SMART_AGENT_SELECTION" },
    "PROTOCOL_QUICK_AGENT_SELECTION": { "allowed_token_categories": ["protocol"], "overridable_by": ["system"], "default_token": null },
    "POLICY_OUTPUT_LANGUAGE": { "allowed_token_categories": ["policy"], "overridable_by": ["system"], "default_token": "POLICY_OUTPUT_LANGUAGE" },
    "POLICY_PRIVACY": { "allowed_token_categories": ["policy"], "overridable_by": ["system"], "default_token": "POLICY_PRIVACY" },
    "POLICY_NO_OPEN_LOOPS": { "allowed_token_categories": ["policy"], "overridable_by": ["system"], "default_token": "POLICY_NO_OPEN_LOOPS" },
    "POLICY_ANTI_GUARDIAN": { "allowed_token_categories": ["policy"], "overridable_by": ["system"], "default_token": "POLICY_ANTI_GUARDIAN" },
    "POLICY_WITTY_ACCENTUATION": { "allowed_token_categories": ["policy"], "overridable_by": ["system"], "default_token": "POLICY_WITTY_ACCENTUATION" },
    "POLICY_ALIGN_WITH_ANCHORS": { "allowed_token_categories": ["policy"], "overridable_by": ["system"], "default_token": "POLICY_ALIGN_WITH_ANCHORS" },
    "DIRECTIVE_SLACK_FORMATTING": { "allowed_token_categories": ["final_directive"], "overridable_by": ["system"], "default_token": "DIRECTIVE_SLACK_FORMATTING" },
    "DIRECTIVE_BREVITY": { "allowed_token_categories": ["final_directive"], "overridable_by": ["system"], "default_token": "DIRECTIVE_BREVITY" },
    "FEW_SHOT_EXAMPLES_DEFAULT": { "allowed_token_categories": ["few_shot_examples"], "overridable_by": ["system"], "default_token": "FEW_SHOT_EXAMPLES_DEFAULT" }
  }
}
```

### 1.4 Agent Profile Document (v3)

Stored in `development_domain_prompt_profiles_v3`.
Document ID: `{blueprint_id}_{OWNER_TYPE}_{owner_value}` (e.g., `universal_agent_v1_SYSTEM_quick`).

```json
{
  "profile_id": "universal_agent_v1_SYSTEM_quick",
  "blueprint_id": "universal_agent_v1",
  "owner_type": "SYSTEM",
  "owner_value": "quick",
  "slots": [
    {"type": "token", "value": "COGNITIVE_PROCESS_QUICK", "non_overridable": false},
    {"type": "token", "value": "HUMOR_PRESET_LIGHT",      "non_overridable": false},
    {"type": "token", "value": "VOICE_CONVERSATIONAL",    "non_overridable": false},
    {"type": "token", "value": "RESPONSE_CONCISE",        "non_overridable": false},
    {"type": "token", "value": "OUTPUT_FORMAT_STANDARD",  "non_overridable": false},
    {"type": "token", "value": "PROTOCOL_QUICK_AGENT_SELECTION", "non_overridable": true},
    {"type": "slot",  "value": "ARCHETYPE",               "non_overridable": true},
    {"type": "slot",  "value": "VIBE",                    "non_overridable": true}
  ]
}
```

Slot `type: "slot"` = exclude this named slot (prevent default_token from filling it).
Slot `type: "token"` = assign this token_id.
Other types: `"class"`, `"category"` — used for fallback resolution.

### 1.5 V3 Assembly Logic (summary)

1. Load blueprint → get template string + classes dict
2. Resolve slots across 4 levels: USER > ACCOUNT > AGENT > SYSTEM
3. For each blueprint class: find assigned token (from profile slot, default_token, or exclusion)
4. Replace `{{CLASS_NAME}}` placeholders in template string
5. Remove unfilled `{{...}}` placeholders
6. Append runtime context (bio facts, history) with CACHE_BOUNDARY

---

## 2. V4 Design

### 2.1 Core Idea

- **Blueprint** = declarative: outer class name + ordered list of Groovy section names. No template string.
- **Token** = self-describing: knows its `class` (Groovy section) and `category` (semantic group). No positional data.
- **Agent Profile** = map of `token_id → {order, non_overridable}`. Order is per-agent, not global.
- **Override** = account/user replaces a token by matching `class + category`. Can only replace, not add new classes.

### 2.2 Token Document (v4)

```json
{
  "token_id": "HUMOR_PRESET_RANEVSKAYA",
  "class": "properties",
  "category": "humor_engine",
  "content": "humor_engine { persona: \"Faina Ranevskaya\" ... }",
  "metadata": { "version": "2.0", "description": "..." }
}
```

**Field semantics:**
- `class` — Groovy section this token renders into (`properties`, `cognitive_process`, `policies`, `protocols`, `output_format`, `final_directives`)
- `category` — semantic group; used as deduplication key when overriding (`humor_engine`, `voice`, `cognitive_process`, `policy`, `protocol`, `output_format`, `final_directive`, `archetype`, `vibe`, `motto`, `behavior_guide`)
- `content` — bare Groovy block(s), NO outer section wrapper (the wrapper is generated by assembly)

### 2.3 Blueprint Document (v4)

```json
{
  "blueprint_id": "universal_agent_v1",
  "outer_class": "Alek extends Agent",
  "class_order": [
    "properties",
    "cognitive_process",
    "policies",
    "protocols",
    "output_format",
    "final_directives"
  ]
}
```

No `template`, no `classes` dict, no `default_token` — these concepts move to the token and profile.

### 2.4 Agent Profile Document (v4)

Stored in `development_domain_prompt_profiles_v3`.
Document ID: `{agent_id}` (e.g., `quick`, `router`, `consolidation`).

> **Implementation change vs. RFC draft:** Document ID was simplified to just `agent_id`.
> The `blueprint_id` lives inside the document as a field. This means the assembly service
> reads `blueprint_id` from the profile itself — no hardcoded mapping in application code.

```json
{
  "blueprint_id": "universal_agent_v1",
  "agent_id": "quick",
  "tokens": {
    "ARCHETYPE_INTELLECTUAL_SNIPER":  {"order": 10},
    "VIBE_BATTLE_WEARY":              {"order": 20},
    "MOTTO_DEFAULT":                  {"order": 30},
    "VOICE_CONVERSATIONAL":           {"order": 40},
    "BEHAVIOR_GUIDE_RANEVSKAYA_MODE": {"order": 50},
    "HUMOR_PRESET_LIGHT":             {"order": 60},
    "COGNITIVE_PROCESS_QUICK":        {"order": 10, "non_overridable": true},
    "POLICY_OUTPUT_LANGUAGE":         {"order": 10},
    "POLICY_PRIVACY":                 {"order": 20},
    "POLICY_NO_OPEN_LOOPS":           {"order": 30},
    "POLICY_ANTI_GUARDIAN":           {"order": 40},
    "POLICY_WITTY_ACCENTUATION":      {"order": 50},
    "POLICY_ALIGN_WITH_ANCHORS":      {"order": 60},
    "PROTOCOL_SEARCH_MEMORY":         {"order": 10},
    "PROTOCOL_QUICK_AGENT_SELECTION": {"order": 20, "non_overridable": true},
    "OUTPUT_FORMAT_STANDARD":         {"order": 10},
    "DIRECTIVE_SLACK_FORMATTING":     {"order": 10},
    "DIRECTIVE_BREVITY":              {"order": 20}
  }
}
```

**Field semantics:**
- Key = `token_id`. Duplicate keys impossible (map constraint).
- `order: int` — rendering position within the class. Step 10, gaps for easy insertion.
- `non_overridable: bool` — default `false`. If `true`, account/user cannot replace this token.

### 2.5 Account/User Override Document (v4)

Stored in a separate overrides collection (configured in `FirestoreAgentProfileRepository`
constructor as `overrides_collection`).
Document ID: `{OWNER_TYPE}_{owner_id}` (e.g., `ACCOUNT_acc_123`, `USER_user_456`).

> **Implementation change vs. RFC draft:** Document ID no longer includes `blueprint_id`.
> Overrides are account/user-global and apply to whatever blueprint the agent uses.
> The `blueprint_id` prefix was redundant since override tokens are matched by class+category,
> not by blueprint identity.

```json
{
  "blueprint_id": "universal_agent_v1",
  "owner_type": "ACCOUNT",
  "owner_id": "acc_xxx",
  "tokens": {
    "VOICE_CONVERSATIONAL": {"order": 40}
  }
}
```

Override replaces agent tokens by `class + category` match. Rules:
- Account token replaces agent token IF: same `class` + same `category` AND agent token `non_overridable != true`
- If agent has NO token with matching `class + category` → override is ignored (cannot add new classes)
- User override applies on top of account override with same rules

### 2.6 Assembly Algorithm

```
Input: agent_id, blueprint_id, account_id?, user_id?

1. Load blueprint → outer_class, class_order
2. Load agent profile tokens map → resolve each token_id to Token document
3. If account_id → load account overrides → for each override token:
      find agent token with same class + category
      if found AND not non_overridable → replace in working set
4. If user_id → load user overrides → same replacement logic on top of step 3
5. Group working set tokens by class
6. For each class in class_order (skip if empty):
      sort tokens by order
      join content with "\n\n"
      wrap: "    {class_name} {{\n        {content}\n    }}"
7. Build final prompt:
      "class {outer_class} {{\n{sections}\n}}"
8. Append CACHE_BOUNDARY + runtime context (bio facts, datetime, Q-S context)
```

### 2.7 Assembly Output Example

```groovy
class Alek extends Agent {

    properties {

        archetype { ... }

        vibe { ... }

        motto { ... }

        voice { ... }

        behavior_guide { ... }

        humor_engine { ... }
    }

    cognitive_process {

        instruction: "..."
        steps: [...]
    }

    policies {

        policy_output_language { ... }

        policy_privacy { ... }

        ...
    }

    protocols {

        agents_registry { ... }
    }

    output_format {

        response_rules: [...]
    }

    final_directives {

        directive_slack_formatting { ... }

        directive_brevity { ... }
    }
}

<!-- CACHE_BOUNDARY -->
[current datetime]
[query-specific context if any]
```

---

## 3. Token Content Changes Required

Some existing tokens have content that spans multiple Groovy sections. These must be split.

### 3.1 COGNITIVE_PROCESS_WEBSEARCH_LIGHT (critical)

**Current content** (spans 4 sections):
```groovy
properties { archetype: "..." }
cognitive_process { instruction: "..." rules: [...] }
output_format { language: "..." style: "..." rules: [...] }
execution { instruction: "WebSearchLightAgent.run(query)" }
```

**V4 split:**

| New Token | class | category | Content |
|-----------|-------|----------|---------|
| `ARCHETYPE_WEBSEARCH_LIGHT` | `properties` | `archetype` | `archetype { ... }` → bare inner content |
| `COGNITIVE_PROCESS_WEBSEARCH_LIGHT` | `cognitive_process` | `cognitive_process` | inner content only |
| `OUTPUT_FORMAT_WEBSEARCH_LIGHT` | `output_format` | `output_format` | inner content only |

`execution {}` block — remove (meta-comment for developer, not consumed by LLM as instruction).

### 3.2 COGNITIVE_PROCESS_MEMORY_SEARCH

Check if content wraps `cognitive_process { }` — if yes, strip the wrapper.

### 3.3 All COGNITIVE_PROCESS_* tokens

Currently content is `cognitive_process { ... }`. In v4: strip the outer wrapper, content = inner body only.

### 3.4 All OUTPUT_FORMAT_* tokens

Currently content is `output_format { ... }`. In v4: strip the outer wrapper.

### 3.5 Tokens that are already bare (no change needed)

Tokens in `properties`, `policies`, `protocols`, `final_directives` — their content is already a named inner block (e.g., `humor_engine { ... }`, `policy_privacy { ... }`). These are correct as-is.

---

## 4. Firestore Collections — What Changes

| Collection | Change |
|-----------|--------|
| `development_domain_prompt_tokens_v3` | Each document: remove `updated_at` metadata fields if desired; `class` changes from `"HUMOR_ENGINE"` (slot name) to `"properties"` (section name); `category` stays; no new fields |
| `development_domain_prompt_blueprints_v3` | Replace entire document: remove `template`, remove `classes` dict, add `outer_class` + `class_order` |
| `development_domain_prompt_profiles_v3` | Replace entire document: `slots` array → `tokens` map; remove `owner_type`, add `agent_id` |
| `development_domain_prompt_overrides_v3` | Replace entire document: same `slots` → `tokens` map migration |

---

## 5. Code Changes

### 5.1 Domain Layer (`src/domain/prompt_v3/`) ✅ DONE

| File | Change | Status |
|------|--------|--------|
| `blueprint.py` | `Blueprint`: removed `classes` dict + `template`, added `outer_class: str`, `class_order: List[str]` | ✅ |
| `slot.py` | Kept `OwnerType` enum (AGENT/ACCOUNT/USER) | ✅ |
| `token.py` | `Token.class_` now maps to Groovy section name (e.g., `properties`), not named slot | ✅ |
| `profile_slot.py` | Replaced with `ProfileToken(token_id, order, non_overridable)` | ✅ |
| `agent_profile.py` | **NEW** — `AgentProfile(blueprint_id: str, tokens: Dict[str, ProfileToken])` | ✅ NEW |

### 5.2 Ports (`src/ports/prompt_v3/`) ✅ DONE

| File | Change | Status |
|------|--------|--------|
| `agent_profile_repository.py` | `get_agent_tokens(blueprint_id, agent_id)` → `get_agent_profile(agent_id) -> AgentProfile`; `get_override_tokens(owner_type, owner_id)` (no blueprint_id); `delete_profile(owner_type, owner_value)` (no blueprint_id) | ✅ |
| `blueprint_repository.py` | Signatures unchanged | ✅ |
| `token_repository.py` | `list_by_class(token_class)` signature stays; class values now = section names | ✅ |

### 5.3 Adapters (`src/adapters/prompt_v3/`) ✅ DONE

| File | Change | Status |
|------|--------|--------|
| `firestore_blueprint_repository.py` | Reads `outer_class` + `class_order` | ✅ |
| `firestore_agent_profile_repository.py` | `get_agent_profile(agent_id)`: doc ID = `agent_id`; returns `AgentProfile`. `get_override_tokens(owner_type, owner_id)`: doc ID = `{OWNER_TYPE}_{owner_id}` | ✅ |
| `firestore_token_repository.py` | Dual-collection lookup: `_system` first, then `_user` | ✅ |

### 5.4 Assembly Service (`src/services/prompt_v3/prompt_assembly_service.py`) ✅ DONE

Changes implemented:
- `_assemble_static_template()` — rewrote to: (1) fetch agent profile, (2) parallel blueprint + overrides, (3) class+category override resolution, (4) token grouping by class, (5) section rendering
- `_BLUEPRINT_FOR_AGENT` class constant — **not implemented** (blueprint_id read from profile instead)
- `_normalize_whitespace()` — kept
- Cache logic — kept unchanged

### 5.5 Tests ✅ DONE

- `tests/unit/services/prompt_v3/test_prompt_assembly_service.py` — updated for v4 (profile mock → `AgentProfile`, override side_effects updated)
- `tests/unit/ports/test_prompt_v3_contracts.py` — updated signatures (`get_agent_profile`, `get_override_tokens` without blueprint_id)
- `tests/unit/adapters/prompt_v3/test_firestore_repositories.py` — updated for `get_agent_profile()`, added `blueprint_id` assertion
- All 1198 unit tests passing (1 xfailed)

### 5.6 Firestore Utils

`download.py` / `upload.py` — blueprint groovy mode is now unused (no template field). Update or remove groovy support for blueprints, or repurpose. JSON mode still works for full doc.

---

## 6. Migration Steps ✅ COMPLETED (2026-02-27)

1. ✅ **Code** — domain + ports + adapters + assembly service rewritten for v4
2. ✅ **Upload files** — migration script `scripts/migration/generate_v4_uploads.py` generated 45 files:
   - 6 blueprint documents (`universal_agent_v1`, `router_agent_v1`, `websearch_agent_v1`, `websearch_light_agent_v1`, `consolidation_agent_v1`, `memorysearch_agent_v1`)
   - 7 agent profile documents (`quick`, `smart`, `router`, `websearch`, `websearch_light`, `consolidation`, `memorysearch`)
   - 7 updated system tokens (wrapper stripped / class fixed)
   - 25 new split tokens (ROUTER_*, WEBSEARCH_*, CONSOLIDATION_*, MEMORYSEARCH_*)
3. ✅ **Upload to dev** — all 45 documents uploaded to `us-production` database dev collections
4. ✅ **Tests** — 1198 unit tests passing
5. ✅ **UAT** — end-to-end prompt inspection via `scripts/prompt/test_agent_e2e.py`, result: "perfect"
6. Prod migration — pending (dev collections use `development_` prefix; prod collections have no prefix)

---

## 7. What Does NOT Change

- `PromptBuilderPort` interface (agents don't change)
- `SecurityPort` interface (validation pipeline unchanged)
- `PROMPT_CACHE_BOUNDARY` mechanism (static/dynamic split preserved)
- Runtime context injection (`biographical_facts`, `conversation_history`, `query_specific_context`)
- Cache TTL logic (24h in-memory cache)
- Agent code (all 7 agents call `prompt_builder.build_for_agent()` — interface stable)
- `BiographicalFactsFormatter`, `ContextFormatter`
- Firestore collection names (still `*_v3` suffix — "v4" refers to assembly model, not schema generation)

---

## 8. Implementation Notes — RFC vs. Final

Changes made during implementation that differ from the original RFC design:

### 8.1 Agent Profile Document ID

**RFC design:** `{blueprint_id}_{agent_id}` (e.g., `universal_agent_v1_quick`)

**Implemented:** `{agent_id}` (e.g., `quick`)

**Reason:** `blueprint_id` should live in the data, not the key. The assembly service reads
`blueprint_id` from inside the profile document, enabling blueprint changes without code
deployments. Document IDs are also simpler and don't encode a design decision.

### 8.2 Override Document ID

**RFC design:** `{blueprint_id}_{ACCOUNT|USER}_{id}`

**Implemented:** `{OWNER_TYPE}_{owner_id}` (e.g., `ACCOUNT_acc_123`)

**Reason:** Overrides are account/user-global. An account's preference for `VOICE_FORMAL`
applies to any agent that has a `voice` class token — regardless of blueprint. Embedding
`blueprint_id` in the override key would require separate override documents per blueprint,
creating maintenance overhead with zero benefit.

### 8.3 New Domain Type: AgentProfile

**RFC design:** `get_agent_tokens(blueprint_id, agent_id)` returns `Dict[str, ProfileToken]`

**Implemented:** `get_agent_profile(agent_id)` returns `AgentProfile(blueprint_id, tokens)`

**Reason:** The caller needs `blueprint_id` to fetch the blueprint. If the port only returned
a tokens dict, the caller would still need a way to learn the blueprint_id — leading back to
a hardcoded map in the service. The `AgentProfile` wrapper makes the data self-contained.

### 8.4 Per-Agent Blueprints (not in original RFC)

The RFC proposed a single `universal_agent_v1` for all agents. During implementation, the
need for domain-specific section names became clear:
- `router_agent_v1`: sections `identity`, `knowledge_base`, `policies`, `cognitive_process`, `conflict_resolution`, `output_format`, `examples`
- `consolidation_agent_v1`: sections `taxonomy`, `cognitive_process`, `tools`, `examples`, `policies`, `output_specification`
- `memorysearch_agent_v1`: sections `identity`, `cognitive_process`, `examples`, `anti_patterns`, `output_format`

6 blueprints total. quick and smart still share `universal_agent_v1`.

### 8.5 Token Split: 25 New Tokens

The RFC identified multi-section tokens that needed splitting. All splits were completed
via `scripts/migration/generate_v4_uploads.py`:
- `COGNITIVE_PROCESS_ROUTER` → 7 tokens (ROUTER_IDENTITY, ROUTER_KNOWLEDGE_BASE, ROUTER_POLICIES, ROUTER_COGNITIVE_PROCESS, ROUTER_CONFLICT_RESOLUTION, ROUTER_OUTPUT_FORMAT, ROUTER_EXAMPLES)
- `COGNITIVE_PROCESS_WEBSEARCH` → 4 tokens (WEBSEARCH_PROPERTIES, WEBSEARCH_COGNITIVE_PROCESS, WEBSEARCH_OUTPUT_FORMAT, WEBSEARCH_EXECUTION)
- `COGNITIVE_PROCESS_WEBSEARCH_LIGHT` → 4 tokens (WEBSEARCH_LIGHT_*)
- `COGNITIVE_PROCESS_MEMORY_SEARCH` → 4 tokens (MEMORYSEARCH_IDENTITY, MEMORYSEARCH_COGNITIVE_PROCESS, MEMORYSEARCH_EXAMPLES, MEMORYSEARCH_ANTI_PATTERNS)
- `COGNITIVE_PROCESS_CONSOLIDATION` → 6 tokens (CONSOLIDATION_TAXONOMY, CONSOLIDATION_COGNITIVE_PROCESS, CONSOLIDATION_TOOLS, CONSOLIDATION_EXAMPLES, CONSOLIDATION_POLICIES, CONSOLIDATION_OUTPUT_SPEC)
