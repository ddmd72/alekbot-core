# Rich Content Protocol (Building Block)

## Purpose

Define how structured (non-text) responses flow from SmartAgent through the application layer
to platform-specific delivery — without leaking platform details into agents or domain.

## When to Read

- Before adding a new rich content type (new format, new media source)
- When modifying `ConversationHandler` rich content routing
- When adding a new platform adapter (Telegram, Web UI)

## When to Update

This document MUST be updated when:
- [ ] `RichContent` schema changes
- [ ] New content types added to `RichContentService`
- [ ] `ConversationHandler._deliver_rich_content()` routing logic changes
- [ ] New `PlatformMediaPort` adapter implemented

## Cross-References

- **RFC:** [../../10_rfcs/RICH_CONTENT_RFC.md](../../10_rfcs/RICH_CONTENT_RFC.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)

---

## 1. Overview

SmartAgent returns a `rich_content` object alongside `full_response` text. The protocol
routes that object to the correct delivery mechanism without agents knowing about Slack,
Telegram, or file formats.

---

## 2. Domain DTO

Located in `src/domain/messaging.py`:

```python
@dataclass
class RichContent:
    content_type: str        # "file", "table", "map_image"
    data: Dict[str, Any]     # type-specific payload
    fallback_text: str       # plain text if rich delivery unavailable
```

---

## 3. Flow

```
SmartAgent JSON output
  rich_content: {"type": "file", "data": {...}, "fallback": "File: name.ext"}

ConversationHandler._deliver_rich_content()
  ├─ type in {"file", "map_image"} AND rich_content_service present AND channel_id available
  │     → RichContentService.process(content, channel_id)
  │          ├─ "file" (.md/.html/.txt)  → encode UTF-8 → PlatformMediaPort.upload_file()
  │          ├─ "file" (.xlsx)           → CSV → openpyxl → PlatformMediaPort.upload_file()
  │          ├─ "file" (.docx)           → Markdown → python-docx → PlatformMediaPort.upload_file()
  │          └─ unknown type            → WARNING log, skip
  └─ otherwise (type="table", no service, no channel_id)
        → response_channel.send_rich_content()  [Block Kit / fallback text]

PlatformMediaPort
  ├─ SlackMediaAdapter  → files_upload_v2  ✅ implemented
  └─ TelegramMediaAdapter → sendDocument   ⏳ deferred
```

---

## 4. Content Types

| Type | Handler | LLM generates | Server action |
|---|---|---|---|
| `file` (.md / .html / .txt) | `RichContentService` | Full text string | Encode UTF-8, upload |
| `file` (.xlsx) | `RichContentService` | CSV string | `openpyxl` → bytes, upload |
| `file` (.docx) | `RichContentService` | Markdown string | `python-docx` → bytes, upload |
| `table` | `response_channel.py` Block Kit | Structured data dict | Rendered as Slack blocks in-chat |
| `map_image` | `RichContentService` (M3) | `{"address": "..."}` | Google Maps Static API → bytes |

---

## 5. Routing Logic in ConversationHandler

```python
_MEDIA_CONTENT_TYPES = frozenset({"file", "map_image"})

async def _deliver_rich_content(self, content, response_channel, thread_id):
    if content.content_type in _MEDIA_CONTENT_TYPES and self._rich_content_service:
        channel_id = getattr(response_channel, "channel_id", None)
        if channel_id:
            await self._rich_content_service.process(content, channel_id)
        else:
            await response_channel.send_rich_content(content, thread_id=thread_id)
    else:
        await response_channel.send_rich_content(content, thread_id=thread_id)
```

`table` is never in `_MEDIA_CONTENT_TYPES` → always goes to Block Kit renderer.

---

## 6. Adapter Responsibilities

- `PlatformMediaPort` implementations handle platform-specific upload API calls
- Use `fallback_text` when `channel_id` is unavailable or service is not wired
- Never introduce conversion logic into adapters — conversion belongs in `RichContentService`

---

## 7. Telegram (Deferred)

Telegram `ConversationHandler` is wired without `rich_content_service=None`.
`_deliver_rich_content()` falls back to `send_rich_content()` → delivers `fallback_text`
as a plain text message.

To enable Telegram media: implement `TelegramMediaAdapter(PlatformMediaPort)` and
wire it into the Telegram `ConversationHandler` in `main.py`.

---

## 8. Code References

- `src/domain/messaging.py` — `RichContent` dataclass
- `src/ports/platform_media_port.py` — `PlatformMediaPort` ABC
- `src/services/rich_content_service.py` — conversion + dispatch
- `src/adapters/slack/media_adapter.py` — `SlackMediaAdapter`
- `src/handlers/conversation_handler.py` — `_deliver_rich_content()` routing
- `firestore_utils/uploads/OUTPUT_FORMAT_JSON.groovy` — LLM trigger instructions

---

## 9. Status

**V1 — Production Ready**

Delivered: file delivery (md, html, xlsx, docx) via direct Slack upload.
Not yet: GCS storage, map images (M3), Telegram adapter (M6), PDF (M5).

Added: 2026-02-25 (M1 weather_image + M2 file delivery)
Updated: 2026-02-26 (weather_image removed, xlsx/docx/html added, V1 delivered)
