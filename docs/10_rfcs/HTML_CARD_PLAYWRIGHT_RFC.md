# RFC: HTML Card — Inline Images via Playwright

**Status:** Implemented (2026-02-26)
**Date:** 2026-02-26
**Author:** Solo dev
**Scope:** HtmlRendererPort, PlaywrightHtmlRenderer, RichContentService, ConversationHandler, Docker

---

## 1. Problem Statement

Rich content delivery is text-first or file-first. When an agent wants to present
structured data (metrics, breakdowns, comparisons) visually, the only options are:

- Plain text — readable but not engaging
- File upload (`.html`, `.md`) — user must open separately, no inline preview
- GCS URL — external link, no inline preview in Slack/Telegram

**Goal:** Agent generates lightweight HTML → system renders it as PNG → delivered inline
as an image on any platform (Slack, Telegram, future). Agent decides when to include a
visual card, just as it decides when to use an emoji.

---

## 2. Use Cases

| # | Agent decision | Expected result |
|---|---|---|
| U1 | Show expense breakdown | Inline bar chart / table card in chat |
| U2 | Present weather summary | Visual card with conditions and temperature |
| U3 | Compare two options | Side-by-side HTML card inline |
| U4 | Share a metric / stat | Highlighted number card |
| U5 | Conversational reply | No HTML — agent chooses not to add one |

---

## 3. Solution Overview

```
Agent JSON output
  └── rich_content: { type: "html_card", data: { html, alt_text }, fallback }

parse_llm_response()          → extracts RichContent(content_type="html_card", ...)

ConversationHandler           → routes to RichContentService.process()

RichContentService            → _handle_html_card()
  └── HtmlRendererPort.render(html, width=480)   → PNG bytes

PlatformMediaPort.upload_image(png_bytes, alt_text, channel_id)
  └── Slack: files_upload_v2  → inline image in chat
  └── Telegram: sendPhoto     → inline photo
```

Text response is sent first (agent already does this via `full_response`).
Image arrives ~300–800ms later — feels like one message.

**No GCS bucket involved.** PNG bytes go directly from renderer to platform upload API.
This is fundamentally different from `weather_image` / `map_image` (those return a GCS URL
which is sent as a message; Slack/Telegram unfurls the URL). `html_card` uses
`upload_image()` which posts the binary directly — no intermediate URL, no unfurling.

---

## 4. Agent Interface (JSON output)

The agent returns structured JSON (unchanged envelope):

```json
{
  "full_response": "Here's your expense breakdown for February:",
  "response_summary": "Expense breakdown: food 35%, transport 20%",
  "rich_content": {
    "type": "html_card",
    "data": {
      "html": "<div style='font-family:sans-serif;padding:16px;...'>...</div>",
      "alt_text": "Expense breakdown February 2026"
    },
    "fallback": "Food: 5 200, Transport: 3 100, Other: 1 800. Total: 10 100."
  }
}
```

HTML constraints (enforced via agent prompt, not at render time):
- Inline CSS only — no `<link>`, no `<style>` blocks referencing external files
- No `<script>` tags — static content only
- No external resources (`src=`, `href=` pointing to external URLs)
- Max width 480px — mobile-first, fits both Slack sidebar and Telegram
- Self-contained — renders correctly without network access
- May be a bare fragment (`<div>...`) or a full `<html>` document — both handled

---

## 5. New Port: HtmlRendererPort

**Justified by:** testable substitution (tests use `AsyncMock(spec=HtmlRendererPort)`),
potential future alternative renderers (Pillow-based for simple cases).

`HtmlRenderError` lives in `src/ports/html_renderer_port.py` (not in `adapters/`) so
services can catch it without violating hexagonal import rules.

```python
# src/ports/html_renderer_port.py
from abc import ABC, abstractmethod

class HtmlRenderError(Exception):
    """Raised when HTML rendering fails."""

class HtmlRendererPort(ABC):
    """Renders an HTML string to PNG bytes."""

    @abstractmethod
    async def render(self, html: str, width: int = 480) -> bytes:
        """Render HTML to PNG. Height auto-fits content."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """No-op by default — lazy init."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Close browser on graceful shutdown."""
        ...
```

---

## 6. New Adapter: PlaywrightHtmlRenderer

**File:** `src/adapters/playwright_html_renderer.py`

**Design decisions:**

- **Browser singleton** — one Chromium instance per process. Creating a browser per
  request adds ~1–2s; reusing it costs only new-page overhead (~50ms).
- **Lazy init** — browser starts on first `render()` call, not at container startup.
  Cold start does not pay Chromium launch cost (~1–2s); only the first `html_card`
  request pays it.
- **`asyncio.Lock`** guards concurrent initialization — two simultaneous first calls
  will not spawn two browsers.
- **Auto-reconnect** — `is_connected()` check before each render; if browser crashed,
  `_launch_browser()` is called again transparently.
- **`device_scale_factor=2`** — retina-quality output (960px physical width for 480px
  logical). Looks crisp on HiDPI Slack/Telegram clients.
- **Timeout** — 8s per render. Raises `HtmlRenderError` on breach.
- **`--no-sandbox`** — required in Cloud Run non-root containers. Detected automatically
  via `K_SERVICE` env var. Not applied locally.

**Widget structure detection (v2):**

LLMs produce two distinct HTML output patterns:

1. **Bare fragment** — LLM returns a single widget element:
   `<div style="background:...padding:16px">...</div>`
   Body has exactly 1 child. Screenshot that child with `omit_background=True` → clean
   widget with transparent outside.

2. **Full page layout** — LLM places the widget background on `<body>` with multiple
   children (e.g., header div + grid div):
   `<body style="background:linear-gradient(...)"><div class="header">...</div><div class="grid">...</div></body>`
   Body has 2+ children. Screenshot `body` itself — the gradient background is an
   element-level style, preserved by the render. Area outside the body is transparent.

`body.getBoundingClientRect()` returns the viewport rectangle, not content height.
`height:fit-content` on `body` prevents it from stretching to the 800px viewport height.
`body` background is never overridden — the LLM may place the widget gradient on `<body>`.

```python
async def render(self, html: str, width: int = 480) -> bytes:
    page = await self._browser.new_page(
        viewport={"width": width, "height": 800},
        device_scale_factor=_DEVICE_SCALE_FACTOR,
    )
    try:
        await page.set_content(html, wait_until="networkidle", timeout=_RENDER_TIMEOUT_MS)
        # Reset browser default margins. Keep body height as fit-content so it wraps the
        # widget without stretching to viewport height. Do NOT override background — the
        # LLM may place the widget background on <body> itself (full-page layout).
        await page.add_style_tag(content="html,body{margin:0;padding:0;height:fit-content!important}")
        # Detect widget structure:
        #   Fragment (LLM returns bare <div>): body has 1 child → screenshot that child.
        #   Full page (LLM uses <body> as widget root with 2+ children): screenshot body.
        child_count = await page.evaluate("document.body.children.length")
        if child_count == 1:
            element = await page.query_selector("body > *:first-child")
        else:
            element = await page.query_selector("body")
        if element is None:
            element = await page.query_selector("body")
        png = await element.screenshot(omit_background=True)
        return png
    except Exception as e:
        raise HtmlRenderError(f"render failed: {e}") from e
    finally:
        await page.close()
```

`element.screenshot(omit_background=True)` replaces the previous `page.screenshot(clip=...)`
approach. Benefits: no JS height measurement needed, transparent area outside the element
(not white), works correctly for both layout patterns.

---

## 7. RichContentService Extension

`_html_renderer: Optional[HtmlRendererPort]` added to constructor.
`"html_card"` branch in `process()` → `_handle_html_card()`.

```python
async def _handle_html_card(self, content: RichContent, channel_id: str) -> None:
    if not self._html_renderer:
        logger.warning("RichContentService: html_card received but HtmlRendererPort not configured")
        return

    html = content.data.get("html", "").strip()
    if not html:
        logger.warning("RichContentService: html_card missing 'html' field — skipping")
        return

    alt_text = content.data.get("alt_text", "Visual card")

    try:
        png_bytes = await self._html_renderer.render(html)
        await self._media_port.upload_image(
            image_bytes=png_bytes,
            alt_text=alt_text,
            channel_id=channel_id,
        )
    except HtmlRenderError as e:
        logger.error("RichContentService: html_card render failed — %s", e)
    except Exception as e:
        logger.error("RichContentService: html_card upload failed — %s", e)
```

`process()` returns `None` for `html_card` — no URL, upload is direct.

---

## 8. ConversationHandler

`"html_card"` added to `_MEDIA_CONTENT_TYPES`:

```python
_MEDIA_CONTENT_TYPES = frozenset({"weather_image", "map_image", "file", "html_card"})
```

No other changes — the existing `_deliver_rich_content` path handles it:
`process()` returns `None` → `if url:` guard does not fire → only the direct upload
(inside `_handle_html_card`) happens. Text was already delivered before this point.

---

## 9. Lifecycle: main.py

```python
html_renderer = None
if config.get("ENABLE_HTML_RENDERER"):
    from src.adapters.playwright_html_renderer import PlaywrightHtmlRenderer
    html_renderer = PlaywrightHtmlRenderer()
# html_renderer passed to SlackAdapterFactory.create_adapter(html_renderer=html_renderer)

# On graceful shutdown:
if html_renderer:
    await html_renderer.stop()
```

**Feature flag:** `ENABLE_HTML_RENDERER=true` in `.env` / Cloud Run env vars.
When `false`: `HtmlRendererPort` is `None` → `html_card` content is silently skipped.
Agent's `full_response` text already conveyed the information — no data loss.

---

## 10. Docker Image

```dockerfile
# After pip install requirements:
RUN python -m playwright install chromium --with-deps
```

**Impact:**
- Image size: +~300MB (Chromium + deps)
- Cold start: +~1–2s on first `html_card` request (lazy init)
- With `min-instances=0`: no cold start on container startup; only first html_card request
  pays the ~1–2s browser launch cost
- Memory: Chromium idle ~80MB; per-page peak ~150MB. Total process stays under 512MB.

`playwright>=1.40.0` in `requirements.txt`.

---

## 11. Agent Prompt Design

The agent's cognitive process token (`COGNITIVE_PROCESS_SMART`, `COGNITIVE_PROCESS_QUICK`)
includes a **FORMAT** step that runs **before DRAFT**, not after:

```
4. FORMAT: Before writing anything — decide the delivery format.
   Defaults: weather → html_card. Prices/rates → html_card.
   Multi-day or multi-item data → html_card.
   Conversational reply, opinion, advice, single fact → plain text.
   If the user explicitly asked for text — respect that.
   This decision locks in before drafting.

5. DRAFT: Write full_response AND HTML simultaneously (if FORMAT decided html_card).
```

**Why FORMAT before DRAFT:** placing the visual decision after drafting ("VISUAL" step)
caused the model to treat the card as an afterthought — it had already mentally committed
to text format. Moving the decision before drafting makes html_card a primary output
mode, not an optional add-on.

Prompt tokens: `firestore_utils/uploads/COGNITIVE_PROCESS_SMART.groovy`,
`firestore_utils/uploads/COGNITIVE_PROCESS_QUICK.groovy`,
`firestore_utils/uploads/OUTPUT_FORMAT_JSON.groovy`.

---

## 12. Error Handling & Fallback

| Failure | Behavior |
|---|---|
| `HtmlRenderError` (timeout, crash) | Log error, skip image, `full_response` text stands |
| `HtmlRendererPort` not configured | Log warning, skip image silently |
| `html` field empty | Log warning, skip |
| `upload_image` fails | Logged in `RichContentService`, does not propagate |
| Browser crashes between requests | `is_connected()` check → auto-reconnect on next render |

No double-delivery risk: `full_response` text is sent before image rendering starts.

---

## 13. Testing

**Unit tests (implemented):**
- `tests/unit/ports/test_html_renderer_port.py` — 5 port contract tests
- `tests/unit/services/test_rich_content_html_card.py` — 5 service tests:
  - normal render → `upload_image` called with correct bytes
  - renderer not configured → no upload, no exception
  - empty `html` field → render not called
  - `HtmlRenderError` → no upload, no exception propagated
  - default alt_text when field missing

**Local smoke test:**
```bash
python scripts/test_html_render.py
open /tmp/html_card_test.png
```

---

## 14. Files Created / Modified

| File | Action | Status |
|---|---|---|
| `src/ports/html_renderer_port.py` | **Created** — `HtmlRendererPort` ABC + `HtmlRenderError` | ✅ |
| `src/ports/platform_media_port.py` | **Created** — `PlatformMediaPort` ABC | ✅ |
| `src/adapters/playwright_html_renderer.py` | **Created** — `PlaywrightHtmlRenderer` (v2: element.screenshot) | ✅ |
| `src/adapters/slack/media_adapter.py` | **Created** — `SlackMediaAdapter` (`files_upload_v2`) | ✅ |
| `src/adapters/telegram/media_adapter.py` | **Created** — `TelegramMediaAdapter` (`send_photo` / `send_document`) | ✅ |
| `src/services/rich_content_service.py` | **Modified** — `_html_renderer`, `_handle_html_card()` | ✅ |
| `src/handlers/conversation_handler.py` | **Modified** — `"html_card"` in `_MEDIA_CONTENT_TYPES` | ✅ |
| `src/composition/slack_adapter_factory.py` | **Modified** — wire `html_renderer` param, `SlackMediaAdapter` | ✅ |
| `src/composition/telegram_adapter_factory.py` | **Created** — `TelegramAdapterFactory` (composition root) | ✅ |
| `src/config/settings.py` | **Modified** — `ENABLE_HTML_RENDERER` setting | ✅ |
| `main.py` | **Modified** — renderer lifecycle, `TelegramAdapterFactory` | ✅ |
| `Dockerfile` | **Modified** — Playwright + Chromium install | ✅ |
| `requirements.txt` | **Modified** — `playwright>=1.40.0` | ✅ |
| `tests/unit/ports/test_html_renderer_port.py` | **Created** — port contract tests | ✅ |
| `tests/unit/services/test_rich_content_html_card.py` | **Created** — service tests | ✅ |
| `scripts/test_html_render.py` | **Created** — local smoke test | ✅ |
| `firestore_utils/uploads/OUTPUT_FORMAT_JSON.groovy` | **Modified** — `html_card` type + iOS widget style + grid layout | ✅ |
| `firestore_utils/uploads/COGNITIVE_PROCESS_SMART.groovy` | **Modified** — FORMAT step added | ✅ |
| `firestore_utils/uploads/COGNITIVE_PROCESS_QUICK.groovy` | **Rewritten** — aligned with SMART, FORMAT step, grid constraints | ✅ |

---

## 15. Resolved Open Questions

| # | Question | Resolution |
|---|---|---|
| Q1 | `device_scale_factor`: 2 or 1? | 2 — sharper on HiDPI screens |
| Q2 | Max render timeout: 5s or 8s? | 8s — allows complex layouts |
| Q3 | Browser restart on crash — auto or manual? | Auto — `is_connected()` guard + `_launch_browser()` reconnect |
| Q4 | Multiple `html_card` per response (future)? | Not in V1 — single `rich_content`. Extend later if needed. |
| Q5 | Sandbox Chromium (`--no-sandbox`)? | Auto-detected via `K_SERVICE` env var (Cloud Run only) |
| Q6 | Content height — `body.screenshot()` or `full_page`? | `element.screenshot(omit_background=True)` — child count determines target element (body > :first-child for bare fragment, body for full-page layout) |
| Q7 | Two Bot instances for Telegram (media + webhook)? | Acceptable — `Bot` is a stateless HTTP client. `TelegramMediaAdapter` creates its own instance with the same token. |
| Q8 | Shared `html_renderer` singleton across platforms? | Yes — `main.py` creates one `PlaywrightHtmlRenderer`; both factories receive the same instance. One Chromium browser per process. |
