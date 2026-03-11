# Essential Reading for AI Sessions

**Purpose:** Minimal documentation set for quick context without overload.

---

## 📖 Tier 1: Essential Reading (ALWAYS read at the start of a session)

These 6 documents provide **80% of necessary context** in **20% of the time**:

### 1. 📌 Documentation Entry Map

**File:** `docs/ESSENTIAL_READING.md`  
**Purpose:** Documentation map and rules for working with Tier 1-3

### 2. 🗺️ Navigation & Overview

**File:** `docs/README.md`  
**Purpose:** Entry point, complete documentation map, quick navigation

### 3. 🏗️ Target Architecture

**File:** `04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md`
**Purpose:** Main project blueprint, target architecture, Milestones

### 4. 📂 Current Structure

**File:** `04_solution_strategy/current_implementation/STRUCTURE.md`
**Purpose:** Current project structure, description of all components

### 5. 🤖 Development Culture

**File:** `ai/AI_DEVELOPMENT_CULTURE.md`
**Purpose:** Development rules, security, AI collaboration protocols

### 6. 🛣️ Implementation Roadmap

**File:** `12_risks/IMPLEMENTATION_ROADMAP.md`
**Purpose:** Current status, Milestones, and change history (Session Context)

---

## 📚 Tier 2: Contextual Reading (read AS NEEDED)

Load only for specific tasks:

### Building Blocks

- `docs/05_building_blocks/multi_agent_system/README.md` - Actor Model & ACP
- `docs/05_building_blocks/sliding_window_consolidation/README.md` - Memory Pipeline
- `docs/05_building_blocks/slack_dual_mode/README.md` - Platform Integration

### Operational Guides

- `docs/guides/INSTALLATION.md` - when changing dependencies
- `docs/guides/OPERATIONS.md` - when working with Makefile/deployment
- `docs/guides/SLACK_SETUP.md` - when working with Slack integration

### Planning Documents

- `docs/_project/migration/MIGRATION_PLAN.md` - when working on a specific migration/optimization
- `docs/_project/management/GIT_STRATEGY.md` - when working with branching/releases
- `docs/_project/management/CURRENT_SPRINT.md` - when working with the current sprint

### Concepts & Philosophy

- `docs/08_concepts/agent_best_practices.md` - Production-ready agent patterns
- `docs/08_concepts/groovy_prompt_pattern.md` - Prompt engineering with Groovy DSL
- `docs/08_concepts/agent_business_logic.md` - Multi-agent workflows
- `docs/_project/archive/concepts_legacy/BELIEF_SYSTEM_MANIFESTO.md` - Legacy memory philosophy
- `docs/_project/archive/concepts_legacy/RAG_MANIFESTO.md` - Legacy RAG architecture

---

## 🗄️ Tier 3: Archive (DO NOT read)

**Directory:** `docs/archive/`
**Purpose:** Historical context, outdated versions
**Rule:** Load ONLY if explicitly requested by the user

---

## ⚡ Quick Start Protocol

### On Deep Context Initialization:

```
1. Read docs/ESSENTIAL_READING.md (this file)
2. Load all Tier 1 documents (6 files)
3. Skim STRUCTURE.md to understand the project
4. Read SESSION CONTEXT in IMPLEMENTATION_ROADMAP.md
5. Confirm readiness
```

### On new task within a session:

```
1. Recursive Check (Stop, Align, Declare)
2. Load Tier 2 documents if needed
3. Proceed with task
```

### On task continuation:

```
1. Continue from context
2. No re-reading needed
```

---

## 📊 Estimated Reading Time

| Tier   | Files | Tokens (approx) | Time    |
| ------ | ----- | --------------- | ------- |
| Tier 1 | 6     | ~18,000         | 2-3 min |
| Tier 2 | ~15   | ~30,000         | 5-7 min |
| Tier 3 | N/A   | Archive         | N/A     |

**Total Essential Reading:** 18K tokens (~10% of 200K context window)

---

---

## 🔄 Migration Note (2026-01-30)

The documentation structure has been migrated to **Arc42** standard:

- Legacy `docs/architecture/` → `docs/_project/archive/architecture_legacy/`
- Legacy `docs/concepts/` → `docs/_project/archive/concepts_legacy/`
- New Arc42 structure: `docs/01_introduction/` through `docs/12_risks/`

See: [\_project/migration/MIGRATION_PLAN.md](./_project/migration/MIGRATION_PLAN.md) for details.

---

_Last Updated: 2026-02-10_
