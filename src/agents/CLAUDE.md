# Agents

Multi-agent system with ActorModel-like coordination.

## Structure

- `base_agent.py` — ABC. All agents inherit `BaseAgent`.
- `core/` — business agents: `RouterAgent`, `QuickResponseAgent`, `SmartResponseAgent`.
- `infrastructure/` — system agents: `BillingAgent`, `LoggerAgent`.
- `prompts/` — prompt templates (`.prompt`, `.groovy`).

## Creating a New Agent

1. Inherit `BaseAgent`, implement `can_handle()` and `execute()`.
2. Dependencies — via constructor (LLMService, SessionStore, PromptBuilder).
3. Return `AgentResponse.success()` / `AgentResponse.failure()`.
4. Register in `main.py` via `coordinator.register_agent()`.

```python
class MyAgent(BaseAgent):
    async def can_handle(self, message: AgentMessage) -> bool:
        return "my_capability" in message.payload.get("needs_tools", [])

    async def execute(self, message: AgentMessage) -> AgentResponse:
        # Load history via self._load_conversation_context(...)
        # Call LLM via self._llm.generate_content(...)
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result=...)
```

## Important

- Agents do NOT access the database directly — only through services/ports.
- Prompts in `prompts/`, do not hardcode in code.
- CircuitBreaker is built into BaseAgent — do not duplicate.
- AgentExecutionContext contains model_name, tier, provider — the agent does not select the model itself.
