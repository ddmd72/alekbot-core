# Epic: Reliability and Architectural Cleanliness

> **Source:** Codebase audit 2026-02-18
> **Audit documents:** `docs/11_quality/audit_2026_02/`
> **Status:** Backlog — not scheduled
> **Overall estimate:** P0 ~4-6h | P1 ~2-3d | P2 ~4-6d

---

## Context

An audit of 159 Python files identified three classes of problems:

1. **Critical bugs** — functions that currently don't work or may crash in prod
2. **Data safety** — paths where data can be silently lost (sessions, billing, history)
3. **Technical debt** — architectural violations, memory leaks, maintainability

The epic does not need to be completed in full. Each milestone is independent. Start with P0.

---

## Milestone 1: P0 Critical Bugs
> **Estimate:** 4–6 hours | **Risk:** breaks prod in certain scenarios

### Task 1.1 — `delete_session` does not work at all

**File:** `src/adapters/firestore_session_store.py:265`

**Problem:**
```python
# Current code — coroutine is created but never executed
async def delete_session(self, session_id: str) -> None:
    self._delete_session(session_id)  # ← no await!
```

**Fix:**
```python
async def delete_session(self, session_id: str) -> None:
    await self._delete_session(session_id)
```

**Verification:** Call `$admin_delete_session` or any path through admin tooling, confirm the document is deleted from Firestore.

---

### Task 1.2 — `SessionState()` without `session_id`

**File:** `src/adapters/firestore_session_store.py:87, 120`

**Problem:** On TTL expiry or an exception, a `SessionState()` is created without a `session_id`. If downstream writes it — a document will be created with the key `""`.

```python
# Line 87 (TTL expiry):
return SessionState()       # ← bug

# Line 120 (exception):
return SessionState()       # ← bug
```

**Fix — both lines:**
```python
return SessionState(session_id=session_id)
```

**Verification:** Wait for session TTL (or reduce `ttl_hours` in a test) — confirm the returned object has `session_id`.

---

### Task 1.3 — `overflow_callback` captures `agent_factory` before it is created

**File:** `main.py:203-316`

**Problem:** Python closure captures variables by reference. Between line ~252 (creating `session_store` with callback) and line ~316 (creating `agent_factory`) — if the callback fires, it will raise `UnboundLocalError`.

```python
# Current order (DANGEROUS):
async def overflow_callback(...):
    agent_factory=agent_factory  # ← does not exist yet!

session_store = FirestoreSessionStore(overflow_callback=overflow_callback)  # line ~252
...
agent_factory = UserAgentFactory(...)  # line ~316
```

**Fix — option A (simpler):** Move the callback definition AFTER `agent_factory` is created:
```python
agent_factory = UserAgentFactory(...)      # factory first

async def overflow_callback(...):          # callback second
    agent_factory=agent_factory  # already exists

session_store = FirestoreSessionStore(overflow_callback=overflow_callback)
```

**Fix — option B (cleaner):** Use a factory function:
```python
def make_overflow_callback(factory, queue):
    async def overflow_callback(user_id, session_id, messages):
        asyncio.create_task(process_user_batches_on_overflow(
            agent_factory=factory, ...
        ))
    return overflow_callback

agent_factory = UserAgentFactory(...)
session_store = FirestoreSessionStore(
    overflow_callback=make_overflow_callback(agent_factory, consolidation_queue)
)
```

**Verification:** Run with overflow (reduce `max_history_length` to 5), confirm consolidation is triggered.

---

### Task 1.4 — Circular dependency via post-construction mutation

**File:** `src/services/user_agent_factory.py:104-132`

**Problem:** Objects are created in an invalid state and then retroactively patched:
```python
self.biographical_search_enrichment = SearchEnrichmentService(repository=None, ...)  # line 104
self.biographical_context_service = BiographicalContextService(repository=None, ...)  # line 115
self.repository = FirestoreFactRepository(..., biographical_context_service=self.biographical_context_service)  # line 123
self.biographical_search_enrichment._repo = self.repository   # line 131 — direct access to private!
self.biographical_context_service._repo = self.repository     # line 132
```

If anything calls `enrich_context()` between lines 104 and 131 — `NoneType` error.

**Fix — RepositoryProxy:**
```python
class RepositoryProxy:
    """Lazy proxy to break circular dependency."""
    def __init__(self):
        self._repo = None
    def set(self, repo):
        self._repo = repo
    def __getattr__(self, name):
        if self._repo is None:
            raise RuntimeError("RepositoryProxy not initialized")
        return getattr(self._repo, name)

# In UserAgentFactory.__init__:
repo_proxy = RepositoryProxy()

self.biographical_search_enrichment = SearchEnrichmentService(repository=repo_proxy, ...)
self.biographical_context_service = BiographicalContextService(repository=repo_proxy, ...)
self.repository = repository or FirestoreFactRepository(
    ..., biographical_context_service=self.biographical_context_service
)
repo_proxy.set(self.repository)  # Once, publicly
```

**Verification:** `make test-unit` — confirm all tests pass. Run with `make dev` and execute several requests through biographical cache.

---

## Milestone 2: P1 Data Safety
> **Estimate:** 1–2 days | **Risk:** silent data loss

### Task 2.1 — Fire-and-forget overflow: data is lost on error

**File:** `src/adapters/firestore_session_store.py:233`

**Problem:**
```python
# Line 233 — transaction already committed, batch deleted from hot storage
asyncio.create_task(self.overflow_callback(owner, session_id, batch))
# If the task fails — the batch is lost forever
```

**Fix — tracked task with error handling:**
```python
task = asyncio.create_task(self.overflow_callback(owner, session_id, batch))
task.add_done_callback(self._on_overflow_done)
self._pending_tasks.add(task)
task.add_done_callback(self._pending_tasks.discard)

def _on_overflow_done(self, task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        logger.error(
            "❌ [SessionStore] Overflow callback failed — batch may be lost: %s",
            task.exception()
        )
```

Add `self._pending_tasks: set[asyncio.Task] = set()` to `__init__`.

**Verification:** Temporarily break `overflow_callback` (raise exception inside), confirm the error appears in logs and there is no `Task exception was never retrieved`.

---

### Task 2.2 — `save_session` and `append_message*` swallow write errors

**File:** `src/adapters/firestore_session_store.py:146, 153, 238`

**Problem:** Silent data loss — the caller does not know the write failed.

```python
except Exception as e:
    logger.error(...)
    # ← no raise, no return False, caller does not know
```

**Fix — reraise strategy (recommended):**
```python
# save_session, append_message, append_messages_batch — all three:
except Exception as e:
    logger.error(f"❌ [...] {e}")
    raise  # Let ConversationHandler decide how to handle it
```

**Alternative (if reraise breaks too much):** Return `bool` and log in the caller.

**Verification:** Disconnect Firestore (emulator → stop), confirm the error surfaces to the user and is not swallowed.

---

### Task 2.3 — `asyncio.Lock` in BillingAgent and LoggerAgent

**Files:** `src/agents/infrastructure/billing_agent.py:39,66,79`, `logger_agent.py:39,65,81`

**Problem BillingAgent:**
```python
# Race: execute() and _periodic_flush() compete without a lock
self.pending_records[user_id].append(record)    # execute()
records = self.pending_records.pop(user_id, []) # _flush_user()
# A record written between append and pop — lost
```

**Problem LoggerAgent:**
```python
entries = self.buffer[:]   # copy
self.buffer.clear()        # ← another coroutine calls append() between these two lines → loss
```

**Fix — both agents:**
```python
# In __init__:
self._lock = asyncio.Lock()

# In execute():
async with self._lock:
    self.pending_records[user_id].append(record)
    if len(self.pending_records[user_id]) >= self.flush_threshold:
        await self._flush_user(user_id)

# In _flush_user() / _flush_logs():
async with self._lock:
    records = self.pending_records.pop(user_id, [])
    # or
    entries, self.buffer = self.buffer[:], []
```

**Verification:** Load test — several concurrent requests with billing tracking, confirm the record count matches.

---

### Task 2.4 — `create_task` in `__init__` without a running event loop

**Files:** `src/agents/infrastructure/billing_agent.py:40`, `logger_agent.py:40`

**Problem:**
```python
# __init__ — called before event loop in tests
self._flush_task = asyncio.create_task(self._periodic_flush())  # RuntimeError in tests
```

**Fix:** Move to an explicit `start()` method:
```python
# Remove from __init__, add:
async def start(self) -> None:
    """Start background flush task. Call after event loop is running."""
    self._flush_task = asyncio.create_task(self._periodic_flush())

# In main.py after agents are created:
await billing_agent.start()
await logger_agent.start()
```

**Verification:** `make test-unit` — tests must not throw `RuntimeError: no running event loop`.

---

### Task 2.5 — `UserAgentFactory._cache` without a Lock

**File:** `src/services/user_agent_factory.py:172`

**Problem:**
```python
# Two concurrent requests for the same user_id:
if user_id in self._cache:  # Both see cache miss
    ...
# Both create 6 agents, both write to _cache — the last one overwrites the first
# _register_agents() raises ValueError for duplicates — ignored
```

**Fix:**
```python
# In __init__:
self._cache_lock = asyncio.Lock()

# In get_or_create_agents():
async with self._cache_lock:
    if user_id in self._cache:
        cached = self._cache[user_id]
        if (time.time() - cached["last_used"]) < self._cache_ttl:
            cached["last_used"] = time.time()
            return cached
    # ... agent creation inside the lock
```

---

## Milestone 3: P1 Production Reliability
> **Estimate:** 1 day | **Risk:** loss of in-flight data during deployment

### Task 3.1 — Graceful shutdown

**File:** `main.py`

**Problem:** Cloud Run sends `SIGTERM` on deploy/scale-down. The current code does not handle it — in-flight LLM requests (2-5 sec) are aborted, consolidation tasks are lost.

**Fix:**
```python
import signal

# After all services are started:
shutdown_event = asyncio.Event()

def handle_shutdown(signum, frame):
    logger.info("🛑 Shutdown signal received, draining...")
    shutdown_event.set()

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# In main():
await shutdown_event.wait()

# Drain:
logger.info("🛑 Draining background tasks...")
# Wait for pending tasks from session_store._pending_tasks
# Wait for billing_agent pending flush
# Close LLM clients
logger.info("✅ Graceful shutdown complete")
```

**Verification:** `kill -SIGTERM <pid>` during an active request — confirm the request completes, then the process exits.

---

### Task 3.2 — Unbounded agent cache → TTLCache

**File:** `src/services/user_agent_factory.py:75`

**Problem:** Every unique `user_id` adds an entry with 6 agents forever. 1000 users = significant memory footprint in Cloud Run (RAM constraints).

**Fix:**
```python
# pip install cachetools (already in deps?)
from cachetools import TTLCache

# Instead of:
self._cache: Dict[str, Dict] = {}

# Use:
self._cache = TTLCache(maxsize=50, ttl=3600)  # 50 active user-sets max

# On eviction — deregister agents from coordinator:
# Needs a custom __missing__ or on_evict callback
```

**Alternative (without cachetools):** Periodic sweep:
```python
async def _evict_expired_cache(self) -> None:
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        now = time.time()
        async with self._cache_lock:
            expired = [uid for uid, c in self._cache.items()
                       if now - c["last_used"] > self._cache_ttl]
            for uid in expired:
                # unregister agents from coordinator
                del self._cache[uid]
```

---

### Task 3.3 — CORS fix

**File:** `main.py:487-490`

**Problem:** `Access-Control-Allow-Origin: *` + `Access-Control-Allow-Credentials: true` — browsers ignore credentials with a wildcard origin. OAuth Cabinet does not work cross-origin.

**Fix:**
```python
ALLOWED_ORIGINS = {
    "https://your-cabinet-domain.com",
    "http://localhost:3000",  # dev
}

origin = request.headers.get("Origin", "")
if origin in ALLOWED_ORIGINS:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
```

---

### Task 3.4 — Variable `config` is shadowed

**File:** `main.py:61, 536`

**Problem:**
```python
config = load_settings()    # line 61 — application config
...
config = HypercornConfig()  # line 536 — SHADOWS IT! After this, config["KEY"] will crash
```

**Fix:** Rename:
```python
hypercorn_config = HypercornConfig()
hypercorn_config.bind = ["0.0.0.0:8080"]
await serve(app, hypercorn_config)
```

---

## Milestone 4: P2 Architectural Debt
> **Estimate:** 3–5 days | **Risk:** 0 (refactoring without behavior change) | **Do as planned**

### Task 4.1 — `AgentExecutionContext` → `domain/agent.py`

**File:** `src/services/agent_context_builder.py` → move to `src/domain/agent.py`

**Problem:** `AgentExecutionContext` is a dataclass with fields `provider`, `model_name`, `performance_tier`. It is not a service, it is a value object. It lives in `services/` so all agents violate the dependency rule by importing it.

**Fix:**
```python
# Move class AgentExecutionContext from services/agent_context_builder.py
# to domain/agent.py (alongside RoutingMetadata, AgentMessage)
# Update all imports:
# WAS: from ...services.agent_context_builder import AgentExecutionContext
# NOW: from ...domain.agent import AgentExecutionContext
```

**Affected files:** `router_agent.py`, `quick_response_agent.py`, `smart_response_agent.py`, `web_search_agent.py`, `agent_context_builder.py` (keep a backward-compat re-export).

---

### Task 4.2 — `calculate_cost` → `domain/billing.py`

**File:** `src/services/cost_calculator.py`

**Problem:** Pure function with no I/O or state. Should live in domain.

**Fix:**
```python
# Create src/domain/billing.py
# Move calculate_cost() there
# Update import in quick_response_agent.py:
# WAS: from ...services.cost_calculator import calculate_cost
# NOW: from ...domain.billing import calculate_cost
```

---

### Task 4.3 — `domain/tone.py` — remove logger from utils

**File:** `src/domain/tone.py:5`

**Problem:** Domain imports `utils.logger` — an infrastructure dependency in the core.

**Fix:**
```python
# Replace:
from ..utils.logger import logger

# With:
import logging
logger = logging.getLogger(__name__)
```

---

### Task 4.4 — `UserAgentFactory` → extract composition root

**File:** `src/services/user_agent_factory.py` (549 lines, 19 responsibilities)

**Goal:** Split into:
- `src/composition/service_container.py` — instantiation of adapters, DB clients, shared services (responsibilities 1-10)
- `src/services/user_agent_factory.py` — only per-user agents (responsibilities 11-18)

**ServiceContainer** takes: `config`, `db_client` — creates: `llm_port`, `embedding_service`, `session_store`, `repository`, `prompt_infrastructure`.

**AgentFactory** takes: services via constructor (ports!) — creates: per-user agent sets.

**Affected files:** `main.py` (wiring moves here), `user_agent_factory.py` (drastically simplified).

**Note:** This is the largest task. Do it last, once P0/P1 are closed.

---

### Task 4.5 — Remove dead code

**File:** `src/services/user_agent_factory.py:450-545`

**Problem:** ~95 lines of the `_get_model_for_tier()` method — commented out, never called by anyone.

**Fix:** Delete lines 450-545 entirely.

---

### Task 4.6 — Overflow: while loop instead of a single batch

**File:** `src/adapters/firestore_session_store.py:194-196`

**Problem:**
```python
if len(history) > self.max_history_length:
    extracted_batch = history[:self.batch_size]
    history = history[self.batch_size:]
    # If history was 350, max=200, batch=100 → 250 remain (still > max)
```

**Fix:**
```python
while len(history) > self.max_history_length:
    extracted_batch = history[:self.batch_size]
    history = history[self.batch_size:]
    # send batch to callback (currently: only the last batch is sent)
    # need to collect all batches and process them
```

**Caution:** This changes overflow behavior — test on the emulator.

---

## Dependency map between tasks

```
1.1 (await)          — independent
1.2 (session_id)     — independent
1.3 (overflow order) — independent of 1.4, but both in main.py → do together
1.4 (circular dep)   — independent

2.1 (tracked tasks)  — best after 1.3
2.2 (reraise)        — independent
2.3 (locks)          — independent
2.4 (start method)   — required for 2.3
2.5 (cache lock)     — required for 3.2

3.1 (graceful shut)  — best after 2.4
3.2 (TTLCache)       — after 2.5
3.3 (CORS)           — independent
3.4 (config shadow)  — independent

4.1 (ExecutionCtx)   — independent, do first among P2
4.2 (calc_cost)      — independent
4.3 (tone logger)    — independent, 1 line
4.4 (composition)    — after 4.1, 4.2 (fewer imports to touch)
4.5 (dead code)      — any time
4.6 (while overflow) — independent
4.7 (prompt_builder) — independent
```

---

## Summary table

| Task | File | Effort | Severity | Independent |
|------|------|--------|----------|-------------|
| 1.1 await delete | session_store.py:265 | 5 min | P0 | ✅ |
| 1.2 session_id | session_store.py:87,120 | 10 min | P0 | ✅ |
| 1.3 overflow order | main.py:203-316 | 1h | P0 | ✅ |
| 1.4 circular dep | user_agent_factory.py:104-132 | 3-4h | P0 | ✅ |
| 2.1 tracked tasks | session_store.py:233 | 2h | P1 | after 1.3 |
| 2.2 reraise | session_store.py:146,153,238 | 1h | P1 | ✅ |
| 2.3 locks billing | billing_agent.py, logger_agent.py | 2h | P1 | ✅ |
| 2.4 start() method | billing_agent.py, logger_agent.py | 1h | P1 | ✅ |
| 2.5 cache lock | user_agent_factory.py:172 | 1h | P1 | ✅ |
| 3.1 graceful shut | main.py | 4h | P1 | after 2.4 |
| 3.2 TTLCache | user_agent_factory.py:75 | 2h | P1 | after 2.5 |
| 3.3 CORS | main.py:487 | 30 min | P1 | ✅ |
| 3.4 config shadow | main.py:536 | 5 min | P1 | ✅ |
| 4.1 ExecutionCtx | agent_context_builder.py | 2h | P2 | ✅ |
| 4.2 calc_cost | cost_calculator.py | 30 min | P2 | ✅ |
| 4.3 tone logger | domain/tone.py:5 | 5 min | P2 | ✅ |
| 4.4 composition | user_agent_factory.py | 2-3d | P2 | after 4.1,4.2 |
| 4.5 dead code | user_agent_factory.py:450-545 | 5 min | P2 | ✅ |
| 4.6 while overflow | session_store.py:194 | 1h | P2 | ✅ |
| 4.7 prompt_builder Optional→required | 12 agent files | 1h | P2 | ✅ |

---

### Task 4.7 — `prompt_builder: Optional[PromptBuilderPort]` → required in all agents

**Files:** 12 agents with `Optional[PromptBuilderPort] = None` in constructor:
- `src/agents/core/router_agent.py` (2 places)
- `src/agents/doc_generator_agent.py`
- `src/agents/doc_planner_agent.py`
- `src/agents/email_classification_agent.py`
- `src/agents/web_search_light_agent.py`
- `src/agents/compute_agent.py`
- `src/agents/web_search_agent.py`
- `src/agents/deep_research_agent.py`
- `src/agents/consolidation_agent.py`
- `src/agents/memory_search_agent.py`

**Problem:** PromptBuilder is always injected by `UserAgentFactory` — `None` is never passed
in production. But the `Optional` type signature implies it is acceptable, and some agents
have fallback code (`if self.prompt_builder: ...`) that degrades to an empty prompt silently.
This masks misconfiguration instead of failing fast. `PdfGeneratorAgent` was already fixed
(2026-03-15) — the rest should follow.

**Fix per agent:**
1. Change `prompt_builder: Optional[PromptBuilderPort] = None` → `prompt_builder: PromptBuilderPort`
2. Remove `if self.prompt_builder:` guard — call unconditionally
3. On `build_for_agent()` failure → `AgentResponse.failure()`, not empty string fallback
4. Update tests if they instantiate without prompt_builder (add mock)

**Also update:**
- `docs/how_to/NEW_AGENT_PLAYBOOK.md` — template uses `Optional`, change to required
- RFCs that reference the old pattern (informational, not blocking)

**Verification:** `make test-unit` — all tests must pass after removing Optional.

---

> **Audit source:** `docs/11_quality/audit_2026_02/`
> **Last updated:** 2026-03-15
> **Status:** Mark [DONE] as tasks are completed

---

## [TECH DEBT] Deep Research Cloud Run Job: .md round files never uploaded to GCS

**Discovered:** 2026-03-24

**Root cause:** `job_main.py` calls `deliver_deep_research()` without `media_storage` and `round1_text` arguments:

```python
# job_main.py — current (broken)
await deliver_deep_research(
    result_text=result_text,
    ...
    # media_storage=None (default) → skips GCS upload silently
    # round1_text="" (default) → second-pass round never uploaded
)
```

`deliver_deep_research()` has an `if media_storage and notification:` guard — when `media_storage` is `None`, GCS upload is silently skipped and a warning is logged. The `.md` files (`deep_research/{user_id}/{timestamp}-round1.md`, `round2.md`) are never written.

**Impact:** Raw markdown research output is lost after the Cloud Run Job completes. Only the HTML page (via HtmlPageGeneratorAgent Cloud Task) is delivered.

**Fix:** Wire `GcsMediaAdapter` and a notification port into `job_main.py` and pass them to `deliver_deep_research()`. Requires bootstrapping `GCS_MEDIA_BUCKET` env var and a lightweight notification adapter in the Job entrypoint.

**Priority:** Low — HTML delivery works; raw `.md` is a nice-to-have for debugging and archival.
