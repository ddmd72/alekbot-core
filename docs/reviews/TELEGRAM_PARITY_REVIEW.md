# Code Review: Telegram Delivery Parity

**Scope:** Telegram adapter gaps relative to Slack — delivery, formatting, wiring.
**Status:** Review only. No changes applied.

---

## Finding 1 — CRITICAL: Long background notifications crash for Telegram users

**File:** [src/services/user_notification_service.py](../../src/services/user_notification_service.py#L188-L192)

```python
# Lines 188-192
if len(text) > response_channel.max_message_length:
    placeholder = await response_channel.send_message("📩")
    message_id = placeholder['ts']          # ← crash for Telegram
    await response_channel.send_chunked_message(
        text, message_id, link_list=link_list or None
    )
```

**Root cause:** `SlackResponseChannel.send_message()` returns the full Slack API response dict; `placeholder['ts']` extracts the timestamp. `TelegramResponseChannel.send_message()` returns a plain `str` (message_id). Accessing `['ts']` on a string raises `TypeError: string indices must be integers`.

**When it fires:** Any background notification where the formatted agent response exceeds `TelegramResponseChannel.max_message_length` (4096 chars) — e.g. a detailed deep research delivery notification, a long reminder alert, a daily email review summary.

**Evidence:** [src/adapters/slack/response_channel.py](../../src/adapters/slack/response_channel.py#L168-L173) — `send_message` returns `response` (dict), `send_status` does `response['ts']`. [src/adapters/telegram/response_channel.py](../../src/adapters/telegram/response_channel.py#L261) — returns `str(message.message_id)`.

**Fix:** `message_id = placeholder['ts'] if isinstance(placeholder, dict) else placeholder`

---

## Finding 2 — CRITICAL: Async file delivery (DOCX) is Slack-only

**File:** [src/services/user_notification_service.py](../../src/services/user_notification_service.py#L328-L339)

```python
# Lines 328-339
await response_channel.send_message("📎")
resolved_channel_id = response_channel.channel_id

await self._platform_media.upload_file(    # ← always SlackMediaAdapter
    file_bytes=file_bytes,
    filename=filename,
    title=title,
    channel_id=resolved_channel_id,        # ← Telegram chat_id sent to Slack API
)
```

**Root cause:** [main.py](../../main.py#L624-L628) wires only one `_platform_media` into `notification_service`:
```python
notification_service._platform_media = SlackMediaAdapter(
    app_client=slack_adapter.app.client,
    bot_token=config.get("SLACK_BOT_TOKEN", ""),
)
```
`PlatformMediaPort` is a single reference — not platform-aware. When `notify_file_bytes` is called for a Telegram user, it calls `SlackMediaAdapter.upload_file()` with a Telegram numeric chat_id, which either fails silently (Slack rejects it) or sends the file to a completely wrong Slack channel.

**When it fires:** User on Telegram requests document creation (`create_document` intent). `AgentWorkerHandler._deliver_docx_result()` calls `notification.notify_file_bytes(...)` → no file arrives.

**Evidence:** Both `SlackResponseChannel` and `TelegramResponseChannel` implement `send_file(content, filename, title, thread_id)` using their own platform API. There is no reason to bypass them via `_platform_media` for background file delivery.

**Fix:** Replace `_platform_media.upload_file(...)` with `response_channel.send_file(content=file_bytes, filename=filename, title=title)`. The Slack-specific U→D channel ID resolution (done by sending the placeholder "📎") should be kept for Slack only and removed for other platforms.

---

## Finding 3 — BUG: `send_chunked_message` passes message_id as message_thread_id

**File:** [src/adapters/telegram/response_channel.py](../../src/adapters/telegram/response_channel.py#L380-L384)

```python
# Lines 380-384
await self.update_message(message_id, "✅ Відповідь готова.")

for chunk in chunks:
    await self.send_message(chunk, thread_id=thread_id or message_id, ...)
    #                                                   ^^^^^^^^^^^^^^
    #                                                   wrong fallback
```

**Root cause:** The Slack pattern is to use the original message's `thread_ts` as both the anchor and the thread identifier. In Telegram, `message_thread_id` is a *forum topic ID* (only relevant in supergroups with Topics enabled) — it has nothing to do with message IDs. Using `message_id` as a fallback for `message_thread_id` is conceptually incorrect and will cause `BadRequest: message thread not found` errors in regular groups or forum groups where the message_id doesn't correspond to a topic.

**Where `send_message` uses it:** [response_channel.py L259](../../src/adapters/telegram/response_channel.py#L259) — `message_thread_id=int(thread_id) if thread_id else None`.

**Impact:** In practice, regular Telegram chats silently ignore unknown `message_thread_id` values, so the bug is not always visible. In forum supergroups it can produce API errors and failed chunk delivery.

**Fix:** `thread_id=thread_id` — drop the `or message_id` fallback entirely.

---

## Finding 4 — MISSING FEATURE: Tables render as unformatted fallback text

**File:** [src/adapters/telegram/response_channel.py](../../src/adapters/telegram/response_channel.py#L386-L397)

```python
# Lines 386-397
async def send_rich_content(
    self,
    content: RichContent,
    thread_id: Optional[str] = None
) -> Any:
    """
    For MVP: use fallback text only.
    TODO: Implement Inline Keyboards for tables.
    """
    return await self.send_message(content.fallback_text, thread_id)
```

**What Slack does:** [src/adapters/slack/response_channel.py](../../src/adapters/slack/response_channel.py) — `send_rich_content` dispatches `content_type == "table"` to `_build_generic_table_blocks()` which produces native Slack Block Kit table blocks with proper column alignment, title, and footer.

**When it fires:** Any SmartAgent/QuickAgent response containing a table — web search results with series data, email digest summaries, task lists, financial calculations, weather comparisons, etc. These arrive as `RichContent(content_type="table", data={title, headers, rows, footer}, fallback_text=...)`. Telegram users see the `fallback_text` — a flat string with no formatting.

**What's available in Telegram:** MarkdownV2 code blocks (` ``` `) render in monospace font. A properly aligned ASCII table with `|` separators inside a code block is readable, consistently spaced, and platform-appropriate. Inside a MarkdownV2 code block, only `` ` `` and `\` need escaping — `|`, `-`, `+`, spaces are all literal.

**Proposed render output example:**
```
```
Ticker  | Price   | Change
--------|---------|-------
AAPL    | $189.30 | +1.2%
GOOGL   | $142.75 | -0.4%
MSFT    | $374.50 | +0.8%
```
```

Row format compatibility: `RichContent.data["rows"]` can contain `{"cells": [...]}` objects or plain `[...]` arrays — both need normalizing before column width calculation. This normalization is already done in `_build_generic_table_blocks` in Slack and can be replicated.

---

## Finding 5 — MISSING WIRING: TelegramAdapterFactory has no GCS storage

**Files:** [src/composition/telegram_adapter_factory.py](../../src/composition/telegram_adapter_factory.py#L83-L86), [src/composition/slack_adapter_factory.py](../../src/composition/slack_adapter_factory.py#L94-L100)

Slack factory:
```python
# slack_adapter_factory.py L94-100
gcs_bucket = config.get("GCS_MEDIA_BUCKET", "")
storage_adapter = GcsMediaAdapter(bucket_name=gcs_bucket) if gcs_bucket else None
rich_content_service = RichContentService(
    media_port=media_adapter,
    storage_port=storage_adapter,   # ← GCS wired
    html_renderer=html_renderer,
)
```

Telegram factory:
```python
# telegram_adapter_factory.py L83-86
rich_content_service = RichContentService(
    media_port=media_adapter,
    # storage_port missing
    html_renderer=html_renderer,
)
```

**Impact:** `RichContentService` uses `storage_port` to upload HTML content to GCS and return a public URL (the `store_html()` / `deliver_as_url()` path). With `storage_port=None`, any code path inside a Telegram conversation that attempts to produce a GCS-backed URL will silently produce `None` and no link will be delivered.

**Note:** The primary async delivery paths (HtmlPageGeneratorAgent, PdfGeneratorAgent, DeepResearch) use `AgentWorkerHandler._deliver_document_result()` which calls `DocumentDeliveryService` directly with its own `gcs_media_adapter` — those paths are not affected. The gap matters for the synchronous in-conversation `RichContentService` path.

**Fix:** Add `media_storage: Optional[MediaStoragePort] = None` to `TelegramAdapterFactory.create_adapter()`, pass `storage_port=media_storage` to `RichContentService`, and wire `gcs_media_adapter` from `main.py`.

---

## Finding 6 — MINOR: Background notification channels created without localization

**File:** [main.py](../../main.py#L697-L708)

```python
def _make_telegram_channel(adapter, channel_id):
    try:
        return TelegramResponseChannel(
            bot=adapter.bot,
            chat_id=int(channel_id),
            # language and localization not passed
        )
    except (ValueError, TypeError) as e:
        ...
```

`TelegramResponseChannel.__init__` defaults to `language=LanguageCode.UK` when no language is provided. The `localization` adapter used for status phrases (`_get_status_phrases`, `get_entertainment_intro`) is also `None`, falling back to `from ...locales.uk import get_message as get_uk_message`.

**When it matters:** If `notify()` or any background handler calls `send_status()` on a notification channel belonging to a non-Ukrainian Telegram user, they receive Ukrainian phrases. Today `UserNotificationService.notify()` does not call `send_status()` directly — the status phrase path is only active during `ConversationHandler` processing where the channel is created with proper language from the webhook adapter. So this is currently inert.

**Fix:** Capture `_localization` in the closure: `localization=_localization`. Language resolution per-user requires user_id which is not available at channel-create time — acceptable to leave at default for now.

---

## Summary Table

| # | Severity | File | Description |
|---|----------|------|-------------|
| 1 | CRITICAL | `user_notification_service.py:191` | `placeholder['ts']` crashes for Telegram long notifications |
| 2 | CRITICAL | `user_notification_service.py:334` | `_platform_media` is Slack-only — DOCX never reaches Telegram |
| 3 | BUG | `telegram/response_channel.py:384` | `message_id` used as `message_thread_id` fallback |
| 4 | MISSING | `telegram/response_channel.py:397` | Tables sent as fallback_text instead of formatted code block |
| 5 | MISSING | `telegram_adapter_factory.py:83` | `RichContentService` has no `storage_port` (GCS) |
| 6 | MINOR | `main.py:699` | Notification channel created without localization |
