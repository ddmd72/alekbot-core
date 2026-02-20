# Rich Content Protocol (Building Block)

## 📖 HowTo: Using This Document

### Purpose
Define how structured (non-text) responses flow through the Actor Model without leaking platform details.

### When to Read
- **For AI Agents:** Before changing agent-to-agent payloads or SmartResponse handling.
- **For Developers:** When adding new rich content types or rendering logic in adapters.

### When to Update
This document MUST be updated when:
- [ ] RichContent schema changes.
- [ ] SmartResponse routing rules change.
- [ ] New structured content types are added.

### Cross-References
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Slack Dual Mode:** [../slack_dual_mode/README.md](../slack_dual_mode/README.md)

---

## 1. Overview
Rich Content Protocol extends the SmartResponse pipeline so agents can return
structured payloads alongside plain text. The protocol remains platform-agnostic;
adapters decide how to render the structured data.

## 2. Core Domain DTOs
Located in `src/domain/messaging.py`:

```python
@dataclass
class RichContent:
    content_type: str
    data: Dict[str, Any]
    fallback_text: str

@dataclass
class SmartResponse:
    text: str
    structured_data: Optional[RichContent] = None
```

## 3. Actor Model Flow
1. Tool/agent produces structured output.
2. Smart agent wraps it in `SmartResponse`.
3. `AgentResponse.result` carries `SmartResponse` across agents.
4. `ConversationHandler` routes:
   - `structured_data` → `ResponseChannel.send_rich_content()`
   - otherwise → `send_chunked_message()`

## 4. Content Types
Current examples:
- `table`
- `weather`
- `chart`

Each type includes a schema in `data` and a `fallback_text` for clients without rich UI support.

## 5. Adapter Responsibilities
- Render `RichContent` per platform (Slack Block Kit, Web UI, etc.).
- Use `fallback_text` when rich rendering is unavailable.
- Do not introduce UI logic into agents or domain layers.

## 6. Legacy Note
`BrainService` (legacy) still references the old Smart Pass flow. The Actor Model
path is the canonical source moving forward.

## 7. Code References
- `src/domain/messaging.py`
- `src/domain/agent.py`
- `src/handlers/conversation_handler.py`
- `src/adapters/slack/response_channel.py`

## 8. Status
**Production Ready** (SmartResponse + RichContent in Actor Model pipeline).