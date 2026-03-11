# RFC: Web Scout Agent — Proactive Web Intelligence

**Status:** DRAFT
**Date:** 2026-03-03
**Owner:** AI Engineering
**Milestone:** Proactive Agents Phase 1

**Depends on:** Agent delegation infrastructure (existing), WorkerHandler (existing)
**Parallel with:** Email Secretary RFC (future)

---

## 1. Problem Statement

Alek-Core is reactive — it only retrieves information when the user asks. For topics the user
follows regularly (news, markets, domain-specific developments), this means asking the same
questions repeatedly. The system has no mechanism to surface relevant information proactively.

**Desired outcome:** A scheduled agent reads current web information on the user's interest
topics, compiles a digest, and stores it so the user can access it on demand or on a push schedule.

---

## 2. Architecture

### 2.1 Overview

```
Cloud Scheduler (cron)
  → Cloud Tasks (task_type="web_scout")
    → WorkerHandler._handle_web_scout(payload)
      → WebScoutAgent.run(user_id)
          │
          Phase 1: Topic Selection (1 LLM call, no tools)
          │   Input: session history summary + biographical context
          │           + user.web_search_preferences
          │   Output: List[{topic, search_query}]  (count ≤ WebScoutConfig.max_topics)
          │
          Phase 2: Deep Research (per topic, MIN_PASSES enforced by Python)
          │   for each topic:
          │     Pass 1..N:
          │       LLM generates search query (or refines previous)
          │       → search_web intent → WebSearchAgent via coordinator
          │       → result appended to topic context
          │     Python blocks completion until pass_count >= min_research_passes
          │     LLM drives query direction; Python enforces depth
          │   Output: List[{topic, findings_summary, queries_used}]
          │
          Phase 3: Digest Composition (1 LLM call, no tools)
              Input: all findings from Phase 2
              Output: BulletinBoardItem → Firestore
```

### 2.2 Delegation

Phase 2 uses the standard coordinator delegation pattern — identical to how SmartAgent
calls WebSearchAgent. Web Scout sends `AgentMessage(intent="search_web", payload={"query": "..."})`.
No changes to WebSearchAgent or AgentCoordinator.

### 2.3 Retrieval

```
User: "покажи свіже" / "що нового?"
  → Quick/Smart → get_fresh_info intent
    → BulletinBoardReaderAgent
        reads unread BulletinBoardItems for user_id (newest first)
        formats digest
        marks items as read
      → Quick delivers to user
```

`BulletinBoardReaderAgent` is a thin read-format-mark agent. No LLM needed — pure Firestore
read + formatting. Alternatively, reading can be handled inline in ConversationHandler
(same trade-off as `history_addendum` RFC — prefer specialist agent for composability).

---

## 3. Domain Model

### 3.1 BulletinBoardItem

```python
# src/domain/bulletin_board.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class BulletinBoardItem:
    item_id: str                          # uuid4
    user_id: str
    agent_id: str                         # "web_scout_agent"
    source_type: str                      # "web_scout" | "email_secretary" (future)
    created_at: datetime
    expire_at: datetime                   # Firestore TTL field (auto-delete)
    content: str                          # formatted digest text (Slack mrkdwn)
    read_at: Optional[datetime] = None    # None = unread
    metadata: Dict[str, Any] = field(default_factory=dict)
    # metadata example:
    # {
    #   "topics_searched": ["AI regulation EU", "Bitcoin ETF"],
    #   "total_searches": 9,
    #   "run_duration_s": 47
    # }
```

**TTL:** `expire_at = created_at + timedelta(days=WebScoutConfig.bulletin_ttl_days)`.
Firestore deletes documents automatically when `expire_at` is reached.
The field name `expire_at` must match the TTL policy configured on the collection in GCP Console.

### 3.2 Web Scout Run State (internal, not persisted)

```python
@dataclass
class TopicResearch:
    topic: str
    queries_used: List[str]
    findings: List[str]        # raw WebSearchAgent results per pass
    pass_count: int = 0
```

---

## 4. Port

```python
# src/ports/bulletin_board_repository.py

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional
from ..domain.bulletin_board import BulletinBoardItem


class BulletinBoardRepository(ABC):

    @abstractmethod
    async def save_item(self, item: BulletinBoardItem) -> None: ...

    @abstractmethod
    async def get_unread_items(
        self, user_id: str, limit: int = 10
    ) -> List[BulletinBoardItem]: ...

    @abstractmethod
    async def mark_read(self, item_ids: List[str], read_at: datetime) -> None: ...

    @abstractmethod
    async def get_items(
        self,
        user_id: str,
        since: Optional[datetime] = None,
        limit: int = 20,
    ) -> List[BulletinBoardItem]: ...
```

Firestore adapter: `src/adapters/firestore_bulletin_board_adapter.py`.
Collection: `{env}_domain_bulletin_board_v1`.
Index required: `user_id ASC + read_at ASC + created_at DESC`.

---

## 5. Agent Configuration

Two layers:

### 5.1 System defaults — `WebScoutConfig` (dev-level, `agent_config.py`)

Infrastructure parameters that are not user-facing:

```python
# src/infrastructure/agent_config.py — add to existing file

@dataclass
class WebScoutConfig:
    min_research_passes: int = 3      # per topic: initial search + min 2 follow-ups
    bulletin_ttl_days: int = 90       # BulletinBoardItem lifetime (debug; reduce in prod)


WEB_SCOUT = WebScoutConfig()
```

### 5.2 User preferences — `ScoutPreferences` (user-level, `UserProfile`)

Parameters the user can tune — stored on the user document, same pattern as `PromptPreferences`:

```python
# src/domain/user.py — new nested model

class ScoutPreferences(BaseModel):
    max_topics: int = 5           # total topics per run
    context_topics: int = 1       # slots reserved for session/bio context signals
                                  # preference_topics = max_topics - context_topics

# Added to UserProfile:
scout_preferences: ScoutPreferences = Field(default_factory=ScoutPreferences)
```

`WebScoutAgent._run_scout()` reads `user_profile.scout_preferences` and falls back to
`ScoutPreferences()` defaults if the field is absent. The user can set `max_topics=10,
context_topics=2` for an extended weekly digest without any code change.

No UI for now — set via Firestore directly or future Cabinet UI.

---

## 6. User Profile Extension

`web_search_preferences: List[str]` added to `UserProfile` domain model.

```python
# src/domain/user.py — add field to UserProfile
web_search_preferences: List[str] = Field(default_factory=list)
# Example: ["AI regulation EU", "Bitcoin price", "Formula 1 2026"]
```

Stored on the user Firestore document. No UI for now — set manually or via future intent.

---

## 7. Agent Implementation

### 7.1 Class Structure

```
src/agents/web_scout_agent.py
  class WebScoutAgent(BaseAgent):
    WEB_SCOUT_CONFIG = WEB_SCOUT          # from agent_config.py
    _descriptor: AgentDescriptor          # from agent_manifest.py

    async def process(message: AgentMessage) -> AgentResponse:
        user_id = message.context["user_id"]
        await self._run_scout(user_id)

    async def _run_scout(user_id):
        context = await self._build_context(user_id)     # bio + history + preferences
        topics  = await self._phase1_select_topics(context)
        results = await self._phase2_deep_research(topics)
        await   self._phase3_write_digest(user_id, results)

    async def _phase2_deep_research(topics) -> List[TopicResearch]:
        for topic in topics:
            research = TopicResearch(topic=topic.topic, ...)
            while research.pass_count < WEB_SCOUT_CONFIG.min_research_passes:
                query = await self._generate_next_query(research)
                result = await self._delegate_search(query)   # search_web via coordinator
                research.findings.append(result)
                research.pass_count += 1
            results.append(research)
        return results
```

### 7.2 Context Building

Phase 1 LLM call receives:
1. **Session history summary** — last N model responses from the user's most recently active
   session. "Most recently active" is resolved via `user_notification_state` collection (already
   exists): the channel stored there corresponds to the last platform the user interacted on
   (Slack or Telegram), which maps to a session. No per-platform special-casing needed — follow
   the channel, get the session. Summarized model responses only (not full messages) to keep
   token count bounded.
2. **Biographical context** — `get_biographical_context_cached(user_id)` (same as Quick/Smart).
3. **`web_search_preferences`** — raw list from user profile.

### 7.3 Prompt Design

Three dedicated Firestore tokens required:

| Token class | Purpose |
|---|---|
| `COGNITIVE_PROCESS_WEB_SCOUT_TOPIC` | Phase 1: topic selection from context + preferences |
| `COGNITIVE_PROCESS_WEB_SCOUT_RESEARCH` | Phase 2: query refinement based on previous findings |
| `COGNITIVE_PROCESS_WEB_SCOUT_DIGEST` | Phase 3: synthesis from all findings |

All three follow standard PromptBuilder v3 pattern. Agent type: `"web_scout"`.

---

## 8. Scheduling

### 8.1 Cloud Tasks Trigger

New `task_type = "web_scout"` in `WorkerHandler`.

```python
# WorkerHandler.handle() — add branch:
elif task_type == "web_scout":
    return await self._handle_web_scout(payload)

async def _handle_web_scout(self, payload: dict) -> Tuple[dict, int]:
    user_id = payload.get("user_id")
    if not user_id:
        return {"error": "missing user_id"}, 400
    await self._web_scout_agent.run(user_id)
    return {"status": "ok"}, 200
```

### 8.2 Cloud Scheduler

One job per active user (or one global job that fans out per user — same pattern as
`email_indexing_watchdog`). Schedule: TBD (start with 2× daily, morning + evening).

Payload: `{"task_type": "web_scout", "user_id": "..."}`.

---

## 9. Retrieval: get_fresh_info Intent

`BulletinBoardReaderAgent` — minimal agent (or inline ConversationHandler logic):

1. Read unread `BulletinBoardItem`s for `user_id` (newest first, limit=5).
2. Format into readable digest.
3. Mark items as read.
4. Return to Quick/Smart as delegation result.

`get_fresh_info` registered in `AgentDescriptor` with `internal=False` → visible to both Quick
and Smart LLMs as an available intent. Description: "Retrieve fresh proactive intelligence
digest: web news and research compiled since last check."

---

## 10. Implementation Phases

### Phase 1 — Core Infrastructure
- [ ] `BulletinBoardItem` domain model (`src/domain/bulletin_board.py`)
- [ ] `BulletinBoardRepository` port (`src/ports/bulletin_board_repository.py`)
- [ ] `FirestoreBulletinBoardAdapter` (`src/adapters/firestore_bulletin_board_adapter.py`)
- [ ] `web_search_preferences` field on `UserProfile`
- [ ] `WebScoutConfig` in `agent_config.py`
- [ ] Firestore TTL policy configured on `{env}_domain_bulletin_board_v1`

### Phase 2 — WebScoutAgent
- [ ] `WebScoutAgent` class (`src/agents/web_scout_agent.py`)
- [ ] `AgentDescriptor` in `agent_manifest.py`
- [ ] `AgentProviderStrategy` entry for `"web_scout"` in `agent_context_builder.py`
- [ ] PromptBuilder tokens uploaded to Firestore (3 tokens: topic, research, digest)
- [ ] `WorkerHandler` — add `web_scout` task_type branch
- [ ] `UserAgentFactory` — instantiate and wire `WebScoutAgent`
- [ ] Unit tests

### Phase 3 — Retrieval
- [ ] `BulletinBoardReaderAgent` (or inline ConversationHandler logic)
- [ ] `get_fresh_info` intent registered
- [ ] E2E test: scout run → bulletin board write → retrieval

### Phase 4 — Scheduling
- [ ] Cloud Scheduler job(s) configured
- [ ] Monitoring: log scout run duration, topics searched, items written

---

## 11. Trade-offs

| Concern | Decision |
|---|---|
| Push vs pull delivery | Pull (on-demand `get_fresh_info`) first; push (scheduled morning delivery) in Phase 4 |
| BulletinBoardReaderAgent vs inline | Dedicated agent — composable when Email Secretary adds second source_type |
| Session history in Phase 1 | Summarized model responses only (not full messages) — avoids token bloat |
| Mandatory MIN_PASSES in Python | Python enforces depth; LLM drives direction. Prevents premature stopping without prompt hacking. |
| `web_search_preferences` storage | On user document (simple). Future: dynamic update via user intent. |
| Scout behavior params (`max_topics`, `context_topics`) | Per-user `ScoutPreferences` on `UserProfile` — not dev config. System-level `WebScoutConfig` holds only infrastructure defaults (`min_research_passes`, `bulletin_ttl_days`). |
| TTL | 90 days (debug). Reduce to 7–30 days in production once behavior is validated. |

---

## 12. Open Questions

1. **Per-user scheduling**: one Cloud Scheduler job per user vs. one global job with fan-out.
   At current scale (single user): one job. Multi-user: watchdog-style global job that
   iterates active users.

---

## 13. Concerns

### 13.1 Topic Deduplication Across Runs

If the scout runs twice daily (morning + evening), Phase 1 may select the same topics both
times. Two behaviors are possible:

- **No dedup (current default):** Both runs research the same topic independently. Evening run
  gets fresher results. Simple, predictable.
- **Dedup:** Phase 1 reads today's existing `BulletinBoardItems` before selecting topics and
  avoids re-researching topics already covered. Reduces redundancy; may miss intraday updates
  on fast-moving topics.

**Unresolved.** Requires observing real run behavior before deciding. The `metadata.topics_searched`
field on `BulletinBoardItem` provides the data needed to implement dedup when the time comes.
No code change needed in Phase 1–3; dedup logic lives entirely in Phase 1 topic selection.
