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
    eager=True,                 # True = created on session start; False = created on first delegation
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

**When to use `eager=False`:** agents called on <10% of requests (document generation, deep
research, file management). The descriptor is still registered at startup — intents appear in
LLM tool lists immediately. The agent instance is created on first delegation via
`AgentFactoryPort`. See [Agent Registry §3.5](../05_building_blocks/agent_registry/README.md).

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
        prompt_builder: PromptBuilderPort,
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

        try:
            system_prompt = await self.prompt_builder.build_for_agent("foo", self.user_id)
        except Exception as e:
            self._on_agent_error(e, "prompt_builder")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"PromptBuilder failed: {e}",
            )

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

### Agents with Specialist Delegation (Tool Calling)

If your agent needs to call other specialists (search_memory, search_web, open_file, etc.),
use `DelegationEngine` instead of building a custom loop. This is the same engine used by
Quick, Smart, and DomainResearcher agents.

1. Add `allowed_intents` to your `AgentDescriptor` in `agent_manifest.py`:
   ```python
   allowed_intents=frozenset({Intent.SEARCH_MEMORY, Intent.OPEN_FILE}),
   ```
2. Add `max_delegation_turns` to your agent config in `agent_config.py`.
3. Set `_descriptor` class attribute on your agent.
4. In `execute()`, build tool declarations and use the engine:
   ```python
   from ..infrastructure.delegation_engine import DelegationEngine

   tools = None
   if self.coordinator:
       available = self.coordinator.get_available_intents_for(self._descriptor)
       if available:
           tools = [self._build_delegate_tool_declaration(available)]

   base_request = LLMRequest(model_name=..., system_instruction=..., messages=..., tools=tools, ...)
   engine = DelegationEngine(self.coordinator)
   result = await engine.execute(
       call_llm=self._call_llm, base_request=base_request,
       context=message.context,  # dict from AgentMessage, spread as **context by engine
       max_turns=MY_AGENT.max_delegation_turns,
   )
   # result.text is the final LLM response
   # result.failed indicates max_turns exhausted
   ```

The engine handles: multi-turn iteration, memory-first parallel dispatch, history management,
`raw_content` preservation, `file_data` forwarding. Your agent only builds the `LLMRequest`
and post-processes the `DelegationResult`.

Reference: `DomainResearcherAgent` (`src/agents/domain_researcher_agent.py`).

**Provider-specific tools** — use flags on `LLMRequest`, never inject `types.Tool(...)` from agent or factory:
- `use_code_execution=True` — Gemini sandbox Python execution (`ComputeAgent` reference)
- `use_grounding=True` — Google Search grounding (`WebSearchAgent` reference)

### Structured JSON Output — `response_mime_type` and `response_schema`

Pick exactly one of three modes. Mixing them incorrectly causes silent failures.

#### Mode 1 — Single-pass JSON (no custom tools)

Use when the agent makes one LLM call and expects a structured JSON response back.

```python
_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["status", "summary", "data"],
    "properties": {
        "status":  {"type": "string"},
        "summary": {"type": "string"},
        "data":    {"type": "object"},   # ← flat, no nested properties
    },
}

request = LLMRequest(
    ...
    response_mime_type="application/json",
    response_schema=self._RESPONSE_SCHEMA,
)
```

**Gemini:** GeminiAdapter passes `response_mime_type` directly and routes the dict schema to
`response_json_schema` (standard JSON Schema, SDK 1.64+). Both enforce JSON output.

**Claude:** `response_mime_type` is **silently ignored**. `response_schema` is translated to
`output_config={"format":{"type":"json_schema","schema":...}}` (GA structured outputs API) —
JSON is returned directly in a text block. Make sure the blueprint includes an OUTPUT_FORMAT class.

**Gemini nesting limit:** Gemini returns `400 INVALID_ARGUMENT` if the schema nests deeper
than ~2 levels. Declare any field that is itself an object as `{"type": "object"}` with no
further `properties`. Never go deeper in the schema definition.

#### Mode 2 — Tool-using agent (custom `tools=`)

Use when the agent has its own tool loop (e.g. `generate_docx`, code execution).
Do **not** set `response_mime_type` or `response_schema`.

```python
request = LLMRequest(
    ...
    tools=MY_TOOL_DEFINITIONS,   # no response_mime_type, no response_schema
)
```

**Gemini:** combining `response_mime_type` with custom tools causes silent empty responses
or API errors — they are mutually exclusive in Gemini.

**Claude:** `response_schema` is always translated to `output_config.format` (GA API),
regardless of whether `tools` are also present. No `respond` tool is injected. For specialist
agents with their own tools that don't need structured JSON output, omit `response_schema` — output
format is enforced by the OUTPUT_FORMAT prompt token.

#### Mode 3 — Free-text output

No `response_mime_type`, no `response_schema`, no custom tools. Agent returns natural language.
Output format is handled entirely by the prompt. Use for most simple agents.

#### Adapter behaviour at a glance

| Field | GeminiAdapter | ClaudeAdapter |
|-------|--------------|---------------|
| `response_mime_type` | Passed to `GenerateContentConfig` directly | **Ignored entirely** |
| `response_schema: dict` | Routed to `response_json_schema` | Translated to `output_config.format` (GA structured outputs) — works with or without `tools` |

**PromptBuilder is mandatory** for all agents. If `build_for_agent()` fails, return
`AgentResponse.failure()` — do not fall back to an empty string or a hardcoded prompt.
Without the correct tokens (OUTPUT_FORMAT, cognitive process), the LLM will not produce
the expected structure. Fail fast, don't degrade silently.

### Step 5 — `src/composition/user_agent_factory.py`

Choose **eager** or **lazy** based on the `eager` flag you set in Step 1.

#### If `eager=True` (default) — 4 touch points

Mirror an existing eager agent like `compute_agent`:

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

#### If `eager=False` — 3 touch points

Mirror an existing lazy agent like `_build_pdf_generator`:

```python
# 1. Imports at top (same as eager)
from ..infrastructure.agent_config import FOO as FOO_CFG
from ..agents.foo_agent import FooAgent

# 2. Add builder method (uses typed _UserContext, not untyped dict):
def _build_foo(self, user_id: str, ctx: _UserContext) -> FooAgent:
    execution_context = self.context_builder.build("foo", ctx.user_profile.config)
    return FooAgent(
        config=AgentConfig(
            agent_id=f"foo_agent_{user_id}",
            agent_type="foo",
            timeout_ms=FOO_CFG.timeout_ms,
            capabilities=["foo"],
        ),
        execution_context=execution_context,
        prompt_builder=ctx.prompt_builder,
        user_id=user_id,
    )

# 3. Register in dispatch tables:
_LAZY_BUILDERS = {..., "foo": _build_foo}
_LAZY_AGENT_IDS = {..., "foo": "foo_agent"}
```

No cache dict or eviction changes needed — lazy agents are tracked automatically via
`_lazy_agent_ids`. The coordinator triggers creation on first delegation via `AgentFactoryPort`.

### Step 6 — `tests/unit/agents/test_foo_agent.py`

Minimum required coverage:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.agents.foo_agent import FooAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.ports.llm_port import LLMPort
from src.ports.prompt_builder_port import PromptBuilderPort


@pytest.fixture
def mock_llm():
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = MagicMock(
        text="result text",
        usage_metadata=MagicMock(total_tokens=42),
    )
    return llm


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a foo specialist."
    return pb


@pytest.fixture
def agent(mock_llm, mock_prompt_builder):
    config = AgentConfig(agent_id="foo_agent_test", agent_type="foo",
                         timeout_ms=30000, capabilities=["foo"])
    ctx = MagicMock(provider=mock_llm, model_name="gemini-flash")
    return FooAgent(config=config, execution_context=ctx,
                    prompt_builder=mock_prompt_builder)


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
  src/infrastructure/agent_manifest.py     Intent + AgentDescriptor (+ eager flag) + ALL_DESCRIPTORS
  src/infrastructure/agent_config.py       @dataclass config + singleton
  src/services/agent_context_builder.py    "foo" entry in STRATEGIES
  src/agents/foo_agent.py                  FooAgent class
  src/composition/user_agent_factory.py    eager: instantiate + register + cache
                                           lazy:  _build_foo + _LAZY_BUILDERS + _LAZY_AGENT_IDS
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
