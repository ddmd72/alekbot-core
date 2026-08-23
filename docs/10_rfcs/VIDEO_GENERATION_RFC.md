# RFC: VideoGenerationAgent — Video Generation & Editing via grok-imagine-video-1.5

**Status:** DRAFT
**Date:** 2026-08-23
**Owner:** AI Engineering
**Milestone:** Specialist Agents — new capability
**Related:** `docs/10_rfcs/IMAGE_GENERATION_RFC.md` (structural precedent — this RFC follows its
pattern for both axes, per its own §3.9 forward-compatibility note), `DeepResearchPort`/
`job_registry` (ACK+poll precedent), `docs/how_to/NEW_AGENT_PLAYBOOK.md`

---

## 1. Problem Statement

`ImageGenerationAgent` (shipped 2026-08-21) gave the bot the ability to create and edit images.
The same gap exists for video: no path from "make a video of X" / "extend this clip" to a
delivered video. `IMAGE_GENERATION_RFC.md` §3.9 explicitly deferred this, predicting the shape
it would need: "a different prompting framework," "likely needs an ACK+poll shape
(`DeepResearchPort`-style) rather than `HtmlPageGeneratorAgent`'s fire-and-forget-but-fast
pattern," and "its own `DeliveryItem` shape/size, its own port contract." All three predictions
are confirmed against xAI's actual API (§2) and drive this design.

**Desired outcome:** the user keeps talking to Smart exactly as today. When a request calls for
a video, Smart delegates to a new `VideoGenerationAgent`, which crafts an Aurora-video prompt,
submits it to `grok-imagine-video-1.5`, and — because generation takes minutes, not seconds —
acknowledges immediately and delivers the finished video into the same conversation once it's
ready.

---

## 2. Market Research Summary

Researched 2026-08-23, verified directly against `docs.x.ai` (not third-party aggregators —
an initial search of OpenRouter/ImagineArt/Vercel pages incorrectly claimed image-to-video was
the *only* supported mode; official xAI docs confirm otherwise).

| Aspect | Finding |
|---|---|
| Model | `grok-imagine-video-1.5` — image/video sibling of `grok-imagine-image-2.0` (Aurora family). |
| Capabilities | Five, all via `/v1/videos/*`: **text-to-video** and **image-to-video** (same `POST /v1/videos/generations` endpoint; `image` is an optional param — source image becomes frame 1), **reference-to-video** (same endpoint + `reference_images`, up to 3, + `reference_audios`, up to 3 preset voices, tagged `<IMAGE_N>`/`<AUDIO_N>` in the prompt), **video editing** (`POST /v1/videos/edits` — modify an existing video via prompt, preserving the rest of the scene), **video extension** (`POST /v1/videos/extensions`). |
| Async model | **Poll-only, no webhook mechanism documented.** Submit → `GET /v1/videos/{request_id}` → `status`: `pending` / `done` (`video.url`, `duration`) / `failed` (`error.code`/`error.message`) / `expired`. "Video generation is an asynchronous process that typically takes up to several minutes to complete," varying with prompt complexity, duration, resolution, and whether editing is involved. |
| Parameters | `prompt` (required), `duration` (1-15s), `resolution` (`480p`/`720p`/`1080p`, default `480p`), `aspect_ratio` (`1:1`/`16:9`/`9:16`/`4:3`/`3:4`/`3:2`/`2:3`), `image` (url / base64 data URI / `file_id`, for image-to-video). |
| Pricing | **$0.080/sec at 480p — confirmed directly on docs.x.ai.** 720p ($0.14/sec) and 1080p ($0.25/sec) figures, and a $0.01/input-image charge, appear only in third-party sources (OpenRouter, Vercel AI Gateway) — **not independently confirmed against xAI's own pricing page.** Treated as unconfirmed; flagged in §10. Edits are presumed to follow the same per-second-of-output billing (unconfirmed — no separate edit pricing found). |
| Rate limits | 10 requests/sec, regions `us-east-1`/`us-west-2`, batch API supported (per the model's docs.x.ai page). |
| Error codes | `invalid_argument` (bad input/duration/moderation block), `permission_denied`, `failed_precondition` (unsupported op for model/settings), `service_unavailable` (retry advised), `internal_error`. |

Sources: [xAI Imagine overview](https://docs.x.ai/developers/model-capabilities/imagine) ·
[Video generation](https://docs.x.ai/developers/model-capabilities/video/generation) ·
[Image-to-video](https://docs.x.ai/developers/model-capabilities/video/image-to-video) ·
[Reference-to-video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video) ·
[grok-imagine-video-1.5 model page](https://docs.x.ai/developers/models/grok-imagine-video-1.5)

### 2.1 Prompting technique — Subject/Background/Camera + Motion, not the image clusters

Per `IMAGE_GENERATION_RFC.md` §3.9, video prompting is a genuinely different framework from the
three image technique clusters (photoreal / text-heavy / asset-set): video prompts decompose
motion across **Subject+Motion** (what moves and how), **Background+Motion** (what the
environment/camera-independent scene does), and **Camera+Motion** (pans, zooms, tracking shots) —
composed together rather than picked as alternatives like the image clusters were. As with images,
this technique lives in the specialist's own Firestore prompt (`COGNITIVE_PROCESS_VIDEO_GEN`), not
in Smart's always-loaded protocol token — same boundary reasoning as image RFC §3.5.

### 2.2 Scope decision — v1 covers 2 of xAI's 5 operations

**Decided with the owner 2026-08-23:** v1 ships `generate_video` (text-to-video, with an optional
starting image folded in as image-to-video — same endpoint, one extra param) and `edit_video`
(modify an uploaded video via prompt). **Reference-to-video and video extension are explicitly
deferred**, same pattern as image editing's original single-reference decision (RFC #6) that was
later extended once a real use case appeared — extending to a 3rd/4th/5th intent later is
additive, not a rewrite, because both deferred operations already share `create_video`'s request/
poll shape.

---

## 3. Architecture

### 3.1 Core Design Principles

1. **Commissioning model, unchanged.** Smart still elicits *what* the user wants interactively;
   `VideoGenerationAgent`'s `query` is a natural-language creative brief, same as image generation.
2. **The specialist owns *how* to phrase it for Aurora-video specifically** — one LLM call
   translates the brief into a Subject/Background/Camera+Motion prompt (§2.1).
3. **ACK-then-deliver, not fire-and-forget-but-fast.** Unlike `ImageGenerationAgent` (pixel
   rendering happens *inside* `execute()`, ~10-60s, tolerable inside one Cloud Task),
   `VideoGenerationAgent.execute()` does only the fast part (craft prompt, submit to xAI, arrange
   delivery) and returns in seconds — matching `DeepResearchAgent`'s shape, not
   `ImageGenerationAgent`'s. The multi-minute wait happens entirely outside any single request.
4. **Provider is behind a port from day one**, mirroring image RFC §3.6-3.7 — same reasoning,
   same mechanism, applied to a fourth `ProviderRegistry` family.
5. **Reference-to-video and extension are out of scope for v1**, per §2.2.

### 3.2 Intent Design — Two Intents, One Agent

| Intent | Orchestrator signal | xAI endpoint |
|---|---|---|
| `generate_video` | User wants a new video from a description, optionally animating an uploaded starting image | `/v1/videos/generations` |
| `edit_video` | User wants an existing uploaded video changed via instruction | `/v1/videos/edits` |

### 3.3 Full Flow — `generate_video`

```
User (interactive): describes the video; Smart clarifies subject/motion/mood/duration
if vague, same commissioning pattern as image generation
  |
  v Smart -> delegate_to_specialist(intent="generate_video", query="<brief>",
             context={"image_ref": "<optional starting-image filename>"})
      |
      +-- VideoGenerationAgent.execute()   [SYNC — returns in seconds, like DeepResearchAgent]
      |     |
      |     +-- (if image_ref present) resolve to bytes via FileConversionService.resolve_bytes()
      |     |     — same file_ref/image_ref trap as image RFC §3.4: this must NOT go through
      |     |       AgentCoordinator._resolve_file_refs()'s text-only auto-injection.
      |     |
      |     +-- Build system prompt via PromptBuilder (agent_type="video_generation")
      |     |
      |     +-- LLM call #1 (structured JSON output — Mode 1, NEW_AGENT_PLAYBOOK.md):
      |     |     brief (+ image presence) -> {video_prompt, duration, resolution, aspect_ratio}.
      |     |     Owner decided (2026-08-23) the LLM should INFER duration/resolution from the
      |     |     brief rather than defaulting to fixed conservative values — unlike image
      |     |     generation's freeform-text crafting output, this needs structured fields.
      |     |
      |     +-- VideoGenerationPort.create_video(prompt, user_id, account_id, session_id,
      |     |     image_data=..., duration=..., resolution=..., aspect_ratio=...) -> request_id
      |     |     — the ADAPTER submits to xAI AND enqueues the first poll tick as a side
      |     |       effect (§3.4), exactly mirroring how ClaudeDeepResearchAdapter triggers its
      |     |       Cloud Run Job inside create_interaction(). The agent never touches TaskQueue.
      |     |
      |     +-- AgentResponse.success(result={"status": "started", "request_id": request_id})
      |           — ACK only, no delivery_items. Smart speaks an immediate
      |             "generating your video, I'll send it when it's ready" — same co-emission
      |             pattern as deep_research's ACK, not image generation's same-turn delivery.
      |
      +-- [several minutes later, out of band]
            WorkerHandler._handle_video_generation_polling (§3.4) delivers the finished video
            as a GCS-linked message via UserNotificationService, independent of the original
            Smart turn.
```

### 3.4 Delivery Mechanism — ACK + Adapter-Owned Poll Enqueue (the core architectural decision)

**Why not `ImageGenerationAgent`'s shape:** that agent's `execute()` blocks on the pixel-rendering
call (`await self._image_port.generate(prompt)`) inside a single `ExecutionMode.ASYNC` Cloud Task,
bounded by `dispatch_deadline_s` (720s and safely inside the Cloud Tasks hard ceiling of 1800s).
xAI's own docs describe video generation as taking "up to several minutes," varying with duration/
resolution/prompt complexity — an unbounded-enough tail that blocking a single request on it is
the wrong shape, exactly as image RFC §3.9 predicted.

**Why not reusing `DeepResearchPort`/`_handle_deep_research_polling` directly:** two blockers.
First, `DeepResearchPort.get_status()` returns `tuple[str, str]` — a text payload — which doesn't
fit binary video without an awkward base64-as-string threading; video needs "its own port
contract" (image RFC §3.9, explicit). Second — a concrete finding from this investigation —
**`_handle_deep_research_polling` is currently dead code**: `grep` confirms
`enqueue_deep_research_polling` is called only from inside that same handler (self-re-enqueue on
`in_progress`/error), never from an actual job-kickoff site. It is a leftover from the Gemini deep
research backend removed 2026-05-29. Copying its *pattern* is right; copying its (orphaned) code
path is not.

**Why not a Cloud Run Job** (like `ClaudeDeepResearchRunnerAgent`): justified there because the
job does 5-60+ minutes of real agentic work in-process. Here we are only submitting to a REST API
and polling it — a Job's container/entrypoint overhead is unearned.

**Chosen shape**, verified against real precedent in `deep_research_delivery.py` and
`ClaudeDeepResearchAdapter` (not invented fresh):

1. **`VideoGenerationAgent.execute()` stays delivery-agnostic** — exactly like `DeepResearchAgent`
   ("the agent calls port.create_interaction() and returns ACK... no queue logic in the agent").
   It never touches `TaskQueue`/`TaskDispatchService`. `ExecutionMode.SYNC` (not `ASYNC`) — there
   is no more slow work inside `execute()` to justify a dedicated Cloud Task dispatch.
2. **The adapter enqueues the first poll tick as a side effect of submission** — mirrors
   `ClaudeDeepResearchAdapter.create_interaction()` triggering its Cloud Run Job inside the same
   call. `GrokVideoAdapter` takes a `task_queue: TaskQueue` constructor dependency (a port, same
   pattern as `ClaudeDeepResearchAdapter` taking `job_runner: JobRunnerPort` — adapters may depend
   on other ports per `adapters/ → domain/, ports/, config/`).
3. **`TaskQueue` port gets a new named method** `enqueue_video_generation_polling(request_id,
   user_id, account_id, session_id, attempt=0, delay_seconds=...)`, implemented in
   `GcpTaskQueue` — a direct sibling of the already-existing (if currently unreached)
   `enqueue_deep_research_polling`. `TaskDispatchService` gets a thin passthrough wrapper of the
   same name, used by `WorkerHandler` for its own self-re-enqueue on `pending`.
4. **New `WorkerHandler` task type `video_generation_polling`**, structurally identical to
   `_handle_deep_research_polling` (one attempt per Cloud Task, re-enqueues with a delay via
   `schedule_time` on `pending`, no in-process sleep) but *actually wired* from step 2 above.
   On `done`: calls a new `services/video_generation_delivery.py::deliver_video()` (mirrors
   `deliver_deep_research()`'s shape: upload bytes to GCS via `MediaStoragePort`, then
   `notification.notify_document_link(...)` — the same call deep research already uses for its
   round-1/round-2 report links). On `failed`/`expired`: `notification.notify()` the user directly
   — no silent drop, matching deep research's failure path.
5. Per the owner's delivery-format decision (2026-08-23): **GCS link only, no native
   `file_upload`** — same choice `ImageGenerationAgent` made (`file_upload: False`), and for video
   specifically it also sidesteps Telegram's 50MB bot-API document cap, which a long/1080p
   generation could otherwise hit.

### 3.5 New Port — `VideoGenerationPort`

```python
# src/ports/video_generation_port.py
@dataclass(frozen=True)
class VideoPollResult:
    status: str                    # "pending" | "done" | "failed" | "expired"
    data: Optional[bytes] = None   # populated only when status == "done"
    mime_type: str = "video/mp4"   # unconfirmed against a live response — see §10
    error: str = ""                # populated on "failed"

class VideoGenerationPort(ABC):
    @abstractmethod
    async def create_video(
        self, prompt: str, user_id: str, account_id: str, *,
        image_data: Optional[bytes] = None, image_mime_type: str = "image/png",
        duration: Optional[int] = None, resolution: Optional[str] = None,
        aspect_ratio: Optional[str] = None, session_id: Optional[str] = None,
    ) -> str:
        """Submit text-to-video or image-to-video. Arranges delivery (enqueues the first
        poll tick) as a side effect, mirroring ClaudeDeepResearchAdapter. Returns request_id."""

    @abstractmethod
    async def edit_video(
        self, prompt: str, video_data: bytes, user_id: str, account_id: str, *,
        video_mime_type: str = "video/mp4", session_id: Optional[str] = None,
    ) -> str:
        """Submit a video edit. Same delivery-arrangement contract as create_video()."""

    @abstractmethod
    async def get_status(self, request_id: str) -> VideoPollResult:
        """Poll job status. On "done", downloads xAI's returned video.url into bytes
        internally — callers never handle a raw URL, keeping delivery byte-based like
        ImageGenerationPort."""
```

`GrokVideoAdapter` (`src/adapters/grok_video_adapter.py`, new file, independent `AsyncOpenAI`
client construction — no sharing with `GrokImageAdapter`/`GrokAdapter`, same no-cross-adapter-
coupling convention as image RFC §3.6) implements this. `get_status()` needs an HTTP client
(`httpx.AsyncClient`) to download `video.url` on `done` — a new capability this adapter needs that
`GrokImageAdapter` didn't (images return `b64_json` inline; video returns a URL).

`create_video()`/`edit_video()` request bodies: image/video source data is sent as a base64 data
URI (`data:{mime_type};base64,{...}`), same wire convention `GrokImageAdapter.edit()` already
established for xAI's `image_url`-typed fields.

**No retry, anywhere** — same rationale as image gen: `VideoGenerationAgent.RETRY_POLICY =
NO_RETRY_POLICY`, adapter's `AsyncOpenAI(max_retries=0)`. A transient 5xx after xAI has already
rendered (and billed for, per-second) a video must not trigger a second paid render. Polling
(`get_status`) is unaffected — status checks are not a billed generation.

### 3.6 Provider Abstraction — Same Two Axes, Fourth `ProviderRegistry` Family

**Axis 1 — crafting LLM.** Standard `AgentProviderStrategy` entry:
```python
"video_generation": {
    "default_provider": "grok",
    "allowed_providers": ["grok"],
    "required_capabilities": [],
    "fallback": None,
},
```

**Axis 2 — video-rendering port.** New `ProviderRegistry[VideoGenerationPort]` (`video_registry`),
new constructor param on `UserAgentFactory` and `WorkerHandler` (mirrors `image_registry`/
`job_registry` exactly — `UserAgentFactory` already holds three separately-typed registries;
this is the fourth). `GrokVideoAdapter` registered under `"grok"`, `allowed_providers: ["grok"]`,
no fallback — only one implementation exists, same as image RFC decision #8.

### 3.7 Tier — `PERFORMANCE`, Set on Day One

`_DEFAULT_AGENT_TIERS["video_generation"] = PerformanceTier.PERFORMANCE` goes in as part of this
RFC's implementation, **not discovered missing after the fact**. This is the exact gap
`decisions/agent_tier_default_enforcement.md` (2026-08-23) closed for `image_generation`/
`compute`/`tasks` — a video-motion prompt (§2.1) needs real reasoning quality for the same reason
image's technique clusters did, and `test_every_llm_agent_has_a_default_tier` will fail the build
if this entry is skipped.

### 3.8 Structured Output — Crafting Call Differs From Image Generation's

Image generation's crafting call returns **freeform text** (the prompt string only). Video's
crafting call needs **structured fields** (`video_prompt`, `duration`, `resolution`,
`aspect_ratio`) because the owner chose LLM-inferred duration/resolution over fixed defaults
(§2.2 decision context). This is Mode 1 from `NEW_AGENT_PLAYBOOK.md` (single-pass JSON, no custom
tools) — `response_schema` + `response_mime_type`, both enforced on the locked `grok` provider
(Grok honors `response_schema` via `text.format={"type":"json_schema",...}` since 2026-08-14, per
`src/adapters/CLAUDE.md`). This is a real divergence from `ImageGenerationAgent`'s pattern, called
out explicitly per CLAUDE.md's delta-declaration gate rather than silently copied.

### 3.9 `edit_video` — Reference Trap, Same As Image

`edit_video`'s context schema uses `video_ref` (not `file_ref`), resolved directly via
`FileConversionService.resolve_bytes()` — identical reasoning to image RFC §3.4:
`AgentCoordinator._resolve_file_refs()`'s generic `file_ref` auto-injection is text-only
(`markitdown`-based) and would not produce usable video bytes for an `image/*`-or-`video/*` mime
type. v1 supports a single reference video (not a list) — no known xAI multi-video-edit
capability exists to justify the array shape `image_refs` needed for images.

### 3.10 Hexagonal Compliance Check

| File (layer) | Depends on | Compliant because |
|---|---|---|
| `ports/video_generation_port.py` | stdlib only | Matches `image_generation_port.py` — zero domain/adapter imports. `VideoPollResult` defined locally, not imported from `image_generation_port.py` (cross-port imports forbidden, `ports/CLAUDE.md`) |
| `ports/task_queue.py` (new method) | stdlib only | Sibling addition to an existing port, no new import surface |
| `adapters/grok_video_adapter.py` | `ports/` (`VideoGenerationPort`, `TaskQueue`), `config/`, `openai` SDK, `httpx` | `adapters/ → domain/, ports/, config/`. Depending on `TaskQueue` (a port) mirrors `ClaudeDeepResearchAdapter` depending on `JobRunnerPort` — established, not novel |
| `adapters/gcp_task_queue.py` (new method) | unchanged | Sibling addition to an existing adapter |
| `services/agent_context_builder.py` (new method) | `ports.video_generation_port.VideoGenerationPort` | Same top-level-import precedent as `resolve_image_generation_context()` |
| `services/video_generation_delivery.py` | `ports/` (`MediaStoragePort`, `NotificationPort`) only | `services/ → domain/, ports/`. Direct structural sibling of `deep_research_delivery.py` |
| `agents/video_generation_agent.py` | `ports.video_generation_port.VideoGenerationPort` + `services.file_conversion_service.FileConversionService` (constructor params) | Same precedent as `ImageGenerationAgent` taking `ImageGenerationPort` + `FileConversionService` directly |
| `handlers/worker_handler.py` (new method + `video_registry` param) | `ports.provider_registry.ProviderRegistry` | Mirrors existing `job_registry` param exactly |
| `composition/*` | `GrokVideoAdapter` concrete class | Only place it's named, same as `GrokImageAdapter` |

---

## 4. New Components

| Component | File | Notes |
|---|---|---|
| `VideoGenerationPort`, `VideoPollResult` | `src/ports/video_generation_port.py` | New. `create_video()`, `edit_video()`, `get_status()`. |
| `TaskQueue.enqueue_video_generation_polling` | `src/ports/task_queue.py` | New method, sibling of `enqueue_deep_research_polling`. |
| `GrokVideoAdapter` | `src/adapters/grok_video_adapter.py` | New. Implements `VideoGenerationPort` via xAI `/v1/videos/*`; takes `task_queue: TaskQueue`. |
| `GcpTaskQueue.enqueue_video_generation_polling` | `src/adapters/gcp_task_queue.py` | New method implementation. |
| `VideoGenerationAgent` | `src/agents/video_generation_agent.py` | New. `DeepResearchAgent`-shaped: 1 structured-output LLM call + port call + ACK response. `ExecutionMode.SYNC`. |
| `VideoGenerationAgentConfig` | `src/infrastructure/agent_config.py` | New `@dataclass` — temperature, timeouts (see §10). |
| `deliver_video()` | `src/services/video_generation_delivery.py` | New. Structural sibling of `deliver_deep_research()`. |
| Intent constants | `src/infrastructure/agent_manifest.py` | `GENERATE_VIDEO`, `EDIT_VIDEO`. |
| `AgentDescriptor` | `src/infrastructure/agent_manifest.py` | `eager=False`, `ExecutionMode.SYNC` both intents, `context_schemas={EDIT_VIDEO: {"video_ref": ...}, GENERATE_VIDEO: {"image_ref": ... (optional)}}`. |
| `"video_generation"` strategy | `src/services/agent_context_builder.py` | Text-LLM axis (§3.6 Axis 1). |
| `resolve_video_generation_context()` | `src/services/agent_context_builder.py` | New method, mirrors `resolve_image_generation_context()`. Video-port axis (§3.6 Axis 2). |
| `video_registry` | `src/composition/user_agent_factory.py` + `src/handlers/worker_handler.py` | New constructor param on both, mirrors `image_registry`/`job_registry`. |
| `_handle_video_generation_polling` | `src/handlers/worker_handler.py` | New task type `video_generation_polling`. |
| `_DEFAULT_AGENT_TIERS["video_generation"]` | `src/domain/user.py` | `PerformanceTier.PERFORMANCE`, set on day one (§3.7). |
| Firestore token | `COGNITIVE_PROCESS_VIDEO_GEN` | New. Subject/Background/Camera+Motion framework (§2.1). |
| Firestore blueprint/profile | `video_generation_agent_v1` / `video_generation` | New, per playbook Phase 2. |
| `PROTOCOL_SMART_AGENT_SELECTION` section | Firestore (manual edit) | `generate_video`/`edit_video`, ACK-then-deliver wording (not same-turn like images). |

---

## 5. Files Changed

```
Code:
  src/ports/video_generation_port.py         NEW — port + VideoPollResult
  src/ports/task_queue.py                    + enqueue_video_generation_polling
  src/adapters/grok_video_adapter.py         NEW — GrokVideoAdapter
  src/adapters/gcp_task_queue.py             + enqueue_video_generation_polling impl
  src/infrastructure/agent_manifest.py       Intent.GENERATE_VIDEO/EDIT_VIDEO + AgentDescriptor
  src/infrastructure/agent_config.py         VideoGenerationAgentConfig
  src/domain/user.py                         _DEFAULT_AGENT_TIERS["video_generation"]
  src/services/agent_context_builder.py      "video_generation" strategy entry +
                                              resolve_video_generation_context()
  src/services/video_generation_delivery.py  NEW — deliver_video()
  src/services/task_dispatch_service.py      + enqueue_video_generation_polling wrapper
  src/agents/video_generation_agent.py       NEW — VideoGenerationAgent
  src/handlers/worker_handler.py             video_registry param + _handle_video_generation_polling
  src/composition/user_agent_factory.py      video_registry param + wiring (eager=False path)
  src/composition/*.py (bootstrap / main.py)  register GrokVideoAdapter under "grok" in
                                              video_registry; wire video_registry into
                                              WorkerHandler
  src/utils/capabilities.py                  user-facing capabilities entry
  tests/unit/agents/test_video_generation_agent.py       NEW
  tests/unit/adapters/test_grok_video_adapter.py         NEW — wire test, mock at SDK boundary
  tests/unit/handlers/test_worker_handler.py             + video_generation_polling coverage
  tests/unit/domain/test_user.py                         video_generation tier coverage
  tests/contracts/adapter_contracts.py                   new ContractRule for VideoGenerationPort

Prompt files:
  firestore_utils/uploads/COGNITIVE_PROCESS_VIDEO_GEN.groovy / .json
  firestore_utils/uploads/video_generation_agent_v1.json
  firestore_utils/uploads/video_generation.json

Firestore token updates (manual):
  PROTOCOL_SMART_AGENT_SELECTION   add generate_video/edit_video section
```

---

## 6. Design Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | `VideoGenerationAgent` is `ExecutionMode.SYNC`, not `ASYNC` | The slow part (waiting on xAI) lives entirely outside `execute()`; no remaining reason to pay for a dedicated Cloud Task dispatch — §3.4 |
| 2 | Delivery = ACK + adapter-owned poll enqueue, own `video_generation_polling` task type | `DeepResearchPort`'s shape doesn't fit binary payloads; its polling handler is dead code anyway (verified by grep) — §3.4 |
| 3 | v1 = `generate_video` + `edit_video` only; reference-to-video and extension deferred | Owner decision 2026-08-23, same minimal-then-extend pattern as image editing's original single-reference decision — §2.2 |
| 4 | Duration/resolution/aspect_ratio **inferred by the LLM**, not fixed defaults | Owner decision 2026-08-23, accepting variable per-request cost for more natural UX — §2.2, §3.8 |
| 5 | Delivery = GCS link only, no native `file_upload` | Owner decision 2026-08-23: sidesteps Telegram's 50MB bot document cap and matches image generation's existing precedent — §3.4 |
| 6 | `edit_video` uses context key `video_ref`, single reference (not an array) | Mirrors image RFC #3/#6's original single-reference decision; no known multi-video-edit xAI capability to justify more — §3.9 |
| 7 | Tier `PERFORMANCE` set in `_DEFAULT_AGENT_TIERS` from day one | Closes the exact gap `decisions/agent_tier_default_enforcement.md` found for three prior agents — §3.7 |
| 8 | `allowed_providers: ["grok"]` only, no fallback, on both axes | No second video-gen adapter exists; matches image RFC decision #8 |

---

## 7. Prompt Work (Phase 2 of playbook)

`COGNITIVE_PROCESS_VIDEO_GEN` token structure (Groovy source, uploaded as JSON):

```groovy
identity: "You are a video-generation prompt specialist. You receive a creative brief
           (from the orchestrator, already clarified with the user) and produce a
           technically precise prompt plus generation parameters for
           grok-imagine-video-1.5 (Aurora)."

motion_framework: [
    "subject_motion: what the main subject does — action verbs, pacing, emotion.",
    "background_motion: what the environment does independent of the subject
     (weather, crowd, background elements) — omit if static.",
    "camera_motion: pan / zoom / tracking / static — name it explicitly, do not
     leave camera behavior implicit.",
]

edit_mode_rule: "For edit_video tasks: stay surgical. Translate the instruction precisely —
                 do NOT add creative elaboration the user did not ask for."

parameter_inference: "Infer duration (1-15s), resolution (480p/720p/1080p), and aspect_ratio
                       from what the brief implies. Default to the shortest duration and
                       lowest resolution that satisfies the request when the brief gives no
                       signal either way — do not pad toward expensive defaults."

output_format: "Return JSON: {video_prompt, duration, resolution, aspect_ratio}. video_prompt
                is the ONLY field shown to xAI — everything it needs must be in that string."
```

Upload order (dev then prod, per playbook §Step 9 — **human-executed only**, never by AI):
```bash
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_VIDEO_GEN --format json
python firestore_utils/upload.py development_domain_prompt_blueprints_v3 video_generation_agent_v1 --format json
python firestore_utils/upload.py development_domain_prompt_profiles_v3 video_generation --format json
```

---

## 8. Cost Impact

| Metric | Value |
|---|---|
| Video generation | $0.08/sec confirmed at 480p; 720p/1080p figures unconfirmed against xAI directly (§10) |
| Video edit | Presumed same per-second-of-output billing — unconfirmed, no separate edit pricing found |
| Example | A 6s/480p clip ≈ $0.48; a naive 15s/1080p clip could reach the ~$3.75+ range on unconfirmed pricing — an order of magnitude above a single image ($0.04-0.08) |
| Extra LLM call per request | 1 short structured-output call (brief → prompt + params), cheap/fast |
| Owner accepted variable cost | Duration/resolution are LLM-inferred (§2.2 decision #4), not fixed — cost varies per request by design |

---

## 9. Test Strategy

### Unit (`tests/unit/agents/test_video_generation_agent.py`)
1. `can_handle` — correct intents, wrong intent, empty query
2. `execute` `generate_video` happy path — structured LLM call → port `.create_video()` → ACK response, no `delivery_items`
3. `execute` `generate_video` with `image_ref` — resolved via `FileConversionService.resolve_bytes()` (not coordinator auto-injection)
4. `execute` `edit_video` happy path — `video_ref` resolved via `resolve_bytes()`, port `.edit_video()` called with bytes
5. `edit_video` missing `video_ref` — failure response
6. Prompt builder failure — `AgentResponse.failure()`, no silent fallback
7. Malformed/invalid structured JSON from crafting call — failure response, no `create_video()` call with garbage params
8. Port `create_video()`/`edit_video()` raises — failure response

### Adapter wire tests (`tests/unit/adapters/test_grok_video_adapter.py`)
Mock at the `AsyncOpenAI` SDK boundary + the `httpx` download boundary, not the port. Cover:
`create_video()`/`edit_video()` request shape, `get_status()` for all four statuses (`pending`/
`done`/`failed`/`expired`), video-bytes download on `done`, `task_queue.enqueue_video_generation_polling`
called exactly once per submission (not per poll).

### Worker handler tests (`tests/unit/handlers/test_worker_handler.py`)
`_handle_video_generation_polling`: `pending` → re-enqueue with delay; `done` → `deliver_video()`
called, no re-enqueue; `failed`/`expired` → `notification.notify()` called, no re-enqueue;
max-attempts exhaustion → timeout notification, matching `_handle_deep_research_polling`'s shape.

### Contract (`tests/contracts/adapter_contracts.py`)
New `ContractRule` for `VideoGenerationPort`.

---

## 10. Open Questions / Follow-ups (not blocking, but not silently assumed)

1. **720p/1080p pricing and the $0.01/input-image figure are third-party-sourced only** (OpenRouter,
   Vercel AI Gateway) — not confirmed on docs.x.ai directly. Confirm against a live billing event
   or xAI's pricing page before quoting exact cost to users.
2. **Exact output `mime_type`.** Assumed `video/mp4` (industry-standard default for this class of
   API) — unconfirmed against a live response. Same posture as image RFC's open question #1: read
   it off the response if the SDK exposes it, don't hardcode blind.
3. **Poll interval / max attempts.** Proposing ~25-30s interval, ~20-24 attempts (~10-12 min
   budget) as a starting point — xAI's own "up to several minutes" is vague. Tune after first live
   latency measurements, same caution image RFC gave its own timeout values (§10 there).
4. **Edit pricing.** No separate `/v1/videos/edits` pricing was found distinct from generation —
   assumed same per-second-of-output billing. Confirm before shipping `edit_video` broadly.
5. **`create_video()`/`edit_video()` signature growth if reference-to-video/extension are added
   later.** Both share the same request/poll shape (§2.2), so extension is additive — but confirm
   the exact reference-image/audio wire format against a live call before implementing, same as
   image RFC's own resolved-later open question (#2 there) for multi-reference images.

---

## 11. Implementation Order

1. `src/ports/video_generation_port.py` — port + `VideoPollResult`
2. `src/ports/task_queue.py` + `src/adapters/gcp_task_queue.py` — `enqueue_video_generation_polling`
3. `src/adapters/grok_video_adapter.py` — adapter + wire tests
4. `src/infrastructure/agent_manifest.py` — Intents + `AgentDescriptor`
5. `src/infrastructure/agent_config.py` — `VideoGenerationAgentConfig`
6. `src/domain/user.py` — `_DEFAULT_AGENT_TIERS["video_generation"]` (do this now, not after the fact)
7. `src/services/agent_context_builder.py` — strategy entry + `resolve_video_generation_context()`
8. `src/services/video_generation_delivery.py` — `deliver_video()`
9. `src/services/task_dispatch_service.py` — `enqueue_video_generation_polling` wrapper
10. `src/agents/video_generation_agent.py` — agent + unit tests
11. `src/handlers/worker_handler.py` — `video_registry` param + `_handle_video_generation_polling`
12. `src/composition/user_agent_factory.py` + bootstrap — `video_registry` wiring
13. `src/utils/capabilities.py` — user-facing capability entry
14. Prompt tokens (§7) — human uploads dev, validate, then prod
15. `PROTOCOL_SMART_AGENT_SELECTION` update — human upload
16. `make test-unit` + `make test-e2e-all`
17. Manual spot-check in Slack/Telegram — both intents, verify ACK arrives immediately and the
    finished video link arrives minutes later in the same channel, check
    `_on_agent_start`/`_on_agent_success` logs plus the poll loop's own logging
18. `make deploy`
