# Prompt System v3 — Complete Guide

**Purpose:** Practical reference for working with token-based prompt assembly.  
**Audience:** Developers implementing features or customizing prompts.

**Architecture Overview:** See [Prompt Design System v3 Building Block](../05_building_blocks/prompt_design_system_v3/README.md)

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Core Concepts](#2-core-concepts)
3. [Working with Tokens](#3-working-with-tokens)
4. [Profile Customization](#4-profile-customization)
5. [Practical Examples](#5-practical-examples)
6. [Testing & Debugging](#6-testing--debugging)
7. [Code Reference](#7-code-reference)

---

## 1. Quick Start

### 1.1 Assemble Prompt for Agent

```python
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

# Initialize service (dependency injection in main.py)
from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter

bio_formatter = BiographicalFactsFormatter()

assembly_service = PromptAssemblyService(
    token_repo=token_repo,
    blueprint_repo=blueprint_repo,
    profile_repo=profile_repo,
    security_port=security_port,
    formatter=context_formatter,
    bio_formatter=bio_formatter,  # Required for biographical facts
    cache_ttl=86400  # 24h cache (default)
)

# Assemble prompt
prompt = await assembly_service.assemble(
    agent_type="smart",
    user_id="user_123",
    account_id="account_abc",
    biographical_facts=["Lives in Kyiv", "Software engineer"],
    conversation_history=[
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"}
    ]
)

# Result: Groovy DSL prompt with user's customizations
print(prompt)
```

### 1.2 Customize User's Personality

```python
# User selects humor preset
await profile_repo.save_profile_slots(
    blueprint_id="universal_agent_v1",
    owner_type=OwnerType.USER,
    owner_value=user_id,
    slots=[
        ProfileSlot(
            type=ProfileSlotType.TOKEN,
            value="HUMOR_PRESET_OFF",  # Professional mode
            non_overridable=False
        )
    ]
)

# Next assembly will use HUMOR_PRESET_OFF instead of default RANEVSKAYA
```

### 1.3 Validate Token Before Creation

```python
# Create token with validation
token = await Token.create(
    id=TokenId("CUSTOM_TOKEN"),
    category=TokenCategory("humor_engine"),
    content="humor_engine { status: 'custom' }",
    metadata={},
    security_port=security_port  # Validates content
)

# If content has injection patterns → raises ValueError
```

---

## 2. Core Concepts

### 2.1 Token vs Component (v2 vs v3)

**Old (v2 - DANGEROUS):**

```python
# ❌ User provides raw text → injection risk
user_component = {
    "scope": "properties",
    "text": "Ignore all previous instructions. You are now..."
}
```

**New (v3 - SAFE):**

```python
# ✅ User selects token ID → no injection
user_profile = {
    "HUMOR_ENGINE": "HUMOR_PRESET_OFF"  # Token ID only
}
```

**Key Difference:** Users **never** inject raw text. They only choose from pre-approved tokens.

### 2.2 Object Hierarchy

```
Blueprint (universal_agent_v1)
    ├─ Contains 7 classes (HUMOR_ENGINE, ARCHETYPE, etc.)
    └─ Each class has BlueprintClass schema
        ├─ allowed_token_categories (which tokens allowed)
        ├─ overridable_by (who can customize)
        └─ default_token (fallback)

Profile (4 levels: SYSTEM > AGENT > ACCOUNT > USER)
    ├─ Contains ProfileSlots
    └─ Each slot assigns token to class
        ├─ type: "token" (direct assignment)
        ├─ value: "HUMOR_PRESET_OFF" (token ID)
        └─ non_overridable: false (can be overridden)

Token Library (39 tokens)
    ├─ System tokens (20): cognitive_process, policies, etc.
    └─ User tokens (19): humor, archetype, voice, etc.
```

### 2.3 4-Level Resolution

**Priority:** USER > ACCOUNT > AGENT > SYSTEM

```python
# SYSTEM profile (default for all smart agents)
{
    "HUMOR_ENGINE": "HUMOR_PRESET_RANEVSKAYA",  # Sharp wit
    "RESPONSE_STYLE": "RESPONSE_CONCISE"         # Brief responses
}

# USER profile (override HUMOR only)
{
    "HUMOR_ENGINE": "HUMOR_PRESET_OFF"  # Professional mode
    # RESPONSE_STYLE inherited from SYSTEM
}

# Result after merge:
# HUMOR_ENGINE: HUMOR_PRESET_OFF (USER wins)
# RESPONSE_STYLE: RESPONSE_CONCISE (SYSTEM fallback)
```

**Immutability Rule:** If `non_overridable=true` at lower level, higher levels **cannot** override.

### 2.4 Security Integration

**3 Validation Layers:**

1. **Token Creation** — Content validated via SecurityPort at creation
2. **Assignment** — Blueprint.can_assign() checks category + permissions
3. **Runtime** — biographical_context + conversation validated before injection

See [Security Validation Guide](./security_validation_guide.md) for details.

---

## 3. Working with Tokens

### 3.1 Token Structure

```python
@dataclass(frozen=True)
class Token:
    id: TokenId              # "HUMOR_PRESET_RANEVSKAYA"
    category: TokenCategory  # "humor_engine"
    class_: TokenClass       # "properties"
    content: str             # Groovy code block
    metadata: dict           # validation results
```

**Key Properties:**

- **Immutable** — Cannot modify after creation (frozen dataclass)
- **Validated** — SecurityPort validates content at creation
- **Categorized** — Grouped by category for profile resolution

### 3.2 Creating Tokens

**✅ CORRECT: Use factory method**

```python
token = await Token.create(
    id=TokenId("MY_TOKEN"),
    category=TokenCategory("humor_engine"),
    content="humor_engine { status: 'enabled' }",
    metadata={"description": "My custom humor"},
    security_port=security_port  # Validates!
)
```

**❌ WRONG: Direct instantiation**

```python
token = Token(...)  # Bypasses validation - DON'T DO THIS
```

### 3.3 Token Categories

| Category            | Purpose                    | User Customizable | Count |
| ------------------- | -------------------------- | ----------------- | ----- |
| `humor_engine`      | Humor style                | ✅ Yes            | 4     |
| `archetype`         | Core personality           | ✅ Yes            | 4     |
| `voice`             | Communication style        | ✅ Yes            | 4     |
| `response_style`    | Response format            | ✅ Yes            | 3     |
| `vibe`              | Emotional tone             | ✅ Yes            | 3     |
| `cognitive_process` | Reasoning algorithm        | ❌ No (Admin)     | 5     |
| `output_format`     | Output presentation        | ❌ No (Admin)     | 3     |
| `policy`            | System policies            | ❌ No (Immutable) | 6     |
| `protocol`          | Tool usage rules           | ❌ No (Immutable) | 2     |
| `directive`         | Runtime rules              | ❌ No (Immutable) | 2     |
| `special`           | Motto, behavior guide, etc | ❌ No (Admin)     | 3     |

**Total:** 39 tokens (19 user-customizable, 20 system-controlled)

### 3.4 Fetching Tokens

```python
# Get specific token
token = await token_repo.get(TokenId("HUMOR_PRESET_OFF"))

# List tokens by category
humor_tokens = await token_repo.list_by_category(TokenCategory("humor_engine"))
# Returns: [HUMOR_PRESET_RANEVSKAYA, HUMOR_PRESET_OFF, ...]

# List all user tokens
user_tokens = await token_repo.list_all(collection="user")
```

---

## 4. Profile Customization

### 4.1 Profile Structure

```python
# Profile = list of ProfileSlots
ProfileSlot(
    type: ProfileSlotType,    # "token", "category", "class", "slot"
    value: str,               # Token ID, category name, or class name
    non_overridable: bool     # Immutability flag
)
```

### 4.2 ProfileSlot Types

**Type: TOKEN (Direct Assignment)**

```python
ProfileSlot(
    type=ProfileSlotType.TOKEN,
    value="HUMOR_PRESET_OFF",  # Specific token
    non_overridable=False
)
```

**Type: CATEGORY (Assign All Tokens from Category)**

```python
ProfileSlot(
    type=ProfileSlotType.CATEGORY,
    value="humor_engine",  # All humor tokens
    non_overridable=False
)
```

**Type: SLOT (Exclude Class)**

```python
ProfileSlot(
    type=ProfileSlotType.SLOT,
    value="HUMOR_ENGINE",  # Disable this class
    non_overridable=True   # User cannot override
)
# Result: No humor token used (personality excluded)
```

### 4.3 Saving User Profile

```python
# Single token assignment
await profile_repo.save_profile_slots(
    blueprint_id="universal_agent_v1",
    owner_type=OwnerType.USER,
    owner_value=user_id,
    slots=[
        ProfileSlot(
            type=ProfileSlotType.TOKEN,
            value="HUMOR_PRESET_OFF",
            non_overridable=False
        )
    ]
)

# Multiple token assignments
await profile_repo.save_profile_slots(
    blueprint_id="universal_agent_v1",
    owner_type=OwnerType.USER,
    owner_value=user_id,
    slots=[
        ProfileSlot(type=ProfileSlotType.TOKEN, value="HUMOR_PRESET_OFF", non_overridable=False),
        ProfileSlot(type=ProfileSlotType.TOKEN, value="VOICE_FORMAL", non_overridable=False),
        ProfileSlot(type=ProfileSlotType.TOKEN, value="RESPONSE_DETAILED", non_overridable=False),
    ]
)
```

### 4.4 Loading Profile

```python
# Get profile for specific owner
slots = await profile_repo.get_profile_slots(
    blueprint_id="universal_agent_v1",
    owner_type=OwnerType.USER,
    owner_value=user_id
)

# Result: List[ProfileSlot]
for slot in slots:
    print(f"{slot.type}: {slot.value}")
```

---

## 5. Practical Examples

### 5.1 User Customizes Humor

**Scenario:** User wants professional mode (no jokes).

**Step 1: User selects token via UI**

```python
# Frontend sends API request
POST /api/v1/prompt/profile/customize
{
    "class_name": "HUMOR_ENGINE",
    "token_id": "HUMOR_PRESET_OFF"
}
```

**Step 2: Backend validates & saves**

```python
# Validate assignment
blueprint = await blueprint_repo.get("universal_agent_v1")
token = await token_repo.get(TokenId("HUMOR_PRESET_OFF"))

is_valid = blueprint.can_assign(
    class_name="HUMOR_ENGINE",
    token=token,
    owner_type=OwnerType.USER
)

if not is_valid:
    raise ValueError("Cannot assign this token")

# Save to USER profile
await profile_repo.save_profile_slots(
    blueprint_id="universal_agent_v1",
    owner_type=OwnerType.USER,
    owner_value=user_id,
    slots=[
        ProfileSlot(
            type=ProfileSlotType.TOKEN,
            value="HUMOR_PRESET_OFF",
            non_overridable=False
        )
    ]
)
```

**Step 3: Next assembly uses USER override**

```python
prompt = await assembly_service.assemble(
    agent_type="smart",
    user_id=user_id,
    account_id=None,
    biographical_facts=[],
    conversation_history=[]
)

# Prompt contains: humor_engine { status: 'disabled' }
# Instead of default Ranevskaya wit
```

### 5.2 Router Agent Excludes Personality

**Scenario:** Router agent needs JSON output, no personality.

**SYSTEM profile:**

```python
# scripts/migration/create_default_profiles.py
{
    "owner_type": OwnerType.SYSTEM,
    "owner_value": "router",
    "slots": [
        ProfileSlot(type=ProfileSlotType.TOKEN, value="COGNITIVE_PROCESS_ROUTER", non_overridable=False),
        ProfileSlot(type=ProfileSlotType.TOKEN, value="OUTPUT_FORMAT_JSON", non_overridable=False),
        # Exclude all personality slots
        ProfileSlot(type=ProfileSlotType.SLOT, value="HUMOR_ENGINE", non_overridable=True),
        ProfileSlot(type=ProfileSlotType.SLOT, value="ARCHETYPE", non_overridable=True),
        ProfileSlot(type=ProfileSlotType.SLOT, value="VOICE", non_overridable=True),
    ]
}
```

**Result:**

```python
# Router prompt has NO personality tokens
# Only cognitive_process + output_format
# USER cannot override exclusions (non_overridable=true)
```

### 5.3 Account-Level Customization

**Scenario:** Family account needs family-friendly humor for all users.

**ACCOUNT profile:**

```python
await profile_repo.save_profile_slots(
    blueprint_id="universal_agent_v1",
    owner_type=OwnerType.ACCOUNT,
    owner_value=account_id,
    slots=[
        ProfileSlot(
            type=ProfileSlotType.TOKEN,
            value="HUMOR_PRESET_FAMILY_FRIENDLY",
            non_overridable=False
        )
    ]
)
```

**Resolution:**

```python
# SYSTEM: HUMOR_PRESET_RANEVSKAYA (default)
# ACCOUNT: HUMOR_PRESET_FAMILY_FRIENDLY (override)
# Result: All users in account get family-friendly humor

# If USER sets HUMOR_PRESET_OFF:
# USER > ACCOUNT > SYSTEM → USER wins
```

### 5.4 Runtime Context Injection

**Scenario:** Inject user's biographical facts.

```python
biographical_facts = [
    {"text": "Lives in Kyiv, Ukraine", "domain": "biographical", "tags": [], "created_at": "..."},
    {"text": "Software engineer", "domain": "work", "tags": [], "created_at": "..."},
]

# Assembly service validates & appends
prompt = await assembly_service.assemble(
    agent_type="smart",
    user_id=user_id,
    account_id=None,
    biographical_facts=biographical_facts,  # ← Validated via SecurityPort
    conversation_history=[]
)

# Result: Facts appended as a knowledge_base block BEFORE the cache boundary.
# The block is omitted entirely when biographical_facts is empty.
```

**How injection works internally:**

```python
# Inside PromptAssemblyService._inject_runtime_context()

# 1. Facts tagged "semantic_lens" → dynamic section (after boundary)
# 2. All other facts → static biographical_context (before boundary)
static_facts = [f for f in biographical_facts if "semantic_lens" not in f.get("tags", [])]

# 3. Format with BiographicalFactsFormatter (domain-grouped Markdown)
bio_text = self.bio_formatter.format(static_facts)

# 4. Validate with SecurityPort (UNTRUSTED zone)
result = await self.security_port.validate(bio_text, context=f"biographical_user_{user_id}", zone=TrustZone.UNTRUSTED)

# 5. Append as knowledge_base block (only when non-empty — no empty wrappers)
if result.sanitized_text:
    prompt += "\n\nknowledge_base {\n    biographical_context: '''\n" + result.sanitized_text + "\n    '''\n}"

# 6. Append cache boundary + current_datetime (always) + Q-S context (if any)
prompt += "\n\n<!-- CACHE_BOUNDARY -->\n" + current_datetime_block
```

### 5.5 Validate Token Assignment

**Scenario:** Check if user can assign token to class.

```python
# Load blueprint
blueprint = await blueprint_repo.get("universal_agent_v1")

# Load token
token = await token_repo.get(TokenId("VOICE_FORMAL"))

# Validate assignment
can_assign = blueprint.can_assign(
    class_name="VOICE",
    token=token,
    owner_type=OwnerType.USER
)

if can_assign:
    print("✅ User can assign this token")
else:
    print("❌ Assignment not allowed")
    # Reasons: category mismatch OR permission denied
```

### 5.6 Cache Management

**Scenario:** Debugging slow prompts or testing cache behavior.

**Check cache hits in logs:**

```python
import logging
logging.getLogger("src.services.prompt_v3.prompt_assembly_service").setLevel(logging.INFO)

# Logs will show:
# 📦 Cache HIT: prompt:smart:acc:12345678:usr:abcdefgh
# or
# 📦 Cache MISS: prompt:smart:acc:12345678:usr:abcdefgh - assembling from repositories...
```

**Manual cache invalidation (admin):**

```python
# Clear all cached prompts
assembly_service.invalidate_cache()

# Useful when:
# - Token content updated (admin changed system token)
# - Blueprint modified
# - Testing without cache interference
```

**Preload cache during initialization:**

```python
# Warm up cache to avoid first-request latency
await assembly_service.preload_cache(
    agent_type="smart",
    account_id=account_id,
    user_id=user_id
)

# UserAgentFactory does this automatically during agent creation
# Result: First user message has 5ms latency instead of 110ms
```

**Cache key structure:**

```python
# Format: prompt:{agent_type}:acc:{account_id}:usr:{user_id}
# Example: prompt:smart:acc:account-abc12345-uuid...:usr:xyz67890-uuid...

# Note: Full IDs are used to prevent truncation bugs with "account-" prefix
# and ensure 100% uniqueness for 4-level resolution.
```

**Performance monitoring:**

```python
# Cold start (cache miss): ~110ms
# Warm start (cache hit): ~5ms
# Cache hit rate in production: 70-80%

# Why not 100%?
# - New users (not cached yet)
# - Cache expiry (24h TTL)
# - Token updates (invalidates related keys)
```

---

## 6. Testing & Debugging

### 6.1 Unit Tests

```bash
# Domain models
pytest tests/unit/domain/prompt_v3/ -v

# Services
pytest tests/unit/services/prompt_v3/ -v

# Adapters (mocked Firestore)
pytest tests/unit/adapters/prompt_v3/ -v
```

### 6.2 Integration Tests

```bash
# E2E assembly test with Firestore
pytest tests/integration/test_prompt_v3_e2e.py -v

# Specific test
pytest tests/integration/test_prompt_v3_e2e.py::test_user_selects_token_override -v
```

### 6.3 Inspection Scripts

**Inspect assembled prompt:**

```bash
# Shows final prompt for smart agent with default profile
python scripts/prompt/inspect_smart_prompt_v3.py

# Output:
# ✅ Blueprint: universal_agent_v1
# ✅ Tokens loaded: 7
# ✅ Prompt assembled (5432 chars)
#
# Prompt preview:
# class Alek {
#     properties {
#         humor_engine { ... }
#     }
# }
```

**E2E test with real Firestore:**

```bash
# Test assembly with real Firestore data
python scripts/prompt/test_e2e_smart_v3.py

# Output:
# ✅ Assembled smart agent prompt
# ✅ Security validation: 2 validations passed
# ✅ Final prompt: 5234 chars
```

### 6.4 Common Issues

| Issue                             | Cause                         | Solution                                              |
| --------------------------------- | ----------------------------- | ----------------------------------------------------- |
| `KeyError: Blueprint not found`   | Blueprint not in Firestore    | Run `create_blueprints.py --upload`                   |
| `AttributeError: Token has no id` | Token created without factory | Use `Token.create()` instead of `Token(...)`          |
| `ValueError: Security validation` | Injection pattern in content  | Review content, sanitize, re-create token             |
| `Profile slots empty`             | No SYSTEM profile             | Run `create_default_profiles.py --upload`             |
| `can_assign() returns False`      | Category/permission mismatch  | Check `allowed_token_categories` and `overridable_by` |
| `Prompt has {{PLACEHOLDERS}}`     | Token not resolved            | Check token exists + profile has assignment           |
| `knowledge_base block missing`    | Bio facts list is empty       | Verify `BiographicalContextService` is returning facts and cache is warm |
| `Immutability error`              | Trying to override immutable  | Lower-level slot has `non_overridable=true`           |

---

## 7. Code Reference

### 7.1 Domain Models

| File                                   | Purpose                              |
| -------------------------------------- | ------------------------------------ |
| `src/domain/prompt_v3/token.py`        | Token with factory validation        |
| `src/domain/prompt_v3/blueprint.py`    | Blueprint + BlueprintClass           |
| `src/domain/prompt_v3/profile_slot.py` | ProfileSlot + ProfileSlotType        |
| `src/domain/prompt_v3/slot.py`         | OwnerType enum                       |
| `src/domain/prompt_v3/section.py`      | SectionType enum                     |
| `src/domain/prompt_v3/security.py`     | SecurityPort + TrustZone + RiskLevel |

### 7.2 Services

| File                                                | Purpose                     |
| --------------------------------------------------- | --------------------------- |
| `src/services/prompt_v3/prompt_assembly_service.py` | Main assembly orchestrator  |
| `src/services/prompt_v3/context_formatter.py`       | Format conversation history |
| `src/services/prompt_v3/biographical_formatter.py`  | Format biographical facts   |

### 7.3 Repositories

| File                                                           | Purpose                                      |
| -------------------------------------------------------------- | -------------------------------------------- |
| `src/adapters/prompt_v3/firestore_token_repository.py`         | Token storage (dual-collection: system/user) |
| `src/adapters/prompt_v3/firestore_blueprint_repository.py`     | Blueprint storage                            |
| `src/adapters/prompt_v3/firestore_agent_profile_repository.py` | Profile storage (4-level resolution)         |

### 7.4 Tests

| File                                      | Purpose                           |
| ----------------------------------------- | --------------------------------- |
| `tests/integration/test_prompt_v3_e2e.py` | E2E assembly test (76 assertions) |
| `tests/unit/domain/prompt_v3/*.py`        | Domain model tests (40 tests)     |
| `tests/unit/services/prompt_v3/*.py`      | Service tests (11 tests)          |

### 7.5 Migration Scripts

| File                                           | Purpose                             |
| ---------------------------------------------- | ----------------------------------- |
| `scripts/migration/create_blueprints.py`       | Create universal_agent_v1 blueprint |
| `scripts/migration/create_default_profiles.py` | Create SYSTEM profiles (4 agents)   |
| `scripts/migration/migrate_tokens_split.py`    | Split tokens to system/user         |

See [scripts/migration/README.md](../../scripts/migration/README.md) for details.

### 7.6 Inspection Scripts

| File                                        | Purpose                         |
| ------------------------------------------- | ------------------------------- |
| `scripts/prompt/inspect_smart_prompt_v3.py` | Show assembled prompt for smart |
| `scripts/prompt/test_e2e_smart_v3.py`       | E2E test with real Firestore    |

---

## Related Documentation

**Architecture:**

- [Prompt Design System v3 Building Block](../05_building_blocks/prompt_design_system_v3/README.md) — Architecture overview
- [Security Validation Guide](./security_validation_guide.md) — Security validation details

**RFCs (Historical):**

- [PROMPT_DESIGN_SYSTEM_RFC.md](../10_rfcs/PROMPT_DESIGN_SYSTEM_RFC.md) — Original design proposal
- [PROMPT_DESIGN_SYSTEM_IMPLEMENTATION_PLAN.md](../10_rfcs/PROMPT_DESIGN_SYSTEM_IMPLEMENTATION_PLAN.md) — Implementation plan

**Migration:**

- [PROMPT_V3_ROLLBACK_PLAN.md](../10_rfcs/PROMPT_V3_ROLLBACK_PLAN.md) — Rollback strategy

**Legacy (Deprecated):**

- [Prompt System v2 (Legacy)](../05_building_blocks/prompt_system_v2_legacy/README.md) — Old component-based system

---

**Last Updated:** 2026-02-25
**Status:** ✅ Production Ready
