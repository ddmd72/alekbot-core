# RFC: DeliveryItems — Agent Response Delivery Layer

**Status:** Phase 1 Implemented
**Date:** 2026-03-04
**Implemented:** 2026-03-05
**Owner:** Solo Dev

---

## 1. Problem

`AgentResponse` has no structured transport for arbitrary deliverable objects.
Currently every "extra thing" to deliver requires a one-off hack:

| What | How today | Problem |
|------|-----------|---------|
| Rich content (table, widget, file) | `SmartResponse.structured_data` — single object | Cannot deliver >1 item; separate from response flow |
| Email search context | `metadata["email_search_context"]` → appended to history text | Leaks delivery concern into LLM history |
| Grounding attribution widget | No path — lost at orchestrator boundary | Feature blocked |

Root cause: there is no typed, aggregatable list of "things to deliver to the user" in `AgentResponse`.

---

## 2. Design

### 2.1 DeliveryItem

```python
# src/domain/agent.py

@dataclass
class DeliveryItem:
    type: str               # "rich_content", "html_gcs_link", "message"
    data: Dict[str, Any]    # arbitrary payload, type-specific
```

**Known types (initial):**

| type | data keys | Handler in ConversationHandler |
|------|-----------|-------------------------------|
| `rich_content` | `content_type`, `data`, `fallback` | existing `_deliver_rich_content` |
| `html_gcs_link` | `html`, `filename`, `link_text` | `_store_html` → `send_message("<url\|link_text>")` |
| `message` | `text` | `send_message(text)` |

New types added by registering a handler in ConversationHandler — no other changes needed.

### 2.2 AgentResponse

```python
@dataclass
class AgentResponse:
    # existing fields unchanged
    ...
    delivery_items: List[DeliveryItem] = field(default_factory=list)
```

Backward compatible — default empty list.

### 2.3 Orchestrator aggregation (Quick / Smart)

When an orchestrator delegates to a specialist, it transparently collects `delivery_items`
from every sub-agent response and merges them into its own `AgentResponse.delivery_items`.

```python
# In _delegate_quick / delegation loop:
sub_response = await coordinator.handle_delegation(...)
self._pending_delivery_items.extend(sub_response.delivery_items)

# On final return:
return AgentResponse.success(..., delivery_items=self._pending_delivery_items)
```

No orchestrator needs to know what types are in the list — pure pass-through.

### 2.4 ConversationHandler

```python
# After main text is delivered, before history save:
for item in response.delivery_items:
    await self._deliver_item(item, response_channel, context.thread_id)
```

```python
async def _deliver_item(self, item: DeliveryItem, ...) -> None:
    if item.type == "rich_content":
        content = _to_rich_content(item.data)
        await self._deliver_rich_content(content, ...)
    elif item.type == "html_gcs_link":
        url = await self._rich_content_service._store_html(
            item.data["html"], item.data.get("filename", "content.html")
        )
        if url:
            link_text = item.data.get("link_text", "View details")
            await response_channel.send_message(f"<{url}|{link_text}>", thread_id)
    elif item.type == "message":
        await response_channel.send_message(item.data["text"], thread_id)
    else:
        logger.warning("Unknown DeliveryItem type: %s — skipping", item.type)
```

---

## 3. Migration

### Phase 1 (this RFC) — Add layer, first use cases ✅ Implemented
- [x] Add `DeliveryItem` to `domain/agent.py`
- [x] Add `delivery_items` to `AgentResponse`
- [x] Wire aggregation in Quick + Smart delegation loops
- [x] Wire processing in `ConversationHandler` (`_deliver_item` dispatcher)
- [x] **Use case 1:** Grounding attribution widget from `WebSearchAgent` / `WebSearchLightAgent`
      (disabled by default behind `ENABLE_GROUNDING_ATTRIBUTION=false` flag — Google ToS attribution
      chip; enable when going multi-user)
- [x] **Use case 2:** Google Maps widget from `MapsSearchAgent` — when `google_maps_widget_context_token`
      is returned, generates HTML with `<gmp-place-contextual>` (Maps JS API `v=alpha`), uploads to GCS,
      sends "📍 Open Map" link to user. Requires `enable_widget=True` in `types.GoogleMaps()`.
      See [MAPS_SEARCH_RFC.md](MAPS_SEARCH_RFC.md) §11.

### Phase 2 (future) — Migrate rich_content
- `SmartResponse.structured_data` → `DeliveryItem(type="rich_content", ...)`
- Remove `SmartResponse` and `structured_data` as separate path
- `email_search_context` stays in history metadata (it feeds the LLM prompt, not delivery)

Phase 2 is not blocked by Phase 1. Both can coexist: `delivery_items` processed first,
then existing `structured_data` path as before.

---

## 4. Scope (Phase 1)

**Files touched:**

1. `src/domain/agent.py` — `DeliveryItem` dataclass + field in `AgentResponse`
2. `src/agents/web_search_agent.py` — grounding attribution → `DeliveryItem(type="html_gcs_link", ...)` (behind `ENABLE_GROUNDING_ATTRIBUTION` flag)
3. `src/agents/web_search_light_agent.py` — same flag, Quick path
4. `src/agents/maps_search_agent.py` — maps widget HTML → `DeliveryItem(type="html_gcs_link", ...)`
5. `src/agents/core/quick_response_agent.py` — aggregate `delivery_items` in delegation loop
6. `src/agents/core/smart_response_agent.py` — same
7. `src/handlers/conversation_handler.py` — `_deliver_item` dispatcher + loop after main delivery
8. `src/composition/user_agent_factory.py` — `types.GoogleMaps(enable_widget=True)` for maps widget token

**Not touched:** `SmartResponse`, `structured_data`, `email_search_context`, all adapters.

---

## 5. Non-goals

- Telegram adapter changes (same `delivery_items` list, different platform adapter handles types it supports)
- Deep research / async delivery patterns — separate RFC
- LLM-facing schema changes — none needed
