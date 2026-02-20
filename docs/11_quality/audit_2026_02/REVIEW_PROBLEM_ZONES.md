# Code Review: Problem Zones — Detailed Analysis with Code

> **Date:** 2026-02-18
> **Scope:** `src/adapters/firestore_session_store.py`, `main.py`, `src/services/user_agent_factory.py`, async/concurrency, memory management
> **Methodology:** Line-by-line code analysis, dependency tracing, race condition analysis

---

## Table of Contents

- [Priority Summary Table](#priority-summary-table)
- [1. FirestoreSessionStore — Critical Bugs](#1-firestoresessionstore--critical-bugs)
- [2. main.py — God-function and Bootstrap](#2-mainpy--god-function-and-bootstrap)
- [3. UserAgentFactory — Composition Root in the Wrong Place](#3-useragentfactory--composition-root-in-the-wrong-place)
- [4. Async/Concurrency — Race Conditions](#4-asyncconcurrency--race-conditions)
- [5. Memory Leaks — Unbounded Caches](#5-memory-leaks--unbounded-caches)
- [Recommended Fix Order](#recommended-fix-order)

---

## Priority Summary Table

### P0 — Critical Bugs (fix now)

| # | File | Line | Problem |
|---|------|------|---------|
| 1.1 | `firestore_session_store.py` | 265 | Missing `await` — `delete_session()` does not work at all |
| 3.1 | `user_agent_factory.py` | 104-132 | Circular dependency via `._repo = self.repository` — objects in invalid state |
| 2.1 | `main.py` | 203-235, 316 | `overflow_callback` captures `agent_factory` before it is created — `UnboundLocalError` |

### P1 — Serious Problems (next sprint)

| # | File | Line | Problem |
|---|------|------|---------|
| 1.2 | `firestore_session_store.py` | 87, 120 | `SessionState()` without `session_id` on TTL/error — identity loss |
| 1.3 | `firestore_session_store.py` | 146-147 | `save_session` swallows errors — silent data loss |
| 1.4 | `firestore_session_store.py` | 153, 238 | `append_message/batch` swallow errors — message loss |
| 1.5 | `firestore_session_store.py` | 233 | Fire-and-forget `create_task` for overflow — batch lost on error |
| 4.1 | `billing_agent.py` | 39, 66 | `pending_records` without lock — billing record loss |
| 4.2 | `logger_agent.py` | 39, 65 | `buffer` without lock — log loss |
| 4.3 | `user_agent_factory.py` | 75, 172 | `_cache` without lock — agent duplication |
| 4.4 | `billing_agent.py` / `logger_agent.py` | 40 | `create_task` in `__init__` — breaks without event loop |
| 2.2 | `main.py` | 487-490 | CORS `*` + `Credentials: true` — spec violation |
| 2.3 | `main.py` | 544 | Monkey-patching `slack_adapter.start` |
| 2.4 | `main.py` | 524 | Calling private `_handle_worker_task()` |
| 5.1 | `user_agent_factory.py` | 75 | Unbounded agent cache — primary memory leak |

### P2 — Technical Debt (planned)

| # | File | Problem |
|---|------|---------|
| 1.6 | `firestore_session_store.py` | Overflow extracts only 1 batch — history may remain > max |
| 1.7 | `firestore_session_store.py` | `ToolCall.thought_signature` lost during serialization |
| 1.8 | `firestore_session_store.py` | `cleanup_expired_sessions` — limit 100, no pagination |
| 2.5 | `main.py` | No graceful shutdown / signal handling |
| 2.6 | `main.py` | No cleanup in `finally` blocks |
| 3.2 | `user_agent_factory.py` | 8 concrete adapter imports in `services/` — hexagon violation |
| 3.3 | `user_agent_factory.py` | 95 lines of dead code (lines 450-545) |
| 5.2-5.4 | prompt_*.py | Unbounded caches in PromptAssembly/Component/Builder |

---

## 1. FirestoreSessionStore — Critical Bugs

**File:** `src/adapters/firestore_session_store.py`

### 1.1 [P0] Missing `await` — `delete_session()` does not work at all

```python
# Line 258-265
async def delete_session(self, session_id: str) -> None:
    """Delete a session from Firestore."""
    self._delete_session(session_id)   # ← MISSING AWAIT!

async def _delete_session(self, session_id: str) -> None:
    """Internal delete method."""
    try:
        doc_ref = self.db.collection(self.collection_name).document(session_id)
        await doc_ref.delete()
```

**What happens:** `_delete_session` is an `async def`. Calling it without `await` creates a coroutine that **is never executed**. Python will emit `RuntimeWarning: coroutine '_delete_session' was never awaited`, but the deletion **will silently not happen**. The public API `delete_session()` (from the `SessionStore` port) is completely non-functional.

**Consequences:** Any call to `delete_session()` (admin tooling, lifecycle management) assumes the session was deleted, but Firestore is never touched.

**Fix:**
```python
async def delete_session(self, session_id: str) -> None:
    await self._delete_session(session_id)
```

> **Note:** In the TTL-path (line 86) `_delete_session` is called correctly with `await`. The bug is only in the public method.

---

### 1.2 [P1] `SessionState()` without `session_id` — session identity loss

```python
# Line 84-87 (TTL expiry path):
if time.time() - last_activity > (self.ttl_hours * 3600):
    logger.info(f"⏰ Session {session_id[:8]}... expired, creating new")
    await self._delete_session(session_id)
    return SessionState()   # ← NO session_id!

# Line 118-120 (exception path):
except Exception as e:
    logger.error(f"❌ Error loading session {session_id[:8]}...: {e}")
    return SessionState()   # ← NO session_id!
```

Compare with the normal path (line 78):
```python
return SessionState(session_id=session_id)  # ← Correct
```

**What happens:** `SessionState()` creates an object with `session_id=""` (Pydantic default). If downstream code checks `state.session_id`, it will receive an empty string. If someone writes this session to Firestore — a document will be created with key `""`.

**Fix:**
```python
return SessionState(session_id=session_id)  # In both cases
```

---

### 1.3 [P1] `save_session` silently swallows write errors

```python
# Line 146-147
except Exception as e:
    logger.error(f"❌ Error saving session {session_id[:8]}...: {e}")
    # returns None — caller does not know that save failed
```

**What happens:** If Firestore is unavailable, quota is exceeded, or the document is too large — all session state is **silently lost**. The caller continues as if the save succeeded. Especially dangerous after a long LLM conversation — all user work is gone.

**Fix (option A — reraise):**
```python
except Exception as e:
    logger.error(f"❌ Error saving session {session_id[:8]}...: {e}")
    raise  # Let the caller decide
```

**Fix (option B — Result type):**
```python
async def save_session(self, session_id: str, state: SessionState) -> bool:
    try:
        ...
        return True
    except Exception as e:
        logger.error(...)
        return False
```

---

### 1.4 [P1] `append_message` and `append_messages_batch` — same pattern

```python
# Line 153-154 (append_message):
except Exception as e:
    logger.error(f"❌ Error appending message for {session_id[:8]}...: {e}")

# Line 238-239 (append_messages_batch):
except Exception as e:
    logger.error(f"❌ Error batch appending messages for {session_id[:8]}...: {e}")
```

**What happens:** A transactional write to Firestore failed (contention, network, size limit) — messages are **silently lost**. The next request will see stale history.

---

### 1.5 [P1] Fire-and-forget overflow callback — dequeue before ack

```python
# Line 192-233 (inside the append_messages_batch transaction):
if len(history) > self.max_history_length:
    extracted_batch = history[:self.batch_size]    # Extracted batch
    history = history[self.batch_size:]             # Trimmed history
# ...
transaction.set(doc_ref, data, merge=True)          # Wrote trimmed history
# ...
# Line 233 (OUTSIDE the transaction):
asyncio.create_task(self.overflow_callback(owner, session_id, batch))  # Fire-and-forget!
```

**What happens:** Classic "dequeue before ack" anti-pattern:
1. Transaction **already committed** — batch removed from hot storage
2. `create_task` creates a background task to write to cold storage
3. If the task **fails** (exception, shutdown, OOM) — batch is **permanently lost**
4. No reference to the Task is saved — exception goes to an `asyncio` warning and that's it

**Fix:**
```python
# Option 1: await instead of create_task (simple, but blocks append)
await self.overflow_callback(owner, session_id, batch)

# Option 2: TaskGroup with error tracking
task = asyncio.create_task(self.overflow_callback(owner, session_id, batch))
task.add_done_callback(self._on_overflow_done)
self._pending_tasks.add(task)
```

---

### 1.6 [P2] Overflow extracts only 1 batch

```python
# Line 194-196
if len(history) > self.max_history_length:
    extracted_batch = history[:self.batch_size]
    history = history[self.batch_size:]
```

With `max_history_length=200` and `batch_size=100`: if history grows to 350, after one extraction 250 remain — still > 200. No `while` loop. The document can grow toward Firestore's 1 MiB limit.

---

### 1.7 [P2] `ToolCall.thought_signature` lost during serialization

Serialization (lines ~334-338) saves only `name` and `args`:
```python
part_dict["tool_call"] = {
    "name": part.tool_call.name,
    "args": part.tool_call.args,
}
# thought_signature: Optional[str] — NOT saved
```

Deserialization (lines ~364-367) also does not restore it:
```python
tool_call = ToolCall(
    name=part_dict["tool_call"].get("name", ""),
    args=part_dict["tool_call"].get("args", {}),
)
```

---

### 1.8 [P2] `cleanup_expired_sessions` — limit 100, no pagination

```python
# Line 287-291
query = (
    self.db.collection(self.collection_name)
    .where("last_activity", "<", cutoff_time.timestamp())
    .limit(100)
)
```

Deletes max 100 documents per call, no loop/pagination. Deletes are sequential (one `await` each). 10K expired sessions → 100 cleanup calls.

---

## 2. main.py — God-function and Bootstrap

**File:** `main.py` (563 lines)

### 2.1 [P0] `overflow_callback` captures `agent_factory` before it is created

```python
# Line 203-237: overflow_callback is defined HERE
async def overflow_callback(user_id: str, session_id: str, messages: list[Message]):
    ...
    asyncio.create_task(process_user_batches_on_overflow(
        ...
        agent_factory=agent_factory,  # ← DOES NOT EXIST YET!
        ...
    ))

# Line 244-252: session_store created WITH callback
session_store = FirestoreSessionStore(
    ...
    overflow_callback=overflow_callback  # callback registered
)

# Line 316: agent_factory FINALLY created
agent_factory = UserAgentFactory(...)
```

**What happens:** Python closures capture variables by reference, not by value. At the time the callback is called, the variable `agent_factory` will be resolved. But if `overflow_callback` is called **between lines 252 and 316** (e.g., during session_store initialization that loads a session and sees overflow) — `UnboundLocalError`.

**Temporal coupling:** Initialization order is critical but is neither documented nor enforced anywhere.

**Fix:**
```python
# Option 1: Pass agent_factory via a deferred getter
async def overflow_callback(user_id, session_id, messages):
    factory = get_agent_factory()  # lazy resolve
    ...

# Option 2: Create callback AFTER agent_factory
agent_factory = UserAgentFactory(...)
overflow_callback = create_overflow_callback(agent_factory, consolidation_queue)
session_store.set_overflow_callback(overflow_callback)
```

---

### 2.2 [P1] CORS `*` + `Allow-Credentials: true`

```python
# Line 487-490
response.headers["Access-Control-Allow-Origin"] = "*"
response.headers["Access-Control-Allow-Credentials"] = "true"  # ← Contradicts wildcard
```

**Per the CORS specification:** browsers **ignore** `Allow-Credentials: true` when origin = `*`. This is either a bug (credentials don't work cross-origin) or a security misconfiguration.

**Fix:**
```python
origin = request.headers.get("Origin", "*")
response.headers["Access-Control-Allow-Origin"] = origin  # Echo actual origin
response.headers["Access-Control-Allow-Credentials"] = "true"
```

---

### 2.3 [P1] Monkey-patching `slack_adapter.start`

```python
# Line 544
slack_adapter.start = start_shared_app
```

Runtime method replacement from outside. Breaks adapter encapsulation, invisible to the reader of `SlackAdapter`, will break if `start` is called from `__init__`.

---

### 2.4 [P1] Direct call to private method across a boundary

```python
# Line 524
async def worker():
    return await slack_adapter._handle_worker_task()
```

The shared Quart app depends on an internal adapter API (`_handle_worker_task` is private by `_` convention).

---

### 2.5 [P2] No graceful shutdown

Three `try/except` blocks with `sys.exit(1)`, **zero** `finally` blocks:
- Firestore client is not closed
- LLM adapters are not closed
- Consolidation queue is not drained
- Background tasks are not cancelled

In Cloud Run / Kubernetes: `SIGTERM` → immediate exit → in-flight data loss.

---

### 2.6 [P2] `config` variable shadowed

```python
# Line 61
config = load_settings()  # ← Application config
...
# Line 536
config = HypercornConfig()  # ← SHADOWS application config!
config.bind = ["0.0.0.0:8080"]
```

After line 536, `config` refers to `HypercornConfig`, not to the application settings. A latent defect — not triggered yet, but adding code after line 536 that accesses `config["GEMINI_API_KEY"]` will crash.

---

### God-function: What is inside 563 lines of `main()`

| Line block | What it does |
|-----------|-------------|
| 46-55 | Telemetry + logging |
| 57-65 | Settings loading |
| 67-81 | Database client (emulator branching) |
| 84-98 | LLM adapters (x2 — GeminiAdapter created TWICE) |
| 104-134 | 5 repositories |
| 136-149 | IAM + InviteCode services |
| 151-187 | Agent coordinator + agent registration |
| 189-252 | Overflow callback + Session store |
| 259-312 | Prompt Design System v3 (7 components) |
| 315-329 | UserAgentFactory |
| 336-367 | OAuth + Cabinet |
| 373-409 | SlackAdapterFactory |
| 416-548 | Shared Quart app + inline route handlers |

**~25 services** wired manually. Each is a local variable inside a single function.

**Recommendation:** Decompose into:
```
init_database(config) -> db_client
init_llm_services(config) -> llm_service, embedding_service
init_repositories(db_client, env_config) -> repos
init_agents(coordinator, repos, llm) -> agent_factory
init_web_app(slack_adapter, auth_service) -> app
```

---

## 3. UserAgentFactory — Composition Root in the Wrong Place

**File:** `src/services/user_agent_factory.py` (549 lines)

### 3.1 [P0] Circular Dependency via post-construction mutation

```python
# Line 104-111: Creating with repository=None
self.biographical_search_enrichment = SearchEnrichmentService(
    repository=None,  # Will be set after repository is created
    embedding_service=self.embedding_service,
    ...
)

# Line 115-120: Creating with repository=None
self.biographical_context_service = BiographicalContextService(
    search_enrichment_service=self.biographical_search_enrichment,
    repository=None,  # Will be set after repository is created
    ...
)

# Line 123-128: Creating repository WITH biographical_context_service
self.repository = repository or FirestoreFactRepository(
    self.db_client,
    self.env_config,
    embedding_service=self.embedding_service,
    biographical_context_service=self.biographical_context_service  # DI
)

# Line 131-132: RETROACTIVE MUTATION of private fields
self.biographical_search_enrichment._repo = self.repository  # Circular!
self.biographical_context_service._repo = self.repository
```

**Circular dependency chain:**
```
FirestoreFactRepository → BiographicalContextService
  → SearchEnrichmentService → FactRepository (= FirestoreFactRepository)
```

**Problems:**
1. **Objects in invalid state** between lines 104 and 131. If any code calls `biographical_search_enrichment.enrich_context()` before line 131 — `NoneType` error
2. **Encapsulation violation** — accessing `_repo` (private by convention) from outside the class
3. **Testing** — requires 7 `patch()` calls to instantiate the factory

**Fix (correct):**
```python
# Option 1: Lazy proxy / callback
class RepositoryProxy:
    def __init__(self):
        self._repo = None
    def set_repo(self, repo):
        self._repo = repo
    def __getattr__(self, name):
        return getattr(self._repo, name)

# Option 2: Restructure — remove circular dependency
# BiographicalContextService should not depend on the same Repository
# that depends on it. Split FactRepository into ReadRepo + WriteRepo.
```

---

### 3.2 [P1] 8 concrete adapters imported in `services/`

```python
# src/services/user_agent_factory.py — this is the services layer!
from ..adapters.firestore_repo import FirestoreFactRepository           # VIOLATION
from ..adapters.firestore_session_store import FirestoreSessionStore     # VIOLATION
from ..adapters.gemini_adapter import GeminiAdapter                     # VIOLATION
from ..adapters.claude_adapter import ClaudeAdapter                     # VIOLATION
from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter  # VIOLATION
from ..adapters.firestore_prompt_repository import FirestorePromptComponentRepository  # VIOLATION
from ..adapters.groovy_prompt_assembler import GroovyPromptAssembler    # VIOLATION
from ..adapters.xml_prompt_assembler import XmlPromptAssembler          # VIOLATION
```

In hexagonal architecture `services/` depends on `ports/` (interfaces), and concrete adapters are wired in the composition root.

**Parameter types are also concrete** (lines 60-61):
```python
session_store: Optional[FirestoreSessionStore] = None,  # ← Should be SessionStore
repository: Optional[FirestoreFactRepository] = None,   # ← Should be FactRepository
```

---

### 3.3 [P2] 95 lines of dead code

```python
# Lines 450-545
def _get_model_for_tier(self, provider: LLMProvider, tier: PerformanceTier) -> str:
    # ... 30 lines of commented-out code ...
    pass  # Method is empty, called by nobody
```

---

### 19 responsibilities in one class

| # | Responsibility |
|---|---------------|
| 1 | LLM adapter instantiation (Gemini, Claude, Grok) |
| 2 | Embedding service instantiation |
| 3 | Session store instantiation |
| 4 | Repository instantiation + DI |
| 5 | Circular dependency resolution |
| 6 | Provider registry setup |
| 7 | Prompt component infrastructure |
| 8 | ConfigurationService creation |
| 9 | BiographicalContextService creation |
| 10 | SearchEnrichmentService creation |
| 11 | User profile loading/validation |
| 12 | Per-user LLM provider resolution |
| 13 | Per-user agent context building |
| 14 | Per-user search limit resolution |
| 15 | Per-user agent creation (6 agents) |
| 16 | Agent registration with coordinator |
| 17 | Agent caching with TTL |
| 18 | Prompt cache preloading |
| 19 | Dead code maintenance |

**Recommendation:** Split into:
- `ServiceContainer` (responsibilities 1-10) → `src/composition/`
- `AgentFactory` (15-16) → `src/services/`
- `AgentCache` (17) → `src/infrastructure/`

---

## 4. Async/Concurrency — Race Conditions

### 4.1 [P1] BillingAgent — `pending_records` without lock

```python
# billing_agent.py, line 39
self.pending_records: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

# Line 66 (execute — can be called concurrently):
self.pending_records[user_id].append(record)

# Line 68-69:
if len(self.pending_records[user_id]) >= self.flush_threshold:
    await self._flush_user(user_id)

# Line 79 (_flush_user — pop):
records = self.pending_records.pop(user_id, [])

# Line 102 (_periodic_flush — iteration):
for user_id in list(self.pending_records.keys()):
    await self._flush_user(user_id)
```

**Race:** `execute()` is called from a fire-and-forget `create_task` (QuickResponseAgent, line 434). `_periodic_flush()` runs in the background. If `execute()` appends a record while `_flush_user()` is doing `pop()` — the record is lost.

**Fix:**
```python
self._lock = asyncio.Lock()

async def execute(self, message):
    async with self._lock:
        self.pending_records[user_id].append(record)
        if len(self.pending_records[user_id]) >= self.flush_threshold:
            await self._flush_user(user_id)
```

---

### 4.2 [P1] LoggerAgent — `buffer` without lock (same pattern)

```python
# logger_agent.py, line 39
self.buffer: List[Dict[str, Any]] = []

# Line 65 (execute):
self.buffer.append(entry)

# Line 81-82 (_flush_logs):
entries = self.buffer[:]   # Shallow copy
self.buffer.clear()        # ← Between copy and clear another coroutine may append!
```

**Race:** Between `self.buffer[:]` and `self.buffer.clear()` another coroutine calls `self.buffer.append(entry)`. This entry lands in `self.buffer`, but **not in `entries`**. Then `clear()` destroys it. Log entry is lost.

**Fix:** Same — `asyncio.Lock()`.

---

### 4.3 [P1] UserAgentFactory._cache — duplicate agent creation

```python
# user_agent_factory.py, line 172-176
if user_id in self._cache:
    cached = self._cache[user_id]
    if (time.time() - cached["last_used"]) < self._cache_ttl:
        cached["last_used"] = time.time()
        return cached
# ...falls through to agent creation
```

**Race:** Two concurrent requests for the same user_id: both see a cache miss, both create a full set of agents (6 each), both write to `self._cache[user_id]` — the last one overwrites the first. Duplicate agents are created, resources are wasted. `_register_agents` raises `ValueError` for duplicates, which is silently ignored (line 424).

---

### 4.4 [P1] `asyncio.create_task` in `__init__` — requires running event loop

```python
# billing_agent.py AND logger_agent.py, line 40
self._flush_task: asyncio.Task | None = asyncio.create_task(self._periodic_flush())
```

`asyncio.create_task()` in `__init__()` requires a running event loop. If the object is created in a synchronous context (tests, CLI, module import) — `RuntimeError: no running event loop`. The task starts **before `__init__` completes** — `_periodic_flush()` may access attributes that are not yet initialized.

---

### 4.5 [P2] CircuitBreaker in base_agent.py — without lock

```python
# base_agent.py, line 28
self._failures: Dict[str, tuple[int, float]] = {}

# record_failure — read-modify-write without atomicity:
count = self._failures.get(agent_id, (0, 0))[0]
self._failures[agent_id] = (count + 1, time.time())
```

**Note:** The standalone `CircuitBreaker` in `src/utils/circuit_breaker.py` **correctly** uses `asyncio.Lock()`. The one built into `base_agent.py` does not.

---

### Fire-and-forget tasks — full map

| File | Line | What is lost on error |
|------|------|-----------------------|
| `firestore_quota_service.py` | 22 | Billing record |
| `firestore_session_store.py` | 233 | Overflow batch (100 messages) |
| `conversation_handler.py` | 507 | Consolidation trigger |
| `consolidation_handler.py` | 222 | Background consolidation |
| `quick_response_agent.py` | 434 | Usage tracking message |

**Fix pattern for all:**
```python
# Instead of:
asyncio.create_task(do_something())

# Use:
task = asyncio.create_task(do_something())
task.add_done_callback(_handle_task_exception)
self._background_tasks.add(task)
task.add_done_callback(self._background_tasks.discard)
```

---

## 5. Memory Leaks — Unbounded Caches

### 5.1 [P1] UserAgentFactory._cache — primary memory leak

```python
# user_agent_factory.py, line 75-76
self._cache: Dict[str, Dict[str, object]] = {}
self._cache_ttl = 3600
```

**Problem:** Every unique `user_id` adds an entry with 6 agents + SearchEnrichmentService + PromptBuilder. Expired entries are **never removed** — lazy eviction only on re-access (line 174). If a user comes once and leaves — their agents remain in memory forever.

**1000 users = 1000 × (6 agents + services) = significant memory footprint.**

**AgentCoordinator.agents** also grows — agents are registered but **never removed** (`unregister_agent()` exists but is never called).

**Fix:**
```python
from cachetools import TTLCache

self._cache = TTLCache(maxsize=100, ttl=3600)
# OR periodic sweep:
async def _evict_expired_cache(self):
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = [uid for uid, c in self._cache.items()
                   if now - c["last_used"] > self._cache_ttl]
        for uid in expired:
            self._unregister_user_agents(uid)
            del self._cache[uid]
```

---

### 5.2 [P2] PromptAssemblyService._assembled_cache

```python
# prompt_assembly_service.py, line 82
self._assembled_cache: Dict[str, Tuple[str, float]] = {}  # (prompt, timestamp)
self._cache_ttl = cache_ttl  # 86400 = 24 hours
```

Key: `prompt:{agent_type}:acc:{account_id}:usr:{user_id}`. For N users — up to 2N entries (quick + smart). Prompts are several KB each. Lazy eviction only.

---

### 5.3 [P2] PromptComponentService._cache

```python
# prompt_component_service.py, line 78
self._cache: Dict[str, tuple] = {}
```

TTL 3600s, lazy eviction, no maxsize. The key includes `user_id[:8]` — **potential collision** between users with the same 8-character ID prefix.

---

### 5.4 [P2] PromptBuilder._component_cache

```python
# prompt_builder.py, line 54
self._component_cache: Dict[str, tuple] = {}
```

Biographical context is cached **with no TTL at all**. Entries are only removed on explicit invalidation via consolidation. If consolidation does not run — entries live forever.

---

### 5.5 [P2] RouterAgent._cached_triage_prompt — never invalidated

```python
# router_agent.py, line 227
self._cached_triage_prompt: Optional[str] = None
```

Cached forever. If the prompt is updated in Firestore via `$admin_cache_reset` — the router continues using the old version until process restart.

---

## Recommended Fix Order

### Week 1: P0 (3 bugs, ~2-4 hours)

```
1. firestore_session_store.py:265 — add await (1 line)
2. main.py:203-316 — move overflow_callback after agent_factory
3. user_agent_factory.py:131-132 — replace ._repo with RepositoryProxy or lazy init
```

### Week 2: P1 Critical Data Path (~1-2 days)

```
4. firestore_session_store.py — error handling:
   - Line 87, 120: add session_id to SessionState()
   - Line 146: decide strategy (reraise vs Result type) for save_session
   - Line 233: replace create_task with await or tracked task

5. billing_agent.py + logger_agent.py:
   - Add asyncio.Lock to execute/flush
   - Move create_task from __init__ to a separate start() method

6. user_agent_factory.py._cache:
   - Add maxsize (TTLCache or OrderedDict with LRU)
   - Add unregistration on eviction
```

### Week 3: P1 Architecture (~2-3 days)

```
7. main.py decomposition:
   - Extract init_database(), init_services(), init_agents(), init_web_app()
   - Remove monkey-patching of slack_adapter.start
   - Fix CORS
   - Add graceful shutdown

8. main.py config shadowing — rename HypercornConfig variable
```

### Week 4: P2 Tech Debt (planned)

```
9. user_agent_factory.py — move to src/composition/, type config properly
10. Delete dead code (lines 450-545)
11. Add maxsize to all prompt caches
12. firestore_session_store.py — overflow while loop, pagination in cleanup
```

---

> **This document is a living document. Mark [DONE] as fixes are applied.**
