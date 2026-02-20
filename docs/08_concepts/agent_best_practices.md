# LLM Agent Best Practices
**Subtitle:** Principles for building a provider-invariant and efficient AI agent

## 📖 HowTo: Using This Document

### Purpose
Critical practices for building production-ready LLM agents: security, cost control, reliability, and observability.

### When to Read
- **For AI Agents:** Before implementing new agents or tools.
- **For Developers:** When adding agent features or debugging production issues.

### When to Update
This document MUST be updated when:
- [ ] New best practices are identified from production incidents.
- [ ] Security vulnerabilities are discovered and mitigated.
- [ ] Cost optimization patterns change.

### Cross-References
- **Multi-Agent System:** [../05_building_blocks/multi_agent_system/README.md](../05_building_blocks/multi_agent_system/README.md)
- **Provider Resolution:** [../05_building_blocks/provider_resolution/README.md](../05_building_blocks/provider_resolution/README.md)
- **Observability Strategy:** [../05_building_blocks/observability_strategy/README.md](../05_building_blocks/observability_strategy/README.md)
- **Fractal Architecture:** [./fractal_architecture.md](./fractal_architecture.md)

---

## 🎯 Goal

This document contains 10 critically important practices for building a production-ready LLM agent that is:
- ✅ **Provider-invariant** (Gemini, Claude, GPT)
- ✅ **Efficient** in cost and performance
- ✅ **Reliable** during failures and edge cases

---

## 1. 🔒 Idempotency Keys

**Problem:** Duplicate HTTP requests can create duplicate facts/actions.

**Solution:**
```python
class FactRepository:
    async def save(self, fact: Fact, idempotency_key: str):
        # Check if key already processed
        if await self._firestore.collection('idempotency_keys').document(idempotency_key).get():
            logger.info(f"Skipping duplicate request: {idempotency_key}")
            return

        # Save fact + idempotency key atomically
        await self._firestore.run_transaction(async_transaction)
```

**Applicability:** 🔴 Critical for HTTP/Cloud Run deployments

**Implemented in:**
- `src/adapters/firestore_dedup_store.py` - Event deduplication with TTL
- `src/adapters/slack/http_adapter.py` - Slack event deduplication

---

## 2. 📊 Telemetry & Observability

**Problem:** Bottlenecks are unclear and optimization is difficult.

**Solution:** OpenTelemetry spans
```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

async def process(self, message: AgentMessage) -> AgentResponse:
    with tracer.start_as_current_span(f"agent.{self.agent_id}") as span:
        span.set_attribute("agent.id", self.agent_id)
        span.set_attribute("message.intent", message.intent.value)

        response = await self._execute(message)

        span.set_attribute("response.status", response.status.value)
        span.set_attribute("response.tokens", response.metadata.get("tokens", 0))

        return response
```

**Metrics to Track:**
- Latency per agent (p50, p95, p99)
- Token usage per agent
- Cost per agent
- Success/failure rates
- Delegation depth (for complex flows)

**Applicability:** 🟡 Important for optimization

**Implemented in:**
- `src/utils/telemetry.py` - OpenTelemetry setup
- `src/utils/logger.py` - Structured logging with trace IDs

See: [Observability Strategy](../05_building_blocks/observability_strategy/README.md)

---

## 3. 💰 Cost Control & Budgets

**Problem:** Recursive agents can blow up the budget.

**Solution:**
```python
class CostManager:
    def __init__(self):
        self.daily_limit = 10_000  # tokens
        self.per_user_limit = 1_000
        self._usage = {}  # {user_id: {date: token_count}}

    async def check_budget(self, user_id: str, estimated_tokens: int) -> bool:
        today = datetime.now().date()
        usage_today = self._usage.get(user_id, {}).get(today, 0)

        if usage_today + estimated_tokens > self.per_user_limit:
            raise BudgetExceededError(f"Daily limit reached: {usage_today}/{self.per_user_limit}")

        return True

    async def track_usage(self, user_id: str, actual_tokens: int):
        # Store in Firestore for persistence
        await self._repo.increment_usage(user_id, actual_tokens)
```

**Rate Limiting Strategies:**
- Per-user daily limits
- Per-agent cost caps
- Global budget alerts
- Graceful degradation (cheaper model fallback)

**Applicability:** 🔴 Critical for production

**Implemented in:**
- `src/adapters/firestore_quota_service.py` - Non-blocking quota tracking
- `src/services/cost_calculator.py` - Token cost calculation

---

## 4. 🔐 Prompt Injection Defense

**Problem:** Users can "hack" the system through clever prompts.

**Solution:**
```python
class PromptShield:
    INJECTION_PATTERNS = [
        r"ignore previous instructions",
        r"you are now",
        r"system:",
        r"<\|im_start\|>",  # ChatML injection
    ]

    def detect_injection(self, user_input: str) -> bool:
        for pattern in self.INJECTION_PATTERNS:
            if re.search(pattern, user_input, re.IGNORECASE):
                logger.warning(f"Potential injection detected: {pattern}")
                return True
        return False

    def sanitize(self, user_input: str) -> str:
        # Remove special tokens
        sanitized = user_input.replace("<|im_start|>", "")
        sanitized = sanitized.replace("<|im_end|>", "")
        return sanitized[:MAX_INPUT_LENGTH]  # Length limit
```

**Additional Defenses:**
- Structured outputs only (force JSON where possible)
- Separate system/user message contexts
- Input validation (no special tokens)
- Output filtering

**Applicability:** 🔴 Critical for user-facing agents

**Status:** ⚠️ Not yet implemented in v6.0 (planned for security hardening)

---

## 5. 🎯 Intent Classification

**Problem:** The system spends expensive resources on simple queries.

**Solution:**
```python
class RouterAgent(BaseAgent):
    async def classify(self, query: str) -> RoutingDecision:
        # Fast heuristics first (rule-based)
        if len(query.split()) <= 5 and "?" in query:
            return RoutingDecision(
                target="quick_response_agent",
                complexity=2,
                reason="Simple question"
            )

        # LLM triage for ambiguous cases
        triage_result = await self.llm_service.generate(
            prompt=self.triage_prompt.format(query=query)
        )

        return self._parse_triage_result(triage_result)
```

**Benefits:**
- 70% cost reduction for simple queries (Flash vs Thinking model)
- Better UX (faster simple responses)
- Resource optimization

**Applicability:** 🟢 Implemented in v6.0

**Implemented in:**
- `src/agents/core/router_agent.py` - Hybrid LLM + rule-based routing

See: [Hybrid Router](../05_building_blocks/hybrid_router/README.md)

---

## 6. 💾 Response Caching

**Problem:** Identical queries repeatedly call the expensive API.

**Solution:**
```python
class ResponseCache:
    def __init__(self, redis_client):
        self._redis = redis_client

    async def get(self, query: str, user_id: str) -> Optional[str]:
        # Hash query for key
        cache_key = f"response:{user_id}:{hashlib.md5(query.encode()).hexdigest()}"

        cached = await self._redis.get(cache_key)
        if cached:
            logger.info(f"Cache hit for query: {query[:50]}...")
            return cached

        return None

    async def set(self, query: str, user_id: str, response: str, ttl_seconds: int = 3600):
        cache_key = f"response:{user_id}:{hashlib.md5(query.encode()).hexdigest()}"
        await self._redis.setex(cache_key, ttl_seconds, response)
```

**Cache Strategy:**
- **Factual queries**: TTL = 24 hours
- **Personal data**: TTL = 0 (no cache)
- **Web search**: TTL = 1 hour
- **Calculations**: TTL = infinite

**Cache Invalidation:**
- On fact updates
- On user preference changes
- Manual purge command

**Applicability:** 🟡 Important for cost optimization

**Status:** ⚠️ Not yet implemented in v6.0 (planned with Redis)

**Partial Implementation:**
- `src/adapters/firestore_repo.py` - Biographical context cache (10ms reads)

---

## 7. 🔄 Graceful Degradation

**Problem:** If the primary LLM/service is unavailable, the entire system goes down.

**Solution:**
```python
class ProviderRegistry:
    def get_provider(self, tier: PerformanceTier) -> LLMService:
        """Get provider with fallback logic."""
        try:
            # Try primary provider for tier
            return self._get_primary_provider(tier)
        except Exception as e:
            logger.warning(f"Primary provider failed: {e}")

            # Fallback to next tier
            fallback_tier = self._get_fallback_tier(tier)
            return self._get_primary_provider(fallback_tier)
```

**Fallback Hierarchy:**
1. Primary provider (Gemini) with tier-specific model
2. Fallback to lower tier model (PERFORMANCE → BALANCED → ECO)
3. Circuit breaker auto-disables failing agents
4. Friendly error message if all fail

**Applicability:** 🔴 Critical for reliability

**Implemented in:**
- `src/services/provider_registry.py` - Multi-provider support
- `src/agents/base_agent.py` - Circuit breaker pattern

See: [Provider Resolution](../05_building_blocks/provider_resolution/README.md)

---

## 8. 📝 Audit Logging

**Problem:** No transparency — what was done, when, and by whom.

**Solution:**
```python
class AuditLogger:
    async def log_action(
        self,
        user_id: str,
        action: str,
        resource: str,
        changes: dict,
        reason: str = ""
    ):
        audit_entry = {
            "timestamp": datetime.now(timezone.utc),
            "user_id": user_id,
            "action": action,  # CREATE, UPDATE, DELETE, ACCESS
            "resource": resource,  # fact, session, etc.
            "changes": changes,
            "reason": reason,
            "metadata": {
                "ip": request.client.host if request else None,
                "user_agent": request.headers.get("user-agent") if request else None,
            }
        }

        # Append-only collection (immutable)
        await self._firestore.collection('audit_log').add(audit_entry)
```

**GDPR Compliance:**
- Right to access (export all audit logs for user)
- Right to erasure (delete user data + audit trail)
- Retention policy (keep logs for X years)

**Applicability:** 🟡 Important for compliance

**Status:** ⚠️ Not yet implemented in v6.0 (planned for enterprise features)

---

## 9. 🧪 Canary Deployments

**Problem:** Deploying a new version to all users at once is a risk of mass failures.

**Solution:**
```yaml
# Cloud Run traffic splitting
# cloudbuild.yaml
steps:
  - name: 'gcr.io/cloud-builders/gcloud'
    args:
      - 'run'
      - 'deploy'
      - 'alek-bot'
      - '--image=gcr.io/project/alek:${SHORT_SHA}'
      - '--revision-suffix=${SHORT_SHA}'
      - '--no-traffic'  # Don't send traffic to new revision yet

  - name: 'gcr.io/cloud-builders/gcloud'
    args:
      - 'run'
      - 'services'
      - 'update-traffic'
      - 'alek-bot'
      - '--to-revisions=LATEST=10,PREVIOUS=90'  # 10% canary
```

**Monitoring During Canary:**
- Error rate comparison (canary vs stable)
- Latency comparison
- User feedback/reports
- Auto-rollback on spike

**Applicability:** 🟢 Recommended for safe deployments

**Status:** ⚠️ Not yet implemented (manual rollback only via Cloud Run revisions)

---

## 10. 🎨 User Feedback & Transparency

**Problem:** The user does not understand what the system is doing ("black box").

**Solution:**
```python
class SmartResponse:
    def __init__(self, text: str, structured_data: Optional[dict] = None):
        self.text = text
        self.structured_data = structured_data  # For rich content

    def add_metadata(self, agents_used: List[str], confidence: float):
        """Add transparency metadata to response."""
        if confidence < 0.7:
            self.text += f"\n\n⚠️ _Confidence: {confidence:.0%} - please verify_"

        if agents_used:
            self.text += f"\n\n_Agents used: {', '.join(agents_used)}_"
```

**Real-time Status Updates:**
```python
# In ConversationHandler:
await response_channel.send_status_with_phrase(StatusType.THINKING)
await response_channel.send_status_with_phrase(StatusType.PROCESSING_FILE)
await response_channel.send_status_with_phrase(StatusType.SEARCHING)
# ... final response
```

**Applicability:** 🟢 Implemented in v6.0

**Implemented in:**
- `src/domain/messaging.py` - SmartResponse with metadata
- `src/domain/ui_messages.py` - StatusType enum
- `src/locales/uk.py` - Localized status phrases

See: [Rich Content Protocol](../05_building_blocks/rich_content_protocol/README.md)

---

## 11. 🧠 Thinking Model Latency Control

**Problem:** Thinking models (Gemini Pro Preview, o1) have internal reasoning overhead that scales with tool count and context size. Adding one unnecessary tool can double Turn 1 latency.

**Rule: Tool Count Matters**

Every tool in the declaration forces the thinking model to reason "should I call this?" on every turn, even in `AUTO` mode. Measured impact (gemini-3-pro-preview, 17K token context):

| Tool count | Turn 1 latency |
|------------|---------------|
| 3 tools (search_memory, web_search, deliver_response) | ~57s |
| 2 tools (search_memory, web_search) | ~13s |

**Solution:** Keep tool declarations minimal. Never add a tool just for structured output — use `response_schema` + `response_mime_type` at the API level instead.

**Rule: Never Retry on Timeout (Thinking Models)**

```python
# ❌ Wrong — retry doubles wall-time to 5 min
max_retries=2, timeout_ms=90000

# ✅ Right — fail fast, surface the error
max_retries=0, timeout_ms=150000
```

**Rule: Fire-and-Forget Postprocessing**

Any non-blocking work that runs after a response (summary generation, analytics) must use `asyncio.create_task()` — never `await` before returning to the user:

```python
# ❌ Blocks response delivery by 1-2s
summary = await self._generate_summary(text)
return AgentResponse(metadata={"response_summary": summary})

# ✅ User sees response immediately; summary runs concurrently
task = asyncio.create_task(self._generate_summary(text))
return AgentResponse(metadata={"response_summary_task": task})

# ConversationHandler resolves after Slack delivery:
await response_channel.send(text)   # ← user sees this first
summary = await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
```

**`asyncio.shield()` is critical:** prevents task cancellation if the handler times out waiting for the summary. Fallback to full text on any error — never lose user data.

**Rule: Unified `response_summary` Key**

Both Quick and Smart agents converge on a single field name for history compression:
- QuickAgent JSON output: `{"full_response": "...", "response_summary": "..."}`
- SmartAgent postprocessing task result: `response_summary`
- `parse_llm_response()` parser: reads `data.get("response_summary")`
- `ConversationHandler` session save: uses `response_summary` from metadata

Never use `history_summary` — it's a retired alias.

**Applicability:** 🔴 Critical for thinking model deployments

**Implemented in:**
- `src/agents/core/smart_response_agent.py` — `_generate_history_summary()`, async task creation
- `src/handlers/conversation_handler.py` — task resolution with `asyncio.shield()`
- `src/utils/llm_response_parser.py` — unified `response_summary` parsing

---

## 📊 Summary: Priority Matrix

| Practice | Priority | Status v6.0 | Impact | Effort |
|----------|----------|-------------|--------|--------|
| Idempotency Keys | 🔴 P0 | ✅ Implemented | High | Low |
| Prompt Injection Defense | 🔴 P0 | ⏳ Planned | High | Medium |
| Cost Control | 🔴 P0 | ✅ Implemented | High | Medium |
| Graceful Degradation | 🔴 P0 | ✅ Implemented | High | Medium |
| Thinking Model Latency Control | 🔴 P0 | ✅ Implemented | High | Low |
| Telemetry & Observability | 🟡 P1 | ✅ Implemented | High | High |
| Intent Classification | 🟡 P1 | ✅ Implemented | Medium | Medium |
| Response Caching | 🟡 P1 | ⏳ Planned | Medium | Medium |
| Audit Logging | 🟡 P1 | ⏳ Planned | Medium | Low |
| Canary Deployments | 🟢 P2 | ⏳ Planned | Medium | High |
| User Transparency | 🟢 P2 | ✅ Implemented | Low | Low |

**Legend:**
- ✅ Implemented - Production ready
- ⏳ Planned - Scheduled for future milestone
- ❌ Not Started

---

## 🚀 Implementation Roadmap

### Phase 1: Critical Safety (Completed)
1. ✅ Idempotency Keys (Firestore dedup store)
2. ✅ Cost Control & Budgets (Quota service)
3. ✅ Graceful Degradation (Provider registry + circuit breaker)

### Phase 2: Optimization (Completed)
4. ✅ Intent Classification (Hybrid router)
5. ✅ Telemetry & Observability (OpenTelemetry + Cloud Trace)
6. ✅ User Transparency (Status updates + localization)

### Phase 3: Security Hardening (Planned)
7. ⏳ Prompt Injection Defense
8. ⏳ Response Caching (Redis integration)
9. ⏳ Audit Logging

### Phase 4: Enterprise Features (Planned)
10. ⏳ Canary Deployments
11. ⏳ GDPR Compliance
12. ⏳ Advanced monitoring & alerting

---

## ✅ Checklist: Production-Ready Agent

- [x] 🔒 Idempotency for all state-changing operations
- [ ] 🔐 Prompt injection defense at input
- [x] 💰 Cost limits and budget tracking
- [x] 🔄 Multi-provider fallback
- [x] 📊 OpenTelemetry spans for all operations
- [x] 🎯 Intent classification for route optimization
- [x] 🧠 Thinking model latency control (tool count, async postprocessing, response_summary)
- [ ] 💾 Redis cache for repeated queries
- [ ] 📝 Audit log for all changes
- [ ] 🧪 Canary deployment strategy
- [x] 🎨 Transparent UI with status updates

**v6.0 Compliance:** 7/11 practices implemented ✅

---

**Last Updated:** 2026-02-18
**Status:** ✅ Current (v6.0 compliance documented)
