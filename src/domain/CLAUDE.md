# Domain

The system core. ZERO infrastructure dependencies.

## Import Rules

Allowed: `stdlib`, `pydantic`, `dataclasses`, `enum`, `typing`.
Forbidden: everything from `adapters/`, `services/`, `config/`, `utils/`, `ports/`.

## Key Models

- `FactEntity` (BaseModel) — fact with SCD2 versioning (valid_from/valid_to/is_current).
- `FactType` — STATE, EVENT, PRINCIPLE, SYSTEM, ALERT.
- `AgentMessage` / `AgentResponse` (dataclass) — agent communication protocol.
- `AgentConfig` (dataclass) — agent config (id, type, model, timeout, circuit_breaker).
- `MessageContext` (dataclass) — platform-agnostic message context.
- `UserBotConfig` (BaseModel) — user settings (tier, provider, per-agent overrides).
- `PerformanceTier` — ECO / BALANCED / PERFORMANCE.

## Conventions

- Entities — `BaseModel` with validation.
- Value objects — `@dataclass` (immutable, no id).
- Enums — `(str, Enum)` for serialization.
- Factory methods — `AgentResponse.success()`, `AgentMessage.create()`.
