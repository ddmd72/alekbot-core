# New Specialist Agent — Step-by-Step Playbook

> **Scope:** Simple SYNC specialist (one or more intents, one LLM call, returns inline result).
> For ASYNC agents (Cloud Tasks + polling) or agents with service layer — see the relevant RFC first.

**Time budget per agent:** ~2 hours (code ~1h, prompt work ~1h).

---

## Phase 0 — Decisions Before Writing Code

Answer these before touching any file:

| Question | Options | Example |
|----------|---------|---------|
| What is the agent's single responsibility? | One sentence | "Execute Python code in sandbox" |
| How many intents? | 1 (simple) or 2–4 (typed variants) | 4 compute intents |
| What goes in `payload.query`? | Natural language string | Task verbatim |
| Which provider? | gemini / claude / grok | gemini (if uses Gemini-specific tools) |
| Which PerformanceTier? | ECO / BALANCED / PERFORMANCE | BALANCED (Flash) |
| Does it need a service layer? | Yes if: DB, external API, multi-step logic | No for single LLM call |
| Does it need new ports? | Yes if: 2+ impls, testable boundary | No for single LLM call |

---

## Phase 1 — Code

Touch files **in this exact order**.

### Step 1 — `src/infrastructure/agent_manifest.py`

Add at the `class Intent:` block:
```python
class Intent:
    # ... existing ...
    FOO = "foo"
```

Add `AgentDescriptor` after existing specialists:
```python
FOO = AgentDescriptor(
    agent_id="foo_agent",
    agent_type="foo",           # matches profile document ID in Firestore
    capabilities={Intent.FOO: ExecutionMode.SYNC},
    description="One-line description for logs",
    capability_descriptions={
        Intent.FOO: (
            "What this agent does and when to call it. "
            "State clearly what it CANNOT do. "
            "payload: {\"query\": \"<task as natural language>\"}"
        ),
    },
    internal=False,             # True = hidden from LLM tool list (e.g. websearch_light)
)

ALL_DESCRIPTORS = [..., FOO]    # append to the existing list
```

Both Quick and Smart automatically discover the new intent — no changes to either orchestrator.

### Step 2 — `src/infrastructure/agent_config.py`

Add after existing configs:
```python
@dataclass
class FooAgentConfig:
    temperature: float = 0.7    # 0.0 for deterministic computation
    timeout_ms: int = 30_000    # budget per single LLM call

FOO = FooAgentConfig()
```

### Step 3 — `src/services/agent_context_builder.py`

Add to `AgentProviderStrategy.STRATEGIES`:
```python
"foo": {
    "default_provider": "gemini",           # or "claude", "grok"
    "allowed_providers": ["gemini"],        # lock if agent needs provider-specific features
    "required_capabilities": ["native_tools"],
    "fallback": None,
},
```

### Step 4 — `src/agents/foo_agent.py`

Use this exact structure. Do not deviate.

```python
import time
from typing import Optional

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse, AgentIntent
from ..infrastructure.agent_config import FOO
from ..ports.llm_port import LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from .base_agent import BaseAgent
from ..utils.logger import logger


class FooAgent(BaseAgent):
    TEMPERATURE = FOO.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self.user_id = user_id

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        return bool(message.payload.get("query", ""))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        if not query:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided in payload",
            )
        return await self._call_foo(message, query)

    async def _call_foo(self, message: AgentMessage, query: str) -> AgentResponse:
        start_time = time.time()
        self._on_agent_start(query)

        system_prompt = ""
        if self.prompt_builder:
            try:
                system_prompt = await self.prompt_builder.build_for_agent("foo", self.user_id)
            except Exception as e:
                logger.warning(f"FooAgent: PromptBuilder failed, using empty prompt: {e}")

        try:
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=[Message(role="user", parts=[MessagePart(text=query)])],
                temperature=self.TEMPERATURE,
                # use_code_execution=True  # if Gemini sandbox needed
                # use_grounding=True       # if Google Search grounding needed
            )
            response = await self._call_llm(request)  # auto-logs request + response
            result_text = response.text or "No result."
            token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
            self._on_agent_success(len(result_text), token_count, output_text=result_text)
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=result_text,
            )
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=str(e),
            )

    def _get_alternative_agents(self) -> list[str]:
        return ["web_search_agent"]
```

**Provider-specific tools** — use flags on `LLMRequest`, never inject `types.Tool(...)` from agent or factory:
- `use_code_execution=True` — Gemini sandbox Python execution (`ComputeAgent` reference)
- `use_grounding=True` — Google Search grounding (`WebSearchAgent` reference)

### Step 5 — `src/composition/user_agent_factory.py`

Four touch points (search for an existing agent like `compute_agent` and mirror the pattern):

```python
# 1. Imports at top
from ..infrastructure.agent_config import FOO as FOO_CFG
from ..agents.foo_agent import FooAgent

# 2. Inside _create_and_cache_agents():
foo_context = self.context_builder.build("foo", user_profile.config)
foo_agent = FooAgent(
    config=AgentConfig(
        agent_id=f"foo_agent_{user_id}",
        agent_type="foo",
        timeout_ms=FOO_CFG.timeout_ms,
        capabilities=["foo"],
    ),
    execution_context=foo_context,
    prompt_builder=prompt_builder,
    user_id=user_id,
)

# 3. Register
self._register_agents([..., foo_agent])

# 4. Cache + eviction
cached = {..., "foo_agent": foo_agent}
# In _evict_expired_cache: add "foo_agent" to the key tuple
```

### Step 6 — `tests/unit/agents/test_foo_agent.py`

Minimum required coverage:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.agents.foo_agent import FooAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.ports.llm_port import LLMPort


@pytest.fixture
def mock_llm():
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = MagicMock(
        text="result text",
        usage_metadata=MagicMock(total_tokens=42),
    )
    return llm


@pytest.fixture
def agent(mock_llm):
    config = AgentConfig(agent_id="foo_agent_test", agent_type="foo",
                         timeout_ms=30000, capabilities=["foo"])
    ctx = MagicMock(provider=mock_llm, model_name="gemini-flash")
    return FooAgent(config=config, execution_context=ctx)


def make_message(query="test query"):
    return AgentMessage(
        task_id="t1", sender="coordinator", recipient="foo_agent",
        intent=AgentIntent.QUERY, payload={"query": query},
        context={"user_id": "u1", "account_id": "a1"},
    )


async def test_can_handle_correct(agent):
    assert await agent.can_handle(make_message()) is True

async def test_can_handle_wrong_intent(agent):
    msg = make_message()
    msg.intent = AgentIntent.INFORM
    assert await agent.can_handle(msg) is False

async def test_can_handle_empty_query(agent):
    assert await agent.can_handle(make_message(query="")) is False

async def test_execute_happy_path(agent, mock_llm):
    response = await agent.execute(make_message())
    assert response.status == AgentStatus.SUCCESS
    assert response.result == "result text"
    mock_llm.generate_content.assert_called_once()

async def test_execute_empty_query(agent):
    response = await agent.execute(make_message(query=""))
    assert response.status == AgentStatus.FAILED

async def test_execute_llm_exception(agent, mock_llm):
    mock_llm.generate_content.side_effect = Exception("LLM error")
    response = await agent.execute(make_message())
    assert response.status == AgentStatus.FAILED
```

Run after:
```bash
make test-unit
```

---

## Phase 2 — Prompt Work

### Step 7 — Download real examples

Before writing anything, download existing agents as reference:

```bash
# Reference token (cognitive_process class)
python firestore_utils/download.py development_domain_prompt_tokens_v3_system COMPUTE_COGNITIVE_PROCESS

# Reference blueprint (simple, 1 class)
python firestore_utils/download.py development_domain_prompt_blueprints_v3 compute_agent_v1 --format json

# Reference profile (simple, 1 token)
python firestore_utils/download.py development_domain_prompt_profiles_v3 compute --format json

# If your agent is more complex (multiple token classes):
python firestore_utils/download.py development_domain_prompt_blueprints_v3 emailsearch_agent_v1 --format json
python firestore_utils/download.py development_domain_prompt_profiles_v3 email_search --format json
```

Downloaded files land in `firestore_utils/downloads/`. Use them as structural templates.

### Step 8 — Create prompt files

**Token** — `firestore_utils/uploads/COGNITIVE_PROCESS_FOO.groovy` (human-readable source):

```groovy
identity: "You are a foo specialist in a multi-agent network. ..."

capability: "You ..."

rules: [
    "Rule 1.",
    "Rule 2.",
]

failure_protocol: "If the task cannot be completed: ..."

anti_patterns: [
    "Do NOT ...",
]
```

**Token** — `firestore_utils/uploads/COGNITIVE_PROCESS_FOO.json` (Firestore upload):

```json
{
  "token_id": "COGNITIVE_PROCESS_FOO",
  "category": "cognitive_process",
  "class": "cognitive_process",
  "content": "<paste .groovy content here as a single string>",
  "metadata": {
    "description": "FooAgent — identity and behavior",
    "override_by": ["SYSTEM", "AGENT"]
  }
}
```

**Blueprint** — `firestore_utils/uploads/foo_agent_v1.json`:

```json
{
  "blueprint_id": "foo_agent_v1",
  "outer_class": "FooAgent extends Agent",
  "class_order": ["cognitive_process"]
}
```

Add more classes to `class_order` if you have multiple token classes (e.g. `["identity", "cognitive_process", "output_format"]`).

**Profile** — `firestore_utils/uploads/foo.json`:

```json
{
  "agent_id": "foo",
  "blueprint_id": "foo_agent_v1",
  "tokens": {
    "COGNITIVE_PROCESS_FOO": {"order": 10, "non_overridable": true}
  }
}
```

**Rules:**
- `agent_id` = document ID = `agent_type` string passed to `build_for_agent()`.
- Token `class` field must match a class name in `blueprint.class_order`.
- `non_overridable: true` = user/account overrides cannot replace this token.

### Step 9 — Upload to Firestore

> ⛔ **AI agents are forbidden from running upload commands.** Uploads connect to production
> Firestore and are destructive/irreversible. AI prepares the files and the commands below —
> **only the human owner executes them manually.** See `firestore_utils/README.md`.

**Always dev first, prod after validation.**

```bash
# Dev
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_FOO --format json
python firestore_utils/upload.py development_domain_prompt_blueprints_v3 foo_agent_v1 --format json
python firestore_utils/upload.py development_domain_prompt_profiles_v3 foo --format json

# Prod (after make test-e2e-all passes against dev)
python firestore_utils/upload.py domain_prompt_tokens_v3_system COGNITIVE_PROCESS_FOO --format json
python firestore_utils/upload.py domain_prompt_blueprints_v3 foo_agent_v1 --format json
python firestore_utils/upload.py domain_prompt_profiles_v3 foo --format json
```

> Requires `gcloud auth application-default login` if credentials are missing.

---

## Phase 3 — Protocol Tokens

### Step 10 — Update agent selection tokens in Firestore

Download current versions:

```bash
python firestore_utils/download.py development_domain_prompt_tokens_v3_system PROTOCOL_SMART_AGENT_SELECTION
python firestore_utils/download.py development_domain_prompt_tokens_v3_system PROTOCOL_QUICK_AGENT_SELECTION
```

Edit `firestore_utils/downloads/PROTOCOL_SMART_AGENT_SELECTION.groovy`. Add a section for each new intent:

```groovy
# Inside the existing protocols {} block, add:
foo: {
    when: "User asks for ... / needs ...",
    how: "Pass the task verbatim as query. The agent is self-contained.",
    anti_patterns: [
        "Do not use for ... (use search_web instead)",
        "Do not use when ... is needed",
    ]
}
```

Upload back (**human only**):

```bash
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system PROTOCOL_SMART_AGENT_SELECTION
# If Quick should also call this agent:
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system PROTOCOL_QUICK_AGENT_SELECTION
```

---

## Phase 4 — Verification

### Step 11 — Test

```bash
# Unit tests — must all pass
make test-unit

# E2E — verify Quick and Smart delegate correctly
make test-e2e-all
```

For manual spot-check — fire a real message in Slack/Telegram that should trigger the new intent. Check logs:
- `_on_agent_start` → agent received the delegation
- `_on_agent_success` → result returned
- No circuit breaker trips

---

## Phase 5 — Deploy

### Step 12 — Deploy and validate

```bash
# Deploy to dev environment
make deploy-dev

# After smoke test in dev Slack:
make deploy
```

---

## Quick Reference — File Checklist

```
Code (6 files):
  src/infrastructure/agent_manifest.py     Intent + AgentDescriptor + ALL_DESCRIPTORS
  src/infrastructure/agent_config.py       @dataclass config + singleton
  src/services/agent_context_builder.py    "foo" entry in STRATEGIES
  src/agents/foo_agent.py                  FooAgent class
  src/composition/user_agent_factory.py    import + instantiate + register + cache
  tests/unit/agents/test_foo_agent.py      6 minimum tests

Prompt files (4 files):
  firestore_utils/uploads/COGNITIVE_PROCESS_FOO.groovy   human-readable source
  firestore_utils/uploads/COGNITIVE_PROCESS_FOO.json     token upload
  firestore_utils/uploads/foo_agent_v1.json              blueprint upload
  firestore_utils/uploads/foo.json                       profile upload

Firestore token updates (manual download → edit → upload):
  PROTOCOL_SMART_AGENT_SELECTION           add when/how/anti_patterns for each new intent
  PROTOCOL_QUICK_AGENT_SELECTION           if Quick should also call this agent
```

---

## Common Pitfalls

| Symptom | Likely cause |
|---------|-------------|
| Agent never called by Quick/Smart | `internal=True` set accidentally; or token upload missing |
| PromptBuilder returns empty string | Profile document ID ≠ `agent_type`; or blueprint/profile not uploaded |
| `types.Tool` import in agent/factory | Wrong pattern — use `LLMRequest(use_code_execution=True)` instead |
| Test fails: wrong method on mock | Always use `AsyncMock(spec=LLMPort)` — catches typos at test time |
| Agent called but wrong model/tier | Check `AgentProviderStrategy.STRATEGIES["foo"]` entry |
| `gcloud` hangs on upload | Run `gcloud auth application-default login` first |
| Upload says "Token v3 updated content only" | Expected for `.groovy` uploads; use `--format json` for full-doc uploads |

---

## Cross-References

- [Agent Registry Building Block](../05_building_blocks/agent_registry/README.md) — registry mechanics, §9 step details
- [Multi-Agent System](../05_building_blocks/multi_agent_system/README.md) — agent categories overview
- [`COMPUTE_AGENT_RFC.md`](../10_rfcs/COMPUTE_AGENT_RFC.md) — canonical reference for a simple SYNC specialist
- [`firestore_utils/README.md`](../../firestore_utils/README.md) — download/upload commands reference
- `CLAUDE.md` — "Adding a New Specialist Agent" links here as the authoritative reference
