# Rich Content Protocol (Building Block)

## Purpose

Define how structured (non-text) responses flow from agents through the application layer
to platform-specific delivery — without leaking platform details into agents or domain.

## When to Read

- Before adding a new rich content type (new format, new media source)
- When modifying `ConversationHandler` rich content routing
- When adding a new platform adapter (Web UI, etc.)

## When to Update

This document MUST be updated when:
- [ ] `RichContent` schema changes
- [ ] New content types added to `RichContentService`
- [ ] `ConversationHandler._deliver_rich_content()` routing logic changes
- [ ] New `PlatformMediaPort` adapter implemented
- [ ] New renderer port/adapter added (e.g., PDF renderer)

## Cross-References

- **RFC:** [../../10_rfcs/WIDGET_PLAYWRIGHT_RFC.md](../../10_rfcs/WIDGET_PLAYWRIGHT_RFC.md)
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
    content_type: str        # "file", "table", "widget", "map_image"
    data: Dict[str, Any]     # type-specific payload
    fallback_text: str       # plain text if rich delivery unavailable
```

---

## 3. Flow

```
Agent JSON output
  rich_content: {"type": "...", "data": {...}, "fallback": "..."}

ConversationHandler._deliver_rich_content()
  ├─ type in {"file", "map_image", "widget"} AND rich_content_service present AND channel_id available
  │     → RichContentService.process(content, channel_id)
  │
  │          file path:
  │          ├─ "file" (.md/.html/.txt)  → encode UTF-8 → PlatformMediaPort.upload_file()
  │          ├─ "file" (.xlsx)           → CSV → openpyxl → PlatformMediaPort.upload_file()
  │          ├─ "file" (.docx)           → Markdown → python-docx → PlatformMediaPort.upload_file()
  │
  │          widget path:
  │          └─ "widget"             → HtmlRendererPort.render(html) → PNG bytes
  │                                        → PlatformMediaPort.upload_image(png_bytes)
  │                                        (returns None — no URL, direct binary upload)
  │
  └─ otherwise (type="table", no service, no channel_id)
        → response_channel.send_rich_content()  [Block Kit / fallback text]

PlatformMediaPort
  ├─ SlackMediaAdapter      → files_upload_v2             ✅ implemented
  └─ TelegramMediaAdapter   → sendPhoto / sendDocument    ✅ implemented
```

**Key distinction — upload vs URL:**
- `upload_file()` / `upload_image()` — binary bytes posted directly to platform API.
  Returns `None`. No intermediate URL.
- GCS-based types (`map_image`, `weather_image`) — generate a public GCS URL, return it
  as a string. `ConversationHandler` then sends the URL as a message; platform unfurls it.
- `widget` uses direct `upload_image()` (no GCS, no URL). Image appears inline.

---

## 4. Content Types

| Type | Handler | LLM generates | Server action | Delivery method |
|---|---|---|---|---|
| `file` (.md / .html / .txt) | `RichContentService` | Full text string | Encode UTF-8 | Direct upload |
| `file` (.xlsx) | `RichContentService` | CSV string | `openpyxl` → bytes | Direct upload |
| `file` (.docx) | `RichContentService` | Markdown string | `python-docx` → bytes | Direct upload |
| `widget` | `RichContentService` + `HtmlRendererPort` | Self-contained HTML | Playwright → PNG | Direct upload (inline image) |
| `table` | `response_channel.py` Block Kit | Structured data dict | Rendered as Slack blocks | In-chat blocks |
| `map_image` | `RichContentService` (M3, deferred) | `{"address": "..."}` | Google Maps Static API → GCS URL | URL message (unfurl) |

---

## 5. HTML Card Pipeline (Playwright)

The `widget` type adds a rendering layer between the agent output and the platform upload:

```
Agent HTML string
  └─ HtmlRendererPort.render(html, width=480)
       └─ PlaywrightHtmlRenderer
            ├─ Browser singleton (lazy init, auto-reconnect)
            ├─ page.set_content(html, wait_until="networkidle")
            ├─ CSS inject: body { margin:0; padding:0; height:fit-content }
            ├─ Detect widget structure:
            │     body.children.length == 1 → element = body > *:first-child  (bare fragment)
            │     body.children.length >= 2 → element = body                  (full page)
            └─ element.screenshot(omit_background=True)
                 → PNG bytes (device_scale_factor=2, retina quality, transparent outside widget)

PNG bytes → PlatformMediaPort.upload_image(image_bytes, alt_text, channel_id)
  ├─ SlackMediaAdapter    → files_upload_v2             → inline image in Slack thread
  └─ TelegramMediaAdapter → bot.send_photo(BytesIO(...)) → inline photo in Telegram chat
```

**Why `element.screenshot()` instead of full-page clip:**
Two patterns LLM uses:
- **Bare fragment** (`<div style="background:...">whole widget</div>`): body has 1 child.
  Screenshot that child — `omit_background=True` makes area outside the element transparent.
- **Full page** (`<body style="background:gradient..."><div class="header">...</div><div class="grid">...</div></body>`):
  body has 2+ children. Screenshot `body` itself — gradient background is an element-level style,
  preserved by the render. Area outside the body bounds is transparent.

`height:fit-content` on body prevents it from stretching to viewport height (800px).
Body background is NOT overridden — the LLM may place the widget gradient directly on `<body>`.

**Feature flag:** `ENABLE_HTML_RENDERER=true` in `.env` or Cloud Run env vars.
When disabled: `html_renderer=None` → `widget` is silently skipped (agent's
`full_response` text already conveyed the content).

**Shared singleton:** One `PlaywrightHtmlRenderer` instance is created per worker process
and passed to both Slack and Telegram `ConversationHandler` instances. Both platforms
share the same Chromium browser.

---

## 6. Routing Logic in ConversationHandler

```python
_MEDIA_CONTENT_TYPES = frozenset({"weather_image", "map_image", "file", "widget"})

async def _deliver_rich_content(self, content, response_channel, thread_id):
    if content.content_type in _MEDIA_CONTENT_TYPES and self._rich_content_service:
        channel_id = getattr(response_channel, "channel_id", None)
        if channel_id:
            url = await self._rich_content_service.process(content, channel_id)
            if url:
                # GCS-based types: URL sent as message → Slack/Telegram unfurls
                await response_channel.send_message(url, thread_id=thread_id)
            # widget / file: upload_image/upload_file called internally → url is None
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

## 8. Composition: Factories

Both platform adapters are assembled in their respective factories in `composition/`:

**Slack** (`src/composition/slack_adapter_factory.py`):
```
SlackMediaAdapter(app.client)
  → RichContentService(media_port, storage_port, html_renderer)
  → ConversationHandler(..., rich_content_service)
  → SlackAdapter
```

**Telegram** (`src/composition/telegram_adapter_factory.py`):
```
Bot(token)
  → TelegramMediaAdapter(bot)
  → RichContentService(media_port, html_renderer)  ← no GCS storage
  → ConversationHandler(..., rich_content_service)
  → TelegramWebhookAdapter
```

Both factories receive the same `html_renderer` singleton from `main.py`.

---

## 9. link_list — Clickable Link Anchors

`link_list` is a parallel delivery channel alongside `rich_content`. It carries named URL
references that the LLM embeds as numeric anchors in `full_response` text. Platform adapters
resolve these anchors into native clickable links before display.

### 9.1 LLM Output Format

```json
{
  "full_response": "Best restaurant is [Cafe Roma][1] near the station.",
  "response_summary": "Cafe Roma recommended.",
  "rich_content": null,
  "link_list": [
    {"anchor": 1, "title": "Cafe Roma", "url": "https://maps.google.com/?q=..."}
  ]
}
```

Two anchor styles the LLM may produce:
- `[display text][N]` — reference-style: display text is used as the link label.
- `[N]` — bare numeric anchor: `title` from `link_list` is used as the link label.

### 9.2 Flow

```
parse_llm_response(raw_text)
  → (user_text, summary, rich_content, link_list)

SmartResponse(text=user_text, structured_data=rich_content, link_list=link_list)
  ↓
ConversationHandler → response_channel.send_message(text, link_list=link_list)
                                        update_message(text, link_list=link_list)
                                        send_chunked_message(text, link_list=link_list)
  ↓
Platform-specific resolution (before display):
  Slack:    _resolve_links_slack()    → <url|display text>  (BEFORE _format_for_platform)
  Telegram: _resolve_links_telegram() → [display text](url) (AFTER _format_for_platform)
```

### 9.3 Platform Resolution Rules

**Slack** (`src/adapters/slack/response_channel.py :: _resolve_links_slack`):
- Called **before** `_format_for_platform()` — Slack mrkdwn link syntax `<url|title>` survives formatting.
- Normalization pre-pass: `"Title [N]"` (plain text + bare anchor) → `"[Title][N]"` →
  `<url|Title>`. Prevents name duplication when LLM writes the name in plain text followed
  by its own anchor.
- `[Display text][N]` → `<url|Display text>`
- `[N]` → `<url|title from link_list>`

**Telegram** (`src/adapters/telegram/response_channel.py :: _resolve_links_telegram`):
- Called **after** `_format_for_platform()` — escaping turns `[N]` → `\[N\]`, so the
  resolver operates on already-escaped text.
- Same normalization and two-anchor-style logic as Slack.
- URL escaping: `)` → `\)` inside the URL part (Telegram MarkdownV2 spec).
- Output: `[display text](url)` or `[escaped title](url)`.

### 9.4 Pass-Through in QuickResponseAgent

`QuickResponseAgent` preserves `link_list` through the full loop:
- `parse_llm_response()` extracts `link_list` from the JSON envelope.
- `SmartResponse.link_list` carries it to `AgentResponse.result.link_list`.
- `ConversationHandler` passes it to all three `send_*` methods on the response channel.
- `link_list` is never modified or filtered by the agent.

### 9.5 Tests

| File | What it tests |
|------|--------------|
| `tests/unit/adapters/test_slack_link_resolution.py` | `_resolve_links_slack`: reference-style, bare anchor, normalization, no-op cases |
| `tests/unit/adapters/test_telegram_link_resolution.py` | `_resolve_links_telegram`: same scenarios on pre-formatted text, URL escaping |
| `tests/unit/agents/core/test_quick_response_agent.py::TestLinkListPassThrough` | link_list survives execute() end-to-end |

---

## 10. Code References

| File | Purpose |
|---|---|
| `src/domain/messaging.py` | `RichContent` dataclass, `SmartResponse.link_list` |
| `src/ports/platform_media_port.py` | `PlatformMediaPort` ABC |
| `src/ports/html_renderer_port.py` | `HtmlRendererPort` ABC + `HtmlRenderError` |
| `src/services/rich_content_service.py` | Conversion + dispatch + widget handler |
| `src/adapters/playwright_html_renderer.py` | `PlaywrightHtmlRenderer` (Chromium singleton) |
| `src/adapters/slack/media_adapter.py` | `SlackMediaAdapter` (`files_upload_v2`) |
| `src/adapters/telegram/media_adapter.py` | `TelegramMediaAdapter` (`send_photo` / `send_document`) |
| `src/composition/slack_adapter_factory.py` | Slack composition root (wires RichContentService) |
| `src/composition/telegram_adapter_factory.py` | Telegram composition root (wires RichContentService) |
| `src/handlers/conversation_handler.py` | `_deliver_rich_content()` routing |
| `src/config/settings.py` | `ENABLE_HTML_RENDERER` flag |
| `main.py` | Renderer lifecycle (lazy init, graceful stop) |
| `firestore_utils/uploads/OUTPUT_FORMAT_JSON.groovy` | LLM output format with `widget` type |
| `scripts/test_html_render.py` | Local smoke test for Playwright rendering |

---

## 11. Status

**V4 — Production Ready (2026-03-08)**

Delivered:
- File delivery (md, html, txt, xlsx, docx) via direct Slack upload
- `widget` — agent-generated HTML → Playwright PNG → inline image on Slack and Telegram
- `TelegramMediaAdapter` — `send_photo` (images) + `send_document` (files)
- `TelegramAdapterFactory` — Telegram composition root, mirrors Slack factory
- Playwright renderer v2 — `element.screenshot()` with smart widget detection (fragment vs full page)

Not yet:
- GCS storage path (map images — M3)
- PDF rendering (M5)

History:
- Added: 2026-02-25 (M1 weather_image + M2 file delivery)
- Updated: 2026-02-26 — `widget` type + `HtmlRendererPort` + `PlaywrightHtmlRenderer`
- Updated: 2026-02-26 — `TelegramMediaAdapter` + `TelegramAdapterFactory` + Playwright v2
- Updated: 2026-03-08 — `link_list` clickable anchor protocol (§ 9): Slack + Telegram resolvers, QuickAgent pass-through
