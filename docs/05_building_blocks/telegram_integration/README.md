<!-- living-doc
covers:
  src/adapters/telegram/
  src/composition/telegram_adapter_factory.py
  scripts/admin/add_telegram_secrets.sh
-->
# Telegram Integration

## Purpose

This block is the Telegram door into the exocortex. Telegram is only another chat platform: the adapter turns a Bot API
update into a platform-neutral `MessageContext`, hands it to the shared `ConversationHandler`, and implements the
`ResponseChannel` and `PlatformMediaPort` contracts so replies, status bubbles, files and rendered widgets go back to
the same chat. Nothing about agents, memory or routing is Telegram-specific.

What it is not: it does no message intelligence of its own (commands, geo marks and turns are the handler's), it has no
channel binding and no history source (the Bot API has none), and it is not a second authorization system (IAM decides
who may talk).

## Owned data

The block owns no collection. It reads and writes two things owned elsewhere:

- The dedup marks of the **inline** path only, in `EnvironmentConfig.event_dedup_collection`, key `telegram::{update_id}`,
  through `FirestoreDedupStore` (default TTL is `FirestoreDedupStore.__init__`'s `ttl_seconds`; `_build_telegram` passes none).
- Configuration: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` (with a minimum length), checked by
  `src/config/environment.py::validate_telegram_config`. The bot token must match the `<bot_id>:<secret>` shape.
  `scripts/admin/add_telegram_secrets.sh` stores both in Secret Manager for the dev environment.

The session of a Telegram turn is `session_id_for(user_id, chat_id)`: one history per chat.

## Flows

**Startup.** `src/composition/application_graph.py::_build_telegram` runs only when `TELEGRAM_BOT_TOKEN` is set. It builds
a `Bot` for the `TelegramMediaAdapter`, then a `RichContentService` over it, then
`src/composition/telegram_adapter_factory.py::TelegramAdapterFactory.create_adapter`, which builds the
`ConversationHandler` over the shared chat services. Only after all of these exist does
`src/composition/platform_kit.py::register_late_platform` bind the platform to the notification service, the channel
factory and the worker. `main.py` mounts the adapter's blueprint at `/telegram`, so the webhook is `/telegram/webhook`.

**Intake.** `TelegramWebhookAdapter._handle_telegram_update`:
1. `_verify_webhook_signature` compares `X-Telegram-Bot-Api-Secret-Token` with the configured secret
   (constant-time `hmac.compare_digest`; no HMAC is computed); a missing or wrong header is `403`.
2. Only `update.message` is handled; any other update kind (edits, callbacks) is acknowledged with `200` and ignored.
3. With a task queue: the update is enqueued as `telegram_update` (a fixed task deadline, name keyed by `telegram::{update_id}`)
   and the webhook answers `200` at once. Without one (local dev) the intake mark in the dedup store is the only
   duplicate guard and the turn runs inline.

**Queued path.** `WorkerHandler._handle_telegram_update` calls the entry bound by `WorkerHandler.register_telegram_worker`
(`503` until bound), which is `TelegramWebhookAdapter.handle_queued_update`; it re-parses the update and runs
`_process_message`. It always answers `200`, even on error: a retry would re-run a turn already written.

**One message.** `TelegramWebhookAdapter._process_message`:
1. `iam_service.authorize("telegram", ...)`. A rejection sends the decision's message (onboarding link) and stops.
2. Resolve the session and the UI and preferred language, and build a `TelegramResponseChannel`.
3. A plain location pin goes to `ConversationHandler.handle_geo_mark` through `_handle_location`; a venue or a live
   location gets a one-line notice and is not taken; a forwarded point is not a mark.
4. Text starting with `$` (after `unwrapped`) goes to `ConversationHandler.handle_command`, unless the message is
   forwarded.
5. Otherwise attachments are translated (`_translate_platform_files`) and the turn goes to `ConversationHandler.handle_message`.

**Files.** Telegram sends a `file_id`, not a URL. `_translate_platform_files` calls `bot.get_file` for each file in
parallel (`asyncio.gather`); the largest photo size is used; a voice message is marked `is_voice_message` and named
`voice.ogg` by default, because the transcription API infers the container from the extension; an `audio` track is a
plain file. The MIME type is the file's own, else guessed from the path, else `application/octet-stream`. A file that
fails is dropped and the others go on. `TelegramResponseChannel.download_file` fetches the URL.

**Replies.** `TelegramResponseChannel.send_message` / `update_message` format and send text (next section).
`send_chunked_message` and `send_flat_response` replace the status bubble with the first chunk and send the rest as new
messages. A follow-up chunk carries the caller's forum-topic id when there is one; the status message id is never used as a thread id (a private chat has no threads). `send_late_answer` posts a long turn's answer as a
reply to the originating message, since Telegram has no message links. `send_literal`, `send_reply` and
`send_message_ref` send with no parse mode, for text that must not meet MarkdownV2 escaping.

**Rich content.** `ConversationHandler._deliver_rich_content` calls `RichContentService.deliver`, which routes media types to `RichContentService.process`. A `widget` is
rendered to PNG and sent by `TelegramMediaAdapter.upload_image` (`send_photo`); a `file` goes by `upload_file`
(`send_document`). A type outside the media types (a `table`) reaches `TelegramResponseChannel.send_rich_content`, which sends only
the fallback text; the GCS-URL media types are dropped with a warning. Inline keyboards are not implemented.

## Invariants and traps

- **Fail closed on the signature.** No update is parsed before `_verify_webhook_signature` passes
  (`test_dispatcher_skips_processing_on_bad_signature`, `test_invalid_signature_rejected`).
- **Telegram is all or nothing** (owner ruling): if building the adapter fails, the bot runs without Telegram and a
  `telegram_update` task answers `503` (nothing bound). But `validate_telegram_config` runs before that guard, so a malformed token or a
  short secret fails startup instead.
- **Duplicate guard differs by path.** On the queue path the Cloud Task name is the guard (`ALREADY_EXISTS` is success)
  and no intake mark is written (`decisions/worker_task_dedup.md`); only the inline path uses `FirestoreDedupStore`
  (`test_duplicate_update_skipped`).
- **Length limits.** A message is at most `TELEGRAM_MAX_MESSAGE_LENGTH`. Raw text is cut at 70% of it before formatting,
  because MarkdownV2 escaping inflates text, and the formatted text is cut again at the hard limit. Chunks are
  `TELEGRAM_CHUNK_SIZE`, kept under the 70% line so a chunk is never re-truncated (`test_send_message_truncation` covers the truncation; the chunk relation rests on the comment at `TELEGRAM_CHUNK_SIZE`).
  Telegram allows about 30 messages per second across a bot (one chat: about one per second).
- **MarkdownV2 is three layers of defence**, because cutting at a fixed offset can split `**bold**` and models write
  bold near the boundary: `_format_for_platform` escapes special characters around placeholders for `**` and `__`;
  `_validate_markdown_pairs` and `_sanitize_unpaired_tags` strip unpaired `*`/`_` before sending; if Telegram still
  answers "can't parse entities", `send_message` and `update_message` retry as plain text (`parse_mode=None`). When an
  edit fails altogether (for example a message too old to edit), `update_message` sends a new message.
- **Text we write is not Markdown.** Anything model-translated or code-written that must show `*`, `_` or backticks as
  they are goes through `send_literal` / `send_reply` / `send_message_ref`.
- **The file URL embeds the bot token.** `bot.get_file` returns a URL with the token in it: it is never logged
  (the `_translate_platform_files` comment) and travels only inside `FileAttachment.url`.
- **A forwarded message is someone else's text:** never a `$` command and never a geo mark.
- **A turn can outlive the webhook.** An inline webhook loses CPU once Telegram drops the connection, which is why the
  queue path exists (`LONG_RUNNING_TURNS_RFC.md` section 5.8); the turn then runs in a worker, not in the request.
- **Unsupported:** inline keyboards, and rich content of the GCS-URL kinds, which are dropped with a warning (see the rich content protocol).
- **Link previews are decided per answer.** More than two distinct links in an answer disable the preview (`link_preview_options`) on every send and on the edit that carries the first chunk (`src/domain/link_previews.py`, `rich_content_protocol/README.md` § Link previews). `tests/unit/adapters/test_telegram_link_previews.py`.

## Links

- [`LONG_RUNNING_TURNS_RFC.md`](../../10_rfcs/LONG_RUNNING_TURNS_RFC.md) (queue hand-off, late answer),
  [`long_running_turns.md`](../../04_solution_strategy/decisions/long_running_turns.md),
  [`worker_task_dedup.md`](../../04_solution_strategy/decisions/worker_task_dedup.md)
- [`GEO_MARKS_RFC.md`](../../10_rfcs/GEO_MARKS_RFC.md) section 4.6 (location pins)
- [Slack integration](../slack_integration/README.md) (the twin adapter),
  [rich content protocol](../rich_content_protocol/README.md)

## Where to start reading

- `src/adapters/telegram/webhook_adapter.py::TelegramWebhookAdapter._process_message`
- `src/adapters/telegram/webhook_adapter.py::TelegramWebhookAdapter._handle_telegram_update`
- `src/adapters/telegram/response_channel.py::TelegramResponseChannel.send_message`
- `src/adapters/telegram/media_adapter.py::TelegramMediaAdapter`
- `src/composition/application_graph.py::_build_telegram`
- `src/composition/telegram_adapter_factory.py::TelegramAdapterFactory.create_adapter`
