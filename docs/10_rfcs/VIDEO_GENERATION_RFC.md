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
User (interactive): describes the video; Smart clarifies subject/motion/mood if vague,
same commissioning pattern as image generation. Duration/resolution are NOT something
Smart proactively solicits — they only enter context if the user stated them unprompted
(§3.11 decision #9)
  |
  v Smart -> delegate_to_specialist(intent="generate_video", query="<brief>",
             context={"image_ref": "<optional starting-image filename>",
                      "duration": "<optional, only if user stated one>",
                      "resolution": "<optional, only if user stated one>"})
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
      |     |     brief (+ image presence) -> {video_prompt, aspect_ratio}. Duration/resolution
      |     |     are NOT inferred by this call — see §3.11, decision #9 (superseding the
      |     |     original 2026-08-23 "LLM infers duration/resolution" call).
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
bounded by `dispatch_deadline_s` (520s, per `agent_manifest.py:591` — safely inside the Cloud Tasks
hard ceiling of 1800s).
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

**All three port methods (`create_video`, `edit_video`, `get_status`) go through
`AsyncOpenAI.post(..., cast_to=dict)` — raw JSON — never the SDK's typed `.videos.create()` /
`.videos.retrieve()` / `.videos.edit()`.** Verified directly against installed `openai==2.53.0`:
those typed methods are hardcoded to **OpenAI's own Sora-2 shape**, not xAI's — `create()` forces
`multipart/form-data` and Sora-only fields (`input_reference`, `seconds` as an enum of `{4,8,12}`,
`size` as a `"720x1280"`-style string) with no `duration`/`resolution`/`aspect_ratio`/`image`
fields at all; the response model's `status` is `Literal["queued","in_progress","completed",
"failed"]` — missing xAI's own `"pending"`/`"done"`/`"expired"` values entirely — and it carries no
`url` field (Sora content is fetched via a separate `download_content()` call, not inline). Live
`docs.x.ai` confirms this independently: every code sample for `/v1/videos/*` uses xAI's own
`xai_sdk`, plain `requests`, or curl — the OpenAI-compatible SDK is not mentioned anywhere for
video. This is the same class of incompatibility already hit and fixed for `GrokImageAdapter.edit()`
(HTTP 415 in production, `images.edit()` sends multipart when xAI wants JSON — see that file's
docstring) — here it applies to all three operations, not just one, so the low-level `.post()`
escape hatch is the adapter's only call path, not a special case for one method.

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
crafting call still needs **structured fields** — `video_prompt`, `aspect_ratio` — because
`aspect_ratio` (unlike duration/resolution, see §3.11) is still worth inferring from the brief and
freeform text has no natural place to carry it. This is Mode 1 from `NEW_AGENT_PLAYBOOK.md`
(single-pass JSON, no custom tools) — `response_schema` + `response_mime_type`, both enforced on
the locked `grok` provider (Grok honors `response_schema` via
`text.format={"type":"json_schema",...}` since 2026-08-14, per `src/adapters/CLAUDE.md`). This is
a real divergence from `ImageGenerationAgent`'s pattern, called out explicitly per CLAUDE.md's
delta-declaration gate rather than silently copied.

**`duration` and `resolution` were originally part of this schema too** (owner decision
2026-08-23) — **superseded same-day** after a cost-safeguard review: see §3.11 decision #9. They
are no longer LLM outputs at all; the crafting call's job narrowed to "what to render," not "how
long/how big."

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

### 3.11 Cost Safeguards (added 2026-08-23, post cost-safeguard review — supersedes original decision #4)

Video is an order-of-magnitude cost jump over anything else in the system ($0.08/sec confirmed;
a naive 15s request ≈ $1.20 at minimum, more if resolution turns out to carry a premium after
all). The original design (LLM freely infers duration/resolution from the brief's "vibe") put a
real money decision behind LLM judgment with only a prompt instruction as a backstop — not
acceptable. Three changes, decided together:

**Decision #9 — Fixed default (5s / 480p), overridden only by an explicit orchestrator signal.**
`VideoGenerationAgent` no longer asks the crafting LLM to infer duration/resolution (§3.8). It
resolves `duration = context.get("duration") or DEFAULT_VIDEO_DURATION_S` (5) and
`resolution = context.get("resolution") or DEFAULT_VIDEO_RESOLUTION` ("480p"). `context.duration`/
`context.resolution` are new optional keys on **`generate_video`'s `context_schema` only** (§4) —
`edit_video`'s port method (§3.5) takes no `duration`/`resolution` at all, since editing preserves
the source video's existing length/resolution by definition, nothing to default or override.
Populated by **Smart only when the user explicitly stated a specific length or quality** ("10 second video",
"in 1080p") — never inferred from subject matter or implied mood. `PROTOCOL_SMART_AGENT_SELECTION`'s
`generate_video`/`edit_video` section (§7) must say this explicitly: *"Only pass context.duration /
context.resolution when the user literally specified a duration or resolution. Do not infer these
from the brief — the specialist defaults to 5s/480p on its own."* This moves the cost-bearing
decision out of LLM inference (crafting-call *or* orchestrator-call) into a deterministic default
plus an explicit, attributable override.

**Decision #10 — Hard cap on `duration`, 10s system default, per-user overridable.** Even an
explicit user ask is clamped: `duration = min(resolved_duration, self.max_duration_s)`.
`max_duration_s` follows the existing per-user-override convention exactly (`UserBotConfig.
semantic_search_limit` / `.biographical_cache_limit`, `src/domain/user.py:219,228-229` — `Optional[
int] = None` on `UserBotConfig`, resolved USER → ACCOUNT → SYSTEM by a new `ConfigurationService.
get_max_video_duration()`, mirroring `get_semantic_search_limit()` at `configuration_service.py:
260`). System default lives as a constant (`DEFAULT_MAX_VIDEO_DURATION_S = 10`), resolved once at
agent-construction time in `UserAgentFactory._create_and_cache_agents()` and passed into
`VideoGenerationAgent`'s constructor — same pattern as `history_recent_full_turns`, not
re-resolved per request. New field: `UserBotConfig.max_video_duration_s: Optional[int] = None`.
No Cabinet UI at v1 (matches the existing precedent for `semantic_search_limit` etc. — Firestore-
only, no `/api/user/...` route); add one later if a non-owner user ever needs self-service access.
**No equivalent cap on `resolution`**: xAI's own pricing page states "$0.080 per second" with no
resolution differentiation (the $0.14/$0.25 tiered figures are third-party-only, already flagged
unconfirmed in §10 #1) — resolution does not appear to be a cost lever, so it stays governed by
decision #9 alone (default 480p unless explicitly requested), no separate ceiling.

**Decision #11 — External-cost billing, for image generation too, not just video.** Confirmed by
direct inspection: `ImageGenerationAgent` calls `self._image_port.generate()`/`.edit()` directly,
never through `BaseAgent._call_llm()` — the only path that feeds `TokenLedger`
(`base_agent.py:1126-1130`). `GrokImageAdapter` never touches a billing port. `daily_cost_limit`
(`billing.py:107`) sums `TokenLedger.cost()` only — token-derived. **Result: every dollar of xAI
image spend has been completely invisible to the account's own cost alert since the image agent
shipped 2026-08-21; video would inherit the identical blind spot at $0.08/sec instead of
$0.04-0.08/image.** Fix, both media types, same mechanism:
- `QuotaService.record_usage` / `AccountRepository.increment_account_usage` already accept a
  `cost: float` param independent of token count (`ports/quota_service.py:10`,
  `ports/account_repository.py:26`) — it is simply never called with a non-ledger-derived value
  today. Both agents already hold `self._quota_service` (set uniformly by `UserAgentFactory`,
  `user_agent_factory.py:527`) — no new wiring needed to call it from the agent itself.
- New `calculate_external_cost()` helper alongside `calculate_cost()` in `billing.py`, next to
  `_PRICING_PER_MILLION_TOKENS` — a small pricing table for non-token services
  (`grok-imagine-image-2.0`, `grok-imagine-video-1.5`), same "price can change under you, keep it
  in one place" reasoning CLAUDE.md's Economics section already applies to LLM pricing.
- **Video**: cost = `resolved_duration_s * 0.08` — deterministic, since duration is now always
  either the default or a known, clamped override (decision #9/#10). Recorded where the actual
  delivered duration is known: `deliver_video()` (§3.4 step 4), mirroring `deliver_deep_research()`'s
  shape but adding one `record_usage()` call it doesn't have.
- **Image — companion fix to already-shipped code, not new-component scope.** Recording a cost is
  meaningless while the actual billed tier is unknown: `GrokImageAdapter.generate()`/`.edit()`
  currently send no `size`/`quality` field at all (`grok_image_adapter.py:70-84,107-129`), so xAI's
  server-side default — and therefore whether each call is billed $0.04 or $0.08 — is unconfirmed.
  Fix: pin an explicit tier in the adapter call (proposed: 1K/low = $0.04, the cheaper of the two,
  consistent with this RFC's general "don't pad toward expensive defaults" posture), then record
  that fixed cost via `self._quota_service.record_usage(...)` in `_respond_with_images()`
  (`image_generation_agent.py:235-282`). Touches `src/adapters/grok_image_adapter.py` and
  `src/agents/image_generation_agent.py` — both already in production, so this ships as a small
  patch alongside the video RFC, not as new files in §4/§5.

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
| `AgentDescriptor` | `src/infrastructure/agent_manifest.py` | `eager=False`, `ExecutionMode.SYNC` both intents, `context_schemas={EDIT_VIDEO: {"video_ref": ...}, GENERATE_VIDEO: {"image_ref": ... (optional), "duration": ... (optional), "resolution": ... (optional)}}` — `duration`/`resolution` are `generate_video`-only (§3.11 decision #9; `edit_video`'s port signature takes neither), Smart-populated only on explicit user ask. |
| `UserBotConfig.max_video_duration_s` | `src/domain/user.py` | New `Optional[int] = None` field, mirrors `semantic_search_limit` convention. §3.11 decision #10. |
| `ConfigurationService.get_max_video_duration()` | `src/services/configuration_service.py` | New resolver, mirrors `get_semantic_search_limit()`. USER → ACCOUNT → SYSTEM (`DEFAULT_MAX_VIDEO_DURATION_S = 10`). §3.11 decision #10. |
| `calculate_external_cost()` | `src/domain/billing.py` | New helper + pricing table for non-token services (image + video). §3.11 decision #11. |
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
  PROTOCOL_SMART_AGENT_SELECTION   add generate_video/edit_video section, incl. §3.11 decision #9
                                    wording on when to pass context.duration/context.resolution

Companion fix — cost safeguards (§3.11), touches already-shipped code, not new files:
  src/domain/user.py                          + UserBotConfig.max_video_duration_s
  src/services/configuration_service.py       + get_max_video_duration()
  src/domain/billing.py                       + calculate_external_cost() + pricing table
  src/agents/video_generation_agent.py        duration/resolution default+clamp logic (§3.11 #9/#10)
  src/services/video_generation_delivery.py   + record_usage() call for video cost (§3.11 #11)
  src/agents/image_generation_agent.py        + record_usage() call for image cost (§3.11 #11)
  src/adapters/grok_image_adapter.py          pin explicit size/quality tier (§3.11 #11)
  tests/unit/domain/test_user.py              max_video_duration_s coverage
  tests/unit/services/test_configuration_service.py   get_max_video_duration() coverage
  tests/unit/domain/test_billing.py           calculate_external_cost() coverage
```

---

## 6. Design Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | `VideoGenerationAgent` is `ExecutionMode.SYNC`, not `ASYNC` | The slow part (waiting on xAI) lives entirely outside `execute()`; no remaining reason to pay for a dedicated Cloud Task dispatch — §3.4 |
| 2 | Delivery = ACK + adapter-owned poll enqueue, own `video_generation_polling` task type | `DeepResearchPort`'s shape doesn't fit binary payloads; its polling handler is dead code anyway (verified by grep) — §3.4 |
| 3 | v1 = `generate_video` + `edit_video` only; reference-to-video and extension deferred | Owner decision 2026-08-23, same minimal-then-extend pattern as image editing's original single-reference decision — §2.2 |
| 4 | **[SUPERSEDED 2026-08-23, same day]** ~~Duration/resolution/aspect_ratio inferred by the LLM, not fixed defaults~~ | Reversed after cost-safeguard review — a real money decision behind bare LLM judgment with only a prompt instruction as backstop was not acceptable. Replaced by decisions #9-#11 — §3.11 |
| 5 | Delivery = GCS link only, no native `file_upload` | Owner decision 2026-08-23: sidesteps Telegram's 50MB bot document cap and matches image generation's existing precedent — §3.4 |
| 6 | `edit_video` uses context key `video_ref`, single reference (not an array) | Mirrors image RFC #3/#6's original single-reference decision; no known multi-video-edit xAI capability to justify more — §3.9 |
| 7 | Tier `PERFORMANCE` set in `_DEFAULT_AGENT_TIERS` from day one | Closes the exact gap `decisions/agent_tier_default_enforcement.md` found for three prior agents — §3.7 |
| 8 | `allowed_providers: ["grok"]` only, no fallback, on both axes | No second video-gen adapter exists; matches image RFC decision #8 |
| 9 | Duration/resolution default to **5s / 480p**; changed only by an explicit orchestrator signal, never inferred from the brief's vibe | Owner decision 2026-08-23 (cost-safeguard review): moves the cost-bearing choice out of LLM judgment into a deterministic default + attributable override — §3.11 |
| 10 | Hard cap on `duration`: **10s system default, per-user overridable** via `UserBotConfig.max_video_duration_s` | Owner decision 2026-08-23: bounds worst-case spend even on an explicit user ask; no equivalent `resolution` cap since xAI's confirmed pricing is duration-only — §3.11 |
| 11 | External (non-token) cost billing added for **both** image and video generation, via `QuotaService.record_usage(cost=...)` | Owner decision 2026-08-23: image-gen spend has been invisible to `daily_cost_limit` since it shipped 2026-08-21; video would inherit the same blind spot at higher stakes — §3.11 |

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

output_format: "Return JSON: {video_prompt, aspect_ratio}. video_prompt is the ONLY field shown
                to xAI — everything it needs must be in that string. Duration and resolution
                are NOT yours to decide — the orchestrator resolves them before this brief
                reaches you (default 5s/480p unless the user explicitly asked for something
                else). Do not infer or suggest a duration or resolution in video_prompt."
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
| Video generation | $0.08/sec confirmed, and per xAI's own pricing page not resolution-differentiated (§10, §3.11 #10) |
| Video edit | Presumed same per-second-of-output billing — unconfirmed, no separate edit pricing found |
| Default request | 5s/480p (§3.11 #9) ≈ **$0.40** |
| Worst case, system default | 10s hard cap (§3.11 #10) ≈ **$0.80** per request — down from an unbounded ~$1.20+ under the original LLM-inferred design (up to 15s, xAI's own max) |
| Worst case, per-user override raised | Bounded only by whatever `UserBotConfig.max_video_duration_s` that specific user is granted — still an explicit, attributable ceiling, not open-ended inference |
| Extra LLM call per request | 1 short structured-output call (brief → prompt + aspect_ratio only, §3.8), cheap/fast |
| Cost now visible | Both image and video spend flow into `daily_cost_limit` for the first time (§3.11 #11) — previously $0 recorded regardless of actual xAI spend |

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
9. No `context.duration`/`context.resolution` given — `create_video()` called with 5s/480p defaults (§3.11 #9)
10. `context.duration=8` explicitly given, under the cap — port called with 8s, not clamped
11. `context.duration=14` explicitly given, over `max_duration_s` — port called with the clamped value, not 14 (§3.11 #10)
12. Per-user `max_duration_s` override (e.g. 20) raises the effective ceiling — a `context.duration=14` request is NOT clamped when the user's own override exceeds it

### Billing (`tests/unit/domain/test_billing.py`, `tests/unit/services/test_configuration_service.py`)
13. `calculate_external_cost()` — video: `duration_s * 0.08`; image: fixed pinned-tier constant (§3.11 #11)
14. `get_max_video_duration()` — USER → ACCOUNT → SYSTEM resolution, mirrors existing `get_semantic_search_limit()` test shape

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
   image RFC's own resolved-later open question (#2 there) for multi-reference images. One concrete
   gotcha already confirmed on `docs.x.ai`: the two reference arrays use **inconsistent indexing**
   in the prompt-tag convention — `reference_images` are tagged `<IMAGE_1>`/`<IMAGE_2>`/`<IMAGE_3>`
   (one-indexed), `reference_audios` are tagged `<AUDIO_0>`/`<AUDIO_1>`/`<AUDIO_2>` (zero-indexed).
   Don't assume symmetric indexing when this gets built.

---

## 11. Implementation Order

1. `src/ports/video_generation_port.py` — port + `VideoPollResult`
2. `src/ports/task_queue.py` + `src/adapters/gcp_task_queue.py` — `enqueue_video_generation_polling`
3. `src/adapters/grok_video_adapter.py` — adapter + wire tests (all three methods via raw
   `AsyncOpenAI.post()`, §3.5)
4. `src/infrastructure/agent_manifest.py` — Intents + `AgentDescriptor` (context_schemas incl.
   optional `duration`/`resolution`, §3.11 #9)
5. `src/infrastructure/agent_config.py` — `VideoGenerationAgentConfig`
6. `src/domain/user.py` — `_DEFAULT_AGENT_TIERS["video_generation"]` (do this now, not after the
   fact) + `UserBotConfig.max_video_duration_s` (§3.11 #10)
7. `src/services/configuration_service.py` — `get_max_video_duration()` (§3.11 #10)
8. `src/domain/billing.py` — `calculate_external_cost()` + pricing table for image/video (§3.11 #11)
9. `src/services/agent_context_builder.py` — strategy entry + `resolve_video_generation_context()`
10. `src/services/video_generation_delivery.py` — `deliver_video()`, incl. `record_usage()` call
    for video cost (§3.11 #11)
11. `src/services/task_dispatch_service.py` — `enqueue_video_generation_polling` wrapper
12. `src/agents/video_generation_agent.py` — agent incl. duration/resolution default+clamp logic
    (§3.11 #9/#10) + unit tests
13. `src/handlers/worker_handler.py` — `video_registry` param + `_handle_video_generation_polling`
14. `src/composition/user_agent_factory.py` + bootstrap — `video_registry` wiring; resolve
    `max_video_duration_s` once at agent-construction time and pass into `VideoGenerationAgent`
    (§3.11 #10, same pattern as `history_recent_full_turns`)
15. **Companion fix, already-shipped code:** `src/agents/image_generation_agent.py` +
    `src/adapters/grok_image_adapter.py` — pin explicit size/quality tier + `record_usage()` call
    for image cost (§3.11 #11)
16. `src/utils/capabilities.py` — user-facing capability entry
17. Prompt tokens (§7) — human uploads dev, validate, then prod
18. `PROTOCOL_SMART_AGENT_SELECTION` update — human upload, incl. §3.11 #9 wording on when Smart
    may pass `context.duration`/`context.resolution`
19. `make test-unit` + `make test-e2e-all`
20. Manual spot-check in Slack/Telegram — both intents, verify ACK arrives immediately and the
    finished video link arrives minutes later in the same channel, check
    `_on_agent_start`/`_on_agent_success` logs plus the poll loop's own logging; confirm an
    unqualified "make me a video of X" actually renders at 5s/480p, not longer
21. `make deploy`
