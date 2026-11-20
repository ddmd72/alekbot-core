<!-- living-doc
covers:
  src/services/rich_content_service.py
  src/services/user_notification_service.py
  src/adapters/playwright_html_renderer.py
  src/adapters/notification_channel_factory.py
  src/adapters/firestore_notification_state_adapter.py
  src/ports/html_renderer_port.py
  src/ports/platform_media_port.py
  src/ports/notification_channel_factory_port.py
  src/ports/notification_state_port.py
  src/domain/notification.py
  src/domain/notification_kind.py
  src/domain/notify_result.py
  src/infrastructure/notification_sla.py
  src/composition/notification_setup.py
  src/composition/platform_kit.py
  scripts/test_html_render.py
-->
# Rich Content and Notifications

## Purpose

Two jobs share one delivery routine. **Rich content**: an agent returns a structured item next to its text
(`RichContent`: `content_type`, `data`, `fallback_text`) and the application layer turns it into a file, an image or a
link on the user's platform, so agents and the domain never know about Slack or Telegram. **Notifications**: code that
runs with no chat turn (a reminder, the daily review, a document or research result, the chat copy of a call answer)
finds the user's channel, lets the agent write the message and delivers it through the same routine as a chat turn.
Also here, because it rides the same channels: `link_list`, the numbered link anchors in a reply.

What it is not: it does not decide what an agent returns (the output-format token does), it does not store files for
the user (`FileLinkService` mints the link), and it does not write memory.

## Owned data

- **Last active channel**: `EnvironmentConfig.user_notification_state_collection`, document id `user_id`
  (`platform`, `channel_id`, `updated_at`). Written on every incoming chat message
  (`UserNotificationService.save_channel`), last writer wins.
- **Primary channel**: `{firestore_collection_prefix}user_primary_channel`, same shape. Written by the `$primary`
  command (`UserNotificationService.save_primary`). Both are read through `NotificationStatePort`;
  `channel_id` for Slack may be a user id (`U...`), for Telegram it is the chat id.
- **Stored html pages**: the private media bucket under `html/<uuid>-<filename>`
  (`RichContentService._store_html`). The key never reaches the user; the user gets a `/f/<token>` link.
- Nothing else is stored: files and images are posted to the platform and kept nowhere here.

## Flows

### Rich content, chat turn or background

1. `src/handlers/conversation_handler.py::ConversationHandler._deliver_rich_content` calls
   `RichContentService.deliver(content, response_channel, thread_id, user_id)`; without a service it calls
   `ResponseChannel.send_rich_content`. The same call is made for a `rich_content` delivery item
   (`ConversationHandler._deliver_item`).
2. `RichContentService.deliver` routes: a type outside `MEDIA_CONTENT_TYPES` (`src/domain/messaging.py`), or a channel
   with no `channel_id`, goes to `send_rich_content`; a media type goes to `RichContentService.process` on the
   channel's id. A link `process` returns is posted as a plain message.
3. `process` by type: `file` converts by extension and uploads through `PlatformMediaPort.upload_file`
   (`.xlsx` from CSV via openpyxl, `.docx` from Markdown via python-docx, anything else UTF-8 bytes; a failed
   conversion falls back to a `.txt`). An `.html` file with a storage port, a link service and a `user_id` is stored
   and returned as a `/f/<token>` link; otherwise it is uploaded like any file. `widget` renders the html to a PNG and
   uploads it as an inline image (`upload_image`). Any other type is logged and skipped.
4. Platform end: `src/adapters/slack/media_adapter.py::SlackMediaAdapter` (`files_upload_v2`) and
   `src/adapters/telegram/media_adapter.py::TelegramMediaAdapter` (`send_photo`, `send_document`).
   `table` is rendered by `SlackResponseChannel.send_rich_content` as Block Kit; Telegram's sends `fallback_text` only.

### Widget rendering

`src/adapters/playwright_html_renderer.py::PlaywrightHtmlRenderer.render` (port: `HtmlRendererPort`): a lazily
started Chromium, one per process, reconnected if it died. The page is built with JavaScript disabled and every
request that is not `data:`, `about:` or `blob:` aborted (`is_request_allowed`), because the html is model-written.
A body with one child is screenshotted as that child, otherwise the body is, with a transparent background.
`ENABLE_HTML_RENDERER` (`src/config/settings.py`) off means no renderer on either platform.
`scripts/test_html_render.py` is the local smoke test.

### Notification

1. A caller (worker task, agent worker) calls `src/services/user_notification_service.py::UserNotificationService.notify`
   with a `NotificationKind`. `resolve_channel` picks the destination: explicit override, then the primary channel,
   then the last active one; none means `NotifyResult(error="no_channel")`.
2. The timeout comes from `src/infrastructure/notification_sla.py::NOTIFICATION_SLA[kind]`, a tier override winning
   when the caller passes a tier.
3. The alert goes to the agent as the current message (`AgentCoordinator.route_message`, with `origin_channel_id` and
   `origin_platform` in the context so an async document returns to this channel). The reply text is sent by
   `_send_answer_text` (a Slack DM id gets an `<@U...>` mention), the history pair is saved, and a `structured_data`
   item goes through `_send_rich_content` into step 2 above.
4. `NotifyResult` (`src/domain/notify_result.py`) tells the caller how far it got: `delivered`, `sent`, `turn_started`.
   Callers use `is_retryable_failure` and friends to decide whether a Cloud Task may retry.
5. Other entry points skip the agent: `notify_raw` (text as given, `literal=True` for text the bot did not write),
   `notify_ui_notice`, `notify_call_summary` and `notify_drive_note_receipt` (text plus a history pair),
   `notify_answer_copy` (the chat copy of a call answer, `notify`'s delivery without the agent),
   `notify_document_link`, `notify_file_bytes`.

### Wiring

`src/composition/notification_setup.py::build_notifications` builds Slack's media adapter, its one `RichContentService`
(with the graph's media storage and `FileLinkService`) and the renderer; the chat adapter factory and the notification
service get the same instance. Telegram's is built in `src/composition/application_graph.py` once its adapter exists
(no storage port) and bound by `src/composition/platform_kit.py::register_late_platform`; Telegram is all or nothing,
so a failed build registers nothing. The renderer is stopped in `ApplicationGraph.shutdown`.

### link_list

The model puts `[display text][N]` or `[N]` anchors in the reply and the matching `{anchor, title, url}` items in
`link_list`; `extract_structured_response` (`src/utils/llm_response_parser.py`) reads them into
`SmartResponse.link_list`, and the handler hands them to the response channel's `send_*` calls. Each channel resolves
them: `SlackResponseChannel._resolve_links_slack` (before mrkdwn formatting, to `<url|label>`) and
`TelegramResponseChannel._resolve_links_telegram` (after formatting, on the escaped text, to `[label](url)`).
A `Title [N]` pair is first rewritten to `[Title][N]` so the name is not printed twice.

## Invariants and traps

- **One routine.** A chat turn and a background notification deliver rich content through `RichContentService.deliver`;
  a copy of the routing elsewhere drifts (`decisions/rich_content_one_delivery_path.md`).
  `tests/unit/services/test_rich_content_deliver.py`, `tests/unit/services/test_user_notification_rich_content.py`.
- **A stored page is never posted by its key.** Without a user id, a link service or a storage port, or when the store
  fails (logged as an error), the page is uploaded as a file. Telegram has no storage port, so it always uploads.
  `tests/unit/services/test_rich_content_service_store_pins.py`.
- **`html_gcs_link` posts nothing without a link.** `ConversationHandler._deliver_item` calls the private
  `_store_html` and posts `<url|text>` (Slack syntax); when no link can be minted nothing is sent.
- **`map_image` and `weather_image` are in `MEDIA_CONTENT_TYPES` but `process` skips them**, so the user sees nothing
  (no fallback text). A `widget` with no renderer (`ENABLE_HTML_RENDERER` off) is also skipped with a warning only.
- **Telegram shows no table**: `send_rich_content` posts `fallback_text`.
- **A Slack channel stored as a user id cannot take an upload.** `_send_rich_content` and `notify_file_bytes` first send
  a `📎` message, which turns the id into the DM id. A platform with no registered media port returns before posting.
- **Unanchored links are rescued only in the chat handler.** When `link_list` has items and the text has no `[N]` anchor,
  every link would be dropped silently. `ConversationHandler._append_unanchored_sources` appends a localized
  `UIMessage.SOURCES_HEADING` block of bare `[N]` lines, once, before chunking (an adapter-level append would repeat
  per Telegram chunk). `notify` and `notify_answer_copy` do not call it, so an unanchored `link_list` there is dropped.
  `tests/unit/test_ui_message_strings.py`; `decisions/unanchored_links_sources_block.md`.
- **Anchor resolvers differ in order on purpose**: Slack before formatting, Telegram after, because Telegram escaping
  turns `[N]` into `\[N\]`. `tests/unit/adapters/test_slack_link_resolution.py`,
  `tests/unit/adapters/test_telegram_link_resolution.py`.
- **Model html is never trusted in the renderer**: scripts off, network blocked.
  `tests/unit/adapters/test_playwright_html_renderer_filter.py`.
- **`NotifyResult.sent` is set before the first send**, so a send that raised still counts; a retry is allowed only
  when the turn never started. `tests/unit/services/test_notify_turn_started.py`,
  `tests/unit/services/test_notify_result_sent.py`.
- **Every `NotificationKind` needs an SLA entry.** `tests/unit/infrastructure/test_notification_sla.py`.
- **Trap: bare `[System: ...]` frames.** The root rule is that code-written text never claims the system's voice and
  uses `wrap_system` (`src/domain/system_fingerprint.py::wrap_system`). `UserNotificationService` builds bare frames:
  `src/services/user_notification_service.py:506` (the alert sent to the agent as `[System: {system_alert}{framing_suffix}]`)
  and `:692` (the `[System: async document ready - ...]` history turn of `notify_document_link`). The drive-note history
  lines (`drive_note_call_event`, `drive_note_late_event`) come from `src/domain/drive_note_receipt.py` in the same bare form. Not fixed here.

## Links

- [`WIDGET_PLAYWRIGHT_RFC.md`](../../10_rfcs/WIDGET_PLAYWRIGHT_RFC.md), [`RICH_CONTENT_RFC.md`](../../10_rfcs/RICH_CONTENT_RFC.md):
  the original designs.
- [`rich_content_one_delivery_path.md`](../../04_solution_strategy/decisions/rich_content_one_delivery_path.md),
  [`unanchored_links_sources_block.md`](../../04_solution_strategy/decisions/unanchored_links_sources_block.md).
- [`NOTIFICATION_DELIVERY_REFACTOR_RFC.md`](../../10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md) (obsolete, reasoning
  only), [`SYSTEM_TURN_FINGERPRINT_RFC.md`](../../10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md) (the framing rule),
  [`VOICE_COMPANION_RFC.md`](../../10_rfcs/VOICE_COMPANION_RFC.md) (call summary and answer copy).
- [Multi-agent system](../multi_agent_system/README.md).

## Where to start reading

1. `src/services/rich_content_service.py::RichContentService.deliver`: the one routing.
2. `src/services/rich_content_service.py::RichContentService._handle_file`: conversion and the html link path.
3. `src/services/user_notification_service.py::UserNotificationService.notify`: a background turn end to end.
4. `src/services/user_notification_service.py::UserNotificationService.resolve_channel`: where a notice goes.
5. `src/adapters/playwright_html_renderer.py::PlaywrightHtmlRenderer.render`: widget to PNG.
6. `src/composition/notification_setup.py::build_notifications`: the Slack kit and the service.
7. `src/handlers/conversation_handler.py::ConversationHandler._append_unanchored_sources`: link rescue.
