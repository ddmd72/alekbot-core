# RFC: Execution Context & Hexagonal Architecture Cleanup

**Status:** DRAFT (for future)
**Created:** 2026-02-01
**Scope:** Domain layer, Service layer, Dependency Injection

---

## Problem Statement

Currently execution context (user_id, account_id, session_id, platform) leaks into domain layer as separate scalar parameters. This violates hexagonal architecture principles and creates:

1. **Parameter Proliferation:** Each new context field = new parameter in 50+ methods
2. **Domain Coupling:** Domain objects know about infrastructure identifiers
3. **Poor Encapsulation:** No single source of truth for "who is executing this?"
4. **Testing Complexity:** Mock N parameters instead of 1 context object

### Current State (Examples)

**Agent (Domain):**
```python
async def execute(self, message: AgentMessage):
    user_id = message.context.get("user_id")
    account_id = message.context.get("account_id")  # NEW

    facts = await self._repo.get_active_facts(user_id=user_id)
    prompt = await self._prompt_service.get_assembled_prompt(
        template=TEMPLATE_SMART,
        agent_type="smart",
        user_id=user_id,
        account_id=account_id
    )
```

**Service:**
```python
async def get_assembled_prompt(
    self, template, agent_type,
    user_id: Optional[str] = None,
    account_id: Optional[str] = None,
    session_id: Optional[str] = None,  # future
    org_id: Optional[str] = None,      # future
    ...
):
```

**Problems:**
- Domain knows about `user_id`, `account_id` (infrastructure identifiers)
- Services accumulate parameters (not scalable)
- No type safety (all are `Optional[str]`)

---

## Proposed Solution: ExecutionContext Value Object

### Domain Layer

**1. Execution Context (Value Object):**
```python
# src/domain/execution_context.py

from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ExecutionContext:
    """
    Immutable execution context for domain operations.

    Encapsulates "who/what/where" without coupling domain to infrastructure.
    Domain treats these as opaque identifiers, adapters map to DB fields.

    Philosophy:
    - Domain: "I need context for this user"
    - Adapter: "user_id maps to Firestore field 'owner_id'"

    Hexagonal Principle:
    - Domain depends on abstractions (ExecutionContext)
    - Adapters depend on implementations (Firestore schema)
    """

    # Core identifiers (opaque to domain)
    user_id: Optional[str] = None
    account_id: Optional[str] = None

    # Extended context (for auditing, analytics)
    session_id: Optional[str] = None
    platform: Optional[str] = None  # slack, telegram, web
    request_id: Optional[str] = None

    # Future: org_id, team_id, workspace_id

    @classmethod
    def anonymous(cls) -> "ExecutionContext":
        """Create anonymous context (system operations)."""
        return cls()

    @classmethod
    def from_user_profile(cls, user: "UserProfile", **kwargs) -> "ExecutionContext":
        """Create context from UserProfile (common case)."""
        return cls(
            user_id=user.id,
            account_id=user.account_id,
            platform=kwargs.get("platform"),
            session_id=kwargs.get("session_id")
        )

    def is_authenticated(self) -> bool:
        """Check if context has user."""
        return self.user_id is not None

    def is_account_scoped(self) -> bool:
        """Check if context has account."""
        return self.account_id is not None
```

**2. Agent Refactoring:**
```python
# src/agents/consolidation_agent.py

from ..domain.execution_context import ExecutionContext

class ConsolidationAgent(BaseAgent):
    async def execute(
        self,
        message: AgentMessage,
        context: ExecutionContext  # ← Single parameter!
    ) -> AgentResponse:
        """
        Execute consolidation with clean context.

        Domain doesn't care HOW context is built or WHERE identifiers come from.
        It just uses opaque identifiers for delegation to services/repos.
        """

        # Get biographical context (repo maps context to Firestore query)
        bio_context = await self._repo.get_biographical_context(context)

        # Get assembled prompt (service maps context to 4-level resolution)
        prompt = await self._prompt_service.get_assembled_prompt(
            template=TEMPLATE_CONSOLIDATION,
            agent_type="consolidation",
            context=context  # ← Clean!
        )

        # Create facts (adapter maps context.user_id to owner_id field)
        fact = FactEntity(
            account_id=context.account_id,
            created_by_user_id=context.user_id,
            ...
        )
```

**3. Service Refactoring:**
```python
# src/services/prompt_component_service.py

async def get_assembled_prompt(
    self,
    template: PromptTemplate,
    agent_type: str,
    context: ExecutionContext,  # ← Single parameter!
    runtime_data: Optional[Dict] = None
) -> str:
    """
    Assemble prompt with execution context.

    Service extracts what it needs from context without domain knowing.
    """

    components = await self._resolve_components(
        template=template,
        agent_type=agent_type,
        user_id=context.user_id,      # Extract internally
        account_id=context.account_id  # Extract internally
    )
```

**4. Repository Refactoring:**
```python
# src/adapters/firestore_repo.py

async def get_biographical_context(
    self,
    context: ExecutionContext,
    limit: int = 100
) -> List[Dict]:
    """
    Fetch facts for context.

    Adapter maps context.user_id → Firestore field 'owner_id'.
    Domain doesn't know about Firestore schema.
    """

    if not context.user_id:
        return []

    query = self.facts_col.where(
        filter=FieldFilter("owner_id", "==", context.user_id)  # Adapter knows schema
    ).limit(limit)

    # ... fetch
```

---

## Migration Strategy

### Phase 1: Introduce ExecutionContext (Backward Compatible)

**Step 1:** Create `ExecutionContext` in `src/domain/execution_context.py`

**Step 2:** Update services to accept BOTH old params AND context:
```python
async def get_assembled_prompt(
    self,
    template: PromptTemplate,
    agent_type: str,
    user_id: Optional[str] = None,      # DEPRECATED
    account_id: Optional[str] = None,   # DEPRECATED
    context: Optional[ExecutionContext] = None  # NEW
):
    # Build context from old params if not provided
    if context is None:
        context = ExecutionContext(user_id=user_id, account_id=account_id)

    # Use context internally
    components = await self._resolve_components(template, agent_type, context)
```

**Step 3:** Update high-level callers (coordinators, handlers) to pass context

**Step 4:** Update agents one by one to use context

**Step 5:** Remove deprecated params (breaking change, bump major version)

### Phase 2: Repository Overloads

**Current:**
```python
async def get_active_facts(self, user_id: str, tags: List[str]):
```

**Future:**
```python
async def get_active_facts(self, context: ExecutionContext, tags: List[str]):
    user_id = context.user_id
    # ... query with user_id
```

---

## Benefits

### Before (Current - 2026-02-01)
```python
# 6 parameters, all Optional[str], no type safety
prompt = await service.get_assembled_prompt(
    template, agent_type,
    user_id=user_id,
    account_id=account_id,
    session_id=session_id,
    platform=platform,
    org_id=org_id,
    workspace_id=workspace_id
)
```

### After (Future)
```python
# 1 parameter, type-safe, encapsulated
prompt = await service.get_assembled_prompt(
    template, agent_type,
    context=context
)
```

**Wins:**
1. ✅ **Scalability:** Add new context fields without changing 50 signatures
2. ✅ **Type Safety:** `context.user_id` (typed) vs `user_id: Optional[str]` (untyped)
3. ✅ **Testability:** Mock 1 object vs N parameters
4. ✅ **Hexagonal:** Domain doesn't know infrastructure identifiers
5. ✅ **Encapsulation:** Single source of truth for execution context

---

## Trade-offs

| Aspect | Current (Separate Params) | Future (ExecutionContext) |
|--------|---------------------------|---------------------------|
| Simplicity | ✅ Simple for 2-3 params | ⚠️ Overkill for 1 param |
| Scalability | ❌ Accumulates params | ✅ Add fields without breaking |
| Hexagonal | ❌ Domain knows identifiers | ✅ Domain uses abstractions |
| Refactoring Cost | ✅ Zero (already done) | ❌ 100+ call sites |
| Testing | ❌ Mock N params | ✅ Mock 1 object |

---

## Decision

**POSTPONED** until architecture session 27+.

**Rationale:**
- Current approach works for 2-3 params (user_id, account_id)
- Refactoring cost is HIGH (100+ call sites)
- No blocking issue yet
- Can migrate incrementally (Phase 1 strategy)

**Trigger for revisit:**
- When we need 5+ context parameters
- When testing becomes painful (too many mocks)
- When hexagonal violations cause bugs

---

## Notes

**Hexagonal Principle Reminder:**
- Domain should depend on **WHAT** (abstractions, interfaces)
- Adapters should depend on **HOW** (implementations, schemas)

**Current compromise:**
- Domain uses `user_id`, `account_id` as **opaque identifiers**
- Adapters map to Firestore fields (`owner_id`, `account_id`)
- This is ACCEPTABLE but not IDEAL

**Future ideal:**
- Domain uses `ExecutionContext` (abstraction)
- Adapters extract fields and map to schema
- This is CLEAN hexagonal architecture

---

**Related RFCs:**
- Multi-Tenant OAuth (Session 1) - introduced `account_id`
- Prompt Component Architecture (Session 23-25) - 3→4 level system

**See also:**
- `docs/architecture/TARGET_ARCHITECTURE_v6.md` - Hexagonal principles
- `docs/guides/PROMPT_COMPONENTS_GUIDE.md` - Current prompt system
