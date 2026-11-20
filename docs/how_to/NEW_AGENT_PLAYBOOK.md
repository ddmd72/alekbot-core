<!-- living-doc
covers: none
-->

# New Specialist Agent: Step-by-Step Playbook

**Scope:** a SYNC specialist (one or more intents, one LLM call or one delegation loop, result returned inline).
An ASYNC agent (Cloud Tasks + polling) or an agent with a service layer: read its closest RFC first
(`docs/10_rfcs/DOCUMENT_DELIVERY_RFC.md`, `docs/10_rfcs/IMAGE_GENERATION_RFC.md`). Registry mechanics and the runtime
flows: [Agent Registry](../05_building_blocks/agent_registry/README.md). Quick and Smart: [Hybrid Router](../05_building_blocks/hybrid_router/README.md).
Rules that hold for every agent: `src/agents/CLAUDE.md`.

The steps run in order. Every change reaches `main` through a pull request; the prompt upload (Step 9) is the owner's.

---

## Phase 0: Decisions before writing code

| Question | Options |
|----------|---------|
| The agent's single responsibility | One sentence |
| How many intents | 1, or 2-4 typed variants |
| What goes in `payload.query` | A natural-language string |
| Which provider | `allowed_providers` is a lock: narrow it only if the agent needs a provider-specific feature |
| Which PerformanceTier? | ECO / BALANCED / PERFORMANCE; the answer is written down in Step 3, not left to a default |
| Eager or lazy | Lazy for an agent called on a small share of turns; eager otherwise (Step 1) |
| Service layer, new ports | Only for a DB, an external API or multi-step logic; a port only with 2+ implementations or a boundary to test |
| Output shape | Free text, single-pass JSON, or a tool loop (Step 4, "Output modes") |
| Does it make an LLM call | If not (like `help`, `file_management`), Step 3 differs |

---

## Phase 1: Code

### Step 1: manifest (`src/infrastructure/agent_manifest.py`)

Add an `Intent` constant in `agent_manifest.py::Intent` and an `AgentDescriptor` (`src/infrastructure/agent_registry.py::AgentDescriptor`),
then append it to `agent_manifest.py::ALL_DESCRIPTORS`. Mirror `COMPUTE` for a plain specialist.

- `agent_type` is the profile document id in Step 8 and the key in Steps 3 and 5.
- `capability_descriptions` is what the orchestrator LLM reads to choose the intent: say when to call it and what it
  cannot do.
- `internal=True` hides the intents from every caller whose `allowed_intents` is `None`; a caller that names them in an
  explicit allowlist still sees them (`decisions/allowlist_names_internal_intent.md`). Set it by accident and the agent is never called.
- `eager=False`: the descriptor is registered at startup, so the intents are in the tool lists at once, but the agent
  instance is built on the first delegation (`AgentCoordinator._ensure_lazy_agent` -> `AgentFactoryPort.create_agent_on_demand`).
- Quick and Smart both carry `allowed_intents=None` (`QUICK_RESPONSE`, `SMART_RESPONSE`), so a non-internal intent reaches
  both with no change to either. The prompt side is Step 10.

### Step 2: tunables (`src/infrastructure/agent_config.py`)

Add a typed dataclass and a module-level instance (mirror `ComputeAgentConfig` / `COMPUTE`): `timeout_ms`, plus whatever
the agent reads as class constants. Provider choice is not here (Step 3).

### Step 3: provider and tier

Two separate lookups, both silent when skipped:

1. **Provider.** Add an entry to `src/services/agent_context_builder.py::AgentProviderStrategy.STRATEGIES`
   (`default_provider`, `allowed_providers`, `required_capabilities`, `fallback`). An agent type with no entry resolves
   to the `quick` strategy (`AgentProviderStrategy.get_strategy`), so it silently runs on Quick's provider.
2. **Tier.** `UserBotConfig.get_tier_for_agent` falls through to the user's `default_tier` when the type is not in
   `src/domain/user.py::_DEFAULT_AGENT_TIERS`, with no error and no log line. Add the Phase 0 answer there with a
   one-line reason. Three agents shipped without it (`decisions/agent_tier_default_enforcement.md`).
   An agent with no LLM call goes into `_ZERO_LLM_AGENT_TYPES` in `tests/unit/domain/test_user.py` instead, with a reason.
   `test_every_llm_agent_has_a_default_tier` enumerates `ALL_DESCRIPTORS` and fails on a type that is in neither.

A JSON-output agent must be locked to providers that support its output mode (`src/adapters/CLAUDE.md`, "Agent Output
Format Standards"). An agent that deliberately has no cross-provider fallback sets `fallback: None` and goes on the
reasoned allowlist of `tests/unit/agents/test_agents_set_execution_context.py` (Step 5).

### Step 4: the agent class (`foo_agent.py` in `src/agents/`)

A `BaseAgent` subclass implementing `can_handle` and `execute`; the constructor takes `config`, `execution_context`,
`prompt_builder`, `user_id`. Mirror `DomainResearcherAgent` for the shape that fails correctly. Rules the code and tests enforce:

- **Hand the context to the base class:** `self._set_execution_context(execution_context)` in the constructor. Without
  it `BaseAgent._call_llm` has no breaker accounting and no cross-provider failover, and nothing errors
  (`tests/unit/agents/test_agents_set_execution_context.py`, which lists the deliberate exceptions).
- **PromptBuilder is mandatory and fails fast.** If `PromptBuilderPort.build_for_agent(agent_type, ...)` raises, return
  `AgentResponse.failure(...)`; never fall back to an empty or inline prompt. `ComputeAgent` and `DeepResearchAgent`
  still degrade to an empty prompt on a builder failure: do not copy that part.
- Call the model only through `BaseAgent._call_llm`; log through `_on_agent_start` / `_on_agent_success` / `_on_agent_error`.
- Provider-specific tools are flags on `LLMRequest` (`use_code_execution`, `use_grounding`); never import `types.Tool` in
  an agent or the factory.
- An agent instance serves concurrent turns: instance state written outside `__init__` needs a line in
  `tests/unit/agents/agent_instance_writes_allowlist.txt` (`tests/unit/agents/test_agent_request_state_meta.py`).

**Output modes.** Pick one; the adapter behaviour per provider is in `src/adapters/CLAUDE.md` ("Agent Output Format Standards"),
not repeated here.
- *Mode 1, single-pass JSON, no tools:* `response_mime_type` and `response_schema`, an `OUTPUT_FORMAT_{AGENT}` token in the
  blueprint, `_parse_response()` with `json.loads` and retry on invalid output. Describe every schema field.
- *Mode 2, own tool loop:* `tools=` and `response_schema` only if needed, never `response_mime_type` (Gemini cannot
  combine it with function calling; Claude ignores it).
- *Mode 3, free text:* neither; the prompt carries the format.

**Delegating to other specialists.** Use `src/infrastructure/delegation_engine.py::DelegationEngine`, not a hand-rolled loop:
1. `allowed_intents=frozenset({...})` on the descriptor (it may name internal intents) and `max_delegation_turns` in the agent config.
2. A class attribute `_descriptor` set to that descriptor.
3. In `execute()`: `self.coordinator.get_available_intents_for(self._descriptor)`, `self._build_delegate_tool_declaration(...)`,
   then `DelegationEngine(self.coordinator).execute(call_llm=self._call_llm, base_request=..., context=..., max_turns=..., calling_agent_id=self.agent_id)`.
   `DelegationResult.failed` means the turns ran out.

### Step 5: factory wiring (`src/composition/user_agent_factory.py`)

Build the context with `self.context_builder.build("foo", user_profile.config)`, then follow the eager or lazy path.

**Eager** (mirror `compute_agent` in `UserAgentFactory._create_and_cache_agents`): construct the agent, add it to
`agents_to_register`, add it to the `cached` dict, and add its key to `UserAgentFactory._EAGER_AGENT_KEYS`, which is what
eviction uses to unregister it. A key missing there leaves a stale registered agent after `$admin_cache_reset`.
Runtime binding (`bind_runtime`) happens before `_register_agents`: an eager agent must be complete the moment it is
routable (`tests/unit/composition/test_agent_registration_window.py`).

**Lazy** (mirror `_build_pdf_generator`): add a `_build_foo(self, user_id, ctx)` method and one entry each in
`UserAgentFactory._LAZY_BUILDERS` and `UserAgentFactory._LAZY_AGENT_IDS`. Creation, runtime binding, registration and
eviction tracking (`cached["_lazy_agent_ids"]`) are done by `create_agent_on_demand`.

**Wiring guards.** The production graph walk (`tests/CLAUDE.md`, "The production graph") builds one user's eager and every
lazy agent. The two ledgers `tests/unit/composition/wiring_none_ledger.txt` and `wiring_instance_ledger.txt` fail when a
new agent leaves an attribute `None` or a new class adds an instance; wire the dependency in `build_application_graph`,
and treat a ledger line as a reviewer's decision. They are `slow` tests (`RUN_SLOW_TESTS=1`).

### Step 6: tests

One primary test file per agent, `test_foo_agent.py` in `tests/unit/agents/` (where a test goes: `tests/CLAUDE.md`, "Where a test
goes"). Cover `can_handle`, the happy path, an empty query, an LLM exception, and a prompt-builder failure returning
`AgentResponse.failure`. Mirror `tests/unit/agents/test_compute_agent.py`. Use fakes that evaluate over call assertions;
a new descriptor needs no test of its own beyond the guards above.

### Step 6b: user-facing text

If the agent adds something the user can ask for, update `src/utils/capabilities.py::CAPABILITIES_TEXT` (what the
`user_guide` intent returns) and add the agent's row to the roster in `src/agents/CLAUDE.md`.

---

## Phase 2: Prompt work

The prompt layer (tokens, blueprints, profiles) lives in Firestore; `prompts_snapshot/` is its git mirror
(`prompts_snapshot/README.md`). Agents never carry prompt text. Assembly rules and token classes:
[Prompt Design System](../05_building_blocks/prompt_design_system_v3/README.md).

### Step 7: read a reference

Open the mirror files of a similar agent: `prompts_snapshot/profiles/compute.yaml`,
`prompts_snapshot/blueprints/compute_agent_v1.yaml`, `prompts_snapshot/tokens/system/COMPUTE_COGNITIVE_PROCESS.groovy`
(a larger one: `email_search`). The legacy `download.py` is not part of this flow.

### Step 8: create the mirror files

- Token `<TOKEN>.groovy` in `prompts_snapshot/tokens/system/`: frontmatter plus the groovy body; its `class` must be a class in
  the blueprint's `class_order`. The prompt text states what its output is for before the rules (root `CLAUDE.md`, "Code Conventions").
- Blueprint `foo_agent_v1.yaml` in `prompts_snapshot/blueprints/`: `blueprint_id`, `outer_class`, `class_order`.
- Profile `foo.yaml` in `prompts_snapshot/profiles/`: the file name and `agent_id` equal the `agent_type` passed to
  `build_for_agent`; `tokens` maps token ids to `order` and `non_overridable`.
- A structured-output agent also gets its `OUTPUT_FORMAT_{AGENT}` token, in its own class.

Two failures are silent: a missing profile document yields an empty profile (no tokens, no error;
`FirestoreAgentProfileRepository.get_agent_profile`), and a token id absent from the token repository is skipped with a
warning (`PromptAssemblyService`). Either way the agent runs on a shorter prompt.

### Step 9: upload (owner only)

AI agents prepare the files and the commands and never run `--apply`; the owner does. Back up the prompt layer first
(`firestore_utils/backup_prompt_layer.py`, output into `scripts/memory/`), then:

```bash
python firestore_utils/snapshot_pull.py --check                 # drift check
python firestore_utils/snapshot_upload.py <files>               # dry run: per-field diff, no write
python firestore_utils/snapshot_upload.py --apply <files>       # owner only, interactive
python firestore_utils/snapshot_pull.py                         # then commit the mirror
```

A document that does not exist yet is created. Credentials: `gcloud auth application-default login`.

---

## Phase 3: Protocol token

### Step 10: the agent-selection token

Quick and Smart share one token, `PROTOCOL_AGENT_SELECTION` (both profiles assign it). Without a section for the new
intent the orchestrators see only its `capability_descriptions`. Edit the mirror
`prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy` and, because tests pin the two equal
(`tests/unit/firestore_utils/test_protocol_drive_specialist.py`), its draft
`firestore_utils/uploads/PROTOCOL_AGENT_SELECTION.json`. Add `when`, `how` and `anti_patterns` for the intent in the
existing structure, in the style of its neighbours. Upload with the Step 9 commands; one upload changes Quick and Smart together.

---

## Phase 4: Verification

### Step 11: test

Run the targeted tests for the files you touched, then the guards named above (execution-context test, the tier test,
the ledgers with `RUN_SLOW_TESTS=1`). The one batch run per push and the commands' meaning: root `CLAUDE.md`, "Commands".
Then a manual check: a message that should trigger the intent, and in the logs `_on_agent_start`, `_on_agent_success`, no circuit-breaker trips.

---

## Phase 5: Deploy

### Step 12: deploy

The prompt upload (Step 9) ships separately from the code; deploy the code only after the mirror files are applied.
`make deploy` builds the working tree, not git (root `CLAUDE.md`, "Branching & Environment"). If the agent adds a
Firestore composite query, its index must be `READY` first (root `CLAUDE.md`, "Traps").

---

## Common pitfalls

| Symptom | Likely cause |
|---------|--------------|
| Agent never called by Quick or Smart | `internal=True`; or Step 10 not uploaded |
| Prompt empty or short | Profile file name or `agent_id` differs from `agent_type`; mirror not uploaded; a token id missing (Step 8) |
| Wrong model or tier | No `STRATEGIES` entry (falls to `quick`) or no `_DEFAULT_AGENT_TIERS` entry (Step 3) |
| No failover, no breaker accounting | `_set_execution_context` not called (Step 4) |
| Stale agent after `$admin_cache_reset` | Eager key missing from `_EAGER_AGENT_KEYS` (Step 5) |
| `types.Tool` import in an agent or factory | Use the `LLMRequest` flag instead (Step 4) |
| `gcloud` hangs on upload | `gcloud auth application-default login` first |
