# Agents

Multi-agent system with ActorModel-like coordination.

## Structure

- `base_agent.py` — ABC. All agents inherit `BaseAgent`.
- `core/` — orchestrators: `RouterAgent`, `SmartResponseAgent`, `QuickResponseAgent` (fallback/formatter).
- `infrastructure/` — system agents: `BillingAgent`, `LoggerAgent`.
- All other files — specialist agents (see CLAUDE.md "Key Mechanisms" at repo root).

## Creating a New Agent

Follow [`docs/how_to/NEW_AGENT_PLAYBOOK.md`](../../docs/how_to/NEW_AGENT_PLAYBOOK.md) — mandatory protocol. Short version:

1. Add `Intent` constant + `AgentDescriptor` to `infrastructure/agent_manifest.py`
   (include in `ALL_DESCRIPTORS`).
2. Inherit `BaseAgent`, implement `can_handle()` and `execute()`.
   Dependencies — via constructor (LLMPort, SessionStore, PromptBuilderPort).
3. Return `AgentResponse.success()` / `AgentResponse.failure()`.
4. Wire creation in `composition/user_agent_factory.py` (eager, or `eager=False`
   for on-demand creation via `AgentFactoryPort`).
5. Update `src/utils/capabilities.py` (user-facing `get_help` reference).

## Important

- Agents do NOT access the database directly — only through services/ports.
- Prompts live in Firestore (token + blueprint via PromptBuilder). NO inline or
  fallback prompts in code — if `build_for_agent()` fails, return
  `AgentResponse.failure()`. Fail fast.
- CircuitBreaker is built into BaseAgent — do not duplicate.
- `AgentExecutionContext` contains model_name, tier, provider — the agent does not
  select the model itself (see `AgentProviderStrategy`).
- Use `_call_llm(request, turn)` from BaseAgent — it is the single billing + debug
  logging point. Never call the provider port directly.
- Multi-turn tool loops: use `infrastructure/delegation_engine.py`, do not hand-roll.
