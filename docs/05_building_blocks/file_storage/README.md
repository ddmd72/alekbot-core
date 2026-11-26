<!-- living-doc
covers:
  src/adapters/gcs_file_storage_adapter.py
  src/adapters/gcs_media_adapter.py
  src/ports/file_storage_port.py
  src/ports/media_storage_port.py
  src/agents/file_management_agent.py
  src/web/file_access_app.py
  src/services/file_access_token_service.py
  src/services/file_link_service.py
  src/domain/file_access.py
  src/composition/file_delivery_setup.py
  src/domain/short_link.py
  src/ports/short_link_repository_port.py
  src/adapters/firestore_short_link_repository.py
  src/services/short_link_service.py
  src/web/short_link_app.py
-->
# File Storage (Building Block)

## Purpose

This block keeps what the user attaches in chat and what the system generates for the user (PDF, HTML, DOCX, email
review, research reports, videos) in one **private** GCS bucket, and hands it out two ways: a capability link on our own
domain for the user, and a server-side re-read by key for the agents.

The central rule is **by reference**: file bytes never enter the session history, and the orchestrator does not get them on the upload turn
or when it delegates. An upload becomes a label plus a small `file_data` dict (`ref`, `mime_type`, `size_bytes`); the model reads the content on demand
through the `open_file` intent (a native image or PDF then reaches it on that tool turn only), and specialists get it pre-read by the coordinator. History therefore holds about a
line per file, and a file stays readable on later turns.

What it is not: parsing and text extraction are [file_conversion](../file_conversion/README.md); the user's long-term
drive (OneDrive App Folder) is [user_drive](../user_drive/README.md); generating documents is
[document_generation](../document_generation/README.md); the agent's own working files are the agent workspace
(`docs/10_rfcs/AGENT_WORKSPACE_RFC.md`). This block only stores, addresses and serves bytes, and routes `open_file`,
`delete_file` and `send_file_to_user` (the last takes any ref, email and drive ones included).

## Owned data

One bucket, `GCS_MEDIA_BUCKET`, with no public access. Two adapters write to it and keep apart by key prefix.

| Key | Written by | Read by |
|---|---|---|
| `{user_id}/files/{name}`: a chat upload, addressed by bare file name | `GcsFileStorageAdapter.upload` | `GcsFileStorageAdapter.download` (via `FileConversionService`) |
| `docs/{user_id}/{uuid}-{name}` and `email_review/{user_id}/{uuid}-{name}`: generated documents | `DocumentDeliveryService.store` (`_PREFIX`) through `GcsMediaAdapter.store` | `GcsMediaAdapter.fetch` (agents), the `/f` route (users) |
| `deep_research/{user_id}/...`, `video_generation/{user_id}/{timestamp}.mp4` | `src/services/deep_research_delivery.py`, `src/services/video_generation_delivery.py` | same |
| `external_tasks/{user_id}/{task_id}/{n}-{name}`: the copies an external task carries | `ExternalTaskFiles.store_all` through `GcsMediaAdapter.store` | the worker, through a signed link `ExternalTaskFiles.link` mints for the lease holder (`docs/05_building_blocks/external_task_queue/README.md`); never a ref the bot opens |

- **Delivered keys carry `user_id` as the second path segment.** That segment is the ownership proof for `open_file`
  (see Invariants).
- **Retention** is a bucket lifecycle rule set outside the repo: `email_review/` is deleted after 5 days,
  `external_tasks/` after 8 (`docs/07_deployment/README.md`, "Media bucket: the task files' age rule"), everything
  else after 30 (stated in the `GcsMediaAdapter` docstring and `decisions/private_file_storage_token_redirect.md`). History keeps
  keys and refs, so a ref can outlive its object; the readers answer "not found" or "expired" for it (Flows).
- **Capability token**, stored nowhere: an HS256 JWT with `key`, `uid`, `gated`, `type`, `iat`, `exp`
  (`FileAccessTokenService.mint`), signed with the Cabinet session secret passed to
  `src/composition/file_delivery_setup.py::build_file_delivery`. Its lifetime is policy in
  `src/domain/file_access.py`: `DEFAULT_FILE_LINK_TTL`, and the shorter `EMAIL_REVIEW_FILE_LINK_TTL`.
- **The upload's history footprint:** the user message holds a `MessagePart` with the label
  `[File: "<ref>" (<size>)]` and `file_data` = `ref`, `mime_type`, `size_bytes`, plus `original_name` when the stored
  name differs from the given one, plus a temporary `path` for a native binary (image, PDF), present on the upload turn
  only.
- **Ports:** `FileStoragePort` (uploads, by file name and `user_id`; the adapter assembles the key) and
  `MediaStoragePort` (delivered content, by full key, with a server-side read and a signed-URL mint). Neither returns a public
  URL. `FileStoragePort.get_url` assembles a bare `storage.googleapis.com` path that the private bucket answers with 403;
  nothing in `src/` calls it.

## Flows

### Upload turn

Entry: `src/handlers/conversation_handler.py::ConversationHandler._handle_message`.

1. **Voice messages never take this path.** An attachment with `is_voice_message` is transcribed first, its transcript
   is appended to the turn's text, and the attachment is dropped from the list
   (`tests/unit/handlers/test_conversation_handler_voice_message.py`). Without an audio service it stays a file. An audio
   *file* the user uploads is not a voice message and follows the steps below
   (`decisions/voice_message_transcription.md`).
2. For each attachment, `response_channel.download_file` fetches it to a temp path. When it returns `None`, the
   attachment's slot carries `src/utils/file_conversion.py::download_alert`: a note naming the file, telling the agent
   it did not arrive and must not be guessed. This is deliberately not the Quick fallback
   (`AgentFallbackService`): the primary agent is healthy and one input is missing.
3. `FileConversionService.process_attachment` reads the bytes, uploads them through `FileStoragePort.upload`, and returns
   the reference-only part. A failed upload (storage outage) or a handler built without the service puts
   `storage_unavailable_alert` in the slot instead; both cases log an ERROR line, which the Errors-dev alert watches.
   The turn goes on in all cases
   (`tests/unit/handlers/test_conversation_handler_upload_storage_down.py`,
   `tests/unit/handlers/test_conversation_handler_no_file_service.py`).
4. A turn with attachments and no text gets a localized file prompt as its text (set before the downloads, step 2).
5. In a bound channel (`SessionMode.is_bound`) the handler strips `path` from `file_data` before the agent runs, so
   adapters cannot inline the binary; the agent opens the file by `open_file` when it needs it.
6. Before the history save, `ConversationHandler._clean_user_parts` strips `path` from every part
   (`tests/unit/handlers/test_conversation_handler_delivery.py`); the `ref` stays.

### `open_file`, `delete_file`, `send_file_to_user`

Entry: `src/agents/file_management_agent.py::FileManagementAgent.execute`, a zero-LLM agent
(`src/infrastructure/agent_manifest.py`, descriptor with `prefetch_file_ref=False`).

1. `open_file` with a `drive:` ref goes to `DriveOperations.open`. Every other ref goes to
   `src/infrastructure/drive_operations.py::DriveOperations.fetch_stored`, then `FileConversionService.open`, which reads
   through the document service. A native file (image, PDF) comes back as `file_data` in the response metadata and
   reaches the model as a file-only part on the tool turn (`src/domain/llm.py::build_tool_turn`); text is marked as
   untrusted and cut at `MAX_CONVERTED_CHARS` (`src/domain/document_limits.py`); a failure is a note.
2. **The ref's shape picks the store, never the model**: `FileConversionService._download_by_ref` sends a
   `workspace:` ref to the workspace store, a `skill:` ref to the skill resolver, a key starting with one of
   `DELIVERED_REF_PREFIXES` (`src/domain/skill.py`) to `MediaStoragePort.fetch` after the ownership check, and anything
   else to `FileStoragePort.download`.
3. Exceptions to plain reading: a video ref is resent to the user's chat as a file (`DriveOperations._fetch_video`,
   capped by `MAX_VIDEO_ATTACH_BYTES`); a `workspace:` ref opens only under `binary_files/`
   (`DriveOperations._fetch_workspace`).
4. A missing object answers as "not found" with a re-upload hint (`DriveOperations.fetch_stored`).
5. `delete_file` deletes only a chat upload (`FileStoragePort.delete`). It refuses `drive:`, `workspace:` and `skill:`
   refs with a pointer to the right tool.
6. `send_file_to_user` resolves the bytes through `FileConversionService.resolve_file(..., action=ACTION_SEND)` and
   uploads them to the origin channel with `UserNotificationService.notify_file_bytes`.

### Specialist prefetch

Entry: `src/infrastructure/agent_coordinator.py::AgentCoordinator.prefetch_file_ref`.

1. When a delegation carries `file_ref`, the coordinator calls `FileConversionService.prefetch` and puts the text into
   the specialist's params as `file_content`, or a one-line `file_note` when it could not be read. An `http(s)` ref is
   skipped, and a resolver error becomes a `file_note` plus an ERROR line.
2. An agent that reads files itself opts out with `prefetch_file_ref=False`: this agent, the drive specialist, the email
   search agent.
3. The orchestrator never sees the content; the specialist gets text only. A specialist that needs the original bytes
   calls `FileConversionService.resolve_bytes`.

### Delivery and the `/f` link

Entry: `src/services/document_delivery_service.py::DocumentDeliveryService.store`.

1. `store` writes the bytes through `GcsMediaAdapter.store` (private; HTML gets a `noindex` meta) and returns the **key**,
   then `FileLinkService.build_link` mints the token and the link `<base>/f/<token>`. The prefix decides policy:
   `email_review/` is gated and short-lived, everything else is not gated and takes the default lifetime (`DEFAULT_FILE_LINK_TTL`).
2. The channel gets the link; the **history note** gets the key (`UserNotificationService.notify_document_link`), worded
   to send the agent back to `open_file` with that key. A video key gets a note that says `open_file` resends it as a
   file.
3. A click on `/f/<token>` (`src/web/file_access_app.py::create_file_access_blueprint`): verify signature, expiry and
   type, answering 401 on failure; for a gated token require a live Cabinet session (`CabinetSessionVerifier.identify`), else redirect to `/auth/login`, and
   answer 403 when the session's user is not the token's. The object must still be stored (else 410, below); a `video_generation/`
   key then renders a landing page (Download, Share); any other key gets a fresh signed URL of `_SIGNED_URL_TTL` and a 302.
4. `/f/<token>/raw` streams the bytes through `MediaStoragePort.fetch` with Range support (a Safari video needs it).
5. An object the lifecycle already deleted answers **410** (a video key too, before its landing page), a storage failure
   **503** (`tests/unit/web/test_file_access_app.py`).

## Invariants and traps

- **The label carries the unique `ref`, never the original file name.** The model addresses a file by the name in the
  label, and the download is an exact-key match. Slack names every pasted image `image.png`; dedup stores it as
  `image (4).png`. A label showing `image.png` would open whatever old object holds that un-suffixed name, and the bot would
  describe the wrong picture. `original_name` is metadata only. Enforced by
  `tests/unit/services/test_file_conversion_service_gcs.py::TestProcessAttachmentDedup.test_label_uses_deduped_ref_not_original_name`.
- **The orchestrator is meant to be blind to an attachment on the upload turn.** In every LLM adapter (Claude, OpenAI,
  Gemini, Grok) the part loop is `if part.text ... elif part.file_data`, and the upload part carries the label as
  `text`, so only the label is emitted; the image arrives on the `open_file` tool turn. This is the design, not a bug:
  do not make an adapter emit the file part next to the label, and do not simplify that chain. (OpenAI and Grok also skip
  a `file_data` with a `ref`; Claude and Gemini would inline a ref plus `path` one if the chain let it through.)
- **Vision routing follows the same rule.** `RouterAgent` forces deep reasoning only for a `ref` file whose mime type is
  `image/*` (or a legacy `file_data` without `ref`); a PDF label alone does not
  (`src/agents/core/router_agent.py`, `tests/unit/agents/core/test_router_agent.py`).
- **A stored `prompt_content` row cannot tell whether an image was sent.** `_render_messages`
  (`src/adapters/bigquery_prompt_content_adapter.py`) renders text and tool calls only, never `file_data`. Check the code
  path and the `open_file` result size in the logs instead.
- **Ownership is checked on the key's second segment.** `_download_by_ref` raises `PermissionError` for a delivered key
  whose `user_id` segment is not the requester's, or that has fewer than three segments, before any read
  (`tests/unit/services/test_file_conversion_service_gcs.py`,
  `tests/integration/offline/test_private_file_storage_roundtrip.py::TestPrivateStorageRoundTrip::test_refetch_by_other_user_rejected`).
- **The bucket is private; the long-lived thing is the token, not a storage signature.** A click mints a signed URL
  that lives `_SIGNED_URL_TTL`, so the link's lifetime (the two TTL constants in `src/domain/file_access.py`) does not run into the 7-day cap on GCS signed URLs. A direct
  `storage.googleapis.com` path is 403. The token's `uid` is enforced only when `gated`.
- **Gating comes from the key prefix**, decided in one place, `FileLinkService.build_link`; the storage adapters return
  keys and know nothing of tokens. `DocumentDeliveryService` repeats the TTL per storage class in `_TTL_SECONDS` for its
  caller's short alias; keep the two in step (`tests/unit/services/test_file_link_service.py`,
  `tests/unit/services/test_document_delivery_service.py`).
- **Agents re-read delivered files server-side, never through the external `fetch_url`**, which cannot reach a private
  object and has no session. The history note therefore carries the key, not the link.
- **Upload names are made safe and unique in the adapter.** `sanitize_filename` normalizes to NFC (Slack sends NFD, and
  upload and download paths diverge on Cyrillic diacritics otherwise; `download` normalizes too) and replaces `#?[]*`,
  tab, CR, LF and `:`; the colon would collide with the `skill:` ref prefix. Dedup is Finder-style
  (`report (1).docx`), and the write is conditional (`if_generation_match=0`): a name taken in between moves to the next
  free one and never replaces that file; a fresh `upload_id` in the object metadata lets a client's own retry of a
  landed write read back as success; `_MAX_NAME_ATTEMPTS` bounds the loop
  (`tests/unit/adapters/test_file_storage_store_kit.py`).
- **A temp-file suffix is bounded in bytes.** `safe_temp_suffix` keeps `tempfile` under NAME_MAX (255 bytes; one
  Cyrillic character costs 2). An unbounded suffix made a long browser-saved file name raise `OSError` in the response
  channel's `download_file`, which swallowed it, and the file vanished (`tests/unit/utils/test_safe_temp_suffix.py`).
  A failed `download_file` is still loud: the response channels log it with `logger.error`, which the ops alert
  watches (`src/adapters/slack/response_channel.py`).
- **Voice transcription precedes the file path.** A voice attachment sent down the file path would leave history with
  only a "no text plus attachment" fallback and consolidation would have nothing to read.
- **Without `GCS_MEDIA_BUCKET` the block is off.** `ServiceContainer` builds no `GcsFileStorageAdapter`, no
  `FileConversionService` and no `GcsMediaAdapter`; `UserAgentFactory._build_file_management` returns `None`;
  `build_file_delivery` returns no link or delivery service. An attachment then gets the storage-unavailable note and an
  ERROR line, so a mis-wired graph is loud.

## Links

- [`FILE_STORAGE_RFC.md`](../../10_rfcs/FILE_STORAGE_RFC.md): the original design.
- [`private_file_storage_token_redirect.md`](../../04_solution_strategy/decisions/private_file_storage_token_redirect.md):
  private bucket, `/f` capability route, server-side agent re-read.
- [`voice_message_transcription.md`](../../04_solution_strategy/decisions/voice_message_transcription.md): why voice
  messages skip the file path.
- [`EXTERNAL_FILES_RFC.md`](../../10_rfcs/EXTERNAL_FILES_RFC.md): refs, the open path, caps, untrusted marking.
- [`AGENT_WORKSPACE_RFC.md`](../../10_rfcs/AGENT_WORKSPACE_RFC.md) (`workspace:` refs),
  [`USER_DRIVE_RFC.md`](../../10_rfcs/USER_DRIVE_RFC.md) and `docs/10_rfcs/DRIVE_SPECIALIST_RFC.md` (the drive, the other
  store).
- [file_conversion](../file_conversion/README.md), [document_generation](../document_generation/README.md),
  [user_drive](../user_drive/README.md).

## Where to start reading

1. `src/handlers/conversation_handler.py::ConversationHandler._handle_message`: the upload turn, voice and failure notes.
2. `src/services/file_conversion_service.py::FileConversionService.process_attachment`: upload and the reference-only part.
3. `src/services/file_conversion_service.py::FileConversionService._download_by_ref`: ref shape to store, ownership.
4. `src/agents/file_management_agent.py::FileManagementAgent.execute`: the three intents.
5. `src/services/document_delivery_service.py::DocumentDeliveryService.store`: store, key and link.
6. `src/web/file_access_app.py::create_file_access_blueprint`: the `/f` route.
7. `src/adapters/gcs_file_storage_adapter.py::GcsFileStorageAdapter.upload`: names, dedup, conditional write.
