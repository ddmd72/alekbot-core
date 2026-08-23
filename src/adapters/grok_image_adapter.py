"""
Grok Image Adapter (xAI)
=========================

Adapter for xAI's Grok Imagine Image API using the OpenAI-compatible SDK against
xAI's REST image endpoints (/v1/images/generations, /v1/images/edits).

Independent from GrokAdapter (src/adapters/grok_adapter.py) on purpose: that adapter
implements LLMPort against xAI's /v1/responses (chat) surface — a structurally
different API. This adapter implements ImageGenerationPort and constructs its own
AsyncOpenAI client rather than sharing GrokAdapter's — see
docs/10_rfcs/IMAGE_GENERATION_RFC.md §3.6.

mime_type is currently hardcoded to "image/png" — xAI's docs do not state the
returned format explicitly (RFC §10, open question #1). Confirm against a live
response before this ships broadly; if it differs, read it off the SDK response
instead of hardcoding.

edit() does NOT use the SDK's images.edit() convenience method. That method always
sends multipart/form-data (the OpenAI API's own convention for file uploads) — xAI's
/v1/images/edits rejects that outright (confirmed live in production 2026-08-21:
HTTP 415, "Expected request with Content-Type: application/json") and its own docs
state plainly that "The OpenAI SDK's images.edit() method is not supported." xAI
wants a plain JSON body with the reference image as {"url": <data-uri-or-real-url>,
"type": "image_url"}. We get there via AsyncOpenAI.post() — the SDK's own low-level
escape hatch for non-standard-shaped endpoints (the same method images.edit()/
images.generate() call internally under their typed wrappers), which sends JSON when
called with body= and no files=, reusing the client's configured auth/timeout/base_url.

edit() supports 1-3 reference images (xAI's hard cap, confirmed against
docs.x.ai/developers/rest-api-reference/inference/images): exactly 1 uses the
singular "image": {...} field; 2 or 3 use a DIFFERENT, plural "images": [...] field
(mutually exclusive with "image", not an array under the same key).
"""
import base64
from typing import List

from openai import AsyncOpenAI
from openai.types.images_response import ImagesResponse

from ..ports.image_generation_port import GeneratedImage, ImageGenerationPort, ReferenceImage
from ..utils.logger import logger

_MODEL = "grok-imagine-image-2.0"


class GrokImageAdapter(ImageGenerationPort):
    """Implements ImageGenerationPort via xAI's images.generate / images.edit."""

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self._client = AsyncOpenAI(
            base_url="https://api.x.ai/v1",
            api_key=self.api_key,
            # Explicit timeout/retries — the SDK defaults (600s, max_retries=2) would
            # silently undermine ImageGenerationAgent.RETRY_POLICY = NO_RETRY_POLICY
            # (set specifically to avoid double-billing xAI on retry, since a
            # transient 5xx can arrive AFTER xAI has already rendered and billed for
            # an image). max_retries=0 pushes retry policy fully up to the agent
            # layer, where NO_RETRY_POLICY actually applies. 300.0s — raised from an
            # initial 120.0s after a live generate() call measured >120s and hit
            # that ceiling in production 2026-08-21 (openai.APITimeoutError). Still
            # not a fully measured value — the true upper bound for
            # grok-imagine-image-2.0 is unknown (RFC §10 open question #3) — but
            # now backed by one real data point instead of a guess.
            timeout=300.0,
            max_retries=0,
        )

    async def generate(
        self, prompt: str, *, aspect_ratio: str = "auto", n: int = 1,
        resolution: str = "1k", quality: str = "medium",
    ) -> List[GeneratedImage]:
        try:
            response = await self._client.images.generate(
                model=_MODEL,
                prompt=prompt,
                n=n,
                response_format="b64_json",
                # resolution/quality previously unset entirely — output size/quality
                # rested on xAI's own unstated server default. Explicit now, and
                # settable per request by the agent (see
                # docs/10_rfcs/VIDEO_GENERATION_RFC.md §3.11 for the sibling video
                # decision this mirrors — an explicit orchestrator signal, not LLM
                # inference). Pricing is flat regardless of either value (confirmed
                # on docs.x.ai/developers/pricing) — this is about determinism and
                # user control, NOT cost tier selection.
                extra_body={"aspect_ratio": aspect_ratio, "resolution": resolution, "quality": quality},
            )
            return [
                GeneratedImage(data=base64.b64decode(item.b64_json), mime_type="image/png")
                for item in response.data
            ]
        except Exception as e:
            # Decode is inside this try on purpose: the port contract promises []
            # on any failure, including a malformed response (None data / None
            # b64_json), not just an SDK-raised exception.
            logger.error("GrokImageAdapter.generate failed: %s: %s", type(e).__name__, e, exc_info=True)
            return []

    async def edit(self, prompt: str, reference_images: List[ReferenceImage]) -> GeneratedImage:
        # Defensive backstop — the agent validates first, but a port implementation
        # shouldn't blindly trust its caller at a system boundary.
        if not 1 <= len(reference_images) <= 3:
            raise ValueError(
                f"edit() accepts 1-3 reference images (xAI's hard cap), got {len(reference_images)}"
            )

        # xAI's JSON shape for a reference image: {"url": <data-uri-or-real-url>,
        # "type": "image_url"} — same wrapper for a base64 data URI as for a real
        # public URL (confirmed against docs.x.ai/developers/rest-api-reference/
        # inference/images). A single reference uses the singular "image" field
        # (unchanged from the original single-image implementation); 2 or 3 use
        # the plural "images" array instead — a different field, not "image" as
        # an array — the two are mutually exclusive per xAI's schema.
        images = [
            {
                "url": f"data:{ref.mime_type};base64,{base64.b64encode(ref.data).decode('ascii')}",
                "type": "image_url",
            }
            for ref in reference_images
        ]

        body = {
            "model": _MODEL,
            "prompt": prompt,
            "response_format": "b64_json",
        }
        if len(images) == 1:
            body["image"] = images[0]
        else:
            body["images"] = images

        response = await self._client.post(
            "/images/edits",
            cast_to=ImagesResponse,
            body=body,
        )
        item = response.data[0]
        return GeneratedImage(data=base64.b64decode(item.b64_json), mime_type="image/png")
