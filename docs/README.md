# Alek-Core Documentation

Documentation for **Alek-Core**, a personal exocortex built on hexagonal (ports & adapters)
architecture. Organised with the [arc42](https://arc42.org) template.

## 🚀 Getting Started

1. **[Essential Reading](./ESSENTIAL_READING.md)** — short onboarding for AI and humans.
2. **[Bootstrap](../BOOTSTRAP.md)** — from-scratch deployment runbook (local + GCP).
3. **[01 Introduction](./01_introduction/README.md)** — goals, scope, glossary.
4. **[04 Solution Strategy](./04_solution_strategy/README.md)** — architecture and target state.

---

## 📖 Arc42 Sections

### [01 Introduction](./01_introduction/README.md)
Project goals, business requirements, and glossary.

### [02 Constraints](./02_constraints/README.md)
Technical, organizational, and architectural constraints.

### [03 Context & Scope](./03_context/README.md)
System boundaries and external integrations (Slack, Telegram).

### [04 Solution Strategy](./04_solution_strategy/README.md)
The big picture: [Target Architecture](./04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md),
[current implementation structure](./04_solution_strategy/current_implementation/STRUCTURE.md), and
[decision records](./04_solution_strategy/decisions/).

### [05 Building Blocks](./05_building_blocks/README.md)
Detailed specifications of implemented subsystems:

- **[Multi-Agent System](./05_building_blocks/multi_agent_system/README.md)** — agent network & coordination.
- **[Hybrid Router](./05_building_blocks/hybrid_router/README.md)** — LLM triage & complexity classification.
- **[Sliding Window Consolidation](./05_building_blocks/sliding_window_consolidation/README.md)** — memory pipeline.
- **[Prompt Design System](./05_building_blocks/prompt_design_system_v3/README.md)** — token-based assembly.
- **[Security Validation](./05_building_blocks/security_validation/README.md)** — input defense layers.
- **[Telegram Integration](./05_building_blocks/telegram_integration/README.md)** / **[Slack Dual Mode](./05_building_blocks/slack_dual_mode/README.md)** — channel adapters.
- **[Search Enrichment](./05_building_blocks/search_enrichment/README.md)** — multi-vector RRF search.
- **[OAuth Multi-Tenant](./05_building_blocks/oauth_multi_tenant/README.md)** — identity & IAM.
- **[Remote MCP Server](./05_building_blocks/remote_mcp_server/README.md)** — claude.ai connector.
- **[User Cabinet](./05_building_blocks/user_cabinet/README.md)** — self-service portal.

### [06 Runtime View](./06_runtime/README.md)
Dynamic behavior: message flow, agent orchestration, consolidation.
- **[API Reference](./06_runtime/API_REFERENCE.md)** — HTTP endpoints.

### [07 Deployment](./07_deployment/README.md)
GCP topology, schedulers, logging. Setup: **[Bootstrap](../BOOTSTRAP.md)**.

### [08 Concepts](./08_concepts/README.md)
Cross-cutting deep dives:
- **[Database Schema](./08_concepts/DATABASE_SCHEMA.md)** — Firestore collections & indexes.
- **[Multi-Vector RRF Search](./08_concepts/multi_vector_rrf_search.md)** — retrieval philosophy.
- **[Prompt Assembly Guide](./08_concepts/prompt_assembly_guide.md)** — token assembly (v4).
- **[Provider Resolution](./08_concepts/provider_resolution_guide.md)** — LLM tier management.
- **[User Management](./08_concepts/user_management_complete_guide.md)** — onboarding & linking.

### [09 Decisions (ADRs)](./09_decisions/README.md)
Architecture Decision Records.

### [10 RFCs](./10_rfcs/README.md)
Feature and refactoring proposals.

### [11 Quality](./11_quality/README.md)
Quality goals, testing strategy, use cases.

### [12 Risks](./12_risks/README.md)
Risk management and [Implementation Roadmap](./12_risks/IMPLEMENTATION_ROADMAP.md).

---

_Alek-Core — personal exocortex._
