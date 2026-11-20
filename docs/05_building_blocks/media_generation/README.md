<!-- living-doc
covers:
  src/agents/image_generation_agent.py
  src/ports/image_generation_port.py
  src/adapters/grok_image_adapter.py
  src/agents/video_generation_agent.py
  src/ports/video_generation_port.py
  src/adapters/grok_video_adapter.py
  src/services/video_generation_delivery.py
  src/services/video_generation_polling_task.py
  src/ports/video_duration_port.py
-->
# Media Generation

## Purpose

This block lets the chat orchestrator make and change pictures and short videos through xAI's Grok Imagine models. Four
intents reach it: `generate_image`, `edit_image`, `generate_video`, `edit_video` (`src/infrastructure/agent_manifest.py::Intent`).
Two specialist agents own them, each behind its own port with one adapter, so a second provider would be a new adapter
and a registry entry.

Each agent makes **one text-LLM call first**, to turn the orchestrator's plain brief into a prompt the image or video
model answers well, and only then calls the render API. The call is there because the render models respond to prompting
techniques the orchestrator's conversational phrasing does not produce. The technique itself lives in the specialist's
prompt tokens (Firestore, loaded by `PromptBuilderPort.build_for_agent`), not in code.

The two media differ in shape, and that is the main thing to know:

- An **image** renders inside one Cloud Task and comes back as a delivery item.
- A **video** takes minutes. The agent only submits and acknowledges; a chain of `/worker` polling tasks waits for the
  render and delivers it later.

What it is not: it holds no store of its own (nothing generated is kept except through file storage), it does not pick a
user's channel (the request's origin channel is carried through), and it is not a general media pipeline: one image per
request, and no video extension or reference-to-video.

## Owned data

The block owns no collection. What it touches:

- **Generated files.** An image goes to file storage as a `document` through `DocumentDeliveryService` (see
  [file_storage](../file_storage/README.md)). A video goes straight to media storage under the key prefix
  `video_generation/<user_id>/`, with a link built by the file link service and shortened by the short-link service.
- **Usage cost.** A successful render records an external, non-token cost on the account through
  `QuotaService.record_usage` (tokens zero, cost from `src/domain/billing.py::calculate_external_cost`). The service
  keys are the constants `IMAGE_GENERATE_MODEL`, `IMAGE_EDIT_MODEL`, `VIDEO_GENERATE_MODEL`; the tier table
  (`_IMAGE_TIER_PRICING_USD`), the edit surcharge and the per-second video price live only in that module. This path
  does not go through `TokenLedger`, so without it xAI spend would be invisible to the `daily_cost_limit` alert.
- **User setting.** `UserBotConfig.max_video_duration_s` (optional) caps the video duration; resolved by
  `ConfigurationService.get_max_video_duration`, which supports user, account default and `DEFAULT_MAX_VIDEO_DURATION_S`, but
  the only caller (`UserAgentFactory._build_video_generation`) passes no account default, so in practice user, then the
  system constant. No Cabinet screen sets
  it; it is edited in the stored profile.
- **Poll state** exists only in the payload of the next `video_generation_polling` Cloud Task (the request id, the
  caller's identity and session, the duration estimate, the origin platform and the attempt count), built in
  `src/adapters/gcp_task_queue.py::GcpTaskQueue.enqueue_video_generation_polling`.

## Flows

### Wiring

`src/composition/application_graph.py::_build_generation_registries` registers `GrokImageAdapter` and `GrokVideoAdapter`
under provider name `grok`, **only when `XAI_API_KEY` is configured**; without it both registries are empty and the
agents are not built. `UserAgentFactory._build_image_generation` / `_build_video_generation` build one agent per user and
resolve the port through `AgentContextBuilder.resolve_image_generation_context` / `resolve_video_generation_context`.
Each agent has two provider axes: the text model for the crafting call (strategy entries `image_generation` and
`video_generation`, tier `PERFORMANCE` in `src/domain/user.py::_DEFAULT_AGENT_TIERS`) and the render port, both
`grok`-only with no fallback. The video agent is also skipped (logged as an error) when no `VideoDurationPort` is wired.

### Image: generate and edit

1. Smart delegates `generate_image` or `edit_image`. Both are `ExecutionMode.ASYNC` in the manifest, so
   `AgentCoordinator` enqueues a Cloud Task and `AgentWorkerHandler` runs the agent
   (`src/agents/image_generation_agent.py::ImageGenerationAgent.execute`). The task deadline is the descriptor's
   `dispatch_deadline_s`.
2. `edit_image` is validated **before** the paid crafting call: `image_refs` present, at most `_MAX_REFERENCE_IMAGES`. With more than one reference the crafting call is told the count and the placeholders
   `<IMAGE_0>`, `<IMAGE_1>`, `<IMAGE_2>`, xAI's positional convention.
3. The crafting call builds the prompt text. An empty answer fails the request.
4. Generate: `GrokImageAdapter.generate` posts to the images endpoint through the SDK. Edit: the agent resolves every ref
   with `FileConversionService.resolve_bytes` (all or nothing, order preserved), wraps each as `ReferenceImage` and calls
   `GrokImageAdapter.edit`.
5. `ImageGenerationAgent._respond_with_images` records the cost, then returns one `document` `DeliveryItem`.
6. `AgentWorkerHandler._deliver_document_result` stores it, shortens the link and sends it with `notify_document_link`.
   `file_upload` is false on purpose: the channel unfurls the link inline, and a native upload would show the picture
   twice. A failed task goes through `_notify_docx_failure`.

Resolution and quality are **never inferred**: the agent uses its fixed defaults (see `_resolve_resolution`, `_resolve_quality`) unless the orchestrator passes
`resolution` / `quality` in `context`, which the manifest tells it to do only when the user stated one. Price is tiered
by that pair, so the choice is a cost lever.

### Video: generate and edit

1. Smart delegates `generate_video` / `edit_video`. Both are `ExecutionMode.SYNC`: the agent returns an acknowledgement
   in seconds and the user gets the finished video minutes later in the same channel
   (`src/agents/video_generation_agent.py::VideoGenerationAgent.execute`).
2. `edit_video` needs `video_ref`, checked before the crafting call. The crafting call returns JSON
   (`video_prompt`, `aspect_ratio`), parsed in `_parse_crafted_response`; truncation and malformed JSON become a
   user-facing failure with nothing submitted.
3. Duration and resolution are **not crafted by the model**. `_resolve_duration` uses `DEFAULT_VIDEO_DURATION_S` and `_resolve_resolution` uses
   `DEFAULT_VIDEO_RESOLUTION`, unless the orchestrator passed an explicit value; only the duration is then clamped to the
   user's cap. This keeps the cost-bearing choice deterministic and attributable.
4. Generate with an `image_ref` is image-to-video: the bytes are resolved and sent, and the crafted `aspect_ratio` is
   **dropped** (the source image's own proportions must govern, otherwise xAI distorts it).
5. Edit: `_execute_edit` resolves the bytes, refuses a clip over `MAX_EDIT_VIDEO_BYTES`, then asks `VideoDurationPort`
   for the duration and refuses a clip over `MAX_EDIT_VIDEO_SOURCE_DURATION_S`. A `None` answer **does not block**; xAI's
   own check decides.
6. `GrokVideoAdapter.create_video` / `edit_video` submit and get a `request_id`, **log it**, and then enqueue the first
   poll through `TaskQueue.enqueue_video_generation_polling`. The adapter, not the agent, owns that side effect, so the
   agent stays delivery-agnostic. The agent returns `{"status": "started", "request_id": ...}`.
7. `/worker` runs `src/services/video_generation_polling_task.py::VideoGenerationPollingTask` (registered by
   `src/composition/worker_setup.py` under `video_generation_polling`; a `DisabledTask` when the registry or dispatch
   service is missing). One Cloud Task is one poll attempt:
   - `pending`, or a `get_status` exception: re-enqueue with `attempt + 1` and a delay.
   - `done`: `GrokVideoAdapter.get_status` has already downloaded the bytes; the task calls
     `src/services/video_generation_delivery.py::deliver_video`.
   - `failed` or `expired`: `notify` the user with the provider's reason.
   - `attempt` at `_MAX_VIDEO_POLL_ATTEMPTS`: notify "timed out" and stop.
8. `deliver_video` uploads to media storage, builds and shortens the link, calls `notify_document_link` to the origin
   channel, then records the cost from the **duration xAI reported** (falling back to the payload's estimate).

### Duration check of a user's input video

`VideoDurationPort.duration_s` never raises and answers `None` when it cannot tell. The adapter wired in
`src/composition/sandbox_media_setup.py::build_sandbox_media` is `SandboxMediaAdapter`: the bytes go to the sandboxed media decoder, where
`ffprobe` runs, so a user's untrusted video is never decoded in the main service (see
[`sandbox_render_service.md`](../../04_solution_strategy/decisions/sandbox_render_service.md) and
`docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md` S5b).

## Invariants and traps

- **No retries anywhere on a render.** Both agents set `RETRY_POLICY = NO_RETRY_POLICY` and both adapters build their
  client with `max_retries=0`: a 5xx can arrive after xAI has rendered and billed. Do not raise either, and keep the
  image task deadline above the agent timeout so a Cloud Tasks retry cannot re-run the render
  (only the image adapter's `max_retries=0` is pinned, in `tests/unit/adapters/test_grok_image_adapter.py`; the agents' policy and the deadline ordering are held by comments in `src/infrastructure/agent_config.py` and `src/infrastructure/agent_manifest.py` alone).
- **Context fields arrive in `message.payload`, not `message.context`.** The coordinator spreads the orchestrator's
  `context={...}` into the payload; `message.context` holds coordinator fields only. Reading `resolution`, `duration`,
  `image_ref(s)` or `video_ref` from the wrong one drops them silently, and the value is then silently ignored.
- **File refs are explicit fields, not the generic `file_ref` injection**, which is text-only and would misfire on binary.
  `edit_image` takes the array `image_refs`; `edit_video` takes `video_ref`; both are declared in `context_schemas`.
- **Rollout order for a new context field:** code first, then the Firestore `PROTOCOL_SMART_AGENT_SELECTION` text. The
  tool schema is generated from the manifest, so the reverse order has Smart passing a field the deployed agent ignores.
- **Do not use the SDK's typed methods for xAI.** `images.edit()` sends multipart, which xAI rejects; the typed
  `videos.*` methods follow OpenAI's own Sora shape. Image edit and all video calls use `AsyncOpenAI.post/get` (image
  generate does use the typed `images.generate`), and the video adapter uses
  `cast_to=object` so an unmodeled status or field cannot raise a validation error.
- **Edit takes a different shape.** One reference goes in `image`, two or three in the plural `images` (mutually
  exclusive). Video edit uses `_EDIT_MODEL`, a different model id from `_MODEL`; do not "unify" them without a live call.
- **The finished-video URL is a CDN link outside the API's auth**, so `get_status` downloads it with a plain `httpx`
  client. A failed download is a `failed` poll result, not an exception.
- **Cost is recorded after the fact, and only on success.** A video's cost is recorded in `deliver_video`, so a failed
  GCS upload (the function returns early), a `failed`/`expired` render or a timeout records nothing even if xAI billed.
  An image's cost needs an `account_id` in the message context and a wired quota service; either missing skips it
  silently.
- **A lost poll chain is a lost video.** The first poll is enqueued only after submission; if the enqueue raises, the
  only trace of the paid render is the logged `request_id`.
- **The poll payload carries no provider**; the task defaults to `grok`. A second video provider needs the field added
  to the payload and to `TaskQueue.enqueue_video_generation_polling`.
- **Failure notices reuse `NotificationKind.DEEP_RESEARCH`** in the polling task; that is a routing choice, not a claim
  that the job was research.
- **Origin channel:** `UserNotificationService` honours the channel override only when the platform is also given, so
  the polling task carries `origin_platform` through every re-enqueue; dropping it sends the video to the user's last
  active channel.
- **Output type is assumed:** the image adapter labels every result `image/png` without reading the response.

## Links

- [`IMAGE_GENERATION_RFC.md`](../../10_rfcs/IMAGE_GENERATION_RFC.md) and
  [`VIDEO_GENERATION_RFC.md`](../../10_rfcs/VIDEO_GENERATION_RFC.md): why the agent crafts a prompt, why video is a
  separate poll shape, the cost-safeguard rulings (fixed video defaults, duration cap, external-cost billing). Both are
  point-in-time records; where they differ from the code, the code is right.
- [`image_edit_multi_reference.md`](../../04_solution_strategy/decisions/image_edit_multi_reference.md): up to three
  references, per-image mime type, the order contract.
- [`grok_revival_2026_08.md`](../../04_solution_strategy/decisions/grok_revival_2026_08.md) and
  [Grok Integration](../grok_integration/README.md): the Grok chat adapter. The image and video adapters are deliberately
  independent of it (a different API surface, its own client).
- [File Storage](../file_storage/README.md), [File Conversion](../file_conversion/README.md),
  [`worker_task_dedup.md`](../../04_solution_strategy/decisions/worker_task_dedup.md).
- `src/agents/CLAUDE.md` § Agent Roster for the per-agent traps.

## Where to start reading

- `src/agents/image_generation_agent.py::ImageGenerationAgent.execute`
- `src/adapters/grok_image_adapter.py::GrokImageAdapter.edit`
- `src/agents/video_generation_agent.py::VideoGenerationAgent.execute`
- `src/adapters/grok_video_adapter.py::GrokVideoAdapter.get_status`
- `src/services/video_generation_polling_task.py::VideoGenerationPollingTask.__call__`
- `src/services/video_generation_delivery.py::deliver_video`
- `src/infrastructure/agent_manifest.py::VIDEO_GENERATION`
