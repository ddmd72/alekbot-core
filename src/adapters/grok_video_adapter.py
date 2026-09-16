"""
Grok Video Adapter (xAI)
=========================

Adapter for xAI's Grok Imagine Video API (/v1/videos/*) via the OpenAI-compatible
SDK's low-level AsyncOpenAI.post()/.get() escape hatch — NEVER the SDK's typed
`.videos.*` methods (client.videos.create/retrieve/edit). Those are hardcoded to
OpenAI's own Sora-2 API shape: multipart/form-data, Sora-only fields
(input_reference, seconds as an enum of {4,8,12}, size as a "720x1280"-style
string), and a response model whose `status` is
Literal["queued","in_progress","completed","failed"] — missing xAI's own
"pending"/"done"/"expired" values entirely, with no `url` field at all (Sora
content is fetched via a separate download_content() call). Confirmed directly
against installed openai==2.53.0 and against docs.x.ai, whose own code samples use
xai_sdk/requests/curl — never the OpenAI SDK. See
docs/10_rfcs/VIDEO_GENERATION_RFC.md §3.5. All three methods below use
cast_to=object, the SDK's own zero-validation passthrough (confirmed in
openai._base_client.BaseClient._process_response_data: `if cast_to is object:
return data` — no pydantic construction, so an unmodeled xAI field shape can never
raise a validation error here).

Independent from GrokImageAdapter/GrokAdapter on purpose — same no-cross-adapter-
coupling convention as IMAGE_GENERATION_RFC.md §3.6: a structurally different API
surface, own AsyncOpenAI client construction, no shared imports.

get_status() downloads the finished video's bytes via a plain httpx.AsyncClient GET
against the URL xAI returns — that URL is a CDN link, NOT under api.x.ai's
base_url/auth scope, so it cannot go through self._client.post()/.get(). There is
no existing httpx.AsyncClient convention elsewhere in src/adapters/ (confirmed by
grep) — this is a new, narrowly-scoped pattern: one GET, no pooling/retry needed
since VideoGenerationAgent.RETRY_POLICY is NO_RETRY_POLICY. A failed download (CDN
blip, expired URL, xAI 5xx) is caught here and surfaces as a "failed" poll result —
WorkerHandler._handle_video_generation_polling also wraps the whole get_status()
call as a belt-and-suspenders backstop for any other unexpected exception.
"""
import base64
from typing import Optional

import httpx
from openai import AsyncOpenAI

from ..ports.task_queue import TaskQueue
from ..ports.video_generation_port import VideoGenerationPort, VideoPollResult
from ..utils.logger import logger

_MODEL = "grok-imagine-video-1.5"
# edit_video uses a DIFFERENT model id than create_video — evidenced live 2026-08-24:
# a real edit_video call with _MODEL got a 400 "Video editing is not supported for
# this model." docs.x.ai's REST reference example body for /v1/videos/edits uses
# "grok-imagine-video" (no version suffix), while the /v1/videos/generations example
# uses "grok-imagine-video-1.5". HYPOTHESIS, not confirmed against a live edit_video
# call yet (docs.x.ai examples are informal, not a strict spec) — do not "fix" this
# back to matching _MODEL without re-checking against a real xAI response first.
_EDIT_MODEL = "grok-imagine-video"
_DEFAULT_DURATION_S = 5


class GrokVideoAdapter(VideoGenerationPort):
    """Implements VideoGenerationPort via xAI's /v1/videos/* REST surface."""

    def __init__(self, api_key: str, task_queue: TaskQueue):
        self.api_key = api_key.strip()
        self._client = AsyncOpenAI(
            base_url="https://api.x.ai/v1",
            api_key=self.api_key,
            # Submission itself should be fast (xAI queues the job and returns
            # request_id immediately, per docs.x.ai) — 60s is generous for a
            # submit call, not a render-wait. max_retries=0: retry policy lives
            # entirely at the agent layer (NO_RETRY_POLICY) — a transient 5xx must
            # never trigger a second paid render via the SDK's own retry loop.
            timeout=60.0,
            max_retries=0,
        )
        self._task_queue = task_queue

    async def create_video(
        self, prompt: str, user_id: str, account_id: str, *,
        image_data: Optional[bytes] = None, image_mime_type: str = "image/png",
        duration: Optional[int] = None, resolution: Optional[str] = None,
        aspect_ratio: Optional[str] = None, session_id: Optional[str] = None,
        origin_platform: Optional[str] = None,
    ) -> str:
        body = {"model": _MODEL, "prompt": prompt}
        if duration is not None:
            body["duration"] = duration
        if resolution is not None:
            body["resolution"] = resolution
        if aspect_ratio is not None:
            body["aspect_ratio"] = aspect_ratio
        if image_data is not None:
            b64 = base64.b64encode(image_data).decode("ascii")
            # xAI's REST schema wants {"url": "<data-uri or public URL>", "file_id": null},
            # not a bare string — confirmed 2026-08-24 against a live 422 ("expected a map")
            # and against docs.x.ai/developers/rest-api-reference/inference/videos.
            body["image"] = {"url": f"data:{image_mime_type};base64,{b64}"}

        response = await self._client.post("/videos/generations", cast_to=object, body=body)
        request_id = response.get("request_id")
        if not request_id:
            raise RuntimeError(f"xAI response missing request_id: {response!r}")

        # Logged BEFORE enqueue: xAI has already accepted (and will bill) the render
        # at this point — if enqueue itself raises, the request_id must still survive
        # in logs for manual recovery.
        logger.info("[GrokVideoAdapter] create_video submitted: request_id=%s", request_id[:16])
        await self._task_queue.enqueue_video_generation_polling(
            request_id=request_id, user_id=user_id, account_id=account_id,
            session_id=session_id or "", duration_s=duration if duration is not None else _DEFAULT_DURATION_S,
            origin_platform=origin_platform,
        )
        return request_id

    async def edit_video(
        self, prompt: str, video_data: bytes, user_id: str, account_id: str, *,
        video_mime_type: str = "video/mp4", session_id: Optional[str] = None,
        origin_platform: Optional[str] = None,
    ) -> str:
        b64 = base64.b64encode(video_data).decode("ascii")
        body = {
            "model": _EDIT_MODEL,
            "prompt": prompt,
            # Same {"url": ...} wrapping as create_video's "image" field — same xAI
            # REST convention, confirmed against the same live 422 + docs page.
            "video": {"url": f"data:{video_mime_type};base64,{b64}"},
        }

        response = await self._client.post("/videos/edits", cast_to=object, body=body)
        request_id = response.get("request_id")
        if not request_id:
            raise RuntimeError(f"xAI response missing request_id: {response!r}")

        # Logged BEFORE enqueue — see create_video's comment for why.
        logger.info("[GrokVideoAdapter] edit_video submitted: request_id=%s", request_id[:16])
        # No duration_s passed: edit_video has no duration param (editing preserves
        # the source's own length, which the adapter can't know without probing the
        # file) — the port's own default applies here. The REAL duration is picked up
        # from xAI's "done" response in get_status() below and used at delivery time,
        # which is the actual fix for edit_video's billing accuracy.
        await self._task_queue.enqueue_video_generation_polling(
            request_id=request_id, user_id=user_id, account_id=account_id,
            session_id=session_id or "", origin_platform=origin_platform,
        )
        return request_id

    async def get_status(self, request_id: str) -> VideoPollResult:
        response = await self._client.get(f"/videos/{request_id}", cast_to=object)
        status = response.get("status", "failed")

        if status == "done":
            video = response.get("video", {})
            url = video.get("url")
            duration_s = video.get("duration")
            if not url:
                return VideoPollResult(status="failed", error="xAI reported done with no video.url")
            try:
                async with httpx.AsyncClient(timeout=60.0) as http:
                    download = await http.get(url)
                    download.raise_for_status()
            except Exception as exc:
                # CDN blip, expired URL, or xAI 5xx on download — a clean "failed"
                # poll result instead of raising out of get_status() (WorkerHandler
                # also guards this call as a backstop, but this is the primary guard).
                return VideoPollResult(status="failed", error=f"video download failed: {exc}")
            return VideoPollResult(status="done", data=download.content, duration_s=duration_s)

        if status == "failed":
            error = response.get("error", {})
            message = error.get("message", "unknown error") if isinstance(error, dict) else str(error)
            return VideoPollResult(status="failed", error=message)

        # "pending" or "expired" pass through as-is — nothing to fetch yet.
        return VideoPollResult(status=status)
