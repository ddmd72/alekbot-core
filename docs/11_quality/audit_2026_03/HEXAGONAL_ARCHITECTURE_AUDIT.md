# Hexagonal Architecture Audit — 2026-03-09

## Scope

Full AST-based import analysis across all layers of `src/`.
208 Python files scanned. No false positives (docstrings excluded via AST parsing).

## Results

| Layer | Files | Violations | Compliance |
|-------|-------|-----------|------------|
| domain/ | 33 | 0 | 100% |
| ports/ | 46 | 0 | 100% |
| adapters/ | 61 | 0 | 100% |
| services/ | 34 | 0 | 100% |
| agents/ | 20 | 0 | 100% |
| handlers/ | 5 | **2** | 60% |
| infrastructure/ | 6 | 0 | 100% |
| config/ | 3 | 0 | 100% |
| **TOTAL** | **208** | **2** | **99.0%** |

**Score after fix: 100% (208/208)**

## Violations Found and Fixed

### handlers/ → composition/ (2 files, same pattern)

**Files:**
- `src/handlers/consolidation_handler.py:13`
- `src/handlers/conversation_handler.py:19`

**Import:** `from ..composition.user_agent_factory import UserAgentFactory`

**Root cause:** Both handlers imported `UserAgentFactory` as a runtime import despite
using it only for type annotations. The composition layer creates handlers and passes
the factory via constructor injection — importing back from composition creates a
circular architectural dependency.

**Fix:** Moved import under `TYPE_CHECKING` guard. No runtime behavior change.

**Why tests didn't catch it:** `test_handlers_layer_isolation` (REQ-ARCH-10) intentionally
did not include `src.composition` in its forbidden list. The test docstring stated:
*"they coordinate via infrastructure/ and composition/"* — treating composition as an
allowed dependency for handlers. This was an architectural blind spot.

**Test fix:** Added `src.composition` to the forbidden list in REQ-ARCH-10.

## Compliance by Hexagonal Principle

| Principle | Score | Detail |
|-----------|-------|--------|
| Domain isolation (no outward deps) | 100% | 33/33 files clean |
| Port purity (domain + stdlib only) | 100% | 46/46 files clean |
| Adapter discipline (no cross-adapter) | 100% | REQ-ARCH-23 fully respected |
| Service isolation (ports only, no adapters) | 100% | 34/34 files clean |
| Dependency direction (inward only) | 100% | After fix |
| Composition as sole wiring point | 100% | All adapter instantiation in composition/ |

## Additional Observations

### Pre-existing violation (not addressed in this audit)

`src/services/google_oauth_service.py:21` imports `aiohttp` directly — violates
REQ-ARCH-18 (no HTTP client libraries in core layers). This file is not in the
`HTTP_CLIENT_WHITELIST_FILES`. Separate fix needed.

### Import style inconsistency

77 files use absolute `src.` imports instead of relative `..` imports. Not an
architecture violation, but inconsistent. Concentrated in newer subpackages
(prompt_v3, security, email ports/adapters).

### agents/ → infrastructure/ (by design)

17 imports across agents for `agent_config` and `agent_manifest`. This is explicitly
sanctioned: agents read `AgentConfig` values as class-level constants, and `Intent`
constants + `AgentDescriptor` live in infrastructure by design.

---

## Operational Resilience Assessment

The architectural audit revealed a broader concern: **bus factor = 1**. While the
codebase is exceptionally well-structured for a solo-dev project, operational
resilience has gaps.

### What already mitigates bus factor

- **CLAUDE.md** — comprehensive project "brain dump" that enables any developer or
  AI assistant to understand architecture, rules, and patterns. This is rare for
  solo-dev projects.
- **27 AST architecture tests** — automated "fence" preventing structural degradation
  under deadline pressure.
- **Hexagonal architecture** — layers are independent. An adapter can be fixed without
  understanding consolidation logic.
- **RFCs in `docs/10_rfcs/`** — decisions documented with "why" context.
- **Decision-Making Protocol** — 4-gate system enabling AI assistants to work
  autonomously within defined rules.

### Remaining risks (prioritized by impact)

#### 1. Operational knowledge gap — HIGH

No runbook exists. Deployment procedure, rollback steps, incident response, log
access patterns — all implicit. A single-page runbook covering:
- How to deploy (dev and prod)
- How to rollback
- How to read logs and trace errors
- What to do when Cloud Tasks stall
- How to recover from OAuth credential rotation

**Effort:** ~1 hour. **Impact:** Eliminates the most critical bus factor risk.

#### 2. Dev environment bootstrap — MEDIUM

No `scripts/bootstrap.sh` or equivalent. Onboarding from clone to working dev
environment requires implicit knowledge of env vars, emulator setup, and dependency
installation.

**Effort:** ~2 hours. **Impact:** Reduces onboarding from days to minutes.

#### 3. Infrastructure as Code — MEDIUM (acknowledged tech debt)

Cloud Run, Cloud Scheduler, Cloud Tasks, OAuth redirect URIs are configured manually
in GCP Console. If the GCP project is lost, reconstruction is painful.

**Note:** This is acknowledged as conscious tech debt by the project owner. The
trade-off (speed of iteration vs. reproducibility) is reasonable for a solo-dev
project, but becomes critical if the project needs to survive a handoff.

#### 4. Credential backup — LOW-MEDIUM

Second set of keys in a secure location outside GCP Secret Manager. If GCP access
is lost, all secrets are lost with it.

### What is NOT a risk

- **Prompt tuning know-how** — this is craft knowledge built through experience. It
  cannot be transferred via documentation; it requires "feeling it from the inside."
  The Groovy-style token system (vs. raw markdown) is itself a mitigation — it
  provides structure that prevents getting lost in prompt complexity.
- **Domain model understanding** — CLAUDE.md + RFC docs + architecture tests provide
  sufficient context for a new contributor to be productive without full domain expertise.

---

## Commit

Fix applied in commit `6165879` on branch `claude/check-hexagonal-architecture-iPgEn`:
- `src/handlers/consolidation_handler.py` — TYPE_CHECKING guard
- `src/handlers/conversation_handler.py` — TYPE_CHECKING guard
- `tests/unit/test_req_arch_01_hexagonal_isolation.py` — REQ-ARCH-10 strengthened
