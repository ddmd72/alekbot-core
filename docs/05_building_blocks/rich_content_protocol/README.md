# Rich Content Protocol (Building Block)

## Purpose

Define how structured (non-text) responses flow from agents through the application layer
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
- [ ] New renderer port/adapter added (e.g., PDF renderer)

## Cross-References

- **RFC:** [../../10_rfcs/HTML_CARD_PLAYWRIGHT_RFC.md](../../10_rfcs/HTML_CARD_PLAYWRIGHT_RFC.md)
- **Rich Content RFC (original):** [../../10_rfcs/RICH_CONTENT_RFC.md](../../10_rfcs/RICH_CONTENT_RFC.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)

---

## 1. Overview

Both SmartAgent and QuickAgent return a `rich_content` object alongside `full_response`
text. The protocol routes that object to the correct delivery mechanism without agents
knowing about Slack, Telegram, or rendering details.

---

## 2. Domain DTO

Located in `src/domain/messaging.py`:

```python
@dataclass
class RichContent:
    content_type: str        # "file", "table", "html_card", "map_image"
    data: Dict[str, Any]     # type-specific payload
    fallback_text: str       # plain text if rich delivery unavailable
```

---

## 3. Flow

```
Agent JSON output
  rich_content: {"type": "...", "data": {...}, "fallback": "..."}

ConversationHandler._deliver_rich_content()
  ├─ type in {"file", "map_image", "html_card"} AND rich_content_service present AND channel_id available
  │     → RichContentService.process(content, channel_id)
  │
  │          file path:
  │          ├─ "file" (.md/.html/.txt)  → encode UTF-8 → PlatformMediaPort.upload_file()
  │          ├─ "file" (.xlsx)           → CSV → openpyxl → PlatformMediaPort.upload_file()
  │          ├─ "file" (.docx)           → Markdown → python-docx → PlatformMediaPort.upload_file()
  │
  │          html_card path:
  │          └─ "html_card"             → HtmlRendererPort.render(html) → PNG bytes
  │                                        → PlatformMediaPort.upload_image(png_bytes)
  │                                        (returns None — no URL, direct binary upload)
  │
  └─ otherwise (type="table", no service, no channel_id)
        → response_channel.send_rich_content()  [Block Kit / fallback text]

PlatformMediaPort
  ├─ SlackMediaAdapter  → files_upload_v2  ✅ implemented
  └─ TelegramMediaAdapter → sendDocument / sendPhoto   ⏳ deferred
```

**Key distinction — upload vs URL:**
- `upload_file()` / `upload_image()` — binary bytes posted directly to platform API.
  Returns `None`. No intermediate URL.
- GCS-based types (`map_image`, `weather_image`) — generate a public GCS URL, return it
  as a string. `ConversationHandler` then sends the URL as a message; platform unfurls it.
- `html_card` uses direct `upload_image()` (no GCS, no URL). Image appears inline.

---

## 4. Content Types

| Type | Handler | LLM generates | Server action | Delivery method |
|---|---|---|---|---|
| `file` (.md / .html / .txt) | `RichContentService` | Full text string | Encode UTF-8 | Direct upload |
| `file` (.xlsx) | `RichContentService` | CSV string | `openpyxl` → bytes | Direct upload |
| `file` (.docx) | `RichContentService` | Markdown string | `python-docx` → bytes | Direct upload |
| `html_card` | `RichContentService` + `HtmlRendererPort` | Self-contained HTML | Playwright → PNG | Direct upload (inline image) |
| `table` | `response_channel.py` Block Kit | Structured data dict | Rendered as Slack blocks | In-chat blocks |
| `map_image` | `RichContentService` (M3, deferred) | `{"address": "..."}` | Google Maps Static API → GCS URL | URL message (unfurl) |

---

## 5. HTML Card Pipeline (Playwright)

The `html_card` type adds a rendering layer between the agent output and the platform upload:

```
Agent HTML string
  └─ HtmlRendererPort.render(html, width=480)
       └─ PlaywrightHtmlRenderer
            ├─ Browser singleton (lazy init, auto-reconnect)
            ├─ page.set_content(html, wait_until="networkidle")
            ├─ JS: measure content height (walk body.children → maxBottom)
            └─ page.screenshot(clip={width, content_height})
                 → PNG bytes (device_scale_factor=2, retina quality)

PNG bytes → PlatformMediaPort.upload_image(image_bytes, alt_text, channel_id)
  └─ SlackMediaAdapter → files_upload_v2 → inline image in Slack thread
```

**Why clip, not `body.screenshot()`:**
`document.body.getBoundingClientRect()` in Chrome returns the full viewport rectangle
(e.g., 0,0,480,800) regardless of actual content height. Using `body.screenshot()`
would capture 800px with white space below the card. The JS walk finds the real rendered
bottom of all body children, then `page.screenshot(clip=...)` clips exactly to that height.

**Bare fragment support:**
Agents often generate bare `<div>` fragments without `<html><body>` wrappers. Playwright
auto-wraps these; the clip approach works identically for both fragments and full documents.

**Feature flag:** `ENABLE_HTML_RENDERER=true` in `.env` or Cloud Run env vars.
When disabled: `html_renderer=None` → `html_card` is silently skipped (agent's
`full_response` text already conveyed the content).

---

## 6. Routing Logic in ConversationHandler

```python
_MEDIA_CONTENT_TYPES = frozenset({"weather_image", "map_image", "file", "html_card"})

async def _deliver_rich_content(self, content, response_channel, thread_id):
    if content.content_type in _MEDIA_CONTENT_TYPES and self._rich_content_service:
        channel_id = getattr(response_channel, "channel_id", None)
        if channel_id:
            url = await self._rich_content_service.process(content, channel_id)
            if url:
                # GCS-based types: URL sent as message → Slack/Telegram unfurls
                await response_channel.send_message(url, thread_id=thread_id)
            # html_card / file: upload_image/upload_file called internally → url is None
        else:
            await response_channel.send_rich_content(content, thread_id=thread_id)
    else:
        await response_channel.send_rich_content(content, thread_id=thread_id)
```

`table` is never in `_MEDIA_CONTENT_TYPES` → always goes to Block Kit renderer.

---

## 7. Adapter Responsibilities

- `PlatformMediaPort` implementations handle platform-specific upload API calls
- `HtmlRendererPort` implementations handle HTML → binary rendering
- Never introduce conversion or rendering logic into adapters — belongs in `RichContentService`
- Use `fallback_text` when `channel_id` is unavailable or service is not wired

---

## 8. Telegram (Deferred)

Telegram `ConversationHandler` is wired with `rich_content_service=None`.
`_deliver_rich_content()` falls back to `send_rich_content()` → delivers `fallback_text`
as a plain text message.

To enable Telegram media: implement `TelegramMediaAdapter(PlatformMediaPort)` and
wire it into the Telegram `ConversationHandler` in `main.py`.
`HtmlRendererPort` is platform-agnostic — same renderer works for both Slack and Telegram.

---

## 9. Code References

| File | Purpose |
|---|---|
| `src/domain/messaging.py` | `RichContent` dataclass |
| `src/ports/platform_media_port.py` | `PlatformMediaPort` ABC |
| `src/ports/html_renderer_port.py` | `HtmlRendererPort` ABC + `HtmlRenderError` |
| `src/services/rich_content_service.py` | Conversion + dispatch + html_card handler |
| `src/adapters/playwright_html_renderer.py` | `PlaywrightHtmlRenderer` (Chromium singleton) |
| `src/adapters/slack/media_adapter.py` | `SlackMediaAdapter` (`files_upload_v2`) |
| `src/handlers/conversation_handler.py` | `_deliver_rich_content()` routing |
| `src/config/settings.py` | `ENABLE_HTML_RENDERER` flag |
| `main.py` | Renderer lifecycle (lazy init, graceful stop) |
| `firestore_utils/uploads/OUTPUT_FORMAT_JSON.groovy` | LLM output format with `html_card` type |
| `scripts/test_html_render.py` | Local smoke test for Playwright rendering |

---

## 10. Status

**V2 — Production Ready (2026-02-26)**

Delivered:
- File delivery (md, html, txt, xlsx, docx) via direct Slack upload
- `html_card` — agent-generated HTML → Playwright PNG → inline Slack image

Not yet:
- GCS storage path (map images — M3)
- Telegram adapter (M6)
- PDF rendering (M5)

History:
- Added: 2026-02-25 (M1 weather_image + M2 file delivery)
- Updated: 2026-02-26 — `html_card` type + `HtmlRendererPort` + `PlaywrightHtmlRenderer`
