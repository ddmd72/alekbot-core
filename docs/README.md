# Alek-Core Documentation

Welcome to the documentation for **Alek-Core**, a Sovereign Exocortex built on Clean Architecture principles.

## 🚀 Getting Started!

If you are new to the project, please follow this reading order:

1.  **[Essential Reading](./ESSENTIAL_READING.md)** — The 25-minute onboarding guide for AI and humans.
2.  **[01 Introduction](./01_introduction/README.md)** — Project goals, stakeholders, and core terminology.
3.  **[04 Solution Strategy](./04_solution_strategy/README.md)** — High-level architecture and target state.
4.  **[Installation Guide](./guides/INSTALLATION.md)** — How to set up your local development environment.

---

## 📖 Arc42 Architecture Documentation

We follow the **Arc42** standard for architectural documentation, organized into the following sections:

### [01 Introduction](./01_introduction/README.md)

Project goals, business requirements, and glossary.

### [02 Constraints](./02_constraints/README.md)

Technical, organizational, and architectural constraints.

### [03 Context & Scope](./03_context/README.md)

System boundaries, external integrations (Slack, Telegram), and personas.

### [04 Solution Strategy](./04_solution_strategy/README.md)

The big picture: [Target Architecture](./04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md) and milestones.

### [05 Building Blocks](./05_building_blocks/README.md)

Detailed specifications of implemented subsystems:

- **[Multi-Agent System](./05_building_blocks/multi_agent_system/README.md)** — Actor Model & ACP.
- **[Hybrid Router](./05_building_blocks/hybrid_router/README.md)** — Intent classification & triage.
- **[Sliding Window Consolidation](./05_building_blocks/sliding_window_consolidation/README.md)** — Memory pipeline.
- **[Prompt Design System v3](./05_building_blocks/prompt_design_system_v3/README.md)** — Token-based assembly.
- **[Security Validation](./05_building_blocks/security_validation/README.md)** — 5-layer defense.
- **[Telegram Integration](./05_building_blocks/telegram_integration/README.md)** — Webhook adapter.
- **[Slack Dual Mode](./05_building_blocks/slack_dual_mode/README.md)** — Socket & HTTP adapters.
- **[Search Enrichment](./05_building_blocks/search_enrichment/README.md)** — Multi-vector RRF search.
- **[Biographical Context Cache](./05_building_blocks/biographical_context_cache/README.md)** — High-speed retrieval.
- **[OAuth Multi-Tenant](./05_building_blocks/oauth_multi_tenant/README.md)** — Identity & IAM.
- **[User Cabinet](./05_building_blocks/user_cabinet/README.md)** — Self-service portal: platform linking, facts browser, semantic search.

### [06 Runtime View](./06_runtime/README.md)

Dynamic behavior: message flow, agent orchestration, and consolidation process.

- **[API Reference](./06_runtime/API_REFERENCE.md)** — All HTTP endpoints in one place (auth, cabinet, facts).

### [07 Deployment](./07_deployment/README.md)

Physical topology, GCP infrastructure, and CI/CD pipeline.

### [08 Concepts](./08_concepts/README.md)

Cross-cutting concerns and deep dives:

- **[Multi-Vector RRF Search](./08_concepts/multi_vector_rrf_search.md)** — Retrieval philosophy.
- **[Prompt v3 Guide](./08_concepts/prompt_v3_complete_guide.md)** — Practical assembly reference.
- **[Provider Resolution](./08_concepts/provider_resolution_guide.md)** — LLM tier management.
- **[User Management](./08_concepts/user_management_complete_guide.md)** — Onboarding & linking.

### [09 Decisions (ADRs)](./09_decisions/README.md)

Architecture Decision Records for critical design choices.

### [10 RFCs](./10_rfcs/README.md)

Active proposals for new features and refactorings.

### [11 Quality](./11_quality/README.md)

Quality goals, testing strategy, and metrics.

### [12 Risks](./12_risks/README.md)

Risk management and [Implementation Roadmap](./12_risks/IMPLEMENTATION_ROADMAP.md).

---

## 🛠️ Operational Guides

- **[Operations](./guides/OPERATIONS.md)** — Daily tasks, memory management, and commands.
- **[Slack Setup](./guides/SLACK_SETUP.md)** — Configuring the Slack integration.
- **[Observability](./guides/OBSERVABILITY_LOGS_GUIDE.md)** — Working with logs and traces.
- **[Documentation](./guides/DOCUMENTATION.md)** — How to maintain this knowledge base.

---

## 🤖 AI Protocols

- **[AI Development Culture](./ai/AI_DEVELOPMENT_CULTURE.md)** — Rules for AI-assisted development.
- **[Documentation Protocol](./ai/DOCUMENTATION_PROTOCOL.md)** — Mandatory rules for doc work.

---

_Alek-Core: Toward a Sovereign Exocortex_
