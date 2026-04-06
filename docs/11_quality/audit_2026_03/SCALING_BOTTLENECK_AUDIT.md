# Scaling Bottleneck Audit — 2026-03-09

Analysis of architectural risks that could cause cascading failures or
"collapse" under load. Six areas examined: state management, multi-tenancy,
agent lifecycle, prompt system rigidity, data model evolution, concurrency.

## Collapse Risk Ranking

| # | Area | Severity | Key Issue |
|---|------|----------|-----------|
| 1 | Concurrency: sync Cloud Tasks client | **HIGH** | `GcpTaskQueue.create_task()` blocks the event loop |
| 2 | Concurrency: shared semaphore | MEDIUM | `_FIND_NEAREST_SEMAPHORE(30)` is global across tenants |
| 3 | Data model: no migration framework | MEDIUM | Breaking schema changes require manual Firestore migration |
| 4 | Agent lifecycle: unbounded `_creation_locks` | LOW | Minor memory leak, trivial fix |
| 5 | Multi-tenancy: `admin_cache_reset` is global | LOW | Any admin disrupts all tenants on the instance |
| 6 | Prompt system: cache invalidation across instances | LOW | Stale prompts for up to 24h in multi-instance deployments |

---

## 1. Sync Cloud Tasks Client Blocks Event Loop — HIGH

**Location:** `src/adapters/gcp_task_queue.py:81`

`self.client.create_task(request=...)` uses the synchronous `tasks_v2.CloudTasksClient()`.
Every enqueue operation (consolidation, email indexing, agent execution) blocks the entire
event loop for 50–200ms. With multiple concurrent users, all Cloud Tasks enqueue operations
are serialized, stalling all other async work.

**Why it's dangerous now:** Not noticeable at 1–2 users. At 5+ concurrent users,
unexplainable latency spikes that are difficult to diagnose. The pattern could
proliferate to other adapters.

**Fix (1 file, ~30 min):** Replace with `asyncio.to_thread(self.client.create_task, ...)`
or switch to `tasks_v2.CloudTasksAsyncClient()`.

## 2. Global Semaphore Starves User Requests — MEDIUM

**Location:** `src/adapters/firestore_repo.py:43`

`_FIND_NEAREST_SEMAPHORE = asyncio.Semaphore(30)` is module-level, shared across ALL users
in the same worker process. Each memory search uses 6 parallel vector queries. Consolidation
agent also runs vector searches. Under concurrent load, consolidation competes with user
requests for the same 30 semaphore slots.

**Impact:** User waits for a response while background consolidation occupies semaphore slots.
No priority mechanism exists.

**Fix:** Split into two semaphores (user / background) or implement a priority-aware
semaphore wrapper. User requests should always have priority.

## 3. No Schema Migration Framework — MEDIUM

**Location:** Firestore schema-on-read throughout `src/adapters/firestore_repo.py`

Additive changes work fine (Optional fields with defaults). However:
- Field renames (e.g., `owner_id` → `account_id`) require manual migration of all documents
- Enum breaking changes (`visibility: str → enum`) rely on Pydantic coercion
- Collection names have hardcoded versions (`domain_facts_v1`) with no blue-green switching
- `context_priority_rank` is computed adapter-side and stored; changing the mapping
  requires re-indexing all documents with no tooling to do so

**Fix:** Not a framework — just a `scripts/migrations/` directory with numbered scripts
and a Cloud Tasks dispatch pattern. Needed before the first breaking schema change.

## 4. Unbounded `_creation_locks` Dict — LOW

**Location:** `src/composition/user_agent_factory.py:142`

`self._creation_locks: Dict[str, asyncio.Lock] = {}` — new locks added via `setdefault`
but never removed, even when corresponding cache entries are evicted. Each `asyncio.Lock`
is small, but the dict grows forever.

**Fix (trivial):** Clean up locks in the eviction sweep alongside cache entries.

## 5. `admin_cache_reset` Is a Global Operation — LOW

**Location:** `src/handlers/conversation_handler.py:648`

Any user with admin access can reset prompt caches for ALL users on the same Cloud Run
instance. In a multi-tenant system, this is a blast radius concern.

**Fix:** Scope to per-user or per-account cache reset.

## 6. Prompt Cache Invalidation Across Instances — LOW

**Location:** `src/services/prompt_v3/prompt_assembly_service.py:484`

`_build_cache_key` uses `(agent_type, account_id, user_id)` — no blueprint version.
Changing a token or blueprint in Firestore results in stale prompts for up to 24 hours.
`admin_cache_reset` only affects the current instance.

**Current mitigation:** Single Cloud Run instance. Becomes a problem at `min-instances > 1`.

---

## Areas That Are NOT At Risk

- **Hexagonal layer isolation** — clean, adapters are swappable
- **Multi-tenancy isolation** — `RequestContext` via `contextvars` is correctly scoped per-task
- **Agent lifecycle** — 1h TTL + eviction sweep works correctly for Cloud Run's lifecycle
- **Prompt token system** — flexible override mechanism, blueprint-driven assembly
- **SCD2 fact versioning** — robust deduplication and version history
- **CircuitBreaker** — per-agent instances, correct failure isolation

## Detailed Findings

### State Management (LOW-MEDIUM)

- Module-level `_FIND_NEAREST_SEMAPHORE` (see #2 above)
- Module-level `_global_logger` singleton in `debug_logger.py` — stateless, no risk
- Agent config singletons (`BASE`, `QUICK`, etc.) — frozen dataclasses, genuinely immutable
- CircuitBreaker is per-user-per-agent — during a provider outage, every user independently
  burns through retry attempts (no global circuit breaker per provider)
- `UserAgentFactory._creation_locks` — `setdefault` is safe in single-threaded asyncio

### Multi-Tenancy (MEDIUM)

- `PromptAssemblyService._assembled_cache` — unbounded `Dict`, keyed by
  `(account_id, user_id)`, no max entries (only 24h TTL expiry). Under many active users,
  grows proportionally to `users × agent_types`
- `get_biographical_context_cached` falls back to `get_effective_account_id()` from
  `RequestContext` — if context is not set, silently returns empty list (no cross-tenant leak)

### Agent Lifecycle (MEDIUM)

- Each user creates ~13 **eager** agent instances + per-user `PromptBuilder`, `MCPClient`,
  `MCPMapsAdapter` — the `MCPClient` per user is notable (HTTP client per user).
  7 additional agents (doc generation, deep research, file management) are **lazy** — created
  on first delegation via `AgentFactoryPort`, reducing per-user initialization by ~40%.
- 1h TTL + 5min sweep interval appropriate for Cloud Run auto-scale-to-zero
- Eviction calls `coordinator.unregister_agent()` (both eager and lazy agents) but does not cancel any in-flight
  agent tasks that hold references to evicted agents

### Concurrency (MEDIUM-HIGH)

- `GcpTaskQueue` sync client — see #1 above
- Consolidation runs in-process via `asyncio.create_task` (fire-and-forget) — shares
  CPU/memory with user requests; no graceful shutdown coordination
- Multiple fire-and-forget tasks without shutdown tracking:
  - Notification save (`conversation_handler.py:266`)
  - Quota recording (`firestore_quota_service.py:22`)
- BillingAgent and LoggerAgent have per-instance background flush tasks with proper locking
